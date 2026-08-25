"""Synchronous /api/run-broker/ingest seam tests.

Covers the SPEC run-broker-ingest acceptance scenarios: server-bound identity
(caller cannot choose the profile), fail-closed Bearer auth, field validation,
the unconfigured-profile guard, and synchronous JSON return.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

from tests._sync import SYNC_TIMEOUT


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


def test_ingest_secret_prompt_requires_direct_execution_and_no_secret_echo(monkeypatch):
    from hermes_multitenancy import webui_broker_server as broker_mod

    app, seen = _app(monkeypatch)
    status, text = _post(
        app,
        {
            "content": "查询对账差异",
            "secrets": {
                "cms_bearer": {
                    "type": "bearer_token",
                    "value": "eyJ.fake.full.token",
                }
            },
        },
        headers={"Authorization": "Bearer testkey"},
    )

    assert status == 200, text
    prompt = broker_mod._build_webui_event(seen[0]).text
    assert "cms_bearer" in prompt
    assert "delegate_task" in prompt
    assert "do not delegate" in prompt.lower()
    assert "do not print" in prompt.lower()
    assert "token preview" in prompt.lower()
    assert "authorization header" in prompt.lower()
    assert "execute" in prompt.lower()
    assert "eyJ.fake.full.token" not in prompt


def test_ingest_ignores_caller_supplied_secret_metadata(monkeypatch, tmp_path):
    from hermes_multitenancy import webui_broker_server as broker_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    captured: dict[str, object] = {}

    async def dispatch(request):
        event = broker_mod._build_webui_event(request)
        captured["metadata"] = dict(request.metadata)
        captured["event_text"] = event.text
        captured["raw_event_metadata"] = dict(event.raw_event["metadata"])
        return "ok"

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    status, text = _post(
        app,
        {
            "content": "hi",
            "metadata": {
                "trace": "t1",
                "ingest_secret_dir": str(tmp_path / "fake-secrets"),
                "ingest_secrets": [
                    {"name": "fake", "type": "opaque", "usage": "spoofed"}
                ],
            },
        },
        headers={"Authorization": "Bearer testkey"},
    )

    assert status == 200
    assert json.loads(text)["ok"] is True
    assert captured["metadata"]["trace"] == "t1"
    assert "ingest_secret_dir" not in captured["metadata"]
    assert "ingest_secrets" not in captured["metadata"]
    assert "ingest_secret_dir" not in captured["raw_event_metadata"]
    assert "ingest_secrets" not in captured["raw_event_metadata"]
    assert captured["event_text"] == "hi"


def test_ingest_secrets_are_files_only_and_response_is_redacted(monkeypatch, tmp_path):
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy import webui_broker_server as broker_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    secret_value = "eyJhbGciOiJIUzI1NiJ9.full.jwt.secret"
    profile_home = tmp_path / "profiles" / "owner"
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: profile_home if profile_name == "owner" else tmp_path / profile_name,
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    captured: dict[str, object] = {}

    async def dispatch(request):
        captured["request"] = request
        captured["metadata_json"] = json.dumps(request.metadata, ensure_ascii=False)
        event = broker_mod._build_webui_event(request)
        captured["event_text"] = event.text
        captured["raw_event_json"] = json.dumps(event.raw_event, ensure_ascii=False)
        from pathlib import Path

        secret_dir = Path(request.metadata["ingest_secret_dir"])
        captured["secret_dir"] = secret_dir
        return f"tool saw {(profile_home / 'tmp').exists()}:{(secret_dir / 'cms_bearer').read_text(encoding='utf-8')}"

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    status, text = _post(
        app,
        {
            "content": "查询 2026-06-01 到 2026-06-22 的对账数据",
            "secrets": {
                "cms_bearer": {
                    "type": "bearer_token",
                    "value": secret_value,
                }
            },
        },
        headers={"Authorization": "Bearer testkey"},
    )

    assert status == 200
    assert secret_value not in text
    body = json.loads(text)
    assert body["ok"] is True
    assert "[REDACTED:cms_bearer]" in body["result"]
    request = captured["request"]
    assert request.content == "查询 2026-06-01 到 2026-06-22 的对账数据"
    assert secret_value not in captured["metadata_json"]
    assert secret_value not in captured["raw_event_json"]
    assert "cms_bearer" in captured["event_text"]
    assert "bearer_token" in captured["event_text"]
    assert secret_value not in captured["event_text"]
    assert not captured["secret_dir"].exists()


def test_ingest_sync_failure_log_redacts_secret_prefix_preview(monkeypatch, caplog):
    secret_value = "sk-live-sync-1234567890abcdef"
    leaked_prefix = secret_value[:12]

    async def dispatch(_request):
        raise RuntimeError(f"sync failed after token prefix {leaked_prefix}")

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    with caplog.at_level(logging.ERROR, logger="hermes_multitenancy.webui_broker_server"):
        status, text = _post(
            app,
            {
                "content": "sync failure",
                "secrets": {
                    "cms_api_key": {
                        "type": "api_key",
                        "value": secret_value,
                    }
                },
            },
            headers={"Authorization": "Bearer testkey"},
        )

    assert status == 500
    assert json.loads(text)["error"] == "internal error"
    assert "[REDACTED:cms_api_key:prefix]" in caplog.text
    assert "Traceback" not in caplog.text
    assert leaked_prefix not in caplog.text
    assert secret_value not in caplog.text


def test_ingest_sync_secret_materialization_failure_allows_different_secret_retry(
    monkeypatch, tmp_path
):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import webui_broker_server as server_mod
    from hermes_multitenancy.webui_broker import periphery

    prepare_calls = 0
    marked: list[str] = []
    seen: set[str] = set()
    dispatched = 0

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        return request

    def mark_seen(request):
        key = request.effective_idempotency_key
        marked.append(key)
        if key in seen:
            return False
        seen.add(key)
        return True

    def is_seen(request):
        return request.effective_idempotency_key in seen

    async def dispatch(_request):
        nonlocal dispatched
        dispatched += 1
        return "ok"

    real_write = periphery.os.write
    fail_once = True

    def flaky_write(fd, data):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("simulated sync secret write failure")
        return real_write(fd, data)

    monkeypatch.setattr(periphery.os, "write", flaky_write)
    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "shared"))

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        is_seen=is_seen,
        sandbox_available=lambda: True,
    )
    secret_root = (
        tmp_path
        / "shared"
        / "profiles"
        / "owner"
        / "tmp"
        / "ingest-secrets"
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer testkey"}
        base = {"content": "hi", "idempotency_key": "sync-secret-write-retry"}
        try:
            failed = await client.post(
                "/api/run-broker/ingest",
                json={
                    **base,
                    "secrets": {"cms": {"type": "opaque", "value": "secret-a"}},
                },
                headers=headers,
            )
            failed_body = await failed.json()
            leftovers = list(secret_root.iterdir()) if secret_root.exists() else []

            retry = await client.post(
                "/api/run-broker/ingest",
                json={
                    **base,
                    "secrets": {"cms": {"type": "opaque", "value": "secret-b"}},
                },
                headers=headers,
            )
            retry_body = await retry.json()

            old_secret_retry = await client.post(
                "/api/run-broker/ingest",
                json={
                    **base,
                    "secrets": {"cms": {"type": "opaque", "value": "secret-a"}},
                },
                headers=headers,
            )
            return (
                failed.status,
                failed_body,
                leftovers,
                retry.status,
                retry_body,
                old_secret_retry.status,
                await old_secret_retry.json(),
            )
        finally:
            await client.close()

    (
        failed_status,
        failed_body,
        leftovers,
        retry_status,
        retry_body,
        old_retry_status,
        old_retry_body,
    ) = asyncio.run(runner())

    assert failed_status == 400
    assert failed_body["error"] == "invalid secrets"
    assert leftovers == []
    assert retry_status == 200
    assert retry_body["ok"] is True
    assert retry_body["duplicate"] is False
    assert old_retry_status == 409
    assert old_retry_body["error"] == "secret_mismatch"
    assert prepare_calls == 2
    assert len(marked) == 1
    assert dispatched == 1


def test_ingest_sync_concurrent_secret_mismatch_is_rejected_before_shared_billing(
    monkeypatch, tmp_path
):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import webui_broker_server as server_mod

    prepare_calls = 0
    materialize_calls = 0
    marked: list[str] = []
    seen: set[str] = set()
    dispatches = 0
    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        prepare_started.set()
        await release_prepare.wait()
        return request

    def mark_seen(request):
        key = request.effective_idempotency_key
        marked.append(key)
        if key in seen:
            return False
        seen.add(key)
        return True

    def is_seen(request):
        return request.effective_idempotency_key in seen

    async def dispatch(_request):
        nonlocal dispatches
        dispatches += 1
        return "ok"

    real_materialize = server_mod._ingest_materialize_secret_dir

    def capture_materialize(**kwargs):
        nonlocal materialize_calls
        materialize_calls += 1
        return real_materialize(**kwargs)

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(
        server_mod,
        "_ingest_materialize_secret_dir",
        capture_materialize,
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "shared"))

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        is_seen=is_seen,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer testkey"}
        base = {"content": "hi", "idempotency_key": "sync-concurrent-secret"}
        first_task = asyncio.create_task(
            client.post(
                "/api/run-broker/ingest",
                json={
                    **base,
                    "secrets": {"cms": {"type": "opaque", "value": "secret-a"}},
                },
                headers=headers,
            )
        )
        try:
            await asyncio.wait_for(prepare_started.wait(), timeout=SYNC_TIMEOUT)
            mismatch = await asyncio.wait_for(
                client.post(
                    "/api/run-broker/ingest",
                    json={
                        **base,
                        "secrets": {
                            "cms": {"type": "opaque", "value": "secret-b"}
                        },
                    },
                    headers=headers,
                ),
                timeout=0.5,
            )
            mismatch_body = await mismatch.json()
            release_prepare.set()
            first = await asyncio.wait_for(first_task, timeout=SYNC_TIMEOUT)
            first_body = await first.json()
            return first.status, first_body, mismatch.status, mismatch_body
        finally:
            release_prepare.set()
            if not first_task.done():
                first_task.cancel()
            await client.close()

    first_status, first_body, mismatch_status, mismatch_body = asyncio.run(runner())

    assert first_status == 200
    assert first_body["ok"] is True
    assert mismatch_status == 409, mismatch_body
    assert mismatch_body["error"] == "secret_mismatch"
    assert prepare_calls == 1
    assert materialize_calls == 1
    assert len(marked) == 1
    assert dispatches == 1


@pytest.mark.parametrize(
    "secrets_payload",
    [
        "not-object",
        {"../token": {"type": "bearer_token", "value": "x"}},
        {"cms": {"type": "unsupported", "value": "x"}},
        {"cms": {"type": "bearer_token", "value": ""}},
        {"cms": {"type": "bearer_token", "value": "x" * (16 * 1024 + 1)}},
        {f"k{i}": {"type": "opaque", "value": "x" * 2048} for i in range(33)},
    ],
)
def test_ingest_rejects_invalid_secrets_without_dispatch(monkeypatch, secrets_payload):
    app, seen = _app(monkeypatch)
    status, text = _post(
        app,
        {"content": "hi", "secrets": secrets_payload},
        headers={"Authorization": "Bearer testkey"},
    )
    assert status == 400
    assert json.loads(text)["ok"] is False
    assert seen == []


# ── Gap D: duplicate returns the original result ──────────────────────────

def test_ingest_duplicate_returns_cached_result(monkeypatch):
    calls = {"n": 0}

    def mark_seen(_request):
        calls["n"] += 1
        return calls["n"] == 1  # first new, second is a duplicate

    app, seen = _app(monkeypatch, mark_seen=mark_seen)
    body = {
        "content": "summarize",
        "idempotency_key": "k1",
        "secrets": {"cms": {"type": "opaque", "value": "same-secret"}},
    }

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


def test_ingest_same_idempotency_with_different_secret_fingerprint_is_409(monkeypatch):
    calls = {"n": 0}

    async def dispatch(request):
        calls["n"] += 1
        return f"echo:{request.content}"

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            base = {"content": "same work", "idempotency_key": "same-key"}
            first = await client.post(
                "/api/run-broker/ingest",
                json={**base, "secrets": {"cms": {"type": "opaque", "value": "old"}}},
                headers={"Authorization": "Bearer testkey"},
            )
            first_body = json.loads(await first.text())
            second = await client.post(
                "/api/run-broker/ingest",
                json={**base, "secrets": {"cms": {"type": "opaque", "value": "new"}}},
                headers={"Authorization": "Bearer testkey"},
            )
            second_body = json.loads(await second.text())
            return first.status, first_body, second.status, second_body
        finally:
            await client.close()

    first_status, first_body, second_status, second_body = asyncio.run(runner())

    assert first_status == 200
    assert first_body["ok"] is True
    assert second_status == 409
    assert second_body["error"] == "secret_mismatch"
    assert calls["n"] == 1


def test_ingest_same_idempotency_from_no_secret_to_secret_is_409(monkeypatch):
    app, seen = _app(monkeypatch)

    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer testkey"}
        base = {"content": "same work", "idempotency_key": "empty-secret-key"}
        try:
            first = await client.post(
                "/api/run-broker/ingest",
                json=base,
                headers=headers,
            )
            first_body = await first.json()
            second = await client.post(
                "/api/run-broker/ingest",
                json={
                    **base,
                    "secrets": {"cms": {"type": "opaque", "value": "new"}},
                },
                headers=headers,
            )
            second_body = await second.json()
            return first.status, first_body, second.status, second_body
        finally:
            await client.close()

    first_status, first_body, second_status, second_body = asyncio.run(runner())

    assert first_status == 200
    assert first_body["ok"] is True
    assert second_status == 409
    assert second_body["error"] == "secret_mismatch"
    assert len(seen) == 1


def test_ingest_sync_idempotency_is_scoped_by_ingest_caller(monkeypatch, tmp_path):
    keys_file = tmp_path / "ingest-keys.json"
    keys_file.write_text(
        json.dumps(
            {
                "keys": [
                    {"token": "caller-a", "profile": "owner", "name": "caller a"},
                    {"token": "caller-b", "profile": "owner", "name": "caller b"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.delenv("HERMES_INGEST_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE", str(keys_file))

    dispatched = {"n": 0}
    seen_keys: set[str] = set()

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen_keys:
            return False
        seen_keys.add(key)
        return True

    async def dispatch(request):
        dispatched["n"] += 1
        return f"run-{dispatched['n']}:{request.content}"

    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )
    body = {
        "content": "same work",
        "idempotency_key": "same-key",
        "secrets": {"cms": {"type": "opaque", "value": "same-secret"}},
    }

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            first = await client.post(
                "/api/run-broker/ingest",
                json=body,
                headers={"Authorization": "Bearer caller-a"},
            )
            first_body = json.loads(await first.text())
            second = await client.post(
                "/api/run-broker/ingest",
                json=body,
                headers={"Authorization": "Bearer caller-b"},
            )
            second_body = json.loads(await second.text())
            return first.status, first_body, second.status, second_body
        finally:
            await client.close()

    first_status, first_body, second_status, second_body = asyncio.run(runner())

    assert first_status == 200
    assert first_body["result"] == "run-1:same work"
    assert second_status == 200
    assert second_body["ok"] is True
    assert second_body["duplicate"] is False
    assert second_body["result"] == "run-2:same work"
    assert dispatched["n"] == 2


def test_ingest_sync_secret_mismatch_is_scoped_by_ingest_caller(monkeypatch, tmp_path):
    keys_file = tmp_path / "ingest-keys.json"
    keys_file.write_text(
        json.dumps(
            {
                "keys": [
                    {"token": "caller-a", "profile": "owner", "name": "caller a"},
                    {"token": "caller-b", "profile": "owner", "name": "caller b"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.delenv("HERMES_INGEST_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE", str(keys_file))

    dispatched = {"n": 0}

    async def dispatch(request):
        dispatched["n"] += 1
        return f"run-{dispatched['n']}:{request.content}"

    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )
    base = {"content": "same work", "idempotency_key": "same-key"}

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            first = await client.post(
                "/api/run-broker/ingest",
                json={**base, "secrets": {"cms": {"type": "opaque", "value": "old"}}},
                headers={"Authorization": "Bearer caller-a"},
            )
            first_body = json.loads(await first.text())
            second = await client.post(
                "/api/run-broker/ingest",
                json={**base, "secrets": {"cms": {"type": "opaque", "value": "new"}}},
                headers={"Authorization": "Bearer caller-b"},
            )
            second_body = json.loads(await second.text())
            return first.status, first_body, second.status, second_body
        finally:
            await client.close()

    first_status, first_body, second_status, second_body = asyncio.run(runner())

    assert first_status == 200
    assert first_body["result"] == "run-1:same work"
    assert second_status == 200
    assert second_body["ok"] is True
    assert second_body["result"] == "run-2:same work"
    assert dispatched["n"] == 2


def test_ingest_sync_secret_fingerprint_mismatch_expires_with_result_cache(monkeypatch):
    import hermes_multitenancy.webui_broker_server as mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["t"])
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    dispatched = {"n": 0}

    async def dispatch(request):
        dispatched["n"] += 1
        return f"run-{dispatched['n']}:{request.content}"

    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )
    base = {"content": "same work", "idempotency_key": "same-key"}

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            first = await client.post(
                "/api/run-broker/ingest",
                json={**base, "secrets": {"cms": {"type": "opaque", "value": "old"}}},
                headers={"Authorization": "Bearer testkey"},
            )
            first_body = json.loads(await first.text())
            clock["t"] += 4000.0
            second = await client.post(
                "/api/run-broker/ingest",
                json={**base, "secrets": {"cms": {"type": "opaque", "value": "new"}}},
                headers={"Authorization": "Bearer testkey"},
            )
            second_body = json.loads(await second.text())
            return first.status, first_body, second.status, second_body
        finally:
            await client.close()

    first_status, first_body, second_status, second_body = asyncio.run(runner())

    assert first_status == 200
    assert first_body["result"] == "run-1:same work"
    assert second_status == 200
    assert second_body["ok"] is True
    assert second_body["result"] == "run-2:same work"
    assert dispatched["n"] == 2


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


def test_ingest_sync_timeout_transfers_secret_ownership_and_dedupes_retry(
    monkeypatch, tmp_path
):
    from pathlib import Path

    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import webui_broker_server as server_mod

    secret_value = "sync-timeout-secret"
    prepare_calls = 0
    marked: list[str] = []
    seen: set[str] = set()
    materialized_dirs: list[Path] = []
    dispatches = 0
    dispatch_cancelled = False
    dispatch_started = asyncio.Event()
    dispatch_finished = asyncio.Event()
    release_dispatch = asyncio.Event()
    captured: dict[str, object] = {}

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        return request

    def mark_seen(request):
        key = request.effective_idempotency_key
        marked.append(key)
        if key in seen:
            return False
        seen.add(key)
        return True

    def is_seen(request):
        return request.effective_idempotency_key in seen

    real_materialize = server_mod._ingest_materialize_secret_dir

    def capture_materialize(**kwargs):
        secret_dir = Path(real_materialize(**kwargs))
        materialized_dirs.append(secret_dir)
        return str(secret_dir)

    async def dispatch(request):
        nonlocal dispatches, dispatch_cancelled
        dispatches += 1
        secret_dir = Path(request.metadata["ingest_secret_dir"])
        captured["secret_dir"] = secret_dir
        captured["value_before_timeout"] = (secret_dir / "cms").read_text(
            encoding="utf-8"
        )
        dispatch_started.set()
        try:
            await release_dispatch.wait()
        except asyncio.CancelledError:
            dispatch_cancelled = True
            raise
        captured["value_after_timeout"] = (secret_dir / "cms").read_text(
            encoding="utf-8"
        )
        dispatch_finished.set()
        return "ok"

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(
        server_mod,
        "_ingest_materialize_secret_dir",
        capture_materialize,
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setenv("HERMES_INGEST_TIMEOUT", "0.02")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "shared"))

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        is_seen=is_seen,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        payload = {
            "content": "hi",
            "idempotency_key": "sync-timeout-owned-secret",
            "secrets": {"cms": {"type": "opaque", "value": secret_value}},
        }
        headers = {"Authorization": "Bearer testkey"}
        try:
            first = await client.post(
                "/api/run-broker/ingest", json=payload, headers=headers
            )
            first_body = await first.json()
            await asyncio.wait_for(dispatch_started.wait(), timeout=SYNC_TIMEOUT)
            secret_dir = Path(captured["secret_dir"])
            retained_after_timeout = secret_dir.is_dir()

            retry = await asyncio.wait_for(
                client.post(
                    "/api/run-broker/ingest", json=payload, headers=headers
                ),
                timeout=0.5,
            )
            retry_body = await retry.json()
            retained_after_retry = secret_dir.is_dir()

            release_dispatch.set()
            await asyncio.wait_for(dispatch_finished.wait(), timeout=SYNC_TIMEOUT)
            for _ in range(100):
                if not secret_dir.exists():
                    break
                await asyncio.sleep(0.005)
            cleaned_after_execution = not secret_dir.exists()

            third = await client.post(
                "/api/run-broker/ingest", json=payload, headers=headers
            )
            third_body = await third.json()
            return (
                first.status,
                first_body,
                retry.status,
                retry_body,
                third.status,
                third_body,
                retained_after_timeout,
                retained_after_retry,
                cleaned_after_execution,
            )
        finally:
            release_dispatch.set()
            await client.close()

    (
        first_status,
        first_body,
        retry_status,
        retry_body,
        third_status,
        third_body,
        retained_after_timeout,
        retained_after_retry,
        cleaned_after_execution,
    ) = asyncio.run(runner())

    assert first_status == 504
    assert first_body["status"] == "timeout"
    assert retry_status == 200
    assert retry_body["status"] == "duplicate_pending"
    assert retry_body["duplicate"] is True
    assert third_status == 200
    assert third_body["status"] == "duplicate_pending"
    assert third_body["duplicate"] is True
    assert retained_after_timeout is True
    assert retained_after_retry is True
    assert cleaned_after_execution is True
    assert captured["value_before_timeout"] == secret_value
    assert captured["value_after_timeout"] == secret_value
    assert dispatch_cancelled is False
    assert prepare_calls == 1
    assert len(marked) == 1
    assert len(materialized_dirs) == 1
    assert dispatches == 1


def test_ingest_sync_outer_cancel_after_mark_preserves_secret_until_execution_finishes(
    monkeypatch, tmp_path
):
    from pathlib import Path

    from aiohttp import ClientConnectionError, ServerDisconnectedError
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import webui_broker_server as server_mod

    secret_value = "sync-outer-cancel-secret"
    captured: dict[str, object] = {}
    seen: set[str] = set()
    dispatches = 0
    dispatch_started = asyncio.Event()
    dispatch_finished = asyncio.Event()
    release_dispatch = asyncio.Event()

    def sandbox_available():
        captured.setdefault("request_waiter", asyncio.current_task())
        return True

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen:
            return False
        seen.add(key)
        request_waiter = captured["request_waiter"]
        assert isinstance(request_waiter, asyncio.Task)
        request_waiter.cancel()
        return True

    def is_seen(request):
        return request.effective_idempotency_key in seen

    async def dispatch(request):
        nonlocal dispatches
        dispatches += 1
        secret_dir = Path(request.metadata["ingest_secret_dir"])
        captured["secret_dir"] = secret_dir
        captured["value_at_dispatch"] = (secret_dir / "cms").read_text(
            encoding="utf-8"
        )
        dispatch_started.set()
        await release_dispatch.wait()
        captured["value_after_cancel"] = (secret_dir / "cms").read_text(
            encoding="utf-8"
        )
        dispatch_finished.set()
        return "ok"

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "shared"))

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        is_seen=is_seen,
        sandbox_available=sandbox_available,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        payload = {
            "content": "hi",
            "idempotency_key": "sync-outer-cancel-after-mark",
            "secrets": {"cms": {"type": "opaque", "value": secret_value}},
        }
        headers = {"Authorization": "Bearer testkey"}
        try:
            with pytest.raises((ServerDisconnectedError, ClientConnectionError)):
                await client.post(
                    "/api/run-broker/ingest",
                    json=payload,
                    headers=headers,
                )
            await asyncio.wait_for(dispatch_started.wait(), timeout=SYNC_TIMEOUT)
            secret_dir = Path(captured["secret_dir"])
            retained_after_cancel = secret_dir.is_dir()

            retry = await asyncio.wait_for(
                client.post(
                    "/api/run-broker/ingest",
                    json=payload,
                    headers=headers,
                ),
                timeout=0.5,
            )
            retry_body = await retry.json()

            release_dispatch.set()
            await asyncio.wait_for(dispatch_finished.wait(), timeout=SYNC_TIMEOUT)
            for _ in range(100):
                if not secret_dir.exists():
                    break
                await asyncio.sleep(0.005)
            return (
                retained_after_cancel,
                not secret_dir.exists(),
                retry.status,
                retry_body,
            )
        finally:
            release_dispatch.set()
            await client.close()

    retained_after_cancel, cleaned, retry_status, retry_body = asyncio.run(runner())

    assert retained_after_cancel is True
    assert cleaned is True
    assert retry_status == 200
    assert retry_body["status"] == "duplicate_pending"
    assert retry_body["duplicate"] is True
    assert captured["value_at_dispatch"] == secret_value
    assert captured["value_after_cancel"] == secret_value
    assert dispatches == 1


@pytest.mark.parametrize("stable_task_failure", ["raise", "cancel"])
def test_ingest_sync_stable_task_failure_before_first_step_is_fresh_retry(
    monkeypatch, tmp_path, stable_task_failure
):
    from pathlib import Path

    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import run_broker as broker_mod
    from hermes_multitenancy import webui_broker_server as server_mod

    secret_dir = tmp_path / "stable-cancel-secret"
    cleanup_calls: list[Path] = []
    cleanup_done = asyncio.Event()
    seen: set[str] = set()
    marked: list[str] = []
    dispatches = 0

    def materialize(**_kwargs):
        secret_dir.mkdir(mode=0o700)
        (secret_dir / "cms").write_text("v", encoding="utf-8")
        return str(secret_dir)

    real_cleanup = server_mod._ingest_cleanup_secret_dir

    def capture_cleanup(path):
        if path:
            cleanup_calls.append(Path(path))
        real_cleanup(path)
        if path:
            cleanup_done.set()

    def mark_seen(request):
        key = request.effective_idempotency_key
        marked.append(key)
        if key in seen:
            return False
        seen.add(key)
        return True

    def is_seen(request):
        return request.effective_idempotency_key in seen

    async def dispatch(_request):
        nonlocal dispatches
        dispatches += 1
        return "unexpected"

    real_create_task = broker_mod.asyncio.create_task
    cancel_once = True

    def fail_stable_task(coro):
        nonlocal cancel_once
        if (
            cancel_once
            and getattr(getattr(coro, "cr_code", None), "co_name", "")
            == "_run_after_admission"
        ):
            cancel_once = False
            if stable_task_failure == "raise":
                raise RuntimeError("stable task factory failed")
            task = real_create_task(coro)
            task.cancel()
            return task
        return real_create_task(coro)

    monkeypatch.setattr(
        server_mod,
        "_ingest_materialize_secret_dir",
        materialize,
    )
    monkeypatch.setattr(server_mod, "_ingest_cleanup_secret_dir", capture_cleanup)
    monkeypatch.setattr(broker_mod.asyncio, "create_task", fail_stable_task)
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        is_seen=is_seen,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        payload = {
            "content": "hi",
            "idempotency_key": "sync-stable-cancel-before-step",
            "secrets": {"cms": {"type": "opaque", "value": "v"}},
        }
        headers = {"Authorization": "Bearer testkey"}
        try:
            failed = await client.post(
                "/api/run-broker/ingest",
                json=payload,
                headers=headers,
            )
            failed_body = await failed.json()
            await asyncio.wait_for(cleanup_done.wait(), timeout=SYNC_TIMEOUT)
            retry = await client.post(
                "/api/run-broker/ingest",
                json={
                    **payload,
                    "secrets": {"cms": {"type": "opaque", "value": "different"}},
                },
                headers=headers,
            )
            retry_body = await retry.json()
            mismatch = await client.post(
                "/api/run-broker/ingest",
                json=payload,
                headers=headers,
            )
            return (
                failed.status,
                failed_body,
                retry.status,
                retry_body,
                mismatch.status,
                await mismatch.json(),
            )
        finally:
            await client.close()

    (
        failed_status,
        failed_body,
        retry_status,
        retry_body,
        mismatch_status,
        mismatch_body,
    ) = asyncio.run(runner())

    assert failed_status == 500
    assert failed_body["error"] == "internal error"
    assert retry_status == 200
    assert retry_body["ok"] is True
    assert retry_body["duplicate"] is False
    assert mismatch_status == 409
    assert mismatch_body["error"] == "secret_mismatch"
    assert len(marked) == 1
    assert dispatches == 1
    assert cleanup_calls == [secret_dir, secret_dir]
    assert not secret_dir.exists()


@pytest.mark.parametrize("handoff_failure", ["raise", "cancel"])
def test_ingest_sync_shared_entry_handoff_failure_releases_transient_claim(
    monkeypatch, tmp_path, handoff_failure
):
    from pathlib import Path

    from aiohttp import ClientConnectionError, ServerDisconnectedError
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import webui_broker_server as server_mod

    prepare_calls = 0
    materialize_calls = 0
    marked = 0
    dispatched = 0
    seen: set[str] = set()
    secret_dirs: list[Path] = []

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        return request

    def materialize(**_kwargs):
        nonlocal materialize_calls
        materialize_calls += 1
        secret_dir = tmp_path / f"shared-handoff-secret-{materialize_calls}"
        secret_dir.mkdir(mode=0o700)
        (secret_dir / "cms").write_text("secret-b", encoding="utf-8")
        secret_dirs.append(secret_dir)
        return str(secret_dir)

    def mark_seen(request):
        nonlocal marked
        key = request.effective_idempotency_key
        if key in seen:
            return False
        seen.add(key)
        marked += 1
        return True

    def is_seen(request):
        return request.effective_idempotency_key in seen

    async def dispatch(request):
        nonlocal dispatched
        dispatched += 1
        secret_dir = Path(request.metadata["ingest_secret_dir"])
        assert (secret_dir / "cms").read_text(encoding="utf-8") == "secret-b"
        return "ok"

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(
        server_mod,
        "_ingest_materialize_secret_dir",
        materialize,
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        is_seen=is_seen,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        loop = asyncio.get_running_loop()
        real_create_task = loop.create_task
        injected = False

        def fail_or_cancel_shared_task(coro, *args, **kwargs):
            nonlocal injected
            if (
                not injected
                and getattr(getattr(coro, "cr_code", None), "co_name", "")
                == "_prepare_admit_execute_once"
            ):
                injected = True
                if handoff_failure == "raise":
                    coro.close()
                    raise RuntimeError("shared task creation failed")
                task = real_create_task(coro, *args, **kwargs)
                task.cancel()
                return task
            return real_create_task(coro, *args, **kwargs)

        headers = {"Authorization": "Bearer testkey"}
        base = {"content": "hi", "idempotency_key": "shared-handoff-claim"}
        loop.create_task = fail_or_cancel_shared_task
        try:
            if handoff_failure == "raise":
                failed = await client.post(
                    "/api/run-broker/ingest",
                    json={
                        **base,
                        "secrets": {
                            "cms": {"type": "opaque", "value": "secret-a"}
                        },
                    },
                    headers=headers,
                )
                failed_status = failed.status
                failed_body = await failed.json()
            else:
                with pytest.raises((ServerDisconnectedError, ClientConnectionError)):
                    await client.post(
                        "/api/run-broker/ingest",
                        json={
                            **base,
                            "secrets": {
                                "cms": {"type": "opaque", "value": "secret-a"}
                            },
                        },
                        headers=headers,
                    )
                failed_status = 0
                failed_body = {}
        finally:
            loop.create_task = real_create_task

        for _ in range(10):
            await asyncio.sleep(0)
        retry = await client.post(
            "/api/run-broker/ingest",
            json={
                **base,
                "secrets": {"cms": {"type": "opaque", "value": "secret-b"}},
            },
            headers=headers,
        )
        retry_body = await retry.json()
        await client.close()
        return failed_status, failed_body, retry.status, retry_body

    failed_status, failed_body, retry_status, retry_body = asyncio.run(runner())

    if handoff_failure == "raise":
        assert failed_status == 500
        assert failed_body["error"] == "internal error"
    assert retry_status == 200
    assert retry_body["ok"] is True
    assert retry_body["duplicate"] is False
    assert prepare_calls == 1
    assert materialize_calls == 1
    assert marked == 1
    assert dispatched == 1
    assert all(not path.exists() for path in secret_dirs)


def test_ingest_sync_interactive_outer_cancel_keeps_claim_until_waiter_stops(
    monkeypatch, tmp_path
):
    from pathlib import Path

    from aiohttp import ClientConnectionError, ServerDisconnectedError
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import webui_broker_server as server_mod

    prepare_calls = 0
    materialize_calls = 0
    marked: list[str] = []
    seen: set[str] = set()
    dispatches = 0
    outer_handler: dict[str, asyncio.Task] = {}
    prepare_started = asyncio.Event()
    prepare_cancelled = asyncio.Event()
    release_cancel_cleanup = asyncio.Event()
    secret_dirs: list[Path] = []

    real_binding_for_request = server_mod._ingest_binding_for_request

    def capture_outer_handler(request):
        task = asyncio.current_task()
        assert isinstance(task, asyncio.Task)
        outer_handler.setdefault("task", task)
        return real_binding_for_request(request)

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        if prepare_calls == 1:
            prepare_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                prepare_cancelled.set()
                while not release_cancel_cleanup.is_set():
                    try:
                        await release_cancel_cleanup.wait()
                    except asyncio.CancelledError:
                        continue
                raise
        return request

    def materialize(**_kwargs):
        nonlocal materialize_calls
        materialize_calls += 1
        secret_dir = tmp_path / f"interactive-secret-{materialize_calls}"
        secret_dir.mkdir(mode=0o700)
        (secret_dir / "cms").write_text("secret-b", encoding="utf-8")
        secret_dirs.append(secret_dir)
        return str(secret_dir)

    def mark_seen(request):
        key = request.effective_idempotency_key
        marked.append(key)
        if key in seen:
            return False
        seen.add(key)
        return True

    def is_seen(request):
        return request.effective_idempotency_key in seen

    async def dispatch(request):
        nonlocal dispatches
        dispatches += 1
        secret_dir = Path(request.metadata["ingest_secret_dir"])
        assert (secret_dir / "cms").read_text(encoding="utf-8") == "secret-b"
        return "ok"

    monkeypatch.setattr(
        server_mod,
        "_ingest_binding_for_request",
        capture_outer_handler,
    )
    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(
        server_mod,
        "_ingest_materialize_secret_dir",
        materialize,
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        is_seen=is_seen,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer testkey"}
        base = {
            "content": "hi",
            "interactive": True,
            "idempotency_key": "interactive-outer-cancel-claim",
        }
        first_request = asyncio.create_task(
            client.post(
                "/api/run-broker/ingest",
                json={
                    **base,
                    "secrets": {"cms": {"type": "opaque", "value": "secret-a"}},
                },
                headers=headers,
            )
        )
        try:
            await asyncio.wait_for(prepare_started.wait(), timeout=SYNC_TIMEOUT)
            outer_handler["task"].cancel()
            await asyncio.wait_for(prepare_cancelled.wait(), timeout=SYNC_TIMEOUT)

            mismatch = await asyncio.wait_for(
                client.post(
                    "/api/run-broker/ingest",
                    json={
                        **base,
                        "secrets": {
                            "cms": {"type": "opaque", "value": "secret-b"}
                        },
                    },
                    headers=headers,
                ),
                timeout=0.5,
            )
            mismatch_body = await mismatch.json()

            release_cancel_cleanup.set()
            with pytest.raises((ServerDisconnectedError, ClientConnectionError)):
                await first_request

            retry = await client.post(
                "/api/run-broker/ingest",
                json={
                    **base,
                    "secrets": {"cms": {"type": "opaque", "value": "secret-b"}},
                },
                headers=headers,
            )
            return (
                mismatch.status,
                mismatch_body,
                retry.status,
                await retry.json(),
            )
        finally:
            release_cancel_cleanup.set()
            if not first_request.done():
                first_request.cancel()
            await client.close()

    mismatch_status, mismatch_body, retry_status, retry_body = asyncio.run(runner())

    assert mismatch_status == 409, mismatch_body
    assert mismatch_body["error"] == "secret_mismatch"
    assert retry_status == 200
    assert retry_body["ok"] is True
    assert retry_body["duplicate"] is False
    assert prepare_calls == 2
    assert materialize_calls == 1
    assert len(marked) == 1
    assert dispatches == 1
    assert all(not path.exists() for path in secret_dirs)


def test_ingest_sync_pre_admission_timeout_keeps_claim_until_shared_task_stops(
    monkeypatch, tmp_path
):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import webui_broker_server as server_mod

    prepare_calls = 0
    materialize_calls = 0
    marked = 0
    dispatched = 0
    prepare_cancelled = asyncio.Event()
    prepare_exited = asyncio.Event()
    release_cancel_cleanup = asyncio.Event()
    seen: set[str] = set()

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        if prepare_calls == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                prepare_cancelled.set()
                while not release_cancel_cleanup.is_set():
                    try:
                        await release_cancel_cleanup.wait()
                    except asyncio.CancelledError:
                        continue
                prepare_exited.set()
                raise
        return request

    def materialize(**_kwargs):
        nonlocal materialize_calls
        materialize_calls += 1
        secret_dir = tmp_path / "timeout-retry-secret"
        secret_dir.mkdir(mode=0o700)
        (secret_dir / "cms").write_text("secret-b", encoding="utf-8")
        return str(secret_dir)

    def mark_seen(request):
        nonlocal marked
        key = request.effective_idempotency_key
        if key in seen:
            return False
        seen.add(key)
        marked += 1
        return True

    def is_seen(request):
        return request.effective_idempotency_key in seen

    async def dispatch(_request):
        nonlocal dispatched
        dispatched += 1
        return "ok"

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(
        server_mod,
        "_ingest_materialize_secret_dir",
        materialize,
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    # Only the FIRST request is supposed to time out, and it does so
    # deterministically: its ``prepare`` blocks on an event that is never set,
    # so any positive timeout fires. The handler re-reads HERMES_INGEST_TIMEOUT
    # per request, so the later requests — which must SUCCEED — get a generous
    # bound instead of racing a 20ms wall clock (that race is the whole flake:
    # under load the retry's full round trip exceeds 20ms and returns 504).
    monkeypatch.setenv("HERMES_INGEST_TIMEOUT", "0.02")

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        is_seen=is_seen,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer testkey"}
        base = {"content": "hi", "idempotency_key": "premark-timeout-claim"}
        try:
            timed_out = await client.post(
                "/api/run-broker/ingest",
                json={
                    **base,
                    "secrets": {"cms": {"type": "opaque", "value": "secret-a"}},
                },
                headers=headers,
            )
            timed_out_body = await timed_out.json()
            monkeypatch.setenv("HERMES_INGEST_TIMEOUT", "60")
            await asyncio.wait_for(prepare_cancelled.wait(), timeout=SYNC_TIMEOUT)

            mismatch = await client.post(
                "/api/run-broker/ingest",
                json={
                    **base,
                    "secrets": {"cms": {"type": "opaque", "value": "secret-b"}},
                },
                headers=headers,
            )
            mismatch_body = await mismatch.json()

            release_cancel_cleanup.set()
            await asyncio.wait_for(prepare_exited.wait(), timeout=SYNC_TIMEOUT)
            for _ in range(10):
                await asyncio.sleep(0)

            retry = await client.post(
                "/api/run-broker/ingest",
                json={
                    **base,
                    "secrets": {"cms": {"type": "opaque", "value": "secret-b"}},
                },
                headers=headers,
            )
            return (
                timed_out.status,
                timed_out_body,
                mismatch.status,
                mismatch_body,
                retry.status,
                await retry.json(),
            )
        finally:
            release_cancel_cleanup.set()
            await client.close()

    (
        timeout_status,
        timeout_body,
        mismatch_status,
        mismatch_body,
        retry_status,
        retry_body,
    ) = asyncio.run(runner())

    assert timeout_status == 504
    assert timeout_body["status"] == "timeout"
    assert mismatch_status == 409
    assert mismatch_body["error"] == "secret_mismatch"
    assert retry_status == 200
    assert retry_body["ok"] is True
    assert retry_body["duplicate"] is False
    assert prepare_calls == 2
    assert materialize_calls == 1
    assert marked == 1
    assert dispatched == 1
    assert not (tmp_path / "timeout-retry-secret").exists()


# ── Async polling ingest ─────────────────────────────────────────────────

def test_ingest_async_billing_failure_keeps_idempotency_retryable(monkeypatch):
    from dataclasses import replace

    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy.run_broker import RunRejected
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    prepare_attempts = 0
    marked = []
    seen = set()
    dispatched = []

    async def prepare(request):
        nonlocal prepare_attempts
        prepare_attempts += 1
        if prepare_attempts == 1:
            raise RunRejected("temporary billing lookup failure")
        return replace(request, metadata={**request.metadata, "billing_prepared": True})

    def mark_seen(request):
        key = request.effective_idempotency_key
        marked.append(key)
        if key in seen:
            return False
        seen.add(key)
        return True

    async def dispatch(request):
        dispatched.append(request)
        return "ok"

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        payload = {"content": "hi", "idempotency_key": "async-turn-1"}
        headers = {"Authorization": "Bearer testkey"}
        try:
            failed = await client.post("/api/run-broker/ingest/async", json=payload, headers=headers)
            failed_body = await failed.json()
            retry = await client.post("/api/run-broker/ingest/async", json=payload, headers=headers)
            retry_body = await retry.json()
            final_body = {}
            for _ in range(20):
                poll = await client.get(retry_body["poll_url"], headers=headers)
                final_body = await poll.json()
                if final_body["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            duplicate = await client.post("/api/run-broker/ingest/async", json=payload, headers=headers)
            return (
                failed.status,
                failed_body,
                retry.status,
                retry_body,
                final_body,
                duplicate.status,
                await duplicate.json(),
            )
        finally:
            await client.close()

    failed_status, failed_body, retry_status, retry_body, final_body, duplicate_status, duplicate_body = asyncio.run(runner())

    assert failed_status == 503
    assert failed_body["status"] == "prepare_failed"
    assert retry_status == 202
    assert retry_body["duplicate"] is False
    assert final_body["status"] == "succeeded"
    assert duplicate_status == 202
    assert duplicate_body["duplicate"] is True
    assert duplicate_body["run_id"] == retry_body["run_id"]
    assert len(marked) == 1
    assert len(dispatched) == 1
    assert dispatched[0].metadata["billing_prepared"] is True


def test_ingest_async_secret_materialization_failure_keeps_idempotency_retryable(
    monkeypatch, tmp_path
):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy.webui_broker import periphery
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    prepare_calls = 0
    marked = []
    dispatched = []

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        return request

    def mark_seen(request):
        marked.append(request.effective_idempotency_key)
        return True

    async def dispatch(request):
        dispatched.append(request)
        return "ok"

    real_write = periphery.os.write
    fail_once = True

    def flaky_write(fd, data):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("simulated secret write failure")
        return real_write(fd, data)

    monkeypatch.setattr(periphery.os, "write", flaky_write)
    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "shared"))

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )
    secret_root = tmp_path / "shared" / "profiles" / "owner" / "tmp" / "ingest-secrets"

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        payload = {
            "content": "hi",
            "idempotency_key": "async-secret-retry",
            "secrets": {"cms": {"type": "opaque", "value": "retry-secret"}},
        }
        headers = {"Authorization": "Bearer testkey"}
        try:
            failed = await client.post("/api/run-broker/ingest/async", json=payload, headers=headers)
            failed_body = await failed.json()
            leftovers_after_failure = list(secret_root.iterdir()) if secret_root.exists() else []

            retry = await client.post("/api/run-broker/ingest/async", json=payload, headers=headers)
            retry_body = await retry.json()
            final_body = {}
            for _ in range(20):
                poll = await client.get(retry_body["poll_url"], headers=headers)
                final_body = await poll.json()
                if final_body["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            duplicate = await client.post("/api/run-broker/ingest/async", json=payload, headers=headers)
            return (
                failed.status,
                failed_body,
                leftovers_after_failure,
                retry.status,
                retry_body,
                final_body,
                duplicate.status,
                await duplicate.json(),
            )
        finally:
            await client.close()

    (
        failed_status,
        failed_body,
        leftovers_after_failure,
        retry_status,
        retry_body,
        final_body,
        duplicate_status,
        duplicate_body,
    ) = asyncio.run(runner())

    assert failed_status == 400
    assert failed_body["error"] == "invalid secrets"
    assert leftovers_after_failure == []
    assert retry_status == 202
    assert retry_body["duplicate"] is False
    assert final_body["status"] == "succeeded"
    assert duplicate_status == 202
    assert duplicate_body["duplicate"] is True
    assert duplicate_body["run_id"] == retry_body["run_id"]
    # Secret staging is shared after billing preparation and before durable
    # admission, so the local-write retry prepares billing again but never
    # consumes the idempotency key on the failed attempt.
    assert prepare_calls == 2
    assert len(marked) == 1
    assert len(dispatched) == 1


def test_ingest_async_cancelled_prepare_cleans_secrets_and_keeps_retryable(
    monkeypatch, tmp_path
):
    from aiohttp import ClientConnectionError, ServerDisconnectedError
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    prepare_calls = 0
    marked = []
    dispatched = []

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        if prepare_calls == 1:
            raise asyncio.CancelledError()
        return request

    def mark_seen(request):
        marked.append(request.effective_idempotency_key)
        return True

    async def dispatch(request):
        dispatched.append(request)
        return "ok"

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "shared"))

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )
    secret_root = tmp_path / "shared" / "profiles" / "owner" / "tmp" / "ingest-secrets"

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        payload = {
            "content": "hi",
            "idempotency_key": "async-cancel-retry",
            "secrets": {"cms": {"type": "opaque", "value": "retry-secret"}},
        }
        headers = {"Authorization": "Bearer testkey"}
        try:
            with pytest.raises((ServerDisconnectedError, ClientConnectionError)):
                await client.post("/api/run-broker/ingest/async", json=payload, headers=headers)
            leftovers_after_cancel = list(secret_root.iterdir()) if secret_root.exists() else []

            retry = await client.post("/api/run-broker/ingest/async", json=payload, headers=headers)
            retry_body = await retry.json()
            final_body = {}
            for _ in range(20):
                poll = await client.get(retry_body["poll_url"], headers=headers)
                final_body = await poll.json()
                if final_body["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            return leftovers_after_cancel, retry.status, retry_body, final_body
        finally:
            await client.close()

    leftovers_after_cancel, retry_status, retry_body, final_body = asyncio.run(runner())

    assert leftovers_after_cancel == []
    assert retry_status == 202
    assert retry_body["duplicate"] is False
    assert final_body["status"] == "succeeded"
    assert prepare_calls == 2
    assert len(marked) == 1
    assert len(dispatched) == 1


def test_ingest_async_job_cancelled_before_first_step_is_retryable(
    monkeypatch, tmp_path, caplog
):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import webui_broker_server as server_mod

    secret_value = "prestep-plain-secret"
    prepare_calls = 0
    materialize_calls = 0
    marked = 0
    dispatched = 0

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        return request

    real_materialize = server_mod._ingest_materialize_secret_dir

    def materialize(**kwargs):
        nonlocal materialize_calls
        materialize_calls += 1
        return real_materialize(**kwargs)

    def mark_seen(_request):
        nonlocal marked
        marked += 1
        return True

    async def dispatch(_request):
        nonlocal dispatched
        dispatched += 1
        return "ok"

    real_create_task = server_mod.asyncio.create_task
    cancel_once = True

    def cancel_job_before_first_step(coro, *args, **kwargs):
        nonlocal cancel_once
        task = real_create_task(coro, *args, **kwargs)
        if (
            cancel_once
            and getattr(getattr(coro, "cr_code", None), "co_name", "")
            == "_run_registered_ingest_async_job"
        ):
            cancel_once = False
            task.cancel()
        return task

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(server_mod, "_ingest_materialize_secret_dir", materialize)
    monkeypatch.setattr(server_mod.asyncio, "create_task", cancel_job_before_first_step)
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "shared"))
    caplog.set_level("ERROR")

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        payload = {
            "content": "hi",
            "idempotency_key": "async-prestep-cancel",
            "secrets": {"cms": {"type": "opaque", "value": secret_value}},
        }
        headers = {"Authorization": "Bearer testkey"}
        try:
            failed = await client.post(
                "/api/run-broker/ingest/async", json=payload, headers=headers
            )
            failed_body = await failed.json()
            retry = await client.post(
                "/api/run-broker/ingest/async", json=payload, headers=headers
            )
            retry_body = await retry.json()
            final_body = {}
            for _ in range(50):
                poll = await client.get(retry_body["poll_url"], headers=headers)
                final_body = await poll.json()
                if final_body["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            return failed.status, failed_body, retry.status, retry_body, final_body
        finally:
            await client.close()

    failed_status, failed_body, retry_status, retry_body, final_body = asyncio.run(
        runner()
    )

    assert failed_status == 503
    assert failed_body["status"] == "prepare_failed"
    assert "run_id" not in failed_body
    assert retry_status == 202
    assert retry_body["duplicate"] is False
    assert final_body["status"] == "succeeded"
    assert prepare_calls == 1
    assert materialize_calls == 1
    assert marked == 1
    assert dispatched == 1
    assert secret_value not in json.dumps(failed_body)
    assert secret_value not in caplog.text


def test_ingest_async_running_task_cancel_is_terminal_and_reclaims_capacity(
    monkeypatch, tmp_path, caplog
):
    from pathlib import Path

    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import webui_broker_server as server_mod

    secret_value = "running-cancel-plain-secret"
    dispatch_started = asyncio.Event()
    job_tasks: list[asyncio.Task] = []
    dispatches = 0
    first_secret_dir: Path | None = None

    async def dispatch(request):
        nonlocal dispatches, first_secret_dir
        dispatches += 1
        if dispatches == 1:
            first_secret_dir = Path(request.metadata["ingest_secret_dir"])
            assert (first_secret_dir / "cms").read_text(encoding="utf-8") == secret_value
            dispatch_started.set()
            await asyncio.Event().wait()
        return "ok"

    real_create_task = server_mod.asyncio.create_task

    def capture_job_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        if (
            getattr(getattr(coro, "cr_code", None), "co_name", "")
            == "_run_registered_ingest_async_job"
        ):
            job_tasks.append(task)
        return task

    monkeypatch.setattr(server_mod.asyncio, "create_task", capture_job_task)
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "shared"))
    monkeypatch.setenv("HERMES_INGEST_ASYNC_CAP", "1")
    caplog.set_level("ERROR")

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def wait_for_terminal(client, poll_url, headers):
        body = {}
        for _ in range(50):
            poll = await client.get(poll_url, headers=headers)
            body = await poll.json()
            if body.get("status") in {"failed", "succeeded"}:
                return body
            await asyncio.sleep(0.01)
        return body

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer testkey"}
        first_payload = {
            "content": "first",
            "idempotency_key": "async-running-cancel",
            "secrets": {"cms": {"type": "opaque", "value": secret_value}},
        }
        try:
            first = await client.post(
                "/api/run-broker/ingest/async",
                json=first_payload,
                headers=headers,
            )
            first_body = await first.json()
            await asyncio.wait_for(dispatch_started.wait(), timeout=SYNC_TIMEOUT)
            assert job_tasks
            job_tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await job_tasks[0]
            failed_body = await wait_for_terminal(
                client, first_body["poll_url"], headers
            )

            second = await client.post(
                "/api/run-broker/ingest/async",
                json={"content": "second", "idempotency_key": "capacity-reuse"},
                headers=headers,
            )
            second_body = await second.json()
            second_final = await wait_for_terminal(
                client, second_body["poll_url"], headers
            )
            return first.status, failed_body, second.status, second_body, second_final
        finally:
            await client.close()

    first_status, failed_body, second_status, second_body, second_final = asyncio.run(
        runner()
    )

    assert first_status == 202
    assert failed_body["status"] == "failed"
    assert failed_body["error"] == "async ingest task cancelled"
    assert first_secret_dir is not None and not first_secret_dir.exists()
    assert second_status == 202
    assert second_body["duplicate"] is False
    assert second_final["status"] == "succeeded"
    assert dispatches == 2
    assert secret_value not in json.dumps(failed_body)
    assert secret_value not in caplog.text


def test_ingest_async_task_factory_throw_is_retryable_and_redacted(
    monkeypatch, tmp_path, caplog
):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import webui_broker_server as server_mod

    secret_value = "factory-throw-plain-secret"
    prepare_calls = 0
    marked = 0
    dispatched = 0

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        return request

    def mark_seen(_request):
        nonlocal marked
        marked += 1
        return True

    async def dispatch(_request):
        nonlocal dispatched
        dispatched += 1
        return "ok"

    real_create_task = server_mod.asyncio.create_task
    fail_once = True

    def fail_job_task_creation(coro, *args, **kwargs):
        nonlocal fail_once
        if (
            fail_once
            and getattr(getattr(coro, "cr_code", None), "co_name", "")
            == "_run_registered_ingest_async_job"
        ):
            fail_once = False
            raise RuntimeError(f"task factory exposed {secret_value}")
        return real_create_task(coro, *args, **kwargs)

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(server_mod.asyncio, "create_task", fail_job_task_creation)
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "shared"))
    caplog.set_level("ERROR")

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer testkey"}
        payload = {
            "content": "hi",
            "idempotency_key": "async-task-factory-throw",
            "secrets": {"cms": {"type": "opaque", "value": secret_value}},
        }
        try:
            failed = await client.post(
                "/api/run-broker/ingest/async", json=payload, headers=headers
            )
            failed_body = await failed.json()
            retry = await client.post(
                "/api/run-broker/ingest/async", json=payload, headers=headers
            )
            retry_body = await retry.json()
            final_body = {}
            for _ in range(50):
                poll = await client.get(retry_body["poll_url"], headers=headers)
                final_body = await poll.json()
                if final_body["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            return failed.status, failed_body, retry.status, retry_body, final_body
        finally:
            await client.close()

    failed_status, failed_body, retry_status, retry_body, final_body = asyncio.run(
        runner()
    )

    assert failed_status == 503
    assert failed_body["status"] == "prepare_failed"
    assert "run_id" not in failed_body
    assert retry_status == 202
    assert retry_body["duplicate"] is False
    assert final_body["status"] == "succeeded"
    assert prepare_calls == 1
    assert marked == 1
    assert dispatched == 1
    assert secret_value not in json.dumps(failed_body)
    assert secret_value not in caplog.text


def test_ingest_async_outer_cancel_after_mark_preserves_handed_off_secrets(
    monkeypatch, tmp_path
):
    from pathlib import Path

    from aiohttp import ClientConnectionError, ServerDisconnectedError
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import webui_broker_server as server_mod

    secret_value = "outer-cancel-secret"
    captured: dict[str, object] = {}
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    seen: set[str] = set()
    dispatches = 0

    real_materialize = server_mod._ingest_materialize_secret_dir

    def capture_materialize(**kwargs):
        secret_dir = real_materialize(**kwargs)
        captured["secret_dir"] = Path(secret_dir)
        return secret_dir

    def sandbox_available():
        # The first policy check runs in the real aiohttp handler, before the
        # broker creates its shared preparation task.
        captured.setdefault("handler_task", asyncio.current_task())
        return True

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen:
            return False
        seen.add(key)
        handler_task = captured["handler_task"]
        assert isinstance(handler_task, asyncio.Task)
        handler_task.cancel()
        return True

    async def dispatch(request):
        nonlocal dispatches
        dispatches += 1
        secret_dir = Path(request.metadata["ingest_secret_dir"])
        captured["dir_at_dispatch"] = secret_dir.is_dir()
        captured["file_at_dispatch"] = (secret_dir / "cms").is_file()
        captured["value_at_dispatch"] = (secret_dir / "cms").read_text(
            encoding="utf-8"
        )
        dispatch_started.set()
        await release_dispatch.wait()
        return "ok"

    monkeypatch.setattr(
        server_mod,
        "_ingest_materialize_secret_dir",
        capture_materialize,
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "shared"))

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        sandbox_available=sandbox_available,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        payload = {
            "content": "hi",
            "idempotency_key": "async-outer-cancel-after-mark",
            "secrets": {"cms": {"type": "opaque", "value": secret_value}},
        }
        headers = {"Authorization": "Bearer testkey"}
        try:
            with pytest.raises((ServerDisconnectedError, ClientConnectionError)):
                await client.post(
                    "/api/run-broker/ingest/async",
                    json=payload,
                    headers=headers,
                )

            await asyncio.wait_for(dispatch_started.wait(), timeout=SYNC_TIMEOUT)
            retained_while_running = Path(captured["secret_dir"]).is_dir()

            retry = await client.post(
                "/api/run-broker/ingest/async",
                json=payload,
                headers=headers,
            )
            retry_body = await retry.json()
            release_dispatch.set()

            final_body = {}
            for _ in range(50):
                poll = await client.get(retry_body["poll_url"], headers=headers)
                final_body = await poll.json()
                if final_body["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            terminal_secret_exists = Path(captured["secret_dir"]).exists()
            return (
                retained_while_running,
                retry.status,
                retry_body,
                final_body,
                terminal_secret_exists,
            )
        finally:
            release_dispatch.set()
            await client.close()

    (
        retained_while_running,
        retry_status,
        retry_body,
        final_body,
        terminal_secret_exists,
    ) = asyncio.run(runner())

    assert captured["dir_at_dispatch"] is True
    assert captured["file_at_dispatch"] is True
    assert captured["value_at_dispatch"] == secret_value
    assert retained_while_running is True
    assert retry_status == 202
    assert retry_body["duplicate"] is True
    assert final_body["status"] == "succeeded"
    assert dispatches == 1
    assert terminal_secret_exists is False


def test_ingest_async_pre_mark_leader_cancel_with_live_peer_uses_one_owned_secret(
    monkeypatch, tmp_path
):
    from pathlib import Path

    from aiohttp import ClientConnectionError, ServerDisconnectedError
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import run_broker as broker_mod
    from hermes_multitenancy import webui_broker_server as server_mod

    secret_value = "shared-pre-mark-secret"
    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    server_handlers: list[asyncio.Task] = []
    materialized_dirs: list[Path] = []
    seen: set[str] = set()
    captured: dict[str, object] = {}
    dispatches = 0

    async def prepare(request):
        prepare_started.set()
        await release_prepare.wait()
        return request

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen:
            return False
        seen.add(key)
        return True

    async def dispatch(request):
        nonlocal dispatches
        dispatches += 1
        secret_dir = Path(request.metadata["ingest_secret_dir"])
        captured["secret_dir"] = secret_dir
        captured["dir_at_dispatch"] = secret_dir.is_dir()
        captured["file_at_dispatch"] = (secret_dir / "cms").is_file()
        captured["value_at_dispatch"] = (secret_dir / "cms").read_text(
            encoding="utf-8"
        )
        dispatch_started.set()
        await release_dispatch.wait()
        return "ok"

    real_prepare_and_execute = broker_mod.RunBroker.prepare_and_execute

    async def capture_server_handler(self, *args, **kwargs):
        task = asyncio.current_task()
        assert isinstance(task, asyncio.Task)
        server_handlers.append(task)
        return await real_prepare_and_execute(self, *args, **kwargs)

    real_materialize = server_mod._ingest_materialize_secret_dir

    def capture_materialize(**kwargs):
        secret_dir = Path(real_materialize(**kwargs))
        materialized_dirs.append(secret_dir)
        return str(secret_dir)

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(
        broker_mod.RunBroker,
        "prepare_and_execute",
        capture_server_handler,
    )
    monkeypatch.setattr(
        server_mod,
        "_ingest_materialize_secret_dir",
        capture_materialize,
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "shared"))

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )
    secret_root = tmp_path / "shared" / "profiles" / "owner" / "tmp" / "ingest-secrets"

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        payload = {
            "content": "hi",
            "idempotency_key": "async-pre-mark-leader-cancel",
            "secrets": {"cms": {"type": "opaque", "value": secret_value}},
        }
        headers = {"Authorization": "Bearer testkey"}
        try:
            leader_request = asyncio.create_task(
                client.post(
                    "/api/run-broker/ingest/async",
                    json=payload,
                    headers=headers,
                )
            )
            await asyncio.wait_for(prepare_started.wait(), timeout=SYNC_TIMEOUT)
            peer_request = asyncio.create_task(
                client.post(
                    "/api/run-broker/ingest/async",
                    json=payload,
                    headers=headers,
                )
            )

            for _ in range(100):
                with broker_mod._PREPARE_INFLIGHT_LOCK:
                    joined = any(
                        entry.waiters == 2
                        for entry in broker_mod._EXECUTION_INFLIGHT.values()
                    )
                if len(server_handlers) >= 2 and joined:
                    break
                await asyncio.sleep(0.005)
            assert len(server_handlers) >= 2
            assert joined is True

            server_handlers[0].cancel()
            with pytest.raises((ServerDisconnectedError, ClientConnectionError)):
                await leader_request
            assert materialized_dirs == []

            release_prepare.set()
            peer = await peer_request
            peer_body = await peer.json()
            await asyncio.wait_for(dispatch_started.wait(), timeout=SYNC_TIMEOUT)

            retry = await client.post(
                "/api/run-broker/ingest/async",
                json=payload,
                headers=headers,
            )
            retry_body = await retry.json()
            release_dispatch.set()

            final_body = {}
            for _ in range(50):
                poll = await client.get(peer_body["poll_url"], headers=headers)
                final_body = await poll.json()
                if final_body["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            leftovers = list(secret_root.iterdir()) if secret_root.exists() else []
            return peer.status, peer_body, retry.status, retry_body, final_body, leftovers
        finally:
            release_prepare.set()
            release_dispatch.set()
            await client.close()

    peer_status, peer_body, retry_status, retry_body, final_body, leftovers = (
        asyncio.run(runner())
    )

    assert peer_status == 202
    assert peer_body["duplicate"] is True
    assert retry_status == 202
    assert retry_body["duplicate"] is True
    assert retry_body["run_id"] == peer_body["run_id"]
    assert captured["dir_at_dispatch"] is True
    assert captured["file_at_dispatch"] is True
    assert captured["value_at_dispatch"] == secret_value
    assert final_body["status"] == "succeeded"
    assert dispatches == 1
    assert len(materialized_dirs) == 1
    assert captured["secret_dir"] == materialized_dirs[0]
    assert leftovers == []


def test_ingest_async_concurrent_secret_mismatch_is_rejected_before_shared_billing(
    monkeypatch, tmp_path
):
    from pathlib import Path

    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import webui_broker_server as server_mod

    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()
    prepare_calls = 0
    materialize_calls = 0
    dispatched_values: list[str] = []

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        prepare_started.set()
        await release_prepare.wait()
        return request

    async def dispatch(request):
        secret_dir = Path(request.metadata["ingest_secret_dir"])
        dispatched_values.append((secret_dir / "cms").read_text(encoding="utf-8"))
        return "ok"

    real_materialize = server_mod._ingest_materialize_secret_dir

    def count_materialize(**kwargs):
        nonlocal materialize_calls
        materialize_calls += 1
        return real_materialize(**kwargs)

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(
        server_mod,
        "_ingest_materialize_secret_dir",
        count_materialize,
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "shared"))

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer testkey"}
        first_payload = {
            "content": "hi",
            "idempotency_key": "async-concurrent-secret-mismatch",
            "secrets": {"cms": {"type": "opaque", "value": "leader-secret"}},
        }
        second_payload = {
            **first_payload,
            "secrets": {"cms": {"type": "opaque", "value": "peer-secret"}},
        }
        try:
            first_request = asyncio.create_task(
                client.post(
                    "/api/run-broker/ingest/async",
                    json=first_payload,
                    headers=headers,
                )
            )
            await asyncio.wait_for(prepare_started.wait(), timeout=SYNC_TIMEOUT)
            second = await client.post(
                "/api/run-broker/ingest/async",
                json=second_payload,
                headers=headers,
            )
            second_body = await second.json()
            materialized_while_preparing = materialize_calls

            release_prepare.set()
            first = await first_request
            first_body = await first.json()
            final_body = {}
            for _ in range(50):
                poll = await client.get(first_body["poll_url"], headers=headers)
                final_body = await poll.json()
                if final_body["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            return (
                first.status,
                first_body,
                second.status,
                second_body,
                materialized_while_preparing,
                final_body,
            )
        finally:
            release_prepare.set()
            await client.close()

    (
        first_status,
        first_body,
        second_status,
        second_body,
        materialized_while_preparing,
        final_body,
    ) = asyncio.run(runner())

    assert first_status == 202
    assert first_body["duplicate"] is False
    assert second_status == 409
    assert second_body["ok"] is False
    assert materialized_while_preparing == 0
    assert final_body["status"] == "succeeded"
    assert prepare_calls == 1
    assert materialize_calls == 1
    assert dispatched_values == ["leader-secret"]


def test_ingest_async_abandoned_claim_does_not_release_same_fingerprint_successor(
    monkeypatch, tmp_path
):
    from pathlib import Path

    from aiohttp import ClientConnectionError, ServerDisconnectedError
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy import run_broker as broker_mod
    from hermes_multitenancy import webui_broker_server as server_mod

    first_prepare_started = asyncio.Event()
    first_prepare_cancelled = asyncio.Event()
    second_before_broker = asyncio.Event()
    release_second_broker = asyncio.Event()
    second_prepare_started = asyncio.Event()
    release_second_prepare = asyncio.Event()
    handler_tasks: list[asyncio.Task] = []
    prepare_calls = 0
    materialize_calls = 0
    dispatches = 0
    dispatched_values: list[str] = []

    async def prepare(request):
        nonlocal prepare_calls
        prepare_calls += 1
        if prepare_calls == 1:
            first_prepare_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                first_prepare_cancelled.set()
                raise
        second_prepare_started.set()
        await release_second_prepare.wait()
        return request

    async def dispatch(request):
        nonlocal dispatches
        dispatches += 1
        secret_dir = Path(request.metadata["ingest_secret_dir"])
        dispatched_values.append((secret_dir / "cms").read_text(encoding="utf-8"))
        return "ok"

    real_prepare_and_execute = broker_mod.RunBroker.prepare_and_execute
    prepare_and_execute_calls = 0

    async def pause_successor_before_broker(self, *args, **kwargs):
        nonlocal prepare_and_execute_calls
        prepare_and_execute_calls += 1
        task = asyncio.current_task()
        assert isinstance(task, asyncio.Task)
        handler_tasks.append(task)
        if prepare_and_execute_calls == 2:
            second_before_broker.set()
            await release_second_broker.wait()
        return await real_prepare_and_execute(self, *args, **kwargs)

    real_materialize = server_mod._ingest_materialize_secret_dir

    def count_materialize(**kwargs):
        nonlocal materialize_calls
        materialize_calls += 1
        return real_materialize(**kwargs)

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)
    monkeypatch.setattr(
        broker_mod.RunBroker,
        "prepare_and_execute",
        pause_successor_before_broker,
    )
    monkeypatch.setattr(
        server_mod,
        "_ingest_materialize_secret_dir",
        count_materialize,
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "shared"))

    app = server_mod.create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )
    secret_root = tmp_path / "shared" / "profiles" / "owner" / "tmp" / "ingest-secrets"

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer testkey"}
        payload_a = {
            "content": "hi",
            "idempotency_key": "async-claim-generation",
            "secrets": {"cms": {"type": "opaque", "value": "fingerprint-a"}},
        }
        payload_b = {
            **payload_a,
            "secrets": {"cms": {"type": "opaque", "value": "fingerprint-b"}},
        }
        try:
            first_request = asyncio.create_task(
                client.post(
                    "/api/run-broker/ingest/async",
                    json=payload_a,
                    headers=headers,
                )
            )
            await asyncio.wait_for(first_prepare_started.wait(), timeout=SYNC_TIMEOUT)

            successor_request = asyncio.create_task(
                client.post(
                    "/api/run-broker/ingest/async",
                    json=payload_a,
                    headers=headers,
                )
            )
            await asyncio.wait_for(second_before_broker.wait(), timeout=SYNC_TIMEOUT)

            handler_tasks[0].cancel()
            with pytest.raises((ServerDisconnectedError, ClientConnectionError)):
                await first_request
            await asyncio.wait_for(first_prepare_cancelled.wait(), timeout=SYNC_TIMEOUT)

            release_second_broker.set()
            await asyncio.wait_for(second_prepare_started.wait(), timeout=SYNC_TIMEOUT)
            assert materialize_calls == 0

            mismatch = await asyncio.wait_for(
                client.post(
                    "/api/run-broker/ingest/async",
                    json=payload_b,
                    headers=headers,
                ),
                timeout=1,
            )
            mismatch_body = await mismatch.json()

            release_second_prepare.set()
            successor = await successor_request
            successor_body = await successor.json()
            final_body = {}
            for _ in range(50):
                poll = await client.get(successor_body["poll_url"], headers=headers)
                final_body = await poll.json()
                if final_body["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            leftovers = list(secret_root.iterdir()) if secret_root.exists() else []
            return (
                mismatch.status,
                mismatch_body,
                successor.status,
                successor_body,
                final_body,
                leftovers,
            )
        finally:
            release_second_broker.set()
            release_second_prepare.set()
            await client.close()

    (
        mismatch_status,
        mismatch_body,
        successor_status,
        successor_body,
        final_body,
        leftovers,
    ) = asyncio.run(runner())

    assert mismatch_status == 409
    assert mismatch_body["ok"] is False
    assert successor_status == 202
    assert successor_body["duplicate"] is False
    assert final_body["status"] == "succeeded"
    assert prepare_calls == 2
    assert materialize_calls == 1
    assert dispatches == 1
    assert dispatched_values == ["fingerprint-a"]
    assert leftovers == []


def test_ingest_async_ignores_host_tools_opt_out_and_requires_sandbox(monkeypatch):
    calls = {"n": 0}
    prepares = {"n": 0}

    async def prepare(request):
        prepares["n"] += 1
        return request

    async def dispatch(request):
        calls["n"] += 1
        return f"async:{request.content}"

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from hermes_multitenancy import billing_identity
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.setattr(billing_identity, "prepare_billing_request", prepare)

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
    assert prepares["n"] == 0

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


def test_ingest_async_secrets_are_files_only_and_terminal_cleanup_redacts_poll(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import router as router_mod

    from pathlib import Path

    secret_value = "eyJhbGciOiJIUzI1NiJ9.async.full.jwt.secret"
    profile_home = tmp_path / "profiles" / "owner"
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: profile_home if profile_name == "owner" else tmp_path / profile_name,
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    captured: dict[str, object] = {}

    async def dispatch(request):
        secret_dir = Path(request.metadata["ingest_secret_dir"])
        captured["secret_dir"] = secret_dir
        captured["metadata_json"] = json.dumps(request.metadata, ensure_ascii=False)
        return f"async tool saw:{(secret_dir / 'cms_bearer').read_text(encoding='utf-8')}"

    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            submit = await client.post(
                "/api/run-broker/ingest/async",
                json={
                    "content": "查异步对账",
                    "secrets": {
                        "cms_bearer": {
                            "type": "bearer_token",
                            "value": secret_value,
                        }
                    },
                },
                headers={"Authorization": "Bearer testkey"},
            )
            submit_text = await submit.text()
            submit_body = json.loads(submit_text)
            final_body = {}
            poll_text = ""
            poll_status = 0
            for _ in range(20):
                poll = await client.get(
                    f"/api/run-broker/ingest/runs/{submit_body['run_id']}",
                    headers={"Authorization": "Bearer testkey"},
                )
                poll_status = poll.status
                poll_text = await poll.text()
                final_body = json.loads(poll_text)
                if final_body["status"] == "succeeded":
                    return submit.status, submit_text, poll_status, poll_text, final_body
                await asyncio.sleep(0.02)
            return submit.status, submit_text, poll_status, poll_text, final_body
        finally:
            await client.close()

    submit_status, submit_text, poll_status, poll_text, final_body = asyncio.run(runner())

    assert submit_status == 202
    assert poll_status == 200
    assert final_body["ok"] is True
    assert final_body["result"] == "async tool saw:[REDACTED:cms_bearer]"
    assert secret_value not in submit_text
    assert secret_value not in poll_text
    assert secret_value not in captured["metadata_json"]
    assert not captured["secret_dir"].exists()


def test_ingest_async_fake_bearer_dry_run_constructs_authorization_without_leak(
    monkeypatch,
    tmp_path,
):
    from hermes_multitenancy import router as router_mod

    from pathlib import Path

    secret_value = "eyJhbGciOiJIUzI1NiJ9.daryu.fake.jwt.signature"
    profile_home = tmp_path / "profiles" / "owner"
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: profile_home if profile_name == "owner" else tmp_path / profile_name,
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    captured: dict[str, object] = {}

    def fake_daryu_query(headers: dict[str, str]) -> dict[str, object]:
        auth = headers.get("Authorization") or ""
        captured["authorization_matches"] = auth == f"Bearer {secret_value}"
        captured["authorization_len"] = len(auth)
        captured["authorization_scheme"] = auth.split(" ", 1)[0] if auth else ""
        return {"ok": captured["authorization_matches"], "rows": 2}

    async def dispatch(request):
        secret_dir = Path(request.metadata["ingest_secret_dir"])
        bearer = (secret_dir / "cms_bearer").read_text(encoding="utf-8")
        response = fake_daryu_query({"Authorization": f"Bearer {bearer}"})
        assert response["ok"] is True
        return (
            "mock daryu query ok "
            f"rows={response['rows']} "
            f"auth_scheme={captured['authorization_scheme']} "
            f"auth_len={captured['authorization_len']}"
        )

    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            submit = await client.post(
                "/api/run-broker/ingest/async",
                json={
                    "content": "查 2026-06-01 到 2026-06-22 的对账数据",
                    "secrets": {
                        "cms_bearer": {
                            "type": "bearer_token",
                            "value": secret_value,
                        }
                    },
                },
                headers={"Authorization": "Bearer testkey"},
            )
            submit_text = await submit.text()
            submit_body = json.loads(submit_text)
            poll_text = ""
            final_body = {}
            poll_status = 0
            for _ in range(20):
                poll = await client.get(
                    f"/api/run-broker/ingest/runs/{submit_body['run_id']}",
                    headers={"Authorization": "Bearer testkey"},
                )
                poll_status = poll.status
                poll_text = await poll.text()
                final_body = json.loads(poll_text)
                if final_body["status"] == "succeeded":
                    return submit.status, submit_text, poll_status, poll_text, final_body
                await asyncio.sleep(0.02)
            return submit.status, submit_text, poll_status, poll_text, final_body
        finally:
            await client.close()

    submit_status, submit_text, poll_status, poll_text, final_body = asyncio.run(runner())

    assert submit_status == 202
    assert poll_status == 200
    assert final_body["ok"] is True
    assert final_body["status"] == "succeeded"
    assert final_body["result"].startswith("mock daryu query ok rows=2")
    assert "auth_scheme=Bearer" in final_body["result"]
    assert captured["authorization_matches"] is True
    assert secret_value not in submit_text
    assert secret_value not in poll_text
    assert secret_value[:16] not in poll_text
    assert f"Bearer {secret_value}" not in poll_text


def test_ingest_async_default_dispatch_streams_terminal_with_secret_env_without_leak(
    monkeypatch,
    tmp_path,
):
    from contextlib import contextmanager
    from pathlib import Path

    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    secret_value = "eyJhbGciOiJIUzI1NiJ9.default.dispatch.fake.jwt.signature"
    result_text = f"mock daryu terminal dry-run auth_len={len('Bearer ' + secret_value)}"
    profile_home = tmp_path / "profiles" / "owner"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: profile_home if profile_name == "owner" else tmp_path / profile_name,
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setenv("HERMES_AIAGENT_FIRST_EVENT_HEARTBEAT_SECONDS", "0")
    monkeypatch.setenv("HERMES_AIAGENT_WAIT_HEARTBEAT_SECONDS", "0")
    captured: dict[str, object] = {}

    @contextmanager
    def fake_lark_scope(_profile_home, _sender_open_id):
        yield {}

    monkeypatch.setattr(agent_real, "_lark_cli_auth_broker_scope", fake_lark_scope)

    class FakeStdin:
        def write(self, payload):
            captured["child_payload"] = json.loads(payload.decode("utf-8"))

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class FakeStdout:
        def __init__(self):
            self.lines = [
                json.dumps(
                    {
                        "event": "tool_started",
                        "name": "terminal",
                        "preview": "python daryu_dry_run.py --secret cms_bearer",
                        "args": {
                            "command": (
                                "python daryu_dry_run.py --secret cms_bearer "
                                "--emit auth_len only"
                            )
                        },
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                + b"\n",
                json.dumps(
                    {"event": "content", "text": result_text},
                    ensure_ascii=False,
                ).encode("utf-8")
                + b"\n",
                json.dumps(
                    {"event": "done", "result": result_text, "error": None},
                    ensure_ascii=False,
                ).encode("utf-8")
                + b"\n",
            ]

        async def readline(self):
            if self.lines:
                return self.lines.pop(0)
            return b""

    class FakeStderr:
        async def read(self):
            return b""

    class FakeProc:
        def __init__(self):
            self.stdin = FakeStdin()
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()
            self.pid = 4242
            self.returncode = None

        async def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        env = dict(kwargs["env"])
        secret_dir = Path(env["HERMES_INGEST_SECRET_DIR"])
        captured["cmd"] = list(cmd)
        captured["cwd"] = str(kwargs.get("cwd") or "")
        captured["env"] = env
        captured["secret_file_value"] = (secret_dir / "cms_bearer").read_text(
            encoding="utf-8"
        )
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    app = create_run_broker_app(
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            submit = await client.post(
                "/api/run-broker/ingest/async",
                json={
                    "content": "查 2026-06-01 到 2026-06-22 的对账数据",
                    "interactive": True,
                    "secrets": {
                        "cms_bearer": {
                            "type": "bearer_token",
                            "value": secret_value,
                        }
                    },
                },
                headers={"Authorization": "Bearer testkey"},
            )
            submit_text = await submit.text()
            submit_body = json.loads(submit_text)
            poll_text = ""
            final_body = {}
            poll_status = 0
            for _ in range(40):
                poll = await client.get(
                    f"/api/run-broker/ingest/runs/{submit_body['run_id']}",
                    headers={"Authorization": "Bearer testkey"},
                )
                poll_status = poll.status
                poll_text = await poll.text()
                final_body = json.loads(poll_text)
                if final_body["status"] == "succeeded":
                    return submit.status, submit_text, poll_status, poll_text, final_body
                await asyncio.sleep(0.02)
            return submit.status, submit_text, poll_status, poll_text, final_body
        finally:
            await client.close()

    submit_status, submit_text, poll_status, poll_text, final_body = asyncio.run(runner())

    assert "env" in captured, {"captured": captured, "final_body": final_body}
    env = captured["env"]
    child_payload = captured["child_payload"]
    child_event = child_payload["event"]
    child_metadata = child_event["raw_event"]["metadata"]
    secret_dir = Path(env["HERMES_INGEST_SECRET_DIR"])
    manifest = json.loads(env["HERMES_INGEST_SECRET_MANIFEST"])

    assert submit_status == 202
    assert poll_status == 200
    assert final_body["ok"] is True
    assert final_body["status"] == "succeeded"
    assert final_body["result"] == result_text
    assert captured["secret_file_value"] == secret_value
    assert child_metadata["source"] == "ingest"
    assert child_metadata["ingest_secret_dir"] == env["HERMES_INGEST_SECRET_DIR"]
    assert child_metadata["ingest_secrets"] == manifest
    assert manifest == [
        {
            "name": "cms_bearer",
            "type": "bearer_token",
            "usage": "Authorization Bearer",
        }
    ]
    assert "cms_bearer" in child_event["text"]
    assert "Authorization Bearer" in child_event["text"]
    assert "terminal or execute_code" in child_event["text"]
    assert secret_value not in submit_text
    assert secret_value not in poll_text
    assert secret_value not in json.dumps(child_payload, ensure_ascii=False)
    assert secret_value not in json.dumps(env, ensure_ascii=False)
    assert f"Bearer {secret_value}" not in poll_text
    assert not secret_dir.exists()


def test_ingest_async_classifies_agent_max_iterations_in_poll_and_logs_run_id(
    monkeypatch,
    caplog,
):
    async def failing_dispatch(_request):
        raise RuntimeError("AIAgent subprocess failed: max_iterations_reached(45/45)")

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=failing_dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            submit = await client.post(
                "/api/run-broker/ingest/async",
                json={"content": "trigger max iteration"},
                headers={"Authorization": "Bearer testkey"},
            )
            submit_body = json.loads(await submit.text())
            final_body = {}
            for _ in range(20):
                poll = await client.get(
                    f"/api/run-broker/ingest/runs/{submit_body['run_id']}",
                    headers={"Authorization": "Bearer testkey"},
                )
                final_body = json.loads(await poll.text())
                if final_body["status"] == "failed":
                    return submit.status, submit_body["run_id"], poll.status, final_body
                await asyncio.sleep(0.02)
            return submit.status, submit_body["run_id"], 0, final_body
        finally:
            await client.close()

    with caplog.at_level(logging.ERROR, logger="hermes_multitenancy.webui_broker_server"):
        submit_status, run_id, poll_status, final_body = asyncio.run(runner())

    assert submit_status == 202
    assert poll_status == 200
    assert final_body["ok"] is False
    assert final_body["status"] == "failed"
    assert final_body["error"].startswith("agent_max_iterations:")
    assert "internal error" not in final_body["error"]
    assert run_id in caplog.text
    assert "session_id=" in caplog.text
    assert "agent_max_iterations" in caplog.text


def test_ingest_async_redacts_secret_before_error_truncation(monkeypatch):
    secret_value = "eyJ" + ("A" * 520) + ".signature"

    async def failing_dispatch(_request):
        raise RuntimeError(
            "upstream rejected Authorization: Bearer "
            + secret_value
            + " after request"
        )

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=failing_dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            submit = await client.post(
                "/api/run-broker/ingest/async",
                json={
                    "content": "trigger secret error",
                    "secrets": {
                        "cms_bearer": {
                            "type": "bearer_token",
                            "value": secret_value,
                        }
                    },
                },
                headers={"Authorization": "Bearer testkey"},
            )
            submit_body = json.loads(await submit.text())
            for _ in range(20):
                poll = await client.get(
                    f"/api/run-broker/ingest/runs/{submit_body['run_id']}",
                    headers={"Authorization": "Bearer testkey"},
                )
                body_text = await poll.text()
                body = json.loads(body_text)
                if body["status"] == "failed":
                    return body_text, body
                await asyncio.sleep(0.02)
            return "", {}
        finally:
            await client.close()

    body_text, body = asyncio.run(runner())

    assert body["error"].startswith("agent_runtime_error:")
    assert "[REDACTED:cms_bearer]" in body["error"]
    assert secret_value not in body_text
    assert secret_value[:40] not in body_text
    assert "Authorization: Bearer eyJ" not in body_text


def test_ingest_async_redacts_jwt_like_error_even_without_declared_secret(
    monkeypatch,
    caplog,
):
    leaked_token = "eyJ" + ("B" * 80) + ".payload.signature"

    async def failing_dispatch(_request):
        raise RuntimeError(f"upstream Authorization: Bearer {leaked_token}")

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=failing_dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            submit = await client.post(
                "/api/run-broker/ingest/async",
                json={"content": "content only error"},
                headers={"Authorization": "Bearer testkey"},
            )
            submit_body = json.loads(await submit.text())
            for _ in range(20):
                poll = await client.get(
                    f"/api/run-broker/ingest/runs/{submit_body['run_id']}",
                    headers={"Authorization": "Bearer testkey"},
                )
                body_text = await poll.text()
                body = json.loads(body_text)
                if body["status"] == "failed":
                    return body_text, body
                await asyncio.sleep(0.02)
            return "", {}
        finally:
            await client.close()

    with caplog.at_level(logging.ERROR, logger="hermes_multitenancy.webui_broker_server"):
        body_text, body = asyncio.run(runner())

    assert body["error"].startswith("agent_runtime_error:")
    assert "[REDACTED:authorization]" in body["error"]
    assert leaked_token not in body_text
    assert leaked_token[:40] not in body_text
    assert leaked_token not in caplog.text
    assert leaked_token[:40] not in caplog.text
    assert "Traceback" not in caplog.text


def test_ingest_async_redacts_opaque_secret_prefix_preview(monkeypatch, caplog):
    secret_value = "sk-live-1234567890abcdef"
    leaked_prefix = secret_value[:10]

    async def failing_dispatch(_request):
        raise RuntimeError(f"upstream rejected token prefix {leaked_prefix}")

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=failing_dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            submit = await client.post(
                "/api/run-broker/ingest/async",
                json={
                    "content": "opaque prefix failure",
                    "secrets": {
                        "cms_api_key": {
                            "type": "api_key",
                            "value": secret_value,
                        }
                    },
                },
                headers={"Authorization": "Bearer testkey"},
            )
            submit_body = json.loads(await submit.text())
            for _ in range(20):
                poll = await client.get(
                    f"/api/run-broker/ingest/runs/{submit_body['run_id']}",
                    headers={"Authorization": "Bearer testkey"},
                )
                body_text = await poll.text()
                body = json.loads(body_text)
                if body["status"] == "failed":
                    return body_text, body
                await asyncio.sleep(0.02)
            return "", {}
        finally:
            await client.close()

    with caplog.at_level(logging.ERROR, logger="hermes_multitenancy.webui_broker_server"):
        body_text, body = asyncio.run(runner())

    assert body["error"].startswith("agent_runtime_error:")
    assert "[REDACTED:cms_api_key:prefix]" in body["error"]
    assert leaked_prefix not in body_text
    assert leaked_prefix not in caplog.text
    assert secret_value not in body_text
    assert secret_value not in caplog.text


def test_ingest_async_ignores_caller_supplied_secret_metadata(monkeypatch, tmp_path):
    from hermes_multitenancy import webui_broker_server as broker_mod

    captured: dict[str, object] = {}

    async def dispatch(request):
        event = broker_mod._build_webui_event(request)
        captured["metadata"] = dict(request.metadata)
        captured["event_text"] = event.text
        captured["raw_event_metadata"] = dict(event.raw_event["metadata"])
        return "ok"

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            submit = await client.post(
                "/api/run-broker/ingest/async",
                json={
                    "content": "hi",
                    "metadata": {
                        "trace": "t1",
                        "ingest_secret_dir": str(tmp_path / "fake-secrets"),
                        "ingest_secrets": [
                            {"name": "fake", "type": "opaque", "usage": "spoofed"}
                        ],
                    },
                },
                headers={"Authorization": "Bearer testkey"},
            )
            submit_body = json.loads(await submit.text())
            poll_body = {}
            poll_status = 0
            for _ in range(20):
                poll = await client.get(
                    f"/api/run-broker/ingest/runs/{submit_body['run_id']}",
                    headers={"Authorization": "Bearer testkey"},
                )
                poll_status = poll.status
                poll_body = json.loads(await poll.text())
                if poll_body["status"] == "succeeded":
                    return submit.status, poll_status, poll_body
                await asyncio.sleep(0.02)
            return submit.status, poll_status, poll_body
        finally:
            await client.close()

    submit_status, poll_status, poll_body = asyncio.run(runner())

    assert submit_status == 202
    assert poll_status == 200
    assert poll_body["ok"] is True
    assert captured["metadata"]["trace"] == "t1"
    assert "ingest_secret_dir" not in captured["metadata"]
    assert "ingest_secrets" not in captured["metadata"]
    assert "ingest_secret_dir" not in captured["raw_event_metadata"]
    assert "ingest_secrets" not in captured["raw_event_metadata"]
    assert captured["event_text"] == "hi"


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
            body = {
                "content": "same work",
                "idempotency_key": "same-key",
                "secrets": {"cms": {"type": "opaque", "value": "same-secret"}},
            }
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


def test_ingest_async_same_idempotency_with_different_secret_fingerprint_is_409(monkeypatch):
    release = asyncio.Event()
    calls = {"n": 0}

    async def slow_dispatch(request):
        calls["n"] += 1
        await release.wait()
        return f"async:{request.content}"

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")

    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=slow_dispatch,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            base = {"content": "same work", "idempotency_key": "same-key"}
            first = await client.post(
                "/api/run-broker/ingest/async",
                json={**base, "secrets": {"cms": {"type": "opaque", "value": "old"}}},
                headers={"Authorization": "Bearer testkey"},
            )
            first_body = json.loads(await first.text())
            await asyncio.sleep(0.01)
            second = await client.post(
                "/api/run-broker/ingest/async",
                json={**base, "secrets": {"cms": {"type": "opaque", "value": "new"}}},
                headers={"Authorization": "Bearer testkey"},
            )
            second_body = json.loads(await second.text())
            release.set()
            return first.status, first_body, second.status, second_body
        finally:
            await client.close()

    first_status, first_body, second_status, second_body = asyncio.run(runner())

    assert first_status == 202
    assert first_body["run_id"].startswith("ing_")
    assert second_status == 409
    assert second_body["error"] == "secret_mismatch"
    assert calls["n"] == 1


def test_ingest_async_idempotency_is_scoped_by_ingest_caller(monkeypatch, tmp_path):
    keys_file = tmp_path / "ingest-keys.json"
    keys_file.write_text(
        json.dumps(
            {
                "keys": [
                    {"token": "caller-a", "profile": "owner", "name": "caller a"},
                    {"token": "caller-b", "profile": "owner", "name": "caller b"},
                ]
            }
        ),
        encoding="utf-8",
    )
    release = asyncio.Event()
    calls = {"n": 0}
    seen_keys: set[str] = set()

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen_keys:
            return False
        seen_keys.add(key)
        return True

    async def slow_dispatch(request):
        calls["n"] += 1
        await release.wait()
        return f"async-{calls['n']}:{request.content}"

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.delenv("HERMES_INGEST_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE", str(keys_file))

    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=slow_dispatch,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )
    body = {
        "content": "same work",
        "idempotency_key": "same-key",
        "secrets": {"cms": {"type": "opaque", "value": "same-secret"}},
    }

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            first = await client.post(
                "/api/run-broker/ingest/async",
                json=body,
                headers={"Authorization": "Bearer caller-a"},
            )
            first_body = json.loads(await first.text())
            second = await client.post(
                "/api/run-broker/ingest/async",
                json=body,
                headers={"Authorization": "Bearer caller-b"},
            )
            second_body = json.loads(await second.text())
            release.set()
            return first.status, first_body, second.status, second_body
        finally:
            await client.close()

    first_status, first_body, second_status, second_body = asyncio.run(runner())

    assert first_status == 202
    assert first_body["duplicate"] is False
    assert second_status == 202
    assert second_body["duplicate"] is False
    assert second_body["run_id"] != first_body["run_id"]
    assert calls["n"] == 2


def test_ingest_async_secret_mismatch_is_scoped_by_ingest_caller(monkeypatch, tmp_path):
    keys_file = tmp_path / "ingest-keys.json"
    keys_file.write_text(
        json.dumps(
            {
                "keys": [
                    {"token": "caller-a", "profile": "owner", "name": "caller a"},
                    {"token": "caller-b", "profile": "owner", "name": "caller b"},
                ]
            }
        ),
        encoding="utf-8",
    )
    release = asyncio.Event()
    calls = {"n": 0}
    seen_keys: set[str] = set()

    def mark_seen(request):
        key = request.effective_idempotency_key
        if key in seen_keys:
            return False
        seen_keys.add(key)
        return True

    async def slow_dispatch(request):
        calls["n"] += 1
        await release.wait()
        return f"async-{calls['n']}:{request.content}"

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.delenv("HERMES_INGEST_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE", str(keys_file))

    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=slow_dispatch,
        mark_seen=mark_seen,
        sandbox_available=lambda: True,
    )
    base = {"content": "same work", "idempotency_key": "same-key"}

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            first = await client.post(
                "/api/run-broker/ingest/async",
                json={**base, "secrets": {"cms": {"type": "opaque", "value": "old"}}},
                headers={"Authorization": "Bearer caller-a"},
            )
            first_body = json.loads(await first.text())
            second = await client.post(
                "/api/run-broker/ingest/async",
                json={**base, "secrets": {"cms": {"type": "opaque", "value": "new"}}},
                headers={"Authorization": "Bearer caller-b"},
            )
            second_body = json.loads(await second.text())
            release.set()
            return first.status, first_body, second.status, second_body
        finally:
            await client.close()

    first_status, first_body, second_status, second_body = asyncio.run(runner())

    assert first_status == 202
    assert first_body["duplicate"] is False
    assert second_status == 202
    assert second_body["duplicate"] is False
    assert second_body["run_id"] != first_body["run_id"]
    assert calls["n"] == 2


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
