"""Rate-limited send queue for push cards (SPEC push-card-fill-loop P2).

A thin封装 over the live gateway's bare card-send (the same
``adapter._feishu_send_with_retry`` path ``send_auth_card`` uses). The value
this module adds over calling the adapter directly is the pacing the 1259-user
early-morning burst needs (design §2.1 P0-3):

* **token-bucket** pacing to stay under the Feishu single-app QPS quota;
* **exponential backoff retry** on transient send failure, then a *visible*
  ``failed`` (never a silent drop, design §2.2);
* **per-user daily cap** (default 3) and **quiet-hours顺延** 21:00–09:00
  (design §2.6) — both DEFER (leave the row ``sending`` for a later sweep), they
  never fabricate a ``failed``.

The actual live-adapter binding is a seam (``register_push_card_sender``) filled
by the gateway process — this module is fully unit-testable with an injected
async sender, a fake clock, and a no-op sleep.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from . import push_registry as _reg

logger = logging.getLogger(__name__)


class PushCardSenderUnavailable(RuntimeError):
    """No live card sender is wired (or the adapter cannot send)."""


class PushCardSenderNotReady(PushCardSenderUnavailable):
    """The live Feishu adapter has not been captured yet — a *transient*
    condition (the gateway is mid-startup, or no inbound event has arrived to
    stash the adapter). ``process`` treats it as a DEFER (leave the row
    ``sending`` for the re-drive sweep), never a ``failed`` — distinct from a
    genuine send failure."""


@dataclass
class SendResult:
    message_id: Optional[str]
    chat_id: Optional[str] = None


@dataclass
class QueueOutcome:
    status: str  # 'sent' | 'failed' | 'deferred' | 'skipped'
    message_id: Optional[str] = None
    reason: Optional[str] = None
    retry_at: Optional[int] = None


# --- live-adapter seam ---------------------------------------------------

#: Async callable ``(profile_name, open_id, card, payload) -> SendResult``.
_card_sender: Optional[Callable[..., Awaitable[SendResult]]] = None


def register_push_card_sender(fn: Optional[Callable[..., Awaitable[SendResult]]]) -> None:
    """Wire (or in tests, unwire with ``None``) the live card sender."""
    global _card_sender
    _card_sender = fn


# --- live Feishu adapter capture (proactive-send binding) ----------------
#
# The notify-card endpoint's send dispatch is PROACTIVE — there is no inbound
# event carrying the adapter, so the queue needs its own reference to the live
# ``FeishuAdapter``. The matcher/confirm patches call ``note_live_adapter(self)``
# on every inbound event/callback, and ``install_live_push_card_sender`` binds a
# sender that uses whatever adapter was last stashed. Before the first inbound
# event the adapter is None → the sender raises ``PushCardSenderNotReady`` and
# ``process`` defers to the re-drive sweep (never a false ``failed``). This is
# what makes the live sender actually fire — nothing registered it before
# (finding live-sender-never-wired).

_live_adapter: Any = None


def note_live_adapter(adapter: Any) -> None:
    """Stash the live FeishuAdapter instance for proactive push sends."""
    global _live_adapter
    if adapter is not None:
        _live_adapter = adapter


def get_live_adapter() -> Any:
    return _live_adapter


async def _live_adapter_sender(
    *, profile_name: str, open_id: str, card: Any, payload: Any
) -> SendResult:
    adapter = _live_adapter
    if adapter is None:
        raise PushCardSenderNotReady("no live Feishu adapter captured yet")
    return await send_push_card_via_adapter(adapter, open_id=open_id, card=card, metadata=payload)


def install_live_push_card_sender() -> None:
    """Register the live-adapter-backed sender as the queue's card sender.

    Called once at gateway startup (plugin ``register``). Idempotent — re-binding
    the same closure is harmless."""
    register_push_card_sender(_live_adapter_sender)
    logger.info("[push_card] live push-card sender registered")


# --- eager cold-start adapter capture ------------------------------------
#
# The inbound matcher/confirm patches only ``note_live_adapter`` when an event
# arrives — so a freshly (re)started gateway could NOT push to a user until that
# user spoke first. That breaks this product's whole premise (the system pushes
# cards proactively every day; users must not have to message the bot first).
# Hook ``GatewayRunner._create_adapter`` — the same seam the cron startup watcher
# uses to reach the live gateway — and stash the Feishu adapter the moment the
# gateway builds it, before any inbound. Keying on ``_create_adapter`` also means
# a reconnect (which rebuilds the adapter) re-captures the fresh instance for
# free. The first-inbound stash stays as a belt-and-braces fallback.

_gateway_adapter_capture_installed = False


def _patch_gateway_create_adapter(GatewayRunner: Any) -> bool:
    """Wrap ``_create_adapter`` so any Feishu adapter it returns is captured.

    Split out (like the matcher's ``_patch_dispatch``) so the behaviour is
    unit-testable against a fake runner without the live gateway. Idempotent via
    a per-function marker; returns True when the class carries the patch."""
    original = getattr(GatewayRunner, "_create_adapter", None)
    if original is None:
        return False
    if getattr(original, "_hermes_push_card_capture_patched", False):
        return True

    @functools.wraps(original)
    def wrapped_create_adapter(self: Any, *args: Any, **kwargs: Any) -> Any:
        adapter = original(self, *args, **kwargs)
        try:
            # ``_feishu_send_with_retry`` uniquely identifies the Feishu adapter
            # AND is exactly the capability the proactive sender needs, so this
            # duck-type is both the filter and the contract check.
            if adapter is not None and callable(getattr(adapter, "_feishu_send_with_retry", None)):
                note_live_adapter(adapter)
                logger.info("[push_card] captured live Feishu adapter at gateway startup")
        except Exception:
            logger.debug("[push_card] gateway adapter capture failed", exc_info=True)
        return adapter

    setattr(wrapped_create_adapter, "_hermes_push_card_capture_patched", True)
    GatewayRunner._create_adapter = wrapped_create_adapter
    return True


def install_gateway_push_card_adapter_capture() -> None:
    """Eagerly capture the live Feishu adapter at gateway startup (cold-start
    proactive push). Router-runtime only; fail-open like its sibling installs."""
    global _gateway_adapter_capture_installed
    if _gateway_adapter_capture_installed:
        return
    from .gateway_deferred import install_when_gateway_runner_ready

    def _install(GatewayRunner: Any) -> None:
        global _gateway_adapter_capture_installed
        if _patch_gateway_create_adapter(GatewayRunner):
            _gateway_adapter_capture_installed = True
            logger.info("[push_card] installed gateway Feishu adapter capture")

    install_when_gateway_runner_ready("gateway-push-card-capture", _install)


def ensure_push_card_adapter_patches_armed(adapter: Any) -> None:
    """Re-arm the inbound matcher / confirm patches against the LIVE adapter and
    stash it for proactive sends. Called on every gateway dispatch (cheap once
    armed — each ``_patch_*`` returns immediately on the per-function marker it
    reads off the class it is about to patch).

    register() installs these once, but if the runtime FeishuAdapter class was
    not importable at that moment (``load_feishu_adapter`` deferred) the install
    returned WITHOUT arming and never retried — the patches would stay
    permanently uninstalled (inbound replies never reach the fill loop; proactive
    cards stay parked in 'sending'). The FIRST dispatch carries a fully-built live
    adapter, so we arm the patches against ``type(adapter)`` — the exact class the
    gateway runs, no reliance on ``load_feishu_adapter`` resolving the same class.
    Fail-open: any error must never break dispatch."""
    if adapter is None:
        return
    note_live_adapter(adapter)  # keep the freshest instance for proactive sends
    FeishuAdapter = type(adapter)
    try:
        from .push_card_matcher import install_feishu_push_card_matcher_patch
        install_feishu_push_card_matcher_patch(FeishuAdapter)
    except Exception:
        logger.debug("[push_card] matcher re-arm on dispatch failed", exc_info=True)
    try:
        from .push_card_confirm import install_feishu_push_card_confirm_patch
        install_feishu_push_card_confirm_patch(FeishuAdapter)
    except Exception:
        logger.debug("[push_card] confirm re-arm on dispatch failed", exc_info=True)


async def _default_sender(*, profile_name: str, open_id: str, card: Any, payload: Any) -> SendResult:
    if _card_sender is None:
        raise PushCardSenderUnavailable("no live push-card sender registered")
    return await _card_sender(
        profile_name=profile_name, open_id=open_id, card=card, payload=payload
    )


async def send_push_card_via_adapter(
    adapter: Any, *, open_id: str, card: dict[str, Any], metadata: Any = None
) -> SendResult:
    """Reference bare-send wrapper the gateway registers as the live sender.

    Mirrors ``feishu_auth_cards.send_auth_card`` but targets a DM by ``open_id``
    (the gateway's ``_patch_feishu_open_id_send`` makes ``_feishu_send_with_retry``
    accept an open_id as the receive target). Kept here so the wiring builder has
    exactly one call to make; not exercised by P2 unit tests (no live adapter)."""
    sender = getattr(adapter, "_feishu_send_with_retry", None)
    if not callable(sender):
        raise PushCardSenderUnavailable("adapter lacks _feishu_send_with_retry")
    response = await sender(
        chat_id=open_id,
        msg_type="interactive",
        payload=json.dumps(card, ensure_ascii=False),
        reply_to=None,
        metadata=metadata,
    )
    # The live SDK response carries message_id on ``response.data.message_id``
    # (a CreateMessageResponseBody object), NOT on ``response.message_id`` — the
    # earlier object-branch missed ``.data`` and always saw None, so every real
    # send raised "no message_id" and parked the card in ``sending`` forever.
    # Mirror cron's proven ``_feishu_response_message_id``: check ``.data`` first,
    # then the top-level, in both object and dict shapes.
    message_id = None
    for source in (getattr(response, "data", None), response):
        if source is None:
            continue
        if isinstance(source, dict):
            message_id = source.get("message_id") or (source.get("message") or {}).get("message_id")
        else:
            message_id = getattr(source, "message_id", None)
        if message_id:
            break
    if not message_id:
        raise PushCardSenderUnavailable("card send returned no message_id")
    return SendResult(message_id=str(message_id))


async def update_push_card_via_adapter(
    adapter: Any, *, message_id: str, card: dict[str, Any]
) -> bool:
    """Patch an already-sent interactive card IN PLACE (原地改卡) by message_id.

    The fill loop uses this to re-render the SAME confirm card on each reply
    instead of stacking a new card per reply (finding
    confirm-card-no-in-place-update). Returns True on success; a failure (or a
    missing message_id / adapter) returns False so the caller falls back to
    sending a NEW card and re-points ``registry.message_id`` at it."""
    mid = str(message_id or "").strip()
    if adapter is None or not mid:
        return False
    from .card.cardkit_client import _patch_interactive_message
    return bool(await _patch_interactive_message(adapter, mid, card))


# --- token bucket --------------------------------------------------------

class TokenBucket:
    """Classic token bucket. ``take()`` returns the seconds to wait (0 if a token
    is available now) and consumes one token."""

    def __init__(
        self,
        rate_per_sec: float,
        capacity: float,
        *,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = max(float(rate_per_sec), 1e-6)
        self.capacity = max(float(capacity), 1.0)
        self._tokens = self.capacity
        self._now = monotonic_fn
        self._last = self._now()

    def _refill(self) -> None:
        now = self._now()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

    def take(self) -> float:
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        wait = (1.0 - self._tokens) / self.rate
        self._tokens = 0.0
        return wait


# --- queue ---------------------------------------------------------------

def _in_quiet_hours(dt: datetime, start_hour: int, end_hour: int) -> bool:
    """Quiet window may wrap midnight (21:00–09:00)."""
    h = dt.hour
    if start_hour <= end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour


def _next_active_dt(dt: datetime, start_hour: int, end_hour: int) -> datetime:
    """The next instant outside the quiet window (its trailing edge)."""
    candidate = dt.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if candidate <= dt:
        candidate = candidate + timedelta(days=1)
    return candidate


class PushSendQueue:
    def __init__(
        self,
        store: _reg.PushRegistryStore,
        sender: Callable[..., Awaitable[SendResult]] = _default_sender,
        *,
        rate_per_sec: float = 5.0,
        burst: float = 10.0,
        daily_cap: int = 3,
        quiet_start_hour: int = 21,
        quiet_end_hour: int = 9,
        max_retries: int = 4,
        base_backoff: float = 0.5,
        clock: Callable[[], datetime] = datetime.now,
        monotonic_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.store = store
        self.sender = sender
        self.daily_cap = int(daily_cap)
        self.quiet_start_hour = int(quiet_start_hour)
        self.quiet_end_hour = int(quiet_end_hour)
        self.max_retries = int(max_retries)
        self.base_backoff = float(base_backoff)
        self.clock = clock
        self.sleep = sleep_fn
        self._bucket = TokenBucket(rate_per_sec, burst, monotonic_fn=monotonic_fn)

    async def process(self, registry_id: str) -> QueueOutcome:
        """Attempt one delivery. Idempotent: a row already past ``sending`` is a
        no-op ``skipped`` (never a double send)."""
        row = self.store.get(registry_id)
        if row is None:
            return QueueOutcome(status="failed", reason="not_found")
        if row["status"] != _reg.STATUS_SENDING:
            return QueueOutcome(status="skipped", reason=f"status={row['status']}")

        now_dt = self.clock()

        # Quiet hours顺延 — defer, do not send, leave row 'sending'.
        if _in_quiet_hours(now_dt, self.quiet_start_hour, self.quiet_end_hour):
            retry_dt = _next_active_dt(now_dt, self.quiet_start_hour, self.quiet_end_hour)
            return QueueOutcome(
                status="deferred", reason="quiet_hours", retry_at=int(retry_dt.timestamp())
            )

        # Per-user daily cap — defer to next local midnight.
        day_start = int(
            now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        if self.daily_cap > 0 and self.store.count_pushes_today(row["target_open_id"], day_start=day_start) >= self.daily_cap:
            next_day = now_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            return QueueOutcome(
                status="deferred", reason="daily_cap", retry_at=int(next_day.timestamp())
            )

        # Pace against the app QPS quota.
        wait = self._bucket.take()
        if wait > 0:
            await self.sleep(wait)

        card = _loads(row.get("card_json"))
        payload = _loads(row.get("payload_json"))
        last_error = "send failed"
        for attempt in range(self.max_retries):
            try:
                result = await self.sender(
                    profile_name=row["profile_name"],
                    open_id=row["target_open_id"],
                    card=card,
                    payload=payload,
                )
            except PushCardSenderUnavailable:
                # No usable sender in THIS process — DEFER (leave row 'sending').
                # Covers both PushCardSenderNotReady (adapter not captured yet) and
                # the base case where notify-card dispatched the send from the
                # run-broker process, which owns no live Feishu adapter at all: the
                # gateway's re-drive sweep is the only place that can actually send.
                # Never burn retries or mark failed on this cross-process/startup race.
                return QueueOutcome(
                    status="deferred", reason="sender_not_ready",
                    retry_at=int(time.time()) + 30,
                )
            except Exception as exc:  # transient send failure — retry with backoff
                last_error = f"{type(exc).__name__}: {exc}"
                self.store.note_send_attempt(registry_id, error=last_error)
                if attempt < self.max_retries - 1:
                    await self.sleep(self.base_backoff * (2 ** attempt))
                continue
            if not getattr(result, "message_id", None):
                last_error = "send returned no message_id"
                self.store.note_send_attempt(registry_id, error=last_error)
                if attempt < self.max_retries - 1:
                    await self.sleep(self.base_backoff * (2 ** attempt))
                continue
            if self.store.mark_sent(
                registry_id, message_id=result.message_id, chat_id=getattr(result, "chat_id", None)
            ):
                return QueueOutcome(status="sent", message_id=result.message_id)
            # Lost the CAS (already advanced by someone else) — not our send to own.
            return QueueOutcome(status="skipped", reason="cas_lost")

        self.store.mark_send_failed(registry_id, error=last_error)
        return QueueOutcome(status="failed", reason=last_error)


def _loads(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return None


# --- module-level singleton ---------------------------------------------

_queue: Optional[PushSendQueue] = None


def _quiet_hour_env(name: str, default: int) -> int:
    """Quiet-hours bound from env (policy, not code). ``HERMES_PUSH_CARD_QUIET_START
    == HERMES_PUSH_CARD_QUIET_END`` disables quiet hours entirely (never quiet)."""
    import os
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        h = int(raw)
    except ValueError:
        return default
    return h if 0 <= h <= 23 else default


def _daily_cap_env(default: int = 3) -> int:
    """Daily-cap from env (policy, not code). <=0 disables the cap entirely
    (2026-07-22 sunke: cap is a management act, not an interface behavior)."""
    import os
    raw = os.environ.get("HERMES_PUSH_CARD_DAILY_CAP")
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def get_send_queue() -> PushSendQueue:
    global _queue
    if _queue is None:
        _queue = PushSendQueue(
            _reg.get_registry_store(),
            daily_cap=_daily_cap_env(3),
            quiet_start_hour=_quiet_hour_env("HERMES_PUSH_CARD_QUIET_START", 21),
            quiet_end_hour=_quiet_hour_env("HERMES_PUSH_CARD_QUIET_END", 9),
        )
    return _queue


def override_send_queue(queue: Optional[PushSendQueue]) -> None:
    global _queue
    _queue = queue
