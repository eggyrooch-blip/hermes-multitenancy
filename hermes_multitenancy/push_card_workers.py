"""Background workers for the push-card fill loop (SPEC P6, design §2.6/§2.5).

Three sweeps, all idempotent and all safe to run on a timer:

* ``run_send_redrive_sweep`` — re-runs the send queue for ``sending`` rows a
  quiet-hours / daily-cap deferral parked (nothing else re-invokes the queue, so
  without it a deferred card never sends — finding deferred-never-resumes).

* ``run_expiry_sweep`` — flips not-finished rows past ``expires_at`` to
  ``expired`` and re-renders their card to "已过期". Card re-render failure is
  alerted + counted but never blocks the state flip (the matcher's read-time
  ``expires_at`` guard already prevents mis-routing, so a stalled renderer is
  cosmetic, not a correctness SPOF — design §2.6).

* ``run_reconcile_sweep`` — for ``confirmed`` rows that have not reached
  ``committed`` within the reconcile window, asks the backend whether the write
  actually landed (keyed by ``write_idempotency_key = registry_id``) and
  backfills ``committed`` if so. It **never re-issues the write** — a blind
  retry is exactly the double-record failure P0-2 exists to prevent. A row the
  backend has not got stays ``confirmed`` for the next sweep (P5's write path
  owns re-submission).

Both seams (card updater, backend lookup) default to no-ops so the sweeps are
inert until the gateway (and P4/P5) wire the live callables — running them
early can only count zeros, never corrupt state.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Awaitable, Callable, Optional

from . import push_card_metrics as _metrics
from . import push_registry as _reg
from . import push_send_queue as _sendq

logger = logging.getLogger(__name__)

#: Default reconcile window — a confirmed row older than this with no commit is
#: eligible for a backend check (design §2.5 step 3).
RECONCILE_AFTER_S = 5 * 60

#: Default send re-drive grace — a ``sending`` row parked longer than this is
#: re-run through the queue. Also keeps the re-drive off a just-created row the
#: fire-and-forget dispatch is still handling.
SEND_REDRIVE_AFTER_S = 60

# Seam types.
#: Re-render + push the "已过期" card for a row → returns True on success.
CardUpdater = Callable[[dict[str, Any]], Awaitable[bool]]
#: Look the write up in the backend by idempotency key → the record, or None.
BackendLookup = Callable[[dict[str, Any]], Awaitable[Optional[dict[str, Any]]]]


async def run_expiry_sweep(
    store: _reg.PushRegistryStore,
    *,
    card_updater: Optional[CardUpdater] = None,
    now: Optional[int] = None,
) -> dict[str, int]:
    """Expire every due row; re-render its card first. Returns counters."""
    now = _reg._now() if now is None else now
    counters = {"due": 0, "expired": 0, "card_failed": 0}
    for row in store.list_due_open(now=now):
        counters["due"] += 1
        # Re-render the card BEFORE the flip so the message_id is still the live
        # one; a failure is alerted but does not stop expiry.
        if card_updater is not None and row.get("message_id"):
            try:
                ok = await card_updater(row)
            except Exception as exc:  # noqa: BLE001
                ok = False
                logger.warning(
                    "[push_card] expiry card update raised (registry=%s): %s",
                    row["registry_id"], exc,
                )
            if not ok:
                counters["card_failed"] += 1
                _metrics.incr(_metrics.EXPIRE_CARD_FAILED)
                logger.warning(
                    "[push_card] expiry card update FAILED (registry=%s) — row still expired",
                    row["registry_id"],
                )
        if store.advance_status(row["registry_id"], expect=row["status"], to=_reg.STATUS_EXPIRED):
            counters["expired"] += 1
            _metrics.incr(_metrics.EXPIRE_SWEPT)
    if counters["due"]:
        logger.info(
            "[push_card] expiry sweep: due=%d expired=%d card_failed=%d",
            counters["due"], counters["expired"], counters["card_failed"],
        )
    return counters


async def run_send_redrive_sweep(
    store: _reg.PushRegistryStore,
    *,
    queue: Optional["_sendq.PushSendQueue"] = None,
    redrive_after_s: int = SEND_REDRIVE_AFTER_S,
    now: Optional[int] = None,
) -> dict[str, int]:
    """Re-run the send queue for ``sending`` rows parked past ``redrive_after_s``.

    A quiet-hours顺延 / daily-cap deferral leaves a row ``sending`` but nothing
    re-invokes the queue, so without this sweep the card never sends — it sits
    until the expiry worker reaps it (finding deferred-never-resumes). ``process``
    is idempotent and re-checks the deferral window, so each parked row either
    sends now or re-defers; a row already advanced is a ``skipped`` no-op.
    """
    now = _reg._now() if now is None else now
    queue = _sendq.get_send_queue() if queue is None else queue
    counters = {"redriven": 0, "sent": 0, "deferred": 0, "failed": 0, "skipped": 0}
    older_than = now - int(redrive_after_s)
    for row in store.list_sending_stale(older_than=older_than):
        counters["redriven"] += 1
        try:
            outcome = await queue.process(row["registry_id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[push_card] send re-drive raised (registry=%s): %s — leaving sending",
                row["registry_id"], exc,
            )
            continue
        counters[outcome.status] = counters.get(outcome.status, 0) + 1
    if counters["redriven"]:
        logger.info(
            "[push_card] send re-drive sweep: redriven=%d sent=%d deferred=%d "
            "failed=%d skipped=%d",
            counters["redriven"], counters["sent"], counters["deferred"],
            counters["failed"], counters["skipped"],
        )
    return counters


async def run_reconcile_sweep(
    store: _reg.PushRegistryStore,
    *,
    backend_lookup: Optional[BackendLookup] = None,
    reconcile_after_s: int = RECONCILE_AFTER_S,
    now: Optional[int] = None,
) -> dict[str, int]:
    """Backfill ``committed`` for confirmed rows the backend already has. Never
    re-writes. Returns counters."""
    now = _reg._now() if now is None else now
    counters = {"checked": 0, "committed": 0, "still_missing": 0}
    if backend_lookup is None:
        return counters
    older_than = now - int(reconcile_after_s)
    for row in store.list_stale_confirmed(older_than=older_than):
        counters["checked"] += 1
        try:
            record = await backend_lookup(row)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[push_card] reconcile lookup raised (registry=%s): %s — leaving confirmed",
                row["registry_id"], exc,
            )
            continue
        if record:
            # Backend has it → backfill committed. No re-write.
            if store.advance_status(
                row["registry_id"], expect=_reg.STATUS_CONFIRMED, to=_reg.STATUS_COMMITTED,
                submission=record if isinstance(record, dict) else None,
            ):
                counters["committed"] += 1
                _metrics.incr(_metrics.RECONCILE_COMMITTED)
                _metrics.observe_commit_latency_ms(max(0, now - int(row.get("updated_at") or now)) * 1000)
        else:
            counters["still_missing"] += 1
            _metrics.incr(_metrics.RECONCILE_MISSING)
    if counters["checked"]:
        logger.info(
            "[push_card] reconcile sweep: checked=%d committed=%d still_missing=%d",
            counters["checked"], counters["committed"], counters["still_missing"],
        )
    return counters


# --- scheduler (the sweeps are inert until something runs them) ----------

async def run_all_sweeps(
    store: _reg.PushRegistryStore,
    *,
    queue: Optional["_sendq.PushSendQueue"] = None,
    card_updater: Optional[CardUpdater] = None,
    backend_lookup: Optional[BackendLookup] = None,
    now: Optional[int] = None,
) -> dict[str, dict[str, int]]:
    """One tick of all three sweeps. Order: expiry (reap over-due) → send
    re-drive (resume deferred sends) → reconcile (backfill committed)."""
    return {
        "expiry": await run_expiry_sweep(store, card_updater=card_updater, now=now),
        "redrive": await run_send_redrive_sweep(store, queue=queue, now=now),
        "reconcile": await run_reconcile_sweep(store, backend_lookup=backend_lookup, now=now),
    }


#: Default cadence for the sweep worker thread.
SWEEP_INTERVAL_S = 60

_sweep_lock = threading.Lock()
_sweep_thread: Optional[threading.Thread] = None
_sweep_stop: Optional[threading.Event] = None


def _sweep_loop(interval: int, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            asyncio.run(run_all_sweeps(_reg.get_registry_store()))
        except Exception:
            logger.exception("[push_card] sweep tick failed")
        stop_event.wait(timeout=interval)
    logger.info("[push_card] sweep worker stopped")


def ensure_push_card_sweeps_started(*, interval: int = SWEEP_INTERVAL_S) -> None:
    """Start the push-card sweep worker once, at gateway startup.

    Nothing else re-invokes the send queue for a deferred row, re-renders an
    expired card, or reconciles a crashed commit — without this thread all three
    sweeps are dead code (finding sweeps-never-scheduled). Self-contained daemon
    thread (mirrors ``credential_renewal_worker``); each tick opens a fresh event
    loop via ``asyncio.run`` for the async sweep bodies."""
    global _sweep_thread, _sweep_stop
    with _sweep_lock:
        if _sweep_thread is not None and _sweep_thread.is_alive():
            return
        _sweep_stop = threading.Event()
        _sweep_thread = threading.Thread(
            target=_sweep_loop, args=(int(interval), _sweep_stop),
            daemon=True, name="push-card-sweeps",
        )
        _sweep_thread.start()
        logger.info("[push_card] sweep worker started (interval=%ds)", interval)


def stop_push_card_sweeps() -> None:
    """Signal the sweep worker to stop (test/shutdown hook)."""
    global _sweep_thread, _sweep_stop
    with _sweep_lock:
        if _sweep_stop is not None:
            _sweep_stop.set()
        thread = _sweep_thread
    if thread is not None:
        thread.join(timeout=2.0)
    with _sweep_lock:
        _sweep_thread = None
        _sweep_stop = None
