from __future__ import annotations

import json

from hermes_multitenancy.token_usage_uploader import (
    aggregate_day,
    build_records,
    distinct_dates,
    iter_ledger_rows,
    make_owner_resolver,
    _line_date,
)


def _ledger(*rows: dict) -> str:
    return "\n".join(json.dumps(r) for r in rows) + "\n"


# DM-only resolver: no group/profile routing → non-group rows resolve to their sender.
_DM_OWNER = make_owner_resolver(lambda chat_id: None, lambda profile: None)


def test_line_date_converts_to_shanghai_day() -> None:
    assert _line_date("2026-06-11T17:40:12+08:00") == "2026-06-11"
    # 00:30 +08:00 stays same day; a UTC-Z near midnight shifts +8h.
    assert _line_date("2026-06-10T20:00:00Z") == "2026-06-11"
    assert _line_date("garbage") == ""


def test_aggregate_sums_per_sender_and_model_for_the_day() -> None:
    text = _ledger(
        {"ts": "2026-06-11T09:00:00+08:00", "sender_open_id": "ou_a", "model": "m1",
         "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        {"ts": "2026-06-11T10:00:00+08:00", "sender_open_id": "ou_a", "model": "m1",
         "input_tokens": 20, "output_tokens": 6, "total_tokens": 26},
        {"ts": "2026-06-11T11:00:00+08:00", "sender_open_id": "ou_a", "model": "m2",
         "input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        # different day -> excluded
        {"ts": "2026-06-10T11:00:00+08:00", "sender_open_id": "ou_a", "model": "m1",
         "input_tokens": 999, "output_tokens": 999, "total_tokens": 1998},
    )
    agg = aggregate_day(iter_ledger_rows(text), "2026-06-11", _DM_OWNER)
    assert agg[("ou_a", "m1")] == {"input_tokens": 30, "output_tokens": 11, "total_tokens": 41}
    assert agg[("ou_a", "m2")] == {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    assert ("ou_a", "m1") in agg and len(agg) == 2  # yesterday's row excluded


def test_group_usage_attributes_to_group_owner_not_senders() -> None:
    # Two different humans @ the bot in the SAME group. Per sunke's model, ALL of it
    # bills to the group's owner (whoever pulled the bot in), not the @-ers.
    text = _ledger(
        {"ts": "2026-06-11T09:00:00+08:00", "sender_open_id": "ou_alice", "profile": "grp",
         "chat_type": "group", "chat_id": "oc_team", "model": "m",
         "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        {"ts": "2026-06-11T09:05:00+08:00", "sender_open_id": "ou_bob", "profile": "grp",
         "chat_type": "group", "chat_id": "oc_team", "model": "m",
         "input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
    )
    # oc_team was pulled in by ou_owner.
    owner_of = make_owner_resolver(
        lambda chat_id: "ou_owner" if chat_id == "oc_team" else None,
        lambda profile: None,
    )
    agg = aggregate_day(iter_ledger_rows(text), "2026-06-11", owner_of)
    assert agg == {("ou_owner", "m"): {"input_tokens": 17, "output_tokens": 8, "total_tokens": 25}}


def test_group_with_unknown_chat_is_dropped_not_misattributed() -> None:
    text = _ledger(
        {"ts": "2026-06-11T09:00:00+08:00", "sender_open_id": "ou_alice", "profile": "grp",
         "chat_type": "group", "chat_id": "oc_unknown", "model": "m",
         "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    owner_of = make_owner_resolver(lambda chat_id: None, lambda profile: None)
    # Routing can't resolve the group -> dropped, NEVER attributed to the @-er.
    assert aggregate_day(iter_ledger_rows(text), "2026-06-11", owner_of) == {}


def test_topic_group_also_attributes_to_owner() -> None:
    # Feishu 'topic' chats are multi-person too -> bill to the group owner, not the @-er.
    owner_of = make_owner_resolver(
        lambda chat_id: "ou_owner" if chat_id == "oc_topic" else None,
        lambda profile: None,
    )
    row = {"chat_type": "topic", "chat_id": "oc_topic", "sender_open_id": "ou_x"}
    assert owner_of(row) == "ou_owner"


def test_make_owner_resolver_rules() -> None:
    owner_of = make_owner_resolver(
        lambda chat_id: {"oc_g": "ou_inviter"}.get(chat_id),
        lambda profile: {"sunke": "ou_sunke"}.get(profile),
    )
    # group -> inviter
    assert owner_of({"chat_type": "group", "chat_id": "oc_g", "sender_open_id": "ou_x"}) == "ou_inviter"
    # topic (also multi-person) -> inviter, not sender
    assert owner_of({"chat_type": "topic", "chat_id": "oc_g", "sender_open_id": "ou_x"}) == "ou_inviter"
    # group unknown -> None (drop, never the sender)
    assert owner_of({"chat_type": "group", "chat_id": "oc_?", "sender_open_id": "ou_x"}) is None
    # DM with sender -> the person
    assert owner_of({"chat_type": "p2p", "sender_open_id": "ou_p"}) == "ou_p"
    # DM empty sender (e.g. webui ingest) -> profile owner
    assert owner_of({"chat_type": "p2p", "sender_open_id": "", "profile": "sunke"}) == "ou_sunke"
    # empty sender + unknown profile -> None
    assert owner_of({"chat_type": "p2p", "sender_open_id": "", "profile": "ghost"}) is None


def test_build_records_resolves_email_and_skips_unresolved() -> None:
    agg = {
        ("ou_a", "m1"): {"input_tokens": 30, "output_tokens": 11, "total_tokens": 41},
        ("ou_ghost", "m1"): {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
    }
    directory = {"ou_a": {"email": "alice@corp.com", "dept": "Eng"}}
    records, stats = build_records(agg, lambda oid: directory.get(oid))
    assert stats == {"people_models": 2, "records": 1, "skipped_open_ids": 1}
    assert records == [{
        "email": "alice@corp.com", "dept": "Eng", "provider": "", "model": "m1",
        "input_tokens": 30, "output_tokens": 11, "total_tokens": 41,
    }]


def test_build_records_total_fallback_when_zero() -> None:
    agg = {("ou_a", "m"): {"input_tokens": 4, "output_tokens": 6, "total_tokens": 0}}
    records, _ = build_records(agg, lambda oid: {"email": "a@c.com", "dept": "X"})
    assert records[0]["total_tokens"] == 10


def test_resolver_returning_no_email_is_skipped() -> None:
    agg = {("ou_a", "m"): {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    records, stats = build_records(agg, lambda oid: {"email": "", "dept": "X"})
    assert records == [] and stats["skipped_open_ids"] == 1


def test_distinct_dates_for_backfill_are_sorted_unique() -> None:
    text = _ledger(
        {"ts": "2026-06-11T09:00:00+08:00", "sender_open_id": "ou_a", "model": "m",
         "input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        {"ts": "2026-06-09T23:30:00+08:00", "sender_open_id": "ou_a", "model": "m",
         "input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        {"ts": "2026-06-11T20:00:00+08:00", "sender_open_id": "ou_b", "model": "m",
         "input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        {"ts": "garbage", "sender_open_id": "ou_c", "model": "m",
         "input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    assert distinct_dates(iter_ledger_rows(text)) == ["2026-06-09", "2026-06-11"]


def test_rows_without_sender_or_resolvable_owner_are_dropped() -> None:
    text = _ledger(
        {"ts": "2026-06-11T09:00:00+08:00", "sender_open_id": "", "profile": "", "model": "m",
         "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    assert aggregate_day(iter_ledger_rows(text), "2026-06-11", _DM_OWNER) == {}


# --- feishu-sync directory resolution (reuses feishu-sync auth, no lark-cli) ---

class _FakeContact:
    """Stands in for FeishuContactClient: same surface fetch_contact_directory uses."""

    def __init__(self, depts, users_by_dept):
        self._depts = depts
        self._users = users_by_dept

    def fetch_department_tree(self, root_id):
        return self._depts

    def fetch_department_detail(self, root_id):
        return None

    def iter_department_user_records(self, dept_id):
        return self._users.get(dept_id, [])


def test_fetch_contact_directory_maps_open_id_to_email_and_dept() -> None:
    from types import SimpleNamespace
    from hermes_multitenancy.sync.feishu_org import fetch_contact_directory

    depts = [SimpleNamespace(dept_id="d1", name="Engineering")]
    users = {"d1": [
        {"open_id": "ou_a", "user_id": "alice", "name": "Alice",
         "enterprise_email": "alice@corp.com", "email": "alice@personal.com"},
        {"open_id": "ou_b", "user_id": "bob", "name": "Bob", "email": "bob@corp.com"},
        {"open_id": "ou_noemail", "user_id": "ghost", "name": "Ghost"},  # dropped: no email
    ]}
    directory = fetch_contact_directory(client=_FakeContact(depts, users))
    assert directory["ou_a"] == {"email": "alice@corp.com", "dept": "Engineering", "name": "Alice"}
    assert directory["ou_b"]["email"] == "bob@corp.com"  # falls back to email when no enterprise_email
    assert "ou_noemail" not in directory


# --- email via routing (open_id -> user_id -> user_id@domain; no Feishu email scope) ---

class _FakeRoutingTable:
    def __init__(self, rows):
        self._rows = rows  # open_id -> SimpleNamespace(user_id=..., display_label=...)

    def lookup_by_open_id(self, open_id):
        return self._rows.get(open_id)


def test_email_dept_for_open_id_derives_company_key():
    from types import SimpleNamespace
    from hermes_multitenancy.token_usage_uploader import RoutingOwnerLookup

    r = RoutingOwnerLookup.__new__(RoutingOwnerLookup)
    r._table = _FakeRoutingTable({
        "ou_sunke": SimpleNamespace(user_id="sunke", display_label="IT 组"),
        "ou_synth": SimpleNamespace(user_id="ou_7576abcd", display_label=""),   # synthetic user_id
        "ou_empty": SimpleNamespace(user_id="", display_label=""),
    })
    # real user_id -> <user_id>@domain (the company-wide leaderboard key)
    assert r.email_dept_for_open_id("ou_sunke", "keep.com") == {"email": "sunke@keep.com", "dept": "IT 组"}
    # synthetic (ou_*) user_id -> None (can't map to a real employee email)
    assert r.email_dept_for_open_id("ou_synth", "keep.com") is None
    # empty user_id -> None
    assert r.email_dept_for_open_id("ou_empty", "keep.com") is None
    # unknown open_id -> None
    assert r.email_dept_for_open_id("ou_missing", "keep.com") is None


def test_email_dept_dept_falls_back_to_unknown():
    from types import SimpleNamespace
    from hermes_multitenancy.token_usage_uploader import RoutingOwnerLookup

    r = RoutingOwnerLookup.__new__(RoutingOwnerLookup)
    r._table = _FakeRoutingTable({"ou_x": SimpleNamespace(user_id="x", display_label="")})
    assert r.email_dept_for_open_id("ou_x", "keep.com") == {"email": "x@keep.com", "dept": "unknown"}


class _FakeRoutingDupRows:
    """Same open_id has a sync root (real user_id) AND a synthetic sibling.
    resolve_owner_root returns the sync root; lookup_by_open_id (no order) may
    return the synthetic. email resolution must prefer the sync root."""

    def __init__(self, root, sibling):
        self._root = root
        self._sibling = sibling

    def resolve_owner_root(self, open_id):
        return self._root

    def lookup_by_open_id(self, open_id):
        return self._sibling  # the wrong (synthetic) one — must NOT be used


def test_email_prefers_sync_root_over_synthetic_sibling():
    from types import SimpleNamespace
    from hermes_multitenancy.token_usage_uploader import RoutingOwnerLookup

    r = RoutingOwnerLookup.__new__(RoutingOwnerLookup)
    r._table = _FakeRoutingDupRows(
        root=SimpleNamespace(user_id="sunke", display_label="IT 组"),
        sibling=SimpleNamespace(user_id="ou_7576abcd", display_label=""),
    )
    # resolve_owner_root (sync) wins -> real LDAP, not the synthetic sibling.
    assert r.email_dept_for_open_id("ou_sunke", "keep.com") == {"email": "sunke@keep.com", "dept": "IT 组"}


def test_email_falls_back_to_lookup_when_no_sync_root():
    from types import SimpleNamespace
    from hermes_multitenancy.token_usage_uploader import RoutingOwnerLookup

    # sync root absent (None) -> fall back to lookup_by_open_id's real user_id.
    r = RoutingOwnerLookup.__new__(RoutingOwnerLookup)
    r._table = _FakeRoutingDupRows(
        root=None,
        sibling=SimpleNamespace(user_id="bob", display_label="Eng"),
    )
    assert r.email_dept_for_open_id("ou_bob", "keep.com") == {"email": "bob@keep.com", "dept": "Eng"}
