"""Tests for the webui lark-cli credential-expiry -> auth_required -> replay path.

Covers four seams:
  1. lark sidecar invokes ``credential_expiry_sink`` on CredentialExpiredError
     (and NOT on the transient "credential unavailable" branch).
  2. ``_default_dispatch_agent`` emits an ``auth_required`` RunEvent carrying the
     ``signal_run_id`` when the stream yields one (and nothing when it doesn't).
  3. ``handle_run`` parks the original inbound payload keyed by a signal_run_id.
  4. the bearer-protected replay endpoint re-dispatches on a valid owned id, and
     rejects missing bearer (401), unknown/expired id (404), wrong tenant (403).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_multitenancy import webui_broker_server as broker
from hermes_multitenancy.lark_cli_auth_broker import (
    BrokerResponse,
    CredentialExpiredError,
    LarkCliAuthBroker,
    LarkCliAuthBrokerContext,
)
from hermes_multitenancy.run_models import RunEvent, RunRequest


# --------------------------------------------------------------------------- #
# 1. lark sidecar credential_expiry_sink
# --------------------------------------------------------------------------- #
def _sign(key: str, *, timestamp: str, body_sha: str) -> str:
    canonical = "\n".join(
        [
            "v1",
            "GET",
            "open.feishu.cn",
            "/open-apis/authen/v1/user_info",
            body_sha,
            timestamp,
            "user",
            "Authorization",
        ]
    )
    return hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _signed_headers(key: str) -> dict[str, str]:
    body_sha = hashlib.sha256(b"").hexdigest()
    timestamp = str(int(time.time()))
    return {
        "X-Lark-Proxy-Version": "v1",
        "X-Lark-Proxy-Target": "https://open.feishu.cn",
        "X-Lark-Proxy-Identity": "user",
        "X-Lark-Proxy-Auth-Header": "Authorization",
        "X-Lark-Proxy-Signature": _sign(key, timestamp=timestamp, body_sha=body_sha),
        "X-Lark-Proxy-Timestamp": timestamp,
        "X-Lark-Body-SHA256": body_sha,
    }


def _sink_broker(tmp_path: Path, recorder: list[dict]) -> LarkCliAuthBroker:
    return LarkCliAuthBroker(
        LarkCliAuthBrokerContext(
            shared_home=tmp_path / ".hermes",
            profile_name="alice",
            user_open_id="ou_alice",
            hmac_key="proxy-key",
            allowed_identities=frozenset({"user"}),
            credential_expiry_sink=recorder.append,
        ),
        forwarder=lambda *a, **k: BrokerResponse(status=200, body=b"{}"),
    )


def test_sink_fires_on_credential_expired(monkeypatch, tmp_path: Path) -> None:
    recorder: list[dict] = []
    b = _sink_broker(tmp_path, recorder)
    monkeypatch.setattr(
        b, "_resolve_token", lambda identity: (_ for _ in ()).throw(CredentialExpiredError("x"))
    )

    resp = b.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers=_signed_headers("proxy-key"),
        body=b"",
    )

    assert resp.status == 503
    assert recorder == [{"provider": "feishu", "connector_id": "lark-cli"}]


def test_sink_not_fired_on_transient_unavailable(monkeypatch, tmp_path: Path) -> None:
    recorder: list[dict] = []
    b = _sink_broker(tmp_path, recorder)
    monkeypatch.setattr(
        b, "_resolve_token", lambda identity: (_ for _ in ()).throw(PermissionError("credential unavailable"))
    )
    # Skip the real backoff sleeps.
    monkeypatch.setattr("hermes_multitenancy.lark_cli_auth_broker.time.sleep", lambda *a, **k: None)

    resp = b.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers=_signed_headers("proxy-key"),
        body=b"",
    )

    assert resp.status == 503  # terminal transient result
    assert recorder == []  # but the re-auth sink must NOT have fired


# --------------------------------------------------------------------------- #
# 2. _default_dispatch_agent emits auth_required carrying run_id
# --------------------------------------------------------------------------- #
async def _run_dispatch_with_stream(monkeypatch, tmp_path: Path, frames: list[tuple[str, Any]]):
    from hermes_multitenancy import agent_real, router
    from hermes_multitenancy import runtime  # noqa: F401 - ensures _PROFILE_HOME_VAR exists

    async def fake_stream(event, profile_home, *, messages=None):
        for kind, payload in frames:
            yield kind, payload

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)
    monkeypatch.setattr(router, "_profile_name_to_home", lambda name: tmp_path)
    # Identity media passthrough so no router media resolution runs.
    monkeypatch.setattr(
        broker, "_webui_streamable_media_text", lambda text, **kw: ("", [text] if text else [])
    )

    emitted: list[RunEvent] = []

    async def capture(event: RunEvent) -> None:
        emitted.append(event)

    req = RunRequest(channel="webui", profile_name="alice", user_key="ou_alice", content="hi")
    result = await broker._default_dispatch_agent(
        req, emit_event=capture, auth_signal_run_id="sig-abc"
    )
    return emitted, result


def test_dispatch_emits_auth_required_with_run_id(monkeypatch, tmp_path: Path) -> None:
    emitted, _ = asyncio.run(
        _run_dispatch_with_stream(
            monkeypatch,
            tmp_path,
            [
                ("content", "hi"),
                ("auth_required", {"provider": "feishu", "connector_id": "lark-cli"}),
            ],
        )
    )

    auth_events = [e for e in emitted if e.kind == "auth_required"]
    assert len(auth_events) == 1
    assert auth_events[0].payload["run_id"] == "sig-abc"
    assert auth_events[0].payload["connector_id"] == "lark-cli"


def test_dispatch_emits_no_auth_required_on_normal_run(monkeypatch, tmp_path: Path) -> None:
    emitted, _ = asyncio.run(
        _run_dispatch_with_stream(
            monkeypatch,
            tmp_path,
            [("content", "hi")],
        )
    )

    assert [e.kind for e in emitted if e.kind == "auth_required"] == []
    assert any(e.kind == "content" for e in emitted)


# --------------------------------------------------------------------------- #
# 3 + 4. stash + replay endpoint (HTTP seam)
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clear_auth_signal_store():
    broker._auth_signal_store.clear()
    yield
    broker._auth_signal_store.clear()


def _app(dispatch=None):
    async def echo(request):
        return f"echo:{request.content}"

    return broker.create_run_broker_app(
        dispatch_agent=dispatch or echo,
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )


def test_auth_required_run_retains_stash_with_original_payload(monkeypatch, tmp_path) -> None:
    """A run that emits auth_required retains its parked request (keyed by the
    signal_run_id) with the original payload + tenant identity — so replay can
    find it after the user re-auths. Uses the real _default_dispatch_agent path
    with a stream that yields auth_required."""
    from aiohttp.test_utils import TestClient, TestServer
    from hermes_multitenancy import agent_real, router

    async def fake_stream(event, profile_home, *, messages=None):
        yield "content", "working…"
        yield "auth_required", {"provider": "feishu", "connector_id": "lark-cli"}

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)
    monkeypatch.setattr(router, "_profile_name_to_home", lambda name: tmp_path)
    monkeypatch.setattr(
        broker, "_webui_streamable_media_text", lambda text, **kw: ("", [text] if text else [])
    )

    async def _body() -> None:
        # dispatch_agent=None → real _default_dispatch_agent path (emits auth_required)
        app = broker.create_run_broker_app(
            mark_seen=lambda _request: True, sandbox_available=lambda: True
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/run-broker/runs",
                json={
                    "channel": "webui",
                    "profile_name": "alice",
                    "user_key": "ou_alice",
                    "content": "把上周销售额导出到飞书表格",
                },
            )
            await resp.text()
        finally:
            await client.close()

    asyncio.run(_body())

    assert len(broker._auth_signal_store) == 1
    entry = next(iter(broker._auth_signal_store.values()))
    assert entry["payload"]["content"] == "把上周销售额导出到飞书表格"
    assert entry["profile_name"] == "alice"
    assert entry["subject"] == "ou_alice"


def test_normal_run_does_not_retain_stash() -> None:
    """Churn guard (codex MEDIUM): a run that does NOT signal auth_required must
    consume its own speculative stash entry, so normal traffic never fills the
    bounded store and can't evict a genuinely-pending re-auth request."""
    from aiohttp.test_utils import TestClient, TestServer

    async def _body() -> None:
        client = TestClient(TestServer(_app()))  # echo dispatch, no auth_required
        await client.start_server()
        try:
            resp = await client.post(
                "/api/run-broker/runs",
                json={"channel": "webui", "profile_name": "alice", "user_key": "ou_alice", "content": "hi"},
            )
            await resp.text()
        finally:
            await client.close()

    asyncio.run(_body())
    assert len(broker._auth_signal_store) == 0


def test_replay_redispatches_owned_stashed_request() -> None:
    from aiohttp.test_utils import TestClient, TestServer

    seen: list[RunRequest] = []

    async def dispatch(request):
        seen.append(request)
        return f"replayed:{request.content}"

    broker._auth_signal_stash(
        "sig-1",
        payload={
            "channel": "webui",
            "profile_name": "alice",
            "user_key": "ou_alice",
            "content": "原始请求",
            # Original run's idempotency key — must NOT be reused on replay, or the
            # broker dedup would swallow the deliberate re-run (the critical bug).
            "idempotency_key": "webui:sess-1:resp_run_abc",
        },
        profile_name="alice",
        subject="ou_alice",
    )

    status_body: dict[str, Any] = {}

    async def _body() -> None:
        client = TestClient(TestServer(_app(dispatch)))
        await client.start_server()
        try:
            resp = await client.post("/api/run-broker/credentials/replay/sig-1")
            status_body["status"] = resp.status
            status_body["body"] = await resp.text()
        finally:
            await client.close()

    asyncio.run(_body())

    assert status_body["status"] == 200
    assert len(seen) == 1
    assert seen[0].content == "原始请求"
    assert seen[0].profile_name == "alice"
    # CRITICAL: replay must drop the original idempotency_key so the broker's
    # admit()-time dedup can't treat the deliberate re-run as a duplicate.
    assert seen[0].idempotency_key is None
    assert '"kind": "done"' in status_body["body"]
    # one-shot: the consumed entry is gone (a fresh one for the replay run may exist)
    assert "sig-1" not in broker._auth_signal_store


def test_replay_missing_bearer_is_401(monkeypatch) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "secret-key")
    broker._auth_signal_stash(
        "sig-1", payload={"content": "x"}, profile_name="alice", subject="ou_alice"
    )

    status: dict[str, int] = {}

    async def _body() -> None:
        client = TestClient(TestServer(_app()))
        await client.start_server()
        try:
            resp = await client.post("/api/run-broker/credentials/replay/sig-1")
            await resp.read()
            status["code"] = resp.status
        finally:
            await client.close()

    asyncio.run(_body())
    assert status["code"] == 401


def test_replay_unknown_signal_run_id_is_404() -> None:
    from aiohttp.test_utils import TestClient, TestServer

    status: dict[str, int] = {}

    async def _body() -> None:
        client = TestClient(TestServer(_app()))
        await client.start_server()
        try:
            resp = await client.post("/api/run-broker/credentials/replay/does-not-exist")
            await resp.read()
            status["code"] = resp.status
        finally:
            await client.close()

    asyncio.run(_body())
    assert status["code"] == 404


def test_replay_wrong_tenant_is_403(monkeypatch) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    # Caller resolves to a DIFFERENT profile than the one that owns the stash.
    monkeypatch.setattr(broker, "_resolve_owner_scoped_profile", lambda request, payload: ("profileB", None))

    seen: list[RunRequest] = []

    async def dispatch(request):
        seen.append(request)
        return ""

    broker._auth_signal_stash(
        "sig-1",
        payload={"channel": "webui", "profile_name": "profileA", "user_key": "ou_a", "content": "x"},
        profile_name="profileA",
        subject="ou_a",
    )

    status: dict[str, int] = {}

    async def _body() -> None:
        client = TestClient(TestServer(_app(dispatch)))
        await client.start_server()
        try:
            resp = await client.post("/api/run-broker/credentials/replay/sig-1")
            await resp.read()
            status["code"] = resp.status
        finally:
            await client.close()

    asyncio.run(_body())

    assert status["code"] == 403
    assert seen == []  # never dispatched
    assert "sig-1" in broker._auth_signal_store  # not consumed on rejection


def test_replay_owner_enforcement_no_header_is_403(monkeypatch) -> None:
    """Prod chat-plane posture: owner enforcement ON + no owner header must be
    rejected at the resolution_error gate — an identity-less caller can NOT
    replay a tenant-owned entry even with a valid signal_run_id + bearer.
    Locks the fail-closed boundary this slug's cross-review flagged."""
    from aiohttp.test_utils import TestClient, TestServer

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")

    seen: list[RunRequest] = []

    async def dispatch(request):
        seen.append(request)
        return ""

    broker._auth_signal_stash(
        "sig-1",
        payload={"channel": "webui", "profile_name": "alice", "user_key": "ou_alice", "content": "x"},
        profile_name="alice",
        subject="ou_alice",
    )

    status: dict[str, int] = {}

    async def _body() -> None:
        client = TestClient(TestServer(_app(dispatch)))
        await client.start_server()
        try:
            # No X-Hermes-Owner-Open-Id header → resolution_error under enforcement.
            resp = await client.post("/api/run-broker/credentials/replay/sig-1")
            await resp.read()
            status["code"] = resp.status
        finally:
            await client.close()

    asyncio.run(_body())

    assert status["code"] == 403
    assert seen == []  # never dispatched
    assert "sig-1" in broker._auth_signal_store  # not consumed on rejection
