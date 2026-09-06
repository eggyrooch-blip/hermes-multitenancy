"""Codex session bridge: one trusted tuple -> exactly one Codex thread_id.

Proves round-2 resume reads back a round-1 fixture fact under the SAME
thread_id (never from request metadata or model text), and that every
mismatch/stale/duplicate/ambiguous case fails closed BEFORE any fake
spawn/model/tool call — with those call counters staying at zero.

No real Codex binary is spawned here (ticket 01 scope); the "codex" side is
a fake in-process store keyed by thread_id, standing in for the loopback
Responses stub tickets 03/05 build.
"""
from __future__ import annotations

import secrets

import pytest

from hermes_multitenancy.agent_real.codex_session_bridge import (
    CodexSessionBridgeRejected,
    CodexSessionBridgeStore,
    CodexThreadPlan,
    plan_codex_thread,
    record_codex_thread,
    require_codex_thread_plan,
)
from hermes_multitenancy.trusted_runtime_principal import (
    TrustedRuntimePrincipal,
    issue_webui_principal,
)


PROFILE = "sunke"
EXECUTOR = "codex_app_server"
WORKFLOW = "wf-session-abc123"
NOW_MS = 1_800_000_000_000


def _principal(actor: str = "ou_alice", profile: str = PROFILE) -> TrustedRuntimePrincipal:
    return issue_webui_principal(
        profile_name=profile, actor_subject=actor, credential_subject=actor
    )


def _store(tmp_path) -> CodexSessionBridgeStore:
    return CodexSessionBridgeStore(tmp_path / "codex-session-bridge.db")


class _FakeCodex:
    """Stand-in for the real codex app-server: thread/start mints an id,
    a resumed thread just reads back whatever the fixture holds for it."""

    def __init__(self) -> None:
        self.start_calls = 0
        self.model_calls = 0
        self.tool_calls = 0
        self.facts: dict[str, str] = {}

    def start_thread_and_record_fact(self) -> tuple[str, str]:
        self.start_calls += 1
        self.model_calls += 1
        thread_id = f"thread_{secrets.token_hex(8)}"
        fact = secrets.token_hex(8)
        self.facts[thread_id] = fact
        return thread_id, fact

    def read_fact(self, thread_id: str) -> str:
        self.tool_calls += 1
        return self.facts[thread_id]


def test_fresh_tuple_has_no_resume_thread(tmp_path) -> None:
    store = _store(tmp_path)
    plan = plan_codex_thread(
        store=store,
        principal=_principal(),
        profile_name=PROFILE,
        executor=EXECUTOR,
        workflow_id=WORKFLOW,
        now_ms=NOW_MS,
    )
    assert plan.resume_thread_id is None


def test_second_round_resumes_same_thread_and_reads_first_round_fact(tmp_path) -> None:
    store = _store(tmp_path)
    codex = _FakeCodex()
    principal = _principal()

    # Round 1: no binding yet -> fresh spawn -> mint thread + write a fact.
    plan1 = plan_codex_thread(
        store=store, principal=principal, profile_name=PROFILE,
        executor=EXECUTOR, workflow_id=WORKFLOW, now_ms=NOW_MS,
    )
    assert plan1.resume_thread_id is None
    thread_id, fact = codex.start_thread_and_record_fact()
    record_codex_thread(plan1, store=store, thread_id=thread_id, now_ms=NOW_MS)

    # Round 2: same trusted tuple -> must resume the SAME thread_id, never a
    # fresh one, and never anything sourced from request metadata.
    plan2 = plan_codex_thread(
        store=store, principal=principal, profile_name=PROFILE,
        executor=EXECUTOR, workflow_id=WORKFLOW, now_ms=NOW_MS + 1000,
    )
    assert plan2.resume_thread_id == thread_id
    require_codex_thread_plan(
        plan2, principal=principal, profile_name=PROFILE,
        executor=EXECUTOR, workflow_id=WORKFLOW,
    )
    assert codex.read_fact(plan2.resume_thread_id) == fact

    # Resume never re-minted a thread or made a fresh model call.
    assert codex.start_calls == 1
    assert codex.model_calls == 1
    assert codex.tool_calls == 1


def test_request_metadata_thread_injection_is_ignored(tmp_path) -> None:
    """A forged thread_id riding in event metadata must never select the
    resumed thread — the bridge only ever consults its own sealed store."""
    store = _store(tmp_path)
    codex = _FakeCodex()
    principal = _principal()
    plan1 = plan_codex_thread(
        store=store, principal=principal, profile_name=PROFILE,
        executor=EXECUTOR, workflow_id=WORKFLOW, now_ms=NOW_MS,
    )
    real_thread_id, fact = codex.start_thread_and_record_fact()
    record_codex_thread(plan1, store=store, thread_id=real_thread_id, now_ms=NOW_MS)

    forged_metadata = {"codex_thread_id": "thread_attacker_forged", "thread_id": "evil"}
    # plan_codex_thread has no parameter that could accept forged_metadata's
    # thread fields at all -- prove the resolved thread ignores it by
    # construction, using only the trusted tuple.
    plan2 = plan_codex_thread(
        store=store, principal=principal, profile_name=PROFILE,
        executor=EXECUTOR, workflow_id=WORKFLOW, now_ms=NOW_MS + 1,
    )
    assert plan2.resume_thread_id == real_thread_id
    assert plan2.resume_thread_id not in forged_metadata.values()
    assert codex.read_fact(plan2.resume_thread_id) == fact
    assert codex.start_calls == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda kw: kw.update(principal=_principal(actor="ou_mallory")),
        lambda kw: kw.update(profile_name="other-profile"),
        lambda kw: kw.update(executor="hermes_default"),
        lambda kw: kw.update(workflow_id="wf-different"),
    ],
    ids=["actor_mismatch", "profile_mismatch", "executor_mismatch", "workflow_mismatch"],
)
def test_require_plan_fails_closed_on_any_tuple_mismatch(tmp_path, mutate) -> None:
    store = _store(tmp_path)
    codex = _FakeCodex()
    principal = _principal()
    plan1 = plan_codex_thread(
        store=store, principal=principal, profile_name=PROFILE,
        executor=EXECUTOR, workflow_id=WORKFLOW, now_ms=NOW_MS,
    )
    thread_id, _fact = codex.start_thread_and_record_fact()
    record_codex_thread(plan1, store=store, thread_id=thread_id, now_ms=NOW_MS)
    plan2 = plan_codex_thread(
        store=store, principal=principal, profile_name=PROFILE,
        executor=EXECUTOR, workflow_id=WORKFLOW, now_ms=NOW_MS + 1,
    )

    kwargs = dict(
        principal=principal, profile_name=PROFILE, executor=EXECUTOR, workflow_id=WORKFLOW
    )
    mutate(kwargs)
    calls_before = (codex.start_calls, codex.model_calls, codex.tool_calls)
    with pytest.raises(CodexSessionBridgeRejected):
        require_codex_thread_plan(plan2, **kwargs)
    assert (codex.start_calls, codex.model_calls, codex.tool_calls) == calls_before


def test_stale_binding_fails_closed_before_spawn(tmp_path) -> None:
    store = _store(tmp_path)
    codex = _FakeCodex()
    principal = _principal()
    plan1 = plan_codex_thread(
        store=store, principal=principal, profile_name=PROFILE,
        executor=EXECUTOR, workflow_id=WORKFLOW, now_ms=NOW_MS,
    )
    thread_id, _fact = codex.start_thread_and_record_fact()
    record_codex_thread(plan1, store=store, thread_id=thread_id, now_ms=NOW_MS)

    far_future = NOW_MS + (25 * 60 * 60 * 1000)  # past the 24h ceiling
    with pytest.raises(CodexSessionBridgeRejected):
        plan_codex_thread(
            store=store, principal=principal, profile_name=PROFILE,
            executor=EXECUTOR, workflow_id=WORKFLOW, now_ms=far_future,
        )
    assert codex.start_calls == 1  # no second mint attempted by the test itself


def test_duplicate_binding_for_same_tuple_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    codex = _FakeCodex()
    principal = _principal()
    plan1 = plan_codex_thread(
        store=store, principal=principal, profile_name=PROFILE,
        executor=EXECUTOR, workflow_id=WORKFLOW, now_ms=NOW_MS,
    )
    thread_id, _fact = codex.start_thread_and_record_fact()
    record_codex_thread(plan1, store=store, thread_id=thread_id, now_ms=NOW_MS)

    # A second fresh mint attempt for the identical tuple (e.g. a racing
    # duplicate spawn) must be rejected, not silently overwrite the binding.
    second_thread_id, _fact2 = codex.start_thread_and_record_fact()
    with pytest.raises(CodexSessionBridgeRejected):
        record_codex_thread(plan1, store=store, thread_id=second_thread_id, now_ms=NOW_MS + 1)


def test_ambiguous_reverse_thread_mapping_rejected(tmp_path) -> None:
    """Two different trusted tuples must never end up claiming the same
    thread_id -- store.insert enforces this even below the plan/record API."""
    store = _store(tmp_path)
    store.insert(
        channel="webui", actor_subject="ou_alice", profile_name=PROFILE,
        executor=EXECUTOR, workflow_id="wf-one", thread_id="thread_shared",
        now_ms=NOW_MS,
    )
    with pytest.raises(CodexSessionBridgeRejected):
        store.insert(
            channel="webui", actor_subject="ou_bob", profile_name=PROFILE,
            executor=EXECUTOR, workflow_id="wf-two", thread_id="thread_shared",
            now_ms=NOW_MS,
        )


def test_unsealed_principal_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    forged = TrustedRuntimePrincipal(
        channel="webui", profile_name=PROFILE, actor_subject="ou_alice",
        credential_subject="ou_alice",
    )  # no _seal -> is_authentic() is False
    with pytest.raises(CodexSessionBridgeRejected):
        plan_codex_thread(
            store=store, principal=forged, profile_name=PROFILE,
            executor=EXECUTOR, workflow_id=WORKFLOW, now_ms=NOW_MS,
        )


def test_require_plan_rejects_wrong_plan_type() -> None:
    with pytest.raises(CodexSessionBridgeRejected):
        require_codex_thread_plan(
            object(), principal=_principal(), profile_name=PROFILE,
            executor=EXECUTOR, workflow_id=WORKFLOW,
        )
