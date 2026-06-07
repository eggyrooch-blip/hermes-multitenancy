"""OWNER-mode ingest tests — caller specifies which of the owner's agents runs.

SPEC ingest-owner-agent-select: a key bound to HERMES_INGEST_OWNER lets the
caller pick one of THAT owner's agents via the `agent` field; the server
validates ownership (can't reach another owner's agent / an arbitrary profile).
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest

OWNER = "ou_owner_hanmeng"


def _agent(name, profile, owner=OWNER, agent_id=None):
    return types.SimpleNamespace(
        kind="agent",
        owner_open_id=owner,
        display_label=name,
        agent_id=agent_id or f"webui:{owner}:{name}",
        user_id=agent_id or f"webui:{owner}:{name}",
        profile_name=profile,
    )


# Owner ou_owner_hanmeng owns two agents; coder1 is someone else's (NOT returned
# by the owner-scoped query).
_OWNER_AGENTS = [
    _agent("电商文案助手", "webui_aaa_ecom_bbb"),
    _agent("数据分析助手", "webui_ccc_data_ddd"),
]


def _install_fake_routing(monkeypatch):
    from hermes_multitenancy import router as router_mod

    fake_table = types.SimpleNamespace(
        list_agents_for_owner=lambda open_id: list(_OWNER_AGENTS) if open_id == OWNER else []
    )
    monkeypatch.setattr(router_mod, "_get_routing_table", lambda: fake_table)


def _app(monkeypatch, *, owner=OWNER, default_profile=None, seen=None, ingest_key="k"):
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", ingest_key)
    if owner is None:
        monkeypatch.delenv("HERMES_INGEST_OWNER", raising=False)
    else:
        monkeypatch.setenv("HERMES_INGEST_OWNER", owner)
    if default_profile is None:
        monkeypatch.delenv("HERMES_INGEST_PROFILE", raising=False)
    else:
        monkeypatch.setenv("HERMES_INGEST_PROFILE", default_profile)
    _install_fake_routing(monkeypatch)

    recorded = seen if seen is not None else []

    async def dispatch(request):
        recorded.append(request)
        return f"echo:{request.content}"

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _r: True,
        sandbox_available=lambda: True,
    )
    return app, recorded


def _req(app, method, path, *, body=None, headers=None):
    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            if method == "GET":
                r = await client.get(path, headers=headers or {})
            else:
                r = await client.post(path, json=body, headers=headers or {})
            return r.status, await r.text()
        finally:
            await client.close()

    return asyncio.run(runner())


AUTH = {"Authorization": "Bearer k"}
ING = "/api/run-broker/ingest"
AGENTS = "/api/run-broker/ingest/agents"


def test_owner_mode_lists_agents(monkeypatch):
    app, _ = _app(monkeypatch)
    status, text = _req(app, "GET", AGENTS, headers=AUTH)
    assert status == 200
    d = json.loads(text)
    assert d["ok"] is True and d["owner"] == OWNER
    names = [a["name"] for a in d["agents"]]
    assert names == ["电商文案助手", "数据分析助手"]


def test_owner_mode_runs_specified_agent(monkeypatch):
    app, seen = _app(monkeypatch)
    status, text = _req(app, "POST", ING, body={"content": "hi", "agent": "电商文案助手"}, headers=AUTH)
    assert status == 200
    d = json.loads(text)
    assert d["ok"] is True
    # Ran as the AGENT's profile, not the owner's personal profile.
    assert d["profile"] == "webui_aaa_ecom_bbb"
    assert seen[0].profile_name == "webui_aaa_ecom_bbb"


def test_owner_mode_accepts_agent_by_id(monkeypatch):
    app, seen = _app(monkeypatch)
    status, _ = _req(app, "POST", ING, body={"content": "hi", "agent": f"webui:{OWNER}:数据分析助手"}, headers=AUTH)
    assert status == 200
    assert seen[0].profile_name == "webui_ccc_data_ddd"


def test_owner_mode_rejects_unowned_agent(monkeypatch):
    app, seen = _app(monkeypatch)
    status, text = _req(app, "POST", ING, body={"content": "hi", "agent": "coder1"}, headers=AUTH)
    assert status == 403
    d = json.loads(text)
    assert d["ok"] is False
    assert "电商文案助手" in d["agents"]  # hints what's valid
    assert seen == []  # never dispatched


def test_owner_mode_missing_agent_no_default_is_400(monkeypatch):
    app, seen = _app(monkeypatch, default_profile=None)
    status, text = _req(app, "POST", ING, body={"content": "hi"}, headers=AUTH)
    assert status == 400
    d = json.loads(text)
    assert d["error"] == "agent required"
    assert "数据分析助手" in d["agents"]
    assert seen == []


def test_owner_mode_missing_agent_uses_default_profile(monkeypatch):
    app, seen = _app(monkeypatch, default_profile="fallback_profile")
    status, _ = _req(app, "POST", ING, body={"content": "hi"}, headers=AUTH)
    assert status == 200
    assert seen[0].profile_name == "fallback_profile"


def test_agents_endpoint_requires_auth(monkeypatch):
    app, _ = _app(monkeypatch)
    status, _ = _req(app, "GET", AGENTS, headers={"Authorization": "Bearer WRONG"})
    assert status == 401


def test_agents_endpoint_503_without_owner_mode(monkeypatch):
    app, _ = _app(monkeypatch, owner=None)
    status, text = _req(app, "GET", AGENTS, headers=AUTH)
    assert status == 503
    assert json.loads(text)["ok"] is False


def test_v1_mode_ignores_agent_field(monkeypatch):
    # No owner configured → v1 fixed-profile; agent field must be ignored.
    app, seen = _app(monkeypatch, owner=None, default_profile="fixed_profile")
    status, _ = _req(app, "POST", ING, body={"content": "hi", "agent": "电商文案助手"}, headers=AUTH)
    assert status == 200
    assert seen[0].profile_name == "fixed_profile"  # agent ignored in v1 mode
