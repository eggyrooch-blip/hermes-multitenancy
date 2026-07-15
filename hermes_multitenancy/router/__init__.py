"""Pre-gateway-dispatch hook callback (sync) + async dispatch entry point.

Wires together: SQLite RoutingTable (production) + in-memory _SPIKE_ROUTING
(fallback) + LRU RuntimePool (cached profile runtimes) + Hermes-derived slash
command dispatch.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import importlib
import ipaddress
import inspect
import json
import logging
import os
import re
import shlex
import shutil
import socket
import time
import urllib.parse
import urllib.request
import zlib
import zipfile
from contextlib import asynccontextmanager
from itertools import zip_longest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

from ..security_audit import append_security_event

SYNTHETIC_AUTH_COMPLETE_TEXT = "我已完成飞书账号授权，请继续执行之前的操作。"

# Skill slash aliases (hardcoded base + dynamic per-profile scan) live in
# `skill_slash._resolve_alias`; both the Feishu and broker paths share that one source.

try:  # Shared Hermes Feishu card/typewriter transport.
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig  # type: ignore
except Exception:  # pragma: no cover - allows plugin unit tests without gateway on path
    GatewayStreamConsumer = None  # type: ignore
    StreamConsumerConfig = None  # type: ignore

# Per-context in-flight dispatch tasks — used by /stop and replacement turns to
# cancel only the matching profile/chat task while preserving enough history to
# resume.
_user_inflight_tasks: dict[tuple[str, str, str], asyncio.Task] = {}
_user_inflight_history_keys: dict[tuple[str, str, str], tuple[str, str]] = {}
_suppress_interruption_marker_tasks: set[asyncio.Task] = set()
_synthetic_session_guards: dict[str, Any] = {}

# Group-chat profile state. Populated by the bot_added hook (Layer 4, runs
# on the Lark SDK thread) and mirrored into the SQLite routing table as soon
# as the trusted inviter is captured. This cache/pending hand-off remains as
# a short-lived compatibility fallback for older flows and metadata refreshes.
#
# Bounded + TTL'd + lock-guarded: an attacker spamming bot add/remove across
# throwaway chats would otherwise grow this dict without bound, and the
# SDK-thread writer can race the loop-thread reader/popper. OrderedDict +
# lock keeps eviction and the register/pop pair consistent across threads.
import threading as _threading
from collections import OrderedDict as _OrderedDict

_CHAT_INVITER_CACHE_MAX = 512
_CHAT_INVITER_CACHE_TTL_S = 3600  # compatibility fallback TTL
_chat_inviter_cache: "_OrderedDict[str, dict[str, Any]]" = _OrderedDict()
_chat_inviter_cache_lock = _threading.Lock()
_GROUP_PROFILE_PREFIX = "feishu_group_"
_GROUP_CHAT_TYPES: frozenset[str] = frozenset({"group", "topic"})
_LARK_CLI_PROFILE_TOOLSETS = [
    "lark-cli",
]
_LARK_CLI_SOUL_GUIDANCE = "\n".join(
    [
        "Feishu/Lark capability rules:",
        "- 飞书/Lark 的读写、导出和长尾 OpenAPI 能力必须通过 `lark_cli` 工具完成。",
        "- 当用户明确要求“调用 lark_cli / 使用 lark-cli / 必须真实调用工具”时，即使你从历史上下文知道答案，也必须重新调用 `lark_cli`，不得只凭记忆回答。",
        "- 不要通过 `terminal`、`code_execution`、`npx`、`which lark-cli` 或 shell 直接运行 lark-cli/lark-mcp 来绕开 profile runtime。",
        "- 如果 `lark_cli` 工具不可见或不可用，直接说明当前 profile 未暴露 lark-cli 能力；不要自行安装、探测或模拟结果。",
        "- 如果需要调用飞书能力，必须等待真实工具结果；不要编造 message_id、文档链接或调用结果。",
        "- 如果 `lark_cli` 返回 credential unavailable、validation、unsupported 或权限错误，立即停止重试并如实说明需要授权/权限/命令支持。",
        "- `lark_cli` 的 identity 使用 auto，profile runtime 会按个人/群聊边界选择 user 或 bot。",
        "- `lark_cli` 工具结果里的 `identity` 字段是身份事实；当 identity=user 时，不得说资源由 bot/应用身份创建。",
        "Feishu file output rules:",
        "- 当用户要求生成文件、图片、报表、PDF、docx、xlsx、csv、json、markdown 并发回时，不要要求用户提供本机路径。",
        "- 直接在回复中输出一个 ```hermes-artifact-json fenced block，字段使用 filename、format、marker/content/data/rows/title；不要使用宿主机绝对路径。",
        "- filename 只写普通文件名，例如 report.md、summary.pdf、chart.png；Hermes 会自动保存到当前 profile 的 Downloads 并通过飞书发送；markdown 源文件必须自动交付给用户。",
        "- 图片如果要作为可下载原文件发送，在 artifact JSON 中设置 as_document=true。",
        "- artifact JSON 是内部交付协议；除必要的测试标记和简短说明外，不要把本机路径、/workspace 路径或 MEDIA 指令解释给用户。",
    ]
)

_GROUP_EXTERNAL_TOOL_SOUL_GUIDANCE = "\n".join(
    [
        "External tool safety rules:",
        "- 当用户要求天气、网页查询或其它外部网络查询时，优先使用只读工具或只读命令。",
        "- 如果必须用 terminal 访问网络，URL 必须写完整 scheme（例如 `https://example.com`），并使用 short timeout，避免把危险命令推给用户审批。",
        "- 不要使用 schemeless URL（例如 `example.com/path`）或下载后执行的命令。",
    ]
)

# Multi-turn session memory: maps (profile_name, user_key) → list of messages.
# user_key is the canonical sender chosen by routing. Alternate IDs are lookup
# helpers only; using them as the memory key can merge distinct users that share
# a stale or tenant-global alias.
_SESSION_HISTORY_MAX = 20  # keep last N messages (user + assistant alternating)
_DEDUPE_MESSAGE_TTL_SECONDS = 24 * 60 * 60
_DEDUPE_CONTENT_TTL_SECONDS = 2 * 60 * 60
_DEDUPE_CONTENT_MIN_CHARS = 40
_session_history: dict[tuple[str, str], list[dict]] = {}
# Tracks which (profile, user_key) pairs we've lazy-loaded from SessionStore
# so we only hit SQLite once per pair per process lifetime.
_session_loaded: set[tuple[str, str]] = set()
_pending_approval_requests: dict[str, list[dict]] = {}
_PENDING_AUTH_REPLAY_TTL_SECONDS = 600
_PENDING_AUTH_REPLAY_MAX = 2000
_pending_auth_replay: dict[str, tuple[float, str]] = {}
_RECENT_PROFILE_FILE_CONTEXT_MAX = 5
_recent_profile_files_by_chat: dict[tuple[str, str], list[str]] = {}
_RECENT_FILE_CONTEXT_TRIGGER_RE = re.compile(
    r"(这个文件|该文件|这个文档|该文档|刚才.*文件|上面.*文件|源文件|markdown|Markdown|\.md\b|转成飞书云文档|转云文档)"
)


def _history_key(
    profile_name: str,
    sender: str,
    sender_alt: Optional[str],
    *,
    channel: str = "feishu",
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    route_version: int = 0,
) -> tuple[str, str]:
    """Return the per-(profile, user) key used to look up conversation history.

    Constructed via the typed SessionScope (P1-7). With strict context OFF the
    result is byte-identical to the legacy ``(profile, user_key)`` tuple; with
    strict ON channel/chat/thread/route_version isolate surfaces & threads.
    """
    from ..session_scope import build_session_scope

    return build_session_scope(
        profile_name=profile_name,
        user_key=_tenant_user_key(sender, sender_alt),
        channel=channel,
        chat_id=chat_id,
        thread_id=thread_id,
        route_version=route_version,
    ).history_key


def _pending_auth_replay_key(profile_name: str, open_id: str) -> str:
    return f"{str(profile_name or '').strip()}\x1f{str(open_id or '').strip()}"


def _capture_pending_auth_replay(profile_name: str, open_id: str, text: str) -> None:
    """Stash the user's last substantive request for post-/feishu_auth replay."""
    clean_text = str(text or "").strip()
    clean_profile = str(profile_name or "").strip()
    clean_open_id = str(open_id or "").strip()
    if not clean_text or clean_text.startswith("/") or not clean_profile or not clean_open_id:
        return
    now = time.time()
    expired = [
        key
        for key, (ts, _stored_text) in _pending_auth_replay.items()
        if now - ts > _PENDING_AUTH_REPLAY_TTL_SECONDS
    ]
    for key in expired:
        _pending_auth_replay.pop(key, None)
    if len(_pending_auth_replay) >= _PENDING_AUTH_REPLAY_MAX:
        oldest = sorted(_pending_auth_replay.items(), key=lambda item: item[1][0])
        overflow = len(_pending_auth_replay) - _PENDING_AUTH_REPLAY_MAX + 1
        for key, _value in oldest[: max(1, overflow)]:
            _pending_auth_replay.pop(key, None)
    _pending_auth_replay[_pending_auth_replay_key(clean_profile, clean_open_id)] = (now, clean_text)


def _take_pending_auth_replay(profile_name: str, open_id: str) -> Optional[str]:
    """Pop the stashed request for (profile, open_id) if present and fresh."""
    entry = _pending_auth_replay.pop(_pending_auth_replay_key(profile_name, open_id), None)
    if entry is None:
        return None
    ts, text = entry
    if time.time() - ts > _PENDING_AUTH_REPLAY_TTL_SECONDS:
        return None
    return text


def _inflight_key(
    profile_name: Optional[str],
    sender: str,
    sender_alt: Optional[str],
    chat_id: str,
    *,
    channel: str = "feishu",
    thread_id: Optional[str] = None,
) -> tuple[str, str, str]:
    """Return the profile/chat/user scoped key for replace, /stop, and /status.

    Delegates to the typed SessionScope (P1-7): byte-identical to the legacy
    ``(profile, chat, user_key)`` tuple with strict context OFF; channel/thread
    folded into the user dimension with strict ON.
    """
    from ..session_scope import build_session_scope

    return build_session_scope(
        profile_name=profile_name,
        user_key=_tenant_user_key(sender, sender_alt),
        channel=channel,
        chat_id=chat_id,
        thread_id=thread_id,
    ).inflight_key


_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _safe_command_kind(command: str) -> str:
    """Extract ONLY the executable basename for the approval audit — never a raw
    path nor any secret. Uses shlex so a quoted env-assignment value
    (``VAR='top secret' cmd``) stays one token and is skipped; leading
    ``VAR=...`` env assignments are dropped, then the real executable's basename
    is returned. Any pathological result containing '=' or whitespace falls back
    to a sentinel rather than risk leaking a fragment."""
    stripped = str(command or "").strip()
    if not stripped:
        return "<empty>"
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return "<unparseable>"
    idx = 0
    while idx < len(tokens) and _ENV_ASSIGN_RE.match(tokens[idx]):
        idx += 1
    if idx >= len(tokens):
        return "<env-only>" if tokens else "<empty>"
    base = os.path.basename(tokens[idx]) or tokens[idx]
    if "=" in base or any(ch.isspace() for ch in base):
        return "<redacted>"
    return base[:32]


def _tenant_user_key(sender: str, sender_alt: Optional[str]) -> str:
    """Return the canonical per-user key for memory/session scoping."""
    sender_key = str(sender or "").strip()
    if sender_key and sender_key != "unknown":
        return sender_key
    alt_key = str(sender_alt or "").strip()
    return alt_key or sender_key or "unknown"


def _event_thread_id(event: Any) -> Optional[str]:
    """Best-effort thread/topic id from a Feishu event source (mirrors the
    agent_real extraction: thread_id, then chat_topic)."""
    source = getattr(event, "source", None) if event is not None else None
    if source is None:
        return None
    value = getattr(source, "thread_id", None) or getattr(source, "chat_topic", None)
    return str(value) if value else None


def _route_version_for(
    sender: str, sender_alt: Optional[str], chat_id: Optional[str]
) -> int:
    """Resolve the routing-row version so a strict session invalidates when the
    route changes. Fail-open to 0 (no invalidation) if routing is unavailable."""
    try:
        table = _get_routing_table()
        if table is None:
            return 0
        ctx = table.resolve_context(sender, alt_id=sender_alt, chat_id=chat_id)
        return int(getattr(ctx, "route_version", 0) or 0) if ctx is not None else 0
    except Exception:
        return 0


def _dispatch_session_scope(
    profile_name: Optional[str],
    sender: str,
    sender_alt: Optional[str],
    chat_id: Optional[str],
    event: Any = None,
    *,
    channel: str = "feishu",
):
    """Build the typed SessionScope for a dispatch (P1-7). In default (strict
    OFF) mode the thread/route_version discriminators are IGNORED by the scope,
    so we skip resolving them entirely — keeping the prod hot path zero-overhead
    AND byte-identical to the legacy keys. Under strict mode they are resolved so
    DM/group-thread/route-version sessions are isolated."""
    from ..session_scope import build_session_scope
    from ..runtime import strict_context_enabled

    thread_id: Optional[str] = None
    route_version = 0
    if strict_context_enabled():
        thread_id = _event_thread_id(event)
        route_version = _route_version_for(sender, sender_alt, chat_id)
    return build_session_scope(
        profile_name=profile_name,
        user_key=_tenant_user_key(sender, sender_alt),
        channel=channel,
        chat_id=chat_id,
        thread_id=thread_id,
        route_version=route_version,
    )


def _trim_history(history: list[dict]) -> list[dict]:
    """Keep at most ``_SESSION_HISTORY_MAX`` most recent messages."""
    if len(history) <= _SESSION_HISTORY_MAX:
        return history
    return history[-_SESSION_HISTORY_MAX:]


_WRONG_LARK_CLI_BOT_IDENTITY_NOTE_RE = re.compile(
    r"(?im)^[^\n]*(?:(?:由|以|当前)\s*(?:bot|应用)\s*身份(?:创建|下)?)[^\n]*(?:\n|$)"
)


def _strip_wrong_lark_cli_bot_identity_note(text: str) -> str:
    """Remove stale hallucinated bot-identity notes from Feishu UAT history."""
    cleaned = _WRONG_LARK_CLI_BOT_IDENTITY_NOTE_RE.sub("", str(text or ""))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _sanitize_history_messages(history: list[dict]) -> list[dict]:
    sanitized: list[dict] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        if copied.get("role") == "assistant":
            copied["content"] = _strip_wrong_lark_cli_bot_identity_note(str(copied.get("content") or ""))
        sanitized.append(copied)
    return sanitized


def _load_history(key: tuple[str, str]) -> list[dict]:
    """Get history for ``key`` — first call hydrates from SessionStore, subsequent calls hit cache."""
    if key in _session_loaded:
        return _sanitize_history_messages(list(_session_history.get(key, [])))
    store = _get_session_store()
    if store is not None:
        try:
            persisted = store.load_recent(key[0], key[1], _SESSION_HISTORY_MAX)
        except Exception as exc:
            logger.debug("multitenancy: SessionStore.load_recent failed (%s)", exc)
            persisted = []
        if persisted:
            _session_history[key] = _sanitize_history_messages(persisted)
    _session_loaded.add(key)
    return _sanitize_history_messages(list(_session_history.get(key, [])))


def _persist_history_message(key: tuple[str, str], message: dict) -> None:
    """Append one user/assistant message to cache and SessionStore."""
    role = str(message.get("role") or "").strip()
    content = str(message.get("content") or "")
    if role not in {"user", "assistant"} or not content:
        return
    if role == "assistant":
        content = _strip_wrong_lark_cli_bot_identity_note(content)
    new_history = _session_history.get(key, []) + [{"role": role, "content": content}]
    _session_history[key] = _trim_history(new_history)
    store = _get_session_store()
    if store is None:
        return
    try:
        store.append(key[0], key[1], role, content)
    except Exception as exc:
        logger.debug("multitenancy: SessionStore.append(%s) failed (%s)", role, exc)


def _persist_turn(key: tuple[str, str], user_msg: dict, assistant_text: str) -> None:
    """Append a (user, assistant) turn to both in-memory cache and SessionStore."""
    _persist_history_message(key, user_msg)
    _persist_history_message(key, {"role": "assistant", "content": assistant_text})


def _persist_user_message(key: tuple[str, str], user_msg: dict) -> None:
    """Persist the user request before execution so interruption can resume."""
    _persist_history_message(key, user_msg)


def _persist_assistant_message(key: tuple[str, str], assistant_text: str) -> None:
    """Persist an assistant response or interruption marker."""
    _persist_history_message(key, {"role": "assistant", "content": assistant_text})


_INTERRUPTED_TASK_MESSAGE = (
    "上一个任务在完成前被中断或取消；如果用户要求继续，请根据上一条用户请求继续推进，不要丢失上下文。"
)
_FAILED_TASK_MESSAGE = (
    "上一个任务在完成前执行失败或中断；如果用户要求继续，请根据上一条用户请求继续推进，不要丢失上下文。"
)


def _persist_interruption_marker(key: tuple[str, str]) -> None:
    """Persist the canonical interruption marker, avoiding duplicate adjacent markers."""
    history = _session_history.get(key, [])
    if history and history[-1].get("role") == "assistant" and history[-1].get("content") == _INTERRUPTED_TASK_MESSAGE:
        return
    _persist_assistant_message(key, _INTERRUPTED_TASK_MESSAGE)


def _persist_failure_marker(key: tuple[str, str]) -> None:
    """Persist a resumable failure marker without exposing exception details."""
    history = _session_history.get(key, [])
    if history and history[-1].get("role") == "assistant" and history[-1].get("content") == _FAILED_TASK_MESSAGE:
        return
    _persist_assistant_message(key, _FAILED_TASK_MESSAGE)


def _cancel_inflight_task(key: tuple[str, str, str], *, preserve_resume_marker: bool) -> Optional[asyncio.Task]:
    """Cancel one active dispatch and optionally write a resumable marker first."""
    task = _user_inflight_tasks.pop(key, None)
    hist_key = _user_inflight_history_keys.pop(key, None)
    if task is None or task.done():
        return task
    if preserve_resume_marker and hist_key is not None:
        _persist_interruption_marker(hist_key)
    _suppress_interruption_marker_tasks.add(task)
    task.cancel()
    return task


def _clear_history(key: tuple[str, str]) -> bool:
    """Drop a user's history from cache + store. Returns True if anything was cleared."""
    had_cache = _session_history.pop(key, None) is not None
    _session_loaded.discard(key)
    store = _get_session_store()
    if store is None:
        return had_cache
    try:
        rows = store.clear(key[0], key[1])
    except Exception as exc:
        logger.debug("multitenancy: SessionStore.clear failed (%s)", exc)
        rows = 0
    return had_cache or rows > 0


def _dedupe_env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default)).strip()))
    except Exception:
        return default


def _event_message_id(event: Any) -> Optional[str]:
    source = getattr(event, "source", None)
    candidates = [
        getattr(event, "message_id", None),
        getattr(source, "message_id", None) if source is not None else None,
    ]
    raw_event = getattr(event, "raw_event", None)
    if isinstance(raw_event, dict):
        message = (raw_event.get("event") or {}).get("message") or {}
        candidates.append(message.get("message_id"))
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return None


_REACTION_SYNTHETIC_PREFIXES = ("reaction:added:", "reaction:removed:")


def _is_reaction_synthetic_event(event: Any, text: str) -> bool:
    """Return True for Feishu reaction events routed as synthetic text."""
    if not isinstance(text, str):
        return False
    if not text.startswith(_REACTION_SYNTHETIC_PREFIXES):
        return False
    message_type = getattr(event, "message_type", None)
    if message_type is None:
        return True
    return str(getattr(message_type, "name", message_type)).upper() == "TEXT"


def _is_interactive_or_card_event(event: Any) -> bool:
    message_type_obj = getattr(event, "message_type", None)
    message_type_parts = [
        str(getattr(message_type_obj, "value", "") or ""),
        str(getattr(message_type_obj, "name", "") or ""),
        str(message_type_obj or ""),
    ]
    message_type = " ".join(message_type_parts).lower()
    return "interactive" in message_type or "card" in message_type


def _event_reply_to_message_id(event: Any) -> Optional[str]:
    """Anchor the bot reply to the user's original message in ALL chat types.

    Mirrors openclaw-lark (``replyToMessageId ?? ctx.messageId``): the reply
    card quotes the message it answers, in groups AND p2p/dm. This is safe in
    p2p because ``_thread_metadata_for_media_delivery`` returns None for
    non-group chats, so core derives ``reply_in_thread = bool(thread_id)`` =
    False (feishu.py) -> an ordinary quoted reply, NOT a visible topic. The
    earlier p2p->None short-circuit avoided a topic that only appears when
    reply_in_thread is True, which p2p never sets.
    """
    return _event_message_id(event)


def _normalize_dedupe_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _run_request_dedupe_record(request: Any) -> Optional[tuple[str, Optional[str], Optional[str], int]]:
    idempotency_key = str(getattr(request, "idempotency_key", "") or "").strip()
    channel = str(getattr(request, "channel", "") or "").strip()
    message_id = str(getattr(request, "message_id", "") or "").strip()
    profile_name = str(getattr(request, "profile_name", "") or "").strip()
    user_key = str(getattr(request, "user_key", "") or "").strip()
    content = str(getattr(request, "content", "") or "")
    if idempotency_key:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        ttl = _dedupe_env_int(
            "HERMES_MULTITENANCY_EVENT_DEDUPE_TTL_SECONDS",
            _DEDUPE_MESSAGE_TTL_SECONDS,
        )
        return (f"idem:{channel}:{profile_name}:{user_key}:{digest}", None, None, ttl)
    if message_id:
        ttl = _dedupe_env_int(
            "HERMES_MULTITENANCY_EVENT_DEDUPE_TTL_SECONDS",
            _DEDUPE_MESSAGE_TTL_SECONDS,
        )
        return (f"msg:{profile_name}:{user_key}:{message_id}", message_id, None, ttl)
    if channel == "webui":
        return None

    normalized = _normalize_dedupe_text(content)
    min_chars = _dedupe_env_int(
        "HERMES_MULTITENANCY_CONTENT_DEDUPE_MIN_CHARS",
        _DEDUPE_CONTENT_MIN_CHARS,
    )
    if len(normalized) < min_chars:
        return None
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    ttl = _dedupe_env_int(
        "HERMES_MULTITENANCY_CONTENT_DEDUPE_TTL_SECONDS",
        _DEDUPE_CONTENT_TTL_SECONDS,
    )
    return (f"content:{profile_name}:{user_key}:{digest}", None, digest, ttl)


def _mark_run_request_seen(request: Any) -> bool:
    """Return False when this broker RunRequest was processed recently."""
    store = _get_session_store()
    if store is None:
        return True
    record = _run_request_dedupe_record(request)
    if record is None:
        return True
    event_key, message_id, content_hash, ttl = record
    try:
        return bool(store.mark_event_processed(
            event_key,
            profile_name=str(getattr(request, "profile_name", "") or ""),
            user_key=str(getattr(request, "user_key", "") or ""),
            message_id=message_id,
            content_hash=content_hash,
            ttl_seconds=ttl,
        ))
    except Exception as exc:
        logger.debug("multitenancy: run request dedupe check failed (%s)", exc)
        return True


def _host_tools_require_sandbox() -> bool:
    value = os.environ.get("HERMES_REQUIRE_SANDBOX_FOR_HOST_TOOLS", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _router_sandbox_available() -> bool:
    value = os.environ.get("HERMES_USE_SANDBOX", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _run_request_for_routed_event(
    *,
    event: Any,
    profile_name: str,
    sender: str,
    sender_alt: Optional[str],
    chat_id: str,
    text: str,
):
    from ..run_models import RunRequest

    user_key = _tenant_user_key(sender, sender_alt)
    metadata = {
        "sender_open_id": sender,
        "chat_type": _extract_chat_type(event),
    }
    if sender_alt:
        metadata["sender_alt"] = sender_alt
    return RunRequest(
        channel="feishu",
        profile_name=profile_name,
        user_key=user_key,
        content=text,
        chat_id=chat_id,
        message_id=_event_message_id(event),
        credential_subject=user_key,
        requires_host_tools=_host_tools_require_sandbox(),
        metadata=metadata,
    )


def _make_routed_run_broker(*, dispatch_agent: Any = None):
    from ..billing_identity import prepare_billing_request
    from ..run_broker import RunBroker

    return RunBroker(
        dispatch_agent=dispatch_agent or (lambda _request: ""),
        mark_seen=_mark_run_request_seen,
        sandbox_available=_router_sandbox_available,
        prepare_request=prepare_billing_request,
    )


def _build_user_message(event: Any, *, text_override: Optional[str] = None) -> dict:
    """Construct the OpenAI-format user message, splicing in reply context if any.

    Reply context: hermes mainstream sets ``event.reply_to_text`` when the
    user is quoting an earlier message. We surface it inline so the model
    knows what's being replied to.

    ``text_override`` lets ``_enrich_with_vision`` rewrite the text before
    the message is built (so reply context still wraps the enriched text).
    """
    text = text_override if text_override is not None else (getattr(event, "text", "") or "")
    reply_to_text = getattr(event, "reply_to_text", None)
    if reply_to_text:
        text = f"(replying to: {reply_to_text})\n{text}"
    return {"role": "user", "content": text}


def _normalize_feishu_open_id(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if raw.startswith("user:"):
        raw = raw.split(":", 1)[1]
    return raw if raw.startswith("ou_") else None


def _is_feishu_open_id(value: Any) -> bool:
    return _normalize_feishu_open_id(value) is not None


def _nested_get(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
    return current


def _current_sender_open_id() -> Optional[str]:
    """Return the adapter-provided Feishu open_id context, if available."""
    try:
        from tools import feishu_oapi_client as feishu_oapi  # type: ignore

        candidate = feishu_oapi.current_sender_open_id.get()
    except Exception:
        return None
    return _normalize_feishu_open_id(candidate)


def _resolve_sender_for_routing(event: Any, *, fallback: str = "unknown") -> str:
    """Pick the stable Feishu user key used by the multitenancy router.

    Feishu SDK events can expose ``source.user_id`` as a short tenant-local ID
    such as ``g41a5b5g``. The UAT files and explicit routes are keyed by the
    real app-scoped open_id (``ou_*``), so prefer that when the adapter or raw
    event makes it available.
    """
    source = getattr(event, "source", None)
    direct_candidates = (
        _current_sender_open_id(),
        getattr(event, "sender_open_id", None),
        getattr(source, "open_id", None) if source is not None else None,
        getattr(source, "user_id", None) if source is not None else None,
        getattr(source, "user_id_alt", None) if source is not None else None,
    )
    for candidate in direct_candidates:
        normalized = _normalize_feishu_open_id(candidate)
        if normalized:
            return normalized

    raw_candidates = (
        getattr(event, "raw", None),
        getattr(event, "raw_event", None),
        getattr(event, "event", None),
    )
    paths = (
        ("sender", "sender_id", "open_id"),
        ("event", "sender", "sender_id", "open_id"),
        ("event", "message", "sender", "sender_id", "open_id"),
        ("message", "sender", "sender_id", "open_id"),
        ("sender_id", "open_id"),
    )
    for raw in raw_candidates:
        for path in paths:
            candidate = _nested_get(raw, path)
            normalized = _normalize_feishu_open_id(candidate)
            if normalized:
                return normalized

    return fallback


def _event_with_text(event: Any, text: str) -> Any:
    """Return an event-shaped object whose ``text`` matches the agent prompt."""
    if text == (getattr(event, "text", "") or ""):
        return event
    cloned = copy.copy(event)
    setattr(cloned, "text", text)
    return cloned


def _event_with_run_metadata(event: Any, metadata: dict[str, Any]) -> Any:
    """Copy trusted RunBroker metadata into the agent subprocess event."""
    cloned = copy.copy(event)
    raw_event = dict(getattr(event, "raw_event", None) or {})
    raw_metadata = dict(raw_event.get("metadata") or {})
    raw_metadata.update(metadata or {})
    raw_event["metadata"] = raw_metadata
    setattr(cloned, "raw_event", raw_event)
    return cloned






def _sanitize_tool_event_payload(payload: Any, profile_home: Optional[Path]) -> dict[str, Any]:
    """Prepare tool progress metadata for user-visible cards."""
    data = payload if isinstance(payload, dict) else {"name": str(payload or "tool")}
    result: dict[str, Any] = {}
    for key, value in data.items():
        if key in {"preview", "args", "name", "tool_name", "duration", "is_error"}:
            result[key] = _sanitize_tool_event_value(value, profile_home, key=str(key))
    return result


def _sanitize_tool_event_value(value: Any, profile_home: Optional[Path], *, key: str = "") -> Any:
    if re.search(r"(token|secret|password|passwd|credential|authorization)", key, re.IGNORECASE):
        return "[已隐藏]"
    if isinstance(value, str):
        return _sanitize_tool_event_string(value, profile_home)
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_tool_event_value(item_value, profile_home, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_tool_event_value(item, profile_home, key=key) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_tool_event_value(item, profile_home, key=key) for item in value)
    return value


def _sanitize_tool_event_string(text: str, profile_home: Optional[Path]) -> str:
    raw = str(text or "")
    if not raw:
        return raw
    cleaned = raw
    root: Optional[Path] = None
    if profile_home is not None:
        root = Path(profile_home).expanduser().resolve(strict=False)
        root_text = str(root)
        cleaned = cleaned.replace(root_text + "/", "")
        if cleaned == root_text:
            cleaned = "."
        workspace_root = str(root / "workspace")
        cleaned = cleaned.replace(workspace_root + "/", "workspace/")
    cleaned = re.sub(r"(?<!\w)/workspace/", "workspace/", cleaned)

    def repl(match: re.Match[str]) -> str:
        candidate = match.group(0).rstrip(".,;:)]}")
        suffix = match.group(0)[len(candidate) :]
        if root is not None:
            try:
                resolved = Path(candidate).expanduser().resolve(strict=False)
                if resolved == root:
                    return "." + suffix
                if root in resolved.parents:
                    return resolved.relative_to(root).as_posix() + suffix
            except Exception:
                pass
        return "[宿主路径已隐藏]" + suffix

    cleaned = re.sub(r"/(?:Users|home)/[^\s`\"'<>]+", repl, cleaned)
    return cleaned


_MEDIA_DIRECTIVE_RE = re.compile(r'''(?P<prefix>[`"']?MEDIA:\s*)(?P<path>\S+)(?P<suffix>[`"']?)''')
_ARTIFACT_JSON_RE = re.compile(r"```hermes-artifact-json\s*(?P<body>.*?)\s*```", re.DOTALL | re.IGNORECASE)
_MARKDOWN_REMOTE_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<url>https?://[^)\s]+)\)", re.IGNORECASE)
_PROFILE_FILE_PATH_RE = re.compile(
    r'''(?P<path>(?:/workspace|/[^`"'<>\n\r]+?)'''
    r'''\.(?:png|jpe?g|gif|webp|mp4|mov|avi|mkv|webm|ogg|opus|mp3|wav|m4a|'''
    r'''epub|pdf|zip|rar|7z|docx?|xlsx?|pptx?|txt|csv|json|md|markdown))'''
)
_FEISHU_DOCUMENT_ID_LINE_RE = re.compile(
    r"(?m)^(?P<prefix>\s*(?:🆔\s*)?(?:(?:飞书)?(?:云)?文档|wiki|Wiki|WIKI|知识库)\s*"
    r"(?:ID|Id|id|Token|token|令牌)?\s*[:：]\s*)"
    r"[`\"']?(?P<token>[A-Za-z0-9]{20,})[`\"']?(?P<suffix>\s*)$"
)
_AUTO_FILE_DELIVERY_MAX_BYTES = int(os.getenv("HERMES_MULTITENANCY_AUTO_FILE_DELIVERY_MAX_BYTES", "52428800"))
_REMOTE_IMAGE_DELIVERY_MAX_BYTES = int(os.getenv("HERMES_MULTITENANCY_REMOTE_IMAGE_DELIVERY_MAX_BYTES", "10485760"))
_REMOTE_IMAGE_DOWNLOAD_TIMEOUT_S = float(os.getenv("HERMES_MULTITENANCY_REMOTE_IMAGE_DOWNLOAD_TIMEOUT", "15"))
_REMOTE_IMAGE_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_MARKDOWN_DOCUMENT_EXTENSIONS = {".md", ".markdown"}
_SENSITIVE_PROFILE_FILE_NAMES = {
    ".env",
    "auth.json",
    "config.yaml",
    "credential-materialization.yaml",
    "credential-materialization.yml",
}
_SENSITIVE_PROFILE_DIR_NAMES = {
    ".aws",
    ".config",
    ".gnupg",
    ".ssh",
    "credentials",
    "feishu_uat",
    "tokens",
}
_DEFAULT_IMAGE_GEN_PROVIDER = "tencent-vod"
_DEFAULT_IMAGE_GEN_MODEL = "gem-3.1"


async def _deliver_media_from_stream_response(
    gateway: Any,
    response: str,
    event: Any,
    adapter: Any,
    profile_home: Path,
) -> None:
    """Delegate post-stream media attachment delivery to Hermes' native gateway path."""
    response = _materialize_response_artifacts(response, profile_home)
    response_with_remote_images = await _append_remote_image_media_directives_async(response, profile_home)
    response_with_files = _append_profile_file_media_directives(response_with_remote_images, profile_home)
    scoped_response = _profile_scoped_media_response(response_with_files, profile_home)
    if "MEDIA:" not in scoped_response:
        return
    delivered = await _deliver_profile_scoped_media_directives(
        adapter,
        event,
        gateway,
        scoped_response,
        profile_home=profile_home,
    )
    if delivered:
        return
    deliver = getattr(gateway, "_deliver_media_from_response", None)
    if not callable(deliver):
        logger.warning("multitenancy: no post-stream media delivery surface available")
        return
    await deliver(scoped_response, event, adapter)


def _feishu_document_base_url() -> Optional[str]:
    configured = os.getenv("HERMES_FEISHU_DOCUMENT_BASE_URL", "").strip()
    return configured.rstrip("/") if configured else "https://feishu.cn"


def _linkify_feishu_document_ids(text: str) -> str:
    """Turn visible Feishu document tokens into clickable document URLs."""
    raw = str(text or "")
    if not raw:
        return raw
    base = _feishu_document_base_url()

    def repl(match: re.Match[str]) -> str:
        token = match.group("token")
        prefix = match.group("prefix").lower()
        resource = "wiki" if "wiki" in prefix or "知识库" in prefix else "docx"
        label = "飞书 Wiki 链接" if resource == "wiki" else "飞书文档链接"
        return f"🔗 {label}：{base}/{resource}/{token}"

    return _FEISHU_DOCUMENT_ID_LINE_RE.sub(repl, raw)


_MEDIA_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_MEDIA_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
_MEDIA_AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".opus", ".m4a", ".flac"}


async def _deliver_profile_scoped_media_directives(
    adapter: Any,
    event: Any,
    gateway: Any,
    scoped_response: str,
    *,
    profile_home: Optional[Path] = None,
) -> int:
    """Deliver already-scoped MEDIA directives without upstream path regex loss."""
    if adapter is None:
        return 0
    source = getattr(event, "source", None)
    chat_id = str(getattr(source, "chat_id", "") or "")
    if not chat_id:
        return 0
    force_document = "[[as_document]]" in str(scoped_response or "")
    audio_as_voice = "[[audio_as_voice]]" in str(scoped_response or "")
    metadata = _thread_metadata_for_media_delivery(gateway, event)
    reply_to = _event_reply_to_message_id(event)
    delivered = 0
    seen: set[Path] = set()
    for match in _MEDIA_DIRECTIVE_RE.finditer(str(scoped_response or "")):
        raw_path = match.group("path").strip().strip("`\"'")
        path = Path(raw_path).expanduser().resolve(strict=False)
        if path in seen or not path.exists() or not path.is_file():
            continue
        seen.add(path)
        ext = path.suffix.lower()
        try:
            if ext in _MEDIA_IMAGE_EXTENSIONS and not force_document and hasattr(adapter, "send_image_file"):
                result = await adapter.send_image_file(
                    chat_id=chat_id,
                    image_path=str(path),
                    reply_to=reply_to,
                    metadata=metadata,
                )
            elif ext in _MEDIA_VIDEO_EXTENSIONS and hasattr(adapter, "send_video"):
                result = await adapter.send_video(
                    chat_id=chat_id,
                    video_path=str(path),
                    reply_to=reply_to,
                    metadata=metadata,
                )
            elif ext in _MEDIA_AUDIO_EXTENSIONS and audio_as_voice and hasattr(adapter, "send_voice"):
                result = await adapter.send_voice(
                    chat_id=chat_id,
                    audio_path=str(path),
                    reply_to=reply_to,
                    metadata=metadata,
                )
            elif hasattr(adapter, "send_document"):
                result = await adapter.send_document(
                    chat_id=chat_id,
                    file_path=str(path),
                    file_name=path.name,
                    reply_to=reply_to,
                    metadata=metadata,
                )
            else:
                continue
            if getattr(result, "success", True):
                delivered += 1
                if profile_home is not None:
                    _remember_recent_profile_file(profile_home.name, chat_id, path, profile_home)
                logger.info("multitenancy: delivered post-stream media attachment path=%s", path)
            else:
                logger.warning(
                    "multitenancy: post-stream media delivery failed path=%s error=%s",
                    path,
                    getattr(result, "error", None),
                )
        except Exception as exc:
            logger.warning("multitenancy: post-stream media delivery failed path=%s error=%s", path, exc)
    return delivered


def _thread_metadata_for_media_delivery(gateway: Any, event: Any) -> Optional[dict[str, Any]]:
    try:
        if not _is_group_chat_type(_extract_chat_type(event)):
            return None
        source = getattr(event, "source", None)
        reply_anchor = None
        anchor_fn = getattr(gateway, "_reply_anchor_for_event", None)
        if callable(anchor_fn):
            reply_anchor = anchor_fn(event)
        meta_fn = getattr(gateway, "_thread_metadata_for_source", None)
        if callable(meta_fn):
            metadata = meta_fn(source, reply_anchor)
            return dict(metadata) if isinstance(metadata, dict) else metadata
    except Exception as exc:
        logger.debug("multitenancy: thread metadata lookup for media delivery failed: %s", exc)
    return None


































































_IMAGE_FILE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}








_AUX_MAIN_RUNTIME_FIELDS = (
    "_RUNTIME_MAIN",
    "_RUNTIME_MAIN_PROVIDER",
    "_RUNTIME_MAIN_MODEL",
    "_RUNTIME_MAIN_BASE_URL",
    "_RUNTIME_MAIN_API_KEY",
    "_RUNTIME_MAIN_API_MODE",
)

# Per-provider override for the auto-detected image-vision model when the core's
# hardcoded choice is unavailable on our account. zai → glm-5v-turbo is not in
# our z.ai plan (HTTP 429 code 1311); glm-4.6v is, and is multimodal. Applied
# (and restored) inside the image-prep runtime patch under the env lock.
_VISION_MODEL_OVERRIDE: dict[str, str] = {
    "zai": "glm-4.6v",
}






















_TEXT_FILE_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".log", ".xml", ".html", ".htm",
}
_MAX_LOCAL_ENRICH_FILE_BYTES = 10 * 1024 * 1024
_MAX_LOCAL_TEXT_PREVIEW_BYTES = 100_000
_MAX_XLSX_XML_BYTES = 2 * 1024 * 1024










_PDF_STREAM_RE = re.compile(rb"stream\r?\n(?P<body>.*?)\r?\n?endstream", re.DOTALL)
_PDF_TEXT_STRING_RE = re.compile(rb"\((?:\\.|[^\\()])*\)")












# -- Hook entry point --------------------------------------------------------


def _register_session_guard_for_dispatch(event: Any, gateway: Any, task: Any) -> None:
    """Mark this dispatch's session as active so flush-batch re-dispatch is no-op.

    Feishu adapter coalesces multiple WS text chunks into a single message via
    _pending_text_batches, then flushes ~0.6s later by calling handle_message
    again through _handle_message_with_guards. Without this guard, the flush
    sees no active session in base.handle_message and spawns a second
    handle_async task (creating a duplicate streaming card).
    """
    try:
        from gateway.session import build_session_key  # type: ignore
    except Exception:
        return
    try:
        adapter = None
        adapters = getattr(gateway, "adapters", None)
        if isinstance(adapters, dict):
            adapter = adapters.get("feishu")
        if adapter is None:
            adapter = getattr(gateway, "feishu_adapter", None)
        if adapter is None:
            return
        active = getattr(adapter, "_active_sessions", None)
        if active is None:
            return
        source = getattr(event, "source", None)
        if source is None:
            return
        config = getattr(adapter, "config", None)
        extra = getattr(config, "extra", {}) if config is not None else {}
        if not isinstance(extra, dict):
            extra = {}
        session_key = build_session_key(
            source,
            group_sessions_per_user=bool(extra.get("group_sessions_per_user", True)),
            thread_sessions_per_user=bool(extra.get("thread_sessions_per_user", False)),
        )
        if not session_key:
            return
        import asyncio as _asyncio
        guard = _asyncio.Event()
        existing = active.get(session_key)
        if existing is not None and _synthetic_session_guards.get(session_key) is not existing:
            return
        active[session_key] = guard
        _synthetic_session_guards[session_key] = guard

        def _cleanup(_t: Any) -> None:
            try:
                if active.get(session_key) is guard and _synthetic_session_guards.get(session_key) is guard:
                    active.pop(session_key, None)
                    _synthetic_session_guards.pop(session_key, None)
            except Exception:
                pass

        task.add_done_callback(_cleanup)
    except Exception as exc:
        logger.debug("multitenancy: session guard registration failed: %s", exc)


def on_pre_gateway_dispatch(*, event: Any, gateway: Any, session_store: Any = None, **_kwargs) -> dict:
    """Sync hook callback (registered to ``pre_gateway_dispatch``).

    Schedules the async work as a background task and returns immediately
    with ``action: skip`` so the gateway main flow halts for this event.
    """
    try:
        if _should_defer_gateway_processing_complete(event):
            _defer_gateway_processing_complete(event, gateway)
        loop = asyncio.get_running_loop()
        task = loop.create_task(handle_async(event=event, gateway=gateway))
        task.add_done_callback(_log_task_failure)
        # Prevent Feishu's text-batch flush (feishu.py:_flush_text_batch_now,
        # ~0.6s after WS dispatch) from re-routing this message through
        # base.handle_message → pre_gateway_dispatch a SECOND time, which
        # would spawn a duplicate handle_async task and create a second
        # streaming card. Register a synthetic guard in the adapter's
        # _active_sessions so handle_message sees the session as busy.
        _register_session_guard_for_dispatch(event, gateway, task)
    except RuntimeError:
        # Test-only fallback: hook called from sync context (no running loop).
        if os.environ.get("PYTEST_CURRENT_TEST"):
            try:
                asyncio.run(handle_async(event=event, gateway=gateway))
            except Exception as exc:
                logger.warning("multitenancy: sync fallback dispatch failed: %s", exc)
        else:
            logger.error(
                "multitenancy: pre_gateway_dispatch invoked without a running loop "
                "and not in pytest — dropping event"
            )
    except Exception as exc:
        logger.warning("multitenancy: failed to schedule handle_async: %s", exc)
    return {"action": "skip", "reason": "multitenancy router took over"}


def _should_defer_gateway_processing_complete(event: Any) -> bool:
    """Return True when the async router owns visible processing lifecycle."""
    from ..commands import parse_command

    source = getattr(event, "source", None)
    fallback_sender = getattr(source, "user_id", "unknown") if source else "unknown"
    sender = _resolve_sender_for_routing(event, fallback=fallback_sender)
    sender_alt = getattr(source, "user_id_alt", None) if source else None
    text = getattr(event, "text", "") or ""
    if parse_command(text) is not None:
        return False

    chat_type = _extract_chat_type(event)
    if _is_group_chat_type(chat_type):
        chat_id = _extract_chat_id(event)
        if not chat_id:
            return False
        table = _get_routing_table()
        if table is not None and table.lookup_by_chat_id(chat_id) is not None:
            return True
        return (
            _auto_provision_enabled()
            and table is not None
            and _has_cached_chat_inviter(chat_id)
        )

    _profile_name, profile_home = _resolve_route(sender, alt_id=sender_alt)
    if profile_home is not None:
        return True
    return (
        _auto_provision_enabled()
        and _get_routing_table() is not None
        and bool(sender)
        and sender != "unknown"
    )


def _defer_gateway_processing_complete(event: Any, gateway: Any) -> None:
    adapter = _get_feishu_adapter(gateway)
    defer = getattr(adapter, "defer_processing_complete", None)
    if not callable(defer):
        return
    try:
        defer(event)
    except Exception as exc:
        logger.debug("multitenancy: defer_processing_complete failed: %s", exc)


# -- Async dispatch ----------------------------------------------------------






# -- Command dispatch --------------------------------------------------------




















# Keep started kep-auth login subprocesses alive until their OAuth callback
# lands (a GC'd Popen would kill the login mid-flow).
_KEP_LOGIN_PROCS: set[Any] = set()
























































def _profile_relative_skill_dir(skill_info: dict[str, Any], profile_home: Optional[Path]) -> Optional[str]:
    if profile_home is None:
        return None
    raw_skill_dir = skill_info.get("skill_dir")
    if not isinstance(raw_skill_dir, str) or not raw_skill_dir:
        return None
    try:
        skill_dir = Path(raw_skill_dir)
        if not skill_dir.is_absolute():
            return None
        return str(skill_dir.relative_to(profile_home / "skills"))
    except Exception:
        return None


def _scope_profile_skill_loader(profile_home: Optional[Path]) -> list[tuple[Any, str, Any, bool]]:
    """Point upstream skill loader module caches at the routed profile."""
    if profile_home is None:
        return []
    states: list[tuple[Any, str, Any, bool]] = []

    def remember(module: Any, attr: str, value: Any) -> None:
        states.append((module, attr, getattr(module, attr, None), hasattr(module, attr)))
        setattr(module, attr, value)

    try:
        from tools import skills_tool  # type: ignore

        remember(skills_tool, "HERMES_HOME", profile_home)
        remember(skills_tool, "SKILLS_DIR", profile_home / "skills")
    except Exception as exc:
        logger.debug("multitenancy: profile skill loader scope skipped (%s)", exc)

    try:
        from agent import skill_commands  # type: ignore

        # ``agent.skill_commands`` caches slash skills per process, while this
        # router process serves many profiles.  Clear the cache inside the
        # scoped context so `/skill` resolves against this routed profile.
        remember(skill_commands, "_skill_commands", {})
        remember(skill_commands, "_skill_commands_platform", None)
    except Exception as exc:
        logger.debug("multitenancy: skill command cache scope skipped (%s)", exc)

    return states


def _restore_profile_skill_loader(states: list[tuple[Any, str, Any, bool]]) -> None:
    for module, attr, old_value, had_attr in reversed(states):
        try:
            if had_attr:
                setattr(module, attr, old_value)
            else:
                delattr(module, attr)
        except Exception:
            pass


@asynccontextmanager
async def _profile_gateway_context(
    gateway: Any,
    event: Any,
    *,
    sender: str,
    sender_alt: Optional[str],
    profile_name: Optional[str],
    profile_home: Optional[Path],
    chat_id: str,
):
    """Temporarily scope Hermes gateway helpers to the routed profile."""
    from ..runtime import _get_env_lock

    async with _get_env_lock():
        old_home = os.environ.get("HERMES_HOME")
        had_home = "HERMES_HOME" in os.environ
        old_session_key_for_source = getattr(gateway, "_session_key_for_source", None)
        if profile_home is not None:
            os.environ["HERMES_HOME"] = str(profile_home)
        skill_loader_states = _scope_profile_skill_loader(profile_home)
        session_key = _multitenant_gateway_session_key(
            event,
            profile_name=profile_name,
            sender=sender,
            sender_alt=sender_alt,
            chat_id=chat_id,
        )
        if session_key:
            def _scoped_session_key_for_source(source):
                return session_key

            setattr(gateway, "_session_key_for_source", _scoped_session_key_for_source)
        try:
            yield
        finally:
            if old_session_key_for_source is not None:
                setattr(gateway, "_session_key_for_source", old_session_key_for_source)
            elif hasattr(gateway, "_session_key_for_source"):
                try:
                    delattr(gateway, "_session_key_for_source")
                except Exception:
                    pass
            _restore_profile_skill_loader(skill_loader_states)
            if had_home:
                os.environ["HERMES_HOME"] = old_home or ""
            else:
                os.environ.pop("HERMES_HOME", None)


def _multitenant_gateway_session_key(
    event: Any,
    *,
    profile_name: Optional[str],
    sender: str,
    sender_alt: Optional[str],
    chat_id: str,
) -> Optional[str]:
    if not profile_name:
        return None
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None) or "feishu"
    user_key = _tenant_user_key(sender, sender_alt)
    return f"multitenancy:{platform}:{profile_name}:{chat_id}:{user_key}"


def _gateway_help_text() -> str:
    """Render help from Hermes' central command registry when available."""
    try:
        from hermes_cli.commands import gateway_help_lines  # type: ignore

        lines = gateway_help_lines()
        if lines:
            help_lines = [line for line in lines if "/help" in line]
            lines = (help_lines[:1] or ["`/help` -- 显示这条帮助"]) + [
                line for line in lines if "/help" not in line
            ]
            return "📖 可用命令\n" + "\n".join(lines[:30])
    except Exception:
        pass
    return (
        "📖 可用命令\n"
        "/help    — 显示这条帮助\n"
        "/status  — 查看当前 profile + 历史长度\n"
        "/new     — 重置会话历史 (开始新对话)\n"
        "/reset   — /new 的别名\n"
        "/stop    — 取消正在运行的任务\n"
        "/model   — 切换当前会话模型\n"
        "/reasoning — 管理推理强度\n"
        "/voice   — 切换语音回复模式\n"
    )


# -- Routing resolution ------------------------------------------------------


def _resolve_route(sender: str, *, alt_id: Optional[str] = None) -> tuple[str, Optional[Path]]:
    """Resolve sender → (profile_name, profile_home).

    The routing table's ``open_id`` column is overloaded as "any stable user
    identifier" — it can hold a real Feishu open_id (``ou_xxx``), a union_id
    (``on_xxx``), or any other tenant-stable token chosen by feishu-sync.

    Lookup order:
      1. SQLite RoutingTable WHERE open_id = sender (typical: real Feishu ou_* open_id)
      2. SQLite RoutingTable WHERE open_id = alt_id (legacy: source.user_id_alt = union_id)
      3. In-memory ``_SPIKE_ROUTING`` dict (Phase 1 compat / unit tests)

    Returns (sender, None) when no route hits.
    """
    from ..runtime import resolve_profile_home as _spike_resolve

    table = _get_routing_table()
    candidates: list[str] = [sender]
    if alt_id and alt_id != sender:
        candidates.append(alt_id)

    if table is not None:
        for candidate in candidates:
            try:
                row = table.lookup_by_open_id(candidate)
            except Exception as exc:
                logger.debug("multitenancy: routing lookup failed (%s)", exc)
                continue
            if row is not None:
                return (row.profile_name, _profile_name_to_home(row.profile_name))
        # alt_id is the dedicated union_id channel — when sync chose to store a
        # tenant user_id placeholder in the open_id column, the real on_* still
        # lives in the union_id column. Query it directly so router doesn't end
        # up provisioning a duplicate route for the same physical user.
        if alt_id and isinstance(alt_id, str) and alt_id.startswith("on_"):
            try:
                row = table.lookup_by_union_id(alt_id)
            except Exception as exc:
                logger.debug("multitenancy: routing union_id lookup failed (%s)", exc)
            else:
                if row is not None:
                    return (row.profile_name, _profile_name_to_home(row.profile_name))

    # Spike routing dict (Phase 1 compat / unit tests)
    for candidate in candidates:
        spike_home = _spike_resolve(candidate)
        if spike_home is not None:
            return (spike_home.name, spike_home)
    return (sender, None)


def _repair_auto_profile(
    profile_name: str,
    profile_home: Path,
    *,
    route_key: str,
    sender: str,
) -> None:
    if not profile_name.startswith("feishu_"):
        return
    try:
        _ensure_auto_profile(profile_name, profile_home, route_key=route_key, sender=sender)
    except Exception as exc:
        logger.debug("multitenancy: auto profile repair failed for %s: %s", profile_name, exc)


def _dev_mode_enabled() -> bool:
    """Explicit dev/demo escape hatch — re-enables conveniences that strict mode
    turns off by default."""
    return os.environ.get("HERMES_MULTITENANCY_DEV_MODE", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _auto_profile_name(route_key: str) -> str:
    """Return a deterministic, filesystem-safe profile name for a tenant key."""
    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", route_key).strip("_")
    if not clean:
        clean = "unknown"
    return f"feishu_{clean}"


# -- Group-chat profile routing (Layer 2) -----------------------------------


def _extract_chat_type(event: Any) -> str:
    """Best-effort chat_type extraction.

    Feishu v1 message events expose ``source.chat_type`` (``"p2p"`` |
    ``"group"`` | ``"topic"``). Some test fixtures and webhook variants only
    have ``event.chat_type`` or ``event.message.chat_type``. Returns ``""``
    if nothing is available.
    """
    source = getattr(event, "source", None)
    candidates = (
        getattr(source, "chat_type", None) if source is not None else None,
        getattr(event, "chat_type", None),
        getattr(getattr(event, "message", None), "chat_type", None),
    )
    for candidate in candidates:
        if candidate:
            return str(candidate).strip().lower()
    return ""


def _extract_chat_id(event: Any) -> str:
    """Best-effort chat_id extraction with the same fallback chain as feishu.py."""
    source = getattr(event, "source", None)
    candidates = (
        getattr(source, "chat_id", None) if source is not None else None,
        getattr(source, "parent_chat_id", None) if source is not None else None,
        getattr(source, "chat_id_alt", None) if source is not None else None,
        getattr(event, "chat_id", None),
        getattr(getattr(event, "message", None), "chat_id", None),
    )
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return ""


# Commands that read or bind/replace a per-user Feishu identity. None of them
# make sense in a group profile (it owns no UAT), and rendering a member's
# PERSONAL credential hub into a shared group chat would leak/mutate their
# credentials, so all are hard-rejected in groups. ``auth`` (the credential
# hub) is included: its cards now carry group-clickable re-auth buttons, so it
# must never be openable in a group.
_GROUP_BLOCKED_COMMANDS: frozenset[str] = frozenset(
    {"auth", "feishu_auth", "feishu-auth", "feishu_logout", "feishu-logout", "feishu_reauth", "feishu-reauth"}
)
# Zero-width / bidi chars Feishu clients occasionally inject; stripped before
# the command-name membership check so they can't smuggle a blocked command
# past an exact-match gate.
_INVISIBLE_CHARS = str.maketrans(
    "", "", "​‌‍‎‏﻿"
)


_LEADING_MENTIONED_PREFIX_RE = re.compile(
    r"^\s*\[Mentioned:[^\]]*\]\s*",
)
_LEADING_AT_MENTION_RE = re.compile(
    r"^(?:\s*@\S+\s+)+",
)


def _strip_leading_at_mentions(text: str) -> str:
    """Strip leading Feishu @-mentions so slash commands are still parseable.

    The hermes-agent Feishu adapter normalises group messages into a form
    like ``[Mentioned: @all]\\n\\n@all /feishu_auth``. ``parse_command``
    requires the slash to be the first character, so for group messages
    we peel both the ``[Mentioned: …]`` prefix and the @-token tail off
    the front and hand the result to the parser.
    """
    if not text:
        return text
    text = _LEADING_MENTIONED_PREFIX_RE.sub("", text)
    text = _LEADING_AT_MENTION_RE.sub("", text)
    return text


def _short_chat_id(chat_id: str) -> str:
    """Return a deterministic short tag for a chat_id, safe in profile names.

    The 12-char prefix is cosmetic only — Feishu ``oc_`` ids are not
    uniform in their leading bytes, so similar-vintage ids share prefixes.
    Uniqueness rests entirely on a 16-hex (64-bit) sha1 suffix: collision
    probability stays below ~N²/2^65, negligible even for millions of
    chats. (The previous 4-hex/16-bit suffix collided at ~1/65536 per
    shared prefix, which would silently route two groups to one profile —
    the exact isolation this feature promises.)
    """
    raw = re.sub(r"[^A-Za-z0-9]+", "", str(chat_id or ""))
    if raw.lower().startswith("oc"):
        raw = raw[2:]
    prefix = raw[:12] or "noid"
    suffix = hashlib.sha1(str(chat_id).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{suffix}"


def _sanitize_soul_field(text: Any, *, max_len: int = 80) -> str:
    """Make an untrusted Feishu value safe to embed inside a SOUL.md system-prompt
    line. Feishu display names / group names are attacker-controllable; embedded
    raw they could carry newlines + backticks to break out of the markdown code
    span and inject instructions into the group agent's persona (stored prompt
    injection). Collapse to a single line, drop backticks/backslashes/control
    chars, and length-cap. MED-1, audit 2026-07-03."""
    s = str(text or "").replace("`", "").replace("\\", "")
    s = "".join(ch for ch in s if ch >= " " and ch != "\x7f")  # drop \n\r\t + control/DEL
    s = " ".join(s.split())  # collapse remaining whitespace runs
    return s[:max_len]


def register_chat_inviter(
    chat_id: str,
    inviter_open_id: str,
    *,
    chat_name: Optional[str] = None,
    inviter_display: Optional[str] = None,
    inviter_union_id: Optional[str] = None,
) -> None:
    """Layer 4 hook entry — persist who pulled the bot into a chat.

    The durable owner record is the routing row's ``owner_open_id`` column
    written immediately from the trusted bot-added event. The cache/pending
    records are retained as compatibility hand-offs for older first-use
    provisioning paths and welcome-card owner checks.
    """
    if not chat_id or not _is_feishu_open_id(inviter_open_id):
        return
    normalized_inviter_union_id = (
        str(inviter_union_id).strip() if inviter_union_id else None
    )
    now = time.time()
    with _chat_inviter_cache_lock:
        _chat_inviter_cache[chat_id] = {
            "inviter_open_id": inviter_open_id,
            "inviter_union_id": normalized_inviter_union_id,
            "chat_name": chat_name,
            "inviter_display": inviter_display,
            "_ts": now,
        }
        _chat_inviter_cache.move_to_end(chat_id)
        # Drop expired entries, then trim oldest if still over the cap.
        for cid in [
            c
            for c, e in _chat_inviter_cache.items()
            if now - e.get("_ts", 0) > _CHAT_INVITER_CACHE_TTL_S
        ]:
            _chat_inviter_cache.pop(cid, None)
        while len(_chat_inviter_cache) > _CHAT_INVITER_CACHE_MAX:
            _chat_inviter_cache.popitem(last=False)
    table = _get_routing_table()
    if table is None:
        logger.debug(
            "multitenancy: routing table unavailable for bot_added chat_id=%s",
            chat_id,
        )
        return
    try:
        table.put_pending_inviter(
            chat_id,
            inviter_open_id,
            inviter_union_id=normalized_inviter_union_id,
        )
        table.prune_pending_inviters(int(now), _CHAT_INVITER_CACHE_TTL_S)
    except Exception as exc:
        logger.debug(
            "multitenancy: failed to persist pending inviter chat_id=%s: %s",
            chat_id,
            exc,
        )
    try:
        _persist_group_route_from_inviter(
            chat_id=chat_id,
            inviter_open_id=inviter_open_id,
            chat_name=chat_name,
            inviter_display=inviter_display,
            table=table,
        )
    except Exception as exc:
        logger.debug(
            "multitenancy: failed to persist group route from bot_added chat_id=%s: %s",
            chat_id,
            exc,
        )


def _pop_chat_inviter(chat_id: str) -> None:
    with _chat_inviter_cache_lock:
        _chat_inviter_cache.pop(chat_id, None)


def _has_cached_chat_inviter(chat_id: str) -> bool:
    return _resolve_group_inviter_from_cache(chat_id) is not None


async def _fetch_chat_name(chat_id: str, gateway: Any) -> Optional[str]:
    """Pull chat_name from the live Feishu adapter via its async API."""
    adapter = _get_feishu_adapter(gateway)
    getter = getattr(adapter, "get_chat_info", None) if adapter else None
    if getter is None:
        return None
    try:
        info = await getter(chat_id)
    except Exception as exc:
        logger.debug("multitenancy: get_chat_info(%s) failed: %s", chat_id, exc)
        return None
    if isinstance(info, dict):
        for key in ("name", "chat_name", "title"):
            value = info.get(key)
            if value:
                return str(value)
    return None


async def _fetch_user_display(open_id: str, gateway: Any) -> Optional[str]:
    """Look up a Feishu user's display name through any adapter method
    that the running Feishu adapter exposes. Tested method names are kept
    intentionally small — these match the names the adapter and its sync
    helpers currently expose; new names would need a follow-up edit here."""
    adapter = _get_feishu_adapter(gateway)
    for attr in ("get_user_name", "get_user_display", "get_user_info"):
        fn = getattr(adapter, attr, None)
        if fn is None:
            continue
        try:
            result = await fn(open_id)
        except Exception as exc:
            logger.debug("multitenancy: %s(%s) failed: %s", attr, open_id, exc)
            continue
        if isinstance(result, str) and result:
            return result
        if isinstance(result, dict):
            for key in ("name", "display_name", "nickname"):
                value = result.get(key)
                if value:
                    return str(value)
    return None



def _ensure_auto_profile(
    profile_name: str,
    profile_home: Path,
    *,
    route_key: str,
    sender: str,
) -> None:
    """Create the on-disk profile skeleton required by AIAgent."""
    profile_home.mkdir(parents=True, exist_ok=True)
    shared_home = _shared_home_for_profile(profile_home)

    config_path = profile_home / "config.yaml"
    if config_path.exists():
        _normalize_profile_config_file(config_path, shared_home=shared_home)
    else:
        config_path.write_text(
            _dump_profile_config(_profile_config_from_shared_home(shared_home)),
            encoding="utf-8",
        )

    soul_path = profile_home / "SOUL.md"
    if not soul_path.exists():
        soul_path.write_text(
            "\n".join(
                [
                    f"# Hermes Profile {profile_name}",
                    "",
                    f"You are the dedicated Hermes tenant profile for Feishu route `{_sanitize_soul_field(route_key)}`.",
                    f"The current Feishu sender open_id is `{_sanitize_soul_field(sender)}`.",
                    "Keep tools, files, memory, and responses isolated to this profile.",
                    "Do not claim to be another Hermes profile.",
                    "",
                    _LARK_CLI_SOUL_GUIDANCE,
                    "",
                ]
            ),
            encoding="utf-8",
        )
    else:
        _ensure_soul_guidance(soul_path, _LARK_CLI_SOUL_GUIDANCE)

    _ensure_shared_profile_file(profile_home, shared_home, "auth.json")
    ensure_profile_local_env(profile_home, shared_home)
    _sync_default_skills_for_profile(profile_home, shared_home)


def _sync_default_skills_for_profile(
    profile_home: Path,
    shared_home: Path,
    *,
    include_default_skills: bool = False,
    upstream_profile_home: Path | None = None,
) -> None:
    # Use a relative import so the call works both when the package is loaded
    # as ``hermes_multitenancy`` (pytest/direct install) and when the Hermes
    # plugin loader exposes it as ``hermes_plugins.multitenancy.hermes_multitenancy``.
    from ..sync.feishu_org import Employee, _sync_default_profile_skills

    employee = None
    if include_default_skills:
        employee = Employee(
            open_id="",
            user_id=profile_home.name,
            agent_id=profile_home.name,
            profile_name=profile_home.name,
            name=profile_home.name,
            dept_id="",
            dept_name="",
            leader_user_id=None,
        )
    _sync_default_profile_skills(
        profile_home,
        shared_home,
        employee=employee,
        upstream_profile_home=upstream_profile_home,
    )


def _upstream_profile_home_for_owner(owner_open_id: str, shared_home: Path) -> Path | None:
    table = _get_routing_table()
    if table is None or not owner_open_id:
        return None
    try:
        owner = table.resolve_owner_root(owner_open_id) or table.lookup_by_open_id(owner_open_id)
    except Exception as exc:
        logger.debug(
            "multitenancy: owner profile lookup failed owner=%s: %s",
            owner_open_id,
            exc,
        )
        return None
    if owner is None or not owner.profile_name:
        return None
    candidate = shared_home / "profiles" / owner.profile_name
    return candidate if candidate.exists() else None


def _shared_home_for_profile(profile_home: Path) -> Path:
    """Infer the shared Hermes root from a profile path."""
    if profile_home.parent.name == "profiles":
        return profile_home.parent.parent
    return Path.home() / ".hermes"


def _profile_config_from_shared_home(shared_home: Path) -> dict[str, Any]:
    """Build a minimal profile config from the shared Hermes config."""
    config: dict[str, Any] = {}
    shared_config = shared_home / "config.yaml"
    if shared_config.exists():
        try:
            import yaml

            loaded = yaml.safe_load(shared_config.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                for key in ("model", "fallback", "platform_toolsets"):
                    value = loaded.get(key)
                    if value:
                        config[key] = value
                image_gen = loaded.get("image_gen")
                if isinstance(image_gen, dict) and image_gen:
                    config["image_gen"] = dict(image_gen)
                feishu_platform = ((loaded.get("platforms") or {}).get("feishu") or None)
                if feishu_platform:
                    config["platforms"] = {"feishu": feishu_platform}
        except Exception as exc:
            logger.debug("multitenancy: failed to read shared config %s: %s", shared_config, exc)

    _apply_lark_cli_profile_defaults(config)
    return _normalize_profile_config(config)


def _normalize_profile_config(config: dict[str, Any]) -> dict[str, Any]:
    _apply_default_image_gen_config(config)
    model = config.get("model")
    if isinstance(model, dict) and model.get("default"):
        default_model = str(model.get("default") or "").strip()
        provider = str(model.get("provider") or "").strip()
        if default_model and provider and "/" not in default_model:
            model["default"] = f"{provider}/{default_model}"
    return config


def _has_explicit_image_gen_config(config: dict[str, Any]) -> bool:
    section = config.get("image_gen")
    if not isinstance(section, dict):
        return False
    provider = str(section.get("provider") or "").strip()
    model = str(section.get("model") or "").strip()
    return bool(provider or model)


def _needs_default_image_gen_config(config: dict[str, Any]) -> bool:
    section = config.get("image_gen")
    if section is None:
        return True
    if isinstance(section, dict):
        return not _has_explicit_image_gen_config(config)
    return False


def _apply_default_image_gen_config(config: dict[str, Any]) -> bool:
    if not _needs_default_image_gen_config(config):
        return False
    config["image_gen"] = {
        "provider": _DEFAULT_IMAGE_GEN_PROVIDER,
        "model": _DEFAULT_IMAGE_GEN_MODEL,
    }
    return True


def _profile_config_file_needs_default_image_gen(config_path: Path) -> bool:
    try:
        import yaml

        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.debug("multitenancy: failed to inspect profile image_gen config %s: %s", config_path, exc)
        return False
    return isinstance(loaded, dict) and _needs_default_image_gen_config(loaded)


def _normalize_profile_config_file(config_path: Path, *, shared_home: Path) -> None:
    try:
        import yaml

        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.debug("multitenancy: failed to normalize profile config %s: %s", config_path, exc)
        return
    if not isinstance(loaded, dict):
        return
    before = json.dumps(loaded, sort_keys=True, ensure_ascii=True)
    _merge_shared_feishu_platform(loaded, shared_home)
    _apply_lark_cli_profile_defaults(loaded)
    normalized = _normalize_profile_config(loaded)
    after = json.dumps(normalized, sort_keys=True, ensure_ascii=True)
    if after != before:
        config_path.write_text(_dump_profile_config(normalized), encoding="utf-8")


def _apply_lark_cli_profile_defaults(config: dict[str, Any]) -> None:
    """Keep generated Feishu profiles thin: identity/display in Hermes, OAPI in lark-cli."""
    platform_toolsets = config.setdefault("platform_toolsets", {})
    if isinstance(platform_toolsets, dict):
        for platform_key in ("feishu", "api_server", "webui"):
            platform_toolsets[platform_key] = list(_LARK_CLI_PROFILE_TOOLSETS)
    multitenancy = config.setdefault("multitenancy", {})
    if isinstance(multitenancy, dict):
        multitenancy.setdefault("toolsets_mode", "explicit")
        platform_modes = multitenancy.setdefault("platform_toolsets_mode", {})
        if isinstance(platform_modes, dict):
            # feishu must merge (not replace) the platform default toolset.
            # `explicit` returned only ["lark-cli"] and silently dropped the
            # `skills` toolset, so lark-* skill bodies symlinked into the
            # profile never surfaced in the agent manifest (user report:
            # 技能里看不到 lark skills). Keep aligned with api_server/webui.
            for platform_key in ("feishu", "api_server", "webui"):
                explicit_toolsets = _normalize_string_list(platform_toolsets.get(platform_key))
                current_mode = str(platform_modes.get(platform_key) or "").strip().lower()
                if explicit_toolsets == _LARK_CLI_PROFILE_TOOLSETS and current_mode in {
                    "",
                    "explicit",
                    "strict",
                    "replace",
                }:
                    platform_modes[platform_key] = "merge_default"
                else:
                    platform_modes.setdefault(platform_key, "merge_default")


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _ensure_soul_guidance(soul_path: Path, guidance: str) -> None:
    try:
        text = soul_path.read_text(encoding="utf-8")
    except OSError:
        return
    guidance_lines = guidance.splitlines()
    missing_lines = [line for line in guidance_lines if line and line not in text]
    if not missing_lines:
        return
    if guidance_lines and guidance_lines[0] in text:
        suffix = "" if text.endswith("\n") else "\n"
        soul_path.write_text(f"{text}{suffix}" + "\n".join(missing_lines) + "\n", encoding="utf-8")
        return
    suffix = "" if text.endswith("\n") else "\n"
    soul_path.write_text(f"{text}{suffix}\n{guidance}\n", encoding="utf-8")


def _merge_shared_feishu_platform(config: dict[str, Any], shared_home: Path) -> None:
    shared_config = shared_home / "config.yaml"
    if not shared_config.exists():
        return
    try:
        import yaml

        loaded = yaml.safe_load(shared_config.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.debug("multitenancy: failed to read shared Feishu platform config %s: %s", shared_config, exc)
        return
    if not isinstance(loaded, dict):
        return
    feishu_platform = ((loaded.get("platforms") or {}).get("feishu") or None)
    if not feishu_platform:
        return
    platforms = config.setdefault("platforms", {})
    if isinstance(platforms, dict):
        platforms["feishu"] = feishu_platform


def _dump_profile_config(config: dict[str, Any]) -> str:
    try:
        import yaml

        return yaml.safe_dump(config, sort_keys=False, allow_unicode=False)
    except Exception:
        return json.dumps(config, indent=2, ensure_ascii=True) + "\n"


def _ensure_shared_profile_file(profile_home: Path, shared_home: Path, name: str) -> None:
    source = shared_home / name
    target = profile_home / name
    if target.exists() or target.is_symlink() or not source.exists():
        return
    try:
        target.symlink_to(source)
    except OSError:
        shutil.copy2(source, target)


def ensure_profile_local_env(profile_home: Path, shared_home: Path) -> bool:
    """Ensure a tenant profile has a local .env, not a symlink to shared secrets.

    User-scoped UAT and provider credentials are brokered separately; a tenant
    ``.env`` exists for compatibility only and must not point at the service
    root's secret-bearing ``.env``.
    """
    target = profile_home / ".env"
    shared_env = shared_home / ".env"
    if target.is_symlink() and not _symlink_points_to(target, shared_env):
        return False
    if target.exists() and not target.is_symlink():
        _chmod_private_file(target)
        return False
    _write_empty_profile_env(target)
    return True


def repair_profile_local_envs(
    *,
    shared_home: Path | None = None,
    profiles_root: Path | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Backfill profile .env symlinks that point at the shared Hermes .env."""
    root = (shared_home or Path(os.environ.get("HERMES_HOME", "~/.hermes"))).expanduser()
    profiles = profiles_root or (root / "profiles")
    stats = {
        "scanned": 0,
        "repaired": 0,
        "created": 0,
        "planned_repaired": 0,
        "planned_created": 0,
        "kept": 0,
        "skipped_service": 0,
        "skipped_non_shared_symlink": 0,
        "errors": 0,
    }
    if not profiles.exists():
        return stats
    for profile_home in sorted(path for path in profiles.iterdir() if path.is_dir()):
        stats["scanned"] += 1
        if _is_service_profile(profile_home.name):
            stats["skipped_service"] += 1
            continue
        target = profile_home / ".env"
        if target.is_symlink() and not _symlink_points_to(target, root / ".env"):
            stats["skipped_non_shared_symlink"] += 1
            continue
        if target.exists() and not target.is_symlink():
            stats["kept"] += 1
            continue
        was_missing = not target.exists() and not target.is_symlink()
        if dry_run:
            if was_missing:
                stats["planned_created"] += 1
            else:
                stats["planned_repaired"] += 1
            continue
        try:
            ensure_profile_local_env(profile_home, root)
        except OSError:
            logger.exception("multitenancy: failed to repair profile .env profile=%s", profile_home.name)
            stats["errors"] += 1
            continue
        if was_missing:
            stats["created"] += 1
        else:
            stats["repaired"] += 1
    return stats


def repair_profile_image_gen_defaults(
    *,
    shared_home: Path | None = None,
    profiles_root: Path | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Backfill default image_gen config into ordinary profiles that lack it."""
    root = (shared_home or Path(os.environ.get("HERMES_HOME", "~/.hermes"))).expanduser()
    profiles = profiles_root or (root / "profiles")
    stats = {
        "scanned": 0,
        "updated": 0,
        "planned_updated": 0,
        "kept_explicit": 0,
        "skipped_service": 0,
        "skipped_invalid": 0,
        "errors": 0,
    }
    if not profiles.exists():
        return stats
    for profile_home in sorted(path for path in profiles.iterdir() if path.is_dir()):
        stats["scanned"] += 1
        if _is_service_profile(profile_home.name):
            stats["skipped_service"] += 1
            continue
        config_path = profile_home / "config.yaml"
        loaded: dict[str, Any] | None = None
        try:
            if config_path.exists():
                import yaml

                loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                if not isinstance(loaded, dict):
                    stats["skipped_invalid"] += 1
                    continue
                if _has_explicit_image_gen_config(loaded):
                    stats["kept_explicit"] += 1
                    continue
                if not _needs_default_image_gen_config(loaded):
                    stats["kept_explicit"] += 1
                    continue
            if dry_run:
                stats["planned_updated"] += 1
                continue
            if config_path.exists():
                if loaded is None or not _apply_default_image_gen_config(loaded):
                    stats["kept_explicit"] += 1
                    continue
                config_path.write_text(_dump_profile_config(loaded), encoding="utf-8")
            else:
                config_path.write_text(
                    _dump_profile_config(_profile_config_from_shared_home(root)),
                    encoding="utf-8",
                )
            stats["updated"] += 1
        except Exception:
            logger.exception("multitenancy: failed to repair profile image_gen config profile=%s", profile_home.name)
            stats["errors"] += 1
    return stats


def _is_service_profile(profile_name: str) -> bool:
    return profile_name == os.environ.get("HERMES_MULTITENANCY_ROUTER_PROFILE", "multitenancy_router")


def _symlink_points_to(path: Path, expected: Path) -> bool:
    try:
        return path.resolve(strict=False) == expected.resolve(strict=False)
    except OSError:
        return False


def _write_empty_profile_env(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    tmp.write_text("", encoding="utf-8")
    _chmod_private_file(tmp)
    tmp.replace(target)


def _chmod_private_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        logger.debug("multitenancy: failed to chmod private file %s", path, exc_info=True)


def _profile_name_to_home(profile_name: str) -> Path:
    """Map profile_name to its on-disk profile home directory.

    Mirrors ``hermes_cli/profiles.py`` convention: ``~/.hermes/profiles/<name>``.
    """
    return Path.home() / ".hermes" / "profiles" / profile_name


def _touch_route(sender: str, sender_alt: Optional[str] = None) -> None:
    """Best-effort last_active_at update; no-op if no SQLite table or row.

    Touch both identifiers because auto-provisioned rows are keyed by
    app-scoped ``sender`` while older synced rows may still be keyed by
    ``sender_alt`` / union_id.
    """
    table = _get_routing_table()
    if table is None:
        return
    keys = [sender]
    if sender_alt and sender_alt != sender:
        keys.append(sender_alt)
    for key in keys:
        try:
            table.touch_active(key)
        except Exception as exc:
            logger.debug("multitenancy: touch_active failed: %s", exc)


# -- Lazy singletons (RoutingTable + RuntimePool) ----------------------------


_routing_table: Any = None
_routing_db_path: Optional[str] = None
_pool: Any = None
_session_store: Any = None
_session_db_path: Optional[str] = None  # None → DEFAULT_DB_PATH inside SessionStore


def _get_session_store():
    """Lazy-init module-level SessionStore. Returns None if init fails (in-memory only)."""
    global _session_store
    if _session_store is None:
        try:
            from ..sessions import SessionStore
            _session_store = SessionStore(_session_db_path)
        except Exception as exc:
            logger.debug("multitenancy: SessionStore init failed (%s)", exc)
            return None
    return _session_store


def override_session_store(store_or_path) -> None:
    """Test helper — inject a SessionStore (or db path string, or None to disable)."""
    global _session_store, _session_db_path, _session_loaded
    if _session_store is not None and _session_store is not store_or_path:
        try:
            _session_store.close()
        except Exception:
            pass
    _session_loaded.clear()
    if store_or_path is None or isinstance(store_or_path, (str, Path)):
        _session_store = None
        _session_db_path = str(store_or_path) if store_or_path is not None else None
    else:
        _session_store = store_or_path
        _session_db_path = None


def _get_routing_table():
    """Lazy-init module-level RoutingTable. Returns None if init fails."""
    global _routing_table
    if _routing_table is None:
        try:
            from ..routing import RoutingTable
            _routing_table = RoutingTable(_routing_db_path)
        except Exception as exc:
            logger.debug("multitenancy: RoutingTable init failed (%s)", exc)
            return None
    return _routing_table


def _get_pool():
    """Lazy-init module-level RuntimePool."""
    global _pool
    if _pool is None:
        from ..pool import RuntimePool
        _pool = RuntimePool()
    return _pool


def override_routing_table(db_path: Optional[str | Path]) -> None:
    """Test helper — reset the routing-table singleton, optionally pointing it at a different db."""
    global _routing_table, _routing_db_path
    if _routing_table is not None:
        try:
            _routing_table.close()
        except Exception:
            pass
    _routing_table = None
    _routing_db_path = str(db_path) if db_path is not None else None


def override_pool(pool) -> None:
    """Test helper — inject a custom RuntimePool (or None to reset)."""
    global _pool
    _pool = pool


# -- Adapter resolution ------------------------------------------------------


def _get_feishu_adapter(gateway: Any) -> Any:
    """Pull the FeishuAdapter from the gateway, returning None if unavailable."""
    if gateway is None:
        return None
    adapters = getattr(gateway, "adapters", None)
    if adapters is None:
        return None
    try:
        from gateway.platforms.base import Platform  # type: ignore
        result = adapters.get(Platform.FEISHU)
        if result is not None:
            return result
    except Exception as exc:  # pragma: no cover — only triggers when import fails
        logger.debug("multitenancy: Platform import unavailable (%s)", exc)
    if isinstance(adapters, dict):
        return adapters.get("feishu")
    return None


async def _safe_call(fn, *args, **kwargs):
    """Await fn(*args, **kwargs) whether it is sync or async."""
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return await result
    return result


# Detect Feishu rate-limit errors. Strings vary across SDK versions, so we
# match on broad hints rather than exact codes.
_RATE_LIMIT_HINTS = ("429", "rate limit", "too many requests", "ratelimit")


def _is_rate_limit_error(exc: BaseException) -> bool:
    msg = (str(exc) or "").lower()
    return any(h in msg for h in _RATE_LIMIT_HINTS)


async def _edit_with_retry(adapter, chat_id, message_id, content, *, finalize=False):
    """Wrap adapter.edit_message with exponential backoff on 429.

    On 429: backoff 0.5 → 1.0 → 2.0s, max 3 retries (4 total attempts), then
    log a warning and return None so the caller can continue streaming.
    On non-429: 1 retry after 0.2s, then propagate.
    """
    backoffs = (0.5, 1.0, 2.0)
    for attempt in range(4):
        try:
            if finalize:
                try:
                    return await adapter.edit_message(chat_id, message_id, content, finalize=True)
                except TypeError:
                    return await adapter.edit_message(chat_id, message_id, content)
            return await adapter.edit_message(chat_id, message_id, content)
        except Exception as exc:
            if _is_rate_limit_error(exc):
                if attempt < len(backoffs):
                    logger.debug(
                        "multitenancy: edit_message 429, backoff %ss (attempt %d)",
                        backoffs[attempt], attempt + 1,
                    )
                    await asyncio.sleep(backoffs[attempt])
                    continue
                logger.warning("multitenancy: edit_message rate-limited 4x, giving up")
                return None
            if attempt == 0:
                logger.debug("multitenancy: edit_message non-429 retry: %s", exc)
                await asyncio.sleep(0.2)
                continue
            raise
    return None






















# Streaming-card flush throttles match openclaw-lark:
# CARDKIT_MS=100 for cardElement.content and PATCH_MS=1500 for legacy edits.
# The character thresholds are Hermes' local stream-consumer coalescing floor;
# time-based throttle is the cross-project contract.
_STREAM_CONTENT_MIN_CHARS = 120
_STREAM_CONTENT_MIN_SECONDS = 1.5
# issue #4: was 30 — content was pushed to the card in 30-char jumps, which the
# CardKit client typewriter (print_step=1, print_strategy=delay) can't smooth.
# openclaw-lark does NOT char-batch; it pushes accumulated content per 100ms
# window and lets the client animate char-by-char. Lower this near 1 so we feed
# the typewriter granularly. Env-tunable for real-machine tuning.
try:
    _STREAM_CARDKIT_CONTENT_MIN_CHARS = max(1, int(os.getenv("HERMES_CARD_CONTENT_MIN_CHARS") or "1"))
except ValueError:
    _STREAM_CARDKIT_CONTENT_MIN_CHARS = 1
_STREAM_CARDKIT_CONTENT_MIN_SECONDS = 0.1
_STREAM_THINKING_MIN_SECONDS = 2.0
_STREAM_CARD_REASONING_MIN_CHARS = 100
_STREAM_CARD_REASONING_MIN_SECONDS = 2.0
_STREAM_CARD_PRIME_STATUS = ""
_STREAM_INVISIBLE_PLACEHOLDER = "\u200b"
_STREAM_CARD_IDLE_HEARTBEAT_SECONDS = 2.5
_STREAM_STATUS_ANIMATION_MARKERS = ("\u200b", "\u200c", "\u200d", "\ufeff")
_STREAM_ABORT_FALLBACK = "Aborted."
# Soft per-card segment target for the legacy CardKit compat path. This is not
# a Feishu/CardKit platform limit; shared GatewayStreamConsumer uses its own
# adapter-specific splitting and must receive the complete stream.
_STREAM_MAX_VISIBLE_CHARS = 3_000














def _log_task_failure(task: asyncio.Task) -> None:
    """Done-callback for fire-and-forget tasks — surfaces silent exceptions."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("multitenancy: background task crashed: %r", exc)


# -- Streaming subsystem (split into .streaming) — re-export for API stability --
from .streaming import (  # noqa: E402,F401
    _stream_into_feishu,
    _stream_into_feishu_shared_consumer,
    _start_feishu_stream_target,
    _abort_feishu_stream_target,
    _update_feishu_stream_target,
    _update_feishu_stream_tool_event,
    _update_feishu_stream_reasoning,
    _update_feishu_stream_status,
    _stream_card_idle_status,
    _run_terminal_stream_update,
    _is_aiagent_stream_idle_timeout,
    _aiagent_stream_timeout_notice,
    _clean_stream_display_text,
    _clean_stream_delta_text,
    _strip_stream_status_animation_markers,
    _merge_stream_footer_metrics,
    _resolve_stream_footer_model_name,
    _adapter_supports_streaming_card,
    _start_hub_flow_poll,
    _poll_hub_flows,
)


# -- Diagnostics subsystem (split into .diagnostics) — re-export for API stability --
from .diagnostics import (  # noqa: E402,F401
    _build_diagnostics_card,
    _diagnostics_subject_context,
    _render_diagnostics_in_profile_scope,
    _render_diagnostics_reply,
    _send_diagnostics_card,
)


# -- Media/artifact subsystem (split into .media) — re-export for API stability --
from .media import (  # noqa: E402,F401
    _materialize_response_artifacts,
    _append_remote_image_media_directives,
    _append_remote_image_media_directives_async,
    _materialize_remote_image_url,
    _is_public_remote_image_url,
    _is_global_ip_address,
    _remote_image_response_peer_ip,
    _remote_image_extension_from_url,
    _remote_image_extension_from_content_type,
    _header_value,
    _existing_profile_source_for_artifact_spec,
    _write_response_artifact,
    _artifact_rows,
    _write_artifact_xlsx,
    _write_artifact_docx,
    _write_artifact_pdf,
    _write_artifact_image,
    _append_profile_file_media_directives,
    _strip_plain_profile_file_paths_for_display,
    _should_deliver_as_feishu_document,
    _append_media_denied_security_event,
    _profile_scoped_media_response,
    _webui_profile_scoped_media_response,
    _publish_mentioned_profile_file,
    _is_deliverable_profile_file,
    _remember_recent_profile_file,
    _should_append_recent_profile_file_context,
    _workspace_alias_for_profile_file,
    _recent_profile_files_from_history,
    _append_recent_profile_file_context,
    _resolve_profile_media_artifact,
    _publish_profile_media_artifact,
    _event_has_image_media,
    _image_vision_unavailable_response,
    _image_prep_unavailable_note,
    _matching_custom_provider_entry,
    _profile_main_runtime_for_image_prep,
    _install_auxiliary_main_runtime_patch,
    _install_vision_model_override,
    _install_vision_task_endpoint_override,
    _restore_auxiliary_main_runtime_patch,
    _profile_image_prep_runtime,
    _materialize_inbound_media_for_profile,
    _enrich_via_hermes_pipeline,
    _call_enrich_via_hermes_pipeline,
    _append_enrichment,
    _local_enrich_with_file_content,
    _extract_xlsx_text,
    _read_zip_member_limited,
    _extract_docx_text,
    _extract_pdf_text,
    _decode_pdf_stream,
    _extract_pdf_literal_strings,
    _local_enrich_with_vision_only,
    _image_analysis_unavailable_note,
)


# -- Command dispatch subsystem (split into .commands) — re-export for API stability --
from .commands import (  # noqa: E402,F401
    handle_async,
    _processing_outcome,
    _maybe_rewrite_skill_slash_command,
    _should_check_skill_slash_command,
    _handle_command,
    _handle_feishu_auth_command,
    _profile_open_id_for_auth,
    _start_feishu_auth_poll_task,
    _dispatch_synthetic_auth_complete,
    _poll_feishu_auth_session_until_done,
    _filter_hub_rows_for_auth,
    _handle_auth_command,
    _track_kep_login_proc,
    _handle_pending_approval_command,
    _resolve_pending_approval_requests,
    _record_pending_approval,
    _clear_pending_approval,
    _handle_child_approval_required,
    _dispatch_gateway_command,
    _dispatch_quick_command,
    _get_quick_command,
    _quick_exec_allowed,
    _truthy,
    _dispatch_plugin_command,
    _get_plugin_command_handler,
    _gateway_handler_for_command,
    _ensure_command_event_methods,
    _set_command_event_methods,
    _command_args_from_event,
    _command_args_from_text,
    _event_platform_value,
    _event_locale,
)


# -- Group/route auto-provisioning subsystem (split into .provisioning) — re-export for API stability --
from .provisioning import (  # noqa: E402,F401
    _resolve_or_auto_provision_route,
    _auto_provision_enabled,
    _auto_provision_route,
    _is_group_chat_type,
    _is_blocked_group_command,
    _make_group_profile_name,
    is_group_profile_name,
    _group_display_label,
    _persist_group_route_from_inviter,
    _resolve_group_inviter_from_cache,
    resolve_or_auto_provision_group_route,
    _provision_group_route,
    _ensure_group_profile,
    _ensure_webui_agent_profile,
    _write_group_profile_env,
    _disable_webui_agent_feishu_platform_file,
    _disable_webui_agent_feishu_platform,
)
