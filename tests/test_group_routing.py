"""Group-chat routing rows — lookup_by_chat_id / upsert_group / soft-delete."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def table():
    from hermes_multitenancy.routing import RoutingTable
    t = RoutingTable(":memory:")
    yield t
    t.close()


def test_upsert_group_creates_row(table):
    syn_uid = table.upsert_group(
        chat_id="oc_abc",
        profile_name="feishu_group_oc_abc",
        owner_open_id="ou_inviter",
        display_label="owner-IT组",
    )
    assert syn_uid == "group:oc_abc"
    row = table.lookup_by_chat_id("oc_abc")
    assert row is not None
    assert row.user_id == "group:oc_abc"
    assert row.profile_name == "feishu_group_oc_abc"
    assert row.kind == "group"
    assert row.chat_id == "oc_abc"
    assert row.owner_open_id == "ou_inviter"
    assert row.display_label == "owner-IT组"
    assert row.active is True
    assert row.version == 1


def test_lookup_by_chat_id_miss_returns_none(table):
    assert table.lookup_by_chat_id("oc_unknown") is None


def test_user_lookup_does_not_see_group_row(table):
    """A group row's stored open_id sentinel must not be returned by user lookup."""
    table.upsert_group(
        chat_id="oc_abc",
        profile_name="feishu_group_oc_abc",
        owner_open_id="ou_inviter",
    )
    # Group rows store open_id='' as a NOT NULL sentinel — must not surface
    # via the user lookup path.
    assert table.lookup_by_open_id("") is None
    assert table.lookup_by_open_id("oc_abc") is None


def test_group_lookup_does_not_see_user_row(table):
    table.upsert(user_id="owner", profile_name="kite", open_id="ou_owner")
    assert table.lookup_by_chat_id("ou_owner") is None
    assert table.lookup_by_chat_id("owner") is None


def test_two_groups_same_owner_get_distinct_rows(table):
    """One inviter pulling bot into two different chats → two routes."""
    table.upsert_group(
        chat_id="oc_one",
        profile_name="feishu_group_oc_one",
        owner_open_id="ou_inviter",
        display_label="owner-IT组",
    )
    table.upsert_group(
        chat_id="oc_two",
        profile_name="feishu_group_oc_two",
        owner_open_id="ou_inviter",
        display_label="owner-财务组",
    )
    assert table.count_active(kind="group") == 2
    assert table.lookup_by_chat_id("oc_one").display_label == "owner-IT组"
    assert table.lookup_by_chat_id("oc_two").display_label == "owner-财务组"


def test_chat_id_uniqueness_among_active_groups(table):
    """A single chat_id can have only one active group row at a time."""
    table.upsert_group(
        chat_id="oc_dup",
        profile_name="profile_a",
        owner_open_id="ou_inviter",
    )
    # upsert_group is upsert semantics: re-call with same chat_id just bumps
    # version and rebinds profile_name, not raises.
    table.upsert_group(
        chat_id="oc_dup",
        profile_name="profile_b",
        owner_open_id="ou_inviter_other",  # ignored on resurrect — owner immutable
    )
    row = table.lookup_by_chat_id("oc_dup")
    assert row.profile_name == "profile_b"
    assert row.owner_open_id == "ou_inviter", "owner_open_id is immutable"
    assert row.version == 2
    assert table.count_active(kind="group") == 1


def test_soft_delete_group_hides_from_lookup(table):
    table.upsert_group(
        chat_id="oc_x",
        profile_name="feishu_group_x",
        owner_open_id="ou_inviter",
    )
    assert table.soft_delete_group("oc_x") is True
    assert table.lookup_by_chat_id("oc_x") is None
    assert table.count_active(kind="group") == 0


def test_soft_deleted_group_can_be_resurrected(table):
    """bot 被踢后重新被拉回 → 复用历史，owner_open_id 不变."""
    table.upsert_group(
        chat_id="oc_x",
        profile_name="feishu_group_x",
        owner_open_id="ou_original_inviter",
    )
    table.soft_delete_group("oc_x")
    # Resurrect with a different inviter — owner must stay the original.
    table.upsert_group(
        chat_id="oc_x",
        profile_name="feishu_group_x",
        owner_open_id="ou_new_attempt",
    )
    row = table.lookup_by_chat_id("oc_x")
    assert row is not None
    assert row.active is True
    assert row.owner_open_id == "ou_original_inviter"
    assert row.version == 3  # create(1) + delete(2) + resurrect(3)


def test_update_display_label(table):
    """Group rename refreshes display_label without bumping version."""
    table.upsert_group(
        chat_id="oc_x",
        profile_name="feishu_group_x",
        owner_open_id="ou_inviter",
        display_label="owner-IT组",
    )
    row_before = table.lookup_by_chat_id("oc_x")
    assert table.update_display_label("oc_x", "owner-技术中台") is True
    row_after = table.lookup_by_chat_id("oc_x")
    assert row_after.display_label == "owner-技术中台"
    assert row_after.version == row_before.version, "display label change must not bump version"


def test_update_display_label_missing_returns_false(table):
    assert table.update_display_label("oc_missing", "anything") is False


def test_list_by_owner_returns_only_owned_groups(table):
    """Web UI ACL — owner只看到自己 owner 的群."""
    table.upsert_group(
        chat_id="oc_a",
        profile_name="g_a",
        owner_open_id="ou_owner",
        display_label="owner-A",
    )
    table.upsert_group(
        chat_id="oc_b",
        profile_name="g_b",
        owner_open_id="ou_owner",
        display_label="owner-B",
    )
    table.upsert_group(
        chat_id="oc_c",
        profile_name="g_c",
        owner_open_id="ou_alice",
        display_label="alice-C",
    )
    owner_groups = table.list_by_owner("ou_owner")
    assert {r.chat_id for r in owner_groups} == {"oc_a", "oc_b"}
    alice_groups = table.list_by_owner("ou_alice")
    assert {r.chat_id for r in alice_groups} == {"oc_c"}


def test_list_by_owner_skips_soft_deleted(table):
    table.upsert_group(
        chat_id="oc_a",
        profile_name="g_a",
        owner_open_id="ou_owner",
    )
    table.upsert_group(
        chat_id="oc_b",
        profile_name="g_b",
        owner_open_id="ou_owner",
    )
    table.soft_delete_group("oc_a")
    assert {r.chat_id for r in table.list_by_owner("ou_owner")} == {"oc_b"}


def test_touch_active_group_updates_timestamp(table):
    import time
    table.upsert_group(
        chat_id="oc_x",
        profile_name="g_x",
        owner_open_id="ou_inviter",
    )
    row1 = table.lookup_by_chat_id("oc_x")
    assert row1.last_active_at is None
    time.sleep(1.01)
    table.touch_active_group("oc_x")
    row2 = table.lookup_by_chat_id("oc_x")
    assert row2.last_active_at is not None
    assert row2.last_active_at >= row1.synced_at
    assert row2.version == row1.version, "touch must not bump version"


def test_upsert_group_rejects_empty_chat_id(table):
    with pytest.raises(ValueError):
        table.upsert_group(chat_id="", profile_name="x", owner_open_id="ou_x")


def test_upsert_group_rejects_empty_owner(table):
    with pytest.raises(ValueError):
        table.upsert_group(chat_id="oc_x", profile_name="x", owner_open_id="")


def test_count_active_filters_by_kind(table):
    table.upsert(user_id="u_1", profile_name="alice", open_id="ou_1")
    table.upsert(user_id="u_2", profile_name="bob", open_id="ou_2")
    table.upsert_group(
        chat_id="oc_x",
        profile_name="g_x",
        owner_open_id="ou_inviter",
    )
    assert table.count_active() == 3
    assert table.count_active(kind="user") == 2
    assert table.count_active(kind="group") == 1


def test_user_route_with_chat_id_value_does_not_pollute_group_lookup(table):
    """Defensive: even if a stray user row carries an open_id resembling chat_id,
    lookup_by_chat_id must not return it (kind filter is enforced)."""
    table.upsert(user_id="weird", profile_name="x", open_id="oc_looks_like_chat")
    assert table.lookup_by_chat_id("oc_looks_like_chat") is None


# -- Migration from legacy schema -------------------------------------------

def test_migration_from_legacy_schema_preserves_user_rows(tmp_path):
    """Existing prod DBs were created with open_id NOT NULL and no kind/chat_id
    columns. Loading them through RoutingTable() must add new columns and
    preserve all old rows as kind='user'."""
    from hermes_multitenancy.routing import RoutingTable

    db = tmp_path / "legacy.db"
    legacy_conn = sqlite3.connect(str(db))
    legacy_conn.executescript(
        """
        CREATE TABLE multitenancy_routing (
            user_id        TEXT PRIMARY KEY NOT NULL,
            profile_name   TEXT NOT NULL,
            open_id        TEXT NOT NULL,
            union_id       TEXT,
            active         INTEGER NOT NULL DEFAULT 1,
            deleted_at     INTEGER,
            synced_at      INTEGER NOT NULL,
            version        INTEGER NOT NULL DEFAULT 1,
            last_active_at INTEGER,
            created_at     INTEGER NOT NULL,
            updated_at     INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX idx_routing_open_id_active
            ON multitenancy_routing(open_id) WHERE active = 1;
        INSERT INTO multitenancy_routing
            (user_id, profile_name, open_id, active, synced_at, version,
             created_at, updated_at)
        VALUES
            ('owner', 'kite', 'ou_owner', 1, 1700000000, 1, 1700000000, 1700000000),
            ('alice', 'feishu_alice', 'ou_alice', 1, 1700000000, 1, 1700000000, 1700000000);
        """
    )
    legacy_conn.commit()
    legacy_conn.close()

    t = RoutingTable(db)
    try:
        owner = t.lookup_by_open_id("ou_owner")
        alice = t.lookup_by_open_id("ou_alice")
        assert owner is not None and owner.kind == "user"
        assert alice is not None and alice.kind == "user"
        # Now group routing works on the migrated DB too.
        t.upsert_group(
            chat_id="oc_after_migration",
            profile_name="g_x",
            owner_open_id="ou_owner",
        )
        assert t.lookup_by_chat_id("oc_after_migration") is not None
    finally:
        t.close()


def test_migration_drops_legacy_open_id_index(tmp_path):
    """The legacy unique index didn't include a kind filter, so it would block
    group rows that store ''. Migration must drop it."""
    from hermes_multitenancy.routing import RoutingTable

    db = tmp_path / "legacy.db"
    legacy_conn = sqlite3.connect(str(db))
    legacy_conn.executescript(
        """
        CREATE TABLE multitenancy_routing (
            user_id TEXT PRIMARY KEY NOT NULL,
            profile_name TEXT NOT NULL,
            open_id TEXT NOT NULL,
            union_id TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            deleted_at INTEGER,
            synced_at INTEGER NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            last_active_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX idx_routing_open_id_active
            ON multitenancy_routing(open_id) WHERE active = 1;
        """
    )
    legacy_conn.commit()
    legacy_conn.close()

    t = RoutingTable(db)
    try:
        # Now multiple group rows with empty open_id sentinel must coexist.
        t.upsert_group(chat_id="oc_a", profile_name="g_a", owner_open_id="ou_x")
        t.upsert_group(chat_id="oc_b", profile_name="g_b", owner_open_id="ou_x")
        assert t.count_active(kind="group") == 2
    finally:
        t.close()
