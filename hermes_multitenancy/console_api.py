"""Hermes Console M1a — read-only fleet aggregation endpoints (data layer).

Four GET endpoints under ``/api/run-broker/console/*``:

* ``overview``            — broker liveness, skillhub queue counts, reauth-pending
                            count, active tenants by kind.
* ``profiles?q=``         — paged search over ``multitenancy_routing`` (SQLite).
* ``profiles/{name}``     — one profile: routing row + redacted connector status
                            + cron jobs + recent error categories.
* ``reauth-pending``      — fleet list of ``.needs_reauth`` markers.

Contract (SPEC console-m1a-endpoints):

* Read-only. SQLite is opened ``mode=ro``; nothing here writes any store.
* The request path never walks all profile homes. The only directory scan
  (``.needs_reauth`` markers) runs at most once per ``_REAUTH_TTL_S`` (>= 10
  minutes) and is memoized; responses expose ``cache_age_s`` to prove a hit.
* Recent errors expose category/timestamp/platform only — never message
  content (the audit line's ``content`` field is deliberately not read).
* Auth: the existing run-broker master bearer (``_authorized``). No new auth
  model; fleet-level RBAC (union_id allowlist) lives in the WebUI BFF.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .webui_broker.periphery import _authorized, _shared_home_from_env

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".hermes" / "multitenancy.db"

def _int_env(name: str, default: int) -> int:
    """Tolerant int env parse — garbage falls back to the default, never raises."""
    try:
        return int(str(os.environ.get(name, "") or default).strip())
    except (TypeError, ValueError):
        return default


# Floor of 600s is a hard SPEC constraint (mechanical-disk red line): the env
# override can lengthen the TTL but never shorten it below 10 minutes.
_REAUTH_TTL_S = max(600, _int_env("HERMES_CONSOLE_REAUTH_TTL_S", 600))

_MARKER_MAX_BYTES = 4096  # a .needs_reauth marker is a tiny json; bigger = dirty
_MARKER_REASON_MAX_CHARS = 200

_AUDIT_TAIL_MAX_BYTES = 1_048_576  # bounded tail read of conversation-audit.jsonl
_RECENT_ERRORS_LIMIT = 20
_PROFILES_MAX_LIMIT = 100

_STARTED_AT = time.time()
_reauth_cache: dict[str, Any] = {"ts": 0.0, "items": []}
# Miss-lock: on TTL expiry, exactly ONE thread performs the marker scan while
# concurrent requests wait and then serve the refreshed cache — without it, a
# burst of duplicate requests at expiry would each walk profiles/*/feishu_uat
# (the mechanical-disk red line this cache exists to protect).
_reauth_lock = threading.Lock()

# Only these routing columns ever leave the API. An allowlist (rather than
# ``SELECT *``) so future columns holding anything sensitive stay internal.
_ROUTING_FIELDS = (
    "user_id",
    "profile_name",
    "open_id",
    "union_id",
    "kind",
    "active",
    "chat_id",
    "owner_open_id",
    "display_label",
    "last_active_at",
    "synced_at",
    "provenance",
)


# ---------------------------------------------------------------- helpers --


def _db_path() -> Path:
    raw = os.environ.get("HERMES_MULTITENANCY_DB", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_DB_PATH


def _connect_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    """Read-only sqlite connection; ``None`` when the DB is absent/unreadable."""
    if not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _project_routing_row(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    out = {field: row[field] for field in _ROUTING_FIELDS if field in keys}
    # SPEC pins the public field name as ``profile`` (the storage column is
    # ``profile_name``) — rename at the projection boundary.
    if "profile_name" in out:
        out["profile"] = out.pop("profile_name")
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# A canonical profile name is what the sync path ever produces — alphanumerics
# plus `_ . -` (real samples: feishu_group_1a10…, codex_harden_175624, test-group,
# multitenancy_router, 123). Anything else (path separators, `..`, control chars,
# over-length) is rejected BEFORE the name reaches a filesystem-touching reader
# (connector status at <shared>/profiles/<name>). Belt-and-suspenders vs a dirty
# routing row — the SQL is already parameterized; this guards the FS path. The
# BFF/shell reuses the same ruler. (hardening A7, Forge/codex 2026-07-08)
_CANONICAL_PROFILE = re.compile(r"\A[A-Za-z0-9_.-]{1,128}\Z")


def _is_canonical_profile_name(name: str) -> bool:
    if not _CANONICAL_PROFILE.match(name or ""):
        return False
    return ".." not in name  # defense in depth: no traversal even within the charset


def _like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ------------------------------------------------------------- aggregates --


def overview_snapshot(
    db_path: Optional[Path] = None,
    shared_home: Optional[Path] = None,
) -> dict[str, Any]:
    db = db_path or _db_path()

    active_by_kind: dict[str, int] = {}
    skillhub: dict[str, int] = {"queued": 0, "failed": 0, "installed": 0, "total": 0}
    conn = _connect_ro(db)
    if conn is not None:
        try:
            if _has_table(conn, "multitenancy_routing"):
                for row in conn.execute(
                    "SELECT kind, COUNT(*) AS n FROM multitenancy_routing"
                    " WHERE active = 1 GROUP BY kind"
                ):
                    active_by_kind[str(row["kind"] or "unknown")] = int(row["n"])
            if _has_table(conn, "skillhub_events"):
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM skillhub_events GROUP BY status"
                ):
                    status = str(row["status"] or "")
                    count = int(row["n"])
                    skillhub["total"] += count
                    if status in ("queued", "queued_unknown_type"):
                        skillhub["queued"] += count
                    elif status == "failed":
                        skillhub["failed"] += count
                    elif status == "installed":
                        skillhub["installed"] += count
        except sqlite3.Error:
            logger.exception("[console] overview sqlite read failed")
        finally:
            conn.close()

    reauth = reauth_pending_snapshot(shared_home=shared_home)
    return {
        "broker": {"alive": True, "uptime_s": round(time.time() - _STARTED_AT, 1)},
        "gateway_alive": True,  # run-broker runs inside the gateway process
        "active": active_by_kind,
        "skillhub": skillhub,
        "reauth_pending_count": len(reauth["items"]),
        "cache_age_s": reauth["cache_age_s"],
        "generated_at": _now_iso(),
    }


def agents_for_owner(
    owner_open_id: str,
    *,
    limit: int = 100,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Agent-kind routing rows owned by ``owner_open_id``.

    The developer self-view needs "my agents" — an OWNER-column query, not the
    text search of ``search_profiles`` (which matches display_label/open_id/… but
    NOT owner_open_id, so ``q=<open_id>`` would never surface the agents a user
    owns). The BFF derives ``owner_open_id`` from the session and passes it here;
    this function trusts its (master-key-gated) caller.
    """
    limit = min(max(int(limit), 1), _PROFILES_MAX_LIMIT)
    result: dict[str, Any] = {"owner_open_id": owner_open_id, "items": [], "available": False}
    if not owner_open_id:
        result["available"] = True  # a valid query for "no owner" is simply empty
        return result
    conn = _connect_ro(db_path or _db_path())
    if conn is None:
        return result
    try:
        if not _has_table(conn, "multitenancy_routing"):
            return result
        rows = conn.execute(
            "SELECT * FROM multitenancy_routing"
            " WHERE kind = 'agent' AND owner_open_id = ?"
            " ORDER BY (last_active_at IS NULL), last_active_at DESC, user_id"
            " LIMIT ?",
            (str(owner_open_id), limit),
        ).fetchall()
        result["items"] = [_project_routing_row(row) for row in rows]
        result["available"] = True
        return result
    except sqlite3.Error:
        logger.exception("[console] agents_for_owner read failed")
        return result
    finally:
        conn.close()


def search_profiles(
    q: str = "",
    *,
    limit: int = 20,
    offset: int = 0,
    db_path: Optional[Path] = None,
) -> dict[str, Any]:
    limit = min(max(int(limit), 1), _PROFILES_MAX_LIMIT)
    offset = max(int(offset), 0)
    result: dict[str, Any] = {"items": [], "total": 0, "limit": limit, "offset": offset}

    conn = _connect_ro(db_path or _db_path())
    if conn is None:
        result["available"] = False
        return result
    try:
        if not _has_table(conn, "multitenancy_routing"):
            result["available"] = False
            return result
        where = ""
        params: list[Any] = []
        term = str(q or "").strip()[:128]  # clamp: multi-KB patterns buy nothing but LIKE cost
        if term:
            pattern = f"%{_like_escape(term)}%"
            match_cols = ("display_label", "profile_name", "open_id", "union_id", "chat_id", "user_id")
            where = " WHERE " + " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for col in match_cols)
            params = [pattern] * len(match_cols)
        result["total"] = int(
            conn.execute(f"SELECT COUNT(*) FROM multitenancy_routing{where}", params).fetchone()[0]
        )
        rows = conn.execute(
            "SELECT * FROM multitenancy_routing"
            f"{where} ORDER BY (last_active_at IS NULL), last_active_at DESC, user_id"
            " LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        result["items"] = [_project_routing_row(row) for row in rows]
        result["available"] = True
        return result
    except sqlite3.Error:
        logger.exception("[console] profile search failed")
        result["available"] = False
        return result
    finally:
        conn.close()


def profile_detail(
    profile_name: str,
    *,
    db_path: Optional[Path] = None,
    shared_home: Optional[Path] = None,
    audit_path: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Aggregate one profile; ``None`` when no routing row exists (-> 404).

    Rejects non-canonical names up front (path traversal / control chars /
    over-length) so a dirty routing row can never steer the downstream
    connector reader outside the profiles root.
    """
    if not _is_canonical_profile_name(str(profile_name)):
        return None
    conn = _connect_ro(db_path or _db_path())
    if conn is None:
        return None
    try:
        if not _has_table(conn, "multitenancy_routing"):
            return None
        rows = conn.execute(
            "SELECT * FROM multitenancy_routing WHERE profile_name = ?"
            " ORDER BY (last_active_at IS NULL), last_active_at DESC",
            (str(profile_name),),
        ).fetchall()
    except sqlite3.Error:
        logger.exception("[console] profile detail sqlite read failed")
        return None
    finally:
        conn.close()
    if not rows:
        return None

    primary = _project_routing_row(rows[0])
    detail: dict[str, Any] = {
        "profile_name": profile_name,
        "routing": primary,
        "routing_rows": len(rows),
    }

    # Connector status: reuse the registry's redacted, short-TTL-cached reader.
    open_id = str(primary.get("open_id") or primary.get("owner_open_id") or "")
    try:
        from .connectors import registry

        statuses = registry.collect_connector_statuses(
            profile_name=str(profile_name),
            open_id=open_id,
            shared_home=shared_home,
            use_cache=True,
        )
        detail["connectors"] = [status.to_dict() for status in statuses]
    except Exception as exc:
        logger.exception("[console] connector status failed for %s", profile_name)
        detail["connectors"] = []
        detail["connectors_error"] = type(exc).__name__

    # Cron jobs for this profile (existing per-profile API).
    try:
        from . import cron_api

        detail["cron_jobs"] = cron_api.list_jobs(str(profile_name), include_disabled=True)
    except Exception as exc:
        detail["cron_jobs"] = []
        detail["cron_error"] = type(exc).__name__

    detail["recent_errors"] = recent_errors_for_profile(
        str(profile_name), audit_path=audit_path
    )
    return detail


def recent_errors_for_profile(
    profile_name: str,
    *,
    audit_path: Optional[Path] = None,
    limit: int = _RECENT_ERRORS_LIMIT,
) -> list[dict[str, Any]]:
    """Category/timestamp-only error rows from a BOUNDED audit-file tail.

    Reads at most ``_AUDIT_TAIL_MAX_BYTES`` from the end of the file, never
    the whole log; emits no message content. Profile matching applies the
    same embedded-id scrub the writer applies, so redacted rows still match.
    """
    if audit_path is None:
        from .conversation_audit import conversation_audit_path

        audit_path = conversation_audit_path()
    path = Path(audit_path)
    if not path.exists():
        return []

    try:
        from .security_audit import _redact_embedded_ids

        target = _redact_embedded_ids(str(profile_name or ""))
    except Exception:
        target = str(profile_name or "")

    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _AUDIT_TAIL_MAX_BYTES))
            data = fh.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if size > _AUDIT_TAIL_MAX_BYTES and lines:
        lines = lines[1:]  # drop the partial first line of the window

    errors: list[dict[str, Any]] = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            continue
        if str(row.get("profile") or "") not in (target, str(profile_name)):
            continue
        finish = str(row.get("finish_reason") or "").strip()
        if not finish or finish == "stop":
            continue
        errors.append(
            {
                "ts": row.get("@timestamp"),
                "category": finish,
                "platform": row.get("platform") or "",
            }
        )
    return errors[-limit:]


def reauth_pending_snapshot(
    shared_home: Optional[Path] = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Fleet ``.needs_reauth`` list, memoized for ``_REAUTH_TTL_S`` seconds.

    The profiles/*/feishu_uat glob is the only directory walk in this module
    and it runs ONLY here, only on cache expiry — the mechanical-disk red line.
    """
    def _cached(now: float) -> Optional[dict[str, Any]]:
        age = now - float(_reauth_cache["ts"] or 0.0)
        if not force and _reauth_cache["ts"] and age < _REAUTH_TTL_S:
            return {"items": list(_reauth_cache["items"]), "cache_age_s": round(age, 1)}
        return None

    hit = _cached(time.time())
    if hit is not None:
        return hit

    with _reauth_lock:
        # Double-check under the lock: a concurrent request may have refreshed
        # while we waited — serve its result instead of scanning again.
        hit = _cached(time.time())
        if hit is not None:
            return hit

        home = Path(shared_home) if shared_home is not None else _shared_home_from_env()
        items = _scan_reauth_markers(home)
        _reauth_cache["ts"] = time.time()
        _reauth_cache["items"] = items
        return {"items": list(items), "cache_age_s": 0.0}


def _scan_reauth_markers(home: Path) -> list[dict[str, Any]]:
    """The one directory walk in this module — call only under ``_reauth_lock``."""
    items: list[dict[str, Any]] = []
    profiles_dir = home / "profiles"
    if profiles_dir.is_dir():
        try:
            for marker in sorted(profiles_dir.glob("*/feishu_uat/*.needs_reauth")):
                entry: dict[str, Any] = {
                    "profile": marker.parent.parent.name,
                    "open_id": marker.name[: -len(".needs_reauth")].removesuffix(".json"),
                }
                try:
                    if marker.stat().st_size > _MARKER_MAX_BYTES:
                        raise ValueError("oversized marker")
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                    entry["reason"] = str(payload.get("reason") or "")[:_MARKER_REASON_MAX_CHARS]
                    ts = payload.get("ts")
                    entry["ts"] = ts if isinstance(ts, (int, float)) else None
                except (OSError, ValueError):
                    entry["reason"] = "unreadable_marker"
                items.append(entry)
        except OSError:
            logger.exception("[console] reauth marker scan failed")
    return items


# --------------------------------------------------------------- handlers --


def register_console_routes(app) -> None:
    """Attach the read-only console endpoints to the run-broker app."""
    from aiohttp import web  # noqa: F401  (import parity with the host module)

    app.router.add_get("/api/run-broker/console/overview", handle_console_overview)
    app.router.add_get("/api/run-broker/console/profiles", handle_console_profiles)
    app.router.add_get(
        "/api/run-broker/console/profiles/{profile_name}", handle_console_profile_detail
    )
    app.router.add_get(
        "/api/run-broker/console/reauth-pending", handle_console_reauth_pending
    )
    app.router.add_get("/api/run-broker/console/agents", handle_console_agents)


async def handle_console_overview(request):
    from aiohttp import web

    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        snapshot = await asyncio.to_thread(overview_snapshot)
        return web.json_response(snapshot)
    except Exception as exc:
        logger.exception("[console] overview failed")
        return web.json_response({"error": str(exc)}, status=500)


async def handle_console_agents(request):
    from aiohttp import web

    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    owner = str(request.query.get("owner") or "").strip()
    try:
        result = await asyncio.to_thread(agents_for_owner, owner)
        return web.json_response(result)
    except Exception as exc:
        logger.exception("[console] agents failed")
        return web.json_response({"error": str(exc)}, status=500)


async def handle_console_profiles(request):
    from aiohttp import web

    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        try:
            limit = int(request.query.get("limit", "20"))
            offset = int(request.query.get("offset", "0"))
        except ValueError:
            return web.json_response({"error": "limit/offset must be integers"}, status=400)
        result = await asyncio.to_thread(
            search_profiles,
            str(request.query.get("q") or ""),
            limit=limit,
            offset=offset,
        )
        return web.json_response(result)
    except Exception as exc:
        logger.exception("[console] profile search failed")
        return web.json_response({"error": str(exc)}, status=500)


async def handle_console_profile_detail(request):
    from aiohttp import web

    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    profile_name = str(request.match_info.get("profile_name") or "").strip()
    if not profile_name:
        return web.json_response({"error": "profile_name required"}, status=400)
    try:
        detail = await asyncio.to_thread(profile_detail, profile_name)
        if detail is None:
            return web.json_response({"error": "profile not found"}, status=404)
        return web.json_response(detail)
    except Exception as exc:
        logger.exception("[console] profile detail failed")
        return web.json_response({"error": str(exc)}, status=500)


async def handle_console_reauth_pending(request):
    from aiohttp import web

    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        snapshot = await asyncio.to_thread(reauth_pending_snapshot)
        snapshot["count"] = len(snapshot["items"])
        return web.json_response(snapshot)
    except Exception as exc:
        logger.exception("[console] reauth-pending failed")
        return web.json_response({"error": str(exc)}, status=500)
