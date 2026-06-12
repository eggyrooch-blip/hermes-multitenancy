"""Fail-open Feishu group reply-mode valve patch.

Tests run without lark_oapi, so toast/card callback responses fall back to
plain dict descriptors while production returns SDK response objects.
"""
from __future__ import annotations

import functools
import logging
from typing import Any

from .router import _get_routing_table

logger = logging.getLogger(__name__)

_HOOK_INSTALLED = False
_REQUIRE_MENTION_FLAG = "_hermes_multitenancy_group_valve_require_mention_patched"
_SHOULD_ACCEPT_FLAG = "_hermes_multitenancy_group_valve_should_accept_patched"
_CARD_ACTION_FLAG = "_hermes_multitenancy_group_valve_card_action_patched"
_MENTIONS_SELF_FLAG = "_hermes_multitenancy_group_valve_mentions_self_patched"
_VALID_REPLY_MODES = frozenset({"mention", "all"})


def install_feishu_group_valve_patch() -> None:
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    try:
        from gateway.platforms.feishu import FeishuAdapter  # type: ignore
    except Exception:
        logger.info(
            "[multitenancy] FeishuAdapter not importable yet; group valve patch deferred"
        )
        return
    # Two gate shapes across core versions:
    #  - upstream/prod (v0.16.0): _admit() -> _require_mention_for(chat_id) -> bool.
    #    'all' mode means "don't require a mention", so we make that bool False.
    #  - fork/local: _should_accept_group_message(message, sender_id, chat_id).
    # Patch whichever exists; both fail-open to the original.
    _patch_require_mention_for(FeishuAdapter)
    _patch_should_accept_group_message(FeishuAdapter)
    _patch_mentions_self(FeishuAdapter)
    _patch_on_card_action_trigger(FeishuAdapter)
    _HOOK_INSTALLED = True


def _patch_require_mention_for(FeishuAdapter: Any) -> None:
    """Prod path: flip _require_mention_for(chat_id) to False for 'all' groups.

    In _admit(), ``require_mention = is_group and self._require_mention_for(chat_id)``
    and a non-mention group message is rejected only ``if require_mention``. So
    returning False admits every group message. The group-policy gate
    (_allow_group_message) runs separately in _admit and is left untouched.
    """
    original = getattr(FeishuAdapter, "_require_mention_for", None)
    if original is None or getattr(original, _REQUIRE_MENTION_FLAG, False):
        return

    @functools.wraps(original)
    def wrapped(self: Any, chat_id: Any = "") -> Any:
        normalized_chat_id = str(chat_id or "")
        try:
            if normalized_chat_id:
                table = _get_routing_table()
                if table is not None and table.get_group_reply_mode(normalized_chat_id) == "all":
                    return False
        except Exception:
            logger.debug(
                "[multitenancy] require-mention valve failed; delegating to original",
                exc_info=True,
            )
        return original(self, chat_id)

    setattr(wrapped, _REQUIRE_MENTION_FLAG, True)
    FeishuAdapter._require_mention_for = wrapped
    logger.info(
        "[multitenancy] installed group reply valve on FeishuAdapter._require_mention_for"
    )


def _load_normalize() -> Any:
    """Lazy handle to the core post-payload normalizer (indirected for tests)."""
    from gateway.platforms.feishu import normalize_feishu_message  # type: ignore

    return normalize_feishu_message


def _genuinely_mentions_bot(adapter: Any, message: Any) -> bool:
    """True only when the message @-mentions THIS bot by id/name.

    Mirrors core ``_mentions_self`` minus its ``@_all`` shortcut: an @everyone
    (@所有人 / @_all) is NOT a mention of the bot. Reuses the core helpers so the
    id/name matching rules never drift from upstream.
    """
    mentions = getattr(message, "mentions", None) or []
    if mentions and adapter._message_mentions_bot(mentions):
        return True
    normalize_feishu_message = _load_normalize()
    normalized = normalize_feishu_message(
        message_type=getattr(message, "message_type", "") or "",
        raw_content=getattr(message, "content", "") or "",
        mentions=getattr(message, "mentions", None),
        bot=adapter._bot_identity(),
    )
    return adapter._post_mentions_bot(normalized.mentions)


def _patch_mentions_self(FeishuAdapter: Any) -> None:
    """Prod path: stop @everyone (@_all / @所有人) from counting as a bot mention.

    Core ``_mentions_self`` returns True whenever ``@_all`` appears in the raw
    content, so in a mention-only group an @everyone broadcast (e.g. a bot
    notification) wrongly satisfies _admit's mention gate and wakes the bot.
    Here we let the bot wake ONLY on a genuine @-mention of itself; @everyone
    alone is ignored. Fails open to the original on any error.
    """
    original = getattr(FeishuAdapter, "_mentions_self", None)
    if original is None or getattr(original, _MENTIONS_SELF_FLAG, False):
        return

    @functools.wraps(original)
    def wrapped(self: Any, message: Any) -> Any:
        try:
            result = original(self, message)
            if result and not _genuinely_mentions_bot(self, message):
                # Original said "mentioned" only because of @everyone; drop it.
                return False
            return result
        except Exception:
            logger.debug(
                "[multitenancy] mentions-self valve failed; delegating to original",
                exc_info=True,
            )
            return original(self, message)

    setattr(wrapped, _MENTIONS_SELF_FLAG, True)
    FeishuAdapter._mentions_self = wrapped
    logger.info(
        "[multitenancy] installed @everyone-ignore valve on FeishuAdapter._mentions_self"
    )


def _patch_should_accept_group_message(FeishuAdapter: Any) -> None:
    original = getattr(FeishuAdapter, "_should_accept_group_message", None)
    if original is None or getattr(original, _SHOULD_ACCEPT_FLAG, False):
        return

    @functools.wraps(original)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        message = args[0] if len(args) >= 1 else kwargs.get("message")
        sender_id = args[1] if len(args) >= 2 else kwargs.get("sender_id")
        chat_id = args[2] if len(args) >= 3 else kwargs.get("chat_id", "")
        normalized_chat_id = str(chat_id or "")
        try:
            if normalized_chat_id:
                table = _get_routing_table()
                if (
                    table is not None
                    and table.get_group_reply_mode(normalized_chat_id) == "all"
                    and self._allow_group_message(sender_id, normalized_chat_id)
                ):
                    return True
        except Exception:
            logger.debug(
                "[multitenancy] group reply valve failed; delegating to original",
                exc_info=True,
            )
        return original(self, *args, **kwargs)

    setattr(wrapped, _SHOULD_ACCEPT_FLAG, True)
    FeishuAdapter._should_accept_group_message = wrapped
    logger.info("[multitenancy] installed group reply valve on FeishuAdapter")


def _patch_on_card_action_trigger(FeishuAdapter: Any) -> None:
    original = getattr(FeishuAdapter, "_on_card_action_trigger", None)
    if original is None or getattr(original, _CARD_ACTION_FLAG, False):
        return

    @functools.wraps(original)
    def wrapped(self: Any, data: Any) -> Any:
        try:
            event = getattr(data, "event", None)
            action = getattr(event, "action", None)
            action_value = getattr(action, "value", {}) or {}
            if not isinstance(action_value, dict):
                action_value = {}
            hermes_action = action_value.get("hermes_action")
            if hermes_action != "group_reply_mode":
                return original(self, data)
            return _handle_group_reply_mode_action(self, event, action_value)
        except Exception:
            logger.debug(
                "[multitenancy] group reply mode card action failed; delegating to original",
                exc_info=True,
            )
            return original(self, data)

    setattr(wrapped, _CARD_ACTION_FLAG, True)
    FeishuAdapter._on_card_action_trigger = wrapped
    logger.info("[multitenancy] installed group reply mode card-action hook")


def _handle_group_reply_mode_action(
    adapter: Any,
    event: Any,
    action_value: dict[str, Any],
) -> Any:
    del adapter
    try:
        mode = str(action_value.get("mode") or "").strip()
        if mode not in _VALID_REPLY_MODES:
            return _toast_response("无效的模式")
        operator_open_id = _event_operator_open_id(event)
        chat_id = _event_chat_id(event, action_value)
        table = _get_routing_table()
        if table is None or not chat_id:
            return _toast_response("暂时无法保存设置，请稍后再试")
        owner_open_id = _resolve_group_owner(table, chat_id)
        if (
            not operator_open_id
            or not owner_open_id
            or operator_open_id != owner_open_id
        ):
            return _toast_response("无权操作：只有把我拉进群的人能改")
        table.set_group_reply_mode(chat_id, mode)
        return _updated_card_response(_build_group_reply_mode_status_card(chat_id, mode))
    except Exception:
        logger.debug(
            "[multitenancy] group reply mode action handling failed",
            exc_info=True,
        )
        return _toast_response("暂时无法保存设置，请稍后再试")


def _resolve_group_owner(table: Any, chat_id: str) -> str:
    """The owner = inviter. Right after bot-added the group routing row may not
    be provisioned yet (that happens on the first routed group message), but the
    inviter is already in the pending table. Check the provisioned row first,
    then fall back to the pending inviter so the real owner is never wrongly
    denied when they tap the welcome card immediately.
    """
    try:
        row = table.lookup_by_chat_id(chat_id)
        owner = str(getattr(row, "owner_open_id", "") or "")
        if owner:
            return owner
    except Exception:
        logger.debug("[multitenancy] owner lookup_by_chat_id failed", exc_info=True)
    try:
        pending = table.get_pending_inviter(chat_id)
        if pending:
            return str(pending)
    except Exception:
        logger.debug("[multitenancy] owner get_pending_inviter failed", exc_info=True)
    return ""


def _event_operator_open_id(event: Any) -> str:
    operator = getattr(event, "operator", None)
    return str(getattr(operator, "open_id", "") or "")


def _event_chat_id(event: Any, action_value: dict[str, Any]) -> str:
    chat_id = str(action_value.get("chat_id") or "").strip()
    if chat_id:
        return chat_id
    context = getattr(event, "context", None)
    return str(getattr(context, "open_chat_id", "") or "").strip()


def _build_group_reply_mode_status_card(chat_id: str, mode: str) -> dict[str, Any]:
    del chat_id
    title = "已更新群回复模式"
    body = (
        "已设为 仅回复 @我 模式"
        if mode == "mention"
        else "已设为 回复所有消息 模式"
    )
    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": False,
            "update_multi": True,
            "locales": ["zh_cn", "en_us"],
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "Reply mode updated",
                "i18n_content": {
                    "zh_cn": title,
                    "en_us": "Reply mode updated",
                },
            },
            "subtitle": {"tag": "plain_text", "content": ""},
            "template": "green",
            "padding": "12px 12px 12px 12px",
            "icon": {"tag": "standard_icon", "token": "yes_filled"},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": body,
                    "i18n_content": {
                        "zh_cn": body,
                        "en_us": (
                            "Now replying only when mentioned."
                            if mode == "mention"
                            else "Now replying to every group message."
                        ),
                    },
                }
            ]
        },
    }


def _toast_response(content: str, *, level: str = "error") -> Any:
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )
    except Exception:
        return {"kind": "toast", "toast": {"type": level, "content": content}}
    response = P2CardActionTriggerResponse()
    response.toast = {"type": level, "content": content}
    return response


def _updated_card_response(card: dict[str, Any]) -> Any:
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            CallBackCard,
            P2CardActionTriggerResponse,
        )
    except Exception:
        return {"kind": "card", "card": {"type": "raw", "data": card}}
    response = P2CardActionTriggerResponse()
    callback_card = CallBackCard()
    callback_card.type = "raw"
    callback_card.data = card
    response.card = callback_card
    return response
