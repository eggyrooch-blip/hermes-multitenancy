from __future__ import annotations

import asyncio
import io
import json
import sys
import time
import urllib.error
import urllib.parse


def _prepare_shared_home(tmp_path, monkeypatch):
    shared = tmp_path / ".hermes"
    router_home = shared / "profiles" / "multitenancy_router"
    router_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(router_home))
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")

    from hermes_multitenancy.routing import RoutingTable

    table = RoutingTable(shared / "multitenancy.db")
    table.upsert(user_id="owner", profile_name="owner", open_id="ou_owner")
    return shared


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
