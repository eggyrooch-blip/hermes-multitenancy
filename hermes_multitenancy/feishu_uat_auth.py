"""WebUI-triggered Feishu UAT authorization sessions.

This module keeps the WebUI side out of raw token handling.  The browser asks
the WebUI BFF, the BFF calls the router-owned broker with the signed WebUI
identity, and this module stores successful UAT results in the multitenancy
credential vault plus the profile-local compatibility JSON.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .credentials import CredentialStore


class FeishuUatAuthError(Exception):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


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
    _assert_route(shared, profile_name, open_id)
    store = CredentialStore(shared / "multitenancy.db")
    try:
        status = store.get_status(
            profile_name=profile_name,
            subject_id=open_id,
            provider="feishu",
            secret_kind="uat",
            required_scopes=parse_scopes(required_scopes),
        )
    finally:
        store.close()
    status["lark_cli"] = _lark_cli_status(shared, profile_name, open_id)
    return status


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


def poll_session(
    *,
    session_id: str,
    profile_name: str,
    open_id: str,
    shared_home: Optional[Path] = None,
) -> dict[str, Any]:
    shared = shared_home or resolve_shared_home()
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
    if not row or row[0] != profile_name:
        raise FeishuUatAuthError("Feishu user is not bound to this Hermes profile", status=403)


def _clean_id(name: str, value: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise FeishuUatAuthError(f"{name} is required", status=400)
    if "/" in value or "\\" in value or "\0" in value or ".." in value:
        raise FeishuUatAuthError(f"{name} contains illegal characters", status=400)
    return value


def _feishu_app_credentials(shared_home: Path) -> tuple[str, str]:
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
    from hermes_cli.feishu_auth import begin_device_authorization

    return begin_device_authorization(client_id, scope, client_secret)


def _poll_device_token(device_code: str, client_id: str, client_secret: str) -> dict[str, Any]:
    from hermes_cli.feishu_auth import poll_device_token

    return poll_device_token(device_code, client_id, client_secret)


def _fetch_user_info(access_token: str) -> dict[str, Any]:
    from hermes_cli.feishu_auth import fetch_user_info

    return fetch_user_info(access_token)
