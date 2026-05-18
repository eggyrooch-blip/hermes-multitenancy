from __future__ import annotations

import hashlib
import hmac
import json
import http.client
import time
import urllib.parse
from pathlib import Path

import pytest


def _body_sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _sign(
    key: str,
    *,
    version: str = "v1",
    method: str = "GET",
    host: str = "open.feishu.cn",
    path_and_query: str = "/open-apis/authen/v1/user_info",
    body_sha: str | None = None,
    timestamp: str | None = None,
    identity: str = "user",
    auth_header: str = "Authorization",
) -> str:
    canonical = "\n".join(
        [
            version,
            method,
            host,
            path_and_query,
            body_sha or _body_sha(b""),
            timestamp or str(int(time.time())),
            identity,
            auth_header,
        ]
    )
    return hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _headers(key: str, **overrides: str) -> dict[str, str]:
    body_sha = overrides.pop("body_sha", _body_sha(b""))
    timestamp = overrides.pop("timestamp", str(int(time.time())))
    identity = overrides.pop("identity", "user")
    auth_header = overrides.pop("auth_header", "Authorization")
    target = overrides.pop("target", "https://open.feishu.cn")
    host = overrides.pop("host", "open.feishu.cn")
    path_and_query = overrides.pop("path_and_query", "/open-apis/authen/v1/user_info")
    signature = _sign(
        key,
        host=host,
        path_and_query=path_and_query,
        body_sha=body_sha,
        timestamp=timestamp,
        identity=identity,
        auth_header=auth_header,
    )
    headers = {
        "X-Lark-Proxy-Version": "v1",
        "X-Lark-Proxy-Target": target,
        "X-Lark-Proxy-Identity": identity,
        "X-Lark-Proxy-Auth-Header": auth_header,
        "X-Lark-Proxy-Signature": signature,
        "X-Lark-Proxy-Timestamp": timestamp,
        "X-Lark-Body-SHA256": body_sha,
    }
    headers.update(overrides)
    return headers


def _store_uat(shared: Path) -> None:
    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(shared / "multitenancy.db", encryption_key="test-key")
    try:
        store.put_credential(
            profile_name="alice",
            subject_id="ou_alice",
            provider="feishu",
            secret_kind="uat",
            payload={"access_token": "uat-secret", "refresh_token": "refresh-secret"},
        )
    finally:
        store.close()


def _store_app_credential(shared: Path) -> None:
    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(shared / "multitenancy.db", encryption_key="test-key")
    try:
        store.put_credential(
            profile_name="__global__",
            subject_id="feishu_app",
            provider="feishu",
            secret_kind="app",
            payload={"app_id": "cli_app", "app_secret": "app-secret", "domain": "feishu"},
        )
    finally:
        store.close()


def test_broker_mints_tenant_token_for_bot_identity(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.lark_cli_auth_broker import (
        BrokerResponse,
        LarkCliAuthBroker,
        LarkCliAuthBrokerContext,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    _store_app_credential(shared)
    captured: dict[str, object] = {}

    class FakeHTTPResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"code":0,"tenant_access_token":"tat-secret"}'

    def fake_urlopen(request, timeout):
        captured["token_url"] = request.full_url
        captured["token_body"] = json.loads(request.data.decode("utf-8"))
        captured["token_timeout"] = timeout
        return FakeHTTPResponse()

    def fake_forward(_method, _url, headers, _body, _timeout):
        captured["authorization"] = headers.get("Authorization")
        return BrokerResponse(status=200, body=b'{"code":0}')

    monkeypatch.setattr("hermes_multitenancy.lark_cli_auth_broker.urllib.request.urlopen", fake_urlopen)
    broker = LarkCliAuthBroker(
        LarkCliAuthBrokerContext(
            shared_home=shared,
            profile_name="alice",
            user_open_id="ou_alice",
            hmac_key="proxy-key",
            allowed_identities=frozenset({"user", "bot"}),
        ),
        forwarder=fake_forward,
    )

    response = broker.handle(
        method="GET",
        path_and_query="/open-apis/contact/v3/scopes",
        headers=_headers(
            "proxy-key",
            identity="bot",
            path_and_query="/open-apis/contact/v3/scopes",
        ),
        body=b"",
    )

    assert response.status == 200
    assert captured["token_url"] == "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    assert captured["token_body"] == {"app_id": "cli_app", "app_secret": "app-secret"}
    assert captured["authorization"] == "Bearer tat-secret"


def test_broker_prefers_newer_profile_local_uat_json_over_stale_vault(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.lark_cli_auth_broker import (
        BrokerResponse,
        LarkCliAuthBroker,
        LarkCliAuthBrokerContext,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    _store_uat(shared)
    profile_dir = shared / "profiles" / "alice" / "feishu_uat"
    profile_dir.mkdir(parents=True)
    (profile_dir / "ou_alice.json").write_text(
        json.dumps(
            {
                "user_open_id": "ou_alice",
                "access_token": "fresh-json-uat",
                "refresh_token": "fresh-refresh",
                "granted_at": int(time.time() * 1000) + 1000,
                "expires_at": int(time.time() * 1000) + 7200_000,
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_forward(_method, _url, headers, _body, _timeout):
        captured["authorization"] = headers.get("Authorization")
        return BrokerResponse(status=200, body=b'{"code":0}')

    broker = LarkCliAuthBroker(
        LarkCliAuthBrokerContext(
            shared_home=shared,
            profile_name="alice",
            user_open_id="ou_alice",
            hmac_key="proxy-key",
            allowed_identities=frozenset({"user"}),
        ),
        forwarder=fake_forward,
    )

    response = broker.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers=_headers("proxy-key"),
        body=b"",
    )

    assert response.status == 200
    assert captured["authorization"] == "Bearer fresh-json-uat"


def test_broker_refreshes_expired_user_uat_before_forwarding(monkeypatch, tmp_path: Path):
    from hermes_multitenancy import feishu_uat_auth
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.lark_cli_auth_broker import (
        BrokerResponse,
        LarkCliAuthBroker,
        LarkCliAuthBrokerContext,
    )
    from hermes_multitenancy.routing import RoutingTable

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    monkeypatch.setenv("FEISHU_APP_ID", "cli_app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    shared = tmp_path / ".hermes"
    RoutingTable(shared / "multitenancy.db").upsert(
        user_id="alice",
        profile_name="alice",
        open_id="ou_alice",
    )
    now_ms = int(time.time() * 1000)
    CredentialStore(shared / "multitenancy.db").put_credential(
        profile_name="alice",
        subject_id="ou_alice",
        provider="feishu",
        secret_kind="uat",
        payload={
            "user_open_id": "ou_alice",
            "app_id": "cli_app",
            "access_token": "expired-uat",
            "refresh_token": "refresh-secret",
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
        lambda refresh_token, client_id, client_secret: {
            "access_token": "fresh-uat",
            "refresh_token": "fresh-refresh",
            "expires_in": 7200,
            "refresh_token_expires_in": 30 * 24 * 3600,
            "scope": "offline_access",
        },
    )
    captured: dict[str, object] = {}

    def fake_forward(_method, _url, headers, _body, _timeout):
        captured["authorization"] = headers.get("Authorization")
        return BrokerResponse(status=200, body=b'{"code":0}')

    broker = LarkCliAuthBroker(
        LarkCliAuthBrokerContext(
            shared_home=shared,
            profile_name="alice",
            user_open_id="ou_alice",
            hmac_key="proxy-key",
            allowed_identities=frozenset({"user"}),
        ),
        forwarder=fake_forward,
    )

    response = broker.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers=_headers("proxy-key"),
        body=b"",
    )

    assert response.status == 200
    assert captured["authorization"] == "Bearer fresh-uat"
    payload = CredentialStore(shared / "multitenancy.db").get_secret_for_runtime(
        profile_name="alice",
        subject_id="ou_alice",
        provider="feishu",
        secret_kind="uat",
    )
    assert payload["access_token"] == "fresh-uat"


def test_broker_can_use_profile_local_uat_json_without_vault_key(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.lark_cli_auth_broker import (
        BrokerResponse,
        LarkCliAuthBroker,
        LarkCliAuthBrokerContext,
    )

    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)
    shared = tmp_path / ".hermes"
    profile_dir = shared / "profiles" / "alice" / "feishu_uat"
    profile_dir.mkdir(parents=True)
    (profile_dir / "ou_alice.json").write_text(
        json.dumps(
            {
                "user_open_id": "ou_alice",
                "app_id": "cli_public",
                "access_token": "json-only-uat",
                "expires_at": int(time.time() * 1000) + 7200_000,
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_forward(_method, _url, headers, _body, _timeout):
        captured["authorization"] = headers.get("Authorization")
        return BrokerResponse(status=200, body=b'{"code":0}')

    broker = LarkCliAuthBroker(
        LarkCliAuthBrokerContext(
            shared_home=shared,
            profile_name="alice",
            user_open_id="ou_alice",
            hmac_key="proxy-key",
            allowed_identities=frozenset({"user"}),
        ),
        forwarder=fake_forward,
    )

    response = broker.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers=_headers("proxy-key"),
        body=b"",
    )

    assert response.status == 200
    assert captured["authorization"] == "Bearer json-only-uat"


def test_broker_injects_user_uat_and_strips_client_auth_headers(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.lark_cli_auth_broker import (
        BrokerResponse,
        LarkCliAuthBroker,
        LarkCliAuthBrokerContext,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    _store_uat(shared)
    captured: dict[str, object] = {}

    def fake_forward(method, url, headers, body, timeout):
        captured.update(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        return BrokerResponse(status=200, headers={"X-Upstream": "ok"}, body=b'{"code":0}')

    broker = LarkCliAuthBroker(
        LarkCliAuthBrokerContext(
            shared_home=shared,
            profile_name="alice",
            user_open_id="ou_alice",
            hmac_key="proxy-key",
            allowed_identities=frozenset({"user"}),
        ),
        forwarder=fake_forward,
    )

    response = broker.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers={
            **_headers("proxy-key"),
            "Authorization": "Bearer attacker-token",
            "X-Lark-MCP-UAT": "attacker-uat",
        },
        body=b"",
    )

    assert response.status == 200
    assert response.body == b'{"code":0}'
    assert captured["method"] == "GET"
    assert captured["url"] == "https://open.feishu.cn/open-apis/authen/v1/user_info"
    forwarded_headers = captured["headers"]
    assert forwarded_headers["Authorization"] == "Bearer uat-secret"
    assert "X-Lark-MCP-UAT" not in forwarded_headers
    assert "X-Lark-Proxy-Signature" not in forwarded_headers


def test_broker_rejects_bad_hmac_before_forwarding(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.lark_cli_auth_broker import (
        LarkCliAuthBroker,
        LarkCliAuthBrokerContext,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    _store_uat(shared)

    def fail_forward(*_args, **_kwargs):
        raise AssertionError("request should not be forwarded")

    broker = LarkCliAuthBroker(
        LarkCliAuthBrokerContext(
            shared_home=shared,
            profile_name="alice",
            user_open_id="ou_alice",
            hmac_key="proxy-key",
            allowed_identities=frozenset({"user"}),
        ),
        forwarder=fail_forward,
    )
    headers = _headers("wrong-key")

    response = broker.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers=headers,
        body=b"",
    )

    assert response.status == 401
    assert b"HMAC" in response.body


@pytest.mark.parametrize(
    ("target", "expected_status"),
    [
        ("http://open.feishu.cn", 403),
        ("https://evil.example.com", 403),
        ("https://open.feishu.cn/open-apis/authen/v1/user_info", 403),
    ],
)
def test_broker_rejects_invalid_targets(monkeypatch, tmp_path: Path, target: str, expected_status: int):
    from hermes_multitenancy.lark_cli_auth_broker import (
        LarkCliAuthBroker,
        LarkCliAuthBrokerContext,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    _store_uat(shared)
    broker = LarkCliAuthBroker(
        LarkCliAuthBrokerContext(
            shared_home=shared,
            profile_name="alice",
            user_open_id="ou_alice",
            hmac_key="proxy-key",
            allowed_identities=frozenset({"user"}),
        ),
        forwarder=lambda *_args, **_kwargs: pytest.fail("should not forward"),
    )

    response = broker.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers=_headers(
            "proxy-key",
            target=target,
            host=urllib.parse.urlparse(target).netloc or "open.feishu.cn",
        ),
        body=b"",
    )

    assert response.status == expected_status


def test_broker_rejects_identity_and_auth_header_mismatch(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.lark_cli_auth_broker import (
        LarkCliAuthBroker,
        LarkCliAuthBrokerContext,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    _store_uat(shared)
    broker = LarkCliAuthBroker(
        LarkCliAuthBrokerContext(
            shared_home=shared,
            profile_name="alice",
            user_open_id="ou_alice",
            hmac_key="proxy-key",
            allowed_identities=frozenset({"user"}),
        ),
        forwarder=lambda *_args, **_kwargs: pytest.fail("should not forward"),
    )

    bot_response = broker.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers=_headers("proxy-key", identity="bot"),
        body=b"",
    )
    cookie_response = broker.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers=_headers("proxy-key", auth_header="Cookie"),
        body=b"",
    )

    assert bot_response.status == 403
    assert cookie_response.status == 403


def test_broker_missing_credential_error_is_redacted(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.lark_cli_auth_broker import (
        LarkCliAuthBroker,
        LarkCliAuthBrokerContext,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    broker = LarkCliAuthBroker(
        LarkCliAuthBrokerContext(
            shared_home=tmp_path / ".hermes",
            profile_name="alice",
            user_open_id="ou_missing",
            hmac_key="proxy-key",
            allowed_identities=frozenset({"user"}),
        ),
        forwarder=lambda *_args, **_kwargs: pytest.fail("should not forward"),
    )

    response = broker.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers=_headers("proxy-key"),
        body=b"",
    )

    assert response.status == 503
    assert b"uat-secret" not in response.body
    assert b"refresh-secret" not in response.body


def test_localhost_server_wraps_broker_protocol(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.lark_cli_auth_broker import (
        BrokerResponse,
        LarkCliAuthBrokerContext,
        start_lark_cli_auth_broker_server,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    _store_uat(shared)
    seen: dict[str, object] = {}

    def fake_forward(method, url, headers, body, timeout):
        seen["url"] = url
        seen["authorization"] = headers.get("Authorization")
        return BrokerResponse(status=201, headers={"X-Test": "ok"}, body=b"proxied")

    server = start_lark_cli_auth_broker_server(
        LarkCliAuthBrokerContext(
            shared_home=shared,
            profile_name="alice",
            user_open_id="ou_alice",
            hmac_key="proxy-key",
            allowed_identities=frozenset({"user"}),
        ),
        forwarder=fake_forward,
    )
    try:
        parsed = urllib.parse.urlparse(server.url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        path = "/open-apis/authen/v1/user_info"
        conn.request("GET", path, headers=_headers("proxy-key", path_and_query=path))
        response = conn.getresponse()
        body = response.read()
    finally:
        server.close()

    assert response.status == 201
    assert body == b"proxied"
    assert seen["url"] == "https://open.feishu.cn/open-apis/authen/v1/user_info"
    assert seen["authorization"] == "Bearer uat-secret"


def test_localhost_server_strips_hop_by_hop_response_headers(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.lark_cli_auth_broker import (
        BrokerResponse,
        LarkCliAuthBrokerContext,
        start_lark_cli_auth_broker_server,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    _store_uat(shared)

    def fake_forward(method, url, headers, body, timeout):
        return BrokerResponse(
            status=200,
            headers={
                "Transfer-Encoding": "chunked",
                "Content-Length": "999",
                "Connection": "keep-alive",
                "X-Test": "ok",
            },
            body=b'{"code":0}',
        )

    server = start_lark_cli_auth_broker_server(
        LarkCliAuthBrokerContext(
            shared_home=shared,
            profile_name="alice",
            user_open_id="ou_alice",
            hmac_key="proxy-key",
            allowed_identities=frozenset({"user"}),
        ),
        forwarder=fake_forward,
    )
    try:
        parsed = urllib.parse.urlparse(server.url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        path = "/open-apis/authen/v1/user_info"
        conn.request("GET", path, headers=_headers("proxy-key", path_and_query=path))
        response = conn.getresponse()
        body = response.read()
    finally:
        server.close()

    response_headers = {key.lower(): value for key, value in response.getheaders()}
    assert body == b'{"code":0}'
    assert response_headers["x-test"] == "ok"
    assert response_headers["content-length"] == str(len(body))
    assert "transfer-encoding" not in response_headers
    assert response_headers.get("connection") != "keep-alive"
