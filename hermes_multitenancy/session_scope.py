"""Typed, fail-closed session boundary (P1-7 SessionScope).

A single, strongly-typed construction point for the keys that scope a
conversation: history/memory, in-flight (replace / /stop / /status). Built on
top of the P0-2 ``TenantContext`` identity so a dispatch's *who* (tenant) and
*where* (channel/chat/thread) are resolved together and never assembled ad hoc.

Strict-gating (matches P0-1..P0-6 and TenantContext):
  - **default (HERMES_MULTITENANCY_STRICT_CONTEXT off)** — the keys are
    BYTE-IDENTICAL to the legacy ``(profile, user_key)`` / ``(profile, chat,
    user_key)`` tuples. Zero prod behavior change, no session-history orphaning,
    no DB migration (the SessionStore signature stays a 2-tuple).
  - **strict on** — channel / chat_id / thread_id / route_version are folded
    into the user-key *dimension* so DM vs group vs webui vs cron vs distinct
    threads never share a session. Still a 2-tuple → SessionStore untouched.

The scope is a frozen dataclass; ``channel`` and ``user_key`` are required
(empty → ValueError) so a half-built scope can never silently widen isolation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .runtime import strict_context_enabled

# Unit separator — same delimiter the router uses for composite keys, chosen
# because it cannot appear in an open_id / chat_id / channel name.
_SEP = "\x1f"

# Canonical channel names. Surfaces that don't pass a channel default to feishu
# (the legacy assumption), so default-mode keys stay byte-identical.
CHANNEL_FEISHU = "feishu"
CHANNEL_WEBUI = "webui"
CHANNEL_CRON = "cron"


@dataclass(frozen=True)
class SessionScope:
    """Resolved session boundary for a single dispatch.

    ``user_key`` is the stable per-user key (open_id-first; same value the
    legacy ``_tenant_user_key`` produced). ``route_version`` lets a session
    invalidate when the underlying routing row changes (strict mode only).
    """

    profile_name: str
    channel: str
    user_key: str
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    route_version: int = 0
    shared_history: bool = False

    def __post_init__(self) -> None:
        # Fail-closed: a scope with no channel or no user_key must never be
        # constructed — it would collapse distinct sessions into one key.
        if not str(self.channel or "").strip():
            raise ValueError("SessionScope.channel is required")
        if not str(self.user_key or "").strip():
            raise ValueError("SessionScope.user_key is required")

    @property
    def history_key(self) -> tuple[str, str]:
        """``(profile, key)`` for SessionStore / conversation history.

        Default → ``(profile_name, user_key)`` (legacy parity). Strict → the
        second element folds channel/chat/thread/route_version so surfaces and
        threads don't cross. Always a 2-tuple (no store-signature change)."""
        if not strict_context_enabled():
            return (self.profile_name, self.user_key)
        history_user = "_group_topic" if self.shared_history else self.user_key
        composite = _SEP.join(
            (
                self.channel,
                history_user,
                str(self.chat_id or ""),
                str(self.thread_id or ""),
                f"v{int(self.route_version)}",
            )
        )
        return (self.profile_name, composite)

    @property
    def inflight_key(self) -> tuple[str, str, str]:
        """``(profile, chat, key)`` for replace / /stop / /status.

        Default → legacy ``(profile_key, chat_key, user_key)``. Strict → the
        user dimension folds channel + thread so concurrent same-user/same-chat
        runs on different surfaces don't cancel one another."""
        profile_key = str(self.profile_name or "").strip() or "_unrouted"
        chat_key = str(self.chat_id or "").strip() or "unknown"
        if not strict_context_enabled():
            return (profile_key, chat_key, self.user_key)
        user_dim = _SEP.join((self.channel, self.user_key, str(self.thread_id or "")))
        return (profile_key, chat_key, user_dim)


def build_session_scope(
    *,
    profile_name: Optional[str],
    user_key: str,
    channel: str = CHANNEL_FEISHU,
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    route_version: int = 0,
    shared_history: bool = False,
) -> SessionScope:
    """Construct a SessionScope from already-resolved parts.

    ``profile_name`` is normalized to a stable string (None → "" so the legacy
    history key, which used the raw profile_name, is preserved exactly).
    """
    # Do NOT coerce a blank channel to a default here — that would defeat the
    # SessionScope fail-closed check. The default lives on the parameter so
    # callers that omit it get feishu, but an explicit blank/None is an error.
    return SessionScope(
        profile_name=str(profile_name or ""),
        channel=str(channel) if channel is not None else "",
        user_key=str(user_key or ""),
        chat_id=(str(chat_id) if chat_id else None),
        thread_id=(str(thread_id) if thread_id else None),
        route_version=int(route_version or 0),
        shared_history=bool(shared_history),
    )
