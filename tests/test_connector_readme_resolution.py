import asyncio
import json

from scripts.resolve_connector_python_manifests import _git_invocation, _git_parts
from scripts.resolve_connector_npm_locks import _normalize_official_lock
from scripts.resolve_connector_remote_readmes import (
    _REMOTE_OVERRIDES,
    _STDIO_OVERRIDES,
    apply_overrides,
    _github_source,
    _markdown_remote_candidates,
    _recover,
)


def test_readme_git_recovery_ignores_asset_links_and_keeps_monorepo_subdirectory():
    row_key = "trae solo cn:byted-mcp-volcengine.needle"
    markdown = """
![image](https://github.com/user-attachments/assets/example)
```json
{"mcpServers":{"needle":{"command":"uv","args":["--directory","/path/to/needle-mcp","run","needle-mcp"],"env":{"NEEDLE_API_KEY":"<key>"}}}}
```
"""
    assert _github_source(row_key, markdown) == ("https://github.com/needle-ai/needle-mcp", "")
    manifest = _recover(row_key, markdown, "https://example.test/README.md")["runtime_manifest"]
    assert manifest["args"][:3] == [
        "--from", "git+https://github.com/needle-ai/needle-mcp", "needle-mcp"
    ]

    assert _github_source(
        "trae solo cn:byted-mcp-volcengine.live", "https://github.com/volcengine/mcp-server"
    ) == ("https://github.com/volcengine/mcp-server", "server/mcp_server_live")
    assert _github_source(
        "trae solo cn:byted-mcp-volcengine.bocha_search", "git@github.com:BochaAI/bocha-search-mcp.git"
    ) == ("https://github.com/BochaAI/bocha-search-mcp", "")


def test_cloudflare_repository_card_recovers_to_the_official_remote_mcp():
    recovered = _recover(
        "trae solo cn:byted-mcp-volcengine.cloudflare", "unused", "https://example.test/README.md"
    )
    assert recovered == {
        "endpoint": "https://mcp.cloudflare.com/mcp",
        "transport": "streamable_http",
        "fields": [],
        "field_targets": {},
        "auth_flow": "mcp_oauth",
        "state": "resolved",
    }


def test_vendor_only_cards_use_the_owner_supplied_https_adapter_contract():
    recovered = _REMOTE_OVERRIDES["trae solo cn:byted-mcp-volcengine.insurance"]
    assert recovered == {
        "endpoint": "https://adapter.invalid/mcp",
        "transport": "streamable_http",
        "fields": ["MCP_SERVER_URL", "MCP_AUTHORIZATION"],
        "field_targets": {
            "MCP_SERVER_URL": {"kind": "endpoint_base", "path": ""},
            "MCP_AUTHORIZATION": {"kind": "header", "name": "Authorization"},
        },
        "adapter_contract": "owner_supplied_https_mcp",
        "state": "resolved",
    }


def test_official_registry_overrides_replace_stale_build_from_source_instructions():
    assert _STDIO_OVERRIDES["trae solo cn:byted-mcp-volcengine.notion"]["args"] == [
        "-y", "@notionhq/notion-mcp-server@latest"
    ]
    assert _STDIO_OVERRIDES["trae solo cn:byted-mcp-volcengine.browserbase"]["configs"] == [
        {"key": "BROWSERBASE_API_KEY", "type": "env", "required": True, "isPassword": True},
        {"key": "BROWSERBASE_PROJECT_ID", "type": "env", "required": True},
        {"key": "GEMINI_API_KEY", "type": "env", "required": True, "isPassword": True},
    ]
    assert _STDIO_OVERRIDES["trae solo cn:byted-mcp-volcengine.git"] == {
        "command": "uvx",
        "args": ["mcp-server-git", "--repository", "/home/connector"],
        "configs": [],
        "source_url": "https://github.com/modelcontextprotocol/servers",
        "license": "MIT",
    }
    assert _STDIO_OVERRIDES["trae solo cn:byted-mcp-volcengine.flightradar24"] == {
        "command": "npx",
        "args": ["-y", "@flightradar24/fr24api-mcp@latest"],
        "configs": [{
            "key": "FR24_API_KEY", "type": "env", "required": True, "isPassword": True,
        }],
        "static_env": {"FR24_API_URL": "https://fr24api.flightradar24.com"},
        "source_url": "https://github.com/Flightradar24/fr24api-mcp",
        "license": "MIT",
    }
    assert _STDIO_OVERRIDES["trae solo cn:byted-mcp-volcengine.revit-mcp"]["args"] == [
        "-y", "revit-mcp@latest",
    ]
    assert _STDIO_OVERRIDES["trae solo cn:recursechat.mcp-server-apple-shortcuts"]["args"] == [
        "-y", "mcp-server-apple-shortcuts@latest",
    ]
    assert _STDIO_OVERRIDES["trae solo cn:byted-mcp-volcengine.wolframalpha"]["args"] == [
        "mcp-wolfram-alpha",
    ]
    assert _STDIO_OVERRIDES["trae solo cn:byted-mcp-volcengine.china-weather"]["args"] == [
        "-y", "@jablum/weather-mcp@latest",
    ]
    assert _STDIO_OVERRIDES["trae solo cn:byted-mcp-volcengine.gupiaoshuju"]["args"] == [
        "china-a-stock-mcp",
    ]
    assert _STDIO_OVERRIDES["trae solo cn:byted-mcp-volcengine.zoomeye"]["configs"] == [{
        "key": "ZOOMEYE_API_KEY", "type": "env", "required": True, "isPassword": True,
    }]
    assert _STDIO_OVERRIDES["trae solo cn:byted-mcp-volcengine.fireproof"]["source_url"] == (
        "https://github.com/fireproof-storage/mcp-database-server"
    )


def test_retired_web_research_card_recovers_to_a_live_public_mcp():
    assert _REMOTE_OVERRIDES["trae solo cn:chuanmingliu.mcp-webresearch"] == {
        "endpoint": "https://research-mcp.yigitkonur.com/mcp",
        "transport": "streamable_http",
        "fields": [],
        "field_targets": {},
        "state": "resolved",
    }
    assert _REMOTE_OVERRIDES["trae solo cn:byted-mcp-volcengine.sina_finance"]["endpoint"] == (
        "https://mcp.frankfurter.dev/"
    )
    assert _REMOTE_OVERRIDES["trae solo cn:byted-mcp-volcengine.chatppt"]["field_targets"] == {
        "YOO_PPT_KEY": {"kind": "query", "name": "key"},
    }
    assert _REMOTE_OVERRIDES["trae solo cn:byted-mcp-volcengine.opencti"]["field_targets"] == {
        "XTM_ONE_BASE_URL": {"kind": "endpoint_base", "path": "/mcp/opencti"},
        "XTM_ONE_API_KEY": {"kind": "header", "name": "Authorization", "prefix": "Bearer "},
    }
    assert _REMOTE_OVERRIDES["trae solo cn:byted-mcp-volcengine.isjike_mcp"]["transport"] == "sse"


def test_apply_overrides_adds_new_public_remote_replacements(tmp_path):
    source = tmp_path / "recoveries.jsonl"
    output = tmp_path / "updated.jsonl"
    source.write_text("")
    asyncio.run(apply_overrides(source, output))
    asyncio.run(apply_overrides(output, output))
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    recovered = next(
        row for row in rows if row["row_key"] == "trae solo cn:chuanmingliu.mcp-webresearch"
    )
    assert recovered["state"] == "resolved"


def test_git_uvx_invocation_separates_uv_options_from_server_arguments():
    assert _git_invocation(["--from", "git+https://example.test/repo", "server", "--read-only"]) == (
        "server", ["--read-only"]
    )
    assert _git_invocation([
        "--from", "git+https://example.test/repo", "--python", "3.13", "server"
    ]) == ("server", [])
    assert _git_parts(
        "git+https://github.com/acme/server@0123456789abcdef0123456789abcdef01234567#subdirectory=mcp"
    ) == (
        "https://github.com/acme/server", "mcp", "0123456789abcdef0123456789abcdef01234567"
    )


def test_markdown_remote_recovery_strips_example_query_secrets_and_ignores_badges():
    candidates = _markdown_remote_candidates("""
https://img.shields.io/pypi/v/example
https://risk.data.example/mcp/?pname=YOUR_NAME&pkey=example-secret
""")
    assert candidates == [{
        "endpoint": "https://risk.data.example/mcp/",
        "transport": "streamable_http",
        "fields": ["pkey", "pname"],
        "field_targets": {
            "pname": {"kind": "query", "name": "pname"},
            "pkey": {"kind": "query", "name": "pkey"},
        },
        "state": "resolved",
    }]

    bearer = _markdown_remote_candidates(
        "https://mcp.example.test/mcp 请求头 Authorization=Bearer YOUR_TOKEN"
    )
    assert bearer[0]["field_targets"] == {
        "Authorization": {"kind": "header", "name": "Authorization"}
    }


def test_official_npm_lock_must_match_published_dependencies():
    lock = {
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "demo", "version": "1", "dependencies": {"dep": "1.0.0"}},
            "node_modules/dep": {
                "resolved": "https://registry.npmjs.org/dep/-/dep-1.0.0.tgz",
                "integrity": "sha512-example",
            },
        },
    }
    normalized = _normalize_official_lock({"dependencies": {"dep": "1.0.0"}}, lock)
    assert "name" not in normalized["packages"][""]
