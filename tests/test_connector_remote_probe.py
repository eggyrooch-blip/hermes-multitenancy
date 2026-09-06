import asyncio

import pytest


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/mcp",
        "https://user:secret@example.com/mcp",
        "https://example.com/mcp?token=secret",
        "https://127.0.0.1/mcp",
        "https://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
    ],
)
def test_remote_endpoint_guard_rejects_unsafe_urls(url: str):
    from hermes_multitenancy.connector_remote_probe import validate_remote_endpoint

    with pytest.raises(ValueError):
        validate_remote_endpoint(url)


def test_remote_endpoint_guard_rejects_hostnames_resolving_private():
    from hermes_multitenancy.connector_remote_probe import validate_remote_endpoint

    def private_dns(*_args, **_kwargs):
        return [(2, 1, 6, "", ("10.0.0.8", 443))]

    with pytest.raises(ValueError, match="public"):
        validate_remote_endpoint("https://mcp.example/mcp", resolver=private_dns)


@pytest.mark.parametrize("address", ["::ffff:127.0.0.1", "::ffff:169.254.169.254"])
def test_remote_endpoint_guard_rejects_ipv4_mapped_ipv6_private_addresses(address: str):
    from hermes_multitenancy.connector_remote_probe import validate_remote_endpoint

    def mapped_dns(*_args, **_kwargs):
        return [(10, 1, 6, "", (address, 443, 0, 0))]

    with pytest.raises(ValueError, match="public"):
        validate_remote_endpoint("https://mcp.example/mcp", resolver=mapped_dns)


def test_streamable_http_probe_initializes_and_lists_tools_without_credentials():
    from hermes_multitenancy.connector_remote_probe import ProbeResponse, probe_remote_endpoint

    calls = []

    async def request(url, payload, headers, _addresses):
        calls.append((url, payload, dict(headers)))
        if payload.get("method") == "initialize":
            return ProbeResponse(
                200,
                {"Mcp-Session-Id": "session-1"},
                {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-11-25"}},
            )
        if payload.get("method") == "notifications/initialized":
            return ProbeResponse(202, {}, None)
        return ProbeResponse(
            200,
            {},
            {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "search"}]}},
        )

    def public_dns(*_args, **_kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    result = asyncio.run(
        probe_remote_endpoint(
            "https://mcp.example/mcp",
            resolver=public_dns,
            request=request,
        )
    )

    assert result["verdict"] == "pass"
    assert result["reason_code"] == "tools_list_ok"
    assert result["tool_count"] == 1
    assert [item["stage"] for item in result["evidence"]] == [
        "url_validation",
        "initialize",
        "initialized",
        "tools_list",
    ]
    assert calls[1][2]["Mcp-Session-Id"] == "session-1"
    assert all("Authorization" not in headers for _, _, headers in calls)


def test_remote_probe_refuses_redirect_without_following_it():
    from hermes_multitenancy.connector_remote_probe import ProbeResponse, probe_remote_endpoint

    calls = []

    async def redirect(url, payload, headers, addresses):
        calls.append((url, payload, headers, addresses))
        return ProbeResponse(302, {"Location": "https://evil.example/mcp"}, None)

    public_dns = lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
    result = asyncio.run(
        probe_remote_endpoint(
            "https://mcp.example/mcp",
            resolver=public_dns,
            request=redirect,
        )
    )

    assert result["verdict"] == "rejected"
    assert result["reason_code"] == "redirect_refused"
    assert len(calls) == 1


def test_remote_probe_retries_only_explicit_protocol_version_rejection():
    from hermes_multitenancy.connector_remote_probe import ProbeResponse, probe_remote_endpoint

    versions = []

    async def request(_url, payload, _headers, _addresses):
        versions.append(payload.get("params", {}).get("protocolVersion"))
        if len(versions) == 1:
            return ProbeResponse(400, {}, {
                "jsonrpc": "2.0", "error": {"message": "Unsupported MCP-Protocol-Version"}
            })
        if payload.get("method") == "initialize":
            return ProbeResponse(200, {}, {"jsonrpc": "2.0", "id": 1, "result": {}})
        if payload.get("method") == "notifications/initialized":
            return ProbeResponse(202, {}, None)
        return ProbeResponse(200, {}, {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})

    public_dns = lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
    result = asyncio.run(probe_remote_endpoint(
        "https://mcp.example/mcp", resolver=public_dns, request=request
    ))
    assert result["verdict"] == "pass"
    assert versions[:2] == ["2025-11-25", "2025-06-18"]


def test_remote_probe_recognizes_structured_api_key_challenge():
    from hermes_multitenancy.connector_remote_probe import ProbeResponse, probe_remote_endpoint

    async def request(*_args):
        return ProbeResponse(200, {}, {
            "status": {"error": {"type": "AUTH_KEY_INVALID", "message": "missing"}}
        })

    public_dns = lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
    result = asyncio.run(probe_remote_endpoint(
        "https://mcp.example/mcp", resolver=public_dns, request=request
    ))
    assert result["verdict"] == "needs_auth"
    assert result["reason_code"] == "remote_auth_required"


def test_remote_probe_dispatches_legacy_sse_transport():
    from hermes_multitenancy.connector_remote_probe import probe_remote_endpoint

    called = []

    async def legacy_sse(endpoint):
        called.append(endpoint.host)
        return {"verdict": "pass", "complete": True, "reason_code": "tools_list_ok"}

    public_dns = lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
    result = asyncio.run(
        probe_remote_endpoint(
            "https://mcp.example/sse",
            transport="sse",
            resolver=public_dns,
            sse_probe=legacy_sse,
        )
    )

    assert result["verdict"] == "pass"
    assert called == ["mcp.example"]


def test_streamable_sse_response_selects_the_matching_jsonrpc_id():
    from hermes_multitenancy.connector_remote_probe import _decode_streamable_body

    body = _decode_streamable_body(
        b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
        b'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n\n',
        "text/event-stream; charset=utf-8",
        2,
    )
    assert body == {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}


def test_bounded_remote_read_collects_chunked_body_until_eof():
    from aiohttp import StreamReader
    from aiohttp.base_protocol import BaseProtocol

    from hermes_multitenancy.connector_remote_probe import _read_bounded

    async def check():
        loop = asyncio.get_running_loop()
        stream = StreamReader(BaseProtocol(loop), 2**16, loop=loop)
        stream.feed_data(b"first")
        stream.feed_data(b"-second")
        stream.feed_eof()
        assert await _read_bounded(stream, 1024) == b"first-second"

    asyncio.run(check())
