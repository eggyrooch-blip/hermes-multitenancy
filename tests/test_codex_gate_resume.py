"""Gate A->B->C->D WAITING_GATE, one-time capability, same-thread resume
(SPEC ticket 03).

No real write credential and no GitLab call anywhere in this file -- the
"write action" a consumed capability unlocks is nothing but an in-module
counter (`GateResumeResult.write_action_count`); this ticket only proves the
state machine + capability boundary, not a real write executor.
"""
from __future__ import annotations

import threading

import pytest

from hermes_multitenancy.agent_real.codex_gate_resume import (
    GATES,
    GateResumeRejected,
    GateResumeStore,
    consume_gate_capability,
    current_gate_state,
    describe_gate_state,
    enter_waiting_gate,
    issue_gate_capability,
)
from hermes_multitenancy.agent_real import executor_unavailable_ux as ux
from hermes_multitenancy.agent_real.codex_session_bridge import (
    CodexSessionBridgeStore,
    plan_codex_thread,
    record_codex_thread,
)
from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal


RUN_ID = "run-w1-gate-1"
ACTOR = "ou_alice"
THREAD_ID = "thread_abc123"
NOW_MS = 1_800_000_000_000


def _store(tmp_path) -> GateResumeStore:
    return GateResumeStore(tmp_path / "codex-gate-resume.db")


def _enter_a(store, *, run_id=RUN_ID, actor=ACTOR, thread_id=THREAD_ID, now_ms=NOW_MS):
    return enter_waiting_gate(
        store=store, run_id=run_id, actor_subject=actor, thread_id=thread_id,
        gate="A", now_ms=now_ms,
    )


def _issue(store, *, gate, run_id=RUN_ID, actor=ACTOR, thread_id=THREAD_ID, now_ms=NOW_MS, ttl_ms=None):
    kwargs = dict(
        store=store, run_id=run_id, actor_subject=actor, thread_id=thread_id,
        gate=gate, now_ms=now_ms,
    )
    if ttl_ms is not None:
        kwargs["ttl_ms"] = ttl_ms
    return issue_gate_capability(**kwargs)


def _consume(store, token, *, gate, run_id=RUN_ID, actor=ACTOR, thread_id=THREAD_ID, now_ms=NOW_MS):
    return consume_gate_capability(
        store=store, token=token, run_id=run_id, actor_subject=actor,
        thread_id=thread_id, gate=gate, now_ms=now_ms,
    )


# --------------------------------------------------------------------------- #
# 1. WAITING_GATE entry: write-action counter stays 0, projection halts
# --------------------------------------------------------------------------- #
def test_fresh_write_intent_enters_gate_a_with_zero_write_actions(tmp_path) -> None:
    store = _store(tmp_path)
    state = _enter_a(store)
    assert state.gate == "A"
    assert state.status == "waiting"
    assert state.write_action_count == 0
    projection = describe_gate_state(state)
    assert projection["gate"] == "A"
    assert projection["status_zh"] == "等待确认"


def test_current_gate_state_is_a_readonly_projection(tmp_path) -> None:
    store = _store(tmp_path)
    assert current_gate_state(store, RUN_ID) is None
    _enter_a(store)
    state = current_gate_state(store, RUN_ID)
    assert state.gate == "A"
    assert state.write_action_count == 0
    # reading again does not mutate anything
    assert current_gate_state(store, RUN_ID) == state


def test_new_run_must_start_at_gate_a(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(GateResumeRejected):
        enter_waiting_gate(
            store=store, run_id=RUN_ID, actor_subject=ACTOR, thread_id=THREAD_ID,
            gate="B", now_ms=NOW_MS,
        )


def test_reentering_the_same_still_waiting_gate_is_a_harmless_noop(tmp_path) -> None:
    store = _store(tmp_path)
    _enter_a(store)
    state = _enter_a(store, now_ms=NOW_MS + 5)
    assert state.gate == "A"
    assert state.write_action_count == 0


# --------------------------------------------------------------------------- #
# 2. issue + consume: correct capability advances exactly one gate, once
# --------------------------------------------------------------------------- #
def test_consuming_gate_a_capability_advances_to_b_and_resumes_same_thread(tmp_path) -> None:
    store = _store(tmp_path)
    _enter_a(store)
    token = _issue(store, gate="A")
    result = _consume(store, token, gate="A")
    assert result.resume_thread_id == THREAD_ID
    assert result.resumed_gate == "A"
    assert result.next_gate == "B"
    assert result.status == "waiting"
    assert result.write_action_count == 1


def test_full_sequence_a_through_f_reaches_done(tmp_path) -> None:
    store = _store(tmp_path)
    _enter_a(store)
    for gate in GATES:
        token = _issue(store, gate=gate)
        result = _consume(store, token, gate=gate)
    assert result.next_gate is None
    assert result.status == "done"
    assert result.write_action_count == len(GATES)
    # a fifth capability for a done run can never be minted again
    with pytest.raises(GateResumeRejected):
        _issue(store, gate="D")


def test_duplicate_click_on_same_token_is_a_noop_not_a_second_execution(tmp_path) -> None:
    store = _store(tmp_path)
    _enter_a(store)
    token = _issue(store, gate="A")
    first = _consume(store, token, gate="A")
    assert first.write_action_count == 1
    with pytest.raises(GateResumeRejected):
        _consume(store, token, gate="A")
    # state must still show exactly one write action -- no second execution
    state = describe_gate_state(current_gate_state(store, RUN_ID))
    assert state["write_action_count"] == 1


# --------------------------------------------------------------------------- #
# 3. capability bound to (actor, run, gate, thread) -- every mismatch rejected
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "wrong_kwargs",
    [
        {"actor": "ou_mallory"},
        {"run_id": "run-different"},
        {"thread_id": "thread_different"},
    ],
    ids=["wrong_actor", "wrong_run", "wrong_thread"],
)
def test_consume_rejects_wrong_bound_field(tmp_path, wrong_kwargs) -> None:
    store = _store(tmp_path)
    _enter_a(store)
    token = _issue(store, gate="A")
    with pytest.raises(GateResumeRejected):
        _consume(store, token, gate="A", **wrong_kwargs)
    # the legitimate holder can still consume it afterwards -- a wrong
    # attempt must not burn the one-time capability for the real caller
    result = _consume(store, token, gate="A")
    assert result.write_action_count == 1


def test_consume_rejects_wrong_gate_claim(tmp_path) -> None:
    store = _store(tmp_path)
    _enter_a(store)
    token = _issue(store, gate="A")
    with pytest.raises(GateResumeRejected):
        _consume(store, token, gate="B")


def test_consume_rejects_expired_capability(tmp_path) -> None:
    store = _store(tmp_path)
    _enter_a(store)
    token = _issue(store, gate="A", ttl_ms=1_000)
    with pytest.raises(GateResumeRejected):
        _consume(store, token, gate="A", now_ms=NOW_MS + 1_000)


def test_consume_rejects_unknown_token(tmp_path) -> None:
    store = _store(tmp_path)
    _enter_a(store)
    with pytest.raises(GateResumeRejected):
        _consume(store, "token-never-issued", gate="A")


def test_consume_rejects_out_of_order_when_run_has_moved_past_this_gate(tmp_path) -> None:
    store = _store(tmp_path)
    _enter_a(store)
    stale_token = _issue(store, gate="A")
    fresh_token = _issue(store, gate="A")
    # advance the run past gate A using a different, still-valid capability
    advanced = _consume(store, fresh_token, gate="A")
    assert advanced.next_gate == "B"
    # the older unconsumed capability for A is now out of order
    with pytest.raises(GateResumeRejected):
        _consume(store, stale_token, gate="A")


# --------------------------------------------------------------------------- #
# 4. concurrent double consume: exactly one winner, atomic
# --------------------------------------------------------------------------- #
def test_concurrent_double_consume_only_one_thread_wins(tmp_path) -> None:
    store = _store(tmp_path)
    _enter_a(store)
    token = _issue(store, gate="A")
    results: list[object] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            result = _consume(store, token, gate="A")
            with lock:
                results.append(result)
        except GateResumeRejected as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 1
    assert len(errors) == 7
    assert results[0].write_action_count == 1


# --------------------------------------------------------------------------- #
# 5. reuses the existing unavailable-UX boundary (t04) -- no second UX layer
# --------------------------------------------------------------------------- #
def test_gate_resume_rejected_is_recognized_by_existing_unavailable_ux(tmp_path) -> None:
    store = _store(tmp_path)
    _enter_a(store)
    try:
        _consume(store, "token-never-issued", gate="A")
    except GateResumeRejected as exc:
        caught = exc
    else:  # pragma: no cover
        raise AssertionError("expected GateResumeRejected")
    assert ux.is_unavailable(caught)
    assert ux.classify(caught) == ux.CODEX_GATE_DENIED
    message = ux.employee_message(ux.classify(caught))
    assert message == "本次操作未获批准，已停止执行。"


# --------------------------------------------------------------------------- #
# 6. composes with ticket 01's thread bridge -- resumes the SAME bound thread
# --------------------------------------------------------------------------- #
def test_gate_resume_carries_the_thread_bound_by_session_bridge(tmp_path) -> None:
    bridge_store = CodexSessionBridgeStore(tmp_path / "session-bridge.db")
    principal = issue_webui_principal(
        profile_name="sunke", actor_subject=ACTOR, credential_subject=ACTOR
    )
    plan = plan_codex_thread(
        store=bridge_store, principal=principal, profile_name="sunke",
        executor="codex_app_server", workflow_id="wf-gate-1", now_ms=NOW_MS,
    )
    assert plan.resume_thread_id is None
    bound_thread_id = "thread_bound_by_t01"
    record_codex_thread(plan, store=bridge_store, thread_id=bound_thread_id, now_ms=NOW_MS)

    gate_store = _store(tmp_path)
    enter_waiting_gate(
        store=gate_store, run_id="wf-gate-1", actor_subject=ACTOR,
        thread_id=bound_thread_id, gate="A", now_ms=NOW_MS,
    )
    token = issue_gate_capability(
        store=gate_store, run_id="wf-gate-1", actor_subject=ACTOR,
        thread_id=bound_thread_id, gate="A", now_ms=NOW_MS,
    )
    result = consume_gate_capability(
        store=gate_store, token=token, run_id="wf-gate-1", actor_subject=ACTOR,
        thread_id=bound_thread_id, gate="A", now_ms=NOW_MS,
    )
    assert result.resume_thread_id == bound_thread_id


# --------------------------------------------------------------------------- #
# 7. malformed inputs fail closed, never crash into a stray exception type
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_gate", ["Z", "", "a", None])
def test_malformed_gate_rejected(tmp_path, bad_gate) -> None:
    store = _store(tmp_path)
    with pytest.raises(GateResumeRejected):
        enter_waiting_gate(
            store=store, run_id=RUN_ID, actor_subject=ACTOR, thread_id=THREAD_ID,
            gate=bad_gate, now_ms=NOW_MS,
        )


def test_malformed_run_id_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(GateResumeRejected):
        enter_waiting_gate(
            store=store, run_id="bad run id with spaces", actor_subject=ACTOR,
            thread_id=THREAD_ID, gate="A", now_ms=NOW_MS,
        )
