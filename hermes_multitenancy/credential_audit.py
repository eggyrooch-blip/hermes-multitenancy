"""L5 — startup credential audit.

Runs once when the multitenancy plugin registers. Scans every UAT on disk,
classifies it, and writes `.needs_reauth` markers for the broken ones so the
task gate can attribute the right owner. Also audits the multitenancy routing
DB for orphan profiles (a running gateway under ``profiles/<name>`` with no
``active=1, kind='user'`` row pointing back at it) and surfaces the count in
the gateway log.

The audit is read-only against the vault and the routing DB; the only writes
are `.needs_reauth` markers (idempotent — same reason + same day is a no-op).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .credential_renewal_common import (
    CredentialIdentityLockTimeout,
    _unique_active_user_profile_for_open_id,
    classify_uat_payload,
    credential_identity_lock,
    current_valid_uat_exists,
    iter_uat_locations,
    is_fixture_path,
    marker_path_for_open_id,
    read_needs_reauth_marker,
    write_needs_reauth_marker,
)

logger = logging.getLogger(__name__)

_AUDIT_IDENTITY_LOCK_TIMEOUT_SECONDS = 0.05


@dataclass
class AuditReport:
    scanned: int = 0
    valid: int = 0
    needs_reauth: int = 0
    fresh_markers: int = 0
    orphan_profiles: list[str] = field(default_factory=list)
    missing_uat_for_routing: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"[credential_audit] scanned={self.scanned} valid={self.valid} "
            f"needs_reauth={self.needs_reauth} fresh_markers={self.fresh_markers} "
            f"orphan_profiles={len(self.orphan_profiles)} "
            f"routing_missing_uat={len(self.missing_uat_for_routing)}"
        )


def run_startup_audit(shared_home: Path) -> AuditReport:
    """Single-shot scan. Safe to call from plugin register()."""
    report = AuditReport()
    _audit_uat_files(shared_home, report)
    _audit_routing_orphans(shared_home, report)
    logger.info(report.summary_line())
    if report.orphan_profiles:
        logger.warning(
            "[credential_audit] orphan profiles (running but no active routing row): %s",
            ",".join(report.orphan_profiles),
        )
    if report.missing_uat_for_routing:
        logger.warning(
            "[credential_audit] routing rows without any usable UAT on disk: %s",
            ",".join(report.missing_uat_for_routing),
        )
    return report


def _audit_uat_files(shared_home: Path, report: AuditReport) -> None:
    # Two passes: classify everything first, so a broken STALE COPY of an
    # open_id whose CURRENT token is usable elsewhere (e.g. the legacy
    # shared-home file after credentials went profile-local) never gets a
    # needs_reauth marker. That false positive deferred every cron job and
    # reauth-prompted owners whose auth was actually valid (2026-07-08).
    broken: list[tuple[Any, str, str]] = []  # (loc, reason, detail)
    usable_open_ids: set[str] = set()
    for loc in iter_uat_locations(shared_home):
        report.scanned += 1
        payload = _load_payload(loc.path)
        if payload is None:
            # Malformed JSON treated as broken; write a marker so notifier surfaces it.
            broken.append((loc, "malformed_uat_json", f"unreadable UAT at {loc.path}"))
            continue
        reason = classify_uat_payload(payload)
        if reason is None:
            report.valid += 1
            usable_open_ids.add(loc.open_id)
            continue
        broken.append((loc, reason, f"L5 audit detected broken UAT at {loc.path.name}"))

    for loc, reason, detail in broken:
        if loc.open_id in usable_open_ids:
            logger.info(
                "[credential_audit] stale UAT copy ignored (usable UAT exists elsewhere): %s reason=%s",
                loc.path,
                reason,
            )
            continue
        report.needs_reauth += 1
        _maybe_write_marker(
            shared_home,
            loc.path.parent,
            loc.open_id,
            reason=reason,
            detail=detail,
            report=report,
            profile=loc.profile_name,
        )


def _audit_routing_orphans(shared_home: Path, report: AuditReport) -> None:
    profiles_dir = shared_home / "profiles"
    if not profiles_dir.is_dir():
        return
    running_profiles: set[str] = set()
    for entry in profiles_dir.iterdir():
        if not entry.is_dir() or is_fixture_path(entry):
            continue
        if not (entry / "gateway.pid").is_file():
            continue
        running_profiles.add(entry.name)
    if not running_profiles:
        return

    routed_profiles: set[str] = set()
    routed_open_ids_by_profile: dict[str, list[str]] = {}
    db_path = shared_home / "multitenancy.db"
    if not db_path.is_file():
        return
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            rows = conn.execute(
                "SELECT profile_name, open_id, kind FROM multitenancy_routing "
                "WHERE active = 1 AND profile_name IS NOT NULL AND profile_name != ''"
            ).fetchall()
    except sqlite3.Error:
        logger.exception("[credential_audit] failed to read routing table")
        return
    for profile_name, open_id, kind in rows:
        routed_profiles.add(str(profile_name))
        if str(kind) == "user" and str(open_id).startswith("ou_"):
            routed_open_ids_by_profile.setdefault(str(profile_name), []).append(str(open_id))

    # Orphan: running profile with no active routing row
    report.orphan_profiles = sorted(running_profiles - routed_profiles)

    # Routing row without UAT on disk
    for profile_name, open_ids in routed_open_ids_by_profile.items():
        for open_id in open_ids:
            uat_profile = shared_home / "profiles" / profile_name / "feishu_uat" / f"{open_id}.json"
            uat_legacy = shared_home / "feishu_uat" / f"{open_id}.json"
            if not uat_profile.is_file() and not uat_legacy.is_file():
                report.missing_uat_for_routing.append(f"{profile_name}:{open_id}")


def _load_payload(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _maybe_write_marker(
    shared_home: Path,
    parent: Path,
    open_id: str,
    *,
    reason: str,
    detail: str,
    report: AuditReport,
    profile: str = "",
) -> None:
    """Write marker if absent OR reason changed; otherwise leave existing alone.

    L5 is idempotent — if a marker is already on disk with the same reason, we
    do nothing (avoid bumping mtime and re-triggering L3 every gateway restart).
    """
    def write_if_changed() -> None:
        # Classification happened before the identity lock. A successful OAuth
        # write may have repaired this identity while L5 was waiting.
        current_route = _unique_active_user_profile_for_open_id(shared_home, open_id)
        if profile and current_route and current_route != profile:
            return
        if current_valid_uat_exists(shared_home, open_id):
            return
        marker = marker_path_for_open_id(parent, open_id)
        existing = read_needs_reauth_marker(marker) if marker.is_file() else None
        if existing and existing.get("reason") == reason:
            return
        write_needs_reauth_marker(
            marker,
            reason=reason,
            detail=detail,
            extra={"layer": "L5", "profile": profile},
        )
        report.fresh_markers += 1

    active_profile = _unique_active_user_profile_for_open_id(shared_home, open_id)
    if profile and active_profile and profile != active_profile:
        logger.info(
            "[credential_audit] stale profile UAT ignored after route move "
            "profile=%s active_profile=%s open_id=%s",
            profile,
            active_profile,
            open_id,
        )
        return
    lock_profile = active_profile or profile
    if not lock_profile:
        write_if_changed()
        return
    try:
        with credential_identity_lock(
            shared_home,
            lock_profile,
            open_id,
            create_missing=False,
            timeout_seconds=_AUDIT_IDENTITY_LOCK_TIMEOUT_SECONDS,
        ):
            write_if_changed()
    except CredentialIdentityLockTimeout:
        logger.warning(
            "[credential_audit] identity busy; marker audit skipped "
            "profile=%s open_id=%s",
            lock_profile,
            open_id,
        )
    except ValueError:
        # A malformed leftover profile/UAT filename must not abort gateway
        # startup. Recovery rejects the same unsafe identity, so preserving the
        # audit's historical unlocked write is fail-closed for that artifact.
        logger.warning(
            "[credential_audit] unsafe marker identity cannot use shared lock "
            "profile=%r open_id=%r",
            lock_profile,
            open_id,
        )
        write_if_changed()
