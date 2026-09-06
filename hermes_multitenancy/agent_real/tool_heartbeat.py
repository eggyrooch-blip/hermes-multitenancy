"""Worker-side liveness while a tool call is executing.

The parent (``agent_real/streaming.py``) kills the AIAgent child after
``HERMES_AIAGENT_SUBPROCESS_TIMEOUT`` (300s) of stream silence, but the child
emits nothing between ``tool_started`` and ``tool_completed``. Any tool that
legitimately runs longer than that (terminal allows 600s foreground) therefore
died as "中途出错" with zero log lines — prod 2026-09-03, a 5-minute
``./oup adobe init`` in sunke's Feishu DM.

While at least one tool is in flight this emits a private ``tool_heartbeat``
event every ``interval_s`` so the parent can tell "tool still running" from
"worker dead". ``max_s`` is the ceiling: once the OLDEST in-flight call has run
longer than that, heartbeats stop for the whole run (a younger sibling must not
keep vouching for a hung one), so a genuinely hung tool still falls back to the
watchdog instead of becoming an immortal turn.

# ponytail: one daemon thread + a dict; per-tool timers would be more code
# for the same signal.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

EVENT = "tool_heartbeat"
DEFAULT_INTERVAL_S = 30.0
DEFAULT_MAX_S = 1800.0
INTERVAL_ENV = "HERMES_AIAGENT_TOOL_HEARTBEAT_SECONDS"
MAX_ENV = "HERMES_AIAGENT_TOOL_HEARTBEAT_MAX_SECONDS"


def _positive_finite(value: Any, default: float) -> float:
    """Only a finite, strictly positive number is a usable interval/ceiling;
    ``inf`` would make ``Event.wait`` raise and ``nan`` would never fire."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number <= 0:
        return default
    return number


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return _positive_finite(raw, default)


class ToolHeartbeat:
    def __init__(
        self,
        emit: Callable[..., Any],
        *,
        interval_s: float | None = None,
        max_s: float | None = None,
    ) -> None:
        self._emit = emit
        self.interval_s = (
            _positive_finite(interval_s, DEFAULT_INTERVAL_S)
            if interval_s is not None
            else _env_float(INTERVAL_ENV, DEFAULT_INTERVAL_S)
        )
        self.max_s = (
            _positive_finite(max_s, DEFAULT_MAX_S)
            if max_s is not None
            else _env_float(MAX_ENV, DEFAULT_MAX_S)
        )
        self._inflight: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = False
        self._thread: threading.Thread | None = None

    def started(self, tool_call_id: str, tool_name: str) -> None:
        if not tool_call_id:
            # No stable id means completed() could never clear it; an orphan
            # entry would vouch for a dead worker for up to max_s. Skip liveness.
            logger.debug("[multitenancy] tool heartbeat skipped: tool %r has no tool_call_id", tool_name)
            return
        with self._lock:
            # A duplicate start for the same id must not reset its clock.
            self._inflight.setdefault(tool_call_id, (tool_name, time.monotonic()))
            if self._thread is None and not self._stopped:
                self._thread = threading.Thread(
                    target=self._run, name="hermes-tool-heartbeat", daemon=True
                )
                self._thread.start()

    def completed(self, tool_call_id: str) -> None:
        if not tool_call_id:
            return
        with self._lock:
            self._inflight.pop(tool_call_id, None)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._inflight.clear()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _snapshot(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            items = [
                {"tool_call_id": key, "name": name, "elapsed": round(now - started, 1)}
                for key, (name, started) in self._inflight.items()
            ]
        if not items:
            return []
        if max(item["elapsed"] for item in items) > self.max_s:
            # Ceiling reached by the oldest call: stop vouching for the run.
            return []
        return items

    def _run(self) -> None:
        while not self._stopped:
            self._wake.wait(self.interval_s)
            if self._stopped:
                return
            inflight = self._snapshot()
            if not inflight:
                continue
            try:
                self._emit(EVENT, inflight=inflight)
            except Exception:
                # The heartbeat must never be what crashes a run.
                logger.debug("[multitenancy] tool heartbeat emit failed", exc_info=True)
