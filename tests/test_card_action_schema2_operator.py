"""Schema 2 operator-identity fallback for the group reply-mode card action.

Bug (vault [[计划 — Hermes-multitenancy 参考 openclaw-lark 提升清单(可验证) 2026-06-13]] G1):
the card-action owner check read ONLY ``operator.open_id``. Under Feishu card
callback Schema 2 the operator may carry only ``union_id`` / ``user_id`` and no
``open_id`` — so the legitimate group owner was wrongly denied
("无权操作：只有把我拉进群的人能改").

Fix contract (sunke 2026-06-13, robust + auto-backfill):
  - extract the operator's full id set {open_id, union_id, user_id}
  - resolve the owner's full id set (owner_open_id -> synced user row gives
    union_id / user_id), match if ANY operator id equals ANY owner id
  - capture the inviter's union_id at bot_added so a not-yet-synced owner in the
    pending table can still match a Schema 2 operator
  - never relax: a non-owner is still denied; fail-closed when no owner is known

These tests are RED against the open_id-only implementation.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
def routing():
    from hermes_multitenancy import router as router_mod

    router_mod.override_routing_table(":memory:")
    table = router_mod._get_routing_table()
    assert table is not None
    return table


def _resp_kind(r):
    if isinstance(r, dict):
        return r["kind"]
    return "toast" if getattr(r, "toast", None) is not None else "card"


def _resp_toast_content(r):
    return r["toast"]["content"] if isinstance(r, dict) else r.toast["content"]


class _CardAdapter:
    def _on_card_action_trigger(self, data):
        return "ORIGINAL_CALLED"


def _card_data(*, operator, mode="all", chat_id="oc_group"):
    """Build a card.action.trigger payload with an arbitrary operator object.

    ``operator`` is a SimpleNamespace carrying any subset of
    open_id / union_id / user_id (Schema 2 omits open_id).
    """
    return SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value={
                    "hermes_action": "group_reply_mode",
                    "mode": mode,
                    "chat_id": chat_id,
                }
            ),
            operator=operator,
            context=SimpleNamespace(open_chat_id=chat_id),
        )
    )


def _seed_owner_user_row(table, *, open_id, union_id, user_id):
    """A synced sync-root user row for the owner, carrying all three ids."""
    table.upsert(
        user_id=user_id,
        profile_name="owner_profile",
        open_id=open_id,
        union_id=union_id,
        provenance="sync",
    )


# --------------------------------------------------------------------------- #
# Schema 2: operator has NO open_id — must resolve through the owner's id set
# --------------------------------------------------------------------------- #

def test_schema2_union_id_only_operator_matches_owner_via_synced_row(routing):
    """Operator carries only union_id (Schema 2). Owner is provisioned by
    open_id; their synced user row supplies the union_id to match on."""
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger

    _patch_on_card_action_trigger(_CardAdapter)
    _seed_owner_user_row(routing, open_id="ou_owner", union_id="on_owner", user_id="uid_owner")
    routing.upsert_group(chat_id="oc_group", profile_name="group_profile", owner_open_id="ou_owner")

    resp = _CardAdapter()._on_card_action_trigger(
        _card_data(operator=SimpleNamespace(union_id="on_owner"))
    )
    assert routing.get_group_reply_mode("oc_group") == "all"
    assert _resp_kind(resp) == "card"


def test_schema2_user_id_only_operator_matches_owner_via_synced_row(routing):
    """Operator carries only user_id (Schema 2). Match via the owner's synced
    user row user_id."""
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger

    _patch_on_card_action_trigger(_CardAdapter)
    _seed_owner_user_row(routing, open_id="ou_owner", union_id="on_owner", user_id="uid_owner")
    routing.upsert_group(chat_id="oc_group", profile_name="group_profile", owner_open_id="ou_owner")

    resp = _CardAdapter()._on_card_action_trigger(
        _card_data(operator=SimpleNamespace(user_id="uid_owner"))
    )
    assert routing.get_group_reply_mode("oc_group") == "all"
    assert _resp_kind(resp) == "card"


def test_schema2_union_id_only_operator_matches_pending_inviter_union_id(routing):
    """Owner is only in the pending table (group not provisioned yet) AND not
    synced as a user row. The inviter's union_id captured at bot_added is the
    only way a Schema 2 operator can be matched."""
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger

    _patch_on_card_action_trigger(_CardAdapter)
    assert routing.lookup_by_chat_id("oc_group") is None
    # no synced user row for the owner; only the pending capture with union_id
    routing.put_pending_inviter("oc_group", "ou_owner", inviter_union_id="on_owner")

    resp = _CardAdapter()._on_card_action_trigger(
        _card_data(operator=SimpleNamespace(union_id="on_owner"))
    )
    assert routing.get_group_reply_mode("oc_group") == "all"
    assert _resp_kind(resp) == "card"


def test_schema2_non_owner_union_id_still_denied(routing):
    """A Schema 2 operator whose union_id is NOT the owner's must still be
    denied — the broadened matching must not relax the permission."""
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger

    _patch_on_card_action_trigger(_CardAdapter)
    _seed_owner_user_row(routing, open_id="ou_owner", union_id="on_owner", user_id="uid_owner")
    routing.upsert_group(chat_id="oc_group", profile_name="group_profile", owner_open_id="ou_owner")

    resp = _CardAdapter()._on_card_action_trigger(
        _card_data(operator=SimpleNamespace(union_id="on_someone_else"))
    )
    assert routing.get_group_reply_mode("oc_group") == "mention"
    assert _resp_kind(resp) == "toast"
    assert "无权" in _resp_toast_content(resp)


def test_schema2_empty_operator_denied(routing):
    """Operator with no usable id at all is denied (fail-closed)."""
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger

    _patch_on_card_action_trigger(_CardAdapter)
    _seed_owner_user_row(routing, open_id="ou_owner", union_id="on_owner", user_id="uid_owner")
    routing.upsert_group(chat_id="oc_group", profile_name="group_profile", owner_open_id="ou_owner")

    resp = _CardAdapter()._on_card_action_trigger(
        _card_data(operator=SimpleNamespace(open_id="", union_id="", user_id=""))
    )
    assert routing.get_group_reply_mode("oc_group") == "mention"
    assert _resp_kind(resp) == "toast"


# --------------------------------------------------------------------------- #
# bot_added must capture the inviter's union_id (auto-backfill source)
# --------------------------------------------------------------------------- #

def test_pending_inviter_union_id_column_migrated_on_old_db(tmp_path):
    """Regression guard (SPEC): opening an old DB whose
    multitenancy_pending_group_inviter table predates the inviter_union_id
    column must add it idempotently without losing existing rows."""
    import sqlite3

    db_path = tmp_path / "routing-old-pending.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE multitenancy_pending_group_inviter (
            chat_id          TEXT PRIMARY KEY NOT NULL,
            inviter_open_id  TEXT NOT NULL,
            created_at       INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO multitenancy_pending_group_inviter "
        "(chat_id, inviter_open_id, created_at) VALUES (?, ?, ?)",
        ("oc_old", "ou_old_owner", 123),
    )
    conn.commit()
    conn.close()

    from hermes_multitenancy.routing import RoutingTable

    table = RoutingTable(str(db_path))
    try:
        cols = {
            row["name"]
            for row in table._conn.execute(
                "PRAGMA table_info(multitenancy_pending_group_inviter)"
            )
        }
        assert "inviter_union_id" in cols
        # pre-existing row survives, its (absent) union_id reads as None
        assert table.get_pending_inviter("oc_old") == "ou_old_owner"
        assert table.get_pending_inviter_union_id("oc_old") is None
        # and the migrated table accepts a union_id write
        table.put_pending_inviter("oc_new", "ou_new", inviter_union_id="on_new")
        assert table.get_pending_inviter_union_id("oc_new") == "on_new"
    finally:
        table.close()


def test_bot_added_captures_inviter_union_id_into_pending(routing):
    """group_inviter_hook must persist the inviter's union_id so a later
    Schema 2 card action can match it pre-provision."""
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.group_inviter_hook import _capture_inviter_from_event

    router_mod._chat_inviter_cache.clear()
    data = SimpleNamespace(
        event=SimpleNamespace(
            chat_id="oc_abc",
            chat_name="IT 组",
            operator_id=SimpleNamespace(open_id="ou_inviter", union_id="on_inviter"),
        ),
    )
    _capture_inviter_from_event(data)

    cached = router_mod._chat_inviter_cache["oc_abc"]
    assert cached["inviter_open_id"] == "ou_inviter"
    assert cached.get("inviter_union_id") == "on_inviter"
    # and it reached the durable pending table union_id slot
    assert routing.get_pending_inviter_union_id("oc_abc") == "on_inviter"
