"""Standard Streamable HTTP surface for owner-scoped Hermes connectors."""
from __future__ import annotations

import argparse
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.sse import SseServerTransport
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware, get_access_token
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import (
    CallToolResult,
    GetPromptResult,
    ListPromptsResult,
    ListResourcesResult,
    ListToolsResult,
    Prompt,
    Resource,
    Tool,
)
from pydantic import AnyHttpUrl, AnyUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from .connector_client_auth import ClientTokenStore, HermesOAuthProvider
from . import github_mcp_connector
from .connector_custom_runtime import CustomConnectorRuntime
from .trusted_runtime_principal import TrustedRuntimePrincipal


@dataclass(frozen=True, slots=True)
class ConnectorClientPrincipal:
    profile_name: str
    subject_id: str
    client_id: str
    scopes: tuple[str, ...]


class _StreamableApp:
    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self.manager = manager

    async def __call__(self, scope, receive, send) -> None:
        await self.manager.handle_request(scope, receive, send)


class _SseApp:
    def __init__(self, transport: SseServerTransport, server: Server) -> None:
        self.transport = transport
        self.server = server

    async def __call__(self, scope, receive, send) -> None:
        async with self.transport.connect_sse(scope, receive, send) as (read, write):
            await self.server.run(read, write, self.server.create_initialization_options())


def create_connector_mcp_http_app(
    *,
    token_store: ClientTokenStore,
    issuer: str,
    resource: str,
    list_tools: Callable[[ConnectorClientPrincipal], Awaitable[list[Tool] | ListToolsResult]],
    call_tool: Callable[[ConnectorClientPrincipal, str, dict[str, Any]], Awaitable[CallToolResult]],
    authorize_request: Callable[[Request], Awaitable[TrustedRuntimePrincipal | None]] | None = None,
    approval_url: str | None = None,
    list_prompts: Callable[[ConnectorClientPrincipal], Awaitable[list[Prompt] | ListPromptsResult]] | None = None,
    get_prompt: Callable[[ConnectorClientPrincipal, str, dict[str, str] | None], Awaitable[GetPromptResult]] | None = None,
    list_resources: Callable[[ConnectorClientPrincipal], Awaitable[list[Resource] | ListResourcesResult]] | None = None,
    read_resource: Callable[[ConnectorClientPrincipal, AnyUrl], Awaitable[list[ReadResourceContents]]] | None = None,
    enable_sse: bool = True,
) -> Starlette:
    """Create a stateless MCP resource server; identity comes only from bearer auth."""
    issuer_url = AnyHttpUrl(issuer)
    resource_url = AnyHttpUrl(resource)
    server = Server("hermes-connectors")
    if (list_prompts is None) != (get_prompt is None):
        raise ValueError("prompt list and get callbacks must be configured together")
    if (list_resources is None) != (read_resource is None):
        raise ValueError("resource list and read callbacks must be configured together")
    # ponytail: process-local catalogs; prune by token/client expiry if long-lived
    # untrusted client churn becomes measurable.
    allowed_tools: dict[tuple[str, str, str], set[str]] = {}
    allowed_prompts: dict[tuple[str, str, str], set[str]] = {}
    allowed_resources: dict[tuple[str, str, str], set[str]] = {}

    def principal_key(principal: ConnectorClientPrincipal) -> tuple[str, str, str]:
        return principal.profile_name, principal.subject_id, principal.client_id

    async def owner_tools(principal: ConnectorClientPrincipal):
        result = await list_tools(principal)
        rows = result.tools if isinstance(result, ListToolsResult) else result
        allowed_tools[principal_key(principal)] = {tool.name for tool in rows}
        return result

    @server.list_tools()
    async def handle_list_tools():
        return await owner_tools(_principal(issuer=issuer, resource=resource))

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any]):
        principal = _principal(issuer=issuer, resource=resource)
        key = principal_key(principal)
        if name not in allowed_tools.get(key, set()):
            await owner_tools(principal)
        if name not in allowed_tools.get(key, set()):
            raise PermissionError("tool is not approved for this owner")
        return await call_tool(principal, name, arguments)

    if list_prompts is not None and get_prompt is not None:
        async def owner_prompts(principal: ConnectorClientPrincipal):
            result = await list_prompts(principal)
            rows = result.prompts if isinstance(result, ListPromptsResult) else result
            allowed_prompts[principal_key(principal)] = {prompt.name for prompt in rows}
            return result

        @server.list_prompts()
        async def handle_list_prompts():
            return await owner_prompts(_principal(issuer=issuer, resource=resource))

        @server.get_prompt()
        async def handle_get_prompt(name: str, arguments: dict[str, str] | None):
            principal = _principal(issuer=issuer, resource=resource)
            key = principal_key(principal)
            if name not in allowed_prompts.get(key, set()):
                await owner_prompts(principal)
            if name not in allowed_prompts.get(key, set()):
                raise PermissionError("prompt is not approved for this owner")
            return await get_prompt(principal, name, arguments)

    if list_resources is not None and read_resource is not None:
        async def owner_resources(principal: ConnectorClientPrincipal):
            result = await list_resources(principal)
            rows = result.resources if isinstance(result, ListResourcesResult) else result
            allowed_resources[principal_key(principal)] = {str(item.uri) for item in rows}
            return result

        @server.list_resources()
        async def handle_list_resources():
            return await owner_resources(_principal(issuer=issuer, resource=resource))

        @server.read_resource()
        async def handle_read_resource(uri: AnyUrl):
            principal = _principal(issuer=issuer, resource=resource)
            key = principal_key(principal)
            if str(uri) not in allowed_resources.get(key, set()):
                await owner_resources(principal)
            if str(uri) not in allowed_resources.get(key, set()):
                raise PermissionError("resource is not approved for this owner")
            return await read_resource(principal, uri)

    manager = StreamableHTTPSessionManager(
        server,
        json_response=True,
        stateless=True,
    )
    resource_metadata_url = build_resource_metadata_url(resource_url)
    routes = []
    oauth_provider = None
    if authorize_request is not None or approval_url is not None:
        oauth_provider = HermesOAuthProvider(token_store, approval_url=approval_url)
        routes.extend(
            create_auth_routes(
                provider=oauth_provider,
                issuer_url=issuer_url,
                client_registration_options=ClientRegistrationOptions(
                    enabled=True,
                    client_secret_expiry_seconds=3600,
                    valid_scopes=["mcp:tools"],
                    default_scopes=["mcp:tools"],
                ),
                revocation_options=RevocationOptions(enabled=True),
            )
        )

        if authorize_request is not None:
            async def approve(request: Request):
                request_id = str(request.query_params.get("request_id") or "").strip()
                principal = await authorize_request(request)
                if not request_id or principal is None or not principal.is_authentic():
                    return JSONResponse({"error": "authorization required"}, status_code=401)
                try:
                    target = oauth_provider.approve(request_id, principal)
                except PermissionError:
                    return JSONResponse({"error": "authorization unavailable"}, status_code=403)
                return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

            routes.append(Route("/oauth/approve", endpoint=approve, methods=["GET"]))

    routes.extend(create_protected_resource_routes(
        resource_url=resource_url,
        authorization_servers=[issuer_url],
        scopes_supported=["mcp:tools"],
        resource_name="Hermes Connectors",
    ))
    routes.append(
        Route(
            resource_url.path,
            endpoint=RequireAuthMiddleware(
                _StreamableApp(manager),
                ["mcp:tools"],
                resource_metadata_url,
            ),
        )
    )
    if enable_sse:
        sse = SseServerTransport("/messages")
        routes.extend([
            Route(
                "/sse",
                endpoint=RequireAuthMiddleware(
                    _SseApp(sse, server),
                    ["mcp:tools"],
                    resource_metadata_url,
                ),
            ),
            Route(
                "/messages",
                endpoint=RequireAuthMiddleware(
                    sse.handle_post_message,
                    ["mcp:tools"],
                    resource_metadata_url,
                ),
                methods=["POST"],
            ),
        ])
    @asynccontextmanager
    async def lifespan(_app):
        try:
            async with manager.run():
                yield
        finally:
            if oauth_provider is not None:
                oauth_provider.close()

    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(token_store)),
            Middleware(AuthContextMiddleware),
        ],
        lifespan=lifespan,
    )
    app.state.oauth_provider = oauth_provider
    return app


def create_github_connector_mcp_http_app(
    *, shared_home: Path | str, public_origin: str, approval_url: str
) -> Starlette:
    """Wire the existing owner-scoped GitHub connector to the HTTP transport."""
    root = Path(shared_home).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Hermes shared home is unavailable")
    issuer = str(public_origin or "").strip().rstrip("/")
    resource = f"{issuer}/mcp"
    token_store = ClientTokenStore(root / "multitenancy.db", issuer=issuer, resource=resource)
    custom = CustomConnectorRuntime(root / "multitenancy.db")

    async def list_tools(principal: ConnectorClientPrincipal) -> list[Tool]:
        try:
            rows = await github_mcp_connector.list_tools(
                root, principal.profile_name, principal.subject_id
            )
        except github_mcp_connector.ConnectorUnavailable:
            rows = []
        builtins = [
            Tool(
                name=str(row["name"]),
                description=str(row.get("description") or ""),
                inputSchema=row.get("inputSchema") or {"type": "object"},
            )
            for row in rows
        ]
        remote = await custom.list_tools(principal.profile_name, principal.subject_id)
        return builtins + [Tool.model_validate(row) for row in remote]

    async def call_tool(
        principal: ConnectorClientPrincipal, name: str, arguments: dict[str, Any]
    ) -> CallToolResult:
        if str(name).startswith("custom-"):
            result = await custom.call_tool(
                principal.profile_name, principal.subject_id, name, arguments
            )
        else:
            result = await github_mcp_connector.call_tool(
                root, principal.profile_name, principal.subject_id, name, arguments
            )
        if isinstance(result, dict):
            return CallToolResult.model_validate(result)
        from mcp.types import TextContent

        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        )

    app = create_connector_mcp_http_app(
        token_store=token_store,
        issuer=issuer,
        resource=resource,
        list_tools=list_tools,
        call_tool=call_tool,
        approval_url=approval_url,
    )
    app.state.token_store = token_store
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the owner-scoped Hermes MCP gateway")
    parser.add_argument("--host", default=os.environ.get("HERMES_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HERMES_MCP_PORT", "8767")))
    parser.add_argument(
        "--shared-home",
        default=os.environ.get("HERMES_SHARED_HOME") or os.environ.get("HERMES_HOME") or "~/.hermes",
    )
    parser.add_argument("--public-origin", default=os.environ.get("HERMES_MCP_PUBLIC_ORIGIN"))
    parser.add_argument("--approval-url", default=os.environ.get("HERMES_MCP_APPROVAL_URL"))
    args = parser.parse_args(argv)
    public_origin = args.public_origin or f"http://{args.host}:{args.port}"
    if not args.approval_url:
        parser.error("--approval-url or HERMES_MCP_APPROVAL_URL is required")

    import uvicorn

    app = create_github_connector_mcp_http_app(
        shared_home=args.shared_home,
        public_origin=public_origin,
        approval_url=args.approval_url,
    )
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        app.state.token_store.close()
    return 0


def _principal(*, issuer: str, resource: str) -> ConnectorClientPrincipal:
    access = get_access_token()
    claims = access.claims if access is not None and isinstance(access.claims, dict) else {}
    profile = str(claims.get("profile") or "").strip()
    subject = str(access.subject or "").strip() if access is not None else ""
    if (
        access is None
        or str(claims.get("iss") or "").rstrip("/") != str(issuer).rstrip("/")
        or str(access.resource or "").rstrip("/") != str(resource).rstrip("/")
        or not profile
        or not subject
    ):
        raise PermissionError("MCP client principal is unavailable")
    return ConnectorClientPrincipal(
        profile_name=profile,
        subject_id=subject,
        client_id=access.client_id,
        scopes=tuple(access.scopes),
    )


if __name__ == "__main__":
    raise SystemExit(main())
