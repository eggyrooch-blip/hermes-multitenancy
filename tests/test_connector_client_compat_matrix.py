import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

import pytest


def test_client_projections_cover_local_shapes_without_embedding_credentials(tmp_path: Path):
    from hermes_multitenancy.connector_client_adapters import build_projection

    gateway = "http://127.0.0.1:8767/mcp"
    projections = {
        client: build_projection(client, gateway_url=gateway, launcher="/opt/hermes/python")
        for client in (
            "codex",
            "claude",
            "cursor",
            "gemini",
            "workbuddy",
            "qoderwork",
            "doubaowork",
            "trae",
            "1mcp",
        )
    }

    assert projections["codex"]["config_toml"] == (
        '[mcp_servers.hermes-connectors]\n'
        'url = "http://127.0.0.1:8767/mcp"\n'
        'auth = "oauth"\n'
        'scopes = ["mcp:tools"]\n'
    )
    assert 'bearer_token_env_var = "HERMES_MCP_CLIENT_TOKEN"' in projections["codex"]["bearer_env_config_toml"]
    assert projections["claude"]["command"] == [
        "claude", "mcp", "add", "--transport", "http",
        "hermes-connectors", gateway,
    ]
    assert projections["cursor"]["mcp.json"]["mcpServers"]["hermes-connectors"] == {
        "url": gateway,
    }
    assert projections["gemini"]["settings.json"]["mcpServers"]["hermes-connectors"] == {
        "httpUrl": gateway,
    }
    assert projections["workbuddy"]["mcp.json"]["mcpServers"]["hermes-connectors"]["type"] == "streamableHttp"
    assert projections["trae"][".mcp.json"]["mcpServers"]["hermes-connectors"]["type"] == "http"
    assert projections["trae"]["connector.json"]["connectors"]["github-mcp"] == {
        "type": "oauth",
        "auth_policy": "ON_INSTALL",
    }
    for client in ("qoderwork", "1mcp"):
        server = projections[client]["mcp.json"]["mcpServers"]["hermes-connectors"]
        assert server == {
            "command": "/opt/hermes/python",
            "args": ["-m", "hermes_multitenancy.connector_mcp_stdio"],
        }
    assert projections["doubaowork"]["install"] == "product-managed"
    assert projections["doubaowork"]["protocol_probe"] == {
        "streamable_http": gateway,
        "sse": "http://127.0.0.1:8767/sse",
    }

    rendered = json.dumps(projections, ensure_ascii=False)
    for forbidden in ("Authorization", "Bearer ", "client_secret", "HERMES_RUN_BROKER_KEY"):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/mcp",
        "http://127.0.0.1:8767/mcp?token=x",
        "http://user:pass@127.0.0.1:8767/mcp",
        "file:///tmp/mcp",
    ],
)
def test_client_projection_rejects_unsafe_gateway_urls(url: str):
    from hermes_multitenancy.connector_client_adapters import build_projection

    with pytest.raises(ValueError, match="gateway_url"):
        build_projection("cursor", gateway_url=url)


def test_client_projection_rejects_unknown_clients():
    from hermes_multitenancy.connector_client_adapters import build_projection

    with pytest.raises(ValueError, match="unsupported client"):
        build_projection("mystery", gateway_url="https://gateway.example/mcp")


def test_legacy_sse_projection_preserves_gateway_path_prefix():
    from hermes_multitenancy.connector_client_adapters import build_projection

    projection = build_projection(
        "doubaowork",
        gateway_url="https://gateway.example/hermes/mcp",
    )
    assert projection["protocol_probe"]["sse"] == "https://gateway.example/hermes/sse"


def test_stdio_launcher_forwards_through_the_bound_http_policy(tmp_path: Path):
    import asyncio

    import uvicorn
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
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
        client_id="stdio-launcher",
        scopes=["mcp:tools"],
    )
    seen = []

    async def list_tools(principal):
        seen.append(principal)
        return [Tool(name="identity_probe", inputSchema={"type": "object"})]

    async def call_tool(principal, _name, _arguments):
        seen.append(principal)
        return CallToolResult(content=[TextContent(type="text", text="stdio-owner-ok")])

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
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "hermes_multitenancy.connector_mcp_stdio"],
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).parents[1]),
                "HERMES_MCP_URL": resource,
                "HERMES_MCP_CLIENT_TOKEN": token,
            },
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert [tool.name for tool in (await session.list_tools()).tools] == ["identity_probe"]
                result = await session.call_tool("identity_probe", {})
                assert result.content[0].text == "stdio-owner-ok"

    try:
        asyncio.run(run())
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        store.close()
    assert [(item.profile_name, item.subject_id, item.client_id) for item in seen] == [
        ("alice", "subject-alice", "stdio-launcher"),
        ("alice", "subject-alice", "stdio-launcher"),
    ]
