"""Phase 2 (card-fsm) — additive FSM guard tests.

Exercises ``hermes_multitenancy.card.state``'s new ``CardPhase`` / ``_transition``
/ ``_acquire_epoch`` surface without touching the existing
``test_streaming_card_transport.py`` 2074-line contract. Older state-dict
fields (``finalized`` / ``aborted``) remain in place for backward compat and
are NOT covered here — they stay covered by the streaming-card tests.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from hermes_multitenancy.card.state import (
    PHASE_ABORTED,
    PHASE_COMPLETED,
    PHASE_CREATING,
    PHASE_CREATION_FAILED,
    PHASE_ERROR,
    PHASE_IDLE,
    PHASE_STREAMING,
    PHASE_TERMINATED,
    _PHASE_TRANSITIONS,
    _acquire_epoch,
    _check_epoch,
    _new_state,
    _transition,
)

_ALL_PHASES = {
    PHASE_IDLE,
    PHASE_CREATING,
    PHASE_STREAMING,
    PHASE_ERROR,
    PHASE_COMPLETED,
    PHASE_ABORTED,
    PHASE_TERMINATED,
    PHASE_CREATION_FAILED,
}


def test_new_state_starts_at_idle_phase_with_epoch_zero():
    state = _new_state()
    assert state["phase"] == PHASE_IDLE
    assert state["epoch"] == 0
    # Backward-compat fields must remain present and falsy on a fresh state.
    assert state["finalized"] is False
    assert state["aborted"] is False
    assert state["errored"] is False


def test_phase_transitions_table_covers_all_states():
    assert set(_PHASE_TRANSITIONS.keys()) == _ALL_PHASES
    # Every reachable phase mentioned as a successor must itself be a key.
    for successors in _PHASE_TRANSITIONS.values():
        assert successors <= _ALL_PHASES
    # Terminal phases have no successors.
    for terminal in (PHASE_COMPLETED, PHASE_ABORTED, PHASE_ERROR, PHASE_TERMINATED, PHASE_CREATION_FAILED):
        assert _PHASE_TRANSITIONS[terminal] == frozenset()


def test_transition_writes_valid_phase():
    state = _new_state()
    assert _transition(state, PHASE_CREATING) is True
    assert state["phase"] == PHASE_CREATING
    assert _transition(state, PHASE_STREAMING) is True
    assert state["phase"] == PHASE_STREAMING
    assert _transition(state, PHASE_COMPLETED) is True
    assert state["phase"] == PHASE_COMPLETED


def test_transition_logs_and_keeps_old_phase_on_invalid(caplog):
    state = _new_state()
    _transition(state, PHASE_CREATING)
    _transition(state, PHASE_STREAMING)
    _transition(state, PHASE_COMPLETED)
    caplog.set_level(logging.WARNING, logger="hermes_multitenancy.feishu_cardkit_compat")
    # COMPLETED is terminal; any further transition must be refused.
    assert _transition(state, PHASE_STREAMING) is False
    assert state["phase"] == PHASE_COMPLETED  # original phase preserved
    assert any("illegal card phase transition" in record.message for record in caplog.records)


def test_transition_idempotent_re_entry_is_silent(caplog):
    state = _new_state()
    _transition(state, PHASE_CREATING)
    caplog.set_level(logging.WARNING, logger="hermes_multitenancy.feishu_cardkit_compat")
    # Re-transitioning to the current phase is a no-op without warning noise.
    assert _transition(state, PHASE_CREATING) is True
    assert state["phase"] == PHASE_CREATING
    assert not [record for record in caplog.records if "illegal" in record.message]


def test_epoch_acquire_is_monotonic_per_adapter():
    adapter = SimpleNamespace()
    seen = [_acquire_epoch(adapter) for _ in range(5)]
    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen)


def test_epoch_isolated_between_adapters():
    a = SimpleNamespace()
    b = SimpleNamespace()
    assert _acquire_epoch(a) == 1
    assert _acquire_epoch(b) == 1
    assert _acquire_epoch(a) == 2


def test_check_epoch_detects_stale_callback():
    state = _new_state()
    state["epoch"] = 7
    assert _check_epoch(state, 7) is True
    assert _check_epoch(state, 6) is False
    assert _check_epoch(state, 8) is False


def test_full_streaming_lifecycle_transitions_idle_to_completed():
    state = _new_state()
    assert state["phase"] == PHASE_IDLE
    _transition(state, PHASE_CREATING)
    _transition(state, PHASE_STREAMING)
    _transition(state, PHASE_COMPLETED)
    assert state["phase"] == PHASE_COMPLETED


def test_abort_path_transitions_streaming_to_aborted():
    state = _new_state()
    _transition(state, PHASE_CREATING)
    _transition(state, PHASE_STREAMING)
    assert _transition(state, PHASE_ABORTED) is True
    assert state["phase"] == PHASE_ABORTED


def test_abort_on_fresh_idle_state_succeeds_without_log_noise(caplog):
    """Aborting a message_id that was never started — e.g. popped or unknown —
    must legally transition idle → aborted without illegal-transition warnings.
    Covers the _abort_streaming_card path against a fresh _state_for state."""
    state = _new_state()
    assert state["phase"] == PHASE_IDLE
    caplog.set_level(logging.WARNING, logger="hermes_multitenancy.feishu_cardkit_compat")
    assert _transition(state, PHASE_ABORTED) is True
    assert state["phase"] == PHASE_ABORTED
    assert not [record for record in caplog.records if "illegal" in record.message]


def test_creation_failed_path_is_terminal_from_creating():
    state = _new_state()
    _transition(state, PHASE_CREATING)
    assert _transition(state, PHASE_CREATION_FAILED) is True
    assert state["phase"] == PHASE_CREATION_FAILED
    # Cannot recover from creation_failed.
    assert _transition(state, PHASE_STREAMING) is False
    assert state["phase"] == PHASE_CREATION_FAILED


def test_start_streaming_card_fsm_runs_idle_through_streaming():
    """Integration: ensure_feishu_cardkit_streaming + start_streaming_card moves
    a fresh state through idle → creating → streaming and acquires an epoch."""
    from hermes_multitenancy.card import ensure_feishu_cardkit_streaming
    from hermes_multitenancy.card.state import _STATE_ATTR

    class _DummyAdapter:
        def __init__(self):
            self.sent = []

        def _patch_auth_card(self, message_id, card):
            return True

        async def _feishu_send_with_retry(self, *, chat_id, msg_type, payload, reply_to=None, metadata=None):
            self.sent.append({"chat_id": chat_id, "payload": payload})
            return SimpleNamespace(success=True, message_id="msg-1")

        def _finalize_send_result(self, response, default_message):
            return SimpleNamespace(
                success=bool(getattr(response, "success", False)),
                message_id=getattr(response, "message_id", None),
            )

    adapter = ensure_feishu_cardkit_streaming(_DummyAdapter())
    assert adapter.supports_streaming_card() is True
    result = asyncio.run(adapter.start_streaming_card(chat_id="chat-1"))
    assert result.success is True
    assert result.message_id == "msg-1"
    states = getattr(adapter, _STATE_ATTR, {})
    state = states["msg-1"]
    assert state["phase"] == PHASE_STREAMING
    assert state["epoch"] >= 1
