from __future__ import annotations

import json

from hermes_multitenancy.token_usage_uploader import (
    aggregate_day,
    build_records,
    distinct_dates,
    iter_ledger_rows,
    _line_date,
)


def _ledger(*rows: dict) -> str:
    return "\n".join(json.dumps(r) for r in rows) + "\n"


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
    agg = aggregate_day(iter_ledger_rows(text), "2026-06-11")
    assert agg[("ou_a", "m1")] == {"input_tokens": 30, "output_tokens": 11, "total_tokens": 41}
    assert agg[("ou_a", "m2")] == {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    assert ("ou_a", "m1") in agg and len(agg) == 2  # yesterday's row excluded


def test_group_chat_two_humans_attribute_separately() -> None:
    text = _ledger(
        {"ts": "2026-06-11T09:00:00+08:00", "sender_open_id": "ou_alice", "profile": "grp",
         "model": "m", "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        {"ts": "2026-06-11T09:05:00+08:00", "sender_open_id": "ou_bob", "profile": "grp",
         "model": "m", "input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
    )
    agg = aggregate_day(iter_ledger_rows(text), "2026-06-11")
    assert agg[("ou_alice", "m")]["total_tokens"] == 15
    assert agg[("ou_bob", "m")]["total_tokens"] == 10


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


def test_rows_without_sender_are_dropped_from_aggregate() -> None:
    text = _ledger(
        {"ts": "2026-06-11T09:00:00+08:00", "sender_open_id": "", "model": "m",
         "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    assert aggregate_day(iter_ledger_rows(text), "2026-06-11") == {}


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


def test_feishu_sync_resolver_reads_same_day_cache_without_network(monkeypatch, tmp_path) -> None:
    from hermes_multitenancy.token_usage_uploader import FeishuSyncResolver

    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({
        "day": "2026-06-11",
        "map": {"ou_a": {"email": "alice@corp.com", "dept": "Eng"}},
    }), encoding="utf-8")

    # If it tried the network it would import fetch_contact_directory; ensure it doesn't need to.
    resolver = FeishuSyncResolver(cache, day="2026-06-11")
    assert resolver("ou_a") == {"email": "alice@corp.com", "dept": "Eng"}
    assert resolver("ou_unknown") is None
