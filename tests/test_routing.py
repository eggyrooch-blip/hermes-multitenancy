"""US-007 / US-03 — RoutingTable behavior and schema migration coverage."""
from __future__ import annotations

import sqlite3

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


def test_unique_sync_root_among_active(table):
    """US-04 narrowed invariant: at most one active *sync-root* user row per
    open_id. The unique index is intentionally narrowed to
    ``provenance = 'sync'`` so one owner can accumulate multiple non-sync
    agents that share an open_id — but the single login-resolution root the
    ``resolve_owner_root`` query depends on must still be unique.

    Two active ``provenance='sync'`` user rows with the same open_id MUST
    collide; a sync row plus a non-sync (auto) row with the same open_id MUST
    coexist (the behavior US-04 deliberately unlocked).
    """
    import sqlite3

    table.upsert(user_id="u_sync_1", profile_name="alice", open_id="ou_shared")
    table._conn.execute(
        "UPDATE multitenancy_routing SET provenance = 'sync' WHERE user_id = ?",
        ("u_sync_1",),
    )
    table._conn.commit()

    # A second active sync-root row on the same open_id violates the narrowed
    # unique index — this is the invariant resolve_owner_root relies on.
    table._conn.execute(
        """
        INSERT INTO multitenancy_routing
            (user_id, profile_name, open_id, active, synced_at, version,
             created_at, updated_at, kind, provenance)
        VALUES ('u_sync_2', 'bob', 'ou_shared', 1, 1, 1, 1, 1, 'user', 'auto')
        """
    )
    table._conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        table._conn.execute(
            "UPDATE multitenancy_routing SET provenance = 'sync' "
            "WHERE user_id = ?",
            ("u_sync_2",),
        )
    table._conn.rollback()

    # A non-sync (auto) user row sharing the open_id is allowed to coexist —
    # this is exactly what US-04's narrowing unlocked, and resolve_owner_root
    # must still deterministically return the sync root.
    row = table.resolve_owner_root("ou_shared")
    assert row is not None
    assert row.user_id == "u_sync_1"


def test_resolve_owner_root_finds_fresh_sync_upsert(table):
    table.upsert(
        user_id="u_sync_new",
        profile_name="alice",
        open_id="ou_sync_new",
        provenance="sync",
    )

    row = table.resolve_owner_root("ou_sync_new")
    assert row is not None
    assert row.user_id == "u_sync_new"
    assert row.profile_name == "alice"


def test_upsert_defaults_to_auto_and_stays_out_of_owner_root(table):
    table.upsert(
        user_id="ou_auto_new",
        profile_name="feishu_ou_auto_new",
        open_id="ou_auto_new",
    )

    cur = table._conn.execute(
        "SELECT provenance FROM multitenancy_routing WHERE user_id = ?",
        ("ou_auto_new",),
    )
    row = cur.fetchone()
    assert row is not None
    assert row["provenance"] == "auto"
    assert table.resolve_owner_root("ou_auto_new") is None


def test_upsert_conflict_updates_provenance_to_sync(table):
    table.upsert(
        user_id="u_sync_flip",
        profile_name="alice",
        open_id="ou_sync_flip",
    )

    table.upsert(
        user_id="u_sync_flip",
        profile_name="alice",
        open_id="ou_sync_flip",
        provenance="sync",
    )

    cur = table._conn.execute(
        "SELECT provenance FROM multitenancy_routing WHERE user_id = ?",
        ("u_sync_flip",),
    )
    row = cur.fetchone()
    assert row is not None
    assert row["provenance"] == "sync"

    root = table.resolve_owner_root("ou_sync_flip")
    assert root is not None
    assert root.user_id == "u_sync_flip"


def test_inactive_row_does_not_block_open_id_reuse(table):
    """After soft-delete, the open_id can be re-used by another user."""
    table.upsert(user_id="u_1", profile_name="alice", open_id="ou_x")
    table.soft_delete("u_1")
    # u_2 can now claim ou_x
    table.upsert(user_id="u_2", profile_name="bob", open_id="ou_x")
    row = table.lookup_by_open_id("ou_x")
    assert row.user_id == "u_2"


def test_lookup_agent_returns_row_by_agent_id(table):
    table.upsert(user_id="u_1", profile_name="alice", open_id="ou_1")
    table._conn.execute(
        """
        UPDATE multitenancy_routing
        SET agent_id = ?, owner_open_id = ?, provenance = ?
        WHERE user_id = ?
        """,
        ("agent-sync-1", "ou_1", "sync", "u_1"),
    )
    table._conn.commit()

    row = table.lookup_agent("agent-sync-1")
    assert row is not None
    assert row.user_id == "u_1"
    assert row.open_id == "ou_1"

    assert table.lookup_agent("agent-missing") is None

    table.soft_delete("u_1")
    assert table.lookup_agent("agent-sync-1") is None


def test_list_agents_for_owner_cross_owner_isolation(table):
    table.upsert(user_id="u_sync_a", profile_name="pa", open_id="ou_a")
    table.upsert_group(
        chat_id="oc_a",
        profile_name="ga",
        owner_open_id="ou_a",
        display_label="A",
    )
    table.upsert(user_id="u_sync_b", profile_name="pb", open_id="ou_b")
    table.upsert_group(
        chat_id="oc_b",
        profile_name="gb",
        owner_open_id="ou_b",
        display_label="B",
    )
    table._conn.execute(
        """
        UPDATE multitenancy_routing
        SET owner_open_id = CASE user_id
                WHEN 'u_sync_a' THEN 'ou_a'
                WHEN 'u_sync_b' THEN 'ou_b'
                ELSE owner_open_id
            END,
            provenance = CASE user_id
                WHEN 'u_sync_a' THEN 'sync'
                WHEN 'u_sync_b' THEN 'sync'
                WHEN 'group:oc_a' THEN 'group'
                WHEN 'group:oc_b' THEN 'group'
                ELSE provenance
            END,
            agent_id = user_id
        WHERE user_id IN ('u_sync_a', 'u_sync_b', 'group:oc_a', 'group:oc_b')
        """
    )
    table._conn.commit()

    owner_a_rows = table.list_agents_for_owner("ou_a")
    assert {(row.user_id, row.owner_open_id) for row in owner_a_rows} == {
        ("u_sync_a", "ou_a"),
        ("group:oc_a", "ou_a"),
    }
    assert all(row.owner_open_id == "ou_a" for row in table.list_by_owner("ou_a"))

    owner_b_rows = table.list_agents_for_owner("ou_b")
    assert {(row.user_id, row.owner_open_id) for row in owner_b_rows} == {
        ("u_sync_b", "ou_b"),
        ("group:oc_b", "ou_b"),
    }
    assert all(row.owner_open_id == "ou_b" for row in table.list_by_owner("ou_b"))


def test_list_by_owner_ordering_sync_root_first(table):
    table.upsert_group(
        chat_id="oc_early",
        profile_name="g_early",
        owner_open_id="ou_order",
        display_label="early",
    )
    table.upsert(user_id="u_sync_order", profile_name="p_order", open_id="ou_order")
    table._conn.execute(
        """
        UPDATE multitenancy_routing
        SET owner_open_id = CASE user_id
                WHEN 'u_sync_order' THEN 'ou_order'
                ELSE owner_open_id
            END,
            provenance = CASE user_id
                WHEN 'u_sync_order' THEN 'sync'
                WHEN 'group:oc_early' THEN 'group'
                ELSE provenance
            END,
            agent_id = user_id,
            created_at = CASE user_id
                WHEN 'group:oc_early' THEN 100
                WHEN 'u_sync_order' THEN 200
                ELSE created_at
            END
        WHERE user_id IN ('u_sync_order', 'group:oc_early')
        """
    )
    table._conn.commit()

    rows = table.list_by_owner("ou_order", kind=None)
    assert [row.user_id for row in rows] == ["u_sync_order", "group:oc_early"]


def test_list_by_owner_kind_filter_backcompat(table):
    from hermes_multitenancy.routing import KIND_GROUP

    table.upsert(user_id="u_sync_kind", profile_name="p_kind", open_id="ou_kind")
    table.upsert_group(
        chat_id="oc_kind",
        profile_name="g_kind",
        owner_open_id="ou_kind",
    )
    table._conn.execute(
        """
        UPDATE multitenancy_routing
        SET owner_open_id = CASE user_id
                WHEN 'u_sync_kind' THEN 'ou_kind'
                ELSE owner_open_id
            END,
            provenance = CASE user_id
                WHEN 'u_sync_kind' THEN 'sync'
                WHEN 'group:oc_kind' THEN 'group'
                ELSE provenance
            END,
            agent_id = user_id
        WHERE user_id IN ('u_sync_kind', 'group:oc_kind')
        """
    )
    table._conn.commit()

    group_rows = table.list_by_owner("ou_kind", kind=KIND_GROUP)
    assert [row.user_id for row in group_rows] == ["group:oc_kind"]

    all_rows = table.list_by_owner("ou_kind", kind=None)
    assert {row.user_id for row in all_rows} == {"u_sync_kind", "group:oc_kind"}


def test_resolve_owner_root_deterministic_with_narrowed_index(tmp_path):
    from hermes_multitenancy.routing import RoutingTable

    db_path = tmp_path / "us04-resolve-owner-root.db"
    _create_legacy_db(db_path)
    _insert_legacy_row(
        db_path,
        user_id="u_sync_root",
        profile_name="sync-profile",
        open_id="ou_owner",
        synced_at=101,
        created_at=101,
        updated_at=201,
        kind="user",
    )
    _insert_legacy_row(
        db_path,
        user_id="ou_owner",
        profile_name="auto-profile",
        open_id="ou_owner",
        synced_at=102,
        created_at=102,
        updated_at=202,
        kind="user",
    )

    table = RoutingTable(db_path)
    try:
        rows = list(
            table._conn.execute(
                """
                SELECT user_id, open_id, active, provenance
                FROM multitenancy_routing
                WHERE open_id = ?
                ORDER BY user_id
                """,
                ("ou_owner",),
            ).fetchall()
        )
        assert [(row["user_id"], row["provenance"], row["active"]) for row in rows] == [
            ("ou_owner", "auto", 1),
            ("u_sync_root", "sync", 1),
        ]

        root = table.resolve_owner_root("ou_owner")
        assert root is not None
        assert root.user_id == "u_sync_root"
        assert root.open_id == "ou_owner"
    finally:
        table.close()


def test_migration_narrows_open_id_index_idempotent(tmp_path):
    from hermes_multitenancy.routing import RoutingTable

    db_path = tmp_path / "us04-index-migrate.db"
    _create_legacy_db(db_path)
    _insert_legacy_row(
        db_path,
        user_id="u_sync",
        profile_name="psync",
        open_id="ou_sync",
        synced_at=101,
        created_at=101,
        updated_at=201,
        kind="user",
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE UNIQUE INDEX idx_routing_open_id_active_user "
        "ON multitenancy_routing(open_id) "
        "WHERE active = 1 AND kind = 'user' AND open_id IS NOT NULL"
    )
    conn.commit()
    conn.close()

    table = RoutingTable(db_path)
    table.close()

    first_snapshot = _snapshot_rows(db_path)
    first_index_sql = _index_sql(db_path, "idx_routing_open_id_active_user")
    assert first_index_sql is not None
    assert "provenance = 'sync'" in first_index_sql

    table = RoutingTable(db_path)
    table.close()

    second_snapshot = _snapshot_rows(db_path)
    second_index_sql = _index_sql(db_path, "idx_routing_open_id_active_user")
    assert second_snapshot == first_snapshot
    assert second_index_sql == first_index_sql


# -- US-03 schema delta + backfill -------------------------------------------

_LEGACY_SCHEMA = """
CREATE TABLE multitenancy_routing (
    user_id        TEXT PRIMARY KEY NOT NULL,
    profile_name   TEXT NOT NULL,
    open_id        TEXT,
    union_id       TEXT,
    active         INTEGER NOT NULL DEFAULT 1,
    deleted_at     INTEGER,
    synced_at      INTEGER NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    last_active_at INTEGER,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'user',
    chat_id        TEXT,
    owner_open_id  TEXT,
    display_label  TEXT
);
"""


def _create_legacy_db(path, *, with_legacy_open_id_index: bool = False) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    if with_legacy_open_id_index:
        conn.execute(
            "CREATE UNIQUE INDEX idx_routing_open_id_active "
            "ON multitenancy_routing(open_id) "
            "WHERE active = 1 AND open_id IS NOT NULL"
        )
    conn.commit()
    conn.close()


def _insert_legacy_row(
    path,
    *,
    user_id: str,
    profile_name: str,
    open_id: str | None,
    union_id: str | None = None,
    active: int = 1,
    deleted_at: int | None = None,
    synced_at: int = 1,
    version: int = 1,
    last_active_at: int | None = None,
    created_at: int = 1,
    updated_at: int = 1,
    kind: str = "user",
    chat_id: str | None = None,
    owner_open_id: str | None = None,
    display_label: str | None = None,
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        INSERT INTO multitenancy_routing
            (user_id, profile_name, open_id, union_id, active, deleted_at,
             synced_at, version, last_active_at, created_at, updated_at,
             kind, chat_id, owner_open_id, display_label)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            profile_name,
            open_id,
            union_id,
            active,
            deleted_at,
            synced_at,
            version,
            last_active_at,
            created_at,
            updated_at,
            kind,
            chat_id,
            owner_open_id,
            display_label,
        ),
    )
    conn.commit()
    conn.close()


def _snapshot_rows(path) -> list[tuple]:
    conn = sqlite3.connect(path)
    rows = list(
        conn.execute(
            "SELECT * FROM multitenancy_routing ORDER BY user_id"
        ).fetchall()
    )
    conn.close()
    return rows


def _index_names(path) -> set[str]:
    conn = sqlite3.connect(path)
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'multitenancy_routing'"
        ).fetchall()
    }
    conn.close()
    return names


def _index_sql(path, name: str) -> str | None:
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (name,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _column_names(path) -> list[str]:
    conn = sqlite3.connect(path)
    columns = [
        row[1]
        for row in conn.execute("PRAGMA table_info(multitenancy_routing)").fetchall()
    ]
    conn.close()
    return columns


def test_us03_migrate_adds_columns_and_indexes(tmp_path):
    from hermes_multitenancy.routing import RoutingTable

    db_path = tmp_path / "us03-legacy.db"
    _create_legacy_db(db_path, with_legacy_open_id_index=True)
    _insert_legacy_row(
        db_path,
        user_id="u_legacy",
        profile_name="legacy-profile",
        open_id="ou_legacy",
        union_id="on_legacy",
        synced_at=111,
        created_at=111,
        updated_at=222,
    )

    table = RoutingTable(db_path)
    table.close()

    columns = _column_names(db_path)
    assert "agent_id" in columns
    assert "upstream_profile" in columns
    assert "provenance" in columns

    indexes = _index_names(db_path)
    assert "idx_routing_agent_id_active" in indexes
    assert "idx_routing_upstream" in indexes

    rows = _snapshot_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == "u_legacy"
    assert rows[0][1] == "legacy-profile"
    assert rows[0][2] == "ou_legacy"
    assert rows[0][3] == "on_legacy"


def test_us03_migrate_twice_is_strict_noop(tmp_path):
    from hermes_multitenancy.routing import RoutingTable

    db_path = tmp_path / "us03-noop.db"
    _create_legacy_db(db_path)
    _insert_legacy_row(
        db_path,
        user_id="u_sync",
        profile_name="psync",
        open_id="ou_sync",
        synced_at=101,
        created_at=101,
        updated_at=202,
    )
    _insert_legacy_row(
        db_path,
        user_id="group:oc_room",
        profile_name="pgroup",
        open_id="",
        synced_at=101,
        created_at=101,
        updated_at=202,
        kind="group",
        chat_id="oc_room",
        owner_open_id="ou_sync",
    )

    table = RoutingTable(db_path)
    table.close()
    first_snapshot = _snapshot_rows(db_path)

    table = RoutingTable(db_path)
    table.close()
    second_snapshot = _snapshot_rows(db_path)

    assert second_snapshot == first_snapshot


def test_us03_backfill_provenance_and_owner(tmp_path):
    from hermes_multitenancy.routing import RoutingTable

    db_path = tmp_path / "us03-backfill.db"
    _create_legacy_db(db_path)
    _insert_legacy_row(
        db_path,
        user_id="u_sync",
        profile_name="psync",
        open_id="ou_sync",
        synced_at=100,
        created_at=100,
        updated_at=200,
        kind="user",
    )
    _insert_legacy_row(
        db_path,
        user_id="ou_auto",
        profile_name="feishu_ou_auto",
        open_id="ou_auto",
        synced_at=101,
        created_at=101,
        updated_at=201,
        kind="user",
    )
    _insert_legacy_row(
        db_path,
        user_id="group:oc_x",
        profile_name="feishu_group_x",
        open_id="",
        synced_at=102,
        created_at=102,
        updated_at=202,
        kind="group",
        chat_id="oc_x",
        owner_open_id="ou_sync",
        display_label="IT Group",
    )

    table = RoutingTable(db_path)
    table.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = list(
        conn.execute(
            "SELECT user_id, agent_id, owner_open_id, provenance, upstream_profile "
            "FROM multitenancy_routing WHERE active = 1 ORDER BY user_id"
        ).fetchall()
    )
    conn.close()

    assert all(row["owner_open_id"] is not None for row in rows)
    assert all(row["agent_id"] == row["user_id"] for row in rows)
    assert len({row["agent_id"] for row in rows}) == len(rows)

    by_user_id = {row["user_id"]: row for row in rows}
    assert by_user_id["u_sync"]["provenance"] == "sync"
    assert by_user_id["u_sync"]["owner_open_id"] == "ou_sync"
    assert by_user_id["ou_auto"]["provenance"] == "auto"
    assert by_user_id["ou_auto"]["owner_open_id"] == "ou_auto"
    assert by_user_id["group:oc_x"]["provenance"] == "group"
    assert by_user_id["group:oc_x"]["owner_open_id"] == "ou_sync"
    assert by_user_id["group:oc_x"]["upstream_profile"] == "psync"


def test_us03_scale_idempotency_1300_rows(tmp_path):
    from hermes_multitenancy.routing import RoutingTable

    db_path = tmp_path / "us03-scale.db"
    _create_legacy_db(db_path)
    for i in range(500):
        _insert_legacy_row(
            db_path,
            user_id=f"u_sync_{i:04d}",
            profile_name=f"psync_{i:04d}",
            open_id=f"ou_sync_{i:04d}",
            synced_at=1000 + i,
            created_at=1000 + i,
            updated_at=2000 + i,
        )
    for i in range(500):
        _insert_legacy_row(
            db_path,
            user_id=f"ou_auto_{i:04d}",
            profile_name=f"feishu_ou_auto_{i:04d}",
            open_id=f"ou_auto_{i:04d}",
            synced_at=3000 + i,
            created_at=3000 + i,
            updated_at=4000 + i,
        )
    for i in range(300):
        _insert_legacy_row(
            db_path,
            user_id=f"group:oc_{i:04d}",
            profile_name=f"feishu_group_{i:04d}",
            open_id="",
            synced_at=5000 + i,
            created_at=5000 + i,
            updated_at=6000 + i,
            kind="group",
            chat_id=f"oc_{i:04d}",
            owner_open_id=f"ou_sync_{i:04d}",
            display_label=f"Group {i:04d}",
        )

    table = RoutingTable(db_path)
    table.close()
    first_snapshot = _snapshot_rows(db_path)

    table = RoutingTable(db_path)
    table.close()
    second_snapshot = _snapshot_rows(db_path)

    assert second_snapshot == first_snapshot
