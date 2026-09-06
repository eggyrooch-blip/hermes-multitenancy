import asyncio
import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def test_streamable_http_requires_discoverable_auth_and_uses_bound_principal(tmp_path: Path):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import CallToolResult, TextContent, Tool

    from hermes_multitenancy.connector_client_auth import ClientTokenStore
    from hermes_multitenancy.connector_mcp_http import create_connector_mcp_http_app
    from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal

    issuer = "http://127.0.0.1:8767"
    resource = f"{issuer}/mcp"
    store = ClientTokenStore(tmp_path / "multitenancy.db", issuer=issuer, resource=resource)
    token = store.mint(
        principal=issue_webui_principal(
            profile_name="alice",
            actor_subject="subject-alice",
            credential_subject="subject-alice",
        ),
        client_id="cursor-local",
        scopes=["mcp:tools"],
    )
    seen = []

    async def list_tools(principal):
        seen.append(("list", principal))
        return [Tool(name="identity_probe", description="probe", inputSchema={"type": "object"})]

    async def call_tool(principal, name, arguments):
        seen.append(("call", principal, name, arguments))
        return CallToolResult(content=[TextContent(type="text", text="owner-ok")])

    app = create_connector_mcp_http_app(
        token_store=store,
        issuer=issuer,
        resource=resource,
        list_tools=list_tools,
        call_tool=call_tool,
    )

    async def run():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=issuer) as client:
                denied = await client.post("/mcp", json={})
                assert denied.status_code == 401
                challenge = denied.headers["www-authenticate"]
                assert "oauth-protected-resource/mcp" in challenge
                assert token not in challenge

                client.headers["Authorization"] = f"Bearer {token}"
                async with streamable_http_client(resource, http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert [tool.name for tool in tools.tools] == ["identity_probe"]
                        result = await session.call_tool("identity_probe", {"value": 7})
                        assert result.content[0].text == "owner-ok"

    asyncio.run(run())
    assert [(row[1].profile_name, row[1].subject_id, row[1].client_id) for row in seen] == [
        ("alice", "subject-alice", "cursor-local"),
        ("alice", "subject-alice", "cursor-local"),
    ]
    store.close()


def test_oauth_dcr_pkce_binds_browser_principal_and_rotates_refresh_token(tmp_path: Path):
    import httpx
    from hermes_multitenancy.connector_client_auth import ClientTokenStore
    from hermes_multitenancy.connector_mcp_http import create_connector_mcp_http_app
    from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal

    issuer = "http://127.0.0.1:8767"
    resource = f"{issuer}/mcp"
    store = ClientTokenStore(tmp_path / "multitenancy.db", issuer=issuer, resource=resource)
    principal = issue_webui_principal(
        profile_name="alice",
        actor_subject="subject-alice",
        credential_subject="subject-alice",
    )

    async def authorize_request(_request):
        return principal

    async def list_tools(_principal):
        return []

    async def call_tool(_principal, _name, _arguments):
        raise AssertionError("no tool call expected")

    app = create_connector_mcp_http_app(
        token_store=store,
        issuer=issuer,
        resource=resource,
        list_tools=list_tools,
        call_tool=call_tool,
        authorize_request=authorize_request,
    )

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=issuer,
                follow_redirects=False,
            ) as client:
                registered = await client.post(
                    "/register",
                    json={
                        "redirect_uris": ["http://127.0.0.1:7777/callback"],
                        "token_endpoint_auth_method": "none",
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "client_name": "Cursor local",
                        "scope": "mcp:tools",
                    },
                )
                assert registered.status_code == 201
                client_id = registered.json()["client_id"]
                assert registered.json().get("client_secret") is None

                verifier = "v" * 64
                challenge = base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode()).digest()
                ).decode().rstrip("=")
                authorize = await client.get(
                    "/authorize",
                    params={
                        "client_id": client_id,
                        "redirect_uri": "http://127.0.0.1:7777/callback",
                        "response_type": "code",
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "state": "client-state",
                        "scope": "mcp:tools",
                        "resource": resource,
                    },
                )
                assert authorize.status_code == 302
                approval_path = authorize.headers["location"]
                assert approval_path.startswith(f"{issuer}/oauth/approve?")

                approved = await client.get(approval_path)
                assert approved.status_code == 302
                callback = urlsplit(approved.headers["location"])
                callback_query = parse_qs(callback.query)
                assert callback_query["state"] == ["client-state"]

                issued = await client.post(
                    "/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": client_id,
                        "code": callback_query["code"][0],
                        "code_verifier": verifier,
                        "redirect_uri": "http://127.0.0.1:7777/callback",
                        "resource": resource,
                    },
                )
                assert issued.status_code == 200
                first = issued.json()
                access = await store.verify_token(first["access_token"])
                assert access is not None
                assert access.subject == "subject-alice"
                assert access.claims["profile"] == "alice"

                refreshed = await client.post(
                    "/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": client_id,
                        "refresh_token": first["refresh_token"],
                        "resource": resource,
                    },
                )
                assert refreshed.status_code == 200
                second = refreshed.json()
                assert second["access_token"] != first["access_token"]
                assert second["refresh_token"] != first["refresh_token"]
                assert await store.verify_token(second["access_token"]) is not None

    asyncio.run(run())
    store.close()


def test_oauth_redirects_to_external_trusted_approval_page(tmp_path: Path):
    import httpx

    from hermes_multitenancy.connector_client_auth import ClientTokenStore
    from hermes_multitenancy.connector_mcp_http import create_connector_mcp_http_app

    issuer = "http://127.0.0.1:8767"
    resource = f"{issuer}/mcp"
    store = ClientTokenStore(tmp_path / "multitenancy.db", issuer=issuer, resource=resource)

    async def list_tools(_principal):
        return []

    async def call_tool(_principal, _name, _arguments):
        raise AssertionError("no tool call expected")

    app = create_connector_mcp_http_app(
        token_store=store,
        issuer=issuer,
        resource=resource,
        list_tools=list_tools,
        call_tool=call_tool,
        approval_url="http://127.0.0.1:8649/#/mcp/oauth/approve",
    )

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=issuer,
                follow_redirects=False,
            ) as client:
                registered = await client.post(
                    "/register",
                    json={
                        "redirect_uris": ["http://127.0.0.1:7777/callback"],
                        "token_endpoint_auth_method": "none",
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "client_name": "Local client",
                        "scope": "mcp:tools",
                    },
                )
                authorize = await client.get(
                    "/authorize",
                    params={
                        "client_id": registered.json()["client_id"],
                        "redirect_uri": "http://127.0.0.1:7777/callback",
                        "response_type": "code",
                        "code_challenge": "x" * 43,
                        "code_challenge_method": "S256",
                        "scope": "mcp:tools",
                        "resource": resource,
                    },
                )
                location = urlsplit(authorize.headers["location"])
                assert f"{location.scheme}://{location.netloc}{location.path}" == (
                    "http://127.0.0.1:8649/"
                )
                assert location.fragment.startswith("/mcp/oauth/approve?request_id=hma_")

    asyncio.run(run())
    store.close()


def test_legacy_sse_client_uses_the_same_bound_principal(tmp_path: Path):
    import socket
    import threading
    import time

    import uvicorn
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.types import CallToolResult, TextContent, Tool

    from hermes_multitenancy.connector_client_auth import ClientTokenStore
    from hermes_multitenancy.connector_mcp_http import create_connector_mcp_http_app
    from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    issuer = f"http://127.0.0.1:{port}"
    resource = f"{issuer}/mcp"
    store = ClientTokenStore(tmp_path / "multitenancy.db", issuer=issuer, resource=resource)
    token = store.mint(
        principal=issue_webui_principal(
            profile_name="alice",
            actor_subject="subject-alice",
            credential_subject="subject-alice",
        ),
        client_id="legacy-sse-client",
        scopes=["mcp:tools"],
    )
    seen = []

    async def list_tools(principal):
        seen.append(principal)
        return [Tool(name="identity_probe", inputSchema={"type": "object"})]

    async def call_tool(principal, _name, _arguments):
        seen.append(principal)
        return CallToolResult(content=[TextContent(type="text", text="sse-owner-ok")])

    app = create_connector_mcp_http_app(
        token_store=store,
        issuer=issuer,
        resource=resource,
        list_tools=list_tools,
        call_tool=call_tool,
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.01)
    assert server.started

    async def run():
        async with sse_client(
            f"{issuer}/sse",
            headers={"Authorization": f"Bearer {token}"},
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert [tool.name for tool in (await session.list_tools()).tools] == ["identity_probe"]
                result = await session.call_tool("identity_probe", {})
                assert result.content[0].text == "sse-owner-ok"

    try:
        asyncio.run(run())
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        store.close()
    assert [(item.profile_name, item.subject_id, item.client_id) for item in seen] == [
        ("alice", "subject-alice", "legacy-sse-client"),
        ("alice", "subject-alice", "legacy-sse-client"),
    ]
