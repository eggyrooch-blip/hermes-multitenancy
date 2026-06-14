from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .credentials import _resolve_key
from .runtime import strict_context_enabled

_LEASE_VERSION = "v1"
_LEASE_SECRET_PREFIX = b"hermes-credential-lease-v1:"
_BROKER_ROUTE = "/api/run-broker/credentials/lease"


class LeaseError(PermissionError):
    """Lease verification failed."""


class BrokerClientError(PermissionError):
    """Credential broker request failed closed."""


@dataclass(frozen=True)
class LeaseClaims:
    profile_name: str
    open_id: str
    run_id: str
    issued_at: int
    expires_at: int


def _urlsafe_b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _urlsafe_b64decode(data: str) -> bytes:
    text = str(data or "").strip()
    if not text:
        raise LeaseError("invalid lease")
    padding = "=" * ((4 - len(text) % 4) % 4)
    try:
        return base64.urlsafe_b64decode((text + padding).encode("ascii"))
    except Exception as exc:  # pragma: no cover - malformed input guard
        raise LeaseError("invalid lease") from exc


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _hmac_sha256(secret: bytes, message: bytes) -> bytes:
    return hmac.new(secret, message, hashlib.sha256).digest()


def lease_signing_secret() -> bytes:
    master = _resolve_key(None)
    return hashlib.sha256(_LEASE_SECRET_PREFIX + master).digest()


def mint_lease(
    *,
    profile_name: str,
    open_id: str,
    run_id: str,
    secret: str | bytes,
    ttl_seconds: int = 300,
) -> str:
    issued_at = int(time.time())
    payload = {
        "profile_name": str(profile_name or "").strip(),
        "open_id": str(open_id or "").strip(),
        "run_id": str(run_id or "").strip(),
        "issued_at": issued_at,
        "expires_at": issued_at + int(ttl_seconds),
    }
    payload_bytes = _canonical_payload(payload)
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    signature = _hmac_sha256(secret_bytes, payload_bytes)
    return f"{_LEASE_VERSION}:{_urlsafe_b64encode(payload_bytes)}.{_urlsafe_b64encode(signature)}"


def verify_lease(token: str, *, secret: str | bytes) -> LeaseClaims:
    version, sep, signed = str(token or "").partition(":")
    if version != _LEASE_VERSION or not sep:
        raise LeaseError("invalid lease")
    payload_part, dot, sig_part = signed.partition(".")
    if not dot:
        raise LeaseError("invalid lease")
    payload_bytes = _urlsafe_b64decode(payload_part)
    sig_bytes = _urlsafe_b64decode(sig_part)
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    expected_sig = _hmac_sha256(secret_bytes, payload_bytes)
    if not hmac.compare_digest(sig_bytes, expected_sig):
        raise LeaseError("invalid lease")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:  # pragma: no cover - malformed input guard
        raise LeaseError("invalid lease") from exc
    expires_at = int(payload.get("expires_at") or 0)
    issued_at = int(payload.get("issued_at") or 0)
    if expires_at <= int(time.time()):
        raise LeaseError("invalid lease")
    claims = LeaseClaims(
        profile_name=str(payload.get("profile_name") or "").strip(),
        open_id=str(payload.get("open_id") or "").strip(),
        run_id=str(payload.get("run_id") or "").strip(),
        issued_at=issued_at,
        expires_at=expires_at,
    )
    if not (claims.profile_name and claims.open_id and claims.run_id and claims.issued_at > 0):
        raise LeaseError("invalid lease")
    return claims


def assert_lease_binding(
    claims: LeaseClaims,
    *,
    profile_name: str,
    open_id: str,
    run_id: str,
) -> None:
    if not (
        hmac.compare_digest(claims.profile_name, str(profile_name or "").strip())
        and hmac.compare_digest(claims.open_id, str(open_id or "").strip())
        and hmac.compare_digest(claims.run_id, str(run_id or "").strip())
    ):
        raise LeaseError("forbidden")


def running_as_broker_child() -> bool:
    if not strict_context_enabled():
        return False
    token = str(os.environ.get("HERMES_MULTITENANCY_CRED_BROKER_TOKEN") or "").strip()
    has_master = bool(
        str(os.environ.get("HERMES_MULTITENANCY_CREDENTIAL_KEY") or "").strip()
        or str(os.environ.get("HERMES_CREDENTIAL_KEY") or "").strip()
    )
    return bool(token) and not has_master


def _broker_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise BrokerClientError("credential broker unavailable")
    return value


def fetch_via_broker(
    *,
    kind: str,
    profile_name: str,
    open_id: str,
    run_id: str,
    **selectors: Any,
) -> dict[str, Any] | None:
    url = _broker_env("HERMES_MULTITENANCY_CRED_BROKER_URL")
    token = _broker_env("HERMES_MULTITENANCY_CRED_BROKER_TOKEN")
    lease = _broker_env("HERMES_MULTITENANCY_CRED_LEASE")
    payload = {
        "lease": lease,
        "kind": str(kind or "").strip(),
        "profile_name": str(profile_name or "").strip(),
        "open_id": str(open_id or "").strip(),
        "run_id": str(run_id or "").strip(),
    }
    for key, value in selectors.items():
        if value is not None:
            payload[str(key)] = value
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{url.rstrip('/')}{_BROKER_ROUTE}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = getattr(response, "status", 200)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise BrokerClientError("credential broker denied request") from exc
    except urllib.error.URLError as exc:
        raise BrokerClientError("credential broker unavailable") from exc
    except Exception as exc:  # pragma: no cover - transport guard
        raise BrokerClientError("credential broker unavailable") from exc
    if status != 200:
        raise BrokerClientError("credential broker denied request")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # pragma: no cover - malformed response guard
        raise BrokerClientError("credential broker denied request") from exc
    result = decoded.get("payload")
    if result is None:
        return None
    if not isinstance(result, dict):
        raise BrokerClientError("credential broker denied request")
    return result
