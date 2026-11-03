import asyncio
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import types

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


def test_nonstream_401_degrades_then_retries_once(monkeypatch, tmp_path: Path):
    """`billing-runtime-never-mints`: a 401 no longer re-mints on this path.

    It marks the credential invalid (so the hourly sweep re-provisions) and
    strips attribution so THIS attempt still completes — on the shared key,
    unbilled. The employee still sees a normal answer, which is the whole point;
    what changed is that no key is issued from the employee's own request.
    """
    from hermes_multitenancy.agent_real import _core

    event = _event()
    calls = 0
    degrades = 0
    marks = 0

    async def run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _failure("HTTP 401 invalid API key", retry_safe=True)
        return "ok"

    def degrade(target):
        nonlocal degrades
        degrades += 1
        trace.append("degrade")
        target._hermes_billing_retry = True

    def mark(_target):
        nonlocal marks
        marks += 1
        trace.append("mark")

    trace: list[str] = []
    monkeypatch.setattr(_core, "_run_aiagent_subprocess", run)
    monkeypatch.setattr(_core, "_degrade_billing_event", degrade)
    monkeypatch.setattr(_core, "_mark_billing_event_invalid", mark)

    assert asyncio.run(_core.real_run_agent(event, tmp_path)) == "ok"
    assert (calls, degrades, marks) == (2, 1, 1)
    # ORDER matters and counters cannot see it (codex #p1): degrade strips the
    # billing metadata that `mark_invalid` needs to find the row, so marking
    # second would silently stop the sweep from ever re-provisioning.
    assert trace == ["mark", "degrade"], trace


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
        "_degrade_billing_event",
        lambda _event: pytest.fail("429 must not degrade or rotate credentials"),
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

    def degrade(target):
        target._hermes_billing_retry = True

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", stream)
    monkeypatch.setattr(_core, "_degrade_billing_event", degrade)
    monkeypatch.setattr(_core, "_mark_billing_event_invalid", lambda _e: None)

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


def test_auxiliary_tasks_force_employee_key_and_restore_after_run():
    from hermes_multitenancy.agent_real.billing_auxiliary import (
        install_billing_auxiliary_runtime,
    )

    seen = {}
    evicted = []

    def task_resolver(*_args, **_kwargs):
        return "openrouter", "task-model", "https://other/v1", "other-key", None

    def provider_resolver(
        provider,
        model=None,
        *,
        explicit_base_url=None,
        explicit_api_key=None,
        api_mode=None,
    ):
        seen.update(
            provider=provider,
            model=model,
            base_url=explicit_base_url,
            api_key=explicit_api_key,
            api_mode=api_mode,
        )
        return object(), model

    fake = SimpleNamespace(
        _resolve_task_provider_model=task_resolver,
        resolve_provider_client=provider_resolver,
        _evict_cached_clients=evicted.append,
    )
    cleanup = install_billing_auxiliary_runtime(
        fake,
        model="main-model",
        base_url="https://litellm.example/v1",
        api_key="employee-key",
        api_mode="chat_completions",
    )

    assert fake._resolve_task_provider_model("compression") == (
        "custom",
        "task-model",
        "https://litellm.example/v1",
        "employee-key",
        "chat_completions",
    )
    fake.resolve_provider_client("openrouter", "title-model")
    assert seen == {
        "provider": "custom",
        "model": "title-model",
        "base_url": "https://litellm.example/v1",
        "api_key": "employee-key",
        "api_mode": "chat_completions",
    }

    cleanup()
    assert fake._resolve_task_provider_model is task_resolver
    assert fake.resolve_provider_client is provider_resolver
    assert evicted == ["custom"]


def test_billed_vision_accepts_keyword_model_without_duplicate_binding():
    from hermes_multitenancy.agent_real.billing_auxiliary import (
        install_billing_auxiliary_runtime,
    )

    calls = []

    def task_resolver(*_args, **_kwargs):
        return "custom", "vision-model", None, None, None

    def provider_resolver(
        provider,
        model=None,
        async_mode=False,
        *,
        explicit_base_url=None,
        explicit_api_key=None,
        main_runtime=None,
    ):
        calls.append(
            {
                "provider": provider,
                "model": model,
                "async_mode": async_mode,
                "base_url": explicit_base_url,
                "api_key": explicit_api_key,
                "main_runtime": main_runtime,
            }
        )
        return object(), model

    fake = SimpleNamespace(
        _resolve_task_provider_model=task_resolver,
        resolve_provider_client=provider_resolver,
    )
    cleanup = install_billing_auxiliary_runtime(
        fake,
        model="main-model",
        base_url="https://billing-approved.example/v1",
        api_key="employee-key",
    )
    runtime = {"source": "employee-billing"}

    fake.resolve_provider_client(
        "custom",
        model="vision-model",
        async_mode=True,
        main_runtime=runtime,
    )

    assert calls == [
        {
            "provider": "custom",
            "model": "vision-model",
            "async_mode": True,
            "base_url": "https://billing-approved.example/v1",
            "api_key": "employee-key",
            "main_runtime": runtime,
        }
    ]
    cleanup()


def test_billing_delegation_guard_allows_model_only_and_rejects_credentials(
    monkeypatch,
):
    from hermes_multitenancy.agent_real.run import _install_billing_delegation_guard

    delegate = types.ModuleType("tools.delegate_tool")

    def resolve(config, _parent):
        return {
            "model": config.get("model"),
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }

    delegate._resolve_delegation_credentials = resolve
    tools_module = types.ModuleType("tools")
    tools_module.delegate_tool = delegate
    monkeypatch.setitem(sys.modules, "tools", tools_module)
    monkeypatch.setitem(sys.modules, "tools.delegate_tool", delegate)

    cleanup = _install_billing_delegation_guard(True)
    assert delegate._resolve_delegation_credentials(
        {"model": "another-model"}, object()
    )["model"] == "another-model"
    with pytest.raises(ValueError, match="cannot override"):
        delegate._resolve_delegation_credentials(
            {"model": "another-model", "provider": "openrouter"}, object()
        )
    cleanup()
    assert delegate._resolve_delegation_credentials is resolve


def test_profile_env_cannot_inject_ai_gateway_service_bearer(tmp_path: Path):
    from hermes_multitenancy.agent_real import _core

    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    (profile / ".env").write_text(
        "HERMES_AI_GATEWAY_BROKER_TOKEN=must-not-enter-child\n"
        "OPENAI_API_KEY=legacy-model-key\n",
        encoding="utf-8",
    )

    loaded = _core._profile_env_for_aiagent(profile)

    assert "HERMES_AI_GATEWAY_BROKER_TOKEN" not in loaded
    assert loaded["OPENAI_API_KEY"] == "legacy-model-key"
