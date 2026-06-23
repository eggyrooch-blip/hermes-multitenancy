"""Multi-profile cron worker for multitenancy.

Hermes-agent's built-in cron ticker scans a single ``<HERMES_HOME>/cron/jobs.json``
file. In multitenancy mode each user has their own profile directory under
``<root>/profiles/<name>/``, and ``_configure_cron_home`` lets each AIAgent
subprocess write reminders to its own profile-default cron path
(``<target_profile>/cron/jobs.json``).

This module starts a single background thread inside the gateway process that
periodically scans every profile under ``<root>/profiles/*/cron/jobs.json``
and reuses hermes-agent's native ``cron.scheduler.tick()`` to dispatch due
jobs. The worker is started by a plugin-installed gateway watcher once the
router has connected at least one adapter, with the pre-dispatch hook kept as
a lazy fallback.

Race safety: the worker mutates module-level constants in ``cron.jobs`` to
point at each profile in turn, holds an internal lock for the
patch-then-tick window, and restores the originals in a ``finally`` block.
``cron.scheduler.tick`` already uses a file lock keyed by jobs path, so even
if the gateway's own ticker races on the patched constant the worst case is
a duplicate-tick attempt that resolves into a no-op.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FuturesTimeout
import functools
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
import copy
from urllib import error as urllib_error
from urllib import request as urllib_request
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Optional

from .credential_renewal_common import (
    clear_needs_reauth_marker,
    classify_uat_payload,
    find_marker_for_open_id,
    iter_uat_locations,
    marker_requires_reauth,
    payload_access_expired,
    payload_refresh_expired,
    read_needs_reauth_marker,
)
from .feishu_adapter_compat import load_feishu_adapter, log_feishu_adapter_load_error
from .feishu_inbound_richtext import install_feishu_inbound_richtext_patch
from .run_broker import RunBroker
from .run_models import RunRequest

logger = logging.getLogger(__name__)

_worker_lock = threading.Lock()
_worker_started = False
_worker_thread: Optional[threading.Thread] = None
_worker_stop: Optional[threading.Event] = None
_cron_module_patch_lock = threading.Lock()
_runtime_patches_installed = False
_gateway_watcher_installed = False
_watcher_attr = "_hermes_multitenancy_cron_watch_scheduled"
_CRON_VISIBLE_RESPONSE_INSTRUCTION = (
    "\n\n[Multitenancy cron delivery override: Do not respond with [SILENT]. "
    "If there is nothing new to report, explicitly say that this run completed "
    "and no matching item needed a reminder.]"
)


def ensure_cron_worker_started(gateway: Any) -> None:
    """Start the multi-profile cron worker once, on first hook dispatch.

    No-op when gateway adapters are not yet ready, when no asyncio loop is
    running, or when HERMES_HOME does not look like a multitenancy nested
    layout (``<root>/profiles/<name>``).
    """
    global _worker_started, _worker_thread, _worker_stop
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        adapters = getattr(gateway, "adapters", None)
        if not adapters:
            logger.info("[multitenancy] cron worker delayed: adapters not ready")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.info("[multitenancy] cron worker delayed: no running loop")
            return
        profiles_root = _resolve_profiles_root()
        if profiles_root is None:
            return
        _worker_stop = threading.Event()
        _worker_thread = threading.Thread(
            target=_multiprofile_cron_worker,
            args=(profiles_root, adapters, loop, _worker_stop),
            daemon=True,
            name="multitenancy-cron-worker",
        )
        _worker_thread.start()
        _worker_started = True
        logger.info(
            "[multitenancy] multi-profile cron worker started (scanning %s)",
            profiles_root,
        )


def install_cron_runtime_patches() -> None:
    """Install multitenancy cron delivery adapters without modifying Hermes files."""
    global _runtime_patches_installed
    if _runtime_patches_installed:
        return
    _patch_cron_run_broker()
    _patch_scheduler_owner_open_id_delivery()
    _patch_cron_delivery_mirror()
    _patch_feishu_open_id_send()
    _patch_feishu_outbound_link_render()
    install_feishu_inbound_richtext_patch()
    from .feishu_merge_forward_api import install_feishu_merge_forward_api_patch
    install_feishu_merge_forward_api_patch()
    from .feishu_reply_quote_api import install_feishu_reply_quote_api_patch
    install_feishu_reply_quote_api_patch()
    from .feishu_reaction_lifecycle import install_feishu_reaction_lifecycle_patch
    install_feishu_reaction_lifecycle_patch()
    from .feishu_group_valve import install_feishu_group_valve_patch
    install_feishu_group_valve_patch()
    _runtime_patches_installed = True


def install_profile_native_cron_guard() -> None:
    """Prevent non-router profile gateways from executing profile cron jobs.

    The router-owned multi-profile worker scans all profile cron stores with
    live Feishu adapters and RunBroker credentials. A non-router profile gateway
    may still import upstream ``cron.scheduler``; if it ticks the same profile
    store it can bypass RunBroker and then fail delivery. In multitenancy
    profile layouts, make that native tick yield to the router unless explicitly
    re-enabled for emergency diagnostics.
    """
    try:
        import cron.scheduler as scheduler
    except Exception:
        logger.exception("[multitenancy] failed to patch profile native cron guard")
        return

    original = getattr(scheduler, "tick", None)
    if original is None or getattr(original, "_hermes_multitenancy_profile_guard", False):
        return

    @functools.wraps(original)
    def tick(*args: Any, **kwargs: Any) -> int:
        profile_home = _current_profile_home()
        if profile_home.parent.name == "profiles" and profile_home.name != "multitenancy_router":
            if not _profile_native_cron_enabled():
                logger.info(
                    "[multitenancy] profile native cron tick skipped for %s; router worker owns cron execution",
                    profile_home.name,
                )
                return 0
            logger.warning(
                "[multitenancy] profile native cron tick escape enabled for %s; "
                "this bypasses router-owned RunBroker cron execution",
                profile_home.name,
            )
        return original(*args, **kwargs)

    setattr(tick, "_hermes_multitenancy_profile_guard", True)
    setattr(tick, "_hermes_multitenancy_original", original)
    scheduler.tick = tick
    logger.info("[multitenancy] patched profile native cron tick guard")


def install_gateway_startup_watcher() -> None:
    """Start the cron worker after GatewayRunner connects adapters.

    Hermes core has no generic "gateway started" plugin hook in the upstream
    code we run against. Rather than patching core files, the plugin wraps
    ``GatewayRunner._create_adapter`` early in startup and schedules a small
    async watcher on the same event loop. Once the gateway's adapter map is
    populated, the normal worker start path receives the live gateway object.
    """
    global _gateway_watcher_installed
    if _gateway_watcher_installed:
        return
    try:
        from gateway.run import GatewayRunner
    except Exception:
        logger.exception("[multitenancy] failed to install gateway cron startup watcher")
        return

    original = getattr(GatewayRunner, "_create_adapter", None)
    if original is None or getattr(original, "_hermes_multitenancy_patched", False):
        _gateway_watcher_installed = True
        return

    @functools.wraps(original)
    def wrapped_create_adapter(self: Any, *args: Any, **kwargs: Any) -> Any:
        adapter = original(self, *args, **kwargs)
        _schedule_startup_watch(self)
        return adapter

    setattr(wrapped_create_adapter, "_hermes_multitenancy_patched", True)
    GatewayRunner._create_adapter = wrapped_create_adapter
    _gateway_watcher_installed = True
    logger.info("[multitenancy] installed gateway cron startup watcher")


def _schedule_startup_watch(gateway: Any) -> None:
    try:
        from .webui_broker_server import ensure_run_broker_server_started

        ensure_run_broker_server_started()
    except Exception:
        logger.exception("[multitenancy] failed to schedule WebUI run broker sidecar")
    if getattr(gateway, _watcher_attr, False):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.info("[multitenancy] cron startup watcher delayed: no running loop")
        return
    setattr(gateway, _watcher_attr, True)
    task = loop.create_task(_start_worker_when_adapters_ready(gateway))
    task.add_done_callback(_log_startup_watch_failure)


async def _start_worker_when_adapters_ready(gateway: Any, attempts: int = 90) -> None:
    for _ in range(attempts):
        if getattr(gateway, "adapters", None):
            ensure_cron_worker_started(gateway)
            return
        await asyncio.sleep(1)
    logger.warning("[multitenancy] cron worker not started: gateway adapters stayed empty")


def _log_startup_watch_failure(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception as err:
        logger.error("[multitenancy] cron startup watcher result unavailable: %s", err)
        return
    if exc is not None:
        logger.error(
            "[multitenancy] cron startup watcher failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def _patch_scheduler_owner_open_id_delivery() -> None:
    try:
        import cron.scheduler as scheduler
    except Exception:
        logger.exception("[multitenancy] failed to patch cron owner delivery")
        return

    original = getattr(scheduler, "_resolve_single_delivery_target", None)
    if original is None or getattr(original, "_hermes_multitenancy_patched", False):
        return

    @functools.wraps(original)
    def resolve_single_delivery_target(job: dict, deliver_value: str) -> Optional[dict]:
        target = original(job, deliver_value)
        if target is not None:
            return target
        if str(deliver_value).strip().lower() != "feishu":
            return None
        owner_open_id = str(job.get("owner_open_id") or "").strip()
        if not owner_open_id.startswith("ou_"):
            return None
        return {
            "platform": "feishu",
            "chat_id": owner_open_id,
            "thread_id": None,
        }

    setattr(resolve_single_delivery_target, "_hermes_multitenancy_patched", True)
    scheduler._resolve_single_delivery_target = resolve_single_delivery_target
    logger.info("[multitenancy] patched cron delivery fallback to owner open_id")


def _cron_run_broker_enabled() -> bool:
    value = os.environ.get("HERMES_MULTITENANCY_CRON_RUN_BROKER", "").strip().lower()
    if not value:
        return True
    return value in {"1", "true", "yes", "on"}


def _profile_native_cron_enabled() -> bool:
    value = os.environ.get("HERMES_MULTITENANCY_PROFILE_NATIVE_CRON", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _patch_cron_run_broker() -> None:
    try:
        import cron.scheduler as scheduler
    except Exception:
        logger.exception("[multitenancy] failed to patch cron run broker")
        return

    original = getattr(scheduler, "run_job", None)
    if original is None or getattr(original, "_hermes_multitenancy_patched", False):
        return

    @functools.wraps(original)
    def run_job(job: dict) -> tuple[bool, str, str, Optional[str]]:
        deferred = _l4_check_needs_reauth_and_defer(job)
        if deferred is not None:
            return deferred
        if not _cron_run_broker_enabled():
            return original(job)
        return _run_job_through_broker(job, scheduler)

    setattr(run_job, "_hermes_multitenancy_patched", True)
    setattr(run_job, "_hermes_multitenancy_original", original)
    scheduler.run_job = run_job
    logger.info("[multitenancy] patched cron run_job with opt-in RunBroker path")


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
    profile_home = _current_profile_home()
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
    return _resolve_shared_home()


def _routing_owner_for_profile(profile_home: Path) -> tuple[str, str]:
    """Infer (owner_open_id, owner_profile) from the active routing table."""
    profile_home = Path(profile_home).expanduser().resolve()
    shared_home = _shared_home_for_profile(profile_home)
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
            return (owner if _is_feishu_open_id(owner) else "", owner_profile)
    except Exception:
        logger.debug("[multitenancy] cron owner route lookup failed", exc_info=True)
        return "", ""


def _session_owner_open_id() -> str:
    for key in ("HERMES_FEISHU_USER_OPEN_ID", "HERMES_SESSION_USER_ID"):
        value = str(os.environ.get(key) or "").strip()
        if _is_feishu_open_id(value):
            return value
    return ""


def infer_cron_owner_context(job: dict, *, profile_home: Path) -> dict[str, str]:
    """Return inferred owner context for a cron job without mutating it."""
    owner = str(job.get("owner_open_id") or "").strip()
    owner_profile = str(job.get("owner_profile") or "").strip()
    if not _is_feishu_open_id(owner):
        owner = _session_owner_open_id()
    if not _is_feishu_open_id(owner):
        owner, routed_profile = _routing_owner_for_profile(profile_home)
        if not owner_profile:
            owner_profile = routed_profile
    if not _is_feishu_open_id(owner):
        return {}
    if not owner_profile:
        owner_profile = Path(profile_home).expanduser().name
    return {"owner_open_id": owner, "owner_profile": owner_profile}


def with_cron_owner_context(job: dict, *, profile_home: Path) -> dict:
    """Return a copy of ``job`` with inferred owner context filled in."""
    context = infer_cron_owner_context(job, profile_home=profile_home)
    if not context:
        return dict(job)
    updated = dict(job)
    updated.setdefault("owner_open_id", context["owner_open_id"])
    updated.setdefault("owner_profile", context["owner_profile"])
    if not _is_feishu_open_id(updated.get("owner_open_id")):
        updated["owner_open_id"] = context["owner_open_id"]
    if not str(updated.get("owner_profile") or "").strip():
        updated["owner_profile"] = context["owner_profile"]
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
        checked_job = with_cron_owner_context(job, profile_home=profile_home)
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


def _l4_check_needs_reauth_and_defer(
    job: dict,
) -> Optional[tuple[bool, str, str, Optional[str]]]:
    """If owner_open_id has a fresh `.needs_reauth` marker, defer the job.

    Returns ``(success=False, output_text, final_response, error)`` shaped like
    ``cron.scheduler.run_job`` so the scheduler records the skip. Side effect:
    writes ``<profile>/cron/output/<job_id>.deferred.json`` recording the
    skip metadata for the audit trail. The L3 notifier still owns the
    user-facing DM; L4 only refuses to enter the lark_cli policy black hole.
    """
    owner_open_id = str(job.get("owner_open_id") or "").strip()
    if not owner_open_id.startswith("ou_"):
        return None
    shared_home = _resolve_shared_home()
    while True:
        marker = find_marker_for_open_id(shared_home, owner_open_id)
        if marker is None:
            return None
        if _clear_stale_reauth_markers_if_uat_recovered(shared_home, owner_open_id, marker):
            return None
        marker_body = read_needs_reauth_marker(marker) or {}
        reason = str(marker_body.get("reason") or "unknown")
        if marker_requires_reauth(marker_body):
            break
        cleared = False
        for non_actionable_marker in _iter_reauth_markers_for_open_id(shared_home, owner_open_id):
            body = read_needs_reauth_marker(non_actionable_marker) or {}
            if not marker_requires_reauth(body):
                clear_needs_reauth_marker(non_actionable_marker)
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
        profile_home = _current_profile_home()
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
        f"该任务已暂缓发送，因为 owner 的飞书 UAT 失效（{reason}）。"
        f"待 owner 重新授权 / 管理员修正 app scope 后会自动恢复。"
    )
    return False, output, "", error_msg


def _clear_stale_reauth_markers_if_uat_recovered(
    shared_home: Path,
    owner_open_id: str,
    marker: Path,
) -> bool:
    """Return True when a newer, valid UAT makes existing reauth markers stale."""
    try:
        marker_mtime = marker.stat().st_mtime
    except OSError:
        return False

    recovered_mtime: float | None = None
    for loc in iter_uat_locations(shared_home):
        if loc.open_id != owner_open_id:
            continue
        try:
            uat_mtime = loc.path.stat().st_mtime
            if uat_mtime <= marker_mtime:
                continue
            payload = json.loads(loc.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if classify_uat_payload(payload) is not None:
            continue
        if payload_access_expired(payload) or payload_refresh_expired(payload):
            continue
        recovered_mtime = max(recovered_mtime or 0.0, uat_mtime)

    if recovered_mtime is None:
        return False

    for stale_marker in _iter_reauth_markers_for_open_id(shared_home, owner_open_id):
        try:
            if stale_marker.stat().st_mtime <= recovered_mtime:
                clear_needs_reauth_marker(stale_marker)
        except OSError:
            continue
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


def _cron_user_key(job: dict, profile_name: str) -> str:
    owner_open_id = str(job.get("owner_open_id") or "").strip()
    if not owner_open_id.startswith("ou_"):
        raise ValueError("cron owner_open_id is required")
    return owner_open_id


def _build_cron_run_request(job: dict, *, profile_home: Path, prompt: str) -> RunRequest:
    job = with_cron_owner_context(job, profile_home=profile_home)
    profile_name = str(job.get("owner_profile") or "").strip() or profile_home.name
    if profile_name == "multitenancy_router":
        raise ValueError("cron owner_profile must not be multitenancy_router")
    user_key = _cron_user_key(job, profile_name)
    job_id = str(job.get("id") or "").strip()
    metadata = {
        "job_id": job_id,
        "job_name": str(job.get("name") or job_id or "scheduled task"),
        "deliver": job.get("deliver"),
        "profile_home": str(profile_home),
        "model": job.get("model"),
        "provider": job.get("provider"),
        "base_url": job.get("base_url"),
        "skills": job.get("skills"),
        "workdir": job.get("workdir"),
    }
    return RunRequest(
        channel="cron",
        profile_name=profile_name,
        user_key=user_key,
        content=prompt,
        chat_id=user_key,
        session_id=f"cron:{job_id}" if job_id else None,
        message_id=job_id or None,
        idempotency_key=f"cron:{profile_name}:{user_key}:{job_id}" if job_id else None,
        delivery_mode=str(job.get("deliver") or "feishu"),
        credential_subject=user_key,
        requires_host_tools=True,
        metadata={k: v for k, v in metadata.items() if v not in (None, "", [])},
    )


def _cron_deliver_target(job: dict, user_key: str) -> Optional[dict[str, Any]]:
    deliver = str(job.get("deliver") or "feishu").strip().lower()
    if deliver == "local":
        return None
    if deliver == "feishu" and user_key.startswith("ou_"):
        return {"platform": "feishu", "chat_id": user_key, "thread_id": None}
    return None


def plan_cron_bridge_run(
    job: dict,
    *,
    profile_home: Path,
    due: Optional[bool] = None,
    shadow: bool = True,
) -> dict[str, Any]:
    """Return a secret-free profile cron bridge plan without dispatching.

    The plan intentionally omits prompt text, env vars, credentials, and model
    secrets. It is used by WebUI/runbook canaries to compare upstream
    profile-scoped cron metadata with the multitenancy RunRequest boundary.
    """
    profile_home = profile_home.expanduser().resolve()
    job_id = str(job.get("id") or "").strip()
    problems: list[str] = []
    request: Optional[RunRequest] = None
    planned_job = with_cron_owner_context(job, profile_home=profile_home)
    try:
        request = _build_cron_run_request(
            planned_job,
            profile_home=profile_home,
            prompt=str(job.get("prompt") or "cron bridge shadow prompt"),
        )
    except Exception as exc:
        problems.append(str(exc))

    profile_name = request.profile_name if request is not None else str(planned_job.get("owner_profile") or profile_home.name)
    user_key = request.user_key if request is not None else str(planned_job.get("owner_open_id") or "")
    enabled = bool(job.get("enabled", True)) and str(job.get("state") or "scheduled").strip().lower() != "paused"
    due_value = bool(due) if due is not None else enabled
    would_execute = bool(request is not None and enabled and due_value and not problems)
    deliver_target = _cron_deliver_target(planned_job, user_key) if request is not None else None
    if would_execute and str(planned_job.get("deliver") or "feishu").strip().lower() != "local" and deliver_target is None:
        problems.append("cron deliver target could not be resolved")
        would_execute = False

    return {
        "mode": "shadow" if shadow else "execute",
        "job_id": job_id,
        "job_name": str(job.get("name") or job_id or "scheduled task"),
        "profile_name": profile_name,
        "profile_home": str(profile_home),
        "user_key": user_key,
        "credential_subject": request.credential_subject if request is not None else None,
        "channel": "cron",
        "session_id": request.session_id if request is not None else None,
        "idempotency_key": request.effective_idempotency_key if request is not None else None,
        "deliver": planned_job.get("deliver") or "feishu",
        "deliver_target": deliver_target,
        "next_run_at": job.get("next_run_at") or job.get("next_run"),
        "enabled": enabled,
        "due": due_value,
        "would_execute": would_execute,
        "will_execute": would_execute and not shadow,
        "problems": problems,
        "secret_free": True,
    }


def _build_cron_event(request: RunRequest) -> Any:
    return SimpleNamespace(
        text=request.content,
        message_id=request.message_id,
        channel="cron",
        source=SimpleNamespace(
            user_id=request.user_key,
            open_id=request.user_key,
            user_id_alt=None,
        ),
        raw_event={
            "channel": request.channel,
            "session_id": request.session_id,
            "metadata": dict(request.metadata or {}),
        },
    )


def _force_visible_cron_prompt(prompt: str) -> str:
    text = str(prompt or "")
    if "Do not respond with [SILENT]" in text:
        return text
    return text + _CRON_VISIBLE_RESPONSE_INSTRUCTION


def _cron_response_is_silent(content: str) -> bool:
    return "[SILENT]" in str(content or "").strip().upper()


def _visible_cron_response(job: dict, content: str) -> str:
    text = str(content or "").strip()
    if text and not _cron_response_is_silent(text):
        return text
    job_name = str(job.get("name") or job.get("id") or "定时任务")
    return f"定时任务「{job_name}」已执行完成：本次没有发现需要提醒的事项。"


async def _dispatch_cron_request(request: RunRequest, profile_home: Path) -> str:
    from . import router

    event = _build_cron_event(request)
    return await router._get_pool().dispatch(request.profile_name, profile_home, event)


def _run_job_through_broker(job: dict, scheduler: Any) -> tuple[bool, str, str, Optional[str]]:
    profile_home = _current_profile_home()
    job = with_cron_owner_context(job, profile_home=profile_home)
    job_id = str(job.get("id") or "")
    job_name = str(job.get("name") or job_id or "scheduled task")
    try:
        build_prompt = getattr(scheduler, "_build_job_prompt")
        prompt = _force_visible_cron_prompt(build_prompt(job, prerun_script=None))
        request = _build_cron_run_request(job, profile_home=profile_home, prompt=prompt)
        broker = RunBroker(
            dispatch_agent=lambda run_request: _dispatch_cron_request(run_request, profile_home),
            sandbox_available=lambda: os.environ.get("HERMES_USE_SANDBOX", "").strip().lower()
            in {"1", "true", "yes", "on"},
        )
        result = asyncio.run(broker.run(request))
        final_response = _visible_cron_response(job, result.content)
        output = (
            f"# Cron Job: {job_name}\n\n"
            f"**Job ID:** {job_id}\n"
            f"**Run Path:** RunBroker\n\n"
            f"{final_response}"
        )
        return True, output, final_response, None
    except Exception as exc:
        error = str(exc)
        output = (
            f"# Cron Job: {job_name}\n\n"
            f"**Job ID:** {job_id}\n"
            f"**Run Path:** RunBroker\n\n"
            f"Error: {error}"
        )
        return False, output, "", error


def _run_broker_base_url() -> str:
    explicit = (
        os.environ.get("HERMES_RUN_BROKER_URL")
        or os.environ.get("HERMES_MULTITENANCY_RUN_BROKER_URL")
        or ""
    ).strip()
    if explicit:
        return explicit.rstrip("/")
    host = os.environ.get("HERMES_MULTITENANCY_RUN_BROKER_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.environ.get("HERMES_MULTITENANCY_RUN_BROKER_PORT", "8766").strip() or "8766"
    return f"http://{host}:{port}"


def _dotenv_lookup(path: Path, keys: set[str]) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() in keys:
            return value.strip().strip("'\"")
    return ""


def _run_broker_key_for_profile(profile_home: Path) -> str:
    value = (
        os.environ.get("HERMES_RUN_BROKER_KEY")
        or os.environ.get("HERMES_MULTITENANCY_RUN_BROKER_KEY")
        or ""
    ).strip()
    if value:
        return value
    shared_home = _shared_home_for_profile(profile_home)
    return _dotenv_lookup(
        shared_home / ".env",
        {"HERMES_RUN_BROKER_KEY", "HERMES_MULTITENANCY_RUN_BROKER_KEY"},
    ).strip()


def trigger_profile_cron_job_via_run_broker(
    *,
    job_id: str,
    profile_home: Path,
    owner_open_id: str,
) -> dict[str, Any]:
    """Forward native cronjob(action=run) to the router-owned RunBroker API."""
    profile_home = Path(profile_home).expanduser().resolve()
    profile_name = profile_home.name
    owner = str(owner_open_id or "").strip()
    if not _is_feishu_open_id(owner):
        raise RuntimeError("cron owner_open_id is required for RunBroker trigger")
    job = str(job_id or "").strip()
    if not job:
        raise RuntimeError("cron job_id is required for RunBroker trigger")

    url = f"{_run_broker_base_url()}/api/run-broker/jobs/{job}/run"
    headers = {
        "Content-Type": "application/json",
        "X-Hermes-Profile": profile_name,
        "X-Hermes-User-Key": owner,
    }
    key = _run_broker_key_for_profile(profile_home)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = b"{}"
    req = urllib_request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"RunBroker cron trigger failed ({exc.code}): {body}") from exc
    except OSError as exc:
        raise RuntimeError(f"RunBroker cron trigger failed: {exc}") from exc

    try:
        payload = json.loads(body or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"RunBroker cron trigger returned invalid JSON: {body[:200]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("RunBroker cron trigger returned invalid payload")
    if payload.get("error"):
        raise RuntimeError(f"RunBroker cron trigger failed: {payload.get('error')}")
    result = payload.get("job") or payload
    if not isinstance(result, dict):
        raise RuntimeError("RunBroker cron trigger response did not include a job")
    return result


def _patch_cron_delivery_mirror() -> None:
    try:
        import cron.scheduler as scheduler
    except Exception:
        logger.exception("[multitenancy] failed to patch cron delivery mirror")
        return

    original = getattr(scheduler, "_deliver_result", None)
    if original is None or getattr(original, "_hermes_multitenancy_patched", False):
        return

    @functools.wraps(original)
    def deliver_result(job: dict, content: str, adapters: Any = None, loop: Any = None) -> Optional[str]:
        # Deliver Feishu cron output as a STREAMING CardKit card (same UX as a
        # normal agent reply: streaming print + rendered markdown + Done footer)
        # before core falls back to flattened plain text. Only when every target
        # is Feishu, a streaming-capable live adapter is present, and there is no
        # media. Any failure returns None so we fall through to core delivery —
        # a cron delivery is never dropped.
        if _cron_card_response_enabled():
            try:
                streamed = _try_deliver_cron_feishu_streaming_card(
                    scheduler, job, content, adapters=adapters, loop=loop
                )
            except Exception:
                logger.warning(
                    "[multitenancy] cron streaming card path raised; using core delivery",
                    exc_info=True,
                )
                streamed = None
            if streamed is True:
                _mirror_cron_delivery_to_owner(job, content)
                return None

        error = original(job, content, adapters=adapters, loop=loop)
        if error is not None and _is_feishu_platform_config_error(error):
            live_error = _deliver_cron_feishu_via_live_adapter(
                scheduler,
                job,
                content,
                adapters=adapters,
                loop=loop,
            )
            if live_error is None:
                _mirror_cron_delivery_to_owner(job, content)
                return None
            logger.warning(
                "[multitenancy] cron live Feishu delivery fallback failed job=%s: %s",
                job.get("id", "?"),
                live_error,
            )
            return f"{error}; live Feishu fallback failed: {live_error}"
        if error is None:
            _mirror_cron_delivery_to_owner(job, content)
        return error

    setattr(deliver_result, "_hermes_multitenancy_patched", True)
    scheduler._deliver_result = deliver_result
    logger.info("[multitenancy] patched cron delivery mirror to owner session")


def _is_feishu_platform_config_error(error: object) -> bool:
    text = str(error or "").lower()
    return "platform 'feishu' not configured/enabled" in text


def _deliver_cron_feishu_via_live_adapter(
    scheduler: Any,
    job: dict,
    content: str,
    *,
    adapters: Any = None,
    loop: Any = None,
) -> Optional[str]:
    """Fallback for multitenancy gateways where Feishu exists only as a live adapter."""
    if adapters is None or loop is None or not getattr(loop, "is_running", lambda: False)():
        return "feishu live adapter unavailable"
    try:
        targets = scheduler._resolve_delivery_targets(job)
    except Exception as exc:
        return f"failed to resolve feishu delivery target: {exc}"

    feishu_targets = [
        target for target in targets
        if str(target.get("platform") or "").strip().lower() == "feishu"
    ]
    if not feishu_targets:
        return "no feishu delivery target resolved"

    adapter = _adapter_for_platform(adapters, "feishu")
    if adapter is None:
        return "feishu live adapter unavailable"

    text_to_send, media_files = _cron_delivery_payload_for_adapter(job, content)

    # Cron deliveries historically go out as plain text via ``adapter.send``,
    # which flattens markdown (bullets/bold/links) — unlike normal replies that
    # stream as interactive CardKit cards. When enabled, render a simple
    # interactive card so scheduled-task output looks like a normal reply.
    card: Optional[dict] = None
    if (
        text_to_send
        and _cron_card_response_enabled()
        and callable(getattr(adapter, "_feishu_send_with_retry", None))
    ):
        try:
            card, card_media = _build_cron_card(job, content)
            if card is not None:
                # Card and text paths extract media from the same content; keep
                # one media list so a card→text fallback never double-sends.
                media_files = card_media
        except Exception:
            logger.warning("[multitenancy] cron card build failed; using text", exc_info=True)
            card = None

    errors: list[str] = []
    for target in feishu_targets:
        chat_id = str(target.get("chat_id") or "").strip()
        if not chat_id:
            errors.append("missing feishu chat_id")
            continue
        thread_id = target.get("thread_id")
        metadata = _feishu_delivery_metadata(chat_id, thread_id)
        try:
            sent_payload = False
            if card is not None:
                card_error = _send_cron_card_via_live_adapter(
                    adapter, chat_id, card, metadata, loop
                )
                if card_error is None:
                    sent_payload = True
                    logger.info(
                        "[multitenancy] cron delivered card to feishu:%s job=%s",
                        chat_id,
                        job.get("id", "?"),
                    )
                else:
                    logger.warning(
                        "[multitenancy] cron card send failed for %s (%s); "
                        "falling back to plain text",
                        chat_id,
                        card_error,
                    )
            if text_to_send and not sent_payload:
                future, schedule_error = _schedule_on_gateway_loop(
                    adapter.send(chat_id, text_to_send, metadata=metadata),
                    loop,
                )
                if schedule_error:
                    errors.append(schedule_error)
                    continue
                if future is None:
                    errors.append(f"feishu live adapter loop unavailable for {chat_id}")
                    continue
                try:
                    result = future.result(timeout=15)
                except FuturesTimeout:
                    future.cancel()
                    raise
                if result and not getattr(result, "success", True):
                    errors.append(
                        f"feishu live adapter send failed for {chat_id}: "
                        f"{getattr(result, 'error', 'unknown')}"
                    )
                    continue
            if media_files:
                media_error = _send_media_files_via_live_adapter(
                    adapter,
                    chat_id,
                    media_files,
                    metadata,
                    loop,
                    job,
                )
                if media_error:
                    errors.append(media_error)
                    continue
            logger.info(
                "[multitenancy] cron delivered to feishu:%s via live adapter job=%s",
                chat_id,
                job.get("id", "?"),
            )
        except Exception as exc:
            errors.append(f"feishu live adapter delivery to {chat_id} failed: {exc}")
    return "; ".join(errors) if errors else None


def _schedule_on_gateway_loop(coro: Any, loop: Any) -> tuple[Any, Optional[str]]:
    try:
        from agent.async_utils import safe_schedule_threadsafe
    except Exception as exc:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None, f"safe_schedule_threadsafe unavailable: {exc}"
    try:
        return safe_schedule_threadsafe(coro, loop), None
    except Exception as exc:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None, f"failed to schedule Feishu live adapter send: {exc}"


def _feishu_delivery_metadata(chat_id: str, thread_id: Any) -> Optional[dict]:
    if not thread_id:
        return None
    # Feishu DMs (`ou_...`) should not receive topic metadata; stale thread_id
    # can create visible topic replies in one-to-one chats.
    if str(chat_id).startswith("ou_"):
        return None
    return {"thread_id": thread_id}


def _adapter_for_platform(adapters: Any, platform_name: str) -> Any:
    if not adapters:
        return None
    target = platform_name.lower()
    try:
        from gateway.config import Platform

        platform = Platform(target)
        adapter = adapters.get(platform)
        if adapter is not None:
            return adapter
    except Exception:
        pass
    for key, adapter in adapters.items():
        candidates = {
            str(key).lower(),
            str(getattr(key, "value", "")).lower(),
            str(getattr(key, "name", "")).lower(),
        }
        if target in candidates or any(candidate.endswith(f".{target}") for candidate in candidates):
            return adapter
    return None


def _cron_delivery_payload_for_adapter(job: dict, content: str) -> tuple[str, list]:
    wrap_response = True
    try:
        from cron.config import load_config

        user_cfg = load_config()
        wrap_response = user_cfg.get("cron", {}).get("wrap_response", True)
    except Exception:
        pass

    if wrap_response:
        task_name = job.get("name", job.get("id", "scheduled task"))
        job_id = job.get("id", "")
        delivery_content = (
            f"Cronjob Response: {task_name}\n"
            f"(job_id: {job_id})\n"
            f"-------------\n\n"
            f"{content}\n\n"
            f"To stop or manage this job, send me a new message (e.g. \"stop reminder {task_name}\")."
        )
    else:
        delivery_content = content

    try:
        from gateway.platforms.base import BasePlatformAdapter

        media_files, cleaned = BasePlatformAdapter.extract_media(delivery_content)
        media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
        return cleaned.strip(), media_files
    except Exception:
        return delivery_content.strip(), []


def _cron_card_response_enabled() -> bool:
    """Whether cron deliveries should render as interactive cards (default on)."""
    try:
        from cron.config import load_config

        return bool(load_config().get("cron", {}).get("card_response", True))
    except Exception:
        return True


def _build_cron_card(job: dict, content: str) -> tuple[Optional[dict[str, Any]], list]:
    """Build a simple Feishu interactive card for a cron delivery.

    Returns ``(card, media_files)``. ``card`` is ``None`` when the body is empty
    (caller then falls back to the plain-text path). Layout mirrors the
    ``wrap_response`` text form — header (task name), markdown body, and a
    "stop this job" footer — but renders markdown as a real card so bullets,
    bold, and links are no longer flattened.
    """
    wrap_response = True
    try:
        from cron.config import load_config

        wrap_response = load_config().get("cron", {}).get("wrap_response", True)
    except Exception:
        pass

    try:
        from gateway.platforms.base import BasePlatformAdapter

        media_files, cleaned = BasePlatformAdapter.extract_media(content)
        media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
        body = cleaned.strip()
    except Exception:
        body = content.strip()
        media_files = []

    if not body:
        return None, media_files

    try:
        from .card.markdown_style import _optimize_markdown_style
        from .card.sanitization import _plain_summary

        body_md = _optimize_markdown_style(body)
        summary = _plain_summary(body)
    except Exception:
        body_md = body
        summary = "Hermes"

    task_name = str(job.get("name") or job.get("id") or "Scheduled task")

    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": body_md}]
    if wrap_response:
        stop_hint = (
            "To stop or manage this job, send me a new message "
            f'(e.g. "stop reminder {task_name}").'
        )
        elements.append({"tag": "hr"})
        elements.append(
            {"tag": "markdown", "content": stop_hint, "text_size": "notation"}
        )

    card: dict[str, Any] = {
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
            "locales": ["zh_cn", "en_us"],
            "summary": {"content": summary},
        },
        "elements": elements,
    }
    if wrap_response:
        card["header"] = {
            "title": {"tag": "plain_text", "content": f"⏰ {task_name}"},
            "template": "blue",
        }
    return card, media_files


def _send_cron_card_via_live_adapter(
    adapter: Any,
    chat_id: str,
    card: dict[str, Any],
    metadata: Optional[dict],
    loop: Any,
) -> Optional[str]:
    """Send one interactive card on the gateway loop. Returns error str or None.

    This MUST NEVER raise: any failure (serialize, synchronous adapter raise,
    scheduling, timeout, coroutine exception, non-success response) is returned
    as an error string so the caller can fall back to the plain-text path. An
    escaped exception here would skip the text fallback and silently drop the
    cron delivery.
    """
    try:
        payload = json.dumps(card, ensure_ascii=False)
    except Exception as exc:
        return f"card serialize failed: {exc}"

    try:
        # ``_feishu_send_with_retry`` may raise synchronously while building the
        # coroutine; keep it inside the guard.
        coro = adapter._feishu_send_with_retry(
            chat_id=chat_id,
            msg_type="interactive",
            payload=payload,
            reply_to=None,
            metadata=metadata,
        )
        future, schedule_error = _schedule_on_gateway_loop(coro, loop)
        if schedule_error:
            return schedule_error
        if future is None:
            return f"feishu live adapter loop unavailable for {chat_id}"
        try:
            response = future.result(timeout=15)
        except FuturesTimeout:
            future.cancel()
            return f"feishu card send timed out for {chat_id}"
        # Normalize the raw adapter response the same way the streaming-card
        # paths do; a bare response has no ``.success`` attribute, so the naive
        # ``getattr(result, "success", True)`` would treat a failed send (code
        # != 0, no message_id) as success and skip the text fallback. If
        # ``_finalize`` is unavailable or raises, the outer ``except`` below
        # returns an error string so the caller still falls back to text.
        from .card.card_error import _finalize

        result = _finalize(adapter, response, "cron card send failed")
        if result is not None and not getattr(result, "success", True):
            return (
                f"feishu card send failed for {chat_id}: "
                f"{getattr(result, 'error', 'unknown')}"
            )
        return None
    except Exception as exc:
        return f"feishu card send raised for {chat_id}: {exc}"


def _send_media_files_via_live_adapter(
    adapter: Any,
    chat_id: str,
    media_files: list,
    metadata: Optional[dict],
    loop: Any,
    job: dict,
) -> Optional[str]:
    try:
        import cron.scheduler as scheduler
        from gateway.config import Platform

        scheduler._send_media_via_adapter(
            adapter,
            chat_id,
            media_files,
            metadata,
            loop,
            job,
            platform=Platform("feishu"),
        )
        return None
    except Exception:
        logger.warning("[multitenancy] cron media delivery via live Feishu adapter failed", exc_info=True)
        return f"cron media delivery via live Feishu adapter failed job={job.get('id', '?')}"


def _mirror_cron_delivery_to_owner(job: dict, content: str) -> None:
    owner_open_id = str(job.get("owner_open_id") or "").strip()
    if not owner_open_id.startswith("ou_"):
        return

    owner_profile = str(job.get("owner_profile") or "").strip()
    if not owner_profile:
        try:
            from . import router

            resolved_profile, profile_home = router._resolve_route(owner_open_id)
            if profile_home is not None:
                owner_profile = resolved_profile
        except Exception:
            logger.debug("[multitenancy] cron mirror route lookup failed", exc_info=True)
    if not owner_profile:
        return

    job_name = str(job.get("name") or job.get("id") or "scheduled task")
    job_id = str(job.get("id") or "")
    mirrored_content = (
        f"[Scheduled task delivery]\n"
        f"Task: {job_name}\n"
        f"Job ID: {job_id}\n\n"
        f"{content}"
    )

    try:
        from . import router

        # Use the typed SessionScope construction point (channel="cron") rather
        # than a raw (profile, open_id) tuple, so cron history goes through the
        # single key authority. Default mode → byte-identical (owner_profile,
        # owner_open_id); strict mode → an isolated cron-channel key so cron
        # deliveries don't cross into the owner's DM/webui sessions.
        key = router._history_key(owner_profile, owner_open_id, None, channel="cron")
        existing = router._session_history.get(key, [])
        router._session_history[key] = router._trim_history(
            existing + [{"role": "assistant", "content": mirrored_content}]
        )
        store = router._get_session_store()
        if store is not None:
            store.append(key[0], key[1], "assistant", mirrored_content)
        logger.info(
            "[multitenancy] mirrored cron delivery to profile session profile=%s job=%s",
            owner_profile,
            job_id,
        )
    except Exception:
        logger.exception("[multitenancy] failed to mirror cron delivery to owner session")


def _patch_feishu_open_id_send() -> None:
    try:
        FeishuAdapter = load_feishu_adapter()
    except Exception as exc:
        log_feishu_adapter_load_error(
            logger,
            "[multitenancy] FeishuAdapter not importable yet; open_id delivery patch deferred",
            exc,
        )
        return

    original = getattr(FeishuAdapter, "_send_raw_message", None)
    if original is None or getattr(original, "_hermes_multitenancy_patched", False):
        return

    @functools.wraps(original)
    async def send_raw_message(
        self: Any,
        *,
        chat_id: str,
        msg_type: str,
        payload: str,
        reply_to: Optional[str],
        metadata: Optional[dict],
    ) -> Any:
        if reply_to or not str(chat_id).startswith("ou_"):
            return await original(
                self,
                chat_id=chat_id,
                msg_type=msg_type,
                payload=payload,
                reply_to=reply_to,
                metadata=metadata,
            )

        body = self._build_create_message_body(
            receive_id=chat_id,
            msg_type=msg_type,
            content=payload,
            uuid_value=str(uuid.uuid4()),
        )
        request = self._build_create_message_request("open_id", body)
        response = await asyncio.to_thread(self._client.im.v1.message.create, request)
        logger.info(
            "[multitenancy] delivered Feishu open_id message chat_id=%s message_id=%s",
            chat_id,
            _feishu_response_message_id(response) or "unknown",
        )
        return response

    setattr(send_raw_message, "_hermes_multitenancy_patched", True)
    FeishuAdapter._send_raw_message = send_raw_message
    logger.info("[multitenancy] patched Feishu delivery for user open_id targets")


# Matches a markdown link ``[label](url)`` but NOT an image ``![label](url)``
# (the ``(?<!!)`` lookbehind skips the image form so we don't emit ``!label``).
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")


def _linkify_markdown_links_in_text(text: str) -> str:
    """Rewrite ``[label](url)`` -> ``label (url)`` so bare URLs survive.

    Feishu auto-linkifies bare URLs in plain-text messages but does NOT render
    markdown link syntax. Upstream ``_build_outbound_payload`` force-sends any
    table-containing content as raw plain text, so ``[label](url)`` arrives
    literally and is not clickable. Converting to ``label (url)`` exposes the
    bare URL, which Feishu turns into a working link.

    Known edges (acceptable for the social-radar report, whose URLs are
    percent-encoded and contain no literal ``)`` or images):
    - This is a regex, not a full markdown parser; a URL containing a literal
      ``)`` would be truncated at the first paren.
    - A ``[label](url)`` split across upstream message-truncation chunks is not
      repaired (the split happens before this runs); report rows are short so
      this does not occur in practice.
    """
    return _MD_LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2).strip()})", text)


# Feishu's card ``markdown`` element renders bold / lists / links / emoji, but
# NOT ``#`` headings or GFM pipe tables (verified empirically on prod tenant).
# Like openclaw-lark's outbound path, we CONVERT those to the renderable subset
# before placing the content in a card, instead of dumping raw markdown.
_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$")
_TABLE_ROW_RE = re.compile(r"(?m)^\s*\|.*\|\s*$")
def _split_table_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _convert_md_tables_for_card(text: str) -> str:
    """Convert GFM pipe tables to bullet lines (Feishu cards don't render tables).

    2-column tables (the common key/value shape, e.g. weather) become
    ``- **key**：value``; wider tables become ``- **c0** · h1: c1 · h2: c2``.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if (
            _TABLE_ROW_RE.match(line)
            and i + 1 < n
            and _TABLE_SEP_RE.match(lines[i + 1])
        ):
            header = _split_table_row(line)
            j = i + 2
            rows: list[list[str]] = []
            while j < n and _TABLE_ROW_RE.match(lines[j]):
                rows.append(_split_table_row(lines[j]))
                j += 1
            for r in rows:
                if len(header) == 2 and len(r) >= 2:
                    out.append(f"- **{r[0]}**：{r[1]}")
                else:
                    parts: list[str] = []
                    for k, cell in enumerate(r):
                        if k == 0:
                            parts.append(f"**{cell}**")
                        else:
                            h = header[k] if k < len(header) else ""
                            parts.append(f"{h + ': ' if h else ''}{cell}")
                    out.append("- " + " · ".join(p for p in parts if p))
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _cardify_markdown_for_feishu(text: str) -> str:
    """Render markdown into the subset a Feishu card ``markdown`` element shows.

    Headings -> bold; pipe tables -> bullet lines. Bold / lists / links / emoji
    are already rendered by the card, so they pass through unchanged.
    """
    converted = _HEADING_RE.sub(lambda m: f"**{m.group(1).strip()}**", text)
    converted = _convert_md_tables_for_card(converted)
    return converted


def _build_cron_card_body(job: dict, content: str) -> str:
    """Build the streaming-card body for a cron delivery.

    A clean title (the task name) + the agent content converted to the
    Feishu-card-renderable markdown subset (headings -> bold, tables -> bullet
    key/values) + a small stop-job hint. No "Cronjob Response:" plain wrapper —
    the card already frames it like a normal reply.
    """
    task_name = str(job.get("name") or job.get("id") or "").strip()
    body = _cardify_markdown_for_feishu(content.strip())
    parts: list[str] = []
    if task_name:
        parts.append(f"**⏰ {task_name}**")
    parts.append(body)
    if task_name:
        parts.append(f'_停止该任务：回复 “stop reminder {task_name}”_')
    return "\n\n".join(p for p in parts if p)


def _stream_cron_card_on_loop(
    adapter: Any,
    chat_id: str,
    body: str,
    metadata: Optional[dict],
    loop: Any,
) -> Optional[str]:
    """Drive start -> stream -> finalize of a streaming card. Returns err or None.

    Reuses the same CardKit streaming surface a normal reply uses, so cron output
    streams in with the print effect and finalizes with the Done footer. Never
    raises: any failure is returned as a string so the caller falls back to core
    plain-text delivery (a delivery is never dropped).
    """
    try:
        future, sched_err = _schedule_on_gateway_loop(
            adapter.start_streaming_card(chat_id=chat_id, metadata=metadata), loop
        )
        if sched_err:
            return sched_err
        if future is None:
            return f"streaming loop unavailable for {chat_id}"
        try:
            start_res = future.result(timeout=15)
        except FuturesTimeout:
            future.cancel()
            return f"streaming card start timed out for {chat_id}"
        message_id = getattr(start_res, "message_id", None)
        if not getattr(start_res, "success", False) or not message_id:
            return f"streaming card start failed: {getattr(start_res, 'error', 'unknown')}"

        # First push streams the content (print effect), second finalizes (footer).
        # Non-final failure => the body was never shown => return an error so the
        # caller falls back to plain text. Final-only failure => the body is
        # already on screen => tolerate (return None) to avoid a double-send.
        for finalize in (False, True):
            future, sched_err = _schedule_on_gateway_loop(
                adapter.update_streaming_card(
                    chat_id=chat_id,
                    message_id=message_id,
                    content=body,
                    finalize=finalize,
                ),
                loop,
            )
            if sched_err:
                return None if finalize else sched_err
            if future is None:
                return None if finalize else f"streaming loop unavailable for {chat_id}"
            try:
                res = future.result(timeout=15)
            except Exception:
                future.cancel()
                return None if finalize else f"streaming card update failed for {chat_id}"
            if res is not None and not getattr(res, "success", True):
                if finalize:
                    return None
                return (
                    f"streaming card update failed for {chat_id}: "
                    f"{getattr(res, 'error', 'unknown')}"
                )
        return None
    except Exception as exc:
        return f"streaming card raised for {chat_id}: {exc}"


def _try_deliver_cron_feishu_streaming_card(
    scheduler: Any,
    job: dict,
    content: str,
    *,
    adapters: Any = None,
    loop: Any = None,
) -> Optional[bool]:
    """Deliver an all-Feishu cron job as a streaming card.

    Returns ``True`` when every target was delivered as a streaming card, or
    ``None`` when the streaming path is not applicable / failed (caller then
    falls through to core plain-text delivery). Only handles the case where every
    resolved target is Feishu, a streaming-capable live adapter exists, the loop
    is running, and the content carries no media attachments.
    """
    if adapters is None or loop is None or not getattr(loop, "is_running", lambda: False)():
        return None
    try:
        targets = scheduler._resolve_delivery_targets(job)
    except Exception:
        return None
    if not targets:
        return None
    if any(str(t.get("platform") or "").strip().lower() != "feishu" for t in targets):
        return None
    # Single target only: a partial failure across multiple targets would make us
    # return None and let core re-deliver ALL targets, double-sending the one we
    # already streamed. Multi-target cron is rare — let core handle it as text.
    if len(targets) != 1:
        return None

    adapter = _adapter_for_platform(adapters, "feishu")
    if adapter is not None:
        # The CardKit streaming surface is installed lazily on the gateway's
        # Feishu adapter when it handles a normal reply. The cron adapter
        # instance may not have it yet (no inbound message triggered install),
        # so install it on demand — the same surface normal replies use.
        try:
            from .card import ensure_feishu_cardkit_streaming

            ensure_feishu_cardkit_streaming(adapter)
        except Exception:
            logger.warning(
                "[multitenancy] ensure_feishu_cardkit_streaming failed", exc_info=True
            )
    if adapter is None or not callable(getattr(adapter, "start_streaming_card", None)):
        logger.debug(
            "[multitenancy] cron stream: adapter=%s lacks start_streaming_card=%s",
            type(adapter).__name__ if adapter is not None else None,
            callable(getattr(adapter, "start_streaming_card", None)),
        )
        return None
    supports = getattr(adapter, "supports_streaming_card", None)
    if not callable(supports) or not supports():
        logger.debug(
            "[multitenancy] cron stream: supports callable=%s value=%s",
            callable(supports),
            (supports() if callable(supports) else None),
        )
        return None

    # Media deliveries are handled by core's native path (cards don't carry files).
    try:
        from gateway.platforms.base import BasePlatformAdapter

        media_files, cleaned = BasePlatformAdapter.extract_media(content)
        media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
    except Exception:
        # Fail closed: if we can't tell whether there's media, let core deliver
        # (it handles attachments) rather than risk streaming text and dropping files.
        return None
    if media_files:
        return None
    if not cleaned.strip():
        return None

    body = _build_cron_card_body(job, cleaned)
    for target in targets:
        chat_id = str(target.get("chat_id") or "").strip()
        if not chat_id:
            return None
        metadata = _feishu_delivery_metadata(chat_id, target.get("thread_id"))
        err = _stream_cron_card_on_loop(adapter, chat_id, body, metadata, loop)
        if err is not None:
            logger.warning(
                "[multitenancy] cron streaming card failed for %s job=%s: %s",
                chat_id,
                job.get("id", "?"),
                err,
            )
            return None
        logger.info(
            "[multitenancy] cron delivered streaming card to feishu:%s job=%s",
            chat_id,
            job.get("id", "?"),
        )
    return True


def _patch_feishu_outbound_link_render() -> None:
    """Make markdown links clickable in Feishu plain-text (table) messages.

    Wraps ``FeishuAdapter._build_outbound_payload`` without modifying Hermes
    source. When the core builder returns a ``"text"`` payload (which it does
    for any markdown-table content), rewrite ``[label](url)`` to ``label (url)``
    so the bare URL is auto-linkified by Feishu. ``"post"`` payloads already
    render markdown links and are left untouched.
    """
    try:
        FeishuAdapter = load_feishu_adapter()
    except Exception as exc:
        log_feishu_adapter_load_error(
            logger,
            "[multitenancy] FeishuAdapter not importable yet; outbound link render patch deferred",
            exc,
        )
        return

    original = getattr(FeishuAdapter, "_build_outbound_payload", None)
    if original is None or getattr(original, "_hermes_multitenancy_patched", False):
        return

    @functools.wraps(original)
    def build_outbound_payload(self: Any, content: str) -> tuple[str, str]:
        msg_type, payload = original(self, content)
        if msg_type != "text":
            return msg_type, payload
        try:
            data = json.loads(payload)
        except Exception:
            return msg_type, payload
        text = data.get("text") if isinstance(data, dict) else None
        if isinstance(text, str) and _MD_LINK_RE.search(text):
            data["text"] = _linkify_markdown_links_in_text(text)
            payload = json.dumps(data, ensure_ascii=False)
        return msg_type, payload

    setattr(build_outbound_payload, "_hermes_multitenancy_patched", True)
    setattr(build_outbound_payload, "_hermes_multitenancy_original", original)
    FeishuAdapter._build_outbound_payload = build_outbound_payload
    logger.info("[multitenancy] patched Feishu outbound payload to linkify markdown links in text mode")


def _feishu_response_message_id(response: Any) -> Optional[str]:
    data = getattr(response, "data", None)
    for source in (data, response):
        if source is None:
            continue
        value = getattr(source, "message_id", None)
        if value:
            return str(value)
        if isinstance(source, dict):
            value = source.get("message_id") or source.get("message", {}).get("message_id")
            if value:
                return str(value)
    return None


def _resolve_profiles_root() -> Optional[Path]:
    hermes_home = os.environ.get("HERMES_HOME")
    if not hermes_home:
        logger.info("[multitenancy] cron worker: HERMES_HOME unset; skipping")
        return None
    profile_home = Path(hermes_home).expanduser()
    if profile_home.parent.name != "profiles":
        logger.info(
            "[multitenancy] cron worker: HERMES_HOME=%s not in profiles/<name> layout; skipping",
            profile_home,
        )
        return None
    return profile_home.parent


def _active_cron_profiles(profiles_root: Path) -> Optional[set[str]]:
    """Return active routed profile names, or None when no routing DB exists."""
    db_path = profiles_root.parent / "multitenancy.db"
    if not db_path.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            rows = conn.execute(
                "SELECT DISTINCT profile_name FROM multitenancy_routing "
                "WHERE active = 1 AND profile_name IS NOT NULL AND profile_name != ''"
            ).fetchall()
    except Exception:
        logger.exception("[multitenancy] cron worker failed to read active routing profiles")
        return None
    return {str(row[0]) for row in rows if row and row[0]}


def _multiprofile_cron_worker(
    profiles_root: Path,
    adapters: Any,
    loop: asyncio.AbstractEventLoop,
    stop_event: threading.Event,
    interval: int = 60,
) -> None:
    try:
        import cron.jobs as cron_jobs
        import cron.scheduler as cron_scheduler
    except Exception:
        logger.exception("[multitenancy] cron modules not importable; worker aborted")
        return

    while not stop_event.is_set():
        try:
            active_profiles = _active_cron_profiles(profiles_root)
            for profile_dir in sorted(profiles_root.iterdir()):
                if not profile_dir.is_dir():
                    continue
                if active_profiles is not None and profile_dir.name not in active_profiles:
                    continue
                jobs_file = profile_dir / "cron" / "jobs.json"
                if not jobs_file.exists():
                    continue
                backfill_cron_owner_context_for_profile(profile_dir)
                _tick_one_profile(
                    cron_jobs,
                    cron_scheduler,
                    profile_dir,
                    jobs_file,
                    adapters,
                    loop,
                    _cron_module_patch_lock,
                )
        except Exception:
            logger.exception("[multitenancy] cron worker scan error")
        stop_event.wait(timeout=interval)
    logger.info("[multitenancy] cron worker stopped")


def _tick_one_profile(
    cron_jobs: Any,
    cron_scheduler: Any,
    profile_dir: Path,
    jobs_file: Path,
    adapters: Any,
    loop: asyncio.AbstractEventLoop,
    patch_lock: threading.Lock,
) -> None:
    with patch_lock:
        saved = (
            cron_jobs.HERMES_DIR,
            cron_jobs.CRON_DIR,
            cron_jobs.JOBS_FILE,
            cron_jobs.OUTPUT_DIR,
            getattr(cron_scheduler, "_hermes_home", None),
            getattr(cron_scheduler, "_LOCK_DIR", None),
            getattr(cron_scheduler, "_LOCK_FILE", None),
            os.environ.get("HERMES_HOME"),
        )
        try:
            profile_home = profile_dir.resolve()
            os.environ["HERMES_HOME"] = str(profile_home)
            cron_jobs.HERMES_DIR = profile_home
            cron_jobs.CRON_DIR = cron_jobs.HERMES_DIR / "cron"
            cron_jobs.JOBS_FILE = jobs_file
            cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"
            cron_scheduler._hermes_home = profile_home
            cron_scheduler._LOCK_DIR = cron_jobs.CRON_DIR
            cron_scheduler._LOCK_FILE = cron_jobs.CRON_DIR / ".tick.lock"
            cron_scheduler.tick(verbose=False, adapters=adapters, loop=loop)
        except Exception:
            logger.exception(
                "[multitenancy] cron tick failed for profile %s",
                profile_dir.name,
            )
        finally:
            (
                cron_jobs.HERMES_DIR,
                cron_jobs.CRON_DIR,
                cron_jobs.JOBS_FILE,
                cron_jobs.OUTPUT_DIR,
                cron_scheduler._hermes_home,
                cron_scheduler._LOCK_DIR,
                cron_scheduler._LOCK_FILE,
                previous_home,
            ) = saved
            if previous_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = previous_home
