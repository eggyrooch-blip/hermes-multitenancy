"""The per-profile warm-worker slot must survive a cancelled waiter.

Production incident 2026-09-11 → 2026-09-15 (profile ``zhengshi``, Feishu DM):
one long turn held the profile slot for 5m16s; the user sent six more messages
meanwhile. Each of those runs was parked in ``acquire_run`` and then cancelled by
the router when the next message arrived. ``acquire_run`` awaited
``asyncio.to_thread(lock.acquire)`` with no timeout — the coroutine died on
cancel, but the worker thread still took the lock once the long turn released it,
and nothing ever released it again. Every later DM run for that profile blocked
silently at the same await (log stops right after ``turn_tool_context``), for
3 days 21 hours, until a routine gateway restart cleared the in-memory lock.

These tests pin the two guarantees that close that hole:

1. a waiter cancelled while queued never leaves the slot held (the wait is a
   non-blocking poll from the event loop; no thread ever acquires on our behalf);
2. a waiter gives up after a bounded wait and raises, so the caller's existing
   "slot unavailable → one-shot subprocess" fallback runs instead of an
   unbounded silent wait.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from hermes_multitenancy import agent_real


async def _spin_until(predicate, *, timeout_s: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def test_cancelled_waiter_does_not_leak_the_profile_slot(tmp_path: Path):
    worker = agent_real._AiagentWarmWorker(tmp_path)

    # Run A holds the slot (the long turn).
    run_a = await worker.acquire_run()
    assert worker._lock.locked()

    # Run B queues behind it and is cancelled while queued (the router aborting
    # the previous in-flight dispatch when the user's next message arrives).
    waiter_b = asyncio.ensure_future(worker.acquire_run())
    await _spin_until(lambda: worker._slot_waiters() >= 1)
    waiter_b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_b

    # A finishes and releases. Without the fix, B's parked thread now takes the
    # lock and nobody ever releases it.
    await run_a.close()

    # Run C (the user's next message) must get the slot promptly.
    run_c = await asyncio.wait_for(worker.acquire_run(), timeout=2.0)
    await run_c.close()
    await _spin_until(lambda: not worker._lock.locked())
    assert worker._slot_waiters() == 0


async def test_many_cancelled_waiters_still_leave_the_slot_free(tmp_path: Path):
    """Six queued messages, all cancelled — the incident shape."""
    worker = agent_real._AiagentWarmWorker(tmp_path)
    run_a = await worker.acquire_run()

    waiters = [asyncio.ensure_future(worker.acquire_run()) for _ in range(6)]
    await _spin_until(lambda: worker._slot_waiters() >= 6)
    for w in waiters:
        w.cancel()
    for w in waiters:
        with pytest.raises(asyncio.CancelledError):
            await w

    await run_a.close()

    run_c = await asyncio.wait_for(worker.acquire_run(), timeout=2.0)
    await run_c.close()
    await _spin_until(lambda: not worker._lock.locked() and worker._slot_waiters() == 0)


async def test_acquire_gives_up_after_bounded_wait(tmp_path: Path):
    worker = agent_real._AiagentWarmWorker(tmp_path)
    run_a = await worker.acquire_run()
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError, match="slot still busy"):
            await worker.acquire_run(wait_timeout_s=0.2)
        assert asyncio.get_running_loop().time() - started < 1.5
    finally:
        await run_a.close()
    # The timed-out waiter must not have taken the slot on its way out.
    run_c = await asyncio.wait_for(worker.acquire_run(wait_timeout_s=0.2), timeout=2.0)
    await run_c.close()


async def test_default_wait_timeout_comes_from_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_AIAGENT_WARM_SLOT_WAIT_TIMEOUT", "0.1")
    worker = agent_real._AiagentWarmWorker(tmp_path)
    run_a = await worker.acquire_run()
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError, match="slot still busy"):
            await worker.acquire_run()
        assert asyncio.get_running_loop().time() - started < 1.5
    finally:
        await run_a.close()


def test_slot_waiter_cancelled_from_another_loop_does_not_leak(tmp_path: Path):
    """Cross-loop variant: the slot is a threading.Lock shared by every loop."""
    worker = agent_real._AiagentWarmWorker(tmp_path)
    holder_acquired = threading.Event()
    release_holder = threading.Event()
    waiter_cancelled = threading.Event()
    errors: list[BaseException] = []

    async def holder():
        run = await worker.acquire_run()
        holder_acquired.set()
        await asyncio.to_thread(release_holder.wait)
        await run.close()

    async def cancelled_waiter():
        task = asyncio.ensure_future(worker.acquire_run())
        await _spin_until(lambda: worker._slot_waiters() >= 1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        waiter_cancelled.set()

    def run_coro(coro):
        try:
            asyncio.run(coro())
        except BaseException as exc:  # pragma: no cover - surfaced via assert
            errors.append(exc)

    t_holder = threading.Thread(target=run_coro, args=(holder,))
    t_waiter = threading.Thread(target=run_coro, args=(cancelled_waiter,))
    try:
        t_holder.start()
        assert holder_acquired.wait(timeout=2)
        t_waiter.start()
        # Cancel while parked, THEN let the holder go — the incident ordering.
        assert waiter_cancelled.wait(timeout=5)
    finally:
        # Always let the holder out: a failed assertion above must turn red,
        # not hang the process on an executor thread parked in Event.wait.
        release_holder.set()
        t_holder.join(timeout=5)
        t_waiter.join(timeout=5)
    assert not t_holder.is_alive()
    assert not t_waiter.is_alive()
    assert errors == []

    assert not worker._lock.locked()
    assert worker._slot_waiters() == 0


async def test_oversized_or_non_finite_timeouts_are_clamped(monkeypatch, tmp_path: Path):
    worker = agent_real._AiagentWarmWorker(tmp_path)
    run_a = await worker.acquire_run()
    try:
        for raw in ("inf", "nan", "1e100", "-5", "abc", ""):
            monkeypatch.setenv("HERMES_AIAGENT_WARM_SLOT_WAIT_TIMEOUT", raw)
            value = agent_real._aiagent_warm_slot_wait_timeout_s()
            assert 0 < value <= threading.TIMEOUT_MAX, raw
        # A caller-supplied absurd value must not raise OverflowError either.
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(worker.acquire_run(wait_timeout_s=0.1), timeout=2.0)
        assert asyncio.get_running_loop().time() - started < 1.5
    finally:
        await run_a.close()
