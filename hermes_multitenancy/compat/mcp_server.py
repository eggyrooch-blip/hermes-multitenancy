"""Register the connector handlers on MCP SDK 1.x and 2.x."""
from __future__ import annotations

import base64

import jsonschema

from mcp import types
from mcp.server import Server

SDK_V2 = not hasattr(Server, "list_tools")
if SDK_V2:
    import httpx2 as httpx
else:
    import httpx


def handler(server, name):
    legacy = getattr(server, name, None)
    if legacy is not None:
        return legacy()

    method, params_type, result_type, field = {
        "list_tools": ("tools/list", types.PaginatedRequestParams, types.ListToolsResult, "tools"),
        "call_tool": ("tools/call", types.CallToolRequestParams, types.CallToolResult, "content"),
        "list_prompts": ("prompts/list", types.PaginatedRequestParams, types.ListPromptsResult, "prompts"),
        "get_prompt": ("prompts/get", types.GetPromptRequestParams, types.GetPromptResult, None),
        "list_resources": ("resources/list", types.PaginatedRequestParams, types.ListResourcesResult, "resources"),
        "read_resource": ("resources/read", types.ReadResourceRequestParams, types.ReadResourceResult, "contents"),
    }[name]

    def register(callback):
        async def invoke(_context, params):
            args = ()
            if name in ("call_tool", "get_prompt"):
                args = (params.name, (params.arguments or {}) if name == "call_tool" else params.arguments)
            elif name == "read_resource":
                args = (params.uri,)
            try:
                if name == "call_tool":
                    # Fetch in this request's auth context; never share a tool schema across owners.
                    listing = server.get_request_handler("tools/list")
                    tools = await listing.handler(_context, types.PaginatedRequestParams())
                    tool = next((item for item in tools.tools if item.name == params.name), None)
                    if tool is not None:
                        jsonschema.validate(args[1], tool.model_dump(by_alias=True)["inputSchema"])
                result = await callback(*args)
            except Exception as exc:
                if name != "call_tool":
                    raise
                return types.CallToolResult(
                    isError=True, content=[types.TextContent(type="text", text=str(exc))]
                )
            if isinstance(result, result_type):
                return result
            if name == "read_resource":
                result = [
                    types.TextResourceContents(
                        uri=params.uri, text=item.content, mimeType=item.mime_type, _meta=item.meta
                    ) if isinstance(item.content, str) else types.BlobResourceContents(
                        uri=params.uri, blob=base64.b64encode(item.content).decode(),
                        mimeType=item.mime_type, _meta=item.meta,
                    )
                    for item in result
                ]
            return result_type(**{field: result})

        server.add_request_handler(method, params_type, invoke)
        return callback

    return register
