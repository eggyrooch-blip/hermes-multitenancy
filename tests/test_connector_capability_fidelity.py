import asyncio
from pathlib import Path


def test_rich_mcp_capabilities_round_trip_without_policy_bypass(tmp_path: Path):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.lowlevel.helper_types import ReadResourceContents
    from mcp.types import (
        CallToolResult,
        GetPromptResult,
        ImageContent,
        ListPromptsResult,
        ListResourcesResult,
        ListToolsResult,
        Prompt,
        PromptMessage,
        Resource,
        ResourceLink,
        TextContent,
        Tool,
    )

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
        client_id="rich-client",
        scopes=["mcp:tools"],
    )
    seen = []

    async def list_tools(principal):
        seen.append(("list_tools", principal.profile_name))
        return ListToolsResult(
            tools=[Tool.model_validate({
                "name": "rich_read",
                "description": "rich read",
                "inputSchema": {"type": "object"},
                "outputSchema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
                "annotations": {"readOnlyHint": True},
                "_meta": {"ui": {"resourceUri": "ui://github/result"}},
            })],
            _meta={"catalog": "owner-scoped"},
        )

    async def call_tool(principal, name, _arguments):
        seen.append(("call_tool", principal.profile_name, name))
        if name != "rich_read":
            raise PermissionError("capability is not approved")
        return CallToolResult(
            content=[
                TextContent(type="text", text="ok"),
                ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png"),
                ResourceLink(type="resource_link", name="result", uri="ui://github/result", mimeType="text/html"),
            ],
            structuredContent={"ok": True},
            _meta={"ui": {"resourceUri": "ui://github/result"}},
        )

    async def list_prompts(principal):
        seen.append(("list_prompts", principal.profile_name))
        return ListPromptsResult(
            prompts=[Prompt(name="review_repo", description="review")],
            _meta={"source": "bundle"},
        )

    async def get_prompt(principal, name, arguments):
        seen.append(("get_prompt", principal.profile_name, name, arguments))
        return GetPromptResult(
            description="review",
            messages=[PromptMessage(role="user", content=TextContent(type="text", text="review it"))],
            _meta={"source": "bundle"},
        )

    async def list_resources(principal):
        seen.append(("list_resources", principal.profile_name))
        return ListResourcesResult(
            resources=[Resource(name="result", uri="ui://github/result", mimeType="text/html")],
            _meta={"source": "bundle"},
        )

    async def read_resource(principal, uri):
        seen.append(("read_resource", principal.profile_name, str(uri)))
        return [ReadResourceContents("<main>safe</main>", "text/html", {"ui": {"csp": "self"}})]

    app = create_connector_mcp_http_app(
        token_store=store,
        issuer=issuer,
        resource=resource,
        list_tools=list_tools,
        call_tool=call_tool,
        list_prompts=list_prompts,
        get_prompt=get_prompt,
        list_resources=list_resources,
        read_resource=read_resource,
    )

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=issuer,
                headers={"Authorization": f"Bearer {token}"},
            ) as client:
                async with streamable_http_client(resource, http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert tools.meta == {"catalog": "owner-scoped"}
                        assert tools.tools[0].outputSchema["required"] == ["ok"]
                        assert tools.tools[0].annotations.readOnlyHint is True
                        assert tools.tools[0].meta["ui"]["resourceUri"] == "ui://github/result"
                        result = await session.call_tool("rich_read", {})
                        assert result.structuredContent == {"ok": True}
                        assert result.meta["ui"]["resourceUri"] == "ui://github/result"
                        assert [item.type for item in result.content] == ["text", "image", "resource_link"]
                        denied = await session.call_tool("write_repo", {})
                        assert denied.isError is True

                        prompts = await session.list_prompts()
                        assert prompts.meta == {"source": "bundle"}
                        prompt = await session.get_prompt("review_repo", {"repo": "demo"})
                        assert prompt.meta == {"source": "bundle"}
                        assert prompt.messages[0].content.text == "review it"

                        resources = await session.list_resources()
                        assert resources.meta == {"source": "bundle"}
                        contents = await session.read_resource("ui://github/result")
                        assert contents.contents[0].text == "<main>safe</main>"
                        assert contents.contents[0].meta == {"ui": {"csp": "self"}}

    asyncio.run(run())
    assert not any(row[:3] == ("call_tool", "alice", "write_repo") for row in seen)
    store.close()
