"""Per-message streaming-card state (dict-based, Phase 2 will replace with FSM).

State lives on the adapter under ``_hermes_mt_streaming_card_state`` keyed by
``message_id``. ``_next_sequence`` is the monotonic counter CardKit demands on
every card-element / card.settings / card.update call.
"""
from __future__ import annotations

import time
from typing import Any

_INSTALLED_ATTR = "_hermes_mt_cardkit_compat_installed"
_STATE_ATTR = "_hermes_mt_streaming_card_state"


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
