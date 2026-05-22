"""Per-message streaming-card state + lightweight FSM guard layer.

State remains a ``dict`` for backward compatibility (router.py and tests read
``state["finalized"]`` / ``state["aborted"]`` directly). On top of the dict
we now carry an explicit ``phase`` (`CardPhase`) and ``epoch`` counter,
plus a ``_transition`` guard and ``_PHASE_TRANSITIONS`` adjacency table
mirroring openclaw-lark's lifecycle FSM. Illegal transitions are *logged*
rather than raised so production behavior is preserved verbatim.

Phase ordering::

    idle ─► creating ─► streaming ─► completed
                    │             ├► aborted
                    │             └► terminated
                    └► creation_failed
"""
from __future__ import annotations

import itertools
import logging
import time
from typing import Any, Literal

logger = logging.getLogger("hermes_multitenancy.feishu_cardkit_compat")

_INSTALLED_ATTR = "_hermes_mt_cardkit_compat_installed"
_STATE_ATTR = "_hermes_mt_streaming_card_state"
_EPOCH_ATTR = "_hermes_mt_streaming_card_epoch"

CardPhase = Literal[
    "idle",
    "creating",
    "streaming",
    "completed",
    "aborted",
    "terminated",
    "creation_failed",
]

PHASE_IDLE: CardPhase = "idle"
PHASE_CREATING: CardPhase = "creating"
PHASE_STREAMING: CardPhase = "streaming"
PHASE_COMPLETED: CardPhase = "completed"
PHASE_ABORTED: CardPhase = "aborted"
PHASE_TERMINATED: CardPhase = "terminated"
PHASE_CREATION_FAILED: CardPhase = "creation_failed"

# Adjacency table — keys are allowed source phases, values are the set of
# phases that may legally follow. Mirrors openclaw-lark `PHASE_TRANSITIONS`.
_PHASE_TRANSITIONS: dict[CardPhase, frozenset[CardPhase]] = {
    # idle → aborted is permitted so ``_abort_streaming_card`` against a
    # never-started or already-popped ``message_id`` (where ``_state_for``
    # materializes a fresh idle state) records the abort intent cleanly
    # instead of producing illegal-transition log noise.
    PHASE_IDLE: frozenset({PHASE_CREATING, PHASE_ABORTED}),
    PHASE_CREATING: frozenset({PHASE_STREAMING, PHASE_CREATION_FAILED, PHASE_ABORTED, PHASE_TERMINATED}),
    PHASE_STREAMING: frozenset({PHASE_COMPLETED, PHASE_ABORTED, PHASE_TERMINATED}),
    PHASE_COMPLETED: frozenset(),
    PHASE_ABORTED: frozenset(),
    PHASE_TERMINATED: frozenset(),
    PHASE_CREATION_FAILED: frozenset(),
}


def _new_state() -> dict[str, Any]:
    return {
        "status": "",
        "reasoning": "",
        "reasoning_started_at": None,
        "reasoning_elapsed": None,
        "content": "",
        "tools": [],
        "started_at": time.monotonic(),
        "finalized": False,
        "aborted": False,
        "card_id": None,
        "original_card_id": None,
        "sequence": 0,
        # FSM additions (additive, do not break callers that read finalized/aborted)
        "phase": PHASE_IDLE,
        "epoch": 0,
        # Phase 6 footer observability — router merges metrics in via
        # update_streaming_card_metrics; rendered in done footer when
        # FOOTER_SHOW_METRICS env flag is true.
        "tokens_in": None,
        "tokens_out": None,
        "cache_hit_pct": None,
        "context_pct": None,
        "model_name": None,
    }


def _states(adapter: Any) -> dict[str, dict[str, Any]]:
    state = getattr(adapter, _STATE_ATTR, None)
    if not isinstance(state, dict):
        state = {}
        setattr(adapter, _STATE_ATTR, state)
    return state


def _state_for(adapter: Any, message_id: str) -> dict[str, Any]:
    return _states(adapter).setdefault(str(message_id), _new_state())


def _next_sequence(state: dict[str, Any]) -> int:
    state["sequence"] = int(state.get("sequence") or 0) + 1
    return int(state["sequence"])


def _transition(state: dict[str, Any], new_phase: CardPhase) -> bool:
    """Move ``state["phase"]`` to ``new_phase`` if the FSM allows it.

    Returns ``True`` when the transition is legal and was written; ``False``
    when illegal (in which case a warning is logged and the original phase is
    preserved — production behavior must not change because of the FSM).
    """
    current = state.get("phase") or PHASE_IDLE
    allowed = _PHASE_TRANSITIONS.get(current, frozenset())
    if new_phase == current:
        # Idempotent re-entry is harmless; treat as success without log noise.
        return True
    if new_phase not in allowed:
        logger.warning(
            "multitenancy: illegal card phase transition %s -> %s (allowed: %s); keeping current phase",
            current,
            new_phase,
            sorted(allowed),
        )
        return False
    state["phase"] = new_phase
    return True


def _acquire_epoch(adapter: Any) -> int:
    """Return the next monotonic epoch for ``adapter``.

    Each ``start_streaming_card`` invocation acquires a fresh epoch; long-lived
    callbacks snapshot the epoch at entry and ``_check_epoch`` lets them no-op
    if a newer run has started in the meantime. The counter lives on the
    adapter under ``_hermes_mt_streaming_card_epoch``.
    """
    counter = getattr(adapter, _EPOCH_ATTR, None)
    if not isinstance(counter, itertools.count):
        counter = itertools.count(1)
        setattr(adapter, _EPOCH_ATTR, counter)
    return next(counter)


def _check_epoch(state: dict[str, Any], snapshot: int) -> bool:
    """Return ``True`` iff ``state``'s epoch still matches the snapshot."""
    current = int(state.get("epoch") or 0)
    return current == int(snapshot)
