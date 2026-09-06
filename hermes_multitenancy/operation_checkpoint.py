"""Small durable, actor-bound checkpoint ledger for connector operations."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_STATES = {"pending", "running", "waiting_auth", "uncertain", "confirmed", "failed", "consumed"}
DEFAULT_NO_REF_RECOVERY_MAX_AGE_SECONDS = 15 * 60
_SCHEMA = """
CREATE TABLE IF NOT EXISTS multitenancy_operation_checkpoints (
    operation_id TEXT PRIMARY KEY,
    owner_hash TEXT NOT NULL,
    connector TEXT NOT NULL,
    intent_hash TEXT NOT NULL,
    step TEXT NOT NULL,
    state TEXT NOT NULL,
    session_ref TEXT,
    call_ref TEXT,
    result_ref TEXT,
    tool_scope TEXT,
    chat_type TEXT,
    chat_fence TEXT,
    updated_at INTEGER NOT NULL,
    UNIQUE(owner_hash, connector, intent_hash)
);
"""


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _required(value: str, name: str) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > 240:
        raise ValueError(f"{name} is required and must be at most 240 characters")
    return clean


def stable_operation_id(
    *,
    profile_name: str,
    subject: str,
    connector: str,
    intent_key: str,
) -> str:
    """Return an opaque actor-bound id without persisting any input material."""
    return "op_" + _digest(
        _required(profile_name, "profile_name"),
        _required(subject, "subject"),
        _required(connector, "connector"),
        _required(intent_key, "intent_key"),
    )[:40]


class OperationCheckpointStore:
    """Stores only opaque operation metadata; never prompts, tokens, or tool payloads."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(_SCHEMA)
        columns = {
            str(row[1])
            for row in self._conn.execute(
                "PRAGMA table_info(multitenancy_operation_checkpoints)"
            )
        }
        for column in (
            "session_ref",
            "call_ref",
            "result_ref",
            "tool_scope",
            "chat_type",
            "chat_fence",
        ):
            if column not in columns:
                self._conn.execute(
                    f"ALTER TABLE multitenancy_operation_checkpoints ADD COLUMN {column} TEXT"
                )
        self._conn.commit()

    @staticmethod
    def _owner(profile_name: str, subject: str) -> str:
        return _digest(_required(profile_name, "profile_name"), _required(subject, "subject"))

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = {
            "operation_id": row["operation_id"],
            "connector": row["connector"],
            "step": row["step"],
            "state": row["state"],
            "session_ref": row["session_ref"],
            "call_ref": row["call_ref"],
            "result_ref": row["result_ref"],
        }
        if row["tool_scope"] or row["chat_type"] or row["chat_fence"]:
            result.update(
                {
                    "tool_scope": row["tool_scope"],
                    "chat_type": row["chat_type"],
                    "chat_fence": row["chat_fence"],
                }
            )
        return result

    def put(
        self,
        *,
        operation_id: str,
        profile_name: str,
        subject: str,
        connector: str,
        intent_key: str,
        step: str,
        state: str,
        session_ref: str | None = None,
        call_ref: str | None = None,
        result_ref: str | None = None,
        tool_scope: str | None = None,
        chat_type: str | None = None,
        chat_fence: str | None = None,
    ) -> None:
        operation_id = _required(operation_id, "operation_id")
        connector = _required(connector, "connector")
        step = _required(step, "step")
        state = _required(state, "state")
        if state not in _STATES:
            raise ValueError(f"unsupported checkpoint state {state!r}")
        owner_hash = self._owner(profile_name, subject)
        intent_hash = _digest(_required(intent_key, "intent_key"))
        session_ref = _required(session_ref, "session_ref") if session_ref else None
        call_ref = _required(call_ref, "call_ref") if call_ref else None
        result_ref = _required(result_ref, "result_ref") if result_ref else None
        tool_scope = _required(tool_scope, "tool_scope") if tool_scope else None
        chat_type = _required(chat_type, "chat_type") if chat_type else None
        chat_fence = _required(chat_fence, "chat_fence") if chat_fence else None
        if tool_scope or chat_type or chat_fence:
            if (
                tool_scope not in {"feishu:user", "feishu:bot"}
                or chat_type not in {"p2p", "group"}
                or (tool_scope == "feishu:bot") != (chat_type == "group")
                or not chat_fence
            ):
                raise ValueError("sealed Feishu scope is incomplete or inconsistent")
        with self._lock:
            existing = self._conn.execute(
                "SELECT owner_hash FROM multitenancy_operation_checkpoints WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is not None and existing["owner_hash"] != owner_hash:
                raise PermissionError("operation checkpoint belongs to another actor")
            self._conn.execute(
                "INSERT INTO multitenancy_operation_checkpoints"
                " (operation_id,owner_hash,connector,intent_hash,step,state,session_ref,call_ref,result_ref,tool_scope,chat_type,chat_fence,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(operation_id) DO UPDATE SET"
                " connector=excluded.connector,intent_hash=excluded.intent_hash,"
                " step=excluded.step,state=excluded.state,session_ref=excluded.session_ref,"
                " call_ref=excluded.call_ref,result_ref=excluded.result_ref,"
                " tool_scope=excluded.tool_scope,chat_type=excluded.chat_type,"
                " chat_fence=excluded.chat_fence,updated_at=excluded.updated_at",
                (
                    operation_id,
                    owner_hash,
                    connector,
                    intent_hash,
                    step,
                    state,
                    session_ref,
                    call_ref,
                    result_ref,
                    tool_scope,
                    chat_type,
                    chat_fence,
                    int(time.time()),
                ),
            )
            self._conn.commit()

    def claim(
        self,
        *,
        profile_name: str,
        subject: str,
        connector: str,
        intent_key: str,
        step: str,
        session_ref: str | None = None,
        call_ref: str | None = None,
        tool_scope: str | None = None,
        chat_type: str | None = None,
        chat_fence: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically create the running row, or return the prior exact intent."""
        operation_id = stable_operation_id(
            profile_name=profile_name,
            subject=subject,
            connector=connector,
            intent_key=intent_key,
        )
        owner_hash = self._owner(profile_name, subject)
        connector = _required(connector, "connector")
        intent_hash = _digest(_required(intent_key, "intent_key"))
        step = _required(step, "step")
        session_ref = _required(session_ref, "session_ref") if session_ref else None
        call_ref = _required(call_ref, "call_ref") if call_ref else None
        tool_scope = _required(tool_scope, "tool_scope") if tool_scope else None
        chat_type = _required(chat_type, "chat_type") if chat_type else None
        chat_fence = _required(chat_fence, "chat_fence") if chat_fence else None
        if tool_scope or chat_type or chat_fence:
            if (
                tool_scope not in {"feishu:user", "feishu:bot"}
                or chat_type not in {"p2p", "group"}
                or (tool_scope == "feishu:bot") != (chat_type == "group")
                or not chat_fence
            ):
                raise ValueError("sealed Feishu scope is incomplete or inconsistent")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO multitenancy_operation_checkpoints"
                " (operation_id,owner_hash,connector,intent_hash,step,state,session_ref,call_ref,tool_scope,chat_type,chat_fence,updated_at)"
                " VALUES (?,?,?,?,?,'running',?,?,?,?,?,?)",
                (
                    operation_id,
                    owner_hash,
                    connector,
                    intent_hash,
                    step,
                    session_ref,
                    call_ref,
                    tool_scope,
                    chat_type,
                    chat_fence,
                    int(time.time()),
                ),
            )
            row = self._conn.execute(
                "SELECT operation_id,connector,step,state,session_ref,call_ref,result_ref,tool_scope,chat_type,chat_fence"
                " FROM multitenancy_operation_checkpoints"
                " WHERE operation_id=? AND owner_hash=?",
                (operation_id, owner_hash),
            ).fetchone()
            self._conn.commit()
        if row is None:  # pragma: no cover - deterministic id + same transaction
            raise RuntimeError("operation checkpoint claim failed")
        return self._row(row), cursor.rowcount == 1

    def get(self, operation_id: str, *, profile_name: str, subject: str) -> dict[str, Any] | None:
        owner_hash = self._owner(profile_name, subject)
        with self._lock:
            row = self._conn.execute(
                "SELECT operation_id,connector,step,state,session_ref,call_ref,result_ref,tool_scope,chat_type,chat_fence"
                " FROM multitenancy_operation_checkpoints"
                " WHERE operation_id=? AND owner_hash=?",
                (_required(operation_id, "operation_id"), owner_hash),
            ).fetchone()
        return self._row(row)

    def find_pending(
        self,
        *,
        profile_name: str,
        subject: str,
        connector: str,
        session_ref: str | None = None,
        call_ref: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the newest actor-bound connector step eligible for recovery."""
        owner_hash = self._owner(profile_name, subject)
        session_ref = _required(session_ref, "session_ref") if session_ref else None
        call_ref = _required(call_ref, "call_ref") if call_ref else None
        connector = _required(connector, "connector")
        if (session_ref is None) != (call_ref is None):
            return None
        with self._lock:
            if session_ref is None and call_ref is None:
                rows = self._conn.execute(
                    "SELECT operation_id,connector,step,state,session_ref,call_ref,result_ref,tool_scope,chat_type,chat_fence"
                    " FROM multitenancy_operation_checkpoints"
                    " WHERE owner_hash=? AND connector=? AND state='waiting_auth'"
                    " AND session_ref IS NOT NULL AND call_ref IS NOT NULL"
                    " AND updated_at>=? ORDER BY updated_at DESC, operation_id DESC LIMIT 2",
                    (
                        owner_hash,
                        connector,
                        int(time.time()) - DEFAULT_NO_REF_RECOVERY_MAX_AGE_SECONDS,
                    ),
                ).fetchall()
                return self._row(rows[0]) if len(rows) == 1 else None
            row = self._conn.execute(
                "SELECT operation_id,connector,step,state,session_ref,call_ref,result_ref,tool_scope,chat_type,chat_fence"
                " FROM multitenancy_operation_checkpoints"
                " WHERE owner_hash=? AND connector=?"
                " AND state IN ('running','waiting_auth','uncertain')"
                " AND session_ref IS NOT NULL AND call_ref IS NOT NULL"
                " AND session_ref=? AND call_ref=?"
                " ORDER BY updated_at DESC, operation_id DESC LIMIT 1",
                (owner_hash, connector, session_ref, call_ref),
            ).fetchone()
        return self._row(row)

    def transition(
        self,
        operation_id: str,
        *,
        profile_name: str,
        subject: str,
        expected_state: str,
        state: str,
        step: str,
        result_ref: str | None = None,
    ) -> bool:
        expected_state = _required(expected_state, "expected_state")
        state = _required(state, "state")
        if expected_state not in _STATES or state not in _STATES:
            raise ValueError("unsupported checkpoint state")
        owner_hash = self._owner(profile_name, subject)
        result_ref = _required(result_ref, "result_ref") if result_ref else None
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE multitenancy_operation_checkpoints"
                " SET state=?,step=?,result_ref=COALESCE(?,result_ref),updated_at=?"
                " WHERE operation_id=? AND owner_hash=? AND state=?",
                (
                    state,
                    _required(step, "step"),
                    result_ref,
                    int(time.time()),
                    _required(operation_id, "operation_id"),
                    owner_hash,
                    expected_state,
                ),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._conn.close()
