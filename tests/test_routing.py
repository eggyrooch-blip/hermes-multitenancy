"""US-007 — RoutingTable insert / lookup / soft-delete / resurrect."""
from __future__ import annotations

import pytest


@pytest.fixture
def table():
    from hermes_multitenancy.routing import RoutingTable
    t = RoutingTable(":memory:")
    yield t
    t.close()


def test_upsert_and_lookup(table):
    table.upsert(user_id="u_1", profile_name="alice", open_id="ou_1", union_id="on_1")
    row = table.lookup_by_open_id("ou_1")
    assert row is not None
    assert row.user_id == "u_1"
    assert row.profile_name == "alice"
    assert row.open_id == "ou_1"
    assert row.union_id == "on_1"
    assert row.active is True
    assert row.version == 1
    assert row.synced_at > 0


def test_lookup_miss_returns_none(table):
    assert table.lookup_by_open_id("ou_unknown") is None


def test_upsert_idempotent_bumps_version(table):
    table.upsert(user_id="u_1", profile_name="alice", open_id="ou_1")
    table.upsert(user_id="u_1", profile_name="alice2", open_id="ou_1")
    row = table.lookup_by_open_id("ou_1")
    assert row.profile_name == "alice2"
    assert row.version == 2


def test_soft_delete_hides_from_lookup(table):
    table.upsert(user_id="u_1", profile_name="alice", open_id="ou_1")
    assert table.soft_delete("u_1") is True
    assert table.lookup_by_open_id("ou_1") is None
    assert table.count_active() == 0


def test_soft_delete_then_resurrect(table):
    """Re-upsert after soft delete must un-flag active."""
    table.upsert(user_id="u_1", profile_name="alice", open_id="ou_1")
    table.soft_delete("u_1")
    table.upsert(user_id="u_1", profile_name="alice", open_id="ou_1")
    row = table.lookup_by_open_id("ou_1")
    assert row is not None
    assert row.active is True
    assert row.version == 3  # initial(1) + delete(2) + resurrect(3)


def test_soft_delete_missing_user_returns_false(table):
    assert table.soft_delete("u_nonexistent") is False


def test_touch_active_updates_last_active_at(table):
    import time
    table.upsert(user_id="u_1", profile_name="alice", open_id="ou_1")
    row1 = table.lookup_by_open_id("ou_1")
    assert row1.last_active_at is None
    time.sleep(1.01)  # ensure timestamp changes (second-resolution)
    table.touch_active("ou_1")
    row2 = table.lookup_by_open_id("ou_1")
    assert row2.last_active_at is not None
    assert row2.last_active_at >= row1.synced_at


def test_touch_active_does_not_bump_version(table):
    table.upsert(user_id="u_1", profile_name="alice", open_id="ou_1")
    table.touch_active("ou_1")
    row = table.lookup_by_open_id("ou_1")
    assert row.version == 1, "router-side touch must not bump sync version"


def test_unique_open_id_among_active(table):
    """Two distinct active rows cannot share the same open_id."""
    import sqlite3
    table.upsert(user_id="u_1", profile_name="alice", open_id="ou_shared")
    with pytest.raises(sqlite3.IntegrityError):
        table.upsert(user_id="u_2", profile_name="bob", open_id="ou_shared")


def test_inactive_row_does_not_block_open_id_reuse(table):
    """After soft-delete, the open_id can be re-used by another user."""
    table.upsert(user_id="u_1", profile_name="alice", open_id="ou_x")
    table.soft_delete("u_1")
    # u_2 can now claim ou_x
    table.upsert(user_id="u_2", profile_name="bob", open_id="ou_x")
    row = table.lookup_by_open_id("ou_x")
    assert row.user_id == "u_2"
