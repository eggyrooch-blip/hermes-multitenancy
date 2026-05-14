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
import functools
import logging
import os
import sqlite3
import threading
import uuid
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Optional

from .run_broker import RunBroker
from .run_models import RunRequest

logger = logging.getLogger(__name__)

_worker_lock = threading.Lock()
_worker_started = False
_worker_thread: Optional[threading.Thread] = None
_worker_stop: Optional[threading.Event] = None
_runtime_patches_installed = False
_gateway_watcher_installed = False
_watcher_attr = "_hermes_multitenancy_cron_watch_scheduled"


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
    _runtime_patches_installed = True


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


def _cron_user_key(job: dict, profile_name: str) -> str:
    owner_open_id = str(job.get("owner_open_id") or "").strip()
    if owner_open_id:
        return owner_open_id
    return str(job.get("owner_profile") or "").strip() or profile_name


def _build_cron_run_request(job: dict, *, profile_home: Path, prompt: str) -> RunRequest:
    profile_name = str(job.get("owner_profile") or "").strip() or profile_home.name
    user_key = _cron_user_key(job, profile_name)
    job_id = str(job.get("id") or "").strip()
    metadata = {
        "job_id": job_id,
        "job_name": str(job.get("name") or job_id or "scheduled task"),
        "deliver": job.get("deliver"),
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
        session_id=f"cron:{job_id}" if job_id else None,
        message_id=job_id or None,
        delivery_mode=str(job.get("deliver") or "feishu"),
        credential_subject=user_key,
        requires_host_tools=True,
        metadata={k: v for k, v in metadata.items() if v not in (None, "", [])},
    )


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


async def _dispatch_cron_request(request: RunRequest, profile_home: Path) -> str:
    from . import router

    event = _build_cron_event(request)
    return await router._get_pool().dispatch(request.profile_name, profile_home, event)


def _run_job_through_broker(job: dict, scheduler: Any) -> tuple[bool, str, str, Optional[str]]:
    profile_home = _current_profile_home()
    job_id = str(job.get("id") or "")
    job_name = str(job.get("name") or job_id or "scheduled task")
    try:
        build_prompt = getattr(scheduler, "_build_job_prompt")
        prompt = build_prompt(job, prerun_script=None)
        request = _build_cron_run_request(job, profile_home=profile_home, prompt=prompt)
        broker = RunBroker(
            dispatch_agent=lambda run_request: _dispatch_cron_request(run_request, profile_home),
            sandbox_available=lambda: os.environ.get("HERMES_USE_SANDBOX", "").strip().lower()
            in {"1", "true", "yes", "on"},
        )
        result = asyncio.run(broker.run(request))
        final_response = result.content
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
        error = original(job, content, adapters=adapters, loop=loop)
        if error is None:
            _mirror_cron_delivery_to_owner(job, content)
        return error

    setattr(deliver_result, "_hermes_multitenancy_patched", True)
    scheduler._deliver_result = deliver_result
    logger.info("[multitenancy] patched cron delivery mirror to owner session")


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

        key = (owner_profile, owner_open_id)
        existing = router._session_history.get(key, [])
        router._session_history[key] = router._trim_history(
            existing + [{"role": "assistant", "content": mirrored_content}]
        )
        store = router._get_session_store()
        if store is not None:
            store.append(owner_profile, owner_open_id, "assistant", mirrored_content)
        logger.info(
            "[multitenancy] mirrored cron delivery to profile session profile=%s job=%s",
            owner_profile,
            job_id,
        )
    except Exception:
        logger.exception("[multitenancy] failed to mirror cron delivery to owner session")


def _patch_feishu_open_id_send() -> None:
    try:
        from gateway.platforms.feishu import FeishuAdapter
    except Exception:
        logger.exception("[multitenancy] failed to patch Feishu open_id delivery")
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
        return await asyncio.to_thread(self._client.im.v1.message.create, request)

    setattr(send_raw_message, "_hermes_multitenancy_patched", True)
    FeishuAdapter._send_raw_message = send_raw_message
    logger.info("[multitenancy] patched Feishu delivery for user open_id targets")


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

    patch_lock = threading.Lock()
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
                _tick_one_profile(
                    cron_jobs,
                    cron_scheduler,
                    profile_dir,
                    jobs_file,
                    adapters,
                    loop,
                    patch_lock,
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
