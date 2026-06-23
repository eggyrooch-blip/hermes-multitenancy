from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest


@pytest.fixture(autouse=True)
def isolated_lark_cli_app_id(monkeypatch):
    monkeypatch.delenv("HERMES_LARK_CLI_APP_ID", raising=False)


def test_preflight_reports_missing_credentials_without_secret_values(tmp_path: Path):
    from hermes_multitenancy.lark_cli_canary import lark_cli_canary_preflight

    binary = tmp_path / "bin" / "lark-cli-authsidecar"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    result = lark_cli_canary_preflight(
        shared_home=tmp_path / ".hermes",
        profile_name="alice",
        open_id="ou_alice",
        binary_path=binary,
    )

    assert result["ready"] is False
    assert "feishu_app_credential" in result["missing"]
    assert "user_uat_credential" in result["missing"]
    assert "access_token" not in str(result)
    assert "refresh_token" not in str(result)
    assert "app_secret" not in str(result)


def test_preflight_ready_when_binary_app_and_uat_exist(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.lark_cli_canary import lark_cli_canary_preflight

    shared = tmp_path / ".hermes"
    binary = shared / "bin" / "lark-cli-authsidecar"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    store = CredentialStore(shared / "multitenancy.db", encryption_key="test-key")
    try:
        store.put_credential(
            profile_name="__global__",
            subject_id="feishu_app",
            provider="feishu",
            secret_kind="app",
            payload={"app_id": "cli_public", "app_secret": "secret"},
        )
        store.put_credential(
            profile_name="alice",
            subject_id="ou_alice",
            provider="feishu",
            secret_kind="uat",
            payload={"access_token": "uat-secret", "refresh_token": "refresh-secret"},
        )
    finally:
        store.close()

    result = lark_cli_canary_preflight(
        shared_home=shared,
        profile_name="alice",
        open_id="ou_alice",
        binary_path=binary,
    )

    assert result["ready"] is True
    assert result["missing"] == []
    assert result["canary"]["mode"] == "api"
    assert result["canary"]["argv"] == ["api", "GET", "/open-apis/authen/v1/user_info"]
    assert result["canary"]["identity"] == "user"
    assert "uat-secret" not in str(result)
    assert "refresh-secret" not in str(result)
    assert "secret" not in str(result).replace("secret_free", "")


def test_preflight_does_not_trust_db_uat_status_without_runtime_payload(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.lark_cli_canary import lark_cli_canary_preflight

    shared = tmp_path / ".hermes"
    binary = shared / "bin" / "lark-cli-authsidecar"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("HERMES_LARK_CLI_APP_ID", "cli_public")
    store = CredentialStore(shared / "multitenancy.db", encryption_key="correct-key")
    try:
        store.put_credential(
            profile_name="alice",
            subject_id="ou_alice",
            provider="feishu",
            secret_kind="uat",
            payload={"access_token": "vault-uat-secret", "refresh_token": "vault-refresh-secret"},
        )
    finally:
        store.close()
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "wrong-key")
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)

    result = lark_cli_canary_preflight(
        shared_home=shared,
        profile_name="alice",
        open_id="ou_alice",
        binary_path=binary,
    )

    uat_check = next(check for check in result["checks"] if check["name"] == "user_uat_credential")
    assert result["ready"] is False
    assert "user_uat_credential" in result["missing"]
    assert uat_check["ok"] is False
    assert uat_check["source"] == "multitenancy_db"
    assert "vault-uat-secret" not in str(result)
    assert "vault-refresh-secret" not in str(result)


def test_preflight_ready_with_profile_json_and_app_id_without_vault_key(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.lark_cli_canary import lark_cli_canary_preflight

    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)
    monkeypatch.setenv("HERMES_LARK_CLI_APP_ID", "cli_public")
    shared = tmp_path / ".hermes"
    binary = shared / "bin" / "lark-cli-authsidecar"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    sqlite3.connect(shared / "multitenancy.db").close()
    uat_dir = shared / "profiles" / "alice" / "feishu_uat"
    uat_dir.mkdir(parents=True)
    (uat_dir / "ou_alice.json").write_text(
        json.dumps(
            {
                "access_token": "profile-json-uat-secret",
                "refresh_token": "profile-json-refresh-secret",
                "expires_at": 4102444800000,
                "scope": "offline_access contact:user.base:readonly",
            }
        ),
        encoding="utf-8",
    )

    result = lark_cli_canary_preflight(
        shared_home=shared,
        profile_name="alice",
        open_id="ou_alice",
        binary_path=binary,
    )

    assert result["ready"] is True
    assert result["missing"] == []
    app_check = next(check for check in result["checks"] if check["name"] == "feishu_app_credential")
    uat_check = next(check for check in result["checks"] if check["name"] == "user_uat_credential")
    assert app_check["source"] == "env_or_config"
    assert uat_check["source"] == "profile_feishu_uat_json"
    assert "credential_vault_key" not in result["missing"]
    assert "profile-json-uat-secret" not in str(result)
    assert "profile-json-refresh-secret" not in str(result)


@pytest.mark.parametrize("expiry_key", ["expire_at", "access_token_expires_at"])
def test_preflight_treats_profile_json_expiry_aliases_as_expired(monkeypatch, tmp_path: Path, expiry_key: str):
    from hermes_multitenancy.lark_cli_canary import lark_cli_canary_preflight

    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)
    monkeypatch.setenv("HERMES_LARK_CLI_APP_ID", "cli_public")
    shared = tmp_path / ".hermes"
    binary = shared / "bin" / "lark-cli-authsidecar"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    sqlite3.connect(shared / "multitenancy.db").close()
    uat_dir = shared / "profiles" / "alice" / "feishu_uat"
    uat_dir.mkdir(parents=True)
    expired_at = 1
    (uat_dir / "ou_alice.json").write_text(
        json.dumps(
            {
                "access_token": "profile-json-uat-secret",
                "refresh_token": "profile-json-refresh-secret",
                expiry_key: expired_at,
                "scope": "offline_access contact:user.base:readonly",
            }
        ),
        encoding="utf-8",
    )

    result = lark_cli_canary_preflight(
        shared_home=shared,
        profile_name="alice",
        open_id="ou_alice",
        binary_path=binary,
    )

    uat_check = next(check for check in result["checks"] if check["name"] == "user_uat_credential")
    assert result["ready"] is False
    assert "user_uat_credential" in result["missing"]
    assert uat_check["ok"] is False
    assert uat_check["status"] == "expired"
    assert uat_check["expires_at"] == expired_at
    assert uat_check["source"] == "profile_feishu_uat_json"
    assert "profile-json-uat-secret" not in str(result)
    assert "profile-json-refresh-secret" not in str(result)


def test_preflight_reports_missing_vault_key_without_traceback(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.lark_cli_canary import lark_cli_canary_preflight

    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    shared = tmp_path / ".hermes"
    shared.mkdir()
    sqlite3.connect(shared / "multitenancy.db").close()
    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)

    result = lark_cli_canary_preflight(
        shared_home=shared,
        profile_name="alice",
        open_id="ou_alice",
        binary_path=tmp_path / "missing-bin",
    )

    assert "credential_vault_key" in result["missing"]
    assert result["checks"][-1]["reason"] == "credential encryption key is required"


def test_import_legacy_uat_to_vault_is_secret_free(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.lark_cli_canary import (
        import_legacy_uat_to_vault,
        lark_cli_canary_preflight,
    )

    shared = tmp_path / ".hermes"
    uat_dir = shared / "feishu_uat"
    uat_dir.mkdir(parents=True)
    (uat_dir / "ou_alice.json").write_text(
        json.dumps(
            {
                "access_token": "uat-secret",
                "refresh_token": "refresh-secret",
                "scope": "contact:user.base:readonly",
                "expires_at": 4102444800000,
            }
        ),
        encoding="utf-8",
    )
    binary = shared / "bin" / "lark-cli-authsidecar"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")

    result = import_legacy_uat_to_vault(
        shared_home=shared,
        profile_name="alice",
        open_id="ou_alice",
        legacy_uat_dir=uat_dir,
    )

    assert result["imported"] is True
    assert result["profile_name"] == "alice"
    assert result["open_id"] == "ou_alice"
    assert "uat-secret" not in str(result)
    assert "refresh-secret" not in str(result)

    preflight = lark_cli_canary_preflight(
        shared_home=shared,
        profile_name="alice",
        open_id="ou_alice",
        binary_path=binary,
    )
    assert "user_uat_credential" not in preflight["missing"]
    assert "feishu_app_credential" in preflight["missing"]


def test_import_feishu_app_config_to_vault_is_secret_free(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.lark_cli_canary import (
        import_feishu_app_config_to_vault,
        lark_cli_canary_preflight,
    )

    shared = tmp_path / ".hermes"
    config = shared / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
platforms:
  feishu:
    extra:
      app_id: cli_public
      app_secret: app-secret
      domain: feishu
""",
        encoding="utf-8",
    )
    binary = shared / "bin" / "lark-cli-authsidecar"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")

    result = import_feishu_app_config_to_vault(shared_home=shared, config_path=config)

    assert result["imported"] is True
    assert result["app_id"] == "cli_public"
    assert result["domain"] == "feishu"
    assert "app-secret" not in str(result)

    preflight = lark_cli_canary_preflight(
        shared_home=shared,
        profile_name="alice",
        open_id="ou_alice",
        binary_path=binary,
    )
    assert "feishu_app_credential" not in preflight["missing"]
    assert "user_uat_credential" in preflight["missing"]


def test_canary_cli_preserves_direct_preflight_form(capsys, tmp_path: Path):
    from hermes_multitenancy.lark_cli_canary import main

    code = main([
        "--shared-home",
        str(tmp_path / ".hermes"),
        "--profile",
        "alice",
        "--open-id",
        "ou_alice",
    ])
    output = capsys.readouterr().out

    assert code == 2
    assert '"ready": false' in output


def test_canary_cli_import_legacy_uat_subcommand(monkeypatch, capsys, tmp_path: Path):
    from hermes_multitenancy.lark_cli_canary import main

    shared = tmp_path / ".hermes"
    uat_dir = shared / "feishu_uat"
    uat_dir.mkdir(parents=True)
    (uat_dir / "ou_alice.json").write_text('{"access_token":"uat-secret"}', encoding="utf-8")
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")

    code = main([
        "import-legacy-uat",
        "--shared-home",
        str(shared),
        "--profile",
        "alice",
        "--open-id",
        "ou_alice",
    ])
    output = capsys.readouterr().out

    assert code == 0
    assert '"imported": true' in output
    assert "uat-secret" not in output


def test_canary_cli_import_app_config_subcommand(monkeypatch, capsys, tmp_path: Path):
    from hermes_multitenancy.lark_cli_canary import main

    shared = tmp_path / ".hermes"
    shared.mkdir(parents=True)
    (shared / "config.yaml").write_text(
        "platforms:\n  feishu:\n    extra:\n      app_id: cli_public\n      app_secret: app-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")

    code = main([
        "import-app-config",
        "--shared-home",
        str(shared),
    ])
    output = capsys.readouterr().out

    assert code == 0
    assert '"imported": true' in output
    assert '"app_id": "cli_public"' in output
    assert "app-secret" not in output
