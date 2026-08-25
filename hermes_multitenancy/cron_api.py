"""Profile-aware cron management API for the WebUI broker sidecar.

This module intentionally reuses hermes-agent's native ``cron.jobs`` storage
and validation logic while keeping HTTP/API ownership inside the multitenancy
plugin.  It avoids adding another cron schema and keeps WebUI from depending on
the profile apiserver for job creation and management.
"""
from __future__ import annotations

import os
import re
import inspect
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from . import cron_worker

_JOB_ID_RE = re.compile(r"[a-f0-9]{12}")
# Executor identity (agent_id/expert_id) is deliberately NOT updatable: allowing
# it would let a caller create a job on an agent they can access and then
# repoint it at one they cannot. Changing the executor = delete + recreate.
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
# `model`/`provider` are updatable so an EXISTING expensive job can be moved to a
# cheaper model — the whole point of per-job model selection. Executor identity
# (agent_id/expert_id) deliberately stays out: see the note above.
_UPDATE_ALLOWED_FIELDS = {
    "name", "schedule", "prompt", "deliver", "skills", "skill", "repeat", "enabled",
    "model", "provider",
}
_MAX_NAME_LENGTH = 200
_MAX_PROMPT_LENGTH = 5000
_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}")
# Same charset run_broker.py accepts for metadata.expert_id.
_EXPERT_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


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


def validate_expert_id(expert_id: str) -> str:
    value = str(expert_id or "").strip()
    if not _EXPERT_ID_RE.fullmatch(value):
        raise CronApiError("invalid expert_id", 400)
    return value


def validate_idempotency_key(value: Any) -> str:
    key = str(value or "").strip()
    if key and not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise CronApiError("invalid idempotency_key", 400)
    return key


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


def plan_job(profile_name: str, job_id: str, *, shadow: bool = True, due: Optional[bool] = None) -> dict[str, Any]:
    profile_name = validate_profile_name(profile_name)
    job = get_job(profile_name, job_id)
    return cron_worker.plan_cron_bridge_run(
        job,
        profile_home=profile_home_for(profile_name),
        due=due,
        shadow=shadow,
    )


def _validated_expert_id(profile_name: str, user_key: str, body: dict[str, Any]) -> str:
    """Validate a requested executor expert at CREATION time, fail-closed.

    The stored expert_id is the user's request, not a grant: the run path
    re-resolves it at wake with the creator's identity and rejects the run
    (EXPERT_UNAVAILABLE → run_terminal rejected) if the audience no longer
    admits them. This gate just refuses to persist a request that is already
    invalid today.
    """
    expert_id = str(body.get("expert_id") or "").strip()
    if not expert_id:
        return ""
    if not _EXPERT_ID_RE.fullmatch(expert_id):
        raise CronApiError("invalid expert_id", 400)
    from .expert_overlay import resolve_caller_departments, resolve_expert

    profile_home = profile_home_for(profile_name)
    overlay = resolve_expert(
        profile_home,
        expert_id,
        department_ids=resolve_caller_departments(profile_home, open_id=user_key),
    )
    if overlay is None:
        raise CronApiError("expert not available for this agent", 403)
    return expert_id


def create_job(
    profile_name: str,
    user_key: str,
    body: dict[str, Any],
    *,
    agent_id: str = "",
) -> dict[str, Any]:
    profile_name = validate_profile_name(profile_name)
    user_key = str(user_key or "").strip()
    if not user_key:
        raise CronApiError("user_key is required", 400)
    _validate_common_fields(body, require_create_fields=True)
    # agent_id is server-derived by the broker handler from the access-checked
    # profile's routing row — never taken from the client body.
    agent_id = str(agent_id or "").strip()
    expert_id = _validated_expert_id(profile_name, user_key, body)
    deliver = str(body.get("deliver") or "").strip() or "feishu"
    if expert_id and deliver.lower() != "feishu":
        raise CronApiError("scheduled expert delivery must use feishu", 400)
    idempotency_key = validate_idempotency_key(body.get("idempotency_key"))

    kwargs: dict[str, Any] = {
        "prompt": body.get("prompt", ""),
        "schedule": str(body.get("schedule") or "").strip(),
        "name": str(body.get("name") or "").strip(),
        "deliver": deliver,
    }
    if body.get("skills"):
        kwargs["skills"] = body.get("skills")
    if body.get("repeat") is not None:
        kwargs["repeat"] = body.get("repeat")
    for key in ("model", "provider", "base_url", "workdir", "profile"):
        if body.get(key) is not None:
            kwargs[key] = body.get(key)

    with cron_profile_scope(profile_home_for(profile_name)) as cron_jobs:
        if idempotency_key:
            existing = [
                job
                for job in cron_jobs.list_jobs(include_disabled=True)
                if str(job.get("owner_open_id") or "") == user_key
                and str(job.get("owner_profile") or "") == profile_name
                and str(job.get("create_idempotency_key") or "") == idempotency_key
            ]
            if len(existing) > 1:
                raise CronApiError("duplicate idempotency marker", 409)
            if existing:
                return existing[0]
        try:
            parameters = inspect.signature(cron_jobs.create_job).parameters
            if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
                create_kwargs = kwargs
            else:
                supported = set(parameters)
                create_kwargs = {key: value for key, value in kwargs.items() if key in supported}
        except (TypeError, ValueError):
            create_kwargs = kwargs
        job = cron_jobs.create_job(**create_kwargs)
        owner_updates = {
            "owner_open_id": user_key,
            "owner_profile": profile_name,
        }
        if agent_id:
            owner_updates["agent_id"] = agent_id
        if expert_id:
            owner_updates["expert_id"] = expert_id
        if idempotency_key:
            owner_updates["create_idempotency_key"] = idempotency_key
        updated = cron_jobs.update_job(job["id"], owner_updates)
        if updated is None:
            cron_jobs.remove_job(job["id"])
            raise CronApiError("failed to persist trusted job binding", 500)
        return updated


def update_job(profile_name: str, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
    job_id = validate_job_id(job_id)
    sanitized = {k: v for k, v in body.items() if k in _UPDATE_ALLOWED_FIELDS}
    if not sanitized:
        raise CronApiError("No valid fields to update", 400)
    # model/provider are ONE routing decision, never two independent fields.
    # _model_spec_for_event prefixes the model with whatever provider is stored,
    # so a model-only update (the WebUI path — the BFF strips provider) would
    # otherwise keep a stale provider and route "old-provider/new-model".
    if "model" in sanitized:
        if not str(sanitized.get("model") or "").strip():
            sanitized["model"] = ""
            sanitized["provider"] = ""
        elif "provider" not in sanitized:
            sanitized["provider"] = ""
    _validate_common_fields(sanitized, require_create_fields=False)

    with cron_profile_scope(profile_home_for(profile_name)) as cron_jobs:
        existing = cron_jobs.get_job(job_id)
        if not existing:
            raise CronApiError("Job not found", 404)
        if (
            str(existing.get("expert_id") or "").strip()
            and "deliver" in sanitized
            and str(sanitized["deliver"] or "").strip().lower() != "feishu"
        ):
            raise CronApiError("scheduled expert delivery must use feishu", 400)
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


def trigger_job(profile_name: str, job_id: str) -> dict[str, Any]:
    """Queue a profile job for the next multitenancy cron worker tick."""
    job_id = validate_job_id(job_id)
    with cron_profile_scope(profile_home_for(profile_name)) as cron_jobs:
        trigger = getattr(cron_jobs, "trigger_job", None)
        if trigger is not None:
            job = trigger(job_id)
        else:
            job = cron_jobs.update_job(
                job_id,
                {
                    "enabled": True,
                    "state": "scheduled",
                    "paused_at": None,
                    "paused_reason": None,
                },
            )
    if not job:
        raise CronApiError("Job not found", 404)
    return job
