"""Synchronous /api/run-broker/ingest seam tests.

Covers the SPEC run-broker-ingest acceptance scenarios: server-bound identity
(caller cannot choose the profile), fail-closed Bearer auth, field validation,
the unconfigured-profile guard, and synchronous JSON return.
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _app(
    monkeypatch,
    *,
    ingest_key="testkey",
    ingest_profile="owner",
    seen=None,
    sandbox=True,
    mark_seen=None,
):
    """Build a broker app wired with a recording dispatch + env knobs."""
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    # Deterministic auth surface: only the dedicated ingest key, no master key.
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    if ingest_key is None:
        monkeypatch.delenv("HERMES_INGEST_KEY", raising=False)
    else:
        monkeypatch.setenv("HERMES_INGEST_KEY", ingest_key)
    if ingest_profile is None:
        monkeypatch.delenv("HERMES_INGEST_PROFILE", raising=False)
    else:
        monkeypatch.setenv("HERMES_INGEST_PROFILE", ingest_profile)

    recorded = seen if seen is not None else []

    async def dispatch(request):
        recorded.append(request)
        return f"echo:{request.content}"

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen if mark_seen is not None else (lambda _request: True),
        sandbox_available=lambda: sandbox,
    )
    return app, recorded


def _post(app, body, *, headers=None):
    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/api/run-broker/ingest", json=body, headers=headers or {}
            )
            text = await response.text()
            return response.status, text
        finally:
            await client.close()

    return asyncio.run(runner())


def test_ingest_happy_path_returns_sync_json(monkeypatch):
    app, seen = _app(monkeypatch)
    status, text = _post(
        app,
        {"content": "summarize this"},
        headers={"Authorization": "Bearer testkey"},
    )
    assert status == 200
    data = json.loads(text)
    assert data["ok"] is True
    assert data["result"] == "echo:summarize this"
    assert data["profile"] == "owner"
    assert len(seen) == 1
    assert seen[0].profile_name == "owner"


def test_ingest_ignores_caller_supplied_profile(monkeypatch):
    """Identity is server-bound: a `profile` in the body must be ignored."""
    app, seen = _app(monkeypatch, ingest_profile="owner")
    status, text = _post(
        app,
        {"content": "hi", "profile": "someone-else"},
        headers={"Authorization": "Bearer testkey"},
    )
    assert status == 200
    data = json.loads(text)
    assert data["profile"] == "owner"
    assert seen[0].profile_name == "owner"  # NOT "someone-else"


def test_ingest_rejects_wrong_bearer_without_running_agent(monkeypatch):
    app, seen = _app(monkeypatch)
    status, text = _post(
        app,
        {"content": "hi"},
        headers={"Authorization": "Bearer WRONG"},
    )
    assert status == 401
    assert json.loads(text)["ok"] is False
    assert seen == []  # agent never dispatched


def test_ingest_rejects_missing_bearer(monkeypatch):
    app, seen = _app(monkeypatch)
    status, _ = _post(app, {"content": "hi"})
    assert status == 401
    assert seen == []


def test_ingest_missing_content_is_400(monkeypatch):
    app, seen = _app(monkeypatch)
    status, text = _post(
        app, {"skill": "foo"}, headers={"Authorization": "Bearer testkey"}
    )
    assert status == 400
    assert "content" in json.loads(text)["error"]
    assert seen == []


def test_ingest_unconfigured_profile_is_503(monkeypatch):
    app, seen = _app(monkeypatch, ingest_profile=None)
    status, text = _post(
        app, {"content": "hi"}, headers={"Authorization": "Bearer testkey"}
    )
    assert status == 503
    assert json.loads(text)["ok"] is False
    assert seen == []  # never runs the agent without a bound identity


def test_ingest_fail_closed_when_no_key_configured(monkeypatch):
    app, seen = _app(monkeypatch, ingest_key=None)
    status, _ = _post(
        app, {"content": "hi"}, headers={"Authorization": "Bearer anything"}
    )
    assert status == 401  # both keys unset → public route refuses
    assert seen == []


def test_ingest_skill_is_prepended_as_slash_command(monkeypatch):
    # Stub the broker's skill-slash rewriter to identity so the test is
    # hermetic (the real one imports core `agent.skill_commands`, absent in
    # the plugin's standalone test env). We only assert that OUR handler turns
    # the `skill` field into a leading slash command before dispatch.
    from hermes_multitenancy import run_broker as run_broker_mod

    monkeypatch.setattr(
        run_broker_mod, "rewrite_skill_slash_text", lambda text, **_kw: text
    )

    app, seen = _app(monkeypatch)
    status, _ = _post(
        app,
        {"content": "运动明细", "skill": "keep-query"},
        headers={"Authorization": "Bearer testkey"},
    )
    assert status == 200
    assert seen[0].content.startswith("/keep-query ")


# ── Gap A: host tools (parity with cron/kanban) ──────────────────────────

def test_ingest_defaults_host_tools_true(monkeypatch):
    app, seen = _app(monkeypatch)
    status, _ = _post(
        app, {"content": "hi"}, headers={"Authorization": "Bearer testkey"}
    )
    assert status == 200
    assert seen[0].requires_host_tools is True


def test_ingest_host_tools_default_requires_sandbox(monkeypatch):
    # Default requires_host_tools=True → broker refuses without a sandbox.
    app, seen = _app(monkeypatch, sandbox=False)
    status, text = _post(
        app, {"content": "hi"}, headers={"Authorization": "Bearer testkey"}
    )
    assert status == 403
    assert json.loads(text)["ok"] is False
    assert seen == []


def test_ingest_host_tools_opt_out_runs_without_sandbox(monkeypatch):
    app, seen = _app(monkeypatch, sandbox=False)
    status, _ = _post(
        app,
        {"content": "hi", "requires_host_tools": False},
        headers={"Authorization": "Bearer testkey"},
    )
    assert status == 200
    assert seen[0].requires_host_tools is False


# ── Gap C: model / metadata passthrough ──────────────────────────────────

def test_ingest_model_passthrough(monkeypatch):
    app, seen = _app(monkeypatch)
    status, _ = _post(
        app,
        {"content": "hi", "model": "gpt-5.4", "metadata": {"trace": "t1"}},
        headers={"Authorization": "Bearer testkey"},
    )
    assert status == 200
    assert seen[0].metadata["model"] == "gpt-5.4"
    assert seen[0].metadata["trace"] == "t1"
    assert seen[0].metadata["source"] == "ingest"


# ── Gap D: duplicate returns the original result ──────────────────────────

def test_ingest_duplicate_returns_cached_result(monkeypatch):
    calls = {"n": 0}

    def mark_seen(_request):
        calls["n"] += 1
        return calls["n"] == 1  # first new, second is a duplicate

    app, seen = _app(monkeypatch, mark_seen=mark_seen)
    body = {"content": "summarize", "idempotency_key": "k1"}

    # Both posts must share ONE event loop (the app binds to the first loop),
    # so issue them inside a single runner against one client.
    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r1 = await client.post(
                "/api/run-broker/ingest", json=body,
                headers={"Authorization": "Bearer testkey"},
            )
            b1 = await r1.text()
            r2 = await client.post(
                "/api/run-broker/ingest", json=body,
                headers={"Authorization": "Bearer testkey"},
            )
            b2 = await r2.text()
            return (r1.status, b1), (r2.status, b2)
        finally:
            await client.close()

    (s1, t1), (s2, t2) = asyncio.run(runner())

    assert s1 == 200 and json.loads(t1)["result"] == "echo:summarize"
    d2 = json.loads(t2)
    assert s2 == 200
    assert d2["ok"] is True
    assert d2["duplicate"] is True
    assert d2["result"] == "echo:summarize"  # original result, not empty
    assert len(seen) == 1  # agent ran only once


# ── Gap B: clarify is surfaced (default-dispatch path) ────────────────────

def test_ingest_surfaces_clarify_instead_of_empty(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        yield "clarify_required", {"question": "哪个时间段？"}

    async def fake_real_run_agent(event, profile_home, *, messages=None):
        return ""  # nothing to add — the run is waiting on clarification

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    app = create_run_broker_app(
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )
    # interactive=true → streaming path with the clarify bridge wired and
    # short-circuit on the event.
    status, text = _post(
        app,
        {"content": "查数据", "requires_host_tools": False, "interactive": True},
        headers={"Authorization": "Bearer testkey"},
    )
    data = json.loads(text)
    assert status == 200
    assert data["ok"] is False
    assert data["status"] == "needs_clarification"
    assert "clarify" in data  # the question payload is surfaced, not swallowed


def test_ingest_surfaces_approval_in_interactive_mode(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        yield "approval_required", {"command": "rm -rf x", "description": "danger"}

    async def fake_real_run_agent(event, profile_home, *, messages=None):
        return ""

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    app = create_run_broker_app(
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )
    status, text = _post(
        app,
        {"content": "做点危险操作", "requires_host_tools": False, "interactive": True},
        headers={"Authorization": "Bearer testkey"},
    )
    data = json.loads(text)
    assert status == 200
    assert data["ok"] is False
    assert data["status"] == "needs_approval"
    assert "approval" in data


# ── Auth: master-key acceptance (review test gap) ────────────────────────

def test_ingest_accepts_master_broker_key(monkeypatch):
    # No dedicated ingest key; only the master broker key configured.
    app, seen = _app(monkeypatch, ingest_key=None)
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "masterkey")
    status, _ = _post(
        app, {"content": "hi"}, headers={"Authorization": "Bearer masterkey"}
    )
    assert status == 200
    assert seen[0].profile_name == "owner"


# ── NB1: source provenance cannot be spoofed ─────────────────────────────

def test_ingest_caller_cannot_override_source_marker(monkeypatch):
    app, seen = _app(monkeypatch)
    status, _ = _post(
        app,
        {"content": "hi", "metadata": {"source": "evil", "keep": "v"}},
        headers={"Authorization": "Bearer testkey"},
    )
    assert status == 200
    assert seen[0].metadata["source"] == "ingest"  # forced, not "evil"
    assert seen[0].metadata["keep"] == "v"  # other caller metadata preserved


# ── NB4: hard per-run timeout bounds the held connection ─────────────────

def test_ingest_times_out_on_slow_run(monkeypatch):
    monkeypatch.setenv("HERMES_INGEST_TIMEOUT", "0.2")

    async def slow_dispatch(_request):
        await asyncio.sleep(5)
        return "too late"

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=slow_dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )
    status, text = _post(
        app, {"content": "hi"}, headers={"Authorization": "Bearer testkey"}
    )
    assert status == 504
    assert json.loads(text)["status"] == "timeout"


# ── NB2: stale cache entry is not returned ───────────────────────────────

def test_ingest_duplicate_with_stale_cache_does_not_return_stale(monkeypatch):
    import hermes_multitenancy.webui_broker_server as mod

    calls = {"n": 0}

    def mark_seen(_request):
        calls["n"] += 1
        return calls["n"] == 1

    # Fake clock: first run stores at t=0; the duplicate lookup sees t well
    # past the 3600s TTL, so the cached entry must be treated as expired.
    clock = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["t"])

    app, seen = _app(monkeypatch, mark_seen=mark_seen)
    body = {"content": "summarize", "idempotency_key": "k1"}

    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r1 = await client.post(
                "/api/run-broker/ingest", json=body,
                headers={"Authorization": "Bearer testkey"},
            )
            await r1.text()
            clock["t"] += 4000.0  # advance past TTL (3600s)
            r2 = await client.post(
                "/api/run-broker/ingest", json=body,
                headers={"Authorization": "Bearer testkey"},
            )
            return r2.status, await r2.text()
        finally:
            await client.close()

    s2, t2 = asyncio.run(runner())
    d2 = json.loads(t2)
    assert s2 == 200
    assert d2["ok"] is False
    assert d2["status"] == "duplicate_pending"  # stale entry not served
