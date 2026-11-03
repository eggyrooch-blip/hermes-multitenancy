import json
from pathlib import Path

import pytest


def test_connector_credentials_reuse_owner_scoped_store_and_fail_closed(tmp_path: Path):
    from hermes_multitenancy.connector_credential_schema import (
        credential_schemas,
        resolve_connector_credential,
        revoke_connector_credential,
        store_connector_credential,
    )
    from hermes_multitenancy.credentials import CredentialStore

    source = tmp_path / "source.jsonl"
    source.write_text(
        json.dumps(
            {
                "product": "TRAE",
                "catalog_id": "service",
                "transport": "stdio",
                "auth_mode": "config_keys",
                "credential_key_names": ["API_TOKEN", "REGION"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    [schema] = credential_schemas(source)
    assert schema["fields"] == ["API_TOKEN", "REGION"]
    assert schema["storage"] == "multitenancy_credentials"
    assert schema["binding"] == ["profile_name", "subject_id", "provider", "secret_kind"]

    store = CredentialStore(tmp_path / "multitenancy.db", encryption_key="test-key")
    try:
        store_connector_credential(
            store,
            schema,
            profile_name="alice",
            subject_id="subject-alice",
            fields={"API_TOKEN": "alice-token", "REGION": "cn"},
        )
        assert resolve_connector_credential(
            store,
            schema,
            profile_name="alice",
            subject_id="subject-alice",
        )["API_TOKEN"] == "alice-token"
        with pytest.raises(PermissionError):
            resolve_connector_credential(
                store,
                schema,
                profile_name="bob",
                subject_id="subject-bob",
            )

        store.put_credential(
            profile_name="alice",
            subject_id="subject-alice",
            provider=schema["provider"],
            secret_kind=schema["secret_kind"],
            payload={
                "owner_profile": "bob",
                "owner_subject": "subject-bob",
                "fields": {"API_TOKEN": "wrong-owner", "REGION": "cn"},
            },
        )
        with pytest.raises(PermissionError, match="binding"):
            resolve_connector_credential(
                store,
                schema,
                profile_name="alice",
                subject_id="subject-alice",
            )
        assert revoke_connector_credential(
            store,
            schema,
            profile_name="alice",
            subject_id="subject-alice",
        )
        with pytest.raises(PermissionError):
            resolve_connector_credential(
                store,
                schema,
                profile_name="alice",
                subject_id="subject-alice",
            )
    finally:
        store.close()
