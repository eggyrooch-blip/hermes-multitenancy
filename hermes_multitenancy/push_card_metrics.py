"""Lightweight in-process counters for the push-card fill loop (design §2.7).

The repo has no metrics sink of its own — existing workers just keep a counter
dict and log a summary (``credential_renewal_worker``). This mirrors that: a
process-global ``Counter`` the matcher and workers bump, plus a ``snapshot()``
the gateway can scrape into whatever panel it already feeds.

Tracked (design §2.7): match hits, escape-hatch (误命中) exits, ambiguity
rejects, expiry sweeps, confirmed→committed reconciles and their failures,
send failures. Everything is a monotonic count; latency is summed so the panel
can derive an average without us keeping a histogram.

ponytail: global Counter, no locking — CPython dict item ``+=`` on distinct
keys is atomic enough for coarse counters; swap for a real sink if a panel
needs per-label cardinality.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

logger = logging.getLogger(__name__)

_counter: "Counter[str]" = Counter()

# Canonical metric names (kept as constants so callers can't typo a label into
# a silent new series).
MATCH_HIT = "push_card.match.hit"
MATCH_PASSTHROUGH = "push_card.match.passthrough"
MATCH_EXPIRED_EXIT = "push_card.match.expired_exit"
MATCH_DISAMBIGUATE = "push_card.match.disambiguate"
ESCAPE_HATCH = "push_card.match.escape_hatch"  # 误命中率代理
GRACE_RETRY = "push_card.match.grace_retry"
RECALL_RECOMPUTE = "push_card.recall.recompute"
SEND_FAILED = "push_card.send.failed"
EXPIRE_SWEPT = "push_card.expire.swept"
EXPIRE_CARD_FAILED = "push_card.expire.card_update_failed"
RECONCILE_COMMITTED = "push_card.reconcile.committed"
RECONCILE_MISSING = "push_card.reconcile.still_missing"
COMMIT_LATENCY_MS_SUM = "push_card.commit.latency_ms_sum"
COMMIT_LATENCY_COUNT = "push_card.commit.latency_count"


def incr(name: str, by: int = 1) -> None:
    _counter[name] += by


def observe_commit_latency_ms(ms: int) -> None:
    """Record one confirmed→committed transition latency (design §2.7)."""
    if ms < 0:
        ms = 0
    _counter[COMMIT_LATENCY_MS_SUM] += ms
    _counter[COMMIT_LATENCY_COUNT] += 1


def snapshot() -> dict[str, int]:
    """Copy of the current counts for the metrics panel to scrape."""
    return dict(_counter)


def reset() -> None:
    """Test hook only — never call from production paths."""
    _counter.clear()
