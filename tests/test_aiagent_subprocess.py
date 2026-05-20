from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import time
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


def test_aiagent_subprocess_script_loader_adds_repo_root_for_relative_imports():
    """The child script is executed by file path, so sys.path starts at package dir."""
    script = Path(__file__).resolve().parents[1] / "hermes_multitenancy" / "aiagent_subprocess.py"
    repo_root = script.parents[1]
    code = f"""
import importlib.util
import sys
from pathlib import Path
script = Path({str(script)!r})
repo_root = Path({str(repo_root)!r})
sys.path = [str(script.parent)] + [p for p in sys.path if p and Path(p).resolve() != repo_root]
spec = importlib.util.spec_from_file_location("aiagent_subprocess", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
run = module._load_run_with_aiagent()
print(run.__module__)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "hermes_multitenancy.agent_real"


def test_skill_runtime_compat_substitutes_base_dir_without_agent_patch(monkeypatch, tmp_path: Path):
    """OpenClaw/ClawHub skills often use ``{baseDir}``.

    The compatibility belongs in multitenancy's routed runtime so upstream
    skills can remain unchanged and hermes-agent does not need a local fork
    patch for every profile-scoped skill convention.
    """
    from hermes_multitenancy import agent_real

    calls: list[tuple[str, str | None]] = []

    def original_substitute(content, skill_dir, session_id):
        calls.append((content, str(skill_dir) if skill_dir else None))
        return content.replace("${HERMES_SKILL_DIR}", str(skill_dir))

    fake_skill_preprocessing = SimpleNamespace(
        substitute_template_vars=original_substitute,
    )
    fake_skill_commands = SimpleNamespace(
        _substitute_template_vars=original_substitute,
    )
    agent_pkg = sys.modules.get("agent") or types.ModuleType("agent")
    agent_pkg.skill_preprocessing = fake_skill_preprocessing
    agent_pkg.skill_commands = fake_skill_commands
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.skill_preprocessing", fake_skill_preprocessing)
    monkeypatch.setitem(sys.modules, "agent.skill_commands", fake_skill_commands)

    profile_home = tmp_path / "profile"
    skill_dir = profile_home / "skills" / "Keep" / "keep-record"

    agent_real._install_skill_runtime_compat(profile_home)

    content = "node {baseDir}/scripts/mcp-call.js and ${HERMES_SKILL_DIR}/README.md"
    assert fake_skill_preprocessing.substitute_template_vars(content, skill_dir, "s1") == (
        f"node {skill_dir}/scripts/mcp-call.js and {skill_dir}/README.md"
    )
    assert fake_skill_commands._substitute_template_vars(content, skill_dir, "s1") == (
        f"node {skill_dir}/scripts/mcp-call.js and {skill_dir}/README.md"
    )
    assert calls == [
        (content, str(skill_dir)),
        (content, str(skill_dir)),
    ]


def test_load_feishu_oapi_runtime_patches_legacy_uat_refresh_to_multitenancy(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "owner"
    profile_home.mkdir(parents=True)

    refresh_calls: list[dict[str, object]] = []

    def fake_refresh_uat_for_user(**kwargs):
        refresh_calls.append(kwargs)
        return {"access_token": "new-access"}

    fake_feishu_uat_auth = types.ModuleType("hermes_multitenancy.feishu_uat_auth")
    fake_feishu_uat_auth.refresh_uat_for_user = fake_refresh_uat_for_user
    monkeypatch.setitem(sys.modules, "hermes_multitenancy.feishu_uat_auth", fake_feishu_uat_auth)

    fake_feishu_auth = types.ModuleType("hermes_cli.feishu_auth")

    def old_refresh_uat_for_user(*_args, **_kwargs):
        raise AssertionError("old refresh implementation must not be called")

    fake_feishu_auth.refresh_uat_for_user = old_refresh_uat_for_user
    monkeypatch.setitem(sys.modules, "hermes_cli.feishu_auth", fake_feishu_auth)

    fake_feishu_oapi = types.ModuleType("tools.feishu_oapi_client")
    fake_feishu_oapi.sender_open_id_scope = agent_real._missing_sender_open_id_scope
    fake_feishu_oapi.current_sender_open_id = agent_real._MissingCurrentSenderOpenId()
    fake_feishu_oapi._resolve_feishu_credentials = lambda: ("cli", "secret", "feishu")
    fake_feishu_oapi._load_uat = lambda _open_id=None: {}
    fake_tools = types.ModuleType("tools")
    fake_tools.feishu_oapi_client = fake_feishu_oapi
    monkeypatch.setitem(sys.modules, "tools", fake_tools)
    monkeypatch.setitem(sys.modules, "tools.feishu_oapi_client", fake_feishu_oapi)

    agent_real._load_feishu_oapi_runtime(profile_home)

    result = fake_feishu_auth.refresh_uat_for_user("ou_owner", "legacy-client", "legacy-secret")

    assert result == {"access_token": "new-access"}
    assert refresh_calls == [{
        "open_id": "ou_owner",
        "client_id": "legacy-client",
        "client_secret": "legacy-secret",
        "shared_home": shared_home,
        "force": True,
    }]


def test_credential_env_runtime_compat_loads_env_and_registers_passthrough(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.credentials import CredentialStore

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    profile_home = shared / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        """
credentials:
  - subject_id: kep-prd-skills
    provider: gitlab
    secret_kind: token
    target: workspace/credentials/gitlab.token
    env: GITLAB_TOKEN
    profiles: [alice]
""",
        encoding="utf-8",
    )
    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="__shared__",
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "glpat-test"},
        )
    finally:
        store.close()

    registered: list[list[str]] = []
    fake_env_passthrough = SimpleNamespace(
        register_env_passthrough=lambda names: registered.append(list(names)),
        _config_passthrough=frozenset({"EXISTING_TOKEN"}),
    )
    tools_mod = sys.modules.get("tools") or types.ModuleType("tools")
    tools_mod.env_passthrough = fake_env_passthrough
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.env_passthrough", fake_env_passthrough)

    assert agent_real._credential_env_for_aiagent(profile_home) == {
        "GITLAB_TOKEN": "glpat-test",
    }

    agent_real._install_credential_env_passthrough(profile_home)

    assert registered == [["GITLAB_TOKEN"]]
    assert fake_env_passthrough._config_passthrough == frozenset(
        {"EXISTING_TOKEN", "GITLAB_TOKEN"}
    )


def test_apply_runtime_env_for_aiagent_restores_credential_env(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.credentials import CredentialStore

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    profile_home = shared / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        """
credentials:
  - subject_id: kep-prd-skills
    provider: gitlab
    secret_kind: token
    target: workspace/credentials/gitlab.token
    env: GITLAB_TOKEN
    profiles: [alice]
""",
        encoding="utf-8",
    )
    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="__shared__",
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "glpat-test"},
        )
    finally:
        store.close()

    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("_HERMES_FORCE_GITLAB_TOKEN", raising=False)
    cleanup = agent_real._apply_runtime_env_for_aiagent(profile_home)
    assert os.environ["GITLAB_TOKEN"] == "glpat-test"
    assert os.environ["_HERMES_FORCE_GITLAB_TOKEN"] == "glpat-test"
    cleanup()
    assert "GITLAB_TOKEN" not in os.environ
    assert "_HERMES_FORCE_GITLAB_TOKEN" not in os.environ


def test_build_subprocess_env_forces_credential_env_through_terminal_scrub(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.credentials import CredentialStore

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    profile_home = shared / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        """
credentials:
  - subject_id: kep-prd-skills
    provider: gitlab
    secret_kind: token
    target: workspace/credentials/gitlab.token
    env: GITLAB_TOKEN
    profiles: [alice]
""",
        encoding="utf-8",
    )
    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="__shared__",
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "glpat-test"},
        )
    finally:
        store.close()
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()

    env = agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)

    assert env["GITLAB_TOKEN"] == "glpat-test"
    assert env["_HERMES_FORCE_GITLAB_TOKEN"] == "glpat-test"


def test_build_subprocess_env_adds_browser_runtime_only_when_enabled(tmp_path: Path):
    from hermes_multitenancy import agent_real

    shared = tmp_path / ".hermes"
    profile_home = shared / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()

    env_disabled = agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)
    assert "HERMES_MULTITENANCY_BROWSER_ENABLED" not in env_disabled

    (profile_home / "config.yaml").write_text(
        "multitenancy:\n  browser:\n    enabled: true\n",
        encoding="utf-8",
    )
    env_enabled = agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)

    assert env_enabled["HERMES_MULTITENANCY_BROWSER_ENABLED"] == "1"
    assert env_enabled["HERMES_BROWSER_SOCKET_BASE_DIR"] == str(profile_home / "browser" / "run")
    assert env_enabled["PLAYWRIGHT_BROWSERS_PATH"] == str(profile_home / "browser" / "ms-playwright")


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


@pytest.mark.asyncio
async def test_stream_aiagent_subprocess_wait_heartbeat_emits_pre_first_event_status(monkeypatch, tmp_path: Path):
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
            self.calls = 0

        async def readline(self):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(0.03)
                return b'{"event": "done", "result": "ok", "error": null}\n'
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

        async def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return FakeProc()

    monkeypatch.setenv("HERMES_AIAGENT_FIRST_EVENT_HEARTBEAT_SECONDS", "0.01")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    events = [
        item
        async for item in agent_real._stream_aiagent_subprocess(_event(), tmp_path)
    ]

    assert events[0][0] == "status"
    assert "Hermes 正在准备响应." in events[0][1]
    assert events[-1] == ("done", "ok")


@pytest.mark.asyncio
async def test_stream_aiagent_subprocess_emits_status_while_waiting_after_first_event(
    monkeypatch, tmp_path: Path
):
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
            self.calls = 0

        async def readline(self):
            self.calls += 1
            if self.calls == 1:
                return b'{"event": "tool_started", "name": "delegate_task"}\n'
            if self.calls == 2:
                await asyncio.sleep(0.03)
                return b'{"event": "done", "result": "ok", "error": null}\n'
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

        async def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return FakeProc()

    monkeypatch.setenv("HERMES_AIAGENT_WAIT_HEARTBEAT_SECONDS", "0.01")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    events = [
        item
        async for item in agent_real._stream_aiagent_subprocess(_event(), tmp_path)
    ]

    assert events[0] == ("tool_started", {"name": "delegate_task"})
    assert events[1][0] == "status"
    assert "正在等待当前工具或子任务输出." in events[1][1]
    assert events[-1] == ("done", "ok")


@pytest.mark.asyncio
async def test_stream_aiagent_subprocess_idle_timeout_is_runtime_error_not_cancelled(
    monkeypatch, tmp_path: Path
):
    from hermes_multitenancy import agent_real

    created = {}

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
            self.calls = 0

        async def readline(self):
            self.calls += 1
            if self.calls == 1:
                return b'{"event": "tool_started", "name": "delegate_task"}\n'
            await asyncio.sleep(60)
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
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        created["proc"] = FakeProc()
        return created["proc"]

    monkeypatch.setenv("HERMES_AIAGENT_SUBPROCESS_TIMEOUT", "0.02")
    monkeypatch.setenv("HERMES_AIAGENT_WAIT_HEARTBEAT_SECONDS", "0.01")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="produced no stream events"):
        async for _item in agent_real._stream_aiagent_subprocess(_event(), tmp_path):
            pass

    assert created["proc"].killed is True


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


def test_configure_cron_home_skips_binding_in_multitenancy_layout(monkeypatch, tmp_path: Path):
    """In multitenancy nested layout (<root>/profiles/<name>) the function is
    a no-op: cron writes stay at the profile-default path and the dedicated
    multi-profile worker (``cron_worker``) handles tick-time delivery."""
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / ".hermes" / "profiles" / "coder"
    shared_home = tmp_path / ".hermes"
    profile_home.mkdir(parents=True)
    shared_home.mkdir(exist_ok=True)

    cron_pkg = types.ModuleType("cron")
    cron_jobs = types.ModuleType("cron.jobs")
    sentinel_jobs = object()
    sentinel_output = object()
    cron_jobs.JOBS_FILE = sentinel_jobs
    cron_jobs.OUTPUT_DIR = sentinel_output
    cron_pkg.jobs = cron_jobs

    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    agent_real._configure_cron_home(shared_home)

    # Untouched: multitenancy layout triggers early return before any rebinding.
    assert cron_jobs.JOBS_FILE is sentinel_jobs
    assert cron_jobs.OUTPUT_DIR is sentinel_output


def test_configure_cron_home_binds_shared_in_legacy_layout(monkeypatch, tmp_path: Path):
    """Outside the multitenancy nested layout (e.g. single-profile or legacy
    deployments) the function still rebinds cron paths to the shared store."""
    from hermes_multitenancy import agent_real

    legacy_home = tmp_path / ".hermes-legacy"
    shared_home = tmp_path / ".hermes"
    legacy_home.mkdir(parents=True)
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
    monkeypatch.setenv("HERMES_HOME", str(legacy_home))

    agent_real._configure_cron_home(shared_home)

    assert cron_jobs.JOBS_FILE == shared_home.resolve() / "cron" / "jobs.json"
    assert cron_jobs.OUTPUT_DIR == shared_home.resolve() / "cron" / "output"
    assert os.environ["HERMES_HOME"] == str(legacy_home)
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


def test_run_with_aiagent_tolerates_missing_legacy_feishu_oapi(monkeypatch, tmp_path: Path):
    """Official-clean Hermes no longer ships tools.feishu_oapi_client."""
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\nplatform_toolsets:\n  feishu:\n  - lark-cli\n",
        encoding="utf-8",
    )
    (profile_home / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")

    tools_mod = types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.delitem(sys.modules, "tools.feishu_oapi_client", raising=False)

    seen: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            seen["toolsets"] = kwargs.get("toolsets")

        def run_conversation(self, user_message, task_id, conversation_history=None):
            seen["user_message"] = user_message
            return {"final_response": "ok"}

        def cleanup(self):
            seen["cleanup"] = True

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))

    event = _event()
    event.source.user_id = "ou_clean_upstream_sender"

    assert agent_real._run_with_aiagent(event, profile_home) == "ok"
    assert seen["user_message"] == "hello"
    assert seen["cleanup"] is True


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

    assert agent_real._resolve_aiagent_session_id(event, profile_home, "ou_owner") != (
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


def test_resolve_enabled_toolsets_merges_webui_lark_cli_with_api_server_defaults():
    from hermes_multitenancy import agent_real

    seen: dict[str, object] = {}

    def fake_get_platform_tools(config, platform, *, include_default_mcp_servers=True):
        seen["platform_toolsets"] = config.get("platform_toolsets")
        seen["platform"] = platform
        seen["include_default_mcp_servers"] = include_default_mcp_servers
        if platform == "webui":
            raise KeyError("webui")
        return {"terminal", "file", "web"}

    config = {
        "platform_toolsets": {
            "feishu": ["lark-cli"],
            "api_server": ["lark-cli"],
            "webui": ["lark-cli"],
        },
        "multitenancy": {
            "toolsets_mode": "explicit",
            "platform_toolsets_mode": {
                "feishu": "explicit",
                "api_server": "merge_default",
                "webui": "merge_default",
            },
        },
    }

    assert agent_real._resolve_enabled_toolsets(
        config,
        "webui",
        platform_tools_resolver=fake_get_platform_tools,
    ) == ["file", "lark-cli", "terminal", "web"]
    assert seen == {
        "platform_toolsets": {
            "feishu": ["lark-cli"],
        },
        "platform": "api_server",
        "include_default_mcp_servers": True,
    }


def test_resolve_enabled_toolsets_preserves_webui_core_tools_without_resolver():
    from hermes_multitenancy import agent_real

    assert agent_real._resolve_enabled_toolsets(
        {"platform_toolsets": {"webui": ["lark-cli"]}},
        "webui",
        platform_tools_resolver=None,
    ) == ["file", "lark-cli", "terminal", "web"]


def test_resolve_enabled_toolsets_removes_browser_unless_profile_enabled(tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "alice"
    profile_home.mkdir(parents=True)

    def fake_get_platform_tools(config, platform, *, include_default_mcp_servers=True):
        return {"browser", "web", "file"}

    assert agent_real._resolve_enabled_toolsets(
        {},
        "webui",
        platform_tools_resolver=fake_get_platform_tools,
        profile_home=profile_home,
    ) == ["file", "web"]

    assert agent_real._resolve_enabled_toolsets(
        {"multitenancy": {"browser": {"enabled": True}}},
        "webui",
        platform_tools_resolver=fake_get_platform_tools,
        profile_home=profile_home,
    ) == ["browser", "file", "web"]


def test_resolve_enabled_toolsets_denies_browser_for_router_profile(tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "multitenancy_router"
    profile_home.mkdir(parents=True)

    assert agent_real._resolve_enabled_toolsets(
        {
            "platform_toolsets": {"webui": ["browser", "web"]},
            "multitenancy": {"browser": {"enabled": True}},
        },
        "webui",
        platform_tools_resolver=None,
        profile_home=profile_home,
    ) == ["file", "terminal", "web"]


def test_run_with_aiagent_resolves_toolsets_from_event_platform(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\nplatform_toolsets:\n  webui:\n  - lark-cli\n",
        encoding="utf-8",
    )
    (profile_home / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")
    event = _event()
    event.source.platform = SimpleNamespace(value="webui")
    captured: dict[str, object] = {}

    def fake_resolve(config, platform_key, *, platform_tools_resolver):
        captured["platform_key"] = platform_key
        return ["file", "lark-cli", "terminal", "web"]

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["enabled_toolsets"] = kwargs.get("enabled_toolsets")

        def run_conversation(self, user_message, task_id):
            return {"final_response": "ok"}

        def cleanup(self):
            pass

    monkeypatch.setattr(agent_real, "_resolve_enabled_toolsets", fake_resolve)
    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    _install_fake_feishu_oapi(monkeypatch)

    assert agent_real._run_with_aiagent(event, profile_home) == "ok"
    assert captured == {
        "platform_key": "webui",
        "enabled_toolsets": ["file", "lark-cli", "terminal", "web"],
    }


def test_run_with_aiagent_tolerates_missing_legacy_feishu_oapi(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\nplatform_toolsets:\n  webui:\n  - lark-cli\n",
        encoding="utf-8",
    )
    (profile_home / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")
    monkeypatch.delitem(sys.modules, "tools.feishu_oapi_client", raising=False)
    tools_mod = types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", tools_mod)

    seen: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            seen["enabled_toolsets"] = kwargs.get("enabled_toolsets")

        def run_conversation(self, user_message, task_id):
            seen["user_message"] = user_message
            return {"final_response": "ok"}

        def cleanup(self):
            pass

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))

    event = _event()
    event.source.platform = SimpleNamespace(value="webui")

    assert agent_real._run_with_aiagent(event, profile_home) == "ok"
    assert "lark-cli" in (seen["enabled_toolsets"] or [])
    assert "terminal" in (seen["enabled_toolsets"] or [])
    assert "file" in (seen["enabled_toolsets"] or [])
    assert seen["user_message"] == "hello"


def test_run_with_aiagent_inherits_shared_model_config(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (shared_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\n"
        "toolsets:\n  - hermes-cli\n",
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        "terminal:\n  cwd: /workspace\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["model"] = kwargs.get("model")
            captured["enabled_toolsets"] = kwargs.get("enabled_toolsets")

        def run_conversation(self, user_message, task_id):
            return {"final_response": "ok"}

        def cleanup(self):
            pass

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    _install_fake_feishu_oapi(monkeypatch)

    event = _event()
    event.source.platform = SimpleNamespace(value="webui")

    assert agent_real._run_with_aiagent(event, profile_home) == "ok"
    assert captured["model"] == "test-model"


def test_run_with_aiagent_uses_webui_session_model_metadata(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (shared_home / "config.yaml").write_text(
        "model:\n  default: openai/profile-default\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "profile-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "session-key")
    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_conversation(self, user_message, task_id):
            return {"final_response": "ok"}

        def cleanup(self):
            pass

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    _install_fake_feishu_oapi(monkeypatch)

    event = _event()
    event.source.platform = SimpleNamespace(value="webui")
    event.raw_event = {
        "metadata": {
            "model": "claude-sonnet-4-5",
            "provider": "anthropic",
        }
    }

    assert agent_real._run_with_aiagent(event, profile_home) == "ok"
    assert captured["model"] == "claude-sonnet-4-5"
    assert captured["provider"] == "anthropic"
    assert captured["api_key"] == "session-key"


@pytest.mark.asyncio
async def test_stream_run_agent_inherits_shared_model_config(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (shared_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\n",
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        "terminal:\n  cwd: /workspace\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: dict[str, object] = {}

    class FakeDelta:
        content = "ok"

    class FakeChunk:
        choices = [SimpleNamespace(delta=FakeDelta())]

    class FakeCompletions:
        async def create(self, **kwargs):
            captured["model"] = kwargs["model"]

            async def stream():
                yield FakeChunk()

            return stream()

    class FakeClient:
        def __init__(self, **kwargs):
            captured["api_key"] = kwargs.get("api_key")

        chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeClient))

    chunks = [
        item async for item in agent_real.stream_run_agent(_event(), profile_home)
    ]

    assert chunks == [("content", "ok")]
    assert captured["model"] == "test-model"
    assert captured["api_key"] == "test-key"


@pytest.mark.asyncio
async def test_legacy_real_run_agent_inherits_shared_model_config(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (shared_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\n",
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        "terminal:\n  cwd: /workspace\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: dict[str, object] = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured["model"] = kwargs["model"]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content="legacy ok"))
                ],
                usage=None,
            )

    class FakeClient:
        def __init__(self, **kwargs):
            captured["api_key"] = kwargs.get("api_key")

        chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeClient))

    assert await agent_real._legacy_real_run_agent(_event(), profile_home) == "legacy ok"
    assert captured["model"] == "test-model"
    assert captured["api_key"] == "test-key"


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
            "args": {"action": "patch"},
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


def test_run_with_aiagent_ignores_reasoning_available_preview(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\n",
        encoding="utf-8",
    )
    (profile_home / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")

    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_conversation(self, user_message, task_id):
            self.kwargs["tool_progress_callback"](
                "reasoning.available",
                "",
                "visible answer preview",
                None,
            )
            self.kwargs["stream_delta_callback"]("final answer")
            return {"final_response": "final answer"}

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

    assert result == "final answer"
    assert events == [{"event": "content", "text": "final answer"}]


def test_reasoning_storage_drops_duplicate_visible_content():
    from hermes_multitenancy import agent_real

    assert agent_real._reasoning_for_state_db(
        "\n\n✅ 测试日历已创建成功！",
        "✅ 测试日历已创建成功！",
        preserve_reasoning=True,
    ) is None


def test_reasoning_storage_suppresses_webui_reasoning():
    from hermes_multitenancy import agent_real

    assert agent_real._reasoning_for_state_db(
        "日历已创建成功。",
        "The user wants me to call lark_cli first.",
        preserve_reasoning=False,
    ) is None


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

    # Allowlisted keys carry over, with shared Hermes-managed tool bin prepended.
    assert env["PATH"].endswith("/usr/bin:/bin")
    assert env["PYTHONUNBUFFERED"] == "1"

    # Secret-ish env keys must be absent.
    for leaked in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITLAB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
    ):
        assert leaked not in env, f"{leaked} leaked into subprocess env"


def test_build_subprocess_env_loads_profile_env_for_agent_only(monkeypatch, tmp_path: Path):
    """Profile .env secrets are passed to the AIAgent process, not inherited from parent."""
    from hermes_multitenancy import agent_real

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".env").write_text(
        "\n".join(
            [
                "ANTHROPIC_API_KEY=profile-key",
                "ANTHROPIC_BASE_URL=https://tokenhub.example/v1",
                "FEISHU_APP_SECRET=feishu-secret",
                "PUBLIC_RUNTIME_FLAG=enabled",
            ]
        ),
        encoding="utf-8",
    )
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "parent-key")

    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)

    assert env["ANTHROPIC_API_KEY"] == "profile-key"
    assert env["ANTHROPIC_BASE_URL"] == "https://tokenhub.example/v1"
    assert "FEISHU_APP_SECRET" not in env
    assert env["PUBLIC_RUNTIME_FLAG"] == "enabled"


def test_build_subprocess_env_loads_shared_model_env_when_profile_env_is_empty(tmp_path: Path):
    """Profiles may inherit shared model provider keys without exposing Feishu app secrets."""
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    profile = shared_home / "profiles" / "owner"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("", encoding="utf-8")
    (shared_home / ".env").write_text(
        "\n".join(
            [
                "ANTHROPIC_API_KEY=shared-model-key",
                "ANTHROPIC_BASE_URL=https://tokenhub.example/v1",
                "FEISHU_APP_ID=cli_test",
                "FEISHU_APP_SECRET=feishu-secret",
                "GITLAB_TOKEN=glpat-shared",
            ]
        ),
        encoding="utf-8",
    )
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()

    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)

    assert env["ANTHROPIC_API_KEY"] == "shared-model-key"
    assert env["ANTHROPIC_BASE_URL"] == "https://tokenhub.example/v1"
    assert "FEISHU_APP_ID" not in env
    assert "FEISHU_APP_SECRET" not in env
    assert "GITLAB_TOKEN" not in env


def test_build_subprocess_env_filters_profile_env_symlinked_to_shared_home(tmp_path: Path):
    """A profile .env symlink to shared .env is treated as shared config, not tenant env."""
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    profile = shared_home / "profiles" / "alice"
    profile.mkdir(parents=True)
    (shared_home / ".env").write_text(
        "\n".join(
            [
                "ANTHROPIC_API_KEY=shared-model-key",
                "GITLAB_TOKEN=glpat-shared",
                "PUBLIC_RUNTIME_FLAG=shared-flag",
            ]
        ),
        encoding="utf-8",
    )
    (profile / ".env").symlink_to(shared_home / ".env")
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()

    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)

    assert env["ANTHROPIC_API_KEY"] == "shared-model-key"
    assert "GITLAB_TOKEN" not in env
    assert "PUBLIC_RUNTIME_FLAG" not in env


def test_resolve_base_url_prefers_profile_env_for_primary_model(monkeypatch):
    """Sandboxed AIAgent must honor provider base URLs loaded from profile .env."""
    from hermes_multitenancy import agent_real

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://tokenhub.example")

    base_url = agent_real._resolve_base_url("anthropic", True, {}, {})

    assert base_url == "https://tokenhub.example"


def test_build_subprocess_env_forwards_credential_vault_key_only(monkeypatch, tmp_path: Path):
    """Credential vault key is router plumbing; unrelated parent secrets still stay out."""
    from hermes_multitenancy import agent_real

    profile = tmp_path / "profile"
    profile.mkdir()
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "vault-key")
    monkeypatch.setenv("HERMES_CREDENTIAL_KEY", "legacy-vault-key")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-parent")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parent")

    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)

    assert env["HERMES_MULTITENANCY_CREDENTIAL_KEY"] == "vault-key"
    assert env["HERMES_CREDENTIAL_KEY"] == "legacy-vault-key"
    assert "GITLAB_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env


def test_build_subprocess_env_does_not_forward_feishu_app_or_user_tokens(monkeypatch, tmp_path: Path):
    """Feishu app and user tokens come from credential vault, not subprocess env."""
    from hermes_multitenancy import agent_real

    profile = tmp_path / "profile"
    profile.mkdir()
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("FEISHU_DOMAIN", "feishu")
    monkeypatch.setenv("FEISHU_UAT_ACCESS_TOKEN", "user-token")
    monkeypatch.setenv("FEISHU_UAT_REFRESH_TOKEN", "refresh-token")

    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)

    assert "FEISHU_APP_ID" not in env
    assert "FEISHU_APP_SECRET" not in env
    assert "FEISHU_DOMAIN" not in env
    assert "FEISHU_UAT_ACCESS_TOKEN" not in env
    assert "FEISHU_UAT_REFRESH_TOKEN" not in env


def test_build_subprocess_env_does_not_default_feishu_domain_without_app_env(monkeypatch, tmp_path: Path):
    """Default domain is resolved by the credential broker, not ambient env."""
    from hermes_multitenancy import agent_real

    profile = tmp_path / "profile"
    profile.mkdir()
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    monkeypatch.delenv("FEISHU_DOMAIN", raising=False)
    monkeypatch.setenv("FEISHU_UAT_ACCESS_TOKEN", "user-token")
    monkeypatch.setenv("FEISHU_UAT_REFRESH_TOKEN", "refresh-token")

    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)

    assert "FEISHU_APP_ID" not in env
    assert "FEISHU_APP_SECRET" not in env
    assert "FEISHU_DOMAIN" not in env
    assert "FEISHU_UAT_ACCESS_TOKEN" not in env
    assert "FEISHU_UAT_REFRESH_TOKEN" not in env


def test_mark_session_source_feishu_ignores_state_db_without_sessions_table(tmp_path: Path):
    """Some Run Broker profiles have a state.db before Hermes core creates sessions."""
    import sqlite3
    from hermes_multitenancy import agent_real

    profile = tmp_path / "profile"
    profile.mkdir()
    with sqlite3.connect(profile / "state.db") as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")

    agent_real._mark_session_source_feishu(profile, "session-id")


def test_build_subprocess_env_converts_auth_pool_token_to_provider_env(tmp_path: Path):
    """Auth-only profiles still work when auth.json is masked inside bwrap."""
    from hermes_multitenancy import agent_real

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        "model:\n  default: anthropic/custom-model-a3\n",
        encoding="utf-8",
    )
    (profile / "auth.json").write_text(
        '{"credential_pool":{"anthropic":[{"access_token":"auth-token"}]}}',
        encoding="utf-8",
    )
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()

    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)

    assert env["ANTHROPIC_API_KEY"] == "auth-token"


def test_provider_adapter_profile_local_overrides_org_default(monkeypatch, tmp_path: Path):
    """Profile-local model provider credentials win over org/global fallback rows."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.provider_adapter import plan_provider_credentials

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "alice"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model:\n  default: anthropic/claude-test\n", encoding="utf-8")
    (shared / "provider-adapter.yaml").write_text("enabled: true\n", encoding="utf-8")
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    store = CredentialStore(shared / "multitenancy.db")
    expires_at = int(time.time() * 1000) + 3600_000
    for profile_name, token in (
        ("__global__", "fallback-token"),
        ("__org__", "org-token"),
        ("alice", "profile-token"),
    ):
        store.put_credential(
            profile_name=profile_name,
            subject_id="anthropic",
            provider="anthropic",
            secret_kind="api_key",
            payload={"api_key": token},
            expires_at=expires_at,
        )
    store.close()

    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)
    plan = plan_provider_credentials(profile)
    raw_plan = json.dumps(plan, ensure_ascii=False)

    assert env["ANTHROPIC_API_KEY"] == "profile-token"
    assert "_HERMES_FORCE_ANTHROPIC_API_KEY" not in env
    assert "_HERMES_FORCE_HERMES_MULTITENANCY_CREDENTIAL_KEY" not in env
    anthropic = next(item for item in plan["providers"] if item["provider"] == "anthropic")
    assert anthropic["selected_source"] == "profile"
    assert anthropic["source_profile"] == "alice"
    assert anthropic["status"] == "valid"
    assert "profile-token" not in raw_plan
    assert "org-token" not in raw_plan
    assert "fallback-token" not in raw_plan


def test_provider_adapter_org_default_fills_thin_profile(monkeypatch, tmp_path: Path):
    """A thin profile with no local key can inherit the organization provider."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.provider_adapter import plan_provider_credentials

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "thin"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "model:\n  default: anthropic/claude-test\nfallback:\n  - openrouter/test\n",
        encoding="utf-8",
    )
    (shared / "provider-adapter.yaml").write_text("enabled: true\n", encoding="utf-8")
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    store = CredentialStore(shared / "multitenancy.db")
    expires_at = int(time.time() * 1000) + 3600_000
    store.put_credential(
        profile_name="__org__",
        subject_id="anthropic",
        provider="anthropic",
        secret_kind="api_key",
        payload={"api_key": "org-token"},
        expires_at=expires_at,
    )
    store.put_credential(
        profile_name="__global__",
        subject_id="openrouter",
        provider="openrouter",
        secret_kind="api_key",
        payload={"api_key": "fallback-openrouter-token"},
        expires_at=expires_at,
    )
    store.close()

    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)
    plan = plan_provider_credentials(profile)

    assert env["ANTHROPIC_API_KEY"] == "org-token"
    assert env["OPENROUTER_API_KEY"] == "fallback-openrouter-token"
    sources = {item["provider"]: item["selected_source"] for item in plan["providers"]}
    assert sources == {"anthropic": "org", "openrouter": "fallback"}


def test_provider_adapter_ignores_feishu_credentials(monkeypatch, tmp_path: Path):
    """Provider fallback must not turn Feishu app/UAT rows into model env secrets."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.provider_adapter import plan_provider_credentials

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "alice"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model:\n  default: feishu/not-a-model-provider\n", encoding="utf-8")
    (shared / "provider-adapter.yaml").write_text("enabled: true\n", encoding="utf-8")
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")

    store = CredentialStore(shared / "multitenancy.db")
    store.put_credential(
        profile_name="__global__",
        subject_id="feishu_app",
        provider="feishu",
        secret_kind="app",
        payload={"app_id": "cli_test", "app_secret": "app-secret"},
    )
    store.put_credential(
        profile_name="__org__",
        subject_id="feishu",
        provider="feishu",
        secret_kind="uat",
        payload={"access_token": "uat-token", "refresh_token": "refresh-token"},
    )
    store.close()

    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)
    plan = plan_provider_credentials(profile)
    raw_plan = json.dumps(plan, ensure_ascii=False)

    assert "FEISHU_APP_ID" not in env
    assert "FEISHU_APP_SECRET" not in env
    assert "FEISHU_UAT_ACCESS_TOKEN" not in env
    assert "FEISHU_UAT_REFRESH_TOKEN" not in env
    assert "feishu" not in {item["provider"] for item in plan["providers"]}
    assert "app-secret" not in raw_plan
    assert "uat-token" not in raw_plan


def test_credential_status_uses_provider_adapter_without_secrets(monkeypatch, tmp_path: Path):
    """Model-visible provider status reports inherited state, not raw keys."""
    from hermes_multitenancy.credential_tool import credential_status
    from hermes_multitenancy.credentials import CredentialStore

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "thin"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model:\n  default: anthropic/claude-test\n", encoding="utf-8")
    (shared / "provider-adapter.yaml").write_text("enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    CredentialStore(shared / "multitenancy.db").put_credential(
        profile_name="__org__",
        subject_id="anthropic",
        provider="anthropic",
        secret_kind="api_key",
        payload={"api_key": "org-token"},
        scopes=["llm:chat"],
        expires_at=int(time.time() * 1000) + 3600_000,
    )

    raw = credential_status({"provider": "anthropic", "credential_kind": "api_key"})
    status = json.loads(raw)

    assert status["status"] == "valid"
    assert status["provider"] == "anthropic"
    assert status["credential_kind"] == "api_key"
    assert status["selected_source"] == "org"
    assert status["source_profile"] == "__org__"
    assert status["has_credential"] is True
    assert "org-token" not in raw


def test_build_subprocess_env_pivots_home_workspace_and_token_compat_env(tmp_path: Path):
    """Token-oriented skills should work unchanged inside a profile.

    Existing OpenClaw-style skills and internal CLIs typically discover token
    state through HOME, /workspace, or KEP_PROFILE.  Multitenancy owns the
    routing, so the subprocess env must transparently redirect those anchors
    to the routed profile instead of requiring each skill to call
    hermes_multitenancy.skill_storage.
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
    assert env["HOME"] == str(profile / "home")
    assert env["WORKSPACE"] == str(profile / "workspace")
    assert env["KEP_PROFILE"] == "alice"
    assert env["HERMES_PROFILE"] == "alice"

    # XDG/TMPDIR pivot dirs must exist and be private (mode 0700).
    for sub in ("home", "workspace", "cache", "config", "state", "data", "tmp"):
        d = profile / sub
        assert d.is_dir(), f"{sub} not created"
        mode = d.stat().st_mode & 0o777
        assert mode == 0o700, f"{sub} mode is {oct(mode)}, expected 0o700"


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


def test_build_subprocess_env_auto_approves_inside_sandbox_host(monkeypatch, tmp_path: Path):
    """Sandboxed routed profiles should not pause on duplicate dangerous-command prompts."""
    from hermes_multitenancy import agent_real

    profile = tmp_path / "profiles" / "owner"
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")

    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)

    assert env["HERMES_SANDBOX_HOST"] == "1"
    assert env["HERMES_YOLO_MODE"] == "1"


def test_build_subprocess_env_prepends_shared_bin_to_path(monkeypatch, tmp_path: Path):
    """Shared Hermes-managed CLI installs must be callable while token state stays profile-local."""
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    profile = shared_home / "profiles" / "alice"
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)

    assert env["PATH"].split(":")[0] == str(shared_home / "bin")
    assert env["PATH"].endswith("/usr/bin:/bin")


def test_build_subprocess_env_wires_lark_cli_sidecar_without_tokens(monkeypatch, tmp_path: Path):
    """lark-cli gets only the sidecar key/proxy, never raw Feishu credentials."""
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    shared_bin = shared_home / "bin"
    shared_bin.mkdir(parents=True)
    lark_cli = shared_bin / "lark-cli-authsidecar"
    lark_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    lark_cli.chmod(0o755)
    profile = shared_home / "profiles" / "alice"
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    monkeypatch.setenv("HERMES_LARK_CLI_AUTH_PROXY", "http://127.0.0.1:16384")
    monkeypatch.setenv("HERMES_LARK_CLI_PROXY_KEY", "short-lived-key")
    monkeypatch.setenv("HERMES_LARK_CLI_APP_ID", "cli_public")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("FEISHU_UAT_ACCESS_TOKEN", "uat-secret")

    env = agent_real._build_subprocess_env(profile, approval_dir=approval_dir)

    assert env["HERMES_LARK_CLI_BIN"] == str(lark_cli)
    assert env["LARKSUITE_CLI_AUTH_PROXY"] == "http://127.0.0.1:16384"
    assert env["LARKSUITE_CLI_PROXY_KEY"] == "short-lived-key"
    assert env["LARKSUITE_CLI_APP_ID"] == "cli_public"
    assert env["LARKSUITE_CLI_BRAND"] == "feishu"
    assert env["LARKSUITE_CLI_DEFAULT_AS"] == "bot"
    assert env["LARKSUITE_CLI_STRICT_MODE"] == "off"
    assert "FEISHU_APP_SECRET" not in env
    assert "FEISHU_UAT_ACCESS_TOKEN" not in env


def test_lark_cli_auth_broker_scope_starts_per_run_broker_and_closes(monkeypatch, tmp_path: Path):
    """Each AIAgent run gets a short-lived sidecar key and localhost broker."""
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    shared_bin = shared_home / "bin"
    shared_bin.mkdir(parents=True)
    lark_cli = shared_bin / "lark-cli-authsidecar"
    lark_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    lark_cli.chmod(0o755)
    profile = shared_home / "profiles" / "alice"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_LARK_CLI_APP_ID", "cli_public")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("FEISHU_UAT_ACCESS_TOKEN", "uat-secret")
    seen: dict[str, object] = {}

    class FakeServer:
        url = "http://127.0.0.1:19090"

        def close(self):
            seen["closed"] = True

    def fake_start(context):
        seen["context"] = context
        return FakeServer()

    monkeypatch.setattr(agent_real, "start_lark_cli_auth_broker_server", fake_start)

    with agent_real._lark_cli_auth_broker_scope(profile, "ou_alice") as extra:
        context = seen["context"]
        assert context.profile_name == "alice"
        assert context.user_open_id == "ou_alice"
        assert context.hmac_key
        assert extra["HERMES_LARK_CLI_BIN"] == str(lark_cli)
        assert extra["LARKSUITE_CLI_AUTH_PROXY"] == "http://127.0.0.1:19090"
        assert extra["LARKSUITE_CLI_PROXY_KEY"] == context.hmac_key
        assert extra["LARKSUITE_CLI_APP_ID"] == "cli_public"
        assert extra["LARKSUITE_CLI_DEFAULT_AS"] == "bot"
        assert extra["LARKSUITE_CLI_STRICT_MODE"] == "off"
        assert context.allowed_identities == frozenset({"user", "bot"})
        assert "FEISHU_APP_SECRET" not in extra
        assert "FEISHU_UAT_ACCESS_TOKEN" not in extra

    assert seen["closed"] is True


def test_lark_cli_auth_broker_scope_defaults_to_user_when_profile_has_uat(monkeypatch, tmp_path: Path):
    """Auto identity uses user only when the routed profile has a live sender UAT."""
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    shared_bin = shared_home / "bin"
    shared_bin.mkdir(parents=True)
    lark_cli = shared_bin / "lark-cli-authsidecar"
    lark_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    lark_cli.chmod(0o755)
    profile = shared_home / "profiles" / "alice"
    (profile / "feishu_uat").mkdir(parents=True)
    (profile / "feishu_uat" / "ou_alice.json").write_text(
        json.dumps({
            "access_token": "user-token",
            "expires_at": int(time.time() * 1000) + 3_600_000,
            "app_id": "cli_public",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_LARK_CLI_APP_ID", "cli_public")

    class FakeServer:
        url = "http://127.0.0.1:19090"

        def close(self):
            pass

    monkeypatch.setattr(agent_real, "start_lark_cli_auth_broker_server", lambda _context: FakeServer())

    with agent_real._lark_cli_auth_broker_scope(profile, "ou_alice") as extra:
        assert extra["LARKSUITE_CLI_DEFAULT_AS"] == "user"


def test_lark_cli_auth_broker_scope_forces_bot_for_group_profile(monkeypatch, tmp_path: Path):
    """Group profiles must not default to a member's user identity."""
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    shared_bin = shared_home / "bin"
    shared_bin.mkdir(parents=True)
    lark_cli = shared_bin / "lark-cli-authsidecar"
    lark_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    lark_cli.chmod(0o755)
    profile = shared_home / "profiles" / "feishu_group_abc"
    (profile / "feishu_uat").mkdir(parents=True)
    (profile / "group_profile.json").write_text('{"kind":"group"}', encoding="utf-8")
    (profile / "feishu_uat" / "ou_alice.json").write_text(
        json.dumps({
            "access_token": "user-token",
            "expires_at": int(time.time() * 1000) + 3_600_000,
            "app_id": "cli_public",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_LARK_CLI_APP_ID", "cli_public")

    class FakeServer:
        url = "http://127.0.0.1:19090"

        def close(self):
            pass

    monkeypatch.setattr(agent_real, "start_lark_cli_auth_broker_server", lambda _context: FakeServer())

    with agent_real._lark_cli_auth_broker_scope(profile, "ou_alice") as extra:
        assert extra["LARKSUITE_CLI_DEFAULT_AS"] == "bot"


def test_lark_cli_auth_broker_scope_starts_for_group_without_sender(monkeypatch, tmp_path: Path):
    """Bot-only group profiles must not require a member UAT/open_id to expose lark_cli."""
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    shared_bin = shared_home / "bin"
    shared_bin.mkdir(parents=True)
    lark_cli = shared_bin / "lark-cli-authsidecar"
    lark_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    lark_cli.chmod(0o755)
    profile = shared_home / "profiles" / "feishu_group_abc"
    profile.mkdir(parents=True)
    (profile / "group_profile.json").write_text('{"kind":"group"}', encoding="utf-8")
    monkeypatch.setenv("HERMES_LARK_CLI_APP_ID", "cli_public")
    seen: dict[str, object] = {}

    class FakeServer:
        url = "http://127.0.0.1:19090"

        def close(self):
            seen["closed"] = True

    def fake_start(context):
        seen["context"] = context
        return FakeServer()

    monkeypatch.setattr(agent_real, "start_lark_cli_auth_broker_server", fake_start)

    with agent_real._lark_cli_auth_broker_scope(profile, "") as extra:
        context = seen["context"]
        assert context.profile_name == "feishu_group_abc"
        assert context.user_open_id == ""
        assert extra["HERMES_LARK_CLI_BIN"] == str(lark_cli)
        assert extra["LARKSUITE_CLI_DEFAULT_AS"] == "bot"

    assert seen["closed"] is True


def test_lark_cli_shared_env_loads_only_broker_control_plane(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    shared_home.mkdir()
    (shared_home / ".env").write_text(
        "\n".join(
            [
                "HERMES_MULTITENANCY_CREDENTIAL_KEY=vault-key",
                "HERMES_LARK_CLI_APP_ID=cli_public",
                "FEISHU_APP_SECRET=must-not-load",
                "ANTHROPIC_API_KEY=model-key",
            ]
        ),
        encoding="utf-8",
    )
    profile = shared_home / "profiles" / "feishu_group_abc"
    profile.mkdir(parents=True)
    for key in (
        "HERMES_MULTITENANCY_CREDENTIAL_KEY",
        "HERMES_LARK_CLI_APP_ID",
        "FEISHU_APP_SECRET",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    loaded = agent_real._load_lark_cli_shared_env(profile)

    assert loaded == {
        "HERMES_MULTITENANCY_CREDENTIAL_KEY": "vault-key",
        "HERMES_LARK_CLI_APP_ID": "cli_public",
    }
    assert os.environ["HERMES_MULTITENANCY_CREDENTIAL_KEY"] == "vault-key"
    assert os.environ["HERMES_LARK_CLI_APP_ID"] == "cli_public"
    assert "FEISHU_APP_SECRET" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_lark_cli_app_id_can_come_from_shared_env(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    shared_home.mkdir()
    (shared_home / ".env").write_text("HERMES_LARK_CLI_APP_ID=cli_from_env\n", encoding="utf-8")
    profile = shared_home / "profiles" / "feishu_group_abc"
    profile.mkdir(parents=True)
    monkeypatch.delenv("HERMES_LARK_CLI_APP_ID", raising=False)

    assert agent_real._resolve_lark_cli_app_id(profile) == "cli_from_env"


def test_lark_cli_auth_broker_scope_can_read_public_app_id_from_vault(monkeypatch, tmp_path: Path):
    """Production should not need to duplicate app_id in process env."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.credentials import CredentialStore

    shared_home = tmp_path / ".hermes"
    shared_bin = shared_home / "bin"
    shared_bin.mkdir(parents=True)
    lark_cli = shared_bin / "lark-cli-authsidecar"
    lark_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    lark_cli.chmod(0o755)
    profile = shared_home / "profiles" / "alice"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.delenv("HERMES_LARK_CLI_APP_ID", raising=False)
    store = CredentialStore(shared_home / "multitenancy.db", encryption_key="test-key")
    try:
        store.put_credential(
            profile_name="__global__",
            subject_id="feishu_app",
            provider="feishu",
            secret_kind="app",
            payload={"app_id": "cli_from_vault", "app_secret": "secret"},
        )
    finally:
        store.close()

    class FakeServer:
        url = "http://127.0.0.1:19090"

        def close(self):
            pass

    monkeypatch.setattr(agent_real, "start_lark_cli_auth_broker_server", lambda _context: FakeServer())

    with agent_real._lark_cli_auth_broker_scope(profile, "ou_alice") as extra:
        assert extra["LARKSUITE_CLI_APP_ID"] == "cli_from_vault"


def test_lark_cli_auth_broker_scope_can_read_public_app_id_from_profile_uat(monkeypatch, tmp_path: Path):
    """A local UAT file is enough to start sidecar plumbing when vault key is absent."""
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    shared_bin = shared_home / "bin"
    shared_bin.mkdir(parents=True)
    lark_cli = shared_bin / "lark-cli-authsidecar"
    lark_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    lark_cli.chmod(0o755)
    profile = shared_home / "profiles" / "alice"
    uat_dir = profile / "feishu_uat"
    uat_dir.mkdir(parents=True)
    (uat_dir / "ou_alice.json").write_text(
        json.dumps({"app_id": "cli_from_json", "access_token": "secret"}),
        encoding="utf-8",
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_LARK_CLI_APP_ID", raising=False)

    class FakeServer:
        url = "http://127.0.0.1:19090"

        def close(self):
            pass

    monkeypatch.setattr(agent_real, "start_lark_cli_auth_broker_server", lambda _context: FakeServer())

    with agent_real._lark_cli_auth_broker_scope(profile, "ou_alice") as extra:
        assert extra["HERMES_LARK_CLI_BIN"] == str(lark_cli)
        assert extra["LARKSUITE_CLI_APP_ID"] == "cli_from_json"


def test_feishu_identity_context_log_distinguishes_legacy_uat_and_lark_cli_identity(
    tmp_path: Path,
    caplog,
):
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    profile = shared_home / "profiles" / "feishu_group_abc"
    profile.mkdir(parents=True)
    (profile / "group_profile.json").write_text('{"kind":"group"}', encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="hermes_multitenancy.agent_real"):
        agent_real._log_feishu_identity_context(
            profile_home=profile,
            shared_home=shared_home,
            sender_open_id="ou_sender",
        )

    assert "using shared Feishu UAT dir" not in caplog.text
    assert "legacy Feishu UAT compatibility dir" in caplog.text
    assert f"profile_uat_dir={profile / 'feishu_uat'}" in caplog.text
    assert "lark_cli_default_identity=bot" in caplog.text


def test_aiagent_subprocess_env_scope_adds_sender_and_lark_broker_env(monkeypatch, tmp_path: Path):
    """The actual spawn path should get per-run broker env overrides."""
    from hermes_multitenancy import agent_real

    profile = tmp_path / "profiles" / "alice"
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir(parents=True)
    event = _event()
    seen: dict[str, object] = {}

    @contextmanager
    def fake_scope(profile_home, sender_open_id):
        seen["profile_home"] = profile_home
        seen["sender_open_id"] = sender_open_id
        yield {
            "HERMES_LARK_CLI_BIN": "/tmp/lark-cli-authsidecar",
            "LARKSUITE_CLI_AUTH_PROXY": "http://127.0.0.1:19090",
            "LARKSUITE_CLI_PROXY_KEY": "short-lived",
            "LARKSUITE_CLI_APP_ID": "cli_public",
        }

    monkeypatch.setattr(agent_real, "_lark_cli_auth_broker_scope", fake_scope)

    with agent_real._aiagent_subprocess_env_scope(
        event,
        profile,
        approval_dir=approval_dir,
        event_stream=True,
    ) as env:
        assert seen["profile_home"] == profile
        assert seen["sender_open_id"] == "ou_test"
        assert env["HERMES_FEISHU_USER_OPEN_ID"] == "ou_test"
        assert env["HERMES_LARK_CLI_BIN"] == "/tmp/lark-cli-authsidecar"
        assert env["LARKSUITE_CLI_AUTH_PROXY"] == "http://127.0.0.1:19090"
        assert env["LARKSUITE_CLI_PROXY_KEY"] == "short-lived"
        assert env["LARKSUITE_CLI_APP_ID"] == "cli_public"
        assert env["HERMES_AIAGENT_EVENT_STREAM"] == "1"


def test_aiagent_subprocess_env_scope_prefers_raw_feishu_open_id_for_broker(monkeypatch, tmp_path: Path):
    """Feishu route ids like g41a5b5g must not be used as UAT broker subjects."""
    from hermes_multitenancy import agent_real

    profile = tmp_path / "profiles" / "alice"
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir(parents=True)
    event = _event()
    event.source.user_id = "g41a5b5g"
    event.sender_open_id = "g41a5b5g"
    event.raw_message = {
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": "ou_real_sender",
                },
            },
        },
    }
    seen: dict[str, object] = {}

    @contextmanager
    def fake_scope(profile_home, sender_open_id):
        seen["sender_open_id"] = sender_open_id
        yield {
            "HERMES_LARK_CLI_BIN": "/tmp/lark-cli-authsidecar",
            "LARKSUITE_CLI_AUTH_PROXY": "http://127.0.0.1:19090",
            "LARKSUITE_CLI_PROXY_KEY": "short-lived",
            "LARKSUITE_CLI_APP_ID": "cli_public",
            "LARKSUITE_CLI_DEFAULT_AS": "user",
        }

    monkeypatch.setattr(agent_real, "_lark_cli_auth_broker_scope", fake_scope)

    with agent_real._aiagent_subprocess_env_scope(
        event,
        profile,
        approval_dir=approval_dir,
    ) as env:
        assert seen["sender_open_id"] == "ou_real_sender"
        assert env["HERMES_FEISHU_USER_OPEN_ID"] == "ou_real_sender"
        assert env["LARKSUITE_CLI_DEFAULT_AS"] == "user"


def test_aiagent_subprocess_env_scope_uses_webui_owner_uat_for_lark_cli_default(
    monkeypatch,
    tmp_path: Path,
):
    """WebUI Run Broker runs must select user identity when the owner has UAT."""
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.run_models import RunRequest
    from hermes_multitenancy.webui_broker_server import _build_webui_event

    shared_home = tmp_path / ".hermes"
    shared_bin = shared_home / "bin"
    shared_bin.mkdir(parents=True)
    lark_cli = shared_bin / "lark-cli-authsidecar"
    lark_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    lark_cli.chmod(0o755)
    profile = shared_home / "profiles" / "owner"
    uat_dir = profile / "feishu_uat"
    uat_dir.mkdir(parents=True)
    (uat_dir / "ou_owner.json").write_text(
        json.dumps({
            "access_token": "user-token",
            "expires_at": int(time.time() * 1000) + 3_600_000,
            "app_id": "cli_public",
        }),
        encoding="utf-8",
    )
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir(parents=True)
    event = _build_webui_event(
        RunRequest(
            channel="webui",
            profile_name="owner",
            user_key="ou_owner",
            content="创建一个飞书云文档",
            session_id="webui-lark-cli-identity",
        )
    )

    class FakeServer:
        url = "http://127.0.0.1:19090"

        def close(self):
            pass

    monkeypatch.setattr(agent_real, "start_lark_cli_auth_broker_server", lambda _context: FakeServer())

    with agent_real._aiagent_subprocess_env_scope(
        event,
        profile,
        approval_dir=approval_dir,
    ) as env:
        assert env["HERMES_FEISHU_USER_OPEN_ID"] == "ou_owner"
        assert env["LARKSUITE_CLI_DEFAULT_AS"] == "user"


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
        captured["cwd"] = _kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    profile = tmp_path / "profiles" / "isolated"
    assert await agent_real._run_aiagent_subprocess(_event(), profile) == "ok"

    child_env = captured["env"]
    assert "OPENAI_API_KEY" not in child_env, "parent secret leaked to child"
    assert child_env["HOME"] == str(profile / "home")
    assert child_env["WORKSPACE"] == str(profile / "workspace")
    assert child_env["KEP_PROFILE"] == "isolated"
    assert child_env["HERMES_PROFILE"] == "isolated"
    assert child_env["TMPDIR"] == str(profile / "tmp")
    assert child_env["HERMES_HOME"] == str(profile)
    assert captured["cwd"] == str(profile / "workspace")


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


def test_aiagent_subprocess_cwd_uses_profile_workspace(tmp_path: Path):
    from hermes_multitenancy import agent_real

    profile = tmp_path / "profiles" / "isolated"
    cwd = agent_real._aiagent_subprocess_cwd(profile)
    assert cwd == str(profile / "workspace")
    assert (profile / "workspace").is_dir()


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


def test_sandbox_policy_only_allows_signalling_children():
    from hermes_multitenancy import agent_real

    policy = agent_real._SANDBOX_POLICY_FILE.read_text(encoding="utf-8")

    assert "(allow signal (target children))" in policy
    assert "(allow signal)" not in policy.replace("(allow signal (target children))", "")


def test_macos_sandbox_policy_allows_native_browser_paths_without_personal_chrome():
    from hermes_multitenancy import agent_real

    policy = agent_real._SANDBOX_POLICY_FILE.read_text(encoding="utf-8")

    assert '(string-append (param "HERMES_AGENT_REPO") "/node_modules/agent-browser")' in policy
    assert '(string-append (param "PROFILE_HOME") "/browser")' in policy
    assert '(string-append (param "SHARED_HOME") "/browser-browsers")' in policy
    assert "Application Support/Google/Chrome" not in policy


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


def test_bwrap_default_args_provides_openclaw_workspace_and_shared_bin():
    """Linux sandbox should expose profile workspace at /workspace and shared tool bin."""
    from hermes_multitenancy import agent_real

    args_file = agent_real._BWRAP_ARGS_FILE
    tokens = agent_real._render_bwrap_args(args_file.read_text(), {
        "PROFILE_HOME": "/probe/shared/profiles/alice",
        "SHARED_HOME": "/probe/shared",
        "USER_HOME": "/probe/user",
        "HERMES_VENV": "/probe/venv",
        "HERMES_AGENT_INSTALL": "/probe/install",
        "HERMES_AGENT_REPO": "/probe/agent-repo",
        "HERMES_MT_REPO": "/probe/mt-repo",
    })

    triples = set(zip(tokens, tokens[1:], tokens[2:]))
    assert ("--bind", "/probe/shared/profiles/alice/workspace", "/workspace") in triples
    assert ("--ro-bind-try", "/probe/shared/bin", "/probe/shared/bin") in triples
    assert (
        "--ro-bind-try",
        "/probe/shared/browser-browsers",
        "/probe/shared/browser-browsers",
    ) in triples


def test_bwrap_default_args_does_not_bind_entire_shared_home():
    """Linux sandbox must not expose sibling profile directories read-only."""
    from hermes_multitenancy import agent_real

    args_file = agent_real._BWRAP_ARGS_FILE
    tokens = agent_real._render_bwrap_args(args_file.read_text(), {
        "PROFILE_HOME": "/probe/shared/profiles/alice",
        "SHARED_HOME": "/probe/shared",
        "USER_HOME": "/probe/user",
        "HERMES_VENV": "/probe/venv",
        "HERMES_AGENT_INSTALL": "/probe/install",
        "HERMES_AGENT_REPO": "/probe/agent-repo",
        "HERMES_MT_REPO": "/probe/mt-repo",
    })

    triples = set(zip(tokens, tokens[1:], tokens[2:]))
    assert ("--ro-bind", "/probe/shared", "/probe/shared") not in triples
    assert ("--dir", "/probe/shared/profiles", "--dir") in triples
    assert (
        "--bind",
        "/probe/shared/profiles/alice",
        "/probe/shared/profiles/alice",
    ) in triples


def test_wrap_linux_bwrap_binds_installed_shared_skill_symlink_targets(monkeypatch, tmp_path: Path):
    """Profile skill symlinks to shared skills must resolve inside bwrap."""
    import os as _os
    from hermes_multitenancy import agent_real

    fake_policy = tmp_path / "bwrap.args"
    fake_policy.write_text("--dir ${SHARED_HOME}\n--bind ${PROFILE_HOME} ${PROFILE_HOME}\n")
    fake_bin = tmp_path / "bwrap"
    fake_bin.write_text("#!/bin/sh\nexec \"$@\"\n")
    _os.chmod(fake_bin, 0o755)

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "owner"
    skill_source = shared / "skills" / "Keep" / "kep-prd-analysis"
    (skill_source / "references").mkdir(parents=True)
    (skill_source / "SKILL.md").write_text("# KEP PRD Analysis\n", encoding="utf-8")
    (skill_source / "references" / "architecture-examples.md").write_text("ref", encoding="utf-8")
    skill_link = profile / "skills" / "Keep" / "kep-prd-analysis"
    skill_link.parent.mkdir(parents=True)
    skill_link.symlink_to(skill_source, target_is_directory=True)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_AGENT_REPO", str(tmp_path / "agent-repo"))
    monkeypatch.setattr(agent_real, "_BWRAP_ARGS_FILE", fake_policy)
    monkeypatch.setattr(agent_real, "_BWRAP_EXEC", str(fake_bin))

    wrapped = agent_real._wrap_with_sandbox(["/usr/bin/python3"], profile)

    triples = set(zip(wrapped, wrapped[1:], wrapped[2:]))
    assert ("--dir", str(shared / "skills"), "--dir") in triples
    assert ("--dir", str(shared / "skills" / "Keep"), "--ro-bind") in triples
    assert ("--ro-bind", str(skill_source), str(skill_source)) in triples
    assert ("--ro-bind", str(shared / "skills"), str(shared / "skills")) not in triples


def test_wrap_linux_bwrap_skips_secret_like_shared_skill_targets(monkeypatch, tmp_path: Path):
    """A shared skill symlink is not mounted if its target later gains secrets."""
    import os as _os
    from hermes_multitenancy import agent_real

    fake_policy = tmp_path / "bwrap.args"
    fake_policy.write_text("--dir ${SHARED_HOME}\n--bind ${PROFILE_HOME} ${PROFILE_HOME}\n")
    fake_bin = tmp_path / "bwrap"
    fake_bin.write_text("#!/bin/sh\nexec \"$@\"\n")
    _os.chmod(fake_bin, 0o755)

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "owner"
    skill_source = shared / "skills" / "Keep" / "secret-prd-analysis"
    skill_source.mkdir(parents=True)
    (skill_source / "SKILL.md").write_text("# Secret PRD Analysis\n", encoding="utf-8")
    (skill_source / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    skill_link = profile / "skills" / "Keep" / "secret-prd-analysis"
    skill_link.parent.mkdir(parents=True)
    skill_link.symlink_to(skill_source, target_is_directory=True)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_AGENT_REPO", str(tmp_path / "agent-repo"))
    monkeypatch.setattr(agent_real, "_BWRAP_ARGS_FILE", fake_policy)
    monkeypatch.setattr(agent_real, "_BWRAP_EXEC", str(fake_bin))

    wrapped = agent_real._wrap_with_sandbox(["/usr/bin/python3"], profile)

    triples = set(zip(wrapped, wrapped[1:], wrapped[2:]))
    assert ("--ro-bind", str(skill_source), str(skill_source)) not in triples


def test_wrap_linux_bwrap_ignores_non_skill_dependency_symlinks(monkeypatch, tmp_path: Path, caplog):
    """Dependency symlinks under a copied skill are not treated as skill roots."""
    import os as _os
    from hermes_multitenancy import agent_real

    fake_policy = tmp_path / "bwrap.args"
    fake_policy.write_text("--dir ${SHARED_HOME}\n--bind ${PROFILE_HOME} ${PROFILE_HOME}\n")
    fake_bin = tmp_path / "bwrap"
    fake_bin.write_text("#!/bin/sh\nexec \"$@\"\n")
    _os.chmod(fake_bin, 0o755)

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "owner"
    dep_source = shared / "skills" / "Keep" / "keep-record" / "node_modules"
    dep_source.mkdir(parents=True)
    (dep_source / "token-helper.js").write_text("// not a skill root\n", encoding="utf-8")
    skill_dir = profile / "skills" / "Keep" / "keep-record"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Keep Record\n", encoding="utf-8")
    dep_link = skill_dir / "node_modules"
    dep_link.symlink_to(dep_source, target_is_directory=True)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_AGENT_REPO", str(tmp_path / "agent-repo"))
    monkeypatch.setattr(agent_real, "_BWRAP_ARGS_FILE", fake_policy)
    monkeypatch.setattr(agent_real, "_BWRAP_EXEC", str(fake_bin))

    with caplog.at_level(logging.WARNING, logger="hermes_multitenancy.agent_real"):
        wrapped = agent_real._wrap_with_sandbox(["/usr/bin/python3"], profile)

    triples = set(zip(wrapped, wrapped[1:], wrapped[2:]))
    assert ("--ro-bind", str(dep_source), str(dep_source)) not in triples
    assert not any("skipping shared skill sandbox bind" in rec.message for rec in caplog.records)


def test_bwrap_default_args_masks_profile_and_shared_secret_files():
    """The sandbox may use env-loaded secrets, but tools must not read secret files."""
    from hermes_multitenancy import agent_real

    args_file = agent_real._BWRAP_ARGS_FILE
    tokens = agent_real._render_bwrap_args(args_file.read_text(), {
        "PROFILE_HOME": "/probe/shared/profiles/alice",
        "SHARED_HOME": "/probe/shared",
        "USER_HOME": "/probe/user",
        "HERMES_VENV": "/probe/venv",
        "HERMES_AGENT_INSTALL": "/probe/install",
        "HERMES_AGENT_REPO": "/probe/agent-repo",
        "HERMES_MT_REPO": "/probe/mt-repo",
    })

    triples = set(zip(tokens, tokens[1:], tokens[2:]))
    for secret_path in (
        "/probe/shared/.env",
        "/probe/shared/auth.json",
    ):
        assert ("--ro-bind-try", "/dev/null", secret_path) in triples
