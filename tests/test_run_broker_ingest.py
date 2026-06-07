"""Synchronous /api/run-broker/ingest seam tests.

Covers the SPEC run-broker-ingest acceptance scenarios: server-bound identity
(caller cannot choose the profile), fail-closed Bearer auth, field validation,
the unconfigured-profile guard, and synchronous JSON return.
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _app(monkeypatch, *, ingest_key="testkey", ingest_profile="owner", seen=None):
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
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
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
