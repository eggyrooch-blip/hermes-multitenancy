"""Read-only data plane for owner-scoped custom remote MCP installations."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
import httpx

from .connector_custom_catalog import CustomConnectorStore
from .connector_catalog_oauth import refresh_catalog_oauth
from .connector_remote_probe import (
    _PinnedResolver,
    _decode_streamable_body,
    _latest_protocol_version,
    _read_sse_event,
    _sse_send_url,
    _unsupported_protocol,
    ProbeResponse,
    validate_remote_endpoint,
)
from .connector_stdio_runtime import (
    build_linux_stdio_command,
    catalog_stdio_spec,
    materialize_runtime_values,
    stdio_runtime_fingerprint,
    verify_python_runtime_tree,
    verify_npm_runtime_tree,
    verify_node_git_runtime_tree,
    write_runtime_environment,
    write_runtime_resolver,
)


_TOOL = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


async def _response_json(
    response: aiohttp.ClientResponse, request_id: Any = None
) -> dict[str, Any] | None:
    chunks, size = [], 0
    while chunk := await response.content.read(64 * 1024):
        size += len(chunk)
        if size > 2 * 1024 * 1024:
            raise RuntimeError("remote MCP response is too large")
        chunks.append(chunk)
    data = b"".join(chunks)
    body = _decode_streamable_body(data, response.headers.get("Content-Type", ""), request_id)
    return body if isinstance(body, dict) else None


def _payload(method: str, arguments: dict[str, Any]) -> dict[str, Any]:
    params = {} if method == "tools/list" else arguments
    return {"jsonrpc": "2.0", "id": 2, "method": method, "params": params}


async def _streamable_exchange(runtime: dict[str, Any], method: str, arguments: dict[str, Any]) -> dict[str, Any]:
    endpoint = validate_remote_endpoint(runtime["endpoint"])
    connector = aiohttp.TCPConnector(resolver=_PinnedResolver(endpoint), ttl_dns_cache=0, limit=1)
    headers = {
        **runtime["headers"],
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": _latest_protocol_version(),
    }
    timeout = aiohttp.ClientTimeout(total=30, connect=5, sock_read=20)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False) as session:
        async def post(payload: dict[str, Any], request_headers: dict[str, str]):
            async with session.post(endpoint.url, json=payload, headers=request_headers, allow_redirects=False) as response:
                body = await _response_json(response, payload.get("id"))
                return body, dict(response.headers), response.status

        initialized = response_headers = None
        response_status = 0
        for protocol_version in dict.fromkeys((
            _latest_protocol_version(), "2025-06-18", "2025-03-26", "2024-11-05",
        )):
            headers["MCP-Protocol-Version"] = protocol_version
            initialized, response_headers, response_status = await post({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": protocol_version, "capabilities": {},
                    "clientInfo": {"name": "hermes-connectors", "version": "0.1"},
                },
            }, headers)
            if not _unsupported_protocol(ProbeResponse(response_status, response_headers, initialized)):
                break
        if response_status not in {200, 202, 204}:
            raise PermissionError(f"remote MCP unavailable ({response_status})")
        if not isinstance(initialized, dict) or not isinstance(initialized.get("result"), dict):
            raise RuntimeError("remote MCP initialize failed")
        session_id = next(
            (str(value) for key, value in response_headers.items() if key.casefold() == "mcp-session-id"), ""
        )
        session_headers = {**headers, **({"Mcp-Session-Id": session_id} if session_id else {})}
        _body, _headers, response_status = await post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_headers
        )
        if response_status not in {200, 202, 204}:
            raise PermissionError(f"remote MCP unavailable ({response_status})")
        result, _headers, response_status = await post(_payload(method, arguments), session_headers)
        if response_status not in {200, 202, 204}:
            raise PermissionError(f"remote MCP unavailable ({response_status})")
        if not isinstance(result, dict) or not isinstance(result.get("result"), dict):
            raise RuntimeError("remote MCP returned an invalid response")
        return result["result"]


async def _sse_exchange(runtime: dict[str, Any], method: str, arguments: dict[str, Any]) -> dict[str, Any]:
    endpoint = validate_remote_endpoint(runtime["endpoint"])
    connector = aiohttp.TCPConnector(resolver=_PinnedResolver(endpoint), ttl_dns_cache=0, limit=2)
    timeout = aiohttp.ClientTimeout(total=30, connect=5, sock_read=20)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False) as session:
        async with session.get(
            endpoint.url,
            headers={**runtime["headers"], "Accept": "text/event-stream"},
            allow_redirects=False,
        ) as stream:
            if stream.status != 200:
                raise PermissionError(f"remote MCP unavailable ({stream.status})")
            event, advertised = await _read_sse_event(stream)
            if event != "endpoint":
                raise RuntimeError("remote MCP SSE endpoint event is missing")
            send_url = _sse_send_url(endpoint, advertised)

            async def post(payload: dict[str, Any]):
                async with session.post(
                    send_url,
                    json=payload,
                    headers={
                        **runtime["headers"],
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                        "MCP-Protocol-Version": _latest_protocol_version(),
                    },
                    allow_redirects=False,
                ) as response:
                    if response.status not in {200, 202, 204}:
                        raise PermissionError(f"remote MCP unavailable ({response.status})")

            await post({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": _latest_protocol_version(), "capabilities": {},
                    "clientInfo": {"name": "hermes-connectors", "version": "0.1"},
                },
            })
            _, message = await _read_sse_event(stream)
            initialized = json.loads(message)
            if not isinstance(initialized, dict) or not isinstance(initialized.get("result"), dict):
                raise RuntimeError("remote MCP initialize failed")
            await post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            await post(_payload(method, arguments))
            _, message = await _read_sse_event(stream)
            result = json.loads(message)
            if not isinstance(result, dict) or not isinstance(result.get("result"), dict):
                raise RuntimeError("remote MCP returned an invalid response")
            return result["result"]


async def _exchange(runtime: dict[str, Any], method: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return await (_sse_exchange if runtime["transport"] == "sse" else _streamable_exchange)(runtime, method, arguments)


async def _stdio_exchange(runtime: dict[str, Any], method: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    manifest = runtime["runtime_manifest"]
    spec = catalog_stdio_spec(manifest)
    resolution = spec["resolution"]
    runtime_root = runtime["runtime_base"] / stdio_runtime_fingerprint(spec)
    installed = (
        verify_npm_runtime_tree(runtime_root, resolution)
        if spec["runtime_kind"] == "npm"
        else verify_node_git_runtime_tree(runtime_root, resolution)
        if spec["runtime_kind"] == "node_git"
        else verify_python_runtime_tree(runtime_root, resolution)
    )
    owner_key = hashlib.sha256(runtime["connector_id"].encode()).hexdigest()[:24]
    sandbox_home = runtime["runtime_base"].parent / "connector-sandboxes" / owner_key
    env_dir = Path("/run/hermes-connector-env")
    env_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    env_dir.chmod(0o700)
    fd, raw_env_path = tempfile.mkstemp(prefix="stdio-", suffix=".env", dir=env_dir)
    os.close(fd)
    env_path = Path(raw_env_path)
    values = materialize_runtime_values(
        sandbox_home,
        {**spec["static_env"], **runtime["environment"]},
        spec.get("files") or {},
    )
    write_runtime_environment(env_path, values, allowed_fields=list(values))
    resolver_path = write_runtime_resolver(
        runtime["runtime_base"].parent / "connector-runtime-resolv.conf"
    )
    command = build_linux_stdio_command(
        runtime_root=runtime_root,
        executable=installed["executable"],
        runtime_args=spec["runtime_args"],
        sandbox_home=sandbox_home,
        env_file=env_path,
        resolver_file=resolver_path,
        python_executable=installed.get("python"),
        python_base=(
            Path(installed["python_base"])
            if installed.get("python_base")
            else Path(sys.executable).resolve().parents[1] if installed.get("python") else None
        ),
        python_version=installed.get("python_version"),
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "hermes_multitenancy.connector_stdio_proxy", "--", *command],
        env={},
    )
    try:
        async with asyncio.timeout(45):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if method == "tools/list":
                        result = await session.list_tools()
                    elif method == "tools/call":
                        result = await session.call_tool(
                            str(arguments.get("name") or ""), arguments.get("arguments") or {}
                        )
                    else:
                        raise ValueError("unsupported stdio MCP method")
                    return result.model_dump(mode="json", by_alias=True, exclude_none=True)
    finally:
        env_path.unlink(missing_ok=True)


class CustomConnectorRuntime:
    def __init__(
        self,
        db_path: Path | str,
        *,
        encryption_key: str | bytes | None = None,
        exchange: Callable[[dict[str, Any], str, dict[str, Any]], Awaitable[dict[str, Any]]] = _exchange,
        stdio_exchange: Callable[[dict[str, Any], str, dict[str, Any]], Awaitable[dict[str, Any]]] = _stdio_exchange,
        oauth_transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    ) -> None:
        self.db_path = Path(db_path)
        self.encryption_key = encryption_key
        self.exchange = exchange
        self.stdio_exchange = stdio_exchange
        self.oauth_transport = oauth_transport
        self.resolver = resolver

    def _store(self) -> CustomConnectorStore:
        return CustomConnectorStore(
            self.db_path, encryption_key=self.encryption_key, resolver=self.resolver
        )

    async def _runtime(self, profile_name: str, subject_id: str, connector_id: str) -> dict[str, Any]:
        store = self._store()
        try:
            runtime = store.get_runtime(profile_name, subject_id, connector_id)
        finally:
            store.close()
        if runtime["credential_secret_kind"] == "oauth" and (
            not runtime["oauth_expires_at"]
            or int(runtime["oauth_expires_at"]) <= int(time.time() * 1000) + 60_000
        ):
            await refresh_catalog_oauth(
                self.db_path,
                profile_name,
                subject_id,
                runtime["credential_provider"],
                runtime["endpoint"],
                encryption_key=self.encryption_key,
                resolver=self.resolver,
                transport=self.oauth_transport,
            )
            store = self._store()
            try:
                runtime = store.get_runtime(profile_name, subject_id, connector_id)
            finally:
                store.close()
        return runtime

    async def _exchange(self, runtime: dict[str, Any], method: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if runtime["transport"] == "stdio":
            runtime = {**runtime, "runtime_base": self.db_path.parent / "connector-runtimes"}
            return await self.stdio_exchange(runtime, method, arguments)
        return await self.exchange(runtime, method, arguments)

    async def list_connector_tools(
        self, profile_name: str, subject_id: str, connector_id: str
    ) -> list[dict[str, Any]]:
        runtime = await self._runtime(profile_name, subject_id, connector_id)
        result = await self._exchange(runtime, "tools/list", {})
        tools = []
        for tool in result.get("tools") or []:
            name = str(tool.get("name") or "") if isinstance(tool, dict) else ""
            annotations = tool.get("annotations") or {} if isinstance(tool, dict) else {}
            if _TOOL.fullmatch(name) and annotations.get("readOnlyHint") is True:
                tools.append({
                    **tool,
                    "name": f"{connector_id}__{name}",
                    "description": str(tool.get("description") or ""),
                    "inputSchema": tool.get("inputSchema") or {"type": "object"},
                })
        return tools

    async def list_tools(self, profile_name: str, subject_id: str) -> list[dict[str, Any]]:
        store = self._store()
        try:
            installations = store.list_installations(profile_name, subject_id)
        finally:
            store.close()
        tools: list[dict[str, Any]] = []
        for installation in installations:
            try:
                runtime = await self._runtime(profile_name, subject_id, installation["connector_id"])
                result = await self._exchange(runtime, "tools/list", {})
            except Exception:
                continue
            for tool in result.get("tools") or []:
                name = str(tool.get("name") or "") if isinstance(tool, dict) else ""
                annotations = tool.get("annotations") or {} if isinstance(tool, dict) else {}
                if not _TOOL.fullmatch(name) or annotations.get("readOnlyHint") is not True:
                    continue
                tools.append({
                    **tool,
                    "name": f"{runtime['connector_id']}__{name}",
                    "description": str(tool.get("description") or ""),
                    "inputSchema": tool.get("inputSchema") or {"type": "object"},
                })
        return tools

    async def call_tool(
        self, profile_name: str, subject_id: str, exposed_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        connector_id, separator, tool_name = str(exposed_name).partition("__")
        if not separator or not _TOOL.fullmatch(tool_name):
            raise PermissionError("tool is not approved for this owner")
        runtime = await self._runtime(profile_name, subject_id, connector_id)
        listed = await self._exchange(runtime, "tools/list", {})
        approved = {
            str(tool.get("name"))
            for tool in listed.get("tools") or []
            if isinstance(tool, dict) and (tool.get("annotations") or {}).get("readOnlyHint") is True
        }
        if tool_name not in approved:
            raise PermissionError("tool is not approved for this owner")
        return await self._exchange(runtime, "tools/call", {"name": tool_name, "arguments": arguments or {}})
