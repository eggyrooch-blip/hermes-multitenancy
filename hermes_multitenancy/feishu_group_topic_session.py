"""Canonical Feishu group-topic identity before router dispatch."""
from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from typing import Any, Optional

from .feishu_adapter_compat import load_feishu_adapter, log_feishu_adapter_load_error
from .runtime import strict_context_enabled

logger = logging.getLogger(__name__)

_HOOK_INSTALLED = False
_PROCESS_FLAG = "_hermes_multitenancy_group_topic_process_patched"
_DISPATCH_FLAG = "_hermes_multitenancy_group_topic_dispatch_patched"
_TEXT_BATCH_FLAG = "_hermes_multitenancy_group_topic_text_batch_patched"
_MEDIA_BATCH_FLAG = "_hermes_multitenancy_group_topic_media_batch_patched"
_PENDING_ATTR = "_mt_group_topic_by_message"
_CACHE_ATTR = "_mt_group_topic_by_root"
_CHAT_CAPABILITY_ATTR = "_mt_group_topic_chat_capability"
_MAP_MAX = 256


@dataclass(frozen=True)
class _PendingTopic:
    canonical_thread_id: Optional[str]
    raw_thread_id: Optional[str]
    root_id: Optional[str]
    is_topic: bool


def group_topic_thread(event: Any) -> Optional[str]:
    """Return an API-attested group-topic ID, never an inferred one."""
    source = getattr(event, "source", None)
    if source is None or getattr(source, "hermes_group_topic", False) is not True:
        return None
    chat_id = str(getattr(source, "chat_id", "") or "").strip()
    thread_id = str(getattr(source, "thread_id", "") or "").strip()
    return thread_id if chat_id and thread_id else None


def is_group_topic_event(event: Any) -> bool:
    return group_topic_thread(event) is not None


def is_shared_group_topic_event(event: Any) -> bool:
    return strict_context_enabled() and is_group_topic_event(event)


def group_topic_epoch_actor(event: Any, sender_open_id: str) -> str:
    return (
        group_topic_thread(event)
        if is_shared_group_topic_event(event)
        else None
    ) or sender_open_id or "unknown"


def _mapping(adapter: Any, attr: str) -> dict[str, Any]:
    value = getattr(adapter, attr, None)
    if not isinstance(value, dict):
        value = {}
        setattr(adapter, attr, value)
    return value


def _put_bounded(mapping: dict[str, Any], key: str, value: Any) -> None:
    if len(mapping) >= _MAP_MAX:
        mapping.clear()
    mapping[key] = value


async def _fetch_thread_id(adapter: Any, message_id: str, chat_id: str) -> Optional[str]:
    client = getattr(adapter, "_client", None)
    if client is None or not message_id or not chat_id:
        return None
    request = adapter._build_get_message_request(message_id)
    response = await adapter._run_blocking(client.im.v1.message.get, request)
    if not response or getattr(response, "success", lambda: False)() is False:
        logger.debug("[multitenancy] group-topic hydration unavailable")
        return None
    items = getattr(getattr(response, "data", None), "items", None) or []
    item = items[0] if items else None
    if item is None:
        logger.debug("[multitenancy] group-topic hydration unavailable")
        return None
    if str(getattr(item, "chat_id", "") or "") != chat_id:
        logger.debug("[multitenancy] group-topic hydration rejected (chat mismatch)")
        return None
    thread_id = str(getattr(item, "thread_id", "") or "").strip() or None
    if thread_id is None:
        logger.debug("[multitenancy] group-topic hydration unavailable")
    return thread_id


async def _is_topic_capable_chat(adapter: Any, chat_id: str) -> bool:
    cache = _mapping(adapter, _CHAT_CAPABILITY_ATTR)
    if chat_id in cache:
        return bool(cache[chat_id])
    client = getattr(adapter, "_client", None)
    if client is None or not chat_id:
        logger.debug("[multitenancy] group-topic capability unavailable")
        return False
    try:
        request = adapter._build_get_chat_request(chat_id)
        response = await adapter._run_blocking(client.im.v1.chat.get, request)
        if not response or getattr(response, "success", lambda: False)() is False:
            logger.debug("[multitenancy] group-topic capability unavailable")
            return False
        data = getattr(response, "data", None)
        chat_mode = str(getattr(data, "chat_mode", "") or "").strip().lower()
        group_message_type = str(
            getattr(data, "group_message_type", "") or ""
        ).strip().lower()
        capable = chat_mode == "topic" or group_message_type == "thread"
        _put_bounded(cache, chat_id, capable)
        return capable
    except Exception as exc:
        logger.debug(
            "[multitenancy] group-topic capability unavailable (%s)",
            type(exc).__name__,
        )
        return False


def install_feishu_group_topic_session_patch() -> None:
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    try:
        adapter_type = load_feishu_adapter()
    except Exception as exc:
        log_feishu_adapter_load_error(
            logger,
            "[multitenancy] FeishuAdapter not importable yet; group-topic patch deferred",
            exc,
        )
        return
    _patch_process_inbound(adapter_type)
    _patch_dispatch_inbound(adapter_type)
    _patch_batch_key(adapter_type, "_text_batch_key", _TEXT_BATCH_FLAG)
    _patch_batch_key(adapter_type, "_media_batch_key", _MEDIA_BATCH_FLAG)
    _HOOK_INSTALLED = True


def _patch_process_inbound(adapter_type: Any) -> None:
    original = getattr(adapter_type, "_process_inbound_message", None)
    if original is None or getattr(original, _PROCESS_FLAG, False):
        return

    @functools.wraps(original)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not strict_context_enabled():
            return await original(self, *args, **kwargs)
        message = kwargs.get("message")
        message_id = str(kwargs.get("message_id") or "")
        chat_type = str(kwargs.get("chat_type") or "").strip().lower()
        chat_id = str(getattr(message, "chat_id", "") or "")
        raw_thread_id = str(getattr(message, "thread_id", "") or "").strip() or None
        root_id = str(getattr(message, "root_id", "") or "").strip() or None
        topic_capable = (
            chat_type != "p2p"
            and await _is_topic_capable_chat(self, chat_id)
        )
        canonical_thread_id = raw_thread_id if topic_capable else None

        if topic_capable and not canonical_thread_id:
            cache_key = root_id or message_id
            cached = _mapping(self, _CACHE_ATTR).get(cache_key)
            if cached and cached[0] == chat_id:
                canonical_thread_id = cached[1]
            else:
                try:
                    canonical_thread_id = await _fetch_thread_id(self, message_id, chat_id)
                except Exception as exc:
                    logger.debug(
                        "[multitenancy] group-topic hydration failed (%s)",
                        type(exc).__name__,
                    )
            if canonical_thread_id and cache_key:
                _put_bounded(
                    _mapping(self, _CACHE_ATTR),
                    cache_key,
                    (chat_id, canonical_thread_id),
                )

        if canonical_thread_id and root_id:
            _put_bounded(
                _mapping(self, _CACHE_ATTR),
                root_id,
                (chat_id, canonical_thread_id),
            )
        if message_id:
            _mapping(self, _PENDING_ATTR)[message_id] = _PendingTopic(
                canonical_thread_id=canonical_thread_id,
                raw_thread_id=raw_thread_id,
                root_id=root_id,
                is_topic=bool(canonical_thread_id and topic_capable),
            )
        if canonical_thread_id:
            setattr(message, "thread_id", canonical_thread_id)
        try:
            return await original(self, *args, **kwargs)
        finally:
            _mapping(self, _PENDING_ATTR).pop(message_id, None)

    setattr(wrapped, _PROCESS_FLAG, True)
    adapter_type._process_inbound_message = wrapped


def _patch_dispatch_inbound(adapter_type: Any) -> None:
    original = getattr(adapter_type, "_dispatch_inbound_event", None)
    if original is None or getattr(original, _DISPATCH_FLAG, False):
        return

    @functools.wraps(original)
    async def wrapped(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        message_id = str(getattr(event, "message_id", "") or "")
        metadata = _mapping(self, _PENDING_ATTR).pop(message_id, None)
        source = getattr(event, "source", None)
        if metadata and source is not None:
            if metadata.canonical_thread_id:
                source.thread_id = metadata.canonical_thread_id
            source.hermes_group_topic = metadata.is_topic
            source.hermes_raw_thread_id = metadata.raw_thread_id
            source.hermes_root_id = metadata.root_id
        return await original(self, event, *args, **kwargs)

    setattr(wrapped, _DISPATCH_FLAG, True)
    adapter_type._dispatch_inbound_event = wrapped


def _patch_batch_key(adapter_type: Any, method_name: str, flag: str) -> None:
    original = getattr(adapter_type, method_name, None)
    if original is None or getattr(original, flag, False):
        return

    @functools.wraps(original)
    def wrapped(self: Any, event: Any, *args: Any, **kwargs: Any) -> str:
        key = str(original(self, event, *args, **kwargs))
        if not is_shared_group_topic_event(event):
            return key
        source = getattr(event, "source", None)
        sender = str(
            getattr(source, "user_id_alt", "")
            or getattr(source, "user_id", "")
            or "unknown"
        )
        return f"{key}:batch-sender:{sender}"

    setattr(wrapped, flag, True)
    setattr(adapter_type, method_name, wrapped)
