"""Admission ordering for direct Feishu vision-block replies."""
from __future__ import annotations

from typing import Any

from .. import router as _m


_VISION_BLOCK_REPLY = "vision_blocked_reply"


def attach_vision_block(prepared_run, dispatch_request, *, event: Any, text: str):
    reply = _m._image_vision_unavailable_response(event, text)
    return prepared_run.with_request(
        dispatch_request,
        internal_metadata={_VISION_BLOCK_REPLY: reply} if reply else {},
    )


def vision_block_reply(capability) -> str:
    return str(capability.internal_metadata.get(_VISION_BLOCK_REPLY) or "")


async def send_vision_block_before_admission(
    prepared_run,
    *,
    adapter,
    chat_id: str,
    profile_name: str,
    event: Any,
) -> None:
    reply = vision_block_reply(prepared_run)
    if not reply:
        return
    if adapter is None:
        raise RuntimeError("Feishu adapter unavailable for vision-block reply")
    _m.logger.info(
        "multitenancy: sending image vision unavailable response profile=%s message_id=%s",
        profile_name,
        _m._event_message_id(event) or "",
    )
    await _m._safe_call(adapter.send, chat_id, reply)
