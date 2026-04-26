"""Phase 2 Step 0 — Concurrency safety regression.

Architect verification (architect-verification.md Q4 P0) flagged a race in
runtime.py:72-82 where two overlapping ProfileRuntime.dispatch calls share
process-global os.environ. This test reproduces the race and serves as the
guard for the contextvars + asyncio.Lock fix.

Strategy: launch N concurrent dispatch tasks, each with a runner that
1. reads its observed environment / context
2. yields control mid-dispatch (forces interleaving)
3. reads again
4. asserts what it saw matches its OWN profile_home (no leakage)
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_concurrent_contextvar_isolation():
    """contextvars must isolate per-task profile context with zero leakage.

    Phase 2 fix introduces ``runtime.get_current_profile_home()`` backed by a
    ``contextvars.ContextVar``. Each asyncio task gets its own context copy,
    so concurrent dispatches cannot observe each other's profile.
    """
    from hermes_multitenancy.runtime import ProfileRuntime, get_current_profile_home

    N = 10
    observations: list[tuple[int, str, str | None]] = []

    with contextlib.ExitStack() as stack:
        homes: list[tuple[int, Path]] = []
        for i in range(N):
            tmp = stack.enter_context(tempfile.TemporaryDirectory())
            h = Path(tmp) / f"p{i}"
            h.mkdir()
            homes.append((i, h))

        async def runner(i: int, home: Path):
            async def agent(event, given_home: Path) -> str:
                # Observe at start
                ctx_start = get_current_profile_home()
                observations.append((i, "start", str(ctx_start) if ctx_start else None))
                # Yield to let other tasks interleave
                await asyncio.sleep(0.01)
                # Observe after yield — must still be our own home
                ctx_end = get_current_profile_home()
                observations.append((i, "end", str(ctx_end) if ctx_end else None))
                return "ok"

            rt = ProfileRuntime(profile_home=home, run_agent_fn=agent)
            await rt.dispatch(SimpleNamespace(text=f"msg-{i}"))

        await asyncio.gather(*[runner(i, h) for i, h in homes])

    # Build expected map
    expected = {i: str(h) for i, h in homes}

    # Every observation must match the task's own profile_home
    leaks = [
        (i, when, seen, expected[i])
        for i, when, seen in observations
        if seen != expected[i]
    ]
    assert not leaks, f"contextvar leaked across tasks: {leaks[:5]}"


@pytest.mark.asyncio
async def test_concurrent_dispatch_env_var_restored():
    """After all concurrent dispatches finish, HERMES_HOME must equal the original.

    Without serialization, two tasks each capture each other's profile_home as
    'original' and the final restore lands on the wrong value. The fix uses an
    asyncio.Lock around the env-var section so original capture is reliable.
    """
    from hermes_multitenancy.runtime import ProfileRuntime

    N = 10
    original = os.environ.get("HERMES_HOME")
    sentinel = "/tmp/hermes_multitenancy_test_sentinel_xyz"
    os.environ["HERMES_HOME"] = sentinel
    try:
        with contextlib.ExitStack() as stack:
            tasks = []
            for i in range(N):
                tmp = stack.enter_context(tempfile.TemporaryDirectory())
                h = Path(tmp) / f"p{i}"
                h.mkdir()

                async def agent(event, home):
                    await asyncio.sleep(0.005)
                    return "ok"

                rt = ProfileRuntime(profile_home=h, run_agent_fn=agent)
                tasks.append(rt.dispatch(SimpleNamespace(text=str(i))))

            await asyncio.gather(*tasks)

        # After all dispatches finish, env var must be back to sentinel — no
        # task should have left a stale profile_home behind.
        assert os.environ.get("HERMES_HOME") == sentinel, (
            f"HERMES_HOME leaked: expected {sentinel!r}, "
            f"got {os.environ.get('HERMES_HOME')!r}"
        )
    finally:
        if original is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = original
