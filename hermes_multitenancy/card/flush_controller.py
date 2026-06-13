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
        # Time-based throttle (issue #4): coalesce rapid content/reasoning frames
        # into ≤1 network push per throttle window, while GUARANTEEING the
        # trailing frame lands via a deferred timer. Mirrors openclaw-lark
        # flush-controller.ts throttledUpdate.
        self._last_update: float = 0.0
        self._pending_timer: Optional[asyncio.TimerHandle] = None
        self._pending_callable: Optional[Callable[[], Awaitable[Any]]] = None

    @property
    def in_flight(self) -> bool:
        return self._in_flight

    async def throttled_update(
        self,
        throttle_s: float,
        flush_callable: Callable[[], Awaitable[Any]],
        *,
        long_gap_s: float = 2.0,
        batch_after_gap_s: float = 0.3,
    ) -> Optional[Any]:
        """Coalesce rapid flushes; always land the trailing frame.

        - If at least ``throttle_s`` has elapsed since the last push, flush now.
        - After a long idle gap, briefly defer the next flush so multiple fresh
          tokens can batch into one visible update.
        - Otherwise remember the LATEST ``flush_callable`` and (re)arm a single
          timer that fires the latest pending callable when the window closes.
          Intermediate frames within the window are dropped (their state is
          already superseded), but the final one always fires.

        ``flush_callable`` MUST re-render from the live state at call time
        (it is invoked later than the event that scheduled it).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return await self.flush(flush_callable)
        now = loop.time()
        # Always keep the latest callable so a pending timer fires fresh state.
        self._pending_callable = flush_callable
        if self._last_update <= 0.0:
            self._last_update = now
            cb = self._pending_callable
            self._pending_callable = None
            return await self.flush(cb)
        if self._pending_timer is not None:
            return None  # a timer is already armed; it will use the latest callable
        elapsed = now - self._last_update
        if elapsed >= throttle_s:
            self.cancel()
            self._pending_callable = flush_callable
            if elapsed > max(0.0, long_gap_s):
                self._last_update = now
                self._pending_timer = loop.call_later(
                    max(0.0, batch_after_gap_s),
                    self._fire_pending,
                    loop,
                )
                return None
            self._last_update = now
            cb = self._pending_callable
            self._pending_callable = None
            return await self.flush(cb)
        delay = max(0.0, throttle_s - elapsed)
        self._pending_timer = loop.call_later(delay, self._fire_pending, loop)
        return None

    def _fire_pending(self, loop: Any) -> None:
        self._pending_timer = None
        cb = self._pending_callable
        self._pending_callable = None
        if cb is None:
            return
        self._last_update = loop.time()
        # Schedule the async flush; the timer callback itself is sync.
        asyncio.ensure_future(self.flush(cb))

    def cancel(self) -> None:
        """Cancel any armed trailing timer (call on finalize/abort/terminal)."""
        if self._pending_timer is not None:
            self._pending_timer.cancel()
            self._pending_timer = None
        self._pending_callable = None

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
