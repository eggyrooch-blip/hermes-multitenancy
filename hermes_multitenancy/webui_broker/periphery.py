"""Periphery helpers for the WebUI run-broker seam (pure move from webui_broker_server).

Every top-level symbol except ``create_run_broker_app`` lives here; the parent
``webui_broker_server`` module re-exports them and stays a thin shim around the
route-registration god-function. Cross-module calls that tests monkeypatch on the
shim (``_profile_home_for_name`` / ``_run_broker_key`` / ``_run_broker_host`` /
``_goal_manager``) are routed through ``_m`` so the patch is honoured.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from .. import cron_api
from ..credential_broker import (
    LeaseError,
    assert_lease_binding,
    lease_signing_secret,
    verify_lease,
)
from ..run_broker import RunBroker, RunRejected
from ..run_models import RunEvent, RunRequest
from ..security_audit import append_security_event

from .constants import *  # noqa: F401,F403 -- re-export constants into this namespace

# Shim module (parent) — used to route monkeypatch-sensitive helper calls so a
# test patching ``webui_broker_server._helper`` is honoured from periphery too.
# RELATIVE import keeps the plugin relocatable under an alternate parent package
# name (see tests/test_plugin_install_layout.py).
from .. import webui_broker_server as _m  # noqa: E402

logger = logging.getLogger("hermes_multitenancy.webui_broker_server")


_runner: Any = None


_site: Any = None


_server_task: Optional[asyncio.Task] = None


_pending_clarifies: dict[str, dict[str, Any]] = {}


_credential_broker_tokens: dict[str, dict[str, str]] = {}


_credential_broker_tokens_lock = threading.Lock()


_session_search_broker_tokens: dict[str, dict[str, str]] = {}


_session_search_broker_tokens_lock = threading.Lock()


def _session_history_slash_commands() -> list[dict[str, str]]:
    return [
        {
            "name": "new",
            "slash": "/new",
            "title": "New session history",
            "description": "Clear this profile's broker conversation memory for the current owner",
            "source": "builtin",
            "type": "session_history",
            "category": "Session",
        },
        {
            "name": "reset",
            "slash": "/reset",
            "title": "Reset session history",
            "description": "Clear this profile's broker conversation memory for the current owner",
            "source": "builtin",
            "type": "session_history",
            "category": "Session",
        },
        {
            "name": "status",
            "slash": "/status",
            "title": "Session status",
            "description": "Show current profile and broker history message count",
            "source": "builtin",
            "type": "session_history",
            "category": "Session",
        },
        {
            "name": "plan",
            "slash": "/plan",
            "title": "Plan",
            "description": "Expand the profile's plan skill for the current request",
            "source": "builtin",
            "type": "skill",
            "category": "Session",
        },
        {
            "name": "goal",
            "slash": "/goal",
            "title": "Goal",
            "description": "Set, inspect, pause, resume, or clear the active session goal",
            "source": "builtin",
            "type": "goal",
            "category": "Session",
        },
        {
            "name": "subgoal",
            "slash": "/subgoal",
            "title": "Subgoal",
            "description": "List, add, or clear subgoals for the active session goal",
            "source": "builtin",
            "type": "goal",
            "category": "Session",
        },
    ]


def _dedupe_slash_commands(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the first command for each slash string, preserving input order."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for command in commands:
        slash = str(command.get("slash") or "").strip()
        if not slash or slash in seen:
            continue
        seen.add(slash)
        out.append(command)
    return out


def _shared_home_from_env() -> Path:
    configured = os.environ.get("HERMES_SHARED_HOME") or os.environ.get("HERMES_HOME")
    return Path(configured or (Path.home() / ".hermes")).expanduser()


def _run_skillhub_install_in_background(event_id: str, shared_home: Path) -> None:
    try:
        from .. import skillhub_installer

        skillhub_installer.process_one(event_id=event_id, shared_home=shared_home)
    except Exception:
        logger.exception("[multitenancy] SkillHub background install failed event_id=%s", event_id)


def _safe_plugin_asset_component(raw: Any) -> str | None:
    value = str(raw or "").strip()
    if not value or "/" in value or "\\" in value or value.startswith("."):
        return None
    if not _PLUGIN_ASSET_COMPONENT_RE.fullmatch(value):
        return None
    return value


def _resolve_registered_plugin_asset(
    plugin_id_raw: Any,
    asset_name_raw: Any,
) -> tuple[Path, str, str, str] | None:
    """Resolve a plugin asset only when the managed manifest explicitly registered it."""
    plugin_id = _safe_plugin_asset_component(plugin_id_raw)
    asset_name = _safe_plugin_asset_component(asset_name_raw)
    if not plugin_id or not asset_name:
        return None
    shared_home = _shared_home_from_env()
    manifest_path = shared_home / _PLUGIN_ASSET_MANAGED_DIR / f"{plugin_id}.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    assets = manifest.get("assets")
    if not isinstance(assets, dict):
        return None
    asset = assets.get(asset_name)
    if not isinstance(asset, dict):
        return None
    path_raw = str(asset.get("path") or "").strip()
    if not path_raw:
        return None
    try:
        asset_path = Path(path_raw).expanduser().resolve()
        allowed_root = (shared_home / _PLUGIN_ASSET_STORAGE_DIR / plugin_id).resolve()
        asset_path.relative_to(allowed_root)
    except Exception:
        logger.warning("[multitenancy] plugin asset path rejected: plugin=%r asset=%r", plugin_id, asset_name)
        return None
    if not asset_path.is_file():
        return None
    mime = str(asset.get("mime") or "").strip()
    if mime not in set(_PLUGIN_ASSET_MIME_BY_SUFFIX.values()):
        mime = _PLUGIN_ASSET_MIME_BY_SUFFIX.get(asset_path.suffix.lower(), "application/octet-stream")
    if mime == "application/octet-stream":
        return None
    return asset_path, mime, plugin_id, asset_name


def _dotenv_values(path: Path) -> dict[str, str]:
    try:
        from dotenv import dotenv_values

        return {
            str(key): str(value)
            for key, value in dotenv_values(path).items()
            if key and value is not None
        }
    except Exception:
        values: dict[str, str] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return values
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            if key:
                values[key] = value
        return values


def load_run_broker_shared_env(shared_home: Path | None = None) -> dict[str, str]:
    """Load only Run Broker control-plane env from shared ``.env``.

    A standalone local Run Broker may be started by launchd without the parent
    gateway's vault env. It still needs the credential-vault key to mint bot
    tokens through the lark-cli auth broker, but it must not import broad model
    keys or raw Feishu app secrets into process env.
    """
    env_path = (shared_home or _shared_home_from_env()) / ".env"
    values = _dotenv_values(env_path)
    loaded: dict[str, str] = {}
    for key in sorted(_RUN_BROKER_SHARED_ENV_KEYS):
        value = str(values.get(key) or "").strip()
        if value and not os.environ.get(key):
            os.environ[key] = value
            loaded[key] = value
    return loaded


def _truthy_env(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _run_broker_host() -> str:
    return os.environ.get("HERMES_MULTITENANCY_RUN_BROKER_HOST", "127.0.0.1").strip() or "127.0.0.1"


def _run_broker_port() -> int:
    raw = os.environ.get("HERMES_MULTITENANCY_RUN_BROKER_PORT", "8766").strip()
    try:
        return int(raw)
    except ValueError:
        return 8766


def _run_broker_key() -> str:
    return os.environ.get("HERMES_MULTITENANCY_RUN_BROKER_KEY", "").strip()


def credential_broker_url() -> str:
    return f"http://{_m._run_broker_host()}:{_run_broker_port()}"


def register_credential_broker_token(
    *,
    token: str,
    profile_name: str,
    open_id: str,
    run_id: str,
    agent_id: str = "",
    share_role: str = "",
) -> None:
    key = str(token or "").strip()
    if not key:
        return
    with _credential_broker_tokens_lock:
        _credential_broker_tokens[key] = {
            "profile_name": str(profile_name or "").strip(),
            "open_id": str(open_id or "").strip(),
            "run_id": str(run_id or "").strip(),
            "agent_id": str(agent_id or "").strip(),
            "share_role": str(share_role or "").strip(),
        }


def unregister_credential_broker_token(token: str) -> None:
    key = str(token or "").strip()
    if not key:
        return
    with _credential_broker_tokens_lock:
        _credential_broker_tokens.pop(key, None)


def _lookup_credential_broker_token(token: str) -> dict[str, str] | None:
    key = str(token or "").strip()
    if not key:
        return None
    with _credential_broker_tokens_lock:
        record = _credential_broker_tokens.get(key)
        if record is None or not hmac.compare_digest(key, token):
            return None
        return dict(record)


def register_session_search_broker_token(
    *,
    token: str,
    profile_name: str,
    open_id: str,
    run_id: str,
    agent_id: str = "",
    share_role: str = "",
) -> None:
    key = str(token or "").strip()
    if not key:
        return
    with _session_search_broker_tokens_lock:
        _session_search_broker_tokens[key] = {
            "profile_name": str(profile_name or "").strip(),
            "open_id": str(open_id or "").strip(),
            "run_id": str(run_id or "").strip(),
            "agent_id": str(agent_id or "").strip(),
            "share_role": str(share_role or "").strip(),
        }


def unregister_session_search_broker_token(token: str) -> None:
    key = str(token or "").strip()
    if not key:
        return
    with _session_search_broker_tokens_lock:
        _session_search_broker_tokens.pop(key, None)


def _lookup_session_search_broker_token(token: str) -> dict[str, str] | None:
    key = str(token or "").strip()
    if not key:
        return None
    with _session_search_broker_tokens_lock:
        record = _session_search_broker_tokens.get(key)
        if record is None or not hmac.compare_digest(key, token):
            return None
        return dict(record)


def _call_session_search_compat(session_search: Callable[..., str], **kwargs: Any) -> str:
    try:
        signature = inspect.signature(session_search)
    except (TypeError, ValueError):
        return session_search(**kwargs)
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return session_search(**kwargs)
    accepted = {key: value for key, value in kwargs.items() if key in signature.parameters}
    return session_search(**accepted)


class _ActorScopedSessionDB:
    """Narrow session_search DB reads to one actor inside a shared agent run."""

    def __init__(self, inner: Any, actor_open_id: str) -> None:
        self._inner = inner
        self._actor_open_id = str(actor_open_id or "").strip()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _session_allowed(self, session: dict[str, Any] | None, *, _seen: Optional[set[str]] = None) -> bool:
        if not session or not self._actor_open_id:
            return False
        if str(session.get("user_id") or "").strip() == self._actor_open_id:
            return True
        parent_session_id = str(session.get("parent_session_id") or "").strip()
        if not parent_session_id:
            return False
        seen = _seen or set()
        if parent_session_id in seen:
            return False
        seen.add(parent_session_id)
        return self._session_allowed(self._inner.get_session(parent_session_id), _seen=seen)

    def _session_id_allowed(self, session_id: str) -> bool:
        session_id = str(session_id or "").strip()
        if not session_id:
            return False
        return self._session_allowed(self._inner.get_session(session_id))

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        session = self._inner.get_session(session_id)
        return session if self._session_allowed(session) else None

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        return self._inner.get_messages(session_id) if self._session_id_allowed(session_id) else []

    def get_messages_as_conversation(self, session_id: str) -> list[dict[str, Any]]:
        if not self._session_id_allowed(session_id):
            return []
        return self._inner.get_messages_as_conversation(session_id)

    def search_messages(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        requested_limit = int(kwargs.get("limit") or 20)
        expanded_kwargs = dict(kwargs)
        expanded_kwargs["limit"] = max(requested_limit * 20, 200)
        expanded_kwargs["offset"] = 0
        matches = self._inner.search_messages(*args, **expanded_kwargs)
        filtered = [
            row for row in matches
            if self._session_id_allowed(str(row.get("session_id") or ""))
        ]
        offset = int(kwargs.get("offset") or 0)
        return filtered[offset: offset + requested_limit]

    def list_sessions_rich(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        requested_limit = int(kwargs.get("limit") or 20)
        expanded_kwargs = dict(kwargs)
        expanded_kwargs["limit"] = max(requested_limit * 20, 200)
        expanded_kwargs["offset"] = 0
        rows = self._inner.list_sessions_rich(*args, **expanded_kwargs)
        filtered = [
            row for row in rows
            if self._session_allowed(row)
        ]
        offset = int(kwargs.get("offset") or 0)
        return filtered[offset: offset + requested_limit]


def _session_search_db_for_claims(db: Any, claims: dict[str, str]) -> Any:
    share_role = str(claims.get("share_role") or "").strip()
    if share_role in {"viewer", "editor"}:
        return _ActorScopedSessionDB(db, str(claims.get("open_id") or ""))
    return db


def _positive_int_env(name: str) -> Optional[int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("[multitenancy] ignoring invalid %s=%r", name, raw)
        return None
    if value <= 0:
        logger.warning("[multitenancy] ignoring non-positive %s=%r", name, raw)
        return None
    return value


def _run_broker_client_max_size() -> int:
    configured_bytes = _positive_int_env("HERMES_MULTITENANCY_RUN_BROKER_CLIENT_MAX_SIZE_BYTES")
    if configured_bytes is not None:
        return configured_bytes
    configured_mb = _positive_int_env("HERMES_MULTITENANCY_RUN_BROKER_CLIENT_MAX_SIZE_MB")
    if configured_mb is not None:
        return configured_mb * 1024 * 1024
    return _RUN_BROKER_DEFAULT_CLIENT_MAX_SIZE


def _owner_enforcement_enabled() -> bool:
    return _truthy_env("HERMES_MULTITENANCY_RUN_BROKER_SERVER")


def _authorized(request: Any) -> bool:
    expected = _m._run_broker_key()
    if not expected:
        # No key configured. Keep the dev-friendly "open" behavior ONLY for a
        # loopback bind; if the broker is somehow bound to a non-loopback host
        # with no key, fail CLOSED — a keyless broker must never be reachable
        # off-box. Defense-in-depth: the prod startup path already refuses a
        # keyless bind, but the auth decision no longer trusts that alone.
        # audit 2026-07-03 §4 fail-open hardening.
        host = _m._run_broker_host()
        return host in {"127.0.0.1", "::1", "localhost", ""}
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    return hmac.compare_digest(header[len(prefix):].strip(), expected)


_auth_signal_store: dict[str, dict[str, Any]] = {}


_auth_signal_store_lock = threading.Lock()


def _auth_signal_prune_locked(now: float) -> None:
    cutoff = now - _AUTH_SIGNAL_TTL_SECONDS
    for key in [k for k, e in _auth_signal_store.items() if e["stored_at"] < cutoff]:
        _auth_signal_store.pop(key, None)
    if len(_auth_signal_store) > _AUTH_SIGNAL_CAP:
        overflow = sorted(_auth_signal_store.items(), key=lambda kv: kv[1]["stored_at"])
        for key, _ in overflow[: len(_auth_signal_store) - _AUTH_SIGNAL_CAP]:
            _auth_signal_store.pop(key, None)


def _auth_signal_stash(
    signal_run_id: str,
    *,
    payload: dict[str, Any],
    profile_name: str,
    subject: str,
) -> None:
    """Park an inbound request for post-auth replay. `metadata` is passed
    through `_sanitize_ingest_metadata` (same allowlist handle_run applies to
    the RunRequest) before storage, so no unexpected client-supplied keys sit
    in memory for the TTL window; the rest is user content (channel/content/
    messages), never credentials/env."""
    stored = dict(payload or {})
    if isinstance(stored.get("metadata"), dict):
        stored["metadata"] = _sanitize_ingest_metadata(stored["metadata"])
    now = time.time()
    with _auth_signal_store_lock:
        _auth_signal_prune_locked(now)
        _auth_signal_store[signal_run_id] = {
            "payload": stored,
            "profile_name": str(profile_name or ""),
            "subject": str(subject or ""),
            "stored_at": now,
        }


def _auth_signal_lookup(signal_run_id: str) -> Optional[dict[str, Any]]:
    now = time.time()
    with _auth_signal_store_lock:
        _auth_signal_prune_locked(now)
        entry = _auth_signal_store.get(signal_run_id)
        return dict(entry) if entry is not None else None


def _auth_signal_consume(signal_run_id: str) -> None:
    with _auth_signal_store_lock:
        _auth_signal_store.pop(signal_run_id, None)


def _auth_signal_touch(signal_run_id: str) -> None:
    """Restart a retained entry's TTL/eviction clock at auth_required emission.

    The entry is stashed at run START (before we know the run will re-auth), so
    without this its 600s window and its cap-eviction age would count from run
    start — a slow run could shrink the user's real re-auth window or make the
    pending entry the oldest cap-eviction victim. Refreshing stored_at when the
    signal actually fires gives the user the full window from that moment.
    """
    with _auth_signal_store_lock:
        entry = _auth_signal_store.get(signal_run_id)
        if entry is not None:
            entry["stored_at"] = time.time()


def _skillhub_authorized(request: Any) -> bool:
    """Auth for the SkillHub webhook ingress — **fail-closed**.

    This route is exposed to the public internet (via the reverse proxy), so it
    does NOT inherit ``_authorized``'s dev-friendly "no key configured → open"
    behavior. Rules:

    * Accept a dedicated ``HERMES_SKILLHUB_WEBHOOK_KEY`` (the fixed Bearer token
      handed to the AiDock side) so the master broker key is never shared.
    * Also accept the master ``HERMES_MULTITENANCY_RUN_BROKER_KEY`` (single-key
      / internal-caller setups).
    * If NEITHER key is configured, refuse — never serve this route unauthenticated.
    """
    dedicated = os.environ.get("HERMES_SKILLHUB_WEBHOOK_KEY", "").strip()
    master = _m._run_broker_key()
    if not dedicated and not master:
        return False  # public route must never be open
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    token = header[len(prefix):].strip() if header.startswith(prefix) else ""
    if not token:
        return False
    if dedicated and hmac.compare_digest(token, dedicated):
        return True
    if master and hmac.compare_digest(token, master):
        return True
    return False


def _ingest_bearer_token(request: Any) -> str:
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    return header[len(prefix):].strip() if header.startswith(prefix) else ""


def _ingest_file_bindings() -> list[dict[str, str]]:
    path = os.environ.get("HERMES_INGEST_KEYS_FILE", "").strip()
    if not path:
        return []
    try:
        raw = json.loads(Path(path).expanduser().read_text())
    except Exception:
        logger.exception("[multitenancy] failed to load ingest key bindings file")
        return []
    entries = raw.get("keys", raw.get("bindings", [])) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    bindings: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        token = str(entry.get("token") or "").strip()
        owner = str(entry.get("owner") or "").strip()
        profile = str(entry.get("profile") or "").strip()
        if not token or (not owner and not profile):
            continue
        binding = {
            "owner": owner,
            "profile": profile,
            "agent": str(entry.get("agent") or entry.get("agent_id") or "").strip(),
            "name": str(entry.get("name") or entry.get("display_label") or "").strip(),
            "source": "file",
        }
        bindings.append({"token": token, **binding})
    return bindings


def _legacy_ingest_binding() -> dict[str, str]:
    owner = _ingest_owner()
    return {
        "owner": owner,
        "profile": "" if owner else os.environ.get("HERMES_INGEST_PROFILE", "").strip(),
        "agent": "",
        "name": "",
        "source": "legacy",
    }


def _ingest_binding_for_request(request: Any) -> Optional[dict[str, str]]:
    token = _ingest_bearer_token(request)
    if not token:
        return None
    for binding in _ingest_file_bindings():
        expected = binding.pop("token", "")
        if expected and hmac.compare_digest(token, expected):
            return binding
    dedicated = os.environ.get("HERMES_INGEST_KEY", "").strip()
    if dedicated and hmac.compare_digest(token, dedicated):
        return _legacy_ingest_binding()
    master = _m._run_broker_key()
    if master and hmac.compare_digest(token, master):
        return _legacy_ingest_binding()
    return None


def _ingest_bound_agent_label(binding: dict[str, str]) -> str:
    return binding.get("name") or binding.get("agent") or binding.get("profile") or ""


def _ingest_bound_agent_id(binding: dict[str, str]) -> str:
    return binding.get("agent") or binding.get("profile") or ""


def _ingest_agent_matches_binding(binding: dict[str, str], agent_str: str) -> bool:
    target = (agent_str or "").strip()
    if not target:
        return True
    candidates = {
        binding.get("name"),
        binding.get("agent"),
        binding.get("profile"),
    }
    return target in {str(v) for v in candidates if v}


def _ingest_authorized(request: Any) -> bool:
    """Auth for the synchronous ingest ingress — **fail-closed**.

    Mirrors ``_skillhub_authorized``: a public-facing route that must never
    serve unauthenticated. Accepts a dedicated ``HERMES_INGEST_KEY`` (the
    fixed Bearer token handed to the external caller, so neither the master
    broker key nor the SkillHub key is ever shared) and also accepts the
    master ``HERMES_MULTITENANCY_RUN_BROKER_KEY`` for internal callers. If
    NEITHER key is configured, refuse — never serve this route open.
    """
    return _ingest_binding_for_request(request) is not None


def _ingest_public_interaction(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip internal bridge plumbing from a clarify/approval payload before
    returning it to an internet-facing ingest caller (review NB3).

    Removes the response/decision file paths and the internal session key,
    which are server-side coordination details the external caller must never
    see. Keeps the user-meaningful fields (question/choices/command/etc).
    """
    cleaned = dict(payload or {})
    for key in ("response_path", "decision_path", "session_key"):
        cleaned.pop(key, None)
    return cleaned


def _sanitize_ingest_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(metadata or {})
    for key in _INGEST_RESERVED_METADATA_KEYS:
        sanitized.pop(key, None)
    return sanitized


def _ingest_timeout() -> float:
    """Hard wall-clock cap (seconds) for a synchronous ingest run.

    Bounds the held HTTP connection so a stuck agent / blocked interaction
    bridge can never hang the caller forever. Default 180s; override via
    ``HERMES_INGEST_TIMEOUT``.
    """
    raw = os.getenv("HERMES_INGEST_TIMEOUT", "180")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 180.0
    return value if value > 0 else 180.0


def _ingest_async_timeout() -> float:
    raw = os.getenv("HERMES_INGEST_ASYNC_TIMEOUT", "1800")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1800.0
    return value if value > 0 else 1800.0


def _ingest_async_ttl() -> float:
    raw = os.getenv("HERMES_INGEST_ASYNC_TTL", "3600")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 3600.0
    return value if value > 0 else 3600.0


def _ingest_async_cap() -> int:
    raw = os.getenv("HERMES_INGEST_ASYNC_CAP", "256")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 256
    return value if value > 0 else 256


def _ingest_auth_fingerprint(request: Any, binding: dict[str, str]) -> str:
    token_hash = hashlib.sha256(_ingest_bearer_token(request).encode("utf-8")).hexdigest()
    scope = json.dumps(
        {
            "owner": binding.get("owner", ""),
            "profile": binding.get("profile", ""),
            "agent": binding.get("agent", ""),
            "name": binding.get("name", ""),
            "source": binding.get("source", ""),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    scope_hash = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    return f"{binding.get('source', '')}:{scope_hash}:{token_hash}"


def _parse_ingest_secrets(payload: dict[str, Any]) -> tuple[SimpleNamespace | None, str | None]:
    raw = payload.get("secrets")
    if raw is None:
        entries: list[dict[str, str]] = []
        manifest: list[dict[str, str]] = []
        fingerprint_source: list[dict[str, str]] = []
        fingerprint = hashlib.sha256(b"[]").hexdigest()
        return SimpleNamespace(
            entries=entries,
            manifest=manifest,
            fingerprint=fingerprint,
        ), None
    if not isinstance(raw, dict):
        return None, "secrets must be a JSON object"

    entries = []
    manifest = []
    fingerprint_source = []
    total_bytes = 0
    for name, item in sorted(raw.items(), key=lambda kv: str(kv[0])):
        name = str(name or "")
        if not _INGEST_SECRET_NAME_RE.fullmatch(name):
            return None, "invalid secret name"
        if not isinstance(item, dict):
            return None, "secret entry must be an object"
        secret_type = str(item.get("type") or "").strip()
        if secret_type not in _INGEST_SECRET_TYPES:
            return None, "invalid secret type"
        value = item.get("value")
        if not isinstance(value, str) or not value:
            return None, "secret value must be a non-empty string"
        value_bytes = value.encode("utf-8")
        if len(value_bytes) > _INGEST_SECRET_MAX_VALUE_BYTES:
            return None, "secret value too large"
        total_bytes += len(value_bytes)
        if total_bytes > _INGEST_SECRET_MAX_TOTAL_BYTES:
            return None, "secrets payload too large"
        value_sha = hashlib.sha256(value_bytes).hexdigest()
        usage = _INGEST_SECRET_USAGE.get(secret_type, "caller-defined secret")
        entries.append(
            {
                "name": name,
                "type": secret_type,
                "value": value,
                "sha256": value_sha,
                "usage": usage,
            }
        )
        manifest.append({"name": name, "type": secret_type, "usage": usage})
        fingerprint_source.append({"name": name, "type": secret_type, "sha256": value_sha})

    fingerprint_payload = json.dumps(
        fingerprint_source,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return SimpleNamespace(
        entries=entries,
        manifest=manifest,
        fingerprint=hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest(),
    ), None


def _ingest_secret_manifest_prompt(manifest: list[dict[str, str]]) -> str:
    if not manifest:
        return ""
    lines = [
        "Ingest runtime secrets are available to tools only; their values are not visible here.",
        "Use files under $HERMES_INGEST_SECRET_DIR by secret name when a tool needs them:",
    ]
    for item in manifest:
        lines.append(
            f"- `{item['name']}`: type `{item['type']}`, usage: {item.get('usage') or 'secret'}"
        )
    lines.extend([
        "",
        "Secret handling rules:",
        "- Do not print, echo, log, save, or summarize secret values, token previews, token prefixes, Authorization header values, Cookie header values, Basic credentials, or raw secret file contents.",
        "- It is safe to print only the secret name, type, file existence, and value length when needed.",
        "- For bearer tokens, read the file inside terminal or execute_code and pass it directly as an Authorization header without displaying it.",
        "",
        "Execution rules for ingest API runs:",
        "- Do not delegate this task to delegate_task, subagents, or background tasks; complete it directly in this run.",
        "- Use terminal or execute_code for authenticated business API calls that need secrets; do not use web_extract/web_search for authenticated business APIs.",
    ])
    return "\n".join(lines)


def _ingest_redact_text(text: Any, secret_spec: SimpleNamespace | None) -> str:
    value = str(text or "")
    if not secret_spec:
        return _ingest_redact_secret_like_text(value)
    entries = list(getattr(secret_spec, "entries", []) or [])
    for item in sorted(entries, key=lambda entry: len(entry.get("value") or ""), reverse=True):
        secret_value = str(item.get("value") or "")
        if secret_value:
            value = value.replace(secret_value, f"[REDACTED:{item.get('name') or 'secret'}]")
            value = _ingest_redact_secret_prefixes(
                value,
                secret_name=str(item.get("name") or "secret"),
                secret_value=secret_value,
            )
    return _ingest_redact_secret_like_text(value)


def _ingest_redact_secret_prefixes(
    text: str,
    *,
    secret_name: str,
    secret_value: str,
) -> str:
    if len(secret_value) < 8:
        return text
    value = text
    for length in range(min(len(secret_value) - 1, 64), 7, -1):
        prefix = secret_value[:length]
        if prefix:
            value = value.replace(prefix, f"[REDACTED:{secret_name}:prefix]")
    return value


def _ingest_redact_secret_like_text(text: str) -> str:
    value = _INGEST_AUTH_BEARER_RE.sub(
        lambda match: f"{match.group(1)}[REDACTED:authorization]",
        text,
    )
    value = _INGEST_JWT_LIKE_RE.sub("[REDACTED:jwt]", value)
    return value


def _ingest_compact_error_text(text: Any, *, limit: int = 320) -> str:
    value = " ".join(str(text or "").strip().split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _ingest_classify_agent_error(error: Any) -> str:
    text = _ingest_compact_error_text(error)
    lower = text.lower()
    if "max_iterations_reached" in lower or "maximum iterations" in lower:
        return f"agent_max_iterations: {text or 'agent exhausted tool iterations'}"
    if "cannot schedule new futures after interpreter shutdown" in lower:
        return f"agent_tool_loop_error: {text}"
    if "outer loop error" in lower or "tool loop" in lower:
        return f"agent_tool_loop_error: {text}"
    if "aiagent subprocess" in lower:
        return f"agent_runtime_error: {text}"
    if isinstance(error, BaseException):
        detail = text or type(error).__name__
        return f"agent_runtime_error: {type(error).__name__}: {detail}"
    return f"agent_runtime_error: {text or 'agent run failed'}"


def _ingest_run_session_id(prepared: Any) -> str:
    run_request = getattr(prepared, "run_request", None)
    return str(getattr(run_request, "session_id", "") or "")


def _ingest_materialize_secret_dir(
    *,
    profile_name: str,
    run_id: str,
    secret_spec: SimpleNamespace | None,
) -> str:
    if not secret_spec or not getattr(secret_spec, "entries", None):
        return ""
    root = _m._profile_home_for_name(profile_name) / "tmp" / "ingest-secrets"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except Exception:
        pass
    secret_dir = root / run_id
    secret_dir.mkdir(parents=False, exist_ok=False, mode=0o700)
    for item in secret_spec.entries:
        path = secret_dir / item["name"]
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, item["value"].encode("utf-8"))
        finally:
            os.close(fd)
        try:
            path.chmod(0o600)
        except Exception:
            pass
    logger.info(
        "[multitenancy] ingest secrets materialized profile=%s run_id=%s secrets=%s",
        profile_name,
        run_id,
        [
            {
                "name": item["name"],
                "type": item["type"],
                "sha256": str(item["sha256"])[:12],
            }
            for item in secret_spec.entries
        ],
    )
    return str(secret_dir)


def _ingest_cleanup_secret_dir(secret_dir: str | os.PathLike[str] | None) -> None:
    if not secret_dir:
        return
    try:
        shutil.rmtree(Path(secret_dir), ignore_errors=True)
    except Exception:
        logger.debug("[multitenancy] failed to cleanup ingest secret dir", exc_info=True)


def _ingest_secret_mismatch_response(profile_name: str):
    from aiohttp import web

    return web.json_response(
        {
            "ok": False,
            "status": "secret_mismatch",
            "error": "secret_mismatch",
            "profile": profile_name,
        },
        status=409,
    )


def _ingest_scoped_broker_idempotency_key(
    *,
    auth_fingerprint: str,
    profile_name: str,
    effective_idempotency_key: str,
) -> str:
    payload = json.dumps(
        {
            "auth": auth_fingerprint,
            "profile": profile_name,
            "key": effective_idempotency_key,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "ingest:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ingest_owner() -> str:
    """Owner (Feishu open_id) bound to the ingest key, or '' for v1 fixed-profile mode.

    When set, the ingest endpoint runs in OWNER mode: the caller picks which of
    this owner's agents to run via the ``agent`` field, and the server validates
    that the chosen agent is actually owned by this owner. A leaked key can only
    ever reach agents this one owner owns — never another owner's, never an
    arbitrary profile.
    """
    return os.environ.get("HERMES_INGEST_OWNER", "").strip()


def _ingest_list_owner_agents(owner: str) -> list:
    """Return the active KIND_AGENT routing rows owned by ``owner`` ([] on failure).

    Defense in depth: re-filters on ``owner_open_id == owner`` even though the
    routing query is already owner-scoped.
    """
    if not owner:
        return []
    try:
        from .. import router as router_mod

        table = router_mod._get_routing_table()
        if table is None:
            return []
        rows = table.list_agents_for_owner(owner)
        return [
            r
            for r in rows
            if getattr(r, "kind", None) == "agent"
            and (getattr(r, "owner_open_id", None) or "") == owner
        ]
    except Exception:
        logger.debug("[multitenancy] ingest owner-agent enumeration failed", exc_info=True)
        return []


def _ingest_agent_label(row: Any) -> str:
    return str(getattr(row, "display_label", None) or getattr(row, "agent_id", None) or getattr(row, "user_id", ""))


def _ingest_agent_id(row: Any) -> str:
    return str(getattr(row, "agent_id", None) or getattr(row, "user_id", ""))


def _ingest_resolve_agent(owner: str, agent_str: str) -> tuple[Optional[str], list[str]]:
    """Resolve a caller-supplied ``agent`` to a profile_name for ``owner``.

    Matches by display_label / agent_id / user_id among the owner's own agents.
    Returns ``(profile_name, available_names)``; profile_name is None when the
    agent is not found or not owned by ``owner`` (caller cannot reach another
    owner's agent). ``available_names`` is always the owner's own agent list so
    error responses can hint what is valid.
    """
    agents = _ingest_list_owner_agents(owner)
    available = [_ingest_agent_label(r) for r in agents]
    target = (agent_str or "").strip()
    if not target:
        return None, available
    for r in agents:
        candidates = {
            getattr(r, "display_label", None),
            getattr(r, "agent_id", None),
            getattr(r, "user_id", None),
        }
        if target in candidates and (getattr(r, "owner_open_id", None) or "") == owner:
            return str(getattr(r, "profile_name", "")), available
    return None, available


async def _read_capped_body(request: Any, cap: int) -> Optional[bytes]:
    """Read the request body incrementally, returning None if it exceeds ``cap``.

    Streaming from ``request.content`` (rather than ``request.read()``) means a
    chunked / no-Content-Length body cannot force buffering up to the app-level
    ``client_max_size`` (32MB) before the size check runs.
    """
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await request.content.read(65536)
        if not chunk:
            break
        size += len(chunk)
        if size > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _default_sandbox_available() -> bool:
    value = os.environ.get("HERMES_USE_SANDBOX", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _tenant_from_request(request: Any, payload: Optional[dict[str, Any]] = None) -> tuple[str, str]:
    payload = payload or {}
    profile_name = (
        request.headers.get("X-Hermes-Profile")
        or payload.get("profile_name")
        or payload.get("profile")
        or ""
    )
    user_key = (
        request.headers.get("X-Hermes-User-Key")
        or payload.get("user_key")
        or payload.get("user")
        or ""
    )
    return cron_api.validate_profile_name(str(profile_name)), str(user_key or "").strip()


def _tenant_payload_from_query(request: Any) -> dict[str, Any]:
    return {
        "profile_name": request.query.get("profile_name") or request.query.get("profile"),
        "user_key": request.query.get("user_key") or request.query.get("user"),
    }


def _apply_expert_id_to_metadata(
    request: Any, payload: dict[str, Any], metadata: dict[str, Any]
) -> None:
    """Thread the per-run ``expert_id`` into ``metadata`` so ``agent_real`` can
    overlay that expert's Role-Override persona for this run only.

    Source precedence: header ``X-Hermes-Expert-Id`` > ``payload.metadata.expert_id``
    > top-level ``payload.expert_id``. A blank value clears it (no overlay). The id
    is clamped to a conservative charset (an untrusted value eventually keys a
    managed-manifest lookup; this keeps it from carrying separators / control chars).
    """
    raw = (
        request.headers.get(_EXPERT_ID_HEADER)
        or metadata.get("expert_id")
        or payload.get("expert_id")
        or ""
    )
    eid = str(raw or "").strip()
    if eid and all(c.isalnum() or c in "-_.:" for c in eid):
        metadata["expert_id"] = eid
    else:
        metadata.pop("expert_id", None)


def _trusted_owner_from_request(request: Any) -> str:
    return str(request.headers.get(_OWNER_OPEN_ID_HEADER, "") or "").strip()


def _actor_principal_id_from_request(request: Any) -> str:
    return str(request.headers.get(_ACTOR_PRINCIPAL_ID_HEADER, "") or "").strip()


def _actor_display_name_from_request(request: Any) -> str:
    encoded = str(request.headers.get(_ACTOR_DISPLAY_NAME_ENCODED_HEADER, "") or "").strip()
    if encoded:
        try:
            return urllib.parse.unquote(encoded, encoding="utf-8", errors="strict").strip()
        except UnicodeDecodeError:
            return ""
    return str(request.headers.get(_ACTOR_DISPLAY_NAME_HEADER, "") or "").strip()


def _actor_principal_id_for_request(table: Any, request: Any, actor_open_id: str = "") -> str:
    principal_id = _actor_principal_id_from_request(request)
    if principal_id:
        return principal_id

    provider = str(request.headers.get(_ACTOR_PROVIDER_HEADER, "") or "").strip()
    tenant_key = str(request.headers.get(_ACTOR_TENANT_KEY_HEADER, "") or "").strip()
    app_id = str(request.headers.get(_ACTOR_APP_ID_HEADER, "") or "").strip()
    user_id = str(request.headers.get(_ACTOR_USER_ID_HEADER, "") or "").strip()
    if not (provider and tenant_key and user_id):
        return ""

    email = str(request.headers.get(_ACTOR_EMAIL_HEADER, "") or "").strip()
    aliases: list[dict[str, str]] = []
    if email:
        aliases.append({"id_type": "email", "id_value": email})
    actor_open_id = (actor_open_id or _trusted_owner_from_request(request)).strip()
    if actor_open_id and app_id:
        aliases.append({"id_type": "open_id", "id_value": actor_open_id, "app_id": app_id})

    principal = table.upsert_principal(
        provider=provider,
        tenant_key=tenant_key,
        canonical_id_type="user_id",
        canonical_id=user_id,
        display_name=_actor_display_name_from_request(request) or None,
        avatar_url=str(request.headers.get(_ACTOR_AVATAR_URL_HEADER, "") or "").strip() or None,
        email=email or None,
        aliases=aliases,
    )
    return principal.principal_id


def _requested_profile_name(payload: dict[str, Any]) -> str:
    raw = str(payload.get("profile_name") or payload.get("profile") or "").strip()
    if not raw:
        return ""
    return cron_api.validate_profile_name(raw)


def _assert_requested_profile_matches(*, resolved_profile_name: str, payload: dict[str, Any]) -> None:
    requested = _requested_profile_name(payload)
    if requested and requested != resolved_profile_name:
        raise PermissionError(f"profile_name '{requested}' is not accessible for asserted owner")


def _parse_session_history_command(raw: Any) -> tuple[str, str]:
    text = str(raw or "").strip()
    match = _SESSION_COMMAND_RE.match(text)
    if not match:
        raise ValueError("command must be a slash command")
    name = match.group(1).lower().replace("_", "-")
    return name, (match.group(2) or "").strip()


def _dispatch_session_history_command(
    *, profile_name: str, user_key: str, command: Any, session_id: str = ""
) -> dict[str, Any]:
    from .. import router as router_mod

    command_name, _args = _parse_session_history_command(command)
    if command_name not in _SESSION_HISTORY_COMMANDS:
        return {
            "handled": False,
            "command": command_name,
            "action": "unsupported",
            "message": f"not a supported Run Broker session-history command: /{command_name}",
        }

    # WebUI sessions must not cross with Feishu DM sessions (channel="webui") and,
    # under strict context, distinct WebUI sessions for the same owner must not
    # collapse into one key — so carry session_id as the thread dimension. In
    # default mode both are ignored and the key stays byte-identical.
    key = router_mod._history_key(
        profile_name, user_key, None, channel="webui", thread_id=(session_id or None)
    )
    if command_name in {"new", "reset"}:
        cleared = bool(router_mod._clear_history(key))
        return {
            "handled": True,
            "command": command_name,
            "type": "session_history",
            "action": "reset",
            "message": "会话历史已重置 ✅" if cleared else "会话历史已经为空。",
            "history_count": 0,
        }

    history = router_mod._load_history(key)
    return {
        "handled": True,
        "command": command_name,
        "type": "session_history",
        "action": "status",
        "message": (
            "状态: 空闲\n"
            f"profile: {profile_name}\n"
            f"会话历史: {len(history)} 条消息"
        ),
        "history_count": len(history),
    }


def _profile_home_for_name(profile_name: str) -> Path:
    from .. import router as router_mod

    return router_mod._profile_name_to_home(profile_name)


def _read_skill_name(skill_md: Path) -> str:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return ""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            for line in text[4:end].splitlines():
                if line.startswith("name:"):
                    return line.split(":", 1)[1].strip().strip("'\"").lstrip("/")
    return skill_md.parent.name


def _find_plan_skill(profile_home: Path) -> tuple[Path, str] | None:
    from .. import skill_registry

    roots = [profile_home / "skills", _shared_home_from_env() / "skills"]
    for root in roots:
        if not root.is_dir():
            continue
        for skill_md in skill_registry._iter_skill_files(root):
            if _read_skill_name(skill_md).lower().replace("_", "-") == "plan":
                try:
                    return skill_md.parent, skill_md.read_text(encoding="utf-8")
                except OSError:
                    return None
    return None


@asynccontextmanager
async def _profile_home_context(profile_home: Path):
    from ..runtime import _get_env_lock

    async with _get_env_lock():
        old_home = os.environ.get("HERMES_HOME")
        had_home = "HERMES_HOME" in os.environ
        os.environ["HERMES_HOME"] = str(profile_home)
        try:
            yield
        finally:
            if had_home:
                os.environ["HERMES_HOME"] = old_home or ""
            else:
                os.environ.pop("HERMES_HOME", None)


def _dispatch_plan_command(*, profile_name: str, command: Any) -> dict[str, Any]:
    command_name, args = _parse_session_history_command(command)
    if command_name != "plan":
        raise ValueError("not a plan command")
    profile_home = _m._profile_home_for_name(profile_name)
    found = _find_plan_skill(profile_home)
    if not found:
        return {
            "handled": False,
            "command": "plan",
            "action": "unsupported",
            "message": "plan skill is not installed for this profile",
        }
    _skill_dir, skill_text = found
    prompt = "\n".join([
        '[SYSTEM: The user invoked the "plan" skill. Follow the full skill instructions below.]',
        "",
        skill_text.strip(),
        "",
        "[Skill source: profile-local plan skill; relative output paths are relative to the active profile home.]",
        "",
        f"User request: {args}",
    ]).strip()
    return {
        "handled": True,
        "command": "plan",
        "type": "skill",
        "action": "plan",
        "message": prompt,
    }


def _goal_manager(session_id: str):
    from hermes_cli.goals import GoalManager

    return GoalManager(session_id)


async def _dispatch_goal_command(*, profile_name: str, session_id: str, command: Any) -> dict[str, Any]:
    command_name, args = _parse_session_history_command(command)
    if command_name not in _GOAL_COMMANDS:
        raise ValueError("not a goal command")
    profile_home = _m._profile_home_for_name(profile_name)

    def run() -> dict[str, Any]:
        manager = _m._goal_manager(session_id)
        if command_name == "subgoal":
            text = args.strip()
            if not text or text.lower() in {"list", "status"}:
                return {
                    "handled": True,
                    "command": "subgoal",
                    "type": "goal",
                    "action": "status",
                    "message": manager.render_subgoals(),
                }
            if text.lower() == "clear":
                count = manager.clear_subgoals()
                return {
                    "handled": True,
                    "command": "subgoal",
                    "type": "goal",
                    "action": "clear",
                    "message": f"Cleared {count} subgoal(s).",
                }
            added = manager.add_subgoal(text)
            return {
                "handled": True,
                "command": "subgoal",
                "type": "goal",
                "action": "add",
                "message": f"Subgoal added: {added}",
            }

        text = args.strip()
        lower = text.lower()
        if not text or lower == "status":
            return {
                "handled": True,
                "command": "goal",
                "type": "goal",
                "action": "status",
                "message": manager.status_line(),
            }
        if lower == "clear":
            manager.clear()
            return {
                "handled": True,
                "command": "goal",
                "type": "goal",
                "action": "clear",
                "message": "Goal cleared.",
                "clear_goal_continuations": True,
            }
        if lower == "pause":
            manager.pause("user-paused")
            return {
                "handled": True,
                "command": "goal",
                "type": "goal",
                "action": "pause",
                "message": "Goal paused.",
                "clear_goal_continuations": True,
            }
        if lower == "resume":
            state = manager.resume()
            prompt = manager.next_continuation_prompt() if state else None
            return {
                "handled": True,
                "command": "goal",
                "type": "goal",
                "action": "resume",
                "message": manager.status_line(),
                "kickoff_prompt": prompt,
                "max_turns": state.max_turns if state else None,
            }
        if lower in {"done", "complete"}:
            mark_done = getattr(manager, "mark_done", None)
            if not callable(mark_done):
                raise ValueError("GoalManager does not support /goal done")
            mark_done("user-completed")
            return {
                "handled": True,
                "command": "goal",
                "type": "goal",
                "action": "done",
                "message": "Goal marked done.",
                "clear_goal_continuations": True,
            }

        state = manager.set(text)
        return {
            "handled": True,
            "command": "goal",
            "type": "goal",
            "action": "set",
            "message": "Goal set.",
            "kickoff_prompt": text,
            "max_turns": state.max_turns,
            "clear_goal_continuations": True,
        }

    async with _profile_home_context(profile_home):
        return run()


async def _dispatch_session_command(*, profile_name: str, user_key: str, session_id: str, command: Any) -> dict[str, Any]:
    command_name, _args = _parse_session_history_command(command)
    if command_name in _SESSION_HISTORY_COMMANDS:
        return _dispatch_session_history_command(
            profile_name=profile_name,
            user_key=user_key,
            command=command,
            session_id=session_id,
        )
    if command_name == "plan":
        return _dispatch_plan_command(profile_name=profile_name, command=command)
    if command_name in _GOAL_COMMANDS:
        return await _dispatch_goal_command(
            profile_name=profile_name,
            session_id=session_id,
            command=command,
        )
    return {
        "handled": False,
        "command": command_name,
        "action": "unsupported",
        "message": f"not a supported Run Broker session command: /{command_name}",
    }


async def _evaluate_goal_after_turn(*, profile_name: str, session_id: str, final_response: Any) -> dict[str, Any]:
    profile_home = _m._profile_home_for_name(profile_name)
    final_text = str(final_response or "").strip()

    def run() -> dict[str, Any]:
        manager = _m._goal_manager(session_id)
        decision = manager.evaluate_after_turn(final_text, user_initiated=False)
        state = manager.state
        return {
            "handled": True,
            "active": bool(state and state.status == "active"),
            **decision,
        }

    async with _profile_home_context(profile_home):
        return run()


def _kanban_board_from_request(request: Any) -> str:
    return str(request.query.get("board") or "default").strip() or "default"


def _optional_positive_int(value: Any, name: str, *, max_value: int = 100) -> int | None:
    if value is None or value == "":
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    if value > max_value:
        raise ValueError(f"{name} must be <= {max_value}")
    return value


def _optional_bool(value: Any, name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
    return value


def _webui_agent_id(owner_open_id: str, profile_name: str) -> str:
    return f"webui:{owner_open_id}:{profile_name}"


def _webui_owned_profile_name(owner_open_id: str, profile_name: str) -> str:
    owner_hash = hashlib.sha256(owner_open_id.encode("utf-8")).hexdigest()[:12]
    name_hash = hashlib.sha256(profile_name.encode("utf-8")).hexdigest()[:10]
    prefix = f"webui_{owner_hash}_"
    suffix = f"_{name_hash}"
    stem = profile_name[:128 - len(prefix) - len(suffix)]
    return cron_api.validate_profile_name(f"{prefix}{stem}{suffix}")


def _owner_allowed_upstream_profiles(table: Any, trusted_owner: str, owner_root: Any) -> set[str]:
    profiles = {str(owner_root.profile_name)}
    try:
        profiles.update(
            str(row.profile_name)
            for row in table.list_by_owner(trusted_owner)
            if getattr(row, "profile_name", None)
        )
    except Exception:
        logger.exception("[multitenancy] failed to enumerate owner profiles for upstream validation")
    return profiles


def _shared_agent_payload(route: Any, role: str) -> dict[str, Any]:
    return {
        "agent_id": str(getattr(route, "agent_id", None) or getattr(route, "user_id", "")),
        "profile_name": str(getattr(route, "profile_name", "") or ""),
        "owner_open_id": str(getattr(route, "owner_open_id", "") or ""),
        "display_label": str(getattr(route, "display_label", "") or ""),
        "role": role,
    }


def _agent_share_payload(share: Any) -> dict[str, Any]:
    payload = {
        "agent_id": str(getattr(share, "agent_id", "") or ""),
        "grantee_open_id": str(getattr(share, "grantee_open_id", "") or ""),
        "share_id": str(getattr(share, "share_id", "") or ""),
        "grantee_principal_id": str(getattr(share, "grantee_principal_id", "") or ""),
        "role": str(getattr(share, "role", "") or ""),
        "status": str(getattr(share, "status", "") or ""),
        "created_by_open_id": str(getattr(share, "created_by_open_id", "") or ""),
        "created_by_principal_id": str(getattr(share, "created_by_principal_id", "") or ""),
        "created_at": int(getattr(share, "created_at", 0) or 0),
        "updated_at": int(getattr(share, "updated_at", 0) or 0),
        "revoked_at": getattr(share, "revoked_at", None),
    }
    principal = _principal_payload_from_share(share)
    if principal:
        payload["principal"] = principal
    return payload


def _principal_payload_from_share(share: Any) -> dict[str, Any]:
    provider = str(getattr(share, "principal_provider", "") or "").strip()
    if not provider:
        return {}
    canonical_type = str(getattr(share, "principal_canonical_id_type", "") or "").strip()
    canonical_id = str(getattr(share, "principal_canonical_id", "") or "").strip()
    payload: dict[str, Any] = {
        "provider": provider,
        "display_name": str(getattr(share, "principal_display_name", "") or ""),
        "avatar_url": str(getattr(share, "principal_avatar_url", "") or ""),
        "email": str(getattr(share, "principal_email", "") or ""),
    }
    if canonical_type == "user_id" and canonical_id:
        payload["user_id"] = canonical_id
    return payload


def _resolve_agent_manager(
    table: Any,
    actor_open_id: str,
    agent_id: str,
    actor_principal_id: str = "",
) -> tuple[Any, Optional[str], int, str]:
    actor_open_id = (actor_open_id or "").strip()
    actor_principal_id = (actor_principal_id or "").strip()
    agent_id = (agent_id or "").strip()
    if not actor_open_id and not actor_principal_id:
        return None, "owner identity required (X-Hermes-Owner-Open-Id or X-Hermes-Actor-Principal-Id)", 403, ""
    if not agent_id:
        return None, "agent_id required", 400, ""
    row = table.lookup_agent(agent_id)
    if row is None or not getattr(row, "active", False):
        return None, f"agent_id '{agent_id}' not found", 404, ""
    if actor_open_id and getattr(row, "owner_open_id", None) == actor_open_id:
        return row, None, 200, "owner"
    if actor_principal_id and table.get_agent_share_role_for_principal(agent_id, actor_principal_id) == "manager":
        return row, None, 200, "manager"
    if table.get_agent_share_role(agent_id, actor_open_id) == "manager":
        return row, None, 200, "manager"
    return None, "manager role required for this agent", 403, ""


def _resolve_identity_lookup(table: Any, lookup: dict[str, Any], requester: Any) -> Any:
    provider = str(lookup.get("provider") or "").strip()
    lookup_type = str(lookup.get("type") or lookup.get("id_type") or "").strip()
    value = str(lookup.get("value") or lookup.get("id_value") or "").strip()
    tenant_key = str(getattr(requester, "tenant_key", "") or "").strip()
    if not (provider and lookup_type and value and tenant_key):
        raise ValueError("provider, type, value, and requester tenant are required")
    if lookup_type == "user_id":
        principal = table.find_principal(
            provider=provider,
            tenant_key=tenant_key,
            canonical_id_type="user_id",
            canonical_id=value,
        )
    else:
        principal = table.lookup_principal_by_alias(
            provider=provider,
            tenant_key=tenant_key,
            id_type=lookup_type,
            id_value=value,
        )
    if principal is None and provider == "feishu":
        principal = _resolve_feishu_identity_lookup_from_directory(
            table,
            tenant_key=tenant_key,
            lookup_type=lookup_type,
            value=value,
        )
    if principal is None:
        raise ValueError("identity resolver unavailable for requested principal")
    return principal


def _resolve_feishu_identity_lookup_from_directory(
    table: Any,
    *,
    tenant_key: str,
    lookup_type: str,
    value: str,
) -> Any:
    if lookup_type == "user_id":
        route = table.lookup_by_user_id(value)
        aliases: list[dict[str, str]] = []
        if route is not None and getattr(route, "open_id", None):
            aliases.append({"id_type": "open_id", "id_value": str(route.open_id), "app_id": ""})
        return table.upsert_principal(
            provider="feishu",
            tenant_key=tenant_key,
            canonical_id_type="user_id",
            canonical_id=value,
            display_name=str(getattr(route, "display_label", "") or "") or None,
            aliases=aliases,
        )

    if lookup_type != "email":
        return None

    try:
        api_delay = float(os.environ.get("HERMES_WEBUI_FEISHU_IDENTITY_API_DELAY_SECONDS", "0") or "0")
    except ValueError:
        api_delay = 0.0
    try:
        from ..sync import feishu_org

        directory = feishu_org.fetch_contact_directory(api_delay=api_delay)
    except Exception:
        logger.exception("[multitenancy] failed to resolve Feishu share grantee from contact directory")
        return None

    target_email = value.casefold()
    for open_id, entry in directory.items():
        email = str((entry or {}).get("email") or "").strip()
        if email.casefold() != target_email:
            continue
        route = table.lookup_by_open_id(str(open_id))
        user_id = str(getattr(route, "user_id", "") or "").strip() if route else ""
        if not user_id:
            continue
        return table.upsert_principal(
            provider="feishu",
            tenant_key=tenant_key,
            canonical_id_type="user_id",
            canonical_id=user_id,
            display_name=str((entry or {}).get("name") or getattr(route, "display_label", "") or "").strip() or None,
            email=email,
            aliases=[{"id_type": "email", "id_value": email}],
        )
    return None


def _resolve_owner_scoped_profile(
    request: Any,
    payload: dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """Resolve broker profile_name from a server-asserted owner boundary.

    `X-Hermes-Owner-Open-Id` is trusted only when it arrives as a request
    header. The WebUI BFF HMAC-verifies Feishu identity in
    `hermes-web-ui/packages/server/src/services/request-context.ts:41-69`
    (`verifyTrustedFeishuHeaders`), derives `WebUser.openid`, and forwards the
    verified owner to this localhost-only broker on the same trust basis as the
    shared bearer checked by `_authorized`. This mirrors the existing
    server-stamped-owner pattern in
    `hermes-web-ui/packages/server/src/controllers/hermes/jobs.ts:73-99`
    (`normalizeChatPlaneJobBody`): client owner fields are stripped and the
    verified openid is re-injected server-side. Client-supplied `profile_name`
    remains a compatibility field, but it is never trusted for ownership when
    the trusted owner header is present.
    """
    trusted_owner = str(request.headers.get(_OWNER_OPEN_ID_HEADER, "") or "").strip()
    if not trusted_owner:
        if _owner_enforcement_enabled():
            return None, "owner identity required (X-Hermes-Owner-Open-Id)"
        return None, None

    from .. import router as router_mod

    table = router_mod._get_routing_table()
    if table is None:
        # Fail closed: a trusted owner assertion without routing state cannot be
        # verified safely, so never fall back to client-supplied profile_name.
        return None, "trusted owner header requires routing table verification"

    agent_id = str(
        request.headers.get(_AGENT_ID_HEADER)
        or payload.get("agent_id")
        or ""
    ).strip()
    owner_root = table.resolve_owner_root(trusted_owner)

    if agent_id:
        row = table.lookup_agent(agent_id)
        if row is None or not row.active:
            return None, f"agent_id '{agent_id}' is not accessible for asserted owner"
        matches_owner_root = (
            owner_root is not None
            and row.user_id == owner_root.user_id
            and row.profile_name == owner_root.profile_name
        )
        share_role = None
        if row.owner_open_id != trusted_owner and not matches_owner_root:
            actor_principal_id = _actor_principal_id_for_request(table, request, trusted_owner)
            if actor_principal_id:
                share_role = table.get_agent_share_role_for_principal(agent_id, actor_principal_id)
            if not share_role:
                share_role = table.get_agent_share_role(agent_id, trusted_owner)
        if (
            row.owner_open_id != trusted_owner
            and not matches_owner_root
            and share_role is None
        ):
            return None, f"agent_id '{agent_id}' does not belong to asserted owner"
        return row.profile_name, None

    if owner_root is None:
        return None, f"asserted owner '{trusted_owner}' has no sync-root profile"
    return owner_root.profile_name, None


def _owner_scoped_tenant(request: Any, payload: Optional[dict[str, Any]] = None) -> tuple[str, str]:
    """Return (profile_name, user_key) bound to a server-asserted owner.

    Raises PermissionError (caller maps to HTTP 403) when the owner is missing
    or the requested profile is not owned by / shared to that owner. Fail-closed:
    a trusted-owner assertion that cannot be verified against routing state never
    falls back to the client-supplied profile_name.
    """
    payload = payload or {}
    trusted_owner = ""
    for value in (
        request.headers.get(_OWNER_OPEN_ID_HEADER),
        request.headers.get("X-Hermes-User-Key"),
        payload.get("user_key"),
        payload.get("user"),
        request.query.get("user_key"),
        request.query.get("user"),
    ):
        trusted_owner = str(value or "").strip()
        if trusted_owner:
            break
    if not trusted_owner:
        raise PermissionError("owner identity required (X-Hermes-Owner-Open-Id)")

    requested_profile = ""
    for value in (
        request.headers.get("X-Hermes-Profile"),
        payload.get("profile_name"),
        payload.get("profile"),
        request.query.get("profile_name"),
        request.query.get("profile"),
    ):
        requested_profile = str(value or "").strip()
        if requested_profile:
            break
    if requested_profile:
        requested_profile = cron_api.validate_profile_name(requested_profile)

    from .. import router as router_mod

    table = router_mod._get_routing_table()
    if table is None:
        raise PermissionError("trusted owner header requires routing table verification")

    owner_root = table.resolve_owner_root(trusted_owner)
    owned = set()
    if owner_root is not None:
        owned.add(owner_root.profile_name)
    for row in table.list_agents_for_owner(trusted_owner):
        owned.add(row.profile_name)

    if requested_profile:
        if requested_profile in owned:
            profile = requested_profile
        else:
            row = table.lookup_by_profile_name(requested_profile)
            shared_ok = False
            if row is not None and getattr(row, "agent_id", None):
                try:
                    role = table.get_agent_share_role(row.agent_id, trusted_owner)
                except Exception:
                    role = None
                shared_ok = bool(role)
            if shared_ok:
                profile = requested_profile
            else:
                raise PermissionError(
                    f"profile_name '{requested_profile}' is not accessible for asserted owner"
                )
    else:
        if owner_root is None:
            raise PermissionError(f"asserted owner '{trusted_owner}' has no sync-root profile")
        profile = owner_root.profile_name

    return profile, trusted_owner


def _agent_share_context_for_request(
    request: Any,
    payload: dict[str, Any],
    *,
    resolved_profile_name: str,
) -> dict[str, str]:
    trusted_owner = str(request.headers.get(_OWNER_OPEN_ID_HEADER, "") or "").strip()
    agent_id = str(
        request.headers.get(_AGENT_ID_HEADER)
        or payload.get("agent_id")
        or ""
    ).strip()
    if not (trusted_owner and agent_id and resolved_profile_name):
        return {}

    from .. import router as router_mod

    table = router_mod._get_routing_table()
    if table is None:
        return {}
    row = table.lookup_agent(agent_id)
    if row is None or not row.active or row.profile_name != resolved_profile_name:
        return {}
    if row.owner_open_id == trusted_owner:
        share_role = "owner"
    else:
        actor_principal_id = _actor_principal_id_for_request(table, request, trusted_owner)
        share_role = (
            table.get_agent_share_role_for_principal(agent_id, actor_principal_id)
            if actor_principal_id
            else None
        ) or table.get_agent_share_role(agent_id, trusted_owner) or ""
    if not share_role:
        return {}
    return {
        "agent_id": agent_id,
        "agent_owner_open_id": str(row.owner_open_id or ""),
        "actor_open_id": trusted_owner,
        "share_role": share_role,
    }


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        return await value
    return value


def _is_client_transport_closed(exc: BaseException) -> bool:
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        return True
    if exc.__class__.__name__ == "ClientConnectionResetError":
        return True
    # asyncio transports can surface closed SSE writes as a RuntimeError with this text.
    return "Cannot write to closing transport" in str(exc)


class _DisconnectTolerantEmitter:
    def __init__(self, emit_event: EmitRunEvent) -> None:
        self._emit_event = emit_event
        self.disconnected = False

    async def emit(self, event: RunEvent) -> bool:
        if self.disconnected:
            return False
        try:
            await _maybe_await(self._emit_event(event))
            return True
        except Exception as exc:
            if _is_client_transport_closed(exc):
                self.disconnected = True
                logger.info(
                    "[multitenancy] WebUI SSE client disconnected; continuing run without streaming"
                )
                return False
            raise


def _event_to_sse(event: RunEvent) -> str:
    data = {
        "kind": event.kind,
        "text": event.text,
        "name": event.name,
        "payload": event.payload,
    }
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sanitize_clarify_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    cleaned.pop("response_path", None)
    return cleaned


def _register_pending_clarify(run_request: RunRequest, payload: dict[str, Any]) -> dict[str, Any]:
    clarify_id = str(payload.get("clarify_id") or "").strip()
    response_path = str(payload.get("response_path") or "").strip()
    if not clarify_id or not response_path:
        return _sanitize_clarify_payload(payload)
    _pending_clarifies[clarify_id] = {
        "clarify_id": clarify_id,
        "owner_open_id": str(run_request.user_key or "").strip(),
        "profile_name": str(run_request.profile_name or "").strip(),
        "session_id": str(run_request.session_id or "").strip(),
        "response_path": response_path,
        "created_at": time.time(),
    }
    return _sanitize_clarify_payload(payload)


def _clear_pending_clarify(payload: dict[str, Any]) -> None:
    clarify_id = str(payload.get("clarify_id") or "").strip()
    if clarify_id:
        _pending_clarifies.pop(clarify_id, None)


def _write_pending_clarify_response(
    *,
    clarify_id: str,
    owner_open_id: str,
    profile_name: str,
    session_id: str,
    response_text: str,
) -> bool:
    pending = _pending_clarifies.get(clarify_id)
    if pending is None:
        return False
    if str(pending.get("owner_open_id") or "") != owner_open_id:
        return False
    if str(pending.get("profile_name") or "") != profile_name:
        return False
    if str(pending.get("session_id") or "") != session_id:
        return False

    response_path = Path(str(pending.get("response_path") or ""))
    response_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_text(
        json.dumps({"response": response_text}, ensure_ascii=False),
        encoding="utf-8",
    )
    return True


def _webui_current_datetime_instruction() -> str:
    now = datetime.now().astimezone()
    timezone = now.tzname() or str(now.astimezone().tzinfo)
    return (
        f"Current date/time: {now.strftime('%Y-%m-%d %H:%M:%S %z')}. "
        f"Timezone: {timezone}. Interpret relative dates such as today, "
        "tomorrow, 今天, 明天, and noon from this value. For calendar work, "
        "derive timestamps from this value and verify the created event time "
        "before reporting success. When the user asks for calendar attachments "
        "or meeting rooms, inspect the API response and report failure rather "
        "than success if the attachment is missing or the room attendee declined."
    )


def _metadata_with_webui_runtime_context(metadata: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(metadata or {})
    existing = str(merged.get("instructions") or "").strip()
    current = _webui_current_datetime_instruction()
    merged["instructions"] = "\n".join(part for part in [existing, current] if part)
    return merged


def _build_webui_event(request: RunRequest) -> Any:
    """Create the smallest event shape expected by ProfileRuntime/agent_real."""
    metadata = _metadata_with_webui_runtime_context(request.metadata)
    secret_prompt = _ingest_secret_manifest_prompt(
        list(metadata.get("ingest_secrets") or [])
        if isinstance(metadata.get("ingest_secrets"), list)
        else []
    )
    event_text = request.content if not secret_prompt else f"{secret_prompt}\n\n{request.content}"
    return SimpleNamespace(
        text=event_text,
        message_id=request.message_id,
        channel="webui",
        sender_open_id=request.user_key if str(request.user_key or "").startswith("ou_") else "",
        source=SimpleNamespace(
            platform=SimpleNamespace(value=request.channel),
            user_id=request.user_key,
            open_id=request.user_key,
            user_id_alt=None,
            chat_id=request.chat_id or "",
            chat_type="webui",
            message_id=request.message_id or "",
        ),
        raw_event={
            "channel": request.channel,
            "session_id": request.session_id,
            "metadata": metadata,
        },
    )


async def _default_dispatch_agent(
    request: RunRequest,
    *,
    emit_event: Optional[EmitRunEvent] = None,
    auth_signal_run_id: Optional[str] = None,
) -> str:
    # Plan-A surgical fix (user-approved 2026-05-17). Keeps the streaming path
    # so tool_started/tool_completed/thinking frames reach the WebUI (the
    # tool-call panel beside the avatar) — those emit paths were already
    # correct. The ONLY defect was: content frames were merely accumulated and
    # then RETURNED, so RunBroker.run re-emitted the whole answer a second time
    # (duplicated answer + reasoning leaking into content). Fix: emit each
    # content delta as a content frame (single delivery), and return "" so
    # RunBroker.run's `if content:` is skipped and never re-emits. Only when
    # nothing streamed at all do we fall back to a one-shot run (emitted once
    # by RunBroker, no duplication possible since no frames were sent).
    from .. import router as router_mod
    from ..agent_real import real_run_agent, stream_run_agent
    from ..runtime import _PROFILE_HOME_VAR

    profile_home = router_mod._profile_name_to_home(request.profile_name)
    event = _build_webui_event(request)
    if emit_event is None:
        return await router_mod._get_pool().dispatch(request.profile_name, profile_home, event)

    emitter = _DisconnectTolerantEmitter(emit_event)
    content_parts: list[str] = []
    pending_media_text = ""
    token = _PROFILE_HOME_VAR.set(profile_home)
    try:
        messages = request.messages or None
        async for kind, payload in stream_run_agent(event, profile_home, messages=messages):
            if kind == "content":
                pending_media_text, text_items = _m._webui_streamable_media_text(
                    pending_media_text + str(payload or ""),
                    router_mod=router_mod,
                    profile_home=profile_home,
                )
                for text in text_items:
                    if text:
                        content_parts.append(text)
                        await emitter.emit(
                            RunEvent(
                                kind="content",
                                text=text,
                                payload={"text": text},
                            )
                        )
                continue
            if kind == "thinking":
                text = str(payload or "")
                if text:
                    await emitter.emit(
                        RunEvent(
                            kind="thinking",
                            text=text,
                            payload={"text": text},
                        )
                    )
                continue
            if kind == "tool_started":
                tool_payload = dict(payload or {}) if isinstance(payload, dict) else {"preview": payload}
                await emitter.emit(
                    RunEvent(
                        kind="tool_started",
                        name=str(tool_payload.get("name") or ""),
                        payload=tool_payload,
                    )
                )
                continue
            if kind == "tool_completed":
                tool_payload = dict(payload or {}) if isinstance(payload, dict) else {"output": payload}
                await emitter.emit(
                    RunEvent(
                        kind="tool_completed",
                        name=str(tool_payload.get("name") or ""),
                        payload=tool_payload,
                    )
                )
                continue
            if kind in {"approval_required", "approval_resolved"}:
                event_payload = dict(payload or {}) if isinstance(payload, dict) else {"text": payload}
                await emitter.emit(RunEvent(kind=kind, payload=event_payload))
                continue
            if kind == "auth_required":
                event_payload = dict(payload or {}) if isinstance(payload, dict) else {}
                event_payload["run_id"] = auth_signal_run_id
                await emitter.emit(RunEvent(kind="auth_required", payload=event_payload))
                continue
            if kind == "clarify_required":
                event_payload = dict(payload or {}) if isinstance(payload, dict) else {"text": payload}
                await emitter.emit(
                    RunEvent(
                        kind=kind,
                        payload=_register_pending_clarify(request, event_payload),
                    )
                )
                continue
            if kind == "clarify_resolved":
                event_payload = dict(payload or {}) if isinstance(payload, dict) else {"text": payload}
                _clear_pending_clarify(event_payload)
                await emitter.emit(RunEvent(kind=kind, payload=_sanitize_clarify_payload(event_payload)))
                continue

        if pending_media_text:
            text = await asyncio.to_thread(
                router_mod._webui_profile_scoped_media_response,
                pending_media_text,
                profile_home,
            )
            if text:
                content_parts.append(text)
                await emitter.emit(
                    RunEvent(
                        kind="content",
                        text=text,
                        payload={"text": text},
                    )
                )

        if "".join(content_parts).strip():
            remote_media_text = await _webui_streamed_remote_media_additions(
                "".join(content_parts),
                router_mod=router_mod,
                profile_home=profile_home,
            )
            if remote_media_text:
                content_parts.append(remote_media_text)
                await emitter.emit(
                    RunEvent(
                        kind="content",
                        text=remote_media_text,
                        payload={"text": remote_media_text},
                    )
                )
            # Answer already delivered via streamed content frames above.
            # Return "" so RunBroker.run does NOT emit it a second time.
            return ""
        # Nothing streamed — fall back to one-shot; RunBroker emits it once.
        fallback_text = await real_run_agent(event, profile_home, messages=messages)
        return await asyncio.to_thread(
            router_mod._webui_profile_scoped_media_response,
            fallback_text,
            profile_home,
        )
    finally:
        _PROFILE_HOME_VAR.reset(token)


def _webui_streamable_media_text(text: str, *, router_mod, profile_home: Path) -> tuple[str, list[str]]:
    """Return pending trailing MEDIA directive text and safe-to-stream chunks."""
    raw = str(text or "")
    marker_index = raw.rfind("MEDIA:")
    if marker_index < 0:
        return "", [router_mod._webui_profile_scoped_media_response(raw, profile_home, materialize_remote_images=False)]

    newline_after_marker = raw.find("\n", marker_index)
    if newline_after_marker < 0:
        prefix = raw[:marker_index]
        pending = raw[marker_index:]
        items = (
            [router_mod._webui_profile_scoped_media_response(prefix, profile_home, materialize_remote_images=False)]
            if prefix
            else []
        )
        return pending, items

    return "", [router_mod._webui_profile_scoped_media_response(raw, profile_home, materialize_remote_images=False)]


async def _webui_streamed_remote_media_additions(text: str, *, router_mod, profile_home: Path) -> str:
    """Return only extra MEDIA aliases discovered after streamed chunks are joined."""
    raw = str(text or "")
    if "http" not in raw.lower() or "![" not in raw:
        return ""
    existing = {line.strip() for line in raw.splitlines() if line.strip().startswith("MEDIA:")}
    scoped = await asyncio.to_thread(router_mod._webui_profile_scoped_media_response, raw, profile_home)
    additions: list[str] = []
    for line in scoped.splitlines():
        cleaned = line.strip()
        if not cleaned.startswith("MEDIA:"):
            continue
        if cleaned in existing or cleaned in additions:
            continue
        additions.append(cleaned)
    return "\n".join(additions)


def _default_mark_seen(request: RunRequest) -> bool:
    from .. import router as router_mod

    return router_mod._mark_run_request_seen(request)


def _skillhub_discovery_source(payload: dict[str, Any]) -> str | None:
    for key in ("discovery_source", "catalog_source", "source_kind", "source_type"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return None


async def start_run_broker_server() -> None:
    """Start the localhost broker sidecar in the current event loop."""
    global _runner, _site
    if _truthy_env("HERMES_MULTITENANCY_RUN_BROKER_SERVER") and not _m._run_broker_key():
        logger.critical(
            "[multitenancy] WebUI run broker is enabled "
            "(HERMES_MULTITENANCY_RUN_BROKER_SERVER) but "
            "HERMES_MULTITENANCY_RUN_BROKER_KEY is empty; refusing to start an "
            "unauthenticated multi-user broker"
        )
        raise SystemExit(1)
    if _runner is not None:
        return

    from aiohttp import web

    app = _m.create_run_broker_app()
    _runner = web.AppRunner(app)
    await _runner.setup()
    _site = web.TCPSite(_runner, _m._run_broker_host(), _run_broker_port())
    await _site.start()
    logger.info(
        "[multitenancy] WebUI run broker listening on http://%s:%s/api/run-broker/runs",
        _m._run_broker_host(),
        _run_broker_port(),
    )


def ensure_run_broker_server_started() -> None:
    """Schedule the optional WebUI run broker sidecar when enabled."""
    global _server_task
    if not _truthy_env("HERMES_MULTITENANCY_RUN_BROKER_SERVER"):
        return
    if _server_task is not None and not _server_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("[multitenancy] no running loop; WebUI run broker sidecar not started")
        return
    _server_task = loop.create_task(start_run_broker_server())


async def stop_run_broker_server() -> None:
    """Test/maintenance helper to stop the sidecar."""
    global _runner, _site, _server_task
    if _server_task is not None and not _server_task.done():
        _server_task.cancel()
        try:
            await _server_task
        except asyncio.CancelledError:
            pass
    _server_task = None
    _site = None
    if _runner is not None:
        await _runner.cleanup()
    _runner = None
