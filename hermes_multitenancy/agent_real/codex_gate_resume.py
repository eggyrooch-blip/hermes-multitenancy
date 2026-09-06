"""Gate A->B->C->D->E->F WAITING_GATE, one-time resume capability, and same-thread
resume for a mapped Codex dry-run write intent (SPEC ticket 03).

Design notes
------------
- Reuses the existing patterns in this package rather than inventing a
  second capability/token system: the storage shape mirrors
  ``codex_session_bridge.CodexSessionBridgeStore`` (t01) -- one sqlite table
  per concern, one connection, one lock, chmod 0600 -- because a real
  approval round-trips through the same per-turn subprocess boundary t01's
  thread binding already has to survive. The single-consumption idiom
  (``consumed_at_ms`` set exactly once, checked before any observable
  effect) mirrors ``single_actor_spend_receipt._SpendState``'s
  seal-object-identity + used-flag pattern.
- ``consume_gate_capability`` holds the store's lock for the ENTIRE
  check-then-act sequence (read capability + read run state + mark consumed
  + advance state), not just each individual sqlite call -- that whole
  sequence is exactly the thing that must be atomic under concurrent double
  consume, and a per-call lock (t01's finer-grained style, correct there
  because it never needs a compound read-then-write decision) would leave a
  race window here. The store's lock is an ``RLock`` precisely so the
  internal ``_row``/``_upsert``/``_mark_consumed`` helpers can each also
  hold it for their own single statement without deadlocking against the
  outer acquisition, matching t01's per-call-lock convention at the leaf
  level.
- Only the state machine + capability boundary live here. Nothing calls a
  real write executor, mints a real credential, or touches GitLab -- the
  "write action" a consumed capability unlocks is nothing but
  ``GateResumeResult.write_action_count``, a local fixture counter (ticket
  03's explicit scope line). Wiring this into the actual write-intent
  detection point in ``_core.py``/``streaming.py`` is deferred to ticket 05,
  once a real write intent exists to trigger ``enter_waiting_gate`` from --
  same deferral shape as t01's spawn-site wiring.
- The one additive hook this ticket makes in a shared file:
  ``executor_unavailable_ux._UNAVAILABLE_TYPES`` now also recognizes
  ``GateResumeRejected`` (that module's own ponytail comment anticipated
  exactly this, see its t03 note), and every ``GateResumeRejected`` carries
  ``.code = "CODEX_GATE_DENIED"`` so ``classify()`` picks it up via the
  explicit-code path it already prefers over keyword matching -- no new UX
  layer, no duplicated audit call.
# ponytail: capability TTL is a fixed 5-minute constant (_DEFAULT_TTL_MS);
# make it configurable if a real gate needs a longer approval window.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GATES: tuple[str, ...] = ("A", "B", "C", "D", "E", "F")
_GATE_INDEX = {gate: index for index, gate in enumerate(GATES)}
_OPAQUE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_DEFAULT_TTL_MS = 5 * 60 * 1000

# Matches executor_unavailable_ux.CODEX_GATE_DENIED verbatim -- duplicated as
# a literal (not imported) to keep the dependency direction one-way: that
# module depends on this one's exception type, not the other way round.
_UNAVAILABLE_CODE = "CODEX_GATE_DENIED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS codex_gate_state (
    run_id              TEXT PRIMARY KEY,
    actor_subject       TEXT NOT NULL,
    thread_id           TEXT NOT NULL,
    gate                TEXT NOT NULL,
    status              TEXT NOT NULL,
    write_action_count  INTEGER NOT NULL,
    updated_at_ms       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS codex_gate_capabilities (
    token           TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    actor_subject   TEXT NOT NULL,
    thread_id       TEXT NOT NULL,
    gate            TEXT NOT NULL,
    expires_at_ms   INTEGER NOT NULL,
    consumed_at_ms  INTEGER
);
"""

_STATUS_ZH = {"waiting": "等待确认", "done": "已完成"}


class GateResumeRejected(ValueError):
    """A gate-wait entry, capability mint, or capability consume call is not
    trustworthy. Carries a stable ``.code`` recognized outright by
    ``executor_unavailable_ux.classify()`` and a bare-token ``reason`` for
    internal audit -- never sentence-shaped, never event/request content."""

    def __init__(self, reason: str) -> None:
        self.code = _UNAVAILABLE_CODE
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class GateState:
    run_id: str
    actor_subject: str
    thread_id: str
    gate: str
    status: str  # "waiting" | "done"
    write_action_count: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class GateResumeResult:
    resume_thread_id: str
    resumed_gate: str
    next_gate: str | None
    status: str  # "waiting" | "done"
    write_action_count: int


def current_gate_state(store: GateResumeStore, run_id: str) -> GateState | None:
    """Read-only projection of a run's current gate/status/write-action
    count -- for card rendering, without mutating anything. ``None`` for a
    run that has never entered a gate."""
    run_id = _opaque("run_id", run_id)
    row = store._state_row(run_id)
    return _row_to_state(row) if row is not None else None


def describe_gate_state(state: GateState) -> dict[str, Any]:
    """Pure card/status projection: gate + Chinese status text + counter."""
    return {
        "gate": state.gate,
        "status": state.status,
        "status_zh": _STATUS_ZH.get(state.status, "等待确认"),
        "write_action_count": state.write_action_count,
    }


def _opaque(field_name: str, value: Any) -> str:
    text = str(value) if isinstance(value, str) else ""
    if not _OPAQUE_ID.fullmatch(text):
        raise GateResumeRejected(f"{field_name}_invalid")
    return text


def _gate(value: Any) -> str:
    text = str(value) if isinstance(value, str) else ""
    if text not in _GATE_INDEX:
        raise GateResumeRejected("gate_invalid")
    return text


def _clock(now_ms: Any) -> int:
    if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms <= 0:
        raise GateResumeRejected("clock_invalid")
    return now_ms


def _ttl(ttl_ms: Any) -> int:
    if not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool) or ttl_ms <= 0:
        raise GateResumeRejected("ttl_invalid")
    return ttl_ms


def _row_to_state(row: sqlite3.Row) -> GateState:
    return GateState(
        run_id=row["run_id"],
        actor_subject=row["actor_subject"],
        thread_id=row["thread_id"],
        gate=row["gate"],
        status=row["status"],
        write_action_count=row["write_action_count"],
        updated_at_ms=row["updated_at_ms"],
    )


class GateResumeStore:
    """SQLite-backed gate-wait state + one-time capability store."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        Path(self.db_path).chmod(0o600)

    def _state_row(self, run_id: str) -> sqlite3.Row | None:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM codex_gate_state WHERE run_id=?", (run_id,)
            )
            return cursor.fetchone()

    def _upsert_state(
        self,
        *,
        run_id: str,
        actor_subject: str,
        thread_id: str,
        gate: str,
        status: str,
        write_action_count: int,
        now_ms: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO codex_gate_state (run_id, actor_subject, thread_id, gate, "
                "status, write_action_count, updated_at_ms) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET actor_subject=excluded.actor_subject, "
                "thread_id=excluded.thread_id, gate=excluded.gate, status=excluded.status, "
                "write_action_count=excluded.write_action_count, "
                "updated_at_ms=excluded.updated_at_ms",
                (run_id, actor_subject, thread_id, gate, status, write_action_count, now_ms),
            )
            self._conn.commit()

    def _capability_row(self, token: str) -> sqlite3.Row | None:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM codex_gate_capabilities WHERE token=?", (token,)
            )
            return cursor.fetchone()

    def _insert_capability(
        self,
        *,
        token: str,
        run_id: str,
        actor_subject: str,
        thread_id: str,
        gate: str,
        expires_at_ms: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO codex_gate_capabilities "
                "(token, run_id, actor_subject, thread_id, gate, expires_at_ms, consumed_at_ms) "
                "VALUES (?,?,?,?,?,?,NULL)",
                (token, run_id, actor_subject, thread_id, gate, expires_at_ms),
            )
            self._conn.commit()

    def _mark_capability_consumed(self, *, token: str, now_ms: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE codex_gate_capabilities SET consumed_at_ms=? WHERE token=?",
                (now_ms, token),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def enter_waiting_gate(
    *,
    store: GateResumeStore,
    run_id: str,
    actor_subject: str,
    thread_id: str,
    gate: str,
    now_ms: int,
) -> GateState:
    """Record a write intent's WAITING_GATE for ``gate``. Fail closed unless
    ``gate`` is exactly this run's next expected gate: ``A`` for a brand-new
    run, or the SAME gate the run is already waiting on (a retried write
    intent is a harmless no-op, not an advance). The write-action counter is
    never touched here -- only ``consume_gate_capability`` increments it."""
    run_id = _opaque("run_id", run_id)
    actor_subject = _opaque("actor_subject", actor_subject)
    thread_id = _opaque("thread_id", thread_id)
    gate = _gate(gate)
    now_ms = _clock(now_ms)
    with store._lock:
        row = store._state_row(run_id)
        if row is None:
            if gate != GATES[0]:
                raise GateResumeRejected("gate_out_of_order")
            write_action_count = 0
        else:
            if row["status"] == "done":
                raise GateResumeRejected("run_already_done")
            if row["actor_subject"] != actor_subject or row["thread_id"] != thread_id:
                raise GateResumeRejected("tuple_mismatch")
            if row["gate"] != gate:
                raise GateResumeRejected("gate_out_of_order")
            write_action_count = row["write_action_count"]
        store._upsert_state(
            run_id=run_id, actor_subject=actor_subject, thread_id=thread_id,
            gate=gate, status="waiting", write_action_count=write_action_count,
            now_ms=now_ms,
        )
        return _row_to_state(store._state_row(run_id))


def issue_gate_capability(
    *,
    store: GateResumeStore,
    run_id: str,
    actor_subject: str,
    thread_id: str,
    gate: str,
    now_ms: int,
    ttl_ms: int = _DEFAULT_TTL_MS,
) -> str:
    """Mint a one-time capability bound to (actor, run, gate, thread). Only
    mintable for the run's CURRENT waiting gate -- a capability for a gate
    the run isn't waiting on yet, or has already passed, cannot be created."""
    run_id = _opaque("run_id", run_id)
    actor_subject = _opaque("actor_subject", actor_subject)
    thread_id = _opaque("thread_id", thread_id)
    gate = _gate(gate)
    now_ms = _clock(now_ms)
    ttl_ms = _ttl(ttl_ms)
    with store._lock:
        row = store._state_row(run_id)
        if (
            row is None
            or row["status"] != "waiting"
            or row["gate"] != gate
            or row["actor_subject"] != actor_subject
            or row["thread_id"] != thread_id
        ):
            raise GateResumeRejected("gate_out_of_order")
        token = secrets.token_urlsafe(32)
        store._insert_capability(
            token=token, run_id=run_id, actor_subject=actor_subject,
            thread_id=thread_id, gate=gate, expires_at_ms=now_ms + ttl_ms,
        )
        return token


def consume_gate_capability(
    *,
    store: GateResumeStore,
    token: str,
    run_id: str,
    actor_subject: str,
    thread_id: str,
    gate: str,
    now_ms: int,
) -> GateResumeResult:
    """Atomically consume a capability exactly once and resume the SAME
    thread to the next gate. Fails closed -- without marking anything
    consumed -- on any mismatch, expiry, or the run having moved on since
    this capability was minted. The whole check-then-act sequence runs
    under one lock so a concurrent second caller always observes either
    "not yet consumed, proceed" or "already consumed, reject" -- never a
    torn read between the two."""
    token = _opaque("token", token)
    run_id = _opaque("run_id", run_id)
    actor_subject = _opaque("actor_subject", actor_subject)
    thread_id = _opaque("thread_id", thread_id)
    gate = _gate(gate)
    now_ms = _clock(now_ms)
    with store._lock:
        store._conn.execute("BEGIN IMMEDIATE")
        try:
            cap = store._conn.execute(
                "SELECT * FROM codex_gate_capabilities WHERE token=?", (token,)
            ).fetchone()
            if cap is None:
                raise GateResumeRejected("capability_unknown")
            if cap["consumed_at_ms"] is not None:
                raise GateResumeRejected("capability_replayed")
            for actual, expected, reason in (
                (cap["run_id"], run_id, "run_mismatch"),
                (cap["actor_subject"], actor_subject, "actor_mismatch"),
                (cap["thread_id"], thread_id, "thread_mismatch"),
                (cap["gate"], gate, "gate_mismatch"),
            ):
                if actual != expected:
                    raise GateResumeRejected(reason)
            if now_ms >= cap["expires_at_ms"]:
                raise GateResumeRejected("capability_expired")
            state_row = store._conn.execute(
                "SELECT * FROM codex_gate_state WHERE run_id=?", (run_id,)
            ).fetchone()
            if (
                state_row is None
                or state_row["status"] != "waiting"
                or state_row["gate"] != gate
                or state_row["actor_subject"] != actor_subject
                or state_row["thread_id"] != thread_id
            ):
                raise GateResumeRejected("gate_out_of_order")

            updated = store._conn.execute(
                "UPDATE codex_gate_capabilities SET consumed_at_ms=? "
                "WHERE token=? AND consumed_at_ms IS NULL",
                (now_ms, token),
            ).rowcount
            if updated != 1:
                raise GateResumeRejected("capability_replayed")
            write_action_count = state_row["write_action_count"] + 1
            next_index = _GATE_INDEX[gate] + 1
            status = "done" if next_index >= len(GATES) else "waiting"
            next_gate = None if status == "done" else GATES[next_index]
            store._conn.execute(
                "UPDATE codex_gate_state SET gate=?,status=?,write_action_count=?,updated_at_ms=? "
                "WHERE run_id=?",
                (next_gate or gate, status, write_action_count, now_ms, run_id),
            )
            store._conn.commit()
            return GateResumeResult(
                resume_thread_id=thread_id, resumed_gate=gate, next_gate=next_gate,
                status=status, write_action_count=write_action_count,
            )
        except Exception:
            store._conn.rollback()
            raise


__all__ = [
    "GATES",
    "GateResumeRejected",
    "GateResumeStore",
    "GateState",
    "GateResumeResult",
    "current_gate_state",
    "describe_gate_state",
    "enter_waiting_gate",
    "issue_gate_capability",
    "consume_gate_capability",
]
