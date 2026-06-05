"""Intercept Feishu credential-hub card button clicks inside multitenancy.

Card-action (button-click) events do NOT flow through the plugin's
``pre_gateway_dispatch`` hook — core routes them via
``FeishuAdapter._on_card_action_trigger`` → ``_handle_card_action_event`` →
``handle_message`` (which is *past* the dispatch hook). So to handle a
credential-hub button click in multitenancy we monkeypatch the adapter's
card-action handler — the same runtime-patch approach this plugin already uses
for ``_on_bot_added_to_chat`` / ``_send_raw_message`` / ``_build_outbound_payload``
(no core source fork). A click with ``value.hub_action == "auth"`` is handled
here; everything else falls through to core's original handler.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False


def install_card_action_hook() -> None:
    """Idempotently wrap ``FeishuAdapter._handle_card_action_event``."""
    global _INSTALLED
    if _INSTALLED:
        return
    try:
        from gateway.platforms.feishu import FeishuAdapter  # type: ignore
    except Exception:
        logger.info("[multitenancy] FeishuAdapter not importable yet; card-action hook deferred")
        return
    if getattr(FeishuAdapter, "_hermes_mt_card_action_patched", False):
        _INSTALLED = True
        return
    original = getattr(FeishuAdapter, "_handle_card_action_event", None)
    if original is None:
        logger.warning("[multitenancy] FeishuAdapter has no _handle_card_action_event; card hook not installed")
        return

    async def _patched(self: Any, data: Any):
        try:
            from .router import hub_card_action_from_event

            if await hub_card_action_from_event(self, data):
                return  # handled as a credential-hub auth action
        except Exception:
            logger.exception("[multitenancy] credential-hub card action failed; deferring to core")
        return await original(self, data)

    FeishuAdapter._handle_card_action_event = _patched
    FeishuAdapter._hermes_mt_card_action_patched = True
    _INSTALLED = True
    logger.info("[multitenancy] credential-hub card-action hook installed")


__all__ = ["install_card_action_hook"]
