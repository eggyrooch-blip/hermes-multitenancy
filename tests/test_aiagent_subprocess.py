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


def _install_fake_approval(monkeypatch):
    registered: dict[str, object] = {}
    resolved: list[tuple[str, str]] = []
    current_session = contextvars.ContextVar("approval_session", default="")

    def set_current_session_key(session_key: str):
        return current_session.set(session_key)

    def reset_current_session_key(token):
        current_session.reset(token)

    def register_gateway_notify(session_key: str, cb):
        registered[session_key] = cb

    def unregister_gateway_notify(session_key: str):
        registered.pop(session_key, None)

    def resolve_gateway_approval(session_key: str, choice: str, resolve_all=False):
        resolved.append((session_key, choice))
        return 1

    fake_approval = SimpleNamespace(
        set_current_session_key=set_current_session_key,
        reset_current_session_key=reset_current_session_key,
        register_gateway_notify=register_gateway_notify,
        unregister_gateway_notify=unregister_gateway_notify,
        resolve_gateway_approval=resolve_gateway_approval,
    )
    tools_mod = sys.modules.get("tools") or types.ModuleType("tools")
    tools_mod.approval = fake_approval
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.approval", fake_approval)
    return registered, resolved


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


@pytest.mark.asyncio
async def test_stream_aiagent_subprocess_forwards_child_approval_events(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    class FakeStdin:
        def write(self, _payload):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class FakeStdout:
        def __init__(self):
            self.lines = [
                json.dumps({
                    "event": "approval_required",
                    "session_key": "multitenancy:feishu:coder:oc_test:ou_test",
                    "approval_id": "approval_1",
                    "command": "python -c \"print(1)\"",
                    "description": "script execution via -e/-c flag",
                    "decision_path": "/tmp/approval_1.json",
                }).encode() + b"\n",
                json.dumps({
                    "event": "approval_resolved",
                    "session_key": "multitenancy:feishu:coder:oc_test:ou_test",
                    "approval_id": "approval_1",
                    "choice": "once",
                }).encode() + b"\n",
                b'{"event": "done", "result": "ok", "error": null}\n',
            ]

        async def readline(self):
            if self.lines:
                return self.lines.pop(0)
            return b""

    class FakeStderr:
        async def read(self):
            return b""

    class FakeProc:
        def __init__(self):
            self.stdin = FakeStdin()
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()
            self.pid = 123
            self.returncode = None
            self.killed = False

        async def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            self.killed = True
            self.returncode = -9

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    events = [
        item
        async for item in agent_real._stream_aiagent_subprocess(_event(), tmp_path)
    ]

    assert events == [
        ("approval_required", {
            "session_key": "multitenancy:feishu:coder:oc_test:ou_test",
            "approval_id": "approval_1",
            "command": "python -c \"print(1)\"",
            "description": "script execution via -e/-c flag",
            "decision_path": "/tmp/approval_1.json",
        }),
        ("approval_resolved", {
            "session_key": "multitenancy:feishu:coder:oc_test:ou_test",
            "approval_id": "approval_1",
            "choice": "once",
        }),
        ("done", "ok"),
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


def test_configure_feishu_uat_home_binds_to_profile_local_dir(tmp_path: Path):
    """档 A change: FEISHU_UAT_DIR points at <profile>/feishu_uat/, not shared."""
    from hermes_multitenancy import agent_real

    default_home = tmp_path / ".hermes"
    profile_home = default_home / "profiles" / "coder"
    fake_feishu_oapi = SimpleNamespace(
        FEISHU_UAT_PATH=None,
        FEISHU_UAT_DIR=None,
    )

    shared_home = agent_real._configure_feishu_uat_home(fake_feishu_oapi, profile_home)

    # shared_home is still derived correctly (cron etc. still need it).
    assert shared_home == default_home
    # But UAT lookups are now scoped to the profile.
    assert fake_feishu_oapi.FEISHU_UAT_PATH == profile_home / "feishu_uat.json"
    assert fake_feishu_oapi.FEISHU_UAT_DIR == profile_home / "feishu_uat"
    # And the dir is created mode 0700.
    import stat as stat_mod
    mode = stat_mod.S_IMODE((profile_home / "feishu_uat").stat().st_mode)
    assert mode == 0o700


def test_configure_feishu_uat_home_does_not_leak_other_profiles_dir(tmp_path: Path):
    """Pointing two profiles at the same fake_feishu_oapi must give them disjoint dirs."""
    from hermes_multitenancy import agent_real

    default_home = tmp_path / ".hermes"
    alice = default_home / "profiles" / "alice"
    bob = default_home / "profiles" / "bob"
    fake = SimpleNamespace(FEISHU_UAT_PATH=None, FEISHU_UAT_DIR=None)

    agent_real._configure_feishu_uat_home(fake, alice)
    assert fake.FEISHU_UAT_DIR == alice / "feishu_uat"

    agent_real._configure_feishu_uat_home(fake, bob)
    assert fake.FEISHU_UAT_DIR == bob / "feishu_uat"
    # The previous binding must not still be reachable through any property.
    assert alice / "feishu_uat" != bob / "feishu_uat"


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


def test_multitenant_gateway_session_key_matches_router_shape(tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "coder"
    event = _event()

    assert agent_real._resolve_multitenant_gateway_session_key(
        event,
        profile_home,
        "ou_test",
    ) == "multitenancy:feishu:coder:oc_test:ou_test"


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


def test_run_with_aiagent_bridges_gateway_approval_to_event_sink(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\nplatform_toolsets:\n  feishu:\n  - feishu_chat\n",
        encoding="utf-8",
    )
    (profile_home / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")
    approval_dir = tmp_path / "approval"
    monkeypatch.setenv("HERMES_MULTITENANCY_APPROVAL_DIR", str(approval_dir))
    monkeypatch.setenv("HERMES_MULTITENANCY_APPROVAL_TIMEOUT", "1")

    registered, resolved = _install_fake_approval(monkeypatch)

    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_conversation(self, user_message, task_id):
            session_key = self.kwargs["gateway_session_key"]
            assert session_key == "multitenancy:feishu:coder:oc_test:ou_test"
            notify = registered[session_key]
            notify({
                "command": "python -c 'print(1)'",
                "description": "script execution via -c flag",
                "pattern_keys": ["script execution via -c flag"],
            })
            return {"final_response": "approved"}

        def cleanup(self):
            pass

    events: list[dict] = []

    def sink(event, **payload):
        events.append({"event": event, **payload})
        if event == "approval_required":
            Path(payload["decision_path"]).write_text(
                json.dumps({"choice": "once"}),
                encoding="utf-8",
            )

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    _install_fake_feishu_oapi(monkeypatch)
    _install_fake_gateway_session_context(monkeypatch)

    result = agent_real._run_with_aiagent(_event(), profile_home, event_sink=sink)

    assert result == "approved"
    assert events[0]["event"] == "approval_required"
    assert events[0]["session_key"] == "multitenancy:feishu:coder:oc_test:ou_test"
    assert events[0]["command"] == "python -c 'print(1)'"
    assert events[1]["event"] == "approval_resolved"
    assert events[1]["choice"] == "once"
    assert resolved == [("multitenancy:feishu:coder:oc_test:ou_test", "once")]


def test_approval_bridge_exposes_session_key_to_tool_worker_threads(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    approval_dir = tmp_path / "approval"
    monkeypatch.setenv("HERMES_MULTITENANCY_APPROVAL_DIR", str(approval_dir))
    monkeypatch.setenv("HERMES_MULTITENANCY_APPROVAL_TIMEOUT", "1")
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    registered, resolved = _install_fake_approval(monkeypatch)
    events: list[dict] = []

    def sink(event, **payload):
        events.append({"event": event, **payload})
        if event == "approval_required":
            Path(payload["decision_path"]).write_text(
                json.dumps({"choice": "once"}),
                encoding="utf-8",
            )

    cleanup = agent_real._configure_gateway_approval_bridge(
        sink,
        "multitenancy:feishu:coder:oc_test:ou_test",
    )
    try:
        worker_result: dict[str, str] = {}

        def worker_thread_tool_guard():
            session_key = os.getenv("HERMES_SESSION_KEY", "default")
            notify = registered.get(session_key)
            if notify is None:
                worker_result["status"] = "missing"
                return
            notify({
                "command": "python -c 'print(1)'",
                "description": "script execution via -c flag",
                "pattern_keys": ["script execution via -c flag"],
            })
            worker_result["status"] = "notified"

        worker_thread_tool_guard()

        assert worker_result == {"status": "notified"}
        assert events[0]["event"] == "approval_required"
        assert resolved == [("multitenancy:feishu:coder:oc_test:ou_test", "once")]
    finally:
        cleanup()

    assert os.getenv("HERMES_SESSION_KEY") is None
    assert os.getenv("HERMES_GATEWAY_SESSION") is None
    assert os.getenv("HERMES_EXEC_ASK") is None


def test_run_with_aiagent_closes_real_agent_resources(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\n",
        encoding="utf-8",
    )
    (profile_home / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")
    seen = {"closed": False}

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def run_conversation(self, user_message, task_id):
            return {"final_response": "ok"}

        def close(self):
            seen["closed"] = True

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    _install_fake_feishu_oapi(monkeypatch)

    assert agent_real._run_with_aiagent(_event(), profile_home) == "ok"
    assert seen["closed"] is True


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


# ---------------------------------------------------------------------------
# 档 A — env whitelist + HOME/XDG/TMPDIR pivot
# ---------------------------------------------------------------------------


def test_build_subprocess_env_drops_non_allowlisted_parent_env(monkeypatch, tmp_path: Path):
    """Parent env not on the allowlist (e.g. ambient OPENAI_API_KEY) must NOT leak."""
    from hermes_multitenancy import agent_real

    # Simulate gateway process having a bunch of secret env vars set.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parent-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-parent")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-parent")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-parent")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # allowlisted — must survive
    monkeypatch.setenv("PYTHONUNBUFFERED", "1")  # allowlisted — must survive

    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    env = agent_real._build_subprocess_env(tmp_path / "profile", approval_dir=approval_dir)

    # Allowlisted keys carry over verbatim.
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["PYTHONUNBUFFERED"] == "1"

    # Secret-ish env keys must be absent.
    for leaked in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITLAB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
    ):
        assert leaked not in env, f"{leaked} leaked into subprocess env"


def test_build_subprocess_env_pivots_xdg_and_tmpdir_into_profile(tmp_path: Path):
    """XDG_* / TMPDIR redirect to per-profile dirs, isolating skill caches.

    HOME is deliberately NOT pivoted — host CLI tools (kep-cli, aws, etc.)
    rely on ``Path.home() / ".kep-cli"`` style paths to find their state, so
    rebinding HOME would break them. See _build_subprocess_env docstring.
    """
    from hermes_multitenancy import agent_real

    profile = tmp_path / "profiles" / "alice"
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)

    assert env["XDG_CACHE_HOME"] == str(profile / "cache")
    assert env["XDG_CONFIG_HOME"] == str(profile / "config")
    assert env["XDG_STATE_HOME"] == str(profile / "state")
    assert env["XDG_DATA_HOME"] == str(profile / "data")
    assert env["TMPDIR"] == str(profile / "tmp")

    # HOME must NOT be pivoted to <profile>/home. Either it stays inherited
    # from parent (PATH/etc. allowlist did not include HOME, so it should be
    # absent unless added) or it's empty. The point is that Path.home() inside
    # the child still resolves to the user's real home, so host-installed
    # CLIs (kep-cli, aws) can find ~/.kep-cli, ~/.aws etc.
    if "HOME" in env:
        assert env["HOME"] != str(profile / "home"), (
            "HOME must not pivot to profile — that would break host CLIs that "
            "use Path.home() to locate their per-user state directories."
        )

    # XDG/TMPDIR pivot dirs must exist and be private (mode 0700).
    for sub in ("cache", "config", "state", "data", "tmp"):
        d = profile / sub
        assert d.is_dir(), f"{sub} not created"
        mode = d.stat().st_mode & 0o777
        assert mode == 0o700, f"{sub} mode is {oct(mode)}, expected 0o700"

    # ``home/`` subdir is no longer created automatically by env build —
    # provisioned by sync only if explicit (kept for tools that ask for a
    # per-profile-home location, but it's not the HOME env target).
    assert not (profile / "home").exists()


def test_build_subprocess_env_sets_hermes_plumbing(tmp_path: Path):
    """HERMES_HOME / SHARED_HOME / approval_dir / event_stream flag plumbed correctly."""
    from hermes_multitenancy import agent_real

    profile = tmp_path / "profiles" / "bob"
    approval_dir = tmp_path / "approval-bob"
    approval_dir.mkdir()

    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)
    assert env["HERMES_HOME"] == str(profile)
    assert env["HERMES_GATEWAY_SESSION"] == "1"
    assert env["HERMES_EXEC_ASK"] == "1"
    assert env["HERMES_MULTITENANCY_APPROVAL_DIR"] == str(approval_dir)
    assert "HERMES_AIAGENT_EVENT_STREAM" not in env

    stream_env = agent_real._build_subprocess_env(
        profile, approval_dir=approval_dir, event_stream=True
    )
    assert stream_env["HERMES_AIAGENT_EVENT_STREAM"] == "1"


def test_build_subprocess_env_extra_overrides_default_keys(tmp_path: Path):
    """`extra` parameter wins over defaults so callers can override per-spawn."""
    from hermes_multitenancy import agent_real

    profile = tmp_path / "profiles" / "carol"
    approval_dir = tmp_path / "approval-carol"
    approval_dir.mkdir()

    env = agent_real._build_subprocess_env(
        profile,
        approval_dir=approval_dir,
        extra={"HERMES_MAX_ITERATIONS": "5", "CUSTOM_KEY": "hello"},
    )
    assert env["HERMES_MAX_ITERATIONS"] == "5"
    assert env["CUSTOM_KEY"] == "hello"


@pytest.mark.asyncio
async def test_run_aiagent_subprocess_passes_sanitized_env_to_child(
    monkeypatch, tmp_path: Path
):
    """End-to-end: spawned child receives the sanitized env, not parent's full env."""
    from hermes_multitenancy import agent_real

    monkeypatch.setenv("OPENAI_API_KEY", "sk-parent-leak")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    captured: dict[str, dict[str, str]] = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, _payload):
            return b'{"result": "ok", "error": null}', b""

    async def fake_create_subprocess_exec(*_args, env=None, **_kwargs):
        captured["env"] = dict(env or {})
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    profile = tmp_path / "profiles" / "isolated"
    assert await agent_real._run_aiagent_subprocess(_event(), profile) == "ok"

    child_env = captured["env"]
    assert "OPENAI_API_KEY" not in child_env, "parent secret leaked to child"
    # HOME is NOT pivoted to profile (would break host CLIs like kep-cli).
    # If HOME survives the allowlist (it currently does not) it must not point
    # at the profile.
    if "HOME" in child_env:
        assert child_env["HOME"] != str(profile / "home")
    assert child_env["TMPDIR"] == str(profile / "tmp")
    assert child_env["HERMES_HOME"] == str(profile)


# ---------------------------------------------------------------------------
# 档 B — sandbox-exec wrapper
# ---------------------------------------------------------------------------


def test_wrap_with_sandbox_is_noop_when_disabled(monkeypatch, tmp_path: Path):
    """Default behaviour: HERMES_USE_SANDBOX unset returns cmd unchanged."""
    from hermes_multitenancy import agent_real

    monkeypatch.delenv("HERMES_USE_SANDBOX", raising=False)
    cmd = ["/usr/bin/python3", "child.py"]
    wrapped = agent_real._wrap_with_sandbox(cmd, tmp_path / "profile")
    assert wrapped == cmd, "sandbox wrap must be a no-op when disabled"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec backend")
def test_wrap_with_sandbox_per_profile_gate_excludes_others(monkeypatch, tmp_path: Path):
    """HERMES_SANDBOX_PROFILES allowlist scopes sandboxing to listed profiles only."""
    import os as _os
    from hermes_multitenancy import agent_real

    fake_policy = tmp_path / "p.sb"
    fake_policy.write_text("(version 1) (deny default)")
    fake_bin = tmp_path / "sandbox-exec"
    fake_bin.write_text("#!/bin/sh\nexec \"$@\"\n")
    _os.chmod(fake_bin, 0o755)

    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_SANDBOX_PROFILES", "spike_test, researcher")
    monkeypatch.setattr(agent_real, "_SANDBOX_POLICY_FILE", fake_policy)
    monkeypatch.setattr(agent_real, "_SANDBOX_EXEC", str(fake_bin))

    cmd = ["/usr/bin/python3", "child.py"]

    # Listed profile → wrapped.
    spike = tmp_path / "profiles" / "spike_test"
    spike.mkdir(parents=True)
    wrapped_spike = agent_real._wrap_with_sandbox(cmd, spike)
    assert wrapped_spike[0] == str(fake_bin), "spike_test must be sandboxed"

    # Unlisted profile → bypass.
    prod = tmp_path / "profiles" / "feishu_g41a5b5g"
    prod.mkdir(parents=True)
    wrapped_prod = agent_real._wrap_with_sandbox(cmd, prod)
    assert wrapped_prod == cmd, "unlisted profile must NOT be sandboxed during pilot"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec backend")
def test_wrap_with_sandbox_empty_allowlist_means_all(monkeypatch, tmp_path: Path):
    """HERMES_SANDBOX_PROFILES unset (or empty) → all profiles get sandboxed."""
    import os as _os
    from hermes_multitenancy import agent_real

    fake_policy = tmp_path / "p.sb"
    fake_policy.write_text("(version 1) (deny default)")
    fake_bin = tmp_path / "sandbox-exec"
    fake_bin.write_text("#!/bin/sh\nexec \"$@\"\n")
    _os.chmod(fake_bin, 0o755)

    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.delenv("HERMES_SANDBOX_PROFILES", raising=False)
    monkeypatch.setattr(agent_real, "_SANDBOX_POLICY_FILE", fake_policy)
    monkeypatch.setattr(agent_real, "_SANDBOX_EXEC", str(fake_bin))

    profile = tmp_path / "profiles" / "anybody"
    profile.mkdir(parents=True)
    wrapped = agent_real._wrap_with_sandbox(["/usr/bin/python3"], profile)
    assert wrapped[0] == str(fake_bin), "no allowlist → everyone is sandboxed"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only fallback semantics; Linux fails closed (raises)")
def test_wrap_with_sandbox_falls_back_when_policy_missing(monkeypatch, tmp_path: Path):
    """HERMES_USE_SANDBOX=1 but policy file missing → unsandboxed with warning."""
    from hermes_multitenancy import agent_real

    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setattr(agent_real, "_SANDBOX_POLICY_FILE", tmp_path / "nonexistent.sb")

    cmd = ["/usr/bin/python3", "child.py"]
    wrapped = agent_real._wrap_with_sandbox(cmd, tmp_path / "profile")
    assert wrapped == cmd, (
        "Missing policy must fall back to unsandboxed exec (loud warning), "
        "not silently crash or block the gateway."
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only fallback semantics; Linux fails closed (raises)")
def test_wrap_with_sandbox_falls_back_when_binary_missing(monkeypatch, tmp_path: Path):
    """If /usr/bin/sandbox-exec is not executable (non-mac), no-op + warning."""
    from hermes_multitenancy import agent_real

    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setattr(agent_real, "_SANDBOX_EXEC", str(tmp_path / "fake-sandbox-exec"))

    cmd = ["/usr/bin/python3", "child.py"]
    wrapped = agent_real._wrap_with_sandbox(cmd, tmp_path / "profile")
    assert wrapped == cmd


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec argv shape")
def test_wrap_with_sandbox_builds_full_invocation(monkeypatch, tmp_path: Path):
    """HERMES_USE_SANDBOX=1 + policy present + binary present → full wrap."""
    import os as _os
    from hermes_multitenancy import agent_real

    fake_policy = tmp_path / "sandbox" / "profile-default.sb"
    fake_policy.parent.mkdir()
    fake_policy.write_text("(version 1) (deny default)")
    fake_bin = tmp_path / "bin" / "sandbox-exec"
    fake_bin.parent.mkdir()
    fake_bin.write_text("#!/bin/sh\nexec \"$@\"\n")
    _os.chmod(fake_bin, 0o755)

    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_AGENT_REPO", str(tmp_path / "agent-repo"))
    monkeypatch.setattr(agent_real, "_SANDBOX_POLICY_FILE", fake_policy)
    monkeypatch.setattr(agent_real, "_SANDBOX_EXEC", str(fake_bin))

    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)

    cmd = ["/usr/bin/python3", "child.py"]
    wrapped = agent_real._wrap_with_sandbox(cmd, profile)

    assert wrapped[0] == str(fake_bin)
    assert wrapped[1] == "-f"
    assert wrapped[2] == str(fake_policy)

    # All required -D params must be present.
    params = {}
    i = 3
    while i < len(wrapped) - len(cmd):
        if wrapped[i] == "-D":
            key, _, value = wrapped[i + 1].partition("=")
            params[key] = value
            i += 2
        else:
            break
    for required in ("PROFILE_HOME", "SHARED_HOME", "USER_HOME",
                     "HERMES_VENV", "HERMES_AGENT_REPO", "HERMES_MT_REPO"):
        assert required in params, f"-D {required}=... missing from sandbox-exec args"

    # Original cmd is appended verbatim at the end.
    assert wrapped[-len(cmd):] == cmd


def test_sandbox_policy_file_is_valid_syntax():
    """Smoke test: the shipped profile-default.sb parses successfully."""
    import subprocess
    from pathlib import Path as _P

    if not _P("/usr/bin/sandbox-exec").is_file():
        import pytest as _pytest
        _pytest.skip("sandbox-exec only available on macOS")

    from hermes_multitenancy import agent_real
    policy = agent_real._SANDBOX_POLICY_FILE
    assert policy.is_file(), f"policy {policy} not bundled with plugin"

    result = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-f", str(policy),
            "-D", "PROFILE_HOME=/tmp/probe-profile",
            "-D", "SHARED_HOME=/tmp/probe-shared",
            "-D", "USER_HOME=/tmp/probe-user",
            "-D", "HERMES_VENV=/tmp/probe-venv",
            "-D", "HERMES_AGENT_INSTALL=/tmp/probe-install",
            "-D", "HERMES_AGENT_REPO=/tmp/probe-agent",
            "-D", "HERMES_MT_REPO=/tmp/probe-mt",
            "/usr/bin/true",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"sandbox-exec rejected policy {policy}:\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )


# ---------------------------------------------------------------------------
# 档 B — Linux bwrap backend (cross-platform — runs on macOS via platform mock)
# ---------------------------------------------------------------------------


def test_render_bwrap_args_strips_comments_and_substitutes(tmp_path: Path):
    """_render_bwrap_args splits whitespace, drops comments, substitutes ${KEY}."""
    from hermes_multitenancy import agent_real

    text = (
        "# leading comment\n"
        "\n"
        "--ro-bind /usr /usr   # inline comment\n"
        "--bind ${PROFILE_HOME} ${PROFILE_HOME}\n"
        "  --chdir ${PROFILE_HOME}  \n"
    )
    tokens = agent_real._render_bwrap_args(text, {"PROFILE_HOME": "/p"})
    assert tokens == [
        "--ro-bind", "/usr", "/usr",
        "--bind", "/p", "/p",
        "--chdir", "/p",
    ]


def test_wrap_linux_bwrap_raises_when_policy_missing(monkeypatch, tmp_path: Path):
    """Linux backend is fail-closed: missing policy raises (no silent fallback)."""
    from hermes_multitenancy import agent_real

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setattr(agent_real, "_BWRAP_ARGS_FILE", tmp_path / "nonexistent.args")

    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="bwrap policy.*is missing"):
        agent_real._wrap_with_sandbox(["/usr/bin/python3"], profile)


def test_wrap_linux_bwrap_raises_when_binary_missing(monkeypatch, tmp_path: Path):
    """Linux backend is fail-closed: missing bwrap binary raises."""
    from hermes_multitenancy import agent_real

    fake_policy = tmp_path / "bwrap.args"
    fake_policy.write_text("--die-with-parent\n--bind ${PROFILE_HOME} ${PROFILE_HOME}\n")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setattr(agent_real, "_BWRAP_ARGS_FILE", fake_policy)
    monkeypatch.setattr(agent_real, "_BWRAP_EXEC", str(tmp_path / "no-such-bwrap"))

    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="bwrap is not.*executable"):
        agent_real._wrap_with_sandbox(["/usr/bin/python3"], profile)


def test_wrap_linux_bwrap_builds_full_invocation(monkeypatch, tmp_path: Path):
    """Linux backend assembles bwrap + args + -- + cmd in that order."""
    import os as _os
    from hermes_multitenancy import agent_real

    fake_policy = tmp_path / "bwrap.args"
    fake_policy.write_text(
        "--die-with-parent\n"
        "--ro-bind /usr /usr\n"
        "--bind ${PROFILE_HOME} ${PROFILE_HOME}\n"
        "--chdir ${PROFILE_HOME}\n"
    )
    fake_bin = tmp_path / "bwrap"
    fake_bin.write_text("#!/bin/sh\nexec \"$@\"\n")
    _os.chmod(fake_bin, 0o755)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_AGENT_REPO", str(tmp_path / "agent-repo"))
    monkeypatch.setattr(agent_real, "_BWRAP_ARGS_FILE", fake_policy)
    monkeypatch.setattr(agent_real, "_BWRAP_EXEC", str(fake_bin))

    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    cmd = ["/usr/bin/python3", "child.py"]
    wrapped = agent_real._wrap_with_sandbox(cmd, profile)

    assert wrapped[0] == str(fake_bin)
    # PROFILE_HOME substitution happened.
    profile_resolved = str(profile.resolve())
    assert profile_resolved in wrapped
    # -- separator before user cmd.
    assert "--" in wrapped
    sep_idx = wrapped.index("--")
    assert wrapped[sep_idx + 1:] == cmd


def test_wrap_linux_bwrap_per_profile_gate_excludes_others(monkeypatch, tmp_path: Path):
    """Per-profile allowlist works the same way on Linux as on macOS."""
    import os as _os
    from hermes_multitenancy import agent_real

    fake_policy = tmp_path / "bwrap.args"
    fake_policy.write_text("--die-with-parent\n")
    fake_bin = tmp_path / "bwrap"
    fake_bin.write_text("#!/bin/sh\nexec \"$@\"\n")
    _os.chmod(fake_bin, 0o755)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_SANDBOX_PROFILES", "spike_test")
    monkeypatch.setattr(agent_real, "_BWRAP_ARGS_FILE", fake_policy)
    monkeypatch.setattr(agent_real, "_BWRAP_EXEC", str(fake_bin))

    cmd = ["/usr/bin/python3"]

    # Unlisted profile bypasses the sandbox.
    prod = tmp_path / "profiles" / "feishu_g41a5b5g"
    prod.mkdir(parents=True)
    assert agent_real._wrap_with_sandbox(cmd, prod) == cmd

    # Listed profile gets wrapped.
    spike = tmp_path / "profiles" / "spike_test"
    spike.mkdir(parents=True)
    wrapped = agent_real._wrap_with_sandbox(cmd, spike)
    assert wrapped[0] == str(fake_bin)


def test_wrap_unknown_platform_is_noop_with_info_log(monkeypatch, tmp_path: Path, caplog):
    """Non-darwin, non-linux platforms log INFO and return cmd unchanged."""
    from hermes_multitenancy import agent_real

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")

    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    cmd = ["/usr/bin/python3"]
    with caplog.at_level(logging.INFO, logger="hermes_multitenancy.agent_real"):
        wrapped = agent_real._wrap_with_sandbox(cmd, profile)

    assert wrapped == cmd
    assert any("no sandbox backend" in rec.message for rec in caplog.records)


def test_bwrap_default_args_is_valid_syntax():
    """Smoke test: shipped bwrap-default.args renders without unresolved placeholders."""
    from hermes_multitenancy import agent_real

    args_file = agent_real._BWRAP_ARGS_FILE
    assert args_file.is_file(), f"policy {args_file} not bundled with plugin"

    tokens = agent_real._render_bwrap_args(args_file.read_text(), {
        "PROFILE_HOME": "/probe/profile",
        "SHARED_HOME": "/probe/shared",
        "USER_HOME": "/probe/user",
        "HERMES_VENV": "/probe/venv",
        "HERMES_AGENT_INSTALL": "/probe/install",
        "HERMES_AGENT_REPO": "/probe/agent-repo",
        "HERMES_MT_REPO": "/probe/mt-repo",
    })
    # No unresolved ${...} placeholders.
    leftover = [t for t in tokens if "${" in t]
    assert not leftover, f"unresolved placeholders in bwrap-default.args: {leftover}"
    # Sanity: contains the expected core flags.
    assert "--die-with-parent" in tokens
    assert "--proc" in tokens
    assert "--chdir" in tokens
