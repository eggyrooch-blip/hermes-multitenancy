"""Hermes enterprise capabilities for a Cowork run.

This module deliberately owns no Project, Thread, Run, Workspace, Library,
Checkpoint, Memory, or Agent loop.  It only resolves an authorized Expert into
a deterministic Agent capability mapping and issues one-use, run-scoped
handles for enterprise tool calls.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from . import expert_overlay
from .security_audit import append_security_event


logger = logging.getLogger(__name__)


class CoworkEnterpriseError(RuntimeError):
    status = 400
    code = "COWORK_ENTERPRISE_INVALID"


class CoworkExpertNotFound(CoworkEnterpriseError):
    status = 404
    code = "COWORK_EXPERT_NOT_FOUND"


class CoworkExpertConflict(CoworkEnterpriseError):
    status = 409
    code = "COWORK_EXPERT_CONFLICT"


class CoworkEnterpriseUnavailable(CoworkEnterpriseError):
    status = 503
    code = "COWORK_ENTERPRISE_UNAVAILABLE"


class CoworkCapabilityDenied(CoworkEnterpriseError):
    status = 403
    code = "COWORK_CAPABILITY_DENIED"


class CoworkCapabilityRateLimited(CoworkEnterpriseError):
    status = 429
    code = "COWORK_CAPABILITY_LIMIT"


@dataclass(frozen=True)
class ExpertCapabilityMapping:
    expert_id: str | None
    expert_version: str | None
    agent_scope: str
    skill_policy_id: str | None
    skills: tuple[str, ...]
    hermes_tool_scopes: tuple[str, ...]
    source_fingerprint: str

    def public_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["skills"] = list(self.skills)
        row["hermes_tool_scopes"] = list(self.hermes_tool_scopes)
        return row


def _clean_identifier(value: Any, field: str) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > 256 or any(ord(ch) < 33 for ch in clean):
        raise CoworkEnterpriseError(f"{field} is invalid")
    return clean


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise CoworkEnterpriseUnavailable(f"{field} must be a list")
    rows = tuple(sorted({_clean_identifier(item, field) for item in value}))
    return rows


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_expert_mapping(
    profile_home: Path,
    expert_id: str | None,
    *,
    department_ids: list[str] | None = None,
    actor_subject: str | None = None,
) -> ExpertCapabilityMapping:
    """Resolve one authorized Expert without starting Hermes AIAgent/Harness."""
    eid = str(expert_id or "").strip()
    if not eid:
        payload = {
            "expert_id": None,
            "expert_version": None,
            "agent_scope": "lead_agent",
            "skill_policy_id": None,
            "skills": [],
            "hermes_tool_scopes": [],
        }
        return ExpertCapabilityMapping(
            expert_id=None,
            expert_version=None,
            agent_scope="lead_agent",
            skill_policy_id=None,
            skills=(),
            hermes_tool_scopes=(),
            source_fingerprint=_fingerprint(payload),
        )

    if department_ids is None:
        department_ids = expert_overlay.resolve_caller_departments(
            Path(profile_home), open_id=actor_subject
        )
    matches: list[ExpertCapabilityMapping] = []
    visible = expert_overlay.authorized_expert_records(
        Path(profile_home), eid, department_ids=department_ids,
        include_inactive=True, strict=True,
    )
    for manifest, expert in visible:
        if not expert_overlay._manifest_is_active(manifest) or str(expert.get("status") or "active").strip().lower() != "active":
            continue
        version = str(
            expert.get("version")
            or manifest.get("release_version")
            or manifest.get("version")
            or ""
        ).strip()
        if any(key != "agent_scope" and key.endswith("_agent_scope") for key in expert):
            raise CoworkEnterpriseUnavailable("Expert has a product-specific agent scope")
        agent_scope = str(expert.get("agent_scope") or "").strip()
        if not version or not agent_scope:
            raise CoworkEnterpriseUnavailable("Expert has no immutable Agent mapping")
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", agent_scope) is None:
            raise CoworkEnterpriseUnavailable("agent_scope is invalid")
        skills = _string_list(expert.get("skills"), "skills")
        tool_scopes = _string_list(expert.get("hermes_tool_scopes"), "hermes_tool_scopes")
        skill_policy_id = str(expert.get("skill_policy_id") or "").strip()
        if not skill_policy_id:
            skill_policy_id = "expert:" + _fingerprint({"expert_id": eid, "version": version, "skills": skills})[:24]
        source = {
            "expert_id": eid,
            "expert_version": version,
            "agent_scope": agent_scope,
            "skill_policy_id": skill_policy_id,
            "skills": skills,
            "hermes_tool_scopes": tool_scopes,
            "plugin_id": str(manifest.get("plugin_id") or ""),
        }
        matches.append(
            ExpertCapabilityMapping(
                expert_id=eid,
                expert_version=version,
                agent_scope=agent_scope,
                skill_policy_id=skill_policy_id,
                skills=skills,
                hermes_tool_scopes=tool_scopes,
                source_fingerprint=_fingerprint(source),
            )
        )

    if len(matches) > 1:
        raise CoworkExpertConflict("Expert has multiple active mappings")
    if matches:
        return matches[0]
    if visible:
        raise CoworkCapabilityDenied("Expert is disabled")
    raise CoworkExpertNotFound("Expert is unavailable")


@dataclass(frozen=True)
class _Capability:
    token: str
    profile_name: str
    actor_subject: str
    thread_id: str
    run_id: str
    tool: str
    scope: str
    credential_subject: str
    expires_monotonic: float
    expires_at: int


class CoworkCapabilityRegistry:
    """In-process one-use capability handles; raw credentials never enter it.

    ponytail: single-process registry; move to an atomic shared store only when
    the Run Broker is deployed with multiple writers.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_records: int = 10_000, max_per_run: int = 64) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, _Capability] = {}
        self._max_records = max_records
        self._max_per_run = max_per_run

    def issue(
        self,
        *,
        profile_name: str,
        actor_subject: str,
        thread_id: str,
        run_id: str,
        tool: str,
        scope: str,
        credential_subject: str,
        allowed_scopes: tuple[str, ...],
        ttl_seconds: int = 60,
    ) -> tuple[str, int]:
        values = {
            field: _clean_identifier(value, field)
            for field, value in {
                "profile_name": profile_name,
                "actor_subject": actor_subject,
                "thread_id": thread_id,
                "run_id": run_id,
                "tool": tool,
                "scope": scope,
                "credential_subject": credential_subject,
            }.items()
        }
        if values["actor_subject"] != values["credential_subject"]:
            raise CoworkCapabilityDenied("credential subject does not match actor")
        if values["scope"] not in allowed_scopes:
            raise CoworkCapabilityDenied("tool scope is not authorized")
        if values["tool"].split(".", 1)[0].split(":", 1)[0] != values["scope"].split(".", 1)[0].split(":", 1)[0]:
            raise CoworkCapabilityDenied("tool scope is not authorized")
        if not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 300:
            raise CoworkEnterpriseError("ttl_seconds must be between 1 and 300")
        token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + ttl_seconds
        record = _Capability(
            token=token,
            expires_monotonic=self._clock() + ttl_seconds,
            expires_at=expires_at,
            **values,
        )
        with self._lock:
            self._prune_locked()
            if len(self._records) >= self._max_records or sum(
                record.profile_name == values["profile_name"]
                and record.actor_subject == values["actor_subject"]
                and record.run_id == values["run_id"]
                for record in self._records.values()
            ) >= self._max_per_run:
                raise CoworkCapabilityRateLimited("capability limit reached")
            self._records[token] = record
        return token, expires_at

    def authorize(self, token: str, **assertions: str) -> dict[str, str]:
        presented = str(token or "").strip()
        if not presented:
            raise CoworkCapabilityDenied("capability is missing")
        with self._lock:
            self._prune_locked()
            record = self._records.get(presented)
            if record is None or not hmac.compare_digest(record.token, presented):
                raise CoworkCapabilityDenied("capability is invalid or expired")
            # Every presentation consumes the handle, including a mismatched
            # assertion, so a failed probe cannot be retried as an oracle.
            self._records.pop(presented, None)
            try:
                matched = all(
                    _clean_identifier(assertions.get(field), field) == getattr(record, field)
                    for field in (
                        "profile_name", "actor_subject", "thread_id", "run_id",
                        "tool", "scope", "credential_subject",
                    )
                )
            except CoworkEnterpriseError:
                matched = False
            if not matched:
                raise CoworkCapabilityDenied("capability assertion mismatch")
        return {"tool": record.tool, "scope": record.scope, "run_id": record.run_id}

    def revoke_run(self, *, profile_name: str, actor_subject: str, run_id: str) -> int:
        profile = _clean_identifier(profile_name, "profile_name")
        actor = _clean_identifier(actor_subject, "actor_subject")
        run = _clean_identifier(run_id, "run_id")
        with self._lock:
            doomed = [
                token
                for token, record in self._records.items()
                if record.profile_name == profile
                and record.actor_subject == actor
                and record.run_id == run
            ]
            for token in doomed:
                self._records.pop(token, None)
        return len(doomed)

    def _prune_locked(self) -> None:
        now = self._clock()
        for token in [key for key, record in self._records.items() if record.expires_monotonic <= now]:
            self._records.pop(token, None)


_CAPABILITIES = CoworkCapabilityRegistry()


def register_routes(
    app: Any,
    *,
    authorize: Callable[[Any], bool],
    owner_tenant: Callable[..., tuple[str, str]],
    profile_home: Callable[[str], Path],
    credential_bound: Callable[[Any, str, str, str], bool] = lambda *_args: False,
    registry: CoworkCapabilityRegistry = _CAPABILITIES,
) -> None:
    """Register internal service-to-service routes; none dispatch an Agent."""
    from aiohttp import web

    def error_response(exc: CoworkEnterpriseError):
        return web.json_response({"error": str(exc), "code": exc.code}, status=exc.status)

    def audit(*, decision: str, profile: str = "", actor: str = "", reason: str = "", run_id: str = "", expert_id: str = "") -> None:
        append_security_event(
            event_type="cowork.enterprise.capability",
            decision=decision,
            profile=profile,
            open_id=actor,
            reason=reason,
            run_id=run_id,
            expert_id=expert_id,
        )

    def safe_id(value: Any) -> str:
        clean = str(value or "").strip()
        return clean if clean and len(clean) <= 128 and all(ch.isalnum() or ch in "._:-" for ch in clean) else ""

    async def body_and_identity(request, *, require_write: bool = False):
        body = await request.json()
        if not isinstance(body, dict):
            raise CoworkEnterpriseError("request body must be an object")
        profile_name, actor_subject = owner_tenant(
            request, body, require_write=require_write
        )
        return body, profile_name, actor_subject

    async def resolve(request):
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body: dict[str, Any] = {}
        profile_name = actor = ""
        try:
            body, profile_name, actor = await body_and_identity(request)
            if set(body) - {"expert_id", "profile_name", "user_key"}:
                raise CoworkEnterpriseError("unsupported Expert resolve field")
            mapping = resolve_expert_mapping(
                profile_home(profile_name), body.get("expert_id"), actor_subject=actor
            )
            audit(decision="granted", profile=profile_name, actor=actor, expert_id=mapping.expert_id or "")
            return web.json_response(mapping.public_dict())
        except CoworkEnterpriseError as exc:
            audit(decision="denied", profile=profile_name, actor=actor, reason=exc.code, expert_id=safe_id(body.get("expert_id")))
            return error_response(exc)
        except PermissionError:
            audit(decision="denied", profile=profile_name, actor=actor, reason="COWORK_PERMISSION_DENIED")
            return web.json_response({"error": "forbidden"}, status=403)
        except Exception:
            logger.exception("Cowork Expert resolution failed")
            audit(decision="denied", profile=profile_name, actor=actor, reason="COWORK_ENTERPRISE_UNAVAILABLE")
            return error_response(CoworkEnterpriseUnavailable("Expert resolution unavailable"))

    async def issue(request):
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body: dict[str, Any] = {}
        profile_name = actor = ""
        try:
            body, profile_name, actor = await body_and_identity(request, require_write=True)
            allowed = {
                "expert_id", "thread_id", "run_id", "tool", "scope",
                "credential_subject", "ttl_seconds", "profile_name", "user_key",
                "expected_source_fingerprint",
            }
            if set(body) - allowed:
                raise CoworkEnterpriseError("unsupported capability field")
            mapping = resolve_expert_mapping(
                profile_home(profile_name), body.get("expert_id"), actor_subject=actor
            )
            expected_fingerprint = str(body.get("expected_source_fingerprint") or "").strip()
            if expected_fingerprint and not hmac.compare_digest(expected_fingerprint, mapping.source_fingerprint):
                raise CoworkExpertConflict("Expert mapping changed")
            run_id = _clean_identifier(body.get("run_id"), "run_id")
            if not credential_bound(request, profile_name, actor, run_id):
                raise CoworkEnterpriseUnavailable("Actor-bound credential is unavailable")
            token, expires_at = registry.issue(
                profile_name=profile_name,
                actor_subject=actor,
                thread_id=body.get("thread_id"),
                run_id=run_id,
                tool=body.get("tool"),
                scope=body.get("scope"),
                credential_subject=body.get("credential_subject"),
                allowed_scopes=mapping.hermes_tool_scopes,
                ttl_seconds=body.get("ttl_seconds", 60),
            )
            audit(decision="granted", profile=profile_name, actor=actor, run_id=str(body.get("run_id") or ""), expert_id=str(body.get("expert_id") or ""))
            return web.json_response({"capability": token, "expires_at": expires_at}, status=201)
        except CoworkEnterpriseError as exc:
            audit(decision="denied", profile=profile_name, actor=actor, reason=exc.code, run_id=safe_id(body.get("run_id")), expert_id=safe_id(body.get("expert_id")))
            return error_response(exc)
        except PermissionError:
            audit(decision="denied", profile=profile_name, actor=actor, reason="COWORK_PERMISSION_DENIED", run_id=safe_id(body.get("run_id")), expert_id=safe_id(body.get("expert_id")))
            return web.json_response({"error": "forbidden"}, status=403)
        except Exception:
            logger.exception("Cowork capability issue failed")
            audit(decision="denied", profile=profile_name, actor=actor, reason="COWORK_ENTERPRISE_UNAVAILABLE", run_id=safe_id(body.get("run_id")), expert_id=safe_id(body.get("expert_id")))
            return error_response(CoworkEnterpriseUnavailable("Capability service unavailable"))

    async def check(request):
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body: dict[str, Any] = {}
        profile_name = actor = ""
        try:
            body, profile_name, actor = await body_and_identity(request, require_write=True)
            allowed = {
                "capability", "thread_id", "run_id", "tool", "scope",
                "credential_subject", "profile_name", "user_key",
            }
            if set(body) - allowed:
                raise CoworkEnterpriseError("unsupported capability assertion")
            result = registry.authorize(
                body.get("capability"),
                profile_name=profile_name,
                actor_subject=actor,
                thread_id=body.get("thread_id"),
                run_id=body.get("run_id"),
                tool=body.get("tool"),
                scope=body.get("scope"),
                credential_subject=body.get("credential_subject"),
            )
            audit(decision="granted", profile=profile_name, actor=actor, run_id=result["run_id"])
            return web.json_response({"ok": True, **result})
        except CoworkEnterpriseError as exc:
            audit(decision="denied", profile=profile_name, actor=actor, reason=exc.code, run_id=safe_id(body.get("run_id")))
            return error_response(exc)
        except PermissionError:
            audit(decision="denied", profile=profile_name, actor=actor, reason="COWORK_PERMISSION_DENIED", run_id=safe_id(body.get("run_id")))
            return web.json_response({"error": "forbidden"}, status=403)
        except Exception:
            logger.exception("Cowork capability authorization failed")
            audit(decision="denied", profile=profile_name, actor=actor, reason="COWORK_ENTERPRISE_UNAVAILABLE", run_id=safe_id(body.get("run_id")))
            return error_response(CoworkEnterpriseUnavailable("Capability service unavailable"))

    async def revoke(request):
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            profile_name, actor = owner_tenant(request, require_write=True)
            revoked = registry.revoke_run(
                profile_name=profile_name,
                actor_subject=actor,
                run_id=request.match_info.get("run_id"),
            )
            audit(decision="revoked", profile=profile_name, actor=actor, run_id=str(request.match_info.get("run_id") or ""))
            return web.json_response({"ok": True, "revoked": revoked})
        except CoworkEnterpriseError as exc:
            audit(decision="denied", reason=exc.code, run_id=safe_id(request.match_info.get("run_id")))
            return error_response(exc)
        except PermissionError:
            audit(decision="denied", reason="COWORK_PERMISSION_DENIED", run_id=safe_id(request.match_info.get("run_id")))
            return web.json_response({"error": "forbidden"}, status=403)
        except Exception:
            logger.exception("Cowork capability revoke failed")
            audit(decision="denied", reason="COWORK_ENTERPRISE_UNAVAILABLE", run_id=safe_id(request.match_info.get("run_id")))
            return error_response(CoworkEnterpriseUnavailable("Capability service unavailable"))

    app.router.add_post("/api/run-broker/internal/cowork/expert-resolve", resolve)
    app.router.add_post("/api/run-broker/internal/cowork/capabilities", issue)
    app.router.add_post("/api/run-broker/internal/cowork/capabilities/authorize", check)
    app.router.add_delete("/api/run-broker/internal/cowork/runs/{run_id}/capabilities", revoke)
