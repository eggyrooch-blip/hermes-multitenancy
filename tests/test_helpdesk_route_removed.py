"""The orphaned Feishu ticket-event chain stays removed.

The producer (the ws-adapter patch) was unwired from ``register()`` on
2026-08-09; the consumer route, its RAG index and client were deleted on
2026-10-07. These tests keep the route and modules from coming back.
"""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REMOVED_ROUTE = "/api/run-broker/feishu/" + "help" + "desk/events"
REMOVED_MODULES = (
    "hermes_multitenancy.feishu_" + "help" + "desk_client",
    "hermes_multitenancy.feishu_" + "help" + "desk_event",
    "hermes_multitenancy.feishu_" + "help" + "desk_events",
    "hermes_multitenancy." + "help" + "desk_rag",
)


def test_package_under_test_is_this_checkout():
    import hermes_multitenancy

    assert Path(hermes_multitenancy.__file__).resolve().is_relative_to(REPO_ROOT)


def test_removed_ticket_event_route_returns_404():
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        async def dispatch(_request):
            return "unused"

        app = create_run_broker_app(
            dispatch_agent=dispatch,
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(REMOVED_ROUTE, json={"event": {}})
            status = response.status
        finally:
            await client.close()
        assert status == 404

    asyncio.run(runner())


@pytest.mark.parametrize("module_name", REMOVED_MODULES)
def test_removed_modules_are_not_importable(module_name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)
