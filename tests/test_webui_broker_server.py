"""WebUI run broker HTTP seam tests."""
from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path

import pytest


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
                "profile_name": "owner",
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
        assert seen[0].profile_name == "owner"
        assert seen[0].user_key == "ou_webui"
        assert seen[0].requires_host_tools is True

    asyncio.run(runner())


def test_webui_run_broker_default_dispatch_streams_tool_events(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        assert profile_home == tmp_path / "profiles" / "owner"
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
                "profile_name": "owner",
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
        assert profile_home == tmp_path / "profiles" / "owner"
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
                "profile_name": "owner",
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
                "profile_name": "owner",
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
                "profile_name": "owner",
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
                "profile_name": "owner",
                "user_key": "ou_webui",
                "content": "hello broker",
            })
            authorized = await client.post("/api/run-broker/runs", headers={
                "Authorization": "Bearer broker-secret",
            }, json={
                "channel": "webui",
                "profile_name": "owner",
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

    def create_job(
        *,
        prompt,
        schedule,
        name=None,
        repeat=None,
        deliver=None,
        skills=None,
        model=None,
        provider=None,
        base_url=None,
        workdir=None,
        profile=None,
    ):
        del model, provider, base_url, workdir, profile
        job = {
            "id": "abc123abc123",
            "job_id": "abc123abc123",
            "name": name,
            "prompt": prompt,
            "schedule": schedule,
            "schedule_display": schedule,
            "repeat": {"times": repeat, "completed": 0},
            "deliver": deliver or "local",
            "enabled": True,
            "state": "scheduled",
            "skills": skills or [],
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
            "X-Hermes-Profile": "owner",
            "X-Hermes-User-Key": "ou_owner",
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

        expected_jobs_file = tmp_path / ".hermes" / "profiles" / "owner" / "cron" / "jobs.json"
        assert created.status == 200
        assert create_body["job"]["owner_open_id"] == "ou_owner"
        assert create_body["job"]["owner_profile"] == "owner"
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


def test_webui_run_broker_jobs_expose_shadow_plan(monkeypatch, tmp_path):
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
            "X-Hermes-Profile": "owner",
            "X-Hermes-User-Key": "ou_owner",
        }
        try:
            created = await client.post("/api/run-broker/jobs", headers=headers, json={
                "name": "cron canary",
                "schedule": "*/5 * * * *",
                "prompt": "ping",
            })
            plan_response = await client.get("/api/run-broker/jobs/abc123abc123/plan?shadow=1&due=1", headers=headers)
            plan_body = await plan_response.json()
        finally:
            await client.close()

        assert created.status == 200
        assert plan_response.status == 200
        assert plan_body["plan"]["mode"] == "shadow"
        assert plan_body["plan"]["will_execute"] is False
        assert plan_body["plan"]["would_execute"] is True
        assert plan_body["plan"]["profile_name"] == "owner"
        assert plan_body["plan"]["user_key"] == "ou_owner"
        assert plan_body["plan"]["deliver_target"] == {
            "platform": "feishu",
            "chat_id": "ou_owner",
            "thread_id": None,
        }
        assert plan_body["plan"]["secret_free"] is True

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
                "X-Hermes-User-Key": "ou_owner",
            })
            body = await response.json()
        finally:
            await client.close()

        assert response.status == 400
        assert body == {"error": "invalid profile_name"}

    asyncio.run(runner())


def test_webui_run_broker_owner_header_agent_id_resolves_owned_profile(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    # An owned non-root agent has its own open_id (router auto-provision writes
    # user_id == open_id) and an owner_open_id pointing back at the owner — the
    # production-equivalent state the routing backfill / self-agent provisioning
    # produces. Sharing the owner's open_id here would make the backfill rewrite
    # both rows to provenance='sync', tripping the one-sync-root-per-open_id
    # unique index — an invalid topology, not a real ownership case.
    seeded.upsert(
        user_id="agent-owned",
        profile_name="owned_agent_profile",
        open_id="agent-owned",
        provenance="auto",
    )
    seeded._conn.execute(
        "UPDATE multitenancy_routing SET owner_open_id = 'ou_owner' "
        "WHERE user_id = 'agent-owned'"
    )
    seeded._conn.commit()
    seeded.close()

    async def runner():
        seen = []
        router_mod.override_routing_table(db_path)
        try:
            table = router_mod._get_routing_table()
            assert table is not None
            owned = table.lookup_agent("agent-owned")
            assert owned is not None
            assert owned.owner_open_id == "ou_owner"

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
                response = await client.post("/api/run-broker/runs", headers={
                    "X-Hermes-Owner-Open-Id": " ou_owner ",
                    "X-Hermes-Agent-Id": " agent-owned ",
                }, json={
                    "channel": "webui",
                    "profile_name": "spoofed_client_profile",
                    "user_key": "ou_webui",
                    "content": "hello owner gate",
                })
                body = await response.text()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 200
        assert '"kind": "content"' in body
        assert len(seen) == 1
        assert seen[0].profile_name == "owned_agent_profile"
        assert seen[0].user_key == "ou_owner"
        assert seen[0].credential_subject == "ou_owner"

    asyncio.run(runner())


def test_webui_run_broker_owner_header_rejects_cross_owner_agent_id(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.upsert(
        user_id="other-agent",
        profile_name="other_agent_profile",
        open_id="ou_other",
        provenance="auto",
    )
    seeded.close()

    async def runner():
        seen = []
        router_mod.override_routing_table(db_path)
        try:
            table = router_mod._get_routing_table()
            assert table is not None
            assert table.lookup_agent("other-agent") is not None

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
                response = await client.post("/api/run-broker/runs", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                }, json={
                    "channel": "webui",
                    "profile_name": "spoofed_client_profile",
                    "agent_id": "other-agent",
                    "user_key": "ou_webui",
                    "content": "hello owner gate",
                })
                body = await response.text()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 403
        assert "does not belong" in body
        assert seen == []

    asyncio.run(runner())


def test_webui_skillhub_install_uses_owner_scoped_profile(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = tmp_path / ".hermes"
    skill_source = shared / "skills" / "hub" / "weather"
    profile = shared / "profiles" / "owner_sync_profile"
    skill_source.mkdir(parents=True)
    profile.mkdir(parents=True)
    (skill_source / "SKILL.md").write_text("# Weather\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    db_path = shared / "multitenancy.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.close()

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                dispatch_agent=lambda request: f"echo:{request.content}",
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                response = await client.post(
                    "/api/run-broker/skills/install",
                    headers={"X-Hermes-Owner-Open-Id": "ou_owner"},
                    json={
                        "profile_name": "spoofed_profile",
                        "skill_path": "hub/weather",
                        "version": "v1",
                    },
                )
                body = await response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 200
        assert body["profile_name"] == "owner_sync_profile"
        assert body["install"]["installed"] is True
        assert body["install"]["skill_path"] == "hub/weather"
        assert (profile / "skills" / "hub" / "weather").is_symlink()
        assert not (shared / "profiles" / "spoofed_profile").exists()

    asyncio.run(runner())


def test_webui_skillhub_install_without_owner_header_rejects_spoofed_profile(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = tmp_path / ".hermes"
    skill_source = shared / "skills" / "hub" / "weather"
    skill_source.mkdir(parents=True)
    (skill_source / "SKILL.md").write_text("# Weather\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", raising=False)

    async def runner():
        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/api/run-broker/skills/install",
                json={
                    "profile_name": "victim",
                    "skill_path": "hub/weather",
                    "version": "v1",
                },
            )
            body = await response.json()
        finally:
            await client.close()

        assert response.status == 403
        assert body == {"error": "owner identity required (X-Hermes-Owner-Open-Id)"}
        assert not (shared / "profiles" / "victim").exists()

    asyncio.run(runner())


def test_webui_skill_audit_collects_all_profiles(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = tmp_path / ".hermes"
    alice_skill = shared / "profiles" / "alice" / "skills" / "managed" / "weather"
    group_skill = shared / "profiles" / "feishu_group_sales" / "skills" / "shared" / "readonly"
    alice_skill.mkdir(parents=True)
    group_skill.mkdir(parents=True)
    (alice_skill / "SKILL.md").write_text("# Weather\n", encoding="utf-8")
    (group_skill / "SKILL.md").write_text("# Readonly\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))

    async def runner():
        app = create_run_broker_app(
            dispatch_agent=lambda request: f"echo:{request.content}",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/api/run-broker/skills/audit")
            body = await response.json()
        finally:
            await client.close()

        assert response.status == 200
        assert sorted(body["profiles"]) == ["alice", "feishu_group_sales"]
        assert body["profiles"]["alice"]["skills"][0]["skill_path"] == "managed/weather"
        assert body["profiles"]["feishu_group_sales"]["skills"][0]["skill_path"] == "shared/readonly"

    asyncio.run(runner())


def test_webui_run_broker_owner_header_without_agent_id_resolves_sync_root(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.close()

    async def runner():
        seen = []
        router_mod.override_routing_table(db_path)
        try:
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
                response = await client.post("/api/run-broker/runs", headers={
                    "X-Hermes-Owner-Open-Id": " ou_owner ",
                }, json={
                    "channel": "webui",
                    "profile_name": "spoofed_client_profile",
                    "user_key": "ou_webui",
                    "content": "hello owner root",
                })
                body = await response.text()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 200
        assert '"kind": "content"' in body
        assert len(seen) == 1
        assert seen[0].profile_name == "owner_sync_profile"
        assert seen[0].user_key == "ou_owner"
        assert seen[0].credential_subject == "ou_owner"

    asyncio.run(runner())


def test_webui_run_broker_without_owner_header_keeps_legacy_profile_resolution():
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
                "profile_name": "legacy_client_profile",
                "agent_id": "ignored-without-owner-header",
                "user_key": "ou_webui",
                "content": "hello legacy path",
            })
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert '"kind": "content"' in body
        assert len(seen) == 1
        assert seen[0].profile_name == "legacy_client_profile"

    asyncio.run(runner())


def test_webui_run_broker_enforced_without_owner_header_returns_403(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")

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
                "profile_name": "legacy_client_profile",
                "user_key": "ou_webui",
                "content": "hello enforced path",
            })
            body = await response.json()
        finally:
            await client.close()

        assert response.status == 403
        assert body == {"error": "owner identity required (X-Hermes-Owner-Open-Id)"}
        assert seen == []

    asyncio.run(runner())


def test_webui_run_broker_enforced_with_whitespace_owner_header_returns_403(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")

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
            response = await client.post("/api/run-broker/runs", headers={
                "X-Hermes-Owner-Open-Id": "   ",
            }, json={
                "channel": "webui",
                "profile_name": "legacy_client_profile",
                "user_key": "ou_webui",
                "content": "hello enforced path",
            })
            body = await response.json()
        finally:
            await client.close()

        assert response.status == 403
        assert body == {"error": "owner identity required (X-Hermes-Owner-Open-Id)"}
        assert seen == []

    asyncio.run(runner())


def test_webui_run_broker_enforced_with_owner_header_resolves_owned_profile(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.upsert(
        user_id="agent-owned",
        profile_name="owned_agent_profile",
        open_id="agent-owned",
        provenance="auto",
    )
    seeded._conn.execute(
        "UPDATE multitenancy_routing SET owner_open_id = 'ou_owner' "
        "WHERE user_id = 'agent-owned'"
    )
    seeded._conn.commit()
    seeded.close()

    async def runner():
        seen = []
        router_mod.override_routing_table(db_path)
        try:
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
                response = await client.post("/api/run-broker/runs", headers={
                    "X-Hermes-Owner-Open-Id": " ou_owner ",
                    "X-Hermes-Agent-Id": " agent-owned ",
                }, json={
                    "channel": "webui",
                    "profile_name": "spoofed_client_profile",
                    "user_key": "ou_webui",
                    "content": "hello owner gate",
                })
                body = await response.text()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 200
        assert '"kind": "content"' in body
        assert len(seen) == 1
        assert seen[0].profile_name == "owned_agent_profile"

    asyncio.run(runner())


def test_webui_run_broker_enforcement_disabled_keeps_legacy_resolution():
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
                "profile_name": "legacy_client_profile",
                "agent_id": "ignored-without-owner-header",
                "user_key": "ou_webui",
                "content": "hello legacy path",
            })
            body = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert '"kind": "content"' in body
        assert len(seen) == 1
        assert seen[0].profile_name == "legacy_client_profile"

    asyncio.run(runner())


def test_webui_run_broker_enforced_cross_owner_error_is_not_masked(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(
        user_id="root-owner",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    seeded.upsert(
        user_id="other-agent",
        profile_name="other_agent_profile",
        open_id="ou_other",
        provenance="auto",
    )
    seeded.close()

    async def runner():
        seen = []
        router_mod.override_routing_table(db_path)
        try:
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
                response = await client.post("/api/run-broker/runs", headers={
                    "X-Hermes-Owner-Open-Id": "ou_owner",
                }, json={
                    "channel": "webui",
                    "profile_name": "spoofed_client_profile",
                    "agent_id": "other-agent",
                    "user_key": "ou_webui",
                    "content": "hello owner gate",
                })
                body = await response.json()
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert response.status == 403
        assert body == {"error": "agent_id 'other-agent' does not belong to asserted owner"}
        assert seen == []

    asyncio.run(runner())


def test_start_run_broker_server_refuses_without_key(monkeypatch):
    import hermes_multitenancy.webui_broker_server as broker_mod

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_PORT", "0")

    async def runner():
        await broker_mod.stop_run_broker_server()
        with pytest.raises(SystemExit) as excinfo:
            await broker_mod.start_run_broker_server()
        assert excinfo.value.code not in (None, 0)
        assert broker_mod._runner is None
        assert broker_mod._site is None

    asyncio.run(runner())


def test_start_run_broker_server_starts_with_key(monkeypatch):
    import hermes_multitenancy.webui_broker_server as broker_mod

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "broker-secret")
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_PORT", "0")

    async def runner():
        await broker_mod.stop_run_broker_server()
        try:
            await broker_mod.start_run_broker_server()
            assert broker_mod._runner is not None
            assert broker_mod._site is not None
        finally:
            await broker_mod.stop_run_broker_server()

    asyncio.run(runner())


def test_ensure_run_broker_server_started_is_noop_when_disabled(monkeypatch):
    import hermes_multitenancy.webui_broker_server as broker_mod

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)

    async def runner():
        await broker_mod.stop_run_broker_server()
        broker_mod.ensure_run_broker_server_started()
        await asyncio.sleep(0)
        assert broker_mod._server_task is None
        assert broker_mod._runner is None
        assert broker_mod._site is None

    asyncio.run(runner())
