#!/usr/bin/env python3
"""Freeze non-secret stdio manifests from the two official catalog sources."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
from pathlib import Path, PurePosixPath
from typing import Any


_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DENIED_ENV = {
    "HOME", "PATH", "PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS", "SSLKEYLOGFILE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
}
_FIXES = {
    "trae solo cn:ergut.mcp-bigquery-server": (
        ["-y", "@ergut/mcp-bigquery-server", "--project-id", "@env:GOOGLE_CLOUD_PROJECT",
         "--location", "@env:GOOGLE_CLOUD_LOCATION", "--key-file",
         "/home/connector/credentials/google-service-account.json"],
        [("GOOGLE_CLOUD_PROJECT", False), ("GOOGLE_CLOUD_LOCATION", False)],
    ),
    "trae solo cn:byted-mcp-volcengine.bigquery": (
        ["-y", "@ergut/mcp-bigquery-server", "--project-id", "@env:GOOGLE_CLOUD_PROJECT",
         "--location", "@env:GOOGLE_CLOUD_LOCATION", "--key-file",
         "/home/connector/credentials/google-service-account.json"],
        [("GOOGLE_CLOUD_PROJECT", False), ("GOOGLE_CLOUD_LOCATION", False)],
    ),
    "trae solo cn:byted-mcp-volcengine.mastergo-magic-mcp": (
        ["-y", "@mastergo/magic-mcp", "--url=https://mastergo.com"], [("MG_MCP_TOKEN", True)]
    ),
    "trae solo cn:modelcontextprotocol.servers_redis": (
        ["-y", "@modelcontextprotocol/server-redis"], [("REDIS_URL", True)]
    ),
    "trae solo cn:modelcontextprotocol.servers_filesystem": (
        ["-y", "@modelcontextprotocol/server-filesystem", "/home/connector"], []
    ),
    "trae solo cn:byted-mcp-volcengine.filesystem": (
        ["-y", "@modelcontextprotocol/server-filesystem", "/home/connector"], []
    ),
    "trae solo cn:stripe.agent-toolkit_modelcontextprotocol": (
        ["-y", "@stripe/mcp", "--tools=all"], [("STRIPE_SECRET_KEY", True)]
    ),
    "trae solo cn:evalstate.mcp-hfspace": (
        ["-y", "@llmindset/mcp-hfspace", "--work-dir=/home/connector", "@env:HF_SPACE_ID"],
        [("HF_SPACE_ID", False), ("HF_TOKEN", True)],
    ),
    "trae solo cn:apify.actors-mcp-server": (
        ["-y", "@apify/actors-mcp-server", "--actors", "@env:APIFY_ACTORS"],
        [("APIFY_ACTORS", False), ("APIFY_TOKEN", True)],
    ),
    "trae solo cn:supabase-community.mcp-supabase_mcp-server-postgrest": (
        ["-y", "@supabase/mcp-server-postgrest", "--apiUrl", "@env:POSTGREST_API_URL",
         "--schema", "@env:POSTGREST_SCHEMA", "--apiKey", "@env:POSTGREST_API_KEY"],
        [("POSTGREST_API_URL", False), ("POSTGREST_SCHEMA", False), ("POSTGREST_API_KEY", True)],
    ),
    "trae solo cn:byted-mcp-volcengine.supabase-community": (
        ["-y", "@supabase/mcp-server-postgrest", "--apiUrl", "@env:POSTGREST_API_URL",
         "--schema", "@env:POSTGREST_SCHEMA", "--apiKey", "@env:POSTGREST_API_KEY"],
        [("POSTGREST_API_URL", False), ("POSTGREST_SCHEMA", False), ("POSTGREST_API_KEY", True)],
    ),
    "trae solo cn:kiliczsh.mcp-mongo-server": (
        ["-y", "mcp-mongo-server"], [("MCP_MONGODB_URI", True)]
    ),
    "trae solo cn:byted-mcp-volcengine.xmind": (
        ["-y", "@41px/mcp-xmind", "/home/connector"], []
    ),
    "trae solo cn:byted-mcp-volcengine.motherduck": (
        ["mcp-server-motherduck", "--db-path", "md:"], [("MOTHERDUCK_TOKEN", True)]
    ),
    "trae solo cn:byted-mcp-volcengine.pinecone": (
        ["--index-name", "@env:PINECONE_INDEX_NAME", "--api-key", "@env:PINECONE_API_KEY", "mcp-pinecone"],
        [("PINECONE_INDEX_NAME", False), ("PINECONE_API_KEY", True)],
    ),
    "trae solo cn:byted-mcp-volcengine.postgresql": (
        ["-y", "@modelcontextprotocol/server-postgres", "@env:POSTGRES_URL"],
        [("POSTGRES_URL", True)],
    ),
    "trae solo cn:byted-mcp-volcengine.gitee": (
        ["-y", "mcp-gitee"], [("GITEE_ACCESS_TOKEN", True)],
    ),
    "trae solo cn:oxylabs.oxylabs-mcp": (
        ["--from", "git+https://github.com/oxylabs/oxylabs-mcp", "oxylabs-mcp"],
        [("OXYLABS_USERNAME", False), ("OXYLABS_PASSWORD", True)],
    ),
    "trae solo cn:modelcontextprotocol.servers_git": (
        ["mcp-server-git", "--repository", "/home/connector"], [],
    ),
    "trae solo cn:cloudflare.mcp-server-cloudflare": (
        ["@cloudflare/mcp-server-cloudflare@0.2.0", "run", "@env:CLOUDFLARE_ACCOUNT_ID"],
        [("CLOUDFLARE_ACCOUNT_ID", False), ("CLOUDFLARE_API_TOKEN", True)],
    ),
    "trae solo cn:exa-labs.exa-mcp-server": (
        ["-y", "exa-mcp-server"], [("EXA_API_KEY", True)],
    ),
    "trae solo cn:oschina.mcp-gitee": (
        ["-y", "mcp-gitee"], [("GITEE_ACCESS_TOKEN", True)],
    ),
    "trae solo cn:byted-mcp-volcengine.spotify": (
        ["--from", "git+https://github.com/varunneal/spotify-mcp", "spotify-mcp"],
        [("SPOTIFY_CLIENT_ID", True), ("SPOTIFY_CLIENT_SECRET", True), ("SPOTIFY_REDIRECT_URI", False)],
    ),
    "trae solo cn:byted-mcp-volcengine.cognee-mcp": (
        ["--from", "git+https://github.com/topoteretes/cognee#subdirectory=cognee-mcp", "cognee-mcp"],
        [("LLM_API_KEY", True)],
    ),
    "trae solo cn:byted-mcp-volcengine.dataset_viewer": (
        ["--from", "git+https://github.com/privetin/dataset-viewer", "dataset-viewer"], [],
    ),
}
_FILE_CONFIGS = {
    "trae solo cn:ergut.mcp-bigquery-server": {
        "key": "GOOGLE_SERVICE_ACCOUNT_JSON", "type": "file", "required": True,
        "secret": True, "path": "credentials/google-service-account.json",
    },
    "trae solo cn:byted-mcp-volcengine.bigquery": {
        "key": "GOOGLE_SERVICE_ACCOUNT_JSON", "type": "file", "required": True,
        "secret": True, "path": "credentials/google-service-account.json",
    },
}
_COMMAND_FIXES = {
    "trae solo cn:byted-mcp-volcengine.gitee": "npx",
    "trae solo cn:oxylabs.oxylabs-mcp": "uvx",
    "trae solo cn:modelcontextprotocol.servers_git": "uvx",
    "trae solo cn:cloudflare.mcp-server-cloudflare": "npx",
    "trae solo cn:exa-labs.exa-mcp-server": "npx",
    "trae solo cn:oschina.mcp-gitee": "npx",
    "trae solo cn:byted-mcp-volcengine.spotify": "uvx",
    "trae solo cn:byted-mcp-volcengine.cognee-mcp": "uvx",
    "trae solo cn:byted-mcp-volcengine.dataset_viewer": "uvx",
}
_SOURCE_FIXES = {
    "trae solo cn:codegen-sh.codegen-sdk_codegen-mcp-server": [
        "--from", "git+https://github.com/codegen-sh/codegen-sdk", "python", "-m", "codegen.cli.mcp.server",
    ],
    "trae solo cn:byted-mcp-volcengine.minima": [
        "--from", "git+https://github.com/Minima-AI-Inc/minima#subdirectory=mcp-server", "minima",
    ],
    "trae solo cn:byted-mcp-volcengine.sec_agent": [
        "--from", "git+https://github.com/volcengine/mcp-server#subdirectory=server/mcp_server_sec_agent",
        "mcp-server-sec-agent",
    ],
    "trae solo cn:byted-mcp-volcengine.mcp_server_sec_agent": [
        "--from", "git+https://github.com/volcengine/mcp-server#subdirectory=server/mcp_server_sec_agent",
        "mcp-server-sec-agent",
    ],
    "trae solo cn:byted-mcp-volcengine.mcp_server_web_search": [
        "--from", "git+https://github.com/volcengine/mcp-server@3be81f20ff8566a462cd4a5608a0c371ec69419e#subdirectory=server/mcp_server_web_search",
        "mcp-server-web-search",
    ],
}


def _config(raw: dict[str, Any]) -> dict[str, Any] | None:
    key = str(raw.get("key") or "").strip()
    if not _ENV.fullmatch(key) or key.upper() in _DENIED_ENV or key.upper().startswith("NPM_CONFIG_"):
        return None
    kind = str(raw.get("type") or "env")
    result = {
        "key": key,
        "type": kind,
        "required": raw.get("required") is True,
        "secret": raw.get("isPassword") is True
        or any(part in key.casefold() for part in ("token", "secret", "password", "api_key", "apikey")),
        **({"url": str(raw["url"])} if str(raw.get("url") or "").startswith("https://") else {}),
    }
    if kind == "file":
        raw_path = str(raw.get("path") or "").strip()
        path = PurePosixPath(raw_path)
        if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in raw_path:
            return None
        result["path"] = path.as_posix()
    return result


def _direct(command: str, args: list[Any] | None) -> tuple[str, list[str], list[dict[str, Any]]]:
    tokens = shlex.split(str(command or ""))
    if not tokens:
        return "", [], []
    embedded: list[dict[str, Any]] = []
    if tokens[0] == "env":
        tokens.pop(0)
        while tokens and "=" in tokens[0]:
            key, _value = tokens.pop(0).split("=", 1)
            if _ENV.fullmatch(key):
                embedded.append({"key": key, "type": "env", "required": True, "secret": True})
    if not tokens:
        return "", [], embedded
    return tokens[0], [*tokens[1:], *(str(value) for value in (args or []))], embedded


def _manifest(
    *,
    row_key: str,
    command: str,
    args: list[Any] | None,
    configs: list[dict[str, Any]] | None,
    source_url: str,
    source_version: str,
    license_name: str,
    static_env: dict[str, Any] | None = None,
) -> dict[str, Any]:
    direct, argv, embedded = _direct(command, args)
    fields = [item for raw in (configs or []) if (item := _config(raw))]
    known = {item["key"] for item in fields}
    fields.extend(item for item in embedded if item["key"] not in known)
    safe_env = {
        str(key): str(value)
        for key, value in (static_env or {}).items()
        if _ENV.fullmatch(str(key))
        and str(key).upper() not in _DENIED_ENV
        and not str(key).upper().startswith("NPM_CONFIG_")
        and value not in {None, ""}
        and not str(value).startswith("${")
    }
    result: dict[str, Any] = {
        "row_key": row_key,
        "state": "direct" if direct else "repository_only",
        "source_url": source_url,
        "source_version": source_version,
        "license": license_name,
        "configs": fields,
        "static_env": safe_env,
    }
    if direct:
        result.update(command=direct, args=argv)
    result["source_fingerprint"] = hashlib.sha256(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def normalize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        fixed = _FIXES.get(row["row_key"])
        source_args = _SOURCE_FIXES.get(row["row_key"])
        file_config = _FILE_CONFIGS.get(row["row_key"])
        if not fixed and not source_args and not file_config:
            continue
        if fixed:
            args, fields = fixed
            row["command"] = _COMMAND_FIXES.get(row["row_key"], row.get("command"))
            row["args"] = args
            row["configs"] = [
                {"key": key, "type": "env", "required": True, "secret": secret}
                for key, secret in fields
            ]
            row["static_env"] = {}
        if file_config:
            row["configs"] = [
                item for item in row["configs"] if item.get("key") != file_config["key"]
            ] + [file_config]
        if source_args:
            row["command"], row["args"] = "uvx", source_args
        row.pop("package_resolution", None)
        row["source_fingerprint"] = hashlib.sha256(
            json.dumps({k: v for k, v in row.items() if k != "source_fingerprint"},
                       ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    return rows


def generate(trae_snapshot: Path, workbuddy_root: Path) -> list[dict[str, Any]]:
    payload = json.loads(trae_snapshot.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for source in payload["data"]:
        runs = (((source.get("commands") or {}).get("universal") or {}).get("run") or [])
        run = runs[0] if runs else {}
        rows.append(_manifest(
            row_key=f"trae solo cn:{str(source['id']).casefold()}",
            command=str(run.get("command") or ""),
            args=run.get("args"),
            configs=run.get("configs") or source.get("configs"),
            source_url=str(source.get("repository") or source.get("readme") or ""),
            source_version=str(source.get("version") or ""),
            license_name=str(source.get("license") or ""),
        ))

    for directory in sorted(workbuddy_root.iterdir()):
        path = directory / "mcp.json"
        if not path.is_file():
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        servers = document.get("mcpServers") or {}
        for spec in servers.values():
            if not isinstance(spec, dict) or not spec.get("command"):
                continue
            rows.append(_manifest(
                row_key=f"workbuddy:{directory.name.casefold()}",
                command=str(spec["command"]),
                args=spec.get("args"),
                configs=[
                    {"key": key, "type": "env", "required": True, "isPassword": True}
                    for key, value in (spec.get("env") or {}).items()
                    if value in {None, ""} or str(value).startswith("${")
                ],
                static_env={**(spec.get("staticEnv") or {}), **(spec.get("env") or {})},
                source_url=f"workbuddy-market://{directory.name}/mcp.json",
                source_version="local-official-snapshot",
                license_name="vendor-catalog",
            ))

    selected = {
        row["row_key"]: row
        for row in rows
        if row["row_key"].startswith("trae solo cn:")
        or row["row_key"] in {
            "workbuddy:edgeone-pages", "workbuddy:cloudbase", "workbuddy:weisheng-scrm",
            "workbuddy:mastergo-vibe-mcp", "workbuddy:ai-hive", "workbuddy:ioa",
            "workbuddy:dcs-cloud",
        }
    }
    if len(selected) != 482:
        raise ValueError(f"expected 482 stdio manifests, got {len(selected)}")
    return normalize([selected[key] for key in sorted(selected)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trae-snapshot", type=Path)
    parser.add_argument("--workbuddy-root", type=Path)
    parser.add_argument("--normalize-existing", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.normalize_existing:
        rows = normalize([
            json.loads(line) for line in args.normalize_existing.read_text(encoding="utf-8").splitlines()
            if line
        ])
    elif args.trae_snapshot and args.workbuddy_root:
        rows = generate(args.trae_snapshot, args.workbuddy_root)
    else:
        parser.error("--normalize-existing or both source snapshots are required")
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
