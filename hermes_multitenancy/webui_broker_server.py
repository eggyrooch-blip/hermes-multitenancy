"""Router-owned HTTP/SSE seam for WebUI run submissions.

The endpoint lives in the multitenancy plugin so WebUI can submit normalized
``RunRequest(channel="webui")`` objects without calling a profile apiserver
execution endpoint directly.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from . import cron_api
from .credential_broker import (
    LeaseError,
    assert_lease_binding,
    lease_signing_secret,
    verify_lease,
)
from .run_broker import RunBroker, RunRejected
from .run_models import RunEvent, RunRequest
from .security_audit import append_security_event

logger = logging.getLogger(__name__)

DispatchAgent = Callable[[RunRequest], Awaitable[str] | str]
EmitRunEvent = Callable[[RunEvent], Awaitable[None] | None]
MarkSeen = Callable[[RunRequest], bool]
SandboxAvailable = Callable[[], bool]

_OWNER_OPEN_ID_HEADER = "X-Hermes-Owner-Open-Id"
_AGENT_ID_HEADER = "X-Hermes-Agent-Id"
_RUN_BROKER_DEFAULT_CLIENT_MAX_SIZE = 32 * 1024 * 1024
# Public SkillHub webhook bodies are small JSON envelopes; cap hard to avoid
# disk-fill / DoS on an internet-exposed route (the 32MB broker default is for
# WebUI run submissions, not webhooks).
_SKILLHUB_MAX_BODY_BYTES = 256 * 1024

_runner: Any = None
_site: Any = None
_server_task: Optional[asyncio.Task] = None

_RUN_BROKER_SHARED_ENV_KEYS = frozenset(
    {
        "HERMES_MULTITENANCY_CREDENTIAL_KEY",
        "HERMES_CREDENTIAL_KEY",
        "HERMES_LARK_CLI_APP_ID",
        "HERMES_LARK_CLI_BRAND",
        "HERMES_LARK_CLI_DEFAULT_AS",
        "HERMES_LARK_CLI_STRICT_MODE",
    }
)
_pending_clarifies: dict[str, dict[str, Any]] = {}
_SESSION_COMMAND_RE = re.compile(r"^/([A-Za-z][\w-]*)(?:\s+([\s\S]*))?$")
_SESSION_HISTORY_COMMANDS = frozenset({"new", "reset", "status"})
_GOAL_COMMANDS = frozenset({"goal", "subgoal"})
_credential_broker_tokens: dict[str, dict[str, str]] = {}
_credential_broker_tokens_lock = threading.Lock()


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


def _shared_home_from_env() -> Path:
    configured = os.environ.get("HERMES_SHARED_HOME") or os.environ.get("HERMES_HOME")
    return Path(configured or (Path.home() / ".hermes")).expanduser()


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
    return f"http://{_run_broker_host()}:{_run_broker_port()}"


def register_credential_broker_token(*, token: str, profile_name: str, open_id: str, run_id: str) -> None:
    key = str(token or "").strip()
    if not key:
        return
    with _credential_broker_tokens_lock:
        _credential_broker_tokens[key] = {
            "profile_name": str(profile_name or "").strip(),
            "open_id": str(open_id or "").strip(),
            "run_id": str(run_id or "").strip(),
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
    expected = _run_broker_key()
    if not expected:
        return True
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    return hmac.compare_digest(header[len(prefix):].strip(), expected)


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
    master = _run_broker_key()
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


def _ingest_authorized(request: Any) -> bool:
    """Auth for the synchronous ingest ingress — **fail-closed**.

    Mirrors ``_skillhub_authorized``: a public-facing route that must never
    serve unauthenticated. Accepts a dedicated ``HERMES_INGEST_KEY`` (the
    fixed Bearer token handed to the external caller, so neither the master
    broker key nor the SkillHub key is ever shared) and also accepts the
    master ``HERMES_MULTITENANCY_RUN_BROKER_KEY`` for internal callers. If
    NEITHER key is configured, refuse — never serve this route open.
    """
    dedicated = os.environ.get("HERMES_INGEST_KEY", "").strip()
    master = _run_broker_key()
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
        from . import router as router_mod

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


def _trusted_owner_from_request(request: Any) -> str:
    return str(request.headers.get(_OWNER_OPEN_ID_HEADER, "") or "").strip()


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
    from . import router as router_mod

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
    from . import router as router_mod

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
    from . import skill_registry

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
    from .runtime import _get_env_lock

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
    profile_home = _profile_home_for_name(profile_name)
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
    profile_home = _profile_home_for_name(profile_name)

    def run() -> dict[str, Any]:
        manager = _goal_manager(session_id)
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
    profile_home = _profile_home_for_name(profile_name)
    final_text = str(final_response or "").strip()

    def run() -> dict[str, Any]:
        manager = _goal_manager(session_id)
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

    from . import router as router_mod

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
        if row.owner_open_id != trusted_owner and not matches_owner_root:
            return None, f"agent_id '{agent_id}' does not belong to asserted owner"
        return row.profile_name, None

    if owner_root is None:
        return None, f"asserted owner '{trusted_owner}' has no sync-root profile"
    return owner_root.profile_name, None


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
    return SimpleNamespace(
        text=request.content,
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
            "metadata": _metadata_with_webui_runtime_context(request.metadata),
        },
    )


async def _default_dispatch_agent(
    request: RunRequest,
    *,
    emit_event: Optional[EmitRunEvent] = None,
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
    from . import router as router_mod
    from .agent_real import real_run_agent, stream_run_agent
    from .runtime import _PROFILE_HOME_VAR

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
                pending_media_text, text_items = _webui_streamable_media_text(
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
    from . import router as router_mod

    return router_mod._mark_run_request_seen(request)


def create_run_broker_app(
    *,
    dispatch_agent: Optional[DispatchAgent] = None,
    mark_seen: Optional[MarkSeen] = None,
    sandbox_available: Optional[SandboxAvailable] = None,
):
    try:
        from aiohttp import web
    except Exception as exc:  # pragma: no cover - production dependency guard
        raise RuntimeError("aiohttp is required for the WebUI run broker endpoint") from exc

    async def handle_run(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
            resolved_profile_name, resolution_error = _resolve_owner_scoped_profile(request, payload)
            if resolution_error is not None:
                return web.json_response({"error": resolution_error}, status=403)
            trusted_owner = str(request.headers.get(_OWNER_OPEN_ID_HEADER, "") or "").strip()
            run_request = RunRequest(
                channel=payload.get("channel"),
                profile_name=resolved_profile_name or payload.get("profile_name") or payload.get("profile"),
                user_key=trusted_owner or payload.get("user_key") or payload.get("user"),
                content=payload.get("content"),
                chat_id=payload.get("chat_id"),
                session_id=payload.get("session_id"),
                message_id=payload.get("message_id"),
                idempotency_key=payload.get("idempotency_key"),
                delivery_mode=payload.get("delivery_mode") or "socket",
                credential_subject=trusted_owner or payload.get("credential_subject"),
                requires_host_tools=bool(payload.get("requires_host_tools")),
                metadata=payload.get("metadata") or {},
                messages=payload.get("messages") or [],
            )
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        try:
            admission_broker = RunBroker(
                dispatch_agent=lambda _req: "",
                mark_seen=mark_seen if mark_seen is not None else _default_mark_seen,
                sandbox_available=sandbox_available or _default_sandbox_available,
            )
            admission = await admission_broker.admit(run_request)
        except RunRejected as exc:
            return web.json_response({"error": str(exc)}, status=403)

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
            },
        )
        await response.prepare(request)

        async def write_sse_event(event: RunEvent) -> None:
            await response.write(_event_to_sse(event).encode("utf-8"))

        emitter = _DisconnectTolerantEmitter(write_sse_event)

        async def emit_event(event: RunEvent) -> None:
            await emitter.emit(event)

        if admission.duplicate:
            await emit_event(RunEvent(kind="done"))
            if not emitter.disconnected:
                try:
                    await response.write_eof()
                except Exception as exc:
                    if not _is_client_transport_closed(exc):
                        raise
            return response

        broker_dispatch = dispatch_agent or (
            lambda req: _default_dispatch_agent(req, emit_event=emit_event)
        )

        broker = RunBroker(
            dispatch_agent=broker_dispatch,
            emit_event=emit_event,
            mark_seen=mark_seen if mark_seen is not None else _default_mark_seen,
            sandbox_available=sandbox_available or _default_sandbox_available,
        )

        try:
            await broker.run(run_request, admitted=True)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI run broker request failed")
            await emit_event(RunEvent(kind="error", text=str(exc), payload={"error": str(exc)}))
        finally:
            if not emitter.disconnected:
                try:
                    await response.write_eof()
                except Exception as exc:
                    if not _is_client_transport_closed(exc):
                        raise
        return response

    # Result cache for ingest idempotency (gap D): a duplicate submission
    # returns the SAME result the first one produced, instead of an empty
    # body. Bounded in-memory (TTL + cap), consistent with the broker's own
    # in-memory dedup — survives within one broker process, not across.
    _ingest_results: dict[str, dict[str, Any]] = {}
    _ingest_results_at: dict[str, float] = {}
    _INGEST_RESULT_TTL = 3600.0
    _INGEST_RESULT_CAP = 256

    def _ingest_store_result(key: str, value: dict[str, Any]) -> None:
        now = time.time()
        _ingest_results[key] = value
        _ingest_results_at[key] = now
        cutoff = now - _INGEST_RESULT_TTL
        for k in [k for k, t in _ingest_results_at.items() if t < cutoff]:
            _ingest_results.pop(k, None)
            _ingest_results_at.pop(k, None)
        if len(_ingest_results) > _INGEST_RESULT_CAP:
            overflow = sorted(_ingest_results_at.items(), key=lambda kv: kv[1])
            for k, _ in overflow[: len(_ingest_results) - _INGEST_RESULT_CAP]:
                _ingest_results.pop(k, None)
                _ingest_results_at.pop(k, None)

    def _ingest_get_result(key: str) -> Optional[dict[str, Any]]:
        # Review NB2: validate TTL on READ, not only on write — never hand back
        # a stale cached answer just because no later write has pruned it yet.
        ts = _ingest_results_at.get(key)
        if ts is None:
            return None
        if time.time() - ts > _INGEST_RESULT_TTL:
            _ingest_results.pop(key, None)
            _ingest_results_at.pop(key, None)
            return None
        return _ingest_results.get(key)

    async def handle_ingest(request):
        """Synchronous external-ingest entry.

        Runs the agent as a *server-bound* profile and returns the result as
        one JSON response (no SSE). Identity is pinned via the
        ``HERMES_INGEST_PROFILE`` env var — the caller CANNOT choose the
        profile (any ``profile`` field in the body is ignored), so a leaked
        ``HERMES_INGEST_KEY`` can only ever run as that one bound identity.

        Interaction model (machine API — there is no human to answer):
        - default (``interactive`` false) — NON-INTERACTIVE one-shot dispatch
          with NO clarify/approval bridge wired (the bridges are only attached
          on the streaming path), so the agent can never block waiting on a
          human; it proceeds with best judgement and returns.
        - ``interactive`` true — streaming dispatch WITH the bridges, but the
          handler short-circuits on the first clarify/approval event: it
          cancels the run and returns ``needs_clarification`` / ``needs_approval``
          immediately instead of letting the bridge block up to its timeout.

        A hard ``HERMES_INGEST_TIMEOUT`` (default 180s) bounds either path.

        Capability parity with the cron/WebUI run paths: host tools on by
        default (``requires_host_tools``); ``model`` / ``metadata`` passthrough;
        duplicate submissions return the original result.
        See SPEC run-broker-ingest.
        """
        if not _ingest_authorized(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        owner = _ingest_owner()
        default_profile = os.environ.get("HERMES_INGEST_PROFILE", "").strip()
        # Review B2: in v1 mode (no owner), an unconfigured profile must 503
        # immediately after auth — same precedence as before this feature,
        # independent of body validity.
        if not owner and not default_profile:
            return web.json_response(
                {"ok": False, "error": "ingest profile not configured"},
                status=503,
            )

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid json"}, status=400
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"ok": False, "error": "body must be a JSON object"}, status=400
            )

        content = str(payload.get("content") or "").strip()
        if not content:
            return web.json_response(
                {"ok": False, "error": "content is required"}, status=400
            )

        # Identity resolution (owner/default computed above, right after auth).
        # - OWNER mode (HERMES_INGEST_OWNER set): the caller MUST name one of the
        #   owner's own agents via ``agent``; the server validates ownership. No
        #   fallback to an unvalidated HERMES_INGEST_PROFILE (review B1) — every
        #   owner-mode run is ownership-checked, so a leaked key can only reach
        #   THIS owner's agents, never another owner's, never an arbitrary profile.
        # - v1 mode (no owner): fixed HERMES_INGEST_PROFILE; ``agent``/``profile``
        #   in the body are ignored. Behavior is byte-for-byte unchanged.
        if owner:
            agent_str = str(payload.get("agent") or "").strip()
            if not agent_str:
                return web.json_response(
                    {
                        "ok": False,
                        "error": "agent required",
                        "agents": [_ingest_agent_label(r) for r in _ingest_list_owner_agents(owner)],
                    },
                    status=400,
                )
            bound_profile, available = _ingest_resolve_agent(owner, agent_str)
            if not bound_profile:
                return web.json_response(
                    {
                        "ok": False,
                        "error": "agent not found or not owned by this key's owner",
                        "agents": available,
                    },
                    status=403,
                )
        else:
            bound_profile = default_profile  # guaranteed non-empty (checked after auth)

        # Optional skill: prepend a slash command so the broker's native
        # skill-slash rewriter (run_broker._rewrite_skill_slash_request)
        # expands it — the same path the WebUI uses. A ``profile`` field in
        # the body is deliberately NOT read: identity is server-bound.
        skill = str(payload.get("skill") or "").strip()
        if skill:
            content = f"/{skill.lstrip('/')} {content}"

        # Gap C — model / metadata passthrough (parity with cron). Identity is
        # still server-bound; only run knobs are caller-tunable. Review NB1:
        # caller metadata is applied FIRST, then ``source`` is forced, so a
        # caller can never spoof the ingest provenance marker.
        metadata: dict[str, Any] = {}
        extra_meta = payload.get("metadata")
        if isinstance(extra_meta, dict):
            metadata.update(extra_meta)
        model = str(payload.get("model") or "").strip()
        if model:
            metadata["model"] = model
        metadata["source"] = "ingest"

        # Gap A — host tools on by default (parity with cron/kanban), so
        # credential-backed skills work. Caller may opt out.
        requires_host_tools = bool(payload.get("requires_host_tools", True))
        interactive = bool(payload.get("interactive", False))

        try:
            run_request = RunRequest(
                channel="webui",
                profile_name=bound_profile,
                user_key=bound_profile,
                content=content,
                idempotency_key=str(payload.get("idempotency_key") or "").strip() or None,
                delivery_mode="sync",
                credential_subject=bound_profile,
                requires_host_tools=requires_host_tools,
                metadata=metadata,
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        # Review B3: namespace the idempotency cache by the resolved profile so
        # the same explicit idempotency_key used against two different owner
        # agents can never replay the wrong agent's cached result.
        cache_key = f"{bound_profile}\x00{run_request.effective_idempotency_key}"
        collected: list[str] = []
        error_text: dict[str, str] = {}
        clarify_holder: dict[str, Any] = {}
        approval_holder: dict[str, Any] = {}
        interrupt = asyncio.Event()

        async def emit_event(event: RunEvent) -> None:
            if event.kind == "content" and event.text:
                collected.append(event.text)
            elif event.kind == "error":
                error_text["error"] = event.text or "agent error"
            elif event.kind == "clarify_required" and "payload" not in clarify_holder:
                clarify_holder["payload"] = event.payload or {}
                interrupt.set()
            elif event.kind == "approval_required" and "payload" not in approval_holder:
                approval_holder["payload"] = event.payload or {}
                interrupt.set()

        # NON-INTERACTIVE (default): one-shot dispatch with NO event sink, so
        # agent_real wires neither the clarify nor the approval bridge (both
        # require an event_sink) — the run cannot block on a human.
        # INTERACTIVE: streaming dispatch with the sink so clarify/approval
        # events are emitted and we can short-circuit on them.
        sink = emit_event if interactive else None
        broker_dispatch = dispatch_agent or (
            lambda req: _default_dispatch_agent(req, emit_event=sink)
        )
        broker = RunBroker(
            dispatch_agent=broker_dispatch,
            emit_event=sink,
            mark_seen=mark_seen if mark_seen is not None else _default_mark_seen,
            sandbox_available=sandbox_available or _default_sandbox_available,
        )

        timeout_s = _ingest_timeout()

        # Review BLOCKING fix + NB4: every path is bounded by a hard timeout,
        # and interactive runs are abandoned the instant a human-input event
        # appears so the synchronous connection never waits on the bridge.
        result = None
        try:
            if interactive:
                run_task = asyncio.ensure_future(broker.run(run_request))
                interrupt_task = asyncio.ensure_future(interrupt.wait())
                done, _pending = await asyncio.wait(
                    {run_task, interrupt_task},
                    timeout=timeout_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:  # hard timeout
                    run_task.cancel()
                    interrupt_task.cancel()
                    logger.warning("[multitenancy] ingest run timed out after %ss", timeout_s)
                    return web.json_response(
                        {"ok": False, "status": "timeout", "profile": bound_profile},
                        status=504,
                    )
                if interrupt.is_set() and not run_task.done():
                    # Human-input needed: abandon the (bridge-blocked) run and
                    # surface the request immediately. The orphaned child run
                    # self-resolves at its own bridge timeout.
                    run_task.cancel()
                    try:
                        await run_task
                    except asyncio.CancelledError:
                        pass  # expected: we cancelled it
                    except Exception:
                        # Review NB-new3: don't silently swallow cleanup
                        # failures — log them, but still surface the
                        # interaction request to the caller.
                        logger.exception(
                            "[multitenancy] ingest interactive run cleanup error"
                        )
                    interrupt_task.cancel()
                else:
                    interrupt_task.cancel()
                    result = run_task.result()
            else:
                result = await asyncio.wait_for(broker.run(run_request), timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning("[multitenancy] ingest run timed out after %ss", timeout_s)
            return web.json_response(
                {"ok": False, "status": "timeout", "profile": bound_profile},
                status=504,
            )
        except RunRejected as exc:
            # Policy rejection text is safe-ish, but keep it short.
            return web.json_response({"ok": False, "error": str(exc)}, status=403)
        except Exception:
            # Review NB3: do not leak internal exception text to an
            # internet-facing caller — log server-side, return generic.
            logger.exception("[multitenancy] ingest run failed")
            return web.json_response(
                {"ok": False, "error": "internal error"}, status=500
            )

        if error_text.get("error"):
            logger.error("[multitenancy] ingest agent error: %s", error_text["error"])
            return web.json_response(
                {"ok": False, "error": "agent run failed", "profile": bound_profile},
                status=500,
            )

        text = "".join(collected).strip() or (result.content if result else "")

        # Gap D — duplicate submission returns the ORIGINAL structured response
        # (success OR needs_*), not empty. Review NB-new2: caching the full
        # response (not just text) means an interrupted interactive run is also
        # replayable on its idempotency key instead of degrading to
        # duplicate_pending.
        if result is not None and result.duplicate:
            cached = _ingest_get_result(cache_key)
            if cached is not None:
                replay = dict(cached)
                replay["duplicate"] = True
                return web.json_response(replay, status=200)
            return web.json_response(
                {"ok": False, "status": "duplicate_pending", "profile": bound_profile, "duplicate": True},
                status=200,
            )

        # Gap B — surface clarify/approval as a clear status instead of a
        # silent empty body. Payloads are sanitized (review NB3) and cached so
        # a retry with the same idempotency key replays the same request.
        if clarify_holder:
            # Review NB-new1: clear the pending-clarify registration we
            # abandoned, so it doesn't leak (no clarify_resolved will arrive).
            _clear_pending_clarify(clarify_holder["payload"])
            resp = {
                "ok": False,
                "status": "needs_clarification",
                "clarify": _ingest_public_interaction(clarify_holder["payload"]),
                "result": text,
                "profile": bound_profile,
                "duplicate": False,
            }
            _ingest_store_result(cache_key, resp)
            return web.json_response(resp, status=200)
        if approval_holder:
            resp = {
                "ok": False,
                "status": "needs_approval",
                "approval": _ingest_public_interaction(approval_holder["payload"]),
                "result": text,
                "profile": bound_profile,
                "duplicate": False,
            }
            _ingest_store_result(cache_key, resp)
            return web.json_response(resp, status=200)

        resp = {"ok": True, "result": text, "profile": bound_profile, "duplicate": False}
        _ingest_store_result(cache_key, resp)
        return web.json_response(resp, status=200)

    async def handle_ingest_agents(request):
        """List the ingest owner's own agents (OWNER mode discovery).

        Lets a caller see which ``agent`` values are valid before POSTing to
        /ingest. Same fail-closed Bearer auth as /ingest. Returns 503 when the
        endpoint is not in OWNER mode (no HERMES_INGEST_OWNER configured).
        """
        if not _ingest_authorized(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        owner = _ingest_owner()
        if not owner:
            return web.json_response(
                {"ok": False, "error": "owner mode not configured"}, status=503
            )
        # Review NB2: distinguish "routing unavailable" from "owner has no
        # agents" — don't report ok:true with an empty list during an outage,
        # whether the table is absent OR the query itself raises.
        from . import router as router_mod

        table = router_mod._get_routing_table()
        if table is None:
            return web.json_response(
                {"ok": False, "error": "routing table unavailable"}, status=503
            )
        try:
            rows = table.list_agents_for_owner(owner)
        except Exception:
            logger.exception("[multitenancy] ingest /agents enumeration failed")
            return web.json_response(
                {"ok": False, "error": "routing table unavailable"}, status=503
            )
        agents = [
            r
            for r in rows
            if getattr(r, "kind", None) == "agent"
            and (getattr(r, "owner_open_id", None) or "") == owner
        ]
        return web.json_response(
            {
                "ok": True,
                "owner": owner,
                "agents": [
                    {"name": _ingest_agent_label(r), "id": _ingest_agent_id(r)}
                    for r in agents
                ],
            },
            status=200,
        )

    async def handle_clarify_respond(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        clarify_id = str(request.match_info.get("clarify_id") or "").strip()
        if not clarify_id:
            return web.json_response({"error": "clarify_id required"}, status=400)

        try:
            payload = await request.json()
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        resolved_profile_name, resolution_error = _resolve_owner_scoped_profile(request, payload)
        if resolution_error is not None:
            return web.json_response({"error": resolution_error}, status=403)

        trusted_owner = _trusted_owner_from_request(request)
        if not trusted_owner:
            return web.json_response({
                "error": "owner identity required (X-Hermes-Owner-Open-Id)"
            }, status=403)
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        response_text = str(payload.get("response") or payload.get("answer") or "").strip()

        wrote = _write_pending_clarify_response(
            clarify_id=clarify_id,
            owner_open_id=trusted_owner,
            profile_name=resolved_profile_name or "",
            session_id=session_id,
            response_text=response_text,
        )
        if not wrote:
            return web.json_response({"error": "clarify request not found"}, status=404)
        return web.json_response({"ok": True, "clarify_id": clarify_id})

    async def handle_session_command(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        resolved_profile_name, resolution_error = _resolve_owner_scoped_profile(request, payload)
        if resolution_error is not None:
            return web.json_response({"error": resolution_error}, status=403)
        if not resolved_profile_name:
            return web.json_response({
                "error": "owner identity required (X-Hermes-Owner-Open-Id)"
            }, status=403)

        trusted_owner = _trusted_owner_from_request(request)
        if not trusted_owner:
            return web.json_response({
                "error": "owner identity required (X-Hermes-Owner-Open-Id)"
            }, status=403)

        try:
            _assert_requested_profile_matches(
                resolved_profile_name=resolved_profile_name,
                payload=payload,
            )
            session_id = str(payload.get("session_id") or "").strip()
            if not session_id:
                return web.json_response({"error": "session_id required"}, status=400)
            raw_command = str(payload.get("command") or payload.get("text") or "").strip()
            if not raw_command:
                return web.json_response({"error": "command required"}, status=400)
            result = await _dispatch_session_command(
                profile_name=resolved_profile_name,
                user_key=trusted_owner,
                session_id=session_id,
                command=raw_command,
            )
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        return web.json_response({
            "ok": True,
            "profile_name": resolved_profile_name,
            "session_id": session_id,
            **result,
        })

    async def handle_goal_evaluate(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        resolved_profile_name, resolution_error = _resolve_owner_scoped_profile(request, payload)
        if resolution_error is not None:
            return web.json_response({"error": resolution_error}, status=403)
        if not resolved_profile_name:
            return web.json_response({
                "error": "owner identity required (X-Hermes-Owner-Open-Id)"
            }, status=403)

        try:
            _assert_requested_profile_matches(
                resolved_profile_name=resolved_profile_name,
                payload=payload,
            )
            session_id = str(payload.get("session_id") or "").strip()
            if not session_id:
                return web.json_response({"error": "session_id required"}, status=400)
            result = await _evaluate_goal_after_turn(
                profile_name=resolved_profile_name,
                session_id=session_id,
                final_response=payload.get("final_response") or payload.get("response") or "",
            )
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        return web.json_response({
            "ok": True,
            "profile_name": resolved_profile_name,
            "session_id": session_id,
            **result,
        })

    async def handle_provision_profile(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        trusted_owner = str(request.headers.get(_OWNER_OPEN_ID_HEADER, "") or "").strip()
        if not trusted_owner:
            return web.json_response({
                "error": "owner identity required (X-Hermes-Owner-Open-Id)"
            }, status=403)

        try:
            payload = await request.json()
            profile_name = cron_api.validate_profile_name(
                str(payload.get("profile_name") or payload.get("profile") or "")
            )
            display_label = str(payload.get("display_label") or profile_name).strip() or profile_name
            upstream_profile = str(payload.get("upstream_profile") or "").strip() or None
            if upstream_profile is not None:
                upstream_profile = cron_api.validate_profile_name(upstream_profile)
            requested_agent_id = str(payload.get("agent_id") or "").strip()
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        from . import router as router_mod

        table = router_mod._get_routing_table()
        if table is None:
            return web.json_response({
                "error": "trusted owner header requires routing table verification"
            }, status=403)
        owner_root = table.resolve_owner_root(trusted_owner)
        if owner_root is None:
            return web.json_response({
                "error": f"asserted owner '{trusted_owner}' has no sync-root profile"
            }, status=403)
        allowed_upstreams = _owner_allowed_upstream_profiles(table, trusted_owner, owner_root)
        if upstream_profile is not None and upstream_profile not in allowed_upstreams:
            return web.json_response({
                "error": f"upstream_profile '{upstream_profile}' is not accessible for asserted owner"
            }, status=403)
        effective_upstream_profile = upstream_profile or owner_root.profile_name

        stable_agent_id = _webui_agent_id(trusted_owner, profile_name)
        if requested_agent_id and requested_agent_id != stable_agent_id:
            existing = table.lookup_agent(requested_agent_id)
            if existing is not None and existing.owner_open_id != trusted_owner:
                return web.json_response({
                    "error": f"agent_id '{requested_agent_id}' does not belong to asserted owner"
                }, status=403)
            return web.json_response({"error": "agent_id does not match owner/profile"}, status=400)

        try:
            routed_profile_name = _webui_owned_profile_name(trusted_owner, profile_name)
            agent_id = table.upsert_owned_agent(
                agent_id=stable_agent_id,
                profile_name=routed_profile_name,
                owner_open_id=trusted_owner,
                display_label=display_label,
                upstream_profile=effective_upstream_profile,
            )
            shared_home = _shared_home_from_env()
            router_mod._ensure_webui_agent_profile(
                profile_name=routed_profile_name,
                profile_home=shared_home / "profiles" / routed_profile_name,
                owner_open_id=trusted_owner,
                display_label=display_label,
                agent_id=agent_id,
                upstream_profile=effective_upstream_profile,
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=403)

        return web.json_response({
            "ok": True,
            "agent_id": agent_id,
            "profile_name": profile_name,
            "routed_profile_name": routed_profile_name,
            "owner_open_id": trusted_owner,
            "display_label": display_label,
            "upstream_profile": effective_upstream_profile,
        })

    async def handle_slash_commands(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        payload = {
            "profile_name": request.query.get("profile_name") or request.query.get("profile"),
            "agent_id": request.query.get("agent_id"),
        }
        resolved_profile_name, resolution_error = _resolve_owner_scoped_profile(request, payload)
        if resolution_error is not None:
            return web.json_response({"error": resolution_error}, status=403)
        if not resolved_profile_name:
            return web.json_response({
                "error": "owner identity required (X-Hermes-Owner-Open-Id)"
            }, status=403)

        profile_name = resolved_profile_name
        try:
            profile_name = cron_api.validate_profile_name(str(profile_name or ""))
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)

        try:
            from .skill_registry import list_profile_skill_slash_commands

            shared_home = _shared_home_from_env()
            commands = _session_history_slash_commands() + list_profile_skill_slash_commands(
                profile_home=shared_home / "profiles" / profile_name,
            )
            return web.json_response({
                "ok": True,
                "profile_name": profile_name,
                "commands": commands,
            })
        except Exception as exc:
            logger.exception("[multitenancy] WebUI slash registry failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_list_jobs(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _tenant_from_request(request)
            include_disabled = request.query.get("include_disabled", "").lower() in {"true", "1"}
            jobs = await asyncio.to_thread(
                cron_api.list_jobs,
                profile_name,
                include_disabled=include_disabled,
            )
            return web.json_response({"jobs": jobs})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron list failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_create_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
            profile_name, user_key = _tenant_from_request(request, payload)
            job = await asyncio.to_thread(cron_api.create_job, profile_name, user_key, payload)
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron create failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_get_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _tenant_from_request(request)
            job = await asyncio.to_thread(cron_api.get_job, profile_name, request.match_info["job_id"])
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron get failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_plan_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _tenant_from_request(request)
            shadow = request.query.get("shadow", "1").lower() in {"1", "true", "yes", "on"}
            due_raw = request.query.get("due")
            due = None if due_raw is None else due_raw.lower() in {"1", "true", "yes", "on"}
            plan = await asyncio.to_thread(
                cron_api.plan_job,
                profile_name,
                request.match_info["job_id"],
                shadow=shadow,
                due=due,
            )
            return web.json_response({"plan": plan})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron plan failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_update_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
            profile_name, _user_key = _tenant_from_request(request, payload)
            job = await asyncio.to_thread(
                cron_api.update_job,
                profile_name,
                request.match_info["job_id"],
                payload,
            )
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron update failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_delete_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _tenant_from_request(request)
            await asyncio.to_thread(cron_api.delete_job, profile_name, request.match_info["job_id"])
            return web.json_response({"ok": True})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron delete failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_pause_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _tenant_from_request(request)
            job = await asyncio.to_thread(cron_api.pause_job, profile_name, request.match_info["job_id"])
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron pause failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_resume_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _tenant_from_request(request)
            job = await asyncio.to_thread(cron_api.resume_job, profile_name, request.match_info["job_id"])
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron resume failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_run_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _tenant_from_request(request)
            job = await asyncio.to_thread(cron_api.trigger_job, profile_name, request.match_info["job_id"])
            return web.json_response({"job": job, "queued": True})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron run trigger failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_feishu_uat_status(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import feishu_uat_auth

        try:
            profile_name, user_key = _tenant_from_request(request, _tenant_payload_from_query(request))
            status = feishu_uat_auth.credential_status(
                profile_name=profile_name,
                open_id=user_key,
                required_scopes=request.query.get("required_scopes"),
            )
            return web.json_response(status)
        except feishu_uat_auth.FeishuUatAuthError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Feishu UAT status failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_credential_hub(request):
        """Aggregated, redacted credential status for one tenant.

        Intended convergence point (归口) for credential status in
        multitenancy: the Feishu ``/auth`` card already consumes the same
        ``credential_hub`` aggregation in-process. Full convergence still
        requires hermes-web-ui's ``skill-credentials.ts`` to read THIS endpoint
        instead of computing status independently, and the hub to reach parity
        with the WebUI's credential set (currently 3 vs 5). Until then this is
        an additional reader, not yet the sole source of truth — see
        ``.ftask/auth-cred-hub/SPEC.md``.
        """
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import credential_hub

        try:
            profile_name, user_key = _tenant_from_request(request, _tenant_payload_from_query(request))
            # Offload to a thread: the aggregation may shell out to meegle/kep-auth
            # (subprocess), which must not block the aiohttp event loop.
            rows = await asyncio.to_thread(
                credential_hub.collect_credential_statuses,
                profile_name=profile_name,
                open_id=user_key,
            )
            return web.json_response(
                {
                    "profile_name": profile_name,
                    "subject_id": user_key,
                    "credentials": [row.to_dict() for row in rows],
                }
            )
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI credential hub failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_credential_lease(request):
        header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return web.json_response({"error": "unauthorized"}, status=401)
        token_record = _lookup_credential_broker_token(header[len(prefix):].strip())
        if token_record is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        lease = str(payload.get("lease") or "").strip()
        kind = str(payload.get("kind") or "").strip()
        # The audit must never log a raw, attacker-controlled `kind` (it could
        # carry an injected secret/log-injection payload). Clamp to the known
        # enum; anything else is recorded as <invalid>. The real branch logic
        # below still uses `kind` and simply no-ops on an unknown value.
        audit_kind = kind if kind in {"feishu_uat", "provider_env"} else "<invalid>"
        profile_name = str(payload.get("profile_name") or "").strip()
        open_id = str(payload.get("open_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        try:
            claims = verify_lease(lease, secret=lease_signing_secret())
            assert_lease_binding(
                claims,
                profile_name=profile_name,
                open_id=open_id,
                run_id=run_id,
            )
            assert_lease_binding(
                claims,
                profile_name=token_record["profile_name"],
                open_id=token_record["open_id"],
                run_id=token_record["run_id"],
            )
            append_security_event(
                event_type="credential.lease.granted",
                open_id=claims.open_id,
                profile=claims.profile_name,
                lease_kind=audit_kind,
                decision="granted",
            )
        except LeaseError:
            # Attribute the denial to the AUTHENTICATED broker token, never the
            # untrusted request JSON — otherwise an attacker controls who the
            # audit blames. token_record is keyed by the bearer token already
            # verified above.
            append_security_event(
                event_type="credential.lease.denied",
                open_id=token_record["open_id"],
                profile=token_record["profile_name"],
                lease_kind=audit_kind,
                decision="denied",
                reason="lease_verification_failed",
            )
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            if kind == "feishu_uat":
                # Use _load_best_uat_payload (vault + materialized plaintext,
                # freshest wins) — the SAME source the normal non-strict path
                # uses — so strict credential coverage via the broker == actual
                # coverage. The vault-only _load_vault_uat_payload dropped any
                # profile that is materialized-but-not-yet-vaulted. This runs in
                # the PARENT broker server, so it does NOT let the child read
                # creds locally — the child still fetches via this broker route.
                from .feishu_uat_auth import _load_best_uat_payload
                from .provider_adapter import _resolve_shared_home

                profile_home = _profile_home_for_name(claims.profile_name)
                shared_home = _resolve_shared_home(profile_home)
                payload_value = _load_best_uat_payload(shared_home, claims.profile_name, claims.open_id)
                return web.json_response({"payload": payload_value})
            if kind == "provider_env":
                from .provider_adapter import provider_env_for_aiagent

                profile_home = _profile_home_for_name(claims.profile_name)
                payload_value = provider_env_for_aiagent(profile_home)
                return web.json_response({"payload": payload_value})
            return web.json_response({"error": "bad_request"}, status=400)
        except Exception:
            return web.json_response({"error": "internal"}, status=500)

    async def handle_kep_cli_callback(request):
        """Public OAuth callback for kep-cli in-Feishu auth: forwards the OAuth
        result to the kep-auth localhost server (same host). Intentionally NOT
        bearer-auth'd — it is hit by the user's browser after authorizing, keyed
        by an unguessable one-time session id. Returns a small HTML page."""
        from . import credential_hub_auth as cha

        sid = request.match_info.get("session_id", "")
        try:
            await asyncio.to_thread(cha.complete_kep_callback, sid, request.query_string)
        except cha.HubAuthError as exc:
            return web.Response(
                text=f"<html><body style='font-family:sans-serif'>认证未完成：{exc.message}<br>可重新发送 /auth 重试。</body></html>",
                content_type="text/html", status=exc.status,
            )
        except Exception as exc:
            logger.exception("[multitenancy] kep-cli callback failed")
            return web.Response(text=f"认证出错：{exc}", content_type="text/html", status=500)
        return web.Response(
            text="<html><body style='font-family:sans-serif'>✅ kep-cli 认证成功，可关闭本页面，返回飞书查看结果。</body></html>",
            content_type="text/html",
        )

    async def handle_feishu_auth_start(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import feishu_uat_auth

        try:
            payload = await request.json()
            profile_name, user_key = _tenant_from_request(request, payload)
            body = feishu_uat_auth.start_session(
                profile_name=profile_name,
                open_id=user_key,
                scope=payload.get("scope"),
            )
            return web.json_response(body)
        except feishu_uat_auth.FeishuUatAuthError as exc:
            return web.json_response({"error": exc.message, "status": "error"}, status=exc.status)
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message, "status": "error"}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Feishu auth start failed")
            return web.json_response({"error": str(exc), "status": "error"}, status=500)

    async def handle_feishu_auth_poll(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import feishu_uat_auth

        try:
            profile_name, user_key = _tenant_from_request(request, _tenant_payload_from_query(request))
            body = feishu_uat_auth.poll_session(
                session_id=request.match_info["session_id"],
                profile_name=profile_name,
                open_id=user_key,
            )
            status = 200 if body.get("status") != "error" else 400
            return web.json_response(body, status=status)
        except feishu_uat_auth.FeishuUatAuthError as exc:
            return web.json_response({"error": exc.message, "status": "error"}, status=exc.status)
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message, "status": "error"}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Feishu auth poll failed")
            return web.json_response({"error": str(exc), "status": "error"}, status=500)

    async def handle_feishu_auth_cancel(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import feishu_uat_auth

        try:
            profile_name, user_key = _tenant_from_request(request, _tenant_payload_from_query(request))
            body = feishu_uat_auth.cancel_session(
                session_id=request.match_info["session_id"],
                profile_name=profile_name,
                open_id=user_key,
            )
            return web.json_response(body)
        except feishu_uat_auth.FeishuUatAuthError as exc:
            return web.json_response({"error": exc.message, "status": "error"}, status=exc.status)
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message, "status": "error"}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Feishu auth cancel failed")
            return web.json_response({"error": str(exc), "status": "error"}, status=500)

    async def handle_kanban_assignees(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import kanban_sidecar

        try:
            assignees = kanban_sidecar.list_owner_assignees(
                owner_open_id=_trusted_owner_from_request(request),
                board=_kanban_board_from_request(request),
            )
            return web.json_response({"assignees": assignees})
        except kanban_sidecar.KanbanApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Kanban assignees failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_kanban_capabilities(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import kanban_sidecar

        try:
            # Still require owner scope so this endpoint cannot be used as a
            # generic unauthenticated capability probe when broker auth is off
            # in local development.
            kanban_sidecar.list_owner_assignees(
                owner_open_id=_trusted_owner_from_request(request),
                board=_kanban_board_from_request(request),
            )
            return web.json_response({"capabilities": kanban_sidecar.owner_kanban_capabilities()})
        except kanban_sidecar.KanbanApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Kanban capabilities failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_kanban_stats(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import kanban_sidecar

        try:
            stats = kanban_sidecar.owner_kanban_stats(
                owner_open_id=_trusted_owner_from_request(request),
                board=_kanban_board_from_request(request),
            )
            return web.json_response({"stats": stats})
        except kanban_sidecar.KanbanApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Kanban stats failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_kanban_boards(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import kanban_sidecar

        try:
            include_archived = request.query.get("includeArchived", "").lower() in {"1", "true", "yes", "on"}
            boards = kanban_sidecar.list_owner_boards(
                owner_open_id=_trusted_owner_from_request(request),
                include_archived=include_archived,
            )
            return web.json_response({"boards": boards})
        except kanban_sidecar.KanbanApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Kanban boards failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_kanban_tasks(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import kanban_sidecar

        try:
            include_archived = request.query.get("includeArchived", "").lower() in {"1", "true", "yes", "on"}
            tasks = kanban_sidecar.list_owner_tasks(
                owner_open_id=_trusted_owner_from_request(request),
                board=_kanban_board_from_request(request),
                status=request.query.get("status"),
                assignee=request.query.get("assignee"),
                tenant=request.query.get("tenant"),
                include_archived=include_archived,
            )
            return web.json_response({"tasks": tasks})
        except kanban_sidecar.KanbanApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Kanban list failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_kanban_create_task(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import kanban_sidecar

        try:
            payload = await request.json()
            task = kanban_sidecar.create_owner_task(
                owner_open_id=_trusted_owner_from_request(request),
                payload=payload,
                board=_kanban_board_from_request(request),
            )
            return web.json_response({"task": task})
        except kanban_sidecar.KanbanApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Kanban create failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_kanban_dispatch(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import kanban_sidecar

        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                return web.json_response({"error": "request body must be an object"}, status=400)
            result = kanban_sidecar.dispatch_owner_kanban(
                owner_open_id=_trusted_owner_from_request(request),
                board=_kanban_board_from_request(request),
                dry_run=_optional_bool(payload.get("dryRun"), "dryRun", default=True),
                max_spawn=_optional_positive_int(payload.get("max"), "max"),
                max_in_progress=_optional_positive_int(payload.get("maxInProgress"), "maxInProgress"),
            )
            return web.json_response({"result": result})
        except kanban_sidecar.KanbanApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Kanban dispatch failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_skillhub_install(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import skill_registry

        try:
            payload = await request.json()
            resolved_profile_name, resolution_error = _resolve_owner_scoped_profile(request, payload)
            if resolution_error is not None:
                return web.json_response({"error": resolution_error}, status=403)
            if not resolved_profile_name:
                return web.json_response({
                    "error": "owner identity required (X-Hermes-Owner-Open-Id)"
                }, status=403)
            profile_name = resolved_profile_name
            shared_home = _shared_home_from_env()
            discovery_policy = None
            discovery_source = _skillhub_discovery_source(payload)
            if discovery_source:
                from .discovery_policy import plan_profile_discovery

                owner_open_id = str(request.headers.get(_OWNER_OPEN_ID_HEADER, "") or "").strip()
                plan = plan_profile_discovery(
                    shared_home=shared_home,
                    profile_name=profile_name,
                    user_key=owner_open_id or None,
                    requested_sources=[discovery_source],
                    requested_toolsets=[],
                    audit=True,
                )
                decision = plan["sources"].get(discovery_source) or {}
                discovery_policy = {
                    "source": discovery_source,
                    "allowed": bool(decision.get("allowed")),
                    "reason": decision.get("reason"),
                    "requires_audit": bool(decision.get("requires_audit")),
                    "audit_written": bool((plan.get("audit") or {}).get("written")),
                }
                if not discovery_policy["allowed"]:
                    return web.json_response({
                        "error": "discovery source blocked by policy",
                        "profile_name": profile_name,
                        "discovery_policy": discovery_policy,
                    }, status=403)
            install = skill_registry.install_shared_skill_for_profile(
                shared_home=shared_home,
                profile_home=shared_home / "profiles" / profile_name,
                skill_path=str(payload.get("skill_path") or payload.get("path") or ""),
                source=payload.get("source"),
                version=payload.get("version"),
            )
            installed = skill_registry.list_installed_skills(
                profile_home=shared_home / "profiles" / profile_name
            )
            return web.json_response({
                "profile_name": profile_name,
                "install": install,
                "installed_skills": installed,
                **({"discovery_policy": discovery_policy} if discovery_policy else {}),
            })
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except FileNotFoundError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI SkillHub install failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_skill_audit(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import skill_registry

        try:
            shared_home = _shared_home_from_env()
            report = skill_registry.audit_installed_skills(shared_home=shared_home)
            return web.json_response(report)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI skill audit failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_skillhub_event(request):
        """Ingress for AiDock SkillHub webhooks (skill.install_approved, etc.).

        Validates, deduplicates and persists the event, then acks. Actual skill
        materialization (download/router-install/symlink/callback) is the next
        phase — accepted events land as ``queued`` for a downstream worker.
        """
        if not _skillhub_authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import skillhub_events

        # Read with a hard cap (streams, so chunked bodies can't force a 32MB buffer).
        raw = await _read_capped_body(request, _SKILLHUB_MAX_BODY_BYTES)
        if raw is None:
            return web.json_response(
                {
                    "ok": False,
                    "error_code": "PAYLOAD_TOO_LARGE",
                    "message": f"body exceeds {_SKILLHUB_MAX_BODY_BYTES} bytes",
                    "retryable": False,
                },
                status=413,
            )
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return web.json_response(
                {
                    "ok": False,
                    "error_code": "INVALID_JSON",
                    "message": "request body is not valid JSON",
                    "retryable": False,
                },
                status=400,
            )

        raw_body = raw.decode("utf-8")
        try:
            event = skillhub_events.normalize_event(payload, raw_body=raw_body)
        except skillhub_events.SkillhubEventError as exc:
            return web.json_response(
                {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": str(exc),
                    "retryable": False,
                },
                status=400,
            )

        # Optional HMAC signature gate. Mirrors _authorized: when no secret is
        # configured (dev), the gate is open; when a secret IS set, a missing or
        # bad signature is rejected.
        secret = os.environ.get("HERMES_SKILLHUB_WEBHOOK_SECRET", "").strip()
        signature_verified = False
        if secret:
            timestamp = str(request.headers.get("X-AiDock-Timestamp", "") or "").strip()
            provided = str(request.headers.get("X-AiDock-Signature", "") or "").strip()
            if not skillhub_events.verify_signature(
                secret, timestamp, event["event_id"], raw_body, provided
            ):
                return web.json_response(
                    {
                        "ok": False,
                        "event_id": event["event_id"],
                        "error_code": "INVALID_SIGNATURE",
                        "message": "signature verification failed",
                        "retryable": False,
                    },
                    status=401,
                )
            signature_verified = True

        try:
            store = skillhub_events.get_event_store()
            status, duplicate = store.record(
                event, raw_payload=raw_body, signature_verified=signature_verified
            )
        except Exception:
            logger.exception("[multitenancy] SkillHub event persist failed")
            return web.json_response(
                {
                    "ok": False,
                    "event_id": event["event_id"],
                    "error_code": "INTERNAL_ERROR",
                    "message": "failed to persist event",
                    "retryable": True,
                },
                status=500,
            )

        logger.info(
            "[multitenancy] SkillHub event received event_id=%s type=%s skill=%s release=%s dup=%s",
            event["event_id"],
            event["event_type"],
            event["skill_code"],
            event.get("release_id"),
            duplicate,
        )
        return web.json_response(
            {
                "ok": True,
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "skill_code": event["skill_code"],
                "accepted": True,
                "duplicate": duplicate,
                "status": status,
            }
        )

    async def handle_health(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({
            "ok": True,
            "service": "hermes-multitenancy-run-broker",
        })

    _helpdesk_cache: dict = {}

    async def handle_feishu_helpdesk_events(request):
        # Internal endpoint: the Feishu ws-adapter forwards helpdesk ticket events
        # here. Business logic + the hard test-helpdesk safety filter live in
        # feishu_helpdesk_event.handle_helpdesk_event. Shadow by default (post gate).
        if not _authorized(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"bad json: {exc}"}, status=400)

        from .feishu_helpdesk_event import handle_helpdesk_event

        index = _helpdesk_cache.get("index")
        if index is None:
            from .helpdesk_rag import HelpdeskRagIndex

            db = os.path.expanduser(
                os.environ.get("HERMES_HELPDESK_INDEX_DB", "~/.hermes/profiles/helpdesk/ticket_index.db")
            )
            index = HelpdeskRagIndex(db)
            _helpdesk_cache["index"] = index
            doc_count = index.count()
            if doc_count == 0:
                logger.warning(
                    "[multitenancy] helpdesk RAG index at %s is EMPTY (0 docs) — answers will be "
                    "ungrounded; build the index (ingest faqs/tickets) before relying on it",
                    db,
                )
            else:
                logger.info("[multitenancy] helpdesk RAG index loaded: %d docs (%s)", doc_count, db)

        # The test-helpdesk client is needed even in shadow mode: the event carries no
        # helpdesk_id, so we confirm ticket membership by querying it with this helpdesk's
        # token (real IT-helpdesk tickets fail and are dropped).
        client = _helpdesk_cache.get("client")
        if client is None:
            from .feishu_helpdesk_client import HelpdeskClient

            client = HelpdeskClient(
                app_id=os.environ.get("HERMES_HELPDESK_APP_ID", ""),
                app_secret=os.environ.get("HERMES_HELPDESK_APP_SECRET", ""),
                helpdesk_id=os.environ.get("HERMES_HELPDESK_ID", ""),
                helpdesk_token=os.environ.get("HERMES_HELPDESK_TOKEN", ""),
            )
            _helpdesk_cache["client"] = client

        def _membership_check(ticket_id: str) -> bool:
            try:
                client.get_ticket(ticket_id)
                return True
            except Exception:
                return False

        post_enabled = os.environ.get("HERMES_HELPDESK_POST", "").strip().lower() in ("1", "true", "yes")
        reply_fn = client.send_ticket_message if post_enabled else None

        # SAFETY WELD: never operate against a denied (production) helpdesk, even if the
        # env is misconfigured to point at it.
        from .feishu_helpdesk_event import ALLOWED_HELPDESK_IDS, DENY_HELPDESK_IDS

        configured_id = os.environ.get("HERMES_HELPDESK_ID", "").strip()
        if configured_id in DENY_HELPDESK_IDS or configured_id not in ALLOWED_HELPDESK_IDS:
            logger.error(
                "[multitenancy] REFUSING helpdesk events: HERMES_HELPDESK_ID=%r is denied or not in the "
                "allowlist %s — never auto-answer non-allowlisted / real-employee helpdesks",
                configured_id, sorted(ALLOWED_HELPDESK_IDS),
            )
            return web.json_response({"ok": False, "error": "helpdesk not allowlisted"}, status=403)

        def _process() -> None:
            # heavy work OFF the event loop: membership API + RAG + inference (+ reply)
            try:
                result = handle_helpdesk_event(
                    payload, index=index, membership_check=_membership_check, reply_fn=reply_fn, post=post_enabled
                )
            except Exception:
                logger.exception("[multitenancy] helpdesk event handling failed")
                return
            logger.info(
                "[multitenancy] helpdesk event action=%s ticket=%s posted=%s q=%r",
                result.get("action"), result.get("ticket_id"), result.get("posted"),
                (result.get("question") or "")[:80],
            )
            if result.get("answer"):
                logger.info("[multitenancy] helpdesk draft answer: %s", str(result.get("answer"))[:600])

        # fast-ack: never block the broker event loop on RAG/inference/Feishu I/O
        asyncio.get_event_loop().run_in_executor(None, _process)
        return web.json_response({"ok": True, "accepted": True})

    app = web.Application(client_max_size=_run_broker_client_max_size())
    app.router.add_get("/api/run-broker/health", handle_health)
    app.router.add_post("/api/run-broker/feishu/helpdesk/events", handle_feishu_helpdesk_events)
    app.router.add_post("/api/run-broker/runs", handle_run)
    app.router.add_post("/api/run-broker/ingest", handle_ingest)
    app.router.add_get("/api/run-broker/ingest/agents", handle_ingest_agents)
    app.router.add_post("/api/run-broker/clarify/{clarify_id}/respond", handle_clarify_respond)
    app.router.add_post("/api/run-broker/session-commands", handle_session_command)
    app.router.add_post("/api/run-broker/goals/evaluate", handle_goal_evaluate)
    app.router.add_post("/api/run-broker/profiles", handle_provision_profile)
    app.router.add_get("/api/run-broker/slash/commands", handle_slash_commands)
    app.router.add_post("/api/run-broker/credentials/lease", handle_credential_lease)
    app.router.add_get("/api/run-broker/credentials/feishu/uat/status", handle_feishu_uat_status)
    app.router.add_get("/api/run-broker/credentials/hub", handle_credential_hub)
    app.router.add_get("/api/run-broker/credentials/kep-cli/callback/{session_id}", handle_kep_cli_callback)
    app.router.add_post("/api/run-broker/feishu-auth/sessions", handle_feishu_auth_start)
    app.router.add_get("/api/run-broker/feishu-auth/sessions/{session_id}", handle_feishu_auth_poll)
    app.router.add_delete("/api/run-broker/feishu-auth/sessions/{session_id}", handle_feishu_auth_cancel)
    app.router.add_get("/api/run-broker/kanban/boards", handle_kanban_boards)
    app.router.add_get("/api/run-broker/kanban/capabilities", handle_kanban_capabilities)
    app.router.add_get("/api/run-broker/kanban/assignees", handle_kanban_assignees)
    app.router.add_get("/api/run-broker/kanban/stats", handle_kanban_stats)
    app.router.add_get("/api/run-broker/kanban/tasks", handle_kanban_tasks)
    app.router.add_post("/api/run-broker/kanban/tasks", handle_kanban_create_task)
    app.router.add_post("/api/run-broker/kanban/dispatch", handle_kanban_dispatch)
    app.router.add_post("/api/run-broker/skills/install", handle_skillhub_install)
    app.router.add_get("/api/run-broker/skills/audit", handle_skill_audit)
    app.router.add_post("/api/run-broker/skillhub/events", handle_skillhub_event)
    app.router.add_get("/api/run-broker/jobs", handle_list_jobs)
    app.router.add_post("/api/run-broker/jobs", handle_create_job)
    app.router.add_get("/api/run-broker/jobs/{job_id}/plan", handle_plan_job)
    app.router.add_get("/api/run-broker/jobs/{job_id}", handle_get_job)
    app.router.add_patch("/api/run-broker/jobs/{job_id}", handle_update_job)
    app.router.add_delete("/api/run-broker/jobs/{job_id}", handle_delete_job)
    app.router.add_post("/api/run-broker/jobs/{job_id}/pause", handle_pause_job)
    app.router.add_post("/api/run-broker/jobs/{job_id}/resume", handle_resume_job)
    app.router.add_post("/api/run-broker/jobs/{job_id}/run", handle_run_job)
    return app


def _skillhub_discovery_source(payload: dict[str, Any]) -> str | None:
    for key in ("discovery_source", "catalog_source", "source_kind", "source_type"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return None


async def start_run_broker_server() -> None:
    """Start the localhost broker sidecar in the current event loop."""
    global _runner, _site
    if _truthy_env("HERMES_MULTITENANCY_RUN_BROKER_SERVER") and not _run_broker_key():
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

    app = create_run_broker_app()
    _runner = web.AppRunner(app)
    await _runner.setup()
    _site = web.TCPSite(_runner, _run_broker_host(), _run_broker_port())
    await _site.start()
    logger.info(
        "[multitenancy] WebUI run broker listening on http://%s:%s/api/run-broker/runs",
        _run_broker_host(),
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
