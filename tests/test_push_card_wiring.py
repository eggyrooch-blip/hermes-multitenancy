"""Runtime wiring for the push-card fill loop (review round-1 P0/P1 findings).

These tests prove the components are actually WIRED into the gateway runtime,
not just individually unit-green:

  * end-to-end-loop-unwired (#1): a matched reply really drives merge →
    clarify/confirm card → send, and short-circuits the agent.
  * live-sender-never-wired (#2): the send queue's live sender fires through the
    captured Feishu adapter (and defers, never fails, before capture).
  * sweeps-never-scheduled (#3): the sweep worker runs all three sweeps.
  * expired-disambiguate-notice-undelivered (#6): the已过期/请引用 notice is
    delivered through the adapter and short-circuits.
  * recall-event-not-wired (#7): a message_recalled event recomputes the row.

Everything uses a fake adapter (no live Feishu SDK / network) so the install
path itself is exercised — the patch must attach at load time.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from hermes_multitenancy import push_card_matcher as matcher
from hermes_multitenancy import push_card_metrics as metrics
from hermes_multitenancy import push_card_workers as workers
from hermes_multitenancy import push_registry as reg
from hermes_multitenancy import push_send_queue as sendq

SCENE = "dev-acceptance-claim"
SKILL = "push-fill-form"


@pytest.fixture
def store():
    s = reg.PushRegistryStore(":memory:")
    reg.override_registry_store(s)
    matcher.override_matcher(None)  # rebuild against this store on next get
    metrics.reset()
    yield s
    reg.override_registry_store(None)
    matcher.override_matcher(None)
    sendq.register_push_card_sender(None)
    sendq.note_live_adapter(None)
    sendq._live_adapter = None  # hard reset the captured adapter
    metrics.reset()


def _fresh_adapter_cls():
    """A fresh FeishuAdapter-shaped class each test so the class-level patch is
    installed cleanly (mirrors the real _dispatch_inbound_event / _feishu_send
    / _on_message_recalled surface)."""

    class FakeAdapter:
        def __init__(self):
            self.original_called = False
            self.sent: list[tuple[str, dict]] = []
            self.recall_original_called = False

        async def _dispatch_inbound_event(self, event):
            self.original_called = True
            return None

        async def _feishu_send_with_retry(self, *, chat_id, msg_type, payload, reply_to=None, metadata=None):
            self.sent.append((chat_id, json.loads(payload)))
            return {"message_id": f"om_out_{len(self.sent)}"}

        def _on_message_recalled(self, data):
            self.recall_original_called = True
            return "orig"

    return FakeAdapter


def _pending_row(store, *, open_id="ou_alice", mid="om_card", business_key=None):
    bk = business_key or f"{SCENE}:{open_id}:2026-07-20"
    rid = store.create(scene=SCENE, skill=SKILL, target_open_id=open_id,
                       profile_name=f"{open_id}-profile", business_key=bk).row["registry_id"]
    store.mark_sent(rid, message_id=mid)  # → pending, message_id set
    return rid


# ===================== #1 end-to-end fill loop wired =====================

def test_matcher_dispatch_patch_installs_on_adapter():
    A = _fresh_adapter_cls()
    matcher._patch_dispatch(A)
    assert getattr(A._dispatch_inbound_event, matcher._DISPATCH_FLAG, False)


def test_matched_reply_drives_fill_card_and_short_circuits(store):
    A = _fresh_adapter_cls()
    matcher._patch_dispatch(A)
    adapter = A()
    rid = _pending_row(store)

    ev = SimpleNamespace(sender_open_id="ou_alice",
                         text="打车 上周五 去客户现场花了58", reply_to_message_id="")
    asyncio.run(adapter._dispatch_inbound_event(ev))

    # a clarify/confirm card was SENT to the user's DM (loop actually ran)
    assert len(adapter.sent) == 1
    chat_id, card = adapter.sent[0]
    assert chat_id == "ou_alice"
    assert any(e.get("tag") == "form" for e in card["body"]["elements"])
    # the reply was CONSUMED — it did NOT also become an agent chat turn
    assert adapter.original_called is False
    # row advanced to clarifying with the merged submission persisted
    row = store.get(rid)
    assert row["status"] == reg.STATUS_CLARIFYING
    assert row["submission_json"]
    persisted = json.loads(row["submission_json"])
    assert persisted.get("category") == "打车"  # low-risk field captured
    # the live adapter was captured for later proactive sends
    assert sendq.get_live_adapter() is adapter
    assert metrics.snapshot().get(metrics.FILL_RENDERED) == 1


def test_second_reply_updates_submission_in_place(store):
    A = _fresh_adapter_cls()
    matcher._patch_dispatch(A)
    adapter = A()
    _pending_row(store)

    asyncio.run(adapter._dispatch_inbound_event(
        SimpleNamespace(sender_open_id="ou_alice", text="打车 去客户现场", reply_to_message_id="")))
    asyncio.run(adapter._dispatch_inbound_event(
        SimpleNamespace(sender_open_id="ou_alice", text="发生日期 上周五", reply_to_message_id="")))
    # two fill cards sent, second card carries the accumulated date
    assert len(adapter.sent) == 2
    dumped = json.dumps(adapter.sent[1][1], ensure_ascii=False)
    assert "上周五" in dumped


def test_unmatched_reply_passes_through_to_agent(store):
    A = _fresh_adapter_cls()
    matcher._patch_dispatch(A)
    adapter = A()
    _pending_row(store)  # a pending row exists, but the reply is off-topic chit-chat

    asyncio.run(adapter._dispatch_inbound_event(
        SimpleNamespace(sender_open_id="ou_alice", text="中午吃什么好？", reply_to_message_id="")))
    # nothing sent by the fill loop; the agent path runs normally
    assert adapter.sent == []
    assert adapter.original_called is True


def test_no_pending_row_is_pure_passthrough(store):
    A = _fresh_adapter_cls()
    matcher._patch_dispatch(A)
    adapter = A()
    asyncio.run(adapter._dispatch_inbound_event(
        SimpleNamespace(sender_open_id="ou_nobody", text="打车花了58", reply_to_message_id="")))
    assert adapter.sent == []
    assert adapter.original_called is True


# ===================== #6 expired / disambiguate notices =================

def test_expired_quote_delivers_notice_and_short_circuits(store):
    A = _fresh_adapter_cls()
    matcher._patch_dispatch(A)
    adapter = A()
    rid = store.create(scene=SCENE, skill=SKILL, target_open_id="ou_alice",
                       profile_name="p", business_key=f"{SCENE}:ou_alice:exp",
                       expires_at=reg._now() - 100).row["registry_id"]
    store.mark_sent(rid, message_id="om_exp")
    store.expire_due()  # → expired
    assert store.get(rid)["status"] == reg.STATUS_EXPIRED

    asyncio.run(adapter._dispatch_inbound_event(
        SimpleNamespace(sender_open_id="ou_alice", text="这个还能填吗", reply_to_message_id="om_exp")))
    assert len(adapter.sent) == 1
    assert "过期" in json.dumps(adapter.sent[0][1], ensure_ascii=False)
    assert adapter.original_called is False  # notice short-circuits the agent


def test_disambiguate_delivers_please_quote_notice(store):
    A = _fresh_adapter_cls()
    matcher._patch_dispatch(A)
    adapter = A()
    _pending_row(store, mid="om_a", business_key=f"{SCENE}:ou_alice:a")
    _pending_row(store, mid="om_b", business_key=f"{SCENE}:ou_alice:b")

    asyncio.run(adapter._dispatch_inbound_event(
        SimpleNamespace(sender_open_id="ou_alice", text="好的收到", reply_to_message_id="")))
    assert len(adapter.sent) == 1
    assert "引用" in json.dumps(adapter.sent[0][1], ensure_ascii=False)
    assert adapter.original_called is False


# ===================== #7 recall event wired =============================

def test_recall_patch_installs_and_recomputes(store):
    A = _fresh_adapter_cls()
    matcher._patch_recall(A)
    assert getattr(A._on_message_recalled, matcher._RECALL_FLAG, False)
    adapter = A()

    rid = _pending_row(store, mid="om_r")
    store.advance_status(rid, expect=reg.STATUS_PENDING, to=reg.STATUS_CLARIFYING,
                         submission={"amount": 58, "category": "打车"})
    assert store.get(rid)["submission_json"] is not None

    adapter._on_message_recalled(
        {"event": {"operator_id": {"open_id": "ou_alice"}, "message_id": "om_user_reply"}})
    assert store.get(rid)["submission_json"] is None  # recomputed (wiped → re-extract)
    assert adapter.recall_original_called is True  # original still delegated to


# ===================== #2 live sender wired =============================

def test_live_sender_defers_before_capture_then_sends(store):
    sendq.install_live_push_card_sender()
    sendq._live_adapter = None  # not captured yet
    rid = store.create(scene=SCENE, skill=SKILL, target_open_id="ou_z",
                       profile_name="p", business_key=f"{SCENE}:ou_z:s").row["registry_id"]

    from datetime import datetime
    q = sendq.PushSendQueue(store, clock=lambda: datetime(2026, 7, 20, 10, 0, 0),
                            sleep_fn=lambda _s: asyncio.sleep(0))
    # before capture → deferred (never a false 'failed')
    out = asyncio.run(q.process(rid))
    assert out.status == "deferred" and out.reason == "sender_not_ready"
    assert store.get(rid)["status"] == reg.STATUS_SENDING

    # capture a live adapter → the same queue now sends through it
    A = _fresh_adapter_cls()
    adapter = A()
    sendq.note_live_adapter(adapter)
    out2 = asyncio.run(q.process(rid))
    assert out2.status == "sent"
    assert store.get(rid)["status"] == reg.STATUS_PENDING
    assert adapter.sent and adapter.sent[0][0] == "ou_z"


# ===================== #3 sweeps scheduled =============================

def test_run_all_sweeps_runs_three_and_expires_due(store):
    rid = store.create(scene=SCENE, skill=SKILL, target_open_id="ou_alice",
                       profile_name="p", business_key=f"{SCENE}:ou_alice:e",
                       expires_at=reg._now() - 100).row["registry_id"]
    store.mark_sent(rid, message_id="m")  # pending, past expiry
    res = asyncio.run(workers.run_all_sweeps(store))
    assert set(res) == {"expiry", "redrive", "reconcile"}
    assert res["expiry"]["expired"] == 1
    assert store.get(rid)["status"] == reg.STATUS_EXPIRED


def test_sweep_worker_starts_alive_and_is_idempotent(store):
    try:
        workers.ensure_push_card_sweeps_started(interval=3600)
        t1 = workers._sweep_thread
        assert t1 is not None and t1.is_alive()
        workers.ensure_push_card_sweeps_started(interval=3600)  # idempotent
        assert workers._sweep_thread is t1
    finally:
        workers.stop_push_card_sweeps()
    assert workers._sweep_thread is None
