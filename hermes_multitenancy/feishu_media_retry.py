"""Inbound Feishu media retry patch.

The two wrapped inbound download methods return ``("", "")`` on failure and
never raise, so there is nothing to classify here. Only the first failing item
per message may spend an immediate second call with zero backoff. The outbound
document downloader used by ``send_animation`` is intentionally left alone, and
the core adapter stays untouched.
"""
from __future__ import annotations

import collections
import functools
import logging
from typing import Any

from .feishu_adapter_compat import load_feishu_adapter, log_feishu_adapter_load_error

logger = logging.getLogger(__name__)

_PATCH_FLAG = "_media_retry_patched"
# Only inbound downloads. The outbound document downloader used by send_animation stays unpatched.
_TARGET_METHODS = ("_download_feishu_image", "_download_feishu_message_resource")

# Per-message retry budget: message_ids that have already spent their single retry.
# Bounded LRU (same convention as _welcomed_chats / _AUDITED_EVERYONE_MSG_IDS) so it never
# grows unbounded over the process lifetime.
# ponytail: loop-thread-only. _consume_retry_budget is a sync check-and-set with no await, so it is
# atomic under the event loop; the inbound download path runs on the adapter loop (not the SDK
# callback thread). Add a threading.Lock only if a cross-thread caller ever appears.
_RETRY_BUDGET_MAX = 1024
_spent_retry_message_ids: "collections.OrderedDict[str, None]" = collections.OrderedDict()

_INSTALLED = False


def _is_empty_media_result(result: Any) -> bool:
    return isinstance(result, tuple) and len(result) == 2 and not result[0]


def _consume_retry_budget(message_id: str) -> bool:
    """Return True iff this message_id may spend its single retry now.

    First call for a message_id records it and returns True; any later call for the same
    message_id (a subsequent failing item in the same message) returns False.
    """
    if not message_id:
        return False
    if message_id in _spent_retry_message_ids:
        return False
    _spent_retry_message_ids[message_id] = None
    while len(_spent_retry_message_ids) > _RETRY_BUDGET_MAX:
        _spent_retry_message_ids.popitem(last=False)
    return True


def _wrap_with_retry(original: Any) -> Any:
    @functools.wraps(original)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original(self, *args, **kwargs)
        if not _is_empty_media_result(result):
            return result
        message_id = str(kwargs.get("message_id") or "")
        if not _consume_retry_budget(message_id):
            return result
        logger.info(
            "[multitenancy] inbound media %s returned empty for message_id=%s; "
            "retrying once (zero backoff)",
            getattr(original, "__name__", "?"),
            message_id,
        )
        return await original(self, *args, **kwargs)

    setattr(wrapped, _PATCH_FLAG, True)
    return wrapped


def install_feishu_media_retry_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    try:
        FeishuAdapter = load_feishu_adapter()
    except Exception as exc:
        log_feishu_adapter_load_error(
            logger,
            "[multitenancy] FeishuAdapter not importable yet; media-download retry deferred",
            exc,
        )
        return
    patched = []
    for name in _TARGET_METHODS:
        original = getattr(FeishuAdapter, name, None)
        if original is None:
            logger.warning(
                "[multitenancy] FeishuAdapter has no %s; media retry skipped for it", name
            )
            continue
        if getattr(original, _PATCH_FLAG, False):
            patched.append(name)
            continue
        setattr(FeishuAdapter, name, _wrap_with_retry(original))
        patched.append(name)
    _INSTALLED = True
    logger.info(
        "[multitenancy] installed inbound media-download retry on %s.FeishuAdapter methods=%s",
        FeishuAdapter.__module__,
        patched,
    )
