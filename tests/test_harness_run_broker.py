from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hermes_multitenancy import webui_broker_server as broker
from hermes_multitenancy.agent_real.harness_workflow import HarnessWorkflowStore
from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal


@pytest.fixture(autouse=True)
def _pilot_allowlist(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_WEBUI_HARNESS_PROFILES", "alice")
    revision = "a" * 40
    ready = tmp_path / "harness.ready"
    ready.write_text(f"{revision}\n")
    monkeypatch.setenv("HERMES_WEBUI_HARNESS_SOURCE_REV", revision)
    monkeypatch.setenv("HERMES_WEBUI_HARNESS_READY_FILE", str(ready))


def _principal():
    return issue_webui_principal(
        profile_name="alice",
        actor_subject="ou_alice",
        credential_subject="ou_alice",
    )


def _evidence(gate: str):
    kinds = {
        "A": ["ub"], "B": ["p4"], "C": ["cr", "red"],
        "D": ["test"], "E": ["pre"], "F": ["defect"],
    }[gate]
    return [{"kind": kind, "id": f"{kind}-1", "summary": ""} for kind in kinds]


def _request(token: str, workflow_id: str, payload: dict, *, events: list | None = None,
             credential_available=None):
    async def run():
        app = broker.create_run_broker_app(
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
            harness_credential_available=credential_available,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            if events is not None:
                async def capture(event):
                    events.append(event)
                broker.register_harness_workflow_emitter(workflow_id, capture)
            response = await client.post(
                f"/api/run-broker/harness/workflows/{workflow_id}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Hermes-Owner-Open-Id": "ou_alice",
                },
            )
            return response.status, await response.json()
        finally:
            if events is not None:
                broker.unregister_harness_workflow_emitter(workflow_id)
            await client.close()

    return asyncio.run(run())


def test_run_scoped_harness_route_uses_server_workflow_binding(monkeypatch, tmp_path: Path):
    profile_home = tmp_path / "alice"
    store = HarnessWorkflowStore(profile_home / "harness-runtime.db")
    store.start(_principal(), "wf-1", "thread-1", "server-dev")
    store.close()

    monkeypatch.setenv("HERMES_WEBUI_HARNESS_ENABLED", "true")
    monkeypatch.setattr(broker, "_run_broker_key", lambda: "master")
    monkeypatch.setattr(broker, "_profile_home_for_name", lambda _name: profile_home)
    broker.register_run_broker_scoped_token(
        token="scoped",
        profile_name="alice",
        open_id="ou_alice",
        run_id="run-1",
        workflow_id="wf-1",
    )
    try:
        status, body = _request("scoped", "wf-1", {"action": "snapshot"})
        assert status == 200, body
        assert body["workflow_id"] == "wf-1"

        events = []
        status, body = _request("scoped", "wf-1", {
            "action": "set_stage",
            "stage": "pre_deploy",
            "status": "waiting",
            "summary": "preview ready",
            "related_ids": {"mr": "123"},
        }, events=events)
        assert status == 200, body
        assert body["event"] == "workflow_stage"
        assert body["related_ids"] == {"mr": "123"}
        assert [(event.kind, event.payload["stage"]) for event in events] == [
            ("workflow_stage", "pre_deploy")
        ]

        status, body = _request(
            "scoped", "wf-1",
            {"action": "request_gate", "gate": "A", "checklist": _evidence("A")},
        )
        assert status == 200, body
        assert body["checklist"] == _evidence("A")

        events = []
        status, _body = _request(
            "scoped", "wf-1",
            {"action": "pause_credential", "credential_kind": "mobius"},
            events=events,
        )
        assert status == 200
        assert [event.kind for event in events] == ["auth_required"]
        assert _request(
            "scoped", "wf-1",
            {"action": "resume_credential", "credential_kind": "mobius"},
        )[0] == 403

        status, _body = _request("scoped", "wf-2", {"action": "snapshot"})
        assert status == 403
        status, _body = _request(
            "scoped",
            "wf-1",
            {"action": "snapshot", "profile_name": "bob"},
        )
        assert status == 403
    finally:
        broker.unregister_run_broker_scoped_token("scoped")


def test_run_broker_accepts_trusted_profile_outside_legacy_allowlist(monkeypatch, tmp_path: Path):
    profile_home = tmp_path / "alice"
    store = HarnessWorkflowStore(profile_home / "harness-runtime.db")
    store.start(_principal(), "wf-1", "thread-1", "server-dev")
    store.close()
    monkeypatch.setenv("HERMES_WEBUI_HARNESS_ENABLED", "1")
    monkeypatch.setenv("HERMES_WEBUI_HARNESS_PROFILES", "bob")
    monkeypatch.setattr(broker, "_run_broker_key", lambda: "master")
    monkeypatch.setattr(broker, "_profile_home_for_name", lambda _name: profile_home)
    broker.register_run_broker_scoped_token(
        token="scoped",
        profile_name="alice",
        open_id="ou_alice",
        run_id="run-1",
        workflow_id="wf-1",
    )
    try:
        assert _request("scoped", "wf-1", {"action": "snapshot"})[0] == 200
    finally:
        broker.unregister_run_broker_scoped_token("scoped")


def test_run_broker_rejects_stale_readiness(monkeypatch, tmp_path: Path):
    profile_home = tmp_path / "alice"
    store = HarnessWorkflowStore(profile_home / "harness-runtime.db")
    store.start(_principal(), "wf-1", "thread-1", "server-dev")
    store.close()
    monkeypatch.setenv("HERMES_WEBUI_HARNESS_ENABLED", "1")
    monkeypatch.setenv("HERMES_WEBUI_HARNESS_READY_FILE", str(tmp_path / "missing.ready"))
    monkeypatch.setattr(broker, "_run_broker_key", lambda: "master")
    monkeypatch.setattr(broker, "_profile_home_for_name", lambda _name: profile_home)
    broker.register_run_broker_scoped_token(
        token="scoped",
        profile_name="alice",
        open_id="ou_alice",
        run_id="run-1",
        workflow_id="wf-1",
    )
    try:
        assert _request("scoped", "wf-1", {"action": "snapshot"})[0] == 503
    finally:
        broker.unregister_run_broker_scoped_token("scoped")


def test_master_bff_must_live_verify_connector_before_credential_resume(
    monkeypatch, tmp_path: Path
):
    from hermes_multitenancy.agent_real.harness_webui_runtime import workflow_id_for

    profile_home = tmp_path / "alice"
    workflow_id = workflow_id_for("alice", "ou_alice", "session-1")
    store = HarnessWorkflowStore(profile_home / "harness-runtime.db")
    principal = _principal()
    store.start(principal, workflow_id, "thread-1", "server-dev")
    store.pause_for_credential(principal, workflow_id, "mobius")
    store.close()

    monkeypatch.setenv("HERMES_WEBUI_HARNESS_ENABLED", "1")
    monkeypatch.setattr(broker, "_run_broker_key", lambda: "master")
    monkeypatch.setattr(broker, "_profile_home_for_name", lambda _name: profile_home)
    monkeypatch.setattr(
        broker,
        "_owner_scoped_tenant",
        lambda _request, _payload, require_write=False: ("alice", "ou_alice"),
    )

    payload = {
        "action": "resume_credential",
        "profile_name": "alice",
        "session_id": "session-1",
        "credential_kind": "mobius",
        "connector_id": "kep-cli-online",
    }
    assert _request("master", workflow_id, payload)[0] == 409
    payload["credential_verified"] = True
    assert _request("master", workflow_id, payload)[0] == 200


def test_master_bff_resolves_snapshot_workflow_from_trusted_session(
    monkeypatch, tmp_path: Path
):
    from hermes_multitenancy.agent_real.harness_webui_runtime import workflow_id_for

    profile_home = tmp_path / "alice"
    workflow_id = workflow_id_for("alice", "ou_alice", "session-1")
    store = HarnessWorkflowStore(profile_home / "harness-runtime.db")
    store.start(_principal(), workflow_id, "thread-1", "server-dev")
    store.close()
    monkeypatch.setenv("HERMES_WEBUI_HARNESS_ENABLED", "1")
    monkeypatch.setattr(broker, "_run_broker_key", lambda: "master")
    monkeypatch.setattr(broker, "_profile_home_for_name", lambda _name: profile_home)
    monkeypatch.setattr(
        broker,
        "_owner_scoped_tenant",
        lambda _request, _payload, require_write=False: ("alice", "ou_alice"),
    )

    status, body = _request(
        "master",
        "by-session",
        {"action": "snapshot", "profile_name": "alice", "session_id": "session-1"},
    )
    assert status == 200
    assert body["workflow_id"] == workflow_id


def test_run_broker_operation_executes_once_only_after_gate(monkeypatch, tmp_path: Path):
    profile_home = tmp_path / "alice"
    repo = profile_home / "workspace" / "runs" / "wf-1" / "repo"
    repo.mkdir(parents=True)
    store = HarnessWorkflowStore(profile_home / "harness-runtime.db")
    principal = _principal()
    store.start(principal, "wf-1", "thread-1", "server-dev")
    store.close()

    monkeypatch.setenv("HERMES_WEBUI_HARNESS_ENABLED", "1")
    monkeypatch.setattr(broker, "_run_broker_key", lambda: "master")
    monkeypatch.setattr(broker, "_profile_home_for_name", lambda _name: profile_home)
    broker.register_run_broker_scoped_token(
        token="scoped",
        profile_name="alice",
        open_id="ou_alice",
        run_id="run-1",
        workflow_id="wf-1",
    )
    try:
        payload = {
            "action": "execute",
            "operation": "git_push",
            "arguments": {"branch": "feat/demo"},
            "idempotency_key": "push-1",
        }
        status, _body = _request("scoped", "wf-1", payload)
        assert status == 409
        audit = repo / ".hermes-harness-operations.jsonl"
        assert not audit.exists()

        store = HarnessWorkflowStore(profile_home / "harness-runtime.db")
        for gate in ("A", "B", "C", "D"):
            approval_id = store.request_gate(principal, "wf-1", gate, _evidence(gate))
            store.resolve_gate(principal, approval_id, "approve", "ok")
        store.close()

        assert _request("scoped", "wf-1", payload)[0] == 200
        assert _request("scoped", "wf-1", payload)[0] == 200
        assert len(audit.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        broker.unregister_run_broker_scoped_token("scoped")
