"""One way to open ``~/.hermes/multitenancy.db``.

Every store in this package writes to the SAME SQLite file, and WAL makes
writers global: the gateway, the Web UI, each cron subprocess and the
credential-renewal sweep over ~1600 routes all queue behind one write lock.
Before this module each store picked its own pragmas, so the file's behaviour
depended on which store happened to be talking.

The production host's disk is rotational (``lsblk`` ROTA=1) and SQLite's
default ``synchronous=FULL`` fsyncs on every commit. Measured on that disk,
2026-09-17:

    synchronous=FULL     median  4.88 ms/commit, max 175.09 ms
    synchronous=NORMAL   median  0.03 ms/commit, max   0.08 ms

~160x. With one shared queue, a burst of commits turns that per-commit cost
into seconds of write-lock wait for whoever is last in line — 11.95 s was
sampled in production, which is what dropped Feishu turns with
"请求状态暂时无法保存，请稍后重试。".

``synchronous=NORMAL`` is SQLite's documented setting for WAL: it cannot
corrupt the database, and the exposure is losing the most recent committed
transactions on power loss or kernel panic (an application crash loses
nothing). This file holds conversation history, routing and credential
*caches* — all re-derivable — so that trade is the right one here.
"""

from __future__ import annotations

import sqlite3

# Aligned with the longest timeout any store used before this module.
DEFAULT_BUSY_TIMEOUT_MS = 30_000


def apply_shared_pragmas(conn: sqlite3.Connection, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
    """Apply the shared-database pragmas to an open connection.

    Order matters. ``busy_timeout`` goes first because switching a file to WAL
    needs a brief exclusive lock, and with the default timeout of 0 that pragma
    errors out the moment another connection holds the file.

    ``journal_mode`` is a no-op on ``:memory:`` (it reports ``memory``), which
    is why the return value is not asserted — tests and fixtures open memory
    databases through this same helper on purpose.
    """
    # A floor, not a suggestion: the point of this module is that no writer on
    # the shared file gives up early, so a caller cannot dial it below the
    # agreed wait.
    conn.execute(f"PRAGMA busy_timeout={max(int(busy_timeout_ms), DEFAULT_BUSY_TIMEOUT_MS)}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")


def connect_shared(
    db_path: str,
    *,
    check_same_thread: bool = False,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open a connection to the shared database with the shared pragmas set."""
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    apply_shared_pragmas(conn, busy_timeout_ms=busy_timeout_ms)
    return conn
