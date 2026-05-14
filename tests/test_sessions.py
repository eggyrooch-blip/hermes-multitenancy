"""US-017 — SessionStore append/load/clear/count."""
from __future__ import annotations

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
