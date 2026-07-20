"""Persistent ``push_registry`` — the durable state of every push-card fill loop.

Owns one hot table (``push_registry``) plus a cold archive table in the shared
``~/.hermes/multitenancy.db``. A row is created by ``POST /notify-card`` in
``sending`` and walks the state machine (design §2.2)::

    sending → pending → clarifying → confirmed → committed
        │                      └────────────────────────► failed  (write exhausted)
        └─► failed (send retries exhausted)
    any not-finished state ──(past expires_at)──► expired

Invariants enforced here:

* ``idempotency_key`` UNIQUE — a repeat POST returns the original row, never a
  second card (design §2.1).
* ``business_key`` unique **among not-finished rows** (partial index) — a second
  in-flight push for the same ``scene:open_id:day`` collides → the endpoint
  returns 409 + the original registry_id.
* The matcher's "未完成" set is exactly ``{pending, clarifying}`` and is filtered
  by ``expires_at`` at read time so a dead expiry worker cannot cause a
  mis-route (design §2.2 P1-1/§2.3).

This module is P1 of SPEC push-card-fill-loop. The confirm-time CAS
(``advance_status``) is provided here but exercised by P5.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = Path.home() / ".hermes" / "multitenancy.db"

STATUS_SENDING = "sending"
STATUS_PENDING = "pending"
STATUS_CLARIFYING = "clarifying"
STATUS_CONFIRMED = "confirmed"
STATUS_COMMITTED = "committed"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"

#: The matcher's "未完成" (open) set — only these route an unquoted reply.
OPEN_STATUSES = (STATUS_PENDING, STATUS_CLARIFYING)
#: In-flight for business-key collision purposes (not yet a terminal outcome).
INFLIGHT_STATUSES = (STATUS_SENDING, STATUS_PENDING, STATUS_CLARIFYING, STATUS_CONFIRMED)
#: Expiry-eligible states — deliberately EXCLUDES ``confirmed``: a user who
#: confirmed has met the deadline, and a confirmed-but-not-committed row (write
#: still in flight / crash before commit) is the reconcile worker's job, never
#: the expiry sweep's. Reaping it would silently discard a confirmed submission
#: and drop it out of the reconcile net (finding expiry-reaps-confirmed).
EXPIRABLE_STATUSES = (STATUS_SENDING, STATUS_PENDING, STATUS_CLARIFYING)
#: Terminal states — archivable to the cold table.
FINISHED_STATUSES = (STATUS_COMMITTED, STATUS_FAILED, STATUS_EXPIRED)

_INFLIGHT_SQL_LIST = ", ".join(f"'{s}'" for s in INFLIGHT_STATUSES)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS push_registry (
    registry_id     TEXT PRIMARY KEY,
    scene           TEXT NOT NULL,
    skill           TEXT NOT NULL,
    target_open_id  TEXT NOT NULL,
    target_union_id TEXT,
    profile_name    TEXT NOT NULL,
    chat_id         TEXT,
    message_id      TEXT,
    payload_json    TEXT,
    card_json       TEXT,
    submission_json TEXT,
    callback_json   TEXT,
    behaviors_json  TEXT,
    status          TEXT NOT NULL,
    business_key    TEXT NOT NULL,
    idempotency_key TEXT,
    nonce           TEXT,
    send_attempts   INTEGER NOT NULL DEFAULT 0,
    write_attempts  INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    expires_at      INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
-- Repeat POST with the same idempotency_key collapses to the original row.
CREATE UNIQUE INDEX IF NOT EXISTS idx_push_registry_idem
    ON push_registry(idempotency_key) WHERE idempotency_key IS NOT NULL;
-- At most ONE not-finished row per business_key; a committed/expired/failed row
-- releases the key so a later same-key push is allowed.
CREATE UNIQUE INDEX IF NOT EXISTS idx_push_registry_bizkey_inflight
    ON push_registry(business_key) WHERE status IN ({_INFLIGHT_SQL_LIST});
-- Matcher hot path + expiry sweep.
CREATE INDEX IF NOT EXISTS idx_push_registry_target
    ON push_registry(target_open_id, status, expires_at);

-- Cold archive: committed/expired/failed rows drain here so the hot table stays
-- small (design §2.2). Same columns; no indexes needed (audit/lookup only).
CREATE TABLE IF NOT EXISTS push_registry_archive (
    registry_id     TEXT PRIMARY KEY,
    scene           TEXT NOT NULL,
    skill           TEXT NOT NULL,
    target_open_id  TEXT NOT NULL,
    target_union_id TEXT,
    profile_name    TEXT NOT NULL,
    chat_id         TEXT,
    message_id      TEXT,
    payload_json    TEXT,
    card_json       TEXT,
    submission_json TEXT,
    callback_json   TEXT,
    behaviors_json  TEXT,
    status          TEXT NOT NULL,
    business_key    TEXT NOT NULL,
    idempotency_key TEXT,
    nonce           TEXT,
    send_attempts   INTEGER NOT NULL DEFAULT 0,
    write_attempts  INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    expires_at      INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    archived_at     INTEGER NOT NULL
);
"""

_COLUMNS = (
    "registry_id", "scene", "skill", "target_open_id", "target_union_id",
    "profile_name", "chat_id", "message_id", "payload_json", "card_json",
    "submission_json", "callback_json", "behaviors_json", "status",
    "business_key", "idempotency_key", "nonce",
    "send_attempts", "write_attempts", "last_error", "expires_at",
    "created_at", "updated_at",
)


@dataclass
class CreateResult:
    """Outcome of ``create``. ``conflict`` is one of None|'idempotent'|'business_key'."""

    row: dict[str, Any]
    created: bool
    conflict: Optional[str] = None


def _now() -> int:
    return int(time.time())


class PushRegistryStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = str(db_path) if db_path is not None else str(DEFAULT_DB_PATH)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript("PRAGMA journal_mode=WAL;")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Additive column migration for an older DB (callback/behaviors are
        per-push data added after the first ship). Probe pragma_table_info —
        SQLite has no ``ADD COLUMN IF NOT EXISTS`` — and ALTER what's missing.
        Safe to call repeatedly."""
        for table in ("push_registry", "push_registry_archive"):
            cur = self._conn.execute(f"PRAGMA table_info({table})")
            existing = {row["name"] for row in cur.fetchall()}
            for col in ("callback_json", "behaviors_json"):
                if col not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")

    # --- create -----------------------------------------------------------

    def create(
        self,
        *,
        scene: str,
        skill: str,
        target_open_id: str,
        profile_name: str,
        business_key: str,
        payload: Any = None,
        card: Any = None,
        idempotency_key: Optional[str] = None,
        target_union_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        expires_at: Optional[int] = None,
        nonce: Optional[str] = None,
        callback_json: Optional[str] = None,
        behaviors_json: Optional[str] = None,
    ) -> CreateResult:
        """Insert a new ``sending`` row, or return the colliding one.

        Idempotency and business-key collisions are resolved under the store
        lock (single writer) BEFORE the insert, so the unique indexes are a
        backstop, not the primary path — no reliance on parsing SQLite error
        messages to tell the two constraints apart.
        """
        now = _now()
        registry_id = "pcr_" + secrets.token_hex(12)
        nonce = nonce or secrets.token_hex(16)
        payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
        card_json = json.dumps(card, ensure_ascii=False) if card is not None else None
        with self._lock:
            if idempotency_key:
                existing = self._conn.execute(
                    "SELECT * FROM push_registry WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return CreateResult(dict(existing), created=False, conflict="idempotent")
            inflight = self._conn.execute(
                "SELECT * FROM push_registry WHERE business_key = ?"
                f" AND status IN ({_INFLIGHT_SQL_LIST})",
                (business_key,),
            ).fetchone()
            if inflight is not None:
                return CreateResult(dict(inflight), created=False, conflict="business_key")
            self._conn.execute(
                "INSERT INTO push_registry"
                " (registry_id, scene, skill, target_open_id, target_union_id,"
                "  profile_name, chat_id, message_id, payload_json, card_json,"
                "  submission_json, callback_json, behaviors_json, status,"
                "  business_key, idempotency_key, nonce,"
                "  send_attempts, write_attempts, last_error, expires_at,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?, ?, ?, ?, ?, ?,"
                "         0, 0, NULL, ?, ?, ?)",
                (
                    registry_id, scene, skill, target_open_id, target_union_id,
                    profile_name, chat_id, payload_json, card_json,
                    callback_json, behaviors_json,
                    STATUS_SENDING, business_key, idempotency_key, nonce,
                    expires_at, now, now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM push_registry WHERE registry_id = ?", (registry_id,)
            ).fetchone()
        return CreateResult(dict(row), created=True, conflict=None)

    # --- reads ------------------------------------------------------------

    def get(self, registry_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM push_registry WHERE registry_id = ?", (registry_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def get_by_idempotency_key(self, key: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM push_registry WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return dict(row) if row is not None else None

    def get_inflight_by_business_key(self, business_key: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM push_registry WHERE business_key = ?"
            f" AND status IN ({_INFLIGHT_SQL_LIST})",
            (business_key,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_open_for_user(
        self, target_open_id: str, *, now: Optional[int] = None
    ) -> list[dict[str, Any]]:
        """The matcher's candidate set: pending/clarifying and not yet expired.

        ``expires_at`` is evaluated here (not just trusted via ``status``) so a
        stalled expiry worker never lets an over-due card capture a reply.
        """
        now = _now() if now is None else now
        cur = self._conn.execute(
            "SELECT * FROM push_registry"
            f" WHERE target_open_id = ? AND status IN ({', '.join('?' * len(OPEN_STATUSES))})"
            " AND (expires_at IS NULL OR expires_at > ?)"
            " ORDER BY created_at ASC",
            (target_open_id, *OPEN_STATUSES, now),
        )
        return [dict(r) for r in cur.fetchall()]

    def count_pushes_today(self, target_open_id: str, *, day_start: int) -> int:
        """Rows *actually delivered* to the user since ``day_start`` — the
        per-user daily-cap denominator.

        Only cards that reached the user (status past ``sending``:
        pending/clarifying/confirmed/committed) count. A ``failed`` send never
        reached them; ``expired`` likewise; and — critically — a still-``sending``
        row has NOT been delivered yet, so it must not count against the cap.
        Counting ``sending`` rows self-starves the queue: a user whose cards are
        all parked in ``sending`` by a quiet-hours / cap deferral would count
        themselves over the cap and never send any of them (finding
        daily-cap-self-starvation). The re-drive sweep re-picks the parked rows;
        this counter just stops them from blocking themselves."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM push_registry"
            " WHERE target_open_id = ? AND created_at >= ?"
            " AND status NOT IN (?, ?, ?)",
            (target_open_id, day_start, STATUS_SENDING, STATUS_FAILED, STATUS_EXPIRED),
        ).fetchone()
        return int(row[0])

    def get_by_message_id(self, message_id: str) -> Optional[dict[str, Any]]:
        """Exact ``parent_id/root_id`` hit lookup for the matcher (design §2.3-1).

        Includes ``expired`` rows on purpose — a quoted reply to an expired card
        must reach the explicit "已过期" exit, not fall through to passthrough."""
        mid = str(message_id or "")
        if not mid:
            return None
        row = self._conn.execute(
            "SELECT * FROM push_registry WHERE message_id = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (mid,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_fresh_sending(
        self, target_open_id: str, *, since: int
    ) -> Optional[dict[str, Any]]:
        """A row still in ``sending`` with no ``message_id`` yet, created since
        ``since``. Feeds the matcher's grace retry for the "员工秒回早于
        message_id 回填" window (design §2.2 P1-2)."""
        row = self._conn.execute(
            "SELECT * FROM push_registry"
            " WHERE target_open_id = ? AND status = ?"
            " AND message_id IS NULL AND created_at >= ?"
            " ORDER BY created_at DESC LIMIT 1",
            (target_open_id, STATUS_SENDING, since),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_due_open(self, *, now: Optional[int] = None) -> list[dict[str, Any]]:
        """Expirable rows (sending/pending/clarifying, NOT confirmed) past
        ``expires_at`` — the expiry worker's work list (design §2.6). It
        re-renders each card before flipping to ``expired``. ``confirmed`` is
        excluded on purpose (finding expiry-reaps-confirmed)."""
        now = _now() if now is None else now
        expirable = ", ".join("?" * len(EXPIRABLE_STATUSES))
        cur = self._conn.execute(
            "SELECT * FROM push_registry"
            f" WHERE status IN ({expirable})"
            " AND expires_at IS NOT NULL AND expires_at <= ?"
            " ORDER BY expires_at ASC",
            (*EXPIRABLE_STATUSES, now),
        )
        return [dict(r) for r in cur.fetchall()]

    def list_stale_confirmed(
        self, *, older_than: int
    ) -> list[dict[str, Any]]:
        """``confirmed`` rows whose write has not reached ``committed`` within
        the reconcile window. The reconcile worker checks the backend by
        idempotency key and backfills — it never blindly re-writes (design
        §2.5 P0-2 step 3)."""
        cur = self._conn.execute(
            "SELECT * FROM push_registry"
            " WHERE status = ? AND updated_at <= ?"
            " ORDER BY updated_at ASC",
            (STATUS_CONFIRMED, older_than),
        )
        return [dict(r) for r in cur.fetchall()]

    def list_sending_stale(self, *, older_than: int) -> list[dict[str, Any]]:
        """``sending`` rows last touched at/before ``older_than`` — the send
        re-drive worker's work list. A quiet-hours / daily-cap deferral parks a
        row in ``sending`` with no live re-invocation of the queue; this sweep
        re-runs ``process()`` so the deferral actually resumes (finding
        deferred-never-resumes). ``updated_at`` gates out a just-created row that
        the fire-and-forget dispatch is still handling, avoiding a double send."""
        cur = self._conn.execute(
            "SELECT * FROM push_registry"
            " WHERE status = ? AND updated_at <= ?"
            " ORDER BY updated_at ASC",
            (STATUS_SENDING, older_than),
        )
        return [dict(r) for r in cur.fetchall()]

    def reset_submission(self, registry_id: str) -> bool:
        """Clear the extracted ``submission_json`` so the fill skill re-extracts
        from the surviving messages after a recall (design §2.3 P1-4 — replay,
        not pure-accumulate). Status is untouched."""
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE push_registry SET submission_json = NULL, updated_at = ?"
                f" WHERE registry_id = ? AND status IN ({', '.join('?' * len(OPEN_STATUSES))})",
                (now, registry_id, *OPEN_STATUSES),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM push_registry").fetchone()[0])

    # --- state transitions ------------------------------------------------

    def mark_sent(
        self, registry_id: str, *, message_id: str, chat_id: Optional[str] = None
    ) -> bool:
        """CAS ``sending → pending`` and record the delivered message_id."""
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE push_registry"
                " SET status = ?, message_id = ?,"
                "     chat_id = COALESCE(?, chat_id),"
                "     send_attempts = send_attempts + 1, updated_at = ?"
                " WHERE registry_id = ? AND status = ?",
                (STATUS_PENDING, message_id, chat_id, now, registry_id, STATUS_SENDING),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def mark_send_failed(self, registry_id: str, *, error: str) -> bool:
        """CAS ``sending → failed`` after send retries are exhausted (visible,
        re-pushable — never silent, design §2.2 P1-1)."""
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE push_registry"
                " SET status = ?, last_error = ?,"
                "     send_attempts = send_attempts + 1, updated_at = ?"
                " WHERE registry_id = ? AND status = ?",
                (STATUS_FAILED, str(error)[:2000], now, registry_id, STATUS_SENDING),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def note_send_attempt(self, registry_id: str, *, error: Optional[str] = None) -> None:
        """Increment the send-attempt counter without leaving ``sending`` (a
        transient failure that will be retried)."""
        now = _now()
        with self._lock:
            self._conn.execute(
                "UPDATE push_registry"
                " SET send_attempts = send_attempts + 1,"
                "     last_error = COALESCE(?, last_error), updated_at = ?"
                " WHERE registry_id = ?",
                (str(error)[:2000] if error else None, now, registry_id),
            )
            self._conn.commit()

    def advance_status(
        self,
        registry_id: str,
        *,
        expect: str,
        to: str,
        expect_nonce: Optional[str] = None,
        clear_nonce: bool = False,
        submission: Any = None,
        message_id: Optional[str] = None,
        last_error: Optional[str] = None,
        bump_write_attempts: bool = False,
    ) -> bool:
        """Generic optimistic CAS used by later phases (clarify/confirm/commit).

        Returns True iff a row in state ``expect`` (and matching ``expect_nonce``
        when given) was moved to ``to``. This is the atomic confirm-time guard
        the write path stands on (design §2.5 P0-2). ``bump_write_attempts``
        increments the accepted-submit counter (the ``max_submits`` denominator)."""
        now = _now()
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [to, now]
        if submission is not None:
            sets.append("submission_json = ?")
            params.append(json.dumps(submission, ensure_ascii=False))
        if message_id is not None:
            sets.append("message_id = ?")
            params.append(message_id)
        if last_error is not None:
            sets.append("last_error = ?")
            params.append(str(last_error)[:2000])
        if clear_nonce:
            sets.append("nonce = NULL")
        if bump_write_attempts:
            sets.append("write_attempts = write_attempts + 1")
        where = "registry_id = ? AND status = ?"
        params.extend([registry_id, expect])
        if expect_nonce is not None:
            where += " AND nonce = ?"
            params.append(expect_nonce)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE push_registry SET {', '.join(sets)} WHERE {where}", params
            )
            self._conn.commit()
            return cur.rowcount > 0

    def expire_due(self, *, now: Optional[int] = None) -> int:
        """Sweep expirable rows (sending/pending/clarifying, NOT confirmed) past
        ``expires_at`` into ``expired``.

        The full worker (with card re-render) is P6; this is the DB primitive it
        and the read-time guard share. ``confirmed`` is excluded so a confirmed
        submission is never silently reaped (finding expiry-reaps-confirmed)."""
        now = _now() if now is None else now
        open_list = ", ".join("?" * len(EXPIRABLE_STATUSES))
        with self._lock:
            cur = self._conn.execute(
                "UPDATE push_registry SET status = ?, updated_at = ?"
                f" WHERE status IN ({open_list})"
                " AND expires_at IS NOT NULL AND expires_at <= ?",
                (STATUS_EXPIRED, now, *EXPIRABLE_STATUSES, now),
            )
            self._conn.commit()
            return cur.rowcount

    def archive_finished(self, *, older_than: int) -> int:
        """Move terminal rows with ``updated_at <= older_than`` to the cold table."""
        finished_list = ", ".join("?" * len(FINISHED_STATUSES))
        now = _now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM push_registry"
                f" WHERE status IN ({finished_list}) AND updated_at <= ?",
                (*FINISHED_STATUSES, older_than),
            ).fetchall()
            if not rows:
                return 0
            self._conn.executemany(
                "INSERT OR REPLACE INTO push_registry_archive"
                f" ({', '.join(_COLUMNS)}, archived_at)"
                f" VALUES ({', '.join('?' * len(_COLUMNS))}, ?)",
                [tuple(r[c] for c in _COLUMNS) + (now,) for r in rows],
            )
            self._conn.executemany(
                "DELETE FROM push_registry WHERE registry_id = ?",
                [(r["registry_id"],) for r in rows],
            )
            self._conn.commit()
            return len(rows)

    def close(self) -> None:
        self._conn.close()


# --- module-level singleton (mirrors skillhub_events.get/override_event_store) --

_store: Optional[PushRegistryStore] = None
_store_db_path: Optional[str] = None


def get_registry_store() -> PushRegistryStore:
    global _store
    if _store is None:
        _store = PushRegistryStore(_store_db_path)
    return _store


def override_registry_store(store_or_path: Any) -> None:
    """Test/seam hook: set the singleton to a store or a path (e.g. ``:memory:``)."""
    global _store, _store_db_path
    if _store is not None and _store is not store_or_path:
        try:
            _store.close()
        except Exception:
            pass
    if store_or_path is None or isinstance(store_or_path, (str, Path)):
        _store_db_path = str(store_or_path) if store_or_path is not None else None
        _store = None
    else:
        _store = store_or_path
