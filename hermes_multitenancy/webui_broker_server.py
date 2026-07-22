"""Router-owned HTTP/SSE seam for WebUI run submissions.

The endpoint lives in the multitenancy plugin so WebUI can submit normalized
``RunRequest(channel="webui")`` objects without calling a profile apiserver
execution endpoint directly.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from . import cron_api
from .credential_broker import (
    LeaseError,
    assert_lease_binding,
    lease_signing_secret,
    verify_lease,
)
from .run_broker import RunBroker, RunRejected
from .run_models import RunEvent, RunRequest, RunResult
from .security_audit import append_security_event

logger = logging.getLogger(__name__)


class _IngestSecretMaterializationError(RuntimeError):
    """Secret staging failed before durable async-ingest admission."""


class _IngestAsyncTaskStartError(RuntimeError):
    """The async job owner could not be established before admission."""


class _IngestAsyncSecretClaim:
    """Reference-counted in-process claim for one idempotency/fingerprint pair."""

    __slots__ = ("fingerprint", "request_refs", "job_owned")

    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint
        self.request_refs = 0
        self.job_owned = False


class _IngestSyncSecretClaim:
    """Transient fingerprint claim until synchronous admission is durable."""

    __slots__ = ("fingerprint", "request_refs", "committed")

    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint
        self.request_refs = 0
        self.committed = False

from types import ModuleType as _ModuleType
import sys as _sys

from .webui_broker import constants as _constants, periphery as _periphery

# Live module state (locks + reassigned globals + mutated containers) has a single
# canonical owner: the periphery submodule. NEVER snapshot-copy these into the shim
# — a copy would freeze a stale value / split lock identity. Instead proxy reads AND
# writes to the owner via a ModuleType subclass (below).
_LIVE_STATE_OWNERS = {
    '_auth_signal_store': _periphery,
    '_auth_signal_store_lock': _periphery,
    '_credential_broker_tokens': _periphery,
    '_credential_broker_tokens_lock': _periphery,
    '_pending_clarifies': _periphery,
    '_runner': _periphery,
    '_server_task': _periphery,
    '_session_search_broker_tokens': _periphery,
    '_session_search_broker_tokens_lock': _periphery,
    '_site': _periphery,
}

# Bulk re-export every other top-level symbol (constants + helpers + classes +
# server funcs + each submodule's imported names) so ``create_run_broker_app`` and
# external importers see them exactly as before. Includes underscore + imported
# names; skips live-state (proxied) so identity/freshness is preserved.
for _sub in (_constants, _periphery):
    for _n, _v in vars(_sub).items():
        if _n.startswith("__"):
            continue
        if _n in _LIVE_STATE_OWNERS:
            continue
        globals()[_n] = _v
del _sub, _n, _v


class _ShimModule(_ModuleType):
    def __getattr__(self, name):  # only fires on normal-lookup miss
        owner = _LIVE_STATE_OWNERS.get(name)
        if owner is not None:
            return getattr(owner, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        owner = _LIVE_STATE_OWNERS.get(name)
        if owner is not None:
            setattr(owner, name, value)  # route external writes to the owner
        else:
            super().__setattr__(name, value)


_sys.modules[__name__].__class__ = _ShimModule


def create_run_broker_app(
    *,
    dispatch_agent: Optional[DispatchAgent] = None,
    mark_seen: Optional[MarkSeen] = None,
    is_seen: Optional[IsSeen] = None,
    sandbox_available: Optional[SandboxAvailable] = None,
):
    try:
        from aiohttp import web
    except Exception as exc:  # pragma: no cover - production dependency guard
        raise RuntimeError("aiohttp is required for the WebUI run broker endpoint") from exc
    from .billing_identity import prepare_billing_request

    if mark_seen is not None and is_seen is None:
        # ponytail: this mirror is process-local; custom multi-process hooks need
        # to supply their own shared is_seen callback.
        locally_seen: dict[tuple[str, str, str, str], float] = {}

        def _local_seen_key(request: RunRequest):
            if not (request.idempotency_key or request.message_id):
                return None
            return (
                request.channel,
                request.profile_name,
                request.user_key,
                request.effective_idempotency_key,
            )

        def effective_mark_seen(request: RunRequest) -> bool:
            accepted = bool(mark_seen(request))
            key = _local_seen_key(request)
            if key is not None:
                locally_seen[key] = time.time()
                while len(locally_seen) > _INGEST_RESULT_CAP:
                    locally_seen.pop(next(iter(locally_seen)))
            return accepted

        def effective_is_seen(request: RunRequest) -> bool:
            key = _local_seen_key(request)
            if key is None:
                return False
            seen_at = locally_seen.get(key)
            if seen_at is None:
                return False
            if time.time() - seen_at > _INGEST_RESULT_TTL:
                locally_seen.pop(key, None)
                return False
            return True
    else:
        effective_mark_seen = mark_seen if mark_seen is not None else _default_mark_seen
        effective_is_seen = is_seen if is_seen is not None else _default_is_seen

    async def _stream_run_request(request, run_request, *, stash_payload):
        """Admit + SSE-stream a run_request. Shared by handle_run and replay.

        Mints a fresh ``signal_run_id`` and parks ``stash_payload`` so any
        ``auth_required`` frame this run emits can itself be replayed. The
        parked entry is retained ONLY if this run actually emits an
        ``auth_required`` frame; runs that finish without one consume their own
        speculative entry in the finally-block, so normal traffic never churns
        the bounded store and can't evict a genuinely-pending re-auth request.
        """
        signal_run_id = secrets.token_urlsafe(24)
        _auth_signal_stash(
            signal_run_id,
            payload=stash_payload,
            profile_name=run_request.profile_name,
            subject=run_request.user_key,
        )
        auth_required_seen = False

        try:
            policy_broker = RunBroker(
                dispatch_agent=lambda _req: "",
                sandbox_available=sandbox_available or _default_sandbox_available,
            )
            policy_broker.check_policy(run_request)
        except RunRejected as exc:
            _auth_signal_consume(signal_run_id)
            return web.json_response({"error": str(exc)}, status=403)

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
            },
        )
        await response.prepare(request)

        async def write_sse_event(event: RunEvent) -> None:
            await response.write(_event_to_sse(event).encode("utf-8"))

        emitter = _DisconnectTolerantEmitter(write_sse_event)

        async def emit_event(event: RunEvent) -> None:
            nonlocal auth_required_seen
            if event.kind == "auth_required":
                auth_required_seen = True
                _auth_signal_touch(signal_run_id)  # restart TTL/eviction clock at emission
            await emitter.emit(event)

        try:
            admission_broker = RunBroker(
                dispatch_agent=lambda _req: "",
                mark_seen=effective_mark_seen,
                is_seen=effective_is_seen,
                sandbox_available=sandbox_available or _default_sandbox_available,
                prepare_request=prepare_billing_request,
            )
            broker_dispatch = dispatch_agent or (
                lambda req: _default_dispatch_agent(
                    req, emit_event=emit_event, auth_signal_run_id=signal_run_id
                )
            )
            result = await admission_broker.prepare_and_execute(
                run_request,
                execute=lambda admitted: admission_broker._run_admitted(
                    admitted,
                    dispatch_agent=broker_dispatch,
                    emit_event=emit_event,
                ),
            )
            if result.duplicate:
                await emit_event(RunEvent(kind="done"))
        except Exception as exc:
            logger.exception("[multitenancy] WebUI run broker request failed")
            await emit_event(RunEvent(kind="error", text=str(exc), payload={"error": str(exc)}))
        finally:
            # Retain the parked request ONLY when this run signalled re-auth;
            # otherwise drop it so the bounded store holds only pending-reauth
            # entries and normal traffic can't evict a genuine one.
            if not auth_required_seen:
                _auth_signal_consume(signal_run_id)
            if not emitter.disconnected:
                try:
                    await response.write_eof()
                except Exception as exc:
                    if not _is_client_transport_closed(exc):
                        raise
        return response

    async def handle_run(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
            resolved_profile_name, resolution_error = _resolve_owner_scoped_profile(request, payload)
            if resolution_error is not None:
                return web.json_response({"error": resolution_error}, status=403)
            trusted_owner = str(request.headers.get(_OWNER_OPEN_ID_HEADER, "") or "").strip()
            metadata = _sanitize_ingest_metadata(payload.get("metadata") or {})
            _apply_expert_id_to_metadata(request, payload, metadata)
            share_context = _agent_share_context_for_request(
                request,
                payload,
                resolved_profile_name=str(resolved_profile_name or payload.get("profile_name") or payload.get("profile") or ""),
            )
            if share_context:
                metadata[_AGENT_SHARE_CONTEXT_METADATA_KEY] = share_context
            run_request = RunRequest(
                channel=payload.get("channel"),
                profile_name=resolved_profile_name or payload.get("profile_name") or payload.get("profile"),
                user_key=trusted_owner or payload.get("user_key") or payload.get("user"),
                content=payload.get("content"),
                chat_id=payload.get("chat_id"),
                session_id=payload.get("session_id"),
                message_id=payload.get("message_id"),
                idempotency_key=payload.get("idempotency_key"),
                delivery_mode=payload.get("delivery_mode") or "socket",
                credential_subject=trusted_owner or payload.get("credential_subject"),
                requires_host_tools=bool(payload.get("requires_host_tools")),
                metadata=metadata,
                messages=payload.get("messages") or [],
            )
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        return await _stream_run_request(request, run_request, stash_payload=payload)

    async def handle_credential_replay(request):
        """Re-run a request that failed on expired lark-cli credentials.

        Bearer-protected; the stashed entry is tenant-pinned, so only the owner
        who created it (same resolved profile + owner subject) may replay it.
        One-shot: a successfully re-dispatched entry is consumed.
        """
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        signal_run_id = str(request.match_info.get("signal_run_id") or "").strip()
        entry = _auth_signal_lookup(signal_run_id) if signal_run_id else None
        if entry is None:
            return web.json_response({"error": "unknown or expired signal_run_id"}, status=404)

        # Re-derive the caller's owner boundary exactly as handle_run does; never
        # trust a client-supplied profile that isn't owned.
        caller_profile, resolution_error = _resolve_owner_scoped_profile(request, {})
        if resolution_error is not None:
            return web.json_response({"error": resolution_error}, status=403)
        caller_subject = str(request.headers.get(_OWNER_OPEN_ID_HEADER, "") or "").strip()

        stashed_profile = str(entry.get("profile_name") or "")
        stashed_subject = str(entry.get("subject") or "")
        # Tenant isolation, made fail-closed at the endpoint (do not rely solely
        # on the upstream resolution_error early-return). When owner enforcement
        # is active — i.e. this broker is serving as the multi-user server
        # (HERMES_MULTITENANCY_RUN_BROKER_SERVER, which start_run_broker_server
        # refuses to run without a key) — the caller MUST positively prove the
        # stashed entry's owner identity. Missing/mismatched identity is denied.
        # Enforcement-off is the single-tenant/local path (no cross-tenant
        # boundary exists), so the positive-match requirement is scoped to it.
        if _owner_enforcement_enabled():
            if (
                caller_profile != stashed_profile
                or (stashed_subject and caller_subject != stashed_subject)
            ):
                return web.json_response({"error": "signal_run_id does not belong to caller"}, status=403)
        elif (caller_profile is not None and caller_profile != stashed_profile) or (
            caller_subject and caller_subject != stashed_subject
        ):
            return web.json_response({"error": "signal_run_id does not belong to caller"}, status=403)

        stashed_payload = dict(entry.get("payload") or {})
        try:
            run_request = RunRequest(
                channel=stashed_payload.get("channel"),
                profile_name=stashed_profile or stashed_payload.get("profile_name") or stashed_payload.get("profile"),
                user_key=stashed_subject or stashed_payload.get("user_key") or stashed_payload.get("user"),
                content=stashed_payload.get("content"),
                chat_id=stashed_payload.get("chat_id"),
                session_id=stashed_payload.get("session_id"),
                message_id=stashed_payload.get("message_id"),
                # A replay is a DELIBERATE re-run of the original request. Reusing
                # the original idempotency_key makes the broker's admit() dedup
                # treat it as a duplicate (mark_seen within the 24h window) and
                # return early WITHOUT dispatching — the whole re-auth loop would
                # then produce no answer. Drop the key so the replay dispatches.
                idempotency_key=None,
                delivery_mode=stashed_payload.get("delivery_mode") or "socket",
                credential_subject=stashed_subject or stashed_payload.get("credential_subject"),
                requires_host_tools=bool(stashed_payload.get("requires_host_tools")),
                metadata=_sanitize_ingest_metadata(stashed_payload.get("metadata") or {}),
                messages=stashed_payload.get("messages") or [],
            )
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        # One-shot: consume before dispatch so a retry can't double-fire the
        # same parked request; the new run parks its own fresh entry.
        _auth_signal_consume(signal_run_id)
        return await _stream_run_request(request, run_request, stash_payload=stashed_payload)

    # Result cache for ingest idempotency (gap D): a duplicate submission
    # returns the SAME result the first one produced, instead of an empty
    # body. Bounded in-memory (TTL + cap), consistent with the broker's own
    # in-memory dedup — survives within one broker process, not across.
    _ingest_results: dict[str, dict[str, Any]] = {}
    _ingest_results_at: dict[str, float] = {}
    _ingest_secret_fingerprints: dict[str, str] = {}
    _ingest_secret_fingerprints_at: dict[str, float] = {}
    _ingest_sync_secret_claims: dict[str, _IngestSyncSecretClaim] = {}
    _INGEST_RESULT_TTL = 3600.0
    _INGEST_RESULT_CAP = 256

    def _ingest_prune_secret_fingerprints(now: float) -> None:
        cutoff = now - _INGEST_RESULT_TTL
        for k in [k for k, t in _ingest_secret_fingerprints_at.items() if t < cutoff]:
            _ingest_secret_fingerprints.pop(k, None)
            _ingest_secret_fingerprints_at.pop(k, None)
        if len(_ingest_secret_fingerprints) > _INGEST_RESULT_CAP:
            overflow = sorted(_ingest_secret_fingerprints_at.items(), key=lambda kv: kv[1])
            for k, _ in overflow[: len(_ingest_secret_fingerprints) - _INGEST_RESULT_CAP]:
                _ingest_secret_fingerprints.pop(k, None)
                _ingest_secret_fingerprints_at.pop(k, None)

    def _ingest_store_result(key: str, value: dict[str, Any]) -> None:
        now = time.time()
        _ingest_results[key] = value
        _ingest_results_at[key] = now
        _ingest_prune_secret_fingerprints(now)
        cutoff = now - _INGEST_RESULT_TTL
        for k in [k for k, t in _ingest_results_at.items() if t < cutoff]:
            _ingest_results.pop(k, None)
            _ingest_results_at.pop(k, None)
        if len(_ingest_results) > _INGEST_RESULT_CAP:
            overflow = sorted(_ingest_results_at.items(), key=lambda kv: kv[1])
            for k, _ in overflow[: len(_ingest_results) - _INGEST_RESULT_CAP]:
                _ingest_results.pop(k, None)
                _ingest_results_at.pop(k, None)

    def _ingest_get_result(key: str) -> Optional[dict[str, Any]]:
        # Review NB2: validate TTL on READ, not only on write — never hand back
        # a stale cached answer just because no later write has pruned it yet.
        ts = _ingest_results_at.get(key)
        if ts is None:
            return None
        if time.time() - ts > _INGEST_RESULT_TTL:
            _ingest_results.pop(key, None)
            _ingest_results_at.pop(key, None)
            return None
        return _ingest_results.get(key)

    async def _prepare_ingest_run_request(request, *, delivery_mode: str):
        ingest_binding = _ingest_binding_for_request(request)
        if ingest_binding is None:
            return None, web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        owner = ingest_binding.get("owner", "")
        bound_profile_from_key = ingest_binding.get("profile", "")
        default_profile = bound_profile_from_key or os.environ.get("HERMES_INGEST_PROFILE", "").strip()
        # Preserve the v1 precedence: an authenticated but unconfigured fixed
        # profile fails before body validation.
        if not owner and not default_profile:
            return None, web.json_response(
                {"ok": False, "error": "ingest profile not configured"},
                status=503,
            )

        try:
            payload = await request.json()
        except Exception:
            return None, web.json_response(
                {"ok": False, "error": "invalid json"}, status=400
            )
        if not isinstance(payload, dict):
            return None, web.json_response(
                {"ok": False, "error": "body must be a JSON object"}, status=400
            )

        content = str(payload.get("content") or "").strip()
        if not content:
            return None, web.json_response(
                {"ok": False, "error": "content is required"}, status=400
            )
        secret_spec, secret_error = _parse_ingest_secrets(payload)
        if secret_error:
            return None, web.json_response(
                {"ok": False, "error": secret_error}, status=400
            )

        profile_bound_by_key = bool(bound_profile_from_key and ingest_binding.get("source") == "file")
        if profile_bound_by_key:
            agent_str = str(payload.get("agent") or "").strip()
            if not _ingest_agent_matches_binding(ingest_binding, agent_str):
                return None, web.json_response(
                    {
                        "ok": False,
                        "error": "agent not found or not owned by this key's owner",
                        "agents": [_ingest_bound_agent_label(ingest_binding)],
                    },
                    status=403,
                )
            bound_profile = bound_profile_from_key
        elif owner:
            agent_str = str(payload.get("agent") or "").strip()
            if not agent_str:
                return None, web.json_response(
                    {
                        "ok": False,
                        "error": "agent required",
                        "agents": [_ingest_agent_label(r) for r in _ingest_list_owner_agents(owner)],
                    },
                    status=400,
                )
            bound_profile, available = _ingest_resolve_agent(owner, agent_str)
            if not bound_profile:
                return None, web.json_response(
                    {
                        "ok": False,
                        "error": "agent not found or not owned by this key's owner",
                        "agents": available,
                    },
                    status=403,
                )
        else:
            bound_profile = default_profile

        skill = str(payload.get("skill") or "").strip()
        if skill:
            content = f"/{skill.lstrip('/')} {content}"

        metadata: dict[str, Any] = {}
        extra_meta = payload.get("metadata")
        if isinstance(extra_meta, dict):
            metadata.update(_sanitize_ingest_metadata(extra_meta))
        model = str(payload.get("model") or "").strip()
        if model:
            metadata["model"] = model
        metadata["source"] = "ingest"
        if secret_spec and secret_spec.manifest:
            metadata["ingest_secrets"] = list(secret_spec.manifest)

        # Ingest is an external execution surface: the caller cannot downgrade
        # sandbox admission by declaring that host tools are unnecessary.
        requires_host_tools = True
        interactive = bool(payload.get("interactive", False))

        try:
            run_request = RunRequest(
                channel="webui",
                profile_name=bound_profile,
                user_key=bound_profile,
                content=content,
                idempotency_key=str(payload.get("idempotency_key") or "").strip() or None,
                delivery_mode=delivery_mode,
                credential_subject=bound_profile,
                requires_host_tools=requires_host_tools,
                metadata=metadata,
            )
        except Exception as exc:
            return None, web.json_response({"ok": False, "error": str(exc)}, status=400)

        auth_fingerprint = _ingest_auth_fingerprint(request, ingest_binding)
        original_effective_idempotency_key = run_request.effective_idempotency_key
        run_request = replace(
            run_request,
            idempotency_key=_ingest_scoped_broker_idempotency_key(
                auth_fingerprint=auth_fingerprint,
                profile_name=bound_profile,
                effective_idempotency_key=original_effective_idempotency_key,
            ),
        )
        idempotency_cache_key = f"{bound_profile}\x00{original_effective_idempotency_key}"
        cache_key = f"{idempotency_cache_key}\x00secret:{secret_spec.fingerprint if secret_spec else ''}"
        return SimpleNamespace(
            binding=ingest_binding,
            auth_fingerprint=auth_fingerprint,
            bound_profile=bound_profile,
            cache_key=cache_key,
            idempotency_cache_key=idempotency_cache_key,
            interactive=interactive,
            run_request=run_request,
            secret_spec=secret_spec,
            secret_fingerprint=secret_spec.fingerprint if secret_spec else "",
        ), None

    _ingest_async_jobs: dict[str, dict[str, Any]] = {}
    _ingest_async_by_cache: dict[str, str] = {}
    _ingest_async_secret_fingerprints: dict[str, _IngestAsyncSecretClaim] = {}
    _INGEST_ASYNC_ACTIVE_STATUSES = {"pending", "running"}

    def _ingest_async_is_active(job: dict[str, Any]) -> bool:
        return str(job.get("status") or "") in _INGEST_ASYNC_ACTIVE_STATUSES

    def _ingest_async_remove(run_id: str) -> None:
        job = _ingest_async_jobs.pop(run_id, None)
        if not job:
            return
        _ingest_cleanup_secret_dir(str(job.get("secret_dir") or ""))
        cache = str(job.get("async_cache_key") or "")
        if cache and _ingest_async_by_cache.get(cache) == run_id:
            _ingest_async_by_cache.pop(cache, None)
        idempotency_cache = str(job.get("async_idempotency_cache_key") or "")
        claim = _ingest_async_secret_fingerprints.get(idempotency_cache)
        if idempotency_cache and claim is not None and not any(
                str(other.get("async_idempotency_cache_key") or "") == idempotency_cache
                for other in _ingest_async_jobs.values()
        ):
            claim.job_owned = False
            if (
                claim.request_refs == 0
                and _ingest_async_secret_fingerprints.get(idempotency_cache) is claim
            ):
                _ingest_async_secret_fingerprints.pop(idempotency_cache, None)

    def _ingest_async_prune() -> None:
        now = time.time()
        for run_id, job in list(_ingest_async_jobs.items()):
            if not _ingest_async_is_active(job) and float(job.get("expires_at") or 0) <= now:
                _ingest_async_remove(run_id)
        cap = _ingest_async_cap()
        if len(_ingest_async_jobs) <= cap:
            return
        overflow = sorted(
            [(run_id, job) for run_id, job in _ingest_async_jobs.items() if not _ingest_async_is_active(job)],
            key=lambda item: float(item[1].get("updated_at") or item[1].get("created_at") or 0),
        )
        for run_id, _job in overflow[: max(0, len(_ingest_async_jobs) - cap)]:
            _ingest_async_remove(run_id)

    def _ingest_async_make_room() -> bool:
        _ingest_async_prune()
        cap = _ingest_async_cap()
        if len(_ingest_async_jobs) < cap:
            return True
        removable = sorted(
            [(run_id, job) for run_id, job in _ingest_async_jobs.items() if not _ingest_async_is_active(job)],
            key=lambda item: float(item[1].get("updated_at") or item[1].get("created_at") or 0),
        )
        if removable:
            _ingest_async_remove(removable[0][0])
            return True
        return False

    def _ingest_async_touch(job: dict[str, Any], status: str) -> None:
        now = time.time()
        job["status"] = status
        job["updated_at"] = now
        job["expires_at"] = now + _ingest_async_ttl()
        if status not in _INGEST_ASYNC_ACTIVE_STATUSES:
            _ingest_cleanup_secret_dir(str(job.get("secret_dir") or ""))
            job["secret_dir"] = ""

    def _ingest_async_submit_response(job: dict[str, Any], *, duplicate: bool) -> dict[str, Any]:
        run_id = str(job["run_id"])
        return {
            "ok": True,
            "status": "accepted",
            "run_id": run_id,
            "profile": job["profile"],
            "poll_url": f"/api/run-broker/ingest/runs/{run_id}",
            "duplicate": duplicate,
        }

    def _ingest_async_poll_response(job: dict[str, Any]) -> dict[str, Any]:
        status = str(job.get("status") or "pending")
        resp: dict[str, Any] = {
            "ok": status in {"pending", "running", "succeeded"},
            "status": status,
            "run_id": job["run_id"],
            "profile": job["profile"],
            "duplicate": bool(job.get("duplicate", False)),
        }
        if status == "succeeded":
            resp["result"] = str(job.get("result") or "")
        elif status == "needs_clarification":
            resp["clarify"] = dict(job.get("clarify") or {})
            resp["result"] = str(job.get("result") or "")
        elif status == "needs_approval":
            resp["approval"] = dict(job.get("approval") or {})
            resp["result"] = str(job.get("result") or "")
        elif status in {"failed", "timeout"}:
            if job.get("error"):
                resp["error"] = str(job.get("error"))
        return resp

    async def _run_ingest_async_job(run_id: str, prepared: Any) -> None:
        job = _ingest_async_jobs.get(run_id)
        if job is None:
            return
        session_id = _ingest_run_session_id(prepared)

        collected: list[str] = []
        error_text: dict[str, str] = {}
        clarify_holder: dict[str, Any] = {}
        approval_holder: dict[str, Any] = {}
        interrupt = asyncio.Event()

        async def emit_event(event: RunEvent) -> None:
            if event.kind == "content" and event.text:
                collected.append(event.text)
            elif event.kind == "error":
                error_text["error"] = event.text or "agent error"
            elif event.kind == "clarify_required" and "payload" not in clarify_holder:
                clarify_holder["payload"] = event.payload or {}
                interrupt.set()
            elif event.kind == "approval_required" and "payload" not in approval_holder:
                approval_holder["payload"] = event.payload or {}
                interrupt.set()

        sink = emit_event if prepared.interactive else None
        broker_dispatch = dispatch_agent or (
            lambda req: _default_dispatch_agent(req, emit_event=sink)
        )
        timeout_s = _ingest_async_timeout()
        result = None
        run_task: Optional[asyncio.Task] = None
        interrupt_task: Optional[asyncio.Task] = None

        try:
            _ingest_async_touch(job, "running")
            if prepared.interactive:
                run_task = asyncio.ensure_future(
                    prepared.admission_broker._run_admitted(
                        prepared.admitted_run,
                        dispatch_agent=broker_dispatch,
                        emit_event=sink,
                    )
                )
                interrupt_task = asyncio.ensure_future(interrupt.wait())
                done, _pending = await asyncio.wait(
                    {run_task, interrupt_task},
                    timeout=timeout_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    run_task.cancel()
                    interrupt_task.cancel()
                    logger.warning(
                        "[multitenancy] async ingest run timed out run_id=%s profile=%s session_id=%s timeout_s=%s",
                        run_id,
                        prepared.bound_profile,
                        session_id,
                        timeout_s,
                    )
                    job["error"] = "timeout"
                    _ingest_async_touch(job, "timeout")
                    return
                if interrupt.is_set() and not run_task.done():
                    run_task.cancel()
                    try:
                        await run_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        logger.exception("[multitenancy] async ingest interactive cleanup error")
                    interrupt_task.cancel()
                else:
                    interrupt_task.cancel()
                    result = run_task.result()
            else:
                result = await asyncio.wait_for(
                    prepared.admission_broker._run_admitted(
                        prepared.admitted_run,
                        dispatch_agent=broker_dispatch,
                        emit_event=sink,
                    ),
                    timeout=timeout_s,
                )
        except asyncio.CancelledError:
            pending_children = [
                task
                for task in (run_task, interrupt_task)
                if task is not None and not task.done()
            ]
            for task in pending_children:
                task.cancel()
            if pending_children:
                await asyncio.gather(*pending_children, return_exceptions=True)
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "[multitenancy] async ingest run timed out run_id=%s profile=%s session_id=%s timeout_s=%s",
                run_id,
                prepared.bound_profile,
                session_id,
                timeout_s,
            )
            job["error"] = "timeout"
            _ingest_async_touch(job, "timeout")
            return
        except RunRejected as exc:
            job["error"] = _ingest_redact_text(str(exc), prepared.secret_spec)
            _ingest_async_touch(job, "failed")
            return
        except Exception as exc:
            classified_error = _ingest_classify_agent_error(
                _ingest_redact_text(str(exc), prepared.secret_spec)
            )
            logger.error(
                "[multitenancy] async ingest run failed run_id=%s profile=%s session_id=%s exc_type=%s error=%s",
                run_id,
                prepared.bound_profile,
                session_id,
                type(exc).__name__,
                classified_error,
            )
            job["error"] = classified_error
            _ingest_async_touch(job, "failed")
            return

        text = "".join(collected).strip() or (result.content if result else "")
        text = _ingest_redact_text(text, prepared.secret_spec)
        if error_text.get("error"):
            classified_error = _ingest_classify_agent_error(
                _ingest_redact_text(error_text["error"], prepared.secret_spec)
            )
            logger.error(
                "[multitenancy] async ingest agent error run_id=%s profile=%s session_id=%s error=%s",
                run_id,
                prepared.bound_profile,
                session_id,
                classified_error,
            )
            job["error"] = classified_error
            _ingest_async_touch(job, "failed")
            return
        if clarify_holder:
            _clear_pending_clarify(clarify_holder["payload"])
            job["clarify"] = _ingest_public_interaction(clarify_holder["payload"])
            job["result"] = text
            _ingest_async_touch(job, "needs_clarification")
            return
        if approval_holder:
            job["approval"] = _ingest_public_interaction(approval_holder["payload"])
            job["result"] = text
            _ingest_async_touch(job, "needs_approval")
            return
        job["result"] = text
        _ingest_async_touch(job, "succeeded")

    async def handle_ingest_async(request):
        prepared, error_response = await _prepare_ingest_run_request(
            request,
            delivery_mode="async",
        )
        if error_response is not None:
            return error_response
        _ingest_async_prune()

        idempotency_secret_key = f"{prepared.auth_fingerprint}\x00{prepared.idempotency_cache_key}"
        known_claim = _ingest_async_secret_fingerprints.get(idempotency_secret_key)
        if known_claim is not None and known_claim.fingerprint != prepared.secret_fingerprint:
            return _ingest_secret_mismatch_response(prepared.bound_profile)

        async_cache_key = f"{prepared.auth_fingerprint}\x00{prepared.cache_key}"
        existing_id = _ingest_async_by_cache.get(async_cache_key)
        existing = _ingest_async_jobs.get(existing_id or "")
        if existing is not None:
            return web.json_response(
                _ingest_async_submit_response(existing, duplicate=True),
                status=202,
            )

        if not _ingest_async_make_room():
            return web.json_response(
                {
                    "ok": False,
                    "status": "capacity_reached",
                    "error": "async ingest capacity reached",
                    "profile": prepared.bound_profile,
                },
                status=503,
            )

        admission_broker = RunBroker(
            dispatch_agent=lambda _req: "",
            mark_seen=effective_mark_seen,
            is_seen=effective_is_seen,
            sandbox_available=sandbox_available or _default_sandbox_available,
            prepare_request=prepare_billing_request,
        )
        try:
            # Preserve policy rejection precedence before billing I/O.
            admission_broker.check_policy(prepared.run_request)
        except RunRejected as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=403)

        # Claim the idempotency/secret pairing before the first shared await so
        # a concurrent request cannot join the same broker entry with different
        # credentials.  The shared leader releases this claim on abandonment;
        # a successful handoff keeps it for the registered job's lifetime.
        secret_claim = _ingest_async_secret_fingerprints.get(idempotency_secret_key)
        if secret_claim is not None and secret_claim.fingerprint != prepared.secret_fingerprint:
            return _ingest_secret_mismatch_response(prepared.bound_profile)
        if secret_claim is None:
            secret_claim = _IngestAsyncSecretClaim(prepared.secret_fingerprint)
            _ingest_async_secret_fingerprints[idempotency_secret_key] = secret_claim
        secret_claim.request_refs += 1

        run_id = "ing_" + secrets.token_urlsafe(16)
        secret_dir = ""
        now = time.time()
        job = {
            "run_id": run_id,
            "async_cache_key": async_cache_key,
            "async_idempotency_cache_key": idempotency_secret_key,
            "auth_fingerprint": prepared.auth_fingerprint,
            "profile": prepared.bound_profile,
            "status": "pending",
            "result": "",
            "error": "",
            "duplicate": False,
            "created_at": now,
            "updated_at": now,
            "expires_at": now + _ingest_async_ttl(),
            "secret_dir": secret_dir,
        }
        shared_entry_owned = asyncio.Event()
        execution_owned = asyncio.Event()
        job_start_gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        job_task: Optional[asyncio.Task] = None

        claim_ref_released = False

        def _release_fingerprint_claim_ref() -> None:
            nonlocal claim_ref_released
            if claim_ref_released:
                return
            claim_ref_released = True
            secret_claim.request_refs -= 1
            if secret_claim.request_refs < 0:
                raise RuntimeError("async ingest secret claim reference underflow")
            if (
                secret_claim.request_refs == 0
                and not secret_claim.job_owned
                and _ingest_async_secret_fingerprints.get(idempotency_secret_key)
                is secret_claim
            ):
                _ingest_async_secret_fingerprints.pop(idempotency_secret_key, None)

        def _cleanup_staged_secret() -> None:
            staged = str(job.get("secret_dir") or "")
            if not staged:
                return
            job["secret_dir"] = ""
            _ingest_cleanup_secret_dir(staged)

        def _job_task_unavailable() -> bool:
            if job_task is None or job_task.done():
                return True
            cancelling = getattr(job_task, "cancelling", None)
            return bool(callable(cancelling) and cancelling())

        def _cancel_waiting_job_task() -> None:
            if not job_start_gate.done():
                job_start_gate.cancel()
            if job_task is not None and not job_task.done():
                job_task.cancel()

        def _abandon_staged_resources() -> None:
            _cleanup_staged_secret()
            _cancel_waiting_job_task()

        def _finalize_job_task(done_task: asyncio.Task) -> None:
            if job.get("_task_finalized"):
                return
            job["_task_finalized"] = True
            failure = ""
            try:
                if done_task.cancelled():
                    failure = "async ingest task cancelled"
                else:
                    exc = done_task.exception()
                    if exc is not None:
                        failure = _ingest_classify_agent_error(
                            _ingest_redact_text(str(exc), prepared.secret_spec)
                        )
            except BaseException as exc:
                failure = f"async ingest task failed ({type(exc).__name__})"

            if _ingest_async_jobs.get(run_id) is not job:
                _cleanup_staged_secret()
                return
            if not _ingest_async_is_active(job):
                return
            failure = failure or "async ingest task ended before terminal status"
            job["error"] = failure
            _ingest_async_touch(job, "failed")
            logger.error(
                "[multitenancy] async ingest background task failed "
                "run_id=%s profile=%s error=%s",
                run_id,
                prepared.bound_profile,
                failure,
            )

        async def _run_registered_ingest_async_job() -> None:
            await job_start_gate
            await _run_ingest_async_job(run_id, prepared)

        def _stage_secret(prepared_run):
            nonlocal secret_dir
            staged_secret_dir = ""
            try:
                # The shared broker entry stages one secret directory after
                # billing succeeds but before durable admission.  A cancelled
                # leader with a live peer therefore cannot delete resources
                # that the shared execution will later use.
                staged_secret_dir = _ingest_materialize_secret_dir(
                    profile_name=prepared.bound_profile,
                    run_id=run_id,
                    secret_spec=prepared.secret_spec,
                )
                staged_request = prepared_run.request
                if staged_secret_dir:
                    staged_request = replace(
                        staged_request,
                        metadata={
                            **staged_request.metadata,
                            "ingest_secret_dir": staged_secret_dir,
                        },
                    )
            except Exception as exc:
                _ingest_cleanup_secret_dir(staged_secret_dir)
                raise _IngestSecretMaterializationError from exc
            secret_dir = staged_secret_dir
            job["secret_dir"] = staged_secret_dir
            return staged_request

        def _ensure_job_task_available(_prepared_run) -> None:
            if _job_task_unavailable():
                raise _IngestAsyncTaskStartError(
                    "async ingest task unavailable before admission"
                )

        def _handoff(admitted_run):
            try:
                prepared.admission_broker = admission_broker
                prepared.admitted_run = admitted_run
                prepared.run_request = admitted_run.request
                _ingest_async_jobs[run_id] = job
                _ingest_async_by_cache[async_cache_key] = run_id
                _ingest_async_secret_fingerprints[idempotency_secret_key] = (
                    secret_claim
                )
                secret_claim.job_owned = True
                if _job_task_unavailable() or job_start_gate.done():
                    job["error"] = "async ingest task unavailable after admission"
                    _ingest_async_touch(job, "failed")
                else:
                    job_start_gate.set_result(None)
                return RunResult(content="", duplicate=False)
            except BaseException:
                if _ingest_async_jobs.get(run_id) is job:
                    _ingest_async_jobs.pop(run_id, None)
                if _ingest_async_by_cache.get(async_cache_key) == run_id:
                    _ingest_async_by_cache.pop(async_cache_key, None)
                secret_claim.job_owned = False
                if (
                    secret_claim.request_refs == 0
                    and _ingest_async_secret_fingerprints.get(idempotency_secret_key)
                    is secret_claim
                ):
                    _ingest_async_secret_fingerprints.pop(idempotency_secret_key, None)
                _cleanup_staged_secret()
                _cancel_waiting_job_task()
                raise

        try:
            job_coro = _run_registered_ingest_async_job()
            try:
                job_task = asyncio.create_task(job_coro)
            except BaseException as exc:
                job_coro.close()
                job_start_gate.cancel()
                if isinstance(exc, Exception):
                    raise _IngestAsyncTaskStartError(
                        "async ingest task creation failed"
                    ) from None
                raise
            job["task"] = job_task
            job_task.add_done_callback(_finalize_job_task)
            if _job_task_unavailable():
                raise _IngestAsyncTaskStartError(
                    "async ingest task unavailable before preparation"
                )

            # Billing identity preparation can call the profile apiserver and
            # fail transiently. A persistent read-only duplicate check happens
            # first; concurrent retries then share the same preparation task.
            admission = await admission_broker.prepare_and_execute(
                prepared.run_request,
                execute=_handoff,
                transform_request=_stage_secret,
                before_admit=_ensure_job_task_available,
                shared_entry_owned=shared_entry_owned,
                execution_owned=execution_owned,
                on_abandon=_abandon_staged_resources,
            )
        except asyncio.CancelledError:
            # Known gotcha: the shared broker entry—not an outer request waiter—
            # owns staged secrets through pre-mark cancellation.  It either
            # abandons+cleans them or transfers them to the stable execution.
            raise
        except _IngestAsyncTaskStartError:
            logger.error(
                "[multitenancy] async ingest task startup failed profile=%s",
                prepared.bound_profile,
            )
            return web.json_response(
                {
                    "ok": False,
                    "status": "prepare_failed",
                    "error": "async ingest task startup failed",
                    "profile": prepared.bound_profile,
                },
                status=503,
            )
        except _IngestSecretMaterializationError:
            logger.exception("[multitenancy] async ingest secret store failed")
            return web.json_response(
                {"ok": False, "error": "invalid secrets"},
                status=400,
            )
        except Exception:
            logger.exception(
                "[multitenancy] async ingest billing preparation failed profile=%s",
                prepared.bound_profile,
            )
            return web.json_response(
                {
                    "ok": False,
                    "status": "prepare_failed",
                    "error": "billing preparation failed",
                    "profile": prepared.bound_profile,
                },
                status=503,
            )
        finally:
            _release_fingerprint_claim_ref()
            if not shared_entry_owned.is_set() and not execution_owned.is_set():
                _cancel_waiting_job_task()

        if execution_owned.is_set():
            return web.json_response(
                _ingest_async_submit_response(job, duplicate=False),
                status=202,
            )

        # A concurrent waiter can observe the leader's successful execution
        # result without owning its staged resources.  Return the one job that
        # the shared leader registered.
        _cleanup_staged_secret()
        existing_id = _ingest_async_by_cache.get(async_cache_key)
        existing = _ingest_async_jobs.get(existing_id or "")
        if existing is not None:
            return web.json_response(
                _ingest_async_submit_response(existing, duplicate=True),
                status=202,
            )
        if admission.duplicate:
            return web.json_response(
                {
                    "ok": False,
                    "status": "duplicate_pending",
                    "profile": prepared.bound_profile,
                    "duplicate": True,
                },
                status=200,
            )
        logger.error(
            "[multitenancy] async ingest execution completed without an owned or registered job profile=%s",
            prepared.bound_profile,
        )
        return web.json_response(
            {
                "ok": False,
                "status": "handoff_missing",
                "error": "async ingest handoff missing",
                "profile": prepared.bound_profile,
            },
            status=503,
        )

    async def handle_ingest_async_result(request):
        ingest_binding = _ingest_binding_for_request(request)
        if ingest_binding is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        _ingest_async_prune()
        run_id = str(request.match_info.get("run_id") or "").strip()
        job = _ingest_async_jobs.get(run_id)
        if job is None or job.get("auth_fingerprint") != _ingest_auth_fingerprint(request, ingest_binding):
            return web.json_response({"ok": False, "error": "run not found"}, status=404)
        return web.json_response(_ingest_async_poll_response(job), status=200)

    async def handle_ingest(request):
        """Synchronous external-ingest entry.

        Runs the agent as a *server-bound* profile and returns the result as
        one JSON response (no SSE). Identity is pinned via the
        ``HERMES_INGEST_PROFILE`` env var — the caller CANNOT choose the
        profile (any ``profile`` field in the body is ignored), so a leaked
        ``HERMES_INGEST_KEY`` can only ever run as that one bound identity.

        Interaction model (machine API — there is no human to answer):
        - default (``interactive`` false) — NON-INTERACTIVE one-shot dispatch
          with NO clarify/approval bridge wired (the bridges are only attached
          on the streaming path), so the agent can never block waiting on a
          human; it proceeds with best judgement and returns.
        - ``interactive`` true — streaming dispatch WITH the bridges, but the
          handler short-circuits on the first clarify/approval event: it
          cancels the run and returns ``needs_clarification`` / ``needs_approval``
          immediately instead of letting the bridge block up to its timeout.

        A hard ``HERMES_INGEST_TIMEOUT`` (default 180s) bounds either path.

        Capability parity with the cron/WebUI run paths: host-tool-capable
        admission is server-owned; ``model`` / ``metadata`` passthrough;
        duplicate submissions return the original result.
        See SPEC run-broker-ingest.
        """
        ingest_binding = _ingest_binding_for_request(request)
        if ingest_binding is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        owner = ingest_binding.get("owner", "")
        bound_profile_from_key = ingest_binding.get("profile", "")
        default_profile = bound_profile_from_key or os.environ.get("HERMES_INGEST_PROFILE", "").strip()
        # Review B2: in v1 mode (no owner), an unconfigured profile must 503
        # immediately after auth — same precedence as before this feature,
        # independent of body validity.
        if not owner and not default_profile:
            return web.json_response(
                {"ok": False, "error": "ingest profile not configured"},
                status=503,
            )

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid json"}, status=400
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"ok": False, "error": "body must be a JSON object"}, status=400
            )

        content = str(payload.get("content") or "").strip()
        if not content:
            return web.json_response(
                {"ok": False, "error": "content is required"}, status=400
            )
        secret_spec, secret_error = _parse_ingest_secrets(payload)
        if secret_error:
            return web.json_response(
                {"ok": False, "error": secret_error}, status=400
            )

        # Identity resolution (owner/default computed above, right after auth).
        # - OWNER mode (HERMES_INGEST_OWNER set): the caller MUST name one of the
        #   owner's own agents via ``agent``; the server validates ownership. No
        #   fallback to an unvalidated HERMES_INGEST_PROFILE (review B1) — every
        #   owner-mode run is ownership-checked, so a leaked key can only reach
        #   THIS owner's agents, never another owner's, never an arbitrary profile.
        # - v1 mode (no owner): fixed HERMES_INGEST_PROFILE; ``agent``/``profile``
        #   in the body are ignored. Behavior is byte-for-byte unchanged.
        profile_bound_by_key = bool(bound_profile_from_key and ingest_binding.get("source") == "file")
        if profile_bound_by_key:
            agent_str = str(payload.get("agent") or "").strip()
            if not _ingest_agent_matches_binding(ingest_binding, agent_str):
                return web.json_response(
                    {
                        "ok": False,
                        "error": "agent not found or not owned by this key's owner",
                        "agents": [_ingest_bound_agent_label(ingest_binding)],
                    },
                    status=403,
                )
            bound_profile = bound_profile_from_key
        elif owner:
            agent_str = str(payload.get("agent") or "").strip()
            if not agent_str:
                return web.json_response(
                    {
                        "ok": False,
                        "error": "agent required",
                        "agents": [_ingest_agent_label(r) for r in _ingest_list_owner_agents(owner)],
                    },
                    status=400,
                )
            bound_profile, available = _ingest_resolve_agent(owner, agent_str)
            if not bound_profile:
                return web.json_response(
                    {
                        "ok": False,
                        "error": "agent not found or not owned by this key's owner",
                        "agents": available,
                    },
                    status=403,
                )
        else:
            bound_profile = default_profile  # guaranteed non-empty (checked after auth)

        # Optional skill: prepend a slash command so the broker's native
        # skill-slash rewriter (run_broker._rewrite_skill_slash_request)
        # expands it — the same path the WebUI uses. A ``profile`` field in
        # the body is deliberately NOT read: identity is server-bound.
        skill = str(payload.get("skill") or "").strip()
        if skill:
            content = f"/{skill.lstrip('/')} {content}"

        # Gap C — model / metadata passthrough (parity with cron). Identity is
        # still server-bound; only run knobs are caller-tunable. Review NB1:
        # caller metadata is applied FIRST, then ``source`` is forced, so a
        # caller can never spoof the ingest provenance marker.
        metadata: dict[str, Any] = {}
        extra_meta = payload.get("metadata")
        if isinstance(extra_meta, dict):
            metadata.update(_sanitize_ingest_metadata(extra_meta))
        model = str(payload.get("model") or "").strip()
        if model:
            metadata["model"] = model
        metadata["source"] = "ingest"
        if secret_spec and secret_spec.manifest:
            metadata["ingest_secrets"] = list(secret_spec.manifest)

        # Gap A — host tools on (parity with cron/kanban), so credential-backed
        # skills work. Ingest is an external execution surface: the caller
        # cannot downgrade sandbox admission by declaring that host tools are
        # unnecessary.
        requires_host_tools = True
        interactive = bool(payload.get("interactive", False))

        try:
            run_request = RunRequest(
                channel="webui",
                profile_name=bound_profile,
                user_key=bound_profile,
                content=content,
                idempotency_key=str(payload.get("idempotency_key") or "").strip() or None,
                delivery_mode="sync",
                credential_subject=bound_profile,
                requires_host_tools=requires_host_tools,
                metadata=metadata,
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        # Review B3: namespace the idempotency cache by the resolved profile so
        # the same explicit idempotency_key used against two different owner
        # agents can never replay the wrong agent's cached result.
        auth_fingerprint = _ingest_auth_fingerprint(request, ingest_binding)
        original_effective_idempotency_key = run_request.effective_idempotency_key
        idempotency_cache_key = (
            f"{auth_fingerprint}\x00{bound_profile}\x00{original_effective_idempotency_key}"
        )
        secret_fingerprint = secret_spec.fingerprint if secret_spec else ""
        _ingest_prune_secret_fingerprints(time.time())
        known_fingerprint = _ingest_secret_fingerprints.get(idempotency_cache_key)
        if known_fingerprint is not None and known_fingerprint != secret_fingerprint:
            return _ingest_secret_mismatch_response(bound_profile)
        cache_key = f"{idempotency_cache_key}\x00secret:{secret_fingerprint}"
        run_request = replace(
            run_request,
            idempotency_key=_ingest_scoped_broker_idempotency_key(
                auth_fingerprint=auth_fingerprint,
                profile_name=bound_profile,
                effective_idempotency_key=original_effective_idempotency_key,
            ),
        )
        secret_dir = ""
        collected: list[str] = []
        error_text: dict[str, str] = {}
        clarify_holder: dict[str, Any] = {}
        approval_holder: dict[str, Any] = {}
        interrupt = asyncio.Event()

        async def emit_event(event: RunEvent) -> None:
            if event.kind == "content" and event.text:
                collected.append(event.text)
            elif event.kind == "error":
                error_text["error"] = event.text or "agent error"
            elif event.kind == "clarify_required" and "payload" not in clarify_holder:
                clarify_holder["payload"] = event.payload or {}
                interrupt.set()
            elif event.kind == "approval_required" and "payload" not in approval_holder:
                approval_holder["payload"] = event.payload or {}
                interrupt.set()

        # NON-INTERACTIVE (default): one-shot dispatch with NO event sink, so
        # agent_real wires neither the clarify nor the approval bridge (both
        # require an event_sink) — the run cannot block on a human.
        # INTERACTIVE: streaming dispatch with the sink so clarify/approval
        # events are emitted and we can short-circuit on them.
        sink = emit_event if interactive else None
        broker_dispatch = dispatch_agent or (
            lambda req: _default_dispatch_agent(req, emit_event=sink)
        )
        broker = RunBroker(
            dispatch_agent=broker_dispatch,
            emit_event=sink,
            mark_seen=effective_mark_seen,
            is_seen=effective_is_seen,
            sandbox_available=sandbox_available or _default_sandbox_available,
            prepare_request=prepare_billing_request,
        )
        shared_entry_owned = asyncio.Event()
        execution_owned = asyncio.Event()

        # Claim the key/fingerprint pairing before the first shared await. A
        # different-secret peer must never join this broker entry and reuse the
        # leader's staged credentials. The claim is transient until durable
        # admission creates the stable execution child; pre-admission failure
        # or cancellation releases it so a corrected secret can retry.
        sync_secret_claim = _ingest_sync_secret_claims.get(idempotency_cache_key)
        if (
            sync_secret_claim is not None
            and sync_secret_claim.fingerprint != secret_fingerprint
        ):
            return _ingest_secret_mismatch_response(bound_profile)
        if sync_secret_claim is None:
            sync_secret_claim = _IngestSyncSecretClaim(secret_fingerprint)
            _ingest_sync_secret_claims[idempotency_cache_key] = sync_secret_claim
        sync_secret_claim.request_refs += 1
        sync_claim_ref_released = False

        def _commit_sync_secret_claim() -> None:
            if sync_secret_claim.committed:
                return
            sync_secret_claim.committed = True
            if (
                _ingest_sync_secret_claims.get(idempotency_cache_key)
                is sync_secret_claim
            ):
                _ingest_secret_fingerprints[idempotency_cache_key] = (
                    sync_secret_claim.fingerprint
                )
                _ingest_secret_fingerprints_at[idempotency_cache_key] = time.time()

        def _release_sync_secret_claim_ref() -> None:
            nonlocal sync_claim_ref_released
            if sync_claim_ref_released:
                return
            sync_claim_ref_released = True
            sync_secret_claim.request_refs -= 1
            if sync_secret_claim.request_refs < 0:
                raise RuntimeError("sync ingest secret claim reference underflow")
            if (
                sync_secret_claim.request_refs == 0
                and _ingest_sync_secret_claims.get(idempotency_cache_key)
                is sync_secret_claim
            ):
                _ingest_sync_secret_claims.pop(idempotency_cache_key, None)

        def _abandon_sync_resources() -> None:
            try:
                _ingest_cleanup_secret_dir(secret_dir)
            finally:
                _release_sync_secret_claim_ref()

        def _finalize_sync_execution() -> None:
            # A stable task proves durable admission even if cancellation
            # prevents the execute coroutine from taking its first step.
            _commit_sync_secret_claim()
            try:
                _ingest_cleanup_secret_dir(secret_dir)
            finally:
                _release_sync_secret_claim_ref()

        def _stage_sync_secret(prepared_run):
            nonlocal secret_dir
            staged_secret_dir = ""
            try:
                staged_secret_dir = _ingest_materialize_secret_dir(
                    profile_name=bound_profile,
                    run_id="sync_" + secrets.token_urlsafe(16),
                    secret_spec=secret_spec,
                )
                staged_request = prepared_run.request
                if staged_secret_dir:
                    staged_request = replace(
                        staged_request,
                        metadata={
                            **staged_request.metadata,
                            "ingest_secret_dir": staged_secret_dir,
                        },
                    )
            except Exception as exc:
                _ingest_cleanup_secret_dir(staged_secret_dir)
                raise _IngestSecretMaterializationError from exc
            secret_dir = staged_secret_dir
            return staged_request

        async def _execute_sync(admitted_run):
            # RunBroker created this stable child behind a closed gate before
            # durable mark. Once the gate opens, persist the fingerprint before
            # dispatch can yield so request cancellation cannot reopen the key
            # to a peer carrying different credentials.
            _commit_sync_secret_claim()
            return await broker._run_admitted(admitted_run)

        async def _run_sync_request():
            return await broker.prepare_and_execute(
                run_request,
                execute=_execute_sync,
                transform_request=_stage_sync_secret,
                shared_entry_owned=shared_entry_owned,
                execution_owned=execution_owned,
                on_abandon=_abandon_sync_resources,
                on_execution_done=_finalize_sync_execution,
            )

        timeout_s = _ingest_timeout()

        # Review BLOCKING fix + NB4: every HTTP wait is bounded by a hard
        # timeout. Once durable admission succeeds, the stable execution task
        # continues independently and owns staged credentials until it exits.
        # Interactive runs stop holding the connection as soon as a human-input
        # event appears; their bridge task resolves on its own bounded timeout.
        result = None
        run_task: Optional[asyncio.Task] = None
        interrupt_task: Optional[asyncio.Task] = None
        try:
            if interactive:
                run_task = asyncio.ensure_future(_run_sync_request())
                interrupt_task = asyncio.ensure_future(interrupt.wait())
                done, _pending = await asyncio.wait(
                    {run_task, interrupt_task},
                    timeout=timeout_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:  # hard timeout
                    run_task.cancel()
                    interrupt_task.cancel()
                    logger.warning("[multitenancy] ingest run timed out after %ss", timeout_s)
                    return web.json_response(
                        {"ok": False, "status": "timeout", "profile": bound_profile},
                        status=504,
                    )
                if interrupt.is_set() and not run_task.done():
                    # Human-input needed: abandon the (bridge-blocked) run and
                    # surface the request immediately. The orphaned child run
                    # self-resolves at its own bridge timeout.
                    run_task.cancel()
                    try:
                        await run_task
                    except asyncio.CancelledError:
                        pass  # expected: we cancelled it
                    except Exception as exc:
                        # Review NB-new3: don't silently swallow cleanup
                        # failures — log them, but still surface the
                        # interaction request to the caller.
                        logger.error(
                            "[multitenancy] ingest interactive run cleanup error profile=%s session_id=%s exc_type=%s error=%s",
                            bound_profile,
                            run_request.session_id,
                            type(exc).__name__,
                            _ingest_classify_agent_error(
                                _ingest_redact_text(str(exc), secret_spec)
                            ),
                        )
                    interrupt_task.cancel()
                else:
                    interrupt_task.cancel()
                    result = run_task.result()
            else:
                result = await asyncio.wait_for(_run_sync_request(), timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning("[multitenancy] ingest run timed out after %ss", timeout_s)
            return web.json_response(
                {"ok": False, "status": "timeout", "profile": bound_profile},
                status=504,
            )
        except _IngestSecretMaterializationError:
            logger.exception("[multitenancy] ingest secret store failed")
            return web.json_response(
                {"ok": False, "error": "invalid secrets"},
                status=400,
            )
        except RunRejected as exc:
            # Policy rejection text is safe-ish, but keep it short.
            return web.json_response(
                {"ok": False, "error": _ingest_redact_text(str(exc), secret_spec)},
                status=403,
            )
        except Exception as exc:
            # Review NB3: do not leak internal exception text to an
            # internet-facing caller — log server-side, return generic.
            logger.error(
                "[multitenancy] ingest run failed profile=%s session_id=%s exc_type=%s error=%s",
                bound_profile,
                run_request.session_id,
                type(exc).__name__,
                _ingest_classify_agent_error(
                    _ingest_redact_text(str(exc), secret_spec)
                ),
            )
            return web.json_response(
                {"ok": False, "error": "internal error"}, status=500
            )
        finally:
            if interactive:
                # The aiohttp handler owns these child waiters. In particular,
                # an outer disconnect during billing must stop and await the
                # broker waiter before its transient fingerprint ref is
                # released; otherwise a different-secret request can join the
                # still-running old generation. After durable admission,
                # cancelling this waiter leaves the stable execution child
                # running by RunBroker contract.
                child_tasks = [
                    task
                    for task in (run_task, interrupt_task)
                    if task is not None
                ]
                for task in child_tasks:
                    if not task.done():
                        task.cancel()
                if child_tasks:
                    settled = asyncio.gather(
                        *child_tasks,
                        return_exceptions=True,
                    )
                    # A second transport/server cancellation must not cancel
                    # the cleanup gather and let the claim release race ahead
                    # of a still-live broker waiter. Shield and keep settling;
                    # the original outer cancellation is re-raised naturally
                    # after this finally block completes.
                    while not settled.done():
                        try:
                            await asyncio.shield(settled)
                        except asyncio.CancelledError:
                            continue
            # The stable child normally commits first. This event check covers
            # cancellation after mark opens its gate but before the child gets
            # its next coroutine step.
            if execution_owned.is_set():
                _commit_sync_secret_claim()
            if not (
                shared_entry_owned.is_set()
                and not execution_owned.is_set()
            ):
                _release_sync_secret_claim_ref()

        if error_text.get("error"):
            logger.error(
                "[multitenancy] ingest agent error: %s",
                _ingest_redact_text(error_text["error"], secret_spec),
            )
            return web.json_response(
                {"ok": False, "error": "agent run failed", "profile": bound_profile},
                status=500,
            )

        text = "".join(collected).strip() or (result.content if result else "")
        text = _ingest_redact_text(text, secret_spec)

        # Gap D — duplicate submission returns the ORIGINAL structured response
        # (success OR needs_*), not empty. Review NB-new2: caching the full
        # response (not just text) means an interrupted interactive run is also
        # replayable on its idempotency key instead of degrading to
        # duplicate_pending.
        if result is not None and result.duplicate:
            cached = _ingest_get_result(cache_key)
            if cached is not None:
                replay = dict(cached)
                replay["duplicate"] = True
                return web.json_response(replay, status=200)
            return web.json_response(
                {"ok": False, "status": "duplicate_pending", "profile": bound_profile, "duplicate": True},
                status=200,
            )

        # Gap B — surface clarify/approval as a clear status instead of a
        # silent empty body. Payloads are sanitized (review NB3) and cached so
        # a retry with the same idempotency key replays the same request.
        if clarify_holder:
            # Review NB-new1: clear the pending-clarify registration we
            # abandoned, so it doesn't leak (no clarify_resolved will arrive).
            _clear_pending_clarify(clarify_holder["payload"])
            resp = {
                "ok": False,
                "status": "needs_clarification",
                "clarify": _ingest_public_interaction(clarify_holder["payload"]),
                "result": text,
                "profile": bound_profile,
                "duplicate": False,
            }
            _ingest_store_result(cache_key, resp)
            return web.json_response(resp, status=200)
        if approval_holder:
            resp = {
                "ok": False,
                "status": "needs_approval",
                "approval": _ingest_public_interaction(approval_holder["payload"]),
                "result": text,
                "profile": bound_profile,
                "duplicate": False,
            }
            _ingest_store_result(cache_key, resp)
            return web.json_response(resp, status=200)

        resp = {"ok": True, "result": text, "profile": bound_profile, "duplicate": False}
        _ingest_store_result(cache_key, resp)
        return web.json_response(resp, status=200)

    async def handle_ingest_agents(request):
        """List the ingest owner's own agents (OWNER mode discovery).

        Lets a caller see which ``agent`` values are valid before POSTing to
        /ingest. Same fail-closed Bearer auth as /ingest. Returns 503 when the
        endpoint is not in OWNER mode (no HERMES_INGEST_OWNER configured).
        """
        ingest_binding = _ingest_binding_for_request(request)
        if ingest_binding is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        owner = ingest_binding.get("owner", "")
        bound_profile_from_key = ingest_binding.get("profile", "")
        if bound_profile_from_key and ingest_binding.get("source") == "file":
            return web.json_response(
                {
                    "ok": True,
                    "owner": owner,
                    "agents": [
                        {
                            "name": _ingest_bound_agent_label(ingest_binding),
                            "id": _ingest_bound_agent_id(ingest_binding),
                        }
                    ],
                },
                status=200,
            )
        if not owner:
            return web.json_response(
                {"ok": False, "error": "owner mode not configured"}, status=503
            )
        # Review NB2: distinguish "routing unavailable" from "owner has no
        # agents" — don't report ok:true with an empty list during an outage,
        # whether the table is absent OR the query itself raises.
        from . import router as router_mod

        table = router_mod._get_routing_table()
        if table is None:
            return web.json_response(
                {"ok": False, "error": "routing table unavailable"}, status=503
            )
        try:
            rows = table.list_agents_for_owner(owner)
        except Exception:
            logger.exception("[multitenancy] ingest /agents enumeration failed")
            return web.json_response(
                {"ok": False, "error": "routing table unavailable"}, status=503
            )
        agents = [
            r
            for r in rows
            if getattr(r, "kind", None) == "agent"
            and (getattr(r, "owner_open_id", None) or "") == owner
        ]
        return web.json_response(
            {
                "ok": True,
                "owner": owner,
                "agents": [
                    {"name": _ingest_agent_label(r), "id": _ingest_agent_id(r)}
                    for r in agents
                ],
            },
            status=200,
        )

    async def handle_clarify_respond(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        clarify_id = str(request.match_info.get("clarify_id") or "").strip()
        if not clarify_id:
            return web.json_response({"error": "clarify_id required"}, status=400)

        try:
            payload = await request.json()
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        resolved_profile_name, resolution_error = _resolve_owner_scoped_profile(request, payload)
        if resolution_error is not None:
            return web.json_response({"error": resolution_error}, status=403)

        trusted_owner = _trusted_owner_from_request(request)
        if not trusted_owner:
            return web.json_response({
                "error": "owner identity required (X-Hermes-Owner-Open-Id)"
            }, status=403)
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            return web.json_response({"error": "session_id required"}, status=400)
        response_text = str(payload.get("response") or payload.get("answer") or "").strip()

        wrote = _write_pending_clarify_response(
            clarify_id=clarify_id,
            owner_open_id=trusted_owner,
            profile_name=resolved_profile_name or "",
            session_id=session_id,
            response_text=response_text,
        )
        if not wrote:
            return web.json_response({"error": "clarify request not found"}, status=404)
        return web.json_response({"ok": True, "clarify_id": clarify_id})

    async def handle_session_command(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        resolved_profile_name, resolution_error = _resolve_owner_scoped_profile(request, payload)
        if resolution_error is not None:
            return web.json_response({"error": resolution_error}, status=403)
        if not resolved_profile_name:
            return web.json_response({
                "error": "owner identity required (X-Hermes-Owner-Open-Id)"
            }, status=403)

        trusted_owner = _trusted_owner_from_request(request)
        if not trusted_owner:
            return web.json_response({
                "error": "owner identity required (X-Hermes-Owner-Open-Id)"
            }, status=403)

        try:
            _assert_requested_profile_matches(
                resolved_profile_name=resolved_profile_name,
                payload=payload,
            )
            session_id = str(payload.get("session_id") or "").strip()
            if not session_id:
                return web.json_response({"error": "session_id required"}, status=400)
            raw_command = str(payload.get("command") or payload.get("text") or "").strip()
            if not raw_command:
                return web.json_response({"error": "command required"}, status=400)
            result = await _dispatch_session_command(
                profile_name=resolved_profile_name,
                user_key=trusted_owner,
                session_id=session_id,
                command=raw_command,
            )
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        return web.json_response({
            "ok": True,
            "profile_name": resolved_profile_name,
            "session_id": session_id,
            **result,
        })

    async def handle_internal_session_search(request):
        token = _ingest_bearer_token(request)
        claims = _lookup_session_search_broker_token(token)
        if claims is None:
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            payload = await request.json()
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "json object required"}, status=400)

        profile_name = cron_api.validate_profile_name(str(claims.get("profile_name") or ""))
        requested_profile = str(payload.get("profile") or "").strip()
        if requested_profile:
            try:
                requested_profile = cron_api.validate_profile_name(requested_profile)
            except Exception as exc:
                return web.json_response({"error": str(exc)}, status=400)
            if requested_profile != profile_name:
                return web.json_response({"error": "profile is not accessible for this run"}, status=403)

        session_id = payload.get("session_id")
        if isinstance(session_id, str):
            session_id = session_id.strip()
            if "/" in session_id:
                embedded_profile, _, embedded_session_id = session_id.partition("/")
                if embedded_profile and embedded_session_id:
                    try:
                        embedded_profile = cron_api.validate_profile_name(embedded_profile)
                    except Exception as exc:
                        return web.json_response({"error": str(exc)}, status=400)
                    if embedded_profile != profile_name:
                        return web.json_response({"error": "profile is not accessible for this run"}, status=403)
                    session_id = embedded_session_id.strip()

        try:
            from hermes_state import SessionDB
            from tools.session_search_tool import session_search

            profile_home = _profile_home_for_name(profile_name)
            db = SessionDB(db_path=profile_home / "state.db")
            search_db = _session_search_db_for_claims(db, claims)
            try:
                if session_id:
                    if not search_db.get_session(session_id):
                        result = json.dumps({
                            "success": False,
                            "error": f"session_id not found: {session_id}",
                        }, ensure_ascii=False)
                        return web.json_response({
                            "ok": True,
                            "profile_name": profile_name,
                            "run_id": str(claims.get("run_id") or ""),
                            "result": result,
                        })
                result = _call_session_search_compat(
                    session_search,
                    query=str(payload.get("query") or ""),
                    role_filter=payload.get("role_filter"),
                    limit=payload.get("limit", 3),
                    session_id=session_id,
                    around_message_id=payload.get("around_message_id"),
                    window=payload.get("window", 5),
                    sort=payload.get("sort"),
                    profile=None,
                    db=search_db,
                    current_session_id=payload.get("current_session_id"),
                )
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except Exception as exc:
            logger.exception("[multitenancy] internal session_search broker failed")
            return web.json_response({"error": str(exc)}, status=500)

        return web.json_response({
            "ok": True,
            "profile_name": profile_name,
            "run_id": str(claims.get("run_id") or ""),
            "result": result,
        })

    async def handle_goal_evaluate(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        resolved_profile_name, resolution_error = _resolve_owner_scoped_profile(request, payload)
        if resolution_error is not None:
            return web.json_response({"error": resolution_error}, status=403)
        if not resolved_profile_name:
            return web.json_response({
                "error": "owner identity required (X-Hermes-Owner-Open-Id)"
            }, status=403)

        try:
            _assert_requested_profile_matches(
                resolved_profile_name=resolved_profile_name,
                payload=payload,
            )
            session_id = str(payload.get("session_id") or "").strip()
            if not session_id:
                return web.json_response({"error": "session_id required"}, status=400)
            result = await _evaluate_goal_after_turn(
                profile_name=resolved_profile_name,
                session_id=session_id,
                final_response=payload.get("final_response") or payload.get("response") or "",
            )
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        return web.json_response({
            "ok": True,
            "profile_name": resolved_profile_name,
            "session_id": session_id,
            **result,
        })

    async def handle_provision_profile(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        trusted_owner = str(request.headers.get(_OWNER_OPEN_ID_HEADER, "") or "").strip()
        if not trusted_owner:
            return web.json_response({
                "error": "owner identity required (X-Hermes-Owner-Open-Id)"
            }, status=403)

        try:
            payload = await request.json()
            profile_name = cron_api.validate_profile_name(
                str(payload.get("profile_name") or payload.get("profile") or "")
            )
            display_label = str(payload.get("display_label") or profile_name).strip() or profile_name
            upstream_profile = str(payload.get("upstream_profile") or "").strip() or None
            if upstream_profile is not None:
                upstream_profile = cron_api.validate_profile_name(upstream_profile)
            requested_agent_id = str(payload.get("agent_id") or "").strip()
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)

        from . import router as router_mod

        table = router_mod._get_routing_table()
        if table is None:
            return web.json_response({
                "error": "trusted owner header requires routing table verification"
            }, status=403)
        owner_root = table.resolve_owner_root(trusted_owner)
        if owner_root is None:
            return web.json_response({
                "error": f"asserted owner '{trusted_owner}' has no sync-root profile"
            }, status=403)
        allowed_upstreams = _owner_allowed_upstream_profiles(table, trusted_owner, owner_root)
        if upstream_profile is not None and upstream_profile not in allowed_upstreams:
            return web.json_response({
                "error": f"upstream_profile '{upstream_profile}' is not accessible for asserted owner"
            }, status=403)
        effective_upstream_profile = upstream_profile or owner_root.profile_name

        stable_agent_id = _webui_agent_id(trusted_owner, profile_name)
        if requested_agent_id and requested_agent_id != stable_agent_id:
            existing = table.lookup_agent(requested_agent_id)
            if existing is not None and existing.owner_open_id != trusted_owner:
                return web.json_response({
                    "error": f"agent_id '{requested_agent_id}' does not belong to asserted owner"
                }, status=403)
            return web.json_response({"error": "agent_id does not match owner/profile"}, status=400)

        try:
            routed_profile_name = _webui_owned_profile_name(trusted_owner, profile_name)
            agent_id = table.upsert_owned_agent(
                agent_id=stable_agent_id,
                profile_name=routed_profile_name,
                owner_open_id=trusted_owner,
                display_label=display_label,
                upstream_profile=effective_upstream_profile,
            )
            shared_home = _shared_home_from_env()
            router_mod._ensure_webui_agent_profile(
                profile_name=routed_profile_name,
                profile_home=shared_home / "profiles" / routed_profile_name,
                owner_open_id=trusted_owner,
                display_label=display_label,
                agent_id=agent_id,
                upstream_profile=effective_upstream_profile,
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=403)

        return web.json_response({
            "ok": True,
            "agent_id": agent_id,
            "profile_name": profile_name,
            "routed_profile_name": routed_profile_name,
            "owner_open_id": trusted_owner,
            "display_label": display_label,
            "upstream_profile": effective_upstream_profile,
        })

    async def handle_list_shared_agents(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        actor_open_id = _trusted_owner_from_request(request)
        from . import router as router_mod

        table = router_mod._get_routing_table()
        if table is None:
            return web.json_response({"error": "routing table unavailable"}, status=503)
        actor_principal_id = _actor_principal_id_for_request(table, request, actor_open_id)
        if not actor_open_id and not actor_principal_id:
            return web.json_response({
                "error": "owner identity required (X-Hermes-Owner-Open-Id or actor principal headers)"
            }, status=403)
        shared_rows = []
        if actor_principal_id:
            shared_rows.extend(table.list_shared_agents_for_principal(actor_principal_id))
        if actor_open_id:
            existing = {str(getattr(row.route, "agent_id", "") or "") for row in shared_rows}
            shared_rows.extend(
                row
                for row in table.list_shared_agents_for_actor(actor_open_id)
                if str(getattr(row.route, "agent_id", "") or "") not in existing
            )
        agents = [_shared_agent_payload(shared.route, shared.share.role) for shared in shared_rows]
        return web.json_response({"agents": agents})

    async def handle_list_agent_shares(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        actor_open_id = _trusted_owner_from_request(request)
        agent_id = str(request.match_info.get("agent_id") or "").strip()
        from . import router as router_mod

        table = router_mod._get_routing_table()
        if table is None:
            return web.json_response({"error": "routing table unavailable"}, status=503)
        actor_principal_id = _actor_principal_id_for_request(table, request, actor_open_id)
        _row, error, status, actor_role = _resolve_agent_manager(table, actor_open_id, agent_id, actor_principal_id)
        if error is not None:
            return web.json_response({"error": error}, status=status)
        return web.json_response({
            "actor_role": actor_role,
            "shares": [
                _agent_share_payload(share)
                for share in table.list_agent_shares(agent_id)
            ]
        })

    async def handle_grant_agent_share(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        actor_open_id = _trusted_owner_from_request(request)
        agent_id = str(request.match_info.get("agent_id") or "").strip()
        try:
            payload = await request.json()
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=400)
        from . import router as router_mod

        table = router_mod._get_routing_table()
        if table is None:
            return web.json_response({"error": "routing table unavailable"}, status=503)
        actor_principal_id = _actor_principal_id_for_request(table, request, actor_open_id)
        _row, error, status, _actor_role = _resolve_agent_manager(table, actor_open_id, agent_id, actor_principal_id)
        if error is not None:
            return web.json_response({"error": error}, status=status)
        try:
            grantee_principal_id = str(payload.get("grantee_principal_id") or payload.get("granteePrincipalId") or "").strip()
            lookup = payload.get("grantee") if isinstance(payload.get("grantee"), dict) else None
            if not grantee_principal_id and lookup is not None:
                requester = table.lookup_principal(actor_principal_id)
                if requester is None:
                    return web.json_response({"error": "actor principal is required to resolve grantee"}, status=403)
                grantee_principal_id = _resolve_identity_lookup(table, lookup, requester).principal_id
            if grantee_principal_id:
                share = table.grant_agent_share_principal(
                    agent_id=agent_id,
                    grantee_principal_id=grantee_principal_id,
                    role=str(payload.get("role") or ""),
                    created_by_open_id=actor_open_id,
                    created_by_principal_id=actor_principal_id,
                )
            else:
                share = table.grant_agent_share(
                    agent_id=agent_id,
                    grantee_open_id=str(payload.get("grantee_open_id") or payload.get("granteeOpenId") or ""),
                    role=str(payload.get("role") or ""),
                    created_by_open_id=actor_open_id,
                )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"share": _agent_share_payload(share)})

    async def handle_revoke_agent_share(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        actor_open_id = _trusted_owner_from_request(request)
        agent_id = str(request.match_info.get("agent_id") or "").strip()
        share_key = str(request.match_info.get("share_key") or "").strip()
        from . import router as router_mod

        table = router_mod._get_routing_table()
        if table is None:
            return web.json_response({"error": "routing table unavailable"}, status=503)
        actor_principal_id = _actor_principal_id_for_request(table, request, actor_open_id)
        _row, error, status, _actor_role = _resolve_agent_manager(table, actor_open_id, agent_id, actor_principal_id)
        if error is not None:
            return web.json_response({"error": error}, status=status)
        if not table.revoke_agent_share_by_id(agent_id, share_key):
            table.revoke_agent_share(agent_id, share_key)
        return web.json_response({"ok": True})

    async def handle_slash_commands(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        payload = {
            "profile_name": request.query.get("profile_name") or request.query.get("profile"),
            "agent_id": request.query.get("agent_id"),
        }
        resolved_profile_name, resolution_error = _resolve_owner_scoped_profile(request, payload)
        if resolution_error is not None:
            return web.json_response({"error": resolution_error}, status=403)
        if not resolved_profile_name:
            return web.json_response({
                "error": "owner identity required (X-Hermes-Owner-Open-Id)"
            }, status=403)

        profile_name = resolved_profile_name
        try:
            profile_name = cron_api.validate_profile_name(str(profile_name or ""))
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)

        try:
            from .skill_registry import list_profile_skill_slash_commands

            shared_home = _shared_home_from_env()
            commands = _dedupe_slash_commands(
                _session_history_slash_commands() + list_profile_skill_slash_commands(
                    profile_home=shared_home / "profiles" / profile_name,
                )
            )
            return web.json_response({
                "ok": True,
                "profile_name": profile_name,
                "commands": commands,
            })
        except Exception as exc:
            logger.exception("[multitenancy] WebUI slash registry failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_list_jobs(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _owner_scoped_tenant(request)
            include_disabled = request.query.get("include_disabled", "").lower() in {"true", "1"}
            jobs = await asyncio.to_thread(
                cron_api.list_jobs,
                profile_name,
                include_disabled=include_disabled,
            )
            return web.json_response({"jobs": jobs})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron list failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_create_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
            profile_name, user_key = _owner_scoped_tenant(request, payload)
            job = await asyncio.to_thread(cron_api.create_job, profile_name, user_key, payload)
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron create failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_get_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _owner_scoped_tenant(request)
            job = await asyncio.to_thread(cron_api.get_job, profile_name, request.match_info["job_id"])
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron get failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_plan_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _owner_scoped_tenant(request)
            shadow = request.query.get("shadow", "1").lower() in {"1", "true", "yes", "on"}
            due_raw = request.query.get("due")
            due = None if due_raw is None else due_raw.lower() in {"1", "true", "yes", "on"}
            plan = await asyncio.to_thread(
                cron_api.plan_job,
                profile_name,
                request.match_info["job_id"],
                shadow=shadow,
                due=due,
            )
            return web.json_response({"plan": plan})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron plan failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_update_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
            profile_name, _user_key = _owner_scoped_tenant(request, payload)
            job = await asyncio.to_thread(
                cron_api.update_job,
                profile_name,
                request.match_info["job_id"],
                payload,
            )
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron update failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_delete_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _owner_scoped_tenant(request)
            await asyncio.to_thread(cron_api.delete_job, profile_name, request.match_info["job_id"])
            return web.json_response({"ok": True})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron delete failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_pause_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _owner_scoped_tenant(request)
            job = await asyncio.to_thread(cron_api.pause_job, profile_name, request.match_info["job_id"])
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron pause failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_resume_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _owner_scoped_tenant(request)
            job = await asyncio.to_thread(cron_api.resume_job, profile_name, request.match_info["job_id"])
            return web.json_response({"job": job})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron resume failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_run_job(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, _user_key = _owner_scoped_tenant(request)
            job = await asyncio.to_thread(cron_api.trigger_job, profile_name, request.match_info["job_id"])
            return web.json_response({"job": job, "queued": True})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI cron run trigger failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_feishu_uat_status(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import feishu_uat_auth

        try:
            profile_name, user_key = _owner_scoped_tenant(request)
            status = feishu_uat_auth.credential_status(
                profile_name=profile_name,
                open_id=user_key,
                required_scopes=request.query.get("required_scopes"),
            )
            return web.json_response(status)
        except feishu_uat_auth.FeishuUatAuthError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Feishu UAT status failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_credential_hub(request):
        """Aggregated, redacted credential status for one tenant.

        Intended convergence point (归口) for credential status in
        multitenancy: the Feishu ``/auth`` card already consumes the same
        ``credential_hub`` aggregation in-process. Full convergence still
        requires hermes-web-ui's ``skill-credentials.ts`` to read THIS endpoint
        instead of computing status independently, and the hub to reach parity
        with the WebUI's credential set (currently 3 vs 5). Until then this is
        an additional reader, not yet the sole source of truth — see
        ``.ftask/auth-cred-hub/SPEC.md``.
        """
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import credential_hub

        try:
            profile_name, user_key = _owner_scoped_tenant(request)
            # Offload to a thread: the aggregation may shell out to meegle/kep-auth
            # (subprocess), which must not block the aiohttp event loop.
            rows = await asyncio.to_thread(
                credential_hub.collect_credential_statuses,
                profile_name=profile_name,
                open_id=user_key,
            )
            return web.json_response(
                {
                    "profile_name": profile_name,
                    "subject_id": user_key,
                    "credentials": [row.to_dict() for row in rows],
                }
            )
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI credential hub failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_connectors(request):
        """Connector Registry status for one tenant — the NEW control-plane shape.

        Same five connectors as ``/credentials/hub`` but each row carries the
        additive 防串号 fields (profile / scope / acting_identity /
        credential_owner / runtime_policy_owner). The legacy ``/credentials/hub``
        endpoint stays byte-stable for the current WebUI; this endpoint is what
        the WebUI converges onto in a later phase. Redacted — never emits a
        secret, raw env, keyring path, or real profile-home secret path.
        """
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from .connectors import registry

        try:
            profile_name, user_key = _owner_scoped_tenant(request)
            # Serve from the short-TTL cache by default so repeat panel loads are instant
            # (the readers shell out to meegle/kep-auth — ~1.2s even parallelized). The
            # WebUI passes ?fresh=1 for its post-auth poll and the manual refresh button,
            # forcing a live read so a just-completed login shows without waiting for TTL.
            fresh = str(request.query.get("fresh") or "").strip().lower() in ("1", "true", "yes", "on")
            # Offload to a thread: collection may shell out to meegle/kep-auth.
            statuses = await asyncio.to_thread(
                registry.collect_connector_statuses,
                profile_name=profile_name,
                open_id=user_key,
                use_cache=not fresh,
            )
            return web.json_response(
                {
                    "profile_name": profile_name,
                    "subject_id": user_key,
                    "connectors": [status.to_dict() for status in statuses],
                }
            )
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI connectors failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_experts(request):
        """Expert-square catalog for one tenant — the experts[] a profile can use.

        Aggregates ``experts[]`` across the profile's ``.hermes-plugin-managed/*.json``
        managed manifests, filters by audience, and returns redacted display rows
        (no persona body, no repo path). Mirrors ``handle_connectors`` (tenant from
        request, thread offload, 401/500 shape). An ``expert_id`` from this list is
        what the WebUI passes back on a run to activate that expert's Role-Override.
        """
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import expert_overlay

        try:
            profile_name, user_key = _owner_scoped_tenant(request)
            profile_home = _profile_home_for_name(profile_name)
            # SERVER-SIDE department resolution from the TRUSTED tenant. The
            # caller-supplied ?department_ids= query param is deliberately IGNORED:
            # trusting it would let any caller expose a department-scoped expert by
            # passing a matching id. Unresolved departments → department-scoped
            # experts fail CLOSED inside list_experts.
            department_ids = await asyncio.to_thread(
                expert_overlay.resolve_caller_departments,
                profile_home,
                profile_name=profile_name,
                open_id=user_key,
            )
            experts = await asyncio.to_thread(
                expert_overlay.list_experts,
                profile_home,
                department_ids=department_ids,
            )
            return web.json_response({"profile_name": profile_name, "experts": experts})
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI experts failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_plugin_asset(request):
        """Serve one registered managed-plugin asset (currently expert avatars)."""
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import expert_overlay

        resolved = _resolve_registered_plugin_asset(
            request.match_info.get("plugin_id"),
            request.match_info.get("asset_name"),
        )
        if resolved is None:
            return web.json_response({"error": "not found"}, status=404)
        asset_path, mime, plugin_id, asset_name = resolved
        try:
            profile_name, user_key = _tenant_from_request(request, _tenant_payload_from_query(request))
            profile_home = _profile_home_for_name(profile_name)
            department_ids = await asyncio.to_thread(
                expert_overlay.resolve_caller_departments,
                profile_home,
                profile_name=profile_name,
                open_id=user_key,
            )
            visible_experts = await asyncio.to_thread(
                expert_overlay.list_experts,
                profile_home,
                department_ids=department_ids,
            )
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception:
            logger.exception("[multitenancy] WebUI plugin asset visibility check failed")
            return web.json_response({"error": "asset unavailable"}, status=500)
        asset_url = f"/api/run-broker/plugin-assets/{plugin_id}/{asset_name}"
        if not any(str(row.get("avatar") or "") == asset_url for row in visible_experts):
            return web.json_response({"error": "not found"}, status=404)
        try:
            body = await asyncio.to_thread(asset_path.read_bytes)
        except OSError:
            return web.json_response({"error": "not found"}, status=404)
        return web.Response(
            body=body,
            content_type=mime,
            headers={
                "Cache-Control": "public, max-age=3600",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def handle_credential_lease(request):
        header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return web.json_response({"error": "unauthorized"}, status=401)
        token_record = _lookup_credential_broker_token(header[len(prefix):].strip())
        if token_record is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        lease = str(payload.get("lease") or "").strip()
        kind = str(payload.get("kind") or "").strip()
        # The audit must never log a raw, attacker-controlled `kind` (it could
        # carry an injected secret/log-injection payload). Clamp to the known
        # enum; anything else is recorded as <invalid>. The real branch logic
        # below still uses `kind` and simply no-ops on an unknown value.
        audit_kind = kind if kind in {"feishu_uat", "provider_env"} else "<invalid>"
        profile_name = str(payload.get("profile_name") or "").strip()
        open_id = str(payload.get("open_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        try:
            claims = verify_lease(lease, secret=lease_signing_secret())
            assert_lease_binding(
                claims,
                profile_name=profile_name,
                open_id=open_id,
                run_id=run_id,
            )
            assert_lease_binding(
                claims,
                profile_name=token_record["profile_name"],
                open_id=token_record["open_id"],
                run_id=token_record["run_id"],
            )
            append_security_event(
                event_type="credential.lease.granted",
                open_id=claims.open_id,
                profile=claims.profile_name,
                lease_kind=audit_kind,
                decision="granted",
            )
        except LeaseError:
            # Attribute the denial to the AUTHENTICATED broker token, never the
            # untrusted request JSON — otherwise an attacker controls who the
            # audit blames. token_record is keyed by the bearer token already
            # verified above.
            append_security_event(
                event_type="credential.lease.denied",
                open_id=token_record["open_id"],
                profile=token_record["profile_name"],
                lease_kind=audit_kind,
                decision="denied",
                reason="lease_verification_failed",
            )
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            shared_role = str(token_record.get("share_role") or "")
            if kind == "feishu_uat":
                # Use _load_best_uat_payload (vault + materialized plaintext,
                # freshest wins) — the SAME source the normal non-strict path
                # uses — so strict credential coverage via the broker == actual
                # coverage. The vault-only _load_vault_uat_payload dropped any
                # profile that is materialized-but-not-yet-vaulted. This runs in
                # the PARENT broker server, so it does NOT let the child read
                # creds locally — the child still fetches via this broker route.
                # Shared-agent runs may lease only the grantee actor's own UAT,
                # never the owner fallback. The broker token binding above has
                # already been verified against the lease claims.
                from .feishu_uat_auth import _load_best_uat_payload
                from .provider_adapter import _resolve_shared_home

                profile_home = _profile_home_for_name(claims.profile_name)
                shared_home = _resolve_shared_home(profile_home)
                open_id_for_uat = token_record["open_id"] if shared_role in _AGENT_SHARED_ROLES else claims.open_id
                payload_value = _load_best_uat_payload(shared_home, claims.profile_name, open_id_for_uat)
                return web.json_response({"payload": payload_value})
            if kind == "provider_env":
                from .provider_adapter import provider_env_for_aiagent

                if shared_role in _AGENT_SHARED_ROLES:
                    return web.json_response({"payload": {}})
                profile_home = _profile_home_for_name(claims.profile_name)
                payload_value = provider_env_for_aiagent(profile_home)
                return web.json_response({"payload": payload_value})
            return web.json_response({"error": "bad_request"}, status=400)
        except Exception:
            return web.json_response({"error": "internal"}, status=500)

    async def handle_kep_cli_callback(request):
        """Public OAuth callback for kep-cli in-Feishu auth: forwards the OAuth
        result to the kep-auth localhost server (same host). Intentionally NOT
        bearer-auth'd — it is hit by the user's browser after authorizing, keyed
        by an unguessable one-time session id. Returns a small HTML page."""
        from . import credential_hub_auth as cha

        sid = request.match_info.get("session_id", "")
        try:
            await asyncio.to_thread(cha.complete_kep_callback, sid, request.query_string)
        except cha.HubAuthError as exc:
            return web.Response(
                text=f"<html><body style='font-family:sans-serif'>认证未完成：{exc.message}<br>可重新发送 /auth 重试。</body></html>",
                content_type="text/html", status=exc.status,
            )
        except Exception as exc:
            logger.exception("[multitenancy] kep-cli callback failed")
            return web.Response(text=f"认证出错：{exc}", content_type="text/html", status=500)
        return web.Response(
            text="<html><body style='font-family:sans-serif'>✅ kep-cli 认证成功，可关闭本页面，返回飞书查看结果。</body></html>",
            content_type="text/html",
        )

    async def handle_feishu_auth_start(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import feishu_uat_auth

        try:
            payload = await request.json()
            profile_name, user_key = _owner_scoped_tenant(request, payload)
            body = feishu_uat_auth.start_session(
                profile_name=profile_name,
                open_id=user_key,
                scope=payload.get("scope"),
            )
            return web.json_response(body)
        except feishu_uat_auth.FeishuUatAuthError as exc:
            return web.json_response({"error": exc.message, "status": "error"}, status=exc.status)
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message, "status": "error"}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Feishu auth start failed")
            return web.json_response({"error": str(exc), "status": "error"}, status=500)

    async def handle_feishu_auth_poll(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import feishu_uat_auth

        try:
            profile_name, user_key = _owner_scoped_tenant(request)
            body = feishu_uat_auth.poll_session(
                session_id=request.match_info["session_id"],
                profile_name=profile_name,
                open_id=user_key,
            )
            status = 200 if body.get("status") != "error" else 400
            return web.json_response(body, status=status)
        except feishu_uat_auth.FeishuUatAuthError as exc:
            return web.json_response({"error": exc.message, "status": "error"}, status=exc.status)
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message, "status": "error"}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Feishu auth poll failed")
            return web.json_response({"error": str(exc), "status": "error"}, status=500)

    async def handle_feishu_auth_cancel(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import feishu_uat_auth

        try:
            profile_name, user_key = _owner_scoped_tenant(request)
            body = feishu_uat_auth.cancel_session(
                session_id=request.match_info["session_id"],
                profile_name=profile_name,
                open_id=user_key,
            )
            return web.json_response(body)
        except feishu_uat_auth.FeishuUatAuthError as exc:
            return web.json_response({"error": exc.message, "status": "error"}, status=exc.status)
        except cron_api.CronApiError as exc:
            return web.json_response({"error": exc.message, "status": "error"}, status=exc.status)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Feishu auth cancel failed")
            return web.json_response({"error": str(exc), "status": "error"}, status=500)

    async def handle_kanban_assignees(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import kanban_sidecar

        try:
            assignees = kanban_sidecar.list_owner_assignees(
                owner_open_id=_trusted_owner_from_request(request),
                board=_kanban_board_from_request(request),
            )
            return web.json_response({"assignees": assignees})
        except kanban_sidecar.KanbanApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Kanban assignees failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_kanban_capabilities(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import kanban_sidecar

        try:
            # Still require owner scope so this endpoint cannot be used as a
            # generic unauthenticated capability probe when broker auth is off
            # in local development.
            kanban_sidecar.list_owner_assignees(
                owner_open_id=_trusted_owner_from_request(request),
                board=_kanban_board_from_request(request),
            )
            return web.json_response({"capabilities": kanban_sidecar.owner_kanban_capabilities()})
        except kanban_sidecar.KanbanApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Kanban capabilities failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_kanban_stats(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import kanban_sidecar

        try:
            stats = kanban_sidecar.owner_kanban_stats(
                owner_open_id=_trusted_owner_from_request(request),
                board=_kanban_board_from_request(request),
            )
            return web.json_response({"stats": stats})
        except kanban_sidecar.KanbanApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Kanban stats failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_kanban_boards(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import kanban_sidecar

        try:
            include_archived = request.query.get("includeArchived", "").lower() in {"1", "true", "yes", "on"}
            boards = kanban_sidecar.list_owner_boards(
                owner_open_id=_trusted_owner_from_request(request),
                include_archived=include_archived,
            )
            return web.json_response({"boards": boards})
        except kanban_sidecar.KanbanApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Kanban boards failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_kanban_tasks(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import kanban_sidecar

        try:
            include_archived = request.query.get("includeArchived", "").lower() in {"1", "true", "yes", "on"}
            tasks = kanban_sidecar.list_owner_tasks(
                owner_open_id=_trusted_owner_from_request(request),
                board=_kanban_board_from_request(request),
                status=request.query.get("status"),
                assignee=request.query.get("assignee"),
                tenant=request.query.get("tenant"),
                include_archived=include_archived,
            )
            return web.json_response({"tasks": tasks})
        except kanban_sidecar.KanbanApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Kanban list failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_kanban_create_task(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import kanban_sidecar

        try:
            payload = await request.json()
            task = kanban_sidecar.create_owner_task(
                owner_open_id=_trusted_owner_from_request(request),
                payload=payload,
                board=_kanban_board_from_request(request),
            )
            return web.json_response({"task": task})
        except kanban_sidecar.KanbanApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Kanban create failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_kanban_dispatch(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import kanban_sidecar

        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                return web.json_response({"error": "request body must be an object"}, status=400)
            result = kanban_sidecar.dispatch_owner_kanban(
                owner_open_id=_trusted_owner_from_request(request),
                board=_kanban_board_from_request(request),
                dry_run=_optional_bool(payload.get("dryRun"), "dryRun", default=True),
                max_spawn=_optional_positive_int(payload.get("max"), "max"),
                max_in_progress=_optional_positive_int(payload.get("maxInProgress"), "maxInProgress"),
            )
            return web.json_response({"result": result})
        except kanban_sidecar.KanbanApiError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI Kanban dispatch failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_skillhub_install(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import skill_registry

        try:
            payload = await request.json()
            resolved_profile_name, resolution_error = _resolve_owner_scoped_profile(request, payload)
            if resolution_error is not None:
                return web.json_response({"error": resolution_error}, status=403)
            if not resolved_profile_name:
                return web.json_response({
                    "error": "owner identity required (X-Hermes-Owner-Open-Id)"
                }, status=403)
            profile_name = resolved_profile_name
            shared_home = _shared_home_from_env()
            discovery_policy = None
            discovery_source = _skillhub_discovery_source(payload)
            if discovery_source:
                from .discovery_policy import plan_profile_discovery

                owner_open_id = str(request.headers.get(_OWNER_OPEN_ID_HEADER, "") or "").strip()
                plan = plan_profile_discovery(
                    shared_home=shared_home,
                    profile_name=profile_name,
                    user_key=owner_open_id or None,
                    requested_sources=[discovery_source],
                    requested_toolsets=[],
                    audit=True,
                )
                decision = plan["sources"].get(discovery_source) or {}
                discovery_policy = {
                    "source": discovery_source,
                    "allowed": bool(decision.get("allowed")),
                    "reason": decision.get("reason"),
                    "requires_audit": bool(decision.get("requires_audit")),
                    "audit_written": bool((plan.get("audit") or {}).get("written")),
                }
                if not discovery_policy["allowed"]:
                    return web.json_response({
                        "error": "discovery source blocked by policy",
                        "profile_name": profile_name,
                        "discovery_policy": discovery_policy,
                    }, status=403)
            install = skill_registry.install_shared_skill_for_profile(
                shared_home=shared_home,
                profile_home=shared_home / "profiles" / profile_name,
                skill_path=str(payload.get("skill_path") or payload.get("path") or ""),
                source=payload.get("source"),
                version=payload.get("version"),
            )
            installed = skill_registry.list_installed_skills(
                profile_home=shared_home / "profiles" / profile_name
            )
            return web.json_response({
                "profile_name": profile_name,
                "install": install,
                "installed_skills": installed,
                **({"discovery_policy": discovery_policy} if discovery_policy else {}),
            })
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except FileNotFoundError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI SkillHub install failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_skill_audit(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import skill_registry

        try:
            shared_home = _shared_home_from_env()
            report = skill_registry.audit_installed_skills(shared_home=shared_home)
            return web.json_response(report)
        except Exception as exc:
            logger.exception("[multitenancy] WebUI skill audit failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def handle_skillhub_event(request):
        """Ingress for AiDock SkillHub webhooks (skill.install_approved, etc.).

        Validates, deduplicates and persists the event, then acks. Actual skill
        materialization (download/router-install/symlink/callback) is the next
        phase — accepted events land as ``queued`` for a downstream worker.
        """
        if not _skillhub_authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        from . import skillhub_events

        # Read with a hard cap (streams, so chunked bodies can't force a 32MB buffer).
        raw = await _read_capped_body(request, _SKILLHUB_MAX_BODY_BYTES)
        if raw is None:
            return web.json_response(
                {
                    "ok": False,
                    "error_code": "PAYLOAD_TOO_LARGE",
                    "message": f"body exceeds {_SKILLHUB_MAX_BODY_BYTES} bytes",
                    "retryable": False,
                },
                status=413,
            )
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return web.json_response(
                {
                    "ok": False,
                    "error_code": "INVALID_JSON",
                    "message": "request body is not valid JSON",
                    "retryable": False,
                },
                status=400,
            )

        raw_body = raw.decode("utf-8")
        try:
            event = skillhub_events.normalize_event(payload, raw_body=raw_body)
        except skillhub_events.SkillhubEventError as exc:
            return web.json_response(
                {
                    "ok": False,
                    "error_code": exc.error_code,
                    "message": str(exc),
                    "retryable": False,
                },
                status=400,
            )

        # Optional HMAC signature gate. Mirrors _authorized: when no secret is
        # configured (dev), the gate is open; when a secret IS set, a missing or
        # bad signature is rejected.
        secret = os.environ.get("HERMES_SKILLHUB_WEBHOOK_SECRET", "").strip()
        signature_verified = False
        if secret:
            timestamp = str(request.headers.get("X-AiDock-Timestamp", "") or "").strip()
            provided = str(request.headers.get("X-AiDock-Signature", "") or "").strip()
            if not skillhub_events.verify_signature(
                secret, timestamp, event["event_id"], raw_body, provided
            ):
                return web.json_response(
                    {
                        "ok": False,
                        "event_id": event["event_id"],
                        "error_code": "INVALID_SIGNATURE",
                        "message": "signature verification failed",
                        "retryable": False,
                    },
                    status=401,
                )
            signature_verified = True

        try:
            store = skillhub_events.get_event_store()
            status, duplicate = store.record(
                event, raw_payload=raw_body, signature_verified=signature_verified
            )
            if status == "queued" and not duplicate:
                try:
                    from . import skillhub_installer

                    shared_home = _shared_home_from_env()
                    skillhub_installer.schedule_drain(shared_home)
                except Exception:
                    logger.exception("[multitenancy] SkillHub background install schedule failed")
        except Exception:
            logger.exception("[multitenancy] SkillHub event persist failed")
            return web.json_response(
                {
                    "ok": False,
                    "event_id": event["event_id"],
                    "error_code": "INTERNAL_ERROR",
                    "message": "failed to persist event",
                    "retryable": True,
                },
                status=500,
            )

        logger.info(
            "[multitenancy] SkillHub event received event_id=%s type=%s skill=%s release=%s dup=%s",
            event["event_id"],
            event["event_type"],
            event["skill_code"],
            event.get("release_id"),
            duplicate,
        )
        return web.json_response(
            {
                "ok": True,
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "skill_code": event["skill_code"],
                "accepted": True,
                "duplicate": duplicate,
                "status": status,
            }
        )

    async def handle_health(request):
        if not _authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({
            "ok": True,
            "service": "hermes-multitenancy-run-broker",
        })

    _helpdesk_cache: dict = {}

    async def handle_feishu_helpdesk_events(request):
        # Internal endpoint: the Feishu ws-adapter forwards helpdesk ticket events
        # here. Business logic + the hard test-helpdesk safety filter live in
        # feishu_helpdesk_event.handle_helpdesk_event. Shadow by default (post gate).
        if not _authorized(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"bad json: {exc}"}, status=400)

        from .feishu_helpdesk_event import handle_helpdesk_event

        index = _helpdesk_cache.get("index")
        if index is None:
            from .helpdesk_rag import HelpdeskRagIndex

            db = os.path.expanduser(
                os.environ.get("HERMES_HELPDESK_INDEX_DB", "~/.hermes/profiles/helpdesk/ticket_index.db")
            )
            index = HelpdeskRagIndex(db)
            _helpdesk_cache["index"] = index
            doc_count = index.count()
            if doc_count == 0:
                logger.warning(
                    "[multitenancy] helpdesk RAG index at %s is EMPTY (0 docs) — answers will be "
                    "ungrounded; build the index (ingest faqs/tickets) before relying on it",
                    db,
                )
            else:
                logger.info("[multitenancy] helpdesk RAG index loaded: %d docs (%s)", doc_count, db)

        # The test-helpdesk client is needed even in shadow mode: the event carries no
        # helpdesk_id, so we confirm ticket membership by querying it with this helpdesk's
        # token (real IT-helpdesk tickets fail and are dropped).
        client = _helpdesk_cache.get("client")
        if client is None:
            from .feishu_helpdesk_client import HelpdeskClient

            client = HelpdeskClient(
                app_id=os.environ.get("HERMES_HELPDESK_APP_ID", ""),
                app_secret=os.environ.get("HERMES_HELPDESK_APP_SECRET", ""),
                helpdesk_id=os.environ.get("HERMES_HELPDESK_ID", ""),
                helpdesk_token=os.environ.get("HERMES_HELPDESK_TOKEN", ""),
            )
            _helpdesk_cache["client"] = client

        def _membership_check(ticket_id: str) -> bool:
            try:
                client.get_ticket(ticket_id)
                return True
            except Exception:
                return False

        post_enabled = os.environ.get("HERMES_HELPDESK_POST", "").strip().lower() in ("1", "true", "yes")
        reply_fn = client.send_ticket_message if post_enabled else None

        # SAFETY WELD: never operate against a denied (production) helpdesk, even if the
        # env is misconfigured to point at it.
        from .feishu_helpdesk_event import ALLOWED_HELPDESK_IDS, DENY_HELPDESK_IDS

        configured_id = os.environ.get("HERMES_HELPDESK_ID", "").strip()
        if configured_id in DENY_HELPDESK_IDS or configured_id not in ALLOWED_HELPDESK_IDS:
            logger.error(
                "[multitenancy] REFUSING helpdesk events: HERMES_HELPDESK_ID=%r is denied or not in the "
                "allowlist %s — never auto-answer non-allowlisted / real-employee helpdesks",
                configured_id, sorted(ALLOWED_HELPDESK_IDS),
            )
            return web.json_response({"ok": False, "error": "helpdesk not allowlisted"}, status=403)

        def _process() -> None:
            # heavy work OFF the event loop: membership API + RAG + inference (+ reply)
            try:
                result = handle_helpdesk_event(
                    payload, index=index, membership_check=_membership_check, reply_fn=reply_fn, post=post_enabled
                )
            except Exception:
                logger.exception("[multitenancy] helpdesk event handling failed")
                return
            logger.info(
                "[multitenancy] helpdesk event action=%s ticket=%s posted=%s q=%r",
                result.get("action"), result.get("ticket_id"), result.get("posted"),
                (result.get("question") or "")[:80],
            )
            if result.get("answer"):
                logger.info("[multitenancy] helpdesk draft answer: %s", str(result.get("answer"))[:600])

        # fast-ack: never block the broker event loop on RAG/inference/Feishu I/O
        asyncio.get_event_loop().run_in_executor(None, _process)
        return web.json_response({"ok": True, "accepted": True})

    app = web.Application(client_max_size=_run_broker_client_max_size())
    async def _start_skillhub_drain(_app):
        from . import skillhub_installer

        skillhub_installer.schedule_drain(_shared_home_from_env())

    app.on_startup.append(_start_skillhub_drain)
    app.router.add_get("/api/run-broker/health", handle_health)
    app.router.add_post("/api/run-broker/feishu/helpdesk/events", handle_feishu_helpdesk_events)
    app.router.add_post("/api/run-broker/runs", handle_run)
    app.router.add_post(
        "/api/run-broker/credentials/replay/{signal_run_id}", handle_credential_replay
    )
    app.router.add_post("/api/run-broker/ingest/async", handle_ingest_async)
    app.router.add_get("/api/run-broker/ingest/runs/{run_id}", handle_ingest_async_result)
    app.router.add_post("/api/run-broker/ingest", handle_ingest)
    app.router.add_get("/api/run-broker/ingest/agents", handle_ingest_agents)
    app.router.add_post("/api/run-broker/clarify/{clarify_id}/respond", handle_clarify_respond)
    app.router.add_post("/api/run-broker/session-commands", handle_session_command)
    app.router.add_post("/api/run-broker/internal/session-search", handle_internal_session_search)
    app.router.add_post("/api/run-broker/goals/evaluate", handle_goal_evaluate)
    app.router.add_post("/api/run-broker/profiles", handle_provision_profile)
    app.router.add_get("/api/run-broker/agents/shared", handle_list_shared_agents)
    app.router.add_get("/api/run-broker/agents/{agent_id}/shares", handle_list_agent_shares)
    app.router.add_post("/api/run-broker/agents/{agent_id}/shares", handle_grant_agent_share)
    app.router.add_delete("/api/run-broker/agents/{agent_id}/shares/{share_key}", handle_revoke_agent_share)
    app.router.add_get("/api/run-broker/slash/commands", handle_slash_commands)
    app.router.add_post("/api/run-broker/credentials/lease", handle_credential_lease)
    app.router.add_get("/api/run-broker/credentials/feishu/uat/status", handle_feishu_uat_status)
    app.router.add_get("/api/run-broker/credentials/hub", handle_credential_hub)
    app.router.add_get("/api/run-broker/connectors", handle_connectors)
    app.router.add_get("/api/run-broker/experts", handle_experts)
    app.router.add_get("/api/run-broker/plugin-assets/{plugin_id}/{asset_name}", handle_plugin_asset)
    app.router.add_get("/api/run-broker/credentials/kep-cli/callback/{session_id}", handle_kep_cli_callback)
    app.router.add_post("/api/run-broker/feishu-auth/sessions", handle_feishu_auth_start)
    app.router.add_get("/api/run-broker/feishu-auth/sessions/{session_id}", handle_feishu_auth_poll)
    app.router.add_delete("/api/run-broker/feishu-auth/sessions/{session_id}", handle_feishu_auth_cancel)
    app.router.add_get("/api/run-broker/kanban/boards", handle_kanban_boards)
    app.router.add_get("/api/run-broker/kanban/capabilities", handle_kanban_capabilities)
    app.router.add_get("/api/run-broker/kanban/assignees", handle_kanban_assignees)
    app.router.add_get("/api/run-broker/kanban/stats", handle_kanban_stats)
    app.router.add_get("/api/run-broker/kanban/tasks", handle_kanban_tasks)
    app.router.add_post("/api/run-broker/kanban/tasks", handle_kanban_create_task)
    app.router.add_post("/api/run-broker/kanban/dispatch", handle_kanban_dispatch)
    app.router.add_post("/api/run-broker/skills/install", handle_skillhub_install)
    app.router.add_get("/api/run-broker/skills/audit", handle_skill_audit)
    app.router.add_post("/api/run-broker/skillhub/events", handle_skillhub_event)
    app.router.add_get("/api/run-broker/jobs", handle_list_jobs)
    app.router.add_post("/api/run-broker/jobs", handle_create_job)
    app.router.add_get("/api/run-broker/jobs/{job_id}/plan", handle_plan_job)
    app.router.add_get("/api/run-broker/jobs/{job_id}", handle_get_job)
    app.router.add_patch("/api/run-broker/jobs/{job_id}", handle_update_job)
    app.router.add_delete("/api/run-broker/jobs/{job_id}", handle_delete_job)
    app.router.add_post("/api/run-broker/jobs/{job_id}/pause", handle_pause_job)
    app.router.add_post("/api/run-broker/jobs/{job_id}/resume", handle_resume_job)
    app.router.add_post("/api/run-broker/jobs/{job_id}/run", handle_run_job)
    # Hermes Console M1a read-only fleet endpoints live in their own module
    # (console_api) — registered here, implemented there (god-file split rule).
    from . import console_api as _console_api

    _console_api.register_console_routes(app)
    # push-card fill loop (SPEC push-card-fill-loop): notify-card ingress +
    # status query live in their own module (god-file split rule).
    from . import push_card_routes as _push_card_routes

    _push_card_routes.register_push_card_routes(app)
    # push-custom-message (SPEC push-custom-message): POST /api/run-broker/push —
    # a pure Feishu send bypass (no agent run), in its own module (god-file split).
    from . import push_message_routes as _push_message_routes

    _push_message_routes.register_push_message_routes(app)
    return app
