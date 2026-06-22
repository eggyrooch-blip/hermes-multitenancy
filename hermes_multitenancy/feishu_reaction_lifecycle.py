"""Issue 3 — keep the "Typing"/收到 reaction visible until the reply finishes.

The base FeishuAdapter wraps inbound processing with
``on_processing_start`` (add the Typing reaction) / ``on_processing_complete``
(remove it). When the multitenancy router OWNS the visible lifecycle, its
``on_pre_gateway_dispatch`` hook calls ``adapter.defer_processing_complete`` so
that the base's early ``on_processing_complete`` (fired right after the dispatch
it skipped) does NOT remove the reaction; the router then removes it from its
own ``finally`` via ``adapter.complete_deferred_processing`` — i.e. AFTER the
streaming reply is done.

Problem: the current UPSTREAM runtime core has NEITHER
``defer_processing_complete`` NOR ``complete_deferred_processing`` (owner's fork
had them; the editable switch to upstream dropped them). So the deferral is a
silent no-op: base adds the reaction, the skipped dispatch returns immediately,
base removes the reaction — a ~1s flash while the router is still streaming.

Fix (plugin-only monkeypatch): add the two missing methods to the upstream
adapter and make ``on_processing_complete`` a no-op while a message is deferred,
restoring the exact contract router.py already calls. Mirrors openclaw-lark's
"typing reaction added at reply-start, removed at reply-done".
"""
from __future__ import annotations

import functools
import logging
from typing import Any

from .feishu_adapter_compat import load_feishu_adapter

logger = logging.getLogger(__name__)

_HOOK_INSTALLED = False
_PATCH_FLAG = "_hermes_multitenancy_reaction_lifecycle_patched"
_DEFERRED_ATTR = "_mt_deferred_processing_message_ids"


def _deferred_set(adapter: Any) -> set:
    s = getattr(adapter, _DEFERRED_ATTR, None)
    if not isinstance(s, set):
        s = set()
        setattr(adapter, _DEFERRED_ATTR, s)
    return s


def _event_message_id(event: Any) -> str:
    return str(getattr(event, "message_id", "") or "")


def install_feishu_reaction_lifecycle_patch() -> None:
    """Idempotently add the deferral contract the router expects."""
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    try:
        FeishuAdapter = load_feishu_adapter()
    except Exception:
        logger.info("[multitenancy] FeishuAdapter not importable yet; reaction-lifecycle patch deferred")
        return

    original_complete = getattr(FeishuAdapter, "on_processing_complete", None)
    if original_complete is None:
        logger.info("[multitenancy] FeishuAdapter has no on_processing_complete; reaction-lifecycle patch skipped")
        return
    if getattr(original_complete, _PATCH_FLAG, False):
        _HOOK_INSTALLED = True
        return

    def defer_processing_complete(self: Any, event: Any) -> None:
        mid = _event_message_id(event)
        if mid:
            _deferred_set(self).add(mid)

    async def complete_deferred_processing(self: Any, event: Any, outcome: Any) -> None:
        mid = _event_message_id(event)
        if mid:
            _deferred_set(self).discard(mid)
        # Now actually run the real removal (no longer deferred).
        await original_complete(self, event, outcome)

    @functools.wraps(original_complete)
    async def wrapped_on_processing_complete(self: Any, event: Any, outcome: Any) -> Any:
        mid = _event_message_id(event)
        if mid and mid in _deferred_set(self):
            # Deferred: the router owns removal and will call
            # complete_deferred_processing after streaming finishes. Skip the
            # base's early removal so the reaction stays visible.
            return None
        return await original_complete(self, event, outcome)

    setattr(wrapped_on_processing_complete, _PATCH_FLAG, True)
    # Only install the deferral methods if the upstream core lacks them (it does);
    # never clobber a fork core that already implements them correctly.
    if not hasattr(FeishuAdapter, "defer_processing_complete"):
        FeishuAdapter.defer_processing_complete = defer_processing_complete
    if not hasattr(FeishuAdapter, "complete_deferred_processing"):
        FeishuAdapter.complete_deferred_processing = complete_deferred_processing
    FeishuAdapter.on_processing_complete = wrapped_on_processing_complete
    _HOOK_INSTALLED = True
    logger.info(
        "[multitenancy] installed reaction-lifecycle deferral on FeishuAdapter "
        "(reaction stays until streaming reply completes)"
    )
