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


def test_ingest_ignores_host_tools_opt_out_and_requires_sandbox(monkeypatch):
    app, seen = _app(monkeypatch, sandbox=False)
    status, text = _post(
        app,
        {"content": "hi", "requires_host_tools": False},
        headers={"Authorization": "Bearer testkey"},
    )
    assert status == 403
    assert json.loads(text)["ok"] is False
    assert seen == []


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
        # Include internal bridge plumbing that MUST be stripped before
        # reaching an internet-facing caller (review NB3).
        yield "approval_required", {
            "command": "rm -rf x",
            "description": "danger",
            "decision_path": "/tmp/secret/approval.json",
            "session_key": "sess-internal-123",
        }

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
    assert data["approval"]["command"] == "rm -rf x"
    # Internal plumbing must NOT leak to the caller.
    assert "decision_path" not in data["approval"]
    assert "session_key" not in data["approval"]


def test_ingest_interactive_clarify_is_replayed_on_duplicate(monkeypatch, tmp_path):
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
        return ""

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    calls = {"n": 0}

    def mark_seen(_request):
        calls["n"] += 1
        return calls["n"] == 1

    app = create_run_broker_app(
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )
    body = {
        "content": "查数据",
        "interactive": True,
        "requires_host_tools": False,
        "idempotency_key": "ck1",
    }

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
            return (await r1.text() and json.loads(b1)), (r2.status, await r2.text())
        finally:
            await client.close()

    d1, (s2, t2) = asyncio.run(runner())
    d2 = json.loads(t2)
    assert d1["status"] == "needs_clarification"
    # Duplicate replays the SAME structured request, not duplicate_pending.
    assert s2 == 200
    assert d2["status"] == "needs_clarification"
    assert d2["duplicate"] is True


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


# ── Async polling ingest ─────────────────────────────────────────────────

def test_ingest_async_ignores_host_tools_opt_out_and_requires_sandbox(monkeypatch):
    calls = {"n": 0}

    async def dispatch(request):
        calls["n"] += 1
        return f"async:{request.content}"

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: False,
    )

    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            submit = await client.post(
                "/api/run-broker/ingest/async",
                json={"content": "hi", "requires_host_tools": False},
                headers={"Authorization": "Bearer testkey"},
            )
            return submit.status, json.loads(await submit.text())
        finally:
            await client.close()

    status, body = asyncio.run(runner())

    assert status == 403
    assert body["ok"] is False
    assert "run_id" not in body
    assert calls["n"] == 0

def test_ingest_async_submit_returns_run_id_and_poll_eventually_succeeds(monkeypatch):
    release = asyncio.Event()
    calls = {"n": 0}

    async def slow_dispatch(request):
        calls["n"] += 1
        await release.wait()
        return f"async:{request.content}"

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=slow_dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            submit = await client.post(
                "/api/run-broker/ingest/async",
                json={"content": "summarize this"},
                headers={"Authorization": "Bearer testkey"},
            )
            submit_body = json.loads(await submit.text())
            run_id = submit_body["run_id"]

            first_poll = await client.get(
                f"/api/run-broker/ingest/runs/{run_id}",
                headers={"Authorization": "Bearer testkey"},
            )
            first_body = json.loads(await first_poll.text())

            release.set()
            final_body = None
            for _ in range(20):
                poll = await client.get(
                    f"/api/run-broker/ingest/runs/{run_id}",
                    headers={"Authorization": "Bearer testkey"},
                )
                final_body = json.loads(await poll.text())
                if final_body["status"] == "succeeded":
                    return submit.status, submit_body, first_poll.status, first_body, poll.status, final_body
                await asyncio.sleep(0.01)
            return submit.status, submit_body, first_poll.status, first_body, poll.status, final_body
        finally:
            await client.close()

    submit_status, submit_body, first_status, first_body, final_status, final_body = asyncio.run(runner())

    assert submit_status == 202
    assert submit_body["ok"] is True
    assert submit_body["status"] == "accepted"
    assert submit_body["profile"] == "owner"
    assert submit_body["run_id"].startswith("ing_")
    assert submit_body["poll_url"] == f"/api/run-broker/ingest/runs/{submit_body['run_id']}"
    assert submit_body["duplicate"] is False
    assert first_status == 200
    assert first_body["status"] in {"pending", "running"}
    assert final_status == 200
    assert final_body["ok"] is True
    assert final_body["status"] == "succeeded"
    assert final_body["result"] == "async:summarize this"
    assert calls["n"] == 1


def test_ingest_async_duplicate_idempotency_returns_same_run_without_dispatching_twice(monkeypatch):
    release = asyncio.Event()
    calls = {"n": 0}

    async def slow_dispatch(request):
        calls["n"] += 1
        await release.wait()
        return f"async:{request.content}"

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=slow_dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            body = {"content": "same work", "idempotency_key": "same-key"}
            first = await client.post(
                "/api/run-broker/ingest/async",
                json=body,
                headers={"Authorization": "Bearer testkey"},
            )
            first_body = json.loads(await first.text())
            await asyncio.sleep(0.01)
            second = await client.post(
                "/api/run-broker/ingest/async",
                json=body,
                headers={"Authorization": "Bearer testkey"},
            )
            second_body = json.loads(await second.text())
            release.set()
            return first.status, first_body, second.status, second_body
        finally:
            await client.close()

    first_status, first_body, second_status, second_body = asyncio.run(runner())

    assert first_status == 202
    assert second_status == 202
    assert second_body["duplicate"] is True
    assert second_body["run_id"] == first_body["run_id"]
    assert calls["n"] == 1


def test_ingest_async_poll_rejects_wrong_bearer(monkeypatch):
    async def dispatch(request):
        return f"async:{request.content}"

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            submit = await client.post(
                "/api/run-broker/ingest/async",
                json={"content": "hi"},
                headers={"Authorization": "Bearer testkey"},
            )
            submit_body = json.loads(await submit.text())
            poll = await client.get(
                f"/api/run-broker/ingest/runs/{submit_body['run_id']}",
                headers={"Authorization": "Bearer WRONG"},
            )
            return poll.status, await poll.text()
        finally:
            await client.close()

    status, text = asyncio.run(runner())
    assert status == 401
    assert json.loads(text)["ok"] is False


def test_ingest_async_timeout_is_pollable_status(monkeypatch):
    monkeypatch.setenv("HERMES_INGEST_ASYNC_TIMEOUT", "0.05")

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

    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            submit = await client.post(
                "/api/run-broker/ingest/async",
                json={"content": "slow"},
                headers={"Authorization": "Bearer testkey"},
            )
            submit_body = json.loads(await submit.text())
            run_id = submit_body["run_id"]
            for _ in range(20):
                poll = await client.get(
                    f"/api/run-broker/ingest/runs/{run_id}",
                    headers={"Authorization": "Bearer testkey"},
                )
                poll_body = json.loads(await poll.text())
                if poll_body["status"] == "timeout":
                    return submit.status, poll.status, poll_body
                await asyncio.sleep(0.02)
            return submit.status, poll.status, poll_body
        finally:
            await client.close()

    submit_status, poll_status, poll_body = asyncio.run(runner())
    assert submit_status == 202
    assert poll_status == 200
    assert poll_body["ok"] is False
    assert poll_body["status"] == "timeout"
    assert poll_body["profile"] == "owner"


def test_ingest_async_does_not_prune_active_run_before_it_finishes(monkeypatch):
    monkeypatch.setenv("HERMES_INGEST_ASYNC_TTL", "0.01")
    release = asyncio.Event()

    async def slow_dispatch(request):
        await release.wait()
        return f"async:{request.content}"

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=slow_dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            submit = await client.post(
                "/api/run-broker/ingest/async",
                json={"content": "slow"},
                headers={"Authorization": "Bearer testkey"},
            )
            submit_body = json.loads(await submit.text())
            await asyncio.sleep(0.03)
            poll = await client.get(
                f"/api/run-broker/ingest/runs/{submit_body['run_id']}",
                headers={"Authorization": "Bearer testkey"},
            )
            poll_body = json.loads(await poll.text())
            release.set()
            return poll.status, poll_body
        finally:
            await client.close()

    poll_status, poll_body = asyncio.run(runner())
    assert poll_status == 200
    assert poll_body["status"] in {"pending", "running"}


def test_ingest_async_rejects_new_submission_when_only_active_jobs_fill_cap(monkeypatch):
    monkeypatch.setenv("HERMES_INGEST_ASYNC_CAP", "1")
    release = asyncio.Event()
    calls = {"n": 0}

    async def slow_dispatch(request):
        calls["n"] += 1
        await release.wait()
        return f"async:{request.content}"

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=slow_dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            first = await client.post(
                "/api/run-broker/ingest/async",
                json={"content": "first"},
                headers={"Authorization": "Bearer testkey"},
            )
            first_body = json.loads(await first.text())
            await asyncio.sleep(0.01)
            second = await client.post(
                "/api/run-broker/ingest/async",
                json={"content": "second"},
                headers={"Authorization": "Bearer testkey"},
            )
            second_body = json.loads(await second.text())
            first_poll = await client.get(
                f"/api/run-broker/ingest/runs/{first_body['run_id']}",
                headers={"Authorization": "Bearer testkey"},
            )
            first_poll_body = json.loads(await first_poll.text())
            release.set()
            return second.status, second_body, first_poll.status, first_poll_body
        finally:
            await client.close()

    second_status, second_body, first_poll_status, first_poll_body = asyncio.run(runner())
    assert second_status == 503
    assert second_body["ok"] is False
    assert second_body["status"] == "capacity_reached"
    assert first_poll_status == 200
    assert first_poll_body["status"] in {"pending", "running"}
    assert calls["n"] == 1


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
