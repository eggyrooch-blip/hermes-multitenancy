"""Pre-gateway-dispatch hook callback (sync) + async dispatch entry point.

Wires together: SQLite RoutingTable (production) + in-memory _SPIKE_ROUTING
(fallback) + LRU RuntimePool (cached profile runtimes) + Hermes-derived slash
command dispatch.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import time
import zipfile
from contextlib import asynccontextmanager
from itertools import zip_longest
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

_SKILL_SLASH_ALIASES = {
    "hades": "kep-hades-cli",
}

try:  # Shared Hermes Feishu card/typewriter transport.
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig  # type: ignore
except Exception:  # pragma: no cover - allows plugin unit tests without gateway on path
    GatewayStreamConsumer = None  # type: ignore
    StreamConsumerConfig = None  # type: ignore

# Per-user in-flight dispatch tasks — used by /stop to cancel the right task.
_user_inflight_tasks: dict[str, asyncio.Task] = {}
_synthetic_session_guards: dict[str, Any] = {}

# Group-chat profile state. Populated by the bot_added hook (Layer 4, runs
# on the Lark SDK thread) and consumed by the auto-provision path (asyncio
# loop thread). Persistent state lives in the SQLite routing table; this
# only covers the window between bot_added and the first @mention.
#
# Bounded + TTL'd + lock-guarded: an attacker spamming bot add/remove across
# throwaway chats would otherwise grow this dict without bound, and the
# SDK-thread writer can race the loop-thread reader/popper. OrderedDict +
# lock keeps eviction and the register/pop pair consistent across threads.
import threading as _threading
from collections import OrderedDict as _OrderedDict

_CHAT_INVITER_CACHE_MAX = 512
_CHAT_INVITER_CACHE_TTL_S = 3600  # bot_added → first @; generous for restarts
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


def _history_key(profile_name: str, sender: str, sender_alt: Optional[str]) -> tuple[str, str]:
    """Return the per-(profile, user) key used to look up conversation history."""
    return (profile_name, _tenant_user_key(sender, sender_alt))


def _tenant_user_key(sender: str, sender_alt: Optional[str]) -> str:
    """Return the canonical per-user key for memory/session scoping."""
    sender_key = str(sender or "").strip()
    if sender_key and sender_key != "unknown":
        return sender_key
    alt_key = str(sender_alt or "").strip()
    return alt_key or sender_key or "unknown"


def _trim_history(history: list[dict]) -> list[dict]:
    """Keep at most ``_SESSION_HISTORY_MAX`` most recent messages."""
    if len(history) <= _SESSION_HISTORY_MAX:
        return history
    return history[-_SESSION_HISTORY_MAX:]


def _load_history(key: tuple[str, str]) -> list[dict]:
    """Get history for ``key`` — first call hydrates from SessionStore, subsequent calls hit cache."""
    if key in _session_loaded:
        return list(_session_history.get(key, []))
    store = _get_session_store()
    if store is not None:
        try:
            persisted = store.load_recent(key[0], key[1], _SESSION_HISTORY_MAX)
        except Exception as exc:
            logger.debug("multitenancy: SessionStore.load_recent failed (%s)", exc)
            persisted = []
        if persisted:
            _session_history[key] = persisted
    _session_loaded.add(key)
    return list(_session_history.get(key, []))


def _persist_turn(key: tuple[str, str], user_msg: dict, assistant_text: str) -> None:
    """Append a (user, assistant) turn to both in-memory cache and SessionStore."""
    new_history = _session_history.get(key, []) + [
        user_msg,
        {"role": "assistant", "content": assistant_text},
    ]
    _session_history[key] = _trim_history(new_history)
    store = _get_session_store()
    if store is None:
        return
    try:
        # _build_user_message always sets content as a str — no cast needed.
        store.append(key[0], key[1], user_msg["role"], user_msg["content"])
        store.append(key[0], key[1], "assistant", assistant_text)
    except Exception as exc:
        logger.debug("multitenancy: SessionStore.append failed (%s)", exc)


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


def _normalize_dedupe_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _run_request_dedupe_record(request: Any) -> Optional[tuple[str, Optional[str], Optional[str], int]]:
    message_id = str(getattr(request, "message_id", "") or "").strip()
    profile_name = str(getattr(request, "profile_name", "") or "").strip()
    user_key = str(getattr(request, "user_key", "") or "").strip()
    content = str(getattr(request, "content", "") or "")
    if message_id:
        ttl = _dedupe_env_int(
            "HERMES_MULTITENANCY_EVENT_DEDUPE_TTL_SECONDS",
            _DEDUPE_MESSAGE_TTL_SECONDS,
        )
        return (f"msg:{profile_name}:{user_key}:{message_id}", message_id, None, ttl)

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
    from .run_models import RunRequest

    user_key = _tenant_user_key(sender, sender_alt)
    return RunRequest(
        channel="feishu",
        profile_name=profile_name,
        user_key=user_key,
        content=text,
        chat_id=chat_id,
        message_id=_event_message_id(event),
        credential_subject=user_key,
        requires_host_tools=_host_tools_require_sandbox(),
        metadata={"sender_alt": sender_alt} if sender_alt else {},
    )


def _make_routed_run_broker(*, dispatch_agent: Any = None):
    from .run_broker import RunBroker

    return RunBroker(
        dispatch_agent=dispatch_agent or (lambda _request: ""),
        mark_seen=_mark_run_request_seen,
        sandbox_available=_router_sandbox_available,
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


def _is_feishu_open_id(value: Any) -> bool:
    return bool(value) and str(value).startswith("ou_")


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
    return str(candidate) if _is_feishu_open_id(candidate) else None


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
        if _is_feishu_open_id(candidate):
            return str(candidate)

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
            if _is_feishu_open_id(candidate):
                return str(candidate)

    return fallback


def _event_with_text(event: Any, text: str) -> Any:
    """Return an event-shaped object whose ``text`` matches the agent prompt."""
    if text == (getattr(event, "text", "") or ""):
        return event
    cloned = copy.copy(event)
    setattr(cloned, "text", text)
    return cloned


def _clean_stream_display_text(text: str, profile_home: Optional[Path] = None) -> str:
    """Hide native media-delivery directives from visible streaming text."""
    try:
        from gateway.stream_consumer import GatewayStreamConsumer  # type: ignore

        cleaned = GatewayStreamConsumer._clean_for_display(text)
    except Exception:
        cleaned = str(text or "").replace("[[audio_as_voice]]", "")
        cleaned = re.sub(r'''[`"']?MEDIA:\s*\S+[`"']?''', "", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = cleaned.rstrip()
    if profile_home is not None:
        cleaned = _strip_plain_profile_file_paths_for_display(cleaned, profile_home)
    return cleaned


_MEDIA_DIRECTIVE_RE = re.compile(r'''(?P<prefix>[`"']?MEDIA:\s*)(?P<path>\S+)(?P<suffix>[`"']?)''')
_PROFILE_FILE_PATH_RE = re.compile(
    r'''(?P<path>(?:/workspace|/[^`"'<>\n\r]+?)'''
    r'''\.(?:png|jpe?g|gif|webp|mp4|mov|avi|mkv|webm|ogg|opus|mp3|wav|m4a|'''
    r'''epub|pdf|zip|rar|7z|docx?|xlsx?|pptx?|txt|csv|json|md))'''
)
_AUTO_FILE_DELIVERY_MAX_BYTES = int(os.getenv("HERMES_MULTITENANCY_AUTO_FILE_DELIVERY_MAX_BYTES", "52428800"))
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


async def _deliver_media_from_stream_response(
    gateway: Any,
    response: str,
    event: Any,
    adapter: Any,
    profile_home: Path,
) -> None:
    """Delegate post-stream media attachment delivery to Hermes' native gateway path."""
    deliver = getattr(gateway, "_deliver_media_from_response", None)
    if not callable(deliver):
        return
    response_with_files = _append_profile_file_media_directives(response, profile_home)
    scoped_response = _profile_scoped_media_response(response_with_files, profile_home)
    if "MEDIA:" not in scoped_response:
        return
    await deliver(scoped_response, event, adapter)


def _append_profile_file_media_directives(response: str, profile_home: Path) -> str:
    """Attach profile-local files that the model mentioned as plain paths."""
    text = str(response or "")
    if not text:
        return text
    root = profile_home.expanduser().resolve(strict=False)
    media_paths = {match.group("path").strip() for match in _MEDIA_DIRECTIVE_RE.finditer(text)}
    additions: list[str] = []
    seen: set[Path] = set()
    for match in _PROFILE_FILE_PATH_RE.finditer(text):
        raw_path = match.group("path").strip().rstrip(".,;:)]}")
        if not raw_path or raw_path in media_paths:
            continue
        published = _publish_mentioned_profile_file(raw_path, root)
        if published is None or published in seen:
            continue
        seen.add(published)
        additions.append(f"MEDIA:{published}")
    if not additions:
        return text
    return f"{text.rstrip()}\n" + "\n".join(additions)


def _strip_plain_profile_file_paths_for_display(text: str, profile_home: Path) -> str:
    """Avoid exposing host absolute paths when the file will be attached."""
    raw = str(text or "")
    if not raw:
        return raw
    root = profile_home.expanduser().resolve(strict=False)

    def repl(match: re.Match[str]) -> str:
        raw_path = match.group("path").strip().rstrip(".,;:)]}")
        if not raw_path:
            return match.group(0)
        if _publish_mentioned_profile_file(raw_path, root) is not None:
            return "[文件已作为附件发送]"
        candidate = Path(raw_path).expanduser()
        if raw_path == "/workspace" or raw_path.startswith("/workspace/"):
            candidate = root / "workspace" / raw_path.removeprefix("/workspace").lstrip("/")
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=False)
        if resolved.exists() and (resolved == root or root in resolved.parents):
            return "[受保护文件路径已隐藏]"
        return match.group(0)

    return _PROFILE_FILE_PATH_RE.sub(repl, raw)


def _profile_scoped_media_response(response: str, profile_home: Path) -> str:
    """Drop MEDIA directives outside profile scope and publish artifacts to workspace."""
    root = profile_home.expanduser().resolve(strict=False)

    def repl(match: re.Match[str]) -> str:
        raw_path = match.group("path").strip()
        if raw_path == "/workspace" or raw_path.startswith("/workspace/"):
            workspace_relative = raw_path.removeprefix("/workspace").lstrip("/")
            candidate = root / "workspace" / workspace_relative
        else:
            candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=False)
        if resolved == root or root in resolved.parents:
            workspace_artifact = _publish_profile_media_artifact(resolved, root)
            deliver_path = workspace_artifact or resolved
            return f"{match.group('prefix')}{deliver_path}{match.group('suffix')}"
        profile_artifact = _resolve_profile_media_artifact(raw_path, root)
        if profile_artifact is not None:
            logger.info(
                "multitenancy: rewrote outbound MEDIA to profile workspace artifact path=%s resolved=%s",
                raw_path,
                profile_artifact,
            )
            return f"{match.group('prefix')}{profile_artifact}{match.group('suffix')}"
        logger.warning(
            "multitenancy: blocked outbound MEDIA outside profile home path=%s profile_home=%s",
            raw_path,
            root,
        )
        return ""

    return _MEDIA_DIRECTIVE_RE.sub(repl, str(response or ""))


def _publish_mentioned_profile_file(raw_path: str, profile_home: Path) -> Optional[Path]:
    if raw_path == "/workspace" or raw_path.startswith("/workspace/"):
        workspace_relative = raw_path.removeprefix("/workspace").lstrip("/")
        candidate = profile_home / "workspace" / workspace_relative
    else:
        candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = profile_home / candidate
    source = candidate.resolve(strict=False)
    if not _is_deliverable_profile_file(source, profile_home):
        return None
    workspace_root = (profile_home / "workspace").resolve(strict=False)
    if source == workspace_root or workspace_root in source.parents:
        return source
    target_dir = (workspace_root / "Downloads").resolve(strict=False)
    target = (target_dir / source.name).resolve(strict=False)
    if not (target == profile_home or profile_home in target.parents):
        return None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if source != target:
            shutil.copy2(source, target)
        logger.info(
            "multitenancy: auto-attached mentioned profile file source=%s target=%s",
            source,
            target,
        )
        return target
    except Exception as exc:
        logger.warning(
            "multitenancy: failed to auto-attach mentioned profile file source=%s target=%s error=%s",
            source,
            target,
            exc,
        )
        return None


def _is_deliverable_profile_file(source: Path, profile_home: Path) -> bool:
    root = profile_home.resolve(strict=False)
    if not (source.exists() and source.is_file() and root in source.parents):
        return False
    try:
        if source.stat().st_size > _AUTO_FILE_DELIVERY_MAX_BYTES:
            logger.warning(
                "multitenancy: skipped auto file delivery for oversized file path=%s size=%s",
                source,
                source.stat().st_size,
            )
            return False
    except OSError:
        return False
    relative_parts = source.relative_to(root).parts
    lowered = [part.lower() for part in relative_parts]
    if any(part in _SENSITIVE_PROFILE_DIR_NAMES for part in lowered[:-1]):
        logger.warning("multitenancy: blocked auto file delivery for sensitive directory path=%s", source)
        return False
    name = lowered[-1] if lowered else ""
    if name in _SENSITIVE_PROFILE_FILE_NAMES:
        logger.warning("multitenancy: blocked auto file delivery for sensitive file path=%s", source)
        return False
    return True


def _resolve_profile_media_artifact(raw_path: str, profile_home: Path) -> Optional[Path]:
    """Map tool-reported temp media paths to same-name artifacts in the workspace."""
    name = Path(raw_path).name
    if not name:
        return None
    search_dirs = (
        profile_home / "home" / "Downloads",
        profile_home / "cache" / "images",
        profile_home / "tmp",
        profile_home / "data",
    )
    for directory in search_dirs:
        candidate = (directory / name).resolve(strict=False)
        if candidate.exists() and candidate.is_file() and profile_home in candidate.parents:
            return _publish_profile_media_artifact(candidate, profile_home)
    return None


def _publish_profile_media_artifact(source: Path, profile_home: Path) -> Optional[Path]:
    """Copy a profile-local generated artifact into the WebUI-visible workspace."""
    source = source.resolve(strict=False)
    root = profile_home.resolve(strict=False)
    if not (source.exists() and source.is_file() and root in source.parents):
        return None
    workspace_root = (root / "workspace").resolve(strict=False)
    if source == workspace_root or workspace_root in source.parents:
        return source
    artifact_dirs = (
        root / "home" / "Downloads",
        root / "cache" / "images",
        root / "tmp",
        root / "data",
    )
    source_in_artifacts = any(
        source == directory.resolve(strict=False)
        or directory.resolve(strict=False) in source.parents
        for directory in artifact_dirs
    )
    if not source_in_artifacts:
        return None
    target_dir = (workspace_root / "Downloads").resolve(strict=False)
    target = (target_dir / source.name).resolve(strict=False)
    if not (target == root or root in target.parents):
        return None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if source != target:
            shutil.copy2(source, target)
        return target
    except Exception as exc:
        logger.warning(
            "multitenancy: failed to publish media artifact to workspace source=%s target=%s error=%s",
            source,
            target,
            exc,
        )
        return None


_IMAGE_FILE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}


def _materialize_inbound_media_for_profile(event: Any, profile_home: Path) -> None:
    """Copy gateway-cached inbound media into the routed profile boundary."""
    media_urls = getattr(event, "media_urls", None) or []
    if not media_urls:
        return
    media_types = getattr(event, "media_types", None) or []
    root = profile_home.resolve(strict=False)
    rewritten: list[str] = []
    replacements: dict[str, str] = {}
    changed = False

    for raw_path, raw_mtype in zip_longest(media_urls, media_types, fillvalue=""):
        raw = str(raw_path or "")
        if not raw or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
            rewritten.append(raw)
            continue
        source = Path(raw).expanduser().resolve(strict=False)
        if not (source.exists() and source.is_file()):
            rewritten.append(raw)
            continue
        if source == root or root in source.parents:
            rewritten.append(str(source))
            continue

        suffix = source.suffix.lower()
        media_type = str(raw_mtype or "").lower()
        target_dir = root / "cache" / "images" if (
            media_type.startswith("image") or suffix in _IMAGE_FILE_EXTENSIONS
        ) else root / "uploads"
        target = (target_dir / source.name).resolve(strict=False)
        if target.exists() and target.resolve(strict=False) != source:
            digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:10]
            target = (target_dir / f"{source.stem}-{digest}{source.suffix}").resolve(strict=False)
        if not (target == root or root in target.parents):
            rewritten.append(raw)
            continue
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if target.resolve(strict=False) != source:
                shutil.copy2(source, target)
            rewritten_path = str(target)
            rewritten.append(rewritten_path)
            replacements[raw] = rewritten_path
            changed = True
            logger.info(
                "multitenancy: materialized inbound media for profile source=%s target=%s",
                source,
                target,
            )
        except Exception as exc:
            logger.warning(
                "multitenancy: failed to materialize inbound media source=%s profile_home=%s error=%s",
                source,
                root,
                exc,
            )
            rewritten.append(raw)

    if changed:
        setattr(event, "media_urls", rewritten)
        text = getattr(event, "text", None)
        if isinstance(text, str):
            for old, new in replacements.items():
                text = text.replace(old, new)
            setattr(event, "text", text)


async def _enrich_via_hermes_pipeline(event: Any, gateway: Any) -> Optional[str]:
    """Delegate inbound preprocessing to hermes' ``_prepare_inbound_message_text``.

    This is the single call that mainstream uses to:
      - run vision_analyze_tool on attached images
      - run transcribe_audio on voice messages
      - inject text-file content (.txt / .md / .csv / etc.)
      - prepend reply-quoted context
      - attribute multi-user shared sessions

    By calling the same gateway method, the plugin behaves *identically* to
    mainstream for every multimodal input — no re-implementation, no drift.

    Caveat: this depends on a private GatewayRunner method. If hermes-agent
    refactors ``_prepare_inbound_message_text``, swap to local fallbacks
    (``_local_enrich_with_vision_only`` below as a minimal safety net).

    Returns:
        Enriched text string, or None on failure (caller falls back to event.text).
    """
    native_text: Optional[str] = None
    prep = getattr(gateway, "_prepare_inbound_message_text", None)
    if gateway is None or prep is None or not callable(prep):
        logger.debug("multitenancy: gateway._prepare_inbound_message_text unavailable")
    else:
        source = getattr(event, "source", None)
        if source is not None:
            try:
                native_text = await prep(event=event, source=source, history=[])
            except Exception as exc:
                logger.debug("multitenancy: gateway._prepare_inbound_message_text failed (%s)", exc)

    local_file_text = _local_enrich_with_file_content(event, existing_text=native_text or "")
    if local_file_text:
        return _append_enrichment(native_text or getattr(event, "text", "") or "", local_file_text)

    if native_text:
        return native_text
    return await _local_enrich_with_vision_only(event)


def _append_enrichment(base: str, enrichment: str) -> str:
    base = str(base or "").strip()
    enrichment = str(enrichment or "").strip()
    if not enrichment:
        return base
    if not base:
        return enrichment
    return f"{enrichment}\n{base}"


_TEXT_FILE_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".log", ".xml", ".html", ".htm",
}
_MAX_LOCAL_ENRICH_FILE_BYTES = 10 * 1024 * 1024
_MAX_LOCAL_TEXT_PREVIEW_BYTES = 100_000
_MAX_XLSX_XML_BYTES = 2 * 1024 * 1024


def _local_enrich_with_file_content(event: Any, *, existing_text: str = "") -> Optional[str]:
    """Plugin-owned fallback for Feishu document attachments Hermes does not inline.

    Hermes core remains the first choice for multimodal preprocessing. This
    fallback only covers plain/tabular files already cached as local event paths,
    keeping compatibility in multitenancy instead of patching Hermes-agent.
    """
    media_urls = getattr(event, "media_urls", None) or []
    media_types = getattr(event, "media_types", None) or []
    if not media_urls:
        return None
    existing = str(existing_text or "")
    parts: list[str] = []
    for raw_path, raw_mtype in zip_longest(media_urls, media_types, fillvalue=""):
        path = Path(str(raw_path))
        if not path.is_file():
            continue
        name = path.name
        if f"[Content of {name}]" in existing:
            continue
        try:
            if path.stat().st_size > _MAX_LOCAL_ENRICH_FILE_BYTES:
                logger.debug("multitenancy: local file enrichment skipped oversized file %s", path)
                continue
        except OSError:
            continue
        suffix = path.suffix.lower()
        media_type = str(raw_mtype or "").lower()
        try:
            if suffix == ".xlsx":
                content = _extract_xlsx_text(path)
            elif media_type.startswith("text/") or suffix in _TEXT_FILE_EXTENSIONS:
                with path.open("rb") as handle:
                    content = handle.read(_MAX_LOCAL_TEXT_PREVIEW_BYTES).decode("utf-8", errors="replace")
            else:
                continue
        except Exception as exc:
            logger.debug("multitenancy: local file enrichment failed for %s: %s", path, exc)
            continue
        content = content.strip()
        if content:
            parts.append(f"[Content of {name}]:\n{content}")
    if not parts:
        return None
    return "\n\n".join(parts)


def _extract_xlsx_text(path: Path, *, max_sheets: int = 3, max_rows: int = 50, max_cells: int = 20) -> str:
    """Extract a small text preview from an XLSX file using only stdlib."""
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rel_ns = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
    with zipfile.ZipFile(path) as zf:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(_read_zip_member_limited(zf, "xl/sharedStrings.xml"))
            for si in root.findall("main:si", ns):
                texts = [node.text or "" for node in si.findall(".//main:t", ns)]
                shared_strings.append("".join(texts))

        sheet_names: dict[str, str] = {}
        if "xl/workbook.xml" in zf.namelist():
            workbook = ET.fromstring(_read_zip_member_limited(zf, "xl/workbook.xml"))
            for idx, sheet in enumerate(workbook.findall(".//main:sheet", ns), start=1):
                rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                sheet_names[rel_id or f"rId{idx}"] = sheet.attrib.get("name") or f"sheet{idx}"

        rel_targets: list[tuple[str, str]] = []
        if "xl/_rels/workbook.xml.rels" in zf.namelist():
            rels = ET.fromstring(_read_zip_member_limited(zf, "xl/_rels/workbook.xml.rels"))
            for rel in rels.findall("rel:Relationship", rel_ns):
                target = rel.attrib.get("Target") or ""
                if target.startswith("worksheets/"):
                    rel_targets.append((rel.attrib.get("Id") or "", "xl/" + target))
        if not rel_targets:
            rel_targets = [
                (f"rId{idx}", name)
                for idx, name in enumerate(sorted(
                    n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
                ), start=1)
            ]

        sections: list[str] = []
        for rel_id, sheet_path in rel_targets[:max_sheets]:
            if sheet_path not in zf.namelist():
                continue
            root = ET.fromstring(_read_zip_member_limited(zf, sheet_path))
            rows: list[str] = []
            for row in root.findall(".//main:sheetData/main:row", ns)[:max_rows]:
                cells: list[str] = []
                for cell in row.findall("main:c", ns)[:max_cells]:
                    value = cell.find("main:v", ns)
                    raw = value.text if value is not None else ""
                    if cell.attrib.get("t") == "s" and raw.isdigit():
                        idx = int(raw)
                        raw = shared_strings[idx] if idx < len(shared_strings) else raw
                    cells.append(raw or "")
                if any(cells):
                    rows.append("\t".join(cells))
            if rows:
                sections.append(f"[{sheet_names.get(rel_id, rel_id or sheet_path)}]\n" + "\n".join(rows))
        return "\n\n".join(sections)


def _read_zip_member_limited(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > _MAX_XLSX_XML_BYTES:
        raise ValueError(f"xlsx member too large: {name}")
    return zf.read(name)


async def _local_enrich_with_vision_only(event: Any) -> Optional[str]:
    """Local fallback if hermes' ``_prepare_inbound_message_text`` is unavailable.

    Only handles images (the most common multimodal input). Audio / files /
    reply context degrade gracefully — the model will see ``event.text`` only.
    """
    media_urls = getattr(event, "media_urls", None) or []
    media_types = getattr(event, "media_types", None) or []
    if not media_urls:
        return None
    try:
        from tools.vision_tools import vision_analyze_tool  # type: ignore
    except ImportError:
        return None
    import json as _json
    descriptions: list[str] = []
    for path, mtype in zip(media_urls, media_types or [""] * len(media_urls)):
        if mtype and not mtype.startswith("image"):
            continue
        try:
            result_json = await vision_analyze_tool(
                image_url=path,
                user_prompt="Describe this image in thorough detail.",
            )
            result = _json.loads(result_json) if isinstance(result_json, str) else result_json
            if result.get("success"):
                descriptions.append(f"[Image: {result.get('analysis', '')}]")
        except Exception as exc:
            logger.debug("multitenancy: local vision fallback error on %s: %s", path, exc)
    if not descriptions:
        return None
    base = getattr(event, "text", "") or ""
    return "\n".join(descriptions) + ("\n" + base if base else "")


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
    from .commands import parse_command

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


async def handle_async(*, event: Any, gateway: Any) -> None:
    """Async dispatch — orchestrates routing + pool + adapter calls + commands."""
    from .commands import parse_command

    try:
        source = getattr(event, "source", None)
        chat_id = getattr(source, "chat_id", "unknown") if source else "unknown"
        fallback_sender = getattr(source, "user_id", "unknown") if source else "unknown"
        sender = _resolve_sender_for_routing(event, fallback=fallback_sender)
        if _is_feishu_open_id(sender):
            setattr(event, "sender_open_id", sender)
        text = getattr(event, "text", "") or ""

        sender_alt = getattr(source, "user_id_alt", None) if source else None

        # Group-chat profile resolution — when the event is from a group/topic
        # chat, the route is keyed by chat_id (not the @-er's open_id). This
        # branch runs before slash-command short-circuit so /status & friends
        # see the group profile instead of the @-er's private profile.
        chat_type = _extract_chat_type(event)
        is_group_chat = _is_group_chat_type(chat_type)
        group_profile_name: Optional[str] = None
        group_profile_home: Optional[Path] = None
        if is_group_chat and chat_id and chat_id != "unknown":
            group_profile_name, group_profile_home = (
                await resolve_or_auto_provision_group_route(
                    chat_id=chat_id, gateway=gateway,
                )
            )

        # Slash command short-circuit (resolve route first so /status / /new
        # know which profile's history to inspect). When _resolve_route signals
        # a miss with profile_home=None, surface profile_name=None so command
        # handlers reply "未路由" instead of leaking the sender id.
        # Group messages start with leading @_all / @_user_N tokens that
        # Feishu prepends; strip them before delegating to parse_command so
        # ``@bot /feishu_auth`` is recognised as a slash command.
        command_source_text = _strip_leading_at_mentions(text) if is_group_chat else text
        cmd_pair = parse_command(command_source_text)
        if cmd_pair is not None:
            # Group profiles do not own any UAT — the whole auth command
            # family is hard-rejected so a curious member can't trigger an
            # OAuth dance that would store a per-user token under the
            # group's profile_home. Normalise first: Feishu lets users send
            # `/feishu_auth@bot` or sneak zero-width chars, and an exact
            # string-equality gate would let those through the day
            # feishu_auth becomes gateway-dispatchable.
            if is_group_chat and _is_blocked_group_command(cmd_pair[0]):
                adapter = _get_feishu_adapter(gateway)
                if adapter is not None:
                    await _safe_call(
                        adapter.send,
                        chat_id,
                        "群聊模式下不支持 /feishu_auth。"
                        "如需以你本人的身份调用飞书数据，请在与我私聊时执行。",
                    )
                return

            if is_group_chat:
                cmd_profile_name = group_profile_name
                cmd_profile_home = group_profile_home
            else:
                cmd_profile_name, cmd_profile_home = _resolve_route(sender, alt_id=sender_alt)
            cmd_profile = cmd_profile_name if cmd_profile_home is not None else None
            if _should_check_skill_slash_command(cmd_pair[0], gateway):
                async with _profile_gateway_context(
                    gateway,
                    event,
                    sender=sender,
                    sender_alt=sender_alt,
                    profile_name=cmd_profile,
                    profile_home=cmd_profile_home,
                    chat_id=chat_id,
                ):
                    skill_handled, skill_reply = _maybe_rewrite_skill_slash_command(
                        cmd_pair,
                        event,
                        gateway,
                        sender=sender,
                        sender_alt=sender_alt,
                        profile_name=cmd_profile,
                        profile_home=cmd_profile_home,
                        chat_id=chat_id,
                    )
            else:
                skill_handled, skill_reply = False, None
            if skill_handled:
                if skill_reply:
                    adapter = _get_feishu_adapter(gateway)
                    if adapter is not None:
                        await _safe_call(adapter.send, chat_id, skill_reply)
                    return
                text = getattr(event, "text", "") or ""
            else:
                await _handle_command(
                    cmd_pair,
                    sender,
                    sender_alt,
                    cmd_profile,
                    cmd_profile_home,
                    chat_id,
                    gateway,
                    event,
                )
                return

        # Routing: group already resolved above; sender-based path for p2p.
        if is_group_chat:
            profile_name, profile_home = group_profile_name, group_profile_home
            if profile_home is None:
                logger.info(
                    "multitenancy: no group route for chat_id=%s (inviter "
                    "not captured), ignoring",
                    chat_id,
                )
                adapter = _get_feishu_adapter(gateway)
                if adapter is not None:
                    await _safe_call(
                        adapter.send,
                        chat_id,
                        "👋 我还没有这个群的专属 Profile。"
                        "请移除我后再次拉我进群，让我捕获邀请人身份。",
                    )
                return
        else:
            profile_name, profile_home = _resolve_or_auto_provision_route(sender, alt_id=sender_alt)
            if profile_home is None:
                logger.info("multitenancy: no route for sender=%s, ignoring", sender)
                return

        adapter = _get_feishu_adapter(gateway)
        # Detect whether adapter supports the streaming/reaction APIs we use.
        # Real FeishuAdapter does; unit-test mocks typically don't.
        feishu_full = (
            adapter is not None
            and hasattr(adapter, "edit_message")
            and hasattr(adapter, "on_processing_start")
            and hasattr(adapter, "on_processing_complete")
        )

        # Multi-modal enrichment must happen before RunRequest admission because
        # file-only Feishu events have empty event.text.  The enriched content is
        # the real prompt and the dedupe/admission key should reflect it.
        _materialize_inbound_media_for_profile(event, profile_home)
        enriched_text = await _enrich_via_hermes_pipeline(event, gateway)
        run_content = enriched_text or text
        if not run_content and getattr(event, "media_urls", None):
            run_content = "[media attachment]"

        run_request = _run_request_for_routed_event(
            event=event,
            profile_name=profile_name,
            sender=sender,
            sender_alt=sender_alt,
            chat_id=chat_id,
            text=run_content,
        )
        from .run_broker import RunRejected

        try:
            run_admission = await _make_routed_run_broker().admit(run_request)
        except RunRejected as exc:
            logger.warning("multitenancy: routed run rejected profile=%s sender=%s: %s", profile_name, sender, exc)
            if feishu_full:
                try:
                    out = _processing_outcome(failed=True)
                    complete_deferred = getattr(adapter, "complete_deferred_processing", None)
                    if callable(complete_deferred):
                        await complete_deferred(event, out)
                    else:
                        await adapter.on_processing_complete(event, out)
                except Exception as complete_exc:
                    logger.debug("multitenancy: rejected processing_complete failed: %s", complete_exc)
            return

        if run_admission.duplicate:
            logger.info(
                "multitenancy: duplicate inbound event skipped profile=%s sender=%s message_id=%s",
                profile_name,
                sender,
                _event_message_id(event) or "",
            )
            if feishu_full:
                try:
                    out = _processing_outcome(failed=False)
                    complete_deferred = getattr(adapter, "complete_deferred_processing", None)
                    if callable(complete_deferred):
                        await complete_deferred(event, out)
                    else:
                        await adapter.on_processing_complete(event, out)
                except Exception as exc:
                    logger.debug("multitenancy: duplicate processing_complete failed: %s", exc)
            return

        # Register self in the user's in-flight slot (replace previous)
        current = asyncio.current_task()
        prev = _user_inflight_tasks.get(sender)
        if prev is not None and not prev.done() and prev is not current:
            prev.cancel()
        if current is not None:
            _user_inflight_tasks[sender] = current

        outcome_failed = False
        if feishu_full:
            try:
                await adapter.on_processing_start(event)
            except Exception as exc:
                logger.debug("multitenancy: on_processing_start failed: %s", exc)

        # Build the conversation: prior history + current user message (with
        # reply context spliced in). The runner prepends the profile's SOUL.
        # First lookup for a (profile, user) pair hydrates from SessionStore.
        hist_key = _history_key(profile_name, sender, sender_alt)
        prior = _load_history(hist_key)
        user_msg = _build_user_message(event, text_override=enriched_text)
        conversation = prior + [user_msg]
        agent_event = _event_with_text(event, user_msg["content"])

        try:
            if feishu_full:
                # Streaming path — card stream when available; text edit fallback.
                async def _dispatch_streaming(_request):
                    stream_response = await _stream_into_feishu(
                        adapter, chat_id, profile_name, profile_home, agent_event,
                        messages=conversation,
                    )
                    if stream_response:
                        await _deliver_media_from_stream_response(
                            gateway, stream_response, agent_event, adapter, profile_home
                        )
                    return stream_response

                run_result = await _make_routed_run_broker(
                    dispatch_agent=_dispatch_streaming,
                ).run(run_request, admitted=True)
                response_text = run_result.content
            else:
                # Mock / minimal adapter — old non-stream path (send_typing + pool.dispatch + send)
                if adapter is not None:
                    await _safe_call(adapter.send_typing, chat_id)
                async def _dispatch_nonstream(_request):
                    return await _get_pool().dispatch(profile_name, profile_home, agent_event)

                run_result = await _make_routed_run_broker(
                    dispatch_agent=_dispatch_nonstream,
                ).run(run_request, admitted=True)
                response_text = run_result.content
                if adapter is not None:
                    await _safe_call(adapter.send, chat_id, response_text)

            # Record turn into history + persist to SessionStore.
            if response_text and isinstance(response_text, str):
                _persist_turn(hist_key, user_msg, response_text)

            _touch_route(sender, sender_alt)
        except Exception:
            outcome_failed = True
            raise
        finally:
            if feishu_full:
                try:
                    out = _processing_outcome(failed=outcome_failed)
                    complete_deferred = getattr(adapter, "complete_deferred_processing", None)
                    if callable(complete_deferred):
                        await complete_deferred(event, out)
                    else:
                        await adapter.on_processing_complete(event, out)
                except Exception as exc:
                    logger.debug("multitenancy: on_processing_complete failed: %s", exc)
            if _user_inflight_tasks.get(sender) is current:
                _user_inflight_tasks.pop(sender, None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("multitenancy: handle_async failed: %s", exc)


def _processing_outcome(*, failed: bool) -> Any:
    """Return Hermes' ProcessingOutcome enum, or a string-compatible fallback."""
    try:
        from gateway.platforms.base import ProcessingOutcome  # type: ignore

        return ProcessingOutcome.FAILURE if failed else ProcessingOutcome.SUCCESS
    except Exception:
        class _FallbackOutcome:
            def __str__(self) -> str:
                status = "FAILURE" if failed else "SUCCESS"
                return f"ProcessingOutcome.{status}"

        return _FallbackOutcome()


# -- Command dispatch --------------------------------------------------------


def _maybe_rewrite_skill_slash_command(
    pair: tuple[str, str],
    event: Any,
    gateway: Any,
    *,
    sender: str,
    sender_alt: Optional[str],
    profile_name: Optional[str],
    profile_home: Optional[Path],
    chat_id: str,
) -> tuple[bool, Optional[str]]:
    """Rewrite Hermes skill slash commands into the native skill invocation text.

    Native gateway treats ``/skill-name args`` as an agent turn after loading
    the skill instructions. The multitenancy router sees the slash first, so it
    must preserve that behavior instead of replying "unknown command".
    """
    cmd, args = pair
    try:
        from .commands import is_known_command

        if is_known_command(cmd) or _gateway_handler_for_command(gateway, cmd) is not None:
            return False, None
        if _get_quick_command(gateway, cmd) is not None:
            return False, None
        if _get_plugin_command_handler(cmd) is not None:
            return False, None

        from agent.skill_commands import (  # type: ignore
            build_skill_invocation_message,
            get_skill_commands,
            resolve_skill_command_key,
        )

        skill_cmds = get_skill_commands()
        cmd_key = resolve_skill_command_key(cmd)
        if cmd_key is None:
            alias = _SKILL_SLASH_ALIASES.get(cmd.replace("_", "-"))
            if alias:
                cmd_key = resolve_skill_command_key(alias)
        if cmd_key is None:
            return False, None

        skill_name = (skill_cmds.get(cmd_key) or {}).get("name", "")
        platform = _event_platform_value(event)
        if platform and skill_name:
            try:
                from agent.skill_utils import get_disabled_skill_names  # type: ignore

                if skill_name in get_disabled_skill_names(platform=platform):
                    return (
                        True,
                        f"The **{skill_name}** skill is disabled for {platform}.\n"
                        "Enable it with: `hermes skills config`",
                    )
            except Exception as exc:
                logger.debug("multitenancy: skill disabled check failed (%s)", exc)

        msg = build_skill_invocation_message(
            cmd_key,
            args.strip(),
            task_id=_multitenant_gateway_session_key(
                event,
                profile_name=profile_name,
                sender=sender,
                sender_alt=sender_alt,
                chat_id=chat_id,
            ),
        )
        if not msg:
            return False, None
        logger.info("Hermes skill slash invocation: %s profile=%s", cmd_key, profile_name or "")
        setattr(event, "text", msg)
        return True, None
    except Exception as exc:
        logger.debug("multitenancy: skill command passthrough failed (%s)", exc)
        return False, None


def _should_check_skill_slash_command(cmd: str, gateway: Any) -> bool:
    """Return True only for slash commands that might be native skill aliases."""
    try:
        from .commands import is_known_command

        if is_known_command(cmd):
            return False
    except Exception:
        pass
    if _gateway_handler_for_command(gateway, cmd) is not None:
        return False
    if _get_quick_command(gateway, cmd) is not None:
        return False
    if _get_plugin_command_handler(cmd) is not None:
        return False
    return True


async def _handle_command(
    pair: tuple[str, str],
    sender: str,
    sender_alt: Optional[str],
    profile_name: Optional[str],
    profile_home: Optional[Path],
    chat_id: str,
    gateway: Any,
    event: Any,
) -> None:
    """Execute a parsed slash command and reply via the shared adapter."""
    cmd, _args = pair
    adapter = _get_feishu_adapter(gateway)

    approval_reply = _handle_pending_approval_command(
        cmd,
        _args,
        event,
        profile_name=profile_name,
        sender=sender,
        sender_alt=sender_alt,
        chat_id=chat_id,
    )
    if approval_reply is not None:
        reply = approval_reply
    elif cmd == "stop":
        task = _user_inflight_tasks.pop(sender, None)
        if task is not None and not task.done():
            task.cancel()
            reply = "已停止当前任务"
        else:
            reply = "没有进行中的任务"
    elif cmd == "status":
        task = _user_inflight_tasks.get(sender)
        running = task is not None and not task.done()
        # Surface session memory size + profile so the user knows their context.
        if profile_name:
            hist = _session_history.get(_history_key(profile_name, sender, sender_alt), [])
            hist_len = len(hist)
        else:
            hist_len = 0
        reply = (
            f"状态: {'运行中' if running else '空闲'}\n"
            f"profile: {profile_name or '(未路由)'}\n"
            f"会话历史: {hist_len} 条消息"
        )
    elif cmd in ("new", "reset"):
        # Clear this user's per-profile history (cache + persistent SessionStore).
        if profile_name:
            key = _history_key(profile_name, sender, sender_alt)
            had = _clear_history(key)
            reply = "会话已重置 ✅" if had else "会话已重置 (本来也是空的)"
        else:
            reply = "(未路由的用户) 没有历史可重置"
    elif cmd == "help":
        reply = _gateway_help_text()
    else:
        dispatched = await _dispatch_gateway_command(
            cmd,
            event,
            gateway,
            sender=sender,
            sender_alt=sender_alt,
            profile_name=profile_name,
            profile_home=profile_home,
            chat_id=chat_id,
        )
        if dispatched is not None:
            reply = dispatched
        else:
            from .commands import is_known_command, unknown_command_message

            if is_known_command(cmd):
                reply = (
                    f"Command `/{cmd}` is recognized by Hermes, but this gateway does not "
                    "expose a reusable command dispatcher yet."
                )
            else:
                reply = unknown_command_message(cmd)
                logger.info("%s", reply)

    if adapter is not None:
        await _safe_call(adapter.send, chat_id, reply)


def _handle_pending_approval_command(
    cmd: str,
    args: str,
    event: Any,
    *,
    profile_name: Optional[str],
    sender: str,
    sender_alt: Optional[str],
    chat_id: str,
) -> Optional[str]:
    """Resolve a child AIAgent approval bridge before falling back to gateway commands."""
    if cmd not in {"approve", "deny"}:
        return None
    session_key = _multitenant_gateway_session_key(
        event,
        profile_name=profile_name,
        sender=sender,
        sender_alt=sender_alt,
        chat_id=chat_id,
    )
    if not session_key or not _pending_approval_requests.get(session_key):
        return None

    if cmd == "deny":
        resolve_all = "all" in str(args or "").lower().split()
        count = _resolve_pending_approval_requests(session_key, "deny", resolve_all=resolve_all)
        count_msg = f" ({count} commands)" if count > 1 else ""
        return f"❌ Command{'s' if count > 1 else ''} denied{count_msg}."

    parts = str(args or "").strip().lower().split()
    resolve_all = "all" in parts
    remaining = [part for part in parts if part != "all"]
    if any(part in {"always", "permanent", "permanently"} for part in remaining):
        choice = "always"
        scope_msg = " (pattern approved permanently)"
    elif any(part in {"session", "ses"} for part in remaining):
        choice = "session"
        scope_msg = " (pattern approved for this session)"
    else:
        choice = "once"
        scope_msg = ""
    count = _resolve_pending_approval_requests(session_key, choice, resolve_all=resolve_all)
    count_msg = f" ({count} commands)" if count > 1 else ""
    return f"✅ Command{'s' if count > 1 else ''} approved{scope_msg}{count_msg}. The agent is resuming..."


def _resolve_pending_approval_requests(
    session_key: str,
    choice: str,
    *,
    resolve_all: bool = False,
) -> int:
    queue = _pending_approval_requests.get(session_key) or []
    if not queue:
        return 0
    if resolve_all:
        targets = list(queue)
        queue.clear()
    else:
        targets = [queue.pop(0)]
    if queue:
        _pending_approval_requests[session_key] = queue
    else:
        _pending_approval_requests.pop(session_key, None)
    for entry in targets:
        raw_decision_path = str(entry.get("decision_path") or "").strip()
        if not raw_decision_path:
            continue
        decision_path = Path(raw_decision_path)
        try:
            decision_path.parent.mkdir(parents=True, exist_ok=True)
            decision_path.write_text(json.dumps({"choice": choice}), encoding="utf-8")
        except Exception as exc:
            logger.warning(
                "multitenancy: failed to write approval decision for %s: %s",
                entry.get("approval_id") or "?",
                exc,
            )
    return len(targets)


def _record_pending_approval(payload: dict) -> None:
    session_key = str(payload.get("session_key") or "").strip()
    decision_path = str(payload.get("decision_path") or "").strip()
    if not session_key or not decision_path:
        return
    approval_id = str(payload.get("approval_id") or decision_path)
    queue = _pending_approval_requests.setdefault(session_key, [])
    queue[:] = [
        item for item in queue
        if str(item.get("approval_id") or item.get("decision_path")) != approval_id
    ]
    queue.append(dict(payload))


def _clear_pending_approval(payload: dict) -> None:
    session_key = str(payload.get("session_key") or "").strip()
    approval_id = str(payload.get("approval_id") or "").strip()
    if not session_key or not approval_id:
        return
    queue = _pending_approval_requests.get(session_key) or []
    queue[:] = [item for item in queue if str(item.get("approval_id") or "") != approval_id]
    if queue:
        _pending_approval_requests[session_key] = queue
    else:
        _pending_approval_requests.pop(session_key, None)


async def _handle_child_approval_required(adapter: Any, chat_id: str, payload: Any) -> None:
    data = payload if isinstance(payload, dict) else {}
    _record_pending_approval(data)
    command = str(data.get("command") or "")
    description = str(data.get("description") or "dangerous command")
    logger.info(
        "multitenancy child approval_required session=%s approval_id=%s command=%s",
        str(data.get("session_key") or ""),
        str(data.get("approval_id") or ""),
        command[:120],
    )
    preview = command[:200] + "..." if len(command) > 200 else command
    message = (
        "⚠️ Dangerous command requires approval:\n"
        f"```\n{preview}\n```\n"
        f"Reason: {description}\n\n"
        "Reply `/approve` to execute, `/approve session` to approve this pattern "
        "for the session, `/approve always` to approve permanently, or `/deny` to cancel."
    )
    if adapter is not None:
        await _safe_call(adapter.send, chat_id, message)


async def _dispatch_gateway_command(
    cmd: str,
    event: Any,
    gateway: Any,
    *,
    sender: str,
    sender_alt: Optional[str],
    profile_name: Optional[str],
    profile_home: Optional[Path],
    chat_id: str,
) -> Optional[str]:
    """Delegate a Hermes-known slash command to the gateway when possible."""
    _ensure_command_event_methods(event, cmd)

    dispatcher = getattr(gateway, "_dispatch_slash_command", None)
    if callable(dispatcher):
        async with _profile_gateway_context(
            gateway,
            event,
            sender=sender,
            sender_alt=sender_alt,
            profile_name=profile_name,
            profile_home=profile_home,
            chat_id=chat_id,
        ):
            try:
                result = dispatcher(event, multitenancy_context={
                    "profile_name": profile_name,
                    "profile_home": str(profile_home) if profile_home else "",
                    "sender_open_id": sender,
                    "session_key_override": _multitenant_gateway_session_key(
                        event,
                        profile_name=profile_name,
                        sender=sender,
                        sender_alt=sender_alt,
                        chat_id=chat_id,
                    ),
                })
            except TypeError:
                result = dispatcher(event)
            if asyncio.iscoroutine(result):
                result = await result
            logger.info("Hermes gateway command handled: %s", cmd)
            return str(result) if result is not None else None

    handler = _gateway_handler_for_command(gateway, cmd)
    if handler is None:
        quick_result = await _dispatch_quick_command(
            cmd,
            event,
            gateway,
            sender=sender,
            sender_alt=sender_alt,
            profile_name=profile_name,
            profile_home=profile_home,
            chat_id=chat_id,
        )
        if quick_result is not None:
            return quick_result
        return await _dispatch_plugin_command(
            cmd,
            event,
            gateway,
            sender=sender,
            sender_alt=sender_alt,
            profile_name=profile_name,
            profile_home=profile_home,
            chat_id=chat_id,
        )
    async with _profile_gateway_context(
        gateway,
        event,
        sender=sender,
        sender_alt=sender_alt,
        profile_name=profile_name,
        profile_home=profile_home,
        chat_id=chat_id,
    ):
        result = handler(event)
        if asyncio.iscoroutine(result):
            result = await result
    logger.info("Hermes gateway command handled: %s", cmd)
    return str(result) if result is not None else None


async def _dispatch_quick_command(
    cmd: str,
    event: Any,
    gateway: Any,
    *,
    sender: str,
    sender_alt: Optional[str],
    profile_name: Optional[str],
    profile_home: Optional[Path],
    chat_id: str,
) -> Optional[str]:
    """Handle Hermes config.quick_commands without copying the command list."""
    qcmd = _get_quick_command(gateway, cmd)
    if not isinstance(qcmd, dict):
        return None

    kind = qcmd.get("type")
    if kind == "exec":
        exec_cmd = qcmd.get("command", "")
        if not exec_cmd:
            return f"Quick command '/{cmd}' has no command defined."
        if not _quick_exec_allowed(gateway, qcmd):
            return (
                f"Quick command '/{cmd}' exec is disabled for Feishu multitenancy. "
                "Enable only after profile sandboxing is in place."
            )
        logger.info("Hermes quick command exec: %s", cmd)
        try:
            env = os.environ.copy()
            if profile_home is not None:
                env["HERMES_HOME"] = str(profile_home)
            proc = await asyncio.create_subprocess_shell(
                exec_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = (stdout or stderr).decode().strip()
            return output if output else "Command returned no output."
        except asyncio.TimeoutError:
            return "Quick command timed out (30s)."
        except Exception as exc:
            return f"Quick command error: {exc}"

    if kind == "alias":
        target = str(qcmd.get("target", "") or "").strip()
        if not target:
            return f"Quick command '/{cmd}' has no target defined."
        target = target if target.startswith("/") else f"/{target}"
        target_command = target.lstrip("/")
        new_cmd = target_command.split()[0] if target_command else ""
        if not new_cmd or new_cmd == cmd:
            return f"Quick command '/{cmd}' has invalid target."
        user_args = _command_args_from_event(event).strip()
        setattr(event, "text", f"{target} {user_args}".strip())
        _set_command_event_methods(event, new_cmd)
        logger.info("Hermes quick command alias: %s -> %s", cmd, target)
        return await _dispatch_gateway_command(
            new_cmd,
            event,
            gateway,
            sender=sender,
            sender_alt=sender_alt,
            profile_name=profile_name,
            profile_home=profile_home,
            chat_id=chat_id,
        )

    return f"Quick command '/{cmd}' has unsupported type (supported: 'exec', 'alias')."


def _get_quick_command(gateway: Any, cmd: str) -> Any:
    config = getattr(gateway, "config", None)
    if isinstance(config, dict):
        quick_commands = config.get("quick_commands", {}) or {}
    else:
        quick_commands = getattr(config, "quick_commands", {}) or {}
    if not isinstance(quick_commands, dict):
        return None
    return quick_commands.get(cmd)


def _quick_exec_allowed(gateway: Any, qcmd: dict[str, Any]) -> bool:
    """Return True only when multitenant Feishu quick exec is explicitly enabled."""
    qcmd_flag = qcmd.get("multitenancy_allow_exec")
    if qcmd_flag is None:
        qcmd_cfg = qcmd.get("multitenancy")
        if isinstance(qcmd_cfg, dict):
            qcmd_flag = qcmd_cfg.get("allow_exec")
    if qcmd_flag is not None:
        return _truthy(qcmd_flag)

    config = getattr(gateway, "config", None)
    plugin_cfg = None
    if isinstance(config, dict):
        plugin_cfg = config.get("multitenancy")
    else:
        plugin_cfg = getattr(config, "multitenancy", None)
    if isinstance(plugin_cfg, dict) and "allow_quick_exec" in plugin_cfg:
        return _truthy(plugin_cfg.get("allow_quick_exec"))

    env_value = os.getenv("HERMES_MULTITENANCY_ALLOW_QUICK_EXEC")
    return _truthy(env_value) if env_value is not None else False


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "allow", "enabled"}


async def _dispatch_plugin_command(
    cmd: str,
    event: Any,
    gateway: Any,
    *,
    sender: str,
    sender_alt: Optional[str],
    profile_name: Optional[str],
    profile_home: Optional[Path],
    chat_id: str,
) -> Optional[str]:
    """Delegate plugin-registered slash commands to Hermes' plugin manager."""
    handler = _get_plugin_command_handler(cmd)
    if handler is None:
        return None
    logger.info("Hermes plugin slash handler: %s", cmd.replace("_", "-"))
    user_args = ""
    get_args = getattr(event, "get_command_args", None)
    if callable(get_args):
        user_args = (get_args() or "").strip()
    async with _profile_gateway_context(
        gateway,
        event,
        sender=sender,
        sender_alt=sender_alt,
        profile_name=profile_name,
        profile_home=profile_home,
        chat_id=chat_id,
    ):
        result = handler(user_args)
        if asyncio.iscoroutine(result):
            result = await result
    return str(result) if result else None


def _get_plugin_command_handler(cmd: str) -> Any:
    """Return Hermes' plugin command handler for ``cmd`` when available."""
    try:
        from hermes_cli.plugins import get_plugin_command_handler  # type: ignore

        return get_plugin_command_handler(cmd.replace("_", "-"))
    except Exception as exc:
        logger.debug("multitenancy: plugin command lookup failed (%s)", exc)
        return None


def _gateway_handler_for_command(gateway: Any, cmd: str) -> Any:
    """Return Hermes' handler method using naming conventions, not a command table."""
    normalized = cmd.replace("-", "_")
    candidates = [f"_handle_{normalized}_command"]
    if normalized == "sethome":
        candidates.append("_handle_set_home_command")
    for name in candidates:
        handler = getattr(gateway, name, None)
        if callable(handler):
            return handler
    return None


def _ensure_command_event_methods(event: Any, cmd: str) -> None:
    """Add minimal MessageEvent command helpers for tests/fallback objects."""
    args = _command_args_from_event(event)
    if not callable(getattr(event, "get_command", None)):
        setattr(event, "get_command", lambda: cmd)
    if not callable(getattr(event, "get_command_args", None)):
        setattr(event, "get_command_args", lambda: args)


def _set_command_event_methods(event: Any, cmd: str) -> None:
    args = _command_args_from_text(getattr(event, "text", "") or "")
    setattr(event, "get_command", lambda: cmd)
    setattr(event, "get_command_args", lambda: args)


def _command_args_from_event(event: Any) -> str:
    get_args = getattr(event, "get_command_args", None)
    if callable(get_args):
        return str(get_args() or "")
    return _command_args_from_text(getattr(event, "text", "") or "")


def _command_args_from_text(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _event_platform_value(event: Any) -> Optional[str]:
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None) if source is not None else None
    value = getattr(platform, "value", platform)
    return str(value) if value else None


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
    from .runtime import _get_env_lock

    async with _get_env_lock():
        old_home = os.environ.get("HERMES_HOME")
        had_home = "HERMES_HOME" in os.environ
        old_session_key_for_source = getattr(gateway, "_session_key_for_source", None)
        if profile_home is not None:
            os.environ["HERMES_HOME"] = str(profile_home)
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
    from .runtime import resolve_profile_home as _spike_resolve

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


def _resolve_or_auto_provision_route(
    sender: str,
    *,
    alt_id: Optional[str] = None,
) -> tuple[str, Optional[Path]]:
    """Resolve an existing route, or create a dedicated profile for a new sender."""
    alt_lookup = None if _is_feishu_open_id(sender) else alt_id
    profile_name, profile_home = _resolve_route(sender, alt_id=alt_lookup)
    if profile_home is not None:
        _repair_auto_profile(profile_name, profile_home, route_key=sender, sender=sender)
        return profile_name, profile_home
    if _auto_provision_enabled():
        provisioned = _auto_provision_route(sender, alt_id=alt_id)
        if provisioned is not None:
            return provisioned
        return profile_name, None
    return _resolve_route(sender, alt_id=alt_id)


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


def _auto_provision_enabled() -> bool:
    value = os.environ.get("HERMES_MULTITENANCY_AUTO_PROVISION", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _auto_provision_route(
    sender: str,
    *,
    alt_id: Optional[str] = None,
) -> Optional[tuple[str, Path]]:
    """Create a route/profile for an unseen Feishu user, then return it."""
    if not _auto_provision_enabled():
        return None
    table = _get_routing_table()
    if table is None:
        return None

    route_key = sender
    if not route_key or route_key == "unknown":
        return None

    profile_name = _auto_profile_name(route_key)
    profile_home = _profile_name_to_home(profile_name)
    try:
        _ensure_auto_profile(profile_name, profile_home, route_key=route_key, sender=sender)
        table.upsert(
            user_id=route_key,
            profile_name=profile_name,
            open_id=route_key,
            union_id=alt_id if alt_id and alt_id != sender else None,
        )
    except Exception as exc:
        logger.warning(
            "multitenancy: auto-provision failed sender=%s alt=%s: %s",
            sender,
            alt_id,
            exc,
        )
        return None

    logger.info(
        "multitenancy: auto-provisioned sender=%s alt=%s profile=%s",
        sender,
        alt_id,
        profile_name,
    )
    return profile_name, profile_home


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


def _is_group_chat_type(chat_type: str) -> bool:
    return chat_type.lower() in _GROUP_CHAT_TYPES


# Commands that bind/replace a per-user Feishu identity. None of them make
# sense in a group profile (it owns no UAT), so all are hard-rejected.
_GROUP_BLOCKED_COMMANDS: frozenset[str] = frozenset(
    {"feishu_auth", "feishu_logout", "feishu_reauth"}
)
# Zero-width / bidi chars Feishu clients occasionally inject; stripped before
# the command-name membership check so they can't smuggle a blocked command
# past an exact-match gate.
_INVISIBLE_CHARS = str.maketrans(
    "", "", "​‌‍‎‏﻿"
)


def _is_blocked_group_command(command: str) -> bool:
    """True when ``command`` is an auth-family command barred inside groups.

    Normalises before comparing: lowercase, strip a ``@bot`` suffix Feishu
    appends to at-mentioned commands, and drop zero-width characters. An
    exact ``== "feishu_auth"`` check let ``/feishu_auth@bot`` and
    zero-width-padded variants slip through.
    """
    if not command:
        return False
    normalized = command.translate(_INVISIBLE_CHARS).strip().lower()
    normalized = normalized.split("@", 1)[0]
    return normalized in _GROUP_BLOCKED_COMMANDS


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


def _make_group_profile_name(chat_id: str) -> str:
    return f"{_GROUP_PROFILE_PREFIX}{_short_chat_id(chat_id)}"


def is_group_profile_name(name: Optional[str]) -> bool:
    return bool(name) and str(name).startswith(_GROUP_PROFILE_PREFIX)


def register_chat_inviter(
    chat_id: str,
    inviter_open_id: str,
    *,
    chat_name: Optional[str] = None,
    inviter_display: Optional[str] = None,
) -> None:
    """Layer 4 hook entry — cache who pulled the bot into a chat.

    The cache survives only until the first @mention in the chat (or until
    the process restarts); the durable owner record is the routing row's
    ``owner_open_id`` column written by ``_provision_group_route``.
    """
    if not chat_id or not _is_feishu_open_id(inviter_open_id):
        return
    now = time.time()
    with _chat_inviter_cache_lock:
        _chat_inviter_cache[chat_id] = {
            "inviter_open_id": inviter_open_id,
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
    try:
        table = _get_routing_table()
        if table is None:
            logger.debug(
                "multitenancy: pending inviter store unavailable for chat_id=%s",
                chat_id,
            )
            return
        table.put_pending_inviter(chat_id, inviter_open_id)
        table.prune_pending_inviters(int(now), _CHAT_INVITER_CACHE_TTL_S)
    except Exception as exc:
        logger.debug(
            "multitenancy: failed to persist pending inviter chat_id=%s: %s",
            chat_id,
            exc,
        )


def _resolve_group_inviter_from_cache(chat_id: str) -> Optional[dict[str, Any]]:
    with _chat_inviter_cache_lock:
        entry = _chat_inviter_cache.get(chat_id)
        if entry is None:
            return None
        if time.time() - entry.get("_ts", 0) > _CHAT_INVITER_CACHE_TTL_S:
            _chat_inviter_cache.pop(chat_id, None)
            return None
        return dict(entry)


def _pop_chat_inviter(chat_id: str) -> None:
    with _chat_inviter_cache_lock:
        _chat_inviter_cache.pop(chat_id, None)


def _has_cached_chat_inviter(chat_id: str) -> bool:
    return _resolve_group_inviter_from_cache(chat_id) is not None


async def resolve_or_auto_provision_group_route(
    *,
    chat_id: str,
    gateway: Any,
) -> tuple[Optional[str], Optional[Path]]:
    """Resolve a group route by chat_id, auto-provisioning on first use.

    Group provisioning is intentionally independent from the unknown-user
    auto-provision toggle: a group route can only be created after a trusted
    ``bot_added`` event captures the inviter, so it does not open the same
    attack surface as creating user profiles for arbitrary senders. Returns
    ``(profile_name, profile_home)`` on success, ``(None, None)`` if no route
    exists and no inviter was captured for this chat. Inviter identity MUST
    come from the
    ``im.chat.member.bot.added_v1.operator_id`` cache entry (see
    ``register_chat_inviter``); we never fall back to the group's current
    owner_id because that is not the same person who added the bot.
    """
    if not chat_id:
        return None, None
    table = _get_routing_table()
    if table is None:
        return None, None
    row = table.lookup_by_chat_id(chat_id)
    if row is not None:
        return row.profile_name, _profile_name_to_home(row.profile_name)
    return await _provision_group_route(chat_id=chat_id, gateway=gateway)


async def _provision_group_route(
    *,
    chat_id: str,
    gateway: Any,
) -> tuple[Optional[str], Optional[Path]]:
    """Create a new group routing row + profile skeleton.

    Returns ``(None, None)`` if no inviter is known for the chat — that is
    the explicit "refuse silently" path. The caller is responsible for
    surfacing a user-facing message.
    """
    table = _get_routing_table()
    if table is None:
        return None, None
    inviter_open_id: Optional[str] = None
    chat_name: Optional[str] = None
    inviter_display: Optional[str] = None
    try:
        table.prune_pending_inviters(
            int(time.time()),
            _CHAT_INVITER_CACHE_TTL_S,
        )
        pending_inviter = table.get_pending_inviter(chat_id)
    except Exception as exc:
        logger.debug(
            "multitenancy: pending inviter lookup failed chat_id=%s: %s",
            chat_id,
            exc,
        )
        pending_inviter = None
    if _is_feishu_open_id(pending_inviter):
        inviter_open_id = str(pending_inviter)
    else:
        cached = _resolve_group_inviter_from_cache(chat_id)
        if cached and _is_feishu_open_id(cached.get("inviter_open_id")):
            inviter_open_id = cached["inviter_open_id"]
            chat_name = cached.get("chat_name")
            inviter_display = cached.get("inviter_display")
    if not inviter_open_id:
        logger.warning(
            "multitenancy: cannot auto-provision group route chat_id=%s "
            "(bot_added inviter not captured — re-add the bot to retry)",
            chat_id,
        )
        return None, None

    if not chat_name:
        chat_name = await _fetch_chat_name(chat_id, gateway) or chat_id
    if not inviter_display:
        inviter_display = (
            await _fetch_user_display(inviter_open_id, gateway) or inviter_open_id
        )

    display_label = f"{inviter_display}-{chat_name}".strip("-") or chat_id
    profile_name = _make_group_profile_name(chat_id)
    profile_home = _profile_name_to_home(profile_name)
    try:
        _ensure_group_profile(
            profile_name=profile_name,
            profile_home=profile_home,
            chat_id=chat_id,
            owner_open_id=inviter_open_id,
            display_label=display_label,
        )
        table.upsert_group(
            chat_id=chat_id,
            profile_name=profile_name,
            owner_open_id=inviter_open_id,
            display_label=display_label,
        )
    except Exception as exc:
        logger.warning(
            "multitenancy: failed to provision group route chat_id=%s: %s",
            chat_id,
            exc,
        )
        return None, None

    logger.info(
        "multitenancy: auto-provisioned group profile chat_id=%s profile=%s owner=%s",
        chat_id,
        profile_name,
        inviter_open_id,
    )
    # Eagerly evict the cache entry so a later bot-removal + re-add cycle
    # repopulates from the fresh event rather than reusing stale chat_name.
    _pop_chat_inviter(chat_id)
    try:
        table.clear_pending_inviter(chat_id)
    except Exception as exc:
        logger.debug(
            "multitenancy: failed to clear pending inviter chat_id=%s: %s",
            chat_id,
            exc,
        )
    return profile_name, profile_home


def _ensure_group_profile(
    *,
    profile_name: str,
    profile_home: Path,
    chat_id: str,
    owner_open_id: str,
    display_label: str,
) -> None:
    """Create the on-disk profile skeleton for a group profile.

    Mirrors ``_ensure_auto_profile`` for shared config + SOUL.md, but adds a
    ``group_profile.json`` marker so downstream tools can detect the group
    context and refuse UAT-dependent operations.
    """
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
                    f"# Hermes Group Profile {profile_name}",
                    "",
                    f"You are a Feishu group-chat agent for chat `{chat_id}`.",
                    f"This profile is owned by Feishu user `{owner_open_id}` "
                    f"(display label: `{display_label}`).",
                    "Identity rules (strict):",
                    "- 你不能以群成员任何一个个人的身份操作飞书数据。",
                    "- /feishu_auth 在群聊模式下被禁用，任何用户的 UAT 不会被加载。",
                    "- 该群 profile 使用 `lark_cli` 时默认走 bot identity。",
                    "- 仅响应被 @ 的消息；群里的旁白不要回复。",
                    "- 该群的对话和记忆与其它群、与个人私聊完全隔离。",
                    "",
                    _LARK_CLI_SOUL_GUIDANCE,
                    "",
                ]
            ),
            encoding="utf-8",
        )
    else:
        _ensure_soul_guidance(soul_path, _LARK_CLI_SOUL_GUIDANCE)

    # Group profiles get an empty feishu_uat/ directory but no per-user JSON;
    # the marker file below tells UAT helpers to refuse to load user tokens.
    (profile_home / "feishu_uat").mkdir(parents=True, exist_ok=True, mode=0o700)
    _sync_default_skills_for_profile(profile_home, shared_home)

    marker_path = profile_home / "group_profile.json"
    if not marker_path.exists():
        marker_path.write_text(
            json.dumps(
                {
                    "kind": "group",
                    "chat_id": chat_id,
                    "owner_open_id": owner_open_id,
                    "display_label": display_label,
                    "feishu_auth_disabled": True,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # The group profile's home channel IS the group itself. Pre-write
    # FEISHU_HOME_CHANNEL into .env so the gateway's "no home channel set"
    # onboarding prompt never fires for a group profile.
    _write_group_profile_env(profile_home, shared_home, chat_id)

    # .env is owned by us above; auth.json still follows the shared symlink.
    _ensure_shared_profile_file(profile_home, shared_home, "auth.json")


def _write_group_profile_env(profile_home: Path, shared_home: Path, chat_id: str) -> None:
    """Materialize <profile>/.env with ONLY model keys + FEISHU_HOME_CHANNEL.

    The subprocess env is whitelisted (``_SUBPROCESS_ENV_ALLOWLIST``) and
    does NOT pass model provider API keys, so the AIAgent child reads them
    from ``<profile>/.env`` via its own dotenv path. But the previous
    "copy every shared line" approach also duplicated the credential-vault
    master key, gateway config, and unrelated tool tokens into every group
    profile dir — secret sprawl flagged in code review. We now copy only
    the model provider key/base-url names (``_MODEL_ENV_ALLOWLIST``, the
    same source of truth the model resolver uses) plus the per-group
    FEISHU_HOME_CHANNEL override. ``HERMES_MULTITENANCY_CREDENTIAL_KEY``
    already flows through the allowlisted subprocess env, so it never
    needs to land on disk in a tenant dir.

    Group profiles also preempt the gateway's home-channel onboarding
    prompt by declaring the group chat itself as the home channel. Re-runs
    are idempotent.
    """
    from .agent_real import _MODEL_ENV_ALLOWLIST

    shared_env = shared_home / ".env"
    target = profile_home / ".env"
    base_lines: list[str] = []
    if shared_env.exists():
        try:
            shared_text = shared_env.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug(
                "multitenancy: shared .env unreadable (%s); group .env will only set FEISHU_HOME_CHANNEL",
                exc,
            )
            shared_text = ""
        for line in shared_text.splitlines():
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in _MODEL_ENV_ALLOWLIST:
                base_lines.append(line)
    base_lines.append(f"FEISHU_HOME_CHANNEL={chat_id}")
    # Atomic write: a crash between unlink() and write_text() used to leave
    # the profile with NO .env (all inherited creds gone until reprovision).
    # Write a sibling temp file then os.replace() — the rename is atomic on
    # POSIX and also transparently replaces a stale symlink without ever
    # following it into the shared .env.
    if target.is_symlink():
        target.unlink()
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text("\n".join(base_lines) + "\n", encoding="utf-8")
    os.replace(tmp, target)


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
                    f"You are the dedicated Hermes tenant profile for Feishu route `{route_key}`.",
                    f"The current Feishu sender open_id is `{sender}`.",
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

    for name in ("auth.json", ".env"):
        _ensure_shared_profile_file(profile_home, shared_home, name)
    _sync_default_skills_for_profile(profile_home, shared_home)


def _sync_default_skills_for_profile(profile_home: Path, shared_home: Path) -> None:
    # Use a relative import so the call works both when the package is loaded
    # as ``hermes_multitenancy`` (pytest/direct install) and when the Hermes
    # plugin loader exposes it as ``hermes_plugins.multitenancy.hermes_multitenancy``.
    from .sync.feishu_org import _sync_default_profile_skills

    _sync_default_profile_skills(profile_home, shared_home)


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
                feishu_platform = ((loaded.get("platforms") or {}).get("feishu") or None)
                if feishu_platform:
                    config["platforms"] = {"feishu": feishu_platform}
        except Exception as exc:
            logger.debug("multitenancy: failed to read shared config %s: %s", shared_config, exc)

    _apply_lark_cli_profile_defaults(config)
    return _normalize_profile_config(config)


def _normalize_profile_config(config: dict[str, Any]) -> dict[str, Any]:
    model = config.get("model")
    if isinstance(model, dict) and model.get("default"):
        default_model = str(model.get("default") or "").strip()
        provider = str(model.get("provider") or "").strip()
        if default_model and provider and "/" not in default_model:
            model["default"] = f"{provider}/{default_model}"
    return config


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
            platform_modes.setdefault("feishu", "merge_default")
            platform_modes.setdefault("api_server", "merge_default")
            platform_modes.setdefault("webui", "merge_default")


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
            from .sessions import SessionStore
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
            from .routing import RoutingTable
            _routing_table = RoutingTable(_routing_db_path)
        except Exception as exc:
            logger.debug("multitenancy: RoutingTable init failed (%s)", exc)
            return None
    return _routing_table


def _get_pool():
    """Lazy-init module-level RuntimePool."""
    global _pool
    if _pool is None:
        from .pool import RuntimePool
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


def _adapter_supports_streaming_card(adapter) -> bool:
    """Return True when the shared Feishu adapter can drive card streaming."""
    if adapter is None:
        return False
    supports = getattr(adapter, "supports_streaming_card", None)
    if callable(supports):
        try:
            return bool(supports())
        except Exception as exc:
            logger.debug("multitenancy: supports_streaming_card failed: %s", exc)
            return False
    return bool(getattr(adapter, "SUPPORTS_STREAMING_CARD", False))


async def _start_feishu_stream_target(adapter, chat_id) -> tuple[str, Optional[str]]:
    """Start a card stream when possible, otherwise create the text placeholder."""
    if _adapter_supports_streaming_card(adapter):
        starter = getattr(adapter, "start_streaming_card", None)
        updater = getattr(adapter, "update_streaming_card", None)
        if callable(starter) and callable(updater):
            try:
                result = await starter(chat_id=chat_id, reply_to=None, metadata=None)
            except Exception as exc:
                logger.debug("multitenancy: start_streaming_card failed: %s", exc)
            else:
                message_id = getattr(result, "message_id", None)
                if getattr(result, "success", False) and message_id:
                    logger.info("multitenancy: streaming_card started message_id=%s", message_id)
                    return ("card", str(message_id))
                logger.debug(
                    "multitenancy: start_streaming_card unsuccessful: %s",
                    getattr(result, "error", None),
                )

    placeholder_send = await adapter.send(chat_id, "...")
    message_id = (
        placeholder_send.message_id
        if getattr(placeholder_send, "success", False)
        else None
    )
    return ("edit", str(message_id) if message_id else None)


async def _update_feishu_stream_target(
    adapter, chat_id, message_id, content, *, mode: str, finalize: bool = False
):
    """Update the current Feishu streaming surface."""
    if mode == "card":
        result = await adapter.update_streaming_card(
            chat_id=chat_id,
            message_id=message_id,
            content=content,
            finalize=finalize,
        )
        if not getattr(result, "success", False):
            logger.debug(
                "multitenancy: update_streaming_card unsuccessful: %s",
                getattr(result, "error", None),
            )
        return result
    return await _edit_with_retry(
        adapter, chat_id, message_id, content, finalize=finalize
    )


async def _abort_feishu_stream_target(
    adapter, chat_id, message_id, content, *, mode: str
):
    """Force the current streaming surface into an aborted terminal state."""
    if mode == "card":
        aborter = getattr(adapter, "abort_streaming_card", None)
        if callable(aborter):
            result = await aborter(
                chat_id=chat_id,
                message_id=message_id,
                content=content,
            )
            if not getattr(result, "success", False):
                logger.debug(
                    "multitenancy: abort_streaming_card unsuccessful: %s",
                    getattr(result, "error", None),
                )
            return result
    return await _update_feishu_stream_target(
        adapter,
        chat_id,
        message_id,
        content or "Aborted.",
        mode=mode,
        finalize=True,
    )


async def _run_terminal_stream_update(update_coro, *, label: str):
    """Run terminal card/edit update even if the caller is being cancelled."""
    task = asyncio.create_task(update_coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            result = await task
            logger.info("multitenancy: %s completed while task was cancelling", label)
            return result
        except Exception as exc:
            logger.debug("multitenancy: %s failed while task was cancelling: %s", label, exc)
        raise


async def _update_feishu_stream_reasoning(
    adapter, chat_id, message_id, content, *, mode: str
):
    """Update reasoning/status text without polluting the final answer block."""
    if mode == "card":
        updater = getattr(adapter, "update_streaming_card_reasoning", None)
        if callable(updater):
            return await updater(
                chat_id=chat_id,
                message_id=message_id,
                content=content,
            )
    return await _update_feishu_stream_target(
        adapter, chat_id, message_id, content, mode=mode
    )


async def _update_feishu_stream_status(
    adapter, chat_id, message_id, content, *, mode: str
):
    """Update an ephemeral in-progress status without retaining it as reasoning."""
    if mode == "card":
        updater = getattr(adapter, "update_streaming_card_status", None)
        if callable(updater):
            return await updater(
                chat_id=chat_id,
                message_id=message_id,
                content=content,
            )
    return await _update_feishu_stream_reasoning(
        adapter, chat_id, message_id, content, mode=mode
    )


async def _update_feishu_stream_tool_event(
    adapter, chat_id, message_id, payload, *, mode: str, completed: bool
):
    """Update active/completed tool state on the streaming surface."""
    payload = payload if isinstance(payload, dict) else {"name": str(payload or "tool")}
    tool_name = str(payload.get("name") or payload.get("tool_name") or "tool")
    if mode == "card":
        method_name = (
            "update_streaming_card_tool_completed"
            if completed
            else "update_streaming_card_tool_started"
        )
        updater = getattr(adapter, method_name, None)
        if callable(updater):
            if completed:
                return await updater(
                    chat_id=chat_id,
                    message_id=message_id,
                    tool_name=tool_name,
                    duration=payload.get("duration"),
                    is_error=bool(payload.get("is_error")),
                )
            return await updater(
                chat_id=chat_id,
                message_id=message_id,
                tool_name=tool_name,
                preview=payload.get("preview"),
                args=payload.get("args"),
            )

    status = (
        f"✅ 工具完成: {tool_name}"
        if completed and not payload.get("is_error")
        else f"⚠️ 工具失败: {tool_name}"
        if completed
        else f"🔧 正在调用工具: {tool_name}"
    )
    return await _update_feishu_stream_target(
        adapter, chat_id, message_id, status, mode=mode
    )


# Throttle edit_message calls. Hermes mainstream uses 1.5s between edits
# (run.py:9502 _PROGRESS_EDIT_INTERVAL); we mirror that as the floor for the
# content phase. CardKit reasoning can update more often because it streams
# into a stable card element; legacy edit_message keeps the wider heartbeat.
# Streaming card flush throttle (tuned 2026-05-12 for remote-RTT UX).
# CardKit only exposes cardElement.content as the streaming endpoint, and
# every call fully replaces the rendered markdown — high-frequency flushes
# cause visible flicker when the gateway sits an RTT away from open.feishu.cn.
_STREAM_CONTENT_MIN_CHARS = 120
_STREAM_CONTENT_MIN_SECONDS = 2.0
_STREAM_THINKING_MIN_SECONDS = 2.0
_STREAM_CARD_REASONING_MIN_CHARS = 100
_STREAM_CARD_REASONING_MIN_SECONDS = 2.0
_STREAM_CARD_PRIME_STATUS = "Hermes 正在准备响应..."
_STREAM_CARD_IDLE_HEARTBEAT_SECONDS = 2.5
_STREAM_ABORT_FALLBACK = "Aborted."
_STREAM_MAX_VISIBLE_CHARS = 3_000
_STREAM_TRUNCATION_SUFFIX = "\n\n...[已截断: 回复过长，已保留前半部分以保证卡片及时完成]"


def _stream_card_idle_status(tick: int) -> str:
    """Return a visibly changing pre-token status for CardKit typewriter keepalive."""
    dots = "." * (3 + (tick % 3))
    return f"Hermes 正在准备响应{dots}"


async def _stream_into_feishu_shared_consumer(
    adapter, chat_id, profile_name, profile_home, event, *, messages: Optional[list[dict]] = None
) -> Optional[str]:
    """Stream Feishu card output through Hermes' shared GatewayStreamConsumer.

    Returns None when the shared card surface cannot be started, allowing the
    caller to fall back to the legacy text-edit transport.
    """
    if GatewayStreamConsumer is None or StreamConsumerConfig is None:
        return None

    import time
    from .agent_real import stream_run_agent, real_run_agent
    from .runtime import _PROFILE_HOME_VAR

    stream_started_at = time.monotonic()
    consumer = GatewayStreamConsumer(
        adapter,
        chat_id,
        StreamConsumerConfig(
            edit_interval=_STREAM_CONTENT_MIN_SECONDS,
            buffer_threshold=_STREAM_CONTENT_MIN_CHARS,
            cursor=" ▉",
        ),
        metadata=None,
    )
    consumer_task: Optional[asyncio.Task] = None
    idle_heartbeat_task: Optional[asyncio.Task] = None
    terminal_update_sent = False
    first_agent_event_seen = False
    content_delta_seen = False
    content = ""
    thinking = ""
    last_reasoning_edit = 0.0
    last_reasoning_len = 0

    async def _idle_card_heartbeat() -> None:
        tick = 1
        while True:
            await asyncio.sleep(_STREAM_CARD_IDLE_HEARTBEAT_SECONDS)
            if first_agent_event_seen or content_delta_seen:
                return
            try:
                await consumer.update_streaming_card_status(_stream_card_idle_status(tick))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("multitenancy: shared card idle heartbeat failed: %s", exc)
            tick += 1

    async def _stop_idle_card_heartbeat() -> None:
        if idle_heartbeat_task is None or idle_heartbeat_task.done():
            return
        idle_heartbeat_task.cancel()
        try:
            await idle_heartbeat_task
        except asyncio.CancelledError:
            pass

    def _abort_content() -> str:
        raw = content if content else (thinking if thinking else _STREAM_ABORT_FALLBACK)
        return _clean_stream_display_text(raw, profile_home)

    async def _finish_consumer() -> None:
        if consumer_task is None:
            return
        consumer.finish()
        await consumer_task

    try:
        start_task = asyncio.create_task(consumer.ensure_streaming_card_started())
        try:
            started = await asyncio.shield(start_task)
        except asyncio.CancelledError:
            try:
                started = await start_task
            except Exception as exc:
                logger.debug("multitenancy: shared card start failed while cancelling: %s", exc)
                started = False
            if started:
                try:
                    await consumer.abort_streaming_card(_STREAM_ABORT_FALLBACK)
                except Exception as exc:
                    logger.debug("multitenancy: shared card abort-after-start failed: %s", exc)
            raise

        if not started:
            logger.debug("multitenancy: shared card start unavailable; falling back")
            return None

        logger.info(
            "multitenancy: shared stream card ready elapsed=%.3fs",
            time.monotonic() - stream_started_at,
        )
        consumer_task = asyncio.create_task(consumer.run())

        prime_task = asyncio.create_task(
            consumer.update_streaming_card_status(_STREAM_CARD_PRIME_STATUS)
        )
        try:
            await asyncio.shield(prime_task)
        except asyncio.CancelledError:
            try:
                await prime_task
            except Exception as exc:
                logger.debug("multitenancy: shared card prime failed while cancelling: %s", exc)
            try:
                await consumer.abort_streaming_card(_STREAM_ABORT_FALLBACK)
            except Exception as exc:
                logger.debug("multitenancy: shared card abort-after-prime failed: %s", exc)
            raise

        idle_heartbeat_task = asyncio.create_task(_idle_card_heartbeat())

        token = _PROFILE_HOME_VAR.set(profile_home)
        try:
            try:
                async for kind, delta in stream_run_agent(event, profile_home, messages=messages):
                    if not first_agent_event_seen:
                        first_agent_event_seen = True
                        logger.info(
                            "multitenancy: shared stream first agent event kind=%s total=%.3fs",
                            kind,
                            time.monotonic() - stream_started_at,
                        )

                    if kind == "thinking":
                        thinking += str(delta or "")
                        now = time.monotonic()
                        if (
                            not last_reasoning_len
                            or len(thinking) - last_reasoning_len >= _STREAM_CARD_REASONING_MIN_CHARS
                            or now - last_reasoning_edit >= _STREAM_CARD_REASONING_MIN_SECONDS
                        ):
                            await consumer.update_streaming_card_reasoning(thinking)
                            last_reasoning_len = len(thinking)
                            last_reasoning_edit = now
                        continue

                    if kind == "tool_started":
                        payload = delta if isinstance(delta, dict) else {"name": str(delta or "tool")}
                        await consumer.update_streaming_card_tool_started(
                            str(payload.get("name") or payload.get("tool_name") or "tool"),
                            preview=payload.get("preview"),
                            args=payload.get("args"),
                        )
                        continue

                    if kind == "tool_completed":
                        payload = delta if isinstance(delta, dict) else {"name": str(delta or "tool")}
                        await consumer.update_streaming_card_tool_completed(
                            str(payload.get("name") or payload.get("tool_name") or "tool"),
                            duration=payload.get("duration"),
                            is_error=bool(payload.get("is_error")),
                        )
                        continue

                    if kind == "approval_required":
                        await _handle_child_approval_required(adapter, chat_id, delta)
                        try:
                            await consumer.update_streaming_card_status("等待用户审批: /approve 或 /deny")
                        except Exception as exc:
                            logger.debug("multitenancy: approval status update failed: %s", exc)
                        continue

                    if kind == "approval_resolved":
                        if isinstance(delta, dict):
                            _clear_pending_approval(delta)
                        continue

                    if kind == "done":
                        continue

                    piece = str(delta or "")
                    if not piece:
                        continue
                    remaining = _STREAM_MAX_VISIBLE_CHARS - len(content)
                    if remaining <= 0:
                        continue
                    if len(piece) > remaining:
                        piece = piece[:remaining] + _STREAM_TRUNCATION_SUFFIX
                        content = content[:_STREAM_MAX_VISIBLE_CHARS] + _STREAM_TRUNCATION_SUFFIX
                        consumer.on_delta(_clean_stream_display_text(piece, profile_home))
                        content_delta_seen = True
                        logger.info(
                            "multitenancy: shared stream content truncated max_chars=%s",
                            _STREAM_MAX_VISIBLE_CHARS,
                        )
                        break
                    content += piece
                    consumer.on_delta(_clean_stream_display_text(piece, profile_home))
                    content_delta_seen = True
            except Exception as exc:
                logger.info("multitenancy: shared streaming failed (%s) — falling back to non-stream", exc)
                try:
                    content = await real_run_agent(event, profile_home, messages=messages)
                except Exception as fallback_exc:
                    logger.warning("multitenancy: LLM fully unavailable: %s", fallback_exc)
                    content = (
                        "⚠️ 模型暂时不可用 (LLM provider rejected the request).\n"
                        "请检查 profile 的 config.yaml 模型/凭据, 或稍后再试。"
                    )
                if not content_delta_seen:
                    consumer.on_delta(_clean_stream_display_text(content, profile_home))
                    content_delta_seen = True
        finally:
            _PROFILE_HOME_VAR.reset(token)
            await _stop_idle_card_heartbeat()

        full = content if content else (thinking if thinking else "(empty response)")
        if not content_delta_seen:
            consumer.on_delta(_clean_stream_display_text(full, profile_home))

        await _finish_consumer()
        terminal_update_sent = True
        return full
    except asyncio.CancelledError:
        await _stop_idle_card_heartbeat()
        if not terminal_update_sent:
            try:
                await _run_terminal_stream_update(
                    consumer.abort_streaming_card(_abort_content()),
                    label="shared stream abort update",
                )
            except asyncio.CancelledError:
                raise
            except Exception as abort_exc:
                logger.debug("multitenancy: shared stream abort update failed: %s", abort_exc)
        if consumer_task is not None:
            consumer.finish()
            try:
                await consumer_task
            except Exception:
                pass
        raise


async def _stream_into_feishu(
    adapter, chat_id, profile_name, profile_home, event, *, messages: Optional[list[dict]] = None
) -> str:
    """Stream LLM tokens into Feishu.

    Uses OpenClaw-style interactive card streaming when the shared Feishu
    adapter supports it. Falls back to the legacy text placeholder +
    ``edit_message`` loop for older adapters.

    Falls back to a single non-streamed send() if streaming returns empty or
    the initial stream target fails. Returns the final concatenated text.

    ``messages`` (optional): full conversation including prior history. When
    omitted the runner builds a single-turn system+user prompt from the event.
    """
    import time
    from .agent_real import stream_run_agent, real_run_agent
    from .runtime import _PROFILE_HOME_VAR

    stream_started_at = time.monotonic()

    # Without an adapter we can still produce text (used in unit tests).
    if adapter is None:
        token = _PROFILE_HOME_VAR.set(profile_home)
        try:
            content_parts: list[str] = []
            async for kind, c in stream_run_agent(event, profile_home, messages=messages):
                if kind == "content":
                    content_parts.append(c)
            return (
                "".join(content_parts)
                or await real_run_agent(event, profile_home, messages=messages)
            )
        finally:
            _PROFILE_HOME_VAR.reset(token)

    if _adapter_supports_streaming_card(adapter):
        shared_response = await _stream_into_feishu_shared_consumer(
            adapter,
            chat_id,
            profile_name,
            profile_home,
            event,
            messages=messages,
        )
        if shared_response is not None:
            return shared_response

    stream_mode = "edit"
    placeholder_id: Optional[str] = None
    target_ready_at = stream_started_at
    thinking = ""
    content = ""
    last_edit_time = 0.0
    last_render_len = 0
    last_reasoning_render_len = 0
    content_started = False
    first_agent_event_seen = False
    terminal_update_sent = False
    card_reasoning_sent = False
    idle_heartbeat_task: Optional[asyncio.Task] = None

    async def _idle_card_heartbeat() -> None:
        tick = 1
        while True:
            await asyncio.sleep(_STREAM_CARD_IDLE_HEARTBEAT_SECONDS)
            if first_agent_event_seen or content_started or placeholder_id is None:
                return
            try:
                await _update_feishu_stream_status(
                    adapter,
                    chat_id,
                    placeholder_id,
                    _stream_card_idle_status(tick),
                    mode=stream_mode,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("multitenancy: card idle heartbeat failed: %s", exc)
            tick += 1

    async def _stop_idle_card_heartbeat() -> None:
        if idle_heartbeat_task is None or idle_heartbeat_task.done():
            return
        idle_heartbeat_task.cancel()
        try:
            await idle_heartbeat_task
        except asyncio.CancelledError:
            pass

    def render() -> str:
        if content:
            return _clean_stream_display_text(content, profile_home)
        preview = thinking[-160:].strip() if thinking else ""
        return f"💭 思考中…\n{preview}" if preview else "💭 思考中…"

    def abort_content() -> str:
        raw = content if content else (thinking if thinking else _STREAM_ABORT_FALLBACK)
        return _clean_stream_display_text(raw, profile_home)

    try:
        # Create/send can complete remotely after this task is cancelled. Shield
        # it so we can still obtain the message_id and close the card instead of
        # leaving a Generating card behind.
        start_task = asyncio.create_task(_start_feishu_stream_target(adapter, chat_id))
        try:
            stream_mode, placeholder_id = await asyncio.shield(start_task)
        except asyncio.CancelledError:
            try:
                stream_mode, placeholder_id = await start_task
                logger.info(
                    "multitenancy: stream target start completed while task was cancelling "
                    "mode=%s message_id=%s",
                    stream_mode,
                    placeholder_id,
                )
            except Exception as exc:
                logger.debug("multitenancy: stream target start failed while cancelling: %s", exc)
            raise

        target_ready_at = time.monotonic()
        logger.info(
            "multitenancy: stream target ready mode=%s message_id=%s elapsed=%.3fs",
            stream_mode,
            placeholder_id,
            target_ready_at - stream_started_at,
        )

        if placeholder_id is None:
            # Couldn't get a message to edit — degrade to one-shot non-stream.
            text = await real_run_agent(event, profile_home, messages=messages)
            await adapter.send(chat_id, text)
            return text

        if stream_mode == "card":
            try:
                await _update_feishu_stream_status(
                    adapter,
                    chat_id,
                    placeholder_id,
                    _STREAM_CARD_PRIME_STATUS,
                    mode=stream_mode,
                )
                logger.info(
                    "multitenancy: stream card primed message_id=%s elapsed=%.3fs",
                    placeholder_id,
                    time.monotonic() - target_ready_at,
                )
                idle_heartbeat_task = asyncio.create_task(_idle_card_heartbeat())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("multitenancy: card prime update failed: %s", exc)

        token = _PROFILE_HOME_VAR.set(profile_home)
        try:
            try:
                async for kind, delta in stream_run_agent(event, profile_home, messages=messages):
                    if not first_agent_event_seen:
                        first_agent_event_seen = True
                        logger.info(
                            "multitenancy: stream first agent event kind=%s "
                            "since_target=%.3fs total=%.3fs",
                            kind,
                            time.monotonic() - target_ready_at,
                            time.monotonic() - stream_started_at,
                        )
                    if kind == "thinking":
                        thinking += str(delta or "")
                        if stream_mode == "card" and thinking:
                            now = time.monotonic()
                            should_update_card_reasoning = (
                                not card_reasoning_sent
                                or len(thinking) - last_reasoning_render_len >= _STREAM_CARD_REASONING_MIN_CHARS
                                or now - last_edit_time >= _STREAM_CARD_REASONING_MIN_SECONDS
                            )
                            if should_update_card_reasoning:
                                try:
                                    await _update_feishu_stream_reasoning(
                                        adapter,
                                        chat_id,
                                        placeholder_id,
                                        thinking,
                                        mode=stream_mode,
                                    )
                                    card_reasoning_sent = True
                                    last_reasoning_render_len = len(thinking)
                                except Exception as exc:
                                    logger.debug("multitenancy: card reasoning update failed: %s", exc)
                                last_edit_time = now
                                last_render_len = len(render())
                            continue
                    elif kind == "tool_started":
                        try:
                            await _update_feishu_stream_tool_event(
                                adapter,
                                chat_id,
                                placeholder_id,
                                delta,
                                mode=stream_mode,
                                completed=False,
                            )
                        except Exception as exc:
                            logger.debug("multitenancy: card tool-start update failed: %s", exc)
                        last_edit_time = time.monotonic()
                        last_render_len = len(render())
                        continue
                    elif kind == "tool_completed":
                        try:
                            await _update_feishu_stream_tool_event(
                                adapter,
                                chat_id,
                                placeholder_id,
                                delta,
                                mode=stream_mode,
                                completed=True,
                            )
                        except Exception as exc:
                            logger.debug("multitenancy: card tool-complete update failed: %s", exc)
                        last_edit_time = time.monotonic()
                        last_render_len = len(render())
                        continue
                    elif kind == "approval_required":
                        await _handle_child_approval_required(adapter, chat_id, delta)
                        try:
                            await _update_feishu_stream_status(
                                adapter,
                                chat_id,
                                placeholder_id,
                                "等待用户审批: /approve 或 /deny",
                                mode=stream_mode,
                            )
                        except Exception as exc:
                            logger.debug("multitenancy: approval status update failed: %s", exc)
                        last_edit_time = time.monotonic()
                        last_render_len = len(render())
                        continue
                    elif kind == "approval_resolved":
                        if isinstance(delta, dict):
                            _clear_pending_approval(delta)
                        continue
                    elif kind == "done":
                        continue
                    else:
                        content += str(delta or "")
                        if len(content) > _STREAM_MAX_VISIBLE_CHARS:
                            content = (
                                content[:_STREAM_MAX_VISIBLE_CHARS]
                                + _STREAM_TRUNCATION_SUFFIX
                            )
                            logger.info(
                                "multitenancy: stream content truncated and finalized "
                                "message_id=%s max_chars=%s",
                                placeholder_id,
                                _STREAM_MAX_VISIBLE_CHARS,
                            )
                            break
                        if not content_started:
                            # Force an immediate edit on phase transition so the user
                            # sees the answer start the moment reasoning ends.
                            content_started = True
                            try:
                                await _update_feishu_stream_target(
                                    adapter,
                                    chat_id,
                                    placeholder_id,
                                    render(),
                                    mode=stream_mode,
                                )
                            except Exception as exc:
                                logger.debug(
                                    "multitenancy: phase-transition stream update failed: %s",
                                    exc,
                                )
                            last_edit_time = time.monotonic()
                            last_render_len = len(render())
                            continue

                    now = time.monotonic()
                    rendered = render()
                    if content_started:
                        should_edit = (
                            len(rendered) - last_render_len >= _STREAM_CONTENT_MIN_CHARS
                            or now - last_edit_time >= _STREAM_CONTENT_MIN_SECONDS
                        )
                    else:
                        # Reasoning phase — heartbeat-only edits, no char threshold.
                        should_edit = now - last_edit_time >= _STREAM_THINKING_MIN_SECONDS
                    if should_edit:
                        try:
                            await _update_feishu_stream_target(
                                adapter,
                                chat_id,
                                placeholder_id,
                                rendered,
                                mode=stream_mode,
                            )
                        except Exception as exc:
                            logger.debug("multitenancy: stream update mid-stream failed: %s", exc)
                        last_edit_time = now
                        last_render_len = len(rendered)
            except Exception as exc:
                logger.info("multitenancy: streaming failed (%s) — falling back to non-stream", exc)
                try:
                    content = await real_run_agent(event, profile_home, messages=messages)
                except Exception as fallback_exc:
                    # Both stream + non-stream LLM paths failed (e.g. region block,
                    # exhausted credentials). Surface a user-visible error instead
                    # of leaving the "..." placeholder hanging.
                    logger.warning("multitenancy: LLM fully unavailable: %s", fallback_exc)
                    content = (
                        "⚠️ 模型暂时不可用 (LLM provider rejected the request).\n"
                        "请检查 profile 的 config.yaml 模型/凭据, 或稍后再试。"
                    )
        finally:
            _PROFILE_HOME_VAR.reset(token)
            await _stop_idle_card_heartbeat()

        full = content if content else (thinking if thinking else "(empty response)")
        display_full = _clean_stream_display_text(full, profile_home)

        # 3. Final commit. finalize=True signals end of stream to Feishu.
        try:
            await _run_terminal_stream_update(
                _update_feishu_stream_target(
                    adapter,
                    chat_id,
                    placeholder_id,
                    display_full,
                    mode=stream_mode,
                    finalize=True,
                ),
                label="stream final update",
            )
            terminal_update_sent = True
        except Exception as exc:
            logger.debug("multitenancy: final stream update failed: %s", exc)

        return full
    except asyncio.CancelledError:
        if placeholder_id is not None and not terminal_update_sent:
            full = abort_content()
            logger.info(
                "multitenancy: stream cancelled; aborting target mode=%s message_id=%s content_len=%s",
                stream_mode,
                placeholder_id,
                len(full),
            )
            try:
                await _run_terminal_stream_update(
                    _abort_feishu_stream_target(
                        adapter,
                        chat_id,
                        placeholder_id,
                        full,
                        mode=stream_mode,
                    ),
                    label="stream abort update",
                )
            except asyncio.CancelledError:
                raise
            except Exception as abort_exc:
                logger.debug("multitenancy: stream abort update failed: %s", abort_exc)
        raise


def _log_task_failure(task: asyncio.Task) -> None:
    """Done-callback for fire-and-forget tasks — surfaces silent exceptions."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("multitenancy: background task crashed: %r", exc)
