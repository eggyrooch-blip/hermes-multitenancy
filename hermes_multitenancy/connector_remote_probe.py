"""Credential-free, SSRF-safe MCP Streamable HTTP probe."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import socket
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import aiohttp


def _latest_protocol_version() -> str:
    try:
        from mcp.types import LATEST_PROTOCOL_VERSION

        return LATEST_PROTOCOL_VERSION
    except ImportError:
        # Keep the non-executing URL/catalog validator usable in slim admin installs.
        return "2025-11-25"


@dataclass(frozen=True)
class ProbeResponse:
    status: int
    headers: dict[str, str]
    body: Any


@dataclass(frozen=True)
class ValidatedEndpoint:
    url: str
    host: str
    port: int
    addresses: tuple[str, ...]


def validate_remote_endpoint(
    url: str,
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> ValidatedEndpoint:
    try:
        parsed = urlsplit(str(url or "").strip())
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("malformed endpoint URL") from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme.casefold() != "https" or not host:
        raise ValueError("endpoint must use HTTPS with a hostname")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("endpoint URL must not contain credentials, query, or fragment")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
        raise ValueError("endpoint host must be public")
    try:
        records = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("endpoint DNS resolution failed") from exc
    addresses = tuple(sorted({str(record[4][0]) for record in records if record[4]}))
    parsed_addresses = tuple(ipaddress.ip_address(value) for value in addresses)
    if not parsed_addresses or any(
        not address.is_global
        or (address.version == 6 and address.ipv4_mapped is not None and not address.ipv4_mapped.is_global)
        for address in parsed_addresses
    ):
        raise ValueError("endpoint must resolve only to public addresses")
    return ValidatedEndpoint(str(url).strip(), host, port, addresses)


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, endpoint: ValidatedEndpoint) -> None:
        self.endpoint = endpoint

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            }
            for address in self.endpoint.addresses
        ]

    async def close(self) -> None:
        return None


def _header(headers: dict[str, str], name: str) -> str:
    wanted = name.casefold()
    return next((str(value) for key, value in headers.items() if key.casefold() == wanted), "")


def _decode_streamable_body(data: bytes, content_type: str, request_id: Any) -> Any:
    text = data.decode("utf-8", errors="replace").strip()
    candidates = [text]
    is_sse = content_type.casefold().startswith("text/event-stream")
    if is_sse:
        candidates, event = [], []
        for line in text.splitlines():
            if not line.strip():
                if event:
                    candidates.append("\n".join(event))
                    event = []
            elif line.startswith("data:"):
                event.append(line[5:].lstrip())
        if event:
            candidates.append("\n".join(event))
    decoded = []
    for candidate in candidates:
        try:
            decoded.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    if is_sse and request_id is not None:
        return next((item for item in decoded if isinstance(item, dict) and item.get("id") == request_id), {
            "_invalid_json": True
        })
    return decoded[-1] if decoded else None


async def _read_bounded(stream: aiohttp.StreamReader, limit: int) -> bytes:
    try:
        return await stream.readexactly(limit + 1)
    except asyncio.IncompleteReadError as exc:
        return exc.partial


def _result(
    verdict: str,
    reason_code: str,
    evidence: list[dict[str, Any]],
    *,
    tool_count: int | None = None,
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "complete": True,
        "reason_code": reason_code,
        "evidence": evidence,
        "tool_count": tool_count,
    }


def _response_failure(response: ProbeResponse, evidence: list[dict[str, Any]], stage: str):
    evidence.append({"stage": stage, "status": "failed", "http_status": response.status})
    if response.status in {401, 403}:
        return _result("needs_auth", "remote_auth_required", evidence)
    if 300 <= response.status < 400:
        return _result("rejected", "redirect_refused", evidence)
    return _result("incompatible", f"{stage}_http_error", evidence)


def _auth_error(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    text = json.dumps(body, ensure_ascii=False).casefold()
    return any(marker in text for marker in (
        "auth_key_invalid", "permission_denied", "missing api key", "缺少api密钥",
    ))


def _unsupported_protocol(response: ProbeResponse) -> bool:
    return response.status == 400 and "unsupported mcp-protocol-version" in json.dumps(
        response.body, ensure_ascii=False
    ).casefold()


async def _run_probe(
    endpoint: ValidatedEndpoint,
    request: Callable[[str, dict[str, Any], dict[str, str], tuple[str, ...]], Awaitable[ProbeResponse]],
) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = [
        {
            "stage": "url_validation",
            "status": "pass",
            "address_count": len(endpoint.addresses),
            "dns_fingerprint": hashlib.sha256("\n".join(endpoint.addresses).encode()).hexdigest()[:16],
        }
    ]
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": _latest_protocol_version(),
    }
    response = None
    for protocol_version in dict.fromkeys((
        _latest_protocol_version(), "2025-06-18", "2025-03-26", "2024-11-05",
    )):
        headers["MCP-Protocol-Version"] = protocol_version
        initialize = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": protocol_version, "capabilities": {},
                "clientInfo": {"name": "hermes-conformance", "version": "0.1"},
            },
        }
        try:
            response = await request(endpoint.url, initialize, headers, endpoint.addresses)
        except Exception as exc:
            evidence.append({"stage": "initialize", "status": "failed", "error_type": type(exc).__name__})
            return _result("incompatible", "network_error", evidence)
        if not _unsupported_protocol(response):
            break
    assert response is not None
    if response.status != 200:
        return _response_failure(response, evidence, "initialize")
    if _auth_error(response.body):
        evidence.append({"stage": "initialize", "status": "failed", "error_type": "auth_required"})
        return _result("needs_auth", "remote_auth_required", evidence)
    if not isinstance(response.body, dict) or not isinstance(response.body.get("result"), dict):
        evidence.append({"stage": "initialize", "status": "failed", "error_type": "invalid_jsonrpc"})
        return _result("incompatible", "initialize_invalid_response", evidence)
    evidence.append({"stage": "initialize", "status": "pass", "http_status": 200})

    session_id = _header(response.headers, "Mcp-Session-Id")
    session_headers = {**headers, **({"Mcp-Session-Id": session_id} if session_id else {})}
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    try:
        response = await request(endpoint.url, initialized, session_headers, endpoint.addresses)
    except Exception as exc:
        evidence.append({"stage": "initialized", "status": "failed", "error_type": type(exc).__name__})
        return _result("incompatible", "network_error", evidence)
    if response.status not in {200, 202, 204}:
        return _response_failure(response, evidence, "initialized")
    evidence.append({"stage": "initialized", "status": "pass", "http_status": response.status})

    try:
        response = await request(
            endpoint.url,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            session_headers,
            endpoint.addresses,
        )
    except Exception as exc:
        evidence.append({"stage": "tools_list", "status": "failed", "error_type": type(exc).__name__})
        return _result("incompatible", "network_error", evidence)
    if response.status != 200:
        return _response_failure(response, evidence, "tools_list")
    tools = response.body.get("result", {}).get("tools") if isinstance(response.body, dict) else None
    if not isinstance(tools, list):
        evidence.append({"stage": "tools_list", "status": "failed", "error_type": "invalid_jsonrpc"})
        return _result("incompatible", "tools_list_invalid_response", evidence)
    evidence.append({"stage": "tools_list", "status": "pass", "http_status": 200})
    return _result("pass", "tools_list_ok", evidence, tool_count=len(tools))


async def _read_sse_event(response: aiohttp.ClientResponse) -> tuple[str, str]:
    event = "message"
    data: list[str] = []
    for _ in range(100):
        raw = await asyncio.wait_for(response.content.readline(), timeout=10)
        if not raw:
            raise ValueError("SSE stream ended")
        if len(raw) > 1024 * 1024:
            raise ValueError("SSE line exceeds 1 MiB")
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data:
                return event, "\n".join(data)
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    raise ValueError("SSE event exceeded line limit")


def _sse_send_url(endpoint: ValidatedEndpoint, advertised: str) -> str:
    target = urlsplit(urljoin(endpoint.url, advertised.strip()))
    source = urlsplit(endpoint.url)
    source_origin = (source.scheme.casefold(), (source.hostname or "").casefold(), source.port or 443)
    target_origin = (target.scheme.casefold(), (target.hostname or "").casefold(), target.port or 443)
    if target_origin != source_origin or target.username or target.password or target.fragment:
        raise ValueError("SSE message endpoint must stay on the validated origin")
    return target.geturl()


async def _probe_legacy_sse(endpoint: ValidatedEndpoint) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = [
        {
            "stage": "url_validation",
            "status": "pass",
            "address_count": len(endpoint.addresses),
            "dns_fingerprint": hashlib.sha256("\n".join(endpoint.addresses).encode()).hexdigest()[:16],
        }
    ]
    connector = aiohttp.TCPConnector(resolver=_PinnedResolver(endpoint), ttl_dns_cache=0, limit=4)
    timeout = aiohttp.ClientTimeout(total=30, connect=5, sock_read=15)
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False) as session:
            async with session.get(
                endpoint.url,
                headers={"Accept": "text/event-stream"},
                allow_redirects=False,
            ) as stream:
                if stream.status != 200:
                    return _response_failure(
                        ProbeResponse(stream.status, dict(stream.headers), None), evidence, "sse_connect"
                    )
                if not stream.headers.get("Content-Type", "").casefold().startswith("text/event-stream"):
                    evidence.append({"stage": "sse_connect", "status": "failed", "error_type": "invalid_content_type"})
                    return _result("incompatible", "sse_invalid_response", evidence)
                event, data = await _read_sse_event(stream)
                if event != "endpoint":
                    evidence.append({"stage": "sse_connect", "status": "failed", "error_type": "missing_endpoint_event"})
                    return _result("incompatible", "sse_invalid_response", evidence)
                send_url = _sse_send_url(endpoint, data)
                evidence.append({"stage": "sse_connect", "status": "pass", "http_status": 200})

                async def post(payload: dict[str, Any], stage: str) -> dict[str, Any] | None:
                    async with session.post(
                        send_url,
                        json=payload,
                        headers={
                            "Accept": "application/json, text/event-stream",
                            "Content-Type": "application/json",
                            "MCP-Protocol-Version": _latest_protocol_version(),
                        },
                        allow_redirects=False,
                    ) as response:
                        if response.status not in {200, 202, 204}:
                            return _response_failure(
                                ProbeResponse(response.status, dict(response.headers), None), evidence, stage
                            )
                        evidence.append({"stage": stage, "status": "pass", "http_status": response.status})
                    return None

                failure = await post(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": _latest_protocol_version(),
                            "capabilities": {},
                            "clientInfo": {"name": "hermes-conformance", "version": "0.1"},
                        },
                    },
                    "initialize_post",
                )
                if failure:
                    return failure
                _, message = await _read_sse_event(stream)
                initialized = json.loads(message)
                if not isinstance(initialized, dict) or not isinstance(initialized.get("result"), dict):
                    return _result("incompatible", "initialize_invalid_response", evidence)
                evidence.append({"stage": "initialize", "status": "pass"})

                failure = await post(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    "initialized",
                )
                if failure:
                    return failure
                failure = await post(
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    "tools_list_post",
                )
                if failure:
                    return failure
                _, message = await _read_sse_event(stream)
                listed = json.loads(message)
                tools = listed.get("result", {}).get("tools") if isinstance(listed, dict) else None
                if not isinstance(tools, list):
                    return _result("incompatible", "tools_list_invalid_response", evidence)
                evidence.append({"stage": "tools_list", "status": "pass"})
                return _result("pass", "tools_list_ok", evidence, tool_count=len(tools))
    except Exception as exc:
        evidence.append({"stage": "sse_probe", "status": "failed", "error_type": type(exc).__name__})
        return _result("incompatible", "sse_protocol_error", evidence)


async def probe_remote_endpoint(
    url: str,
    *,
    transport: str = "streamable-http",
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    request: Callable[..., Awaitable[ProbeResponse]] | None = None,
    sse_probe: Callable[[ValidatedEndpoint], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    try:
        endpoint = validate_remote_endpoint(url, resolver=resolver)
    except ValueError as exc:
        return _result(
            "rejected",
            "unsafe_endpoint",
            [{"stage": "url_validation", "status": "failed", "error_type": type(exc).__name__}],
        )
    if transport.casefold() == "sse":
        return await (sse_probe or _probe_legacy_sse)(endpoint)
    if request is not None:
        return await _run_probe(endpoint, request)

    connector = aiohttp.TCPConnector(
        resolver=_PinnedResolver(endpoint),
        ttl_dns_cache=0,
        limit=1,
    )
    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False) as session:
        async def send(
            target: str,
            payload: dict[str, Any],
            headers: dict[str, str],
            _addresses: tuple[str, ...],
        ) -> ProbeResponse:
            async with session.post(target, json=payload, headers=headers, allow_redirects=False) as response:
                data = await _read_bounded(response.content, 2 * 1024 * 1024)
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError("remote response exceeds 2 MiB")
                body = _decode_streamable_body(
                    data, response.headers.get("Content-Type", ""), payload.get("id")
                )
                return ProbeResponse(response.status, dict(response.headers), body)

        return await _run_probe(endpoint, send)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--transport", choices=("streamable-http", "sse"), default="streamable-http")
    args = parser.parse_args(argv)
    print(json.dumps(
        asyncio.run(probe_remote_endpoint(args.url, transport=args.transport)),
        ensure_ascii=False,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
