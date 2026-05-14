"""WebUI run broker HTTP seam tests."""
from __future__ import annotations

import asyncio
import sys
import types


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
