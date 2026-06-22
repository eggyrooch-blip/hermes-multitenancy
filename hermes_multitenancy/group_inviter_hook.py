"""Layer 4 — capture bot-added inviter + group-aware welcome via class-level patch.

When the Feishu adapter receives an ``im.chat.member.bot.added_v1`` event,
the bot has been pulled into a chat by some user. The operator_id on that
event is the inviter — the person who, per design (see PRD § Decisions),
becomes the immutable owner of the resulting group profile.

**Patch site (important):** ``FeishuAdapter.__init__`` registers
``self._on_bot_added_to_chat`` with the Lark SDK callback registry, which
captures the method reference at adapter-init time. Patching an *instance*
attribute after ``_create_adapter`` has returned therefore does nothing —
the SDK already holds the unwrapped reference. We patch the *class*
attribute at plugin-register time so every adapter instance picks up the
wrapped method when it does ``self._on_bot_added_to_chat`` inside ``__init__``.

The same module also replaces ``_send_chat_added_onboarding`` at class
level so the welcome message can vary by chat_type (group vs p2p). This
keeps all hermes-native files untouched while still surfacing the right
message for group chats.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from .feishu_adapter_compat import load_feishu_adapter
from .feishu_auth_cards import send_auth_card

logger = logging.getLogger(__name__)

import threading as _threading
from collections import OrderedDict as _OrderedDict

_HOOK_INSTALLED = False
_CLASS_PATCH_FLAG = "_hermes_multitenancy_bot_added_class_patched"
_WELCOME_PATCH_FLAG = "_hermes_multitenancy_welcome_class_patched"

# Feishu delivers im.chat.member.bot.added_v1 at-least-once, so the same
# chat can fire the welcome path several times. Track recently-welcomed
# chat_ids (bounded LRU) to suppress duplicate group welcomes within the
# process lifetime. Lock-guarded: the welcome runs on the SDK callback
# thread, not the asyncio loop thread.
_WELCOMED_CHATS_MAX = 512
_welcomed_chats: "_OrderedDict[str, bool]" = _OrderedDict()
_welcomed_chats_lock = _threading.Lock()


def _mark_and_check_welcomed(chat_id: str) -> bool:
    """Return True if this chat was already welcomed (and should be skipped).

    Atomically records the chat as welcomed and reports the prior state, so
    only the first caller for a given chat_id sends the message.
    """
    with _welcomed_chats_lock:
        if chat_id in _welcomed_chats:
            _welcomed_chats.move_to_end(chat_id)
            return True
        _welcomed_chats[chat_id] = True
        while len(_welcomed_chats) > _WELCOMED_CHATS_MAX:
            _welcomed_chats.popitem(last=False)
        return False


def install_feishu_bot_added_hook() -> None:
    """Idempotently wrap ``FeishuAdapter._on_bot_added_to_chat`` + welcome at class level.

    Safe to call before the adapter module is importable: the patch is
    skipped (with an info log) and the next plugin-register invocation can
    retry. In practice the gateway loads ``gateway.platforms.feishu`` during
    its own startup so the import succeeds by the time multitenancy runs.
    """
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    try:
        FeishuAdapter = load_feishu_adapter()
    except Exception:
        logger.info(
            "[multitenancy] FeishuAdapter not importable yet; bot-added hook deferred"
        )
        return

    # Upstream cores (prod runs NousResearch upstream) have NO
    # _send_chat_added_onboarding — the fork-only welcome was lost on the
    # upstream realign, which is why prod stopped sending any group welcome.
    # When that method is absent, the welcome card must be sent straight from
    # the _on_bot_added_to_chat wrapper (which DOES exist upstream); otherwise
    # we ride the existing _send_chat_added_onboarding path and let this
    # wrapper stay welcome-free to avoid a double send.
    has_onboarding = getattr(FeishuAdapter, "_send_chat_added_onboarding", None) is not None
    _patch_bot_added(FeishuAdapter, send_welcome=not has_onboarding)
    _patch_chat_added_welcome(FeishuAdapter)
    _HOOK_INSTALLED = True


def _patch_bot_added(FeishuAdapter: Any, *, send_welcome: bool = False) -> None:
    original = getattr(FeishuAdapter, "_on_bot_added_to_chat", None)
    if original is None:
        logger.warning(
            "[multitenancy] FeishuAdapter has no _on_bot_added_to_chat; hook not installed"
        )
        return
    if getattr(original, _CLASS_PATCH_FLAG, False):
        return

    @functools.wraps(original)
    def wrapped(self: Any, data: Any) -> Any:
        try:
            _capture_inviter_from_event(data)
        except Exception:
            logger.exception(
                "[multitenancy] inviter capture failed; deferring to original handler"
            )
        result = original(self, data)
        # im.chat.member.bot.added_v1 fires only for group/topic chats, so the
        # group welcome card is always the right surface here. Only used when
        # the core lacks _send_chat_added_onboarding (upstream/prod).
        if send_welcome:
            try:
                _schedule_group_welcome_card(self, data)
            except Exception:
                logger.debug(
                    "[multitenancy] could not schedule group welcome card",
                    exc_info=True,
                )
        return result

    setattr(wrapped, _CLASS_PATCH_FLAG, True)
    FeishuAdapter._on_bot_added_to_chat = wrapped
    logger.info(
        "[multitenancy] installed bot-added inviter hook on FeishuAdapter class "
        "(welcome_card_here=%s)",
        send_welcome,
    )


def _schedule_group_welcome_card(adapter: Any, data: Any) -> None:
    """Schedule the welcome card on the adapter loop (sync callback context)."""
    event = getattr(data, "event", None)
    chat_id = str(getattr(event, "chat_id", "") or "")
    if not chat_id:
        return
    loop = getattr(adapter, "_loop", None)
    accepts = getattr(adapter, "_loop_accepts_callbacks", None)
    if loop is None or (callable(accepts) and not accepts(loop)):
        logger.debug(
            "[multitenancy] adapter loop not ready; skipping welcome card for chat=%s",
            chat_id,
        )
        return
    asyncio.run_coroutine_threadsafe(_send_group_welcome_card(adapter, chat_id), loop)


def _patch_chat_added_welcome(FeishuAdapter: Any) -> None:
    original = getattr(FeishuAdapter, "_send_chat_added_onboarding", None)
    if original is None:
        logger.info(
            "[multitenancy] FeishuAdapter has no _send_chat_added_onboarding; welcome hook skipped"
        )
        return
    if getattr(original, _WELCOME_PATCH_FLAG, False):
        return

    @functools.wraps(original)
    async def wrapped(self: Any, chat_id: str) -> Any:
        chat_type = await _resolve_chat_type(self, chat_id)
        if chat_type in ("group", "topic"):
            await _send_group_welcome_card(self, chat_id)
            return None
        return await original(self, chat_id)

    setattr(wrapped, _WELCOME_PATCH_FLAG, True)
    FeishuAdapter._send_chat_added_onboarding = wrapped
    logger.info("[multitenancy] installed group-aware welcome on FeishuAdapter class")


async def _send_group_welcome_card(adapter: Any, chat_id: str) -> None:
    """Send the group reply-mode welcome card once per chat (dedup-guarded).

    Shared by both welcome paths: the _send_chat_added_onboarding wrapper
    (fork cores) and the _on_bot_added_to_chat wrapper (upstream/prod cores
    that lack the onboarding method). The dedup makes concurrent paths safe.
    """
    if _mark_and_check_welcomed(chat_id):
        # Duplicate bot-added redelivery / second welcome path — already sent.
        return
    try:
        card = _build_group_welcome_card(chat_id)
        await send_auth_card(adapter=adapter, chat_id=chat_id, card=card)
    except Exception:
        logger.debug(
            "[multitenancy] group welcome card send failed for chat=%s",
            chat_id,
            exc_info=True,
        )
        try:
            await adapter.send(
                chat_id,
                "👋 已加入此群。默认仅回复 @我。"
                "如需以你本人身份调用飞书 API，请私聊我后发送 `/feishu_auth`。",
            )
        except Exception:
            logger.debug(
                "[multitenancy] group welcome text fallback send failed for chat=%s",
                chat_id,
            )


async def _resolve_chat_type(adapter: Any, chat_id: str) -> str:
    cache = getattr(adapter, "_chat_info_cache", None)
    cached = cache.get(chat_id) if isinstance(cache, dict) else None
    if isinstance(cached, dict):
        value = str(cached.get("type") or "").strip().lower()
        if value:
            return value
    getter = getattr(adapter, "get_chat_info", None)
    if getter is None:
        return ""
    try:
        info = await getter(chat_id)
    except Exception:
        return ""
    if isinstance(info, dict):
        return str(info.get("type") or "").strip().lower()
    return ""


def _capture_inviter_from_event(data: Any) -> None:
    """Extract operator_id.open_id and chat_id from a bot_added event payload."""
    from .router import register_chat_inviter

    event = getattr(data, "event", None)
    if event is None:
        return
    chat_id = _read_first(event, ("chat_id",))
    operator = _read_first(event, ("operator_id", "operator"))
    inviter_open_id = _read_first(operator, ("open_id", "operator_open_id"))
    inviter_union_id = _read_first(operator, ("union_id", "operator_union_id"))
    if not inviter_open_id or not chat_id:
        logger.info(
            "[multitenancy] bot_added missing inviter or chat_id; cache not seeded"
        )
        return
    chat_name = _read_first(event, ("chat_name", "name"))
    inviter_display = _read_first(operator, ("name", "display_name"))
    register_chat_inviter(
        str(chat_id),
        str(inviter_open_id),
        chat_name=str(chat_name) if chat_name else None,
        inviter_display=str(inviter_display) if inviter_display else None,
        inviter_union_id=str(inviter_union_id) if inviter_union_id else None,
    )
    logger.info(
        "[multitenancy] captured inviter for chat=%s inviter=%s",
        chat_id,
        inviter_open_id,
    )


def _build_group_welcome_card(chat_id: str) -> dict[str, Any]:
    body_zh = (
        "👋 已加入此群。默认是 **仅回复 @我**，避免打扰群聊。"
        "\n\n如果你希望我回复群里的每条消息，可以直接点下面的按钮切换。"
        "\n\n<font color='grey'>如需以你本人身份调用飞书 API，请私聊我后发送 `/feishu_auth`。</font>"
    )
    body_en = (
        "I joined this group. The default is **reply only when mentioned**."
        "\n\nUse the buttons below to choose the group reply mode."
        "\n\n<font color='grey'>For Feishu API calls as yourself, DM me and send `/feishu_auth`.</font>"
    )
    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": False,
            "update_multi": True,
            "locales": ["zh_cn", "en_us"],
        },
        "header": {
            "title": _plain_i18n("欢迎设置群回复模式", "Choose group reply mode"),
            "subtitle": {"tag": "plain_text", "content": ""},
            "template": "blue",
            "padding": "12px 12px 12px 12px",
            "icon": {"tag": "standard_icon", "token": "robot_filled"},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": body_en,
                    "i18n_content": _i18n(body_zh, body_en),
                },
                {
                    "tag": "column_set",
                    "flex_mode": "stretch",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": _plain_i18n("仅回复 @我", "Mention only"),
                                    "type": "default",
                                    "value": {
                                        "hermes_action": "group_reply_mode",
                                        "mode": "mention",
                                        "chat_id": chat_id,
                                    },
                                }
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": _plain_i18n("回复所有消息", "Reply to all"),
                                    "type": "primary",
                                    "value": {
                                        "hermes_action": "group_reply_mode",
                                        "mode": "all",
                                        "chat_id": chat_id,
                                    },
                                }
                            ],
                        },
                    ],
                },
            ]
        },
    }


def _read_first(obj: Any, names: tuple[str, ...]) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        for n in names:
            value = obj.get(n)
            if value:
                return value
        return None
    for n in names:
        value = getattr(obj, n, None)
        if value:
            return value
    return None


def _plain_i18n(zh: str, en: str) -> dict[str, Any]:
    return {"tag": "plain_text", "content": en, "i18n_content": _i18n(zh, en)}


def _i18n(zh: str, en: str) -> dict[str, str]:
    return {"zh_cn": zh, "en_us": en}
