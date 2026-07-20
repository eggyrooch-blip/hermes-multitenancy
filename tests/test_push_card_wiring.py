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
from hermes_multitenancy import push_card_routes as routes
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
            self.patched: list[tuple[str, dict]] = []  # in-place card.update calls
            self.recall_original_called = False
            parent = self

            class _Msg:
                def patch(self, request):
                    parent.patched.append(
                        (request.message_id, json.loads(request.request_body.content)))
                    return {"code": 0}

            self._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=_Msg())))

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

    # the pushed card (message_id present) is UPDATED IN PLACE into the confirm
    # form — not a second card (finding confirm-card-no-in-place-update).
    assert adapter.sent == []
    assert len(adapter.patched) == 1
    patched_mid, card = adapter.patched[0]
    assert patched_mid == "om_card"  # patched the original card by its message_id
    assert any(e.get("tag") == "form" for e in card["body"]["elements"])
    # the reply was CONSUMED — it did NOT also become an agent chat turn
    assert adapter.original_called is False
    # row advanced to clarifying with the merged submission persisted; message_id
    # unchanged (in-place update keeps the same card).
    row = store.get(rid)
    assert row["status"] == reg.STATUS_CLARIFYING
    assert row["message_id"] == "om_card"
    assert row["submission_json"]
    persisted = json.loads(row["submission_json"])
    assert persisted.get("category") == "打车"  # low-risk field captured
    # the live adapter was captured for later proactive sends
    assert sendq.get_live_adapter() is adapter
    assert metrics.snapshot().get(metrics.FILL_RENDERED) == 1


def test_inline_card_row_drives_fill_loop_via_scene_spec(store):
    """An INLINE card row (scene name NOT registered; full spec carried on
    ``scene_spec_json``) drives the same merge → confirm-card → send loop —
    routing reconstructs the scene from the row via ``scene_def_for_row``, not a
    named registry lookup. This is the 路线A self-serve闭环 (非命名查)."""
    from hermes_multitenancy import push_scenes as scenes
    A = _fresh_adapter_cls()
    matcher._patch_dispatch(A)
    adapter = A()
    spec = scenes.scene_to_spec_json(scenes.scene_from_payload({
        "skill": "atom-claim", "mode": "card",
        "fields": [
            {"key": "amount", "label": "金额", "type": "number", "required": True, "risk": "high"},
            {"key": "category", "label": "类目", "type": "enum", "options": ["打车", "餐饮"]},
        ],
    }))
    assert scenes.get_scene("inline:atom-claim") is None  # unregistered scene
    rid = store.create(scene="inline:atom-claim", skill="atom-claim",
                       target_open_id="ou_alice", profile_name="ou_alice-profile",
                       business_key="inline:atom-claim:ou_alice:2026-07-20",
                       scene_spec_json=spec).row["registry_id"]
    store.mark_sent(rid, message_id="om_inline_card")

    asyncio.run(adapter._dispatch_inbound_event(SimpleNamespace(
        sender_open_id="ou_alice", text="打车花了58", reply_to_message_id="")))

    assert adapter.sent == []
    assert len(adapter.patched) == 1  # inline card row updated in place too
    _, card = adapter.patched[0]
    assert any(e.get("tag") == "form" for e in card["body"]["elements"])  # confirm form rendered
    assert adapter.original_called is False  # reply consumed, not an agent turn
    row = store.get(rid)
    assert row["status"] == reg.STATUS_CLARIFYING
    assert json.loads(row["submission_json"]).get("category") == "打车"


def test_second_reply_updates_submission_in_place(store):
    A = _fresh_adapter_cls()
    matcher._patch_dispatch(A)
    adapter = A()
    _pending_row(store)

    asyncio.run(adapter._dispatch_inbound_event(
        SimpleNamespace(sender_open_id="ou_alice", text="打车 去客户现场", reply_to_message_id="")))
    asyncio.run(adapter._dispatch_inbound_event(
        SimpleNamespace(sender_open_id="ou_alice", text="发生日期 上周五", reply_to_message_id="")))
    # the SAME card is updated in place twice (no stacking); second carries the date
    assert adapter.sent == []
    assert len(adapter.patched) == 2
    assert adapter.patched[0][0] == adapter.patched[1][0] == "om_card"  # same card
    dumped = json.dumps(adapter.patched[1][1], ensure_ascii=False)
    assert "上周五" in dumped


def test_fill_step_falls_back_to_new_card_and_repoints_message_id(store):
    # When the in-place card.update fails, the loop sends a NEW card and CAS-points
    # registry.message_id at it so a quoted reply follows the latest form card.
    A = _fresh_adapter_cls()
    adapter = A()

    def _failing_patch(request):
        raise RuntimeError("patch boom")

    adapter._client.im.v1.message.patch = _failing_patch
    matcher._patch_dispatch(A)
    rid = _pending_row(store)

    asyncio.run(adapter._dispatch_inbound_event(
        SimpleNamespace(sender_open_id="ou_alice", text="打车花了58", reply_to_message_id="")))

    assert len(adapter.sent) == 1  # fell back to a new card
    _, card = adapter.sent[0]
    assert any(e.get("tag") == "form" for e in card["body"]["elements"])
    row = store.get(rid)
    assert row["message_id"] == "om_out_1"  # re-pointed to the new form card
    assert row["status"] == reg.STATUS_CLARIFYING


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


# ===== #1 cold-start: capture the Feishu adapter WITHOUT any inbound =====

def test_gateway_capture_grabs_feishu_adapter_without_inbound(store):
    """The system pushes proactively before the user ever messages the bot —
    the adapter must be captured at gateway startup, not on first inbound."""
    A = _fresh_adapter_cls()
    captured = A()  # has _feishu_send_with_retry → is a Feishu adapter

    class FakeRunner:
        def _create_adapter(self, platform, config):
            return captured

    assert sendq._patch_gateway_create_adapter(FakeRunner) is True
    assert sendq.get_live_adapter() is None  # nothing spoke to the bot yet

    # gateway builds the Feishu adapter at startup — NO inbound event
    out = FakeRunner()._create_adapter("feishu", object())
    assert out is captured
    assert sendq.get_live_adapter() is captured


def test_gateway_capture_ignores_non_feishu_adapter(store):
    class FakeRunner:
        def _create_adapter(self, platform, config):
            return object()  # telegram/slack: no _feishu_send_with_retry

    sendq._patch_gateway_create_adapter(FakeRunner)
    FakeRunner()._create_adapter("telegram", object())
    assert sendq.get_live_adapter() is None


def test_gateway_capture_patch_is_idempotent(store):
    class FakeRunner:
        def _create_adapter(self, platform, config):
            return None

    first = sendq._patch_gateway_create_adapter(FakeRunner)
    wrapped = FakeRunner._create_adapter
    assert first is True
    assert sendq._patch_gateway_create_adapter(FakeRunner) is True
    assert FakeRunner._create_adapter is wrapped  # not re-wrapped


# ===== target resolver uses RoutingTable (guards RoutingStore ImportError) =====

def test_default_target_resolver_resolves_profile(monkeypatch, tmp_path):
    """_default_target_resolver must construct the real RoutingTable and resolve
    a seeded user → profile_name. Guards the RoutingStore ImportError regression
    (the class is RoutingTable, not RoutingStore)."""
    from hermes_multitenancy import routing

    db = tmp_path / "multitenancy.db"
    monkeypatch.setattr(routing, "DEFAULT_DB_PATH", db)  # resolver builds RoutingTable()
    seed = routing.RoutingTable(db)
    seed.upsert(user_id="u_x", profile_name="feishu_x", open_id="ou_x",
                union_id="on_x", provenance="sync")
    seed.close()

    target = routes._default_target_resolver(open_id="ou_x", union_id="")
    assert target is not None
    assert target["profile_name"] == "feishu_x"
    assert target["open_id"] == "ou_x"

    # union_id-only fallback path also resolves
    by_union = routes._default_target_resolver(open_id="", union_id="on_x")
    assert by_union is not None and by_union["profile_name"] == "feishu_x"

    # user_id path resolves too (e.g. caller holds "sunke", not an ou_ open_id)
    by_user = routes._default_target_resolver(open_id="", union_id="", user_id="u_x")
    assert by_user is not None and by_user["profile_name"] == "feishu_x"
    assert by_user["open_id"] == "ou_x"

    # unknown target → None → the endpoint 4xx's
    assert routes._default_target_resolver(open_id="ou_ghost", union_id="") is None


# ===== run-broker process: no live sender → DEFER, never failed =====

def test_process_defers_when_no_sender_in_process(store):
    """notify-card can dispatch the send from the run-broker process, which owns
    NO live Feishu adapter — _default_sender raises the BASE
    PushCardSenderUnavailable (not the NotReady subclass). process() must DEFER
    (row stays 'sending'), never 'failed'. Regression for 8af5237."""
    from datetime import datetime

    sendq.register_push_card_sender(None)  # no sender in this process
    rid = store.create(scene=SCENE, skill=SKILL, target_open_id="ou_rb",
                       profile_name="p", business_key=f"{SCENE}:ou_rb:rb").row["registry_id"]

    # default sender (_default_sender) → base PushCardSenderUnavailable
    q = sendq.PushSendQueue(store, clock=lambda: datetime(2026, 7, 20, 10, 0, 0),
                            sleep_fn=lambda _s: asyncio.sleep(0))
    out = asyncio.run(q.process(rid))
    assert out.status == "deferred" and out.reason == "sender_not_ready"
    assert store.get(rid)["status"] == reg.STATUS_SENDING  # NOT failed


# ===== #2 open_id direct-send path (feeds cron's _patch_feishu_open_id_send) =====

def test_send_push_card_via_adapter_targets_open_id(store):
    """The proactive send hands the ou_ open_id as the receive target with
    reply_to=None — exactly what cron's _patch_feishu_open_id_send keys on to
    route a DM via im.v1.message.create('open_id', ...)."""
    seen: dict = {}

    class Rec:
        async def _feishu_send_with_retry(self, *, chat_id, msg_type, payload,
                                          reply_to=None, metadata=None):
            seen.update(chat_id=chat_id, msg_type=msg_type, reply_to=reply_to)
            return {"message_id": "om_direct_1"}

    card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "hi"}]}}
    res = asyncio.run(sendq.send_push_card_via_adapter(Rec(), open_id="ou_direct", card=card))
    assert res.message_id == "om_direct_1"
    assert seen["chat_id"] == "ou_direct"   # open_id is the receive target
    assert seen["reply_to"] is None          # → triggers the open_id (not reply) path
    assert seen["msg_type"] == "interactive"


# ===================== yolo mode: route into skill, no form =====================

YOLO_SCENE = "dev-yolo-echo"
YOLO_SKILL = "push-yolo-echo"


def _pending_yolo_row(store, *, open_id="ou_alice", mid="om_ycard"):
    rid = store.create(scene=YOLO_SCENE, skill=YOLO_SKILL, target_open_id=open_id,
                       profile_name=f"{open_id}-profile",
                       business_key=f"{YOLO_SCENE}:{open_id}:2026-07-20").row["registry_id"]
    store.mark_sent(rid, message_id=mid)  # → pending, message_id set
    return rid


def test_yolo_reply_injects_skill_slash_and_passes_through(store):
    """A quoted reply to a yolo-mode card rewrites event.text to ``/{skill}
    <reply>`` and PASSES THROUGH (no form card, no short-circuit) so the normal
    agent turn runs the business skill — the skill self-drives compute/确认/
    execute. The row advances pending→clarifying so a multi-turn yolo
    conversation keeps matching."""
    A = _fresh_adapter_cls()
    matcher._patch_dispatch(A)
    adapter = A()
    rid = _pending_yolo_row(store)

    ev = SimpleNamespace(sender_open_id="ou_alice",
                         text="1-10日2.3元 其他1.9元", reply_to_message_id="om_ycard")
    asyncio.run(adapter._dispatch_inbound_event(ev))

    # NO framework form card was sent — yolo never renders a form
    assert adapter.sent == []
    # the message was NOT consumed — it flows on to the normal agent turn
    assert adapter.original_called is True
    # event.text was rewritten to the scene's business-skill slash
    assert ev.text == "/push-yolo-echo 1-10日2.3元 其他1.9元"
    # the row stays open (advanced to clarifying) so the next reply keeps routing
    assert store.get(rid)["status"] == reg.STATUS_CLARIFYING
    assert metrics.snapshot().get(metrics.YOLO_ROUTED) == 1


def test_yolo_multi_turn_keeps_routing(store):
    """A second yolo reply (row already clarifying) still injects the skill —
    the whole rules→计算→「覆盖」→execute conversation routes every turn."""
    A = _fresh_adapter_cls()
    matcher._patch_dispatch(A)
    adapter = A()
    _pending_yolo_row(store)

    asyncio.run(adapter._dispatch_inbound_event(
        SimpleNamespace(sender_open_id="ou_alice", text="规则如上", reply_to_message_id="om_ycard")))
    ev2 = SimpleNamespace(sender_open_id="ou_alice", text="覆盖", reply_to_message_id="om_ycard")
    asyncio.run(adapter._dispatch_inbound_event(ev2))

    assert adapter.sent == []                       # never a form
    assert ev2.text == "/push-yolo-echo 覆盖"        # "覆盖" confirm still routes to skill
    assert adapter.original_called is True
