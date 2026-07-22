import asyncio
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


def _event():
    return SimpleNamespace(
        text="hello",
        message_id="m1",
        sender_open_id="ou_alice",
        source=SimpleNamespace(
            platform=SimpleNamespace(value="webui"),
            chat_id="chat-1",
            chat_type="p2p",
            user_id="ou_alice",
        ),
        raw_event={
            "metadata": {
                "litellm_billing_enforced": True,
                "litellm_billing_employee_user_id": "alice",
            }
        },
    )


def _failure(text: str, *, retry_safe: bool):
    from hermes_multitenancy.agent_real import _subprocess_failure

    return _subprocess_failure(text, retry_safe=retry_safe)


def test_child_reports_if_a_failed_turn_is_safe_to_retry(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.aiagent_subprocess import _run_payload

    monkeypatch.delenv("HERMES_AIAGENT_EVENT_STREAM", raising=False)

    def run_before_side_effect(*_args, **_kwargs):
        raise RuntimeError("HTTP 401 invalid API key")

    previous_stdout = sys.stdout
    output = io.StringIO()
    try:
        _run_payload(
            {"event": {}, "profile_home": str(tmp_path)},
            run_before_side_effect,
            output,
        )
    finally:
        sys.stdout = previous_stdout
    assert json.loads(output.getvalue())["billing_retry_safe"] is True

    def run_after_tool(*_args, event_sink, **_kwargs):
        event_sink("tool_started", name="write")
        raise RuntimeError("HTTP 401 invalid API key")

    output = io.StringIO()
    try:
        _run_payload(
            {"event": {}, "profile_home": str(tmp_path)},
            run_after_tool,
            output,
        )
    finally:
        sys.stdout = previous_stdout
    assert json.loads(output.getvalue())["billing_retry_safe"] is False


def test_nonstream_401_repairs_then_retries_once(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.agent_real import _core

    event = _event()
    calls = 0
    repairs = 0

    async def run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _failure("HTTP 401 invalid API key", retry_safe=True)
        return "ok"

    def repair(target):
        nonlocal repairs
        repairs += 1
        target._hermes_billing_retry = True

    monkeypatch.setattr(_core, "_run_aiagent_subprocess", run)
    monkeypatch.setattr(_core, "_repair_billing_event", repair)

    assert asyncio.run(_core.real_run_agent(event, tmp_path)) == "ok"
    assert (calls, repairs) == (2, 1)


def test_nonstream_401_never_retries_after_a_tool_side_effect(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.agent_real import _core

    calls = 0
    invalidated = 0

    async def run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise _failure("HTTP 401 invalid API key", retry_safe=False)

    def mark(_event):
        nonlocal invalidated
        invalidated += 1

    monkeypatch.setattr(_core, "_run_aiagent_subprocess", run)
    monkeypatch.setattr(_core, "_mark_billing_event_invalid", mark)

    with pytest.raises(RuntimeError, match="未自动重试"):
        asyncio.run(_core.real_run_agent(_event(), tmp_path))
    assert (calls, invalidated) == (1, 1)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        ("HTTP 429 budget_exceeded", "本月 LiteLLM 额度已用尽"),
        ("HTTP 429 too many requests", "请求过于频繁"),
    ],
)
def test_429_types_have_distinct_messages_and_never_rotate(
    monkeypatch, tmp_path: Path, error: str, message: str
):
    from hermes_multitenancy.agent_real import _core

    async def run(*_args, **_kwargs):
        raise _failure(error, retry_safe=True)

    monkeypatch.setattr(_core, "_run_aiagent_subprocess", run)
    monkeypatch.setattr(
        _core,
        "_repair_billing_event",
        lambda _event: pytest.fail("429 must not rotate credentials"),
    )

    with pytest.raises(RuntimeError, match=message):
        asyncio.run(_core.real_run_agent(_event(), tmp_path))


def test_stream_401_retries_once_before_content_or_tools(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core

    calls = 0

    async def stream(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _failure("HTTP 401 invalid API key", retry_safe=True)
        yield "content", "ok"
        yield "done", "ok"

    def repair(target):
        target._hermes_billing_retry = True

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", stream)
    monkeypatch.setattr(_core, "_repair_billing_event", repair)

    async def collect():
        return [item async for item in _core.stream_run_agent(_event(), tmp_path)]

    assert asyncio.run(collect()) == [("content", "ok")]
    assert calls == 2


def test_stream_401_does_not_retry_after_tool_started(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core

    calls = 0

    async def stream(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        yield "tool_started", {"name": "write"}
        raise _failure("HTTP 401 invalid API key", retry_safe=True)

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", stream)
    monkeypatch.setattr(_core, "_mark_billing_event_invalid", lambda _event: None)

    async def collect():
        return [item async for item in _core.stream_run_agent(_event(), tmp_path)]

    with pytest.raises(RuntimeError, match="未自动重试"):
        asyncio.run(collect())
    assert calls == 1


def test_billing_key_is_redacted_from_child_diagnostics():
    from hermes_multitenancy.agent_real import _redact_billing_runtime_text

    key = "sk-employee-secret-1234567890"
    redacted = _redact_billing_runtime_text(
        f"Authorization: Bearer {key}; prefix={key[:12]}",
        _event(),
        {"HERMES_LITELLM_RUNTIME_API_KEY": key},
    )
    assert key not in redacted
    assert key[:12] not in redacted
    assert "[REDACTED:" in redacted
