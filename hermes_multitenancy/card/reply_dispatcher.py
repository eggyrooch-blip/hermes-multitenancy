"""Capability detection — three-tier transport fallback for streaming cards.

CardKit (cardkit.v1.card + card_element) → IM patch (im.v1.message.patch)
→ IM update (im.v1.message.update via adapter helpers). Each ``_can_*``
returns True iff the wired Lark SDK adapter exposes the matching methods.
"""
from __future__ import annotations

from typing import Any


def _can_use_cardkit(adapter: Any) -> bool:
    client = getattr(adapter, "_client", None)
    cardkit = getattr(getattr(client, "cardkit", None), "v1", None)
    card = getattr(cardkit, "card", None)
    card_element = getattr(cardkit, "card_element", None)
    return bool(
        client
        and callable(getattr(adapter, "_feishu_send_with_retry", None))
        and callable(getattr(card, "create", None))
        and callable(getattr(card, "settings", None))
        and callable(getattr(card, "update", None))
        and callable(getattr(card_element, "content", None))
    )


def _can_patch_interactive_message(adapter: Any) -> bool:
    client = getattr(adapter, "_client", None)
    message = getattr(getattr(getattr(client, "im", None), "v1", None), "message", None)
    return bool(client and callable(getattr(message, "patch", None)))


def _can_update_interactive_message(adapter: Any) -> bool:
    client = getattr(adapter, "_client", None)
    message = getattr(getattr(getattr(client, "im", None), "v1", None), "message", None)
    return bool(
        client
        and callable(getattr(message, "update", None))
        and callable(getattr(adapter, "_build_update_message_body", None))
        and callable(getattr(adapter, "_build_update_message_request", None))
    )
