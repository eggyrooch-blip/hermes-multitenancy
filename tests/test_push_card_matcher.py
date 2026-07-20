"""Inbound matcher + expiry/reconcile workers + telemetry (SPEC P3/P6).

Pure logic only — in-memory store, injected heuristics/seams, fake clocks, a
no-op sleep. No live Feishu adapter (the wiring at the bottom of
push_card_matcher is fail-open and exercised in the dev script, not here).
"""
from __future__ import annotations

import asyncio

import pytest

from hermes_multitenancy import push_card_matcher as m
from hermes_multitenancy import push_card_metrics as metrics
from hermes_multitenancy import push_card_workers as workers
from hermes_multitenancy import push_registry as reg


@pytest.fixture
def store():
    s = reg.PushRegistryStore(":memory:")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _clean_metrics():
    metrics.reset()
    yield
    metrics.reset()


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
    return store.create(**kw).row


def _deliver(store, row, *, mid="om_msg1"):
    """sending → pending with a message_id, as the send queue would."""
    assert store.mark_sent(row["registry_id"], message_id=mid)
    return store.get(row["registry_id"])


def _matcher(store, **kw):
    return m.PushCardMatcher(store, **kw)


# --- heuristic intent gate ----------------------------------------------

def test_heuristic_scene_signal_is_yes():
    sc = __import__("hermes_multitenancy.push_scenes", fromlist=["get_scene"]).get_scene(
        "dev-acceptance-claim"
    )
    # amount number + category option + reason label all present.
    assert m.default_heuristic_intent("今天打车去客户现场花了58", sc) == "yes"


def test_heuristic_escape_phrase_is_no():
    sc = __import__("hermes_multitenancy.push_scenes", fromlist=["get_scene"]).get_scene(
        "dev-acceptance-claim"
    )
    assert m.default_heuristic_intent("不是", sc) == "no"
    assert m.default_heuristic_intent("取消", sc) == "no"


def test_heuristic_chitchat_question_is_no():
    sc = __import__("hermes_multitenancy.push_scenes", fromlist=["get_scene"]).get_scene(
        "dev-acceptance-claim"
    )
    assert m.default_heuristic_intent("中午吃什么好？", sc) == "no"


def test_heuristic_ambiguous_is_maybe():
    sc = __import__("hermes_multitenancy.push_scenes", fromlist=["get_scene"]).get_scene(
        "dev-acceptance-claim"
    )
    assert m.default_heuristic_intent("好的收到", sc) == "maybe"


# --- responsibility chain: quoted hit -----------------------------------

def test_quoted_hit_routes_to_that_row(store):
    row = _create(store)
    row = _deliver(store, row, mid="om_A")
    d = _matcher(store).match(open_id="ou_alice", text="随便说点啥", quoted_message_id="om_A")
    assert d.action == m.ROUTE
    assert d.registry_id == row["registry_id"]
    assert d.slash_content == "/push-fill-form 随便说点啥"
    assert d.context["registry_id"] == row["registry_id"]


def test_quoted_expired_gets_explicit_exit(store):
    row = _create(store, expires_at=1)  # already past
    _deliver(store, row, mid="om_exp")
    assert store.expire_due(now=1000) == 1
    d = _matcher(store).match(open_id="ou_alice", text="继续", quoted_message_id="om_exp")
    assert d.action == m.EXPIRED_EXIT
    assert "过期" in d.reply_text
    assert metrics.snapshot().get(metrics.MATCH_EXPIRED_EXIT) == 1


def test_quoted_other_users_card_is_passthrough(store):
    row = _create(store)
    _deliver(store, row, mid="om_A")
    d = _matcher(store).match(open_id="ou_bob", text="x", quoted_message_id="om_A")
    assert d.action == m.PASSTHROUGH


def test_quoted_unknown_message_is_passthrough(store):
    d = _matcher(store).match(open_id="ou_alice", text="x", quoted_message_id="om_nope")
    assert d.action == m.PASSTHROUGH


# --- unquoted single open row -------------------------------------------

def test_single_open_related_reply_routes(store):
    row = _create(store)
    _deliver(store, row)
    d = _matcher(store).match(open_id="ou_alice", text="打车花了58")
    assert d.action == m.ROUTE
    assert d.context["escape_hint"]  # first engagement carries 逃生门
    assert metrics.snapshot().get(metrics.MATCH_HIT) == 1


def test_single_open_escape_phrase_passthrough_and_counts(store):
    row = _create(store)
    _deliver(store, row)
    d = _matcher(store).match(open_id="ou_alice", text="不是")
    assert d.action == m.PASSTHROUGH
    assert metrics.snapshot().get(metrics.ESCAPE_HATCH) == 1


def test_single_open_chitchat_passthrough(store):
    row = _create(store)
    _deliver(store, row)
    d = _matcher(store).match(open_id="ou_alice", text="中午吃什么好？")
    assert d.action == m.PASSTHROUGH


def test_ambiguous_pending_is_conservative_passthrough(store):
    row = _create(store)
    _deliver(store, row)  # pending, not yet engaged
    d = _matcher(store).match(open_id="ou_alice", text="好的收到")
    assert d.action == m.PASSTHROUGH


def test_ambiguous_clarifying_leans_route(store):
    row = _create(store)
    row = _deliver(store, row)
    assert store.advance_status(row["registry_id"], expect=reg.STATUS_PENDING, to=reg.STATUS_CLARIFYING)
    d = _matcher(store).match(open_id="ou_alice", text="好的收到")
    assert d.action == m.ROUTE
    assert "escape_hint" not in d.context  # not first engagement


def test_llm_gate_consulted_only_on_maybe(store):
    row = _create(store)
    _deliver(store, row)
    calls = []

    def llm(text, scene, r):
        calls.append(text)
        return True

    d = _matcher(store, llm_intent=llm).match(open_id="ou_alice", text="好的收到")
    assert d.action == m.ROUTE
    assert calls == ["好的收到"]
    # A clear 'yes' must NOT consult the LLM (cost control P1-7).
    calls.clear()
    _matcher(store, llm_intent=llm).match(open_id="ou_alice", text="打车58")
    assert calls == []


# --- unquoted multiple open rows → disambiguate --------------------------

def test_multi_open_disambiguates_by_signal(store):
    a = _create(store, business_key="dev-acceptance-claim:ou_alice:d1")
    b = _create(store, business_key="dev-acceptance-claim:ou_alice:d2")
    _deliver(store, a, mid="om_a")
    _deliver(store, b, mid="om_b")
    # Two open rows, ambiguous reply → cannot disambiguate → ask to quote.
    d = _matcher(store).match(open_id="ou_alice", text="好的")
    assert d.action == m.DISAMBIGUATE
    assert metrics.snapshot().get(metrics.MATCH_DISAMBIGUATE) == 1


# --- grace retry for the send race --------------------------------------

def test_grace_retry_when_fresh_sending_row(store):
    _create(store)  # still 'sending', no message_id, just created
    d = _matcher(store).match(open_id="ou_alice", text="打车58", now=reg._now())
    assert d.action == m.RETRY_GRACE
    assert metrics.snapshot().get(metrics.GRACE_RETRY) == 1


def test_match_with_grace_rereads_after_flip(store):
    row = _create(store)

    async def go():
        flips = {"n": 0}

        async def fake_sleep(_s):
            # Simulate the send queue flipping sending→pending during the wait.
            _deliver(store, row)
            flips["n"] += 1

        return await _matcher(store).match_with_grace(
            open_id="ou_alice", text="打车58", sleep_fn=fake_sleep
        )

    d = asyncio.run(go())
    assert d.action == m.ROUTE


def test_no_open_no_fresh_is_passthrough(store):
    d = _matcher(store).match(open_id="ou_ghost", text="打车58")
    assert d.action == m.PASSTHROUGH


# --- recall recompute ----------------------------------------------------

def test_recall_resets_submission(store):
    row = _create(store)
    row = _deliver(store, row)
    store.advance_status(
        row["registry_id"], expect=reg.STATUS_PENDING, to=reg.STATUS_CLARIFYING,
        submission={"amount": 58},
    )
    assert store.get(row["registry_id"])["submission_json"] is not None
    rid = m.handle_message_recall(store, open_id="ou_alice", recalled_message_id="om_x")
    assert rid == row["registry_id"]
    assert store.get(row["registry_id"])["submission_json"] is None
    assert metrics.snapshot().get(metrics.RECALL_RECOMPUTE) == 1


def test_recall_noop_when_multiple_open(store):
    a = _create(store, business_key="k1")
    b = _create(store, business_key="k2")
    _deliver(store, a, mid="1")
    _deliver(store, b, mid="2")
    assert m.handle_message_recall(store, open_id="ou_alice", recalled_message_id="z") is None


# --- P6 expiry worker ----------------------------------------------------

def test_expiry_sweep_expires_and_updates_card(store):
    row = _create(store, expires_at=1)
    _deliver(store, row, mid="om_e")
    updated = []

    async def updater(r):
        updated.append(r["registry_id"])
        return True

    counters = asyncio.run(workers.run_expiry_sweep(store, card_updater=updater, now=1000))
    assert counters == {"due": 1, "expired": 1, "card_failed": 0}
    assert updated == [row["registry_id"]]
    assert store.get(row["registry_id"])["status"] == reg.STATUS_EXPIRED
    assert metrics.snapshot().get(metrics.EXPIRE_SWEPT) == 1


def test_expiry_sweep_alerts_but_still_expires_on_card_failure(store):
    row = _create(store, expires_at=1)
    _deliver(store, row, mid="om_e")

    async def updater(r):
        return False  # renderer failed

    counters = asyncio.run(workers.run_expiry_sweep(store, card_updater=updater, now=1000))
    assert counters["card_failed"] == 1
    assert counters["expired"] == 1  # state flip happens regardless
    assert metrics.snapshot().get(metrics.EXPIRE_CARD_FAILED) == 1


def test_expiry_sweep_leaves_unexpired_rows(store):
    row = _create(store, expires_at=10_000)
    _deliver(store, row, mid="om_live")
    counters = asyncio.run(workers.run_expiry_sweep(store, now=1000))
    assert counters["due"] == 0
    assert store.get(row["registry_id"])["status"] == reg.STATUS_PENDING


# --- P6 reconcile worker -------------------------------------------------

def _confirm(store, row):
    assert store.advance_status(
        row["registry_id"], expect=reg.STATUS_PENDING, to=reg.STATUS_CLARIFYING
    )
    assert store.advance_status(
        row["registry_id"], expect=reg.STATUS_CLARIFYING, to=reg.STATUS_CONFIRMED
    )


def test_reconcile_backfills_committed_when_backend_has_record(store):
    row = _create(store)
    row = _deliver(store, row)
    _confirm(store, row)

    async def lookup(r):
        assert r["registry_id"] == row["registry_id"]
        return {"amount": 68}  # backend already has it

    # updated_at is 'now'; use a future now so it's past the window.
    future = reg._now() + workers.RECONCILE_AFTER_S + 10
    counters = asyncio.run(
        workers.run_reconcile_sweep(store, backend_lookup=lookup, now=future)
    )
    assert counters["committed"] == 1
    assert store.get(row["registry_id"])["status"] == reg.STATUS_COMMITTED
    assert metrics.snapshot().get(metrics.RECONCILE_COMMITTED) == 1
    assert metrics.snapshot().get(metrics.COMMIT_LATENCY_COUNT) == 1


def test_reconcile_never_rewrites_when_backend_missing(store):
    row = _create(store)
    row = _deliver(store, row)
    _confirm(store, row)

    async def lookup(r):
        return None  # backend does NOT have it → do not blindly retry

    future = reg._now() + workers.RECONCILE_AFTER_S + 10
    counters = asyncio.run(
        workers.run_reconcile_sweep(store, backend_lookup=lookup, now=future)
    )
    assert counters["still_missing"] == 1
    # Stays confirmed — the write path (P5), not the reconciler, re-submits.
    assert store.get(row["registry_id"])["status"] == reg.STATUS_CONFIRMED
    assert metrics.snapshot().get(metrics.RECONCILE_MISSING) == 1


def test_reconcile_skips_rows_inside_window(store):
    row = _create(store)
    row = _deliver(store, row)
    _confirm(store, row)
    seen = []

    async def lookup(r):
        seen.append(r["registry_id"])
        return None

    # now == updated_at → not yet older_than the window.
    counters = asyncio.run(
        workers.run_reconcile_sweep(store, backend_lookup=lookup, now=reg._now())
    )
    assert counters["checked"] == 0
    assert seen == []


def test_reconcile_noop_without_backend_seam(store):
    row = _create(store)
    row = _deliver(store, row)
    _confirm(store, row)
    future = reg._now() + workers.RECONCILE_AFTER_S + 10
    counters = asyncio.run(workers.run_reconcile_sweep(store, now=future))
    assert counters == {"checked": 0, "committed": 0, "still_missing": 0}
