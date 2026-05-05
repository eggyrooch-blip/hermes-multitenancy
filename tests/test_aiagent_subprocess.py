from __future__ import annotations

import io
import json
import logging
import os
import sys
import asyncio
import contextvars
import types
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


def _event() -> SimpleNamespace:
    return SimpleNamespace(
        text="hello",
        message_id="om_test",
        source=SimpleNamespace(
            platform=SimpleNamespace(value="feishu"),
            chat_id="oc_test",
            chat_name="chat",
            chat_type="dm",
            user_id="ou_test",
            user_name="tester",
            user_id_alt="on_test",
            message_id="om_source",
        ),
    )


def _install_fake_feishu_oapi(monkeypatch) -> None:
    current_sender_open_id = contextvars.ContextVar("current_sender_open_id", default=None)

    @contextmanager
    def sender_open_id_scope(value):
        token = current_sender_open_id.set(value)
        try:
            yield
        finally:
            current_sender_open_id.reset(token)

    fake_feishu_oapi = SimpleNamespace(
        current_sender_open_id=current_sender_open_id,
        sender_open_id_scope=sender_open_id_scope,
        FEISHU_UAT_PATH=None,
        FEISHU_UAT_DIR=None,
    )
    tools_mod = sys.modules.get("tools") or types.ModuleType("tools")
    tools_mod.feishu_oapi_client = fake_feishu_oapi
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.feishu_oapi_client", fake_feishu_oapi)


def _install_fake_gateway_session_context(monkeypatch) -> None:
    session_env = contextvars.ContextVar("session_env", default={})

    def set_session_vars(**kwargs):
        env = {
            "HERMES_SESSION_PLATFORM": kwargs.get("platform", ""),
            "HERMES_SESSION_CHAT_ID": kwargs.get("chat_id", ""),
            "HERMES_SESSION_CHAT_NAME": kwargs.get("chat_name", ""),
            "HERMES_SESSION_THREAD_ID": kwargs.get("thread_id", ""),
            "HERMES_SESSION_USER_ID": kwargs.get("user_id", ""),
            "HERMES_SESSION_USER_NAME": kwargs.get("user_name", ""),
            "HERMES_SESSION_KEY": kwargs.get("session_key", ""),
        }
        return session_env.set(env)

    def clear_session_vars(token):
        session_env.reset(token)

    def get_session_env(name):
        return session_env.get({}).get(name)

    fake_session_context = SimpleNamespace(
        set_session_vars=set_session_vars,
        clear_session_vars=clear_session_vars,
        get_session_env=get_session_env,
    )
    gateway_mod = sys.modules.get("gateway") or types.ModuleType("gateway")
    gateway_mod.session_context = fake_session_context
    monkeypatch.setitem(sys.modules, "gateway", gateway_mod)
    monkeypatch.setitem(sys.modules, "gateway.session_context", fake_session_context)


@pytest.mark.asyncio
async def test_real_run_agent_uses_subprocess_runner(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    calls: list[tuple[str, Path]] = []

    async def fake_subprocess(event, profile_home):
        calls.append((event.text, profile_home))
        return "subprocess reply"

    async def fail_legacy(*_args, **_kwargs):
        raise AssertionError("legacy fallback should not be used on subprocess success")

    monkeypatch.setattr(agent_real, "_run_aiagent_subprocess", fake_subprocess)
    monkeypatch.setattr(agent_real, "_legacy_real_run_agent", fail_legacy)

    response = await agent_real.real_run_agent(_event(), tmp_path)

    assert response == "subprocess reply"
    assert calls == [("hello", tmp_path)]


@pytest.mark.asyncio
async def test_stream_run_agent_yields_subprocess_event_stream(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    async def fake_stream(event, profile_home):
        assert event.text == "hello"
        assert profile_home == tmp_path
        yield ("tool_started", {"name": "feishu_task_tasklist", "preview": "rename"})
        yield ("thinking", "正在调用飞书任务工具")
        yield ("content", "streamed ")
        yield ("content", "subprocess reply")
        yield ("done", "streamed subprocess reply")

    async def fail_subprocess(*_args, **_kwargs):
        raise AssertionError("stream_run_agent should consume the event-stream subprocess")

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", fake_stream, raising=False)
    monkeypatch.setattr(agent_real, "_run_aiagent_subprocess", fail_subprocess)

    chunks = [chunk async for chunk in agent_real.stream_run_agent(_event(), tmp_path)]

    assert chunks == [
        ("tool_started", {"name": "feishu_task_tasklist", "preview": "rename"}),
        ("thinking", "正在调用飞书任务工具"),
        ("content", "streamed "),
        ("content", "subprocess reply"),
    ]


def test_aiagent_subprocess_main_replays_event(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy import aiagent_subprocess

    seen: dict[str, object] = {}

    def fake_run(event, profile_home):
        seen["text"] = event.text
        seen["message_id"] = event.message_id
        seen["profile_home"] = profile_home
        seen["source_user_id"] = event.source.user_id
        seen["source_platform"] = event.source.platform
        return "ok from child"

    payload = {
        "event": {
            "text": "hello child",
            "message_id": "om_child",
            "source": {
                "platform": "feishu",
                "chat_id": "oc_child",
                "user_id": "ou_child",
            },
        },
        "profile_home": str(tmp_path),
    }

    stdout = io.StringIO()
    monkeypatch.setattr(agent_real, "_run_with_aiagent", fake_run)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", stdout)

    aiagent_subprocess.main()

    assert json.loads(stdout.getvalue()) == {"result": "ok from child", "error": None}
    assert seen == {
        "text": "hello child",
        "message_id": "om_child",
        "profile_home": tmp_path,
        "source_user_id": "ou_child",
        "source_platform": "feishu",
    }


def test_aiagent_subprocess_main_streams_ndjson_events(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy import aiagent_subprocess

    def fake_run(event, profile_home, *, event_sink=None):
        assert event.text == "hello child"
        assert profile_home == tmp_path
        assert event_sink is not None
        event_sink("tool_started", name="feishu_task_tasklist", preview="rename")
        event_sink("content", text="done")
        return "done"

    payload = {
        "event": {
            "text": "hello child",
            "message_id": "om_child",
            "source": {"platform": "feishu", "chat_id": "oc_child", "user_id": "ou_child"},
        },
        "profile_home": str(tmp_path),
    }

    stdout = io.StringIO()
    monkeypatch.setenv("HERMES_AIAGENT_EVENT_STREAM", "1")
    monkeypatch.setattr(agent_real, "_run_with_aiagent", fake_run)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", stdout)

    aiagent_subprocess.main()

    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines == [
        {"event": "tool_started", "name": "feishu_task_tasklist", "preview": "rename"},
        {"event": "content", "text": "done"},
        {"event": "done", "result": "done", "error": None},
    ]


def test_event_payload_carries_sender_open_id_from_raw_message(tmp_path: Path):
    from hermes_multitenancy import agent_real

    event = _event()
    event.source.user_id = "g41a5b5g"
    event.raw_message = {
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": "ou_raw_sender",
                    "union_id": "on_raw_sender",
                }
            }
        }
    }

    payload = agent_real._event_to_subprocess_payload(event, tmp_path)

    assert payload["event"]["sender_open_id"] == "ou_raw_sender"


def test_event_payload_carries_router_messages(tmp_path: Path):
    from hermes_multitenancy import agent_real

    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "created doc doxcn123"},
        {"role": "user", "content": "send me the link"},
    ]

    payload = agent_real._event_to_subprocess_payload(_event(), tmp_path, messages=messages)

    assert payload["messages"] == messages


def test_configure_feishu_uat_home_uses_default_home_for_named_profile(tmp_path: Path):
    from hermes_multitenancy import agent_real

    default_home = tmp_path / ".hermes"
    profile_home = default_home / "profiles" / "coder"
    fake_feishu_oapi = SimpleNamespace(
        FEISHU_UAT_PATH=profile_home / "feishu_uat.json",
        FEISHU_UAT_DIR=profile_home / "feishu_uat",
    )

    shared_home = agent_real._configure_feishu_uat_home(fake_feishu_oapi, profile_home)

    assert shared_home == default_home
    assert fake_feishu_oapi.FEISHU_UAT_PATH == default_home / "feishu_uat.json"
    assert fake_feishu_oapi.FEISHU_UAT_DIR == default_home / "feishu_uat"


def test_configure_cron_home_uses_shared_gateway_store(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / ".hermes" / "profiles" / "coder"
    shared_home = tmp_path / ".hermes"
    profile_home.mkdir(parents=True)
    shared_home.mkdir(exist_ok=True)

    cron_pkg = types.ModuleType("cron")
    cron_jobs = types.ModuleType("cron.jobs")
    tools_pkg = types.ModuleType("tools")
    cronjob_tools = types.ModuleType("tools.cronjob_tools")
    path_security = types.ModuleType("tools.path_security")
    path_security.validate_within_dir = lambda path, root: None
    cron_pkg.jobs = cron_jobs
    tools_pkg.cronjob_tools = cronjob_tools
    tools_pkg.path_security = path_security

    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.cronjob_tools", cronjob_tools)
    monkeypatch.setitem(sys.modules, "tools.path_security", path_security)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    agent_real._configure_cron_home(shared_home)

    assert cron_jobs.JOBS_FILE == shared_home.resolve() / "cron" / "jobs.json"
    assert cron_jobs.OUTPUT_DIR == shared_home.resolve() / "cron" / "output"
    assert os.environ["HERMES_HOME"] == str(profile_home)
    assert cronjob_tools._validate_cron_script_path("reminder.py") is None
    assert "relative to shared" in cronjob_tools._validate_cron_script_path("/tmp/reminder.py")


def test_tool_progress_logger_records_tool_names(caplog):
    from hermes_multitenancy import agent_real

    with caplog.at_level(logging.INFO, logger=agent_real.__name__):
        agent_real._log_aiagent_tool_progress(
            "tool.started",
            "feishu_calendar_list_events",
            "list events",
            {},
        )
        agent_real._log_aiagent_tool_progress(
            "tool.completed",
            "feishu_calendar_list_events",
            None,
            None,
            duration=1.25,
            is_error=False,
        )

    assert "[multitenancy] tool.started feishu_calendar_list_events" in caplog.text
    assert "[multitenancy] tool.completed feishu_calendar_list_events" in caplog.text


def test_run_with_aiagent_sets_gateway_session_context(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\nplatform_toolsets:\n  feishu:\n  - feishu_chat\n",
        encoding="utf-8",
    )
    (profile_home / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")

    seen: dict[str, str] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            seen["chat_id_kwarg"] = kwargs.get("chat_id", "")

        def run_conversation(self, user_message, task_id):
            from gateway.session_context import get_session_env

            seen["session_chat_id"] = get_session_env("HERMES_SESSION_CHAT_ID")
            seen["session_platform"] = get_session_env("HERMES_SESSION_PLATFORM")
            return {"final_response": "ok"}

        def cleanup(self):
            pass

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    _install_fake_feishu_oapi(monkeypatch)
    _install_fake_gateway_session_context(monkeypatch)

    event = _event()
    event.source.chat_id = "oc_current"
    event.source.platform = SimpleNamespace(value="feishu")

    assert agent_real._run_with_aiagent(event, profile_home) == "ok"
    assert seen == {
        "chat_id_kwarg": "oc_current",
        "session_chat_id": "oc_current",
        "session_platform": "feishu",
    }


def test_run_with_aiagent_passes_router_history_without_current_user(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\nplatform_toolsets:\n  feishu:\n  - feishu_chat\n",
        encoding="utf-8",
    )
    (profile_home / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")

    seen: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def run_conversation(self, user_message, task_id, conversation_history=None):
            seen["user_message"] = user_message
            seen["conversation_history"] = conversation_history
            return {"final_response": "ok"}

        def cleanup(self):
            pass

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    _install_fake_feishu_oapi(monkeypatch)

    event = _event()
    event.text = "send me the link"
    messages = [
        {"role": "user", "content": "create a doc"},
        {"role": "assistant", "content": "created doc doxcn123"},
        {"role": "user", "content": "send me the link"},
    ]

    assert agent_real._run_with_aiagent(event, profile_home, messages=messages) == "ok"
    assert seen == {
        "user_message": "send me the link",
        "conversation_history": [
            {"role": "user", "content": "create a doc"},
            {"role": "assistant", "content": "created doc doxcn123"},
        ],
    }


def test_aiagent_session_id_is_stable_across_feishu_message_ids(tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "coder"
    first = _event()
    second = _event()
    second.message_id = "om_next"
    second.source.message_id = "om_source_next"

    first_session = agent_real._resolve_aiagent_session_id(
        first,
        profile_home,
        "ou_sender",
    )
    second_session = agent_real._resolve_aiagent_session_id(
        second,
        profile_home,
        "ou_sender",
    )

    assert first_session == second_session
    assert "profile:coder" in first_session
    assert "chat:oc_test" in first_session
    assert "user:ou_sender" in first_session


def test_aiagent_session_id_isolates_users_in_same_feishu_chat(tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "coder"
    event = _event()

    assert agent_real._resolve_aiagent_session_id(event, profile_home, "ou_sunke") != (
        agent_real._resolve_aiagent_session_id(event, profile_home, "ou_yuan")
    )


def test_resolve_enabled_toolsets_merges_feishu_override_with_default_tools():
    from hermes_multitenancy import agent_real

    seen: dict[str, object] = {}

    def fake_get_platform_tools(config, platform, *, include_default_mcp_servers=True):
        seen["platform_toolsets"] = config.get("platform_toolsets")
        seen["platform"] = platform
        seen["include_default_mcp_servers"] = include_default_mcp_servers
        return {"web", "browser", "feishu_doc"}

    config = {
        "platform_toolsets": {
            "feishu": ["feishu_drive"],
        }
    }

    assert agent_real._resolve_enabled_toolsets(
        config,
        "feishu",
        platform_tools_resolver=fake_get_platform_tools,
    ) == ["browser", "feishu_doc", "feishu_drive", "web"]
    assert seen == {
        "platform_toolsets": {},
        "platform": "feishu",
        "include_default_mcp_servers": True,
    }


def test_resolve_enabled_toolsets_allows_explicit_mode(monkeypatch):
    from hermes_multitenancy import agent_real

    def fake_get_platform_tools(*_args, **_kwargs):
        raise AssertionError("explicit mode must not resolve platform defaults")

    monkeypatch.setenv("HERMES_MULTITENANCY_TOOLSETS_MODE", "explicit")

    assert agent_real._resolve_enabled_toolsets(
        {"platform_toolsets": {"feishu": ["feishu_drive", "web"]}},
        "feishu",
        platform_tools_resolver=fake_get_platform_tools,
    ) == ["feishu_drive", "web"]


def test_run_with_aiagent_forwards_stream_and_tool_events(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\nplatform_toolsets:\n  feishu:\n  - feishu_chat\n",
        encoding="utf-8",
    )
    (profile_home / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")

    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_conversation(self, user_message, task_id):
            self.kwargs["tool_progress_callback"](
                "tool.started", "feishu_task_tasklist", "rename", {"action": "patch"}
            )
            self.kwargs["reasoning_callback"]("我在处理工具结果")
            self.kwargs["stream_delta_callback"]("已完成")
            self.kwargs["tool_progress_callback"](
                "tool.completed",
                "feishu_task_tasklist",
                None,
                None,
                duration=1.2,
                is_error=False,
            )
            return {"final_response": "已完成"}

        def cleanup(self):
            pass

    events: list[dict] = []
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    _install_fake_feishu_oapi(monkeypatch)

    result = agent_real._run_with_aiagent(
        _event(),
        profile_home,
        event_sink=lambda event, **payload: events.append({"event": event, **payload}),
    )

    assert result == "已完成"
    assert events == [
        {
            "event": "tool_started",
            "name": "feishu_task_tasklist",
            "preview": "rename",
        },
        {"event": "thinking", "text": "我在处理工具结果"},
        {"event": "content", "text": "已完成"},
        {
            "event": "tool_completed",
            "name": "feishu_task_tasklist",
            "duration": 1.2,
            "is_error": False,
        },
    ]


@pytest.mark.asyncio
async def test_run_aiagent_subprocess_kills_child_when_cancelled(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    started = asyncio.Event()

    class FakeProc:
        def __init__(self):
            self.returncode = None
            self.killed = False

        async def communicate(self, _payload):
            started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    fake_proc = FakeProc()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    task = asyncio.create_task(agent_real._run_aiagent_subprocess(_event(), tmp_path))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake_proc.killed is True


@pytest.mark.asyncio
async def test_run_aiagent_subprocess_executes_sibling_script(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    seen_args: list[str] = []

    class FakeProc:
        returncode = 0

        async def communicate(self, _payload):
            return b'{"result": "ok", "error": null}', b""

    async def fake_create_subprocess_exec(*args, **_kwargs):
        seen_args.extend(str(arg) for arg in args)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    assert await agent_real._run_aiagent_subprocess(_event(), tmp_path) == "ok"
    assert seen_args[0] == sys.executable
    assert seen_args[1].endswith("aiagent_subprocess.py")
    assert "-m" not in seen_args
