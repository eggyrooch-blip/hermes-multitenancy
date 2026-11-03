"""Signed, short-lived proof that a GitLab PAT belongs to one routed actor."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
import secrets
import time
import weakref
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


_SCOPES = ["read_api", "read_repository"]
_PREFIX = b"hermes-gitlab-owner-scope-attestation:v1\n"
_MAX_TTL_MS = 300_000
_CONTEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_NONCE = re.compile(r"[0-9a-f]{32}")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_FIELDS = {
    "version",
    "audience",
    "run_id",
    "actor_subject_fp",
    "profile_fp",
    "gitlab_owner_fp",
    "credential_fp",
    "token_fp",
    "scopes",
    "issued_at_ms",
    "expires_at_ms",
    "nonce",
    "signature",
}
_RUN_SEAL_KEY = secrets.token_bytes(32)


class AttestationError(ValueError):
    pass


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class TrustedGitLabRunAttestation:
    """In-process capability issued only after one receipt is consumed."""

    run_id: str
    actor_subject_fp: str
    profile_fp: str
    token_fp: str
    expires_at_ms: int
    _seal: str = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        run_id: str,
        actor_subject_fp: str,
        profile_fp: str,
        token_fp: str,
        expires_at_ms: int,
        _seal: str | None = None,
    ) -> None:
        expected_seal = _run_seal(
            run_id, actor_subject_fp, profile_fp, token_fp, expires_at_ms
        )
        if not isinstance(_seal, str) or not hmac.compare_digest(_seal, expected_seal):
            _fail()
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "actor_subject_fp", actor_subject_fp)
        object.__setattr__(self, "profile_fp", profile_fp)
        object.__setattr__(self, "token_fp", token_fp)
        object.__setattr__(self, "expires_at_ms", expires_at_ms)
        object.__setattr__(self, "_seal", _seal)


_ISSUED_RUN_ATTESTATIONS: weakref.WeakValueDictionary[
    int, TrustedGitLabRunAttestation
] = weakref.WeakValueDictionary()


def _fail() -> None:
    raise AttestationError("invalid_attestation")


def _run_seal(
    run_id: str,
    actor_subject_fp: str,
    profile_fp: str,
    token_fp: str,
    expires_at_ms: int,
) -> str:
    payload = "\0".join(
        (run_id, actor_subject_fp, profile_fp, token_fp, str(expires_at_ms))
    ).encode()
    return hmac.new(
        _RUN_SEAL_KEY,
        b"hermes-gitlab-trusted-run:v1\n" + payload,
        hashlib.sha256,
    ).hexdigest()


def _context(value: object) -> str:
    result = str(value) if isinstance(value, str) else ""
    if not _CONTEXT.fullmatch(result):
        _fail()
    return result


def _identity(value: object) -> str:
    result = str(value) if isinstance(value, str) else ""
    if not result or result != result.strip() or any(ord(char) < 32 for char in result):
        _fail()
    return result


def _fingerprint(key: bytes, label: str, value: str) -> str:
    if type(key) is not bytes or not 32 <= len(key) <= 4096:
        _fail()
    return hmac.new(
        key,
        f"hermes-gitlab-owner-scope-attestation:{label}:v1\n{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        _fail()
    return value


def create_attestation(
    creation_response: Mapping[str, Any],
    *,
    expected_gitlab_user_id: int,
    get_current_user: Callable[[str], Mapping[str, Any]],
    actor_subject: str,
    profile: str,
    run_id: str,
    audience: str,
    private_key: Ed25519PrivateKey,
    fingerprint_key: bytes,
    now_ms: int | None = None,
    ttl_ms: int = _MAX_TTL_MS,
) -> dict[str, Any]:
    """Validate a GitLab 14.10 PAT response and sign a secret-free receipt."""
    if not isinstance(creation_response, Mapping):
        _fail()
    owner_id = _positive_int(expected_gitlab_user_id)
    credential_id = _positive_int(creation_response.get("id"))
    if _positive_int(creation_response.get("user_id")) != owner_id:
        _fail()
    scopes = creation_response.get("scopes")
    if (
        not isinstance(scopes, list)
        or any(type(scope) is not str for scope in scopes)
        or sorted(scopes) != _SCOPES
        or len(scopes) != 2
    ):
        _fail()
    if (
        creation_response.get("active") is not True
        or creation_response.get("revoked") is not False
    ):
        _fail()
    token = creation_response.get("token")
    if not isinstance(token, str) or not token or token != token.strip():
        _fail()
    actor = _identity(actor_subject)
    routed_profile = _identity(profile)
    receipt_run = _context(run_id)
    receipt_audience = _context(audience)
    if not isinstance(private_key, Ed25519PrivateKey):
        _fail()
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if (
        type(now_ms) is not int
        or now_ms <= 0
        or type(ttl_ms) is not int
        or not 0 < ttl_ms <= _MAX_TTL_MS
    ):
        _fail()
    try:
        current_user = get_current_user(token)
    except Exception:
        raise AttestationError("invalid_attestation") from None
    if (
        not isinstance(current_user, Mapping)
        or _positive_int(current_user.get("id")) != owner_id
    ):
        _fail()

    receipt: dict[str, Any] = {
        "version": 1,
        "audience": receipt_audience,
        "run_id": receipt_run,
        "actor_subject_fp": _fingerprint(fingerprint_key, "actor", actor),
        "profile_fp": _fingerprint(fingerprint_key, "profile", routed_profile),
        "gitlab_owner_fp": _fingerprint(fingerprint_key, "owner", str(owner_id)),
        "credential_fp": _fingerprint(fingerprint_key, "credential", str(credential_id)),
        "token_fp": _fingerprint(fingerprint_key, "token", token),
        "scopes": _SCOPES.copy(),
        "issued_at_ms": now_ms,
        "expires_at_ms": now_ms + ttl_ms,
        "nonce": secrets.token_hex(16),
    }
    receipt["signature"] = base64.b64encode(
        private_key.sign(_PREFIX + _canonical(receipt))
    ).decode()
    return receipt


def verify_attestation(
    receipt: Mapping[str, Any],
    *,
    public_key: Ed25519PublicKey,
    expected_audience: str,
    expected_run_id: str,
    actor_subject: str,
    profile: str,
    token: str,
    expected_gitlab_user_id: int,
    fingerprint_key: bytes,
    consume_nonce: Callable[[str, int], bool],
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Verify one receipt and atomically consume its nonce."""
    if not isinstance(receipt, Mapping) or set(receipt) != _FIELDS:
        _fail()
    if not isinstance(public_key, Ed25519PublicKey):
        _fail()
    audience = _context(expected_audience)
    run_id = _context(expected_run_id)
    actor = _identity(actor_subject)
    routed_profile = _identity(profile)
    secret = _identity(token)
    owner_id = _positive_int(expected_gitlab_user_id)
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if type(now_ms) is not int or now_ms <= 0:
        _fail()

    issued = receipt.get("issued_at_ms")
    expires = receipt.get("expires_at_ms")
    nonce = receipt.get("nonce")
    scopes = receipt.get("scopes")
    if (
        receipt.get("version") != 1
        or receipt.get("audience") != audience
        or receipt.get("run_id") != run_id
        or type(issued) is not int
        or type(expires) is not int
        or issued <= 0
        or expires <= issued
        or expires - issued > _MAX_TTL_MS
        or now_ms < issued
        or now_ms >= expires
        or not isinstance(nonce, str)
        or not _NONCE.fullmatch(nonce)
        or not isinstance(scopes, list)
        or scopes != _SCOPES
    ):
        _fail()

    expected_fingerprints = {
        "actor_subject_fp": _fingerprint(fingerprint_key, "actor", actor),
        "profile_fp": _fingerprint(fingerprint_key, "profile", routed_profile),
        "gitlab_owner_fp": _fingerprint(fingerprint_key, "owner", str(owner_id)),
        "token_fp": _fingerprint(fingerprint_key, "token", secret),
    }
    for name, expected in expected_fingerprints.items():
        actual = receipt.get(name)
        if (
            not isinstance(actual, str)
            or not _FINGERPRINT.fullmatch(actual)
            or not hmac.compare_digest(actual, expected)
        ):
            _fail()
    credential_fp = receipt.get("credential_fp")
    if not isinstance(credential_fp, str) or not _FINGERPRINT.fullmatch(credential_fp):
        _fail()

    unsigned = {name: value for name, value in receipt.items() if name != "signature"}
    encoded_signature = receipt.get("signature")
    if not isinstance(encoded_signature, str):
        _fail()
    try:
        signature = base64.b64decode(encoded_signature, validate=True)
        public_key.verify(signature, _PREFIX + _canonical(unsigned))
    except (ValueError, InvalidSignature):
        _fail()
    try:
        consumed = consume_nonce(nonce, expires)
    except Exception:
        raise AttestationError("invalid_attestation") from None
    if consumed is not True:
        _fail()
    return dict(receipt)


def issue_trusted_gitlab_run_attestation(
    receipt: Mapping[str, Any],
    *,
    public_key: Ed25519PublicKey,
    expected_audience: str,
    expected_run_id: str,
    actor_subject: str,
    profile: str,
    token: str,
    expected_gitlab_user_id: int,
    fingerprint_key: bytes,
    consume_nonce: Callable[[str, int], bool],
    now_ms: int | None = None,
) -> TrustedGitLabRunAttestation:
    """Consume a valid receipt and mint its in-process run capability."""
    verified = verify_attestation(
        receipt,
        public_key=public_key,
        expected_audience=expected_audience,
        expected_run_id=expected_run_id,
        actor_subject=actor_subject,
        profile=profile,
        token=token,
        expected_gitlab_user_id=expected_gitlab_user_id,
        fingerprint_key=fingerprint_key,
        consume_nonce=consume_nonce,
        now_ms=now_ms,
    )
    run_id = str(verified["run_id"])
    actor_subject_fp = str(verified["actor_subject_fp"])
    profile_fp = str(verified["profile_fp"])
    token_fp = str(verified["token_fp"])
    expires_at_ms = int(verified["expires_at_ms"])
    trusted = TrustedGitLabRunAttestation(
        run_id=run_id,
        actor_subject_fp=actor_subject_fp,
        profile_fp=profile_fp,
        token_fp=token_fp,
        expires_at_ms=expires_at_ms,
        _seal=_run_seal(
            run_id, actor_subject_fp, profile_fp, token_fp, expires_at_ms
        ),
    )
    _ISSUED_RUN_ATTESTATIONS[id(trusted)] = trusted
    return trusted


def require_trusted_gitlab_run_attestation(
    attestation: TrustedGitLabRunAttestation,
    *,
    expected_run_id: str,
    actor_subject: str,
    profile: str,
    token: str,
    fingerprint_key: bytes,
    now_ms: int | None = None,
) -> TrustedGitLabRunAttestation:
    """Fail closed unless a live capability matches this exact routed run."""
    if (
        type(attestation) is not TrustedGitLabRunAttestation
        or _ISSUED_RUN_ATTESTATIONS.get(id(attestation)) is not attestation
        or not hmac.compare_digest(
            attestation._seal,
            _run_seal(
                attestation.run_id,
                attestation.actor_subject_fp,
                attestation.profile_fp,
                attestation.token_fp,
                attestation.expires_at_ms,
            ),
        )
    ):
        _fail()
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if (
        type(now_ms) is not int
        or now_ms <= 0
        or type(attestation.expires_at_ms) is not int
        or now_ms >= attestation.expires_at_ms
        or attestation.run_id != _context(expected_run_id)
        or not hmac.compare_digest(
            attestation.actor_subject_fp,
            _fingerprint(fingerprint_key, "actor", _identity(actor_subject)),
        )
        or not hmac.compare_digest(
            attestation.profile_fp,
            _fingerprint(fingerprint_key, "profile", _identity(profile)),
        )
        or not hmac.compare_digest(
            attestation.token_fp,
            _fingerprint(fingerprint_key, "token", _identity(token)),
        )
    ):
        _fail()
    return attestation


def store_attested_read_token(
    *,
    principal: Any,
    token: str,
    receipt: Mapping[str, Any],
    expected_run_id: str,
    expected_audience: str,
    expected_gitlab_user_id: int,
    public_key: Ed25519PublicKey,
    fingerprint_key: bytes,
    consume_nonce: Callable[[str, int], bool],
    shared_home: Path,
    db_path: Path | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Verify one broker receipt and atomically cross the GitLab vault boundary."""
    from .credential_delegation import profile_is_solely_owned_by
    from .credentials import CredentialStore
    from .gitlab_token_intake import assert_runtime_contract_live, retire_legacy_token_file
    from .routing import RoutingTable
    from .trusted_runtime_principal import TrustedRuntimePrincipal

    if (
        not isinstance(principal, TrustedRuntimePrincipal)
        or not principal.is_authentic()
        or principal.channel != "webui"
        or not principal.actor_subject.startswith("ou_")
        or principal.credential_subject != principal.actor_subject
        or not principal.profile_name
    ):
        _fail()
    cleaned = _identity(token)
    home = Path(shared_home)
    database = Path(db_path or home / "multitenancy.db")
    try:
        table = RoutingTable(database)
        try:
            route = table.lookup_by_open_id(principal.actor_subject)
        finally:
            table.close()
    except Exception:
        _fail()
    if (
        route is None
        or route.profile_name != principal.profile_name
        or route.open_id != principal.actor_subject
        or not route.active
        or route.kind != "user"
        or route.provenance != "sync"
        or not profile_is_solely_owned_by(
            database, principal.profile_name, principal.actor_subject
        )
    ):
        _fail()
    try:
        entry = assert_runtime_contract_live(home, profile_name=principal.profile_name)
    except Exception:
        _fail()
    verify_attestation(
        receipt,
        public_key=public_key,
        expected_audience=expected_audience,
        expected_run_id=expected_run_id,
        actor_subject=principal.actor_subject,
        profile=principal.profile_name,
        token=cleaned,
        expected_gitlab_user_id=expected_gitlab_user_id,
        fingerprint_key=fingerprint_key,
        consume_nonce=consume_nonce,
        now_ms=now_ms,
    )
    try:
        retired = retire_legacy_token_file(
            home, profile_name=principal.profile_name, entry=entry
        )
        store = CredentialStore(database)
        try:
            store.put_credential(
                profile_name=principal.profile_name,
                subject_id=str(entry["subject_id"]).strip(),
                provider="gitlab",
                secret_kind="token",
                payload={
                    "token": cleaned,
                    "owner_actor_subject": principal.actor_subject,
                    "token_owner_verified": True,
                },
                scopes=_SCOPES,
                expires_at=None,
            )
        finally:
            store.close()
    except Exception:
        _fail()
    return {
        "stored": True,
        "profile_name": principal.profile_name,
        "scopes": _SCOPES.copy(),
        "scope_binding_verified": True,
        "legacy_file_retired": retired,
    }
