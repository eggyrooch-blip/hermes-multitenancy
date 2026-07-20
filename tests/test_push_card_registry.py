"""push_registry store + scene registry + rate-limited send queue (SPEC P1/P2).

Covers the state machine, idempotency / business-key semantics, the matcher's
open-set read guard, archival, and the send queue's token-bucket / retry /
daily-cap / quiet-hours behavior — all with in-memory stores, fake clocks, and
a no-op sleep (no live Feishu adapter).
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from hermes_multitenancy import push_registry as reg
from hermes_multitenancy import push_scenes as scenes
from hermes_multitenancy import push_send_queue as sq


@pytest.fixture
def store():
    s = reg.PushRegistryStore(":memory:")
    yield s
    s.close()


def _create(store, **over):
    kw = dict(
        scene="dev-acceptance-claim",
        skill="push-fill-form",
        target_open_id="ou_alice",
        profile_name="alice-profile",
        business_key="dev-acceptance-claim:ou_alice:2026-07-20",
        payload={"amount": None},
    )
    kw.update(over)
    return store.create(**kw)


# --- scene registry ------------------------------------------------------

def test_scene_registry_has_dev_acceptance_claim():
    sc = scenes.get_scene("dev-acceptance-claim")
    assert sc is not None
    assert sc.skill == "push-fill-form"
    assert sc.writer == "kep-pre-claim-writer"
    assert sc.required_keys() == ("amount", "date", "category", "reason")
    amount = sc.field("amount")
    assert amount.risk == scenes.RISK_HIGH  # money is never silently pre-filled
    assert sc.field("category").options == ("打车", "餐饮", "办公用品")
    assert "{registry_id}" in sc.deterministic_marker
    assert scenes.scene_exists("dev-acceptance-claim")
    assert not scenes.scene_exists("no-such-scene")
    # config capabilities: the dev scene ships NO hardcoded 回调地址 (dev靠env兜底),
    # and behaviors default to the original submit-once semantics.
    assert sc.callback is None
    assert sc.behaviors.submit_once is True
    assert sc.behaviors.allow_resubmit_before_commit is True
    assert sc.behaviors.max_submits is None


# --- create / idempotency / business key ---------------------------------

def test_create_new_row_is_sending(store):
    res = _create(store)
    assert res.created and res.conflict is None
    assert res.row["status"] == reg.STATUS_SENDING
    assert res.row["registry_id"].startswith("pcr_")
    assert res.row["nonce"]  # generated for the confirm callback (P5)


def test_idempotency_key_returns_original(store):
    first = _create(store, idempotency_key="idem-1")
    again = _create(store, idempotency_key="idem-1", payload={"amount": 999})
    assert again.created is False
    assert again.conflict == "idempotent"
    assert again.row["registry_id"] == first.row["registry_id"]
    assert store.count() == 1


def test_business_key_inflight_collision(store):
    first = _create(store)
    dup = _create(store, idempotency_key="other")  # same business_key, in flight
    assert dup.created is False
    assert dup.conflict == "business_key"
    assert dup.row["registry_id"] == first.row["registry_id"]


def test_business_key_released_after_terminal(store):
    first = _create(store)
    assert store.mark_sent(first.row["registry_id"], message_id="m1")
    # drive to a terminal state so the business_key frees up
    assert store.advance_status(
        first.row["registry_id"], expect=reg.STATUS_PENDING, to=reg.STATUS_COMMITTED
    )
    again = _create(store)
    assert again.created is True and again.conflict is None


# --- state transitions ---------------------------------------------------

def test_mark_sent_cas_and_message_id(store):
    r = _create(store).row
    assert store.mark_sent(r["registry_id"], message_id="om_123", chat_id="oc_1")
    row = store.get(r["registry_id"])
    assert row["status"] == reg.STATUS_PENDING
    assert row["message_id"] == "om_123"
    assert row["chat_id"] == "oc_1"
    assert row["send_attempts"] == 1
    # second mark_sent no-ops (not in 'sending' anymore)
    assert store.mark_sent(r["registry_id"], message_id="om_999") is False


def test_mark_send_failed_visible(store):
    r = _create(store).row
    assert store.mark_send_failed(r["registry_id"], error="feishu 429")
    row = store.get(r["registry_id"])
    assert row["status"] == reg.STATUS_FAILED
    assert "429" in row["last_error"]


def test_advance_status_nonce_guard(store):
    r = _create(store).row
    store.mark_sent(r["registry_id"], message_id="m")
    store.advance_status(r["registry_id"], expect=reg.STATUS_PENDING, to=reg.STATUS_CLARIFYING)
    nonce = store.get(r["registry_id"])["nonce"]
    # wrong nonce cannot advance
    assert not store.advance_status(
        r["registry_id"], expect=reg.STATUS_CLARIFYING, to=reg.STATUS_CONFIRMED,
        expect_nonce="bad", clear_nonce=True,
    )
    # right nonce advances and single-use-clears it
    assert store.advance_status(
        r["registry_id"], expect=reg.STATUS_CLARIFYING, to=reg.STATUS_CONFIRMED,
        expect_nonce=nonce, clear_nonce=True,
    )
    assert store.get(r["registry_id"])["nonce"] is None


# --- matcher open-set read guard -----------------------------------------

def test_list_open_only_pending_clarifying_and_unexpired(store):
    a = _create(store, business_key="bk-a").row
    store.mark_sent(a["registry_id"], message_id="ma")  # -> pending
    b = _create(store, business_key="bk-b", target_open_id="ou_alice").row  # sending (excluded)
    c = _create(store, business_key="bk-c").row
    store.mark_sent(c["registry_id"], message_id="mc")
    store.advance_status(c["registry_id"], expect=reg.STATUS_PENDING, to=reg.STATUS_CLARIFYING)
    # expired pending row must be excluded by the read-time guard
    d = _create(store, business_key="bk-d", expires_at=100).row
    store.mark_sent(d["registry_id"], message_id="md")

    open_rows = store.list_open_for_user("ou_alice", now=1000)
    ids = {r["registry_id"] for r in open_rows}
    assert a["registry_id"] in ids
    assert c["registry_id"] in ids
    assert b["registry_id"] not in ids  # still 'sending'
    assert d["registry_id"] not in ids  # past expires_at


def test_expire_due_sweeps_inflight(store):
    r = _create(store, expires_at=100).row
    store.mark_sent(r["registry_id"], message_id="m")
    assert store.expire_due(now=1000) == 1
    assert store.get(r["registry_id"])["status"] == reg.STATUS_EXPIRED


def test_expire_due_does_not_reap_confirmed(store):
    # A confirmed-but-not-committed row past expires_at is the reconcile worker's
    # job — the expiry sweep must NOT reap it (finding expiry-reaps-confirmed).
    # Silently expiring it would discard a submission the user already confirmed
    # and drop it out of the reconcile net (list_stale_confirmed scans confirmed).
    rid = _create(store, expires_at=100).row["registry_id"]
    store.mark_sent(rid, message_id="m")
    store.advance_status(rid, expect=reg.STATUS_PENDING, to=reg.STATUS_CLARIFYING)
    store.advance_status(rid, expect=reg.STATUS_CLARIFYING, to=reg.STATUS_CONFIRMED,
                         submission={"amount": 68})
    assert store.expire_due(now=10_000) == 0                     # not swept
    assert store.get(rid)["status"] == reg.STATUS_CONFIRMED
    assert rid not in {x["registry_id"] for x in store.list_due_open(now=10_000)}
    # still reconcilable — the reconcile worker can backfill committed
    stale = {x["registry_id"] for x in store.list_stale_confirmed(older_than=reg._now() + 10)}
    assert rid in stale


def test_archive_finished_moves_to_cold_table(store):
    r = _create(store).row
    store.mark_send_failed(r["registry_id"], error="x")
    moved = store.archive_finished(older_than=reg._now() + 10)
    assert moved == 1
    assert store.get(r["registry_id"]) is None
    assert store.count() == 0


def test_count_pushes_today_ignores_failed(store):
    day_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    a = _create(store, business_key="bk1").row
    store.mark_sent(a["registry_id"], message_id="m")
    b = _create(store, business_key="bk2").row
    store.mark_send_failed(b["registry_id"], error="e")
    assert store.count_pushes_today("ou_alice", day_start=day_start) == 1


def test_count_pushes_today_excludes_undelivered_sending(store):
    # Undelivered 'sending' rows must NOT count against the cap — otherwise a
    # user whose cards are all parked in 'sending' (quiet-hours / cap deferral)
    # counts themselves over the cap and never sends any (finding
    # daily-cap-self-starvation). Three sending rows → count is 0, not 3.
    day_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    for i in range(3):
        _create(store, business_key=f"send-{i}")  # left in 'sending'
    assert store.count_pushes_today("ou_alice", day_start=day_start) == 0
    # once one is actually delivered it counts.
    delivered = _create(store, business_key="delivered").row
    store.mark_sent(delivered["registry_id"], message_id="m")
    assert store.count_pushes_today("ou_alice", day_start=day_start) == 1


# --- send queue ----------------------------------------------------------

class _FakeClock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


async def _noop_sleep(_seconds):
    return None


def _queue(store, sender, *, clock_dt=None, **kw):
    clock = _FakeClock(clock_dt or datetime(2026, 7, 20, 12, 0, 0))
    return sq.PushSendQueue(
        store, sender, clock=clock, sleep_fn=_noop_sleep,
        rate_per_sec=1000, burst=1000, **kw,
    )


def test_queue_sends_and_marks_pending(store):
    r = _create(store).row
    calls = []

    async def sender(*, profile_name, open_id, card, payload):
        calls.append((profile_name, open_id))
        return sq.SendResult(message_id="om_sent")

    outcome = asyncio.run(_queue(store, sender).process(r["registry_id"]))
    assert outcome.status == "sent" and outcome.message_id == "om_sent"
    assert store.get(r["registry_id"])["status"] == reg.STATUS_PENDING
    assert calls == [("alice-profile", "ou_alice")]


def test_queue_retries_then_fails_visibly(store):
    r = _create(store).row
    attempts = {"n": 0}

    async def flaky(*, profile_name, open_id, card, payload):
        attempts["n"] += 1
        raise RuntimeError("boom")

    outcome = asyncio.run(_queue(store, flaky, max_retries=3).process(r["registry_id"]))
    assert outcome.status == "failed"
    assert attempts["n"] == 3  # retried up to the cap
    assert store.get(r["registry_id"])["status"] == reg.STATUS_FAILED


def test_queue_recovers_after_transient_failure(store):
    r = _create(store).row
    attempts = {"n": 0}

    async def recover(*, profile_name, open_id, card, payload):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("transient")
        return sq.SendResult(message_id="om_ok")

    outcome = asyncio.run(_queue(store, recover, max_retries=3).process(r["registry_id"]))
    assert outcome.status == "sent"
    assert store.get(r["registry_id"])["status"] == reg.STATUS_PENDING


def test_queue_defers_in_quiet_hours(store):
    r = _create(store).row
    called = {"n": 0}

    async def sender(*, profile_name, open_id, card, payload):
        called["n"] += 1
        return sq.SendResult(message_id="x")

    q = _queue(store, sender, clock_dt=datetime(2026, 7, 20, 22, 30, 0))
    outcome = asyncio.run(q.process(r["registry_id"]))
    assert outcome.status == "deferred" and outcome.reason == "quiet_hours"
    assert called["n"] == 0  # never sent
    assert store.get(r["registry_id"])["status"] == reg.STATUS_SENDING  # still queued


def test_queue_defers_on_daily_cap(store):
    # 3 already delivered today for the same user → 4th defers.
    day = datetime(2026, 7, 20, 12, 0, 0)
    for i in range(3):
        row = _create(store, business_key=f"bk-{i}").row
        store.mark_sent(row["registry_id"], message_id=f"m{i}")
    r = _create(store, business_key="bk-new").row
    called = {"n": 0}

    async def sender(*, profile_name, open_id, card, payload):
        called["n"] += 1
        return sq.SendResult(message_id="x")

    q = _queue(store, sender, clock_dt=day, daily_cap=3)
    outcome = asyncio.run(q.process(r["registry_id"]))
    assert outcome.status == "deferred" and outcome.reason == "daily_cap"
    assert called["n"] == 0


def test_queue_skips_already_sent_row(store):
    r = _create(store).row
    store.mark_sent(r["registry_id"], message_id="already")

    async def sender(*, profile_name, open_id, card, payload):
        raise AssertionError("must not send a non-sending row")

    outcome = asyncio.run(_queue(store, sender).process(r["registry_id"]))
    assert outcome.status == "skipped"


def test_default_sender_unavailable_without_registration():
    sq.register_push_card_sender(None)
    with pytest.raises(sq.PushCardSenderUnavailable):
        asyncio.run(sq._default_sender(profile_name="p", open_id="o", card={}, payload={}))


# --- token bucket --------------------------------------------------------

def test_token_bucket_paces_after_burst():
    t = {"v": 0.0}
    bucket = sq.TokenBucket(rate_per_sec=2.0, capacity=2.0, monotonic_fn=lambda: t["v"])
    assert bucket.take() == 0.0  # burst token 1
    assert bucket.take() == 0.0  # burst token 2
    wait = bucket.take()          # empty now → must wait ~0.5s at 2/s
    assert 0.4 < wait <= 0.5
    t["v"] += 1.0                 # 1s later, 2 tokens refilled
    assert bucket.take() == 0.0


# --- callback / behaviors columns + migration ----------------------------

def test_create_stores_callback_and_behaviors_json(store):
    res = store.create(
        scene="dev-acceptance-claim", skill="push-fill-form", target_open_id="ou_a",
        profile_name="p", business_key="dev-acceptance-claim:ou_a:r",
        callback_json='{"url":"http://x/api","timeout_s":9}',
        behaviors_json='{"submit_once":false,"max_submits":2}',
    )
    row = store.get(res.row["registry_id"])
    assert row["callback_json"] == '{"url":"http://x/api","timeout_s":9}'
    assert row["behaviors_json"] == '{"submit_once":false,"max_submits":2}'


# columns of the ORIGINAL push_registry (pre callback/behaviors), to simulate
# an older DB that _migrate must bring forward without data loss.
_OLD_COLUMNS = (
    "registry_id TEXT PRIMARY KEY", "scene TEXT NOT NULL", "skill TEXT NOT NULL",
    "target_open_id TEXT NOT NULL", "target_union_id TEXT", "profile_name TEXT NOT NULL",
    "chat_id TEXT", "message_id TEXT", "payload_json TEXT", "card_json TEXT",
    "submission_json TEXT", "status TEXT NOT NULL", "business_key TEXT NOT NULL",
    "idempotency_key TEXT", "nonce TEXT", "send_attempts INTEGER NOT NULL DEFAULT 0",
    "write_attempts INTEGER NOT NULL DEFAULT 0", "last_error TEXT", "expires_at INTEGER",
    "created_at INTEGER NOT NULL", "updated_at INTEGER NOT NULL",
)


def test_migration_adds_callback_and_behaviors_columns(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute(f"CREATE TABLE push_registry ({', '.join(_OLD_COLUMNS)})")
    con.execute(
        "INSERT INTO push_registry (registry_id, scene, skill, target_open_id,"
        " profile_name, status, business_key, created_at, updated_at)"
        " VALUES ('pcr_old','dev-acceptance-claim','push-fill-form','ou_x','p',"
        " 'committed','bk', 1, 1)"
    )
    con.commit()
    con.close()

    store = reg.PushRegistryStore(str(db))
    try:
        cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(push_registry)")}
        assert {"callback_json", "behaviors_json"} <= cols  # migrated in
        legacy = store.get("pcr_old")  # legacy row survives, new cols default NULL
        assert legacy is not None and legacy["callback_json"] is None
        # a fresh create with the new overrides works on the migrated DB
        res = store.create(
            scene="dev-acceptance-claim", skill="push-fill-form", target_open_id="ou_y",
            profile_name="p", business_key="dev-acceptance-claim:ou_y:m",
            callback_json='{"url":"http://x/api"}', behaviors_json='{"submit_once":false}',
        )
        got = store.get(res.row["registry_id"])
        assert got["callback_json"] == '{"url":"http://x/api"}'
        assert got["behaviors_json"] == '{"submit_once":false}'
    finally:
        store.close()
