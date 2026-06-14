from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from hermes_multitenancy import agent_real


def _make_profile(tmp_path: Path, name: str = "owner") -> tuple[Path, Path]:
    profile_home = tmp_path / "profiles" / name
    profile_home.mkdir(parents=True)
    approval_dir = tmp_path / "approvals"
    approval_dir.mkdir()
    return profile_home, approval_dir


def _store_uat(shared_home: Path, *, profile_name: str, open_id: str, access_token: str) -> None:
    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(shared_home / "multitenancy.db", encryption_key="test-key")
    try:
        store.put_credential(
            profile_name=profile_name,
            subject_id=open_id,
            provider="feishu",
            secret_kind="uat",
            payload={
                "access_token": access_token,
                "refresh_token": f"{access_token}-refresh",
            },
            expires_at=int(time.time() * 1000) + 3600_000,
        )
    finally:
        store.close()


def test_build_subprocess_env_strict_on_strips_vault_keys_and_keeps_broker_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_home, approval_dir = _make_profile(tmp_path)
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.setenv("HERMES_CREDENTIAL_KEY", "test-key-legacy")
    monkeypatch.setenv("HERMES_MULTITENANCY_CRED_BROKER_TOKEN", "per-run-bearer")
    monkeypatch.setenv("HERMES_MULTITENANCY_CRED_BROKER_URL", "http://127.0.0.1:8766")
    monkeypatch.setenv("HERMES_MULTITENANCY_CRED_LEASE", "lease-token")
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_ID", "run-123")

    env = agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)

    assert "HERMES_MULTITENANCY_CREDENTIAL_KEY" not in env
    assert "HERMES_CREDENTIAL_KEY" not in env
    assert env["HERMES_MULTITENANCY_CRED_BROKER_TOKEN"] == "per-run-bearer"


def test_build_subprocess_env_strict_off_keeps_vault_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_home, approval_dir = _make_profile(tmp_path)
    monkeypatch.delenv("HERMES_MULTITENANCY_STRICT_CONTEXT", raising=False)
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")

    env = agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)

    assert env["HERMES_MULTITENANCY_CREDENTIAL_KEY"] == "test-key"


def test_assert_lease_binding_rejects_mismatched_subject() -> None:
    from hermes_multitenancy.credential_broker import (
        LeaseError,
        assert_lease_binding,
        mint_lease,
        verify_lease,
    )

    lease = mint_lease(
        profile_name="owner",
        open_id="ou_owner",
        run_id="run-1",
        secret=b"lease-secret",
        ttl_seconds=60,
    )
    claims = verify_lease(lease, secret=b"lease-secret")

    with pytest.raises(LeaseError):
        assert_lease_binding(
            claims,
            profile_name="other",
            open_id="ou_other",
            run_id="run-1",
        )


def test_verify_lease_rejects_bad_signature() -> None:
    from hermes_multitenancy.credential_broker import LeaseError, mint_lease, verify_lease

    lease = mint_lease(
        profile_name="owner",
        open_id="ou_owner",
        run_id="run-1",
        secret=b"lease-secret",
        ttl_seconds=60,
    )
    tampered = f"{lease}x"

    with pytest.raises(LeaseError):
        verify_lease(tampered, secret=b"lease-secret")


def test_verify_lease_rejects_missing_signature() -> None:
    from hermes_multitenancy.credential_broker import LeaseError, verify_lease

    with pytest.raises(LeaseError):
        verify_lease("v1:payload-without-signature", secret=b"lease-secret")


def test_verify_lease_rejects_expired() -> None:
    from hermes_multitenancy.credential_broker import LeaseError, mint_lease, verify_lease

    lease = mint_lease(
        profile_name="owner",
        open_id="ou_owner",
        run_id="run-1",
        secret=b"lease-secret",
        ttl_seconds=-1,
    )

    with pytest.raises(LeaseError):
        verify_lease(lease, secret=b"lease-secret")


def test_credential_lease_route_rejects_mismatched_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.credential_broker import lease_signing_secret, mint_lease
    from hermes_multitenancy.webui_broker_server import (
        create_run_broker_app,
        register_credential_broker_token,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared_home = tmp_path / ".hermes"
    shared_home.mkdir(parents=True)
    (shared_home / "profiles" / "owner").mkdir(parents=True)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    _store_uat(shared_home, profile_name="owner", open_id="ou_owner", access_token="uat-secret")

    async def runner() -> None:
        app = create_run_broker_app()
        register_credential_broker_token(
            token="per-run-bearer",
            profile_name="owner",
            open_id="ou_owner",
            run_id="run-1",
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            lease = mint_lease(
                profile_name="owner",
                open_id="ou_owner",
                run_id="run-1",
                secret=lease_signing_secret(),
                ttl_seconds=60,
            )
            response = await client.post(
                "/api/run-broker/credentials/lease",
                headers={"Authorization": "Bearer per-run-bearer"},
                json={
                    "lease": lease,
                    "kind": "feishu_uat",
                    "profile_name": "owner",
                    "open_id": "ou_other",
                    "run_id": "run-1",
                },
            )
            body = await response.json()
        finally:
            await client.close()

        assert response.status == 403
        assert body["error"] == "forbidden"

    asyncio.run(runner())


def test_strict_child_broker_unreachable_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hermes_multitenancy import feishu_uat_auth

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setenv("HERMES_MULTITENANCY_CRED_BROKER_TOKEN", "per-run-bearer")
    monkeypatch.setenv("HERMES_MULTITENANCY_CRED_BROKER_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("HERMES_MULTITENANCY_CRED_LEASE", "lease-token")
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_ID", "run-1")
    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)

    def _fail(*_args, **_kwargs):
        raise RuntimeError("local vault fallback should not run")

    monkeypatch.setattr(feishu_uat_auth, "CredentialStore", _fail)

    result = feishu_uat_auth._load_vault_uat_payload(
        tmp_path / ".hermes",
        "owner",
        "ou_owner",
    )

    assert result is None


def test_provider_env_for_aiagent_strict_child_broker_unreachable_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hermes_multitenancy import provider_adapter

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setenv("HERMES_MULTITENANCY_CRED_BROKER_TOKEN", "per-run-bearer")
    monkeypatch.setenv("HERMES_MULTITENANCY_CRED_BROKER_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("HERMES_MULTITENANCY_CRED_LEASE", "lease-token")
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_ID", "run-1")
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_owner")
    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)

    result = provider_adapter.provider_env_for_aiagent(tmp_path / "profiles" / "owner")

    assert result == {}


def test_lark_cli_auth_broker_strict_child_disables_json_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hermes_multitenancy.lark_cli_auth_broker import (
        LarkCliAuthBroker,
        LarkCliAuthBrokerContext,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setenv("HERMES_MULTITENANCY_CRED_BROKER_TOKEN", "per-run-bearer")
    monkeypatch.setenv("HERMES_MULTITENANCY_CRED_BROKER_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("HERMES_MULTITENANCY_CRED_LEASE", "lease-token")
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_ID", "run-1")
    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)

    broker = LarkCliAuthBroker(
        LarkCliAuthBrokerContext(
            shared_home=tmp_path / ".hermes",
            profile_name="owner",
            user_open_id="ou_owner",
            hmac_key="test-key",
        )
    )

    with pytest.raises(PermissionError, match="credential unavailable"):
        broker._resolve_user_payload(object(), json_payload={"access_token": "json-fallback"})


def test_credential_status_never_returns_raw_payload(tmp_path: Path) -> None:
    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(tmp_path / "multitenancy.db", encryption_key="test-key")
    try:
        store.put_credential(
            profile_name="owner",
            subject_id="ou_owner",
            provider="feishu",
            secret_kind="uat",
            payload={
                "access_token": "uat-secret",
                "refresh_token": "refresh-secret",
            },
        )
        status = store.get_status(
            profile_name="owner",
            subject_id="ou_owner",
            provider="feishu",
        )
    finally:
        store.close()

    assert "access_token" not in status
    assert "refresh_token" not in status
    assert "encrypted_payload" not in status
    encoded = json.dumps(status, ensure_ascii=False)
    assert "uat-secret" not in encoded
    assert "refresh-secret" not in encoded
