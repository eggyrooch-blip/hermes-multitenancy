"""L3 — `.needs_reauth` marker → Feishu bot DM notifier.

A background thread watches the marker directories (legacy + per-profile),
debouncing by (open_id, reason) with a 24h dedupe window. When a fresh marker
appears, the notifier looks up the right recipient open_id:

  * Most reasons → DM the end-user whose UAT broke
  * ``scope_stripped_by_feishu`` → DM **owner** instead (app-config bug; the
    end user can't fix it by re-authorizing)

The bot identity is the tenant_access_token minted from the global
``feishu_app`` vault row. We do not block startup if the vault is empty — the
notifier just disables itself and logs a warning.

Recipient resolution for owner is read from the env var
``HERMES_CREDENTIAL_RENEWAL_OWNER_OPEN_ID`` (set in ``~/.hermes/.env``). If
absent, scope_stripped notifications are skipped and logged loudly instead of
misrouting an app-config issue to the end-user.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional

from .credential_renewal_common import (
    REASON_SCOPE_STRIPPED_BY_FEISHU,
    is_fixture_path,
    read_needs_reauth_marker,
)
from .credentials import CredentialStore

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 30
_DEDUPE_TTL_SECONDS = 24 * 3600
# Freshness gate: only DM for markers written within this window. Stops a first
# live-send enable from blasting a stale backlog (e.g. the 1100+ markers all
# stamped 2026-06-09 by an internal "encryption key not ready" event, NOT real
# Feishu rejections). A user who is genuinely still broken re-triggers a FRESH
# marker on their next attempt, which passes this gate. 0 disables the gate.
_DEFAULT_MAX_MARKER_AGE_SECONDS = 24 * 3600


def _resolve_max_marker_age() -> int:
    raw = (os.environ.get("HERMES_CREDENTIAL_REAUTH_NOTIFIER_MAX_AGE_SECONDS") or "").strip()
    if not raw:
        return _DEFAULT_MAX_MARKER_AGE_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_MAX_MARKER_AGE_SECONDS

_worker_lock = threading.Lock()
_worker_thread: Optional[threading.Thread] = None
_worker_stop: Optional[threading.Event] = None


def ensure_reauth_notifier_started(shared_home: Path, *, interval_seconds: int = _DEFAULT_INTERVAL_SECONDS) -> None:
    """Idempotent start — safe to call multiple times from plugin register()."""
    global _worker_thread, _worker_stop
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_stop = threading.Event()
        _worker_thread = threading.Thread(
            target=_watcher_loop,
            args=(shared_home, interval_seconds, _worker_stop),
            daemon=True,
            name="credential-reauth-notifier",
        )
        _worker_thread.start()
        logger.info("[credential_reauth_notifier] watcher started interval=%ds", interval_seconds)


def stop_reauth_notifier() -> None:
    """For tests — stop the watcher thread cleanly."""
    global _worker_thread, _worker_stop
    with _worker_lock:
        if _worker_stop is not None:
            _worker_stop.set()
        if _worker_thread is not None:
            _worker_thread.join(timeout=5)
        _worker_thread = None
        _worker_stop = None


def _watcher_loop(shared_home: Path, interval_seconds: int, stop_event: threading.Event) -> None:
    state_path = shared_home / "credential_reauth_notifier_state.json"
    seen: dict[str, dict[str, Any]] = _load_state(state_path)
    while not stop_event.is_set():
        try:
            changed = _scan_once(shared_home, seen)
            if changed:
                _save_state(state_path, seen)
        except Exception:
            logger.exception("[credential_reauth_notifier] scan error")
        stop_event.wait(timeout=interval_seconds)
    logger.info("[credential_reauth_notifier] watcher stopped")


def _scan_once(shared_home: Path, seen: dict[str, dict[str, Any]]) -> bool:
    """One pass over marker files; returns True if state changed."""
    changed = False
    bot_token = _get_bot_token(shared_home)
    owner_open_id = (os.environ.get("HERMES_CREDENTIAL_RENEWAL_OWNER_OPEN_ID") or "").strip()
    send_enabled = (os.environ.get("HERMES_CREDENTIAL_REAUTH_NOTIFIER_SEND") or "").strip() == "1"
    max_marker_age = _resolve_max_marker_age()

    for marker in _iter_markers(shared_home):
        body = read_needs_reauth_marker(marker)
        if not body:
            continue
        open_id = _open_id_from_marker(marker)
        if not open_id:
            continue
        reason = str(body.get("reason") or "")
        ts = int(body.get("ts") or 0)
        key = f"{open_id}:{reason}"
        last = seen.get(key)
        # Dedupe must only count REAL sends. A dry-run `seen` entry (recorded while
        # SEND!=1) must NOT block the first live send after enabling — otherwise
        # flipping HERMES_CREDENTIAL_REAUTH_NOTIFIER_SEND=1 within 24h would send
        # nothing for markers already seen in dry-run.
        last_send = last if (last and not last.get("dry_run")) else None
        now = int(time.time())
        if max_marker_age and ts and (now - ts) > max_marker_age:
            # Stale marker (e.g. the 2026-06-09 internal-error backlog): don't DM
            # for a days-old rejection. Keep the marker + don't touch `seen` so a
            # later FRESH re-rejection still notifies.
            logger.debug(
                "[credential_reauth_notifier] skip stale marker open_id=%s reason=%s age=%ss (> %ss)",
                open_id, reason, now - ts, max_marker_age,
            )
            continue
        if last_send and now - int(last_send.get("notified_at", 0)) < _DEDUPE_TTL_SECONDS:
            # Within 24h dedupe window of a real send; skip even if same key.
            continue
        if last_send and ts and ts <= int(last_send.get("marker_ts", 0)):
            # Marker hasn't been refreshed since the last real send.
            continue

        recipient_open_id, message = _route_message(open_id, reason, owner_open_id, body)
        if not recipient_open_id:
            if reason == REASON_SCOPE_STRIPPED_BY_FEISHU and not owner_open_id:
                logger.error(
                    "[credential_reauth_notifier] scope_stripped marker for open_id=%s but "
                    "HERMES_CREDENTIAL_RENEWAL_OWNER_OPEN_ID is unset; cannot route — set it in ~/.hermes/.env",
                    open_id,
                )
            continue
        if not send_enabled:
            seen[key] = {
                "notified_at": now,
                "marker_ts": ts,
                "reason": reason,
                "dry_run": True,
                "recipient_open_id": recipient_open_id,
            }
            changed = True
            logger.warning(
                "[credential_reauth_notifier] dry-run suppressed DM reason=%s subject=%s recipient=%s; "
                "set HERMES_CREDENTIAL_REAUTH_NOTIFIER_SEND=1 to enable live sends",
                reason, open_id, recipient_open_id,
            )
            continue
        if bot_token is None:
            logger.warning(
                "[credential_reauth_notifier] would notify open_id=%s reason=%s but bot token unavailable",
                open_id, reason,
            )
            continue
        ok = _send_feishu_dm(bot_token, recipient_open_id, message)
        if ok:
            seen[key] = {
                "notified_at": now,
                "marker_ts": ts,
                "reason": reason,
                "dry_run": False,
                "recipient_open_id": recipient_open_id,
            }
            changed = True
            logger.info(
                "[credential_reauth_notifier] sent DM reason=%s subject=%s recipient=%s",
                reason, open_id, recipient_open_id,
            )
    return changed


def _iter_markers(shared_home: Path) -> Iterable[Path]:
    legacy = shared_home / "feishu_uat"
    if legacy.is_dir() and not is_fixture_path(legacy):
        for entry in legacy.iterdir():
            if entry.suffix == ".needs_reauth" or entry.name.endswith(".needs_reauth"):
                yield entry
    profiles = shared_home / "profiles"
    if profiles.is_dir():
        for profile_dir in profiles.iterdir():
            if not profile_dir.is_dir() or is_fixture_path(profile_dir):
                continue
            uat_dir = profile_dir / "feishu_uat"
            if not uat_dir.is_dir():
                continue
            for entry in uat_dir.iterdir():
                if entry.suffix == ".needs_reauth" or entry.name.endswith(".needs_reauth"):
                    yield entry


def _open_id_from_marker(marker: Path) -> str:
    """``ou_xxx.needs_reauth`` → ``ou_xxx``; ``ou_xxx.json.needs_reauth`` → ``ou_xxx``."""
    stem = marker.name[: -len(".needs_reauth")] if marker.name.endswith(".needs_reauth") else marker.stem
    if stem.endswith(".json"):
        stem = stem[: -len(".json")]
    return stem


def _route_message(
    subject_open_id: str,
    reason: str,
    owner_open_id: str,
    marker_body: dict[str, Any],
) -> tuple[str, str]:
    """Decide who to DM and what to say. Returns (recipient_open_id, message).

    For ``scope_stripped_by_feishu`` the recipient MUST be owner — the end user
    cannot fix this. If ``HERMES_CREDENTIAL_RENEWAL_OWNER_OPEN_ID`` is unset,
    return ``("", "")`` and the caller logs loudly + skips. We refuse to send a
    placebo message to the end user; the marker stays on disk and will deliver
    correctly once the env var is set.
    """
    detail = str(marker_body.get("detail") or "")
    if reason == REASON_SCOPE_STRIPPED_BY_FEISHU:
        if not owner_open_id:
            return "", ""
        msg = (
            "⚠️ Hermes 凭证守卫触发\n"
            f"用户 `{subject_open_id}` 的飞书 UAT 缺少 `offline_access` scope。\n"
            "原因：Feishu app 后台未勾选 `offline_access` 权限，"
            "导致颁发的 UAT 没有 refresh_token —— 用户重新授权一万次也救不了。\n"
            "请登录 https://open.feishu.cn → 你的 app → 权限管理 → 勾上 `offline_access` 并发布版本，"
            "然后让该用户重新 `/feishu_auth`。\n"
            f"明细：{detail}"
        )
        return owner_open_id, msg

    msg = (
        f"你的飞书授权已失效（{reason}），请在私聊中发送 `/feishu_auth` 重新授权，"
        "否则定时任务和需要你身份的操作将无法继续发送。"
    )
    return subject_open_id, msg


def _get_bot_token(shared_home: Path) -> Optional[str]:
    """Mint a fresh tenant_access_token from the global vault row."""
    try:
        store = CredentialStore(shared_home / "multitenancy.db")
    except Exception:
        logger.exception("[credential_reauth_notifier] vault unavailable")
        return None
    try:
        try:
            payload = store.get_secret_for_runtime(
                profile_name="__global__",
                subject_id="feishu_app",
                provider="feishu",
                secret_kind="app",
            )
        except Exception:
            return None
    finally:
        store.close()
    app_id = str(payload.get("app_id") or payload.get("FEISHU_APP_ID") or "").strip()
    app_secret = str(payload.get("app_secret") or payload.get("FEISHU_APP_SECRET") or "").strip()
    if not (app_id and app_secret):
        return None
    domain = str(payload.get("domain") or payload.get("FEISHU_DOMAIN") or "feishu").strip().lower()
    host = "open.larksuite.com" if domain == "larksuite" else "open.feishu.cn"
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    req = urllib.request.Request(
        f"https://{host}/open-apis/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        logger.exception("[credential_reauth_notifier] tenant token mint failed")
        return None
    if int(data.get("code") or 0) != 0:
        logger.warning("[credential_reauth_notifier] tenant token rejected code=%s", data.get("code"))
        return None
    token = str(data.get("tenant_access_token") or data.get("app_access_token") or "").strip()
    return token or None


def _send_feishu_dm(token: str, recipient_open_id: str, text: str) -> bool:
    """Send a text message via Feishu Open API. Returns True on success."""
    body = {
        "receive_id": recipient_open_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.warning(
            "[credential_reauth_notifier] DM HTTP %s for %s: %s",
            exc.code, recipient_open_id, exc.read()[:200].decode("utf-8", errors="replace"),
        )
        return False
    except Exception:
        logger.exception("[credential_reauth_notifier] DM send error for %s", recipient_open_id)
        return False
    if int(data.get("code") or 0) != 0:
        logger.warning(
            "[credential_reauth_notifier] DM rejected code=%s msg=%s recipient=%s",
            data.get("code"), data.get("msg"), recipient_open_id,
        )
        return False
    return True


def _load_state(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, dict):
            cleaned[key] = value
    return cleaned


def _save_state(path: Path, seen: dict[str, dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        logger.exception("[credential_reauth_notifier] state save failed at %s", path)
