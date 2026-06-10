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

Any UAT/API failure is fail-open: the wrapper falls back to core's original
bot-token lookup so inbound delivery never breaks.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import urllib.parse
import urllib.request
from typing import Any, Optional

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
_REPLY_SENDER_ATTR = "_mt_reply_quote_sender_by_parent_mid"
_REPLY_SENDER_MAP_MAX = 256


def _reply_sender_map(adapter: Any) -> dict[str, str]:
    mapping = getattr(adapter, _REPLY_SENDER_ATTR, None)
    if not isinstance(mapping, dict):
        mapping = {}
        setattr(adapter, _REPLY_SENDER_ATTR, mapping)
    return mapping


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


def _fetch_parent_message_blocking(message_id: str, replying_open_id: str) -> Optional[dict[str, Any]]:
    if not message_id:
        raise RuntimeError("reply quote enrichment requires parent message_id")
    token = _resolve_uat_token(replying_open_id)
    url = (
        f"{_feishu_api_host()}/open-apis/im/v1/messages/"
        f"{urllib.parse.quote(message_id, safe='')}?user_id_type=open_id"
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


async def _build_reply_quote_text(message_id: str, replying_open_id: str) -> Optional[str]:
    item = await _fetch_parent_message(message_id, replying_open_id)
    if not item:
        return None
    full_content = str(_item_text(item) or "").strip()
    if not full_content:
        return None
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
    return _format_reply_quote_text(message_id, sender_name, full_content)


def install_feishu_reply_quote_api_patch() -> None:
    """Idempotently patch Feishu reply/quote lookup with UAT-backed enrichment."""
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    try:
        from gateway.platforms.feishu import FeishuAdapter  # type: ignore
    except Exception:
        logger.info("[multitenancy] FeishuAdapter not importable yet; reply quote API patch deferred")
        return
    _patch_process_inbound(FeishuAdapter)
    _patch_fetch_message_text(FeishuAdapter)
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
            enriched = await _build_reply_quote_text(normalized_message_id, replying_open_id)
            if enriched:
                logger.info(
                    "[multitenancy] reply quote enriched via UAT API (mid=%s, %d chars)",
                    normalized_message_id,
                    len(enriched),
                )
                return enriched
        except Exception as exc:
            logger.debug(
                "[multitenancy] reply quote UAT fetch failed for %s: %s",
                normalized_message_id,
                exc,
                exc_info=True,
            )
        return await original(self, message_id, *args, **kwargs)

    setattr(wrapped, _FETCH_FLAG, True)
    FeishuAdapter._fetch_message_text = wrapped
    logger.info("[multitenancy] installed reply quote UAT-API enrichment on FeishuAdapter")
