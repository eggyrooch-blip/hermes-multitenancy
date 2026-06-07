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


def test_owner_mode_missing_agent_always_400_even_with_default(monkeypatch):
    # Review B1: owner mode has NO unvalidated fallback. A configured
    # HERMES_INGEST_PROFILE must NOT be reachable by omitting `agent`.
    app, seen = _app(monkeypatch, default_profile="fallback_profile")
    status, text = _req(app, "POST", ING, body={"content": "hi"}, headers=AUTH)
    assert status == 400
    assert json.loads(text)["error"] == "agent required"
    assert seen == []  # the unvalidated default profile is never run


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


def test_v1_unconfigured_503_precedence_over_body(monkeypatch):
    # Review B2: v1 + no profile must 503 right after auth, even for an
    # invalid/empty body (precedence unchanged from before this feature).
    app, seen = _app(monkeypatch, owner=None, default_profile=None)
    status, _ = _req(app, "POST", ING, body={}, headers=AUTH)  # no content
    assert status == 503
    assert seen == []


def test_agents_endpoint_fail_closed_when_no_key(monkeypatch):
    # Both ingest key and master key unset → /agents must 401, never open.
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.delenv("HERMES_INGEST_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_OWNER", OWNER)
    _install_fake_routing(monkeypatch)
    app = create_run_broker_app(mark_seen=lambda _r: True, sandbox_available=lambda: True)
    status, _ = _req(app, "GET", AGENTS, headers={"Authorization": "Bearer anything"})
    assert status == 401


def test_owner_filters_foreign_and_non_agent_rows(monkeypatch):
    # Prove ownership/kind filtering against a REALISTIC mixed routing result:
    # a foreign-owner agent, a non-agent (group) row, plus the owner's own.
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    mixed = [
        _agent("电商文案助手", "webui_mine_ok"),                       # owned, agent → keep
        _agent("别人的", "webui_foreign", owner="ou_someone_else"),     # foreign owner → drop
        types.SimpleNamespace(                                          # group kind → drop
            kind="group", owner_open_id=OWNER, display_label="某群",
            agent_id="g1", user_id="g1", profile_name="feishu_group_x",
        ),
    ]
    fake = types.SimpleNamespace(
        list_agents_for_owner=lambda oid: list(mixed) if oid == OWNER else []
    )
    monkeypatch.setattr(router_mod, "_get_routing_table", lambda: fake)
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "k")
    monkeypatch.setenv("HERMES_INGEST_OWNER", OWNER)
    monkeypatch.delenv("HERMES_INGEST_PROFILE", raising=False)

    seen = []

    async def dispatch(r):
        seen.append(r)
        return "echo"

    app = create_run_broker_app(dispatch_agent=dispatch, mark_seen=lambda _r: True, sandbox_available=lambda: True)

    # All requests share ONE loop (app binds to the first loop it sees).
    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r_ok = await client.post(ING, json={"content": "hi", "agent": "电商文案助手"}, headers=AUTH)
            r_for = await client.post(ING, json={"content": "hi", "agent": "别人的"}, headers=AUTH)
            r_grp = await client.post(ING, json={"content": "hi", "agent": "某群"}, headers=AUTH)
            r_list = await client.get(AGENTS, headers=AUTH)
            return (r_ok.status, await r_ok.text()), (r_for.status, await r_for.text()), \
                   (r_grp.status, await r_grp.text()), (r_list.status, await r_list.text())
        finally:
            await client.close()

    (s_ok, _t_ok), (s_for, _t_for), (s_grp, _t_grp), (s_list, t_list) = asyncio.run(runner())
    # only the owner's own agent resolves
    assert s_ok == 200 and seen[0].profile_name == "webui_mine_ok"
    # foreign-owner agent is NOT reachable
    assert s_for == 403
    # group row is NOT selectable as an agent
    assert s_grp == 403
    # GET /agents only lists the owner's own agent
    names = [a["name"] for a in json.loads(t_list)["agents"]]
    assert names == ["电商文案助手"]


def test_idempotency_namespaced_per_agent(monkeypatch):
    # Review B3: same explicit idempotency_key against two different agents must
    # NOT replay the first agent's cached result. Second call is forced to the
    # broker's duplicate path; with per-profile namespacing the cache lookup
    # MISSES (different agent) → duplicate_pending, never agent #1's answer.
    calls = {"n": 0}

    def mark_seen(_r):
        calls["n"] += 1
        return calls["n"] == 1  # 1st new, 2nd seen as duplicate

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "k")
    monkeypatch.setenv("HERMES_INGEST_OWNER", OWNER)
    monkeypatch.delenv("HERMES_INGEST_PROFILE", raising=False)
    _install_fake_routing(monkeypatch)

    async def dispatch(r):
        return f"echo:{r.profile_name}"

    app = create_run_broker_app(dispatch_agent=dispatch, mark_seen=mark_seen, sandbox_available=lambda: True)
    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r1 = await client.post(ING, json={"content": "x", "agent": "电商文案助手", "idempotency_key": "K"}, headers=AUTH)
            b1 = await r1.text()
            r2 = await client.post(ING, json={"content": "x", "agent": "数据分析助手", "idempotency_key": "K"}, headers=AUTH)
            b2 = await r2.text()
            return json.loads(b1), json.loads(b2)
        finally:
            await client.close()

    d1, d2 = asyncio.run(runner())
    assert d1["ok"] is True and d1["result"] == "echo:webui_aaa_ecom_bbb"
    # second agent, same key, duplicate path → did NOT replay agent #1's result
    assert d2.get("result") != "echo:webui_aaa_ecom_bbb"
    assert d2.get("status") == "duplicate_pending"
