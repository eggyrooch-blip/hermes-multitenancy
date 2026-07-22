"""Trusted payer resolution and Hermes LiteLLM runtime credentials.

LiteLLM account/team/key administration belongs to AI Gateway.  This module
only resolves the canonical Hermes payer, keeps the one-time runtime key in
the existing encrypted credential vault, and exposes a short-lived key to the
model runtime after RunBroker admission.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Optional
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .credentials import CredentialStore
from .routing import DEFAULT_DB_PATH, RoutingTable
from .run_broker import RunRejected
from .run_models import RunRequest
from .token_usage_uploader import make_owner_resolver


_TRUE = frozenset({"1", "true", "yes", "on"})
_EMPLOYEE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_OPAQUE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}")
_CONTRACT_MAJOR = "1"
_CONTRACT_VERSION = "1.0"
_MAX_BROKER_RESPONSE_BYTES = 1024 * 1024
_PROVIDER = "litellm"
_SECRET_KIND = "hermes_api_key"
_DAY_MS = 24 * 60 * 60 * 1000
_RENEW_WINDOW_MS = 30 * _DAY_MS
_RENEW_JITTER_MS = 7 * _DAY_MS

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


@dataclass(frozen=True)
class BillingIdentity:
    employee_user_id: str
    profile_name: str = ""
    email: str = ""
    litellm_user_id: str = ""
    team_id: str = ""
    team_alias: str = ""
    key_id: str = ""
    credential_version: int = 0
    expires_at: int = 0
    migration_state: str = "legacy"


@dataclass(frozen=True)
class _ResolvedPayer:
    employee_user_id: str
    profile_name: str
    email: str
    department_alias: str = ""


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


class _GatewayError(RuntimeError):
    def __init__(self, status: int, code: str, retryable: bool = False) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.retryable = retryable


class BillingGatewayClient:
    """Minimal V1 AI Gateway client.  It never calls LiteLLM directly."""

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        *,
        timeout: float = 5.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.token = str(token or "").strip()
        self.timeout = max(0.1, min(float(timeout), 30.0))
        self._opener = opener

    def ensure(
        self,
        *,
        employee_id: str,
        enterprise_email: str,
        department_alias: str,
        reason: str,
        current_key_id: str = "",
        current_credential_version: int = 0,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "employee_id": employee_id,
            "enterprise_email": enterprise_email,
            "reason": reason,
        }
        if department_alias and not current_key_id:
            body["department_alias"] = department_alias
        if bool(current_key_id) != bool(current_credential_version):
            raise RunRejected("billing credential state is inconsistent")
        if current_key_id:
            body.update({
                "current_key_id": current_key_id,
                "current_credential_version": int(current_credential_version),
            })
        return self._post(
            "/internal/v1/hermes/credentials/ensure",
            body,
            idempotency_key=_idempotency_key("ensure", body),
        )

    def ack(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = str(payload.get("api_key") or "")
        body = {
            "employee_id": str(payload.get("employee_id") or ""),
            "key_id": str(payload.get("key_id") or ""),
            "credential_version": int(payload.get("credential_version") or 0),
            "key_sha256": hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
        }
        return self._post(
            "/internal/v1/hermes/credentials/ack",
            body,
            idempotency_key=_idempotency_key("ack", body),
        )

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc or not self.token:
            raise _GatewayError(503, "broker_not_configured", True)
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_BROKER_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raw = exc.read(_MAX_BROKER_RESPONSE_BYTES + 1)
            payload = _decode_json(raw)
            _require_contract(payload)
            error = payload.get("error")
            if not isinstance(error, dict):
                raise _GatewayError(int(exc.code), "invalid_error_envelope") from exc
            code = str(error.get("code") or "").strip()
            retryable = error.get("retryable")
            if not code or not isinstance(retryable, bool):
                raise _GatewayError(int(exc.code), "invalid_error_envelope") from exc
            raise _GatewayError(int(exc.code), code, retryable) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise _GatewayError(503, "broker_unavailable", True) from exc
        if len(raw) > _MAX_BROKER_RESPONSE_BYTES:
            raise _GatewayError(502, "invalid_response")
        payload = _decode_json(raw)
        _require_contract(payload)
        return payload


class BillingCredentialManager:
    """Per-payer single-flight around the encrypted vault and broker V1."""

    def __init__(
        self,
        *,
        vault: CredentialStore,
        gateway: BillingGatewayClient,
        model_base_url: str,
        now_ms: Callable[[], int] | None = None,
        probe: Callable[[str], None] | None = None,
    ) -> None:
        self._vault = vault
        self._gateway = gateway
        self._model_base_url = str(model_base_url or "").strip().rstrip("/")
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._probe = probe or self._probe_key
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def ensure_available(
        self,
        payer: _ResolvedPayer,
        existing: BillingIdentity | None,
        *,
        force_reason: str = "",
    ) -> BillingIdentity:
        with self._payer_lock(payer.employee_user_id):
            return self._ensure_locked(payer, existing, force_reason=force_reason)

    def runtime_api_key(self, metadata: dict[str, Any]) -> str:
        employee_id = str(metadata.get("litellm_billing_employee_user_id") or "")
        profile_name = str(metadata.get("litellm_billing_profile_name") or "")
        if not _EMPLOYEE_ID_RE.fullmatch(employee_id) or not profile_name:
            raise RunRejected("billing payer metadata is invalid")
        with self._payer_lock(employee_id):
            payload = self._load_payload(profile_name, employee_id)
            if payload is None:
                raise RunRejected("billing credential is unavailable")
            self._validate_local_payload(payload, metadata=metadata)
            if payload.get("invalid") or int(payload["expires_at"]) <= self._now_ms():
                raise RunRejected("billing credential is unavailable")
            return str(payload["api_key"])

    def mark_invalid(self, metadata: dict[str, Any]) -> None:
        employee_id = str(metadata.get("litellm_billing_employee_user_id") or "")
        profile_name = str(metadata.get("litellm_billing_profile_name") or "")
        if not employee_id or not profile_name:
            return
        with self._payer_lock(employee_id):
            payload = self._load_payload(profile_name, employee_id)
            if payload is None:
                return
            self._validate_local_payload(payload, metadata=metadata)
            payload["invalid"] = True
            self._save_payload(profile_name, employee_id, payload)

    def _ensure_locked(
        self,
        payer: _ResolvedPayer,
        existing: BillingIdentity | None,
        *,
        force_reason: str,
    ) -> BillingIdentity:
        payload = self._load_payload(payer.profile_name, payer.employee_user_id)
        if payload is not None:
            self._validate_local_payload(payload, payer=payer, existing=existing)
            payload = self._finish_pending(payer, payload)
            if payload is not None:
                binding = _binding_from_payload(payload)
                now = self._now_ms()
                reason = force_reason or (
                    "invalid_401" if payload.get("invalid") else ""
                )
                if not reason and int(payload["expires_at"]) > now:
                    jitter = _renew_jitter_ms(payer.employee_user_id)
                    if int(payload["expires_at"]) - now > _RENEW_WINDOW_MS - jitter:
                        return binding
                    reason = "renewal"
                elif not reason:
                    reason = "renewal"
                current = payload
            else:
                reason = "missing"
                current = None
        else:
            reason = force_reason or "missing"
            current = None

        try:
            response = self._gateway.ensure(
                employee_id=payer.employee_user_id,
                enterprise_email=payer.email,
                department_alias=payer.department_alias,
                reason=reason,
                current_key_id=str((current or {}).get("key_id") or ""),
                current_credential_version=int(
                    (current or {}).get("credential_version") or 0
                ),
            )
        except _GatewayError as exc:
            if (
                exc.retryable
                and current is not None
                and reason == "renewal"
                and not current.get("invalid")
                and int(current["expires_at"]) > self._now_ms()
            ):
                return _binding_from_payload(current)
            raise _gateway_rejection(exc) from exc

        action = str(response.get("action") or "")
        state = str(response.get("state") or "")
        common = self._validate_ensure_response(
            response,
            payer=payer,
            existing=existing,
        )
        if action == "unchanged" and state == "active":
            if reason in {"missing", "invalid_401"} or current is None:
                raise RunRejected("AI Gateway returned an invalid credential state")
            if (
                common["key_id"] != current["key_id"]
                or common["credential_version"] != current["credential_version"]
            ):
                raise RunRejected("AI Gateway returned credential drift")
            self._validate_response_matches_local(common, current)
            current["invalid"] = False
            self._save_payload(payer.profile_name, payer.employee_user_id, current)
            return _binding_from_payload(current)

        if action not in {"issued", "rotated"} or state != "pending":
            raise RunRejected("AI Gateway returned an unsupported credential state")
        api_key = response.get("api_key")
        if not isinstance(api_key, str) or not api_key or len(api_key) > 4096:
            raise RunRejected("AI Gateway returned an invalid credential")
        if current is not None and int(common["credential_version"]) <= int(
            current["credential_version"]
        ):
            raise RunRejected("AI Gateway returned a stale credential")

        new_payload = {
            **common,
            "contract_version": _CONTRACT_VERSION,
            "api_key": api_key,
            "ack_pending": True,
            "probe_pending": True,
            "invalid": False,
        }
        if current is not None:
            new_payload["previous_credential"] = current
        self._save_payload(payer.profile_name, payer.employee_user_id, new_payload)
        finished = self._finish_pending(payer, new_payload)
        if finished is None:
            return self._ensure_locked(payer, existing, force_reason="missing")
        return _binding_from_payload(finished)

    def _finish_pending(
        self,
        payer: _ResolvedPayer,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if payload.get("probe_pending"):
            try:
                self._probe(str(payload["api_key"]))
            except Exception:
                previous = payload.get("previous_credential")
                if isinstance(previous, dict):
                    self._save_payload(
                        payer.profile_name, payer.employee_user_id, previous
                    )
                else:
                    self._delete_payload(payer.profile_name, payer.employee_user_id)
                raise RunRejected("billing credential validation failed")
            payload.pop("previous_credential", None)
            payload["probe_pending"] = False
            self._save_payload(payer.profile_name, payer.employee_user_id, payload)
        if not payload.get("ack_pending"):
            return payload
        try:
            response = self._gateway.ack(payload)
        except _GatewayError as exc:
            if exc.status == 410 and exc.code == "credential_gone":
                self._delete_payload(payer.profile_name, payer.employee_user_id)
                return None
            if exc.retryable:
                return payload
            raise _gateway_rejection(exc) from exc
        if (
            str(response.get("employee_id") or "") != payer.employee_user_id
            or str(response.get("key_id") or "") != str(payload["key_id"])
            or int(response.get("credential_version") or 0)
            != int(payload["credential_version"])
            or str(response.get("state") or "") != "active"
            or str(response.get("action") or "")
            not in {"activated", "already_active"}
        ):
            raise RunRejected("AI Gateway returned an invalid ACK")
        payload["ack_pending"] = False
        self._save_payload(payer.profile_name, payer.employee_user_id, payload)
        return payload

    def _validate_ensure_response(
        self,
        response: dict[str, Any],
        *,
        payer: _ResolvedPayer,
        existing: BillingIdentity | None,
    ) -> dict[str, Any]:
        if str(response.get("employee_id") or "") != payer.employee_user_id:
            raise RunRejected("AI Gateway returned a mismatched employee identity")
        email = str(response.get("enterprise_email") or "").strip()
        if email.lower() != payer.email.lower():
            raise RunRejected("AI Gateway returned a mismatched employee email")
        values = {
            "employee_id": payer.employee_user_id,
            "profile_name": payer.profile_name,
            "enterprise_email": email,
            "litellm_user_id": str(response.get("litellm_user_id") or "").strip(),
            "team_id": str(response.get("team_id") or "").strip(),
            "team_alias": str(response.get("team_alias") or "").strip(),
            "key_id": str(response.get("key_id") or "").strip(),
            "key_alias": str(response.get("key_alias") or "").strip(),
            "credential_version": response.get("credential_version"),
            "expires_at": response.get("expires_at"),
        }
        for key in ("litellm_user_id", "team_id", "key_id"):
            if not _OPAQUE_ID_RE.fullmatch(str(values[key])):
                raise RunRejected(f"AI Gateway returned an invalid {key}")
        if not values["team_alias"] or not values["key_alias"]:
            raise RunRejected("AI Gateway returned incomplete credential identity")
        if (
            not isinstance(values["credential_version"], int)
            or isinstance(values["credential_version"], bool)
            or int(values["credential_version"]) <= 0
            or not isinstance(values["expires_at"], int)
            or isinstance(values["expires_at"], bool)
            or int(values["expires_at"]) <= self._now_ms()
        ):
            raise RunRejected("AI Gateway returned an invalid credential lifetime")
        if existing is not None:
            if (
                existing.profile_name
                and existing.profile_name != payer.profile_name
            ):
                raise RunRejected("billing payer profile drift detected")
            if (
                existing.litellm_user_id
                and existing.litellm_user_id != values["litellm_user_id"]
            ):
                raise RunRejected("AI Gateway returned LiteLLM identity drift")
            if existing.team_id and existing.team_id != values["team_id"]:
                raise RunRejected("AI Gateway returned LiteLLM team drift")
        return values

    def _validate_local_payload(
        self,
        payload: dict[str, Any],
        *,
        payer: _ResolvedPayer | None = None,
        existing: BillingIdentity | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        required = (
            "employee_id", "profile_name", "enterprise_email",
            "litellm_user_id", "team_id", "team_alias", "key_id",
            "key_alias", "credential_version", "expires_at", "api_key",
        )
        if any(not payload.get(key) for key in required):
            raise RunRejected("billing credential vault entry is invalid")
        if payer is not None and (
            payload["employee_id"] != payer.employee_user_id
            or payload["profile_name"] != payer.profile_name
            or str(payload["enterprise_email"]).lower() != payer.email.lower()
        ):
            raise RunRejected("billing credential identity drift detected")
        if existing is not None and existing.migration_state == "enforced":
            if (
                payload["litellm_user_id"] != existing.litellm_user_id
                or payload["team_id"] != existing.team_id
            ):
                raise RunRejected("billing credential tuple drift detected")
        if metadata is not None:
            expected = {
                "employee_id": metadata.get("litellm_billing_employee_user_id"),
                "profile_name": metadata.get("litellm_billing_profile_name"),
                "litellm_user_id": metadata.get("litellm_billing_user_id"),
                "team_id": metadata.get("litellm_billing_team_id"),
                "key_id": metadata.get("litellm_billing_key_id"),
                "credential_version": metadata.get(
                    "litellm_billing_credential_version"
                ),
            }
            if any(str(payload[key]) != str(value) for key, value in expected.items()):
                raise RunRejected("billing runtime credential drift detected")

    @staticmethod
    def _validate_response_matches_local(
        response: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        for key in (
            "employee_id", "enterprise_email", "litellm_user_id", "team_id",
            "team_alias", "key_id", "key_alias", "credential_version",
            "expires_at",
        ):
            if str(response.get(key)) != str(payload.get(key)):
                raise RunRejected("AI Gateway returned credential drift")

    def _load_payload(self, profile_name: str, employee_id: str) -> dict[str, Any] | None:
        try:
            status = self._vault.get_status(
                profile_name=profile_name,
                subject_id=employee_id,
                provider=_PROVIDER,
                secret_kind=_SECRET_KIND,
            )
            if status.get("status") == "missing":
                return None
            payload = self._vault.get_secret_for_runtime(
                profile_name=profile_name,
                subject_id=employee_id,
                provider=_PROVIDER,
                secret_kind=_SECRET_KIND,
            )
        except Exception as exc:
            raise RunRejected("billing credential vault is unavailable") from exc
        if not isinstance(payload, dict):
            raise RunRejected("billing credential vault entry is invalid")
        return dict(payload)

    def _save_payload(
        self, profile_name: str, employee_id: str, payload: dict[str, Any]
    ) -> None:
        try:
            self._vault.put_credential(
                profile_name=profile_name,
                subject_id=employee_id,
                provider=_PROVIDER,
                secret_kind=_SECRET_KIND,
                payload=payload,
                expires_at=int(payload["expires_at"]),
            )
        except Exception as exc:
            raise RunRejected("billing credential vault is unavailable") from exc

    def _delete_payload(self, profile_name: str, employee_id: str) -> None:
        try:
            self._vault.delete_credential(
                profile_name=profile_name,
                subject_id=employee_id,
                provider=_PROVIDER,
                secret_kind=_SECRET_KIND,
            )
        except Exception as exc:
            raise RunRejected("billing credential vault is unavailable") from exc

    def _probe_key(self, api_key: str) -> None:
        if not _allowed_billing_endpoint(self._model_base_url, self._model_base_url):
            raise RunRejected("LiteLLM billing endpoint is not configured")
        request = urllib.request.Request(
            f"{self._model_base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read(1)
                if int(getattr(response, "status", 200)) != 200:
                    raise RunRejected("billing credential validation failed")
        except urllib.error.HTTPError as exc:
            body = exc.read(8192).decode("utf-8", errors="replace").lower()
            if int(exc.code) == 429 and _is_budget_exceeded_text(body):
                return
            raise RunRejected("billing credential validation failed") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RunRejected("LiteLLM credential probe is unavailable") from exc

    def _payer_lock(self, employee_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(employee_id)
            if lock is None:
                # credential_gone immediately re-enters ensure for the same
                # payer after deleting the vanished generation.
                lock = threading.RLock()
                self._locks[employee_id] = lock
            return lock


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
            if _billing_enabled():
                raise RunRejected("employee billing identity could not be resolved")
            return request if metadata == request.metadata else replace(request, metadata=metadata)
        existing = self._store.get(payer.employee_user_id)
        selected = _payer_selected(payer.employee_user_id)
        if not selected and not (
            existing is not None and existing.migration_state == "enforced"
        ):
            return request if metadata == request.metadata else replace(request, metadata=metadata)
        if existing is not None:
            if existing.profile_name and existing.profile_name != payer.profile_name:
                raise RunRejected("billing payer profile drift detected")
            if existing.email and existing.email.lower() != payer.email.lower():
                raise RunRejected("billing payer email drift detected")
        binding = self._credentials.ensure_available(payer, existing)
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
        payer = _ResolvedPayer(employee_id, profile_name, email)
        binding = self._credentials.ensure_available(
            payer, existing, force_reason="invalid_401"
        )
        binding = replace(binding, migration_state="enforced")
        self._store.put(binding)
        clean = _clean_metadata(metadata)
        clean.update(_metadata_for_binding(binding, _billing_model_base_url()))
        return clean

    def _payer(
        self, request: RunRequest, metadata: dict[str, Any]
    ) -> _ResolvedPayer | None:
        with self._routing_lock:
            owner_open_id = self._resolve_owner({
                "chat_type": metadata.get("chat_type"),
                "chat_id": request.chat_id,
                "sender_open_id": (
                    metadata.get("sender_open_id")
                    if request.channel == "feishu"
                    else ""
                ),
                "profile": request.profile_name,
            })
            row = self._employee_row(owner_open_id)
        if row is None:
            return None
        employee_id = str(getattr(row, "user_id", "") or "").strip()
        profile_name = str(getattr(row, "profile_name", "") or "").strip()
        if not profile_name:
            profile_name = employee_id
        email, department = _employee_org_fields(employee_id)
        return _ResolvedPayer(employee_id, profile_name, email, department)

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
        for getter_name in ("resolve_owner_root", "lookup_by_open_id"):
            getter = getattr(self._routing, getter_name, None)
            row = getter(owner_open_id) if callable(getter) else None
            user_id = str(getattr(row, "user_id", "") or "").strip() if row else ""
            if _EMPLOYEE_ID_RE.fullmatch(user_id) and not user_id.startswith("ou_"):
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


async def prepare_billing_request(request: RunRequest) -> RunRequest:
    """Resolve the payer before idempotency is consumed; never trust caller metadata."""
    import asyncio

    return await asyncio.to_thread(_default_preparer().prepare, request)


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


def _binding_from_payload(payload: dict[str, Any]) -> BillingIdentity:
    return BillingIdentity(
        employee_user_id=str(payload["employee_id"]),
        profile_name=str(payload["profile_name"]),
        email=str(payload["enterprise_email"]),
        litellm_user_id=str(payload["litellm_user_id"]),
        team_id=str(payload["team_id"]),
        team_alias=str(payload.get("team_alias") or ""),
        key_id=str(payload["key_id"]),
        credential_version=int(payload["credential_version"]),
        expires_at=int(payload["expires_at"]),
        migration_state="enforced",
    )


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
        return True
    selected = {item.strip() for item in raw.split(",") if item.strip()}
    return "*" in selected or employee_id in selected


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


def _require_contract(payload: dict[str, Any]) -> None:
    version = str(payload.get("contract_version") or "").strip()
    if not version or version.split(".", 1)[0] != _CONTRACT_MAJOR:
        raise _GatewayError(502, "unsupported_contract_version")


def _decode_json(raw: bytes) -> dict[str, Any]:
    if len(raw) > _MAX_BROKER_RESPONSE_BYTES:
        raise _GatewayError(502, "invalid_response")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _GatewayError(502, "invalid_json") from exc
    if not isinstance(payload, dict):
        raise _GatewayError(502, "invalid_response")
    return payload


def _idempotency_key(kind: str, body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"hermes-v1:{kind}:{canonical}".encode("utf-8")).hexdigest()


def _gateway_rejection(exc: _GatewayError) -> RunRejected:
    if exc.code in {"identity_conflict", "account_topology_conflict"}:
        return RunRejected("employee LiteLLM account requires manual reconciliation")
    if exc.retryable:
        return RunRejected("employee billing initialization is temporarily unavailable")
    return RunRejected("employee billing initialization was rejected")


def _renew_jitter_ms(employee_id: str) -> int:
    digest = hashlib.sha256(employee_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % (_RENEW_JITTER_MS + 1)


def _is_budget_exceeded_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "budget_exceeded", "budget exceeded", "max budget",
            "monthly budget", "budget has been exceeded",
        )
    )


def _allowed_billing_endpoint(left: str, right: str) -> bool:
    try:
        configured = urlparse(str(left or "").strip())
        destination = urlparse(str(right or "").strip())
    except ValueError:
        return False
    if configured.scheme not in {"http", "https"} or destination.scheme != configured.scheme:
        return False
    if not configured.netloc or configured.netloc.lower() != destination.netloc.lower():
        return False
    allowed = {
        item.strip().rstrip("/") or "/"
        for item in os.environ.get(
            "HERMES_LITELLM_BILLING_ALLOWED_PATHS", "/v1,/anthropic"
        ).split(",")
        if item.strip()
    }
    path = destination.path.rstrip("/") or "/"
    return any(path == prefix or path.startswith(prefix + "/") for prefix in allowed)


_DEFAULT_LOCK = threading.Lock()
_DEFAULT_PREPARER: Optional[BillingIdentityPreparer] = None
