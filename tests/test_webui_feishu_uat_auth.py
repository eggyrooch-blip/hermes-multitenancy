from __future__ import annotations

import asyncio
import errno
import io
import json
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse

import pytest


def _prepare_shared_home(tmp_path, monkeypatch):
    shared = tmp_path / ".hermes"
    router_home = shared / "profiles" / "multitenancy_router"
    router_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(router_home))
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.routing import RoutingTable

    routing_db = shared / "multitenancy.db"
    table = RoutingTable(routing_db)
    table.upsert(user_id="owner", profile_name="owner", open_id="ou_owner", provenance="sync")
    table.close()
    # The autouse conftest fixture pins routing to an empty ":memory:" table. Point the
    # owner-scoped broker resolver at this seeded db so ou_owner→owner verifies; the same
    # fixture resets the override back to ":memory:" after the test.
    router_mod.override_routing_table(routing_db)
    return shared


def _add_pending_auth_session(feishu_uat_auth, *, session_id: str, device_code: str):
    session = feishu_uat_auth.FeishuAuthSession(
        session_id=session_id,
        profile_name="owner",
        open_id="ou_owner",
        device_code=device_code,
        user_code="TEST-1234",
        verification_uri="https://accounts.feishu.cn/device?user_code=TEST-1234",
        scope="wiki:wiki:readonly offline_access",
        client_id="cli_test",
        client_secret="secret",
        expires_at=int(time.time()) + 600,
        interval=1,
    )
    feishu_uat_auth._sessions[session_id] = session
    return session


def test_webui_feishu_uat_status_is_route_scoped_and_redacted(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    CredentialStore(shared / "multitenancy.db").put_credential(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
        payload={"access_token": "raw-access", "refresh_token": "raw-refresh"},
        scopes=["contact:user.base:readonly"],
        expires_at=int(time.time() * 1000) + 3600_000,
    )

    async def runner():
        app = create_run_broker_app(
            dispatch_agent=lambda request: "",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get(
                "/api/run-broker/credentials/feishu/uat/status",
                headers={
                    "X-Hermes-Profile": "owner",
                    "X-Hermes-User-Key": "ou_owner",
                },
                params={"required_scopes": "wiki:wiki:readonly"},
            )
            wrong_route = await client.get(
                "/api/run-broker/credentials/feishu/uat/status",
                headers={
                    "X-Hermes-Profile": "other",
                    "X-Hermes-User-Key": "ou_owner",
                },
            )
            body = await response.json()
            raw = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert body["status"] == "scope_missing"
        assert body["profile_name"] == "owner"
        assert body["subject_id"] == "ou_owner"
        assert body["missing_scopes"] == ["wiki:wiki:readonly"]
        assert "raw-access" not in raw
        assert "raw-refresh" not in raw
        assert wrong_route.status == 403

    asyncio.run(runner())


def test_feishu_uat_status_reports_lark_cli_bot_fallback(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    shared_bin = shared / "bin"
    shared_bin.mkdir(parents=True)
    lark_cli = shared_bin / "lark-cli-authsidecar"
    lark_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    lark_cli.chmod(0o755)
    monkeypatch.setenv("HERMES_LARK_CLI_APP_ID", "cli_public")

    status = feishu_uat_auth.credential_status(
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )

    assert status["status"] == "missing"
    assert status["lark_cli"] == {
        "available": True,
        "default_identity": "bot",
    }


def test_feishu_uat_status_uses_profile_json_without_vault_key(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)
    now_ms = int(time.time() * 1000)
    profile_uat = shared / "profiles" / "owner" / "feishu_uat"
    profile_uat.mkdir(parents=True)
    (profile_uat / "ou_owner.json").write_text(
        json.dumps(
            {
                "access_token": "profile-json-access-secret",
                "refresh_token": "profile-json-refresh-secret",
                "user_open_id": "ou_owner",
                "scope": "contact:user.base:readonly offline_access",
                "expires_at": now_ms + 3600_000,
                "refresh_expires_at": now_ms + 30 * 24 * 3600_000,
            }
        ),
        encoding="utf-8",
    )

    status = feishu_uat_auth.credential_status(
        profile_name="owner",
        open_id="ou_owner",
        required_scopes="wiki:wiki:readonly",
        shared_home=shared,
    )
    raw = json.dumps(status, ensure_ascii=False)

    assert status["status"] == "scope_missing"
    assert status["storage"] == "profile_feishu_uat_json"
    assert status["has_payload"] is True
    assert status["runtime_available"] is False
    assert status["missing_scopes"] == ["wiki:wiki:readonly"]
    assert status["refresh_expires_at"] == now_ms + 30 * 24 * 3600_000
    assert "profile-json-access-secret" not in raw
    assert "profile-json-refresh-secret" not in raw
    assert "access_token" not in raw
    assert "refresh_token" not in raw


def test_feishu_uat_status_prefers_runtime_profile_json_over_keyless_vault_metadata(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.credentials import CredentialStore

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    now_ms = int(time.time() * 1000)
    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="owner",
            subject_id="ou_owner",
            provider="feishu",
            secret_kind="uat",
            payload={
                "access_token": "vault-access-secret",
                "refresh_token": "vault-refresh-secret",
                "scope": "contact:user.base:readonly offline_access",
                "expires_at": now_ms + 3600_000,
                "refresh_expires_at": now_ms + 30 * 24 * 3600_000,
            },
            scopes=["contact:user.base:readonly", "offline_access"],
            expires_at=now_ms + 3600_000,
        )
    finally:
        store.close()

    profile_uat = shared / "profiles" / "owner" / "feishu_uat"
    profile_uat.mkdir(parents=True)
    (profile_uat / "ou_owner.json").write_text(
        json.dumps(
            {
                "access_token": "expired-profile-access-secret",
                "scope": "contact:user.base:readonly offline_access",
                "expires_at": now_ms - 1000,
                "refresh_expires_at": now_ms - 1000,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)
    monkeypatch.setattr(feishu_uat_auth, "refresh_uat_if_needed", lambda **_kwargs: None)

    status = feishu_uat_auth.credential_status(
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )
    raw = json.dumps(status, ensure_ascii=False)

    assert status["status"] == "expired"
    assert status["storage"] == "profile_feishu_uat_json"
    assert status["runtime_available"] is False
    assert "vault-access-secret" not in raw
    assert "vault-refresh-secret" not in raw
    assert "expired-profile-access-secret" not in raw
    assert "access_token" not in raw
    assert "refresh_token" not in raw


def test_feishu_uat_status_does_not_mark_reauth_when_keyless_refresh_needs_app_creds(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.credentials import CredentialStore

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    now_ms = int(time.time() * 1000)
    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="__global__",
            subject_id="feishu_app",
            provider="feishu",
            secret_kind="app",
            payload={"app_id": "cli_test", "app_secret": "vault-app-secret"},
        )
    finally:
        store.close()

    profile_uat = shared / "profiles" / "owner" / "feishu_uat"
    profile_uat.mkdir(parents=True)
    (profile_uat / "ou_owner.json").write_text(
        json.dumps(
            {
                "access_token": "expired-profile-access-secret",
                "refresh_token": "profile-refresh-secret",
                "user_open_id": "ou_owner",
                "scope": "offline_access wiki:wiki:readonly",
                "expires_at": now_ms - 1000,
                "refresh_expires_at": now_ms + 86400_000,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    status = feishu_uat_auth.credential_status(
        profile_name="owner",
        open_id="ou_owner",
        required_scopes="wiki:wiki:readonly",
        shared_home=shared,
    )
    raw = json.dumps(status, ensure_ascii=False)

    assert status["status"] == "expired"
    assert status["storage"] == "profile_feishu_uat_json"
    assert status["runtime_available"] is False
    assert status["needs_reauth"] is False
    assert "credential encryption key is required" not in raw
    assert "expired-profile-access-secret" not in raw
    assert "profile-refresh-secret" not in raw
    assert "vault-app-secret" not in raw
    assert "access_token" not in raw
    assert "refresh_token" not in raw


def test_feishu_uat_status_prefers_valid_runtime_vault_over_fresher_scope_missing_json(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.credentials import CredentialStore

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    now_ms = int(time.time() * 1000)
    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="owner",
            subject_id="ou_owner",
            provider="feishu",
            secret_kind="uat",
            payload={
                "access_token": "vault-access-secret",
                "refresh_token": "vault-refresh-secret",
                "scope": "contact:user.base:readonly offline_access wiki:wiki:readonly",
                "expires_at": now_ms + 3600_000,
                "refresh_expires_at": now_ms + 30 * 24 * 3600_000,
            },
            scopes=["contact:user.base:readonly", "offline_access", "wiki:wiki:readonly"],
            expires_at=now_ms + 3600_000,
        )
    finally:
        store.close()

    profile_uat = shared / "profiles" / "owner" / "feishu_uat"
    profile_uat.mkdir(parents=True)
    (profile_uat / "ou_owner.json").write_text(
        json.dumps(
            {
                "access_token": "fresher-profile-access-secret",
                "refresh_token": "fresher-profile-refresh-secret",
                "scope": "contact:user.base:readonly offline_access",
                "expires_at": now_ms + 7200_000,
                "refresh_expires_at": now_ms + 31 * 24 * 3600_000,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(feishu_uat_auth, "refresh_uat_if_needed", lambda **_kwargs: None)

    status = feishu_uat_auth.credential_status(
        profile_name="owner",
        open_id="ou_owner",
        required_scopes=["wiki:wiki:readonly"],
        shared_home=shared,
    )
    raw = json.dumps(status, ensure_ascii=False)

    assert status["status"] == "valid"
    assert status["storage"] == "multitenancy_db"
    assert status["runtime_available"] is True
    assert status["missing_scopes"] == []
    assert "vault-access-secret" not in raw
    assert "vault-refresh-secret" not in raw
    assert "fresher-profile-access-secret" not in raw
    assert "fresher-profile-refresh-secret" not in raw
    assert "access_token" not in raw
    assert "refresh_token" not in raw


def test_feishu_uat_status_does_not_use_provider_fallback(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.credentials import CredentialStore

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    (shared / "provider-adapter.yaml").write_text("enabled: true\n", encoding="utf-8")
    store = CredentialStore(shared / "multitenancy.db")
    expires_at = int(time.time() * 1000) + 3600_000
    store.put_credential(
        profile_name="__org__",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
        payload={"access_token": "org-uat-token", "refresh_token": "org-refresh-token"},
        expires_at=expires_at,
    )
    store.put_credential(
        profile_name="owner",
        subject_id="ou_other",
        provider="feishu",
        secret_kind="uat",
        payload={"access_token": "other-uat-token", "refresh_token": "other-refresh-token"},
        expires_at=expires_at,
    )
    store.close()

    status = feishu_uat_auth.credential_status(
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )
    raw = json.dumps(status, ensure_ascii=False)

    assert status["status"] == "missing"
    assert status["profile_name"] == "owner"
    assert status["subject_id"] == "ou_owner"
    assert "org-uat-token" not in raw
    assert "other-uat-token" not in raw


def test_feishu_uat_auth_loads_shared_env_for_router_process(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.credentials import CredentialStore

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    CredentialStore(shared / "multitenancy.db").put_credential(
        profile_name="__global__",
        subject_id="feishu_app",
        provider="feishu",
        secret_kind="app",
        payload={"app_id": "cli_from_vault", "app_secret": "secret_from_vault"},
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    (shared / ".env").write_text(
        "HERMES_MULTITENANCY_CREDENTIAL_KEY=test-key\n"
        "FEISHU_APP_ID=cli_from_env\n"
        "FEISHU_APP_SECRET=secret_from_env\n",
        encoding="utf-8",
    )

    assert feishu_uat_auth._feishu_app_credentials(shared) == ("cli_from_env", "secret_from_env")


def test_refresh_access_token_uses_v2_oauth_endpoint_without_tenant_token(monkeypatch):
    from hermes_multitenancy import feishu_uat_auth

    def fail_tat(*_args, **_kwargs):
        raise AssertionError("refresh_token flow must not mint a tenant access token")

    monkeypatch.setattr(feishu_uat_auth, "_mint_tenant_access_token", fail_tat)
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "access_token": "fresh-access",
                    "refresh_token": "fresh-refresh",
                    "expires_in": 7200,
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout=10):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["method"] = request.get_method()
        seen["headers"] = dict(request.header_items())
        seen["body"] = urllib.parse.parse_qs(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(feishu_uat_auth.urllib.request, "urlopen", fake_urlopen)

    result = feishu_uat_auth._refresh_access_token("cli_test", "app-secret", "old-refresh", timeout=7)

    assert seen["url"] == "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
    assert seen["timeout"] == 7
    assert seen["method"] == "POST"
    assert seen["body"] == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["old-refresh"],
        "client_id": ["cli_test"],
        "client_secret": ["app-secret"],
    }
    assert "Authorization" not in seen["headers"]
    assert result["access_token"] == "fresh-access"


def test_feishu_uat_status_refreshes_expired_token_and_mirrors_json(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.credentials import CredentialStore

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    now_ms = int(time.time() * 1000)
    expired_payload = {
        "app_id": "cli_test",
        "user_open_id": "ou_owner",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": now_ms - 1000,
        "refresh_expires_at": now_ms + 86400_000,
        "scope": "offline_access wiki:wiki:readonly",
        "granted_at": now_ms - 7200_000,
    }
    CredentialStore(shared / "multitenancy.db").put_credential(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
        payload=expired_payload,
        scopes=["offline_access", "wiki:wiki:readonly"],
        expires_at=expired_payload["expires_at"],
    )
    json_path = shared / "profiles" / "owner" / "feishu_uat" / "ou_owner.json"
    feishu_uat_auth._atomic_write_json(json_path, expired_payload)

    seen = {}

    def fake_refresh(refresh_token, client_id, client_secret):
        seen.update({
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        })
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 7200,
            "refresh_token_expires_in": 30 * 24 * 3600,
            "scope": "offline_access wiki:wiki:readonly",
        }

    monkeypatch.setattr(feishu_uat_auth, "_refresh_uat_token", fake_refresh)

    status = feishu_uat_auth.credential_status(
        profile_name="owner",
        open_id="ou_owner",
        required_scopes="wiki:wiki:readonly",
        shared_home=shared,
    )

    assert status["status"] == "valid"
    assert status["refresh_expires_at"] > now_ms
    assert seen == {
        "refresh_token": "old-refresh",
        "client_id": "cli_test",
        "client_secret": "app-secret",
    }
    raw_json = json_path.read_text(encoding="utf-8")
    assert "new-access" in raw_json
    assert "old-access" not in raw_json
    payload = CredentialStore(shared / "multitenancy.db").get_secret_for_runtime(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
    )
    assert payload["access_token"] == "new-access"
    assert payload["refresh_token"] == "new-refresh"


def test_feishu_uat_device_flow_falls_back_without_legacy_helper(monkeypatch):
    from hermes_multitenancy import feishu_uat_auth

    calls = []

    def fake_post(url, payload):
        calls.append((url, payload))
        return {
            "device_code": " dc_test ",
            "user_code": "UAT-123",
            "verification_uri": "https://accounts.feishu.cn/device",
            "verification_uri_complete": "https://accounts.feishu.cn/device?user_code=UAT-123",
            "expires_in": 1800,
            "interval": 1,
        }

    monkeypatch.setattr(feishu_uat_auth, "_api_post", fake_post)

    result = feishu_uat_auth._begin_device_authorization("cli_test", None, "secret_test")

    assert result["device_code"] == "dc_test"
    assert result["user_code"] == "UAT-123"
    assert result["interval"] == 2
    assert calls[0][0].endswith("/oauth/v1/device_authorization")
    assert calls[0][1]["client_secret"] == "secret_test"
    assert "offline_access" in calls[0][1]["scope"].split()


def test_feishu_uat_poll_falls_back_without_legacy_helper(monkeypatch):
    from hermes_multitenancy import feishu_uat_auth

    def fake_post(url, payload):
        assert url.endswith("/open-apis/authen/v2/oauth/token")
        assert payload["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
        assert payload["client_secret"] == "secret_test"
        return {
            "access_token": "uat-access",
            "refresh_token": "uat-refresh",
            "expires_in": 7200,
            "refresh_token_expires_in": 2592000,
            "scope": "offline_access wiki:wiki",
        }

    monkeypatch.setattr(feishu_uat_auth, "_api_post", fake_post)

    result = feishu_uat_auth._poll_device_token("dc_test", "cli_test", "secret_test")

    assert result["access_token"] == "uat-access"
    assert result["refresh_token"] == "uat-refresh"
    assert result["refresh_expires_in"] == 2592000
    assert result["scope"] == "offline_access wiki:wiki"


def test_feishu_uat_api_post_parses_oauth_error_json_from_http_error(monkeypatch):
    from hermes_multitenancy import feishu_uat_auth

    def fake_urlopen(_request, timeout):
        raise urllib.error.HTTPError(
            "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":"authorization_pending"}'),
        )

    monkeypatch.setattr(feishu_uat_auth.urllib.request, "urlopen", fake_urlopen)

    assert feishu_uat_auth._api_post("https://example.com/token", {}) == {"error": "authorization_pending"}


def test_legacy_refresh_entrypoint_delegates_to_multitenancy_refresh(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.credentials import CredentialStore

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    now_ms = int(time.time() * 1000)
    expired_payload = {
        "app_id": "cli_test",
        "user_open_id": "ou_owner",
        "access_token": "legacy-old-access",
        "refresh_token": "legacy-old-refresh",
        "expires_at": now_ms - 1000,
        "refresh_expires_at": now_ms + 86400_000,
        "scope": "offline_access im:message.send_as_user",
        "granted_at": now_ms - 7200_000,
    }
    CredentialStore(shared / "multitenancy.db").put_credential(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
        payload=expired_payload,
        scopes=["offline_access", "im:message.send_as_user"],
        expires_at=expired_payload["expires_at"],
    )

    seen = {}

    def fake_refresh(refresh_token, client_id, client_secret):
        seen.update({
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        })
        return {
            "access_token": "legacy-new-access",
            "refresh_token": "legacy-new-refresh",
            "expires_in": 7200,
            "refresh_token_expires_in": 30 * 24 * 3600,
            "scope": "offline_access im:message.send_as_user",
        }

    monkeypatch.setattr(feishu_uat_auth, "_refresh_uat_token", fake_refresh)

    refreshed = feishu_uat_auth.refresh_uat_for_user(
        "ou_owner",
        client_id="legacy-client",
        client_secret="legacy-secret",
        shared_home=shared,
    )

    assert refreshed["access_token"] == "legacy-new-access"
    assert seen == {
        "refresh_token": "legacy-old-refresh",
        "client_id": "legacy-client",
        "client_secret": "legacy-secret",
    }
    json_path = shared / "profiles" / "owner" / "feishu_uat" / "ou_owner.json"
    assert "legacy-new-access" in json_path.read_text(encoding="utf-8")
    payload = CredentialStore(shared / "multitenancy.db").get_secret_for_runtime(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
    )
    assert payload["access_token"] == "legacy-new-access"


def test_legacy_refresh_entrypoint_rejects_unrouted_open_id(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth

    shared = _prepare_shared_home(tmp_path, monkeypatch)

    try:
        feishu_uat_auth.refresh_uat_for_user(
            "ou_not_bound",
            client_id="legacy-client",
            client_secret="legacy-secret",
            shared_home=shared,
        )
    except feishu_uat_auth.FeishuUatAuthError as exc:
        assert exc.status == 403
        assert "not bound" in exc.message
    else:
        raise AssertionError("unrouted open_id should be rejected")


def test_feishu_uat_status_marks_reauth_when_refresh_token_expired(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.credentials import CredentialStore

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    now_ms = int(time.time() * 1000)
    CredentialStore(shared / "multitenancy.db").put_credential(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
        payload={
            "app_id": "cli_test",
            "user_open_id": "ou_owner",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": now_ms - 1000,
            "refresh_expires_at": now_ms - 500,
            "scope": "offline_access",
        },
        scopes=["offline_access"],
        expires_at=now_ms - 1000,
    )

    status = feishu_uat_auth.credential_status(
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )

    assert status["status"] == "expired"
    assert status["needs_reauth"] is True
    assert "refresh_token" in status["refresh_error"]
    assert "old-access" not in str(status)


def test_feishu_uat_status_reports_unexpected_refresh_error(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.credentials import CredentialStore

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    now_ms = int(time.time() * 1000)
    CredentialStore(shared / "multitenancy.db").put_credential(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        secret_kind="uat",
        payload={
            "app_id": "cli_test",
            "user_open_id": "ou_owner",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": now_ms - 1000,
            "refresh_expires_at": now_ms + 86400_000,
            "scope": "offline_access",
        },
        scopes=["offline_access"],
        expires_at=now_ms - 1000,
    )
    monkeypatch.setattr(
        feishu_uat_auth,
        "_refresh_uat_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("network stack exploded")),
    )

    status = feishu_uat_auth.credential_status(
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )

    assert status["status"] == "expired"
    assert "unexpected refresh error" in status["refresh_error"]
    assert status["needs_reauth"] is False
    assert "old-access" not in str(status)


def test_webui_feishu_auth_session_polls_success_and_saves_vault(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = _prepare_shared_home(tmp_path, monkeypatch)

    monkeypatch.setattr(
        feishu_uat_auth,
        "_begin_device_authorization",
        lambda client_id, scope, client_secret: {
            "device_code": "device-1",
            "user_code": "ABCD-1234",
            "verification_uri_complete": "https://accounts.feishu.cn/device?user_code=ABCD-1234",
            "expires_in": 600,
            "interval": 1,
        },
    )
    monkeypatch.setattr(
        feishu_uat_auth,
        "_poll_device_token",
        lambda device_code, client_id, client_secret: {
            "access_token": "uat-access",
            "refresh_token": "uat-refresh",
            "open_id": "ou_owner",
            "expires_in": 7200,
            "refresh_expires_in": 30 * 24 * 3600,
            "scope": "wiki:wiki:readonly offline_access",
        },
    )

    async def runner():
        app = create_run_broker_app(
            dispatch_agent=lambda request: "",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            started = await client.post("/api/run-broker/feishu-auth/sessions", json={
                "profile_name": "owner",
                "user_key": "ou_owner",
                "scope": "wiki:wiki:readonly offline_access",
            })
            started_body = await started.json()
            polled = await client.get(
                f"/api/run-broker/feishu-auth/sessions/{started_body['session_id']}",
                headers={
                    "X-Hermes-Profile": "owner",
                    "X-Hermes-User-Key": "ou_owner",
                },
            )
            polled_body = await polled.json()
        finally:
            await client.close()

        assert started.status == 200
        assert started_body["status"] == "pending"
        assert started_body["verification_uri"] == "https://accounts.feishu.cn/device?user_code=ABCD-1234"
        assert "device-1" not in str(started_body)
        assert polled.status == 200
        assert polled_body["status"] == "success"
        assert "uat-access" not in str(polled_body)

    asyncio.run(runner())

    status = CredentialStore(shared / "multitenancy.db").get_status(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
        required_scopes=["wiki:wiki:readonly"],
    )
    assert status["status"] == "valid"
    assert (shared / "profiles" / "owner" / "feishu_uat" / "ou_owner.json").is_file()


def test_poll_session_keeps_exchanged_token_while_identity_lock_is_busy(tmp_path, monkeypatch):
    from hermes_multitenancy import credential_renewal_common as common
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.credentials import CredentialStore

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    session_id = "busy-store"
    _add_pending_auth_session(
        feishu_uat_auth,
        session_id=session_id,
        device_code="device-busy",
    )
    poll_calls: list[str] = []

    def poll_token(device_code, _client_id, _client_secret):
        poll_calls.append(device_code)
        return {
            "access_token": "one-time-access",
            "refresh_token": "one-time-refresh",
            "open_id": "ou_owner",
            "expires_in": 7200,
            "refresh_expires_in": 30 * 24 * 3600,
            "scope": "wiki:wiki:readonly offline_access",
        }

    monkeypatch.setattr(feishu_uat_auth, "_poll_device_token", poll_token)
    entered = threading.Barrier(2)
    release = threading.Event()
    holder_errors: list[BaseException] = []

    def hold_identity_lock() -> None:
        try:
            with common.credential_identity_lock(shared, "owner", "ou_owner"):
                entered.wait(timeout=2)
                release.wait(timeout=1)
        except BaseException as exc:  # pragma: no cover - surfaced below
            holder_errors.append(exc)

    holder = threading.Thread(target=hold_identity_lock)
    holder.start()
    entered.wait(timeout=2)
    started = time.monotonic()
    try:
        first = feishu_uat_auth.poll_session(
            session_id=session_id,
            profile_name="owner",
            open_id="ou_owner",
            shared_home=shared,
        )
        elapsed = time.monotonic() - started
        assert first["status"] == "pending"
        assert elapsed < 0.5
        assert poll_calls == ["device-busy"]
        assert "one-time-access" not in str(first)
    finally:
        release.set()
        holder.join(timeout=2)

    assert not holder.is_alive()
    assert holder_errors == []
    second = feishu_uat_auth.poll_session(
        session_id=session_id,
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )
    assert second["status"] == "success"
    assert poll_calls == ["device-busy"]
    store = CredentialStore(shared / "multitenancy.db")
    try:
        assert store.get_secret_for_runtime(
            profile_name="owner",
            subject_id="ou_owner",
            provider="feishu",
        )["access_token"] == "one-time-access"
    finally:
        store.close()


def test_poll_session_revalidates_route_after_waiting_for_identity_lock(
    tmp_path,
    monkeypatch,
):
    from hermes_multitenancy import credential_renewal_common, feishu_uat_auth
    from hermes_multitenancy.credentials import CredentialStore

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    session = _add_pending_auth_session(
        feishu_uat_auth,
        session_id="route-swap",
        device_code="device-route-swap",
    )
    exchanged = threading.Event()
    results: list[dict] = []
    errors: list[BaseException] = []
    poll_calls: list[str] = []

    def poll_token(device_code, _client_id, _client_secret):
        poll_calls.append(device_code)
        exchanged.set()
        return {
            "access_token": "route-swap-access",
            "refresh_token": "route-swap-refresh",
            "open_id": "ou_owner",
            "expires_in": 7200,
            "refresh_expires_in": 30 * 24 * 3600,
            "scope": "wiki:wiki:readonly offline_access",
        }

    def run_poll() -> None:
        try:
            results.append(
                feishu_uat_auth.poll_session(
                    session_id=session.session_id,
                    profile_name="owner",
                    open_id="ou_owner",
                    shared_home=shared,
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    monkeypatch.setattr(feishu_uat_auth, "_poll_device_token", poll_token)
    monkeypatch.setattr(feishu_uat_auth, "_POLL_STORE_LOCK_TIMEOUT_SECONDS", 1.0)
    with credential_renewal_common.credential_identity_lock(shared, "owner", "ou_owner"):
        worker = threading.Thread(target=run_poll)
        worker.start()
        assert exchanged.wait(timeout=2)
        with sqlite3.connect(shared / "multitenancy.db") as conn:
            conn.execute(
                "UPDATE multitenancy_routing SET profile_name = 'replacement' "
                "WHERE open_id = 'ou_owner' AND active = 1 AND kind = 'user'"
            )
            conn.commit()

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert results == []
    assert len(errors) == 1
    assert isinstance(errors[0], feishu_uat_auth.FeishuUatAuthError)
    assert errors[0].status == 403
    assert session.status == "error"
    assert session._pending_token_payload is None
    assert poll_calls == ["device-route-swap"]
    assert not (
        shared / "profiles" / "owner" / "feishu_uat" / "ou_owner.json"
    ).exists()
    store = CredentialStore(shared / "multitenancy.db")
    try:
        assert store.get_status(
            profile_name="owner",
            subject_id="ou_owner",
            provider="feishu",
        )["status"] == "missing"
    finally:
        store.close()


def test_poll_session_serializes_concurrent_duplicate_exchange(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    session_id = "duplicate-poll"
    session = _add_pending_auth_session(
        feishu_uat_auth,
        session_id=session_id,
        device_code="device-duplicate",
    )
    exchange_started = threading.Event()
    release_exchange = threading.Event()
    poll_calls: list[str] = []
    store_calls = 0
    worker_results: list[dict] = []
    worker_errors: list[BaseException] = []

    def poll_token(device_code, _client_id, _client_secret):
        poll_calls.append(device_code)
        if len(poll_calls) == 1:
            exchange_started.set()
            assert release_exchange.wait(timeout=2)
        return {
            "access_token": "one-time-access",
            "refresh_token": "one-time-refresh",
            "open_id": "ou_owner",
            "expires_in": 7200,
            "refresh_expires_in": 30 * 24 * 3600,
            "scope": "wiki:wiki:readonly offline_access",
        }

    def store_uat(*_args, **_kwargs):
        nonlocal store_calls
        store_calls += 1

    def poll_in_worker() -> None:
        try:
            worker_results.append(
                feishu_uat_auth.poll_session(
                    session_id=session_id,
                    profile_name="owner",
                    open_id="ou_owner",
                    shared_home=shared,
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            worker_errors.append(exc)

    monkeypatch.setattr(feishu_uat_auth, "_poll_device_token", poll_token)
    monkeypatch.setattr(feishu_uat_auth, "_store_uat", store_uat)
    worker = threading.Thread(target=poll_in_worker)
    worker.start()
    assert exchange_started.wait(timeout=2)
    started = time.monotonic()
    try:
        duplicate = feishu_uat_auth.poll_session(
            session_id=session_id,
            profile_name="owner",
            open_id="ou_owner",
            shared_home=shared,
        )
        assert duplicate["status"] == "pending"
        assert time.monotonic() - started < 0.5
        assert "one-time-access" not in str(duplicate)
    finally:
        release_exchange.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert worker_errors == []
    assert worker_results[0]["status"] == "success"
    assert poll_calls == ["device-duplicate"]
    assert store_calls == 1
    assert session._poll_lock.acquire(blocking=False)
    session._poll_lock.release()


@pytest.mark.parametrize(
    "store_error",
    [
        sqlite3.OperationalError("database is locked"),
        OSError(errno.EBUSY, "store is busy"),
    ],
    ids=["sqlite-busy", "filesystem-busy"],
)
def test_poll_session_reuses_exchanged_token_after_transient_store_error(
    tmp_path, monkeypatch, store_error
):
    from hermes_multitenancy import feishu_uat_auth

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    session_id = f"transient-{type(store_error).__name__}"
    _add_pending_auth_session(
        feishu_uat_auth,
        session_id=session_id,
        device_code="device-transient",
    )
    poll_calls: list[str] = []
    store_calls = 0

    def poll_token(device_code, _client_id, _client_secret):
        poll_calls.append(device_code)
        return {
            "access_token": "one-time-access",
            "refresh_token": "one-time-refresh",
            "open_id": "ou_owner",
            "expires_in": 7200,
            "refresh_expires_in": 30 * 24 * 3600,
            "scope": "wiki:wiki:readonly offline_access",
        }

    def store_uat(*_args, **_kwargs):
        nonlocal store_calls
        store_calls += 1
        if store_calls == 1:
            raise store_error

    monkeypatch.setattr(feishu_uat_auth, "_poll_device_token", poll_token)
    monkeypatch.setattr(feishu_uat_auth, "_store_uat", store_uat)

    first = feishu_uat_auth.poll_session(
        session_id=session_id,
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )
    second = feishu_uat_auth.poll_session(
        session_id=session_id,
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )

    assert first["status"] == "pending"
    assert "one-time-access" not in str(first)
    assert second["status"] == "success"
    assert poll_calls == ["device-transient"]
    assert store_calls == 2


def test_poll_session_stabilizes_terminal_store_error_without_repoll(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    session_id = "terminal-store"
    session = _add_pending_auth_session(
        feishu_uat_auth,
        session_id=session_id,
        device_code="device-terminal",
    )
    poll_calls: list[str] = []
    store_calls = 0

    def poll_token(device_code, _client_id, _client_secret):
        poll_calls.append(device_code)
        return {
            "access_token": "one-time-access",
            "refresh_token": "",
            "open_id": "ou_owner",
            "expires_in": 7200,
            "scope": "wiki:wiki:readonly offline_access",
        }

    def reject_store(*_args, **_kwargs):
        nonlocal store_calls
        store_calls += 1
        raise feishu_uat_auth.FeishuUatAuthError("terminal L1 rejection", status=400)

    monkeypatch.setattr(feishu_uat_auth, "_poll_device_token", poll_token)
    monkeypatch.setattr(feishu_uat_auth, "_store_uat", reject_store)

    with pytest.raises(feishu_uat_auth.FeishuUatAuthError, match="terminal L1 rejection"):
        feishu_uat_auth.poll_session(
            session_id=session_id,
            profile_name="owner",
            open_id="ou_owner",
            shared_home=shared,
        )
    second = feishu_uat_auth.poll_session(
        session_id=session_id,
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )

    assert second["status"] == "error"
    assert second["error"] == "terminal L1 rejection"
    assert session._pending_token_payload is None
    assert poll_calls == ["device-terminal"]
    assert store_calls == 1


def test_poll_session_expires_and_clears_cached_payload(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    session = _add_pending_auth_session(
        feishu_uat_auth,
        session_id="expired-cached-payload",
        device_code="device-expired",
    )
    session.expires_at = int(time.time()) - 1
    session._pending_token_payload = {
        "access_token": "one-time-access",
        "refresh_token": "one-time-refresh",
        "user_open_id": "ou_owner",
    }
    poll_calls: list[str] = []
    store_calls = 0

    def poll_token(*_args, **_kwargs):
        poll_calls.append("called")
        return {}

    def store_uat(*_args, **_kwargs):
        nonlocal store_calls
        store_calls += 1

    monkeypatch.setattr(feishu_uat_auth, "_poll_device_token", poll_token)
    monkeypatch.setattr(feishu_uat_auth, "_store_uat", store_uat)

    result = feishu_uat_auth.poll_session(
        session_id=session.session_id,
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )

    assert result["status"] == "expired"
    assert result["error"] == "authorization session expired"
    assert session._pending_token_payload is None
    assert poll_calls == []
    assert store_calls == 0


def test_poll_session_expires_if_device_exchange_crosses_deadline(tmp_path, monkeypatch):
    from hermes_multitenancy import feishu_uat_auth

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    session = _add_pending_auth_session(
        feishu_uat_auth,
        session_id="expires-during-exchange",
        device_code="device-expires-during-exchange",
    )
    poll_calls: list[str] = []
    store_calls = 0

    def poll_token(device_code, _client_id, _client_secret):
        poll_calls.append(device_code)
        session.expires_at = int(time.time()) - 1
        return {
            "access_token": "late-access",
            "refresh_token": "late-refresh",
            "open_id": "ou_owner",
            "expires_in": 7200,
            "refresh_expires_in": 30 * 24 * 3600,
            "scope": "wiki:wiki:readonly offline_access",
        }

    def store_uat(*_args, **_kwargs):
        nonlocal store_calls
        store_calls += 1

    monkeypatch.setattr(feishu_uat_auth, "_poll_device_token", poll_token)
    monkeypatch.setattr(feishu_uat_auth, "_store_uat", store_uat)

    first = feishu_uat_auth.poll_session(
        session_id=session.session_id,
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )
    second = feishu_uat_auth.poll_session(
        session_id=session.session_id,
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )

    assert first["status"] == "expired"
    assert first["error"] == "authorization session expired"
    assert second == first
    assert session._pending_token_payload is None
    assert poll_calls == ["device-expires-during-exchange"]
    assert store_calls == 0


@pytest.mark.parametrize("ttl", ["not-an-int", -1, 10**15], ids=["invalid", "negative", "huge"])
def test_start_session_rejects_invalid_device_ttl(tmp_path, monkeypatch, ttl):
    from hermes_multitenancy import feishu_uat_auth

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    monkeypatch.setattr(
        feishu_uat_auth,
        "_begin_device_authorization",
        lambda *_args: {
            "device_code": "device-invalid-ttl",
            "user_code": "TTL-INVALID",
            "verification_uri_complete": "https://accounts.feishu.cn/device?user_code=TTL-INVALID",
            "expires_in": ttl,
            "interval": 1,
        },
    )
    existing_sessions = set(feishu_uat_auth._sessions)
    try:
        with pytest.raises(feishu_uat_auth.FeishuUatAuthError, match="invalid expires_in"):
            feishu_uat_auth.start_session(
                profile_name="owner",
                open_id="ou_owner",
                shared_home=shared,
            )
    finally:
        for session_id in set(feishu_uat_auth._sessions) - existing_sessions:
            feishu_uat_auth._sessions.pop(session_id, None)


@pytest.mark.parametrize(
    ("ttl_field", "ttl"),
    [
        ("expires_in", "not-an-int"),
        ("expires_in", -1),
        ("expires_in", 10**15),
        ("refresh_expires_in", "not-an-int"),
        ("refresh_expires_in", -1),
        ("refresh_expires_in", 10**15),
    ],
    ids=[
        "access-invalid",
        "access-negative",
        "access-huge",
        "refresh-invalid",
        "refresh-negative",
        "refresh-huge",
    ],
)
def test_poll_session_stabilizes_invalid_token_ttl_without_reexchange(
    tmp_path,
    monkeypatch,
    ttl_field,
    ttl,
):
    from hermes_multitenancy import feishu_uat_auth

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    session = _add_pending_auth_session(
        feishu_uat_auth,
        session_id=f"invalid-token-ttl-{ttl_field}-{ttl}",
        device_code="device-invalid-token-ttl",
    )
    poll_calls: list[str] = []
    store_calls = 0

    def poll_token(device_code, _client_id, _client_secret):
        poll_calls.append(device_code)
        result = {
            "access_token": "ttl-access",
            "refresh_token": "ttl-refresh",
            "open_id": "ou_owner",
            "expires_in": 7200,
            "refresh_expires_in": 30 * 24 * 3600,
            "scope": "wiki:wiki:readonly offline_access",
        }
        result[ttl_field] = ttl
        return result

    def store_uat(*_args, **_kwargs):
        nonlocal store_calls
        store_calls += 1

    monkeypatch.setattr(feishu_uat_auth, "_poll_device_token", poll_token)
    monkeypatch.setattr(feishu_uat_auth, "_store_uat", store_uat)

    with pytest.raises(feishu_uat_auth.FeishuUatAuthError, match=f"invalid {ttl_field}"):
        feishu_uat_auth.poll_session(
            session_id=session.session_id,
            profile_name="owner",
            open_id="ou_owner",
            shared_home=shared,
        )
    second = feishu_uat_auth.poll_session(
        session_id=session.session_id,
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )

    assert second["status"] == "error"
    assert f"invalid {ttl_field}" in second["error"]
    assert session._pending_token_payload is None
    assert poll_calls == ["device-invalid-token-ttl"]
    assert store_calls == 0


@pytest.mark.parametrize(
    "store_error",
    [
        sqlite3.OperationalError("no such table: multitenancy_credentials"),
        OSError(errno.ENOSPC, "credential store is full"),
        RuntimeError("unexpected credential store failure"),
    ],
    ids=["sqlite-schema", "filesystem-full", "unknown"],
)
def test_poll_session_stabilizes_nonretryable_store_error_without_reexchange(
    tmp_path, monkeypatch, store_error
):
    from hermes_multitenancy import feishu_uat_auth

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    session_id = f"nonretryable-{type(store_error).__name__}"
    session = _add_pending_auth_session(
        feishu_uat_auth,
        session_id=session_id,
        device_code="device-nonretryable",
    )
    session._pending_token_payload = {
        "access_token": "one-time-access",
        "refresh_token": "one-time-refresh",
        "user_open_id": "ou_owner",
    }

    poll_calls: list[str] = []
    store_calls = 0

    def poll_token(*_args, **_kwargs):
        poll_calls.append("called")
        return {}

    def fail_store(*_args, **_kwargs):
        nonlocal store_calls
        store_calls += 1
        raise store_error

    monkeypatch.setattr(feishu_uat_auth, "_poll_device_token", poll_token)
    monkeypatch.setattr(feishu_uat_auth, "_store_uat", fail_store)

    with pytest.raises(type(store_error)):
        feishu_uat_auth.poll_session(
            session_id=session_id,
            profile_name="owner",
            open_id="ou_owner",
            shared_home=shared,
        )

    second = feishu_uat_auth.poll_session(
        session_id=session_id,
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )

    assert session._pending_token_payload is None
    assert second["status"] == "error"
    assert second["error"] == "authorization credential storage failed; please authorize again"
    assert poll_calls == []
    assert store_calls == 1


def test_webui_feishu_auth_session_uses_vault_app_credentials_when_env_missing(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    CredentialStore(shared / "multitenancy.db").put_credential(
        profile_name="__global__",
        subject_id="feishu_app",
        provider="feishu",
        secret_kind="app",
        payload={"app_id": "cli_from_vault", "app_secret": "secret-from-vault"},
    )
    seen = {}

    def begin_device_authorization(client_id, scope, client_secret):
        seen["client_id"] = client_id
        seen["client_secret"] = client_secret
        return {
            "device_code": "device-vault",
            "user_code": "VAULT-1234",
            "verification_uri_complete": "https://accounts.feishu.cn/device?user_code=VAULT-1234",
            "expires_in": 600,
            "interval": 1,
        }

    monkeypatch.setattr(feishu_uat_auth, "_begin_device_authorization", begin_device_authorization)

    async def runner():
        app = create_run_broker_app(
            dispatch_agent=lambda request: "",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            started = await client.post("/api/run-broker/feishu-auth/sessions", json={
                "profile_name": "owner",
                "user_key": "ou_owner",
            })
            body = await started.json()
        finally:
            await client.close()

        assert started.status == 200
        assert body["status"] == "pending"

    asyncio.run(runner())

    assert seen == {"client_id": "cli_from_vault", "client_secret": "secret-from-vault"}


def test_feishu_uat_auth_has_local_device_flow_fallback_when_hermes_helper_missing(monkeypatch):
    from hermes_multitenancy import feishu_uat_auth

    monkeypatch.setitem(sys.modules, "hermes_cli.feishu_auth", None)
    calls = []

    def fake_http_json(method, url, *, payload=None, headers=None):
        calls.append((method, url, dict(payload or {}), dict(headers or {})))
        if url.endswith("/oauth/v1/device_authorization"):
            return {
                "device_code": "device-local",
                "user_code": "LOCAL-1",
                "verification_uri": "https://accounts.feishu.cn/device",
                "verification_uri_complete": "https://accounts.feishu.cn/device?user_code=LOCAL-1",
                "expires_in": 600,
                "interval": 2,
            }
        if url.endswith("/open-apis/authen/v2/oauth/token"):
            return {
                "access_token": "uat-access",
                "refresh_token": "uat-refresh",
                "open_id": "ou_owner",
                "expires_in": 7200,
                "refresh_token_expires_in": 30 * 24 * 3600,
                "scope": "offline_access",
            }
        if url.endswith("/open-apis/authen/v1/user_info"):
            return {
                "code": 0,
                "data": {
                    "open_id": "ou_owner",
                    "union_id": "on_union",
                    "user_id": "u_owner",
                    "name": "owner",
                },
            }
        raise AssertionError((method, url))

    monkeypatch.setattr(feishu_uat_auth, "_http_json", fake_http_json)

    started = feishu_uat_auth._begin_device_authorization("cli_test", None, "secret")
    polled = feishu_uat_auth._poll_device_token("device-local", "cli_test", "secret")
    user = feishu_uat_auth._fetch_user_info("uat-access")

    assert started["device_code"] == "device-local"
    assert "offline_access" in calls[0][2]["scope"].split()
    assert len(calls[0][2]["scope"].split()) > 20
    assert polled["open_id"] == "ou_owner"
    assert polled["refresh_expires_in"] == 30 * 24 * 3600
    assert user["open_id"] == "ou_owner"
    assert "uat-access" not in str(started)


def test_feishu_uat_auth_fallback_when_hermes_cli_package_missing(monkeypatch):
    from hermes_multitenancy import feishu_uat_auth

    monkeypatch.delitem(sys.modules, "hermes_cli", raising=False)
    monkeypatch.delitem(sys.modules, "hermes_cli.feishu_auth", raising=False)

    def fake_http_json(method, url, *, payload=None, headers=None):
        assert method == "POST"
        assert url.endswith("/oauth/v1/device_authorization")
        return {
            "device_code": "device-no-package",
            "user_code": "NO-PKG",
            "verification_uri_complete": "https://accounts.feishu.cn/device?user_code=NO-PKG",
            "expires_in": 600,
            "interval": 2,
        }

    monkeypatch.setattr(feishu_uat_auth, "_http_json", fake_http_json)

    started = feishu_uat_auth._begin_device_authorization("cli_test", "offline_access", "secret")

    assert started["device_code"] == "device-no-package"
    assert started["user_code"] == "NO-PKG"


def test_webui_feishu_auth_session_rejects_mismatched_authorized_open_id(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = _prepare_shared_home(tmp_path, monkeypatch)

    monkeypatch.setattr(
        feishu_uat_auth,
        "_begin_device_authorization",
        lambda client_id, scope, client_secret: {
            "device_code": "device-2",
            "user_code": "WXYZ-9999",
            "verification_uri_complete": "https://accounts.feishu.cn/device?user_code=WXYZ-9999",
            "expires_in": 600,
            "interval": 1,
        },
    )
    monkeypatch.setattr(
        feishu_uat_auth,
        "_poll_device_token",
        lambda device_code, client_id, client_secret: {
            "access_token": "stranger-access",
            "refresh_token": "stranger-refresh",
            "open_id": "ou_stranger",
            "expires_in": 7200,
            "refresh_expires_in": 30 * 24 * 3600,
            "scope": "wiki:wiki:readonly offline_access",
        },
    )

    async def runner():
        app = create_run_broker_app(
            dispatch_agent=lambda request: "",
            mark_seen=lambda _request: True,
            sandbox_available=lambda: True,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            started = await client.post("/api/run-broker/feishu-auth/sessions", json={
                "profile_name": "owner",
                "user_key": "ou_owner",
            })
            started_body = await started.json()
            polled = await client.get(
                f"/api/run-broker/feishu-auth/sessions/{started_body['session_id']}",
                headers={
                    "X-Hermes-Profile": "owner",
                    "X-Hermes-User-Key": "ou_owner",
                },
            )
            polled_body = await polled.json()
        finally:
            await client.close()

        assert polled.status == 403
        assert polled_body["status"] == "error"
        assert "does not match" in polled_body["error"]
        assert "stranger-access" not in str(polled_body)

    asyncio.run(runner())

    status = CredentialStore(shared / "multitenancy.db").get_status(
        profile_name="owner",
        subject_id="ou_owner",
        provider="feishu",
    )
    assert status["status"] == "missing"
