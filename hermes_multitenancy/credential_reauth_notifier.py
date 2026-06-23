"""Passive maintenance for Feishu `.needs_reauth` credential markers.

This module intentionally does not send Feishu private messages. Re-auth
markers are internal task-gating state: L4 cron/run paths surface auth guidance
only when a real task cannot proceed with the user's current credential.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable

from .credential_renewal_common import (
    clear_needs_reauth_marker,
    clear_reauth_markers_if_uat_recovered,
    current_valid_uat_exists,
    is_fixture_path,
    marker_requires_reauth,
    preserve_reauth_marker_as_refresh_diagnostic,
    read_needs_reauth_marker,
)

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 30
_DEFAULT_MAX_MARKER_AGE_SECONDS = 24 * 3600


def _resolve_max_marker_age() -> int:
    """Compatibility parser for the old proactive-notifier freshness setting."""
    raw = (os.environ.get("HERMES_CREDENTIAL_REAUTH_NOTIFIER_MAX_AGE_SECONDS") or "").strip()
    if not raw:
        return _DEFAULT_MAX_MARKER_AGE_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_MAX_MARKER_AGE_SECONDS


def ensure_reauth_notifier_started(shared_home: Path, *, interval_seconds: int = _DEFAULT_INTERVAL_SECONDS) -> None:
    """No-op compatibility shim; proactive reauth DMs are disabled."""
    logger.info(
        "[credential_reauth_notifier] proactive DM watcher disabled shared_home=%s interval=%s",
        shared_home,
        interval_seconds,
    )


def stop_reauth_notifier() -> None:
    """Compatibility no-op for tests and older callers."""
    return None


def _scan_once(shared_home: Path, seen: dict[str, dict[str, Any]]) -> bool:
    """One passive maintenance pass over marker files.

    Returns True only when marker files changed. The ``seen`` dict is ignored
    because there is no notification/dedupe state after proactive DMs were
    removed.
    """
    del seen
    changed = False

    for marker in _iter_markers(shared_home):
        body = read_needs_reauth_marker(marker)
        if not body:
            continue
        open_id = _open_id_from_marker(marker)
        if not open_id:
            continue
        reason = str(body.get("reason") or "")
        if clear_reauth_markers_if_uat_recovered(shared_home, open_id, marker):
            changed = True
            logger.info(
                "[credential_reauth_notifier] cleared stale reauth marker for open_id=%s because "
                "a newer refreshable UAT exists",
                open_id,
            )
            continue
        if not marker_requires_reauth(body):
            preserve_reauth_marker_as_refresh_diagnostic(
                marker,
                body,
                source="credential_reauth_maintenance",
            )
            clear_needs_reauth_marker(marker)
            changed = True
            if current_valid_uat_exists(shared_home, open_id):
                logger.info(
                    "[credential_reauth_notifier] cleared non-authoritative refresh_rejected marker "
                    "for open_id=%s because current UAT is still refreshable",
                    open_id,
                )
            else:
                logger.warning(
                    "[credential_reauth_notifier] cleared non-authoritative refresh_rejected marker "
                    "for open_id=%s without sending DM",
                    open_id,
                )
            continue
        logger.debug(
            "[credential_reauth_notifier] leaving actionable marker for passive task gate open_id=%s reason=%s",
            open_id,
            reason,
        )
    return changed


def _iter_markers(shared_home: Path) -> Iterable[Path]:
    legacy = shared_home / "feishu_uat"
    if legacy.is_dir() and not is_fixture_path(legacy):
        for entry in legacy.iterdir():
            if entry.suffix == ".needs_reauth" or entry.name.endswith(".needs_reauth"):
                yield entry
    profiles = shared_home / "profiles"
    if profiles.is_dir():
        for profile_dir in profiles.iterdir():
            if not profile_dir.is_dir() or is_fixture_path(profile_dir):
                continue
            uat_dir = profile_dir / "feishu_uat"
            if not uat_dir.is_dir():
                continue
            for entry in uat_dir.iterdir():
                if entry.suffix == ".needs_reauth" or entry.name.endswith(".needs_reauth"):
                    yield entry


def _open_id_from_marker(marker: Path) -> str:
    """``ou_xxx.needs_reauth`` -> ``ou_xxx``; ``ou_xxx.json.needs_reauth`` -> ``ou_xxx``."""
    stem = marker.name[: -len(".needs_reauth")] if marker.name.endswith(".needs_reauth") else marker.stem
    if stem.endswith(".json"):
        stem = stem[: -len(".json")]
    return stem
