"""Trusted payer resolution and Hermes LiteLLM runtime credentials.

LiteLLM account/team/key administration belongs to AI Gateway.  This module
only resolves the canonical Hermes payer, keeps the one-time runtime key in
the existing encrypted credential vault, and exposes a short-lived key to the
model runtime after RunBroker admission.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import replace
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Optional

from .billing_credentials import (
    BillingUnavailable,
    BillingCredentialManager,
    BillingGatewayClient,
    BillingIdentity,
    _EMPLOYEE_ID_RE,
    _GatewayError,
    _ResolvedPayer,
    _allowed_billing_endpoint,
    _is_budget_exceeded_text,
)
from .billing_employee_key import CREDENTIAL_SOURCE, EmployeeKeyClient, store_binding
from .credentials import CredentialStore
from .routing import DEFAULT_DB_PATH, RoutingTable
from .run_broker import RunRejected
from .run_models import RunRequest
from .token_usage_uploader import make_owner_resolver


_TRUE = frozenset({"1", "true", "yes", "on"})
_RUNTIME_KEY_ENV = "HERMES_LITELLM_RUNTIME_API_KEY"
_RUNTIME_BASE_URL_ENV = "HERMES_LITELLM_RUNTIME_BASE_URL"
_RUNTIME_EMPLOYEE_ENV = "HERMES_LITELLM_RUNTIME_EMPLOYEE_ID"

_RESERVED_METADATA = frozenset({
    "litellm_billing_enforced",
    "litellm_billing_user_id",
    "litellm_billing_employee_user_id",
    "litellm_billing_email",
    "litellm_billing_profile_name",
    "litellm_billing_team_id",
    "litellm_billing_team_alias",
    "litellm_billing_key_id",
    "litellm_billing_credential_version",
    "litellm_billing_expires_at",
    "litellm_billing_base_url",
})


def _is_canonical_employee_id(user_id: str) -> bool:
    """The one shape test for "this is a LiteLLM billing subject."

    ``ou_*`` is a Feishu open_id — a chat identity, never a billing subject —
    and the employee-id regex alone does not exclude it (underscore and
    digits are both legal id characters). Every place that turns a raw string
    into a payer's ``employee_id`` MUST run it through this, not just the ones
    that happen to go through :meth:`BillingIdentityPreparer._employee_row`:
    a query that filters routing rows on kind/active/provenance but skips the
    shape check would accept a synthetic ``ou_*`` row that the live request
    path would refuse to ever resolve as a payer.
    """
    value = str(user_id or "")
    return bool(_EMPLOYEE_ID_RE.fullmatch(value)) and not value.startswith("ou_")


def canonical_sync_open_id(routing: Any, sender: Any) -> str:
    """Resolve a Feishu user alias through one active org-sync-owned row."""
    value = str(sender or "").strip()
    if not value:
        return ""
    if value.startswith("ou_"):
        return value
    lookup = getattr(routing, "lookup_by_user_id", None)
    try:
        row = lookup(value) if callable(lookup) else None
    except Exception:
        return ""
    open_id = str(getattr(row, "open_id", "") or "").strip() if row else ""
    if (
        not open_id.startswith("ou_")
        or str(getattr(row, "kind", "") or "") != "user"
        or str(getattr(row, "provenance", "") or "") != "sync"
        or not bool(getattr(row, "active", True))
    ):
        return ""
    return open_id


class BillingIdentityStore:
    """Durable non-secret binding and irreversible migration state."""

    _COLUMNS = {
        "profile_name": "TEXT NOT NULL DEFAULT ''",
        "team_id": "TEXT NOT NULL DEFAULT ''",
        "team_alias": "TEXT NOT NULL DEFAULT ''",
        "key_id": "TEXT NOT NULL DEFAULT ''",
        "credential_version": "INTEGER NOT NULL DEFAULT 0",
        "expires_at": "INTEGER NOT NULL DEFAULT 0",
        "migration_state": "TEXT NOT NULL DEFAULT 'legacy'",
    }

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = str(db_path or DEFAULT_DB_PATH)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS multitenancy_billing_identities (
                employee_user_id TEXT PRIMARY KEY NOT NULL,
                profile_name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL,
                litellm_user_id TEXT NOT NULL,
                team_id TEXT NOT NULL DEFAULT '',
                team_alias TEXT NOT NULL DEFAULT '',
                key_id TEXT NOT NULL DEFAULT '',
                credential_version INTEGER NOT NULL DEFAULT 0,
                expires_at INTEGER NOT NULL DEFAULT 0,
                migration_state TEXT NOT NULL DEFAULT 'legacy',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        existing = {
            str(row["name"])
            for row in self._conn.execute(
                "PRAGMA table_info(multitenancy_billing_identities)"
            ).fetchall()
        }
        for name, declaration in self._COLUMNS.items():
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE multitenancy_billing_identities "
                    f"ADD COLUMN {name} {declaration}"
                )
        self._conn.commit()

    def get(self, employee_user_id: str) -> Optional[BillingIdentity]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT employee_user_id, profile_name, email, litellm_user_id,
                       team_id, team_alias, key_id, credential_version,
                       expires_at, migration_state
                FROM multitenancy_billing_identities
                WHERE employee_user_id = ?
                """,
                (employee_user_id,),
            ).fetchone()
        return BillingIdentity(**dict(row)) if row is not None else None

    def get_by_profile(self, profile_name: str) -> Optional[BillingIdentity]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT employee_user_id, profile_name, email, litellm_user_id,
                       team_id, team_alias, key_id, credential_version,
                       expires_at, migration_state
                FROM multitenancy_billing_identities
                WHERE profile_name = ? AND migration_state = 'enforced'
                """,
                (profile_name,),
            ).fetchall()
        if len(rows) > 1:
            raise RunRejected("billing payer profile is ambiguous")
        return BillingIdentity(**dict(rows[0])) if rows else None

    def put(self, identity: BillingIdentity) -> None:
        if identity.migration_state not in {"legacy", "enforced"}:
            raise ValueError("invalid billing migration state")
        current = self.get(identity.employee_user_id)
        if current is not None and current.migration_state == "enforced":
            identity = replace(identity, migration_state="enforced")
        now = int(time.time() * 1000)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO multitenancy_billing_identities (
                    employee_user_id, profile_name, email, litellm_user_id,
                    team_id, team_alias, key_id, credential_version,
                    expires_at, migration_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(employee_user_id) DO UPDATE SET
                    profile_name = excluded.profile_name,
                    email = excluded.email,
                    litellm_user_id = excluded.litellm_user_id,
                    team_id = excluded.team_id,
                    team_alias = excluded.team_alias,
                    key_id = excluded.key_id,
                    credential_version = excluded.credential_version,
                    expires_at = excluded.expires_at,
                    migration_state = CASE
                        WHEN multitenancy_billing_identities.migration_state = 'enforced'
                        THEN 'enforced' ELSE excluded.migration_state END,
                    updated_at = excluded.updated_at
                """,
                (
                    identity.employee_user_id,
                    identity.profile_name,
                    identity.email,
                    identity.litellm_user_id,
                    identity.team_id,
                    identity.team_alias,
                    identity.key_id,
                    int(identity.credential_version),
                    int(identity.expires_at),
                    identity.migration_state,
                    now,
                    now,
                ),
            )
            self._conn.commit()


class BillingIdentityPreparer:
    def __init__(
        self,
        *,
        routing: Any,
        store: BillingIdentityStore,
        credentials: BillingCredentialManager,
    ) -> None:
        self._routing = routing
        self._store = store
        self._credentials = credentials
        self._routing_lock = threading.Lock()
        self._resolve_owner = make_owner_resolver(
            self._group_owner,
            self._profile_owner,
        )

    def prepare(self, request: RunRequest) -> RunRequest:
        metadata = _clean_metadata(request.metadata)
        payer = self._payer(request, metadata)
        if payer is None:
            profile_binding = self._store.get_by_profile(request.profile_name)
            owner = self._employee_row(self._profile_owner(request.profile_name))
            owner_binding = self._store.get(owner.user_id) if owner is not None else None
            if (
                _billing_enabled()
                or profile_binding is not None
                or (
                    owner_binding is not None
                    and owner_binding.migration_state == "enforced"
                )
            ):
                # NOT degraded on purpose. ``_payer()`` returns None for several
                # different reasons — sender/profile mismatch, a non-sync route,
                # no routing row — and they are not the same kind of problem.
                # The mismatch and non-sync cases are identity SAFETY: letting
                # them through unattributed would quietly admit a run we
                # currently refuse to attribute to anybody. One None cannot tell
                # them apart, so this stays closed; the degrade lives where the
                # reason is unambiguous (credential could not be obtained).
                raise RunRejected("employee billing identity could not be resolved")
            return request if metadata == request.metadata else replace(
                request, metadata=metadata
            )
        selected = _payer_selected(payer.employee_user_id)
        existing = self._store.get(payer.employee_user_id)
        if not selected and not (
            existing is not None and existing.migration_state == "enforced"
        ):
            return request if metadata == request.metadata else replace(request, metadata=metadata)
        profile_binding = self._store.get_by_profile(request.profile_name)
        if (
            profile_binding is not None
            and profile_binding.employee_user_id != payer.employee_user_id
        ):
            raise RunRejected("billing payer profile drift detected")
        email, department = _employee_org_fields(payer.employee_user_id)
        payer = replace(payer, email=email, department_alias=department)
        if existing is not None:
            if existing.profile_name and existing.profile_name != payer.profile_name:
                raise RunRejected("billing payer profile drift detected")
            if existing.email and existing.email.lower() != payer.email.lower():
                raise RunRejected("billing payer email drift detected")
        try:
            # allow_mint=False: this is the employee's own request path, and the
            # employee never triggers issuance (sunke 2026-08-06). A missing or
            # dead credential degrades below; the hourly refresh sweep is the
            # only thing that talks to the gateway about minting.
            binding = self._credentials.ensure_available(
                payer, existing, allow_mint=False
            )
        except BillingUnavailable as exc:
            # Could not OBTAIN a credential (gateway down, rejected, not yet
            # provisioned). Inconsistency raises plain RunRejected and is NOT
            # caught here — falling back on drift would bury a real defect.
            #
            # The decision belongs HERE, before the run is labelled enforced:
            # marking it enforced and failing later would just move the refusal
            # to the runtime guard, which is exactly what we are removing.
            _log_billing_degraded(
                employee_id=payer.employee_user_id,
                profile_name=payer.profile_name,
                reason=type(exc).__name__ + ":" + str(exc),
            )
            return request if metadata == request.metadata else replace(
                request, metadata=metadata
            )
        binding = replace(binding, migration_state="enforced")
        self._store.put(binding)
        metadata.update(_metadata_for_binding(binding, _billing_model_base_url()))
        return replace(request, metadata=metadata)

    def repair_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        employee_id = str(metadata.get("litellm_billing_employee_user_id") or "")
        profile_name = str(metadata.get("litellm_billing_profile_name") or "")
        email = str(metadata.get("litellm_billing_email") or "")
        existing = self._store.get(employee_id)
        if (
            existing is None
            or existing.migration_state != "enforced"
            or existing.profile_name != profile_name
            or existing.email.lower() != email.lower()
        ):
            raise RunRejected("billing repair identity is invalid")
        # Same canonical-subject guard `run_refresh` and `_employee_row` both
        # enforce before ever minting. This payer comes from a vault-stored
        # `BillingIdentityStore` row keyed on caller-supplied metadata, not
        # from the live routing table, so it never passed through either of
        # those checks — for EITHER branch below. Skipping it would let a
        # non-canonical id (an `ou_*` Feishu open_id, never a billing
        # subject) mint or renew a real key through the 401 path alone.
        if not _is_canonical_employee_id(employee_id):
            raise RunRejected("billing repair identity is invalid")
        payer = _ResolvedPayer(employee_id, profile_name, email)
        # Two credential lifecycles now share this row's namespace: the
        # legacy ensure/ack protocol and the employee-key sweep. A 401 must
        # be repaired through whichever one actually minted the dead key —
        # sending an employee_key credential through legacy `ensure` mixes
        # the protocols on one row and contradicts the sweep's timer-only
        # minting promise (README, "Keeping the cohort provisioned").
        # PRODUCTION-UNREACHABLE since `billing-runtime-never-mints`: the only
        # caller (agent_real._core on a 401) now degrades instead of repairing.
        # Both branches are kept mint-free so that re-wiring a caller cannot
        # quietly reintroduce request-path minting — the invariant this slug
        # exists to hold. See DEBT.md for the removal.
        if self._credentials.credential_source(payer) == CREDENTIAL_SOURCE:
            binding = self._repair_employee_key(payer)
        else:
            binding = self._credentials.ensure_available(
                payer, existing, force_reason="invalid_401", allow_mint=False
            )
        binding = replace(binding, migration_state="enforced")
        self._store.put(binding)
        clean = _clean_metadata(metadata)
        clean.update(_metadata_for_binding(binding, _billing_model_base_url()))
        return clean

    def _repair_employee_key(self, payer: _ResolvedPayer) -> BillingIdentity:
        """401 repair for a credential the employee-key endpoint minted.

        Re-issues through the same client the maintenance sweep uses and
        adopts it through :func:`store_binding` — the same double-write path
        the sweep uses, so a failed identity write rolls back here exactly as
        it does there rather than half-landing a repair. Its only caller,
        `repair_metadata`, has already run `payer.employee_user_id` through
        `_is_canonical_employee_id` before reaching either branch.
        """
        # No longer mints. This ran on the employee's own request path, which is
        # exactly what `billing-runtime-never-mints` removed: a 401 now marks the
        # credential invalid and serves the run unattributed, and the hourly
        # refresh sweep re-provisions it. Raising the degradable error keeps any
        # future caller on that same path instead of letting it mint here again.
        raise BillingUnavailable(
            "billing credential is dead; the refresh sweep owns re-provisioning"
        )

    def _payer(
        self, request: RunRequest, metadata: dict[str, Any]
    ) -> _ResolvedPayer | None:
        # Only the Feishu adapter owns trusted chat topology.  WebUI/ingest
        # callers can submit arbitrary metadata, so they must never be able to
        # impersonate a group by supplying ``chat_type=group`` + ``chat_id``.
        is_feishu = request.channel == "feishu"
        is_group = str(metadata.get("chat_type") or "").strip().lower() in {
            "group",
            "topic",
        }
        sender_open_id = (
            self._canonical_sender_open_id(metadata.get("sender_open_id"))
            if is_feishu
            else ""
        )
        if is_feishu and not is_group and not sender_open_id:
            return None
        with self._routing_lock:
            owner_open_id = self._resolve_owner({
                "chat_type": metadata.get("chat_type") if is_feishu else "",
                "chat_id": request.chat_id if is_feishu else "",
                "sender_open_id": sender_open_id,
                "profile": request.profile_name,
            })
            row = self._employee_row(owner_open_id)
            if is_feishu and not is_group:
                profile_row = self._employee_row(
                    self._profile_owner(request.profile_name)
                )
                if (
                    row is None
                    or profile_row is None
                    or row.user_id != profile_row.user_id
                ):
                    return None
        if row is None:
            return None
        employee_id = str(getattr(row, "user_id", "") or "").strip()
        profile_name = str(getattr(row, "profile_name", "") or "").strip()
        if not profile_name:
            profile_name = employee_id
        return _ResolvedPayer(employee_id, profile_name, "")

    def _canonical_sender_open_id(self, sender: Any) -> str:
        """Resolve production Feishu ``user_id`` aliases before billing.

        The adapter may expose an app-scoped ``ou_*`` open_id, while the
        routed event can contain only the tenant ``user_id``.  The sync-owned
        routing row is the sole authority for converting that alias; caller
        metadata and profile display names are never accepted as evidence.
        """
        return canonical_sync_open_id(self._routing, sender)

    def _group_owner(self, chat_id: str) -> Optional[str]:
        row = self._routing.lookup_by_chat_id(chat_id)
        return str(getattr(row, "owner_open_id", "") or "") or None

    def _profile_owner(self, profile_name: str) -> Optional[str]:
        row = self._routing.lookup_by_profile_name(profile_name)
        if row is None:
            return None
        return str(
            getattr(row, "owner_open_id", None)
            or getattr(row, "open_id", None)
            or ""
        ) or None

    def _employee_row(self, owner_open_id: Optional[str]) -> Any:
        if not owner_open_id:
            return None
        # Billing accepts only the org-sync-owned root.  Falling back to an
        # arbitrary active route can turn a synthetic/auto-provisioned row into
        # a real LiteLLM account, which violates the canonical payer boundary.
        getter = getattr(self._routing, "resolve_owner_root", None)
        row = getter(owner_open_id) if callable(getter) else None
        user_id = str(getattr(row, "user_id", "") or "").strip() if row else ""
        if (
            _is_canonical_employee_id(user_id)
            and getattr(row, "active", None) in {True, 1}
            and str(getattr(row, "kind", "") or "") == "user"
            and str(getattr(row, "provenance", "") or "") == "sync"
        ):
            return row
        return None


def _default_preparer() -> BillingIdentityPreparer:
    global _DEFAULT_PREPARER
    with _DEFAULT_LOCK:
        if _DEFAULT_PREPARER is None:
            db_path = (
                os.environ.get("HERMES_MULTITENANCY_DB", "").strip()
                or str(DEFAULT_DB_PATH)
            )
            timeout_raw = os.environ.get("HERMES_AI_GATEWAY_BROKER_TIMEOUT", "5")
            try:
                timeout = float(timeout_raw)
            except ValueError as exc:
                raise RunRejected("AI Gateway broker timeout is invalid") from exc
            gateway = BillingGatewayClient(
                os.environ.get("HERMES_AI_GATEWAY_BROKER_URL", ""),
                os.environ.get("HERMES_AI_GATEWAY_BROKER_TOKEN", ""),
                timeout=timeout,
            )
            _DEFAULT_PREPARER = BillingIdentityPreparer(
                routing=RoutingTable(db_path),
                store=BillingIdentityStore(db_path),
                credentials=BillingCredentialManager(
                    vault=CredentialStore(db_path),
                    gateway=gateway,
                    model_base_url=_billing_model_base_url(),
                ),
            )
        return _DEFAULT_PREPARER



_BILLING_DEGRADED_EVENT = "billing_degraded_unattributed"


def _log_billing_degraded(*, employee_id: str, profile_name: str, reason: str) -> None:
    """One greppable line per unattributed run.

    This is the whole price of dropping fail-closed: spend stops being fully
    attributable, so the gap has to be countable. ``billing_degraded_unattributed``
    is the fixed token to grep or alert on — never reword it.

    MUST NEVER RAISE (codex r2 p1-3): this runs inside prepare()'s only
    degrade branch, called after we've already decided the run should
    complete unattributed. A broken log sink (handler misconfigured, disk
    full, whatever) raising here would propagate out of prepare() and turn
    the degrade decision back into a hard failure — the exact fail-closed
    behavior sunke chose C to remove, reintroduced by the audit call meant
    to make C observable. An audit failure is never grounds to refuse the
    run it's trying to log.
    """
    line = (
        f"{_BILLING_DEGRADED_EVENT} employee={employee_id or '-'} "
        f"profile={profile_name or '-'} reason={reason}"
    )
    try:
        logging.getLogger(__name__).warning(line)
    except Exception:
        try:
            import sys

            print(line, file=sys.stderr)
        except Exception:
            pass


async def prepare_billing_request(request: RunRequest) -> RunRequest:
    """Resolve the payer before idempotency is consumed; never trust caller metadata."""
    import asyncio
    from . import router

    preparer = _default_preparer()
    routing = router._get_routing_table() if request.channel == "feishu" else None
    if routing is not None and routing is not preparer._routing:
        preparer = BillingIdentityPreparer(
            routing=routing,
            store=preparer._store,
            credentials=preparer._credentials,
        )
    return await asyncio.to_thread(preparer.prepare, request)


def runtime_env_for_billing_metadata(metadata: dict[str, Any]) -> dict[str, str]:
    """Decrypt one enforced payer key for one child run only."""
    if metadata.get("litellm_billing_enforced") is not True:
        return {}
    key = _default_preparer()._credentials.runtime_api_key(metadata)
    return {
        _RUNTIME_KEY_ENV: key,
        _RUNTIME_BASE_URL_ENV: str(metadata.get("litellm_billing_base_url") or ""),
        _RUNTIME_EMPLOYEE_ENV: str(
            metadata.get("litellm_billing_employee_user_id") or ""
        ),
    }


def billing_runtime_from_environment() -> dict[str, str]:
    """Consume the per-run key before model-visible tools can inspect env."""
    api_key = os.environ.pop(_RUNTIME_KEY_ENV, "").strip()
    base_url = os.environ.pop(_RUNTIME_BASE_URL_ENV, "").strip()
    employee_id = os.environ.pop(_RUNTIME_EMPLOYEE_ENV, "").strip()
    if not (api_key or base_url or employee_id):
        return {}
    if not api_key or not employee_id or not _allowed_billing_endpoint(base_url, base_url):
        raise RuntimeError("Billing runtime credential is incomplete")
    return {"api_key": api_key, "base_url": base_url, "employee_id": employee_id}


def billing_runtime_for_image_prep(metadata: dict[str, Any]) -> dict[str, str]:
    if metadata.get("litellm_billing_enforced") is not True:
        return {}
    return {
        "api_key": _default_preparer()._credentials.runtime_api_key(metadata),
        "base_url": str(metadata.get("litellm_billing_base_url") or ""),
    }


def mark_billing_credential_invalid(metadata: dict[str, Any]) -> None:
    if metadata.get("litellm_billing_enforced") is True:
        _default_preparer()._credentials.mark_invalid(metadata)


def degrade_billing_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Strip billing attribution so a retry runs on the shared key, unbilled.

    The 401 answer since `billing-runtime-never-mints`: a dead credential used
    to be re-minted on the employee's own request path, which is the one thing
    the refresh sweep exists to prevent. Now the run keeps going without
    attribution and the sweep re-provisions within the hour — the employee sees
    nothing, and the cost is a bounded attribution gap rather than a refusal.

    Pairs with :func:`mark_billing_credential_invalid`: mark first (so the sweep
    knows to re-mint), then strip (so this attempt can still be served).
    """
    employee_id = str(metadata.get("litellm_billing_employee_user_id") or "")
    profile_name = str(metadata.get("litellm_billing_profile_name") or "")
    _log_billing_degraded(
        employee_id=employee_id,
        profile_name=profile_name,
        reason="invalid_credential:retry_unattributed",
    )
    return _clean_metadata(metadata)


def repair_billing_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    if metadata.get("litellm_billing_enforced") is not True:
        raise RunRejected("billing repair requires an enforced payer")
    mark_billing_credential_invalid(metadata)
    return _default_preparer().repair_metadata(metadata)


def classify_litellm_error(error: Any) -> str:
    text = str(error or "").lower()
    if re.search(r"(?:status(?:_code)?|error code|http)\D{0,8}401\b", text) or (
        "authenticationerror" in text or "invalid api key" in text
    ):
        return "invalid_credential"
    if re.search(r"(?:status(?:_code)?|error code|http)\D{0,8}429\b", text):
        return "budget_exceeded" if _is_budget_exceeded_text(text) else "rate_limit"
    return ""


def billing_endpoint_allowed(configured: str, destination: str) -> bool:
    return _allowed_billing_endpoint(configured, destination)


def _metadata_for_binding(identity: BillingIdentity, base_url: str) -> dict[str, Any]:
    return {
        "litellm_billing_enforced": True,
        "litellm_billing_user_id": identity.litellm_user_id,
        "litellm_billing_employee_user_id": identity.employee_user_id,
        "litellm_billing_email": identity.email,
        "litellm_billing_profile_name": identity.profile_name,
        "litellm_billing_team_id": identity.team_id,
        "litellm_billing_team_alias": identity.team_alias,
        "litellm_billing_key_id": identity.key_id,
        "litellm_billing_credential_version": identity.credential_version,
        "litellm_billing_expires_at": identity.expires_at,
        "litellm_billing_base_url": base_url,
    }


def _clean_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(metadata or {}).items()
        if key not in _RESERVED_METADATA
    }


def _billing_enabled() -> bool:
    return os.environ.get("HERMES_LITELLM_BILLING_ENABLED", "").strip().lower() in _TRUE


def _payer_selected(employee_id: str) -> bool:
    if not _billing_enabled():
        return False
    raw = os.environ.get("HERMES_LITELLM_BILLING_PAYER_IDS", "").strip()
    if not raw:
        return False
    selected = {item.strip() for item in raw.split(",") if item.strip()}
    return "*" not in selected and employee_id in selected


def _billing_model_base_url() -> str:
    return os.environ.get("HERMES_LITELLM_BILLING_BASE_URL", "").strip().rstrip("/")


def _latest_org_snapshot() -> Optional[dict[str, Any]]:
    db_path = os.environ.get("HERMES_MULTITENANCY_DB", "").strip() or str(DEFAULT_DB_PATH)
    directory = Path(
        os.environ.get("HERMES_ORG_SNAPSHOT_DIR", "").strip()
        or Path(db_path).expanduser().parent / "org-snapshots"
    ).expanduser()
    try:
        newest = max(directory.glob("org-*.json"), key=lambda path: path.stat().st_mtime)
        payload = json.loads(newest.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _employee_org_fields(employee_id: str) -> tuple[str, str]:
    domain = os.environ.get("HERMES_LITELLM_EMPLOYEE_EMAIL_DOMAIN", "keep.com").strip().lower()
    if not domain or "@" in domain or "/" in domain:
        raise RunRejected("employee email domain is invalid")
    email = f"{employee_id}@{domain}"
    snapshot = _latest_org_snapshot()
    if snapshot is None:
        return email, ""
    employees = snapshot.get("employees")
    departments = snapshot.get("departments")
    if not isinstance(employees, dict) or not isinstance(departments, list):
        return email, ""
    employee = next(
        (
            value for key, value in employees.items()
            if isinstance(value, dict)
            and (str(key) == employee_id or str(value.get("user_id") or "") == employee_id)
        ),
        None,
    )
    if employee is None:
        return email, ""
    snapshot_email = str(
        employee.get("enterprise_email") or employee.get("email") or ""
    ).strip()
    if snapshot_email:
        email = snapshot_email
    by_id = {
        str(dept.get("dept_id")): dept
        for dept in departments
        if isinstance(dept, dict) and dept.get("dept_id")
    }
    path: list[str] = []
    dept_id = str(employee.get("dept_id") or "")
    seen: set[str] = set()
    while dept_id and dept_id not in seen:
        seen.add(dept_id)
        dept = by_id.get(dept_id)
        if dept is None:
            break
        name = str(dept.get("name") or "").strip()
        if name:
            path.insert(0, name)
        parent_id = str(dept.get("parent_id") or "")
        if not parent_id or parent_id == "0":
            break
        dept_id = parent_id
    if not path:
        return email, ""
    first = path[0]
    cooperator = os.environ.get(
        "HERMES_LITELLM_COOPERATOR_DEPT_NAME", "合作商"
    ).strip()
    if first != cooperator:
        return email, first
    separator = os.environ.get("HERMES_LITELLM_COOPERATOR_SEPARATOR", "-")
    vendor = path[-1].split(separator, 1)[0].strip() if separator else path[-1]
    return email, vendor


_DEFAULT_LOCK = threading.Lock()
_DEFAULT_PREPARER: Optional[BillingIdentityPreparer] = None
