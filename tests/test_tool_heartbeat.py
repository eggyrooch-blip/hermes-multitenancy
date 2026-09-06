"""Worker-side tool heartbeat: keeps the parent watchdog fed while a tool runs,
stops on completion, and stops vouching after ``max_s`` (prod 2026-09-03: a
5-minute ``./oup adobe init`` was killed at 300s as 中途出错)."""
from __future__ import annotations

import threading
import time

import pytest

from hermes_multitenancy.agent_real.tool_heartbeat import (
    EVENT,
    INTERVAL_ENV,
    MAX_ENV,
    ToolHeartbeat,
)


def _collector():
    got: list[tuple[str, dict]] = []
    lock = threading.Lock()

    def emit(name: str, **payload):
        with lock:
            got.append((name, payload))

    return got, emit


def _wait_for(pred, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_emits_while_tool_in_flight_and_stops_after_completion():
    got, emit = _collector()
    hb = ToolHeartbeat(emit, interval_s=0.02, max_s=60)
    try:
        hb.started("call-1", "terminal")
        assert _wait_for(lambda: len(got) >= 2)
        name, payload = got[0]
        assert name == EVENT
        assert payload["inflight"][0]["tool_call_id"] == "call-1"
        assert payload["inflight"][0]["name"] == "terminal"
        assert payload["inflight"][0]["elapsed"] >= 0
        hb.completed("call-1")
        seen = len(got)
        time.sleep(0.15)
        # At most one tick that was already mid-flight when completed() ran.
        assert len(got) <= seen + 1
    finally:
        hb.stop()


def test_stops_vouching_after_max_seconds():
    got, emit = _collector()
    hb = ToolHeartbeat(emit, interval_s=0.02, max_s=0.06)
    try:
        hb.started("call-1", "terminal")
        time.sleep(0.3)
        assert got, "expected heartbeats before max_s"
        assert all(
            item["elapsed"] <= 0.06 + 0.03
            for _, payload in got
            for item in payload["inflight"]
        ), got
        seen = len(got)
        time.sleep(0.1)
        assert len(got) == seen, "heartbeats must stop once the tool outlives max_s"
    finally:
        hb.stop()


def test_stop_joins_thread_and_silences():
    got, emit = _collector()
    hb = ToolHeartbeat(emit, interval_s=0.02, max_s=60)
    hb.started("call-1", "terminal")
    assert _wait_for(lambda: len(got) >= 1)
    hb.stop()
    assert hb._thread is not None and not hb._thread.is_alive()
    seen = len(got)
    time.sleep(0.1)
    assert len(got) == seen


def test_emit_failure_never_propagates():
    def boom(name: str, **payload):
        raise RuntimeError("pipe closed")

    hb = ToolHeartbeat(boom, interval_s=0.01, max_s=60)
    hb.started("call-1", "terminal")
    time.sleep(0.1)
    assert hb._thread is not None and hb._thread.is_alive()
    hb.stop()


def test_env_overrides_and_bad_values(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(INTERVAL_ENV, "7")
    monkeypatch.setenv(MAX_ENV, "nonsense")
    hb = ToolHeartbeat(lambda *a, **k: None)
    assert hb.interval_s == 7.0
    assert hb.max_s == 1800.0
    monkeypatch.delenv(INTERVAL_ENV)
    monkeypatch.delenv(MAX_ENV)
    assert ToolHeartbeat(lambda *a, **k: None).interval_s == 30.0


def test_empty_tool_call_id_never_registers_liveness():
    got, emit = _collector()
    hb = ToolHeartbeat(emit, interval_s=0.01, max_s=60)
    try:
        hb.started("", "terminal")
        time.sleep(0.08)
        assert got == [], "an id-less call must not vouch for the worker"
        hb.completed("")  # must be a harmless no-op
    finally:
        hb.stop()


def test_duplicate_start_keeps_original_clock():
    got, emit = _collector()
    hb = ToolHeartbeat(emit, interval_s=0.02, max_s=60)
    try:
        hb.started("call-1", "terminal")
        time.sleep(0.05)
        hb.started("call-1", "terminal")
        before = len(got)
        assert _wait_for(lambda: len(got) > before)
        # A fresh tick after the duplicate start still measures from the first start.
        assert got[-1][1]["inflight"][0]["elapsed"] >= 0.05
    finally:
        hb.stop()


def test_oldest_call_over_ceiling_stops_all_heartbeats():
    got, emit = _collector()
    hb = ToolHeartbeat(emit, interval_s=0.02, max_s=0.08)
    try:
        hb.started("old", "terminal")
        time.sleep(0.06)
        hb.started("young", "lark_cli")  # started shortly before old's ceiling
        time.sleep(0.15)
        assert got, "heartbeats expected while old was under the ceiling"
        assert all(
            max(item["elapsed"] for item in payload["inflight"]) <= 0.08 + 0.03
            for _, payload in got
        ), got
        seen = len(got)
        time.sleep(0.1)
        assert len(got) == seen, "a younger sibling must not keep vouching past the oldest call's ceiling"
    finally:
        hb.stop()


@pytest.mark.parametrize("bad", ["inf", "nan", "0", "-5", "1e999", "abc"])
def test_nonfinite_or_nonpositive_config_falls_back(monkeypatch: pytest.MonkeyPatch, bad: str):
    monkeypatch.setenv(INTERVAL_ENV, bad)
    monkeypatch.setenv(MAX_ENV, bad)
    hb = ToolHeartbeat(lambda *a, **k: None)
    assert hb.interval_s == 30.0 and hb.max_s == 1800.0
    hb2 = ToolHeartbeat(lambda *a, **k: None, interval_s=float("inf"), max_s=float("nan"))
    assert hb2.interval_s == 30.0 and hb2.max_s == 1800.0
