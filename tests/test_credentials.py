from __future__ import annotations

import json
from types import SimpleNamespace
import time

import pytest


def test_credential_store_status_is_profile_scoped_and_redacted(tmp_path):
    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(tmp_path / "multitenancy.db", encryption_key="test-key")
    expires_at = int(time.time() * 1000) + 3600_000
    store.put_credential(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
        payload={
            "access_token": "uat-access-secret",
            "refresh_token": "uat-refresh-secret",
        },
        scopes=["contact:user.base:readonly", "wiki:wiki:readonly"],
        expires_at=expires_at,
    )
    store.put_credential(
        profile_name="other",
        subject_id="ou_other",
        provider="feishu",
        secret_kind="uat",
        payload={"access_token": "other-secret"},
        scopes=["contact:user.base:readonly"],
        expires_at=expires_at,
    )

    status = store.get_status(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        required_scopes=["contact:user.base:readonly"],
    )

    assert status["profile_name"] == "owner"
    assert status["subject_id"] == "ou_owner"
    assert status["provider"] == "feishu"
    assert status["secret_kind"] == "uat"
    assert status["status"] == "valid"
    assert status["storage"] == "multitenancy_db"
    assert status["expires_at"] == expires_at
    assert status["has_payload"] is True
    assert status["scopes"] == ["contact:user.base:readonly", "wiki:wiki:readonly"]

    encoded = json.dumps(status, ensure_ascii=False)
    assert "access_token" not in encoded
    assert "refresh_token" not in encoded
    assert "uat-access-secret" not in encoded
    assert "uat-refresh-secret" not in encoded
    assert "other-secret" not in encoded


def test_credential_status_reports_scope_missing_and_expired(tmp_path):
    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(tmp_path / "multitenancy.db", encryption_key="test-key")
    store.put_credential(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
        payload={"access_token": "secret"},
        scopes=["contact:user.base:readonly"],
        expires_at=int(time.time() * 1000) + 3600_000,
    )
    store.put_credential(
        profile_name="owner",
        subject_id="ou_old",
        provider="feishu",
        secret_kind="uat",
        payload={"access_token": "old-secret"},
        scopes=["contact:user.base:readonly"],
        expires_at=int(time.time() * 1000) - 1,
    )

    missing_scope = store.get_status(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        required_scopes=["wiki:wiki:readonly"],
    )
    expired = store.get_status(
        profile_name="owner",
        subject_id="ou_old",
        provider="feishu",
    )

    assert missing_scope["status"] == "scope_missing"
    assert missing_scope["missing_scopes"] == ["wiki:wiki:readonly"]
    assert expired["status"] == "expired"


def test_credential_status_surfaces_refresh_token_expiry_from_payload(tmp_path):
    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(tmp_path / "multitenancy.db", encryption_key="test-key")
    expires_at = int(time.time() * 1000) + 3600_000
    refresh_expires_at = expires_at + 86_400_000
    store.put_credential(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
        payload={
            "access_token": "secret",
            "refresh_expires_at": refresh_expires_at,
        },
        scopes=[],
        expires_at=expires_at,
    )

    status = store.get_status(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
    )

    assert status["refresh_expires_at"] == refresh_expires_at


def test_credential_status_omits_refresh_token_expiry_when_missing(tmp_path):
    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(tmp_path / "multitenancy.db", encryption_key="test-key")
    store.put_credential(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
        payload={"access_token": "secret"},
        scopes=[],
        expires_at=int(time.time() * 1000) + 3600_000,
    )

    status = store.get_status(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
    )

    assert "refresh_expires_at" not in status


def test_runtime_secret_decrypts_only_for_exact_profile_subject_provider(tmp_path):
    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(tmp_path / "multitenancy.db", encryption_key="test-key")
    store.put_credential(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
        payload={"access_token": "uat-access-secret"},
        scopes=[],
        expires_at=None,
    )

    assert store.get_secret_for_runtime(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
    ) == {"access_token": "uat-access-secret"}

    with pytest.raises(PermissionError, match="credential not found"):
        store.get_secret_for_runtime(
            profile_name="other",
            subject_id="ou_owner",
            provider="feishu",
            secret_kind="uat",
        )


def test_payload_is_not_stored_as_plaintext_json(tmp_path):
    from hermes_multitenancy.credentials import CredentialStore

    db_path = tmp_path / "multitenancy.db"
    store = CredentialStore(db_path, encryption_key="test-key")
    store.put_credential(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
        payload={"access_token": "uat-access-secret"},
        scopes=[],
        expires_at=None,
    )

    raw_db = db_path.read_bytes()
    assert b"uat-access-secret" not in raw_db
    assert b"access_token" not in raw_db


def test_feishu_uat_broker_imports_profile_json_without_exposing_token(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.credentials import CredentialStore

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "owner"
    uat_dir = profile / "feishu_uat"
    uat_dir.mkdir(parents=True)
    expires_at = int(time.time() * 1000) + 3600_000
    (uat_dir / "ou_owner.json").write_text(
        json.dumps(
            {
                "access_token": "uat-access-secret",
                "refresh_token": "uat-refresh-secret",
                "user_open_id": "ou_owner",
                "expires_at": expires_at,
                "scopes": ["contact:user.base:readonly"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")

    def original_load_uat(open_id=None):
        with open(uat_dir / f"{open_id}.json", encoding="utf-8") as fh:
            return json.load(fh)

    fake_feishu = SimpleNamespace(
        FEISHU_UAT_PATH=profile / "feishu_uat.json",
        FEISHU_UAT_DIR=uat_dir,
        _ACCESS_TOKEN_EXPIRY_HEADROOM_S=60,
        _load_uat=original_load_uat,
    )

    agent_real._configure_feishu_uat_home(fake_feishu, profile)
    loaded = fake_feishu._load_uat("ou_owner")

    assert loaded["access_token"] == "uat-access-secret"
    status = CredentialStore(shared / "multitenancy.db", encryption_key="test-key").get_status(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
    )
    assert status["status"] == "valid"
    assert "uat-access-secret" not in json.dumps(status)


def test_feishu_uat_broker_prefers_db_payload_over_json(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.credentials import CredentialStore

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "owner"
    profile.mkdir(parents=True)
    expires_at = int(time.time() * 1000) + 3600_000
    CredentialStore(shared / "multitenancy.db", encryption_key="test-key").put_credential(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
        payload={"access_token": "db-token", "expires_at": expires_at, "user_open_id": "ou_owner"},
        scopes=[],
        expires_at=expires_at,
    )
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")

    def original_load_uat(open_id=None):
        raise AssertionError("JSON fallback should not be used when DB credential is valid")

    fake_feishu = SimpleNamespace(
        FEISHU_UAT_PATH=profile / "feishu_uat.json",
        FEISHU_UAT_DIR=profile / "feishu_uat",
        _ACCESS_TOKEN_EXPIRY_HEADROOM_S=60,
        _load_uat=original_load_uat,
    )

    agent_real._configure_feishu_uat_home(fake_feishu, profile)
    loaded = fake_feishu._load_uat("ou_owner")

    assert loaded["access_token"] == "db-token"


def test_feishu_app_broker_prefers_db_payload_without_env(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.credentials import CredentialStore

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "owner"
    profile.mkdir(parents=True)
    CredentialStore(shared / "multitenancy.db", encryption_key="test-key").put_credential(
        profile_name="__global__",
        subject_id="feishu_app",
        provider="feishu",
        secret_kind="app",
        payload={
            "app_id": "cli_from_db",
            "app_secret": "app-secret-from-db",
            "domain": "feishu",
        },
    )
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("FEISHU_DOMAIN", raising=False)

    def original_resolve():
        raise AssertionError("env/.env fallback should not be used when DB app credential is valid")

    fake_feishu = SimpleNamespace(
        FEISHU_UAT_PATH=profile / "feishu_uat.json",
        FEISHU_UAT_DIR=profile / "feishu_uat",
        _load_uat=lambda open_id=None: {},
        _resolve_feishu_credentials=original_resolve,
    )

    agent_real._configure_feishu_uat_home(fake_feishu, profile)

    assert fake_feishu._resolve_feishu_credentials() == (
        "cli_from_db",
        "app-secret-from-db",
        "feishu",
    )


def test_feishu_app_broker_falls_back_when_db_payload_missing(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "owner"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")

    fake_feishu = SimpleNamespace(
        FEISHU_UAT_PATH=profile / "feishu_uat.json",
        FEISHU_UAT_DIR=profile / "feishu_uat",
        _load_uat=lambda open_id=None: {},
        _resolve_feishu_credentials=lambda: ("cli_fallback", "fallback-secret", "feishu"),
    )

    agent_real._configure_feishu_uat_home(fake_feishu, profile)

    assert fake_feishu._resolve_feishu_credentials() == (
        "cli_fallback",
        "fallback-secret",
        "feishu",
    )
