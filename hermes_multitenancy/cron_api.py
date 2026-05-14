"""Profile-aware cron management API for the WebUI broker sidecar.

This module intentionally reuses hermes-agent's native ``cron.jobs`` storage
and validation logic while keeping HTTP/API ownership inside the multitenancy
plugin.  It avoids adding another cron schema and keeps WebUI from depending on
the profile apiserver for job creation and management.
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from . import cron_worker

_JOB_ID_RE = re.compile(r"[a-f0-9]{12}")
_UPDATE_ALLOWED_FIELDS = {"name", "schedule", "prompt", "deliver", "skills", "skill", "repeat", "enabled"}
_MAX_NAME_LENGTH = 200
_MAX_PROMPT_LENGTH = 5000
_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}")


class CronApiError(Exception):
    """HTTP-shaped error raised by profile-aware cron helpers."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def validate_profile_name(profile_name: str) -> str:
    profile = str(profile_name or "").strip()
    if not profile:
        raise CronApiError("profile_name is required", 400)
    if not _PROFILE_RE.fullmatch(profile):
        raise CronApiError("invalid profile_name", 400)
    return profile


def validate_job_id(job_id: str) -> str:
    value = str(job_id or "").strip()
    if not _JOB_ID_RE.fullmatch(value):
        raise CronApiError("Invalid job ID format", 400)
    return value


def profile_home_for(profile_name: str) -> Path:
    profile = validate_profile_name(profile_name)
    return Path.home() / ".hermes" / "profiles" / profile


@contextmanager
def cron_profile_scope(profile_home: Path) -> Iterator[Any]:
    """Temporarily bind hermes-agent cron modules to ``profile_home``.

    ``cron.jobs`` keeps storage paths as module globals.  The multitenancy cron
    worker also mutates those globals while ticking profiles, so both paths use
    the same lock.
    """
    import cron.jobs as cron_jobs
    try:
        import cron.scheduler as cron_scheduler
    except Exception:
        cron_scheduler = None

    with cron_worker._cron_module_patch_lock:
        saved = (
            cron_jobs.HERMES_DIR,
            cron_jobs.CRON_DIR,
            cron_jobs.JOBS_FILE,
            cron_jobs.OUTPUT_DIR,
            getattr(cron_scheduler, "_hermes_home", None) if cron_scheduler else None,
            getattr(cron_scheduler, "_LOCK_DIR", None) if cron_scheduler else None,
            getattr(cron_scheduler, "_LOCK_FILE", None) if cron_scheduler else None,
            os.environ.get("HERMES_HOME"),
        )
        try:
            home = profile_home.expanduser().resolve()
            os.environ["HERMES_HOME"] = str(home)
            cron_jobs.HERMES_DIR = home
            cron_jobs.CRON_DIR = home / "cron"
            cron_jobs.JOBS_FILE = cron_jobs.CRON_DIR / "jobs.json"
            cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"
            if cron_scheduler is not None:
                cron_scheduler._hermes_home = home
                cron_scheduler._LOCK_DIR = cron_jobs.CRON_DIR
                cron_scheduler._LOCK_FILE = cron_jobs.CRON_DIR / ".tick.lock"
            yield cron_jobs
        finally:
            (
                cron_jobs.HERMES_DIR,
                cron_jobs.CRON_DIR,
                cron_jobs.JOBS_FILE,
                cron_jobs.OUTPUT_DIR,
                saved_home,
                saved_lock_dir,
                saved_lock_file,
                saved_env_home,
            ) = saved
            if cron_scheduler is not None:
                cron_scheduler._hermes_home = saved_home
                cron_scheduler._LOCK_DIR = saved_lock_dir
                cron_scheduler._LOCK_FILE = saved_lock_file
            if saved_env_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = saved_env_home


def _validate_common_fields(body: dict[str, Any], *, require_create_fields: bool) -> None:
    name = body.get("name")
    schedule = body.get("schedule")
    prompt = body.get("prompt", "")
    repeat = body.get("repeat")

    if require_create_fields and not str(name or "").strip():
        raise CronApiError("Name is required", 400)
    if name is not None and len(str(name)) > _MAX_NAME_LENGTH:
        raise CronApiError(f"Name must be ≤ {_MAX_NAME_LENGTH} characters", 400)
    if require_create_fields and not str(schedule or "").strip():
        raise CronApiError("Schedule is required", 400)
    if prompt is not None and len(str(prompt)) > _MAX_PROMPT_LENGTH:
        raise CronApiError(f"Prompt must be ≤ {_MAX_PROMPT_LENGTH} characters", 400)
    if repeat is not None and (not isinstance(repeat, int) or repeat < 1):
        raise CronApiError("Repeat must be a positive integer", 400)


def list_jobs(profile_name: str, *, include_disabled: bool = False) -> list[dict[str, Any]]:
    with cron_profile_scope(profile_home_for(profile_name)) as cron_jobs:
        return cron_jobs.list_jobs(include_disabled=include_disabled)


def get_job(profile_name: str, job_id: str) -> dict[str, Any]:
    job_id = validate_job_id(job_id)
    with cron_profile_scope(profile_home_for(profile_name)) as cron_jobs:
        job = cron_jobs.get_job(job_id)
    if not job:
        raise CronApiError("Job not found", 404)
    return job


def create_job(profile_name: str, user_key: str, body: dict[str, Any]) -> dict[str, Any]:
    profile_name = validate_profile_name(profile_name)
    user_key = str(user_key or "").strip()
    if not user_key:
        raise CronApiError("user_key is required", 400)
    _validate_common_fields(body, require_create_fields=True)
    deliver = str(body.get("deliver") or "").strip() or "feishu"

    kwargs: dict[str, Any] = {
        "prompt": body.get("prompt", ""),
        "schedule": str(body.get("schedule") or "").strip(),
        "name": str(body.get("name") or "").strip(),
        "deliver": deliver,
        "owner_open_id": user_key,
        "owner_profile": profile_name,
    }
    if body.get("skills"):
        kwargs["skills"] = body.get("skills")
    if body.get("repeat") is not None:
        kwargs["repeat"] = body.get("repeat")

    with cron_profile_scope(profile_home_for(profile_name)) as cron_jobs:
        return cron_jobs.create_job(**kwargs)


def update_job(profile_name: str, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
    job_id = validate_job_id(job_id)
    sanitized = {k: v for k, v in body.items() if k in _UPDATE_ALLOWED_FIELDS}
    if not sanitized:
        raise CronApiError("No valid fields to update", 400)
    _validate_common_fields(sanitized, require_create_fields=False)

    with cron_profile_scope(profile_home_for(profile_name)) as cron_jobs:
        job = cron_jobs.update_job(job_id, sanitized)
    if not job:
        raise CronApiError("Job not found", 404)
    return job


def delete_job(profile_name: str, job_id: str) -> None:
    job_id = validate_job_id(job_id)
    with cron_profile_scope(profile_home_for(profile_name)) as cron_jobs:
        success = cron_jobs.remove_job(job_id)
    if not success:
        raise CronApiError("Job not found", 404)


def pause_job(profile_name: str, job_id: str) -> dict[str, Any]:
    job_id = validate_job_id(job_id)
    with cron_profile_scope(profile_home_for(profile_name)) as cron_jobs:
        job = cron_jobs.pause_job(job_id)
    if not job:
        raise CronApiError("Job not found", 404)
    return job


def resume_job(profile_name: str, job_id: str) -> dict[str, Any]:
    job_id = validate_job_id(job_id)
    with cron_profile_scope(profile_home_for(profile_name)) as cron_jobs:
        job = cron_jobs.resume_job(job_id)
    if not job:
        raise CronApiError("Job not found", 404)
    return job
