from __future__ import annotations

import pytest

from hermes_multitenancy.card import tool_use_trace


@pytest.fixture(autouse=True)
def _reset_trace_store() -> None:
    tool_use_trace._reset_for_testing()


def test_record_tool_use_end_pairs_by_tool_call_id() -> None:
    tool_use_trace.start_tool_use_trace_run("session-1")
    tool_use_trace.record_tool_use_start(
        "session-1",
        "exec",
        tool_params={"command": "echo start"},
        tool_call_id="call-1",
    )
    tool_use_trace.record_tool_use_end(
        "session-1",
        "exec",
        result={"ok": True},
        tool_call_id="call-1",
        duration_ms=12,
    )

    steps = tool_use_trace.get_tool_use_trace_steps("session-1")
    assert len(steps) == 1
    assert steps[0]["status"] == "success"
    assert steps[0]["tool_call_id"] == "call-1"
    assert steps[0]["result"] == {"ok": True}
    assert steps[0]["duration_ms"] == 12


def test_record_tool_use_end_pairs_by_params_fingerprint() -> None:
    tool_use_trace.start_tool_use_trace_run("session-2")
    tool_use_trace.record_tool_use_start(
        "session-2",
        "grep",
        tool_params={"glob": "tests/*.py", "pattern": "needle"},
    )
    tool_use_trace.record_tool_use_end(
        "session-2",
        "grep",
        result="done",
        tool_params={"pattern": "needle", "glob": "tests/*.py"},
    )

    steps = tool_use_trace.get_tool_use_trace_steps("session-2")
    assert len(steps) == 1
    assert steps[0]["status"] == "success"
    assert steps[0]["result"] == "done"


def test_record_tool_use_end_pairs_by_name_only_when_needed() -> None:
    tool_use_trace.start_tool_use_trace_run("session-3")
    tool_use_trace.record_tool_use_start("session-3", "browser")
    tool_use_trace.record_tool_use_end("session-3", "browser", error="failed")

    steps = tool_use_trace.get_tool_use_trace_steps("session-3")
    assert len(steps) == 1
    assert steps[0]["status"] == "error"
    assert steps[0]["error"] == "failed"


def test_record_tool_use_end_appends_when_no_pending_step_exists() -> None:
    tool_use_trace.start_tool_use_trace_run("session-4")
    tool_use_trace.record_tool_use_end(
        "session-4",
        "exec",
        result="orphaned completion",
        duration_ms=7,
    )

    steps = tool_use_trace.get_tool_use_trace_steps("session-4")
    assert len(steps) == 1
    assert steps[0]["status"] == "success"
    assert steps[0]["result"] == "orphaned completion"
    assert steps[0]["duration_ms"] == 7


def test_trace_ttl_expiry_returns_empty_list() -> None:
    tool_use_trace.start_tool_use_trace_run("session-ttl")
    tool_use_trace._session_traces["session-ttl"]["updated_at"] -= (
        tool_use_trace.TRACE_TTL_MS + 1
    )

    assert tool_use_trace.get_tool_use_trace_steps("session-ttl") == []
    assert "session-ttl" not in tool_use_trace._session_traces


def test_step_cap_evicts_oldest_entries() -> None:
    tool_use_trace.start_tool_use_trace_run("session-cap")
    for index in range(tool_use_trace.MAX_STEPS_PER_SESSION + 1):
        tool_use_trace.record_tool_use_start(
            "session-cap",
            "exec",
            tool_params={"command": f"echo {index}"},
        )

    steps = tool_use_trace.get_tool_use_trace_steps("session-cap")
    assert len(steps) == tool_use_trace.MAX_STEPS_PER_SESSION
    assert steps[0]["id"] == "2"
    assert steps[-1]["id"] == str(tool_use_trace.MAX_STEPS_PER_SESSION + 1)


def test_running_timeout_surfaces_as_error_without_mutating_store() -> None:
    tool_use_trace.start_tool_use_trace_run("session-timeout")
    tool_use_trace.record_tool_use_start(
        "session-timeout",
        "exec",
        tool_params={"command": "sleep 999"},
    )
    stored = tool_use_trace._session_traces["session-timeout"]["steps"][0]
    stored["started_at"] -= tool_use_trace.STEP_RUNNING_TIMEOUT_MS + 1

    steps = tool_use_trace.get_tool_use_trace_steps("session-timeout")
    assert steps[0]["status"] == "error"
    assert steps[0]["error"] == "timed out"
    assert stored["status"] == "running"
    assert "error" not in stored


def test_sensitive_keys_and_url_query_values_are_redacted() -> None:
    sanitized = tool_use_trace.sanitize_trace_value(
        {
            "client_secret": "top-secret",
            "url": "https://example.com?a=1&api_key=shhh",
            "command": "curl https://example.com?token=secret",
        }
    )

    assert sanitized["client_secret"] == "[redacted]"
    assert sanitized["url"] == "https://example.com?a=1&api_key=[redacted]"
    assert "token=[redacted]" in sanitized["command"]
