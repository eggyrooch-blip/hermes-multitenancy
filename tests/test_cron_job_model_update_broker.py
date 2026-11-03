"""Broker HTTP round-trip for per-job model updates.

Split out of test_webui_broker_server.py so this slug's TEST gate does not
inherit that file's pre-existing session-search failures (red on main).
"""
from __future__ import annotations

from tests.test_webui_broker_server import _install_fake_cron  # noqa: F401
def test_webui_run_broker_job_model_patch_round_trips(monkeypatch, tmp_path):
    """PATCH model through the ACTUAL broker HTTP boundary the WebUI edit hits.

    cron_api unit tests alone would stay green if a broker-side payload filter
    later dropped or rewrote `model`.
    """
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(user_id="owner", profile_name="owner", open_id="ou_owner", provenance="sync")
    seeded.close()
    router_mod.override_routing_table(db_path)

    async def runner():
        _install_fake_cron(monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "broker-secret")
        app = create_run_broker_app(sandbox_available=lambda: True)
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {
            "Authorization": "Bearer broker-secret",
            "X-Hermes-Owner-Open-Id": "ou_owner",
            "X-Hermes-Profile": "owner",
        }
        try:
            created = await client.post(
                "/api/run-broker/jobs",
                json={"name": "cron", "schedule": "*/5 * * * *", "prompt": "ping"},
                headers=headers,
            )
            job_id = (await created.json())["job"]["id"]

            # Move an EXISTING job to a cheaper model.
            patched = await client.patch(
                f"/api/run-broker/jobs/{job_id}",
                json={"model": "cheap-model"},
                headers=headers,
            )
            assert patched.status == 200
            assert (await patched.json())["job"]["model"] == "cheap-model"

            # Executor identity stays immutable even on this path.
            forged = await client.patch(
                f"/api/run-broker/jobs/{job_id}",
                json={"model": "cheap-model", "agent_id": "agent-x", "expert_id": "e-x"},
                headers=headers,
            )
            forged_job = (await forged.json())["job"]
            assert not (forged_job.get("agent_id") or "")
            assert not (forged_job.get("expert_id") or "")

            # Clearing falls back to the profile default.
            cleared = await client.patch(
                f"/api/run-broker/jobs/{job_id}",
                json={"model": ""},
                headers=headers,
            )
            cleared_job = (await cleared.json())["job"]
            assert not (cleared_job.get("model") or "")
            assert not (cleared_job.get("provider") or "")
        finally:
            await client.close()

    try:
        asyncio.run(runner())
    finally:
        router_mod.override_routing_table(None)

