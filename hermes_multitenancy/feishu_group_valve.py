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
_SHOULD_ACCEPT_FLAG = "_hermes_multitenancy_group_valve_should_accept_patched"
_CARD_ACTION_FLAG = "_hermes_multitenancy_group_valve_card_action_patched"
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
    _patch_should_accept_group_message(FeishuAdapter)
    _patch_on_card_action_trigger(FeishuAdapter)
    _HOOK_INSTALLED = True


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
        row = table.lookup_by_chat_id(chat_id)
        owner_open_id = str(getattr(row, "owner_open_id", "") or "")
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
