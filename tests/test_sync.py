"""US-019 — feishu-sync apply_users idempotent reconciler."""
from __future__ import annotations

import json
import pytest


@pytest.fixture
def table():
    from hermes_multitenancy.routing import RoutingTable
    t = RoutingTable(":memory:")
    yield t
    t.close()


def test_apply_inserts_new_users(table):
    from hermes_multitenancy.sync import UserSpec, apply_users
    stats = apply_users(table, [
        UserSpec(user_id="u1", profile_name="alice", open_id="ou_1", union_id="on_1"),
        UserSpec(user_id="u2", profile_name="bob", open_id="ou_2", union_id="on_2"),
    ])
    assert stats == {"upserted": 2, "soft_deleted": 0, "kept": 0}
    assert table.count_active() == 2


def test_apply_idempotent_on_same_list(table):
    from hermes_multitenancy.sync import UserSpec, apply_users
    users = [UserSpec(user_id="u1", profile_name="alice", open_id="ou_1")]
    apply_users(table, users)
    stats = apply_users(table, users)
    assert stats == {"upserted": 0, "soft_deleted": 0, "kept": 1}, "second apply should be no-op"


def test_apply_soft_deletes_missing(table):
    from hermes_multitenancy.sync import UserSpec, apply_users
    apply_users(table, [
        UserSpec(user_id="u1", profile_name="alice", open_id="ou_1"),
        UserSpec(user_id="u2", profile_name="bob", open_id="ou_2"),
    ])
    # second apply leaves u1 only
    stats = apply_users(table, [
        UserSpec(user_id="u1", profile_name="alice", open_id="ou_1"),
    ])
    assert stats == {"upserted": 0, "soft_deleted": 1, "kept": 1}
    assert table.count_active() == 1


def test_apply_upserts_when_profile_changes(table):
    from hermes_multitenancy.sync import UserSpec, apply_users
    apply_users(table, [UserSpec(user_id="u1", profile_name="alice", open_id="ou_1")])
    stats = apply_users(table, [UserSpec(user_id="u1", profile_name="alice2", open_id="ou_1")])
    assert stats["upserted"] == 1
    row = table.lookup_by_open_id("ou_1")
    assert row.profile_name == "alice2"
    assert row.version == 2


def test_cli_apply_end_to_end(tmp_path):
    """The CLI `apply` subcommand reads a JSON file and applies it to a DB."""
    from hermes_multitenancy.sync.cli import main

    users_json = tmp_path / "users.json"
    users_json.write_text(json.dumps([
        {"user_id": "u1", "profile_name": "alice", "open_id": "ou_1", "union_id": "on_1"},
        {"user_id": "u2", "profile_name": "bob",   "open_id": "ou_2"},
    ]))
    db = tmp_path / "routing.db"

    rc = main(["apply", str(users_json), "--db", str(db)])
    assert rc == 0
    # DB now has 2 active rows
    from hermes_multitenancy.routing import RoutingTable
    t = RoutingTable(str(db))
    try:
        assert t.count_active() == 2
    finally:
        t.close()
