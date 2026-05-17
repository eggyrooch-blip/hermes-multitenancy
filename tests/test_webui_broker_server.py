"""WebUI run broker HTTP seam tests."""
from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path


def test_webui_run_broker_endpoint_streams_channel_neutral_events():
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        seen = []

        async def dispatch(request):
            seen.append(request)
            return f"echo:{request.content}"

        app = create_run_broker_app(
            dispatch_agent=dispatch,
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "sunke",
                "user_key": "ou_webui",
                "content": "hello broker",
                "session_id": "session-webui",
                "delivery_mode": "socket",
                "credential_subject": "ou_webui",
                "requires_host_tools": True,
                "metadata": {"model": "gpt-5.4"},
            })
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert '"kind": "content"' in body
        assert '"text": "echo:hello broker"' in body
        assert '"kind": "done"' in body
        assert len(seen) == 1
        assert seen[0].channel == "webui"
        assert seen[0].profile_name == "sunke"
        assert seen[0].user_key == "ou_webui"
        assert seen[0].requires_host_tools is True

    asyncio.run(runner())


def test_webui_run_broker_default_dispatch_streams_tool_events(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        assert profile_home == tmp_path / "profiles" / "sunke"
        assert event.raw_event["session_id"] == "session-webui"
        yield "tool_started", {"name": "lark_cli", "preview": "contact --help"}
        yield "tool_completed", {"name": "lark_cli", "duration": 0.12, "is_error": False}
        yield "content", "tool-backed answer"

    async def fake_real_run_agent(event, profile_home, *, messages=None):  # pragma: no cover
        raise AssertionError("real_run_agent fallback should not run when stream yielded content")

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    async def runner():
        app = create_run_broker_app(
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "sunke",
                "user_key": "ou_webui",
                "content": "hello broker",
                "session_id": "session-webui",
                "requires_host_tools": True,
            })
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert '"kind": "tool_started"' in body
        assert '"name": "lark_cli"' in body
        assert '"kind": "tool_completed"' in body
        assert '"kind": "content"' in body
        assert '"text": "tool-backed answer"' in body
        assert '"kind": "done"' in body

    asyncio.run(runner())


def test_webui_run_broker_flushes_events_before_agent_finishes(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    release = asyncio.Event()

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        assert profile_home == tmp_path / "profiles" / "sunke"
        yield "thinking", "正在连接模型和工具运行环境..."
        await release.wait()
        yield "content", "done"

    async def fake_real_run_agent(event, profile_home, *, messages=None):  # pragma: no cover
        raise AssertionError("real_run_agent fallback should not run when stream yielded content")

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    async def runner():
        app = create_run_broker_app(
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        response = None
        try:
            post_task = asyncio.create_task(client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "sunke",
                "user_key": "ou_webui",
                "content": "hello broker",
                "session_id": "session-webui",
                "requires_host_tools": True,
            }))
            response = await asyncio.wait_for(post_task, timeout=0.2)
            first_line = await asyncio.wait_for(response.content.readline(), timeout=0.2)
            assert b'"kind": "thinking"' in first_line
            assert "正在连接模型".encode("utf-8") in first_line
            release.set()
            body = await response.text()
        finally:
            release.set()
            if response is not None:
                response.close()
            await client.close()

        assert response.status == 200
        assert '"kind": "content"' in body
        assert '"text": "done"' in body

    asyncio.run(runner())


def test_webui_run_broker_default_dispatch_passes_messages_to_aiagent(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    expected_messages = [
        {"role": "user", "content": "查看下明天的天气"},
        {"role": "assistant", "content": "你在哪个城市？"},
        {"role": "user", "content": "北京"},
    ]

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        assert messages == expected_messages
        yield "content", "北京明天的天气..."

    async def fake_real_run_agent(event, profile_home, *, messages=None):  # pragma: no cover
        raise AssertionError("real_run_agent fallback should not run when stream yielded content")

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    async def runner():
        app = create_run_broker_app(
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "sunke",
                "user_key": "ou_webui",
                "content": "北京",
                "session_id": "session-webui",
                "requires_host_tools": True,
                "messages": expected_messages,
            })
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert '"kind": "content"' in body
        assert '"text": "北京明天的天气..."' in body
        assert '"kind": "done"' in body

    asyncio.run(runner())


def test_webui_run_broker_default_dispatch_streams_thinking_separately(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        yield "thinking", "The user wants me to call lark_cli first."
        yield "tool_started", {"name": "lark_cli", "preview": "calendar --help"}
        yield "tool_completed", {"name": "lark_cli", "duration": 0.12, "is_error": False}
        yield "content", "日历已创建成功。"

    async def fake_real_run_agent(event, profile_home, *, messages=None):  # pragma: no cover
        raise AssertionError("real_run_agent fallback should not run when stream yielded content")

    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    async def runner():
        app = create_run_broker_app(
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "sunke",
                "user_key": "ou_webui",
                "content": "帮我创建一个日历",
                "session_id": "session-webui",
                "requires_host_tools": True,
            })
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert '"kind": "thinking"' in body
        assert "The user wants me to call lark_cli first." in body
        assert body.count('"kind": "content"') == 1
        assert '"kind": "done"' in body
        assert '"kind": "done", "text": ""' in body

    asyncio.run(runner())


def test_webui_run_broker_endpoint_rejects_missing_bearer_key(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "broker-secret")
        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            missing = await client.post("/api/run-broker/runs", json={
                "channel": "webui",
                "profile_name": "sunke",
                "user_key": "ou_webui",
                "content": "hello broker",
            })
            authorized = await client.post("/api/run-broker/runs", headers={
                "Authorization": "Bearer broker-secret",
            }, json={
                "channel": "webui",
                "profile_name": "sunke",
                "user_key": "ou_webui",
                "content": "hello broker",
            })
        finally:
            await client.close()

        assert missing.status == 401
        assert authorized.status == 200

    asyncio.run(runner())


def test_webui_run_broker_event_preserves_webui_session_boundary():
    from hermes_multitenancy.agent_real import _resolve_aiagent_session_id
    from hermes_multitenancy.webui_broker_server import _build_webui_event
    from hermes_multitenancy.run_models import RunRequest

    request = RunRequest(
        channel="webui",
        profile_name="feishu_group_dfe8bc83167b_e18e",
        user_key="ou_webui",
        content="hello",
        session_id="matrix_t1_group_webui_doc_123",
    )

    event = _build_webui_event(request)
    session_id = _resolve_aiagent_session_id(
        event,
        profile_home=types.SimpleNamespace(name=request.profile_name),
        sender_open_id=request.user_key,
    )

    assert event.source.platform.value == "webui"
    assert event.raw_event["session_id"] == "matrix_t1_group_webui_doc_123"
    assert "platform:webui" in session_id
    assert "session:matrix_t1_group_webui_doc_123" in session_id
    assert "platform:feishu" not in session_id


def test_run_broker_loads_only_shared_runtime_env(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.webui_broker_server import load_run_broker_shared_env

    shared = tmp_path / ".hermes"
    shared.mkdir()
    (shared / ".env").write_text(
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
    monkeypatch.setenv("HERMES_HOME", str(shared))
    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_LARK_CLI_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    loaded = load_run_broker_shared_env()

    assert loaded == {
        "HERMES_MULTITENANCY_CREDENTIAL_KEY": "vault-key",
        "HERMES_LARK_CLI_APP_ID": "cli_public",
    }
    assert os.environ["HERMES_MULTITENANCY_CREDENTIAL_KEY"] == "vault-key"
    assert os.environ["HERMES_LARK_CLI_APP_ID"] == "cli_public"
    assert "FEISHU_APP_SECRET" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_run_broker_shared_env_does_not_override_existing_secret(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.webui_broker_server import load_run_broker_shared_env

    shared = tmp_path / ".hermes"
    shared.mkdir()
    (shared / ".env").write_text(
        "HERMES_MULTITENANCY_CREDENTIAL_KEY=file-key\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(shared))
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "process-key")

    loaded = load_run_broker_shared_env()

    assert loaded == {}
    assert os.environ["HERMES_MULTITENANCY_CREDENTIAL_KEY"] == "process-key"


def _install_fake_cron(monkeypatch):
    store = {}
    cron_pkg = types.ModuleType("cron")
    jobs_mod = types.ModuleType("cron.jobs")
    scheduler_mod = types.ModuleType("cron.scheduler")

    jobs_mod.HERMES_DIR = None
    jobs_mod.CRON_DIR = None
    jobs_mod.JOBS_FILE = None
    jobs_mod.OUTPUT_DIR = None
    scheduler_mod._hermes_home = None
    scheduler_mod._LOCK_DIR = None
    scheduler_mod._LOCK_FILE = None

    def bucket():
        key = str(jobs_mod.JOBS_FILE)
        store.setdefault(key, [])
        return store[key]

    def create_job(**kwargs):
        job = {
            "id": "abc123abc123",
            "job_id": "abc123abc123",
            "name": kwargs["name"],
            "prompt": kwargs.get("prompt", ""),
            "schedule": kwargs.get("schedule"),
            "schedule_display": kwargs.get("schedule"),
            "repeat": {"times": kwargs.get("repeat"), "completed": 0},
            "deliver": kwargs.get("deliver", "local"),
            "enabled": True,
            "state": "scheduled",
            "owner_open_id": kwargs.get("owner_open_id"),
            "owner_profile": kwargs.get("owner_profile"),
        }
        bucket().append(job)
        return job

    def list_jobs(include_disabled=False):
        jobs = list(bucket())
        return jobs if include_disabled else [j for j in jobs if j.get("enabled", True)]

    def get_job(job_id):
        return next((j for j in bucket() if j["id"] == job_id), None)

    def update_job(job_id, updates):
        job = get_job(job_id)
        if not job:
            return None
        job.update(updates)
        return job

    def remove_job(job_id):
        jobs = bucket()
        before = len(jobs)
        jobs[:] = [j for j in jobs if j["id"] != job_id]
        return len(jobs) != before

    def pause_job(job_id):
        return update_job(job_id, {"enabled": False, "state": "paused"})

    def resume_job(job_id):
        return update_job(job_id, {"enabled": True, "state": "scheduled"})

    jobs_mod.create_job = create_job
    jobs_mod.list_jobs = list_jobs
    jobs_mod.get_job = get_job
    jobs_mod.update_job = update_job
    jobs_mod.remove_job = remove_job
    jobs_mod.pause_job = pause_job
    jobs_mod.resume_job = resume_job
    cron_pkg.jobs = jobs_mod
    cron_pkg.scheduler = scheduler_mod

    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", jobs_mod)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler_mod)
    return store


def test_webui_run_broker_jobs_manage_profile_local_cron(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        store = _install_fake_cron(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "broker-secret")

        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {
            "Authorization": "Bearer broker-secret",
            "X-Hermes-Profile": "sunke",
            "X-Hermes-User-Key": "ou_sunke",
        }
        try:
            created = await client.post("/api/run-broker/jobs", headers=headers, json={
                "name": "cron canary",
                "schedule": "*/5 * * * *",
                "prompt": "ping",
                "deliver": "feishu",
                "owner_open_id": "spoofed",
                "owner_profile": "other",
            })
            create_body = await created.json()
            listed = await client.get("/api/run-broker/jobs?include_disabled=true", headers=headers)
            list_body = await listed.json()
            paused = await client.post("/api/run-broker/jobs/abc123abc123/pause", headers=headers)
            pause_body = await paused.json()
            deleted = await client.delete("/api/run-broker/jobs/abc123abc123", headers=headers)
            delete_body = await deleted.json()
        finally:
            await client.close()

        expected_jobs_file = tmp_path / ".hermes" / "profiles" / "sunke" / "cron" / "jobs.json"
        assert created.status == 200
        assert create_body["job"]["owner_open_id"] == "ou_sunke"
        assert create_body["job"]["owner_profile"] == "sunke"
        assert str(expected_jobs_file) in store
        assert listed.status == 200
        assert list_body["jobs"][0]["id"] == "abc123abc123"
        assert paused.status == 200
        assert pause_body["job"]["state"] == "paused"
        assert deleted.status == 200
        assert delete_body == {"ok": True}
        assert store[str(expected_jobs_file)] == []

    asyncio.run(runner())


def test_webui_run_broker_jobs_default_to_feishu_delivery(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        _install_fake_cron(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "broker-secret")

        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {
            "Authorization": "Bearer broker-secret",
            "X-Hermes-Profile": "yaojunhua",
            "X-Hermes-User-Key": "ou_yaojunhua",
        }
        try:
            created = await client.post("/api/run-broker/jobs", headers=headers, json={
                "name": "cron canary",
                "schedule": "*/5 * * * *",
                "prompt": "ping",
            })
            create_body = await created.json()
        finally:
            await client.close()

        assert created.status == 200
        assert create_body["job"]["deliver"] == "feishu"
        assert create_body["job"]["owner_open_id"] == "ou_yaojunhua"
        assert create_body["job"]["owner_profile"] == "yaojunhua"

    asyncio.run(runner())


def test_webui_run_broker_jobs_reject_invalid_profile(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        _install_fake_cron(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path))
        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/api/run-broker/jobs", headers={
                "X-Hermes-Profile": "../shared",
                "X-Hermes-User-Key": "ou_sunke",
            })
            body = await response.json()
        finally:
            await client.close()

        assert response.status == 400
        assert body == {"error": "invalid profile_name"}

    asyncio.run(runner())
