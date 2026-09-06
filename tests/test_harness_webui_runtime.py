from __future__ import annotations

import asyncio
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_multitenancy import agent_real
from hermes_multitenancy.agent_real import codex_provider_proxy
from hermes_multitenancy.agent_real import executor_map
from hermes_multitenancy.agent_real import _core
from hermes_multitenancy.agent_real import codex_home
from hermes_multitenancy.agent_real import run_workspace
from hermes_multitenancy.agent_real import harness_webui_runtime as harness_runtime
from hermes_multitenancy.agent_real.harness_webui_runtime import (
    HarnessAdmissionRejected,
    codex_thread_id_for_agent,
    codex_thread_resume_scope,
    issue_webui_harness_admission,
    require_event_admission,
)
from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal
from hermes_multitenancy.run_models import RunRequest
from hermes_multitenancy.webui_broker.periphery import _default_dispatch_agent


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return path


def _harness_env(repo: Path, *, profiles: str = "alice") -> dict[str, str]:
    revision = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    codex = repo.parent / "codex"
    codex.write_text("#!/bin/sh\necho codex-cli 0.150.1\n", encoding="utf-8")
    codex.chmod(0o755)
    ready = repo.parent / "harness.ready"
    ready.write_text(f"{revision}\n", encoding="utf-8")
    return {
        "HERMES_WEBUI_HARNESS_ENABLED": "1",
        "HERMES_WEBUI_HARNESS_PROFILES": profiles,
        "HERMES_WEBUI_HARNESS_REPO": str(repo),
        "HERMES_WEBUI_HARNESS_SOURCE_REV": revision,
        "HERMES_WEBUI_HARNESS_READY_FILE": str(ready),
        "HERMES_WEBUI_HARNESS_CODEX_BIN": str(codex),
        "HERMES_WEBUI_HARNESS_CODEX_VERSION": "0.150.1",
    }


def _event(profile: Path, admission):
    principal = issue_webui_principal(
        profile_name=profile.name,
        actor_subject=admission.actor_subject,
        credential_subject=admission.actor_subject,
    )
    return SimpleNamespace(
        raw_event={
            "channel": "webui",
            "session_id": "session-1",
            "workspace": admission.workspace,
            "metadata": {"expert_id": "server-dev"},
        },
        trusted_runtime_principal=principal,
        trusted_harness_admission=admission,
    )


async def _accept_provider_audit(*_args):
    return None


@pytest.fixture(autouse=True)
def _linux_harness_runtime(monkeypatch):
    monkeypatch.setattr(harness_runtime, "_PLATFORM", "linux")


def test_trusted_harness_admission_seals_session_workspace_and_scopes_workflow(
    tmp_path: Path,
):
    repo = _git_repo(tmp_path / "source")
    env = _harness_env(repo)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        workspace="project-a",
        environ=env,
    )
    other = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_bob",
        session_id="session-1",
        engine="harness",
        environ=env,
    )

    assert admission.workspace == "project-a"
    assert admission.workflow_id != other.workflow_id
    assert executor_map.runtime_for_event(
        _event(tmp_path / "alice", admission),
        tmp_path / "alice",
        environ={},
    ) == executor_map.CODEX_APP_SERVER

    changed = _event(tmp_path / "alice", admission)
    changed.raw_event["workspace"] = "project-b"
    with pytest.raises(HarnessAdmissionRejected, match="principal_mismatch"):
        require_event_admission(changed, tmp_path / "alice")


def test_harness_admission_is_default_off(tmp_path: Path):
    repo = _git_repo(tmp_path / "source")

    with pytest.raises(HarnessAdmissionRejected, match="disabled"):
        issue_webui_harness_admission(
            profile_name="alice",
            actor_subject="ou_alice",
            session_id="session-1",
            engine="harness",
            environ={"HERMES_WEBUI_HARNESS_REPO": str(repo)},
        )


def test_harness_admission_is_available_to_every_trusted_profile(tmp_path: Path):
    repo = _git_repo(tmp_path / "source")

    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo, profiles="bob"),
    )
    assert admission.profile_name == "alice"


def test_harness_admission_ignores_project_state_but_closes_when_codex_drifts(tmp_path: Path):
    repo = _git_repo(tmp_path / "source")
    env = _harness_env(repo)
    (repo / "unexpected.txt").write_text("drift\n", encoding="utf-8")

    issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=env,
    )

    (repo / "unexpected.txt").unlink()
    env["HERMES_WEBUI_HARNESS_CODEX_VERSION"] = "0.150.2"
    with pytest.raises(HarnessAdmissionRejected, match="codex_version_mismatch"):
        issue_webui_harness_admission(
            profile_name="alice",
            actor_subject="ou_alice",
            session_id="session-1",
            engine="harness",
            environ=env,
        )

    env["HERMES_WEBUI_HARNESS_CODEX_VERSION"] = "0.150.1"
    Path(env["HERMES_WEBUI_HARNESS_CODEX_BIN"]).write_text(
        "#!/bin/sh\n[ \"$1\" = sandbox ] && exit 1\necho codex-cli 0.150.1\n",
        encoding="utf-8",
    )
    with pytest.raises(HarnessAdmissionRejected, match="sandbox_unavailable"):
        issue_webui_harness_admission(
            profile_name="alice",
            actor_subject="ou_alice",
            session_id="session-1",
            engine="harness",
            environ=env,
        )


def test_harness_admission_seals_one_supported_flow(tmp_path: Path):
    repo = _git_repo(tmp_path / "source")
    env = _harness_env(repo)

    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        flow="server-bugfix",
        environ=env,
    )

    assert admission.flow == "server-bugfix"
    with pytest.raises(HarnessAdmissionRejected, match="flow_invalid"):
        issue_webui_harness_admission(
            profile_name="alice",
            actor_subject="ou_alice",
            session_id="session-2",
            engine="harness",
            flow="invented-flow",
            environ=env,
        )


def test_local_webui_account_subject_is_sealed_without_feishu_shape(tmp_path: Path):
    repo = _git_repo(tmp_path / "source")

    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="1",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )
    principal = issue_webui_principal(
        profile_name="alice", actor_subject="1", credential_subject="1"
    )

    assert admission.actor_subject == "1"
    assert principal.is_authentic()


@pytest.mark.parametrize("actor", ["", "../alice", "alice user"])
def test_local_webui_account_subject_rejects_non_opaque_values(actor: str):
    with pytest.raises(ValueError, match="incomplete or inconsistent"):
        issue_webui_principal(
            profile_name="alice", actor_subject=actor, credential_subject=actor
        )


def test_local_harness_requires_run_scoped_provider_credential(
    tmp_path: Path, monkeypatch
):
    source = _git_repo(tmp_path / "source")
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(source),
    )
    event = _event(profile, admission)
    monkeypatch.setattr(_core, "_require_codex_spend_state_for_event", lambda *_: object())

    workspace = _core._codex_run_workspace_for_event(event, profile)
    assert workspace.repo_dir == (profile / "workspace").resolve()

    with pytest.raises(
        executor_map.ExecutorUnavailable,
        match="run-scoped Codex provider proxy credential",
    ):
        _core._codex_runtime_env(event, profile, workspace, "", "")


def test_local_harness_requires_actor_bound_billing_metadata(tmp_path: Path):
    source = _git_repo(tmp_path / "source")
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(source),
    )
    event = _event(profile, admission)

    with pytest.raises(executor_map.ExecutorUnavailable, match="billing identity"):
        _core._require_codex_spend_state_for_event(event, profile)

    event.raw_event["metadata"] = {
        "litellm_billing_enforced": True,
        "litellm_billing_profile_name": "alice",
        "litellm_billing_employee_user_id": "employee-1",
        "litellm_billing_actor_subject": "ou_alice",
    }
    assert _core._require_codex_spend_state_for_event(event, profile)[
        "litellm_billing_employee_user_id"
    ] == "employee-1"

    event.raw_event["metadata"]["litellm_billing_actor_subject"] = "ou_other"
    with pytest.raises(executor_map.ExecutorUnavailable, match="billing identity"):
        _core._require_codex_spend_state_for_event(event, profile)


def test_local_harness_uses_selected_plain_workspace_without_cloning_source(
    tmp_path: Path, monkeypatch
):
    source = _git_repo(tmp_path / "source")
    profile = tmp_path / "profiles" / "alice"
    selected = profile / "workspace" / "project-a"
    selected.mkdir(parents=True)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        workspace="project-a",
        environ=_harness_env(source),
    )
    event = _event(profile, admission)
    monkeypatch.setattr(_core, "_require_codex_spend_state_for_event", lambda *_: object())
    monkeypatch.setattr(
        _core,
        "_codex_model_and_base_url",
        lambda *_: ("gpt-5.6-terra", "https://litellm.example/v1"),
    )
    plugin = tmp_path / "plugin"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        '{"name":"server-dev","version":"1.0.0"}', encoding="utf-8"
    )
    monkeypatch.setattr(_core, "_codex_expert_plugin_dir", lambda *_: plugin)
    monkeypatch.setattr(_core, "_broker_role_override_block_for_event", lambda *_: "SERVER EXPERT")

    workspace = _core._codex_run_workspace_for_event(event, profile)
    assert workspace.repo_dir == selected.resolve()
    assert not (workspace.repo_dir / "README.md").exists()

    runtime_env = _core._codex_runtime_env(
        event,
        profile,
        workspace,
        "run-scoped-key",
        "http://127.0.0.1:12345/v1",
    )
    assert runtime_env["HERMES_TERMINAL_SECURITY_MODE"] == "approval-required"
    assert runtime_env["HERMES_CODEX_BIN"] == str(admission.codex_bin)
    assert runtime_env["HERMES_HARNESS_WORKFLOW_ID"] == admission.workflow_id
    assert "HERMES_HARNESS_STATE_DB" not in runtime_env
    assert "HERMES_HARNESS_ACTOR" not in runtime_env
    assert "HERMES_HARNESS_PROFILE" not in runtime_env
    config = (Path(runtime_env["CODEX_HOME"]) / "config.toml").read_text(encoding="utf-8")
    assert "approval_policy" not in config
    assert 'sandbox_mode = "workspace-write"' in config
    assert "plugins = false" in config
    assert "auth.json" not in {path.name for path in Path(runtime_env["CODEX_HOME"]).iterdir()}
    assert runtime_env[_core.CODEX_RUNTIME_KEY_ENV] == "run-scoped-key"
    harness_home = workspace.root / "home"
    assert runtime_env["HOME"] == str(harness_home)
    assert runtime_env["_HERMES_FORCE_HOME"] == str(harness_home)
    assert harness_home.is_dir()
    assert harness_home.stat().st_mode & 0o777 == 0o700

    (workspace.repo_dir / "README.md").write_text("isolated\n", encoding="utf-8")
    assert (source / "README.md").read_text() == "source\n"


def test_local_harness_child_keeps_actor_capabilities_without_ambient_keys(
    tmp_path: Path, monkeypatch
):
    source = _git_repo(tmp_path / "source")
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("OPENAI_API_KEY=profile-secret\n", encoding="utf-8")
    (profile / "auth.json").write_text('{"token":"profile-secret"}', encoding="utf-8")
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "ambient-secret")
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(source),
    )
    event = _event(profile, admission)
    approval_dir = tmp_path / "approvals"
    approval_dir.mkdir()
    monkeypatch.setattr(_core, "_codex_run_workspace_for_event", lambda *_: None)
    @contextmanager
    def lark_scope(*_args, **_kwargs):
        yield {
            "LARKSUITE_CLI_AUTH_PROXY": "http://127.0.0.1:19090",
            "LARKSUITE_CLI_PROXY_KEY": "actor-bound",
        }

    monkeypatch.setattr(_core, "_lark_cli_auth_broker_scope", lark_scope)

    with _core._aiagent_subprocess_env_scope(
        event, profile, approval_dir=approval_dir
    ) as env:
        assert "HERMES_MULTITENANCY_CRED_BROKER_TOKEN" not in env
        assert "HERMES_MULTITENANCY_SESSION_SEARCH_TOKEN" in env
        assert env["LARKSUITE_CLI_AUTH_PROXY"] == "http://127.0.0.1:19090"
        assert "HERMES_RUN_BROKER_KEY" in env

    monkeypatch.setitem(
        _core._build_subprocess_env.__globals__,
        "_credential_env_for_aiagent",
        lambda *_args, **_kwargs: {"GITLAB_TOKEN": "actor-gitlab"},
    )
    env = _core._build_subprocess_env(
        profile,
        approval_dir=approval_dir,
        extra={
            "HERMES_LOCAL_HARNESS": "1",
            "LARKSUITE_CLI_AUTH_PROXY": "http://127.0.0.1:19090",
            "LARKSUITE_CLI_PROXY_KEY": "actor-bound",
        },
    )
    assert "HERMES_MULTITENANCY_CREDENTIAL_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["GITLAB_TOKEN"] == "actor-gitlab"
    assert env["LARKSUITE_CLI_PROXY_KEY"] == "actor-bound"
    assert "HERMES_YOLO_MODE" not in env
    assert "HERMES_SANDBOX_HOST" not in env


def test_local_harness_never_maps_an_opaque_web_actor_to_profile_owner(
    tmp_path: Path, monkeypatch
):
    source = _git_repo(tmp_path / "source")
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="web-user-1",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(source),
    )
    event = _event(profile, admission)
    approval_dir = tmp_path / "approvals"
    approval_dir.mkdir()
    monkeypatch.setattr(_core, "_codex_run_workspace_for_event", lambda *_: None)
    monkeypatch.setattr(_core, "_profile_owner_open_id", lambda *_: "ou_profile_owner")
    lark_scope = pytest.fail
    monkeypatch.setattr(_core, "_lark_cli_auth_broker_scope", lark_scope)

    with _core._aiagent_subprocess_env_scope(
        event, profile, approval_dir=approval_dir
    ) as env:
        assert "HERMES_FEISHU_USER_OPEN_ID" not in env
        assert "HERMES_RUN_BROKER_KEY" not in env


def test_credential_passthrough_ignores_ambient_gitlab_env(tmp_path: Path, monkeypatch):
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    registered = []
    monkeypatch.setenv("GITLAB_TOKEN", "operator-token")
    monkeypatch.setattr(_core, "_credential_env_for_aiagent", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(_core, "_run_level_gitlab_env_names", lambda *_: set())
    from tools import env_passthrough as env_passthrough_mod
    monkeypatch.setattr(env_passthrough_mod, "register_env_passthrough", registered.extend)
    monkeypatch.setattr(env_passthrough_mod, "_config_passthrough", frozenset())

    _core._install_credential_env_passthrough(profile)

    assert registered == []


def test_codex_thread_resume_scope_rewrites_only_thread_start():
    class FakeClient:
        calls: list[tuple[str, dict, float]] = []

        def request(self, method, params=None, timeout=30.0):
            self.calls.append((method, dict(params or {}), timeout))
            if method == "thread/resume":
                return {"thread": {"id": params["threadId"]}}
            return {"ok": True}

    class FakeSession:
        def __init__(self, **kwargs):
            self.client_factory = kwargs["client_factory"]

        def close(self):
            pass

    agent = SimpleNamespace(session_cwd="/tmp/isolated", _codex_session=None)
    bound = []
    with codex_thread_resume_scope(
        "thread_bound",
        agent=agent,
        on_thread_bound=bound.append,
        client_class=FakeClient,
        session_class=FakeSession,
        routing_class=lambda: object(),
        event_bridge=lambda _event: None,
    ):
        client = agent._codex_session.client_factory()
        result = client.request("thread/start", {"cwd": "/tmp/isolated"}, timeout=15)
        untouched = client.request("turn/start", {"threadId": "thread_bound"})

    assert result["thread"]["id"] == "thread_bound"
    assert FakeClient.calls == [
        (
            "thread/resume",
            {"threadId": "thread_bound", "cwd": "/tmp/isolated"},
            15,
        ),
        ("turn/start", {"threadId": "thread_bound"}, 30.0),
    ]
    assert untouched == {"ok": True}
    assert bound == ["thread_bound"]

    client.request("thread/start", {"cwd": "/tmp/fresh"})
    assert FakeClient.calls[-1][0] == "thread/start"


def test_codex_thread_resume_scope_pins_binary_on_first_turn(monkeypatch):
    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def close(self):
            pass

    monkeypatch.setenv("HERMES_CODEX_BIN", "/opt/codex/bin/codex")
    agent = SimpleNamespace(session_cwd="/tmp/isolated", _codex_session=None)

    with codex_thread_resume_scope(
        None,
        agent=agent,
        client_class=object,
        session_class=FakeSession,
        routing_class=lambda: object(),
        event_bridge=lambda _event: None,
    ):
        assert agent._codex_session.kwargs["codex_bin"] == "/opt/codex/bin/codex"


def test_codex_thread_resume_scope_closes_run_owned_session_on_exit():
    class FakeSession:
        def __init__(self, **_kwargs):
            self.closed = False

        def close(self):
            self.closed = True

    agent = SimpleNamespace(_codex_session=None)
    with codex_thread_resume_scope(
        None,
        agent=agent,
        client_class=object,
        session_class=FakeSession,
        routing_class=lambda: object(),
        event_bridge=lambda _event: None,
    ):
        session = agent._codex_session

    assert session.closed is True
    assert agent._codex_session is None


def test_codex_thread_resume_scope_preserves_turn_error_when_close_fails():
    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        def close(self):
            raise RuntimeError("close failed")

    agent = SimpleNamespace(_codex_session=None)

    with pytest.raises(ValueError, match="turn failed"):
        with codex_thread_resume_scope(
            None,
            agent=agent,
            client_class=object,
            session_class=FakeSession,
            routing_class=lambda: object(),
            event_bridge=lambda _event: None,
        ):
            raise ValueError("turn failed")

    assert agent._codex_session is None


def test_pinned_real_codex_session_close_is_sync_and_waits_for_process():
    from agent.transports.codex_app_server import CodexAppServerClient
    from agent.transports.codex_app_server_session import CodexAppServerSession

    class FakeStdin:
        closed = False

        def close(self):
            self.closed = True

    class FakeProcess:
        stdin = FakeStdin()
        terminated = False
        killed = False
        waits = []

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            self.waits.append(timeout)

            if len(self.waits) == 1:
                raise subprocess.TimeoutExpired("codex app-server", timeout)

        def kill(self):
            self.killed = True

    client = CodexAppServerClient.__new__(CodexAppServerClient)
    client._closed = False
    client._proc = FakeProcess()
    session = CodexAppServerSession()
    session._client = client
    session._thread_id = "thread_bound"

    assert not asyncio.iscoroutinefunction(session.close)
    session.close()

    assert client._proc.terminated is True
    assert client._proc.killed is True
    assert client._proc.waits == [3.0, 1.0]
    assert session._client is None
    assert session._thread_id is None


def test_codex_thread_resume_scope_rejects_missing_resume_identity():
    class FakeClient:
        def request(self, method, params=None, timeout=30.0):
            return {"ok": True}

    class FakeSession:
        def __init__(self, **kwargs):
            self.client_factory = kwargs["client_factory"]

        def close(self):
            pass

    agent = SimpleNamespace(_codex_session=None)
    with codex_thread_resume_scope(
        "thread_bound",
        agent=agent,
        client_class=FakeClient,
        session_class=FakeSession,
        routing_class=lambda: object(),
        event_bridge=lambda _event: None,
    ):
        with pytest.raises(HarnessAdmissionRejected, match="resume_result_invalid"):
            agent._codex_session.client_factory().request("thread/start", {})


def test_codex_thread_receipt_reads_real_core_private_field():
    agent = SimpleNamespace(
        _codex_session=SimpleNamespace(_thread_id="thread_bound")
    )

    assert codex_thread_id_for_agent(agent) == "thread_bound"


def test_two_rounds_bind_then_resume_same_runtime_thread(tmp_path: Path, monkeypatch):
    repo = _git_repo(tmp_path / "source")
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "model:\n"
        "  default: custom:litellm-sre/auto\n"
        "  base_url: https://server.example/v1\n",
        encoding="utf-8",
    )
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )
    resumes = []
    resolved_models = []

    async def child(event, _profile_home, *, messages=None):
        resolved_models.append(_core._codex_model_and_base_url(event, profile))
        resumes.append(getattr(event, "_harness_resume_thread_id", None))
        yield "harness_thread_bound", {"thread_id": "thread_bound"}
        yield "done", "ok"

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", child)
    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", _accept_provider_audit)

    async def run_two_rounds():
        for _ in range(2):
            event = _event(profile, admission)
            event.raw_event["metadata"].update(
                {"model": "gpt-5.4", "provider": "custom:litellm-sre"}
            )
            items = [
                item
                async for item in _core._verified_codex_stream(event, profile)
            ]
            assert ("done", "ok") in items
            assert all(kind != "harness_thread_bound" for kind, _ in items)

    asyncio.run(run_two_rounds())
    assert resumes == [None, "thread_bound"]
    assert resolved_models == [
        ("gpt-5.4", "https://server.example/v1"),
        ("gpt-5.4", "https://server.example/v1"),
    ]


def test_bound_thread_persists_before_first_round_failure(tmp_path: Path, monkeypatch):
    repo = _git_repo(tmp_path / "source")
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )
    resumes = []

    async def child(event, _profile_home, *, messages=None):
        resume = getattr(event, "_harness_resume_thread_id", None)
        resumes.append(resume)
        yield "harness_thread_bound", {"thread_id": resume or f"thread-{len(resumes)}"}
        yield ("error", "failed") if len(resumes) == 1 else ("done", "ok")

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", child)
    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", _accept_provider_audit)

    async def run_twice():
        for _ in range(2):
            event = _event(profile, admission)
            _ = [item async for item in _core._verified_codex_stream(event, profile)]

    asyncio.run(run_twice())
    assert resumes == [None, "thread-1"]


def test_local_harness_withholds_output_until_provider_audit_passes(
    tmp_path: Path, monkeypatch
):
    repo = _git_repo(tmp_path / "source")
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )

    async def child(_event, _profile_home, *, messages=None):
        yield "harness_thread_bound", {"thread_id": "thread-1"}
        yield "content", "must-not-leak"
        yield "done", "must-not-leak"

    async def reject_audit(_event, _profile_home):
        raise RuntimeError("provider audit failed")

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", child)
    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", reject_audit)

    observed = []

    async def run():
        with pytest.raises(RuntimeError, match="provider audit failed"):
            async for item in _core._verified_codex_stream(
                _event(profile, admission), profile
            ):
                observed.append(item)

    asyncio.run(run())
    assert observed == []


def _mark_budget_exhausted(event) -> None:
    """代理审计里「预算耗尽」的真实形状（收尾门认的那一组计数器）。"""
    event._trusted_codex_proxy_audit = {
        "budget_exhausted": True,
        "rejected_over_limit": 1,
        "rejected_concurrent": 0,
        "rejected_requests": 1,
        "request_count": codex_provider_proxy.MAX_HARNESS_REQUESTS,
        "request_limit": codex_provider_proxy.MAX_HARNESS_REQUESTS,
        "total_tokens": 260000,
    }
    event._trusted_codex_proxy_request_limit = codex_provider_proxy.MAX_HARNESS_REQUESTS


def test_budget_exhausted_run_releases_output_with_a_user_facing_notice(
    tmp_path: Path, monkeypatch
):
    """事故 2026-09-03: 预算耗尽的一轮曾整个变成 Harness is unavailable。"""
    repo = _git_repo(tmp_path / "source")
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )

    async def child(event, _profile_home, *, messages=None):
        yield "harness_thread_bound", {"thread_id": "thread-1"}
        yield "content", "partial answer"
        _mark_budget_exhausted(event)

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", child)
    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", _accept_provider_audit)

    async def run():
        return [
            item
            async for item in _core._verified_codex_stream(
                _event(profile, admission), profile
            )
        ]

    items = asyncio.run(run())
    assert items[0] == ("content", "partial answer")
    kind, notice = items[-1]
    assert kind == "content"
    assert "本轮已用满 64 次模型调用" in notice
    assert "请缩小问题或继续追问" in notice


def test_a_normal_run_gets_no_budget_notice(tmp_path: Path, monkeypatch):
    repo = _git_repo(tmp_path / "source")
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )

    async def child(event, _profile_home, *, messages=None):
        yield "harness_thread_bound", {"thread_id": "thread-1"}
        yield "content", "full answer"
        event._trusted_codex_proxy_audit = {"budget_exhausted": False}
        event._trusted_codex_proxy_request_limit = (
            codex_provider_proxy.MAX_HARNESS_REQUESTS
        )

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", child)
    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", _accept_provider_audit)

    async def run():
        return [
            item
            async for item in _core._verified_codex_stream(
                _event(profile, admission), profile
            )
        ]

    assert asyncio.run(run()) == [("content", "full answer")]


def test_local_harness_releases_control_before_provider_audit(
    tmp_path: Path, monkeypatch
):
    repo = _git_repo(tmp_path / "source")
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )
    order = []

    async def child(_event, _profile_home, *, messages=None):
        yield "harness_thread_bound", {"thread_id": "thread-1"}
        yield "approval_required", {"approval_id": "approval-1"}
        yield "content", "verified later"

    async def audit(_event, _profile_home):
        order.append("audit")

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", child)
    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", audit)

    async def run():
        async for kind, _payload in _core._verified_codex_stream(
            _event(profile, admission), profile
        ):
            order.append(kind)

    asyncio.run(run())
    assert order == ["approval_required", "audit", "content"]


def test_default_dispatch_seals_harness_admission_onto_runtime_event(
    tmp_path: Path, monkeypatch
):
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    repo = _git_repo(tmp_path / "source")
    principal = issue_webui_principal(
        profile_name="alice",
        actor_subject="ou_alice",
        credential_subject="ou_alice",
    )
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )
    request = RunRequest(
        channel="webui",
        profile_name="alice",
        user_key="ou_alice",
        credential_subject="ou_alice",
        content="hello",
        session_id="session-1",
        message_id="message-1",
        idempotency_key="idem-1",
    )
    seen = []

    from hermes_multitenancy import router as router_mod

    monkeypatch.setattr(router_mod, "_profile_name_to_home", lambda _name: profile)

    async def stream(event, _profile_home, *, messages=None):
        seen.append(event)
        yield "content", "ok"
        yield "done", "ok"

    monkeypatch.setattr(agent_real, "stream_run_agent", stream)
    asyncio.run(
        _default_dispatch_agent(
            request,
            emit_event=lambda _event: None,
            trusted_principal=principal,
            trusted_harness_admission=admission,
        )
    )

    assert seen[0].trusted_harness_admission is admission


def test_codex_thread_resume_scope_passes_managed_codex_home(monkeypatch):
    """prod 2026-09-02: core's host-tools gate raises `need a managed CODEX_HOME`
    unless the session is built with an explicit codex_home; MT materializes it
    and exports CODEX_HOME, so the session must carry that value."""

    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def close(self):
            pass

    agent = SimpleNamespace(session_cwd="/tmp/isolated", _codex_session=None)
    scope_kwargs = dict(
        agent=agent,
        client_class=object,
        session_class=FakeSession,
        routing_class=lambda: object(),
        event_bridge=lambda _event: None,
    )

    monkeypatch.setenv("CODEX_HOME", "/tmp/run/codex-home")
    with codex_thread_resume_scope(None, **scope_kwargs):
        assert agent._codex_session.kwargs["codex_home"] == "/tmp/run/codex-home"

    monkeypatch.delenv("CODEX_HOME")
    with codex_thread_resume_scope(None, **scope_kwargs):
        assert agent._codex_session.kwargs["codex_home"] is None


def test_codex_thread_resume_scope_floors_initialize_timeout(monkeypatch):
    """prod 2026-09-02 19:43: codex `initialize` timed out at the core's 10s default
    under CI load; the same session's retry passed. Only `initialize` is floored."""

    class FakeClient:
        calls: list[tuple[str, float]] = []

        def request(self, method, params=None, timeout=30.0):
            self.calls.append((method, timeout))
            return {"ok": True}

    class FakeSession:
        def __init__(self, **kwargs):
            self.client_factory = kwargs["client_factory"]

        def close(self):
            pass

    agent = SimpleNamespace(session_cwd="/tmp/isolated", _codex_session=None)
    scope_kwargs = dict(
        agent=agent,
        client_class=FakeClient,
        session_class=FakeSession,
        routing_class=lambda: object(),
        event_bridge=lambda _event: None,
    )

    monkeypatch.delenv("HERMES_CODEX_INITIALIZE_TIMEOUT", raising=False)
    with codex_thread_resume_scope(None, **scope_kwargs):
        client = agent._codex_session.client_factory()
        client.request("initialize", {}, timeout=10.0)
        client.request("turn/start", {}, timeout=10.0)
    assert FakeClient.calls == [("initialize", 60.0), ("turn/start", 10.0)]

    FakeClient.calls.clear()
    monkeypatch.setenv("HERMES_CODEX_INITIALIZE_TIMEOUT", "90")
    with codex_thread_resume_scope(None, **scope_kwargs):
        client = agent._codex_session.client_factory()
        client.request("initialize", {}, timeout=10.0)
        client.request("initialize", {}, timeout=120.0)
    assert FakeClient.calls == [("initialize", 90.0), ("initialize", 120.0)]


def test_budget_notice_never_swallows_a_done_only_answer(tmp_path: Path, monkeypatch):
    """事故的第二形态: 子进程只发 done 不发 content 时,提示语曾把答案整段吃掉。

    stream_run_agent 只在「没有任何 content」时才把 done 文本兜出来给用户,
    所以一条盲目追加的 content 通知会让那个兜底失效。
    """
    repo = _git_repo(tmp_path / "source")
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )

    async def child(event, _profile_home, *, messages=None):
        yield "harness_thread_bound", {"thread_id": "thread-1"}
        yield "done", "THE ANSWER"
        _mark_budget_exhausted(event)

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", child)
    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", _accept_provider_audit)
    monkeypatch.setattr(
        executor_map,
        "runtime_for_event",
        lambda *_args, **_kwargs: executor_map.CODEX_APP_SERVER,
    )
    # 本用例只考「通知位置 vs done 兜底」,不考专家解析。
    monkeypatch.setattr(
        _core, "_resolve_explicit_expert_for_execution", lambda *_args: None
    )

    async def run():
        return [
            item
            async for item in _core.stream_run_agent(_event(profile, admission), profile)
        ]

    text = "".join(
        str(payload) for kind, payload in asyncio.run(run()) if kind == "content"
    )
    assert "THE ANSWER" in text
    assert "本轮已用满 64 次模型调用" in text


def test_budget_notice_is_withheld_when_the_audit_is_not_a_clean_budget_stop(
    tmp_path: Path, monkeypatch
):
    """并发拒绝的一轮不能拿到「预算已用尽」这句安抚文案(门与提示语共用一个判据)。"""
    repo = _git_repo(tmp_path / "source")
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )

    async def child(event, _profile_home, *, messages=None):
        yield "harness_thread_bound", {"thread_id": "thread-1"}
        yield "content", "partial answer"
        _mark_budget_exhausted(event)
        event._trusted_codex_proxy_audit["rejected_concurrent"] = 1

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", child)
    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", _accept_provider_audit)

    async def run():
        return [
            item
            async for item in _core._verified_codex_stream(
                _event(profile, admission), profile
            )
        ]

    assert asyncio.run(run()) == [("content", "partial answer")]


def _budget_stop_event(**overrides):
    """一个只带审计字段的事件；overrides 用来把干净预算停弄脏。"""
    event = SimpleNamespace()
    _mark_budget_exhausted(event)
    event._trusted_codex_proxy_audit.update(overrides)
    return event


def test_a_clean_budget_stop_is_recognised_on_the_request_boundary():
    """token 总量上限已关闭(2026-09-04),干净的预算停只有请求次数一个边界。"""
    assert codex_provider_proxy.MAX_HARNESS_TOTAL_TOKENS is None
    assert _core._codex_budget_exhausted(_budget_stop_event()) is True


@pytest.mark.parametrize(
    "dirty",
    [
        pytest.param({"rejected_over_limit": 0}, id="no-budget-rejection"),
        pytest.param({"rejected_over_limit": None}, id="counter-missing"),
        pytest.param({"rejected_over_limit": True}, id="counter-not-an-int"),
        pytest.param({"rejected_concurrent": 1}, id="concurrent-rejection"),
        pytest.param({"rejected_requests": 2}, id="totals-mismatch"),
        pytest.param({"rejected_requests": None}, id="total-missing"),
        pytest.param({"request_count": 63, "total_tokens": 10}, id="no-boundary-reached"),
        pytest.param(
            {"request_count": 63, "total_tokens": 3_000_000},
            id="token-total-is-no-longer-a-boundary",
        ),
        pytest.param({"request_limit": 8}, id="audit-limit-mismatch"),
        pytest.param(
            {
                "rejected_over_limit": None,
                "rejected_concurrent": None,
                "rejected_requests": None,
            },
            id="forged-flag-without-counters",
        ),
    ],
)
def test_a_dirty_audit_is_never_a_clean_budget_stop(dirty):
    """放行只认计数器；budget_exhausted=True 是派生标记,伪造它不算数。"""
    event = _budget_stop_event(**dirty)
    assert event._trusted_codex_proxy_audit["budget_exhausted"] is True
    assert _core._codex_budget_exhausted(event) is False


def _budget_409() -> RuntimeError:
    return RuntimeError(
        "AIAgent subprocess failed: unexpected status 409 Conflict "
        '{"error":{"type":"request_limit_exceeded"}}'
    )


def _harness_fixture(tmp_path: Path):
    repo = _git_repo(tmp_path / "source")
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )
    return profile, admission


def test_a_textless_budget_stop_reaches_the_user_instead_of_unavailable(
    tmp_path: Path, monkeypatch
):
    """事故的第三形态: 一个字都没吐就撞预算 → 子进程 done.error → 整轮 unavailable。

    409 在子进程侧变成终局异常,原先直接跳过收尾门与提示语。现在异常先挂起,
    等代理 teardown 公布审计,只有严格校验过的干净预算停才吞掉它。
    """
    profile, admission = _harness_fixture(tmp_path)

    async def child(event, _profile_home, *, messages=None):
        yield "harness_thread_bound", {"thread_id": "thread-1"}
        yield "tool_started", {"name": "shell"}
        # 真实顺序: env scope 的 finally 先落审计,异常才浮上来。
        _mark_budget_exhausted(event)
        raise _budget_409()

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", child)
    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", _accept_provider_audit)

    async def run():
        return [
            item
            async for item in _core._verified_codex_stream(
                _event(profile, admission), profile
            )
        ]

    items = asyncio.run(run())
    assert ("tool_started", {"name": "shell"}) in items
    kind, notice = items[-1]
    assert kind == "content"
    assert notice.startswith("本轮已用满 64 次模型调用")


def test_a_terminal_error_on_a_dirty_audit_is_still_raised(tmp_path: Path, monkeypatch):
    """控制组: 同一个 409,审计不干净(并发拒绝)→ 异常照旧上抛,fail-closed。"""
    profile, admission = _harness_fixture(tmp_path)

    async def child(event, _profile_home, *, messages=None):
        yield "harness_thread_bound", {"thread_id": "thread-1"}
        _mark_budget_exhausted(event)
        event._trusted_codex_proxy_audit["rejected_concurrent"] = 1
        raise _budget_409()

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", child)
    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", _accept_provider_audit)

    async def run():
        return [
            item
            async for item in _core._verified_codex_stream(
                _event(profile, admission), profile
            )
        ]

    with pytest.raises(RuntimeError, match="409 Conflict"):
        asyncio.run(run())


def test_a_non_budget_terminal_error_is_still_raised(tmp_path: Path, monkeypatch):
    """控制组: 非预算的真故障永远不被吞。"""
    profile, admission = _harness_fixture(tmp_path)

    async def child(event, _profile_home, *, messages=None):
        yield "harness_thread_bound", {"thread_id": "thread-1"}
        raise RuntimeError("AIAgent subprocess failed: provider 400")

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", child)
    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", _accept_provider_audit)

    async def run():
        return [
            item
            async for item in _core._verified_codex_stream(
                _event(profile, admission), profile
            )
        ]

    with pytest.raises(RuntimeError, match="provider 400"):
        asyncio.run(run())


def test_a_tool_only_budget_stop_never_replays_an_unmetered_second_run(
    tmp_path: Path, monkeypatch
):
    """只有 tool 事件的预算停必须自己终结这一轮。

    什么都不加时 stream_run_agent 会落到 legacy `_stream_loop`,在花光的代理之外
    再跑一次模型(不计量,还可能重复写操作工具)。
    """
    profile, admission = _harness_fixture(tmp_path)

    async def child(event, _profile_home, *, messages=None):
        yield "harness_thread_bound", {"thread_id": "thread-1"}
        yield "tool_started", {"name": "shell"}
        yield "tool_completed", {"name": "shell"}
        _mark_budget_exhausted(event)

    def _never(*_args, **_kwargs):
        raise AssertionError("legacy _stream_loop replayed an unmetered second run")

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", child)
    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", _accept_provider_audit)
    monkeypatch.setattr(agent_real, "_stream_loop", _never)
    monkeypatch.setattr(
        executor_map,
        "runtime_for_event",
        lambda *_args, **_kwargs: executor_map.CODEX_APP_SERVER,
    )
    monkeypatch.setattr(
        _core, "_resolve_explicit_expert_for_execution", lambda *_args: None
    )

    async def run():
        return [
            item
            async for item in _core.stream_run_agent(_event(profile, admission), profile)
        ]

    text = "".join(
        str(payload) for kind, payload in asyncio.run(run()) if kind == "content"
    )
    assert text.startswith("本轮已用满 64 次模型调用")


def _harness_profile(tmp_path: Path) -> Path:
    profile = tmp_path / "profiles" / "alice"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "model:\n"
        "  default: custom:litellm-sre/auto\n"
        "  base_url: https://server.example/v1\n",
        encoding="utf-8",
    )
    return profile


def _materialize_round_one_home(profile: Path, admission, model: str) -> Path:
    """What round 1 leaves behind: this workflow's pinned CODEX_HOME."""
    return codex_home.materialize(
        run_workspace.workflow_root(profile, admission.workflow_id),
        base_url="https://proxy.example/v1",
        model=model,
        plugin_dir=None,
    )


def test_second_round_without_model_carries_the_thread_bound_model(
    tmp_path: Path, monkeypatch
):
    """Acceptance 1: round 2 with no metadata model reuses round 1's model."""
    repo = _git_repo(tmp_path / "source")
    profile = _harness_profile(tmp_path)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )
    resolved_models = []
    carried_metadata = []

    async def child(event, _profile_home, *, messages=None):
        model, _base_url = _core._codex_model_and_base_url(event, profile)
        resolved_models.append(model)
        carried_metadata.append(event.raw_event["metadata"].get("model"))
        # Round 1 materializes the workflow's CODEX_HOME; round 2 must find it.
        _materialize_round_one_home(profile, admission, model)
        yield "harness_thread_bound", {"thread_id": "thread_bound"}
        yield "done", "ok"

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", child)
    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", _accept_provider_audit)

    async def run_two_rounds():
        for round_index in range(2):
            event = _event(profile, admission)
            if round_index == 0:
                event.raw_event["metadata"]["model"] = "GPT-5-priority"
            items = [item async for item in _core._verified_codex_stream(event, profile)]
            assert ("done", "ok") in items

    asyncio.run(run_two_rounds())

    assert resolved_models == ["GPT-5-priority", "GPT-5-priority"]
    # Same provider/model spec as round 1, so the child bills the same string.
    assert carried_metadata == ["GPT-5-priority", "custom:litellm-sre/GPT-5-priority"]


def test_first_round_without_model_is_still_rejected(tmp_path: Path):
    """Acceptance 2: no bound thread — the original refusal, verbatim.

    A leftover CODEX_HOME from an earlier workflow must NOT be inherited: the
    carry-over is keyed on this event's own thread binding, nothing weaker.
    """
    repo = _git_repo(tmp_path / "source")
    profile = _harness_profile(tmp_path)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )
    _materialize_round_one_home(profile, admission, "GPT-5-priority")
    event = _event(profile, admission)
    event._harness_resume_thread_id = None

    with pytest.raises(executor_map.ExecutorUnavailable) as excinfo:
        _core._codex_model_and_base_url(event, profile)

    assert str(excinfo.value) == (
        "EXECUTOR_UNAVAILABLE: codex requires an explicit GPT model"
    )
    assert "model" not in event.raw_event["metadata"]


def test_second_round_model_beats_the_thread_bound_model(tmp_path: Path):
    """Acceptance 3: an explicit model on THIS turn still wins."""
    repo = _git_repo(tmp_path / "source")
    profile = _harness_profile(tmp_path)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )
    _materialize_round_one_home(profile, admission, "GPT-5-priority")
    event = _event(profile, admission)
    event._harness_resume_thread_id = "thread_bound"
    event.raw_event["metadata"]["model"] = "gpt-5.4"

    assert _core._codex_model_and_base_url(event, profile) == (
        "gpt-5.4",
        "https://server.example/v1",
    )
    assert event.raw_event["metadata"]["model"] == "gpt-5.4"


def test_bound_thread_without_a_readable_model_fails_closed(tmp_path: Path):
    """Acceptance 4: thread record gone/corrupt and no model this turn."""
    repo = _git_repo(tmp_path / "source")
    profile = _harness_profile(tmp_path)
    admission = issue_webui_harness_admission(
        profile_name="alice",
        actor_subject="ou_alice",
        session_id="session-1",
        engine="harness",
        environ=_harness_env(repo),
    )
    home = _materialize_round_one_home(profile, admission, "GPT-5-priority")
    (home / "config.toml").write_text("model = [unparseable\n", encoding="utf-8")
    event = _event(profile, admission)
    event._harness_resume_thread_id = "thread_bound"

    with pytest.raises(executor_map.ExecutorUnavailable) as excinfo:
        _core._codex_model_and_base_url(event, profile)

    assert str(excinfo.value) == (
        "EXECUTOR_UNAVAILABLE: codex requires an explicit GPT model: this harness "
        "thread has no bound model to carry over"
    )
    assert "model" not in event.raw_event["metadata"]
