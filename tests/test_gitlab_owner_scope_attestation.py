from __future__ import annotations

import copy
from dataclasses import asdict
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes_multitenancy.gitlab_owner_scope_attestation import (
    AttestationError,
    TrustedGitLabRunAttestation,
    create_attestation,
    issue_trusted_gitlab_run_attestation,
    require_trusted_gitlab_run_attestation,
    verify_attestation,
)


ACTOR = "12345678-1234-1234-1234-123456789abc"
PROFILE = "sunke"
RUN_ID = "harness-base-codex-w0-r14"
AUDIENCE = "hermes-gitlab-intake"
TOKEN = "glpat-owner-bound-secret"
FINGERPRINT_KEY = b"local-test-fingerprint-key-32-bytes"
NOW_MS = 1_800_000_000_000


def _created(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": 41,
        "user_id": 7,
        "scopes": ["read_repository", "read_api"],
        "active": True,
        "revoked": False,
        "token": TOKEN,
    }
    value.update(overrides)
    return value


def _receipt() -> tuple[dict[str, object], Ed25519PrivateKey]:
    signer = Ed25519PrivateKey.generate()
    seen: list[str] = []
    receipt = create_attestation(
        _created(),
        expected_gitlab_user_id=7,
        get_current_user=lambda token: seen.append(token) or {"id": 7},
        actor_subject=ACTOR,
        profile=PROFILE,
        run_id=RUN_ID,
        audience=AUDIENCE,
        private_key=signer,
        fingerprint_key=FINGERPRINT_KEY,
        now_ms=NOW_MS,
    )
    assert seen == [TOKEN]
    return receipt, signer


def _verify(
    receipt: dict[str, object], signer: Ed25519PrivateKey, **overrides: object
) -> dict[str, object]:
    arguments: dict[str, object] = {
        "public_key": signer.public_key(),
        "expected_audience": AUDIENCE,
        "expected_run_id": RUN_ID,
        "actor_subject": ACTOR,
        "profile": PROFILE,
        "token": TOKEN,
        "expected_gitlab_user_id": 7,
        "fingerprint_key": FINGERPRINT_KEY,
        "consume_nonce": lambda _nonce, _expires: True,
        "now_ms": NOW_MS + 1,
    }
    arguments.update(overrides)
    return verify_attestation(receipt, **arguments)  # type: ignore[arg-type]


def test_create_and_verify_owner_scope_attestation_without_raw_identity_or_token() -> None:
    receipt, signer = _receipt()

    verified = _verify(receipt, signer)

    assert verified["scopes"] == ["read_api", "read_repository"]
    serialized = json.dumps(receipt, sort_keys=True)
    assert TOKEN not in serialized
    assert ACTOR not in serialized
    assert PROFILE not in serialized
    assert "open_id" not in serialized


@pytest.mark.parametrize(
    "override",
    [
        {"user_id": 8},
        {"scopes": ["read_api"]},
        {"scopes": ["read_api", None]},
        {"scopes": ["read_api", "read_repository", "api"]},
        {"active": False},
        {"revoked": True},
        {"id": 0},
        {"token": ""},
    ],
)
def test_create_fails_closed_on_invalid_creation_response(
    override: dict[str, object],
) -> None:
    with pytest.raises(AttestationError):
        create_attestation(
            _created(**override),
            expected_gitlab_user_id=7,
            get_current_user=lambda _token: {"id": 7},
            actor_subject=ACTOR,
            profile=PROFILE,
            run_id=RUN_ID,
            audience=AUDIENCE,
            private_key=Ed25519PrivateKey.generate(),
            fingerprint_key=FINGERPRINT_KEY,
            now_ms=NOW_MS,
        )


def test_create_fails_closed_when_new_token_resolves_to_another_owner() -> None:
    with pytest.raises(AttestationError):
        create_attestation(
            _created(),
            expected_gitlab_user_id=7,
            get_current_user=lambda _token: {"id": 8},
            actor_subject=ACTOR,
            profile=PROFILE,
            run_id=RUN_ID,
            audience=AUDIENCE,
            private_key=Ed25519PrivateKey.generate(),
            fingerprint_key=FINGERPRINT_KEY,
            now_ms=NOW_MS,
        )


def test_verify_requires_atomic_nonce_consumption_and_rejects_replay() -> None:
    receipt, signer = _receipt()
    seen: set[str] = set()

    def consume(nonce: str, _expires: int) -> bool:
        if nonce in seen:
            return False
        seen.add(nonce)
        return True

    _verify(receipt, signer, consume_nonce=consume)
    with pytest.raises(AttestationError):
        _verify(receipt, signer, consume_nonce=consume)

    arguments = {
        "public_key": signer.public_key(),
        "expected_audience": AUDIENCE,
        "expected_run_id": RUN_ID,
        "actor_subject": ACTOR,
        "profile": PROFILE,
        "token": TOKEN,
        "expected_gitlab_user_id": 7,
        "fingerprint_key": FINGERPRINT_KEY,
        "now_ms": NOW_MS + 1,
    }
    with pytest.raises(TypeError):
        verify_attestation(receipt, **arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mutator", "verify_override"),
    [
        (lambda value: value.update(audience="wrong"), {}),
        (lambda value: value.update(run_id="wrong"), {}),
        (lambda value: value.update(expires_at_ms=NOW_MS), {}),
        (lambda value: value.update(scopes=["read_api"]), {}),
        (lambda value: value.update(extra="unsigned-shape"), {}),
        (lambda _value: None, {"actor_subject": "different-actor"}),
        (lambda _value: None, {"profile": "different-profile"}),
        (lambda _value: None, {"token": "glpat-different"}),
        (lambda _value: None, {"expected_gitlab_user_id": 8}),
        (lambda _value: None, {"expected_audience": "wrong"}),
        (lambda _value: None, {"expected_run_id": "wrong"}),
        (lambda _value: None, {"now_ms": NOW_MS + 300_000}),
    ],
)
def test_verify_fails_closed_on_tampering_or_context_mismatch(
    mutator: object, verify_override: dict[str, object]
) -> None:
    receipt, signer = _receipt()
    tampered = copy.deepcopy(receipt)
    mutator(tampered)  # type: ignore[operator]

    with pytest.raises(AttestationError):
        _verify(tampered, signer, **verify_override)


def _trusted_run() -> TrustedGitLabRunAttestation:
    receipt, signer = _receipt()
    consumed: list[tuple[str, int]] = []
    trusted = issue_trusted_gitlab_run_attestation(
        receipt,
        public_key=signer.public_key(),
        expected_audience=AUDIENCE,
        expected_run_id=RUN_ID,
        actor_subject=ACTOR,
        profile=PROFILE,
        token=TOKEN,
        expected_gitlab_user_id=7,
        fingerprint_key=FINGERPRINT_KEY,
        consume_nonce=lambda nonce, expires: consumed.append((nonce, expires)) or True,
        now_ms=NOW_MS + 1,
    )
    assert consumed == [(receipt["nonce"], receipt["expires_at_ms"])]
    return trusted


def test_verified_receipt_issues_secret_free_per_run_capability() -> None:
    trusted = _trusted_run()

    assert (
        require_trusted_gitlab_run_attestation(
            trusted,
            expected_run_id=RUN_ID,
            actor_subject=ACTOR,
            profile=PROFILE,
            token=TOKEN,
            fingerprint_key=FINGERPRINT_KEY,
            now_ms=NOW_MS + 2,
        )
        is trusted
    )
    serialized = json.dumps(asdict(trusted), sort_keys=True)
    assert TOKEN not in serialized
    assert ACTOR not in serialized
    assert PROFILE not in serialized
    assert "open_id" not in serialized


def test_forged_run_capability_is_rejected() -> None:
    receipt, _signer = _receipt()
    with pytest.raises(AttestationError):
        TrustedGitLabRunAttestation(
            run_id=RUN_ID,
            actor_subject_fp=str(receipt["actor_subject_fp"]),
            profile_fp=str(receipt["profile_fp"]),
            token_fp=str(receipt["token_fp"]),
            expires_at_ms=int(receipt["expires_at_ms"]),
        )

    forged = _trusted_run()
    object.__setattr__(forged, "run_id", "different-run")
    with pytest.raises(AttestationError):
        require_trusted_gitlab_run_attestation(
            forged,
            expected_run_id="different-run",
            actor_subject=ACTOR,
            profile=PROFILE,
            token=TOKEN,
            fingerprint_key=FINGERPRINT_KEY,
            now_ms=NOW_MS + 2,
        )


@pytest.mark.parametrize(
    "override",
    [
        {"expected_run_id": "different-run"},
        {"actor_subject": "different-actor"},
        {"profile": "different-profile"},
        {"token": "glpat-different"},
        {"now_ms": NOW_MS + 300_000},
    ],
)
def test_run_capability_rejects_wrong_context_or_expiry(
    override: dict[str, object],
) -> None:
    trusted = _trusted_run()
    arguments: dict[str, object] = {
        "expected_run_id": RUN_ID,
        "actor_subject": ACTOR,
        "profile": PROFILE,
        "token": TOKEN,
        "fingerprint_key": FINGERPRINT_KEY,
        "now_ms": NOW_MS + 2,
    }
    arguments.update(override)
    with pytest.raises(AttestationError):
        require_trusted_gitlab_run_attestation(trusted, **arguments)  # type: ignore[arg-type]


def test_issuer_cannot_skip_receipt_verification_or_nonce_consumption() -> None:
    receipt, signer = _receipt()
    receipt["run_id"] = "tampered"
    consumed: list[str] = []

    with pytest.raises(AttestationError):
        issue_trusted_gitlab_run_attestation(
            receipt,
            public_key=signer.public_key(),
            expected_audience=AUDIENCE,
            expected_run_id=RUN_ID,
            actor_subject=ACTOR,
            profile=PROFILE,
            token=TOKEN,
            expected_gitlab_user_id=7,
            fingerprint_key=FINGERPRINT_KEY,
            consume_nonce=lambda nonce, _expires: consumed.append(nonce) or True,
            now_ms=NOW_MS + 1,
        )
    assert consumed == []


def test_verified_receipt_is_the_only_path_to_a_runtime_eligible_vault_row(
    monkeypatch, tmp_path
) -> None:
    from types import SimpleNamespace

    from hermes_multitenancy.agent_real import _core
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.gitlab_owner_scope_attestation import store_attested_read_token
    from hermes_multitenancy.routing import RoutingTable
    from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    (shared / "profiles" / PROFILE).mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        "credentials:\n  - subject_id: kep-prd-skills\n    provider: gitlab\n"
        "    secret_kind: token\n    env: GITLAB_TOKEN\n"
        "    vault_profile: __self__\n    profiles: ['sunke']\n",
        encoding="utf-8",
    )
    table = RoutingTable(shared / "multitenancy.db")
    try:
        table.upsert(user_id="employee-a", profile_name=PROFILE, open_id="ou_actor")
    finally:
        table.close()
    principal = issue_webui_principal(
        profile_name=PROFILE,
        actor_subject="ou_actor",
        credential_subject="ou_actor",
    )
    signer = Ed25519PrivateKey.generate()
    receipt = create_attestation(
        _created(),
        expected_gitlab_user_id=7,
        get_current_user=lambda _token: {"id": 7},
        actor_subject="ou_actor",
        profile=PROFILE,
        run_id=RUN_ID,
        audience=AUDIENCE,
        private_key=signer,
        fingerprint_key=FINGERPRINT_KEY,
        now_ms=NOW_MS,
    )

    result = store_attested_read_token(
        principal=principal,
        token=TOKEN,
        receipt=receipt,
        expected_run_id=RUN_ID,
        expected_audience=AUDIENCE,
        expected_gitlab_user_id=7,
        public_key=signer.public_key(),
        fingerprint_key=FINGERPRINT_KEY,
        consume_nonce=lambda _nonce, _expires: True,
        shared_home=shared,
        now_ms=NOW_MS + 1,
    )

    assert result["scope_binding_verified"] is True
    store = CredentialStore(shared / "multitenancy.db")
    try:
        payload = store.get_secret_for_runtime(
            profile_name=PROFILE,
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
        )
        status = store.get_status(
            profile_name=PROFILE,
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            required_scopes=("read_api", "read_repository"),
        )
    finally:
        store.close()
    assert payload["owner_actor_subject"] == "ou_actor"
    assert payload["token_owner_verified"] is True
    assert status["scopes"] == ["read_api", "read_repository"]
    assert _core._codex_readonly_token_for_event(
        SimpleNamespace(trusted_runtime_principal=principal),
        shared / "profiles" / PROFILE,
    ) == TOKEN


def test_unsealed_principal_is_rejected_before_vault_write(monkeypatch, tmp_path) -> None:
    from hermes_multitenancy.gitlab_owner_scope_attestation import store_attested_read_token
    from hermes_multitenancy.trusted_runtime_principal import TrustedRuntimePrincipal

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    shared.mkdir()
    with pytest.raises(AttestationError):
        store_attested_read_token(
            principal=TrustedRuntimePrincipal(
                channel="webui",
                profile_name=PROFILE,
                actor_subject="ou_actor",
                credential_subject="ou_actor",
            ),
            token=TOKEN,
            receipt={},
            expected_run_id=RUN_ID,
            expected_audience=AUDIENCE,
            expected_gitlab_user_id=7,
            public_key=Ed25519PrivateKey.generate().public_key(),
            fingerprint_key=FINGERPRINT_KEY,
            consume_nonce=lambda _nonce, _expires: True,
            shared_home=shared,
            now_ms=NOW_MS,
        )
    assert not (shared / "multitenancy.db").exists()
