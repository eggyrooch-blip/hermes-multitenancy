"""Run broker jobs owner-scope regression tests."""
from __future__ import annotations

import asyncio
from pathlib import Path


def test_run_broker_jobs_are_owner_scoped(monkeypatch, tmp_path: Path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import cron_api
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
        user_id="other-owner",
        profile_name="other_sync_profile",
        open_id="ou_other",
        provenance="sync",
    )
    seeded.close()

    list_calls: list[str] = []
    create_calls: list[tuple[str, str]] = []

    def sentinel_list(profile_name, *, include_disabled=False):
        list_calls.append(profile_name)
        return []

    def sentinel_create(profile_name, user_key, body, *, agent_id=""):
        create_calls.append((profile_name, user_key))
        return {"id": "job-1"}

    monkeypatch.setattr(cron_api, "list_jobs", sentinel_list)
    monkeypatch.setattr(cron_api, "create_job", sentinel_create)

    job_body = {"name": "x", "schedule": "* * * * *", "prompt": "p"}

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                before = len(list_calls)
                missing_owner = await client.get(
                    "/api/run-broker/jobs",
                    headers={"X-Hermes-Profile": "other_sync_profile"},
                )
                missing_owner_body = await missing_owner.json()
                missing_owner_calls = list_calls[before:]

                before = len(list_calls)
                wrong_owner = await client.get(
                    "/api/run-broker/jobs",
                    headers={
                        "X-Hermes-Owner-Open-Id": "ou_owner",
                        "X-Hermes-Profile": "other_sync_profile",
                    },
                )
                wrong_owner_body = await wrong_owner.json()
                wrong_owner_calls = list_calls[before:]

                before = len(list_calls)
                owned_profile = await client.get(
                    "/api/run-broker/jobs",
                    headers={
                        "X-Hermes-User-Key": "ou_owner",
                        "X-Hermes-Profile": "owner_sync_profile",
                    },
                )
                owned_profile_body = await owned_profile.json()
                owned_profile_calls = list_calls[before:]

                before = len(list_calls)
                owner_root = await client.get(
                    "/api/run-broker/jobs",
                    headers={"X-Hermes-User-Key": "ou_owner"},
                )
                owner_root_body = await owner_root.json()
                owner_root_calls = list_calls[before:]

                before = len(create_calls)
                cross_owner_create = await client.post(
                    "/api/run-broker/jobs",
                    headers={
                        "X-Hermes-User-Key": "ou_owner",
                        "X-Hermes-Profile": "other_sync_profile",
                    },
                    json=job_body,
                )
                cross_owner_create_body = await cross_owner_create.json()
                cross_owner_create_calls = create_calls[before:]

                before = len(create_calls)
                owned_create = await client.post(
                    "/api/run-broker/jobs",
                    headers={
                        "X-Hermes-User-Key": "ou_owner",
                        "X-Hermes-Profile": "owner_sync_profile",
                    },
                    json=job_body,
                )
                owned_create_body = await owned_create.json()
                owned_create_calls = create_calls[before:]
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

        assert (missing_owner.status, missing_owner_calls) == (403, [])
        assert (wrong_owner.status, "not accessible" in wrong_owner_body["error"], wrong_owner_calls) == (
            403,
            True,
            [],
        )
        assert (owned_profile.status, owned_profile_body, owned_profile_calls) == (
            200,
            {"jobs": []},
            ["owner_sync_profile"],
        )
        assert (owner_root.status, owner_root_body, owner_root_calls) == (
            200,
            {"jobs": []},
            ["owner_sync_profile"],
        )
        assert (cross_owner_create.status, cross_owner_create_calls) == (403, [])
        assert (owned_create.status, owned_create_body, owned_create_calls) == (
            200,
            {"job": {"id": "job-1"}},
            [("owner_sync_profile", "ou_owner")],
        )

    asyncio.run(runner())
