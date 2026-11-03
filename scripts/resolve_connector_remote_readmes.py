#!/usr/bin/env python3
"""Recover remote MCP endpoints from the official TRAE catalog README links."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shlex
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml

try:
    from scripts.generate_connector_stdio_manifests import _manifest
except ModuleNotFoundError:  # direct script execution
    from generate_connector_stdio_manifests import _manifest


_PACKAGE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(?:@[^/\s]+)?$")
_IGNORED_REMOTE_HOSTS = {
    "api.github.com", "code.visualstudio.com", "docs.astral.sh", "docs.windsurf.com",
    "gitee.com", "github.com", "glama.ai", "gofastmcp.com", "goreportcard.com", "img.shields.io",
    "lbs.amap.com", "mcp.so", "medium.com", "modelcontextprotocol.io", "neo4j.com",
    "pkg.go.dev", "pypi.org", "registry.npmjs.org", "smithery.ai", "www.figma.com",
    "vojh.gtja.com", "www.klavis.ai", "www.showapi.com.cn", "www.volcengine.com",
    "yoo-web-public.cdn.bcebos.com",
}
_PYTHON_MODULES = {
    "trae solo cn:byted-mcp-volcengine.tavily_search": "mcp-tavily",
}
_PYTHON_SOURCES = {
    "trae solo cn:byted-mcp-volcengine.coin_api": (
        "git+https://github.com/longmans/coin_api_mcp.git", "coin_api_mcp"
    ),
}
_GITHUB_SOURCES = {
    "trae solo cn:byted-mcp-volcengine.bocha_search": ("https://github.com/BochaAI/bocha-search-mcp", ""),
    "trae solo cn:byted-mcp-volcengine.chroma": ("https://github.com/chroma-core/chroma-mcp", ""),
    "trae solo cn:byted-mcp-volcengine.cognee-mcp": ("https://github.com/topoteretes/cognee", "cognee-mcp"),
    "trae solo cn:byted-mcp-volcengine.dataset-viewer": ("https://github.com/privetin/dataset-viewer", ""),
    "trae solo cn:byted-mcp-volcengine.ida-pro-mcp": ("https://github.com/mrexodia/ida-pro-mcp", ""),
    "trae solo cn:byted-mcp-volcengine.live": ("https://github.com/volcengine/mcp-server", "server/mcp_server_live"),
    "trae solo cn:byted-mcp-volcengine.mcp_server_live": ("https://github.com/volcengine/mcp-server", "server/mcp_server_live"),
    "trae solo cn:byted-mcp-volcengine.mcp_server_vmp": ("https://github.com/volcengine/mcp-server", "server/mcp_server_vmp"),
    "trae solo cn:byted-mcp-volcengine.needle": ("https://github.com/needle-ai/needle-mcp", ""),
    "trae solo cn:byted-mcp-volcengine.spotify": ("https://github.com/varunneal/spotify-mcp", ""),
    "trae solo cn:byted-mcp-volcengine.vmp": ("https://github.com/volcengine/mcp-server", "server/mcp_server_vmp"),
}
_EXECUTABLES = {
    "trae solo cn:byted-mcp-volcengine.ida-pro-mcp": "ida-pro-mcp",
}
_REMOTE_OVERRIDES = {
    "trae solo cn:byted-mcp-volcengine.axiom": {
        "endpoint": "https://mcp.axiom.co/mcp",
        "transport": "streamable_http",
        "fields": [],
        "field_targets": {},
        "auth_flow": "mcp_oauth",
        "state": "resolved",
    },
    "trae solo cn:byted-mcp-volcengine.glean": {
        "endpoint": "https://docs.glean.com/mcp",
        "transport": "streamable_http",
        "fields": ["GLEAN_MCP_URL"],
        "field_targets": {"GLEAN_MCP_URL": {"kind": "endpoint", "name": "url"}},
        "endpoint_field": "GLEAN_MCP_URL",
        "endpoint_host_suffix": "-be.glean.com",
        "endpoint_path_prefix": "/mcp/",
        "auth_flow": "mcp_oauth",
        "state": "resolved",
    },
    "trae solo cn:byted-mcp-volcengine.mem0-mcp": {
        "endpoint": "https://mcp.mem0.ai/mcp/",
        "transport": "streamable_http",
        "fields": [],
        "field_targets": {},
        "auth_flow": "mcp_oauth",
        "state": "resolved",
    },
    "trae solo cn:byted-mcp-volcengine.3rd_caocaochuxing": {
        "endpoint": "https://mcp.caocaokeji.cn/",
        "transport": "streamable_http",
        "fields": ["Authorization"],
        "field_targets": {"Authorization": {"kind": "header", "name": "Authorization"}},
        "state": "resolved",
    },
    "trae solo cn:byted-mcp-volcengine.jipiaojiudianshuju": {
        "endpoint": "https://gateway.wochujia.com/ai/mcp",
        "transport": "streamable_http",
        "fields": ["apikey"],
        "field_targets": {"apikey": {"kind": "header", "name": "apikey"}},
        "state": "resolved",
    },
    "trae solo cn:byted-mcp-volcengine.kxss_mcp": {
        "endpoint": "https://mcp.dknowc.cn/s3/sse",
        "transport": "sse", "fields": ["auth"],
        "field_targets": {"auth": {"kind": "header", "name": "auth"}}, "state": "resolved",
    },
    "trae solo cn:byted-mcp-volcengine.kxwd": {
        "endpoint": "https://mcp.dknowc.cn/s1/sse",
        "transport": "sse", "fields": ["auth"],
        "field_targets": {"auth": {"kind": "header", "name": "auth"}}, "state": "resolved",
    },
    "trae solo cn:byted-mcp-volcengine.kxzh": {
        "endpoint": "https://mcp.dknowc.cn/s2/sse",
        "transport": "sse", "fields": ["auth"],
        "field_targets": {"auth": {"kind": "header", "name": "auth"}}, "state": "resolved",
    },
    "trae solo cn:byted-mcp-volcengine.meta_human": {
        "endpoint": "https://sd28sr3a80c6ft26qf5c0.apigateway-cn-beijing.volceapi.com/mcp/",
        "transport": "streamable_http", "fields": [], "field_targets": {}, "state": "resolved",
    },
    **{
        row_key: {
            "endpoint": f"https://www.showapi.com.cn/mcp/{api_code}",
            "transport": "streamable_http",
            "fields": ["SHOWAPI_APP_KEY"],
            "field_targets": {"SHOWAPI_APP_KEY": {"kind": "path_segment"}},
            "state": "resolved",
        }
        for row_key, api_code in {
            "trae solo cn:byted-mcp-volcengine.qrcode_mcp": "887",
            "trae solo cn:byted-mcp-volcengine.xhdq_mcp": "341",
            "trae solo cn:byted-mcp-volcengine.xzys": "872",
            "trae solo cn:byted-mcp-volcengine.yjcx": "138",
        }.items()
    },
    "trae solo cn:byted-mcp-volcengine.cloudflare": {
        "endpoint": "https://mcp.cloudflare.com/mcp",
        "transport": "streamable_http",
        "fields": [],
        "field_targets": {},
        "auth_flow": "mcp_oauth",
        "state": "resolved",
    },
    "trae solo cn:chuanmingliu.mcp-webresearch": {
        "endpoint": "https://research-mcp.yigitkonur.com/mcp",
        "transport": "streamable_http",
        "fields": [],
        "field_targets": {},
        "state": "resolved",
    },
    "trae solo cn:byted-mcp-volcengine.sina_finance": {
        "endpoint": "https://mcp.frankfurter.dev/",
        "transport": "streamable_http",
        "fields": [],
        "field_targets": {},
        "state": "resolved",
    },
    "trae solo cn:byted-mcp-volcengine.chatppt": {
        "endpoint": "https://mcp.yoo-ai.com/mcp",
        "transport": "streamable_http",
        "fields": ["YOO_PPT_KEY"],
        "field_targets": {"YOO_PPT_KEY": {"kind": "query", "name": "key"}},
        "state": "resolved",
    },
    "trae solo cn:byted-mcp-volcengine.opencti": {
        "endpoint": "https://xtm-one.example/mcp/opencti",
        "transport": "streamable_http",
        "fields": ["XTM_ONE_BASE_URL", "XTM_ONE_API_KEY"],
        "field_targets": {
            "XTM_ONE_BASE_URL": {"kind": "endpoint_base", "path": "/mcp/opencti"},
            "XTM_ONE_API_KEY": {
                "kind": "header", "name": "Authorization", "prefix": "Bearer ",
            },
        },
        "state": "resolved",
    },
    "trae solo cn:byted-mcp-volcengine.isjike_mcp": {
        "endpoint": "https://mcp.isjike.com/mcp-servers/opendata/sse",
        "transport": "sse",
        "fields": ["ISJIKE_API_KEY"],
        "field_targets": {
            "ISJIKE_API_KEY": {"kind": "header", "name": "Authorization", "prefix": "Bearer "},
        },
        "state": "resolved",
    },
    **{
        row_key: {
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
        for row_key in {
            "trae solo cn:byted-mcp.juejin-deploy-mcp",
            "trae solo cn:simonb97.win-cli-mcp-server",
            "trae solo cn:mamertofabian.mcp-everything-search",
            "trae solo cn:byted-mcp-volcengine.insurance",
            "trae solo cn:byted-mcp-volcengine.yuansuleida",
            "trae solo cn:byted-mcp-volcengine.yuansujuzheng",
            "trae solo cn:byted-mcp-volcengine.airuishuzhi",
            "trae solo cn:byted-mcp-volcengine.wanxin_tianmuai",
            "trae solo cn:byted-mcp-volcengine.recommendation",
            "trae solo cn:byted-mcp-volcengine.error_code_parsing",
            "trae solo cn:byted-mcp-volcengine.cec_iot",
            "trae solo cn:byted-mcp-volcengine.chatsum",
            "trae solo cn:byted-mcp-volcengine.chatmcp",
            "trae solo cn:byted-mcp-volcengine.9m_tec",
            "trae solo cn:byted-mcp-volcengine.metoro",
            "trae solo cn:byted-mcp-volcengine.niuqicha",
            "trae solo cn:byted-mcp-volcengine.chuhaijiang",
            "trae solo cn:byted-mcp-volcengine.yunlogin",
            "trae solo cn:byted-mcp-volcengine.mongo",
            "trae solo cn:byted-mcp-volcengine.oatpp-mcp",
            "trae solo cn:byted-mcp-volcengine.drupal",
            "trae solo cn:byted-mcp-volcengine.unitymcp",
            "trae solo cn:byted-mcp-volcengine.jijyun_mcp",
            "trae solo cn:byted-mcp-volcengine.mobile_use",
            "trae solo cn:byted-mcp-volcengine.shangqi",
            "trae solo cn:byted-mcp-volcengine.chunteng_chuotiben",
            "trae solo cn:byted-mcp-volcengine.handaas_news",
            "trae solo cn:byted-mcp-volcengine.3rd_party_mcp_chuhaijiang",
            "trae solo cn:byted-mcp-volcengine.3rd_party_mcp_mongo_mcp_server",
            "trae solo cn:byted-mcp-volcengine.3rd_party_mcp_accessory_recommendation",
            "trae solo cn:byted-mcp-volcengine.3rd_party_mcp_shangqi",
            "trae solo cn:byted-mcp-volcengine.mcp_server_mobile_use",
        }
    },
}
_STDIO_OVERRIDES = {
    **{
        row_key: {
            "command": "npx",
            "args": ["-y", "@jablum/weather-mcp@latest"],
            "configs": [],
            "source_url": "https://github.com/jablum/weather-mcp",
            "license": "MIT",
        }
        for row_key in {
            "trae solo cn:byted-mcp-volcengine.china-weather",
            "trae solo cn:byted-mcp-volcengine.weatherol",
            "trae solo cn:byted-mcp-volcengine.3rd_party_mcp_weatherol",
        }
    },
    **{
        row_key: {
            "command": "uvx",
            "args": ["china-a-stock-mcp"],
            "configs": [],
            "static_env": {"CSM_LOG_LEVEL": "WARNING"},
            "source_url": "https://github.com/wax0629/china-stock-mcp",
            "license": "MIT",
        }
        for row_key in {
            "trae solo cn:byted-mcp-volcengine.gupiaoshuju",
            "trae solo cn:byted-mcp-volcengine.example",
            "trae solo cn:byted-mcp-volcengine.lingxi",
            "trae solo cn:byted-mcp-volcengine.wenda_query",
        }
    },
    "trae solo cn:byted-mcp-volcengine.zoomeye": {
        "command": "uvx",
        "args": ["mcp-server-zoomeye"],
        "configs": [{
            "key": "ZOOMEYE_API_KEY", "type": "env", "required": True, "isPassword": True,
        }],
        "source_url": "https://github.com/zoomeye-ai/mcp_zoomeye",
        "license": "MIT",
    },
    "trae solo cn:byted-mcp-volcengine.fireproof": {
        "command": "npx",
        "args": ["-y", "todos"],
        "configs": [],
        "source_url": "https://github.com/fireproof-storage/mcp-database-server",
        "license": "MIT",
    },
    "trae solo cn:byted-mcp-volcengine.git": {
        "command": "uvx",
        "args": ["mcp-server-git", "--repository", "/home/connector"],
        "configs": [],
        "source_url": "https://github.com/modelcontextprotocol/servers",
        "license": "MIT",
    },
    "trae solo cn:byted-mcp-volcengine.flightradar24": {
        "command": "npx",
        "args": ["-y", "@flightradar24/fr24api-mcp@latest"],
        "configs": [{
            "key": "FR24_API_KEY", "type": "env", "required": True, "isPassword": True,
        }],
        "static_env": {"FR24_API_URL": "https://fr24api.flightradar24.com"},
        "source_url": "https://github.com/Flightradar24/fr24api-mcp",
        "license": "MIT",
    },
    "trae solo cn:byted-mcp-volcengine.revit-mcp": {
        "command": "npx",
        "args": ["-y", "revit-mcp@latest"],
        "configs": [],
        "source_url": "https://github.com/vespo92/revit-mcp",
        "license": "MIT",
    },
    "trae solo cn:recursechat.mcp-server-apple-shortcuts": {
        "command": "npx",
        "args": ["-y", "mcp-server-apple-shortcuts@latest"],
        "configs": [],
        "source_url": "https://github.com/recursechat/mcp-server-apple-shortcuts",
        "license": "Apache-2.0",
    },
    "trae solo cn:byted-mcp-volcengine.wolframalpha": {
        "command": "uvx",
        "args": ["mcp-wolfram-alpha"],
        "configs": [{
            "key": "WOLFRAM_API_KEY", "type": "env", "required": True, "isPassword": True,
        }],
        "source_url": "https://pypi.org/project/mcp-wolfram-alpha/",
        "license": "MIT",
    },
    "trae solo cn:byted-mcp-volcengine.markitdown": {
        "command": "uvx",
        "args": ["markitdown-mcp"],
        "configs": [],
        "source_url": "https://github.com/microsoft/markitdown",
        "license": "MIT",
    },
    "trae solo cn:byted-mcp-volcengine.neo4j": {
        "command": "uvx",
        "args": ["neo4j-mcp-server"],
        "configs": [
            {"key": "NEO4J_URI", "type": "env", "required": True},
            {"key": "NEO4J_USERNAME", "type": "env", "required": True},
            {"key": "NEO4J_PASSWORD", "type": "env", "required": True, "isPassword": True},
        ],
        "static_env": {"NEO4J_READ_ONLY": "true", "NEO4J_TELEMETRY": "false"},
        "source_url": "https://github.com/neo4j/mcp",
        "license": "GPL-3.0",
    },
    "trae solo cn:byted-mcp-volcengine.browserbase": {
        "command": "npx",
        "args": ["-y", "@browserbasehq/mcp-server-browserbase@latest"],
        "configs": [
            {"key": "BROWSERBASE_API_KEY", "type": "env", "required": True, "isPassword": True},
            {"key": "BROWSERBASE_PROJECT_ID", "type": "env", "required": True},
            {"key": "GEMINI_API_KEY", "type": "env", "required": True, "isPassword": True},
        ],
        "source_url": "https://github.com/browserbase/mcp-server-browserbase",
        "license": "Apache-2.0",
    },
    "trae solo cn:byted-mcp-volcengine.notion": {
        "command": "npx",
        "args": ["-y", "@notionhq/notion-mcp-server@latest"],
        "configs": [
            {"key": "NOTION_TOKEN", "type": "env", "required": True, "isPassword": True},
        ],
        "source_url": "https://github.com/makenotion/notion-mcp-server",
        "license": "MIT",
    },
    "trae solo cn:byted-mcp-volcengine.openapi": {
        "command": "npx",
        "args": ["-y", "openapi-mcp-server@latest"],
        "configs": [],
        "source_url": "https://github.com/janwilmake/openapi-mcp-server",
        "license": "MIT",
    },
    "trae solo cn:byted-mcp-volcengine.google_calendar": {
        "command": "npx",
        "args": ["-y", "@cocal/google-calendar-mcp@latest"],
        "configs": [{
            "key": "GOOGLE_OAUTH_CREDENTIALS_JSON", "type": "file", "required": True,
            "isPassword": True, "path": "credentials/google-oauth.json",
        }],
        "static_env": {
            "GOOGLE_OAUTH_CREDENTIALS": "/home/connector/credentials/google-oauth.json",
            "ENABLED_TOOLS": "list-events,search-events,get-event,list-calendars,get-current-time,find-free-time",
        },
        "source_url": "https://github.com/nspady/google-calendar-mcp",
        "license": "MIT",
    },
    "trae solo cn:byted-mcp-volcengine.google_tasks": {
        "command": "npx",
        "args": ["-y", "@scottie-will/google-tasks-mcp@latest"],
        "configs": [{
            "key": "GOOGLE_OAUTH_CREDENTIALS_JSON", "type": "file", "required": True,
            "isPassword": True, "path": "credentials/google-oauth.json",
        }],
        "static_env": {
            "GOOGLE_OAUTH_CREDENTIALS": "/home/connector/credentials/google-oauth.json",
        },
        "source_url": "https://github.com/scottie-will/google-tasks-mcp",
        "license": "MIT",
    },
    "trae solo cn:byted-mcp-volcengine.home_assistant": {
        "command": "npx",
        "args": ["-y", "@jango-blockchained/homeassistant-mcp@latest"],
        "configs": [
            {"key": "HASS_HOST", "type": "env", "required": True},
            {"key": "HASS_TOKEN", "type": "env", "required": True, "isPassword": True},
        ],
        "source_url": "https://github.com/jango-blockchained/homeassistant-mcp",
        "license": "MIT",
    },
    "trae solo cn:byted-mcp.ida-pro-mcp": {
        "command": "uvx",
        "args": ["--from", "git+https://github.com/mrexodia/ida-pro-mcp", "ida-pro-mcp"],
        "configs": [],
        "source_url": "https://github.com/mrexodia/ida-pro-mcp",
        "license": "MIT",
    },
    "trae solo cn:byted-mcp.supabase-mcp": {
        "command": "npx",
        "args": ["-y", "@supabase/mcp-server-supabase@latest", "--read-only"],
        "configs": [{"key": "SUPABASE_ACCESS_TOKEN", "type": "env", "required": True, "isPassword": True}],
        "source_url": "https://github.com/supabase-community/supabase-mcp",
        "license": "MIT",
    },
}
_VERIFIED_PUBLIC_MARKDOWN_REMOTES = {
    "trae solo cn:byted-mcp-volcengine.stockhkf10",
    "trae solo cn:byted-mcp-volcengine.zm",
}
_EXTRA_FIELDS = {
    "trae solo cn:byted-mcp-volcengine.coin_api": ["COINMARKETCAP_API_KEY"],
    "trae solo cn:byted-mcp-volcengine.tavily_search": ["TAVILY_API_KEY"],
}


def _readme_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.hostname == "api.trae.com.cn":
        file_id = urllib.parse.parse_qs(parsed.query).get("fileID", [""])[0]
        if file_id.startswith("lf3-static.bytednsdoc.com/"):
            value = "https://" + file_id
    parsed = urllib.parse.urlsplit(value)
    return urllib.parse.urlunsplit((
        parsed.scheme,
        parsed.netloc,
        urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/%:@"),
        parsed.query,
        parsed.fragment,
    ))


def _fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-connector-resolver/1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        if int(response.headers.get("Content-Length") or 0) > 2 * 1024 * 1024:
            raise ValueError("README is too large")
        return response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")


def _documents(markdown: str) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for block in re.findall(r"```(?:json5?|yaml|yml)?[^\r\n]*\r?\n(.*?)```", markdown, re.I | re.S):
        try:
            value = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(value, dict) and isinstance(value.get("mcpServers"), dict):
            documents.append(value)
    return documents


def _placeholder(value: str) -> bool:
    return not value.strip() or bool(re.search(
        r"(?i)(\$\{|<[^>]+>|\{[^}]+\}|your[_ -]|replace[_ -]|api[_ -]?key\b|x{4,}|填写|申请|您的|你的)",
        value,
    ))


def _remote_candidate(spec: dict[str, Any]) -> dict[str, Any] | None:
    endpoint = str(spec.get("url") or "").strip()
    if not endpoint.startswith("https://"):
        return None
    transport = re.sub(r"[-_ ]", "", str(spec.get("type") or "").casefold())
    if transport not in {"http", "streamablehttp", "sse"}:
        transport = "sse" if endpoint.rstrip("/").endswith("/sse") else "streamable_http"
    else:
        transport = "sse" if transport == "sse" else "streamable_http"

    fields: list[str] = []
    targets: dict[str, dict[str, str]] = {}
    headers = spec.get("headers") or {}
    if not isinstance(headers, dict):
        return None
    for raw_name in headers:
        name = str(raw_name).strip()
        if name.casefold() in {"accept", "content-type"}:
            continue
        fields.append(name)
        targets[name] = {"kind": "header", "name": name}

    parsed = urllib.parse.urlsplit(endpoint)
    clean_query: list[tuple[str, str]] = []
    for name, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        credential = _placeholder(value) or bool(re.search(r"(?i)(api.?key|token|secret|password|app.?id)", name))
        if not credential:
            clean_query.append((name, value))
            continue
        field = name if name not in targets else f"query:{name}"
        fields.append(field)
        targets[field] = {"kind": "query", "name": name}
    endpoint = urllib.parse.urlunsplit((
        parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(clean_query), ""
    ))
    return {
        "endpoint": endpoint,
        "transport": transport,
        "fields": sorted(fields),
        "field_targets": targets,
        "state": "resolved",
    }


def _markdown_remote_candidates(markdown: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    indexes: dict[tuple[str, str], int] = {}
    header_fields = sorted(set(re.findall(
        r"(?i)\b(Authorization|Api-Key|X-API-Key|X-Auth-Key|access-token|tripnow-api-key|yqc-mcp-api-key)\b",
        markdown,
    )))
    for match in re.finditer(r"https://[^\s<>\"'`\\]+", markdown):
        raw = re.split(r"[）】。，，；；]", match.group(0), maxsplit=1)[0].rstrip(").,;]}")
        try:
            parsed = urllib.parse.urlsplit(raw)
        except ValueError:
            continue
        hostname = str(parsed.hostname or "").casefold()
        identity = f"{hostname}{parsed.path}".casefold()
        if (
            not hostname or hostname in _IGNORED_REMOTE_HOSTS
            or parsed.path.casefold().endswith((".htm", ".html"))
            or any(ext in parsed.path.casefold() for ext in (".gif", ".jpg", ".jpeg", ".mp4", ".png", ".svg", ".webp"))
            or not ("mcp" in identity or "/sse" in parsed.path.casefold() or "/streamable" in parsed.path.casefold())
            or _placeholder(urllib.parse.unquote(parsed.path))
        ):
            continue
        fields: list[str] = []
        targets: dict[str, dict[str, str]] = {}
        clean_query: list[tuple[str, str]] = []
        for name, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            if _placeholder(value) or re.search(r"(?i)(api.?key|token|secret|password|app.?id|pkey|pname|key)", name):
                fields.append(name)
                targets[name] = {"kind": "query", "name": name}
            else:
                clean_query.append((name, value))
        endpoint = urllib.parse.urlunsplit((
            "https", parsed.netloc, parsed.path, urllib.parse.urlencode(clean_query), ""
        ))
        transport = "sse" if "/sse" in parsed.path.casefold() else "streamable_http"
        marker = (endpoint, transport)
        candidate = {
            "endpoint": endpoint,
            "transport": transport,
            "fields": sorted(fields),
            "field_targets": targets,
            "state": "resolved",
        }
        if not fields and header_fields:
            candidate["fields"] = header_fields
            candidate["field_targets"] = {
                name: {"kind": "header", "name": name} for name in header_fields
            }
        if marker in indexes:
            index = indexes[marker]
            if len(candidate["fields"]) > len(candidates[index]["fields"]):
                candidates[index] = candidate
            continue
        indexes[marker] = len(candidates)
        candidates.append(candidate)
    return candidates


def _dedupe_remote_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = (candidate["endpoint"], candidate["transport"])
        if key not in result or len(candidate["fields"]) > len(result[key]["fields"]):
            result[key] = candidate
    return list(result.values())


def _github_source(row_key: str, markdown: str) -> tuple[str, str] | None:
    if row_key in _GITHUB_SOURCES:
        return _GITHUB_SOURCES[row_key]
    for match in re.finditer(
        r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/tree/[^/\s)\]]+/([^\s)\]#?]+))?",
        markdown,
    ):
        owner, repository, subdirectory = match.groups()
        repository = repository.removesuffix(".git")
        if owner.casefold() in {"user-attachments", "yourusername"} or (
            owner.casefold() == "modelcontextprotocol" and repository in {"servers", "python-sdk"}
        ) or (owner.casefold(), repository.casefold()) in {
            ("astral-sh", "uv"), ("spotipy-dev", "spotipy")
        }:
            continue
        return f"https://github.com/{owner}/{repository}", str(subdirectory or "").rstrip("/.,")
    return None


def _stdio_candidate(
    row_key: str, spec: dict[str, Any], readme_url: str,
    github_source: tuple[str, str] | None,
) -> dict[str, Any] | None:
    raw_command = str(spec.get("command") or "").strip()
    tokens = shlex.split(raw_command)
    if len(tokens) != 1:
        return None
    command = Path(tokens[0]).name
    args = [str(value) for value in (spec.get("args") or [])]
    if command == "bunx":
        command = "npx"
    if command in {"python", "python3"} and args[:1] == ["-m"] and row_key in _PYTHON_MODULES:
        command, args = "uvx", [_PYTHON_MODULES[row_key], *args[2:]]
    if command in {"python", "python3"} and args[:1] == ["-m"] and row_key in _PYTHON_SOURCES:
        source, executable = _PYTHON_SOURCES[row_key]
        command, args = "uvx", ["--from", source, executable, *args[2:]]
    if command == "uv" and github_source and "--directory" in args and "run" in args:
        run_index = args.index("run")
        if run_index + 1 >= len(args):
            return None
        repository, subdirectory = github_source
        executable = _EXECUTABLES.get(row_key, args[run_index + 1])
        tail = args[run_index + 2:]
        source = f"git+{repository}" + (f"#subdirectory={subdirectory}" if subdirectory else "")
        command, args = "uvx", ["--from", source, executable, *tail]
    if command not in {"npx", "uvx"}:
        return None

    index = next((i for i, value in enumerate(args) if not value.startswith("-")), -1)
    if index < 0:
        return None
    package = args[index]
    if command == "uvx" and package == "--from" and index + 1 < len(args):
        package = args[index + 1]
    if command == "npx" and not _PACKAGE.fullmatch(package):
        return None
    if command == "uvx" and not (
        _PACKAGE.fullmatch(package)
        or re.fullmatch(r"git\+https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?(?:@[^#\s]+)?(?:#subdirectory=[^\s]+)?", package)
    ):
        return None

    unsafe = re.compile(
        r"(?i)(localhost|127\.0\.0\.1|(?:^|[/\\])(users?|home|absolute|path|parent|workspace)(?:[/\\]|$)|"
        r"<[^>]+>|\{[^}]+\}|\$\{|your[_ -]|replace[_ -]|api[_ -]?key|token|secret|password|填写|您的|你的)"
    )
    if any(unsafe.search(value) for value in args):
        return None

    configs: list[dict[str, Any]] = []
    static_env: dict[str, str] = {}
    for raw_name, raw_value in (spec.get("env") or {}).items():
        name, value = str(raw_name).strip(), str(raw_value or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name.upper() in {
            "HOME", "PATH", "PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        }:
            return None
        secret = bool(re.search(r"(?i)(key|token|secret|password|credential|client.?id)", name))
        if secret or _placeholder(value):
            if not secret and re.search(r"(?i)(path|dir|file)", name):
                return None
            configs.append({"key": name, "type": "env", "required": True, "isPassword": secret})
        else:
            static_env[name] = value
    known = {item["key"] for item in configs}
    configs.extend(
        {"key": name, "type": "env", "required": True, "isPassword": True}
        for name in _EXTRA_FIELDS.get(row_key, []) if name not in known
    )

    manifest = _manifest(
        row_key=row_key, command=command, args=args, configs=configs,
        source_url=readme_url, source_version="official-readme-snapshot",
        license_name="official-readme", static_env=static_env,
    )
    return {"state": "stdio_resolved", "runtime_manifest": manifest}


def _recover(row_key: str, markdown: str, readme_url: str) -> dict[str, Any]:
    if row_key in _REMOTE_OVERRIDES:
        return _REMOTE_OVERRIDES[row_key].copy()
    candidates: list[dict[str, Any]] = []
    stdio: list[dict[str, Any]] = []
    github_source = _github_source(row_key, markdown)
    for document in _documents(markdown):
        for spec in document["mcpServers"].values():
            if not isinstance(spec, dict):
                continue
            candidate = _remote_candidate(spec)
            if candidate:
                candidates.append(candidate)
            candidate = _stdio_candidate(row_key, spec, readme_url, github_source)
            if candidate:
                stdio.append(candidate)
    if not candidates and stdio:
        return stdio[0]
    if not candidates:
        candidates = _markdown_remote_candidates(markdown)
        if candidates and not any(candidate["fields"] for candidate in candidates) and row_key not in _VERIFIED_PUBLIC_MARKDOWN_REMOTES:
            return {"state": "unavailable", "reason": "live_probe_requires_undocumented_auth_or_is_incompatible"}
    if not candidates:
        return {
            "state": "unavailable", "reason": "official_readme_has_no_supported_config"
        }
    candidates = _dedupe_remote_candidates(candidates)
    candidates.sort(key=lambda item: (
        item["state"] != "resolved",
        urllib.parse.urlsplit(item["endpoint"]).path in {"", "/"},
        item["transport"] != "streamable_http",
        len(item["fields"]),
    ))
    return candidates[0]


async def resolve(source: dict[str, Any], semaphore: asyncio.Semaphore) -> dict[str, Any]:
    row_key = f"trae solo cn:{str(source['id']).casefold()}"
    if row_key in _STDIO_OVERRIDES:
        spec = _STDIO_OVERRIDES[row_key]
        return {
            "row_key": row_key,
            "readme_url": spec["source_url"],
            "state": "stdio_resolved",
            "runtime_manifest": _manifest(
                row_key=row_key, command=spec["command"], args=spec["args"], configs=spec["configs"],
                source_url=spec["source_url"], source_version="official-repository",
                license_name=spec["license"], static_env=spec.get("static_env"),
            ),
        }
    if row_key in _REMOTE_OVERRIDES:
        return {
            "row_key": row_key,
            "readme_url": str(source.get("readme") or ""),
            **_REMOTE_OVERRIDES[row_key],
        }
    url = _readme_url(str(source.get("readme") or ""))
    base = {"row_key": row_key, "readme_url": url}
    if not url.startswith("https://"):
        return {**base, "state": "unavailable", "reason": "official_readme_url_missing"}
    try:
        async with semaphore:
            markdown = await asyncio.to_thread(_fetch, url)
        result = _recover(row_key, markdown, url)
        return {
            **base,
            **result,
            "readme_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
        }
    except Exception as exc:
        return {**base, "state": "unavailable", "reason": type(exc).__name__}


async def run(snapshot: Path, manifests: Path, output: Path) -> None:
    sources = {str(item["id"]).casefold(): item for item in json.loads(snapshot.read_text())["data"]}
    rows = [json.loads(line) for line in manifests.read_text().splitlines() if line]
    targets = [
        sources[row["row_key"].split(":", 1)[1]]
        for row in rows
        if (row.get("state") == "repository_only" or row.get("command") not in {"npx", "uvx"})
        and row["row_key"].split(":", 1)[1] in sources
        and (
            "volcengine.com" in str(row.get("source_url") or "")
            or row["row_key"] in _STDIO_OVERRIDES
            or row["row_key"] in _REMOTE_OVERRIDES
        )
    ]
    semaphore = asyncio.Semaphore(12)
    results = await asyncio.gather(*(resolve(source, semaphore) for source in targets))
    results.sort(key=lambda item: item["row_key"])
    output.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in results),
        encoding="utf-8",
    )


async def retry_existing(existing: Path, output: Path) -> None:
    rows = [json.loads(line) for line in existing.read_text().splitlines() if line]
    semaphore = asyncio.Semaphore(12)
    results = await asyncio.gather(*(resolve({
        "id": row["row_key"].split(":", 1)[1], "readme": row["readme_url"],
    }, semaphore) for row in rows))
    results.sort(key=lambda item: item["row_key"])
    output.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in results),
        encoding="utf-8",
    )


async def apply_overrides(existing: Path, output: Path) -> None:
    rows = [json.loads(line) for line in existing.read_text().splitlines() if line]
    by_key = {row["row_key"]: row for row in rows}
    for row_key in {*_STDIO_OVERRIDES, *_REMOTE_OVERRIDES}:
        current = by_key.get(row_key)
        if current is None:
            if row_key in _STDIO_OVERRIDES:
                by_key[row_key] = await resolve({"id": row_key.split(":", 1)[1]}, asyncio.Semaphore(1))
            elif row_key in _REMOTE_OVERRIDES:
                by_key[row_key] = {"row_key": row_key, "readme_url": "", **_REMOTE_OVERRIDES[row_key]}
            continue
        by_key[row_key] = await resolve({
            "id": row_key.split(":", 1)[1], "readme": current.get("readme_url", ""),
        }, asyncio.Semaphore(1))
    output.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                for item in sorted(by_key.values(), key=lambda item: item["row_key"])),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trae-snapshot", type=Path)
    parser.add_argument("--trae-url")
    parser.add_argument("--retry-existing", type=Path)
    parser.add_argument("--apply-overrides", type=Path)
    parser.add_argument("--manifests", type=Path, default=Path(
        "hermes_multitenancy/connector_catalog_data/stdio_manifests.jsonl"
    ))
    parser.add_argument("--output", type=Path, default=Path(
        "hermes_multitenancy/connector_catalog_data/stdio_remote_recoveries.jsonl"
    ))
    args = parser.parse_args()
    if args.apply_overrides:
        asyncio.run(apply_overrides(args.apply_overrides, args.output))
    elif args.retry_existing:
        asyncio.run(retry_existing(args.retry_existing, args.output))
    elif args.trae_snapshot:
        asyncio.run(run(args.trae_snapshot, args.manifests, args.output))
    elif args.trae_url:
        with urllib.request.urlopen(urllib.request.Request(
            args.trae_url, headers={"User-Agent": "hermes-connector-resolver/1"}
        ), timeout=30) as response:
            payload = response.read(8 * 1024 * 1024)
        import tempfile
        with tempfile.NamedTemporaryFile() as snapshot:
            snapshot.write(payload)
            snapshot.flush()
            asyncio.run(run(Path(snapshot.name), args.manifests, args.output))
    else:
        parser.error("--trae-snapshot or --retry-existing is required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
