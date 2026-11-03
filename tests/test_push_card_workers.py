"""Background sweeps for the push-card fill loop (SPEC P6).

The send re-drive sweep is the resume path for a quiet-hours顺延 / daily-cap
deferral: those deferrals leave the row `sending` and nothing else re-invokes
the queue, so without this sweep a deferred card never sends (finding
deferred-never-resumes).
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from hermes_multitenancy import push_card_workers as workers
from hermes_multitenancy import push_registry as reg
from hermes_multitenancy import push_send_queue as sq


@pytest.fixture
def store():
    s = reg.PushRegistryStore(":memory:")
    yield s
    s.close()


def _create(store, **over):
    kw = dict(
        scene="dev-acceptance-claim", skill="push-fill-form",
        target_open_id="ou_alice", profile_name="alice-profile",
        business_key="dev-acceptance-claim:ou_alice:2026-07-20",
    )
    kw.update(over)
    return store.create(**kw)


async def _noop_sleep(_s):
    return None


class _Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


def _queue(store, sender, *, hour):
    return sq.PushSendQueue(
        store, sender, clock=_Clock(datetime(2026, 7, 20, hour, 0, 0)),
        sleep_fn=_noop_sleep, rate_per_sec=1000, burst=1000,
    )


def test_redrive_resumes_a_deferred_sending_row(store):
    rid = _create(store).row["registry_id"]
    sends: list[str] = []

    async def sender(*, profile_name, open_id, card, payload):
        sends.append(open_id)
        return sq.SendResult(message_id="om_redriven")

    # 1) quiet-hours process → deferred, row stays sending, nothing sent
    assert asyncio.run(_queue(store, sender, hour=3).process(rid)).status == "deferred"
    assert store.get(rid)["status"] == reg.STATUS_SENDING
    assert sends == []

    # 2) re-drive at an active hour → the parked row actually sends
    counters = asyncio.run(workers.run_send_redrive_sweep(
        store, queue=_queue(store, sender, hour=10), redrive_after_s=0))
    assert counters["redriven"] == 1 and counters["sent"] == 1
    assert sends == ["ou_alice"]
    assert store.get(rid)["status"] == reg.STATUS_PENDING


def test_redrive_skips_fresh_rows_within_grace(store):
    # A row still within the grace window is left for the fire-and-forget
    # dispatch — the sweep must not race it into a double send.
    _create(store)

    async def sender(*, profile_name, open_id, card, payload):
        raise AssertionError("should not send within grace")

    counters = asyncio.run(workers.run_send_redrive_sweep(
        store, queue=_queue(store, sender, hour=10), redrive_after_s=3600))
    assert counters["redriven"] == 0


def test_redrive_redefers_when_still_in_quiet_hours(store):
    # Re-driving while STILL in quiet hours re-defers (idempotent), never fails.
    rid = _create(store).row["registry_id"]

    async def sender(*, profile_name, open_id, card, payload):
        raise AssertionError("should not send during quiet hours")

    counters = asyncio.run(workers.run_send_redrive_sweep(
        store, queue=_queue(store, sender, hour=3), redrive_after_s=0))
    assert counters["redriven"] == 1 and counters["deferred"] == 1
    assert store.get(rid)["status"] == reg.STATUS_SENDING
