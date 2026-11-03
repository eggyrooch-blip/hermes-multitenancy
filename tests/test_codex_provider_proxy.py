from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from hermes_multitenancy.agent_real import codex_provider_proxy


class _Response:
    def __init__(self, status: int, chunks: list[bytes], content_type: str = "text/event-stream"):
        self.status = status
        self.reason = "test"
        self._chunks = iter(chunks)
        self.headers = {"Content-Type": content_type, "Location": "https://evil.invalid/"}

    def read(self, _size: int = -1) -> bytes:
        return next(self._chunks, b"")


class _Connection:
    def __init__(self, response: _Response, observed: dict):
        self._response = response
        self._observed = observed

    def request(self, method, path, *, body, headers):
        self._observed.update(method=method, path=path, body=body, headers=headers)

    def getresponse(self):
        return self._response

    def close(self):
        self._observed["closed"] = True


def _post(base_url: str, token: str, body: bytes = b'{"stream":true}'):
    request = urllib.request.Request(
        f"{base_url}/responses",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=2)


def test_per_run_proxy_keeps_upstream_key_server_side_and_audits_sse(monkeypatch):
    observed: dict = {}
    chunks = [
        b'event: response.output_item.added\ndata: {"type":"response.output_item.added","item":{"type":"function_call"}}\n\n',
        b'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":2,"output_tokens":1}}}\n\n',
    ]
    monkeypatch.setattr(
        codex_provider_proxy,
        "_open_upstream",
        lambda _url: _Connection(_Response(200, chunks), observed),
    )

    proxy = codex_provider_proxy.start(
        upstream_base_url="https://litellm.example/v1",
        upstream_api_key="sk-real-key-must-stay-parent-side",
    )
    try:
        with _post(proxy.base_url, proxy.token) as response:
            assert response.status == 200
            assert response.read() == b"".join(chunks)
        assert observed["method"] == "POST"
        assert observed["path"] == "/v1/responses"
        assert observed["headers"]["Authorization"] == "Bearer sk-real-key-must-stay-parent-side"
        assert b"sk-real-key" not in observed["body"]
        assert json.loads(observed["body"]) == {"stream": True, "store": False}
        assert proxy.audit.snapshot() == {
            "request_count": 1,
            "request_limit": 1,
            "rejected_requests": 0,
            "rejected_over_limit": 0,
            "rejected_concurrent": 0,
            "budget_exhausted": False,
            "response_completed": 1,
            "usage_response_count": 1,
            "tool_events": 1,
            "usage_present": True,
            "store_forced": True,
            "input_tokens": 2,
            "output_tokens": 1,
            "total_tokens": 3,
            "cache_read_tokens": 0,
        }
        with pytest.raises(urllib.error.HTTPError) as second:
            _post(proxy.base_url, proxy.token)
        assert second.value.code == 409
    finally:
        proxy.close()

    with pytest.raises(OSError):
        _post(proxy.base_url, proxy.token)


def test_harness_proxy_sums_multiple_completed_responses(monkeypatch):
    responses = iter(
        [
            _Response(
                200,
                [
                    b'data: {"type":"response.output_item.added","item":{"type":"function_call"}}\n\n',
                    b'data: {"type":"response.completed","response":{"usage":{"input_tokens":2,"output_tokens":1,"total_tokens":3,"input_tokens_details":{"cached_tokens":1}}}}\n\n',
                ],
            ),
            _Response(
                200,
                [
                    b'data: {"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":2,"input_tokens_details":{"cached_tokens":2}}}}\n\n'
                ],
            ),
        ]
    )
    monkeypatch.setattr(
        codex_provider_proxy,
        "_open_upstream",
        lambda _url: _Connection(next(responses), {}),
    )
    proxy = codex_provider_proxy.start(
        upstream_base_url="https://litellm.example/v1",
        upstream_api_key="sk-real-key",
        max_requests=codex_provider_proxy.MAX_HARNESS_REQUESTS,
    )
    try:
        for _ in range(2):
            with _post(proxy.base_url, proxy.token) as response:
                response.read()
        assert proxy.audit.snapshot() == {
            "request_count": 2,
            "request_limit": codex_provider_proxy.MAX_HARNESS_REQUESTS,
            "rejected_requests": 0,
            "rejected_over_limit": 0,
            "rejected_concurrent": 0,
            "budget_exhausted": False,
            "response_completed": 2,
            "usage_response_count": 2,
            "tool_events": 1,
            "usage_present": True,
            "store_forced": True,
            "input_tokens": 5,
            "output_tokens": 3,
            "total_tokens": 8,
            "cache_read_tokens": 3,
        }
    finally:
        proxy.close()


def test_harness_proxy_rejects_the_request_over_the_limit(monkeypatch):
    chunk = b'data: {"type":"response.completed","response":{"usage":{}}}\n\n'
    monkeypatch.setattr(
        codex_provider_proxy,
        "_open_upstream",
        lambda _url: _Connection(_Response(200, [chunk]), {}),
    )
    proxy = codex_provider_proxy.start(
        upstream_base_url="https://litellm.example/v1",
        upstream_api_key="sk-real-key",
        max_requests=codex_provider_proxy.MAX_HARNESS_REQUESTS,
    )
    try:
        for _ in range(codex_provider_proxy.MAX_HARNESS_REQUESTS):
            with _post(proxy.base_url, proxy.token) as response:
                response.read()
        with pytest.raises(urllib.error.HTTPError) as over_limit:
            _post(proxy.base_url, proxy.token)
        assert over_limit.value.code == 409
        assert json.loads(over_limit.value.read())["error"]["type"] == (
            "request_limit_exceeded"
        )
        audit = proxy.audit.snapshot()
        assert audit["request_count"] == codex_provider_proxy.MAX_HARNESS_REQUESTS
        assert audit["rejected_over_limit"] == 1
        assert audit["rejected_concurrent"] == 0
        assert audit["rejected_requests"] == 1
        assert audit["budget_exhausted"] is True
    finally:
        proxy.close()


def test_harness_proxy_admits_requests_past_any_token_total(monkeypatch):
    """token 总量上限已关闭(sunke 2026-09-04): 累计 300 万 token 照样放行。"""
    assert codex_provider_proxy.MAX_HARNESS_TOTAL_TOKENS is None
    spent = (
        b'data: {"type":"response.completed","response":{"usage":'
        b'{"input_tokens":0,"output_tokens":0,"total_tokens":1500000}}}\n\n'
    )
    monkeypatch.setattr(
        codex_provider_proxy,
        "_open_upstream",
        lambda _url: _Connection(_Response(200, [spent]), {}),
    )
    proxy = codex_provider_proxy.start(
        upstream_base_url="https://litellm.example/v1",
        upstream_api_key="sk-real-key",
        max_requests=codex_provider_proxy.MAX_HARNESS_REQUESTS,
    )
    try:
        for _ in range(2):
            with _post(proxy.base_url, proxy.token) as response:
                response.read()
        with _post(proxy.base_url, proxy.token) as response:
            assert response.status == 200
            response.read()
        audit = proxy.audit.snapshot()
        assert audit["request_count"] == 3
        assert audit["total_tokens"] == 4_500_000
        assert audit["rejected_over_limit"] == 0
        assert audit["rejected_concurrent"] == 0
        assert audit["budget_exhausted"] is False
    finally:
        proxy.close()


def test_next_request_starts_before_previous_connection_finishes(monkeypatch):
    first_closing = threading.Event()
    release_first = threading.Event()
    opened = 0
    chunk = b'data: {"type":"response.completed","response":{"usage":{}}}\n\n'

    class SlowFirstClose(_Connection):
        def close(self):
            first_closing.set()
            release_first.wait(2)
            super().close()

    def open_upstream(_url):
        nonlocal opened
        opened += 1
        connection = _Connection(_Response(200, [chunk]), {})
        return SlowFirstClose(connection._response, {}) if opened == 1 else connection

    monkeypatch.setattr(codex_provider_proxy, "_open_upstream", open_upstream)
    proxy = codex_provider_proxy.start(
        upstream_base_url="https://litellm.example/v1",
        upstream_api_key="sk-real-key",
        max_requests=codex_provider_proxy.MAX_HARNESS_REQUESTS,
    )
    first = threading.Thread(
        target=lambda: _post(proxy.base_url, proxy.token).read(), daemon=True
    )
    try:
        first.start()
        assert first_closing.wait(1)
        with _post(proxy.base_url, proxy.token) as response:
            response.read()
        assert proxy.audit.snapshot()["rejected_requests"] == 0
    finally:
        release_first.set()
        first.join(timeout=1)
        proxy.close()


def test_per_run_proxy_forces_no_storage(monkeypatch):
    observed: dict = {}
    monkeypatch.setattr(
        codex_provider_proxy,
        "_open_upstream",
        lambda _url: _Connection(_Response(200, []), observed),
    )
    proxy = codex_provider_proxy.start(
        upstream_base_url="https://litellm.example/v1",
        upstream_api_key="sk-real-key",
    )
    try:
        with _post(proxy.base_url, proxy.token, b'{"stream":true,"store":true}'):
            pass
        assert json.loads(observed["body"])["store"] is False
    finally:
        proxy.close()


def test_per_run_proxy_drops_provider_message_ids_from_replayed_input(monkeypatch):
    observed: dict = {}
    monkeypatch.setattr(
        codex_provider_proxy,
        "_open_upstream",
        lambda _url: _Connection(_Response(200, []), observed),
    )
    proxy = codex_provider_proxy.start(
        upstream_base_url="https://litellm.example/v1",
        upstream_api_key="sk-real-key",
    )
    body = json.dumps(
        {
            "stream": True,
            "input": [
                {"type": "message", "id": "resp_chat_bad_msg", "role": "assistant"},
                {"type": "function_call", "id": "fc_keep", "call_id": "call_keep"},
            ],
        }
    ).encode()
    try:
        with _post(proxy.base_url, proxy.token, body):
            pass
        items = json.loads(observed["body"])["input"]
        assert "id" not in items[0]
        assert items[1]["id"] == "fc_keep"
    finally:
        proxy.close()


def test_close_revokes_an_accepted_half_body_before_upstream(monkeypatch):
    observed: dict = {}
    monkeypatch.setattr(
        codex_provider_proxy,
        "_open_upstream",
        lambda _url: _Connection(_Response(200, []), observed),
    )
    proxy = codex_provider_proxy.start(
        upstream_base_url="https://litellm.example/v1",
        upstream_api_key="sk-real-key",
        max_requests=codex_provider_proxy.MAX_HARNESS_REQUESTS,
    )
    parsed = urllib.parse.urlsplit(proxy.base_url)
    body = b'{"stream":true}'
    client = socket.create_connection(("127.0.0.1", parsed.port), timeout=2)
    client.sendall(
        (
            f"POST {parsed.path}/responses HTTP/1.0\r\n"
            f"Host: 127.0.0.1\r\nAuthorization: Bearer {proxy.token}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode()
        + body[:1]
    )
    deadline = time.monotonic() + 1
    while proxy.audit.snapshot()["request_count"] != 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    with pytest.raises(urllib.error.HTTPError) as second:
        _post(proxy.base_url, proxy.token)
    assert second.value.code == 409
    concurrent_audit = proxy.audit.snapshot()
    assert concurrent_audit["rejected_concurrent"] == 1
    assert concurrent_audit["rejected_over_limit"] == 0
    assert concurrent_audit["rejected_requests"] == 1
    assert concurrent_audit["budget_exhausted"] is False
    proxy.close()
    try:
        client.sendall(body[1:])
    except OSError:
        pass
    client.close()
    time.sleep(0.05)
    assert "method" not in observed
    assert proxy._key_holder == [""]


def test_close_interrupts_a_blocked_upstream_request(monkeypatch):
    entered = threading.Event()
    released = threading.Event()

    class BlockingConnection:
        def request(self, *_args, **_kwargs):
            entered.set()
            released.wait(2)
            raise OSError("closed")

        def close(self):
            released.set()

    monkeypatch.setattr(
        codex_provider_proxy,
        "_open_upstream",
        lambda _url: BlockingConnection(),
    )
    proxy = codex_provider_proxy.start(
        upstream_base_url="https://litellm.example/v1",
        upstream_api_key="sk-real-key",
    )
    request_thread = threading.Thread(
        target=lambda: pytest.raises(Exception, _post, proxy.base_url, proxy.token),
        daemon=True,
    )
    request_thread.start()
    assert entered.wait(1)
    started = time.monotonic()
    proxy.close()
    assert time.monotonic() - started < 0.25
    assert released.is_set()
    assert proxy._key_holder == [""]
    request_thread.join(timeout=1)


def test_per_run_proxy_rejects_wrong_auth_path_and_redirect(monkeypatch):
    observed: dict = {}
    monkeypatch.setattr(
        codex_provider_proxy,
        "_open_upstream",
        lambda _url: _Connection(_Response(302, [b"redirect"]), observed),
    )
    proxy = codex_provider_proxy.start(
        upstream_base_url="https://litellm.example/v1",
        upstream_api_key="sk-real-key",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as wrong_auth:
            _post(proxy.base_url, "wrong")
        assert wrong_auth.value.code == 401

        request = urllib.request.Request(
            f"{proxy.base_url}/models",
            data=b"{}",
            headers={"Authorization": f"Bearer {proxy.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as wrong_path:
            urllib.request.urlopen(request, timeout=2)
        assert wrong_path.value.code == 404

        with pytest.raises(urllib.error.HTTPError) as redirect:
            _post(proxy.base_url, proxy.token)
        assert redirect.value.code == 502
        assert redirect.value.headers.get("Location") is None

        with pytest.raises(urllib.error.HTTPError) as second_request:
            _post(proxy.base_url, proxy.token)
        assert second_request.value.code == 409
    finally:
        proxy.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://litellm.example/v1",
        "https://user:pass@litellm.example/v1",
        "https://litellm.example/v1?x=1",
        "https://litellm.example/v1#fragment",
    ],
)
def test_per_run_proxy_rejects_unsafe_upstream_url(url):
    with pytest.raises(ValueError, match="upstream"):
        codex_provider_proxy.start(upstream_base_url=url, upstream_api_key="sk-real-key")


def test_per_run_proxy_rejects_unbounded_request_limit():
    with pytest.raises(ValueError, match="request limit"):
        codex_provider_proxy.start(
            upstream_base_url="https://litellm.example/v1",
            upstream_api_key="sk-real-key",
            max_requests=codex_provider_proxy.MAX_HARNESS_REQUESTS + 1,
        )


def test_child_runtime_accepts_only_disposable_loopback_credential(monkeypatch):
    metadata = {"litellm_billing_employee_user_id": "alice"}
    monkeypatch.setenv("CODEX_RUNTIME_KEY", "hcx_disposable")
    monkeypatch.setenv(
        codex_provider_proxy.PROXY_BASE_URL_ENV,
        "http://127.0.0.1:34567/run_scoped_route_123/v1",
    )
    assert codex_provider_proxy.runtime_from_environment(metadata)["api_key"] == "hcx_disposable"

    monkeypatch.setenv("CODEX_RUNTIME_KEY", "sk-real-key")
    with pytest.raises(RuntimeError, match="proxy runtime"):
        codex_provider_proxy.runtime_from_environment(metadata)


@pytest.mark.parametrize(
    "usage",
    [
        "{}",
        '{"input_tokens":"2","output_tokens":1}',
        '{"input_tokens":2,"output_tokens":-1}',
        '{"total_tokens":-5}',
        '"nope"',
        "null",
    ],
    ids=["empty", "string", "negative-output", "negative-total", "scalar", "null"],
)
def test_malformed_usage_is_never_counted_as_usage_present(monkeypatch, usage):
    """审计不能把 `{}`/字符串/负数 usage 当成「usage 齐」——收尾门要 fail-closed。"""
    chunk = (
        b'data: {"type":"response.completed","response":{"usage":'
        + usage.encode()
        + b"}}\n\n"
    )
    monkeypatch.setattr(
        codex_provider_proxy,
        "_open_upstream",
        lambda _url: _Connection(_Response(200, [chunk]), {}),
    )
    proxy = codex_provider_proxy.start(
        upstream_base_url="https://litellm.example/v1",
        upstream_api_key="sk-real-key",
    )
    try:
        with _post(proxy.base_url, proxy.token) as response:
            response.read()
        audit = proxy.audit.snapshot()
        assert audit["response_completed"] == 1
        assert audit["usage_response_count"] == 0
        assert audit["usage_present"] is False
        assert audit["total_tokens"] == 0
        # 门的判据: usage_response_count != request_count → incomplete
        assert audit["usage_response_count"] != audit["request_count"]
    finally:
        proxy.close()


def test_valid_usage_shapes_are_still_counted(monkeypatch):
    """只有 total、或只有 input+output,都是合法形状,照旧计量。"""
    chunks = [
        b'data: {"type":"response.completed","response":{"usage":'
        b'{"input_tokens":2,"output_tokens":3}}}\n\n',
        b'data: {"type":"response.completed","response":{"usage":'
        b'{"total_tokens":10,"input_tokens_details":{"cached_tokens":4}}}}\n\n',
    ]
    monkeypatch.setattr(
        codex_provider_proxy,
        "_open_upstream",
        lambda _url: _Connection(_Response(200, chunks), {}),
    )
    proxy = codex_provider_proxy.start(
        upstream_base_url="https://litellm.example/v1",
        upstream_api_key="sk-real-key",
    )
    try:
        with _post(proxy.base_url, proxy.token) as response:
            response.read()
        audit = proxy.audit.snapshot()
        assert audit["usage_response_count"] == 2
        assert audit["usage_present"] is True
        assert audit["input_tokens"] == 2
        assert audit["output_tokens"] == 3
        assert audit["total_tokens"] == 15
        assert audit["cache_read_tokens"] == 4
    finally:
        proxy.close()
