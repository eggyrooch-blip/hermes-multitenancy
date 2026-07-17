"""RunBroker target-state contract tests."""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest


def test_run_request_requires_profile_user_and_content():
    from hermes_multitenancy.run_models import RunRequest

    with pytest.raises(ValueError, match="profile_name"):
        RunRequest(channel="feishu", profile_name="", user_key="ou_1", content="hi")

    with pytest.raises(ValueError, match="user_key"):
        RunRequest(channel="feishu", profile_name="owner", user_key="", content="hi")

    with pytest.raises(ValueError, match="content"):
        RunRequest(channel="feishu", profile_name="owner", user_key="ou_1", content="")


def test_run_request_rejects_unknown_channel():
    from hermes_multitenancy.run_models import RunRequest

    with pytest.raises(ValueError, match="channel"):
        RunRequest(channel="email", profile_name="owner", user_key="ou_1", content="hi")


def test_run_request_builds_stable_idempotency_key():
    from hermes_multitenancy.run_models import RunRequest

    request = RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        message_id="om_1",
    )

    assert request.effective_idempotency_key == "feishu:owner:ou_1:om_1"


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
        profile_name="owner",
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
        profile_name="owner",
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


def test_run_broker_prepares_request_before_dedupe_and_dispatch():
    from dataclasses import replace

    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    prepared = []
    dispatched = []

    async def prepare(request):
        prepared.append(request.message_id)
        return replace(request, metadata={"litellm_billing_user_id": "user-1"})

    async def dispatch(request):
        dispatched.append(request.metadata)
        return "ok"

    broker = RunBroker(
        dispatch_agent=dispatch,
        prepare_request=prepare,
        mark_seen=lambda request: request.message_id != "duplicate",
    )
    duplicate = RunRequest(
        channel="feishu", profile_name="owner", user_key="ou_1",
        content="hi", message_id="duplicate",
    )
    fresh = replace(duplicate, message_id="fresh")

    asyncio.run(broker.run(duplicate))
    asyncio.run(broker.run(fresh))

    assert prepared == ["duplicate", "fresh"]
    assert dispatched == [{"litellm_billing_user_id": "user-1"}]


def test_run_broker_prepare_failure_does_not_consume_idempotency_key():
    from dataclasses import replace

    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    prepare_attempts = 0
    seen = set()
    dispatched = []

    async def prepare(request):
        nonlocal prepare_attempts
        prepare_attempts += 1
        if prepare_attempts == 1:
            raise RuntimeError("temporary billing lookup failure")
        return replace(request, metadata={"litellm_billing_user_id": "user-1"})

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen:
            return False
        seen.add(key)
        return True

    async def dispatch(request):
        dispatched.append(request.metadata)
        return "ok"

    broker = RunBroker(
        dispatch_agent=dispatch,
        prepare_request=prepare,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        idempotency_key="webui:session-1:turn-1",
    )

    with pytest.raises(RuntimeError, match="temporary billing lookup failure"):
        asyncio.run(broker.run(request))
    retry = asyncio.run(broker.run(request))
    duplicate = asyncio.run(broker.run(request))

    assert retry.content == "ok"
    assert duplicate.duplicate is True
    assert len(seen) == 1
    assert dispatched == [{"litellm_billing_user_id": "user-1"}]


def test_run_broker_public_admit_prepares_before_consuming_idempotency():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    prepare_attempts = 0
    marked = []

    async def prepare(request):
        nonlocal prepare_attempts
        prepare_attempts += 1
        if prepare_attempts == 1:
            raise RuntimeError("temporary billing lookup failure")
        return request

    broker = RunBroker(
        dispatch_agent=lambda _request: "should not run",
        prepare_request=prepare,
        mark_seen=lambda request: marked.append(request.effective_idempotency_key) or True,
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        idempotency_key="webui:session-1:turn-1",
    )

    with pytest.raises(RuntimeError, match="temporary billing lookup failure"):
        asyncio.run(broker.admit(request))
    admitted = asyncio.run(broker.admit(request))

    assert admitted.duplicate is False
    assert marked == ["webui:session-1:turn-1"]


def test_run_broker_policy_check_has_no_prepare_or_idempotency_side_effects():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    broker = RunBroker(
        dispatch_agent=lambda _request: "should not run",
        prepare_request=lambda _request: (_ for _ in ()).throw(AssertionError("must not prepare")),
        mark_seen=lambda _request: (_ for _ in ()).throw(AssertionError("must not mark")),
        sandbox_available=lambda: True,
    )
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
    )

    assert broker.check_policy(request) == request


def test_run_broker_rejects_policy_before_preparing_request():
    from hermes_multitenancy.run_broker import RunBroker, RunRejected
    from hermes_multitenancy.run_models import RunRequest

    prepared = []

    async def prepare(request):
        prepared.append(request)
        return request

    broker = RunBroker(
        dispatch_agent=lambda _request: "should not run",
        prepare_request=prepare,
        sandbox_available=lambda: False,
    )
    request = RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        requires_host_tools=True,
    )

    with pytest.raises(RunRejected, match="sandbox"):
        asyncio.run(broker.run(request))

    assert prepared == []


def test_router_mark_seen_dedupes_webui_with_explicit_idempotency_key(tmp_path):
    from hermes_multitenancy import router
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest
    from hermes_multitenancy.sessions import SessionStore

    router.override_session_store(SessionStore(tmp_path / "sessions.db"))
    calls = []

    async def dispatch(request):
        calls.append(request.content)
        return "ok"

    broker = RunBroker(
        dispatch_agent=dispatch,
        mark_seen=router._mark_run_request_seen,
        sandbox_available=lambda: True,
    )

    first = asyncio.run(broker.run(RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="first turn content with enough words to avoid short-content exemptions",
        idempotency_key="webui:session-1:turn-1",
    )))
    second = asyncio.run(broker.run(RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content="retry payload may differ but the explicit turn key is the same",
        idempotency_key="webui:session-1:turn-1",
    )))

    router.override_session_store(None)

    assert first.duplicate is False
    assert second.duplicate is True
    assert calls == ["first turn content with enough words to avoid short-content exemptions"]


def test_router_mark_seen_does_not_content_dedupe_webui_without_explicit_key(tmp_path):
    from hermes_multitenancy import router
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest
    from hermes_multitenancy.sessions import SessionStore

    router.override_session_store(SessionStore(tmp_path / "sessions.db"))
    calls = []

    async def dispatch(request):
        calls.append(request.content)
        return "ok"

    broker = RunBroker(
        dispatch_agent=dispatch,
        mark_seen=router._mark_run_request_seen,
        sandbox_available=lambda: True,
    )
    content = "same webui prompt that is long enough to trigger the old content hash fallback"

    first = asyncio.run(broker.run(RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content=content,
    )))
    second = asyncio.run(broker.run(RunRequest(
        channel="webui",
        profile_name="owner",
        user_key="ou_1",
        content=content,
    )))

    router.override_session_store(None)

    assert first.duplicate is False
    assert second.duplicate is False
    assert calls == [content, content]


def test_router_mark_seen_keeps_content_dedupe_for_non_webui_without_explicit_key(tmp_path):
    from hermes_multitenancy import router
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest
    from hermes_multitenancy.sessions import SessionStore

    router.override_session_store(SessionStore(tmp_path / "sessions.db"))
    calls = []

    async def dispatch(request):
        calls.append(request.content)
        return "ok"

    broker = RunBroker(
        dispatch_agent=dispatch,
        mark_seen=router._mark_run_request_seen,
        sandbox_available=lambda: True,
    )
    content = "same feishu prompt that remains protected by content hash fallback dedupe"

    first = asyncio.run(broker.run(RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content=content,
    )))
    second = asyncio.run(broker.run(RunRequest(
        channel="feishu",
        profile_name="owner",
        user_key="ou_1",
        content=content,
    )))

    router.override_session_store(None)

    assert first.duplicate is False
    assert second.duplicate is True
    assert calls == [content]


def test_run_broker_admit_prepared_runs_policy_and_dedupe_without_dispatch():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    calls = []
    seen = set()

    async def dispatch(request):
        calls.append(request.content)
        return "ok"

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
        profile_name="owner",
        user_key="ou_1",
        content="hi",
        message_id="om_1",
    )

    first = asyncio.run(broker.admit_prepared(request))
    second = asyncio.run(broker.admit_prepared(request))

    assert first.duplicate is False
    assert second.duplicate is True
    assert calls == []


def test_run_broker_run_can_skip_admission_after_prior_admit():
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    calls = []

    async def dispatch(request):
        calls.append(("dispatch", request.content))
        return "ok"

    def mark_seen(_request):
        raise AssertionError("admitted run should not repeat idempotency check")

    broker = RunBroker(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )

    result = asyncio.run(broker.run(
        RunRequest(
            channel="feishu",
            profile_name="owner",
            user_key="ou_1",
            content="hi",
            message_id="om_1",
        ),
        admitted=True,
    ))

    assert result.content == "ok"
    assert result.duplicate is False
    assert calls == [("dispatch", "hi")]


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
            profile_name="owner",
            user_key="ou_1",
            content="hi",
        )
    ))

    assert result.content == "hello"
    assert events == [
        ("content", "hello"),
        ("done", ""),
    ]


def test_run_broker_rewrites_hades_alias_before_dispatch(monkeypatch):
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.run_models import RunRequest

    fake_skill_commands = SimpleNamespace(
        get_skill_commands=lambda: {"/kep-hades-cli": {"name": "kep-hades-cli"}},
        resolve_skill_command_key=lambda command: "/kep-hades-cli"
        if command.replace("_", "-") == "kep-hades-cli"
        else None,
        build_skill_invocation_message=lambda cmd_key, user_instruction, task_id=None: (
            f"[skill:{cmd_key} task:{task_id}] {user_instruction}"
        ),
    )
    monkeypatch.setitem(sys.modules, "agent", ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.skill_commands", fake_skill_commands)
    # the broker now scopes the skill loader per-profile before rewriting; stub
    # tools.skills_tool so _scope_profile_skill_loader can set SKILLS_DIR in any env
    # (the per-profile scope must establish, or the rewrite fails closed).
    fake_skills_tool = SimpleNamespace(HERMES_HOME=None, SKILLS_DIR=None)
    monkeypatch.setitem(sys.modules, "tools", ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.skills_tool", fake_skills_tool)

    seen = []

    async def dispatch(request):
        seen.append(request.content)
        return "ok"

    broker = RunBroker(dispatch_agent=dispatch, sandbox_available=lambda: True)

    result = asyncio.run(broker.run(
        RunRequest(
            channel="webui",
            profile_name="owner",
            user_key="owner",
            session_id="webui-session-1",
            content="/hades get 69df030c1f01cb45ba7ff585",
        )
    ))

    assert result.content == "ok"
    assert seen == [
        "[skill:/kep-hades-cli task:webui-session-1] get 69df030c1f01cb45ba7ff585"
    ]
