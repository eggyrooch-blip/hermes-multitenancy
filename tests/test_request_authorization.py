"""Inline `request_authorization` (TRAE-style) end-to-end contract tests.

The whole point of this feature is that the model can ask the human owner for a
credential IN PLACE — the tool call blocks, a card is shown, the human authorizes
in the browser, and the SAME tool call returns. That makes every leg of the chain
a security boundary, so these tests drive the real chain end to end:

    request_authorization tool (child)
        -> _configure_webui_authorization_bridge (child, file-poll blocking wait)
        -> _register_pending_authorization       (broker, server-issued id + binding)
        -> POST /api/run-broker/authorization/<id>/{authorize,confirm,cancel}
        -> live credential probe (never a cached/file-only reader)
        -> response file -> the blocked tool call returns

Nothing here asserts on a mock of the thing under test: the broker registry, the
sanitizer, the aiohttp routes and the child-side bridge are all the production
objects. Only the OUTERMOST leaves are stubbed — the live credential probe (it
would hit Feishu/Keep over the network) and `tools.approval` (it ships with
hermes-agent core, not with this plugin).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

MASTER_KEY = "master-key-for-webui"
OWNER = "ou_alice"
PEER = "ou_bob"
PROFILE = "alice_profile"
PEER_PROFILE = "bob_profile"
SESSION = "webui-session-1"
SESSION_KEY = "multitenancy:webui:alice_profile:unknown:ou_alice"
RUN_ID = "run-signal-for-alice"
TOOL_CALL_ID = "call_abc123"


# What the Feishu app is actually granted, i.e. the server-side scope policy
# `normalize_requested_scopes` allow-lists against. Stubbed because reading it
# for real is a network hop to Feishu (`_app_granted_scope_names`) — a leaf, not
# the thing under test.
APP_GRANTED_SCOPES = "docx:document:readonly im:message im:message:send_as_bot read offline_access"


@pytest.fixture(autouse=True)
def _clean_module_state():
    from hermes_multitenancy import request_authorization_tool as rat
    from hermes_multitenancy.webui_broker import periphery

    def _reset() -> None:
        rat._authorization_bridges.clear()
        rat._inflight_sessions.clear()
        periphery._pending_authorizations.clear()
        periphery._authorization_live_runs.clear()
        periphery._authorization_flow_locks.clear()
        periphery._auth_signal_consume(RUN_ID)

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _authorization_server_policy(monkeypatch, tmp_path: Path):
    """Per-test rendezvous root + the app's granted scope set.

    The root is what the broker derives every per-run response directory from,
    so pinning it here keeps the whole file off `~/.hermes`.
    """
    from hermes_multitenancy import feishu_uat_auth

    monkeypatch.setenv("HERMES_SHARED_HOME", str(tmp_path))
    monkeypatch.setenv(
        "HERMES_MULTITENANCY_AUTHORIZATION_ROOT", str(tmp_path / "authorization-root")
    )
    monkeypatch.setattr(
        feishu_uat_auth, "login_oauth_scope", lambda **_kw: APP_GRANTED_SCOPES
    )


def _install_fake_approval_session(monkeypatch, session_key: str) -> None:
    """Install the core `tools.approval` seam the tool reads its session from.

    Plain holder rather than a ContextVar: the tool handler runs on the tool
    loop's own thread here (as it does in core's tool_executor), and a
    ContextVar set on the test thread would not be visible there.
    """
    import contextvars

    holder = {"key": session_key}
    tool_call_var = contextvars.ContextVar("approval_tool_call_id", default=TOOL_CALL_ID)

    fake_approval = SimpleNamespace(
        _approval_tool_call_id=tool_call_var,
        get_current_session_key=lambda: holder["key"],
        set_current_session_key=lambda key: holder.update(key=key),
        reset_current_session_key=lambda _token: holder.update(key=session_key),
        register_gateway_notify=lambda *_a, **_k: None,
        unregister_gateway_notify=lambda *_a, **_k: None,
        resolve_gateway_approval=lambda *_a, **_k: 1,
    )
    tools_mod = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setattr(tools_mod, "approval", fake_approval, raising=False)
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.approval", fake_approval)


def _run_request(*, session_id: str = SESSION, profile: str = PROFILE, owner: str = OWNER):
    from hermes_multitenancy.run_models import RunRequest

    return RunRequest(
        channel="webui",
        profile_name=profile,
        user_key=owner,
        content="please read my Feishu docs",
        session_id=session_id,
    )


def _configure_bridge(monkeypatch, tmp_path: Path, *, timeout: str = "20", run_id: str = RUN_ID):
    """Real child-side bridge, wired to a real event sink + the real broker.

    The child's directory is not invented here: it is the very path the broker
    derives from this run id, which is how the two sides agree without the child
    ever sending a path up.
    """
    from hermes_multitenancy.agent_real import _core
    from hermes_multitenancy.webui_broker import periphery

    rendezvous = periphery._authorization_response_dir(run_id)
    rendezvous.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_MULTITENANCY_AUTHORIZATION_DIR", str(rendezvous))
    monkeypatch.setenv("HERMES_MULTITENANCY_AUTHORIZATION_TIMEOUT", timeout)
    return _core._configure_webui_authorization_bridge


def _broker_sink(events: list, decisions: list, run_request):
    """Child event sink that feeds the REAL broker registration path."""
    from hermes_multitenancy.webui_broker import periphery

    def sink(event, **payload):
        events.append({"event": event, **payload})
        if event == "authorization_required":
            decisions.append(periphery._register_pending_authorization(
                run_request, dict(payload), run_id=RUN_ID
            ))

    return sink


def _stub_live_probe(monkeypatch, result, *, calls: list | None = None):
    from hermes_multitenancy import authorization_verify

    def fake_verify(
        service, *, profile_name, open_id, profile_dir, shared_home, required_scopes=()
    ):
        if calls is not None:
            calls.append(
                {
                    "service": service,
                    "profile_name": profile_name,
                    "open_id": open_id,
                    "required_scopes": list(required_scopes),
                }
            )
        return result() if callable(result) else result

    monkeypatch.setattr(authorization_verify, "verify_service_authorized", fake_verify)


def _park_live_run(run_id: str = RUN_ID, *, profile: str = PROFILE, owner: str = OWNER) -> None:
    """Bring a run to the state `_stream_run_request` leaves it in at start.

    Two separate things, exactly as production does them: the auth-signal entry
    (re-auth replay stash, RETAINED past run end) and the execution-liveness
    mark (cleared by the run's own teardown). A test that skips this is testing
    a run the server considers already finished.
    """
    from hermes_multitenancy.webui_broker import periphery

    periphery._auth_signal_stash(run_id, payload={}, profile_name=profile, subject=owner)
    periphery._mark_authorization_run_live(run_id, profile_name=profile, subject=owner)


def _seed_routing(monkeypatch, tmp_path: Path) -> Path:
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable

    (tmp_path / "profiles" / PROFILE).mkdir(parents=True, exist_ok=True)
    (tmp_path / "profiles" / PEER_PROFILE).mkdir(parents=True, exist_ok=True)
    # Same file `feishu_uat_auth._profile_name_for_open_id` reads for the
    # actor-owns-this-profile check, so the KEP actor binding is exercised for
    # real rather than around.
    db_path = tmp_path / "multitenancy.db"
    seeded = RoutingTable(db_path)
    seeded.upsert(user_id="alice", profile_name=PROFILE, open_id=OWNER, provenance="sync")
    seeded.upsert(user_id="bob", profile_name=PEER_PROFILE, open_id=PEER, provenance="sync")
    seeded.close()

    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", MASTER_KEY)
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_SERVER", "1")
    _park_live_run()
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    return db_path


def _call_broker(db_path: Path, calls):
    """Run `calls(client)` against a real run-broker aiohttp app."""
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

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
                return await calls(client)
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

    return asyncio.run(runner())


def _owner_headers(owner: str = OWNER) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {MASTER_KEY}",
        "X-Hermes-Owner-Open-Id": owner,
    }


def _derived_response_path(pending_ref: str, run_id: str = RUN_ID) -> Path:
    """Where the BROKER will write, derived exactly as production derives it.

    Tests must not compute this themselves: the point of the fix is that the
    path is a function of (run_id, pending_ref) on the server side and that the
    `response_path` a child sends up is ignored. Asserting against a path the
    test invented would silently stop testing that.
    """
    from hermes_multitenancy.webui_broker import periphery

    path = periphery._authorization_response_path(run_id, pending_ref)
    assert path is not None, pending_ref
    return path


def _seed_shared_routing(shared_home: Path) -> None:
    """The routing rows `feishu_uat_auth._profile_name_for_open_id` reads.

    That lookup is how kep-cli verification proves the ACTOR owns this profile,
    so a shared_home without it is a shared-agent-shaped run.
    """
    from hermes_multitenancy.routing import RoutingTable

    shared_home.mkdir(parents=True, exist_ok=True)
    table = RoutingTable(shared_home / "multitenancy.db")
    table.upsert(user_id="alice", profile_name=PROFILE, open_id=OWNER, provenance="sync")
    table.upsert(user_id="bob", profile_name=PEER_PROFILE, open_id=PEER, provenance="sync")
    table.close()


def _wait_until(predicate, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _ToolCall:
    """Run the blocking tool handler on its own thread, like the real tool loop."""

    def __init__(self, *, service: str, scopes: list[str]):
        from hermes_multitenancy import request_authorization_tool as rat

        self.result_raw: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            args=(rat, service, scopes),
            daemon=True,
        )

    def _run(self, rat, service, scopes):
        self.result_raw = rat._handle_request_authorization({"service": service, "scopes": scopes})

    def start(self) -> "_ToolCall":
        self._thread.start()
        return self

    def join(self, timeout: float = 20.0) -> dict:
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "request_authorization never returned"
        assert self.result_raw is not None
        return json.loads(self.result_raw)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()


# --------------------------------------------------------------------------- #
# 1. vertical happy path
# --------------------------------------------------------------------------- #


def test_vertical_tool_blocks_until_owner_confirms(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import request_authorization_tool as rat
    from hermes_multitenancy.webui_broker import periphery

    _install_fake_approval_session(monkeypatch, SESSION_KEY)
    db_path = _seed_routing(monkeypatch, tmp_path)
    configure = _configure_bridge(monkeypatch, tmp_path)

    run_request = _run_request()
    events: list = []
    decisions: list = []
    callback, cleanup = configure(_broker_sink(events, decisions, run_request), SESSION_KEY)
    rat.register_authorization_bridge(SESSION_KEY, callback)

    # Not authorized yet at registration; authorized by the time confirm runs.
    authorized = {"value": False}
    _stub_live_probe(monkeypatch, lambda: authorized["value"])

    call = _ToolCall(service="lark-cli", scopes=["docx:document:readonly"]).start()
    try:
        assert _wait_until(lambda: bool(decisions)), "broker never registered the request"
        decision = decisions[0]
        assert decision["kind"] == "authorization_required"
        card = decision["payload"]
        # The ONLY payload allowed to reach the browser.
        assert set(card) == {"authorization_id", "service", "scopes", "expires_at", "state"}
        assert card["service"] == "lark-cli"
        assert card["scopes"] == ["docx:document:readonly"]
        assert card["state"] == "pending"
        assert card["authorization_id"].startswith("auth_")
        serialized = json.dumps(card)
        assert "ou_" not in serialized
        assert PROFILE not in serialized
        assert "response_path" not in serialized and "pending_ref" not in serialized

        authorized["value"] = True

        async def confirm(client):
            resp = await client.post(
                f"/api/run-broker/authorization/{card['authorization_id']}/confirm",
                json={"profile_name": PROFILE, "session_id": SESSION},
                headers=_owner_headers(),
            )
            return resp.status, await resp.json()

        status, body = _call_broker(db_path, confirm)
        assert status == 200, body
        assert body == {"ok": True, "state": "success"}

        result = call.join()
    finally:
        rat.unregister_authorization_bridge(SESSION_KEY)
        cleanup()

    assert set(result) == {"ok", "service", "state", "reason"}
    assert result["ok"] is True
    assert result["state"] == "success"
    assert result["service"] == "lark-cli"
    serialized_result = json.dumps(result, ensure_ascii=False)
    assert "ou_" not in serialized_result
    assert "token" not in serialized_result.lower()

    assert [e["event"] for e in events] == ["authorization_required", "authorization_resolved"]
    assert periphery._pending_authorizations[card["authorization_id"]]["consumed"] is True


# --------------------------------------------------------------------------- #
# 2. already authorized -> no card at all
# --------------------------------------------------------------------------- #


def test_already_authorized_resolves_without_showing_a_card(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import request_authorization_tool as rat

    _install_fake_approval_session(monkeypatch, SESSION_KEY)
    _seed_routing(monkeypatch, tmp_path)
    configure = _configure_bridge(monkeypatch, tmp_path)

    run_request = _run_request()
    events: list = []
    decisions: list = []
    callback, cleanup = configure(_broker_sink(events, decisions, run_request), SESSION_KEY)
    rat.register_authorization_bridge(SESSION_KEY, callback)
    _stub_live_probe(monkeypatch, True)

    try:
        result = _ToolCall(service="kep-cli-online", scopes=["read"]).start().join()
    finally:
        rat.unregister_authorization_bridge(SESSION_KEY)
        cleanup()

    assert result["state"] == "success"
    assert result["ok"] is True
    assert len(decisions) == 1
    assert decisions[0]["kind"] == "authorization_resolved"
    assert decisions[0]["payload"]["state"] == "success"
    assert all(d["kind"] != "authorization_required" for d in decisions)


# --------------------------------------------------------------------------- #
# 3. non-whitelist service
# --------------------------------------------------------------------------- #


def test_non_whitelist_service_is_refused_without_any_event(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import request_authorization_tool as rat

    _install_fake_approval_session(monkeypatch, SESSION_KEY)
    invoked: list = []

    def spy_bridge(**kwargs):
        invoked.append(kwargs)
        return {"state": "success", "reason": ""}

    rat.register_authorization_bridge(SESSION_KEY, spy_bridge)
    try:
        refused = json.loads(
            rat._handle_request_authorization({"service": "github", "scopes": ["repo"]})
        )
        empty_scopes = json.loads(
            rat._handle_request_authorization({"service": "lark-cli", "scopes": []})
        )
        extra_key = json.loads(
            rat._handle_request_authorization(
                {"service": "lark-cli", "scopes": ["im:message"], "open_id": OWNER}
            )
        )
    finally:
        rat.unregister_authorization_bridge(SESSION_KEY)

    assert invoked == []
    for payload in (refused, empty_scopes, extra_key):
        assert set(payload) == {"ok", "service", "state", "reason"}
        assert payload["ok"] is False
        assert payload["state"] == "failed"
        assert payload["reason"].strip()
        assert "http" not in payload["reason"].lower()
    assert "github" in refused["reason"]
    assert "open_id" in extra_key["reason"]


def test_tool_fails_closed_without_a_registered_bridge(monkeypatch):
    from hermes_multitenancy import request_authorization_tool as rat

    _install_fake_approval_session(monkeypatch, "multitenancy:feishu:alice_profile:oc_x:ou_alice")
    payload = json.loads(
        rat._handle_request_authorization({"service": "lark-cli", "scopes": ["im:message"]})
    )
    assert payload["ok"] is False
    assert payload["state"] == "failed"
    assert payload["reason"].strip()


# --------------------------------------------------------------------------- #
# 4. cross-user / cross-session confirm
# --------------------------------------------------------------------------- #


def test_confirm_rejects_foreign_owner_and_foreign_session(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    _stub_live_probe(monkeypatch, False)
    response_path = _derived_response_path("authreq_x")
    _park_live_run()
    decision = periphery._register_pending_authorization(
        _run_request(),
        run_id=RUN_ID,
        payload={
            "pending_ref": "authreq_x",
            "service": "lark-cli",
            "scopes": ["im:message:send_as_bot"],
            "response_path": str(response_path),
        },
    )
    authorization_id = decision["payload"]["authorization_id"]

    # The live probe must never even be consulted for a mismatching binding.
    _stub_live_probe(monkeypatch, True)

    async def calls(client):
        foreign_owner = await client.post(
            f"/api/run-broker/authorization/{authorization_id}/confirm",
            json={"profile_name": PEER_PROFILE, "session_id": SESSION},
            headers=_owner_headers(PEER),
        )
        foreign_session = await client.post(
            f"/api/run-broker/authorization/{authorization_id}/confirm",
            json={"profile_name": PROFILE, "session_id": "some-other-session"},
            headers=_owner_headers(),
        )
        return (
            (foreign_owner.status, await foreign_owner.json()),
            (foreign_session.status, await foreign_session.json()),
        )

    (owner_status, owner_body), (session_status, session_body) = _call_broker(db_path, calls)

    assert owner_status in (403, 404), owner_body
    assert owner_body.get("ok") is not True
    assert session_status == 404, session_body
    assert session_body.get("ok") is not True
    assert not response_path.exists()
    assert periphery._pending_authorizations[authorization_id]["consumed"] is False


# --------------------------------------------------------------------------- #
# 5. cancel
# --------------------------------------------------------------------------- #


def test_cancel_returns_cancelled_and_blocks_a_later_confirm(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import request_authorization_tool as rat

    _install_fake_approval_session(monkeypatch, SESSION_KEY)
    db_path = _seed_routing(monkeypatch, tmp_path)
    configure = _configure_bridge(monkeypatch, tmp_path)

    run_request = _run_request()
    events: list = []
    decisions: list = []
    callback, cleanup = configure(_broker_sink(events, decisions, run_request), SESSION_KEY)
    rat.register_authorization_bridge(SESSION_KEY, callback)
    _stub_live_probe(monkeypatch, False)

    call = _ToolCall(service="lark-cli", scopes=["im:message"]).start()
    try:
        assert _wait_until(lambda: bool(decisions))
        authorization_id = decisions[0]["payload"]["authorization_id"]

        async def calls(client):
            cancelled = await client.post(
                f"/api/run-broker/authorization/{authorization_id}/cancel",
                json={"profile_name": PROFILE, "session_id": SESSION},
                headers=_owner_headers(),
            )
            cancel_body = (cancelled.status, await cancelled.json())
            late = await client.post(
                f"/api/run-broker/authorization/{authorization_id}/confirm",
                json={"profile_name": PROFILE, "session_id": SESSION},
                headers=_owner_headers(),
            )
            return cancel_body, (late.status, await late.json())

        (cancel_status, cancel_body), (late_status, late_body) = _call_broker(db_path, calls)
        result = call.join()
    finally:
        rat.unregister_authorization_bridge(SESSION_KEY)
        cleanup()

    assert cancel_status == 200, cancel_body
    assert cancel_body == {"ok": True, "state": "cancelled"}
    assert result["state"] == "cancelled"
    assert result["ok"] is False
    assert late_status in (404, 409), late_body
    assert late_body.get("ok") is not True


# --------------------------------------------------------------------------- #
# 6. expiry
# --------------------------------------------------------------------------- #


def test_expiry_tells_the_model_to_stop_not_to_guess(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import request_authorization_tool as rat

    _install_fake_approval_session(monkeypatch, SESSION_KEY)
    configure = _configure_bridge(monkeypatch, tmp_path, timeout="0.4")

    run_request = _run_request()
    events: list = []
    decisions: list = []
    _stub_live_probe(monkeypatch, False)
    callback, cleanup = configure(_broker_sink(events, decisions, run_request), SESSION_KEY)
    rat.register_authorization_bridge(SESSION_KEY, callback)
    try:
        result = _ToolCall(service="lark-cli", scopes=["im:message"]).start().join()
    finally:
        rat.unregister_authorization_bridge(SESSION_KEY)
        cleanup()

    assert result["state"] == "expired"
    assert result["ok"] is False
    reason = result["reason"].lower()
    assert "best judgement" not in reason and "best judgment" not in reason
    assert "proceed" not in reason
    assert "did not continue" in reason
    assert "retry" in reason or "again" in reason
    assert events[-1]["event"] == "authorization_resolved"
    assert events[-1]["state"] == "expired"


def test_confirm_after_expiry_is_rejected_and_writes_nothing(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    _stub_live_probe(monkeypatch, False)
    response_path = _derived_response_path("authreq_expired")
    _park_live_run()
    decision = periphery._register_pending_authorization(
        _run_request(),
        run_id=RUN_ID,
        payload={
            "pending_ref": "authreq_expired",
            "service": "lark-cli",
            "scopes": ["im:message"],
            "response_path": str(response_path),
        },
    )
    authorization_id = decision["payload"]["authorization_id"]
    assert decision["payload"]["expires_at"] == pytest.approx(time.time() + 600, abs=10)
    periphery._pending_authorizations[authorization_id]["expires_at"] = time.time() - 1

    _stub_live_probe(monkeypatch, True)

    async def calls(client):
        resp = await client.post(
            f"/api/run-broker/authorization/{authorization_id}/confirm",
            json={"profile_name": PROFILE, "session_id": SESSION},
            headers=_owner_headers(),
        )
        return resp.status, await resp.json()

    status, body = _call_broker(db_path, calls)
    assert status in (404, 409), body
    assert body.get("ok") is not True
    assert not response_path.exists()


# --------------------------------------------------------------------------- #
# 7. forged success
# --------------------------------------------------------------------------- #


def test_confirm_without_a_live_credential_never_writes_success(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import request_authorization_tool as rat
    from hermes_multitenancy.webui_broker import periphery

    _install_fake_approval_session(monkeypatch, SESSION_KEY)
    db_path = _seed_routing(monkeypatch, tmp_path)
    configure = _configure_bridge(monkeypatch, tmp_path)

    run_request = _run_request()
    events: list = []
    decisions: list = []
    callback, cleanup = configure(_broker_sink(events, decisions, run_request), SESSION_KEY)
    rat.register_authorization_bridge(SESSION_KEY, callback)
    _stub_live_probe(monkeypatch, False)

    call = _ToolCall(service="kep-cli-pre", scopes=["read"]).start()
    try:
        assert _wait_until(lambda: bool(decisions))
        card = decisions[0]["payload"]
        response_path = _derived_response_path(events[0]["pending_ref"])

        async def calls(client):
            resp = await client.post(
                f"/api/run-broker/authorization/{card['authorization_id']}/confirm",
                json={"profile_name": PROFILE, "session_id": SESSION},
                headers=_owner_headers(),
            )
            return resp.status, await resp.json()

        status, body = _call_broker(db_path, calls)
        assert status == 200, body
        assert body == {"ok": False, "state": "pending", "reason": "not_authorized"}
        assert not response_path.exists()
        assert call.alive, "the tool call must still be waiting after a failed confirm"
        assert periphery._pending_authorizations[card["authorization_id"]]["consumed"] is False

        # Unblock so the thread does not outlive the test.
        async def cancel(client):
            resp = await client.post(
                f"/api/run-broker/authorization/{card['authorization_id']}/cancel",
                json={"profile_name": PROFILE, "session_id": SESSION},
                headers=_owner_headers(),
            )
            return resp.status, await resp.json()

        _call_broker(db_path, cancel)
        result = call.join()
    finally:
        rat.unregister_authorization_bridge(SESSION_KEY)
        cleanup()

    assert result["state"] == "cancelled"


# --------------------------------------------------------------------------- #
# 8. duplicate / concurrent confirm
# --------------------------------------------------------------------------- #


def test_concurrent_confirms_write_exactly_once(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    _stub_live_probe(monkeypatch, False)
    response_path = _derived_response_path("authreq_dup")
    _park_live_run()
    decision = periphery._register_pending_authorization(
        _run_request(),
        run_id=RUN_ID,
        payload={
            "pending_ref": "authreq_dup",
            "service": "lark-cli",
            "scopes": ["im:message"],
            "response_path": str(response_path),
        },
    )
    authorization_id = decision["payload"]["authorization_id"]

    _stub_live_probe(monkeypatch, True)
    writes: list = []
    real_write = periphery._write_authorization_response_file

    def counting_write(path, state, reason):
        writes.append((path, state))
        return real_write(path, state, reason)

    monkeypatch.setattr(periphery, "_write_authorization_response_file", counting_write)

    async def calls(client):
        async def one():
            resp = await client.post(
                f"/api/run-broker/authorization/{authorization_id}/confirm",
                json={"profile_name": PROFILE, "session_id": SESSION},
                headers=_owner_headers(),
            )
            return resp.status, await resp.json()

        return await asyncio.gather(one(), one())

    results = _call_broker(db_path, calls)
    successes = [body for _status, body in results if body.get("ok") is True]
    others = [body for _status, body in results if body.get("ok") is not True]

    assert len(successes) == 1, results
    assert len(others) == 1, results
    assert others[0].get("state") != "success"
    assert len(writes) == 1, writes
    assert json.loads(response_path.read_text(encoding="utf-8"))["state"] == "success"


# --------------------------------------------------------------------------- #
# 9. late callback after a broker restart
# --------------------------------------------------------------------------- #


def test_confirm_after_broker_restart_is_404_and_writes_nothing(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    _stub_live_probe(monkeypatch, False)
    response_path = _derived_response_path("authreq_restart")
    _park_live_run()
    decision = periphery._register_pending_authorization(
        _run_request(),
        run_id=RUN_ID,
        payload={
            "pending_ref": "authreq_restart",
            "service": "lark-cli",
            "scopes": ["im:message"],
            "response_path": str(response_path),
        },
    )
    authorization_id = decision["payload"]["authorization_id"]

    # Simulate the broker process restarting: the in-memory registry is gone.
    periphery._pending_authorizations.clear()
    _stub_live_probe(monkeypatch, True)

    async def calls(client):
        resp = await client.post(
            f"/api/run-broker/authorization/{authorization_id}/confirm",
            json={"profile_name": PROFILE, "session_id": SESSION},
            headers=_owner_headers(),
        )
        return resp.status, await resp.json()

    status, body = _call_broker(db_path, calls)
    assert status == 404, body
    assert body.get("ok") is not True
    assert authorization_id not in json.dumps(body) or body.get("state") != "success"
    assert not response_path.exists()


# --------------------------------------------------------------------------- #
# 10. KEP env isolation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("service", "expected_env"),
    [("kep-cli-online", "online"), ("kep-cli-pre", "pre")],
)
def test_kep_env_is_never_substituted(monkeypatch, tmp_path: Path, service, expected_env):
    from hermes_multitenancy import authorization_verify, credential_hub_auth

    seen: list = []

    def fake_logged_in(profile_dir, profile_name, shared_home, *, env_name="online"):
        seen.append({"env_name": env_name, "profile_name": profile_name})
        return True

    monkeypatch.setattr(credential_hub_auth, "kep_cli_logged_in", fake_logged_in)
    # kep-cli verification binds the ACTOR to the profile, so the routing rows
    # have to be there — they are what proves this open_id owns this profile.
    _seed_shared_routing(tmp_path)

    assert (
        authorization_verify.verify_service_authorized(
            service,
            profile_name=PROFILE,
            open_id=OWNER,
            profile_dir=tmp_path / "profiles" / PROFILE,
            shared_home=tmp_path,
            required_scopes=authorization_verify.KEP_FIXED_SCOPES,
        )
        is True
    )
    assert seen == [{"env_name": expected_env, "profile_name": PROFILE}]


def test_lark_live_check_fails_closed_when_identity_does_not_match(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import authorization_verify, feishu_uat_auth

    shared = tmp_path / "shared"
    uat_dir = shared / "profiles" / PROFILE / "feishu_uat"
    uat_dir.mkdir(parents=True)
    (uat_dir / f"{OWNER}.json").write_text(
        json.dumps(
            {
                "access_token": "u-live",
                "expires_at": int(time.time() * 1000) + 600_000,
                "scope": "im:message docx:document:readonly",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(feishu_uat_auth, "refresh_uat_if_needed", lambda **_k: None)

    monkeypatch.setattr(feishu_uat_auth, "_fetch_user_info", lambda _t: {"open_id": OWNER})
    assert (
        authorization_verify.verify_service_authorized(
            "lark-cli",
            profile_name=PROFILE,
            open_id=OWNER,
            profile_dir=shared / "profiles" / PROFILE,
            shared_home=shared,
            required_scopes=["im:message"],
        )
        is True
    )

    # A revoked/rotated token that now belongs to somebody else must be False —
    # the file-only reader would still have said "authenticated" here.
    monkeypatch.setattr(feishu_uat_auth, "_fetch_user_info", lambda _t: {"open_id": PEER})
    assert (
        authorization_verify.verify_service_authorized(
            "lark-cli",
            profile_name=PROFILE,
            open_id=OWNER,
            profile_dir=shared / "profiles" / PROFILE,
            shared_home=shared,
            required_scopes=["im:message"],
        )
        is False
    )

    def boom(_t):
        raise RuntimeError("feishu unreachable")

    monkeypatch.setattr(feishu_uat_auth, "_fetch_user_info", boom)
    assert (
        authorization_verify.verify_service_authorized(
            "lark-cli",
            profile_name=PROFILE,
            open_id=OWNER,
            profile_dir=shared / "profiles" / PROFILE,
            shared_home=shared,
            required_scopes=["im:message"],
        )
        is False
    )


# --------------------------------------------------------------------------- #
# 11. the sandboxed child can never self-confirm
# --------------------------------------------------------------------------- #


def test_run_scoped_token_cannot_reach_the_authorization_routes(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.webui_broker import periphery
    from hermes_multitenancy.webui_broker_server import (
        register_run_broker_scoped_token,
        unregister_run_broker_scoped_token,
    )

    db_path = _seed_routing(monkeypatch, tmp_path)
    run_scoped = "run-scoped-token-for-alice"
    register_run_broker_scoped_token(
        token=run_scoped,
        profile_name=PROFILE,
        open_id=OWNER,
        run_id="run-1",
    )

    assert not any(
        prefix.startswith("/api/run-broker/authorization")
        for prefix in periphery._RUN_SCOPED_TOKEN_PATH_PREFIXES
    )

    async def calls(client):
        out = []
        for verb in ("confirm", "authorize", "cancel"):
            resp = await client.post(
                f"/api/run-broker/authorization/x/{verb}",
                json={"profile_name": PROFILE, "session_id": SESSION},
                headers={"Authorization": f"Bearer {run_scoped}"},
            )
            out.append((verb, resp.status))
        return out

    try:
        statuses = _call_broker(db_path, calls)
    finally:
        unregister_run_broker_scoped_token(run_scoped)

    assert statuses == [("confirm", 401), ("authorize", 401), ("cancel", 401)]


# --------------------------------------------------------------------------- #
# 12. at most one pending authorization per run
# --------------------------------------------------------------------------- #


def test_second_pending_authorization_for_the_same_run_is_refused(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    _stub_live_probe(monkeypatch, False)
    run_request = _run_request()
    first_path = _derived_response_path("authreq_1")
    second_path = _derived_response_path("authreq_2")

    first = periphery._register_pending_authorization(
        run_request,
        run_id=RUN_ID,
        payload={
            "pending_ref": "authreq_1",
            "service": "lark-cli",
            "scopes": ["im:message"],
            "response_path": str(first_path),
        },
    )
    second = periphery._register_pending_authorization(
        run_request,
        run_id=RUN_ID,
        payload={
            "pending_ref": "authreq_2",
            "service": "kep-cli-online",
            "scopes": ["read"],
            "response_path": str(second_path),
        },
    )

    assert first["kind"] == "authorization_required"
    assert second["kind"] == "authorization_resolved"
    assert second["payload"]["state"] == "failed"
    assert set(second["payload"]) == {
        "authorization_id",
        "service",
        "scopes",
        "expires_at",
        "state",
    }

    # The first request survives untouched; only the second one was answered.
    first_id = first["payload"]["authorization_id"]
    assert periphery._pending_authorizations[first_id]["consumed"] is False
    assert periphery._pending_authorizations[first_id]["state"] == "pending"
    assert not first_path.exists()
    assert json.loads(second_path.read_text(encoding="utf-8"))["state"] == "failed"
    assert len(periphery._pending_authorizations) == 1


def test_second_tool_call_in_the_same_run_is_refused_without_a_second_event(
    monkeypatch, tmp_path: Path
):
    from hermes_multitenancy import request_authorization_tool as rat

    _install_fake_approval_session(monkeypatch, SESSION_KEY)
    configure = _configure_bridge(monkeypatch, tmp_path, timeout="3")
    run_request = _run_request()
    events: list = []
    decisions: list = []
    _stub_live_probe(monkeypatch, False)
    callback, cleanup = configure(_broker_sink(events, decisions, run_request), SESSION_KEY)
    rat.register_authorization_bridge(SESSION_KEY, callback)

    first = _ToolCall(service="lark-cli", scopes=["im:message"]).start()
    try:
        assert _wait_until(lambda: bool(decisions))
        second = json.loads(
            rat._handle_request_authorization({"service": "kep-cli-online", "scopes": ["read"]})
        )
        assert second["ok"] is False
        assert second["state"] == "failed"
        assert "pending" in second["reason"].lower()
        assert len(decisions) == 1
        result = first.join()
    finally:
        rat.unregister_authorization_bridge(SESSION_KEY)
        cleanup()

    assert result["state"] == "expired"


# --------------------------------------------------------------------------- #
# 13. the authorize route is the ONLY place a URL is minted
# --------------------------------------------------------------------------- #


def test_authorize_starts_the_lark_device_flow_server_side(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    _stub_live_probe(monkeypatch, False)
    response_path = _derived_response_path("authreq_url")
    _park_live_run()
    decision = periphery._register_pending_authorization(
        _run_request(),
        run_id=RUN_ID,
        payload={
            "pending_ref": "authreq_url",
            "service": "lark-cli",
            "scopes": ["im:message"],
            "response_path": str(response_path),
        },
    )
    authorization_id = decision["payload"]["authorization_id"]
    assert "verification_uri" not in json.dumps(decision["payload"])

    started: list = []

    def fake_start_session(*, profile_name, open_id, scope=None, shared_home=None):
        started.append({"profile_name": profile_name, "open_id": open_id, "scope": scope})
        return {"session_id": "sess-lark-1", "verification_uri": "https://example.invalid/device"}

    monkeypatch.setattr(feishu_uat_auth, "find_active_session", lambda **_k: None)
    monkeypatch.setattr(feishu_uat_auth, "start_session", fake_start_session)

    async def calls(client):
        resp = await client.post(
            f"/api/run-broker/authorization/{authorization_id}/authorize",
            json={"profile_name": PROFILE, "session_id": SESSION},
            headers=_owner_headers(),
        )
        return resp.status, await resp.json()

    status, body = _call_broker(db_path, calls)
    assert status == 200, body
    assert body["ok"] is True
    assert body["verification_uri"] == "https://example.invalid/device"
    assert body["authorization_id"] == authorization_id
    assert started == [{"profile_name": PROFILE, "open_id": OWNER, "scope": "im:message"}]
    assert periphery._pending_authorizations[authorization_id]["flow"]["kind"] == "lark"


def test_a_live_session_opened_for_narrower_scopes_is_not_reused(monkeypatch, tmp_path: Path):
    """A live device session that was started for one (narrower) request must
    never be handed back for a LATER request whose frozen scopes it was never
    opened to ask the user to grant — that request needs its own device code.
    """
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)

    # Entry A already has a live lark device flow, opened for a narrower scope.
    decision_a = _register(
        monkeypatch, tmp_path, pending_ref="authreq_scope_narrow",
        scopes=["read"],
    )
    periphery._pending_authorizations[decision_a["payload"]["authorization_id"]]["flow"] = {
        "kind": "lark",
        "session_id": "feishu-shared-1",
        "verification_uri": "https://accounts.feishu.cn/device/shared-1",
        "interval": 1,
    }

    # Entry B, a DIFFERENT run, needs a document scope the live session was
    # never granted.
    other_run = "run-signal-for-alice-scope-b"
    _park_live_run(other_run)
    decision_b = _register(
        monkeypatch, tmp_path, pending_ref="authreq_scope_wide",
        scopes=["docx:document:readonly"], run_id=other_run,
    )
    authorization_id_b = decision_b["payload"]["authorization_id"]

    starts: list = []

    def fake_start(*, profile_name, open_id, scope=None, shared_home=None):
        starts.append(scope)
        return {
            "session_id": "feishu-shared-2",
            "verification_uri": "https://accounts.feishu.cn/device/shared-2",
            "interval": 1,
            "status": "pending",
        }

    monkeypatch.setattr(
        feishu_uat_auth,
        "find_active_session",
        lambda **_k: {
            "session_id": "feishu-shared-1",
            "verification_uri": "https://accounts.feishu.cn/device/shared-1",
            "interval": 1,
            "status": "pending",
        },
    )
    monkeypatch.setattr(feishu_uat_auth, "start_session", fake_start)

    try:
        async def calls(client):
            resp = await client.post(
                f"/api/run-broker/authorization/{authorization_id_b}/authorize",
                json={"profile_name": PROFILE, "session_id": SESSION},
                headers=_owner_headers(),
            )
            return resp.status, await resp.json()

        status, body = _call_broker(db_path, calls)
        assert status == 200, body
        assert body["ok"] is True
        # A fresh device code was minted for entry B's own (wider) scopes —
        # the narrower live session was never handed back for it.
        assert starts == ["docx:document:readonly"]
        assert body["verification_uri"] == "https://accounts.feishu.cn/device/shared-2"
        assert (
            periphery._pending_authorizations[authorization_id_b]["flow"]["session_id"]
            == "feishu-shared-2"
        )
    finally:
        periphery._auth_signal_consume(other_run)


def test_authorize_refuses_kep_without_a_public_callback_origin(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    monkeypatch.delenv("HERMES_PUBLIC_CALLBACK_ORIGIN", raising=False)
    _stub_live_probe(monkeypatch, False)
    _park_live_run()
    decision = periphery._register_pending_authorization(
        _run_request(),
        run_id=RUN_ID,
        payload={
            "pending_ref": "authreq_kep",
            "service": "kep-cli-online",
            "scopes": ["read"],
            "response_path": str(_derived_response_path("authreq_kep")),
        },
    )
    authorization_id = decision["payload"]["authorization_id"]

    async def calls(client):
        resp = await client.post(
            f"/api/run-broker/authorization/{authorization_id}/authorize",
            json={"profile_name": PROFILE, "session_id": SESSION},
            headers=_owner_headers(),
        )
        return resp.status, await resp.json()

    status, body = _call_broker(db_path, calls)
    assert status == 503, body
    assert body.get("ok") is not True
    assert "HERMES_PUBLIC_CALLBACK_ORIGIN" in json.dumps(body)


# --------------------------------------------------------------------------- #
# 14. run.py wiring: webui only
# --------------------------------------------------------------------------- #


def test_run_with_aiagent_registers_the_authorization_bridge_for_webui(monkeypatch, tmp_path: Path):
    from tests.test_aiagent_subprocess import (
        _install_fake_approval,
        _install_fake_feishu_oapi,
        _install_fake_gateway_session_context,
    )

    from hermes_multitenancy import agent_real, request_authorization_tool as rat
    from hermes_multitenancy.run_models import RunRequest
    from hermes_multitenancy.webui_broker_server import _build_webui_event

    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\nplatform_toolsets:\n  webui:\n  - clarify\n",
        encoding="utf-8",
    )
    (profile_home / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MULTITENANCY_AUTHORIZATION_DIR", str(tmp_path / "authorization"))
    _install_fake_approval(monkeypatch)

    observed: dict = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_conversation(self, user_message, task_id, persist_user_message=None):
            key = self.kwargs["gateway_session_key"]
            observed["session_key"] = key
            observed["bridge"] = rat.get_authorization_bridge(key)
            return {"final_response": "ok"}

        def cleanup(self):
            pass

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    _install_fake_feishu_oapi(monkeypatch)
    _install_fake_gateway_session_context(monkeypatch)

    event = _build_webui_event(
        RunRequest(
            channel="webui",
            profile_name="coder",
            user_key="ou_owner",
            content="hello",
            session_id="webui-session-1",
        )
    )
    assert agent_real._run_with_aiagent(event, profile_home, event_sink=lambda *_a, **_k: None) == "ok"
    assert observed["bridge"] is not None
    # Unregistered again in the run's finally — no cross-run bridge leak.
    assert rat.get_authorization_bridge(observed["session_key"]) is None


def test_run_with_aiagent_does_not_register_the_bridge_for_feishu(monkeypatch, tmp_path: Path):
    from tests.test_aiagent_subprocess import (
        _event,
        _install_fake_approval,
        _install_fake_feishu_oapi,
        _install_fake_gateway_session_context,
    )

    from hermes_multitenancy import agent_real, request_authorization_tool as rat

    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  default: openai/test-model\nplatform_toolsets:\n  feishu:\n  - clarify\n",
        encoding="utf-8",
    )
    (profile_home / ".env").write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")
    _install_fake_approval(monkeypatch)

    observed: dict = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_conversation(self, user_message, task_id, persist_user_message=None):
            observed["bridge"] = rat.get_authorization_bridge(self.kwargs["gateway_session_key"])
            return {"final_response": "ok"}

        def cleanup(self):
            pass

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))
    _install_fake_feishu_oapi(monkeypatch)
    _install_fake_gateway_session_context(monkeypatch)

    assert agent_real._run_with_aiagent(_event(), profile_home, event_sink=lambda *_a, **_k: None) == "ok"
    assert observed["bridge"] is None


# --------------------------------------------------------------------------- #
# 15. child -> parent control-event plumbing
#
# Regression for the 2026-09-08 live-stack finding: the tool blocked in-call
# exactly as designed, but the browser never saw a card and the WebUI server log
# had zero `authorization.*` lines. The event pair was missing from the
# child->parent forwarding allowlists and from the child's env, so the whole
# feature was inert on a real run while every unit test above stayed green —
# each of those drives the bridge in-process and never crosses the pipe.
# --------------------------------------------------------------------------- #


def _fake_child_process(lines: list[bytes]):
    """A minimal asyncio subprocess whose stdout replays `lines` (JSONL)."""

    class FakeStdin:
        def write(self, _payload):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class FakeStdout:
        def __init__(self):
            self.lines = list(lines)

        async def readline(self):
            return self.lines.pop(0) if self.lines else b""

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

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return FakeProc()

    return fake_create_subprocess_exec


async def test_child_authorization_events_survive_the_subprocess_pipe(monkeypatch, tmp_path: Path):
    """The real forwarding path must carry BOTH authorization events downstream.

    Drives `_stream_aiagent_subprocess` (the function that owns the forwarded
    event-kind allowlist) with a fake child, exactly like the existing
    `test_stream_aiagent_subprocess_forwards_child_approval_events`. A kind that
    is not on the allowlist is dropped silently, so this fails loudly if the
    pair is ever removed again.
    """
    from tests.test_aiagent_subprocess import _event

    from hermes_multitenancy import agent_real

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_child_process(
            [
                json.dumps(
                    {
                        "event": "authorization_required",
                        "session_key": SESSION_KEY,
                        "pending_ref": "authreq_pipe",
                        "service": "kep-cli-pre",
                        "scopes": ["read"],
                        "response_path": str(_derived_response_path("authreq_pipe")),
                    }
                ).encode()
                + b"\n",
                json.dumps(
                    {
                        "event": "authorization_resolved",
                        "session_key": SESSION_KEY,
                        "pending_ref": "authreq_pipe",
                        "state": "success",
                        "reason": "authorized",
                    }
                ).encode()
                + b"\n",
                b'{"event": "done", "result": "ok", "error": null}\n',
            ]
        ),
    )

    forwarded = [
        item async for item in agent_real._stream_aiagent_subprocess(_event(), tmp_path)
    ]

    kinds = [kind for kind, _payload in forwarded]
    assert kinds == ["authorization_required", "authorization_resolved", "done"], forwarded

    required = dict(forwarded[0][1])
    assert required["pending_ref"] == "authreq_pipe"
    assert required["service"] == "kep-cli-pre"
    assert required["scopes"] == ["read"]
    # response_path must still be present HERE: the broker needs it to write the
    # verdict. It is stripped one layer later, by _sanitize_authorization_payload.
    assert required["response_path"] == str(_derived_response_path("authreq_pipe"))
    assert dict(forwarded[1][1])["state"] == "success"


async def test_mapped_codex_run_yields_authorization_before_the_buffered_output(
    monkeypatch, tmp_path: Path
):
    """The local-harness (mapped codex) path buffers everything that is not a
    control event until the spend receipt clears, then flushes it at the end. An
    authorization ask caught in that buffer would reach the browser only after
    the run had already finished — far too late for a human to answer it.

    Behavioural proof: the child emits content FIRST and the authorization pair
    after it, yet the pair must come out FIRST because control events bypass the
    buffer. Drives the real ``_verified_codex_stream``.
    """
    import os
    from contextlib import contextmanager

    from tests.test_aiagent_subprocess import _event

    from hermes_multitenancy import agent_real
    from hermes_multitenancy.agent_real import _core, executor_map, harness_webui_runtime
    from hermes_multitenancy.agent_real import harness_workflow

    profile_home = tmp_path / "profile"
    profile_home.mkdir()

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_child_process(
            [
                b'{"event":"content","text":"working on it"}\n',
                json.dumps(
                    {
                        "event": "authorization_required",
                        "session_key": SESSION_KEY,
                        "pending_ref": "authreq_mapped",
                        "service": "lark-cli",
                        "scopes": ["im:message"],
                        "response_path": str(_derived_response_path("authreq_mapped")),
                    }
                ).encode()
                + b"\n",
                json.dumps(
                    {
                        "event": "authorization_resolved",
                        "session_key": SESSION_KEY,
                        "pending_ref": "authreq_mapped",
                        "state": "success",
                        "reason": "authorized",
                    }
                ).encode()
                + b"\n",
                b'{"event":"done","result":"working on it","error":null}\n',
            ]
        ),
    )

    @contextmanager
    def fake_env_scope(*_args, **_kwargs):
        yield dict(os.environ)

    class _FakeStore:
        def start(self, *_args, **_kwargs):
            return None

        def close(self):
            return None

    monkeypatch.setattr(agent_real, "_aiagent_subprocess_env_scope", fake_env_scope)
    monkeypatch.setitem(
        agent_real._stream_aiagent_subprocess.__globals__,
        "_bind_codex_run_workspace",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        executor_map, "runtime_for_event", lambda *_args: executor_map.CODEX_APP_SERVER
    )
    monkeypatch.setattr(
        harness_webui_runtime,
        "require_event_admission",
        lambda *_a, **_k: SimpleNamespace(workflow_id="wf-authorization"),
    )
    monkeypatch.setattr(
        harness_webui_runtime,
        "plan_event_thread",
        lambda *_a, **_k: (_FakeStore(), SimpleNamespace(resume_thread_id="th-1")),
    )
    monkeypatch.setattr(harness_webui_runtime, "resolve_event_flow", lambda *_a, **_k: "flow")
    monkeypatch.setattr(harness_workflow, "HarnessWorkflowStore", lambda *_a, **_k: _FakeStore())

    async def fake_receipt(_event, _profile):
        return None

    monkeypatch.setattr(_core, "_complete_codex_spend_receipt", fake_receipt)

    harness_event = _event()
    harness_event.trusted_runtime_principal = SimpleNamespace(profile_name="coder")

    seen = [
        kind
        async for kind, _payload in _core._verified_codex_stream(harness_event, profile_home)
        if kind != "heartbeat"
    ]

    assert seen == [
        "authorization_required",
        "authorization_resolved",
        "content",
        "done",
    ], seen


def test_child_env_pins_the_authorization_rendezvous_directory(monkeypatch, tmp_path: Path):
    """Parent and child must agree on ONE authorization directory.

    The wait is a file rendezvous: the broker writes the verdict, the child polls
    for it. A sandboxed child resolves its own `tempfile.gettempdir()`, so an
    unpinned path means the child polls a directory nobody ever writes to and the
    tool call always expires.
    """
    from hermes_multitenancy.agent_real import _core, subprocess_env
    from hermes_multitenancy.webui_broker import periphery

    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)

    # No run id on the event ⇒ NO pin at all. That is the fail-closed half: the
    # child then gets no bridge and `request_authorization` refuses, instead of
    # falling back to a shared world-writable temp directory.
    monkeypatch.delenv("HERMES_MULTITENANCY_AUTHORIZATION_DIR", raising=False)
    bare = subprocess_env._build_subprocess_env(
        profile_home,
        approval_dir=tmp_path / "approval",
        event_stream=True,
    )
    assert "HERMES_MULTITENANCY_AUTHORIZATION_DIR" not in bare
    assert _core._authorization_bridge_dir() is None
    assert _core._configure_webui_authorization_bridge(lambda *a, **k: None, SESSION_KEY)[0] is None

    # With a run id the parent pins the PER-RUN directory it derived itself, and
    # that is byte-for-byte the directory the broker will write into.
    expected = periphery._authorization_response_dir(RUN_ID)
    event = SimpleNamespace(raw_event={}, trusted_authorization_run_id=RUN_ID)
    with _core._aiagent_subprocess_env_scope(
        event, profile_home, approval_dir=tmp_path / "approval", event_stream=True
    ) as env:
        pinned = env["HERMES_MULTITENANCY_AUTHORIZATION_DIR"]
    assert Path(pinned).is_absolute()
    assert pinned == str(expected)
    assert expected.is_dir()
    monkeypatch.setenv("HERMES_MULTITENANCY_AUTHORIZATION_DIR", pinned)
    assert _core._authorization_bridge_dir() == expected


def test_warm_worker_base_env_never_bakes_the_authorization_directory(monkeypatch, tmp_path: Path):
    """The warm worker's base env outlives every run and is readable by every
    later user's child via /proc/<pid>/environ. A per-run rendezvous path must be
    dropped from it, exactly like APPROVAL_DIR."""
    from hermes_multitenancy.agent_real import subprocess_env, warm_worker

    from hermes_multitenancy.agent_real import _core

    profile_home = tmp_path / "profiles" / "coder"
    profile_home.mkdir(parents=True)

    assert (
        "HERMES_MULTITENANCY_AUTHORIZATION_DIR"
        in warm_worker._AIAGENT_WARM_WORKER_BASE_ENV_DROP
    )

    # Behavioural: the per-run env carries it, the shared warm base env does not.
    event = SimpleNamespace(raw_event={}, trusted_authorization_run_id=RUN_ID)
    with _core._aiagent_subprocess_env_scope(
        event, profile_home, approval_dir=tmp_path / "approval", event_stream=True
    ) as per_run:
        assert "HERMES_MULTITENANCY_AUTHORIZATION_DIR" in per_run

    base = warm_worker._build_aiagent_warm_worker_base_env(profile_home)
    assert "HERMES_MULTITENANCY_AUTHORIZATION_DIR" not in base
    assert "HERMES_MULTITENANCY_APPROVAL_DIR" not in base


# --------------------------------------------------------------------------- #
# 16. ingest is a machine API — it must short-circuit, never wait on a human
# --------------------------------------------------------------------------- #


def test_interactive_ingest_returns_needs_authorization_instead_of_stalling(
    monkeypatch, tmp_path: Path
):
    """Ingest has no human on the other end, so a 600s inline-authorization wait
    would be a stall, not a wait. It must behave exactly like clarify/approval
    there: cancel the run, report a terminal status with the SANITIZED payload,
    and leave no pending registration behind (no `authorization_resolved` will
    ever arrive for a run that was cancelled).
    """
    from tests.test_run_broker_ingest import _post

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker import periphery
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setenv("HERMES_INGEST_KEY", "testkey")
    monkeypatch.setenv("HERMES_INGEST_PROFILE", "owner")
    monkeypatch.setattr(
        router_mod,
        "_profile_name_to_home",
        lambda profile_name: tmp_path / "profiles" / profile_name,
    )
    _stub_live_probe(monkeypatch, False)

    # The ingest dispatch has no streaming run id, so `_default_dispatch_agent`
    # mints one; the rendezvous is named after THAT, and the test reads it off
    # the event rather than inventing a path.
    dispatched: dict = {}
    decoy = tmp_path / "attacker-chosen.json"

    async def fake_stream_run_agent(event, profile_home, *, messages=None):
        dispatched["run_id"] = event.trusted_authorization_run_id
        yield "authorization_required", {
            "pending_ref": "authreq_ingest",
            "session_key": SESSION_KEY,
            "service": "lark-cli",
            "scopes": ["im:message"],
            "response_path": str(decoy),
        }

    async def fake_real_run_agent(event, profile_home, *, messages=None):
        return ""

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    app = create_run_broker_app(
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )
    started = time.monotonic()
    status, text = _post(
        app,
        {"content": "读一下我的飞书文档", "requires_host_tools": False, "interactive": True},
        headers={"Authorization": "Bearer testkey"},
    )
    elapsed = time.monotonic() - started

    data = json.loads(text)
    assert status == 200
    assert data["ok"] is False
    assert data["status"] == "needs_authorization"
    # Returned in seconds, not after the 600s authorization window.
    assert elapsed < 30, elapsed

    surfaced = data["authorization"]
    assert set(surfaced) == {
        "authorization_id",
        "service",
        "scopes",
        "expires_at",
        "state",
    }
    assert surfaced["service"] == "lark-cli"
    assert surfaced["scopes"] == ["im:message"]
    # Today's ingest contract carries no session_id (build_ingest_run_request),
    # so the broker refuses to register a request it could never bind a confirm
    # to — fail-closed, and reported to the caller rather than swallowed.
    assert surfaced["state"] == "failed"
    body = json.dumps(data)
    assert "response_path" not in body and "pending_ref" not in body
    assert "ou_" not in body
    assert "authreq_ingest" not in body

    # Abandoned, not leaked: nothing will ever resolve this request.
    assert periphery._pending_authorizations == {}
    # The child was answered with a terminal state, so its tool call returned
    # immediately instead of polling for the full 600s window — and it was
    # answered on the SERVER-derived path, never the one the payload named.
    assert dispatched["run_id"]
    response_path = _derived_response_path("authreq_ingest", dispatched["run_id"])
    assert json.loads(response_path.read_text(encoding="utf-8"))["state"] == "failed"
    assert not decoy.exists()


def test_a_child_claimed_success_the_server_never_decided_is_not_trusted(
    monkeypatch, tmp_path: Path
):
    """A bare `authorization_resolved: success` from the child is NOT evidence.

    The rendezvous file is the only thing between a prompt-injected child and a
    「已授权」card, and the child can name its own file. So the server reconciles:
    a success it never recorded is reported as `failed`. Here nothing was ever
    registered, so the claim is downgraded and the ingest run is short-circuited
    like any other unresolved authorization — it does NOT sail through as if the
    credential were live.

    (The genuine already-authorized path is covered by
    `test_already_authorized_resolves_without_showing_a_card`, where the success
    comes from the server's own probe and IS recorded.)
    """
    from tests.test_run_broker_ingest import _post

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
        yield "authorization_resolved", {
            "pending_ref": "authreq_already",
            "service": "lark-cli",
            "state": "success",
            "reason": "already_authorized",
        }
        yield "content", "读完了"

    async def fake_real_run_agent(event, profile_home, *, messages=None):
        return "读完了"

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream_run_agent)
    monkeypatch.setattr(agent_real, "real_run_agent", fake_real_run_agent)

    app = create_run_broker_app(
        mark_seen=lambda _request: True,
        sandbox_available=lambda: True,
    )
    status, text = _post(
        app,
        {"content": "读一下我的飞书文档", "requires_host_tools": False, "interactive": True},
        headers={"Authorization": "Bearer testkey"},
    )
    data = json.loads(text)
    assert status == 200
    # Downgraded, not believed: no `success` reaches the caller and the run is
    # reported as unresolved rather than continuing on a credential nobody
    # verified.
    assert data["ok"] is False
    assert data["status"] == "needs_authorization"
    assert data["authorization"]["state"] == "failed"
    assert "success" not in json.dumps(data["authorization"])


# --------------------------------------------------------------------------- #
# 17. the request is frozen to the RUN and the TOOL CALL, not just the session
# --------------------------------------------------------------------------- #


def test_registration_freezes_a_real_run_id_and_tool_call_binding(monkeypatch, tmp_path: Path):
    """Regression for the empty-string binding bug.

    The record used to read `run_id` off `RunRequest`, which has no such field,
    so it silently froze "" and the binding was one dimension short of what the
    PRD requires. Both dimensions must now be real and non-empty.
    """
    from hermes_multitenancy import request_authorization_tool as rat
    from hermes_multitenancy.webui_broker import periphery

    _install_fake_approval_session(monkeypatch, SESSION_KEY)
    _seed_routing(monkeypatch, tmp_path)
    configure = _configure_bridge(monkeypatch, tmp_path, timeout="3")

    run_request = _run_request()
    events: list = []
    decisions: list = []
    _stub_live_probe(monkeypatch, False)
    callback, cleanup = configure(_broker_sink(events, decisions, run_request), SESSION_KEY)
    rat.register_authorization_bridge(SESSION_KEY, callback)

    call = _ToolCall(service="lark-cli", scopes=["im:message"]).start()
    try:
        assert _wait_until(lambda: bool(decisions))
        authorization_id = decisions[0]["payload"]["authorization_id"]
        frozen = periphery._pending_authorizations[authorization_id]

        assert frozen["run_id"] == RUN_ID
        assert frozen["run_id"], "an empty run_id leaves confirm unable to detect a stale run"
        # Preferred source is core's own tool-call id, which the tool reads from
        # `tools.approval`; `pending_ref` is only the fallback.
        assert frozen["tool_call_id"] == TOOL_CALL_ID
        assert frozen["tool_call_id"] != frozen["pending_ref"]
        # …and it is bound to THIS call, not to the session.
        assert events[0]["tool_call_id"] == TOOL_CALL_ID
        # Never surfaced to the browser.
        assert "run_id" not in decisions[0]["payload"]
        assert "tool_call_id" not in decisions[0]["payload"]
        result = call.join()
    finally:
        rat.unregister_authorization_bridge(SESSION_KEY)
        cleanup()

    assert result["state"] == "expired"


def test_registration_without_a_run_identity_is_refused(monkeypatch, tmp_path: Path):
    """No per-run id ⇒ confirm could never tell a live run from a superseded one,
    so the request is refused instead of registered with a weaker binding.

    It is also the case where there is no derivable response path at all: the
    rendezvous is named after the run, so with no run there is nowhere the
    server may legitimately write. It writes NOTHING rather than fall back to a
    path the caller supplied — that fallback is exactly what let a child aim the
    parent's write. (Unreachable from a real dispatch: `_default_dispatch_agent`
    mints an id when its caller has none.)
    """
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    _stub_live_probe(monkeypatch, False)
    decoy = tmp_path / "attacker-chosen.json"

    decision = periphery._register_pending_authorization(
        _run_request(),
        run_id="",
        payload={
            "pending_ref": "authreq_norun",
            "service": "lark-cli",
            "scopes": ["im:message"],
            "response_path": str(decoy),
        },
    )

    assert decision["kind"] == "authorization_resolved"
    assert decision["payload"]["state"] == "failed"
    assert periphery._pending_authorizations == {}
    assert not decoy.exists()
    assert not _derived_response_path("authreq_norun").exists()


def test_confirm_from_a_superseded_run_is_refused_and_writes_nothing(monkeypatch, tmp_path: Path):
    """Right owner, right profile, right session — but the run that raised the
    request is gone (finished, cancelled, or replaced by a newer run on the same
    session). Answering it now would resolve a run nobody is waiting on."""
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    _stub_live_probe(monkeypatch, False)
    response_path = _derived_response_path("authreq_superseded")
    decision = periphery._register_pending_authorization(
        _run_request(),
        run_id=RUN_ID,
        payload={
            "pending_ref": "authreq_superseded",
            "service": "lark-cli",
            "scopes": ["im:message"],
            "response_path": str(response_path),
        },
    )
    authorization_id = decision["payload"]["authorization_id"]
    assert decision["kind"] == "authorization_required"

    # Sanity: while the run is live the binding check passes.
    assert periphery._authorization_run_is_live(
        periphery._pending_authorizations[authorization_id]
    )

    # The run ends. `_stream_run_request`'s finally-block clears the execution
    # mark; the auth-signal entry is deliberately RETAINED here (re-auth replay),
    # which is exactly why liveness may not be read off it.
    periphery._mark_authorization_run_finished(RUN_ID)
    assert periphery._auth_signal_lookup(RUN_ID) is not None
    assert not periphery._authorization_run_is_live(
        periphery._pending_authorizations[authorization_id]
    )

    # Even with the credential genuinely live, the confirm must be refused.
    _stub_live_probe(monkeypatch, True)

    async def confirm_call(client):
        resp = await client.post(
            f"/api/run-broker/authorization/{authorization_id}/confirm",
            json={"profile_name": PROFILE, "session_id": SESSION},
            headers=_owner_headers(),
        )
        return resp.status, await resp.json()

    status, body = _call_broker(db_path, confirm_call)
    assert status in (404, 409), (status, body)
    assert body.get("ok") is not True
    assert not response_path.exists()
    assert periphery._pending_authorizations[authorization_id]["consumed"] is False

    # CANCEL, by contrast, must still work on a dead run — that is the whole
    # point of Stop: it is the one verb whose job is to invalidate a request
    # nobody is executing any more. It grants nothing, so it does not carry the
    # run-liveness precondition, and it must never produce `success`.
    async def cancel_call(client):
        resp = await client.post(
            f"/api/run-broker/authorization/{authorization_id}/cancel",
            json={"profile_name": PROFILE, "session_id": SESSION},
            headers=_owner_headers(),
        )
        return resp.status, await resp.json()

    status, body = _call_broker(db_path, cancel_call)
    assert (status, body) == (200, {"ok": True, "state": "cancelled"})
    assert authorization_id not in periphery._pending_authorizations
    assert json.loads(response_path.read_text(encoding="utf-8"))["state"] == "cancelled"


def test_run_liveness_check_rejects_a_foreign_runs_parked_entry(monkeypatch, tmp_path: Path):
    """A parked entry only proves liveness for ITS owner+profile. A run id parked
    by somebody else must not vouch for this request."""
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    periphery._auth_signal_consume(RUN_ID)
    _park_live_run(RUN_ID, profile=PEER_PROFILE, owner=PEER)

    pending = {
        "run_id": RUN_ID,
        "profile_name": PROFILE,
        "owner_open_id": OWNER,
    }
    assert periphery._authorization_run_is_live(pending) is False

    periphery._auth_signal_consume(RUN_ID)
    _park_live_run(RUN_ID)
    assert periphery._authorization_run_is_live(pending) is True
    assert periphery._authorization_run_is_live({**pending, "run_id": ""}) is False


# --------------------------------------------------------------------------- #
# 18. a dead run must not poison its session
#
# Regression for the 2026-09-08 live finding (session mtsdfft81v8go7): the
# one-pending guard was keyed on session_id and the only cleanup was the child's
# authorization_resolved event. A run stopped by the user never emits that, so
# the record survived its full 600s window and refused every later run in the
# session with "another authorization request is already pending for this run".
# PRD §4 says stop must terminate the wait, not poison the session.
# --------------------------------------------------------------------------- #


def _post_webui_runs(app, body, *, times: int, after_each=None):
    """POST /api/run-broker/runs `times` times on ONE event loop.

    aiohttp refuses to serve an Application from a second loop, so the two runs
    of a session have to share one; `after_each` snapshots state between them.
    """
    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            statuses = []
            for _ in range(times):
                resp = await client.post("/api/run-broker/runs", json=body)
                await resp.text()
                statuses.append(resp.status)
                if after_each is not None:
                    after_each()
            return statuses
        finally:
            await client.close()

    return asyncio.run(runner())


def test_a_terminated_run_leaves_no_pending_and_the_session_stays_usable(
    monkeypatch, tmp_path: Path
):
    """Drives two REAL runs through POST /api/run-broker/runs in one session.

    Run 1 raises an authorization card and then ends without ever emitting
    `authorization_resolved` — exactly what a user pressing 停止 produces. Run 2
    is a fresh run on the SAME session and must be able to raise its own card.
    """
    from hermes_multitenancy import agent_real, router as router_mod
    from hermes_multitenancy.webui_broker import periphery
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.setattr(router_mod, "_profile_name_to_home", lambda name: tmp_path)
    monkeypatch.setattr(
        periphery,
        "_webui_streamable_media_text",
        lambda text, **kw: ("", [text] if text else []),
        raising=False,
    )
    _stub_live_probe(monkeypatch, False)

    paths = [_derived_response_path("authreq_run1"), _derived_response_path("authreq_run2")]
    seen: list[list] = []
    turn = {"n": 0}

    async def fake_stream(event, profile_home, *, messages=None):
        index = turn["n"]
        turn["n"] += 1
        # The card is raised…
        yield "authorization_required", {
            "pending_ref": f"authreq_run{index + 1}",
            "session_key": SESSION_KEY,
            "service": "lark-cli",
            "scopes": ["im:message"],
            "response_path": str(paths[index]),
        }
        # …and the run then ends WITHOUT authorization_resolved (user stopped it).
        seen.append(sorted(periphery._pending_authorizations))
        yield "content", "stopped"

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)

    body = {
        "channel": "webui",
        "profile_name": "alice",
        "user_key": "ou_alice",
        "content": "读一下我的飞书文档",
        "session_id": "same-session",
    }
    app = create_run_broker_app(mark_seen=lambda _r: True, sandbox_available=lambda: True)

    after_run: list[list] = []
    statuses = _post_webui_runs(
        app,
        body,
        times=2,
        after_each=lambda: after_run.append(sorted(periphery._pending_authorizations)),
    )

    assert statuses == [200, 200]
    # During run 1 the record existed…
    assert len(seen[0]) == 1
    # …and the run ENDING tore it down, with no resolved event in sight.
    assert after_run[0] == []
    # Run 2, SAME session, raised its own request instead of being refused by
    # run 1's corpse — this is the live defect, inverted.
    assert len(seen[1]) == 1
    assert seen[1] != seen[0], "run 2 must get its own authorization_id"
    assert after_run[1] == []
    assert periphery._pending_authorizations == {}
    # Neither run was answered — the records were abandoned, not resolved.
    assert not paths[0].exists()
    assert not paths[1].exists()


def test_two_pending_requests_in_the_same_run_are_still_refused(monkeypatch, tmp_path: Path):
    """Re-keying the guard on run_id must NOT weaken one-per-Run."""
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    _stub_live_probe(monkeypatch, False)
    run_request = _run_request()
    first_path = _derived_response_path("authreq_same_run_1")
    second_path = _derived_response_path("authreq_same_run_2")

    first = periphery._register_pending_authorization(
        run_request,
        run_id=RUN_ID,
        payload={
            "pending_ref": "authreq_same_run_1",
            "service": "lark-cli",
            "scopes": ["im:message"],
            "response_path": str(first_path),
        },
    )
    second = periphery._register_pending_authorization(
        run_request,
        run_id=RUN_ID,
        payload={
            "pending_ref": "authreq_same_run_2",
            "service": "kep-cli-online",
            "scopes": ["read"],
            "response_path": str(second_path),
        },
    )

    assert first["kind"] == "authorization_required"
    assert second["kind"] == "authorization_resolved"
    assert second["payload"]["state"] == "failed"
    assert len(periphery._pending_authorizations) == 1
    assert not first_path.exists()
    assert json.loads(second_path.read_text(encoding="utf-8"))["state"] == "failed"


def test_a_different_run_in_the_same_session_is_not_refused(monkeypatch, tmp_path: Path):
    """The direct counterpart of the live defect, at the registry level: a live
    record from run A must not block run B just because they share a session."""
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    _stub_live_probe(monkeypatch, False)
    other_run = "run-signal-for-alice-2"
    _park_live_run(other_run)
    run_request = _run_request()

    first = periphery._register_pending_authorization(
        run_request,
        run_id=RUN_ID,
        payload={
            "pending_ref": "authreq_sess_run_a",
            "service": "lark-cli",
            "scopes": ["im:message"],
            "response_path": str(tmp_path / "auth" / "sess_run_a.json"),
        },
    )
    second = periphery._register_pending_authorization(
        run_request,
        run_id=other_run,
        payload={
            "pending_ref": "authreq_sess_run_b",
            "service": "kep-cli-online",
            "scopes": ["read"],
            "response_path": str(tmp_path / "auth" / "sess_run_b.json"),
        },
    )
    try:
        assert first["kind"] == "authorization_required"
        assert second["kind"] == "authorization_required"
        assert (
            first["payload"]["authorization_id"] != second["payload"]["authorization_id"]
        )
        assert len(periphery._pending_authorizations) == 2
    finally:
        periphery._auth_signal_consume(other_run)


def test_a_stopped_runs_late_confirm_is_still_rejected(monkeypatch, tmp_path: Path):
    """Tearing the record down at run end must not open a hole: a confirm that
    arrives afterwards finds nothing and writes nothing."""
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    _stub_live_probe(monkeypatch, False)
    response_path = _derived_response_path("authreq_stopped")
    decision = periphery._register_pending_authorization(
        _run_request(),
        run_id=RUN_ID,
        payload={
            "pending_ref": "authreq_stopped",
            "service": "lark-cli",
            "scopes": ["im:message"],
            "response_path": str(response_path),
        },
    )
    authorization_id = decision["payload"]["authorization_id"]

    # The user stops the run: the finally-block seam fires.
    assert periphery._clear_pending_authorizations_for_run(RUN_ID) == 1
    assert periphery._pending_authorizations == {}

    _stub_live_probe(monkeypatch, True)

    async def calls(client):
        resp = await client.post(
            f"/api/run-broker/authorization/{authorization_id}/confirm",
            json={"profile_name": PROFILE, "session_id": SESSION},
            headers=_owner_headers(),
        )
        return resp.status, await resp.json()

    status, body = _call_broker(db_path, calls)
    assert status == 404, body
    assert body.get("ok") is not True
    assert not response_path.exists()


# --------------------------------------------------------------------------- #
# 20. the rendezvous: the child may READ the verdict, never AUTHOR it
# --------------------------------------------------------------------------- #


def _register(monkeypatch, tmp_path: Path, *, pending_ref="authreq_probe", service="lark-cli",
              scopes=("im:message",), run_id=RUN_ID, decoy=None, probe=False):
    """Register one pending request through the production entry point."""
    from hermes_multitenancy.webui_broker import periphery

    payload = {
        "pending_ref": pending_ref,
        "service": service,
        "scopes": list(scopes),
    }
    if decoy is not None:
        payload["response_path"] = str(decoy)
    if probe is not None:
        _stub_live_probe(monkeypatch, probe)
    return periphery._register_pending_authorization(
        _run_request(), run_id=run_id, payload=payload
    )


def test_a_child_that_writes_its_own_success_file_gets_no_success(monkeypatch, tmp_path: Path):
    """THE attack: one write to a path the child already knows.

    The child mints `pending_ref`, so it knows its own response file before the
    parent has decided anything. If the terminal event were taken at face value,
    a single `{"state": "success"}` write would flip the card to 已授权 and drop
    the pending record — with nothing authorized and no human involved.

    The server therefore reconciles the child's claim against its OWN recorded
    decision: a success it never made is reported as `failed`.
    """
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    decision = _register(monkeypatch, tmp_path, pending_ref="authreq_selfsign")
    assert decision["kind"] == "authorization_required"
    authorization_id = decision["payload"]["authorization_id"]

    # One write, by the child, to its own path. No confirm, no probe, no human.
    response_path = _derived_response_path("authreq_selfsign")
    response_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_text(json.dumps({"state": "success", "reason": "mine"}), encoding="utf-8")

    resolved = periphery._clear_pending_authorization(
        {"pending_ref": "authreq_selfsign", "state": "success"}
    )
    assert resolved["state"] == "failed"
    assert resolved["authorization_id"] == authorization_id
    # And the registry is not left holding a "successful" request either.
    assert authorization_id not in periphery._pending_authorizations


def test_a_forged_pending_ref_cannot_report_success_either(monkeypatch, tmp_path: Path):
    """No record at all ⇒ no server decision ⇒ the success claim is refused."""
    from hermes_multitenancy.webui_broker import periphery

    resolved = periphery._clear_pending_authorization(
        {"pending_ref": "authreq_never_registered", "state": "success"}
    )
    assert resolved["state"] == "failed"


def test_an_authorization_resolved_event_from_a_foreign_run_is_ignored(
    monkeypatch, tmp_path: Path
):
    """``pending_ref`` is minted by the child and is not guaranteed unique
    across Runs. A ``authorization_resolved`` event whose event-side run_id
    does not match the RECORD's own frozen run_id — a collision or a forged
    replay — must never resolve, or tear down the flow of, that record.
    """
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    _stub_live_probe(monkeypatch, False)
    other_run = "run-signal-for-alice-foreign-event"
    _park_live_run(other_run)
    run_request = _run_request()

    decision = periphery._register_pending_authorization(
        run_request, run_id=RUN_ID,
        payload={"pending_ref": "authreq_collide", "service": "lark-cli", "scopes": ["im:message"]},
    )
    authorization_id = decision["payload"]["authorization_id"]
    periphery._pending_authorizations[authorization_id]["flow"] = {
        "kind": "lark", "session_id": "feishu-foreign-1",
        "verification_uri": "https://accounts.feishu.cn/device/foreign-1", "interval": 1,
    }

    cancelled: list = []
    monkeypatch.setattr(
        feishu_uat_auth, "cancel_session",
        lambda **kwargs: cancelled.append(kwargs["session_id"]) or {},
    )

    try:
        # A DIFFERENT run's event names this run's pending_ref — a collision or
        # a forged replay.
        resolved = periphery._clear_pending_authorization(
            {"pending_ref": "authreq_collide", "state": "success"}, run_id=other_run,
        )
        assert resolved.get("state") != "success"
        assert not resolved.get("authorization_id")
        assert authorization_id in periphery._pending_authorizations
        record = periphery._pending_authorizations[authorization_id]
        assert record["consumed"] is False
        assert record["flow"] is not None
        assert cancelled == [], "a foreign run's event tore down another run's flow"
        assert not _derived_response_path("authreq_collide").exists()

        # The record's OWN run's event still resolves it normally.
        resolved_own = periphery._clear_pending_authorization(
            {"pending_ref": "authreq_collide", "state": "success"}, run_id=RUN_ID,
        )
        assert resolved_own["authorization_id"] == authorization_id
        assert authorization_id not in periphery._pending_authorizations
    finally:
        periphery._auth_signal_consume(other_run)


def test_a_server_recorded_success_still_reaches_the_browser(monkeypatch, tmp_path: Path):
    """The reconciliation must not break the legitimate path.

    Same event shape as the forgery above — the difference is that this time the
    server itself recorded the decision.
    """
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    decision = _register(monkeypatch, tmp_path, pending_ref="authreq_real")
    authorization_id = decision["payload"]["authorization_id"]
    assert periphery._write_pending_authorization_response(
        authorization_id=authorization_id,
        owner_open_id=OWNER,
        profile_name=PROFILE,
        session_id=SESSION,
        state="success",
        reason="authorized",
    )
    resolved = periphery._clear_pending_authorization(
        {"pending_ref": "authreq_real", "state": "success"}
    )
    assert resolved["state"] == "success"


def test_the_response_path_the_child_names_is_never_used(monkeypatch, tmp_path: Path):
    """The parent writes where IT decided, not where the payload pointed."""
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    decoy = tmp_path / "attacker-chosen.json"
    _register(monkeypatch, tmp_path, pending_ref="authreq_one", decoy=decoy)
    # A second request in the same run is refused, and the refusal is written —
    # so this exercises a real parent write with a decoy path in the payload.
    second = _register(monkeypatch, tmp_path, pending_ref="authreq_two", decoy=decoy)

    assert second["payload"]["state"] == "failed"
    assert not decoy.exists()
    assert json.loads(
        _derived_response_path("authreq_two").read_text(encoding="utf-8")
    )["state"] == "failed"


@pytest.mark.parametrize(
    "pending_ref",
    ["../../../../etc/hermes-owned", "authreq_a/../../b", "/tmp/authreq_abs", "authreq_dot.json", ""],
)
def test_a_pending_ref_that_is_not_a_pending_ref_is_refused(monkeypatch, tmp_path: Path, pending_ref):
    """Path metacharacters never reach the derived filename."""
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    decision = _register(monkeypatch, tmp_path, pending_ref=pending_ref)
    assert decision["kind"] == "authorization_resolved"
    assert decision["payload"]["state"] == "failed"
    assert periphery._pending_authorizations == {}
    assert periphery._authorization_response_path(RUN_ID, pending_ref) is None


def test_the_verdict_is_published_atomically(monkeypatch, tmp_path: Path):
    """The child polls every 100ms; a half-written file reads as malformed JSON
    and turns a real success into a terminal failure. Publish by rename."""
    from hermes_multitenancy.webui_broker import periphery

    path = tmp_path / "rendezvous" / "authreq_atomic.json"
    assert periphery._write_authorization_response_file(str(path), "cancelled", "first")
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "cancelled"
    # No temp residue beside it, so nothing half-written is ever pollable.
    assert [p.name for p in path.parent.iterdir()] == ["authreq_atomic.json"]

    # Publication is a rename, so it replaces the visible file in one step —
    # including one the previous write left read-only. An in-place `write_text`
    # cannot do this, which is the deterministic signature of the difference.
    os.chmod(path, 0o444)
    assert periphery._write_authorization_response_file(str(path), "success", "authorized")
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "success"
    assert [p.name for p in path.parent.iterdir()] == ["authreq_atomic.json"]


def test_the_run_teardown_removes_the_whole_rendezvous_directory(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    _register(monkeypatch, tmp_path, pending_ref="authreq_gone")
    rendezvous = periphery._authorization_response_dir(RUN_ID)
    rendezvous.mkdir(parents=True, exist_ok=True)
    (rendezvous / "authreq_gone.json").write_text("{}", encoding="utf-8")

    periphery._clear_pending_authorizations_for_run(RUN_ID)
    assert not rendezvous.exists()
    assert periphery._pending_authorizations == {}


def test_the_linux_sandbox_mounts_the_rendezvous_read_only():
    """bwrap gives the child a private /tmp, so the directory must be bound in
    explicitly — and read-only, because a writable rendezvous is a one-write
    self-authorization that no file mode can prevent at a shared uid."""
    from pathlib import Path as _Path

    policy = (
        _Path(__file__).resolve().parent.parent
        / "hermes_multitenancy/sandbox/bwrap-default.args"
    ).read_text(encoding="utf-8")
    lines = [line.strip() for line in policy.splitlines() if line.strip() and not line.startswith("#")]
    assert "--ro-bind-try ${SHARED_HOME}/authorization ${SHARED_HOME}/authorization" in lines
    assert "--dir ${SHARED_HOME}/authorization" in lines
    assert "--bind ${SHARED_HOME}/authorization ${SHARED_HOME}/authorization" not in lines


def test_the_macos_sandbox_denies_writes_to_the_rendezvous():
    """sandbox-exec is last-match-wins, so the deny must come AFTER the
    /private/tmp allow and after the read allow, or it does nothing."""
    from pathlib import Path as _Path

    policy = (
        _Path(__file__).resolve().parent.parent
        / "hermes_multitenancy/sandbox/profile-default.sb"
    ).read_text(encoding="utf-8")
    read_at = policy.index('(allow file-read*\n    (subpath (string-append (param "SHARED_HOME") "/authorization")))')
    deny_at = policy.index('(deny file-write*\n    (subpath (string-append (param "SHARED_HOME") "/authorization")))')
    tmp_at = policy.index('(subpath "/private/tmp")')
    assert tmp_at < read_at < deny_at


# --------------------------------------------------------------------------- #
# 21. completion is noticed SERVER-side; the user only has to authorize
# --------------------------------------------------------------------------- #


def _lark_pending_with_flow(monkeypatch, tmp_path: Path, *, session_id="feishu-sess-1", probe=False):
    from hermes_multitenancy.webui_broker import periphery

    decision = _register(monkeypatch, tmp_path, pending_ref="authreq_lark", probe=probe)
    authorization_id = decision["payload"]["authorization_id"]
    periphery._pending_authorizations[authorization_id]["flow"] = {
        "kind": "lark",
        "session_id": session_id,
        "verification_uri": "https://accounts.feishu.cn/open-apis/auth/v1/device",
        "interval": 1,
    }
    return authorization_id


def _install_real_lark_verifier(monkeypatch, tmp_path: Path, *, granted_scope="im:message"):
    """Real `verify_service_authorized`; only the NETWORK leaves are stubbed.

    Stubbing the verifier is what hid the missing token exchange in the first
    place, so the credential really has to be on disk for this to pass.
    """
    from hermes_multitenancy import feishu_uat_auth

    monkeypatch.setattr(feishu_uat_auth, "refresh_uat_if_needed", lambda **_k: None)
    monkeypatch.setattr(feishu_uat_auth, "_load_vault_uat_payload", lambda *a, **k: None)
    monkeypatch.setattr(feishu_uat_auth, "_fetch_user_info", lambda _t: {"open_id": OWNER})

    uat_dir = tmp_path / "profiles" / PROFILE / "feishu_uat"

    def persist_credential():
        uat_dir.mkdir(parents=True, exist_ok=True)
        (uat_dir / f"{OWNER}.json").write_text(
            json.dumps(
                {
                    "access_token": "u-exchanged",
                    "expires_at": int(time.time() * 1000) + 600_000,
                    "scope": granted_scope,
                    "granted_at": int(time.time() * 1000),
                }
            ),
            encoding="utf-8",
        )

    return persist_credential


def test_confirm_advances_the_feishu_device_flow_before_verifying(monkeypatch, tmp_path: Path):
    """`start_session` only mints the device code. `poll_session` is the only
    path that exchanges it for a token and persists the credential.

    Without that call the user completes the Feishu consent screen and then
    waits out the whole 10-minute window, because the confirmation probe looks
    at stored credentials that nothing ever stored.
    """
    from hermes_multitenancy import feishu_uat_auth

    db_path = _seed_routing(monkeypatch, tmp_path)
    # The REAL verifier all the way through — stubbing it to True is exactly
    # what hid the missing exchange. Only the network leaves are replaced.
    persist = _install_real_lark_verifier(monkeypatch, tmp_path)
    authorization_id = _lark_pending_with_flow(monkeypatch, tmp_path, probe=None)

    polls: list = []

    def fake_poll(*, session_id, profile_name, open_id, shared_home=None):
        polls.append(session_id)
        persist()  # exactly what the real exchange does: store the UAT
        return {"status": "success"}

    monkeypatch.setattr(feishu_uat_auth, "poll_session", fake_poll)

    async def calls(client):
        resp = await client.post(
            f"/api/run-broker/authorization/{authorization_id}/confirm",
            json={"profile_name": PROFILE, "session_id": SESSION},
            headers=_owner_headers(),
        )
        return resp.status, await resp.json()

    status, body = _call_broker(db_path, calls)
    assert (status, body) == (200, {"ok": True, "state": "success"})
    assert polls == ["feishu-sess-1"]
    assert json.loads(
        _derived_response_path("authreq_lark").read_text(encoding="utf-8")
    )["state"] == "success"


def test_the_watcher_resolves_without_the_user_touching_anything(monkeypatch, tmp_path: Path):
    """sunke's requirement: click 去授权, finish in Feishu, and nothing else.

    Feishu exposes no completion hook, so this is polling — at the session's own
    interval, bounded by the request window. Nobody calls confirm here.
    """
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    persist = _install_real_lark_verifier(monkeypatch, tmp_path)
    authorization_id = _lark_pending_with_flow(monkeypatch, tmp_path, probe=None)

    state = {"consented": False}

    def fake_poll(*, session_id, profile_name, open_id, shared_home=None):
        if not state["consented"]:
            return {"status": "pending"}
        persist()
        return {"status": "success"}

    monkeypatch.setattr(feishu_uat_auth, "poll_session", fake_poll)

    async def drive():
        task = asyncio.create_task(
            periphery._watch_authorization_completion(authorization_id)
        )
        await asyncio.sleep(1.2)
        assert not task.done(), "resolved before the user consented"
        state["consented"] = True
        await asyncio.wait_for(task, timeout=10)

    asyncio.run(drive())

    assert json.loads(
        _derived_response_path("authreq_lark").read_text(encoding="utf-8")
    )["state"] == "success"
    assert authorization_id in periphery._pending_authorizations
    assert periphery._pending_authorizations[authorization_id]["state"] == "success"


def test_the_watcher_does_not_outlive_its_run(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    authorization_id = _lark_pending_with_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(
        feishu_uat_auth, "poll_session", lambda **_k: {"status": "pending"}
    )

    async def drive():
        periphery._mark_authorization_run_finished(RUN_ID)
        await asyncio.wait_for(
            periphery._watch_authorization_completion(authorization_id), timeout=5
        )

    asyncio.run(drive())


def test_a_failed_verification_after_the_lark_exchange_completes_is_terminal(
    monkeypatch, tmp_path: Path
):
    """The device exchange itself finished (Lark reports ``success``) but the
    live credential check still fails — a scope mismatch, a store write that
    never landed, whatever. Nothing will ever poll this exchange again with a
    different answer, so this has to resolve TERMINAL ``failed`` through the
    one guarded writer instead of silently staying ``pending`` until the whole
    window expires with the watcher polling forever for nothing new.
    """
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    authorization_id = _lark_pending_with_flow(monkeypatch, tmp_path, probe=None)

    polls: list = []
    verify_calls: list = []

    def fake_poll(*, session_id, profile_name, open_id, shared_home=None):
        polls.append(session_id)
        return {"status": "success"}

    monkeypatch.setattr(feishu_uat_auth, "poll_session", fake_poll)
    _stub_live_probe(monkeypatch, False, calls=verify_calls)

    outcome = periphery._authorization_probe_and_resolve(authorization_id)
    assert outcome == "failed"
    assert polls == ["feishu-sess-1"]
    assert len(verify_calls) == 1
    assert periphery._pending_authorizations[authorization_id]["consumed"] is True
    assert periphery._pending_authorizations[authorization_id]["state"] == "failed"
    assert json.loads(
        _derived_response_path("authreq_lark").read_text(encoding="utf-8")
    )["state"] == "failed"

    # A second look (what the watcher's next poll interval would otherwise do)
    # must not re-run the exchange or the live check at all: the record is no
    # longer live, so this is a no-op — there is no second attempt.
    outcome_again = periphery._authorization_probe_and_resolve(authorization_id)
    assert outcome_again == "not_found"
    assert polls == ["feishu-sess-1"]
    assert len(verify_calls) == 1


def test_the_kep_callback_landing_resolves_the_request(monkeypatch, tmp_path: Path):
    """The EXISTING unauthenticated callback route is the trigger — no new
    endpoint, and the landing is never taken as proof: the credential still has
    to verify server-side before anything is written."""
    from hermes_multitenancy import credential_hub_auth as cha
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    _seed_shared_routing(tmp_path)
    # Real `verify_service_authorized`; only the kep-auth subprocess+HTTPS leaf
    # is stubbed, and it flips only once the user has actually come back.
    logged_in = {"value": False}
    monkeypatch.setattr(cha, "kep_cli_logged_in", lambda *a, **k: logged_in["value"])
    monkeypatch.setattr(cha, "complete_kep_callback", lambda sid, query: logged_in.__setitem__("value", True))

    decision = _register(
        monkeypatch, tmp_path, pending_ref="authreq_kepcb", service="kep-cli-online",
        scopes=["read"], probe=None,
    )
    assert decision["kind"] == "authorization_required"
    authorization_id = decision["payload"]["authorization_id"]
    periphery._pending_authorizations[authorization_id]["flow"] = {
        "kind": "kep",
        "proc": None,
        "env": "online",
        "verification_uri": "https://kep/auth",
        "callback_sid": "cbsid-1",
    }

    async def calls(client):
        resp = await client.get("/api/run-broker/credentials/kep-cli/callback/cbsid-1")
        return resp.status, await resp.text()

    status, page = _call_broker(db_path, calls)
    assert status == 200
    assert "window.close()" in page and "可以关闭此页" in page
    assert json.loads(
        _derived_response_path("authreq_kepcb").read_text(encoding="utf-8")
    )["state"] == "success"


def test_a_callback_landing_that_does_not_verify_emits_no_success(monkeypatch, tmp_path: Path):
    """The callback itself landed (`complete_kep_callback` did not raise) but
    the live identity check still says not-logged-in. kep-cli has no watcher
    to retry this later, so this must be the TERMINAL answer — a failure
    resolved through the one guarded writer — not a silent `pending` that
    waits out the whole window with nothing left to trigger it again.
    """
    from hermes_multitenancy import credential_hub_auth as cha
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    _seed_shared_routing(tmp_path)
    monkeypatch.setattr(cha, "complete_kep_callback", lambda sid, query: "ok")
    monkeypatch.setattr(cha, "kep_cli_logged_in", lambda *a, **k: False)

    decision = _register(
        monkeypatch, tmp_path, pending_ref="authreq_kepbad", service="kep-cli-online",
        scopes=["read"], probe=None,
    )
    authorization_id = decision["payload"]["authorization_id"]
    periphery._pending_authorizations[authorization_id]["flow"] = {
        "kind": "kep", "proc": None, "env": "online", "callback_sid": "cbsid-2",
        "verification_uri": "https://kep/auth",
    }

    async def calls(client):
        resp = await client.get("/api/run-broker/credentials/kep-cli/callback/cbsid-2")
        return resp.status, await resp.text()

    status, page = _call_broker(db_path, calls)
    assert status == 200
    assert "认证未生效" in page
    assert json.loads(
        _derived_response_path("authreq_kepbad").read_text(encoding="utf-8")
    )["state"] == "failed"
    assert periphery._pending_authorizations[authorization_id]["consumed"] is True
    assert periphery._pending_authorizations[authorization_id]["state"] == "failed"


# --------------------------------------------------------------------------- #
# 22. scopes are server policy, not model input
# --------------------------------------------------------------------------- #


def test_a_scope_the_app_cannot_grant_never_reaches_registration(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    decision = _register(
        monkeypatch, tmp_path, pending_ref="authreq_scope", scopes=["drive:drive:write"]
    )
    assert decision["payload"]["state"] == "failed"
    assert periphery._pending_authorizations == {}


@pytest.mark.parametrize("scope", ["Docx:Document", "im message", "../etc/passwd", "im:message;rm"])
def test_a_malformed_scope_is_refused(monkeypatch, tmp_path: Path, scope):
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    decision = _register(monkeypatch, tmp_path, pending_ref="authreq_badscope", scopes=[scope])
    assert decision["payload"]["state"] == "failed"
    assert periphery._pending_authorizations == {}


def test_scope_policy_fails_closed_when_the_app_scope_set_is_unknown(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import authorization_verify, feishu_uat_auth

    def boom(**_kwargs):
        raise RuntimeError("feishu unreachable")

    monkeypatch.setattr(feishu_uat_auth, "login_oauth_scope", boom)
    with pytest.raises(authorization_verify.ScopePolicyError):
        authorization_verify.normalize_requested_scopes(
            "lark-cli", ["im:message"], shared_home=tmp_path
        )


def test_kep_scopes_are_replaced_by_the_server(monkeypatch, tmp_path: Path):
    """kep-cli logins cannot be narrowed, so a model-chosen scope string would be
    consent-card decoration the server never enforces."""
    from hermes_multitenancy import authorization_verify
    from hermes_multitenancy.webui_broker import periphery

    assert authorization_verify.normalize_requested_scopes(
        "kep-cli-online", ["delete everything", "read"], shared_home=tmp_path
    ) == ["kep-cli"]

    _seed_routing(monkeypatch, tmp_path)
    decision = _register(
        monkeypatch, tmp_path, pending_ref="authreq_kepscope", service="kep-cli-online",
        scopes=["全部权限"],
    )
    assert decision["kind"] == "authorization_required"
    assert decision["payload"]["scopes"] == ["kep-cli"]


def test_a_credential_that_does_not_cover_the_request_is_not_authorized(monkeypatch, tmp_path: Path):
    """Same owner, live token, wrong grant. A contact-only credential must not
    satisfy a document-scope request."""
    from hermes_multitenancy import authorization_verify

    _seed_shared_routing(tmp_path)
    _install_real_lark_verifier(monkeypatch, tmp_path, granted_scope="contact:user.base:readonly")()

    assert authorization_verify.verify_service_authorized(
        "lark-cli",
        profile_name=PROFILE,
        open_id=OWNER,
        profile_dir=tmp_path / "profiles" / PROFILE,
        shared_home=tmp_path,
        required_scopes=["docx:document:readonly"],
    ) is False

    assert authorization_verify.verify_service_authorized(
        "lark-cli",
        profile_name=PROFILE,
        open_id=OWNER,
        profile_dir=tmp_path / "profiles" / PROFILE,
        shared_home=tmp_path,
        required_scopes=["contact:user.base:readonly"],
    ) is True


def test_the_frozen_scopes_are_what_the_probe_is_asked_about(monkeypatch, tmp_path: Path):
    calls: list = []
    _seed_routing(monkeypatch, tmp_path)
    _stub_live_probe(monkeypatch, False, calls=calls)
    _register(
        monkeypatch, tmp_path, pending_ref="authreq_frozen", scopes=["im:message"], probe=None
    )
    assert calls and calls[0]["required_scopes"] == ["im:message"]


# --------------------------------------------------------------------------- #
# 23. kep-cli must bind the ACTOR, not just the profile
# --------------------------------------------------------------------------- #


def test_a_shared_agent_grantee_cannot_ride_the_owners_kep_credential(monkeypatch, tmp_path: Path):
    """Shared-agent routing legitimately pairs the OWNER's execution profile with
    the GRANTEE's identity. kep-cli has one store per profile and no actor
    dimension, so without this check the grantee is declared authorized on the
    owner's credential — and `authorize` starts a login into the owner's profile.
    """
    from hermes_multitenancy import authorization_verify, credential_hub_auth

    _seed_shared_routing(tmp_path)
    monkeypatch.setattr(credential_hub_auth, "kep_cli_logged_in", lambda *a, **k: True)

    # The owner of this profile: authorized.
    assert authorization_verify.verify_service_authorized(
        "kep-cli-online",
        profile_name=PROFILE,
        open_id=OWNER,
        profile_dir=tmp_path / "profiles" / PROFILE,
        shared_home=tmp_path,
        required_scopes=["kep-cli"],
    ) is True

    # A grantee running ON that shared agent: same profile, different actor.
    assert authorization_verify.verify_service_authorized(
        "kep-cli-online",
        profile_name=PROFILE,
        open_id=PEER,
        profile_dir=tmp_path / "profiles" / PROFILE,
        shared_home=tmp_path,
        required_scopes=["kep-cli"],
    ) is False

    # And an identity that routes nowhere at all is refused too.
    assert authorization_verify.verify_service_authorized(
        "kep-cli-online",
        profile_name=PROFILE,
        open_id="ou_stranger",
        profile_dir=tmp_path / "profiles" / PROFILE,
        shared_home=tmp_path,
        required_scopes=["kep-cli"],
    ) is False


# --------------------------------------------------------------------------- #
# 24. lark-cli verification reads the RUNTIME's credential, not one store
# --------------------------------------------------------------------------- #


def test_a_vault_only_credential_verifies_without_prompting(monkeypatch, tmp_path: Path):
    """The runtime resolves vault-or-JSON. Reading only the plaintext JSON made a
    perfectly good vault credential look absent and asked the user to authorize
    something they had already authorized."""
    from hermes_multitenancy import authorization_verify, feishu_uat_auth

    _seed_shared_routing(tmp_path)
    monkeypatch.setattr(feishu_uat_auth, "refresh_uat_if_needed", lambda **_k: None)
    monkeypatch.setattr(feishu_uat_auth, "_fetch_user_info", lambda _t: {"open_id": OWNER})
    monkeypatch.setattr(
        feishu_uat_auth,
        "_load_vault_uat_payload",
        lambda *a, **k: {
            "access_token": "vault-token",
            "expires_at": int(time.time() * 1000) + 600_000,
            "scope": "im:message",
            "granted_at": int(time.time() * 1000),
        },
    )
    # Deliberately no JSON file on disk.
    assert not (tmp_path / "profiles" / PROFILE / "feishu_uat").exists()

    assert authorization_verify.verify_service_authorized(
        "lark-cli",
        profile_name=PROFILE,
        open_id=OWNER,
        profile_dir=tmp_path / "profiles" / PROFILE,
        shared_home=tmp_path,
        required_scopes=["im:message"],
    ) is True


def test_divergent_stores_verify_the_token_the_runtime_would_use(monkeypatch, tmp_path: Path):
    """Two stores, different tokens. Verifying the stale one while lark-cli goes
    on to use the fresh one (or vice versa) is a verification that proves
    nothing about the call that follows."""
    from hermes_multitenancy import authorization_verify, feishu_uat_auth

    _seed_shared_routing(tmp_path)
    now_ms = int(time.time() * 1000)
    uat_dir = tmp_path / "profiles" / PROFILE / "feishu_uat"
    uat_dir.mkdir(parents=True, exist_ok=True)
    (uat_dir / f"{OWNER}.json").write_text(
        json.dumps(
            {
                "access_token": "stale-json-token",
                "expires_at": now_ms + 600_000,
                "scope": "im:message",
                "granted_at": now_ms - 900_000,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(feishu_uat_auth, "refresh_uat_if_needed", lambda **_k: None)
    monkeypatch.setattr(
        feishu_uat_auth,
        "_load_vault_uat_payload",
        lambda *a, **k: {
            "access_token": "fresh-vault-token",
            "expires_at": now_ms + 600_000,
            "scope": "im:message",
            "granted_at": now_ms,
        },
    )
    probed: list = []

    def fake_user_info(token):
        probed.append(token)
        return {"open_id": OWNER}

    monkeypatch.setattr(feishu_uat_auth, "_fetch_user_info", fake_user_info)

    assert authorization_verify.verify_service_authorized(
        "lark-cli",
        profile_name=PROFILE,
        open_id=OWNER,
        profile_dir=tmp_path / "profiles" / PROFILE,
        shared_home=tmp_path,
        required_scopes=["im:message"],
    ) is True
    assert probed == ["fresh-vault-token"]


# --------------------------------------------------------------------------- #
# 25. Stop ends the authorization lifecycle, refresh/reconnect does not
# --------------------------------------------------------------------------- #


def test_stop_cancels_the_request_and_a_late_confirm_can_never_land(monkeypatch, tmp_path: Path):
    """The broker keeps running after the transport disconnects, so without an
    explicit Stop hook the request stayed live and a confirm arriving later was
    still acceptable. Stop must invalidate it server-side, tear the login flow
    down, and answer the blocked child.
    """
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    decision = _register(monkeypatch, tmp_path, pending_ref="authreq_stop")
    authorization_id = decision["payload"]["authorization_id"]

    cancelled: list = []
    from hermes_multitenancy import feishu_uat_auth

    monkeypatch.setattr(
        feishu_uat_auth,
        "cancel_session",
        lambda **kwargs: cancelled.append(kwargs["session_id"]) or {},
    )
    periphery._pending_authorizations[authorization_id]["flow"] = {
        "kind": "lark", "session_id": "feishu-stop-1", "verification_uri": "https://x",
    }

    async def calls(client):
        stop = await client.post(
            "/api/run-broker/authorization/stop",
            json={"profile_name": PROFILE, "session_id": SESSION},
            headers=_owner_headers(),
        )
        stop_body = (stop.status, await stop.json())
        # Exactly the "late confirm" the review reproduced: after Stop, with the
        # credential genuinely live.
        _stub_live_probe(monkeypatch, True)
        late = await client.post(
            f"/api/run-broker/authorization/{authorization_id}/confirm",
            json={"profile_name": PROFILE, "session_id": SESSION},
            headers=_owner_headers(),
        )
        return stop_body, (late.status, await late.json())

    (stop_status, stop_body), (late_status, late_body) = _call_broker(db_path, calls)
    assert stop_status == 200 and stop_body == {"ok": True, "cancelled": 1}
    assert late_status in (404, 409)
    assert late_body.get("ok") is not True
    # The device flow was torn down, and the child was told cancelled — not left
    # blocked, and never told success.
    assert cancelled == ["feishu-stop-1"]
    assert authorization_id not in periphery._pending_authorizations
    written = json.loads(_derived_response_path("authreq_stop").read_text(encoding="utf-8"))
    assert written["state"] == "cancelled"


def test_cancelling_one_of_two_records_sharing_a_flow_does_not_kill_it_for_the_other(
    monkeypatch, tmp_path: Path
):
    """Two Runs ended up sharing one Feishu device session (e.g. via
    ``find_active_session`` reuse). Tearing one Run's pending record down must
    never cancel the login the OTHER Run is still relying on to complete —
    only the LAST owner releasing it may actually cancel the provider session.
    """
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    _stub_live_probe(monkeypatch, False)
    other_run = "run-signal-for-alice-shared-flow"
    _park_live_run(other_run)
    run_request = _run_request()

    shared_flow = {
        "kind": "lark",
        "session_id": "feishu-shared-flow-1",
        "verification_uri": "https://accounts.feishu.cn/device/shared-flow-1",
        "interval": 1,
    }
    first = periphery._register_pending_authorization(
        run_request, run_id=RUN_ID,
        payload={"pending_ref": "authreq_shared_a", "service": "lark-cli", "scopes": ["im:message"]},
    )
    second = periphery._register_pending_authorization(
        run_request, run_id=other_run,
        payload={"pending_ref": "authreq_shared_b", "service": "lark-cli", "scopes": ["im:message"]},
    )
    authorization_id_a = first["payload"]["authorization_id"]
    authorization_id_b = second["payload"]["authorization_id"]
    periphery._pending_authorizations[authorization_id_a]["flow"] = dict(shared_flow)
    periphery._pending_authorizations[authorization_id_b]["flow"] = dict(shared_flow)

    cancelled: list = []
    monkeypatch.setattr(
        feishu_uat_auth, "cancel_session",
        lambda **kwargs: cancelled.append(kwargs["session_id"]) or {},
    )

    def _cancel(authorization_id):
        async def calls(client):
            resp = await client.post(
                f"/api/run-broker/authorization/{authorization_id}/cancel",
                json={"profile_name": PROFILE, "session_id": SESSION},
                headers=_owner_headers(),
            )
            return resp.status, await resp.json()

        return _call_broker(db_path, calls)

    try:
        status, body = _cancel(authorization_id_a)
        assert (status, body) == (200, {"ok": True, "state": "cancelled"})
        assert cancelled == [], "cancelling one owner tore down a flow the other still needs"
        assert authorization_id_a not in periphery._pending_authorizations
        assert authorization_id_b in periphery._pending_authorizations
        assert periphery._pending_authorizations[authorization_id_b]["flow"] is not None

        status, body = _cancel(authorization_id_b)
        assert (status, body) == (200, {"ok": True, "state": "cancelled"})
        assert cancelled == [
            "feishu-shared-flow-1"
        ], "the last owner releasing the flow must cancel the shared session"
    finally:
        periphery._auth_signal_consume(other_run)


def test_after_stop_a_new_request_in_the_same_session_is_admitted(monkeypatch, tmp_path: Path):
    """The other half of the same defect: the dead record must not keep refusing
    later requests as duplicates."""
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    _register(monkeypatch, tmp_path, pending_ref="authreq_before_stop")
    assert periphery._cancel_session_authorizations(
        owner_open_id=OWNER, profile_name=PROFILE, session_id=SESSION
    ) == 1

    next_run = "run-signal-for-alice-after-stop"
    _park_live_run(next_run)
    decision = _register(
        monkeypatch, tmp_path, pending_ref="authreq_after_stop", run_id=next_run
    )
    assert decision["kind"] == "authorization_required"


def test_a_record_whose_run_has_died_does_not_block_its_successor(monkeypatch, tmp_path: Path):
    """Duplicate admission must ignore — and remove — records that can never
    complete, instead of treating a dead run's leftovers as "one is pending"."""
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    first = _register(monkeypatch, tmp_path, pending_ref="authreq_dead")
    assert first["kind"] == "authorization_required"

    # The run exits without the child ever emitting authorization_resolved
    # (killed child / hard stop). The record itself is left behind on purpose
    # here — this asserts the admission path copes, not the teardown path.
    periphery._mark_authorization_run_finished(RUN_ID)

    later_run = "run-signal-for-alice-3"
    _park_live_run(later_run)
    second = _register(monkeypatch, tmp_path, pending_ref="authreq_live", run_id=later_run)
    assert second["kind"] == "authorization_required"
    assert not any(
        p.get("pending_ref") == "authreq_dead"
        for p in periphery._pending_authorizations.values()
    )


def test_liveness_does_not_come_from_the_replay_cache(monkeypatch, tmp_path: Path):
    """A run that emitted whole-message `auth_required` KEEPS its parked replay
    entry past run end. Reading liveness off that entry is what let a confirm
    land after Stop."""
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    decision = _register(monkeypatch, tmp_path, pending_ref="authreq_replay")
    pending = periphery._pending_authorizations[decision["payload"]["authorization_id"]]
    assert periphery._authorization_run_is_live(pending)

    periphery._mark_authorization_run_finished(RUN_ID)
    # The replay entry is deliberately still there…
    assert periphery._auth_signal_lookup(RUN_ID) is not None
    # …and it is NOT evidence that the run is executing.
    assert not periphery._authorization_run_is_live(pending)


# --------------------------------------------------------------------------- #
# 26. the already-authorized fast path goes through the SAME guard
# --------------------------------------------------------------------------- #


def _probe_that(monkeypatch, side_effect):
    """A live probe that takes time and changes the world while it runs."""
    from hermes_multitenancy import authorization_verify

    def fake_verify(service, *, profile_name, open_id, profile_dir, shared_home,
                    required_scopes=()):
        side_effect()
        return True

    monkeypatch.setattr(authorization_verify, "verify_service_authorized", fake_verify)


def test_the_fast_path_emits_no_success_when_the_run_died_during_the_probe(
    monkeypatch, tmp_path: Path
):
    """The probe shells out and does HTTPS — seconds, not microseconds. A Stop
    landing inside that window used to still produce a success card."""
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    _probe_that(monkeypatch, lambda: periphery._mark_authorization_run_finished(RUN_ID))

    decision = _register(monkeypatch, tmp_path, pending_ref="authreq_fp_dead", probe=None)
    assert decision["kind"] == "authorization_resolved"
    assert decision["payload"]["state"] != "success"
    assert periphery._pending_authorizations == {}
    written = json.loads(
        _derived_response_path("authreq_fp_dead").read_text(encoding="utf-8")
    )
    assert written["state"] != "success"


def test_the_fast_path_emits_no_success_when_the_deadline_passed_during_the_probe(
    monkeypatch, tmp_path: Path
):
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)

    def expire_everything():
        for record in periphery._pending_authorizations.values():
            record["expires_at"] = time.time() - 1

    _probe_that(monkeypatch, expire_everything)

    decision = _register(monkeypatch, tmp_path, pending_ref="authreq_fp_late", probe=None)
    assert decision["payload"]["state"] == "expired"
    assert periphery._pending_authorizations == {}
    written = json.loads(
        _derived_response_path("authreq_fp_late").read_text(encoding="utf-8")
    )
    assert written["state"] == "expired"


def test_the_fast_path_emits_no_success_when_it_was_cancelled_during_the_probe(
    monkeypatch, tmp_path: Path
):
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)

    def cancel_everything():
        periphery._cancel_session_authorizations(
            owner_open_id=OWNER, profile_name=PROFILE, session_id=SESSION
        )

    _probe_that(monkeypatch, cancel_everything)

    decision = _register(monkeypatch, tmp_path, pending_ref="authreq_fp_cancel", probe=None)
    assert decision["payload"]["state"] != "success"
    written = json.loads(
        _derived_response_path("authreq_fp_cancel").read_text(encoding="utf-8")
    )
    assert written["state"] != "success"


def test_the_fast_path_emits_no_success_when_the_response_cannot_be_written(
    monkeypatch, tmp_path: Path
):
    """An unwritable rendezvous used to produce a success card while the tool
    call stayed blocked for the full window. Success is announced only after the
    answer has actually been published."""
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    # A real, un-writable directory (no owner write bit ⇒ no file creation).
    rendezvous = periphery._authorization_response_dir(RUN_ID)
    rendezvous.mkdir(parents=True, exist_ok=True)
    os.chmod(rendezvous, 0o500)
    try:
        decision = _register(monkeypatch, tmp_path, pending_ref="authreq_fp_ro", probe=True)
        assert decision["kind"] == "authorization_resolved"
        assert decision["payload"]["state"] != "success"
        assert periphery._pending_authorizations == {}
        assert not (rendezvous / "authreq_fp_ro.json").exists()
    finally:
        os.chmod(rendezvous, 0o700)


def test_the_fast_path_still_succeeds_on_the_happy_case(monkeypatch, tmp_path: Path):
    """The guard must not break the case it guards."""
    from hermes_multitenancy.webui_broker import periphery

    _seed_routing(monkeypatch, tmp_path)
    decision = _register(monkeypatch, tmp_path, pending_ref="authreq_fp_ok", probe=True)
    assert decision["kind"] == "authorization_resolved"
    assert decision["payload"]["state"] == "success"
    assert json.loads(
        _derived_response_path("authreq_fp_ok").read_text(encoding="utf-8")
    )["state"] == "success"


# --------------------------------------------------------------------------- #
# 27. authorize is idempotent — it must not kill the flow it hands back
# --------------------------------------------------------------------------- #


def _stub_feishu_device_flow(monkeypatch, *, starts: list, cancels: list, active: dict):
    from hermes_multitenancy import feishu_uat_auth

    def fake_start(*, profile_name, open_id, scope=None, shared_home=None):
        starts.append(scope)
        session = {
            "session_id": f"feishu-{len(starts)}",
            "verification_uri": f"https://accounts.feishu.cn/device/{len(starts)}",
            "interval": 1,
            "status": "pending",
        }
        active["session"] = session
        return session

    def fake_find(*, profile_name, open_id):
        return dict(active["session"]) if active.get("session") else None

    def fake_cancel(*, session_id, profile_name, open_id):
        cancels.append(session_id)
        if active.get("session", {}).get("session_id") == session_id:
            active["session"] = None
        return {}

    monkeypatch.setattr(feishu_uat_auth, "start_session", fake_start)
    monkeypatch.setattr(feishu_uat_auth, "find_active_session", fake_find)
    monkeypatch.setattr(feishu_uat_auth, "cancel_session", fake_cancel)
    monkeypatch.setattr(feishu_uat_auth, "poll_session", lambda **_k: {"status": "pending"})


def test_a_second_authorize_reuses_the_flow_instead_of_killing_it(monkeypatch, tmp_path: Path):
    """Reopened card / second tab. The old code called `find_active_session` to
    reuse the session and then immediately cancelled it, so the URL it returned
    pointed at a device session it had just destroyed."""
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    decision = _register(monkeypatch, tmp_path, pending_ref="authreq_idem")
    authorization_id = decision["payload"]["authorization_id"]

    starts: list = []
    cancels: list = []
    active: dict = {}
    _stub_feishu_device_flow(monkeypatch, starts=starts, cancels=cancels, active=active)

    async def calls(client):
        out = []
        for _ in range(3):
            resp = await client.post(
                f"/api/run-broker/authorization/{authorization_id}/authorize",
                json={"profile_name": PROFILE, "session_id": SESSION},
                headers=_owner_headers(),
            )
            out.append((resp.status, await resp.json()))
        return out

    results = _call_broker(db_path, calls)
    urls = set()
    for status, body in results:
        assert status == 200, body
        assert body["ok"] is True
        urls.add(body["verification_uri"])

    assert len(starts) == 1, "a second authorize minted a second device code"
    assert cancels == [], "authorize cancelled the session it handed back"
    assert len(urls) == 1
    assert active["session"]["session_id"] == periphery._pending_authorizations[
        authorization_id
    ]["flow"]["session_id"]


def test_concurrent_authorize_calls_start_exactly_one_flow(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    decision = _register(monkeypatch, tmp_path, pending_ref="authreq_race")
    authorization_id = decision["payload"]["authorization_id"]

    starts: list = []
    cancels: list = []
    active: dict = {}
    _stub_feishu_device_flow(monkeypatch, starts=starts, cancels=cancels, active=active)

    async def calls(client):
        async def one():
            resp = await client.post(
                f"/api/run-broker/authorization/{authorization_id}/authorize",
                json={"profile_name": PROFILE, "session_id": SESSION},
                headers=_owner_headers(),
            )
            return resp.status, await resp.json()

        return await asyncio.gather(*(one() for _ in range(4)))

    results = _call_broker(db_path, calls)
    assert all(status == 200 and body["ok"] for status, body in results), results
    assert len({body["verification_uri"] for _s, body in results}) == 1
    assert len(starts) == 1
    assert cancels == []


def test_authorize_replaces_a_flow_that_is_genuinely_dead(monkeypatch, tmp_path: Path):
    """Reuse must not mean "never start a new one": a session that has expired or
    been cancelled elsewhere has to be replaced, or the card hands out a URL that
    can no longer be completed."""
    from hermes_multitenancy.webui_broker import periphery

    db_path = _seed_routing(monkeypatch, tmp_path)
    decision = _register(monkeypatch, tmp_path, pending_ref="authreq_dead_flow")
    authorization_id = decision["payload"]["authorization_id"]

    starts: list = []
    cancels: list = []
    active: dict = {}
    _stub_feishu_device_flow(monkeypatch, starts=starts, cancels=cancels, active=active)

    async def first(client):
        resp = await client.post(
            f"/api/run-broker/authorization/{authorization_id}/authorize",
            json={"profile_name": PROFILE, "session_id": SESSION},
            headers=_owner_headers(),
        )
        return await resp.json()

    body_one = _call_broker(db_path, first)
    active["session"] = None  # the device session died out of band

    body_two = _call_broker(db_path, first)
    assert body_two["ok"] is True
    assert body_two["verification_uri"] != body_one["verification_uri"]
    assert len(starts) == 2


# ── 全量灰度：request-authorization 是 WebUI 的默认工具集 ───────────────
# 生产 2151 个 profile 没有一个在 config.yaml 里列这个工具集，手工逐个添加也不
# 持久（2026-09-09 实测：写配置的进程用旧内存副本回写，把手加的那行抹掉）。默认
# 集是唯一能覆盖全量又不怕回写的位置。

def _resolve_for(platform_key: str, config: dict, resolver=None):
    from hermes_multitenancy.agent_real import subprocess_env

    if resolver is None:
        def resolver(cfg, key, **_kwargs):  # noqa: ANN001 - test double
            return ["file", "web"]

    return subprocess_env._resolve_enabled_toolsets(
        config,
        platform_key,
        platform_tools_resolver=resolver,
    )


_MERGE_DEFAULT_CONFIG = {
    "platform_toolsets": {"webui": ["lark-cli"], "feishu": ["lark-cli"]},
    "multitenancy": {
        "toolsets_mode": "explicit",
        "platform_toolsets_mode": {"webui": "merge_default", "feishu": "merge_default"},
    },
}


def test_webui_merge_default_profile_gets_inline_authorization():
    """生产上 2117/2117 个自定义 profile 都是这条路径。"""
    resolved = _resolve_for("webui", dict(_MERGE_DEFAULT_CONFIG))
    assert "request-authorization" in resolved
    assert "lark-cli" in resolved


def test_webui_profile_without_platform_toolsets_gets_inline_authorization():
    """余下 34 个不写 platform_toolsets 的 profile 走纯默认路径。"""
    resolved = _resolve_for("webui", {})
    assert "request-authorization" in resolved


def test_feishu_channel_does_not_get_inline_authorization():
    """内嵌授权按设计只在 WebUI 可用（需要 owner 已登录的浏览器 + broker 核验）。"""
    resolved = _resolve_for("feishu", dict(_MERGE_DEFAULT_CONFIG))
    assert "request-authorization" not in resolved


def test_webui_explicit_mode_is_left_alone():
    """显式模式是租户的硬清单，不许被塞东西（生产 webui 侧 0 个在用，语义仍须守住）。"""
    config = {
        "platform_toolsets": {"webui": ["lark-cli"]},
        "multitenancy": {"platform_toolsets_mode": {"webui": "explicit"}},
    }
    assert _resolve_for("webui", config) == ["lark-cli"]


def test_absent_resolver_without_explicit_list_still_means_core_defaults():
    """护栏：解析器缺席且 profile 没列表时必须仍返回 None（= core 全量默认），
    否则这里一追加就把租户砍成只剩一个工具。"""
    from hermes_multitenancy.agent_real import subprocess_env

    assert (
        subprocess_env._resolve_enabled_toolsets(
            {}, "webui", platform_tools_resolver=None
        )
        is None
    )
