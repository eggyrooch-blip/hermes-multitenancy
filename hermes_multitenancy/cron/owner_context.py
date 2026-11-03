"""Cron job owner-context inference and routing.

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

# Keep the historical logger name so log records are attributed to
# ``hermes_multitenancy.cron_worker`` exactly as before the split.
logger = logging.getLogger("hermes_multitenancy.cron_worker")



def _current_profile_home() -> Path:
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw).expanduser().resolve() if raw else Path.home() / ".hermes"


def _resolve_shared_home() -> Path:
    """The ``<shared>`` ancestor that owns multitenancy.db + legacy feishu_uat/.

    Identical resolution rule to ``feishu_uat_auth.resolve_shared_home`` so L4
    and L1 agree on which directory holds markers.
    """
    explicit = os.environ.get("HERMES_SHARED_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    profile_home = _cw._current_profile_home()
    parts = profile_home.parts
    if len(parts) >= 2 and parts[-2] == "profiles":
        return profile_home.parent.parent
    return profile_home


def _is_feishu_open_id(value: Any) -> bool:
    return str(value or "").strip().startswith("ou_")


def _shared_home_for_profile(profile_home: Path) -> Path:
    profile_home = Path(profile_home).expanduser().resolve()
    if profile_home.parent.name == "profiles":
        return profile_home.parent.parent
    return _cw._resolve_shared_home()


def _routing_owner_for_profile(profile_home: Path) -> tuple[str, str]:
    """Infer (owner_open_id, owner_profile) from the active routing table."""
    profile_home = Path(profile_home).expanduser().resolve()
    shared_home = _cw._shared_home_for_profile(profile_home)
    db_path = shared_home / "multitenancy.db"
    if not db_path.is_file():
        return "", ""
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT open_id, owner_open_id, profile_name, kind, provenance "
                "FROM multitenancy_routing "
                "WHERE profile_name = ? AND active = 1 "
                "ORDER BY (kind = 'user') DESC, (provenance = 'sync') DESC, updated_at DESC "
                "LIMIT 1",
                (profile_home.name,),
            ).fetchone()
            if row is None:
                return "", ""
            owner = str(row["owner_open_id"] or row["open_id"] or "").strip()
            owner_profile = str(row["profile_name"] or profile_home.name).strip()
            return (owner if _cw._is_feishu_open_id(owner) else "", owner_profile)
    except Exception:
        logger.debug("[multitenancy] cron owner route lookup failed", exc_info=True)
        return "", ""


def _session_owner_open_id() -> str:
    for key in ("HERMES_FEISHU_USER_OPEN_ID", "HERMES_SESSION_USER_ID"):
        value = str(os.environ.get(key) or "").strip()
        if _cw._is_feishu_open_id(value):
            return value
    return ""


def infer_cron_owner_context(job: dict, *, profile_home: Path) -> dict[str, Any]:
    """Return inferred owner context for a cron job without mutating it."""
    owner = str(job.get("owner_open_id") or "").strip()
    owner_profile = str(job.get("owner_profile") or "").strip()
    if not _cw._is_feishu_open_id(owner):
        owner = _cw._session_owner_open_id()
    if not _cw._is_feishu_open_id(owner):
        owner, routed_profile = _cw._routing_owner_for_profile(profile_home)
        if not owner_profile:
            owner_profile = routed_profile
    if not _cw._is_feishu_open_id(owner):
        return {}
    if not owner_profile:
        owner_profile = Path(profile_home).expanduser().name
    context: dict[str, Any] = {"owner_open_id": owner, "owner_profile": owner_profile}

    # Expert cron routing label. Set ONLY from the dedicated app-id env, which the
    # expert bot forwards to its create subprocess. We deliberately do NOT read
    # HERMES_MULTITENANCY_FIXED_EXPERT here: it is banned from the subprocess
    # allowlist (it would flip _may_own_feishu_runtime() in a per-user subprocess),
    # and the app-id env alone is a routing label — never an ownership grant, and
    # only the expert gateway ever sets it (so per-user/router create subprocesses
    # stay untagged = regression-safe). The expert_id itself is derived at execute
    # time from the OWNING gateway's trusted identity, not persisted here.
    from ..expert_bot_route import fixed_expert_app_id_from_env

    source_app = fixed_expert_app_id_from_env()
    if not source_app:
        return context
    context.update({"source_app": source_app, "writable_authorized": True})
    return context


def with_cron_owner_context(job: dict, *, profile_home: Path) -> dict:
    """Return a copy of ``job`` with inferred owner context filled in."""
    context = _cw.infer_cron_owner_context(job, profile_home=profile_home)
    if not context:
        return dict(job)
    updated = dict(job)
    updated.setdefault("owner_open_id", context["owner_open_id"])
    updated.setdefault("owner_profile", context["owner_profile"])
    if not _cw._is_feishu_open_id(updated.get("owner_open_id")):
        updated["owner_open_id"] = context["owner_open_id"]
    if not str(updated.get("owner_profile") or "").strip():
        updated["owner_profile"] = context["owner_profile"]
    # OWNER-ONLY on purpose. Do NOT stamp source_app / writable_authorized here:
    # this runs during backfill/scan across ALL profiles, so stamping the expert
    # gateway's labels onto a legacy/user job would silently convert ordinary cron
    # into expert writable cron and steal it from the router. Source labels are
    # created ONLY in the create hook (infer_cron_owner_context via create_job) —
    # i.e. by the user's act of creating the job in the expert bot.
    return updated


def backfill_cron_owner_context_for_profile(profile_home: Path) -> dict[str, Any]:
    """Persist missing owner fields for legacy profile cron jobs.

    This is metadata-only: it never changes prompt/schedule/enabled/run state and
    never executes or delivers a job.
    """
    profile_home = Path(profile_home).expanduser().resolve()
    jobs_file = profile_home / "cron" / "jobs.json"
    if not jobs_file.is_file():
        return {"checked": 0, "updated": 0, "path": str(jobs_file)}
    try:
        raw = json.loads(jobs_file.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("[multitenancy] failed to read cron jobs for owner backfill: %s", jobs_file)
        return {"checked": 0, "updated": 0, "path": str(jobs_file), "error": "read_failed"}
    if isinstance(raw, dict):
        jobs = raw.get("jobs")
    else:
        jobs = raw
    if not isinstance(jobs, list):
        return {"checked": 0, "updated": 0, "path": str(jobs_file), "error": "invalid_shape"}

    changed = False
    updated_count = 0
    new_jobs = []
    for job in jobs:
        if not isinstance(job, dict):
            new_jobs.append(job)
            continue
        checked_job = _cw.with_cron_owner_context(job, profile_home=profile_home)
        if (
            checked_job.get("owner_open_id") != job.get("owner_open_id")
            or checked_job.get("owner_profile") != job.get("owner_profile")
        ):
            changed = True
            updated_count += 1
            logger.info(
                "[multitenancy] backfilled cron owner context profile=%s job=%s owner=%s",
                profile_home.name,
                checked_job.get("id") or "",
                checked_job.get("owner_open_id") or "",
            )
        new_jobs.append(checked_job)

    if not changed:
        return {"checked": len(jobs), "updated": 0, "path": str(jobs_file)}
    new_raw = copy.deepcopy(raw)
    if isinstance(new_raw, dict):
        new_raw["jobs"] = new_jobs
    else:
        new_raw = new_jobs
    tmp = jobs_file.with_suffix(jobs_file.suffix + ".tmp")
    tmp.write_text(json.dumps(new_raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, jobs_file)
    try:
        os.chmod(jobs_file, 0o600)
    except OSError:
        pass
    return {"checked": len(jobs), "updated": updated_count, "path": str(jobs_file)}
