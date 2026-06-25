"""Receiver for AiDock SkillHub webhook events.

This is the Hermes-side ingress that AiDock SkillHub (王克杰's side) posts to when
a skill is approved, updated, revoked, etc. The job of this module is narrow and
honest: **validate, deduplicate, and persist** inbound events. Actual skill
materialization — downloading the zip, installing the canonical release into the
router managed store, creating the per-profile symlink, classifying credentials,
and calling back AiDock with the result — is the next phase. Events land here with
``status="queued"`` for a downstream worker to pick up.

Two payload shapes are accepted and normalized to one internal shape:

* **王克杰's shipped shape** (flat)::

      {"event_type": "skill.install_approved", "skill_code": "daily-breaking",
       "release_id": "rel_001", "version": "1.0.3", "download_url": "https://...",
       "skill_status": "active", "audience": {"auth_type": "all", "users": [{"profile_id": "profile-xxx"}]}}

* **PRD §9.5 full envelope** (nested ``skill`` / ``release.package`` / ``audience.type``).

No token, cookie, or secret is ever expected, stored, or logged. State co-resides
with the rest of multitenancy in ``~/.hermes/multitenancy.db``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = Path.home() / ".hermes" / "multitenancy.db"

#: Event types AiDock may send (PRD §9.5). Unknown types are still accepted and
#: stored — forward-compatibility beats rejecting an event we'd lose otherwise —
#: but they are flagged via ``status="queued_unknown_type"`` for visibility.
KNOWN_EVENT_TYPES = frozenset(
    {
        "skill.install_approved",
        "skill.status_changed",
        "skill.auth_type_changed",
        "skill.permission_approved",
        "skill.updated",
        "skill.revoked",
        "skill.permission_changed",
        "skill.rollback_requested",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skillhub_events (
    event_id           TEXT PRIMARY KEY,
    event_type         TEXT NOT NULL,
    skill_code         TEXT NOT NULL,
    release_id         TEXT,
    version            TEXT,
    download_url       TEXT,
    checksum_sha256    TEXT,
    skill_status       TEXT,
    auth_type          TEXT,
    audience_json      TEXT,
    raw_payload        TEXT,
    results_json       TEXT,
    status             TEXT NOT NULL,
    signature_verified INTEGER NOT NULL DEFAULT 0,
    received_at        INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skillhub_events_skill
    ON skillhub_events(skill_code, release_id);
CREATE INDEX IF NOT EXISTS idx_skillhub_events_status
    ON skillhub_events(status, received_at);
"""


class SkillhubEventError(ValueError):
    """Raised when an inbound payload cannot be normalized into an event.

    Carries a stable ``error_code`` so the HTTP layer can map it to the
    AiDock callback error-code vocabulary (PRD §9.6).
    """

    def __init__(self, message: str, *, error_code: str = "INVALID_PAYLOAD") -> None:
        super().__init__(message)
        self.error_code = error_code


def _first_str(*values: Any) -> Optional[str]:
    """Return the first non-empty string among values, else None."""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def synthesize_event_id(skill_code: str, release_id: Optional[str], raw_body: str) -> str:
    """Deterministic fallback event_id when AiDock omits one.

    王克杰's current payload has no ``event_id`` field, but idempotency needs a
    stable key. Hash of the raw body makes identical re-posts collapse to the
    same id (true duplicate), while a changed body yields a new id.
    """
    digest = hashlib.sha256(raw_body.encode("utf-8")).hexdigest()[:16]
    return f"{skill_code}:{release_id or 'norel'}:{digest}"


def _normalize_users(audience: dict[str, Any]) -> list[dict[str, Any]]:
    users_raw = audience.get("users")
    if not isinstance(users_raw, list):
        return []
    users: list[dict[str, Any]] = []
    for entry in users_raw:
        if not isinstance(entry, dict):
            continue
        normalized = {
            "profile_id": _first_str(entry.get("profile_id")),
            "employee_id": _first_str(entry.get("employee_id")),
            "open_id": _first_str(entry.get("open_id")),
        }
        # Drop all-empty entries; keep any with at least one identifier.
        if any(normalized.values()):
            users.append({k: v for k, v in normalized.items() if v is not None})
    return users


def normalize_event(payload: dict[str, Any], *, raw_body: str) -> dict[str, Any]:
    """Validate and flatten an inbound payload (either shape) into one event dict.

    Raises ``SkillhubEventError`` on a payload we cannot act on (missing
    event_type or skill_code).
    """
    if not isinstance(payload, dict):
        raise SkillhubEventError("payload must be a JSON object", error_code="INVALID_PAYLOAD")

    skill_block = payload.get("skill") if isinstance(payload.get("skill"), dict) else {}
    release_block = payload.get("release") if isinstance(payload.get("release"), dict) else {}
    package_block = (
        release_block.get("package") if isinstance(release_block.get("package"), dict) else {}
    )
    audience = payload.get("audience") if isinstance(payload.get("audience"), dict) else {}

    event_type = _first_str(payload.get("event_type"))
    if not event_type:
        raise SkillhubEventError("event_type is required", error_code="INVALID_PAYLOAD")

    skill_code = _first_str(payload.get("skill_code"), skill_block.get("skill_code"))
    if not skill_code:
        raise SkillhubEventError("skill_code is required", error_code="INVALID_PAYLOAD")

    release_id = _first_str(payload.get("release_id"), release_block.get("release_id"))
    version = _first_str(payload.get("version"), release_block.get("version"))
    download_url = _first_str(
        payload.get("download_url"), package_block.get("download_url")
    )
    checksum = _first_str(
        payload.get("checksum_sha256"), package_block.get("checksum_sha256")
    )
    skill_status = _first_str(payload.get("skill_status"), skill_block.get("status")) or "active"
    # 王克杰 uses audience.auth_type (all|auth); PRD uses audience.type (users|...).
    auth_type = _first_str(audience.get("auth_type"), audience.get("type")) or "auth"
    users = _normalize_users(audience)

    event_id = _first_str(payload.get("event_id"))
    if not event_id:
        event_id = synthesize_event_id(skill_code, release_id, raw_body)

    return {
        "event_id": event_id,
        "event_type": event_type,
        "skill_code": skill_code,
        "release_id": release_id,
        "version": version,
        "download_url": download_url,
        "checksum_sha256": checksum,
        "skill_status": skill_status,
        "auth_type": auth_type,
        "audience": {"auth_type": auth_type, "users": users},
        "known_type": event_type in KNOWN_EVENT_TYPES,
    }


def signature_for(secret: str, timestamp: str, event_id: str, raw_body: str) -> str:
    """Compute the expected ``sha256=<hex>`` signature (PRD §9.5 construction).

    payload_to_sign = timestamp + "." + event_id + "." + raw_body
    """
    payload_to_sign = f"{timestamp}.{event_id}.{raw_body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload_to_sign, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(
    secret: str, timestamp: str, event_id: str, raw_body: str, provided: str
) -> bool:
    """Constant-time compare of provided vs expected signature."""
    expected = signature_for(secret, timestamp, event_id, raw_body)
    return hmac.compare_digest(expected, str(provided or "").strip())


class SkillhubEventStore:
    """Persistent inbound-event log with event_id idempotency."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = str(db_path) if db_path is not None else str(DEFAULT_DB_PATH)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript("PRAGMA journal_mode=WAL;")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            try:
                self._conn.execute("ALTER TABLE skillhub_events ADD COLUMN results_json TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
            self._conn.commit()
        # The connection is shared (check_same_thread=False) across the aiohttp
        # event loop and the cron worker thread; serialize writes.

    def record(
        self,
        event: dict[str, Any],
        *,
        raw_payload: str,
        signature_verified: bool,
    ) -> tuple[str, bool]:
        """Insert a normalized event. Returns ``(status, duplicate)``.

        A second call with the same ``event_id`` is a no-op insert and returns
        ``duplicate=True`` — this is the idempotency guarantee AiDock relies on
        for safe webhook retries.
        """
        event_id = event["event_id"]
        status = "queued" if event.get("known_type", False) else "queued_unknown_type"
        now = int(time.time())
        with self._lock:
            # Atomic idempotency: INSERT OR IGNORE collapses a duplicate event_id
            # to a no-op (rowcount 0) instead of racing SELECT-then-INSERT into an
            # IntegrityError under concurrent access.
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO skillhub_events"
                " (event_id, event_type, skill_code, release_id, version, download_url,"
                "  checksum_sha256, skill_status, auth_type, audience_json, raw_payload,"
                "  status, signature_verified, received_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    event["event_type"],
                    event["skill_code"],
                    event.get("release_id"),
                    event.get("version"),
                    event.get("download_url"),
                    event.get("checksum_sha256"),
                    event.get("skill_status"),
                    event.get("auth_type"),
                    json.dumps(event.get("audience") or {}, ensure_ascii=False),
                    raw_payload,
                    status,
                    1 if signature_verified else 0,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                row = self._conn.execute(
                    "SELECT status FROM skillhub_events WHERE event_id = ?", (event_id,)
                ).fetchone()
                return (str(row["status"]) if row is not None else status), True
            return status, False

    def get(self, event_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM skillhub_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM skillhub_events").fetchone()[0])

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM skillhub_events ORDER BY received_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]

    def list_queued(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM skillhub_events"
            " WHERE status IN ('queued', 'queued_unknown_type')"
            " ORDER BY received_at ASC, event_id ASC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]

    def mark_installed(self, event_id: str, results: dict[str, Any]) -> bool:
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE skillhub_events"
                " SET status = ?, results_json = ?, updated_at = ?"
                " WHERE event_id = ?",
                ("installed", json.dumps(results, ensure_ascii=False), now, event_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def mark_failed(self, event_id: str, error_code: str, message: str) -> bool:
        now = int(time.time())
        payload = {
            "error_code": str(error_code),
            "message": str(message),
            "failed_at": now,
        }
        with self._lock:
            cur = self._conn.execute(
                "UPDATE skillhub_events"
                " SET status = ?, results_json = ?, updated_at = ?"
                " WHERE event_id = ?",
                ("failed", json.dumps(payload, ensure_ascii=False), now, event_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def close(self) -> None:
        self._conn.close()


# --- module-level singleton (mirrors router._get_session_store / override) ---

_event_store: Any = None
_event_db_path: Optional[str] = None


def get_event_store() -> SkillhubEventStore:
    global _event_store
    if _event_store is None:
        _event_store = SkillhubEventStore(_event_db_path)
    return _event_store


def override_event_store(store_or_path: Any) -> None:
    """Test/seam hook: set the singleton to a store or a path (e.g. ``:memory:``)."""
    global _event_store, _event_db_path
    if _event_store is not None and _event_store is not store_or_path:
        try:
            _event_store.close()
        except Exception:
            pass
    if store_or_path is None or isinstance(store_or_path, (str, Path)):
        _event_db_path = str(store_or_path) if store_or_path is not None else None
        _event_store = None
    else:
        _event_store = store_or_path
