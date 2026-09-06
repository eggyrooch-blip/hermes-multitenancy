"""Connector Registry — list / get / status / auth / requirements entry point.

This is the control plane. It calls the existing ``credential_hub`` readers to
get live status, then enriches each into a ``ConnectorStatus`` carrying the
防串号 scope fields. The legacy ``credential_hub.collect_credential_statuses``
delegates here (via a lazy import) and maps back to ``CredentialRow`` with
``compat.to_credential_row`` — byte-identical output, registry as the core.

Caching: an OPTIONAL short-TTL cache keyed by ``(profile, open_id, scope)``.
It defaults OFF so the legacy path is never made staler than before; the
``/connectors`` endpoint may opt in. The key ALWAYS includes profile + open_id +
scope so a cached entry can never leak across profiles (asserted by
``test_connector_status_cache_isolation``).
"""
from __future__ import annotations

import os
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from . import builtin, compat
from .drivers import driver_for
from .models import AuthAction, ConnectorDefinition, ConnectorStatus
from .requirements import (
    ConnectorRequirementSet,
    SkillRequirementInput,
    detect_requirements as _detect_requirements,
)


# --- definitions ------------------------------------------------------------

logger = logging.getLogger(__name__)


def list_definitions() -> list[ConnectorDefinition]:
    """All built-in connector definitions, in display order."""
    return [builtin.BUILTIN_CONNECTORS[cid] for cid in builtin.CONNECTOR_ORDER]


def get_connector(connector_id: str) -> Optional[ConnectorDefinition]:
    """Resolve a connector definition by id or compat id."""
    return builtin.get_definition(connector_id)


# --- status -----------------------------------------------------------------


def _enrich_rows(rows, *, profile: Optional[str]) -> list[ConnectorStatus]:
    out: list[ConnectorStatus] = []
    for row in rows:
        definition = builtin.get_definition(row.id)
        if definition is None:
            # An unknown legacy id (should not happen for the five built-ins).
            # Surface it rather than dropping it, with a permissive default def.
            out.append(
                ConnectorStatus(
                    id=row.id, title=row.title, provider=row.provider,
                    installed=row.installed, status=row.status,  # type: ignore[arg-type]
                    expires_at=row.expires_at, account_hint=row.account_hint,
                    default_identity=row.default_identity, detail=row.detail,
                    required_by=list(row.required_by),
                    action=compat.AuthAction.from_dict(row.action),
                    environments=dict(getattr(row, "environments", {}) or {}),
                    profile=profile, scope="profile",
                )
            )
            continue
        out.append(compat.enrich(row, definition, profile=profile))
    return out


def collect_connector_statuses(
    *,
    profile_name: str,
    open_id: str,
    shared_home: Optional[Path] = None,
    home_dir: Optional[Path] = None,
    use_cache: bool = False,
) -> list[ConnectorStatus]:
    """Live status for every built-in connector for one (profile, open_id).

    Calls the existing ``credential_hub`` reader path (which shells out to
    meegle/kep-auth as needed) and enriches the result. ``use_cache`` is OFF by
    default to preserve the legacy path's freshness.
    """
    if use_cache:
        cached = _cache_get(profile_name, open_id)
        if cached is not None:
            return cached

    # Lazy import: avoids an import-time cycle (credential_hub must be importable
    # without importing the registry; the registry imports credential_hub).
    from .. import credential_hub

    rows = credential_hub._collect_credential_rows(
        profile_name=profile_name,
        open_id=open_id,
        shared_home=shared_home,
        home_dir=home_dir,
    )
    statuses = _enrich_rows(rows, profile=profile_name)
    from .. import feishu_uat_auth
    from ..github_mcp_connector import status as github_status

    try:
        shared = Path(shared_home) if shared_home else feishu_uat_auth.resolve_shared_home()
        statuses.append(github_status(shared, profile_name, open_id))
    except Exception as exc:
        logger.warning("GitHub connector status unavailable (%s)", type(exc).__name__)
        statuses.append(ConnectorStatus(
            id="github-mcp",
            title="GitHub",
            provider="github",
            installed=False,
            status="error",
            detail="GitHub Connector 状态暂不可用",
            action=AuthAction(kind="manual", label="重试"),
            profile=profile_name,
            scope="profile",
            acting_identity="user",
            credential_owner=profile_name,
            runtime_policy_owner="run_broker",
            kind="external",
        ))

    # Always refresh the cache after a live compute — INCLUDING a use_cache=False
    # (fresh) read. A fresh read bypasses the cache for its own result, but if it did
    # not also overwrite the cached entry, a pre-auth "needs_auth" could survive the TTL
    # and the next default /connectors call would regress the panel to stale data right
    # after a successful login. Writing here keeps cached reads consistent with the last
    # live read.
    _cache_put(profile_name, open_id, statuses)
    return statuses


def list_connectors(
    profile: str,
    user_context: Optional[str] = None,
    *,
    shared_home: Optional[Path] = None,
    home_dir: Optional[Path] = None,
    use_cache: bool = False,
) -> list[ConnectorStatus]:
    """Plan-named alias for ``collect_connector_statuses``."""
    return collect_connector_statuses(
        profile_name=profile,
        open_id=user_context or "",
        shared_home=shared_home,
        home_dir=home_dir,
        use_cache=use_cache,
    )


# --- requirements -----------------------------------------------------------


def detect_requirements(skills: list[SkillRequirementInput]) -> ConnectorRequirementSet:
    """Source-of-truth connector requirement detection (Phase 1 == legacy)."""
    return _detect_requirements(skills)


# --- auth lifecycle (delegates to drivers → auth_flows → credential_hub_auth) --


def start_auth(connector_id: str, *, profile_dir: Path, profile_name: str,
               shared_home: Path, public_origin: Optional[str] = None):
    definition = _require(connector_id)
    return driver_for(definition).start_auth(
        profile_dir=profile_dir, profile_name=profile_name,
        shared_home=shared_home, public_origin=public_origin,
    )


def poll_auth(connector_id: str, *, profile_dir: Path, profile_name: str,
              shared_home: Path, qrcode_id: Optional[str] = None, timeout_ms: int = 15000):
    definition = _require(connector_id)
    return driver_for(definition).poll_auth(
        profile_dir=profile_dir, profile_name=profile_name, shared_home=shared_home,
        qrcode_id=qrcode_id, timeout_ms=timeout_ms,
    )


def complete_auth(connector_id: str, *, session_id: str, query: str) -> str:
    definition = _require(connector_id)
    return driver_for(definition).complete_auth(session_id=session_id, query=query)


def _require(connector_id: str) -> ConnectorDefinition:
    definition = builtin.get_definition(connector_id)
    if definition is None:
        raise KeyError(f"unknown connector id {connector_id!r}")
    return definition


# --- optional short-TTL cache (profile/user/scope isolated) ------------------

# 15s: connector auth status changes rarely (login/logout/expiry), so a brief cache
# makes repeat panel loads instant while staying fresh enough; the only freshness-
# critical path (the post-auth poll) bypasses the cache with use_cache=False. Only the
# /connectors endpoint opts into the cache, so this default affects nothing else.
# Override with HERMES_CONNECTOR_STATUS_CACHE_TTL_MS.
_CACHE_TTL_MS_DEFAULT = 15000
_cache_lock = threading.Lock()
_cache: dict[tuple[str, str, str], tuple[float, list[ConnectorStatus]]] = {}


def _cache_ttl_ms() -> int:
    raw = os.environ.get("HERMES_CONNECTOR_STATUS_CACHE_TTL_MS", "").strip()
    if raw.isdigit():
        return int(raw)
    return _CACHE_TTL_MS_DEFAULT


def _cache_key(profile_name: str, open_id: str, scope: str = "profile") -> tuple[str, str, str]:
    # The key MUST include profile + open_id + scope so a cached status can never
    # be served across profiles/users — the core 防串号 invariant.
    return (str(profile_name), str(open_id), str(scope))


def _cache_get(profile_name: str, open_id: str) -> Optional[list[ConnectorStatus]]:
    key = _cache_key(profile_name, open_id)
    now = time.time() * 1000
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        ts, value = entry
        if now - ts > _cache_ttl_ms():
            _cache.pop(key, None)
            return None
        # Return a fresh list so a caller can't mutate the cached sequence
        # (e.g. sort/append) and corrupt what later same-profile callers see.
        return list(value)


def _cache_put(profile_name: str, open_id: str, statuses: list[ConnectorStatus]) -> None:
    key = _cache_key(profile_name, open_id)
    with _cache_lock:
        # Store a copy so a post-put mutation of the caller's list can't leak in.
        _cache[key] = (time.time() * 1000, list(statuses))


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
