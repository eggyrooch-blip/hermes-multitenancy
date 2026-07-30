"""Expert usage counter — one bump per expert run, all channels.

sunke 2026-07-30 口径: 一次"使用" = 一次带专家身份的 run(轮次); webui/飞书/cron
全渠道汇总, 不排除任何渠道。Bump 只发生在父进程(子进程沙箱不能写共享库),
整段 best-effort: 任何失败只 debug、绝不影响回复。

Garbage-row note: bump() only whitelists the id shape; ids that never resolve to
a catalog expert may still create rows, but they are invisible — the read side
merges counts onto catalog rows only (webui_broker_server.handle_experts).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".hermes" / "multitenancy.db"

# Same shape run_broker's expert-id whitelist accepts (alnum plus -_.:).
_EXPERT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS expert_usage (
    expert_id  TEXT PRIMARY KEY,
    use_count  INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript("PRAGMA journal_mode=WAL;")
    conn.executescript(_SCHEMA)
    return conn


def bump(expert_id: str, db_path: Path | None = None) -> bool:
    """Best-effort +1 for one run; True only when the row was written.

    Single atomic UPSERT — two gateway processes (multitenancy_router +
    expert_krd) share this DB, so no SELECT-then-UPDATE.
    """
    eid = str(expert_id or "").strip()
    if not _EXPERT_ID_RE.match(eid):
        return False
    try:
        conn = _connect(db_path or DEFAULT_DB_PATH)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO expert_usage(expert_id, use_count, updated_at)"
                    " VALUES(?, 1, ?)"
                    " ON CONFLICT(expert_id) DO UPDATE SET"
                    " use_count = use_count + 1, updated_at = excluded.updated_at",
                    (eid, int(time.time())),
                )
        finally:
            conn.close()
        return True
    except Exception:
        logger.debug("[multitenancy] expert_usage bump failed for %s", eid, exc_info=True)
        return False


def counts(db_path: Path | None = None) -> dict[str, int]:
    """expert_id → use_count; empty dict on any failure (best-effort read)."""
    try:
        conn = _connect(db_path or DEFAULT_DB_PATH)
        try:
            rows = conn.execute("SELECT expert_id, use_count FROM expert_usage").fetchall()
        finally:
            conn.close()
        out: dict[str, int] = {}
        for r in rows:
            # one dirty row (NULL / non-int) must not blank the whole map
            try:
                eid = str(r[0] or "").strip()
                if eid:
                    out[eid] = int(r[1] or 0)
            except Exception:
                continue
        return out
    except Exception:
        logger.debug("[multitenancy] expert_usage counts read failed", exc_info=True)
        return {}
