from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from hermes_multitenancy.feishu_helpdesk_client import HelpdeskApiError, HelpdeskClient


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def test_helpdesk_auth_header_is_base64_encoded() -> None:
    client = HelpdeskClient(
        app_id="app-id",
        app_secret="app-secret",
        helpdesk_id="desk-id",
        helpdesk_token="desk-token",
    )

    assert client._helpdesk_auth_header() == base64.b64encode(b"desk-id:desk-token").decode("ascii")


def test_token_is_cached_until_near_expiry_then_re_minted(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[str] = []
    now = {"value": 1_700_000_000.0}

    def fake_urlopen(request: Any, timeout: int = 0) -> FakeResponse:
        requests.append(request.full_url)
        if request.full_url.endswith("/auth/v3/app_access_token/internal/"):
            return FakeResponse(
                {
                    "code": 0,
                    "msg": "ok",
                    "tenant_access_token": f"tenant-{len([url for url in requests if 'auth/v3' in url])}",
                    "expire": 120,
                }
            )
        return FakeResponse({"code": 0, "msg": "ok", "data": {"categories": []}})

    monkeypatch.setattr("hermes_multitenancy.feishu_helpdesk_client.time.time", lambda: now["value"])
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = HelpdeskClient(
        app_id="app-id",
        app_secret="app-secret",
        helpdesk_id="desk-id",
        helpdesk_token="desk-token",
    )

    client.list_categories()
    client.list_categories()
    now["value"] += 61
    client.list_categories()

    token_calls = [url for url in requests if url.endswith("/auth/v3/app_access_token/internal/")]
    assert len(token_calls) == 2


def test_iter_tickets_paginates_and_forwards_query_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_urls: list[str] = []

    def fake_urlopen(request: Any, timeout: int = 0) -> FakeResponse:
        seen_urls.append(request.full_url)
        if request.full_url.endswith("/auth/v3/app_access_token/internal/"):
            return FakeResponse({"code": 0, "msg": "ok", "tenant_access_token": "tenant", "expire": 3600})
        if "page_token=next-page" in request.full_url:
            return FakeResponse(
                {
                    "code": 0,
                    "msg": "ok",
                    "data": {"tickets": [{"ticket_id": "t-2"}], "page_token": ""},
                }
            )
        return FakeResponse(
            {
                "code": 0,
                "msg": "ok",
                "data": {
                    "tickets": [{"ticket_id": "t-1"}],
                    "page_token": "next-page",
                },
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = HelpdeskClient(
        app_id="app-id",
        app_secret="app-secret",
        helpdesk_id="desk-id",
        helpdesk_token="desk-token",
    )

    tickets = list(client.iter_tickets(page_size=1, status="open"))

    assert tickets == [{"ticket_id": "t-1"}, {"ticket_id": "t-2"}]
    ticket_urls = [url for url in seen_urls if "/helpdesk/v1/tickets" in url]
    assert any("status=open" in url and "page_size=1" in url for url in ticket_urls)


def test_iter_faqs_supports_faqs_and_items_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {"faqs": 0}

    def fake_urlopen(request: Any, timeout: int = 0) -> FakeResponse:
        if request.full_url.endswith("/auth/v3/app_access_token/internal/"):
            return FakeResponse({"code": 0, "msg": "ok", "tenant_access_token": "tenant", "expire": 3600})
        if seen["faqs"] == 0:
            seen["faqs"] += 1
            return FakeResponse(
                {
                    "code": 0,
                    "msg": "ok",
                    "data": {"faqs": [{"faq_id": "f-1"}], "page_token": "next"},
                }
            )
        return FakeResponse(
            {
                "code": 0,
                "msg": "ok",
                "data": {"items": [{"faq_id": "f-2"}], "page_token": ""},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = HelpdeskClient(
        app_id="app-id",
        app_secret="app-secret",
        helpdesk_id="desk-id",
        helpdesk_token="desk-token",
    )

    faqs = list(client.iter_faqs(page_size=1))

    assert faqs == [{"faq_id": "f-1"}, {"faq_id": "f-2"}]


def test_non_zero_envelope_code_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: int = 0) -> FakeResponse:
        if request.full_url.endswith("/auth/v3/app_access_token/internal/"):
            return FakeResponse({"code": 0, "msg": "ok", "tenant_access_token": "tenant", "expire": 3600})
        return FakeResponse({"code": 40042, "msg": "bad request", "data": {}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = HelpdeskClient(
        app_id="app-id",
        app_secret="app-secret",
        helpdesk_id="desk-id",
        helpdesk_token="desk-token",
    )

    with pytest.raises(HelpdeskApiError) as excinfo:
        client.list_categories()

    assert excinfo.value.code == 40042
    assert excinfo.value.msg == "bad request"
    assert "40042" in str(excinfo.value)
    assert "bad request" in str(excinfo.value)


def test_invalid_envelope_shape_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: int = 0) -> FakeResponse:
        if request.full_url.endswith("/auth/v3/app_access_token/internal/"):
            return FakeResponse({"code": 0, "msg": "ok", "tenant_access_token": "tenant", "expire": 3600})
        return FakeResponse({"data": {"categories": []}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = HelpdeskClient(
        app_id="app-id",
        app_secret="app-secret",
        helpdesk_id="desk-id",
        helpdesk_token="desk-token",
    )

    with pytest.raises(ValueError):
        client.list_categories()


def test_get_ticket_messages_supports_messages_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: int = 0) -> FakeResponse:
        if request.full_url.endswith("/auth/v3/app_access_token/internal/"):
            return FakeResponse({"code": 0, "msg": "ok", "tenant_access_token": "tenant", "expire": 3600})
        return FakeResponse(
            {
                "code": 0,
                "msg": "ok",
                "data": {"messages": [{"message_id": "m-1"}]},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = HelpdeskClient(
        app_id="app-id",
        app_secret="app-secret",
        helpdesk_id="desk-id",
        helpdesk_token="desk-token",
    )

    assert client.get_ticket_messages("ticket-1") == [{"message_id": "m-1"}]


def test_get_ticket_messages_supports_items_shape_and_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {"messages": 0}

    def fake_urlopen(request: Any, timeout: int = 0) -> FakeResponse:
        if request.full_url.endswith("/auth/v3/app_access_token/internal/"):
            return FakeResponse({"code": 0, "msg": "ok", "tenant_access_token": "tenant", "expire": 3600})
        if seen["messages"] == 0:
            seen["messages"] += 1
            return FakeResponse(
                {
                    "code": 0,
                    "msg": "ok",
                    "data": {"items": [{"message_id": "m-1"}], "page_token": "next"},
                }
            )
        return FakeResponse(
            {
                "code": 0,
                "msg": "ok",
                "data": {"items": [{"message_id": "m-2"}], "page_token": ""},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = HelpdeskClient(
        app_id="app-id",
        app_secret="app-secret",
        helpdesk_id="desk-id",
        helpdesk_token="desk-token",
    )

    assert client.get_ticket_messages("ticket-1") == [{"message_id": "m-1"}, {"message_id": "m-2"}]


def test_request_json_retries_transient_then_succeeds(monkeypatch):
    """Transient URLError/timeout must auto-recover; real HTTPError must not."""
    import socket
    import urllib.error
    from hermes_multitenancy import feishu_helpdesk_client as mod

    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    client = HelpdeskClient(app_id="a", app_secret="s", helpdesk_id="h", helpdesk_token="t")

    calls = {"n": 0}

    def flaky_urlopen(request, timeout=0):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError(socket.timeout("handshake timed out"))
        return FakeResponse({"code": 0, "msg": "ok", "data": {"ok": True}})

    monkeypatch.setattr(mod.urllib.request, "urlopen", flaky_urlopen)
    out = client._request_json("https://open.feishu.cn/x")
    assert out["data"] == {"ok": True}
    assert calls["n"] == 3  # failed twice, succeeded on third


def test_request_json_does_not_retry_http_error(monkeypatch):
    import urllib.error
    from hermes_multitenancy import feishu_helpdesk_client as mod

    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    client = HelpdeskClient(app_id="a", app_secret="s", helpdesk_id="h", helpdesk_token="t")
    calls = {"n": 0}

    def http_err(request, timeout=0):
        calls["n"] += 1
        raise urllib.error.HTTPError("https://open.feishu.cn/x", 401, "unauth", {}, None)

    monkeypatch.setattr(mod.urllib.request, "urlopen", http_err)
    with pytest.raises(urllib.error.HTTPError):
        client._request_json("https://open.feishu.cn/x")
    assert calls["n"] == 1  # not retried
