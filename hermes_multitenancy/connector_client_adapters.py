"""Secretless config projections for known MCP client shapes."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.parse import urlsplit, urlunsplit


CLIENTS = (
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


def _urls(gateway_url: str) -> tuple[str, str]:
    value = str(gateway_url or "").strip()
    parsed = urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or (parsed.scheme == "http" and not loopback)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("gateway_url must be HTTPS or loopback HTTP without credentials or query")
    path = parsed.path.rstrip("/")
    sse_path = f"{path[:-4]}/sse" if path.endswith("/mcp") else f"{path}/sse"
    return value.rstrip("/"), urlunsplit((parsed.scheme, parsed.netloc, sse_path, "", ""))


def _stdio(launcher: str) -> dict[str, Any]:
    return {
        "mcpServers": {
            "hermes-connectors": {
                "command": launcher,
                "args": ["-m", "hermes_multitenancy.connector_mcp_stdio"],
            }
        }
    }


def build_projection(
    client: str,
    *,
    gateway_url: str,
    launcher: str = sys.executable,
) -> dict[str, Any]:
    """Return data-only client config; credentials remain in OAuth/broker state."""
    name = str(client or "").strip().lower()
    if name not in CLIENTS:
        raise ValueError(f"unsupported client: {client}")
    mcp_url, sse_url = _urls(gateway_url)
    if name == "codex":
        return {
            "transport": "streamable-http+oauth",
            "config_toml": (
                '[mcp_servers.hermes-connectors]\n'
                f'url = {json.dumps(mcp_url)}\n'
                'auth = "oauth"\n'
                'scopes = ["mcp:tools"]\n'
            ),
            "bearer_env_config_toml": (
                '[mcp_servers.hermes-connectors]\n'
                f'url = {json.dumps(mcp_url)}\n'
                'bearer_token_env_var = "HERMES_MCP_CLIENT_TOKEN"\n'
            ),
        }
    if name == "claude":
        return {
            "transport": "streamable-http+oauth",
            "command": [
                "claude", "mcp", "add", "--transport", "http",
                "hermes-connectors", mcp_url,
            ],
        }
    if name == "cursor":
        return {
            "transport": "streamable-http+oauth",
            "mcp.json": {"mcpServers": {"hermes-connectors": {"url": mcp_url}}},
        }
    if name == "gemini":
        return {
            "transport": "streamable-http+oauth",
            "settings.json": {"mcpServers": {"hermes-connectors": {"httpUrl": mcp_url}}},
        }
    if name == "workbuddy":
        return {
            "transport": "skills+streamable-http",
            "mcp.json": {
                "mcpServers": {
                    "hermes-connectors": {"type": "streamableHttp", "url": mcp_url}
                }
            },
            "skill": {"requires_connectors": ["github-mcp"]},
            "install": "product-managed",
        }
    if name == "trae":
        return {
            "transport": "connector+skills+streamable-http",
            ".mcp.json": {
                "mcpServers": {
                    "hermes-connectors": {"type": "http", "url": mcp_url}
                }
            },
            "connector.json": {
                "connectors": {
                    "github-mcp": {"type": "oauth", "auth_policy": "ON_INSTALL"}
                }
            },
            "skill": {"requires_connectors": ["github-mcp"]},
            "install": "product-managed",
        }
    if name in {"qoderwork", "1mcp"}:
        return {
            "transport": "secretless-stdio",
            "mcp.json": _stdio(launcher),
            "runtime_requirement": "Hermes run-scoped broker environment",
        }
    return {
        "transport": "protocol-compatible",
        "install": "product-managed",
        "protocol_probe": {"streamable_http": mcp_url, "sse": sse_url},
        "note": "DoubaoWork has no stable third-party config import surface; do not inject its private helper.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a secretless Hermes MCP client projection")
    parser.add_argument("client", choices=CLIENTS)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--launcher", default=sys.executable)
    args = parser.parse_args()
    print(json.dumps(build_projection(
        args.client,
        gateway_url=args.gateway_url,
        launcher=args.launcher,
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
