#!/usr/bin/env python3
"""Synthetic B-group replay for the push-card fill loop (design §5.3 step 12).

The 12-step dev script runs steps 1-11 on a real Feishu 开发号; step 12 is the
"合成层" B-group — the failure/abuse/scale cases that 10-person真机 can never
cover. This script forges inbound message events and card-action callback报文
and drives them through the REAL pipeline entry points (no live Feishu SDK,
no network), asserting the design's hard invariants:

  B1 非本人确认拒   — a card callback signed by someone other than target本人
                      is rejected; kep零写入; row untouched.
  B2 nonce 重放拒   — a callback carrying a stale/forged nonce is rejected;
                      a replay AFTER commit no-ops → backend still恰好一条.
  B3 business_key 409 — a second in-flight POST for the same scene:open_id:day
                      returns 409 + the ORIGINAL registry_id (no second card).
  B4 多 pending 消歧 — two open rows + an ambiguous reply → "请引用" (先消歧再拒);
                      a QUOTED reply resolves to exactly the quoted row.
  B5 崩溃对账       — a crash after the backend write but before the commit CAS
                      leaves the row `confirmed`; the reconcile worker backfills
                      `committed` WITHOUT re-writing → backend恰好一条.
  B6 撤回重算       — a message-recall event wipes the extracted submission so
                      the fill skill re-extracts (replay, not pure-accumulate).
  B7 200 突发限流    — a 200-user burst is all accepted 202; the rate-limited
                      send queue drains every one (0 failed) under token-bucket
                      pacing (队列平滑, not synchronous直发).
  B8 静默顺延       — a send during quiet hours (21:00–09:00) is DEFERRED, the
                      row stays `sending`, and the adapter is never called.

Also proves the regression guard (design §2.5): a card callback that is NOT
`push_confirm` is delegated UNCHANGED to the original handler — the 5th patch
never吞 the other four's events.

Standalone: overrides the module singletons onto an in-memory store, restores
them in `finally`. Run with the repo venv::

    cd <worktree> && source .ftask/push-card-fill-loop/.env
    .venv/bin/python scripts/sim_push_card_b_group.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from hermes_multitenancy import push_card_confirm as confirm
from hermes_multitenancy import push_card_matcher as matcher
from hermes_multitenancy import push_card_metrics as metrics
from hermes_multitenancy import push_card_routes as routes
from hermes_multitenancy import push_card_workers as workers
from hermes_multitenancy import push_registry as reg
from hermes_multitenancy import push_scenes as scenes
from hermes_multitenancy import push_send_queue as sendq

SCENE = "dev-acceptance-claim"
SKILL = "push-fill-form"

_FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _FAILURES.append(label)


# --- forged report builders (mirror the real Feishu shapes) --------------

def card_callback(
    *, registry_id: str, nonce: str, operator_open_id: str,
    form_value: dict | None = None, hermes_action: str = "push_confirm",
    extra_value: dict | None = None,
) -> dict:
    """A card.action.trigger callback报文 (design §2.5 value编码 registry+nonce)."""
    value = {"hermes_action": hermes_action, "registry_id": registry_id,
             "nonce": nonce, "operation_id": "op_" + registry_id[-4:]}
    if extra_value:
        value.update(extra_value)
    return {
        "event": {
            "operator": {"open_id": operator_open_id},
            "action": {
                "value": value,
                "form_value": form_value if form_value is not None else {
                    "amount": 68, "date": "上周五", "category": "打车", "reason": "去客户现场",
                },
            },
        }
    }


def inbound_message(*, open_id: str, text: str, quoted: str = "") -> SimpleNamespace:
    """A forged inbound DM event, shaped like what the matcher patch reads."""
    return SimpleNamespace(sender_open_id=open_id, text=text, reply_to_message_id=quoted)


class FakeFeishuAdapter:
    """Minimal adapter carrying the base card-action handler the real 5th patch
    chains onto — lets us replay a callback报文 through the ACTUAL wrapped
    `_on_card_action_trigger`, including the undefined-放行 delegation."""

    DELEGATE_SENTINEL = {"kind": "delegated-to-original"}

    def _on_card_action_trigger(self, data):  # noqa: D401 - the "original"
        return dict(self.DELEGATE_SENTINEL)


def replay_inbound(store, ev: SimpleNamespace):
    """Run the matcher's real inbound body (open_id/text/quote extraction → match)."""
    open_id = matcher._event_open_id(ev)
    text = matcher._event_text(ev)
    quoted = str(getattr(ev, "reply_to_message_id", "") or "")
    return matcher.PushCardMatcher(store).match(
        open_id=open_id, text=text, quoted_message_id=quoted
    )


# --- row helpers ---------------------------------------------------------

def make_clarifying(store, *, open_id="ou_alice", business_key=None, mid="om_card"):
    """A delivered card walked to `clarifying` (post fill-skill), as B1/B2/B5 need."""
    bk = business_key or f"{SCENE}:{open_id}:2026-07-20"
    rid = store.create(scene=SCENE, skill=SKILL, target_open_id=open_id,
                       profile_name=f"{open_id}-profile", business_key=bk).row["registry_id"]
    store.mark_sent(rid, message_id=mid)
    store.advance_status(rid, expect=reg.STATUS_PENDING, to=reg.STATUS_CLARIFYING)
    return rid, store.get(rid)["nonce"]


# ========================= B1 / B2 / regression ==========================

def phase_confirm_guards(store):
    print("\n[B1] 非本人确认拒 — callback signed by a non-owner")
    # Install the REAL 5th patch on a fake adapter and replay through it.
    installed = confirm._patch_card_action(FakeFeishuAdapter)
    check(installed, "[B1] 5th card-action patch installed on adapter")
    adapter = FakeFeishuAdapter()
    reg.override_registry_store(store)
    confirm.override_writer("kep-pre-claim-writer", confirm.MockKepPreClaimWriter())
    writer = confirm.get_writer("kep-pre-claim-writer")

    rid, nonce = make_clarifying(store, open_id="ou_alice", mid="om_b1")
    resp = adapter._on_card_action_trigger(
        card_callback(registry_id=rid, nonce=nonce, operator_open_id="ou_mallory")
    )
    check(writer.write_calls == 0, "[B1] non-owner triggered ZERO backend writes")
    check(store.get(rid)["status"] == reg.STATUS_CLARIFYING, "[B1] row untouched (still clarifying)")
    check("本人" in json.dumps(resp, ensure_ascii=False), "[B1] toast tells non-owner 只有本人可提交")

    print("\n[B2] nonce 重放拒 — stale/forged nonce, then replay-after-commit")
    rid2, nonce2 = make_clarifying(store, open_id="ou_bob",
                                   business_key=f"{SCENE}:ou_bob:2026-07-20", mid="om_b2")
    # (a) forged nonce → reject, no write
    adapter._on_card_action_trigger(
        card_callback(registry_id=rid2, nonce="forged-" + nonce2, operator_open_id="ou_bob")
    )
    check(writer.write_calls == 0, "[B2a] forged-nonce callback wrote NOTHING")
    check(store.get(rid2)["status"] == reg.STATUS_CLARIFYING, "[B2a] row still clarifying")
    # (b) legit confirm commits exactly once; then a REPLAY of that same报文 no-ops
    adapter._on_card_action_trigger(
        card_callback(registry_id=rid2, nonce=nonce2, operator_open_id="ou_bob")
    )
    check(store.get(rid2)["status"] == reg.STATUS_COMMITTED, "[B2b] owner+nonce commits")
    check(writer.write_calls == 1 and len(writer.records) == 1, "[B2b] backend恰好一条 after commit")
    resp_replay = adapter._on_card_action_trigger(  # nonce cleared on commit → replay no-ops
        card_callback(registry_id=rid2, nonce=nonce2, operator_open_id="ou_bob")
    )
    check(writer.write_calls == 1 and len(writer.records) == 1,
          "[B2b] replay-after-commit still恰好一条 (no second write)")
    check("已录入" in json.dumps(resp_replay, ensure_ascii=False), "[B2b] replay toast=已录入,勿重复")

    print("\n[regression] a non-push_confirm callback is delegated UNCHANGED")
    other = card_callback(registry_id="x", nonce="y", operator_open_id="ou_alice",
                          hermes_action="cred_auth")
    resp_other = adapter._on_card_action_trigger(other)
    check(resp_other == FakeFeishuAdapter.DELEGATE_SENTINEL,
          "[regression] foreign callback → original handler, patch never吞它")
    check(writer.write_calls == 1, "[regression] foreign callback triggered no push write")


# ============================== B3: 409 ==================================

def phase_business_key_409():
    print("\n[B3] business_key 撞 → 409 + 原 registry_id (real /notify-card endpoint)")

    async def run():
        from aiohttp.test_utils import TestClient, TestServer
        from hermes_multitenancy.webui_broker_server import create_run_broker_app

        app = create_run_broker_app(dispatch_agent=lambda r: "noop",
                                    mark_seen=lambda _r: True, sandbox_available=lambda: True)
        client = TestClient(TestServer(app))
        await client.start_server()
        hdr = {"Authorization": "Bearer push-key-1"}
        body = {"open_id": "ou_alice", "scene": SCENE,
                "business_key": f"{SCENE}:ou_alice:collide"}
        try:
            r1 = await client.post("/api/run-broker/notify-card", json=body, headers=hdr)
            j1 = json.loads(await r1.text())
            r2 = await client.post("/api/run-broker/notify-card", json=body, headers=hdr)
            j2 = json.loads(await r2.text())
            return (r1.status, j1), (r2.status, j2)
        finally:
            await client.close()

    (s1, j1), (s2, j2) = asyncio.run(run())
    check(s1 == 202 and j1.get("registry_id"), f"[B3] first push accepted 202 ({s1})")
    check(s2 == 409, f"[B3] second same-business_key push → 409 (got {s2})")
    check(j2.get("registry_id") == j1.get("registry_id"),
          "[B3] 409 returns the ORIGINAL registry_id (no second card)")


# ============================ B4: 消歧 ===================================

def phase_disambiguate(store):
    print("\n[B4] 多 pending 消歧 — ambiguous → 请引用; quoted → routes to exactly one")
    a = store.create(scene=SCENE, skill=SKILL, target_open_id="ou_carol",
                     profile_name="carol", business_key=f"{SCENE}:ou_carol:d1").row
    b = store.create(scene=SCENE, skill=SKILL, target_open_id="ou_carol",
                     profile_name="carol", business_key=f"{SCENE}:ou_carol:d2").row
    store.mark_sent(a["registry_id"], message_id="om_card_a")
    store.mark_sent(b["registry_id"], message_id="om_card_b")

    amb = replay_inbound(store, inbound_message(open_id="ou_carol", text="好的收到"))
    check(amb.action == matcher.DISAMBIGUATE, "[B4] ambiguous reply → DISAMBIGUATE (ask to quote)")
    check("引用" in (amb.reply_text or ""), "[B4] reply asks 请引用具体卡片")

    quoted = replay_inbound(store, inbound_message(
        open_id="ou_carol", text="打车花了58", quoted="om_card_b"))
    check(quoted.action == matcher.ROUTE and quoted.registry_id == b["registry_id"],
          "[B4] quoted reply resolves to EXACTLY the quoted row")
    check(quoted.slash_content == f"/{SKILL} 打车花了58", "[B4] routed into scene's fill skill")


# ============================ B5: 崩溃对账 ================================

def phase_crash_reconcile(store):
    print("\n[B5] 崩溃对账 — crash after write, before commit CAS → reconcile backfills")
    rid, nonce = make_clarifying(store, open_id="ou_dave",
                                 business_key=f"{SCENE}:ou_dave:2026-07-20", mid="om_b5")
    writer = confirm.MockKepPreClaimWriter()
    scene_def = scenes.get_scene(SCENE)

    # Replay the confirm path up to — but not through — the final commit CAS:
    # CAS clarifying→confirmed, then the backend write lands, then "crash".
    store.advance_status(rid, expect=reg.STATUS_CLARIFYING, to=reg.STATUS_CONFIRMED,
                         expect_nonce=nonce)
    writer.write(scene=scene_def, values={"amount": 68, "date": "上周五",
                 "category": "打车", "reason": "去客户现场"},
                 registry_id=rid, write_idempotency_key=rid, profile_name="ou_dave-profile")
    check(store.get(rid)["status"] == reg.STATUS_CONFIRMED, "[B5] crashed row stuck at confirmed")
    check(writer.write_calls == 1 and len(writer.records) == 1, "[B5] backend has the one write")

    async def backend_lookup(row):
        # kep dedup lookup by write_idempotency_key = registry_id.
        return writer.records.get(row["registry_id"])

    future = reg._now() + workers.RECONCILE_AFTER_S + 10
    counters = asyncio.run(workers.run_reconcile_sweep(
        store, backend_lookup=backend_lookup, now=future))
    check(counters["committed"] == 1, "[B5] reconcile backfilled committed")
    check(store.get(rid)["status"] == reg.STATUS_COMMITTED, "[B5] row now committed")
    check(writer.write_calls == 1 and len(writer.records) == 1,
          "[B5] reconcile did NOT re-write → backend恰好一条")


# ============================ B6: 撤回重算 ================================

def phase_recall_recompute(store):
    print("\n[B6] 撤回重算 — a recall event wipes the extracted submission")
    rid = store.create(scene=SCENE, skill=SKILL, target_open_id="ou_erin",
                       profile_name="erin", business_key=f"{SCENE}:ou_erin:2026-07-20").row["registry_id"]
    store.mark_sent(rid, message_id="om_b6")
    store.advance_status(rid, expect=reg.STATUS_PENDING, to=reg.STATUS_CLARIFYING,
                         submission={"amount": 58, "category": "打车"})
    check(store.get(rid)["submission_json"] is not None, "[B6] submission extracted before recall")

    # Forged message-recall report → the recall handler (open_id + recalled mid).
    recall = {"event": {"operator_id": {"open_id": "ou_erin"},
                        "message_id": "om_user_reply_1"}}
    op = recall["event"]["operator_id"]["open_id"]
    mid = recall["event"]["message_id"]
    reset_rid = matcher.handle_message_recall(store, open_id=op, recalled_message_id=mid)
    check(reset_rid == rid, "[B6] recall targeted the user's single open row")
    check(store.get(rid)["submission_json"] is None, "[B6] submission wiped → will re-extract (replay)")


# ============================ B7: 200 突发限流 ===========================

def phase_burst_200():
    print("\n[B7] 200 突发限流 — all 202 accepted, queue drains every one, 0 failed")

    async def accept_burst():
        from aiohttp.test_utils import TestClient, TestServer
        from hermes_multitenancy.webui_broker_server import create_run_broker_app

        app = create_run_broker_app(dispatch_agent=lambda r: "noop",
                                    mark_seen=lambda _r: True, sandbox_available=lambda: True)
        client = TestClient(TestServer(app))
        await client.start_server()
        hdr = {"Authorization": "Bearer push-key-1"}
        accepted = []
        try:
            for i in range(200):
                oid = f"ou_burst_{i:03d}"
                r = await client.post("/api/run-broker/notify-card", headers=hdr,
                                      json={"open_id": oid, "scene": SCENE,
                                            "business_key": f"{SCENE}:{oid}:burst"})
                if r.status == 202:
                    accepted.append(json.loads(await r.text())["registry_id"])
            return accepted
        finally:
            await client.close()

    accepted = asyncio.run(accept_burst())
    check(len(accepted) == 200, f"[B7] all 200 bursts accepted 202 ({len(accepted)}/200)")

    # Drain the REAL rate-limited queue with a fake monotonic clock (so the token
    # bucket forces waits) + an active-hours clock + a recording sender/sleep.
    store = reg.get_registry_store()
    sends: list[str] = []
    waits: list[float] = []

    async def rec_sender(*, profile_name, open_id, card, payload):
        sends.append(open_id)
        return sendq.SendResult(message_id=f"om_{open_id}")

    async def rec_sleep(sec):
        waits.append(sec)  # no real wait — just prove pacing engaged

    frozen = [1000.0]

    def mono():
        frozen[0] += 0.001  # advance a hair per call, far under the refill rate
        return frozen[0]

    queue = sendq.PushSendQueue(
        store, rec_sender, rate_per_sec=5.0, burst=10.0,
        clock=lambda: datetime(2026, 7, 20, 10, 0, 0),  # active hours
        monotonic_fn=mono, sleep_fn=rec_sleep,
    )

    async def drain():
        return [await queue.process(rid) for rid in accepted]

    outcomes = asyncio.run(drain())
    sent = sum(1 for o in outcomes if o.status == "sent")
    failed = sum(1 for o in outcomes if o.status == "failed")
    check(sent == 200, f"[B7] queue delivered all 200 ({sent}/200 sent)")
    check(failed == 0, f"[B7] ZERO drops — nothing silently failed ({failed} failed)")
    check(len(waits) > 0, f"[B7] token-bucket paced the burst (队列平滑, {len(waits)} waits)")


# ============================ B8: 静默顺延 ===============================

def phase_quiet_hours():
    print("\n[B8] 静默顺延 — a send at 03:00 is DEFERRED, row stays sending, no send")
    store = reg.PushRegistryStore(":memory:")
    try:
        rid = store.create(scene=SCENE, skill=SKILL, target_open_id="ou_frank",
                           profile_name="frank", business_key=f"{SCENE}:ou_frank:q").row["registry_id"]
        called = {"n": 0}

        async def sender(*, profile_name, open_id, card, payload):
            called["n"] += 1
            return sendq.SendResult(message_id="om_should_not_happen")

        queue = sendq.PushSendQueue(
            store, sender, clock=lambda: datetime(2026, 7, 20, 3, 0, 0),  # inside 21:00–09:00
        )
        outcome = asyncio.run(queue.process(rid))
        check(outcome.status == "deferred" and outcome.reason == "quiet_hours",
              "[B8] send deferred with reason=quiet_hours")
        check(store.get(rid)["status"] == reg.STATUS_SENDING, "[B8] row stays sending (顺延, not failed)")
        check(called["n"] == 0, "[B8] adapter NEVER called during quiet hours")
        check(outcome.retry_at and outcome.retry_at > 0, "[B8] a concrete retry_at was scheduled")
    finally:
        store.close()


# ================================ main ==================================

def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sim-push-card-b-"))
    key_file = tmp / "keys.json"
    key_file.write_text(json.dumps({"keys": [{
        "token": "push-key-1", "owner": "sunke", "profile": "alice-profile",
        "allowed_scenes": [SCENE],
    }]}), encoding="utf-8")

    prev_keys_env = os.environ.get("HERMES_INGEST_KEYS_FILE")
    os.environ["HERMES_INGEST_KEYS_FILE"] = str(key_file)
    for var in ("HERMES_MULTITENANCY_RUN_BROKER_KEY", "HERMES_INGEST_KEY"):
        os.environ.pop(var, None)

    metrics.reset()
    # Shared in-memory registry singleton for the endpoint/queue-driven phases.
    reg.override_registry_store(":memory:")
    # Every push target resolves to a sending profile (no real routing table).
    # Accept user_id too — the endpoint resolves by open_id | union_id | user_id.
    routes.override_target_resolver(lambda *, open_id, union_id, user_id="": {
        "open_id": open_id or "ou_from_union", "union_id": union_id,
        "profile_name": f"{open_id or 'x'}-profile", "chat_id": None,
    })
    # Endpoint accept must NOT fire a real send (queue is driven explicitly).
    routes.override_send_dispatch(lambda registry_id: None)

    # A separate in-memory store for the confirm/matcher pure phases so their
    # forged rows don't collide with the endpoint burst rows.
    confirm_store = reg.PushRegistryStore(":memory:")

    try:
        phase_confirm_guards(confirm_store)
        phase_business_key_409()
        phase_disambiguate(confirm_store)
        phase_crash_reconcile(confirm_store)
        phase_recall_recompute(confirm_store)
        phase_burst_200()
        phase_quiet_hours()

        print("\n==== B-GROUP SIM RESULT ====")
        if _FAILURES:
            print(f"FAILED ({len(_FAILURES)} assertion(s)):")
            for f in _FAILURES:
                print(f"  - {f}")
            return 1
        print("ALL B-GROUP CASES PASSED — B1 非本人拒 · B2 nonce重放拒 · B3 409 · "
              "B4 消歧 · B5 崩溃对账 · B6 撤回重算 · B7 200突发平滑 · B8 静默顺延")
        return 0
    finally:
        confirm_store.close()
        reg.override_registry_store(None)
        routes.override_target_resolver(None)
        routes.override_send_dispatch(None)
        confirm.override_writer("kep-pre-claim-writer", None)
        metrics.reset()
        if prev_keys_env is None:
            os.environ.pop("HERMES_INGEST_KEYS_FILE", None)
        else:
            os.environ["HERMES_INGEST_KEYS_FILE"] = prev_keys_env
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
