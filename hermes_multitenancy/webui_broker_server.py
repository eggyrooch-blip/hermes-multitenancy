"""Router-owned HTTP/SSE seam for WebUI run submissions.

The endpoint lives in the multitenancy plugin so WebUI can submit normalized
``RunRequest(channel="webui")`` objects without calling a profile apiserver
execution endpoint directly.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from . import cron_api
from .run_broker import RunBroker, RunRejected
from .run_models import RunEvent, RunRequest

logger = logging.getLogger(__name__)

DispatchAgent = Callable[[RunRequest], Awaitable[str] | str]
MarkSeen = Callable[[RunRequest], bool]
SandboxAvailable = Callable[[], bool]

_runner: Any = None
_site: Any = None
_server_task: Optional[asyncio.Task] = None


def _truthy_env(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _run_broker_host() -> str:
    return os.environ.get("HERMES_MULTITENANCY_RUN_BROKER_HOST", "127.0.0.1").strip() or "127.0.0.1"


def _run_broker_port() -> int:
    raw = os.environ.get("HERMES_MULTITENANCY_RUN_BROKER_PORT", "8766").strip()
    try:
        return int(raw)
    except ValueError:
        return 8766


def _run_broker_key() -> str:
    return os.environ.get("HERMES_MULTITENANCY_RUN_BROKER_KEY", "").strip()


def _authorized(request: Any) -> bool:
    expected = _run_broker_key()
    if not expected:
        return True
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    return hmac.compare_digest(header[len(prefix):].strip(), expected)


def _default_sandbox_available() -> bool:
    value = os.environ.get("HERMES_USE_SANDBOX", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _tenant_from_request(request: Any, payload: Optional[dict[str, Any]] = None) -> tuple[str, str]:
    payload = payload or {}
    profile_name = (
        request.headers.get("X-Hermes-Profile")
        or payload.get("profile_name")
        or payload.get("profile")
        or ""
    )
    user_key = (
        request.headers.get("X-Hermes-User-Key")
        or payload.get("user_key")
        or payload.get("user")
        or ""
    )
    return cron_api.validate_profile_name(str(profile_name)), str(user_key or "").strip()


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        return await value
    return value


def _event_to_sse(event: RunEvent) -> str:
    data = {
        "kind": event.kind,
        "text": event.text,
        "name": event.name,
        "payload": event.payload,
    }
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _build_webui_event(request: RunRequest) -> Any:
    """Create the smallest event shape expected by ProfileRuntime/agent_real."""
    return SimpleNamespace(
        text=request.content,
        message_id=request.message_id,
        channel="webui",
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


async def _default_dispatch_agent(request: RunRequest) -> str:
    from . import router as router_mod

    profile_home = router_mod._profile_name_to_home(request.profile_name)
    event = _build_webui_event(request)
    return await router_mod._get_pool().dispatch(request.profile_name, profile_home, event)


def _default_mark_seen(request: RunRequest) -> bool:
    from . import router as router_mod

    return router_mod._mark_run_request_seen(request)


def create_run_broker_app(
    *,
    dispatch_agent: Optional[DispatchAgent] = None,
    mark_seen: Optional[MarkSeen] = None,
    sandbox_available: Optional[SandboxAvailable] = None,
):
    try:
        from aiohttp import web
    except Exception as exc:  # pragma: no cover - production dependency guard
        raise RuntimeError("aiohttp is required for the WebUI run broker endpoint") from exc

    async def handle_run(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
            run_request = RunRequest(
                channel=payload.get("channel"),
                profile_name=payload.get("profile_name") or payload.get("profile"),
                user_key=payload.get("user_key") or payload.get("user"),
                content=payload.get("content"),
                chat_id=payload.get("chat_id"),
                session_id=payload.get("session_id"),
                message_id=payload.get("message_id"),
                idempotency_key=payload.get("idempotency_key"),
                delivery_mode=payload.get("delivery_mode") or "socket",
                credential_subject=payload.get("credential_subject"),
                requires_host_tools=bool(payload.get("requires_host_tools")),
                metadata=payload.get("metadata") or {},
            )
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        events: list[RunEvent] = []

        async def emit_event(event: RunEvent) -> None:
            events.append(event)

        broker = RunBroker(
            dispatch_agent=dispatch_agent or _default_dispatch_agent,
            emit_event=emit_event,
            mark_seen=mark_seen if mark_seen is not None else _default_mark_seen,
            sandbox_available=sandbox_available or _default_sandbox_available,
        )

        try:
            await broker.run(run_request)
        except RunRejected as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI run broker request failed")
            events.append(RunEvent(kind="error", text=str(exc), payload={"error": str(exc)}))

        body = "".join(_event_to_sse(event) for event in events)
        return web.Response(text=body, content_type="text/event-stream")

    async def handle_list_jobs(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _tenant_from_request(request)
            include_disabled = request.query.get("include_disabled", "").lower() in {"true", "1"}
            jobs = cron_api.list_jobs(profile_name, include_disabled=include_disabled)
            return web.json_response({"jobs": jobs})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron list failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_create_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
            profile_name, user_key = _tenant_from_request(request, payload)
            job = cron_api.create_job(profile_name, user_key, payload)
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron create failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_get_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _tenant_from_request(request)
            job = cron_api.get_job(profile_name, request.match_info["job_id"])
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron get failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_update_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
            profile_name, _user_key = _tenant_from_request(request, payload)
            job = cron_api.update_job(profile_name, request.match_info["job_id"], payload)
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron update failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_delete_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _tenant_from_request(request)
            cron_api.delete_job(profile_name, request.match_info["job_id"])
            return web.json_response({"ok": True})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron delete failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_pause_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _tenant_from_request(request)
            job = cron_api.pause_job(profile_name, request.match_info["job_id"])
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron pause failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_resume_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _tenant_from_request(request)
            job = cron_api.resume_job(profile_name, request.match_info["job_id"])
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron resume failed")
            return web.json_response({"error": str(exc)}, status=500)

    app = web.Application()
    app.router.add_post("/api/run-broker/runs", handle_run)
    app.router.add_get("/api/run-broker/jobs", handle_list_jobs)
    app.router.add_post("/api/run-broker/jobs", handle_create_job)
    app.router.add_get("/api/run-broker/jobs/{job_id}", handle_get_job)
    app.router.add_patch("/api/run-broker/jobs/{job_id}", handle_update_job)
    app.router.add_delete("/api/run-broker/jobs/{job_id}", handle_delete_job)
    app.router.add_post("/api/run-broker/jobs/{job_id}/pause", handle_pause_job)
    app.router.add_post("/api/run-broker/jobs/{job_id}/resume", handle_resume_job)
    return app


async def start_run_broker_server() -> None:
    """Start the localhost broker sidecar in the current event loop."""
    global _runner, _site
    if _runner is not None:
        return

    from aiohttp import web

    app = create_run_broker_app()
    _runner = web.AppRunner(app)
    await _runner.setup()
    _site = web.TCPSite(_runner, _run_broker_host(), _run_broker_port())
    await _site.start()
    logger.info(
        "[multitenancy] WebUI run broker listening on http://%s:%s/api/run-broker/runs",
        _run_broker_host(),
        _run_broker_port(),
    )


def ensure_run_broker_server_started() -> None:
    """Schedule the optional WebUI run broker sidecar when enabled."""
    global _server_task
    if not _truthy_env("HERMES_MULTITENANCY_RUN_BROKER_SERVER"):
        return
    if _server_task is not None and not _server_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("[multitenancy] no running loop; WebUI run broker sidecar not started")
        return
    _server_task = loop.create_task(start_run_broker_server())


async def stop_run_broker_server() -> None:
    """Test/maintenance helper to stop the sidecar."""
    global _runner, _site, _server_task
    if _server_task is not None and not _server_task.done():
        _server_task.cancel()
        try:
            await _server_task
        except asyncio.CancelledError:
            pass
    _server_task = None
    _site = None
    if _runner is not None:
        await _runner.cleanup()
    _runner = None
