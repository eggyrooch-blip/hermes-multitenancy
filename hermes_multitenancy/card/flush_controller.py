"""FlushController — mutex + reflush-on-conflict for streaming-card writes.

Mirrors openclaw-lark `FlushController` semantics:

- One in-flight CardKit API call per ``card_id`` at a time, guarded by an
  ``asyncio.Lock``.
- If new events arrive *during* the in-flight call, the lock holder sees a
  ``needs_reflush`` flag and re-runs the flush coroutine immediately after
  releasing (``asyncio.create_task`` with no delay) so the final visible card
  state always reflects the latest batched events. Without this, the throttle
  window can swallow the last token at the moment Feishu's server diff is
  computed.

The controller is *additive* — existing callers continue to call their own
``_flush_state`` helpers directly. Phase 3+ may opt-in by routing flushes
through a ``FlushController`` instance bound per-message; Phase 3 ships the
primitive + tests, leaves caller wiring as a separate follow-up (see plan's
``card-throttle-long-gap-batch`` for the next adjacency).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("hermes_multitenancy.feishu_cardkit_compat")


class FlushController:
    """Serialize streaming-card flush calls with reflush-on-conflict.

    Usage::

        controller = FlushController()
        # In an event handler:
        await controller.flush(lambda: _do_actual_flush(state))

    The ``flush_callable`` is an async no-arg callable. While one ``flush``
    is in flight, subsequent ``flush`` calls *do not* await the coroutine
    again — they set ``needs_reflush`` and return, trusting the holder to
    schedule a follow-up. This matches openclaw's behavior: the latest
    pending flush is the one that matters; intermediate ones are coalesced.
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock = asyncio.Lock()
        self._needs_reflush: bool = False
        self._in_flight: bool = False

    @property
    def in_flight(self) -> bool:
        return self._in_flight

    @property
    def needs_reflush(self) -> bool:
        return self._needs_reflush

    async def flush(self, flush_callable: Callable[[], Awaitable[Any]]) -> Optional[Any]:
        """Run ``flush_callable`` under the controller's mutex.

        If the lock is already held by another caller, set ``needs_reflush``
        and return ``None`` — the holder will pick it up after its current
        run completes.

        After the wrapped call returns, if ``needs_reflush`` was raised,
        schedule a follow-up flush via ``asyncio.create_task(...)`` so the
        latest batched state lands without waiting for the next event.
        """
        if self._in_flight:
            self._needs_reflush = True
            return None

        self._in_flight = True
        try:
            async with self._lock:
                result = await flush_callable()
        except Exception:
            self._needs_reflush = False
            self._in_flight = False
            raise
        finally:
            self._in_flight = False

        if self._needs_reflush:
            self._needs_reflush = False
            # Schedule the follow-up without awaiting it — caller's event loop
            # picks it up at the next tick; the follow-up re-enters this same
            # ``flush`` method and benefits from the mutex.
            asyncio.create_task(self.flush(flush_callable))
        return result
