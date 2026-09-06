"""Zero-model heartbeat over the canonical (kind, payload) event stream
(SPEC ticket 02).

A heartbeat is a timer projecting the CURRENT controlled state enum
(running / waiting_tool / waiting_gate / finishing) into the SAME canonical
``RunEventKind`` stream WebUI and Feishu already consume (``run_models.py``,
``_core.stream_run_agent`` -- SEAM MAP streaming_and_events) -- never a new
LLM call, never a raw Codex notification, never prompt/model deltas/tool
args/chain-of-thought/token/open_id/path. Its payload is always exactly
``{"state": <enum>, "text": <fixed Chinese string>}``, derived only from the
*kind* string of the last real event -- never from that event's payload, so
nothing a real event carries (an open_id, a path, a secret) can ever reach a
heartbeat.

Hook point: hermes_multitenancy/agent_real/_core.py's
``_verified_codex_stream`` -- the mapped-run event projection seam the SEAM
MAP names. That function currently buffers the *entire* raw per-item
subprocess stream (``[item async for item in stream]``) before releasing
anything to the caller, gated behind the spend-receipt check -- which is
exactly why no periodic signal ever reached the UI during a mapped run
before this ticket. The hook wraps the RAW stream with
:func:`wrap_with_heartbeat` and yields heartbeat items immediately
(bypassing the buffer -- they carry no content/spend implications), while
every other kind keeps the exact prior buffer-then-release-after-receipt
ordering untouched.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Callable, Optional

HEARTBEAT_KIND = "heartbeat"

DEFAULT_INTERVAL_MS = 15_000

_TERMINAL_KINDS = frozenset({"done", "error"})

# ponytail: fixed 4-state enum with static Chinese text; add a state only
# when a real kind exists to drive it -- no speculative growth.
_STATE_TEXT: dict[str, str] = {
    "running": "处理中…",
    "waiting_tool": "工具执行中…",
    "waiting_gate": "等待确认…",
    "finishing": "收尾中…",
}

_KIND_TO_STATE: dict[str, str] = {
    "content": "running",
    "thinking": "running",
    "tool_started": "waiting_tool",
    "tool_completed": "running",
    "approval_required": "waiting_gate",
    "approval_resolved": "running",
    "auth_required": "waiting_gate",
}

_STREAM_END = object()


def heartbeat_payload(state: str) -> dict[str, str]:
    """The ONLY data a heartbeat item ever carries. ``state`` outside the
    closed enum falls back to ``"running"`` rather than raising -- a
    heartbeat must never be the thing that crashes a run."""
    safe_state = state if state in _STATE_TEXT else "running"
    return {"state": safe_state, "text": _STATE_TEXT[safe_state]}


def _default_clock_ms() -> int:
    return int(time.monotonic() * 1000)


async def _safe_anext(stream: AsyncIterator[Any]) -> Any:
    try:
        return await stream.__anext__()
    except StopAsyncIteration:
        return _STREAM_END


async def wrap_with_heartbeat(
    stream: AsyncIterator[tuple[str, Any]],
    *,
    interval_ms: int = DEFAULT_INTERVAL_MS,
    clock_ms: Optional[Callable[[], int]] = None,
    initial_state: str = "running",
) -> AsyncIterator[tuple[str, Any]]:
    """Pass every item of ``stream`` straight through, interleaving a
    ``(HEARTBEAT_KIND, payload)`` item whenever no real item has arrived for
    ``interval_ms``. The timer resets on every real item. Once a terminal
    kind ("done"/"error") has been yielded, heartbeats stop firing but the
    wrapper keeps draining ``stream`` to natural exhaustion -- matching
    plain ``async for`` semantics, since trailing cleanup code in the
    wrapped generator can run after its last yield and callers (like the
    mapped-codex buffering loop) rely on that drain completing.
    """
    clock = clock_ms or _default_clock_ms
    state = initial_state
    terminal = False
    last_at = clock()
    fetch = asyncio.ensure_future(_safe_anext(stream))
    try:
        while True:
            if terminal:
                result = await fetch
                if result is _STREAM_END:
                    return
                yield result
                fetch = asyncio.ensure_future(_safe_anext(stream))
                continue

            remaining_ms = interval_ms - (clock() - last_at)
            if remaining_ms <= 0:
                yield HEARTBEAT_KIND, heartbeat_payload(state)
                last_at = clock()
                continue

            done, _pending = await asyncio.wait({fetch}, timeout=remaining_ms / 1000)
            if fetch not in done:
                continue  # timed out -> loop recomputes remaining -> heartbeat
            result = fetch.result()
            last_at = clock()
            if result is _STREAM_END:
                return
            kind, payload = result
            state = _KIND_TO_STATE.get(kind, state)
            yield result
            fetch = asyncio.ensure_future(_safe_anext(stream))
            if kind in _TERMINAL_KINDS:
                terminal = True
    finally:
        if not fetch.done():
            fetch.cancel()
