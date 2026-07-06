"""WebUI-triggered Feishu UAT authorization sessions.

This module keeps the WebUI side out of raw token handling.  The browser asks
the WebUI BFF, the BFF calls the router-owned broker with the signed WebUI
identity, and this module stores successful UAT results in the multitenancy
credential vault plus the profile-local compatibility JSON.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Optional

from .credentials import CredentialStore
from .credential_renewal_common import (
    REASON_EMPTY_REFRESH_TOKEN,
    REASON_SCOPE_STRIPPED_BY_FEISHU,
    marker_path_for_open_id,
    payload_has_offline_access,
    payload_has_refresh_token,
    write_needs_reauth_marker,
)

logger = logging.getLogger(__name__)


class FeishuUatAuthError(Exception):
    def __init__(self, message: str, *, status: int = 400, refresh_class: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.refresh_class = refresh_class


LARK_REFRESH_ERROR = MappingProxyType(
    {
        "TOKEN_INVALID": 99991668,
        "REFRESH_TOKEN_INVALID": 20026,
        "REFRESH_TOKEN_EXPIRED": 20037,
        "REFRESH_TOKEN_REVOKED": 20064,
        "REFRESH_TOKEN_ALREADY_USED": 20073,
        "REFRESH_SERVER_ERROR": 20050,
    }
)
REFRESH_RETRYABLE_CODES: frozenset[int] = frozenset({LARK_REFRESH_ERROR["REFRESH_SERVER_ERROR"]})
REFRESH_INVALID_CODES: frozenset[int] = frozenset(
    {
        LARK_REFRESH_ERROR["TOKEN_INVALID"],
        LARK_REFRESH_ERROR["REFRESH_TOKEN_INVALID"],
        LARK_REFRESH_ERROR["REFRESH_TOKEN_EXPIRED"],
        LARK_REFRESH_ERROR["REFRESH_TOKEN_REVOKED"],
        LARK_REFRESH_ERROR["REFRESH_TOKEN_ALREADY_USED"],
    }
)


def classify_refresh_error(code: int | None) -> str:
    if code is None or code == 0:
        return "ok"
    if code in REFRESH_RETRYABLE_CODES:
        return "retryable"
    if code in REFRESH_INVALID_CODES:
        return "invalid"
    return "unknown"


@dataclass
class FeishuAuthSession:
    session_id: str
    profile_name: str
    open_id: str
    device_code: str
    user_code: str
    verification_uri: str
    scope: str
    client_id: str
    client_secret: str
    expires_at: int
    interval: int
    status: str = "pending"
    error: str = ""


_sessions: dict[str, FeishuAuthSession] = {}

FEISHU_ACCOUNTS_BASE_URL = os.environ.get(
    "FEISHU_ACCOUNTS_BASE_URL", "https://accounts.feishu.cn"
).rstrip("/")
FEISHU_OPEN_BASE_URL = os.environ.get(
    "FEISHU_OPEN_BASE_URL", "https://open.feishu.cn"
).rstrip("/")

FEISHU_DEFAULT_SCOPE = " ".join(
    dict.fromkeys(
        """
        approval:instance:read approval:instance:write approval:task:read approval:task:write
        attendance:task:readonly auth:user.id:read
        base:app:create base:app:read base:app:copy base:app:update
        base:table:read base:table:create base:table:update base:table:delete
        base:field:read base:field:create base:field:update base:field:delete
        base:view:read base:view:write_only
        base:record:read base:record:create base:record:update base:record:delete
        base:dashboard:create base:dashboard:delete base:dashboard:read base:dashboard:update
        base:form:create base:form:delete base:form:read base:form:update
        base:history:read base:role:create base:role:delete base:role:read base:role:update
        base:workflow:create base:workflow:read base:workflow:update
        bitable:app
        board:whiteboard:node:create board:whiteboard:node:delete board:whiteboard:node:read board:whiteboard:node:update
        calendar:calendar calendar:calendar:create calendar:calendar:read calendar:calendar:readonly calendar:calendar:update
        calendar:calendar.event:create calendar:calendar.event:read calendar:calendar.event:reply calendar:calendar.event:update
        calendar:calendar.free_busy:read
        contact:contact.base:readonly contact:user.base:readonly contact:user.basic_profile:readonly contact:user:search
        docs:document.content:read docs:document:export docs:document:import docs:document:copy
        docs:document.comment:create docs:document.comment:read docs:document.comment:update docs:document.comment:write_only
        docs:document.media:download docs:document.media:upload
        docs:permission.member:apply docs:permission.member:auth docs:permission.member:create docs:permission.member:transfer
        docs:permission.setting:read docs:permission.setting:write_only
        docx:document:create docx:document:readonly docx:document:write_only
        drive:drive drive:drive.metadata:readonly drive:drive:readonly drive:export:readonly
        drive:file:download drive:file:upload drive:file:view_record:readonly
        im:chat im:chat:create im:chat:create_by_user im:chat:read im:chat:readonly im:chat:update
        im:chat.members:read im:chat.members:write_only
        im:feed.flag:read im:feed.flag:write
        im:message im:message.group_msg im:message.group_msg:get_as_user
        im:message.p2p_msg:readonly im:message.p2p_msg:get_as_user
        im:message.reactions:read im:message.reactions:write_only
        im:message.send_as_user im:message:send_as_bot im:message:readonly
        im:message.pins:read im:message.pins:write_only im:resource
        mail:event mail:user_mailbox:readonly mail:user_mailbox.folder:read mail:user_mailbox.folder:write
        mail:user_mailbox.mail_contact:read mail:user_mailbox.mail_contact:write
        mail:user_mailbox.message.address:read mail:user_mailbox.message.body:read
        mail:user_mailbox.message.subject:read mail:user_mailbox.message:modify
        mail:user_mailbox.message:readonly mail:user_mailbox.message:send
        mail:user_mailbox.rule:read mail:user_mailbox.rule:write mail:user_mailbox.event.mail_address:read
        minutes:minutes.search:read minutes:minutes.media:export minutes:minutes:readonly
        minutes:minutes.artifacts:read minutes:minutes.transcript:export minutes:minutes.upload:write
        okr:okr.content:readonly okr:okr.content:writeonly okr:okr.period:readonly okr:okr.setting:read
        okr:okr.progress.file:upload okr:okr.progress:delete okr:okr.progress:readonly okr:okr.progress:writeonly
        search:search search:message search:docs:read
        sheets:spreadsheet sheets:spreadsheet:create sheets:spreadsheet.meta:read
        sheets:spreadsheet.meta:write_only sheets:spreadsheet:read sheets:spreadsheet:readonly sheets:spreadsheet:write_only
        slides:presentation:create slides:presentation:read slides:presentation:update slides:presentation:write_only
        space:document:delete space:document:move space:document:retrieve space:document:shortcut space:folder:create
        task:task task:task:write task:task:read task:tasklist:write task:tasklist:read
        task:section:write task:section:read task:comment:write task:custom_field:write task:custom_field:read
        task:attachment:write
        vc:meeting.bot.join:write vc:meeting.meetingevent:read vc:meeting.search:read vc:note:read vc:record:readonly
        wiki:wiki wiki:wiki:readonly wiki:space:read wiki:space:retrieve wiki:space:write_only
        wiki:node:copy wiki:node:create wiki:node:move wiki:node:read wiki:node:retrieve
        wiki:member:create wiki:member:retrieve wiki:member:update
        offline_access
        """.split()
    )
)


def resolve_shared_home() -> Path:
    configured = os.environ.get("HERMES_SHARED_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    parts = hermes_home.parts
    if len(parts) >= 2 and parts[-2] == "profiles":
        return hermes_home.parent.parent
    return hermes_home


def parse_scopes(raw: str | Iterable[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = raw.replace(",", " ").split()
    else:
        values = [str(item) for item in raw]
    return sorted({value.strip() for value in values if value.strip()})


def credential_status(
    *,
    profile_name: str,
    open_id: str,
    required_scopes: str | Iterable[str] | None = None,
    shared_home: Optional[Path] = None,
) -> dict[str, Any]:
    shared = shared_home or resolve_shared_home()
    _load_shared_env(shared)
    _assert_route(shared, profile_name, open_id)
    required = parse_scopes(required_scopes)
    refresh_error = ""
    refresh_needs_reauth = False
    try:
        refresh_uat_if_needed(
            profile_name=profile_name,
            open_id=open_id,
            shared_home=shared,
            headroom_seconds=300,
        )
    except FeishuUatAuthError as exc:
        refresh_error = _redact_status_refresh_error(exc.message)
        refresh_needs_reauth = _refresh_error_requires_reauth(exc)
    except Exception as exc:
        refresh_error = f"unexpected refresh error: {exc}"

    status = _profile_uat_status(
        shared_home=shared,
        profile_name=profile_name,
        open_id=open_id,
        required_scopes=required,
    )
    payload: dict[str, Any] = {}
    store = None
    try:
        store = CredentialStore(shared / "multitenancy.db")
        vault_status = store.get_status(
            profile_name=profile_name,
            subject_id=open_id,
            provider="feishu",
            secret_kind="uat",
            required_scopes=required,
        )
        try:
            payload = store.get_secret_for_runtime(
                profile_name=profile_name,
                subject_id=open_id,
                provider="feishu",
                secret_kind="uat",
            )
        except Exception:
            payload = {}
        vault_status["_runtime_payload_available"] = bool(payload)
        vault_status["runtime_available"] = bool(payload) and vault_status.get("status") == "valid"
        if _prefer_status(vault_status, status):
            status = vault_status
    finally:
        if store is not None:
            store.close()
    refresh_expires_at = _as_int((payload or {}).get("refresh_expires_at"))
    if refresh_expires_at and status.get("storage") == "multitenancy_db":
        status["refresh_expires_at"] = refresh_expires_at
    if status.get("storage") == "profile_feishu_uat_json":
        status["runtime_available"] = bool(status.get("has_payload")) and status.get("status") == "valid"
    else:
        status["runtime_available"] = bool(payload) and status.get("status") == "valid"
    status.pop("_runtime_payload_available", None)
    if refresh_error:
        status["needs_reauth"] = bool(refresh_needs_reauth and status.get("status") == "expired")
        status["refresh_error"] = refresh_error
    status["lark_cli"] = _lark_cli_status(shared, profile_name, open_id)
    return status


def _refresh_error_requires_reauth(exc: FeishuUatAuthError) -> bool:
    if exc.refresh_class == "invalid":
        return True
    if exc.status != 401:
        return False
    message = exc.message.lower()
    return "refresh_token" in message or "refresh token" in message


def _redact_status_refresh_error(message: str) -> str:
    if "credential encryption key is required" in message:
        return "Feishu app credentials are unavailable for background refresh"
    return message


def _profile_uat_status(
    *,
    shared_home: Path,
    profile_name: str,
    open_id: str,
    required_scopes: str | Iterable[str] | None = None,
) -> dict[str, Any]:
    base = {
        "profile_name": str(profile_name),
        "subject_id": str(open_id),
        "provider": "feishu",
        "secret_kind": "uat",
        "storage": "profile_feishu_uat_json",
        "has_payload": False,
        "runtime_available": False,
        "_runtime_payload_available": False,
        "scopes": [],
        "missing_scopes": [],
        "expires_at": None,
    }
    payload = _load_profile_uat_payload(shared_home, profile_name, open_id)
    if not payload or not str(payload.get("access_token") or "").strip():
        return {**base, "status": "missing"}

    scopes = parse_scopes(payload.get("scope") or payload.get("scopes"))
    required = parse_scopes(required_scopes)
    missing = [scope for scope in required if scope not in set(scopes)]
    expires_at = _as_int(
        payload.get("expires_at")
        or payload.get("expire_at")
        or payload.get("access_token_expires_at")
    ) or None
    status = "valid"
    if expires_at is not None and expires_at <= _now_ms():
        status = "expired"
    elif missing:
        status = "scope_missing"

    result = {
        **base,
        "status": status,
        "has_payload": True,
        "runtime_available": status == "valid",
        "_runtime_payload_available": True,
        "scopes": scopes,
        "missing_scopes": missing,
        "expires_at": expires_at,
    }
    refresh_expires_at = _as_int(payload.get("refresh_expires_at"))
    if refresh_expires_at:
        result["refresh_expires_at"] = refresh_expires_at
    return result


def _prefer_status(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    candidate_has = bool(candidate.get("has_payload"))
    current_has = bool(current.get("has_payload"))
    candidate_runtime = bool(candidate.get("runtime_available"))
    current_runtime = bool(current.get("runtime_available"))
    if current_runtime and not candidate_runtime:
        return False
    if candidate_runtime and not current_runtime:
        return True
    candidate_payload = bool(candidate.get("_runtime_payload_available"))
    current_payload = bool(current.get("_runtime_payload_available"))
    if current_payload and not candidate_payload:
        return False
    if candidate_payload and not current_payload:
        return True
    candidate_rank = _status_rank(candidate)
    current_rank = _status_rank(current)
    if candidate_rank != current_rank:
        return candidate_rank > current_rank
    if candidate_has and not current_has:
        return True
    if not candidate_has:
        return False
    if current_has:
        return _status_freshness(candidate) >= _status_freshness(current)
    return True


def _status_rank(status: dict[str, Any]) -> int:
    return {
        "valid": 3,
        "scope_missing": 2,
        "expired": 1,
        "missing": 0,
    }.get(str(status.get("status") or ""), 0)


def _status_freshness(status: dict[str, Any]) -> tuple[int, int]:
    return (
        _as_int(status.get("refresh_expires_at")),
        _as_int(status.get("expires_at")),
    )


def refresh_uat_if_needed(
    *,
    profile_name: str,
    open_id: str,
    shared_home: Optional[Path] = None,
    force: bool = False,
    headroom_seconds: int = 300,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, Any] | None:
    """Refresh one user's Feishu UAT if it is expired or near expiry.

    The multitenancy credential vault is the canonical store, but legacy
    Hermes paths still read ``profiles/<profile>/feishu_uat/<open_id>.json``.
    A successful refresh must update both in one place to avoid token skew.
    """
    shared = shared_home or resolve_shared_home()
    _load_shared_env(shared)
    _assert_route(shared, profile_name, open_id)
    payload = _load_best_uat_payload(shared, profile_name, open_id)
    if not payload:
        return None
    if not force and not _payload_needs_refresh(payload, headroom_seconds=headroom_seconds):
        return payload

    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not refresh_token:
        raise FeishuUatAuthError("stored Feishu UAT has no refresh_token", status=401)
    refresh_expires_at = _as_int(payload.get("refresh_expires_at"))
    if refresh_expires_at and refresh_expires_at <= _now_ms():
        raise FeishuUatAuthError("Feishu UAT refresh_token is expired", status=401)

    client_id = str(client_id or "").strip()
    client_secret = str(client_secret or "").strip()
    if not (client_id and client_secret):
        client_id, client_secret = _feishu_app_credentials(shared)
    result = _refresh_uat_token(refresh_token, client_id, client_secret)
    refreshed = _token_payload(
        {
            **result,
            "refresh_token": result.get("refresh_token") or refresh_token,
            "scope": result.get("scope") or payload.get("scope") or "",
        },
        open_id=open_id,
        app_id=str(payload.get("app_id") or client_id),
        scope=str(payload.get("scope") or ""),
    )
    if (
        "refresh_token_expires_in" not in result
        and "refresh_expires_in" not in result
        and payload.get("refresh_expires_at")
    ):
        refreshed["refresh_expires_at"] = int(payload["refresh_expires_at"])
    _store_uat(shared, profile_name, open_id, refreshed)
    return refreshed


def refresh_uat_for_user(
    open_id: str,
    client_id: str | None = None,
    client_secret: str | None = None,
    *,
    shared_home: Optional[Path] = None,
    profile_name: str | None = None,
    force: bool = True,
    headroom_seconds: int = 300,
) -> dict[str, Any] | None:
    """Legacy-compatible UAT refresh entrypoint backed by multitenancy.

    Older Hermes fork code calls ``hermes_cli.feishu_auth.refresh_uat_for_user``
    with only an ``open_id`` plus app credentials.  In multitenancy, an open_id
    must first resolve to an active routed profile; the credential vault is the
    canonical store and the profile-local JSON is only the compatibility mirror.
    """
    shared = shared_home or resolve_shared_home()
    _load_shared_env(shared)
    routed_profile = profile_name or _profile_name_for_open_id(shared, open_id)
    return refresh_uat_if_needed(
        profile_name=routed_profile,
        open_id=open_id,
        shared_home=shared,
        force=force,
        headroom_seconds=headroom_seconds,
        client_id=client_id,
        client_secret=client_secret,
    )


def _lark_cli_status(shared_home: Path, profile_name: str, open_id: str) -> dict[str, Any]:
    try:
        from . import agent_real

        profile_home = shared_home / "profiles" / profile_name
        binary = agent_real._resolve_lark_cli_authsidecar_binary(profile_home)
        app_id = agent_real._resolve_lark_cli_app_id(profile_home)
        return {
            "available": bool(binary.exists() and app_id),
            "default_identity": agent_real._lark_cli_default_identity(profile_home, open_id),
        }
    except Exception:
        return {
            "available": False,
            "default_identity": "bot",
        }


def start_session(
    *,
    profile_name: str,
    open_id: str,
    scope: str | None = None,
    shared_home: Optional[Path] = None,
) -> dict[str, Any]:
    shared = shared_home or resolve_shared_home()
    _load_shared_env(shared)
    _assert_route(shared, profile_name, open_id)
    client_id, client_secret = _feishu_app_credentials(shared)
    data = _begin_device_authorization(client_id, scope, client_secret)
    session_id = secrets.token_urlsafe(18)
    expires_in = int(data.get("expires_in", 1800))
    session = FeishuAuthSession(
        session_id=session_id,
        profile_name=profile_name,
        open_id=open_id,
        device_code=str(data["device_code"]),
        user_code=str(data["user_code"]),
        verification_uri=str(data["verification_uri_complete"]),
        scope=scope or "",
        client_id=client_id,
        client_secret=client_secret,
        expires_at=int(time.time()) + expires_in,
        interval=max(int(data.get("interval", 3)), 1),
    )
    _sessions[session_id] = session
    return _session_public(session)


def find_active_session(
    *,
    profile_name: str,
    open_id: str,
) -> dict[str, Any] | None:
    """Return a still-pending, non-expired device-flow session for this user.

    Lets callers reuse one live device code per ``(profile_name, open_id)``
    instead of minting a second concurrent session — e.g. ``/auth`` reuses a
    session already started by ``/feishu_auth`` (or a prior ``/auth``) so the
    two cards/pollers don't race two device codes for the same user.
    """
    now = int(time.time())
    for session in _sessions.values():
        if session.profile_name != profile_name or session.open_id != open_id:
            continue
        if session.status != "pending" or now >= session.expires_at:
            continue
        return _session_public(session)
    return None


def poll_session(
    *,
    session_id: str,
    profile_name: str,
    open_id: str,
    shared_home: Optional[Path] = None,
) -> dict[str, Any]:
    shared = shared_home or resolve_shared_home()
    _load_shared_env(shared)
    session = _sessions.get(session_id)
    if session is None:
        raise FeishuUatAuthError("authorization session not found", status=404)
    if session.profile_name != profile_name or session.open_id != open_id:
        raise FeishuUatAuthError("authorization session does not belong to this user", status=403)
    _assert_route(shared, profile_name, open_id)
    if session.status in {"success", "error", "expired"}:
        return _session_public(session)
    if int(time.time()) >= session.expires_at:
        session.status = "expired"
        session.error = "authorization session expired"
        return _session_public(session)

    result = _poll_device_token(session.device_code, session.client_id, session.client_secret)
    error = str(result.get("error") or "").strip()
    if error in {"authorization_pending", "slow_down"} or (not error and not result.get("access_token")):
        return _session_public(session)
    if error:
        session.status = "error"
        session.error = str(result.get("error_description") or error)
        return _session_public(session)

    token_open_id = str(result.get("open_id") or "").strip()
    if not token_open_id:
        user_info = _fetch_user_info(str(result.get("access_token") or ""))
        token_open_id = str(user_info.get("open_id") or "").strip()
    if token_open_id != open_id:
        session.status = "error"
        session.error = f"authorized account does not match requesting Feishu user ({token_open_id} does not match {open_id})"
        raise FeishuUatAuthError(session.error, status=403)

    payload = _token_payload(result, open_id=open_id, app_id=session.client_id, scope=session.scope)
    _store_uat(shared, profile_name, open_id, payload)
    session.status = "success"
    return _session_public(session)


def cancel_session(*, session_id: str, profile_name: str, open_id: str) -> dict[str, Any]:
    session = _sessions.get(session_id)
    if session is None:
        raise FeishuUatAuthError("authorization session not found", status=404)
    if session.profile_name != profile_name or session.open_id != open_id:
        raise FeishuUatAuthError("authorization session does not belong to this user", status=403)
    session.status = "error"
    session.error = "cancelled"
    return _session_public(session)


def _assert_route(shared_home: Path, profile_name: str, open_id: str) -> None:
    profile_name = _clean_id("profile_name", profile_name)
    open_id = _clean_id("open_id", open_id)
    routed_profile = _profile_name_for_open_id(shared_home, open_id)
    if routed_profile != profile_name:
        raise FeishuUatAuthError("Feishu user is not bound to this Hermes profile", status=403)


def _profile_name_for_open_id(shared_home: Path, open_id: str) -> str:
    open_id = _clean_id("open_id", open_id)
    db_path = shared_home / "multitenancy.db"
    if not db_path.is_file():
        raise FeishuUatAuthError("multitenancy routing DB is missing", status=503)
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            row = conn.execute(
                "SELECT profile_name FROM multitenancy_routing "
                "WHERE open_id = ? AND active = 1 AND kind = 'user' LIMIT 1",
                (open_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise FeishuUatAuthError(f"routing lookup failed: {exc}", status=503) from exc
    if not row:
        raise FeishuUatAuthError("Feishu user is not bound to this Hermes profile", status=403)
    return str(row[0])


def _clean_id(name: str, value: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise FeishuUatAuthError(f"{name} is required", status=400)
    if "/" in value or "\\" in value or "\0" in value or ".." in value:
        raise FeishuUatAuthError(f"{name} contains illegal characters", status=400)
    return value


def _feishu_app_credentials(shared_home: Path) -> tuple[str, str]:
    _load_shared_env(shared_home)
    client_id = os.environ.get("FEISHU_APP_ID", "").strip()
    client_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if client_id and client_secret:
        return client_id, client_secret

    store = None
    try:
        store = CredentialStore(shared_home / "multitenancy.db")
        status = store.get_status(
            profile_name="__global__",
            subject_id="feishu_app",
            provider="feishu",
            secret_kind="app",
        )
        if status.get("status") == "valid":
            payload = store.get_secret_for_runtime(
                profile_name="__global__",
                subject_id="feishu_app",
                provider="feishu",
                secret_kind="app",
            )
            client_id = str(payload.get("app_id") or payload.get("FEISHU_APP_ID") or "").strip()
            client_secret = str(payload.get("app_secret") or payload.get("FEISHU_APP_SECRET") or "").strip()
            if client_id and client_secret:
                return client_id, client_secret
    except Exception as exc:
        raise FeishuUatAuthError(f"Feishu app credential lookup failed: {exc}", status=503) from exc
    finally:
        if store is not None:
            store.close()
    raise FeishuUatAuthError("Feishu app credentials are not configured", status=503)


def _load_shared_env(shared_home: Path) -> None:
    env_path = shared_home / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in {
            "HERMES_MULTITENANCY_CREDENTIAL_KEY",
            "HERMES_CREDENTIAL_KEY",
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "FEISHU_DOMAIN",
            "HERMES_LARK_CLI_APP_ID",
        }:
            continue
        if os.environ.get(key):
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def _token_payload(result: dict[str, Any], *, open_id: str, app_id: str, scope: str) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    expires_in = int(result.get("expires_in") or 7200)
    refresh_expires_in = int(result.get("refresh_expires_in") or result.get("refresh_token_expires_in") or 30 * 24 * 3600)
    granted_scope = str(result.get("scope") or scope or "").strip()
    return {
        "app_id": app_id,
        "user_open_id": open_id,
        "access_token": str(result.get("access_token") or ""),
        "refresh_token": str(result.get("refresh_token") or ""),
        "expires_at": now_ms + expires_in * 1000,
        "refresh_expires_at": now_ms + refresh_expires_in * 1000,
        "scope": granted_scope,
        "granted_at": now_ms,
    }


def _store_uat(shared_home: Path, profile_name: str, open_id: str, payload: dict[str, Any]) -> None:
    """Persist a UAT after L1 write-time validation.

    Refuses to overwrite a known-good UAT with a degraded one. Two ways a UAT
    can be born broken: (a) Feishu didn't grant ``offline_access`` so no
    refresh_token will ever come (app-config bug, owner-actionable), (b) the
    response payload literally lacks ``refresh_token``. Either case writes a
    `.needs_reauth` marker keyed by reason so task gates can attribute the
    right owner — owner/admin for scope_stripped (app config), end-user for
    empty_refresh_token (re-auth).
    """
    rejection = _l1_validate_uat(payload)
    if rejection is not None:
        _l1_write_reject_marker(shared_home, profile_name, open_id, rejection)
        raise FeishuUatAuthError(
            f"UAT rejected by L1 write-time validation: {rejection['reason']}",
            status=400,
        )
    scopes = parse_scopes(payload.get("scope"))
    store = CredentialStore(shared_home / "multitenancy.db")
    try:
        store.put_credential(
            profile_name=profile_name,
            subject_id=open_id,
            provider="feishu",
            secret_kind="uat",
            payload=payload,
            scopes=scopes,
            expires_at=int(payload["expires_at"]) if payload.get("expires_at") else None,
        )
    finally:
        store.close()
    target = shared_home / "profiles" / profile_name / "feishu_uat" / f"{open_id}.json"
    _atomic_write_json(target, payload)


def _l1_validate_uat(payload: dict[str, Any]) -> Optional[dict[str, str]]:
    """Return a rejection record `{reason, detail}` or None if the UAT is sound."""
    if not payload_has_offline_access(payload):
        return {
            "reason": REASON_SCOPE_STRIPPED_BY_FEISHU,
            "detail": "Feishu granted a UAT without offline_access; refresh_token will never arrive",
        }
    if not payload_has_refresh_token(payload):
        return {
            "reason": REASON_EMPTY_REFRESH_TOKEN,
            "detail": "Feishu UAT payload has no refresh_token; cannot continue past access_token expiry",
        }
    return None


def _l1_write_reject_marker(
    shared_home: Path,
    profile_name: str,
    open_id: str,
    rejection: dict[str, str],
) -> None:
    """Write `.needs_reauth` markers for both the profile and legacy paths."""
    targets = [
        shared_home / "profiles" / profile_name / "feishu_uat",
        shared_home / "feishu_uat",
    ]
    for parent in targets:
        try:
            write_needs_reauth_marker(
                marker_path_for_open_id(parent, open_id),
                reason=rejection["reason"],
                detail=rejection.get("detail", ""),
                extra={"layer": "L1", "profile": profile_name},
            )
        except OSError:
            continue


def _load_best_uat_payload(shared_home: Path, profile_name: str, open_id: str) -> dict[str, Any] | None:
    vault_payload = _load_vault_uat_payload(shared_home, profile_name, open_id)
    json_payload = _load_profile_uat_payload(shared_home, profile_name, open_id)
    candidates = [payload for payload in (vault_payload, json_payload) if payload]
    if not candidates:
        return None
    return max(candidates, key=_payload_freshness)


def _load_vault_uat_payload(shared_home: Path, profile_name: str, open_id: str) -> dict[str, Any] | None:
    from .credential_broker import BrokerClientError, fetch_via_broker, running_as_broker_child

    if running_as_broker_child():
        try:
            return fetch_via_broker(
                kind="feishu_uat",
                profile_name=profile_name,
                open_id=open_id,
                run_id=os.environ.get("HERMES_MULTITENANCY_RUN_ID", ""),
            )
        except BrokerClientError:
            return None
    store = CredentialStore(shared_home / "multitenancy.db")
    try:
        return store.get_secret_for_runtime(
            profile_name=profile_name,
            subject_id=open_id,
            provider="feishu",
            secret_kind="uat",
        )
    except Exception:
        return None
    finally:
        store.close()


def _load_profile_uat_payload(shared_home: Path, profile_name: str, open_id: str) -> dict[str, Any] | None:
    path = shared_home / "profiles" / profile_name / "feishu_uat" / f"{open_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _payload_needs_refresh(payload: dict[str, Any], *, headroom_seconds: int = 300) -> bool:
    expires_at = _as_int(payload.get("expires_at") or payload.get("expire_at") or payload.get("access_token_expires_at"))
    if not expires_at:
        return False
    return _now_ms() >= expires_at - int(headroom_seconds) * 1000


def _payload_freshness(payload: dict[str, Any]) -> tuple[int, int]:
    return (
        _as_int(payload.get("granted_at") or payload.get("updated_at")),
        _as_int(payload.get("expires_at") or payload.get("expire_at") or payload.get("access_token_expires_at")),
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _refresh_access_token(client_id: str, client_secret: str, refresh_token: str, *, timeout: int = 10) -> dict[str, Any]:
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{FEISHU_OPEN_BASE_URL}/open-apis/authen/v2/oauth/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed Feishu host.
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise
        if isinstance(data, dict):
            return data
        raise


def _mint_tenant_access_token(client_id: str, client_secret: str, *, timeout: int = 10) -> str:
    body = json.dumps({"app_id": client_id, "app_secret": client_secret}).encode("utf-8")
    request = urllib.request.Request(
        f"{FEISHU_OPEN_BASE_URL}/open-apis/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed Feishu host.
        raw = json.loads(response.read().decode("utf-8"))
    if int(raw.get("code") or 0) != 0:
        raise FeishuUatAuthError("Feishu tenant token request rejected", status=503)
    token = str(raw.get("tenant_access_token") or raw.get("app_access_token") or "").strip()
    if not token:
        raise FeishuUatAuthError("Feishu tenant token response is missing token", status=503)
    return token


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _session_public(session: FeishuAuthSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "profile_name": session.profile_name,
        "subject_id": session.open_id,
        "status": session.status,
        "verification_uri": session.verification_uri,
        "user_code": session.user_code,
        "expires_at": session.expires_at,
        "interval": session.interval,
        **({"error": session.error} if session.error else {}),
    }


def _begin_device_authorization(client_id: str, scope: str | None, client_secret: str) -> dict[str, Any]:
    try:
        from hermes_cli.feishu_auth import begin_device_authorization

        return begin_device_authorization(client_id, scope, client_secret)
    except ModuleNotFoundError as exc:
        if not _is_missing_legacy_feishu_auth(exc):
            raise
    return _begin_device_authorization_local(client_id, scope, client_secret)


def _poll_device_token(device_code: str, client_id: str, client_secret: str) -> dict[str, Any]:
    try:
        from hermes_cli.feishu_auth import poll_device_token

        return poll_device_token(device_code, client_id, client_secret)
    except ModuleNotFoundError as exc:
        if not _is_missing_legacy_feishu_auth(exc):
            raise
    return _poll_device_token_local(device_code, client_id, client_secret)


def _refresh_uat_token(refresh_token: str, client_id: str, client_secret: str) -> dict[str, Any]:
    data = _refresh_access_token(client_id, client_secret, refresh_token)
    code = int(data.get("code") or 0)
    refresh_class = classify_refresh_error(code)
    if refresh_class == "retryable":
        logger.warning("Feishu UAT refresh transient error: code=%s; retrying once", code)
        data = _refresh_access_token(client_id, client_secret, refresh_token)
        code = int(data.get("code") or 0)
        refresh_class = classify_refresh_error(code)
        if code != 0:
            msg = str(data.get("msg") or data.get("message") or "refresh rejected").strip()
            if refresh_class == "invalid":
                raise FeishuUatAuthError(
                    f"Feishu UAT refresh failed: code={code} msg={msg}",
                    status=401,
                    refresh_class="invalid",
                )
            raise FeishuUatAuthError(
                f"Feishu UAT refresh failed after retry: code={code} msg={msg}",
                status=502,
                refresh_class="retryable_exhausted" if refresh_class == "retryable" else refresh_class,
            )
    elif refresh_class == "invalid":
        msg = str(data.get("msg") or data.get("message") or "refresh rejected").strip()
        raise FeishuUatAuthError(
            f"Feishu UAT refresh failed: code={code} msg={msg}",
            status=401,
            refresh_class="invalid",
        )
    elif refresh_class == "unknown":
        msg = str(data.get("msg") or data.get("message") or "refresh rejected").strip()
        raise FeishuUatAuthError(
            f"Feishu UAT refresh failed with non-token error: code={code} msg={msg}",
            status=502,
            refresh_class="unknown",
        )
    if isinstance(data.get("data"), dict):
        data = data["data"]
    return _normalise_refresh_response(data, require_refresh_token=False)


def _normalise_refresh_response(data: dict[str, Any], *, require_refresh_token: bool = True) -> dict[str, Any]:
    """Refuse degraded refresh responses outright.

    Initial grants must include a non-empty ``refresh_token``. Refresh grants
    may omit a replacement refresh token; in that case the caller preserves the
    existing one. An explicitly empty refresh_token is still treated as a
    degraded response.
    """
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise FeishuUatAuthError("Feishu refresh response missing access_token", status=502)
    has_refresh_token_field = "refresh_token" in data
    refresh_token = str(data.get("refresh_token") or "").strip()
    if not refresh_token:
        if require_refresh_token or has_refresh_token_field:
            raise FeishuUatAuthError(
                "Feishu refresh response missing refresh_token (offline_access likely stripped by app config)",
                status=502,
            )
        refresh_token = None
    scope = str(data.get("scope") or "").strip()
    if scope and "offline_access" not in parse_scopes(scope):
        raise FeishuUatAuthError(
            "Feishu refresh granted a scope without offline_access; future refreshes will fail",
            status=502,
        )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": int(data.get("expires_in") or 7200),
        "refresh_expires_in": _refresh_token_expires_in(data),
        "scope": scope,
        "token_type": str(data.get("token_type", "Bearer")).strip(),
    }


def _fetch_user_info(access_token: str) -> dict[str, Any]:
    try:
        from hermes_cli.feishu_auth import fetch_user_info

        return fetch_user_info(access_token)
    except ModuleNotFoundError as exc:
        if not _is_missing_legacy_feishu_auth(exc):
            raise
    return _fetch_user_info_local(access_token)


def _is_missing_legacy_feishu_auth(exc: ModuleNotFoundError) -> bool:
    return exc.name in {"hermes_cli", "hermes_cli.feishu_auth"}


def _app_granted_scope_names(client_id: str, client_secret: str, *, timeout: int = 10) -> set[str] | None:
    """Return scope names the APP is actually granted (grant_status == 1), or
    None on any failure so callers fall back to the full requested set.

    Feishu rejects the WHOLE device-auth with error 20027 if it requests any
    scope the app lacks. Each expert bot is a separate app with its own granted
    scope set, so requesting a fixed superset (FEISHU_DEFAULT_SCOPE, built for
    the main bot) breaks /auth on any bot missing a single scope. Intersecting
    the request with what the app actually has is the fix (fail-open: if this
    query fails we request the full set, i.e. current behavior).
    """
    try:
        token = _mint_tenant_access_token(client_id, client_secret, timeout=timeout)
        request = urllib.request.Request(
            f"{FEISHU_OPEN_BASE_URL}/open-apis/application/v6/scopes",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed Feishu host.
            raw = json.loads(response.read().decode("utf-8"))
    except Exception:
        logger.warning("[feishu-auth] app-scope query failed; requesting full scope set", exc_info=True)
        return None
    if int(raw.get("code") or 0) != 0:
        return None
    scopes = (raw.get("data") or {}).get("scopes")
    if not isinstance(scopes, list):
        return None
    granted = {
        str(s.get("scope_name")).strip()
        for s in scopes
        if isinstance(s, dict) and s.get("grant_status") == 1 and str(s.get("scope_name") or "").strip()
    }
    # Empty set = misconfigured/unexpected — fall back to full set rather than
    # request an empty (offline_access-only) scope.
    return granted or None


def _scope_with_offline_access(scope: str | None, granted: set[str] | None = None) -> str:
    parts = parse_scopes(scope or os.environ.get("HERMES_FEISHU_UAT_DEFAULT_SCOPE") or FEISHU_DEFAULT_SCOPE)
    if granted is not None:
        kept: list[str] = []
        dropped: list[str] = []
        for part in parts:
            # offline_access is an OAuth grant type, not an app scope — always keep.
            if part == "offline_access" or part in granted:
                kept.append(part)
            else:
                dropped.append(part)
        if dropped:
            logger.info(
                "[feishu-auth] app lacks %d requested scope(s); dropping from device-auth: %s",
                len(dropped), " ".join(sorted(dropped)),
            )
        parts = kept
    if "offline_access" not in parts:
        parts.append("offline_access")
    return " ".join(dict.fromkeys(parts))


def _api_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        data = _http_json("POST", url, payload=payload)
    except OSError as exc:
        raise FeishuUatAuthError(f"Network error calling {url}: {exc}", status=503) from exc
    if not isinstance(data, dict):
        raise FeishuUatAuthError(f"Unexpected response from {url}", status=502)
    error_code = data.get("error") or data.get("error_code")
    if error_code and error_code not in {"authorization_pending", "slow_down"}:
        description = data.get("error_description") or data.get("msg") or "unknown error"
        raise FeishuUatAuthError(f"Feishu OAuth error: {description}", status=502)
    return data


def _begin_device_authorization_local(
    client_id: str,
    scope: str | None,
    client_secret: str,
) -> dict[str, Any]:
    granted = _app_granted_scope_names(client_id, client_secret)
    data = _api_post(
        f"{FEISHU_ACCOUNTS_BASE_URL}/oauth/v1/device_authorization",
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": _scope_with_offline_access(scope, granted),
        },
    )
    required = {"device_code", "user_code", "verification_uri_complete"}
    missing = sorted(required - set(data))
    if missing:
        raise FeishuUatAuthError(
            f"device authorization response missing fields: {', '.join(missing)}",
            status=502,
        )
    return {
        "device_code": str(data["device_code"]).strip(),
        "user_code": str(data["user_code"]).strip(),
        "verification_uri": str(data.get("verification_uri") or "").strip(),
        "verification_uri_complete": str(data["verification_uri_complete"]).strip(),
        "expires_in": int(data.get("expires_in", 1800)),
        "interval": max(int(data.get("interval", 3)), 2),
    }


def _refresh_token_expires_in(data: dict[str, Any], default: int = 30 * 24 * 3600) -> int:
    raw = data.get("refresh_token_expires_in")
    if raw is None:
        raw = data.get("refresh_expires_in", default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _poll_device_token_local(device_code: str, client_id: str, client_secret: str) -> dict[str, Any]:
    data = _api_post(
        f"{FEISHU_OPEN_BASE_URL}/open-apis/authen/v2/oauth/token",
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "device_code": device_code,
        },
    )
    return {
        "access_token": str(data.get("access_token", "")).strip() or None,
        "refresh_token": str(data.get("refresh_token", "")).strip() or None,
        "open_id": str(data.get("open_id", "")).strip() or None,
        "expires_in": int(data.get("expires_in", 7200)),
        "refresh_expires_in": _refresh_token_expires_in(data),
        "token_type": str(data.get("token_type", "Bearer")).strip(),
        "scope": str(data.get("scope", "")).strip(),
        "error": str(data.get("error") or data.get("error_code") or "").strip() or None,
        "error_description": str(data.get("error_description") or "").strip() or None,
    }


def _fetch_user_info_local(access_token: str) -> dict[str, Any]:
    url = f"{FEISHU_OPEN_BASE_URL}/open-apis/authen/v1/user_info"
    try:
        body = _http_json("GET", url, headers={"Authorization": f"Bearer {access_token}"})
    except OSError as exc:
        raise FeishuUatAuthError(f"Network error calling {url}: {exc}", status=503) from exc
    data = (body or {}).get("data") or {}
    if (body or {}).get("code") != 0 or not data:
        raise FeishuUatAuthError(
            f"user_info failed: code={(body or {}).get('code')} msg={(body or {}).get('msg') or 'unknown'}",
            status=502,
        )
    return {
        "open_id": str(data.get("open_id") or "").strip(),
        "union_id": str(data.get("union_id") or "").strip() or None,
        "user_id": str(data.get("user_id") or "").strip() or None,
        "name": str(data.get("name") or "").strip() or None,
    }


def _http_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        preview = raw[:200].decode("utf-8", errors="replace") if raw else ""
        raise FeishuUatAuthError(f"Non-JSON response from {url}: {preview}", status=502) from exc
