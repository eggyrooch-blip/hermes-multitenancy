from __future__ import annotations

from pathlib import Path

from hermes_multitenancy.lark_cli_auth_broker import (
    LarkCliAuthBrokerContext,
    _live_owner_mapped_group,
    _personal_bot_identity_policy_error,
)
from hermes_multitenancy.routing import RoutingTable


def _ctx(shared: Path, *, allowed=frozenset()) -> LarkCliAuthBrokerContext:
    return LarkCliAuthBrokerContext(
        shared_home=shared,
        profile_name="alice",
        user_open_id="ou_alice",
        hmac_key="k",
        allowed_identities=frozenset({"user", "bot"}),
        profile_kind="user",
        allowed_bot_chat_ids=allowed,
    )


def _seed_group(shared: Path, chat_id: str, owner: str):
    t = RoutingTable(shared / "multitenancy.db")
    try:
        t.upsert_group(chat_id=chat_id, profile_name="grp", owner_open_id=owner)
    finally:
        t.close()


def _send_args(chat_id: str):
    import json
    return {
        "identity": "bot",
        "method": "POST",
        "path_and_query": "/open-apis/im/v1/messages?receive_id_type=chat_id",
        "body": json.dumps({"receive_id": chat_id, "msg_type": "text"}).encode("utf-8"),
    }


def test_live_check_allows_senders_fresh_owned_group_not_in_cache(tmp_path):
    # The group exists in routing (owner=ou_alice) but the cached allow-set is EMPTY
    # (freshness race: created mid-turn). Live re-check must allow the send.
    _seed_group(tmp_path, "oc_fresh", "ou_alice")
    err = _personal_bot_identity_policy_error(_ctx(tmp_path, allowed=frozenset()), **_send_args("oc_fresh"))
    assert err is None


def test_live_check_rejects_group_not_owned_by_sender(tmp_path):
    _seed_group(tmp_path, "oc_other", "ou_someone_else")
    err = _personal_bot_identity_policy_error(_ctx(tmp_path, allowed=frozenset()), **_send_args("oc_other"))
    assert err is not None and "owner mapped group chats" in err


def test_live_check_rejects_unknown_chat(tmp_path):
    _seed_group(tmp_path, "oc_known", "ou_alice")  # db exists but target chat is different
    err = _personal_bot_identity_policy_error(_ctx(tmp_path, allowed=frozenset()), **_send_args("oc_unknown"))
    assert err is not None


def test_cached_allow_still_works_without_live_check(tmp_path):
    # No routing db needed: cached set hit short-circuits before any live lookup.
    err = _personal_bot_identity_policy_error(
        _ctx(tmp_path, allowed=frozenset({"oc_cached"})), **_send_args("oc_cached")
    )
    assert err is None


def test_live_owner_mapped_group_helper(tmp_path):
    _seed_group(tmp_path, "oc_g", "ou_owner")
    assert _live_owner_mapped_group(tmp_path, "oc_g", "ou_owner") is True
    assert _live_owner_mapped_group(tmp_path, "oc_g", "ou_other") is False
    assert _live_owner_mapped_group(tmp_path, "oc_missing", "ou_owner") is False
    assert _live_owner_mapped_group(tmp_path, "", "ou_owner") is False
    # Missing db -> best-effort False, no raise.
    assert _live_owner_mapped_group(tmp_path / "nope", "oc_g", "ou_owner") is False
