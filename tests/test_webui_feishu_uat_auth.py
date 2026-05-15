from __future__ import annotations

import asyncio
import time


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
    table.upsert(user_id="sunke", profile_name="sunke", open_id="ou_sunke")
    return shared


def test_webui_feishu_uat_status_is_route_scoped_and_redacted(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    shared = _prepare_shared_home(tmp_path, monkeypatch)
    CredentialStore(shared / "multitenancy.db").put_credential(
        profile_name="sunke",
        subject_id="ou_sunke",
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
                    "X-Hermes-Profile": "sunke",
                    "X-Hermes-User-Key": "ou_sunke",
                },
                params={"required_scopes": "wiki:wiki:readonly"},
            )
            wrong_route = await client.get(
                "/api/run-broker/credentials/feishu/uat/status",
                headers={
                    "X-Hermes-Profile": "other",
                    "X-Hermes-User-Key": "ou_sunke",
                },
            )
            body = await response.json()
            raw = await response.text()
        finally:
            await client.close()

        assert response.status == 200
        assert body["status"] == "scope_missing"
        assert body["profile_name"] == "sunke"
        assert body["subject_id"] == "ou_sunke"
        assert body["missing_scopes"] == ["wiki:wiki:readonly"]
        assert "raw-access" not in raw
        assert "raw-refresh" not in raw
        assert wrong_route.status == 403

    asyncio.run(runner())


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
            "open_id": "ou_sunke",
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
                "profile_name": "sunke",
                "user_key": "ou_sunke",
                "scope": "wiki:wiki:readonly offline_access",
            })
            started_body = await started.json()
            polled = await client.get(
                f"/api/run-broker/feishu-auth/sessions/{started_body['session_id']}",
                headers={
                    "X-Hermes-Profile": "sunke",
                    "X-Hermes-User-Key": "ou_sunke",
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
        profile_name="sunke",
        subject_id="ou_sunke",
        provider="feishu",
        required_scopes=["wiki:wiki:readonly"],
    )
    assert status["status"] == "valid"
    assert (shared / "profiles" / "sunke" / "feishu_uat" / "ou_sunke.json").is_file()


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
                "profile_name": "sunke",
                "user_key": "ou_sunke",
            })
            body = await started.json()
        finally:
            await client.close()

        assert started.status == 200
        assert body["status"] == "pending"

    asyncio.run(runner())

    assert seen == {"client_id": "cli_from_vault", "client_secret": "secret-from-vault"}


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
                "profile_name": "sunke",
                "user_key": "ou_sunke",
            })
            started_body = await started.json()
            polled = await client.get(
                f"/api/run-broker/feishu-auth/sessions/{started_body['session_id']}",
                headers={
                    "X-Hermes-Profile": "sunke",
                    "X-Hermes-User-Key": "ou_sunke",
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
        profile_name="sunke",
        subject_id="ou_sunke",
        provider="feishu",
    )
    assert status["status"] == "missing"
