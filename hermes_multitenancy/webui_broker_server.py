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
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from . import cron_api
from .run_broker import RunBroker, RunRejected
from .run_models import RunEvent, RunRequest

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


def _dispatch_session_history_command(*, profile_name: str, user_key: str, command: Any) -> dict[str, Any]:
    from . import router as router_mod

    command_name, _args = _parse_session_history_command(command)
    if command_name not in _SESSION_HISTORY_COMMANDS:
        return {
            "handled": False,
            "command": command_name,
            "action": "unsupported",
            "message": f"not a supported Run Broker session-history command: /{command_name}",
        }

    key = router_mod._history_key(profile_name, user_key, None)
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
            text = router_mod._webui_profile_scoped_media_response(pending_media_text, profile_home)
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
            # Answer already delivered via streamed content frames above.
            # Return "" so RunBroker.run does NOT emit it a second time.
            return ""
        # Nothing streamed — fall back to one-shot; RunBroker emits it once.
        fallback_text = await real_run_agent(event, profile_home, messages=messages)
        return router_mod._webui_profile_scoped_media_response(fallback_text, profile_home)
    finally:
        _PROFILE_HOME_VAR.reset(token)


def _webui_streamable_media_text(text: str, *, router_mod, profile_home: Path) -> tuple[str, list[str]]:
    """Return pending trailing MEDIA directive text and safe-to-stream chunks."""
    raw = str(text or "")
    marker_index = raw.rfind("MEDIA:")
    if marker_index < 0:
        return "", [router_mod._webui_profile_scoped_media_response(raw, profile_home)]

    newline_after_marker = raw.find("\n", marker_index)
    if newline_after_marker < 0:
        prefix = raw[:marker_index]
        pending = raw[marker_index:]
        items = [router_mod._webui_profile_scoped_media_response(prefix, profile_home)] if prefix else []
        return pending, items

    return "", [router_mod._webui_profile_scoped_media_response(raw, profile_home)]


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
            jobs = cron_api.list_jobs(profile_name, include_disabled=include_disabled)
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
            job = cron_api.create_job(profile_name, user_key, payload)
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
            job = cron_api.get_job(profile_name, request.match_info["job_id"])
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
            plan = cron_api.plan_job(profile_name, request.match_info["job_id"], shadow=shadow, due=due)
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
            job = cron_api.update_job(profile_name, request.match_info["job_id"], payload)
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
            cron_api.delete_job(profile_name, request.match_info["job_id"])
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
            job = cron_api.pause_job(profile_name, request.match_info["job_id"])
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
            job = cron_api.resume_job(profile_name, request.match_info["job_id"])
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
            job = cron_api.trigger_job(profile_name, request.match_info["job_id"])
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
            rows = credential_hub.collect_credential_statuses(
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

    app = web.Application(client_max_size=_run_broker_client_max_size())
    app.router.add_get("/api/run-broker/health", handle_health)
    app.router.add_post("/api/run-broker/runs", handle_run)
    app.router.add_post("/api/run-broker/clarify/{clarify_id}/respond", handle_clarify_respond)
    app.router.add_post("/api/run-broker/session-commands", handle_session_command)
    app.router.add_post("/api/run-broker/goals/evaluate", handle_goal_evaluate)
    app.router.add_post("/api/run-broker/profiles", handle_provision_profile)
    app.router.add_get("/api/run-broker/slash/commands", handle_slash_commands)
    app.router.add_get("/api/run-broker/credentials/feishu/uat/status", handle_feishu_uat_status)
    app.router.add_get("/api/run-broker/credentials/hub", handle_credential_hub)
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
