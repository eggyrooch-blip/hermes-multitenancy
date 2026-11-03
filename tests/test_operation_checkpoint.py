from __future__ import annotations

import sqlite3

import pytest

from hermes_multitenancy.operation_checkpoint import OperationCheckpointStore


def test_checkpoint_survives_restart_and_is_actor_bound(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    first = OperationCheckpointStore(db_path)
    first.put(
        operation_id="op-1",
        profile_name="alice",
        subject="ou_alice",
        connector="lark-cli",
        intent_key="export-weekly-sales",
        step="write-sheet",
        state="waiting_auth",
        session_ref="session-1",
        call_ref="call-1",
    )
    first.close()

    restarted = OperationCheckpointStore(db_path)
    row = restarted.get("op-1", profile_name="alice", subject="ou_alice")
    assert row == {
        "operation_id": "op-1",
        "connector": "lark-cli",
        "step": "write-sheet",
        "state": "waiting_auth",
        "session_ref": "session-1",
        "call_ref": "call-1",
        "result_ref": None,
    }
    assert restarted.get("op-1", profile_name="bob", subject="ou_bob") is None

    raw = db_path.read_bytes()
    assert b"ou_alice" not in raw
    assert b"export-weekly-sales" not in raw


def test_checkpoint_finds_only_same_actor_pending_connector_step(tmp_path) -> None:
    store = OperationCheckpointStore(tmp_path / "state.db")
    store.put(
        operation_id="op-alice",
        profile_name="alice",
        subject="ou_alice",
        connector="lark-cli",
        intent_key="call:session-1:call-1",
        step="execute",
        state="waiting_auth",
        session_ref="session-1",
        call_ref="call-1",
    )
    store.put(
        operation_id="op-bob",
        profile_name="bob",
        subject="ou_bob",
        connector="lark-cli",
        intent_key="call:session-2:call-2",
        step="execute",
        state="waiting_auth",
        session_ref="session-2",
        call_ref="call-2",
    )

    assert store.find_pending(
        profile_name="alice",
        subject="ou_alice",
        connector="lark-cli",
    ) == {
        "operation_id": "op-alice",
        "connector": "lark-cli",
        "step": "execute",
        "state": "waiting_auth",
        "session_ref": "session-1",
        "call_ref": "call-1",
        "result_ref": None,
    }
    assert store.find_pending(
        profile_name="alice",
        subject="ou_bob",
        connector="lark-cli",
    ) is None

    store.put(
        operation_id="op-alice-newer",
        profile_name="alice",
        subject="ou_alice",
        connector="lark-cli",
        intent_key="call:session-newer:call-newer",
        step="execute",
        state="waiting_auth",
        session_ref="session-newer",
        call_ref="call-newer",
    )
    assert store.find_pending(
        profile_name="alice",
        subject="ou_alice",
        connector="lark-cli",
        session_ref="session-1",
        call_ref="call-1",
    )["operation_id"] == "op-alice"


def test_no_ref_recovery_requires_one_fresh_waiting_auth_row(tmp_path) -> None:
    store = OperationCheckpointStore(tmp_path / "state.db")
    for operation_id, state in (
        ("op-running", "running"),
        ("op-uncertain", "uncertain"),
    ):
        store.put(
            operation_id=operation_id,
            profile_name="alice",
            subject="ou_alice",
            connector="lark-cli",
            intent_key=f"call:{operation_id}:call",
            step="execute",
            state=state,
            session_ref=operation_id,
            call_ref="call",
        )
    assert store.find_pending(
        profile_name="alice", subject="ou_alice", connector="lark-cli"
    ) is None

    store.put(
        operation_id="op-auth-1",
        profile_name="alice",
        subject="ou_alice",
        connector="lark-cli",
        intent_key="call:session-auth-1:call-auth-1",
        step="execute",
        state="waiting_auth",
        session_ref="session-auth-1",
        call_ref="call-auth-1",
    )
    assert store.find_pending(
        profile_name="alice", subject="ou_alice", connector="lark-cli"
    )["operation_id"] == "op-auth-1"

    store.put(
        operation_id="op-auth-2",
        profile_name="alice",
        subject="ou_alice",
        connector="lark-cli",
        intent_key="call:session-auth-2:call-auth-2",
        step="execute",
        state="waiting_auth",
        session_ref="session-auth-2",
        call_ref="call-auth-2",
    )
    assert store.find_pending(
        profile_name="alice", subject="ou_alice", connector="lark-cli"
    ) is None


def test_partial_ref_recovery_fails_closed_instead_of_selecting_newest_match(tmp_path) -> None:
    store = OperationCheckpointStore(tmp_path / "state.db")
    for call_ref in ("call-1", "call-2"):
        store.put(
            operation_id=f"op-{call_ref}",
            profile_name="alice",
            subject="ou_alice",
            connector="lark-cli",
            intent_key=f"call:session-shared:{call_ref}",
            step="execute",
            state="waiting_auth",
            session_ref="session-shared",
            call_ref=call_ref,
        )

    assert store.find_pending(
        profile_name="alice",
        subject="ou_alice",
        connector="lark-cli",
        session_ref="session-shared",
    ) is None
    assert store.find_pending(
        profile_name="alice",
        subject="ou_alice",
        connector="lark-cli",
        call_ref="call-1",
    ) is None


def test_no_ref_recovery_rejects_stale_waiting_auth_but_exact_refs_remain_available(
    tmp_path,
) -> None:
    store = OperationCheckpointStore(tmp_path / "state.db")
    store.put(
        operation_id="op-stale",
        profile_name="alice",
        subject="ou_alice",
        connector="lark-cli",
        intent_key="call:session-stale:call-stale",
        step="execute",
        state="waiting_auth",
        session_ref="session-stale",
        call_ref="call-stale",
    )
    store._conn.execute(
        "UPDATE multitenancy_operation_checkpoints SET updated_at=0 WHERE operation_id='op-stale'"
    )
    store._conn.commit()

    assert store.find_pending(
        profile_name="alice", subject="ou_alice", connector="lark-cli"
    ) is None
    assert store.find_pending(
        profile_name="alice",
        subject="ou_alice",
        connector="lark-cli",
        session_ref="session-stale",
        call_ref="call-stale",
    )["operation_id"] == "op-stale"


def test_checkpoint_transition_is_compare_and_swap(tmp_path) -> None:
    store = OperationCheckpointStore(tmp_path / "state.db")
    store.put(
        operation_id="op-1",
        profile_name="alice",
        subject="ou_alice",
        connector="lark-cli",
        intent_key="intent-1",
        step="write",
        state="uncertain",
    )

    assert store.transition(
        "op-1",
        profile_name="alice",
        subject="ou_alice",
        expected_state="uncertain",
        state="confirmed",
        step="readback",
    )
    assert not store.transition(
        "op-1",
        profile_name="alice",
        subject="ou_alice",
        expected_state="uncertain",
        state="pending",
        step="write",
    )
    assert store.get("op-1", profile_name="alice", subject="ou_alice")["state"] == "confirmed"


def test_checkpoint_rejects_untrusted_values(tmp_path) -> None:
    store = OperationCheckpointStore(tmp_path / "state.db")
    with pytest.raises(ValueError):
        store.put(
            operation_id="",
            profile_name="alice",
            subject="ou_alice",
            connector="lark-cli",
            intent_key="intent-1",
            step="write",
            state="pending",
        )
    with pytest.raises(ValueError):
        store.put(
            operation_id="op-1",
            profile_name="alice",
            subject="ou_alice",
            connector="lark-cli",
            intent_key="intent-1",
            step="write",
            state="made-up",
        )


def test_checkpoint_schema_has_no_payload_column(tmp_path) -> None:
    store = OperationCheckpointStore(tmp_path / "state.db")
    store.close()
    with sqlite3.connect(tmp_path / "state.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(multitenancy_operation_checkpoints)")}
    assert "payload" not in columns
    assert "content" not in columns
    assert "token" not in columns
    assert {"session_ref", "call_ref", "result_ref"} <= columns
