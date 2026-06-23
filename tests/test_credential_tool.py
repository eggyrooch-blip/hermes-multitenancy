from __future__ import annotations

import json
import time


def test_credential_status_tool_returns_redacted_current_profile_status(monkeypatch, tmp_path):
    from hermes_multitenancy.credential_tool import credential_status
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.routing import RoutingTable

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "owner"
    profile.mkdir(parents=True)
    RoutingTable(shared / "multitenancy.db").upsert(
        user_id="owner",
        profile_name="owner",
        open_id="ou_owner",
    )
    store = CredentialStore(shared / "multitenancy.db", encryption_key="test-key")
    try:
        store.put_credential(
            profile_name="owner",
            subject_id="ou_owner",
            provider="feishu",
            secret_kind="uat",
            payload={"access_token": "uat-access-secret", "refresh_token": "uat-refresh-secret"},
            scopes=["contact:user.base:readonly"],
            expires_at=int(time.time() * 1000) + 3600_000,
        )
    finally:
        store.close()

    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_owner")
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")

    payload = json.loads(credential_status({"provider": "feishu"}))

    assert payload["profile"] == "owner"
    assert payload["provider"] == "feishu"
    assert payload["subject_id"] == "ou_owner"
    assert payload["status"] == "valid"
    assert payload["storage"] == "multitenancy_db"
    assert payload["sandbox_note"] == ".env/auth.json are masked by design"
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "uat-access-secret" not in encoded
    assert "uat-refresh-secret" not in encoded
    assert "access_token" not in encoded
    assert "refresh_token" not in encoded


def test_credential_status_tool_uses_profile_json_without_vault_key(monkeypatch, tmp_path):
    from hermes_multitenancy.credential_tool import credential_status
    from hermes_multitenancy.routing import RoutingTable

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "owner"
    uat_dir = profile / "feishu_uat"
    uat_dir.mkdir(parents=True)
    RoutingTable(shared / "multitenancy.db").upsert(
        user_id="owner",
        profile_name="owner",
        open_id="ou_owner",
    )
    now_ms = int(time.time() * 1000)
    (uat_dir / "ou_owner.json").write_text(
        json.dumps(
            {
                "access_token": "tool-profile-json-access-secret",
                "refresh_token": "tool-profile-json-refresh-secret",
                "scope": "contact:user.base:readonly offline_access",
                "expires_at": now_ms + 3600_000,
                "refresh_expires_at": now_ms + 30 * 24 * 3600_000,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    monkeypatch.setenv("HERMES_FEISHU_USER_OPEN_ID", "ou_owner")
    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)

    payload = json.loads(
        credential_status(
            {
                "provider": "feishu",
                "required_scopes": ["wiki:wiki:readonly"],
            }
        )
    )
    raw = json.dumps(payload, ensure_ascii=False)

    assert payload["profile"] == "owner"
    assert payload["provider"] == "feishu"
    assert payload["subject_id"] == "ou_owner"
    assert payload["status"] == "scope_missing"
    assert payload["storage"] == "profile_feishu_uat_json"
    assert payload["has_credential"] is True
    assert payload["missing_scopes"] == ["wiki:wiki:readonly"]
    assert "tool-profile-json-access-secret" not in raw
    assert "tool-profile-json-refresh-secret" not in raw
    assert "access_token" not in raw
    assert "refresh_token" not in raw


def test_credential_status_tool_rejects_cross_profile_query(monkeypatch, tmp_path):
    from hermes_multitenancy.credential_tool import credential_status

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "owner"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")

    payload = json.loads(credential_status({"profile": "other", "provider": "feishu"}))

    assert "error" in payload
    assert "current profile only" in payload["error"]


def test_register_adds_credential_status_tool_without_raw_secret_access(monkeypatch):
    import hermes_multitenancy

    calls = []

    class FakeCtx:
        def register_hook(self, name, cb):
            pass

        def register_tool(self, **kwargs):
            calls.append(kwargs)

    hermes_multitenancy.register(FakeCtx())

    tool = next(call for call in calls if call["name"] == "multitenancy_credential_status")
    assert tool["toolset"] == "multitenancy_diagnostics"
    schema = tool["schema"]
    assert "profile" not in schema["parameters"]["properties"]
    assert "secret" not in json.dumps(schema).lower()
    assert "token" not in json.dumps(schema).lower()
