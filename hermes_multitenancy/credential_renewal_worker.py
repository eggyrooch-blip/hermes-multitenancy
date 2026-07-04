"""L2 — proactive UAT refresh worker.

A single background thread per gateway process. Every ``interval`` seconds:

  1. Read every ``active=1, kind='user'`` row from ``multitenancy_routing``.
  2. For each, load the freshest UAT payload (vault vs profile JSON).
  3. If the payload enters its refresh-headroom window, force-refresh via
     ``refresh_uat_if_needed(force=True)``.
  4. On authoritative Feishu invalid/revoked refresh-token responses, write a
     ``.needs_reauth`` marker (L1 helpers) so L3 picks it up and notifies the
     right party. Local infra / network / unknown-code failures write
     non-user-facing ``.refresh_diagnostic`` state instead.

Reads only — never overwrites a valid UAT outside of the existing
``feishu_uat_auth`` machinery. L1 sits underneath the actual write, so a
refresh that comes back with a degraded payload is rejected at write time
even if L2 didn't see the problem.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional

from .credential_renewal_common import (
    REASON_REFRESH_REJECTED,
    classify_uat_payload,
    is_fixture_path,
    marker_path_for_open_id,
    refresh_diagnostic_path_for_open_id,
    write_refresh_diagnostic_marker,
    write_needs_reauth_marker,
)

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_HEADROOM_SECONDS = 5 * 60  # refresh anything entering its last 5 minutes (WorkBuddy parity)

_worker_lock = threading.Lock()
_worker_thread: Optional[threading.Thread] = None
_worker_stop: Optional[threading.Event] = None


def ensure_renewal_worker_started(
    shared_home: Path,
    *,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    headroom_seconds: int = DEFAULT_HEADROOM_SECONDS,
) -> None:
    """Idempotent — safe to call multiple times from plugin register()."""
    global _worker_thread, _worker_stop
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_stop = threading.Event()
        _worker_thread = threading.Thread(
            target=_renewal_loop,
            args=(shared_home, interval_seconds, headroom_seconds, _worker_stop),
            daemon=True,
            name="credential-renewal-worker",
        )
        _worker_thread.start()
        logger.info(
            "[credential_renewal] worker started interval=%ds headroom=%ds",
            interval_seconds, headroom_seconds,
        )


def stop_renewal_worker() -> None:
    """For tests — stop cleanly."""
    global _worker_thread, _worker_stop
    with _worker_lock:
        if _worker_stop is not None:
            _worker_stop.set()
        if _worker_thread is not None:
            _worker_thread.join(timeout=5)
        _worker_thread = None
        _worker_stop = None


def _renewal_loop(
    shared_home: Path,
    interval_seconds: int,
    headroom_seconds: int,
    stop_event: threading.Event,
) -> None:
    while not stop_event.is_set():
        try:
            run_renewal_tick(shared_home, headroom_seconds=headroom_seconds)
        except Exception:
            logger.exception("[credential_renewal] tick error")
        stop_event.wait(timeout=interval_seconds)
    logger.info("[credential_renewal] worker stopped")


def run_renewal_tick(shared_home: Path, *, headroom_seconds: int) -> dict[str, int]:
    """One pass — exposed for tests and on-demand triggers.

    Returns counters: ``{scanned, refreshed, skipped, failed}``.
    """
    counters = {"scanned": 0, "refreshed": 0, "skipped": 0, "failed": 0}
    for profile_name, open_id in _iter_active_user_routes(shared_home):
        counters["scanned"] += 1
        try:
            outcome = _refresh_one(shared_home, profile_name, open_id, headroom_seconds)
        except Exception as exc:
            counters["failed"] += 1
            _record_failure(shared_home, profile_name, open_id, exc)
            continue
        if outcome == "refreshed":
            counters["refreshed"] += 1
        else:
            counters["skipped"] += 1
    if counters["scanned"]:
        logger.info(
            "[credential_renewal] tick scanned=%d refreshed=%d skipped=%d failed=%d",
            counters["scanned"], counters["refreshed"], counters["skipped"], counters["failed"],
        )
    return counters


def _iter_active_user_routes(shared_home: Path) -> Iterable[tuple[str, str]]:
    db_path = shared_home / "multitenancy.db"
    if not db_path.is_file():
        return
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            rows = conn.execute(
                "SELECT profile_name, open_id FROM multitenancy_routing "
                "WHERE active = 1 AND kind = 'user' AND open_id LIKE 'ou_%'"
            ).fetchall()
    except sqlite3.Error:
        logger.exception("[credential_renewal] failed to read routing table")
        return
    for profile_name, open_id in rows:
        profile_name = str(profile_name or "")
        open_id = str(open_id or "")
        if not (profile_name and open_id):
            continue
        if is_fixture_path(shared_home / "profiles" / profile_name):
            continue
        yield profile_name, open_id


def _refresh_one(
    shared_home: Path,
    profile_name: str,
    open_id: str,
    headroom_seconds: int,
) -> str:
    """``"refreshed"`` if we called refresh, ``"skipped"`` otherwise."""
    from .feishu_uat_auth import _load_best_uat_payload, _payload_needs_refresh, refresh_uat_if_needed

    payload = _load_best_uat_payload(shared_home, profile_name, open_id)
    if payload is None:
        return "skipped"

    # If the payload is already classifiable as broken, write a marker
    # and skip — L3 will surface, no point hitting Feishu.
    broken_reason = classify_uat_payload(payload)
    if broken_reason is not None:
        write_needs_reauth_marker(
            marker_path_for_open_id(
                shared_home / "profiles" / profile_name / "feishu_uat", open_id
            ),
            reason=broken_reason,
            detail="L2 detected broken UAT before refresh attempt",
            extra={"layer": "L2", "profile": profile_name},
        )
        return "skipped"

    if not _payload_needs_refresh(payload, headroom_seconds=headroom_seconds):
        return "skipped"

    refresh_uat_if_needed(
        profile_name=profile_name,
        open_id=open_id,
        shared_home=shared_home,
        force=True,
        headroom_seconds=headroom_seconds,
    )
    logger.info("[credential_renewal] refreshed user=%s profile=%s", open_id, profile_name)
    return "refreshed"


def _record_failure(
    shared_home: Path,
    profile_name: str,
    open_id: str,
    exc: BaseException,
) -> None:
    detail = f"{type(exc).__name__}: {exc}"
    # Truncate to keep marker tight; never include token material because the
    # exception path in feishu_uat_auth raises with curated messages only.
    if len(detail) > 240:
        detail = detail[:237] + "..."
    from .feishu_uat_auth import FeishuUatAuthError
    from .connector_failure_classifier import classify_connector_failure, is_needs_reauth

    # Route the terminal-vs-transient decision through the ONE unified classifier
    # (same function the reactive card + non-Feishu detect use). For Feishu the
    # curated refresh_class is the authoritative signal; http status is a fallback.
    _refresh_class = exc.refresh_class if isinstance(exc, FeishuUatAuthError) else None
    _http = exc.status if isinstance(exc, FeishuUatAuthError) else None
    verdict = classify_connector_failure(
        "lark-cli", http_status=_http, stderr=detail, refresh_class=_refresh_class
    )
    # User-facing needs_reauth marker only when the classifier says terminal AND it is
    # Feishu-authoritative-invalid (marker_requires_reauth stays the surfacing gate,
    # unchanged — this preserves the "silent unless authoritative" discipline).
    authoritative_invalid = isinstance(exc, FeishuUatAuthError) and exc.refresh_class == "invalid"
    if not (is_needs_reauth(verdict) and authoritative_invalid):
        write_refresh_diagnostic_marker(
            refresh_diagnostic_path_for_open_id(
                shared_home / "profiles" / profile_name / "feishu_uat", open_id
            ),
            detail=detail,
            extra={"layer": "L2", "profile": profile_name},
        )
        logger.warning(
            "[credential_renewal] refresh failed without user-actionable reauth marker "
            "user=%s profile=%s reason=%s",
            open_id,
            profile_name,
            detail,
        )
        return
    write_needs_reauth_marker(
        marker_path_for_open_id(
            shared_home / "profiles" / profile_name / "feishu_uat", open_id
        ),
        reason=REASON_REFRESH_REJECTED,
        detail=detail,
        extra={
            "layer": "L2",
            "profile": profile_name,
            "authoritative": True,
            "refresh_class": exc.refresh_class,
        },
    )
    logger.warning(
        "[credential_renewal] refresh failed user=%s profile=%s reason=%s",
        open_id, profile_name, detail,
    )
