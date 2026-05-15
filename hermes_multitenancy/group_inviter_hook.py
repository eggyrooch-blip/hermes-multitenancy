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

import functools
import logging
from typing import Any

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
        from gateway.platforms.feishu import FeishuAdapter  # type: ignore
    except Exception:
        logger.info(
            "[multitenancy] FeishuAdapter not importable yet; bot-added hook deferred"
        )
        return

    _patch_bot_added(FeishuAdapter)
    _patch_chat_added_welcome(FeishuAdapter)
    _HOOK_INSTALLED = True


def _patch_bot_added(FeishuAdapter: Any) -> None:
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
        return original(self, data)

    setattr(wrapped, _CLASS_PATCH_FLAG, True)
    FeishuAdapter._on_bot_added_to_chat = wrapped
    logger.info("[multitenancy] installed bot-added inviter hook on FeishuAdapter class")


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
            if _mark_and_check_welcomed(chat_id):
                # Duplicate bot-added redelivery — already welcomed.
                return None
            try:
                await self.send(
                    chat_id,
                    "👋 已加入此群。@我提问即可，群里不会以任何成员的身份调用飞书数据。"
                    "如需以你本人身份调用飞书 API，请私聊我后发送 `/feishu_auth`。",
                )
            except Exception:
                logger.debug(
                    "[multitenancy] group welcome send failed for chat=%s", chat_id
                )
            return None
        return await original(self, chat_id)

    setattr(wrapped, _WELCOME_PATCH_FLAG, True)
    FeishuAdapter._send_chat_added_onboarding = wrapped
    logger.info("[multitenancy] installed group-aware welcome on FeishuAdapter class")


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
    operator = getattr(event, "operator_id", None) or getattr(event, "operator", None)
    inviter_open_id = _read_first(operator, ("open_id", "operator_open_id"))
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
    )
    logger.info(
        "[multitenancy] captured inviter for chat=%s inviter=%s",
        chat_id,
        inviter_open_id,
    )


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
