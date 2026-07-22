"""AI Gateway contract client and encrypted per-payer LiteLLM credentials."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
import re
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .credentials import CredentialStore
from .run_broker import RunRejected


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


