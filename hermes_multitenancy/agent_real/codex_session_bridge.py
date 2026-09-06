"""Bind one trusted (principal, profile, executor, workflow) tuple to exactly
one Codex ``thread_id`` and fail closed before any spawn/model/tool call on
mismatch, staleness, duplication, or ambiguity (SPEC ticket 01).

Design notes
------------
- The tuple is looked up server-side (:func:`plan_codex_thread`) purely from
  our own sealed store -- callers never pass a candidate ``thread_id`` in, so
  a forged/request-carried thread id has no code path that can select the
  resumed thread.
- Storage reuses the exact pattern ``credentials.py``'s ``CredentialStore``
  already uses in this repo: a dedicated sqlite table, its own connection,
  UNIQUE constraints doing the fail-closed heavy lifting. No new DB engine,
  no second identity system -- this is the "single sqlite table in the
  existing MT state dir" the ticket asks for when nothing else fits, because
  nothing else in this repo maps a trusted tuple to a third-party session id.
- Real ``thread/resume`` wiring is a cross-repo gap: hermes-agent's
  ``CodexAppServerSession``/``run_codex_app_server_turn`` only ever call
  ``thread/start`` today (see SEAM MAP trusted_principal_and_sessions). This
  module owns the lookup/bind decision only; ``plan_codex_thread`` /
  ``record_codex_thread`` are the ready-to-call hook a later ticket wires at
  the actual spawn site once hermes-agent (or a loopback stub, tickets
  03/05) can accept a resume thread id. Wiring it into ``run.py`` now would
  either pass a kwarg nothing downstream reads, or require driving the codex
  JSON-RPC protocol directly here -- the latter reads as a second executor,
  which this SPEC's dead-ends forbid. Not done in this ticket.
# ponytail: staleness ceiling is a fixed 24h constant (_DEFAULT_MAX_AGE_MS);
# make it configurable if a workflow legitimately needs a longer-lived thread.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..trusted_runtime_principal import TrustedRuntimePrincipal


_OPAQUE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_DEFAULT_MAX_AGE_MS = 24 * 60 * 60 * 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS codex_session_threads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    channel        TEXT NOT NULL,
    actor_subject  TEXT NOT NULL,
    profile_name   TEXT NOT NULL,
    executor       TEXT NOT NULL,
    workflow_id    TEXT NOT NULL,
    thread_id      TEXT NOT NULL,
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL,
    UNIQUE(channel, actor_subject, profile_name, executor, workflow_id),
    UNIQUE(thread_id)
);
"""


class CodexSessionBridgeRejected(ValueError):
    """The trusted tuple, existing binding, or thread_id is not trustworthy."""


def _opaque(field_name: str, value: Any) -> str:
    text = str(value or "")
    if not _OPAQUE_ID.fullmatch(text):
        raise CodexSessionBridgeRejected(f"{field_name}_invalid")
    return text


class CodexSessionBridgeStore:
    """SQLite-backed tuple -> thread_id binding, mirroring CredentialStore."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        Path(self.db_path).chmod(0o600)

    def _rows_for_tuple(
        self, channel: str, actor_subject: str, profile_name: str, executor: str, workflow_id: str
    ) -> list[sqlite3.Row]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM codex_session_threads WHERE channel=? AND actor_subject=? "
                "AND profile_name=? AND executor=? AND workflow_id=?",
                (channel, actor_subject, profile_name, executor, workflow_id),
            )
            return cursor.fetchall()

    def _rows_for_thread(self, thread_id: str) -> list[sqlite3.Row]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM codex_session_threads WHERE thread_id=?", (thread_id,)
            )
            return cursor.fetchall()

    def insert(
        self,
        *,
        channel: str,
        actor_subject: str,
        profile_name: str,
        executor: str,
        workflow_id: str,
        thread_id: str,
        now_ms: int,
    ) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO codex_session_threads "
                    "(channel, actor_subject, profile_name, executor, workflow_id, "
                    "thread_id, created_at_ms, updated_at_ms) VALUES (?,?,?,?,?,?,?,?)",
                    (channel, actor_subject, profile_name, executor, workflow_id,
                     thread_id, now_ms, now_ms),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise CodexSessionBridgeRejected("binding_conflict") from exc

    def close(self) -> None:
        self._conn.close()


@dataclass(frozen=True, slots=True)
class CodexThreadPlan:
    """Result of a pre-spawn lookup. ``resume_thread_id`` is ``None`` for a
    tuple with no existing binding (caller must spawn fresh), else the exact
    thread_id to resume -- never a value derived from request/event data."""

    resume_thread_id: str | None
    _tuple_key: tuple[str, str, str, str, str] = field(repr=False)


def _validate_principal(
    principal: TrustedRuntimePrincipal, profile_name: str
) -> tuple[str, str]:
    if (
        not isinstance(principal, TrustedRuntimePrincipal)
        or not principal.is_authentic()
        or not principal.channel
        or not principal.profile_name
        or not principal.actor_subject
        or principal.credential_subject != principal.actor_subject
        or principal.profile_name != str(profile_name or "")
    ):
        raise CodexSessionBridgeRejected("principal_invalid")
    return principal.channel, principal.actor_subject


def _clock(now_ms: int) -> int:
    if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms <= 0:
        raise CodexSessionBridgeRejected("clock_invalid")
    return now_ms


def plan_codex_thread(
    *,
    store: CodexSessionBridgeStore,
    principal: TrustedRuntimePrincipal,
    profile_name: str,
    executor: str,
    workflow_id: str,
    now_ms: int,
    max_age_ms: int = _DEFAULT_MAX_AGE_MS,
) -> CodexThreadPlan:
    """Look up an existing thread binding for this trusted tuple BEFORE any
    spawn/model/tool call. Raises :class:`CodexSessionBridgeRejected` (never
    returns) for a stale or ambiguous row -- callers must not spawn, call a
    model, or call a tool when this raises."""
    channel, actor_subject = _validate_principal(principal, profile_name)
    profile_name = _opaque("profile_name", profile_name)
    executor = _opaque("executor", executor)
    workflow_id = _opaque("workflow_id", workflow_id)
    now_ms = _clock(now_ms)

    tuple_key = (channel, actor_subject, profile_name, executor, workflow_id)
    rows = store._rows_for_tuple(*tuple_key)
    if not rows:
        return CodexThreadPlan(resume_thread_id=None, _tuple_key=tuple_key)
    if len(rows) != 1:
        raise CodexSessionBridgeRejected("binding_ambiguous")
    row = rows[0]
    if now_ms < row["updated_at_ms"] or now_ms - row["updated_at_ms"] > max_age_ms:
        raise CodexSessionBridgeRejected("binding_stale")
    thread_id = row["thread_id"]
    # A live binding's thread_id must resolve back to exactly this one row --
    # otherwise some other tuple also claims it, which the UNIQUE(thread_id)
    # constraint should prevent, but a fail-closed re-check costs nothing.
    reverse = store._rows_for_thread(thread_id)
    if len(reverse) != 1 or reverse[0]["id"] != row["id"]:
        raise CodexSessionBridgeRejected("binding_ambiguous")
    return CodexThreadPlan(resume_thread_id=thread_id, _tuple_key=tuple_key)


def require_codex_thread_plan(
    plan: object,
    *,
    principal: TrustedRuntimePrincipal,
    profile_name: str,
    executor: str,
    workflow_id: str,
) -> CodexThreadPlan:
    """Re-validate a previously computed plan against the CURRENT trusted
    tuple immediately before using it to resume. Fail closed on any drift."""
    if not isinstance(plan, CodexThreadPlan):
        raise CodexSessionBridgeRejected("plan_invalid")
    channel, actor_subject = _validate_principal(principal, profile_name)
    profile_name = _opaque("profile_name", profile_name)
    executor = _opaque("executor", executor)
    workflow_id = _opaque("workflow_id", workflow_id)
    if plan._tuple_key != (channel, actor_subject, profile_name, executor, workflow_id):
        raise CodexSessionBridgeRejected("tuple_mismatch")
    return plan


def record_codex_thread(
    plan: CodexThreadPlan,
    *,
    store: CodexSessionBridgeStore,
    thread_id: str,
    now_ms: int,
) -> None:
    """Persist a freshly minted thread_id for a tuple that had no existing
    binding. Raises if the tuple already has a binding (duplicate spawn) or
    the thread_id is already claimed by another tuple (ambiguous)."""
    if not isinstance(plan, CodexThreadPlan):
        raise CodexSessionBridgeRejected("plan_invalid")
    if plan.resume_thread_id is not None:
        raise CodexSessionBridgeRejected("binding_duplicate")
    thread_id = _opaque("thread_id", thread_id)
    now_ms = _clock(now_ms)
    if store._rows_for_thread(thread_id):
        raise CodexSessionBridgeRejected("binding_ambiguous")
    channel, actor_subject, profile_name, executor, workflow_id = plan._tuple_key
    store.insert(
        channel=channel,
        actor_subject=actor_subject,
        profile_name=profile_name,
        executor=executor,
        workflow_id=workflow_id,
        thread_id=thread_id,
        now_ms=now_ms,
    )
