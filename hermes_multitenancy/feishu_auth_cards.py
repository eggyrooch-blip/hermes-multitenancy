"""Feishu UAT authorization cards.

OpenClaw-style v2 interactive cards for the multitenancy-owned device-flow
auth path. The helpers here only render and transport card state; token
exchange remains in ``feishu_uat_auth``.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .card.card_error import _finalize
from .card.cardkit_client import (
    _create_cardkit_card,
    _patch_interactive_message,
    _update_cardkit_card,
    _update_interactive_message,
)
from .card.reply_dispatcher import (
    _can_patch_interactive_message,
    _can_update_interactive_message,
    _can_use_cardkit,
)

logger = logging.getLogger(__name__)

_LOCALES = ["zh_cn", "en_us"]


def build_auth_card(
    *,
    verification_uri: str,
    user_code: str,
    expires_min: int = 10,
    scope: Optional[str] = None,
) -> dict[str, Any]:
    auth_url = verification_uri
    scope_zh, scope_en = _scope_description(scope)
    expires_zh = f"<font color='grey'>授权链接将在 {expires_min} 分钟后失效，届时需重新发起</font>"
    expires_en = f"<font color='grey'>This link will time out in {expires_min} minutes.</font>"

    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": False,
            "update_multi": True,
            "locales": _LOCALES,
        },
        "header": {
            "title": _plain_i18n("请授权以继续当前操作", "Authorize to continue"),
            "subtitle": {"tag": "plain_text", "content": ""},
            "template": "blue",
            "padding": "12px 12px 12px 12px",
            "icon": {"tag": "standard_icon", "token": "lock-chat_filled"},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": scope_en,
                    "i18n_content": _i18n(scope_zh, scope_en),
                    "text_size": "normal",
                },
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "horizontal_align": "right",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": _plain_i18n("前往授权", "Authorize Now"),
                                    "type": "primary",
                                    "size": "medium",
                                    "multi_url": {
                                        "url": auth_url,
                                        "pc_url": auth_url,
                                        "android_url": auth_url,
                                        "ios_url": auth_url,
                                    },
                                }
                            ],
                        }
                    ],
                },
                {
                    "tag": "markdown",
                    "content": expires_en,
                    "i18n_content": _i18n(expires_zh, expires_en),
                    "text_size": "notation",
                },
            ]
        },
    }


def build_auth_success_card() -> dict[str, Any]:
    body_zh = (
        "你的飞书账号已成功授权，后续 lark_cli 将优先使用你的 user 身份。\n\n"
        "<font color='grey'>如需撤销授权，可随时重新管理飞书 UAT。</font>"
    )
    body_en = (
        "Feishu account authorized. lark_cli will prefer your user identity for later calls.\n\n"
        "<font color='grey'>You can revoke or refresh the authorization later.</font>"
    )
    return _status_card(
        title_zh="授权成功",
        title_en="Authorized",
        body_zh=body_zh,
        body_en=body_en,
        template="green",
        icon_token="yes_filled",
    )


def build_auth_failed_card(reason: str = "") -> dict[str, Any]:
    reason = str(reason or "").strip()
    body_zh = "授权链接已过期，请重新发送 /feishu_auth。"
    body_en = "The link is no longer active. Please restart with /feishu_auth."
    if reason and reason != "expired":
        body_zh = f"授权未完成：{reason}\n\n请重新发送 /feishu_auth。"
        body_en = f"Authorization incomplete: {reason}\n\nPlease restart with /feishu_auth."
    return _status_card(
        title_zh="授权未完成",
        title_en="Authorization incomplete",
        body_zh=body_zh,
        body_en=body_en,
        template="yellow",
        icon_token="warning_filled",
    )


def build_auth_identity_mismatch_card() -> dict[str, Any]:
    return _status_card(
        title_zh="授权失败，操作账号与发起账号不一致",
        title_en="Authorization failed: Account mismatch",
        body_zh=(
            "检测到当前进行授权操作的飞书账号与发起授权请求的账号不一致。"
            "为保障数据安全，本次授权已被拒绝。\n\n"
            "<font color='grey'>请授权请求的发起人使用其账号点击授权链接完成授权。</font>"
        ),
        body_en=(
            "The Feishu account used for authorization does not match the account that initiated the request. "
            "To protect your data, this request has been denied.\n\n"
            "<font color='grey'>Only the person who started this request can authorize it.</font>"
        ),
        template="red",
        icon_token="close_filled",
    )


def _card_has_form(card: Any) -> bool:
    """True if the card contains an interactive ``form`` element anywhere.

    CardKit v1 ``card.create`` rejects schema-2.0 ``form`` components with
    ``code=11310 form's name is required`` — misleading, because it fails even
    when the form carries a non-empty ``name`` (verified: the confirm/fill card's
    form is built with ``name="push_fill_form"`` yet still rejected). Static form
    cards must go over the raw interactive transport, which renders them
    correctly; CardKit's streaming is only useful for text-streaming cards, not
    a one-shot confirm/fill form. So we skip the guaranteed-to-fail CardKit
    attempt for any form-bearing card and send raw directly.
    """
    if isinstance(card, dict):
        if card.get("tag") == "form":
            return True
        return any(_card_has_form(v) for v in card.values())
    if isinstance(card, list):
        return any(_card_has_form(v) for v in card)
    return False


async def send_auth_card(
    *,
    adapter: Any,
    chat_id: str,
    card: dict[str, Any],
    metadata: Any = None,
) -> Optional[dict[str, Any]]:
    """Send an auth card and return mutable card state for later updates."""
    logger.info(
        "Feishu auth card send: can_cardkit=%s can_raw_interactive=%s",
        _can_use_cardkit(adapter),
        callable(getattr(adapter, "_feishu_send_with_retry", None)),
    )
    if _can_use_cardkit(adapter) and not _card_has_form(card):
        try:
            card_id = await _create_cardkit_card(adapter, card)
            if card_id:
                logger.info("Feishu auth CardKit card created card_id=%s", card_id)
                response = await adapter._feishu_send_with_retry(
                    chat_id=chat_id,
                    msg_type="interactive",
                    payload=json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False),
                    reply_to=None,
                    metadata=metadata,
                )
                result = _finalize(adapter, response, "auth CardKit send failed")
                if getattr(result, "success", False):
                    logger.info("Feishu auth CardKit card sent message_id=%s", getattr(result, "message_id", None))
                    return {
                        "transport": "cardkit",
                        "card_id": card_id,
                        "message_id": getattr(result, "message_id", None),
                        "sequence": 1,
                    }
        except Exception as exc:
            logger.warning("Feishu auth CardKit send failed; falling back to raw interactive card: %s", exc)

    sender = getattr(adapter, "_feishu_send_with_retry", None)
    if not callable(sender):
        logger.warning("Feishu auth card send unavailable: adapter lacks _feishu_send_with_retry")
        return None
    try:
        logger.info("Feishu auth sending raw interactive card")
        response = await sender(
            chat_id=chat_id,
            msg_type="interactive",
            payload=json.dumps(card, ensure_ascii=False),
            reply_to=None,
            metadata=metadata,
        )
        result = _finalize(adapter, response, "auth interactive card send failed")
        if getattr(result, "success", False):
            logger.info("Feishu auth raw interactive card sent message_id=%s", getattr(result, "message_id", None))
            return {
                "transport": "interactive",
                "message_id": getattr(result, "message_id", None),
                "sequence": 0,
            }
    except Exception as exc:
        logger.warning("Feishu auth interactive card send failed: %s", exc)
    return None


async def update_auth_card(
    *,
    adapter: Any,
    auth_card: Optional[dict[str, Any]],
    card: dict[str, Any],
) -> bool:
    if not auth_card:
        return False

    card_id = str(auth_card.get("card_id") or "")
    if card_id and _can_use_cardkit(adapter):
        try:
            sequence = int(auth_card.get("sequence") or 1) + 1
            await _update_cardkit_card(adapter, card_id, card, sequence)
            auth_card["sequence"] = sequence
            return True
        except Exception as exc:
            logger.warning("Feishu auth CardKit update failed; falling back to IM update: %s", exc)

    message_id = str(auth_card.get("message_id") or "")
    if not message_id:
        return False
    if _can_patch_interactive_message(adapter):
        try:
            return await _patch_interactive_message(adapter, message_id, card)
        except Exception as exc:
            logger.warning("Feishu auth card patch failed; falling back to update: %s", exc)
    if _can_update_interactive_message(adapter):
        try:
            return await _update_interactive_message(adapter, message_id, card)
        except Exception as exc:
            logger.warning("Feishu auth card update failed: %s", exc)
    return False


def auth_text_fallback(*, verification_uri: str, user_code: str) -> str:
    return (
        "请打开下面链接完成飞书 UAT 授权：\n"
        f"{verification_uri}\n\n"
        f"验证码：`{user_code}`\n"
        "完成后我会自动确认，并把 UAT 写入当前 Hermes profile。"
    )


def _status_card(
    *,
    title_zh: str,
    title_en: str,
    body_zh: str,
    body_en: str,
    template: str,
    icon_token: str,
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": False,
            "update_multi": True,
            "locales": _LOCALES,
        },
        "header": {
            "title": _plain_i18n(title_zh, title_en),
            "subtitle": {"tag": "plain_text", "content": ""},
            "template": template,
            "padding": "12px 12px 12px 12px",
            "icon": {"tag": "standard_icon", "token": icon_token},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": body_en,
                    "i18n_content": _i18n(body_zh, body_en),
                }
            ]
        },
    }


def _scope_description(scope: Optional[str]) -> tuple[str, str]:
    scopes = [part for part in str(scope or "").split() if part]
    if not scopes:
        return (
            "授权后，应用将能够以你的身份执行飞书 UAT 相关操作。",
            "Once authorized, Hermes can perform Feishu UAT operations on your behalf.",
        )
    scope_lines = "\n".join(f"- {item}" for item in scopes)
    return (
        "授权后，应用将能够以你的身份执行相关操作。\n\n所需权限：\n" + scope_lines,
        "Once authorized, Hermes can perform related operations on your behalf.\n\nRequired permissions:\n"
        + scope_lines,
    )


def _plain_i18n(zh: str, en: str) -> dict[str, Any]:
    return {"tag": "plain_text", "content": en, "i18n_content": _i18n(zh, en)}


def _i18n(zh: str, en: str) -> dict[str, str]:
    return {"zh_cn": zh, "en_us": en}
