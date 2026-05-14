"""RunBroker target-state contract tests."""
from __future__ import annotations

import asyncio

import pytest


def test_run_request_requires_profile_user_and_content():
    from hermes_multitenancy.run_models import RunRequest

    with pytest.raises(ValueError, match="profile_name"):
        RunRequest(channel="feishu", profile_name="", user_key="ou_1", content="hi")

    with pytest.raises(ValueError, match="user_key"):
        RunRequest(channel="feishu", profile_name="sunke", user_key="", content="hi")

    with pytest.raises(ValueError, match="content"):
        RunRequest(channel="feishu", profile_name="sunke", user_key="ou_1", content="")


def test_run_request_rejects_unknown_channel():
    from hermes_multitenancy.run_models import RunRequest

    with pytest.raises(ValueError, match="channel"):
        RunRequest(channel="email", profile_name="sunke", user_key="ou_1", content="hi")


def test_run_request_builds_stable_idempotency_key():
    from hermes_multitenancy.run_models import RunRequest

    request = RunRequest(
        channel="feishu",
        profile_name="sunke",
        user_key="ou_1",
        content="hi",
        message_id="om_1",
    )

    assert request.effective_idempotency_key == "feishu:sunke:ou_1:om_1"


def test_run_broker_rejects_host_tool_run_without_sandbox():
    from hermes_multitenancy.run_broker import RunBroker, RunRejected
    from hermes_multitenancy.run_models import RunRequest

    async def dispatch(_request):
        return "should not run"

    broker = RunBroker(
        dispatch_agent=dispatch,
        sandbox_available=lambda: False,
    )
    request = RunRequest(
        channel="webui",
        profile_name="sunke",
        user_key="ou_1",
        content="hi",
        requires_host_tools=True,
    )

    with pytest.raises(RunRejected, match="sandbox"):
        asyncio.run(broker.run(request))


def test_run_broker_dedupes_before_dispatch():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    calls = []

    async def dispatch(request):
        calls.append(request.content)
        return "ok"

    seen = set()

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen:
            return False
        seen.add(key)
        return True

    broker = RunBroker(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="feishu",
        profile_name="sunke",
        user_key="ou_1",
        content="hi",
        message_id="om_1",
    )

    first = asyncio.run(broker.run(request))
    second = asyncio.run(broker.run(request))

    assert first.content == "ok"
    assert first.duplicate is False
    assert second.content == ""
    assert second.duplicate is True
    assert calls == ["hi"]


def test_run_broker_emits_channel_neutral_events():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunEvent, RunRequest

    events = []

    async def dispatch(_request):
        return "hello"

    def emit(event: RunEvent):
        events.append((event.kind, event.text))

    broker = RunBroker(
        dispatch_agent=dispatch,
        emit_event=emit,
        sandbox_available=lambda: True,
    )

    result = asyncio.run(broker.run(
        RunRequest(
            channel="webui",
            profile_name="sunke",
            user_key="ou_1",
            content="hi",
        )
    ))

    assert result.content == "hello"
    assert events == [
        ("content", "hello"),
        ("done", ""),
    ]
