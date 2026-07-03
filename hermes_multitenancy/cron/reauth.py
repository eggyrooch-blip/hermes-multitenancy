"""L4 credential-defer guard for cron dispatch.

Split out of the historical ``cron_worker`` god-node. Pure move + re-export:
``cron_worker`` re-exports every symbol so import paths and ``monkeypatch.setattr(
cron_worker, ...)`` targets are unchanged. Cross-module and monkeypatched helpers
are reached through the ``cron_worker`` shim (``_cw``) so patches applied to
``cron_worker.<name>`` are honoured by callers that live in a sibling module.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
from concurrent.futures import TimeoutError as FuturesTimeout
import functools
import json
import math
import signal
import logging
import os
import re
import subprocess
import sqlite3
import sys
import threading
import time
import uuid
import copy
from urllib import error as urllib_error
from urllib import request as urllib_request
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None

from .. import cron_worker as _cw
from ..credential_renewal_common import (
    clear_needs_reauth_marker,
    clear_reauth_markers_if_uat_recovered,
    find_marker_for_open_id,
    marker_requires_reauth,
    preserve_reauth_marker_as_refresh_diagnostic,
    read_needs_reauth_marker,
)

# Keep the historical logger name so log records are attributed to
# ``hermes_multitenancy.cron_worker`` exactly as before the split.
logger = logging.getLogger("hermes_multitenancy.cron_worker")



def _l4_check_needs_reauth_and_defer(
    job: dict,
) -> Optional[tuple[bool, str, str, Optional[str]]]:
    """If owner_open_id has a fresh `.needs_reauth` marker, defer the job.

    Returns ``(success=False, output_text, final_response, error)`` shaped like
    ``cron.scheduler.run_job`` so the scheduler records the skip. Side effect:
    writes ``<profile>/cron/output/<job_id>.deferred.json`` recording the
    skip metadata for the audit trail. This is the passive user-facing auth
    guidance path; background credential scans never send DMs.
    """
    owner_open_id = str(job.get("owner_open_id") or "").strip()
    if not owner_open_id.startswith("ou_"):
        return None
    shared_home = _cw._resolve_shared_home()
    while True:
        marker = _cw.find_marker_for_open_id(shared_home, owner_open_id)
        if marker is None:
            return None
        if _cw._clear_stale_reauth_markers_if_uat_recovered(shared_home, owner_open_id, marker):
            return None
        marker_body = _cw.read_needs_reauth_marker(marker) or {}
        reason = str(marker_body.get("reason") or "unknown")
        if _cw.marker_requires_reauth(marker_body):
            break
        cleared = False
        for non_actionable_marker in _cw._iter_reauth_markers_for_open_id(shared_home, owner_open_id):
            body = _cw.read_needs_reauth_marker(non_actionable_marker) or {}
            if not _cw.marker_requires_reauth(body):
                _cw.preserve_reauth_marker_as_refresh_diagnostic(
                    non_actionable_marker,
                    body,
                    source="cron_worker_l4",
                )
                # Only count progress if the marker was ACTUALLY removed — a
                # silently-failed unlink (root-owned marker / read-only FS) must
                # NOT keep the while-loop alive re-finding the same marker. HIGH-2.
                if _cw.clear_needs_reauth_marker(non_actionable_marker):
                    cleared = True
        logger.warning(
            "[multitenancy] L4 ignored non-authoritative reauth marker owner=%s reason=%s",
            owner_open_id,
            reason,
        )
        if not cleared:
            return None

    job_id = str(job.get("id") or "")
    job_name = str(job.get("name") or job_id or "scheduled task")
    deferred_payload = {
        "deferred": True,
        "reason": reason,
        "marker_path": str(marker),
        "marker_ts": int(marker_body.get("ts") or 0),
        "deferred_ts": int(time.time()),
        "job_id": job_id,
        "owner_open_id": owner_open_id,
    }
    try:
        profile_home = _cw._current_profile_home()
        output_dir = profile_home / "cron" / "output"
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        deferred_path = output_dir / f"{job_id or 'unnamed'}.deferred.json"
        deferred_path.write_text(
            json.dumps(deferred_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("[multitenancy] L4 failed to write deferred output for job=%s", job_id)
    logger.warning(
        "[multitenancy] L4 cron dispatch deferred job=%s owner=%s reason=%s",
        job_id, owner_open_id, reason,
    )
    error_msg = f"deferred: feishu UAT needs reauth ({reason})"
    output = (
        f"# Cron Job: {job_name}\n\n"
        f"**Job ID:** {job_id}\n"
        f"**Status:** DEFERRED — feishu credential needs re-auth\n"
        f"**Reason:** {reason}\n\n"
        f"本次任务未发送，因为 owner 的飞书用户授权当前不可用（{reason}）。"
        f"请 owner 在飞书私聊 Hermes 发送 `/feishu_auth` 重新授权后重试；"
        f"如果原因是 app scope 配置，请管理员修正并发布后再重试。"
    )
    return False, output, "", error_msg


def _clear_stale_reauth_markers_if_uat_recovered(
    shared_home: Path,
    owner_open_id: str,
    marker: Path,
) -> bool:
    """Return True when a newer, valid UAT makes existing reauth markers stale."""
    if not _cw.clear_reauth_markers_if_uat_recovered(shared_home, owner_open_id, marker):
        return False
    logger.info(
        "[multitenancy] L4 ignored stale reauth marker for owner=%s because a newer valid UAT exists",
        owner_open_id,
    )
    return True


def _iter_reauth_markers_for_open_id(shared_home: Path, open_id: str) -> list[Path]:
    markers = [shared_home / "feishu_uat" / f"{open_id}.needs_reauth"]
    profiles_dir = shared_home / "profiles"
    try:
        profile_dirs = list(profiles_dir.iterdir())
    except OSError:
        profile_dirs = []
    for profile_dir in profile_dirs:
        if profile_dir.is_dir():
            markers.append(profile_dir / "feishu_uat" / f"{open_id}.needs_reauth")
    return markers
