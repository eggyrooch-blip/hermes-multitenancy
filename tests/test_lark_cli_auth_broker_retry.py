from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

from hermes_multitenancy.lark_cli_auth_broker import (
    BrokerResponse,
    CredentialExpiredError,
    LarkCliAuthBroker,
    LarkCliAuthBrokerContext,
    _CRED_RESOLVE_BACKOFFS,
)


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
    method = overrides.pop("method", "GET")
    target = overrides.pop("target", "https://open.feishu.cn")
    host = overrides.pop("host", "open.feishu.cn")
    path_and_query = overrides.pop("path_and_query", "/open-apis/authen/v1/user_info")
    signature = _sign(
        key,
        method=method,
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


def _build_broker(tmp_path: Path, forward_calls: list[dict[str, object]]) -> LarkCliAuthBroker:
    def fake_forward(method, url, headers, body, timeout):
        forward_calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        return BrokerResponse(status=200, body=b'{"code":0}')

    return LarkCliAuthBroker(
        LarkCliAuthBrokerContext(
            shared_home=tmp_path / ".hermes",
            profile_name="alice",
            user_open_id="ou_alice",
            hmac_key="proxy-key",
            allowed_identities=frozenset({"user"}),
        ),
        forwarder=fake_forward,
    )


def test_broker_retries_transient_credential_resolution_and_self_heals(monkeypatch, tmp_path: Path) -> None:
    forward_calls: list[dict[str, object]] = []
    broker = _build_broker(tmp_path, forward_calls)
    resolve_calls: list[str] = []
    sleeps: list[tuple[object, ...]] = []

    def fake_resolve(identity: str) -> str:
        resolve_calls.append(identity)
        if len(resolve_calls) == 1:
            raise PermissionError("credential unavailable")
        return "good-token"

    monkeypatch.setattr(broker, "_resolve_token", fake_resolve)
    monkeypatch.setattr("hermes_multitenancy.lark_cli_auth_broker.time.sleep", lambda *args, **kwargs: sleeps.append(args))

    response = broker.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers=_headers("proxy-key"),
        body=b"",
    )

    assert response.status == 200
    assert len(resolve_calls) == 2
    assert len(forward_calls) == 1
    assert forward_calls[0]["headers"]["Authorization"] == "Bearer good-token"
    assert sleeps == [(_CRED_RESOLVE_BACKOFFS[0],)]


def test_broker_does_not_retry_terminal_expired_credential(monkeypatch, tmp_path: Path) -> None:
    forward_calls: list[dict[str, object]] = []
    broker = _build_broker(tmp_path, forward_calls)
    resolve_calls: list[str] = []
    sleeps: list[tuple[object, ...]] = []

    def fake_resolve(identity: str) -> str:
        resolve_calls.append(identity)
        raise CredentialExpiredError("credential expired")

    monkeypatch.setattr(broker, "_resolve_token", fake_resolve)
    monkeypatch.setattr("hermes_multitenancy.lark_cli_auth_broker.time.sleep", lambda *args, **kwargs: sleeps.append(args))

    response = broker.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers=_headers("proxy-key"),
        body=b"",
    )

    assert response.status == 503
    assert b"credential expired" in response.body
    assert len(resolve_calls) == 1
    assert sleeps == []
    assert forward_calls == []


def test_broker_returns_503_after_bounded_transient_retries(monkeypatch, tmp_path: Path) -> None:
    forward_calls: list[dict[str, object]] = []
    broker = _build_broker(tmp_path, forward_calls)
    resolve_calls: list[str] = []
    sleeps: list[tuple[object, ...]] = []

    def fake_resolve(identity: str) -> str:
        resolve_calls.append(identity)
        raise PermissionError("credential unavailable")

    monkeypatch.setattr(broker, "_resolve_token", fake_resolve)
    monkeypatch.setattr("hermes_multitenancy.lark_cli_auth_broker.time.sleep", lambda *args, **kwargs: sleeps.append(args))

    response = broker.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers=_headers("proxy-key"),
        body=b"",
    )

    assert response.status == 503
    assert b"credential unavailable" in response.body
    assert len(resolve_calls) == 1 + len(_CRED_RESOLVE_BACKOFFS)
    assert len(sleeps) == len(_CRED_RESOLVE_BACKOFFS)
    assert forward_calls == []


def test_broker_happy_path_still_forwards_without_sleep(monkeypatch, tmp_path: Path) -> None:
    forward_calls: list[dict[str, object]] = []
    broker = _build_broker(tmp_path, forward_calls)
    resolve_calls: list[str] = []
    sleeps: list[tuple[object, ...]] = []

    def fake_resolve(identity: str) -> str:
        resolve_calls.append(identity)
        return "good-token"

    monkeypatch.setattr(broker, "_resolve_token", fake_resolve)
    monkeypatch.setattr("hermes_multitenancy.lark_cli_auth_broker.time.sleep", lambda *args, **kwargs: sleeps.append(args))

    response = broker.handle(
        method="GET",
        path_and_query="/open-apis/authen/v1/user_info",
        headers=_headers("proxy-key"),
        body=b"",
    )

    assert response.status == 200
    assert len(resolve_calls) == 1
    assert len(forward_calls) == 1
    assert sleeps == []
