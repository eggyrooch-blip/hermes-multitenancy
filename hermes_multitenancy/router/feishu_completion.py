"""Match Feishu defer generations to one shared broker completion."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


_CLAIM_ATTR = "_mt_deferred_completion_claim"
_STATES_ATTR = "_mt_deferred_completion_generations"
_MAX_COMPLETED_STATES = 2048


@dataclass(frozen=True)
class _CompletionClaim:
    adapter_id: int
    message_id: str
    sequence: int


@dataclass
class _CompletionState:
    registered: int = 0
    covered_through: int = 0


def _message_id(event: Any) -> str:
    return str(getattr(event, "message_id", "") or "")


def _states(adapter: Any) -> "OrderedDict[str, _CompletionState]":
    states = getattr(adapter, _STATES_ATTR, None)
    if not isinstance(states, OrderedDict):
        states = OrderedDict()
        setattr(adapter, _STATES_ATTR, states)
    return states


def register_deferred_completion(adapter: Any, event: Any) -> None:
    """Attach an ordered claim after the adapter successfully defers a message."""
    message_id = _message_id(event)
    if adapter is None or not message_id:
        return
    states = _states(adapter)
    state = states.get(message_id)
    if state is None:
        if len(states) >= _MAX_COMPLETED_STATES:
            for old_message_id, old_state in list(states.items()):
                if len(states) < _MAX_COMPLETED_STATES:
                    break
                if old_state.covered_through >= old_state.registered:
                    states.pop(old_message_id, None)
        if len(states) >= _MAX_COMPLETED_STATES:
            # Under an extreme number of simultaneously unfinished messages,
            # fall back to the shared finalizer without a late-generation
            # claim rather than growing an unbounded adapter-side map.
            return
        state = _CompletionState()
        states[message_id] = state
    state.registered += 1
    setattr(
        event,
        _CLAIM_ATTR,
        _CompletionClaim(id(adapter), message_id, state.registered),
    )
    states.move_to_end(message_id)


def begin_deferred_completion(adapter: Any, event: Any) -> None:
    """Snapshot every defer that the immediately-following adapter call covers."""
    message_id = _message_id(event)
    if adapter is None or not message_id:
        return
    claim = getattr(event, _CLAIM_ATTR, None)
    if not isinstance(claim, _CompletionClaim) or claim.adapter_id != id(adapter):
        return
    state = _states(adapter).get(message_id)
    if state is None:
        return
    state.covered_through = max(state.covered_through, state.registered)


def deferred_completion_is_covered(adapter: Any, event: Any) -> bool:
    """Return whether this hook claim was present at an earlier completion."""
    claim = getattr(event, _CLAIM_ATTR, None)
    if not isinstance(claim, _CompletionClaim) or claim.adapter_id != id(adapter):
        return False
    states = getattr(adapter, _STATES_ATTR, None)
    if not isinstance(states, OrderedDict):
        return False
    state = states.get(claim.message_id)
    return state is not None and claim.sequence <= state.covered_through


def has_deferred_completion_claim(adapter: Any, event: Any) -> bool:
    claim = getattr(event, _CLAIM_ATTR, None)
    return isinstance(claim, _CompletionClaim) and claim.adapter_id == id(adapter)
