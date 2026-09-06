"""Stdio MCP facade over the run-scoped connector broker."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


def _broker() -> tuple[str, str]:
    url = str(
        os.environ.get("HERMES_RUN_BROKER_URL")
        or os.environ.get("HERMES_MULTITENANCY_RUN_BROKER_URL")
        or ""
    ).rstrip("/")
    token = str(os.environ.get("HERMES_RUN_BROKER_KEY") or "").strip()
    if not url or url.startswith("${") or not token or token.startswith("${"):
        raise PermissionError("run-scoped connector broker is unavailable")
    return url, token


async def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url, token = _broker()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as client:
            async with client.request(
                method,
                f"{url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise PermissionError(str(body.get("error") or "connector unavailable"))
                return body
    except PermissionError:
        raise
    except Exception as exc:
        raise RuntimeError("connector broker request failed") from exc


async def _serve_broker() -> None:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    server = Server("hermes-connectors")

    @server.list_tools()
    async def list_tools():
        try:
            body = await _request("GET", "/api/run-broker/connectors/github-mcp/tools")
        except Exception as exc:
            logger.warning("connector tools unavailable (%s)", type(exc).__name__)
            return []
        return [
            Tool(
                name=str(row["name"]),
                description=str(row.get("description") or ""),
                inputSchema=row.get("inputSchema") or {"type": "object"},
            )
            for row in body.get("tools") or []
            if isinstance(row, dict) and row.get("name")
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]):
        body = await _request(
            "POST",
            "/api/run-broker/connectors/github-mcp/call",
            {"name": name, "arguments": arguments or {}},
        )
        return [TextContent(type="text", text=json.dumps(body.get("result"), ensure_ascii=False))]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def _http_target() -> tuple[str, str] | None:
    url = str(os.environ.get("HERMES_MCP_URL") or "").strip()
    token = str(os.environ.get("HERMES_MCP_CLIENT_TOKEN") or "").strip()
    if not url and not token:
        return None
    if not url or not token or url.startswith("${") or token.startswith("${"):
        raise PermissionError("short-lived MCP client identity is unavailable")
    from .connector_client_adapters import _urls

    return _urls(url)[0], token


async def _serve_http(url: str, token: str) -> None:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as client:
        async with streamable_http_client(url, http_client=client) as (up_read, up_write, _):
            async with ClientSession(up_read, up_write) as upstream:
                initialized = await upstream.initialize()
                server = Server("hermes-connectors")

                @server.list_tools()
                async def list_tools():
                    return await upstream.list_tools()

                @server.call_tool()
                async def call_tool(name: str, arguments: dict[str, Any]):
                    return await upstream.call_tool(name, arguments)

                if initialized.capabilities.prompts is not None:
                    @server.list_prompts()
                    async def list_prompts():
                        return await upstream.list_prompts()

                    @server.get_prompt()
                    async def get_prompt(name: str, arguments: dict[str, str] | None):
                        return await upstream.get_prompt(name, arguments)

                if initialized.capabilities.resources is not None:
                    @server.list_resources()
                    async def list_resources():
                        return await upstream.list_resources()

                    @server.read_resource()
                    async def read_resource(uri):
                        return await upstream.read_resource(uri)

                async with stdio_server() as (read, write):
                    await server.run(read, write, server.create_initialization_options())


async def run() -> None:
    target = _http_target()
    if target is not None:
        await _serve_http(*target)
        return
    await _serve_broker()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
