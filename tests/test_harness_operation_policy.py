from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from hermes_multitenancy.agent_real.harness_workflow import (
    HarnessWorkflowRejected,
    HarnessWorkflowStore,
)
from hermes_multitenancy.agent_real import _core
from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal


def _principal(actor: str = "ou_alice"):
    return issue_webui_principal(
        profile_name="alice", actor_subject=actor, credential_subject=actor
    )


def _evidence(gate: str, *, light: bool = False):
    kinds = {
        "A": ["ub", *( ["p4"] if light else [])],
        "B": ["p4"],
        "C": ["cr", "red"],
        "D": ["test"],
        "E": ["pre"],
        "F": ["defect"],
    }[gate]
    return [{"kind": kind, "id": f"{kind}-1", "summary": ""} for kind in kinds]


def test_gate_requires_structured_rfc_evidence(tmp_path: Path):
    store = HarnessWorkflowStore(tmp_path / "harness.db")
    principal = _principal()
    store.start(principal, "wf-evidence", "thread-evidence", "server-dev")

    with pytest.raises(HarnessWorkflowRejected, match="gate_evidence_invalid"):
        store.request_gate(principal, "wf-evidence", "A", ["reviewed"])

    approval_id = store.request_gate(
        principal,
        "wf-evidence",
        "A",
        [{"kind": "ub", "id": "UB-42", "summary": "business target accepted"}],
    )
    assert approval_id.startswith("gate_")


def test_gate_d_and_e_hard_block_operations_until_approved(tmp_path: Path):
    store = HarnessWorkflowStore(tmp_path / "harness.db")
    principal = _principal()
    store.start(principal, "wf-1", "thread-1", "server-dev")
    calls = []

    def adapter(operation, arguments):
        calls.append((operation, arguments))
        return {"ok": True}

    with pytest.raises(HarnessWorkflowRejected, match="gate_not_approved"):
        store.execute(principal, "wf-1", "git_push", {}, "push-1", adapter)
    assert calls == []

    for gate in ("A", "B", "C", "D"):
        approval_id = store.request_gate(principal, "wf-1", gate, _evidence(gate))
        store.resolve_gate(principal, approval_id, "approve", "ok")
    assert store.execute(
        principal, "wf-1", "git_push", {"branch": "feature/demo"}, "push-1", adapter
    ) == {"ok": True}
    assert store.execute(
        principal, "wf-1", "git_push", {"branch": "feature/demo"}, "push-1", adapter
    ) == {"ok": True}
    assert len(calls) == 1
    with pytest.raises(HarnessWorkflowRejected, match="idempotency_conflict"):
        store.execute(
            principal, "wf-1", "git_push", {"branch": "feature/other"}, "push-1", adapter
        )

    with pytest.raises(HarnessWorkflowRejected, match="gate_not_approved"):
        store.execute(principal, "wf-1", "deploy", {}, "deploy-1", adapter)
    assert len(calls) == 1
    approval_id = store.request_gate(principal, "wf-1", "E", _evidence("E"))
    store.resolve_gate(principal, approval_id, "reject", "do not deploy")
    with pytest.raises(HarnessWorkflowRejected, match="workflow_not_running"):
        store.execute(principal, "wf-1", "git_push", {}, "push-2", adapter)
    approval_id = store.request_gate(principal, "wf-1", "E", _evidence("E"))
    store.resolve_gate(principal, approval_id, "approve", "ship pre")
    store.execute(principal, "wf-1", "deploy", {}, "deploy-1", adapter)
    assert len(calls) == 2


def test_operation_retry_never_repeats_an_unknown_side_effect(tmp_path: Path):
    store = HarnessWorkflowStore(tmp_path / "harness.db")
    principal = _principal()
    store.start(principal, "wf-once", "thread-once", "server-dev")
    for gate in ("A", "B", "C", "D"):
        approval_id = store.request_gate(
            principal, "wf-once", gate, _evidence(gate)
        )
        store.resolve_gate(principal, approval_id, "approve", "ok")
    calls = 0

    def adapter(_operation, _arguments):
        nonlocal calls
        calls += 1
        raise RuntimeError("process died after side effect")

    with pytest.raises(RuntimeError, match="process died"):
        store.execute(principal, "wf-once", "git_push", {}, "push-once", adapter)
    with pytest.raises(HarnessWorkflowRejected, match="operation_outcome_unknown"):
        store.execute(principal, "wf-once", "git_push", {}, "push-once", adapter)
    assert calls == 1


def test_bugfix_gate_order_and_actor_binding_survive_reopen(tmp_path: Path):
    db = tmp_path / "harness.db"
    principal = _principal()
    store = HarnessWorkflowStore(db)
    store.start(principal, "bug-1", "thread-bug", "server-bugfix")
    with pytest.raises(HarnessWorkflowRejected, match="gate_out_of_order"):
        store.request_gate(principal, "bug-1", "C", _evidence("C"))
    approval_id = store.request_gate(principal, "bug-1", "F", _evidence("F"))
    store.close()

    reopened = HarnessWorkflowStore(db)
    pending = reopened.pending(principal, "bug-1")
    assert pending["approval_id"] == approval_id
    snapshot = reopened.snapshot(principal, "bug-1")
    assert snapshot["pending_gate"] == {
        "approval_id": approval_id,
        "gate": "F",
        "checklist": _evidence("F"),
    }
    with pytest.raises(HarnessWorkflowRejected, match="principal_mismatch"):
        reopened.resolve_gate(_principal("ou_mallory"), approval_id, "approve", "")
    reopened.resolve_gate(principal, approval_id, "rework", "fix classification")
    assert reopened.snapshot(principal, "bug-1")["status"] == "rework"


def test_light_flow_combines_gate_a_and_b(tmp_path: Path):
    store = HarnessWorkflowStore(tmp_path / "harness.db")
    principal = _principal()
    store.start(principal, "light-1", "thread-light", "server-dev-light")
    approval_id = store.request_gate(principal, "light-1", "A", _evidence("A", light=True))
    store.resolve_gate(principal, approval_id, "approve", "")
    snapshot = store.snapshot(principal, "light-1")
    assert snapshot["approved_gates"] == ["A", "B"]


def test_credential_pause_is_owner_scoped_and_persistent(tmp_path: Path):
    db = tmp_path / "harness.db"
    principal = _principal()
    store = HarnessWorkflowStore(db)
    store.start(principal, "wf-auth", "thread-auth", "server-dev")
    store.pause_for_credential(principal, "wf-auth", "mobius")
    store.close()

    reopened = HarnessWorkflowStore(db)
    assert reopened.snapshot(principal, "wf-auth")["status"] == "waiting_credential"
    with pytest.raises(HarnessWorkflowRejected, match="principal_mismatch"):
        reopened.resume_credential(_principal("ou_mallory"), "wf-auth", "mobius")
    with pytest.raises(HarnessWorkflowRejected, match="credential_unavailable"):
        reopened.resume_credential(principal, "wf-auth", "mobius")
    reopened.resume_credential(
        principal, "wf-auth", "mobius", validator=lambda _principal, _kind: True
    )
    assert reopened.snapshot(principal, "wf-auth")["status"] == "running"


def test_workflow_stage_and_related_ids_survive_reopen(tmp_path: Path):
    db = tmp_path / "harness.db"
    principal = _principal()
    store = HarnessWorkflowStore(db)
    store.start(principal, "wf-stage", "thread-stage", "server-dev")
    event = store.set_stage(
        principal,
        "wf-stage",
        "pre_deploy",
        "waiting",
        "preview smoke is ready",
        {"mr": "123", "preview": "pre-42"},
    )
    store.close()

    assert event["event"] == "workflow_stage"
    reopened = HarnessWorkflowStore(db)
    snapshot = reopened.snapshot(principal, "wf-stage")
    assert snapshot["stage"] == "pre_deploy"
    assert snapshot["stage_status"] == "waiting"
    assert snapshot["summary"] == "preview smoke is ready"
    assert snapshot["related_ids"] == {"mr": "123", "preview": "pre-42"}
    assert snapshot["audit_id"] == event["audit_id"]


def test_placeholder_thread_binds_once(tmp_path: Path):
    store = HarnessWorkflowStore(tmp_path / "harness.db")
    principal = _principal()
    store.start(principal, "wf-bind", "pending:wf-bind", "server-dev")
    store.start(principal, "wf-bind", "thread-real", "server-dev")
    assert store.snapshot(principal, "wf-bind")["workflow_id"] == "wf-bind"
    with pytest.raises(HarnessWorkflowRejected, match="principal_mismatch"):
        store.start(principal, "wf-bind", "thread-other", "server-dev")


def test_codex_exec_callback_allows_only_reviewed_workspace_status(monkeypatch):
    from tools.terminal_tool import _get_approval_callback

    calls = []
    events = []

    def broker_action(action, **payload):
        calls.append((action, payload))
        return {
            "ok": True,
            "flow": "server-dev",
            "status": "running",
            "approved_gates": ["A", "B", "C", "D", "E"],
        }

    monkeypatch.setenv("HERMES_LOCAL_HARNESS", "1")
    monkeypatch.setenv("HERMES_HARNESS_WORKFLOW_ID", "wf-1")
    monkeypatch.setattr(_core, "_harness_run_broker_action", broker_action)
    monkeypatch.setattr(_core, "_read_approval_choice", lambda _path: "once")
    cleanup = _core._configure_gateway_approval_bridge(
        lambda name, **payload: events.append((name, payload)), "session-1"
    )
    try:
        callback = _get_approval_callback()
        assert callback("git status", "read") == "once"
        assert callback("pytest -q tests/test_one.py", "test") == "once"
        assert callback("apply_patch: README.md", "write") == "once"
        assert callback("git push origin HEAD", "push") == "deny"
        assert callback("rg token /Users/hermes/.hermes", "read") == "deny"
        assert callback("rg token ../profile", "read") == "deny"
        assert callback("g=git; $g push origin HEAD", "push") == "deny"
        assert callback("c=commit; git $c -m bypass", "commit") == "deny"
        assert callback("a=routekey-add; banana.sh $a", "routekey") == "deny"
    finally:
        cleanup()

    assert calls == []
    assert [name for name, _payload in events].count("gate_required") == 0
    assert [name for name, _payload in events].count("approval_required") == 3


def test_local_harness_callback_install_failure_aborts(monkeypatch):
    import tools.terminal_tool as terminal_tool

    monkeypatch.setenv("HERMES_LOCAL_HARNESS", "1")
    monkeypatch.setattr(terminal_tool, "set_approval_callback", lambda _callback: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="approval callback unavailable"):
        _core._configure_gateway_approval_bridge(lambda *_args, **_kwargs: None, "session-1")


def test_idempotency_is_shared_across_store_connections(tmp_path: Path):
    db = tmp_path / "harness.db"
    principal = _principal()
    setup = HarnessWorkflowStore(db)
    setup.start(principal, "wf-race", "thread-race", "server-dev")
    for gate in ("A", "B", "C", "D"):
        approval_id = setup.request_gate(principal, "wf-race", gate, _evidence(gate))
        setup.resolve_gate(principal, approval_id, "approve", "ok")
    setup.close()

    calls = 0
    lock = threading.Lock()

    def adapter(_operation, _arguments):
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.05)
        return {"ok": True}

    def execute():
        store = HarnessWorkflowStore(db)
        try:
            return store.execute(
                principal, "wf-race", "git_push", {}, "same-key", adapter
            )
        finally:
            store.close()

    def concurrent_execute():
        try:
            return execute()
        except HarnessWorkflowRejected as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: concurrent_execute(), range(2)))
    outcomes = {str(result) for result in results}
    assert "{'ok': True}" in outcomes
    assert outcomes <= {"{'ok': True}", "operation_outcome_unknown"}
    assert calls == 1
