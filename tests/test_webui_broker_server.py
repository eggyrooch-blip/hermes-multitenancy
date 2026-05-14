"""WebUI run broker HTTP seam tests."""
from __future__ import annotations

import asyncio


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
