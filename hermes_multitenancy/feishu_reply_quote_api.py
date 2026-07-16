"""Enrich Feishu reply/quote text via the replying user's UAT message lookup.

Core Feishu inbound processing already exposes the replied-to parent message id
(``parent_id`` / ``upper_message_id``) before it calls
``FeishuAdapter._fetch_message_text``. This plugin-only patch mirrors the
existing merge-forward API machinery:

- ``_process_inbound_message`` captures the replying user's ``sender_id.open_id``
  keyed by the parent message id.
- ``_fetch_message_text`` detects those captured parent ids, fetches the full
  parent message through ``GET /open-apis/im/v1/messages/{message_id}`` using
  that replying user's UAT, and returns an agent-visible
  ``[message_id=<id>] <sender>: <full content>`` string.
- If the parent message carries image/file resources, the patch downloads them
  through the adapter's bot/app message-resource path and attaches the cached
  local paths to the inbound event before Hermes' native media preprocessing.

Any UAT/API failure is fail-open: the wrapper falls back to core's original
bot-token lookup so inbound delivery never breaks.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Any, Optional

from .feishu_adapter_compat import (
    load_feishu_adapter,
    load_normalize_feishu_message,
    log_feishu_adapter_load_error,
)
# Intentional private-helper reuse: keep UAT, host, normalization, and contact
# lookup behavior aligned with the proven merge-forward implementation.
from .feishu_merge_forward_api import (
    _feishu_api_host,
    _item_sender_open_id,
    _item_text,
    _resolve_names_blocking,
    _resolve_uat_token,
)

logger = logging.getLogger(__name__)

_HOOK_INSTALLED = False
_PROCESS_FLAG = "_hermes_multitenancy_reply_quote_sender_capture_patched"
_FETCH_FLAG = "_hermes_multitenancy_reply_quote_fetch_patched"
_DISPATCH_FLAG = "_hermes_multitenancy_reply_quote_media_dispatch_patched"
_TEXT_BATCH_FLAG = "_hermes_multitenancy_reply_quote_media_text_batch_patched"
_REPLY_SENDER_ATTR = "_mt_reply_quote_sender_by_parent_mid"
_REPLY_MEDIA_ATTR = "_mt_reply_quote_media_by_parent_mid"
_REPLY_SENDER_MAP_MAX = 256
_QUOTE_MEDIA_MAX = 8
_CARD_PREVIEW_KEY_RE = re.compile(r"\bimg_v\d+_[A-Za-z0-9_-]+\b")
_CARD_PREVIEW_HINTS = ("请升级至最新版本客户端", "update to the latest version")


def _reply_sender_map(adapter: Any) -> dict[str, str]:
    mapping = getattr(adapter, _REPLY_SENDER_ATTR, None)
    if not isinstance(mapping, dict):
        mapping = {}
        setattr(adapter, _REPLY_SENDER_ATTR, mapping)
    return mapping


def _reply_media_map(adapter: Any) -> dict[str, list[tuple[str, str]]]:
    mapping = getattr(adapter, _REPLY_MEDIA_ATTR, None)
    if not isinstance(mapping, dict):
        mapping = {}
        setattr(adapter, _REPLY_MEDIA_ATTR, mapping)
    return mapping


def _event_media_pairs(event: Any) -> list[tuple[str, str]]:
    urls = list(getattr(event, "media_urls", None) or [])
    media_types = list(getattr(event, "media_types", None) or [])
    pairs: list[tuple[str, str]] = []
    for index, url in enumerate(urls):
        path = str(url or "").strip()
        if not path:
            continue
        media_type = str(media_types[index] if index < len(media_types) else "" or "application/octet-stream")
        pairs.append((path, media_type))
    return pairs


def _append_event_media(event: Any, media: list[tuple[str, str]]) -> int:
    if not media:
        return 0
    existing_urls = list(getattr(event, "media_urls", None) or [])
    existing_types = list(getattr(event, "media_types", None) or [])
    while len(existing_types) < len(existing_urls):
        existing_types.append("application/octet-stream")
    seen = set(existing_urls)
    added = 0
    for path, media_type in media:
        if path and path not in seen:
            existing_urls.append(path)
            existing_types.append(media_type or "application/octet-stream")
            seen.add(path)
            added += 1
    if added:
        setattr(event, "media_urls", existing_urls)
        setattr(event, "media_types", existing_types)
    return added


def _reply_prefix(message_id: str) -> str:
    return f"[message_id={message_id}]"


def _format_reply_quote_text(message_id: str, sender_name: str, full_content: str) -> Optional[str]:
    content = str(full_content or "").strip()
    if not content:
        return None
    prefix = _reply_prefix(message_id)
    if content.startswith(prefix):
        return content
    sender = str(sender_name or "").strip()
    if sender:
        return f"{prefix} {sender}: {content}"
    return f"{prefix} {content}"


def _sanitize_degraded_preview(message_id: str, value: Any) -> Any:
    text = str(value or "")
    lowered = text.lower()
    if _CARD_PREVIEW_KEY_RE.search(text) and any(hint.lower() in lowered for hint in _CARD_PREVIEW_HINTS):
        return _format_reply_quote_text(message_id, "", "[interactive 消息内容暂不可用]")
    return value


def _fetch_parent_message_blocking(message_id: str, replying_open_id: str) -> Optional[dict[str, Any]]:
    if not message_id:
        raise RuntimeError("reply quote enrichment requires parent message_id")
    token = _resolve_uat_token(replying_open_id)
    url = (
        f"{_feishu_api_host()}/open-apis/im/v1/messages/"
        f"{urllib.parse.quote(message_id, safe='')}?"
        f"{urllib.parse.urlencode({'user_id_type': 'open_id', 'card_msg_content_type': 'raw_card_content'})}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (fixed host)
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("code") != 0:
        raise RuntimeError(f"im/v1/messages code={body.get('code')} msg={body.get('msg')}")
    items = list((body.get("data") or {}).get("items") or [])
    return items[0] if items else None


async def _fetch_parent_message(message_id: str, replying_open_id: str) -> Optional[dict[str, Any]]:
    return await asyncio.to_thread(_fetch_parent_message_blocking, message_id, replying_open_id)


def _item_raw_content(item: dict[str, Any]) -> str:
    body = item.get("body") or {}
    raw_content = body.get("content") if isinstance(body, dict) else ""
    if isinstance(raw_content, (dict, list)):
        raw_content = json.dumps(raw_content, ensure_ascii=False)
    return str(raw_content or "")


def _load_item_payload(item: dict[str, Any]) -> Any:
    raw_content = _item_raw_content(item)
    try:
        return json.loads(raw_content) if raw_content else {}
    except Exception:
        return {}


def _collect_nested_strings(value: Any, key: str) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            found.append(raw.strip())
        for child in value.values():
            found.extend(_collect_nested_strings(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(_collect_nested_strings(child, key))
    return found


def _unique_strings(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def _normalize_parent_item(item: dict[str, Any]) -> Any:
    try:
        return load_normalize_feishu_message()(
            message_type=str(item.get("msg_type") or ""),
            raw_content=_item_raw_content(item),
            mentions=item.get("mentions"),
        )
    except Exception:
        return None


def _parent_image_keys(item: dict[str, Any], normalized: Any) -> list[str]:
    keys = [str(value or "").strip() for value in (getattr(normalized, "image_keys", None) or [])]
    if not keys:
        keys = _collect_nested_strings(_load_item_payload(item), "image_key")
    return _unique_strings([value for value in keys if value])


def _media_ref_value(ref: Any, key: str) -> str:
    if isinstance(ref, dict):
        return str(ref.get(key) or "").strip()
    return str(getattr(ref, key, "") or "").strip()


def _parent_media_refs(item: dict[str, Any], normalized: Any) -> list[tuple[str, str, str]]:
    refs: list[tuple[str, str, str]] = []
    for ref in getattr(normalized, "media_refs", None) or []:
        file_key = _media_ref_value(ref, "file_key")
        if not file_key:
            continue
        resource_type = _media_ref_value(ref, "resource_type") or "file"
        file_name = _media_ref_value(ref, "file_name")
        refs.append((file_key, resource_type, file_name))
    if not refs:
        payload = _load_item_payload(item)
        for file_key in _collect_nested_strings(payload, "file_key"):
            refs.append((file_key, "file", ""))
    unique: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for file_key, resource_type, file_name in refs:
        key = (file_key, resource_type)
        if key not in seen:
            unique.append((file_key, resource_type, file_name))
            seen.add(key)
    return unique


async def _download_reply_quote_media(adapter: Any, message_id: str, item: dict[str, Any]) -> list[tuple[str, str]]:
    if str(item.get("msg_type") or "").strip().lower() in {"interactive", "card"}:
        return []
    normalized = _normalize_parent_item(item)
    media: list[tuple[str, str]] = []

    download_image = getattr(adapter, "_download_feishu_image", None)
    if callable(download_image):
        for image_key in _parent_image_keys(item, normalized):
            if len(media) >= _QUOTE_MEDIA_MAX:
                break
            try:
                cached_path, media_type = await download_image(message_id=message_id, image_key=image_key)
            except Exception:
                logger.debug("[multitenancy] reply quote image download failed (mid=%s)", message_id, exc_info=True)
                continue
            if cached_path:
                media.append((str(cached_path), str(media_type or "image/jpeg")))

    download_resource = getattr(adapter, "_download_feishu_message_resource", None)
    if callable(download_resource):
        for file_key, resource_type, file_name in _parent_media_refs(item, normalized):
            if len(media) >= _QUOTE_MEDIA_MAX:
                break
            try:
                cached_path, media_type = await download_resource(
                    message_id=message_id,
                    file_key=file_key,
                    resource_type=resource_type,
                    fallback_filename=file_name,
                )
            except Exception:
                logger.debug(
                    "[multitenancy] reply quote resource download failed (mid=%s key=%s)",
                    message_id,
                    file_key,
                    exc_info=True,
                )
                continue
            if cached_path:
                media.append((str(cached_path), str(media_type or "application/octet-stream")))

    deduped: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    for path, media_type in media:
        if path and path not in seen_paths:
            deduped.append((path, media_type))
            seen_paths.add(path)
    return deduped


def _append_quote_media_notes(text: Optional[str], media: list[tuple[str, str]]) -> Optional[str]:
    if not media:
        return text
    lines: list[str] = [text] if text else []
    for path, _media_type in media:
        lines.append(f"[Quoted media saved at: {path}]")
    return "\n".join(line for line in lines if line)


async def _build_reply_quote_context(
    adapter: Any,
    message_id: str,
    replying_open_id: str,
) -> tuple[Optional[str], list[tuple[str, str]]]:
    item = await _fetch_parent_message(message_id, replying_open_id)
    if not item:
        return None, []
    full_content = str(_item_text(item) or "").strip()
    sender_name = ""
    parent_sender_open_id = _item_sender_open_id(item)
    if parent_sender_open_id:
        try:
            names = await asyncio.to_thread(
                _resolve_names_blocking,
                [parent_sender_open_id],
                replying_open_id,
            )
            sender_name = str((names or {}).get(parent_sender_open_id) or "").strip()
        except Exception:
            logger.debug("[multitenancy] reply quote sender lookup failed", exc_info=True)
    text = _format_reply_quote_text(message_id, sender_name, full_content)
    media = await _download_reply_quote_media(adapter, message_id, item)
    return text, media


async def _build_reply_quote_text(message_id: str, replying_open_id: str) -> Optional[str]:
    text, _media = await _build_reply_quote_context(None, message_id, replying_open_id)
    return text


def install_feishu_reply_quote_api_patch() -> None:
    """Idempotently patch Feishu reply/quote lookup with UAT-backed enrichment."""
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    try:
        FeishuAdapter = load_feishu_adapter()
    except Exception as exc:
        log_feishu_adapter_load_error(
            logger,
            "[multitenancy] FeishuAdapter not importable yet; reply quote API patch deferred",
            exc,
        )
        return
    _patch_process_inbound(FeishuAdapter)
    _patch_fetch_message_text(FeishuAdapter)
    _patch_dispatch_inbound_event(FeishuAdapter)
    _patch_enqueue_text_event(FeishuAdapter)
    _HOOK_INSTALLED = True


def _patch_process_inbound(FeishuAdapter: Any) -> None:
    original = getattr(FeishuAdapter, "_process_inbound_message", None)
    if original is None or getattr(original, _PROCESS_FLAG, False):
        return

    @functools.wraps(original)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            message = kwargs.get("message")
            sender_id = kwargs.get("sender_id")
            reply_to_message_id = (
                getattr(message, "parent_id", None)
                or getattr(message, "upper_message_id", None)
                or getattr(message, "root_id", None)
                or ""
            )
            replying_open_id = str(getattr(sender_id, "open_id", "") or "")
            if reply_to_message_id and replying_open_id:
                mapping = _reply_sender_map(self)
                if len(mapping) >= _REPLY_SENDER_MAP_MAX:
                    mapping.clear()
                mapping[str(reply_to_message_id)] = replying_open_id
        except Exception:
            logger.debug("[multitenancy] reply quote sender capture failed", exc_info=True)
        return await original(self, *args, **kwargs)

    setattr(wrapped, _PROCESS_FLAG, True)
    FeishuAdapter._process_inbound_message = wrapped


def _patch_fetch_message_text(FeishuAdapter: Any) -> None:
    original = getattr(FeishuAdapter, "_fetch_message_text", None)
    if original is None or getattr(original, _FETCH_FLAG, False):
        return

    @functools.wraps(original)
    async def wrapped(self: Any, message_id: str, *args: Any, **kwargs: Any) -> Any:
        normalized_message_id = str(message_id or "")
        if not normalized_message_id:
            return await original(self, message_id, *args, **kwargs)

        replying_open_id = str(_reply_sender_map(self).pop(normalized_message_id, "") or "")
        if not replying_open_id:
            return await original(self, message_id, *args, **kwargs)

        try:
            enriched, media = await _build_reply_quote_context(self, normalized_message_id, replying_open_id)
            if media:
                media_mapping = _reply_media_map(self)
                if len(media_mapping) >= _REPLY_SENDER_MAP_MAX:
                    media_mapping.clear()
                media_mapping[normalized_message_id] = media
                enriched = _append_quote_media_notes(enriched, media)
            if enriched:
                logger.info(
                    "[multitenancy] reply quote enriched via UAT API (mid=%s, %d chars, media=%d)",
                    normalized_message_id,
                    len(enriched),
                    len(media),
                )
                return enriched
        except Exception as exc:
            logger.debug(
                "[multitenancy] reply quote UAT fetch failed for %s: %s",
                normalized_message_id,
                exc,
                exc_info=True,
            )
        fallback = await original(self, message_id, *args, **kwargs)
        return _sanitize_degraded_preview(normalized_message_id, fallback)

    setattr(wrapped, _FETCH_FLAG, True)
    FeishuAdapter._fetch_message_text = wrapped
    logger.info("[multitenancy] installed reply quote UAT-API enrichment on FeishuAdapter")


def _patch_dispatch_inbound_event(FeishuAdapter: Any) -> None:
    original = getattr(FeishuAdapter, "_dispatch_inbound_event", None)
    if original is None or getattr(original, _DISPATCH_FLAG, False):
        return

    @functools.wraps(original)
    async def wrapped(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            reply_to_message_id = str(getattr(event, "reply_to_message_id", "") or "")
            media = _reply_media_map(self).pop(reply_to_message_id, []) if reply_to_message_id else []
            if media:
                added = _append_event_media(event, media)
                logger.info(
                    "[multitenancy] attached reply quote media to inbound event (reply_mid=%s, media=%d)",
                    reply_to_message_id,
                    added,
                )
        except Exception:
            logger.debug("[multitenancy] reply quote media dispatch attachment failed", exc_info=True)
        return await original(self, event, *args, **kwargs)

    setattr(wrapped, _DISPATCH_FLAG, True)
    FeishuAdapter._dispatch_inbound_event = wrapped


def _patch_enqueue_text_event(FeishuAdapter: Any) -> None:
    original = getattr(FeishuAdapter, "_enqueue_text_event", None)
    if original is None or getattr(original, _TEXT_BATCH_FLAG, False):
        return

    @functools.wraps(original)
    async def wrapped(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        incoming_media = _event_media_pairs(event)
        batch_key = None
        before_existing = None
        if incoming_media:
            try:
                pending_batches = getattr(self, "_pending_text_batches", None)
                text_batch_key = getattr(self, "_text_batch_key", None)
                if isinstance(pending_batches, dict) and callable(text_batch_key):
                    batch_key = text_batch_key(event)
                    before_existing = pending_batches.get(batch_key)
            except Exception:
                logger.debug("[multitenancy] reply quote text batch media probe failed", exc_info=True)

        result = await original(self, event, *args, **kwargs)

        if incoming_media and batch_key is not None and before_existing is not None:
            try:
                pending_batches = getattr(self, "_pending_text_batches", None)
                after_existing = pending_batches.get(batch_key) if isinstance(pending_batches, dict) else None
                if after_existing is before_existing:
                    added = _append_event_media(after_existing, incoming_media)
                    if added:
                        logger.info(
                            "[multitenancy] preserved reply quote media through text batch merge (media=%d)",
                            added,
                        )
            except Exception:
                logger.debug("[multitenancy] reply quote text batch media preservation failed", exc_info=True)
        return result

    setattr(wrapped, _TEXT_BATCH_FLAG, True)
    FeishuAdapter._enqueue_text_event = wrapped
