"""``push_env_map`` — message_id → {env, key_owner, ts} for the /push bypass.

One tiny table in the shared ``~/.hermes/multitenancy.db``. When ``POST
/api/run-broker/push`` sends a message it records which credential ``env``
(pre|online) the caller asked for, keyed by the Feishu ``message_id`` it got
back. The consumer is a LATER slug (expert-cred-preflight): a topic reply to a
pushed card looks up its parent ``message_id`` here to inject the right
``KEP_ENV`` deterministically. v1 only records + reads back.

ponytail: one table, record + get. Archival/pruning can wait until this table
actually grows — a message_id row is ~100 bytes.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = Path.home() / ".hermes" / "multitenancy.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS push_env_map (
    message_id  TEXT PRIMARY KEY,
    env         TEXT NOT NULL,
    key_owner   TEXT,
    ts          INTEGER NOT NULL
);
"""


class PushEnvMapStore:
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
            self._conn.commit()

    def record(self, *, message_id: str, env: str, key_owner: str = "") -> None:
        """Upsert the env used to send ``message_id``. Last write wins (a
        message_id is unique per Feishu send, so a collision is a replay)."""
        mid = str(message_id or "").strip()
        if not mid:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO push_env_map (message_id, env, key_owner, ts)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(message_id) DO UPDATE SET"
                "   env = excluded.env, key_owner = excluded.key_owner, ts = excluded.ts",
                (mid, str(env), str(key_owner or ""), int(time.time())),
            )
            self._conn.commit()

    def get(self, message_id: str) -> Optional[dict[str, Any]]:
        mid = str(message_id or "").strip()
        if not mid:
            return None
        row = self._conn.execute(
            "SELECT message_id, env, key_owner, ts FROM push_env_map WHERE message_id = ?",
            (mid,),
        ).fetchone()
        return dict(row) if row is not None else None

    def close(self) -> None:
        self._conn.close()


# --- module-level singleton (mirrors push_registry.get/override_registry_store) --

_store: Optional[PushEnvMapStore] = None
_store_db_path: Optional[str] = None


def get_env_map_store() -> PushEnvMapStore:
    global _store
    if _store is None:
        _store = PushEnvMapStore(_store_db_path)
    return _store


def override_env_map_store(store_or_path: Any) -> None:
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
