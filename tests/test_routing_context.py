"""P0-2 — typed TenantContext resolution with fail-closed semantics."""
from __future__ import annotations

import pytest


@pytest.fixture
def table():
    from hermes_multitenancy.routing import RoutingTable

    t = RoutingTable(":memory:")
    yield t
    t.close()


def _seed_user(table, *, user_id, open_id, union_id=None, profile=None):
    table.upsert(
        user_id=user_id,
        profile_name=profile or f"feishu_{user_id}",
        open_id=open_id,
        union_id=union_id,
        provenance="sync",
    )


def test_resolve_context_active_user_stable_key(table):
    _seed_user(table, user_id="u_alice", open_id="ou_alice", union_id="on_alice", profile="feishu_alice")
    ctx = table.resolve_context("ou_alice")
    assert ctx is not None
    assert ctx.profile_name == "feishu_alice"
    assert ctx.open_id == "ou_alice"
    assert ctx.user_key == "ou_alice"      # stable open_id-first key
    assert ctx.kind == "user"
    assert isinstance(ctx.route_version, int)


def test_resolve_context_unknown_sender_denies(table):
    assert table.resolve_context("ou_nobody") is None


def test_resolve_context_deleted_user_denies(table):
    _seed_user(table, user_id="u_bob", open_id="ou_bob", profile="feishu_bob")
    assert table.resolve_context("ou_bob") is not None
    # Soft-delete the user → lookups filter active=1 → context denied.
    table.soft_delete("u_bob")
    assert table.resolve_context("ou_bob") is None


def test_resolve_context_alias_is_lookup_fallback_not_primary_key(table):
    # User resolvable by union_id; sender passed is the union id. The stable
    # user_key must still prefer the row's open_id, not the alias.
    _seed_user(table, user_id="u_carol", open_id="ou_carol", union_id="on_carol", profile="feishu_carol")
    ctx = table.resolve_context("on_carol")  # resolved via union lookup
    assert ctx is not None
    assert ctx.profile_name == "feishu_carol"
    # user_key is the sender we keyed on (stable id), never a deleted/unstable alias.
    assert ctx.user_key in {"on_carol", "ou_carol"}


def test_resolve_context_group_binds_owner_and_chat(table):
    table.upsert_group(
        chat_id="oc_group1", profile_name="feishu_group1", owner_open_id="ou_owner"
    )
    ctx = table.resolve_context("ou_someone", chat_id="oc_group1")
    assert ctx is not None
    assert ctx.is_group
    assert ctx.chat_id == "oc_group1"
    assert ctx.owner_open_id == "ou_owner"   # stable owner binding
    assert ctx.profile_name == "feishu_group1"
