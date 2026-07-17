"""US-017 — SessionStore append/load/clear/count."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture
def store():
    from hermes_multitenancy.sessions import SessionStore
    s = SessionStore(":memory:")
    yield s
    s.close()


def test_append_and_load(store):
    store.append("alice", "u_1", "user", "hi")
    store.append("alice", "u_1", "assistant", "hello!")
    rows = store.load_recent("alice", "u_1", 10)
    assert rows == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello!"},
    ]


def test_load_returns_oldest_first_with_limit(store):
    for i in range(5):
        store.append("p", "u", "user", f"m{i}")
    last3 = store.load_recent("p", "u", 3)
    assert [r["content"] for r in last3] == ["m2", "m3", "m4"]


def test_clear_removes_user_history(store):
    store.append("p", "u_1", "user", "x")
    store.append("p", "u_1", "user", "y")
    store.append("p", "u_2", "user", "z")  # different user
    removed = store.clear("p", "u_1")
    assert removed == 2
    assert store.load_recent("p", "u_1", 10) == []
    # u_2 untouched
    assert len(store.load_recent("p", "u_2", 10)) == 1


def test_isolated_per_profile_user(store):
    store.append("p1", "u", "user", "in p1")
    store.append("p2", "u", "user", "in p2")
    assert store.load_recent("p1", "u", 10) == [{"role": "user", "content": "in p1"}]
    assert store.load_recent("p2", "u", 10) == [{"role": "user", "content": "in p2"}]


def test_count_diagnostic(store):
    assert store.count("p", "u") == 0
    store.append("p", "u", "user", "1")
    store.append("p", "u", "user", "2")
    assert store.count("p", "u") == 2


def test_mark_event_processed_rejects_duplicate_within_ttl(store):
    assert store.mark_event_processed(
        "msg:p:u:om_1",
        profile_name="p",
        user_key="u",
        message_id="om_1",
        content_hash=None,
        ttl_seconds=3600,
    ) is True

    assert store.mark_event_processed(
        "msg:p:u:om_1",
        profile_name="p",
        user_key="u",
        message_id="om_1",
        content_hash=None,
        ttl_seconds=3600,
    ) is False


def test_is_event_processed_is_read_only_and_uses_mark_ttl(store, monkeypatch):
    import hermes_multitenancy.sessions as sessions

    monkeypatch.setattr(sessions.time, "time", lambda: 1_000)
    assert store.is_event_processed("event-1", 60) is False
    assert store._conn.execute(
        "SELECT COUNT(*) FROM multitenancy_processed_events"
    ).fetchone()[0] == 0

    assert store.mark_event_processed(
        "event-1",
        profile_name="p",
        user_key="u",
        message_id="om_1",
        content_hash=None,
        ttl_seconds=60,
    ) is True
    assert store.is_event_processed("event-1", 60) is True

    monkeypatch.setattr(sessions.time, "time", lambda: 1_060)
    assert store.is_event_processed("event-1", 60) is True
    monkeypatch.setattr(sessions.time, "time", lambda: 1_061)
    assert store.is_event_processed("event-1", 60) is False
    assert store._conn.execute(
        "SELECT COUNT(*) FROM multitenancy_processed_events"
    ).fetchone()[0] == 1


@pytest.mark.parametrize("existing_ts", [None, 1])
def test_mark_event_processed_is_atomic_across_connections(
    tmp_path, monkeypatch, existing_ts
):
    """Only one connection may claim a new or expired event key."""
    import sqlite3
    import hermes_multitenancy.sessions as sessions

    db_path = tmp_path / "sessions.db"
    stores = [sessions.SessionStore(db_path), sessions.SessionStore(db_path)]
    if existing_ts is not None:
        stores[0]._conn.execute(
            "INSERT INTO multitenancy_processed_events"
            " (event_key, profile_name, user_key, ts) VALUES (?, ?, ?, ?)",
            ("event-1", "old-profile", "old-user", existing_ts),
        )
        stores[0]._conn.commit()

    monkeypatch.setattr(sessions.time, "time", lambda: 1_000)
    insert_barrier = threading.Barrier(2)

    def authorize(action, table, _column, _database, _trigger):
        if action == sqlite3.SQLITE_INSERT and table == "multitenancy_processed_events":
            insert_barrier.wait(timeout=5)
        return sqlite3.SQLITE_OK

    for candidate in stores:
        candidate._conn.set_authorizer(authorize)

    def mark(candidate):
        return candidate.mark_event_processed(
            "event-1",
            profile_name="p",
            user_key="u",
            message_id="om_1",
            content_hash=None,
            ttl_seconds=60,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(mark, stores))
        assert sorted(results) == [False, True]
        row = stores[0]._conn.execute(
            "SELECT profile_name, user_key, message_id, ts"
            " FROM multitenancy_processed_events WHERE event_key = ?",
            ("event-1",),
        ).fetchone()
        assert tuple(row) == ("p", "u", "om_1", 1_000)
    finally:
        for candidate in stores:
            candidate.close()


def test_mark_event_processed_is_thread_safe_on_shared_connection(store):
    started = threading.Barrier(8)

    def mark(_worker):
        started.wait(timeout=5)
        return store.mark_event_processed(
            "event-1",
            profile_name="p",
            user_key="u",
            message_id="om_1",
            content_hash=None,
            ttl_seconds=60,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(mark, range(8)))
    assert results.count(True) == 1
    assert results.count(False) == 7
