"""RunBroker HTTP bridge + cron RunRequest construction.

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
from ..run_models import RunRequest

# Keep the historical logger name so log records are attributed to
# ``hermes_multitenancy.cron_worker`` exactly as before the split.
logger = logging.getLogger("hermes_multitenancy.cron_worker")



_CRON_VISIBLE_RESPONSE_INSTRUCTION = (
    "\n\n[Multitenancy cron delivery override: Do not respond with [SILENT]. "
    "If there is nothing new to report, explicitly say that this run completed "
    "and no matching item needed a reminder.]"
)


def _cron_user_key(job: dict, profile_name: str) -> str:
    owner_open_id = str(job.get("owner_open_id") or "").strip()
    if not owner_open_id.startswith("ou_"):
        raise ValueError("cron owner_open_id is required")
    return owner_open_id


def _build_cron_run_request(job: dict, *, profile_home: Path, prompt: str) -> RunRequest:
    job = _cw.with_cron_owner_context(job, profile_home=profile_home)
    profile_name = str(job.get("owner_profile") or "").strip() or profile_home.name
    if profile_name == "multitenancy_router":
        raise ValueError("cron owner_profile must not be multitenancy_router")
    user_key = _cw._cron_user_key(job, profile_name)
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
    planned_job = _cw.with_cron_owner_context(job, profile_home=profile_home)
    try:
        request = _cw._build_cron_run_request(
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
    deliver_target = _cw._cron_deliver_target(planned_job, user_key) if request is not None else None
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
    if text and not _cw._cron_response_is_silent(text):
        return text
    job_name = str(job.get("name") or job.get("id") or "定时任务")
    return f"定时任务「{job_name}」已执行完成：本次没有发现需要提醒的事项。"


async def _dispatch_cron_request(request: RunRequest, profile_home: Path) -> str:
    from .. import router

    event = _cw._build_cron_event(request)
    return await router._get_pool().dispatch(request.profile_name, profile_home, event)


def _run_job_through_broker(job: dict, scheduler: Any) -> tuple[bool, str, str, Optional[str]]:
    profile_home = _cw._current_profile_home()
    job = _cw.with_cron_owner_context(job, profile_home=profile_home)
    job_id = str(job.get("id") or "")
    job_name = str(job.get("name") or job_id or "scheduled task")
    try:
        build_prompt = getattr(scheduler, "_build_job_prompt")
        prompt = _cw._force_visible_cron_prompt(build_prompt(job, prerun_script=None))
        request = _cw._build_cron_run_request(job, profile_home=profile_home, prompt=prompt)
        broker = _cw.RunBroker(
            dispatch_agent=lambda run_request: _cw._dispatch_cron_request(run_request, profile_home),
            sandbox_available=lambda: os.environ.get("HERMES_USE_SANDBOX", "").strip().lower()
            in {"1", "true", "yes", "on"},
        )
        result = asyncio.run(broker.run(request))
        final_response = _cw._visible_cron_response(job, result.content)
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
    shared_home = _cw._shared_home_for_profile(profile_home)
    return _cw._dotenv_lookup(
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
    if not _cw._is_feishu_open_id(owner):
        raise RuntimeError("cron owner_open_id is required for RunBroker trigger")
    job = str(job_id or "").strip()
    if not job:
        raise RuntimeError("cron job_id is required for RunBroker trigger")

    url = f"{_cw._run_broker_base_url()}/api/run-broker/jobs/{job}/run"
    headers = {
        "Content-Type": "application/json",
        "X-Hermes-Profile": profile_name,
        "X-Hermes-User-Key": owner,
    }
    key = _cw._run_broker_key_for_profile(profile_home)
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
