"""UnavailableGuard — centralized recall / delete / timeout detection.

Mirrors openclaw-lark ``UnavailableGuard``: every streaming-card callback
calls ``should_proceed(adapter, message_id)`` at entry, and ``mark_unavailable``
is called from the error path when Feishu reports the source message has
been recalled (``99991663``) or deleted (``230006``). Once a message is
flagged unavailable, all subsequent callbacks short-circuit instead of
calling the API again (which would fail and produce log noise).

The unavailable set lives on the adapter under
``_hermes_mt_streaming_card_unavailable`` to keep state per-adapter and
per-message, the same shape as the existing ``_states`` dict.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("hermes_multitenancy.feishu_cardkit_compat")

_UNAVAILABLE_ATTR = "_hermes_mt_streaming_card_unavailable"

# Lark codes that mean the source message no longer exists or is otherwise
# unreachable. ``should_proceed`` returns False after any of these is reported.
_RECALLED_CODE = 99991663
_DELETED_CODE = 230006
UNAVAILABLE_CODES = frozenset({_RECALLED_CODE, _DELETED_CODE})


def _unavailable_set(adapter: Any) -> set[str]:
    """Return (lazily create) the per-adapter unavailable message-id set."""
    existing = getattr(adapter, _UNAVAILABLE_ATTR, None)
    if not isinstance(existing, set):
        existing = set()
        setattr(adapter, _UNAVAILABLE_ATTR, existing)
    return existing


class UnavailableGuard:
    """Stateless facade — all storage lives on the adapter."""

    @staticmethod
    def should_proceed(adapter: Any, message_id: str) -> bool:
        """Return True iff the message is still considered reachable.

        Callers MUST invoke this at the entry of every streaming-card
        callback. If False, the callback should return a ``success=False``
        result without touching the API.
        """
        if not message_id:
            return True  # Pre-message_id paths (e.g. start_streaming_card) always proceed.
        return str(message_id) not in _unavailable_set(adapter)

    @staticmethod
    def mark_unavailable(adapter: Any, message_id: str, code: int) -> None:
        """Flag ``message_id`` as unavailable so subsequent calls short-circuit.

        Logs at WARNING level once per message_id transition (the set itself
        prevents duplicate marks). ``code`` is recorded only in the log line —
        the set is just message_ids.
        """
        if not message_id:
            return
        unavailable = _unavailable_set(adapter)
        key = str(message_id)
        if key in unavailable:
            return
        unavailable.add(key)
        logger.warning(
            "multitenancy: marking Feishu card message %s as unavailable (lark code=%s)",
            key,
            code,
        )

    @staticmethod
    def clear(adapter: Any, message_id: str) -> None:
        """Remove a message_id from the unavailable set (used in tests)."""
        if not message_id:
            return
        _unavailable_set(adapter).discard(str(message_id))
