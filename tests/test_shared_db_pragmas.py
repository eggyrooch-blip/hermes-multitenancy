"""Every store on ~/.hermes/multitenancy.db opens it the same way.

2026-09-17: the production host's disk is rotational and SQLite's default
synchronous=FULL fsyncs on every commit (measured on that disk: median
4.88ms/commit vs 0.03ms at NORMAL). WAL makes writers global, so that
per-commit cost became seconds of write-lock wait for whoever was last in the
queue — 11.95s sampled in production, which dropped Feishu turns. These tests
pin the three pragmas so a new store cannot quietly reintroduce the old
behaviour on the shared file.
"""

import sqlite3

import pytest

from hermes_multitenancy.shared_db import (
    DEFAULT_BUSY_TIMEOUT_MS,
    apply_shared_pragmas,
    connect_shared,
)


def _pragmas(conn: sqlite3.Connection) -> tuple[int, str, int]:
    return (
        conn.execute("PRAGMA busy_timeout").fetchone()[0],
        str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
        conn.execute("PRAGMA synchronous").fetchone()[0],
    )


def _store_factories():
    from hermes_multitenancy.agent_relay_store import RelayStore
    from hermes_multitenancy.billing_identity import BillingIdentityStore
    from hermes_multitenancy.push_env_map import PushEnvMapStore
    from hermes_multitenancy.push_registry import PushRegistryStore
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.sessions import SessionStore
    from hermes_multitenancy.skillhub_events import SkillhubEventStore

    return [
        ("routing", RoutingTable),
        ("sessions", SessionStore),
        ("billing_identity", BillingIdentityStore),
        ("push_registry", PushRegistryStore),
        ("push_env_map", PushEnvMapStore),
        ("skillhub_events", SkillhubEventStore),
        ("agent_relay_store", lambda path: RelayStore(path, "test-encryption-key")),
    ]


@pytest.mark.parametrize("name,factory", _store_factories(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_shared_store_opens_with_the_same_pragmas(name, factory, tmp_path):
    store = factory(tmp_path / "multitenancy.db")
    timeout, journal, sync = _pragmas(store._conn)
    assert timeout >= DEFAULT_BUSY_TIMEOUT_MS, f"{name} waits only {timeout}ms for the write lock"
    assert journal == "wal", f"{name} is not in WAL mode"
    assert sync == 1, f"{name} still fsyncs on every commit (synchronous={sync})"


def test_connector_client_auth_store_matches(tmp_path):
    """Separate: this module needs the optional `mcp` dependency to import."""
    pytest.importorskip("mcp")
    from hermes_multitenancy.connector_client_auth import ClientTokenStore

    store = ClientTokenStore(
        tmp_path / "multitenancy.db",
        issuer="http://127.0.0.1:8767",
        resource="http://127.0.0.1:8767/mcp",
    )
    assert _pragmas(store._conn) == (DEFAULT_BUSY_TIMEOUT_MS, "wal", 1)


def test_expert_usage_connection_matches(tmp_path):
    from hermes_multitenancy.expert_usage import _connect

    conn = _connect(tmp_path / "multitenancy.db")
    timeout, journal, sync = _pragmas(conn)
    assert (timeout, journal, sync) == (DEFAULT_BUSY_TIMEOUT_MS, "wal", 1)


def test_busy_timeout_is_set_before_wal(tmp_path):
    """Switching a shared file to WAL needs a brief exclusive lock.

    With the default timeout of 0, that pragma errors out the moment another
    connection holds the file — so the wait has to be in place first.
    """
    order: list[str] = []
    real = sqlite3.Connection.execute

    class Recorder(sqlite3.Connection):
        def execute(self, sql, *a, **kw):  # type: ignore[override]
            if "busy_timeout" in sql or "journal_mode" in sql or "synchronous" in sql:
                order.append(sql)
            return real(self, sql, *a, **kw)

    conn = sqlite3.connect(tmp_path / "x.db", factory=Recorder)
    apply_shared_pragmas(conn)
    assert len(order) == 3
    assert "busy_timeout" in order[0]
    assert "journal_mode" in order[1]
    assert "synchronous" in order[2]


def test_busy_timeout_cannot_be_dialed_below_the_floor(tmp_path):
    conn = connect_shared(str(tmp_path / "x.db"), busy_timeout_ms=1)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == DEFAULT_BUSY_TIMEOUT_MS


def test_memory_database_still_works_through_the_helper():
    """routing.py opens :memory: through the same path; WAL is a no-op there."""
    conn = connect_shared(":memory:")
    conn.execute("CREATE TABLE t(a)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    assert conn.execute("SELECT a FROM t").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == DEFAULT_BUSY_TIMEOUT_MS


def test_expert_usage_never_returns_a_connection_that_skipped_the_pragmas(tmp_path, monkeypatch):
    """codex r1 #p1.

    The old `_connect` caught OperationalError around the WAL switch and
    carried on, so a connection could come back still at synchronous=FULL —
    an fsync-per-commit writer back on the shared file, which is the whole
    thing this change removes. It must close and raise instead.
    """
    import hermes_multitenancy.expert_usage as eu

    monkeypatch.setattr(eu, "connect_shared", lambda *a, **kw: (_ for _ in ()).throw(
        sqlite3.OperationalError("database is locked")
    ))
    with pytest.raises(sqlite3.OperationalError):
        eu._connect(tmp_path / "multitenancy.db")

    # …and the callers still degrade rather than propagate to a user turn.
    assert eu.bump("expert-a", db_path=tmp_path / "multitenancy.db") is False
    assert eu.counts(db_path=tmp_path / "multitenancy.db") == {}
