"""Layer 4 — capture bot-added inviter into the in-process cache.

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
"""
from __future__ import annotations

import functools
import logging
from typing import Any

logger = logging.getLogger(__name__)

_HOOK_INSTALLED = False
_CLASS_PATCH_FLAG = "_hermes_multitenancy_bot_added_class_patched"


def install_feishu_bot_added_hook() -> None:
    """Idempotently wrap ``FeishuAdapter._on_bot_added_to_chat`` at class level.

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

    original = getattr(FeishuAdapter, "_on_bot_added_to_chat", None)
    if original is None:
        logger.warning(
            "[multitenancy] FeishuAdapter has no _on_bot_added_to_chat; hook not installed"
        )
        return
    if getattr(original, _CLASS_PATCH_FLAG, False):
        _HOOK_INSTALLED = True
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
    _HOOK_INSTALLED = True
    logger.info("[multitenancy] installed bot-added inviter hook on FeishuAdapter class")
    # Sentinel for diagnostic: confirms register() actually ran in this process.
    try:
        from pathlib import Path
        import os as _os
        sentinel = Path.home() / ".hermes" / "logs" / "multitenancy-hook-installed.log"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(f"pid={_os.getpid()} at={__import__('time').strftime('%Y-%m-%d %H:%M:%S')}\n")
    except Exception:
        pass


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
