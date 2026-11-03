#!/usr/bin/env python3
"""Pin catalog uvx sources to Git commits or hashed PyPI releases."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx


_GITHUB = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_DIRECT_PACKAGES = {
    "trae solo cn:modelcontextprotocol.servers_sentry": ("mcp-server-sentry", "mcp-server-sentry", []),
    "trae solo cn:designcomputer.mysql_mcp_server": ("mysql-mcp-server", "mysql_mcp_server", []),
    "trae solo cn:ramxx.mcp-tavily": ("mcp-tavily", "mcp-tavily", []),
    "trae solo cn:ai-1st.deepview-mcp": ("deepview-mcp", "deepview-mcp", []),
    "trae solo cn:byted-mcp-volcengine.mcp-clickhouse": ("mcp-clickhouse", "mcp-clickhouse", []),
}
_GIT_OVERRIDES = {
    "trae solo cn:byted-mcp-volcengine.mcp_server_rds_mssql": {
        "subdirectory": "server/mcp_server_rds_mssql/src",
    },
    "trae solo cn:byted-mcp-volcengine.rds_mssql": {
        "subdirectory": "server/mcp_server_rds_mssql/src",
    },
    "trae solo cn:byted-mcp-volcengine.mcp_server_traffic_route": {
        "subdirectory": "server/mcp_server_traffic_route/python",
        "module": "vcloud.traffic_route.server",
    },
    "trae solo cn:byted-mcp-volcengine.traffic_route": {
        "subdirectory": "server/mcp_server_traffic_route/python",
        "module": "vcloud.traffic_route.server",
    },
}


def _git_parts(source: str) -> tuple[str, str, str]:
    raw = source.removeprefix("git+")
    raw = raw.replace("https://${source-repo}", "https://github.com/volcengine/mcp-server")
    base, _, fragment = raw.partition("#")
    revision = ""
    if ".git@" in base:
        base, revision = base.split(".git@", 1)
        base += ".git"
    elif "@" in base.rsplit("/", 1)[-1]:
        base, revision = base.rsplit("@", 1)
    repository = base.removesuffix(".git")
    if not _GITHUB.fullmatch(repository):
        raise ValueError(f"unsupported git repository: {repository}")
    subdirectory = (parse_qs(fragment).get("subdirectory") or [""])[0]
    if subdirectory and (subdirectory.startswith(("/", "\\")) or ".." in Path(subdirectory).parts):
        raise ValueError("unsafe git subdirectory")
    if revision and (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", revision)
        or ".." in revision or "@{" in revision
    ):
        raise ValueError("unsafe git source revision")
    return repository, subdirectory, revision


def _commit(repository: str, revision: str = "HEAD") -> str:
    output = subprocess.check_output(
        ["git", "ls-remote", repository, revision], text=True, timeout=30
    ).strip()
    commit = output.split()[0] if output else ""
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError(f"git HEAD unavailable: {repository}")
    return commit


def _candidate(value: str) -> str:
    value = str(value).removesuffix("@latest")
    if not value or value.startswith("-") or value.startswith(("{", "<", "$", "/")):
        return ""
    if "://" in value or "=" in value or "/" in value:
        return ""
    return value


def _git_invocation(args: list[str]) -> tuple[str, list[str]]:
    tail = args[2:]
    index = 0
    while index < len(tail) and tail[index].startswith("-"):
        index += 2 if tail[index] in {"--python", "--python-platform"} else 1
    if index >= len(tail):
        raise ValueError("git uvx executable is missing")
    return tail[index], tail[index + 1:]


async def resolve(manifests: list[dict[str, Any]], concurrency: int = 16) -> list[dict[str, Any]]:
    uvx = [row for row in manifests if row.get("command") == "uvx" or row["row_key"] in _DIRECT_PACKAGES]
    git_rows = [row for row in uvx if row.get("args", [])[:1] == ["--from"] and row["args"][1].startswith("git+")]
    sources = {_git_parts(row["args"][1])[:3:2] for row in git_rows}
    commits = {
        (repository, revision): revision if re.fullmatch(r"[a-f0-9]{40}", revision) else _commit(repository, revision or "HEAD")
        for repository, revision in sources
    }
    results: list[dict[str, Any]] = []
    for row in git_rows:
        repository, subdirectory, revision = _git_parts(row["args"][1])
        override = _GIT_OVERRIDES.get(row["row_key"]) or {}
        subdirectory = str(override.get("subdirectory") or subdirectory)
        commit = commits[(repository, revision)]
        executable, runtime_args = _git_invocation(row["args"])
        result = {
            "row_key": row["row_key"],
            "state": "git_resolved",
            "repository": repository,
            "commit": commit,
            "subdirectory": subdirectory,
            "executable": executable,
            "runtime_args": runtime_args,
            "pinned_source": f"git+{repository}@{commit}" + (f"#subdirectory={subdirectory}" if subdirectory else ""),
            **({"module": override["module"]} if override.get("module") else {}),
        }
        result["resolution_fingerprint"] = hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        results.append(result)

    semaphore = asyncio.Semaphore(concurrency)
    cache: dict[str, dict[str, Any] | None] = {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        async def metadata(package: str) -> dict[str, Any] | None:
            if package in cache:
                return cache[package]
            async with semaphore:
                response = await client.get(f"https://pypi.org/pypi/{package}/json")
            cache[package] = response.json() if response.status_code == 200 else None
            return cache[package]

        async def one(row: dict[str, Any]) -> dict[str, Any]:
            if row["row_key"] in _DIRECT_PACKAGES:
                package, executable, runtime_args = _DIRECT_PACKAGES[row["row_key"]]
                args = [package]
            else:
                executable = ""
                runtime_args = []
                args = list(row["args"])
            if args[:1] == ["--from"]:
                candidates = [_candidate(args[1])]
            else:
                candidates = [_candidate(value) for value in args]
            package = ""
            document = None
            for value in candidates:
                if value and (found := await metadata(value)) is not None:
                    package, document = value, found
                    break
            if not package or not document:
                raise ValueError(f"PyPI package unavailable: {row['row_key']}")
            version = str((document.get("info") or {}).get("version") or "")
            files = (document.get("releases") or {}).get(version) or []
            artifacts = [
                {"filename": str(item["filename"]), "url": str(item["url"]), "sha256": str((item.get("digests") or {}).get("sha256") or "")}
                for item in files
                if str(item.get("url") or "").startswith("https://files.pythonhosted.org/")
                and str((item.get("digests") or {}).get("sha256") or "")
            ]
            if not version or not artifacts:
                raise ValueError(f"PyPI release metadata incomplete: {row['row_key']}")
            if row["row_key"] in _DIRECT_PACKAGES:
                pass
            elif args[:1] == ["--from"]:
                executable = args[-1]
                runtime_args = args[2:-1]
            else:
                chosen = next(index for index, value in enumerate(args) if _candidate(value) == package)
                executable = package
                runtime_args = args[:chosen] + args[chosen + 1:]
            result = {
                "row_key": row["row_key"],
                "state": "pypi_resolved",
                "package": package,
                "version": version,
                "executable": executable,
                "runtime_args": runtime_args,
                "license": str((document.get("info") or {}).get("license") or ""),
                "artifacts": artifacts,
                **({"normalized_command": "uvx"} if row["row_key"] in _DIRECT_PACKAGES else {}),
            }
            result["resolution_fingerprint"] = hashlib.sha256(
                json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            return result

        results.extend(await asyncio.gather(*(one(row) for row in uvx if row not in git_rows)))
    return sorted(results, key=lambda row: row["row_key"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifests", type=Path, required=True)
    parser.add_argument("--recoveries", type=Path, default=Path(
        "hermes_multitenancy/connector_catalog_data/stdio_remote_recoveries.jsonl"
    ))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifests = [json.loads(line) for line in args.manifests.read_text(encoding="utf-8").splitlines() if line]
    recovered = [
        row["runtime_manifest"] for line in args.recoveries.read_text(encoding="utf-8").splitlines()
        if line and (row := json.loads(line)).get("state") == "stdio_resolved"
    ]
    manifests = [*manifests, *recovered]
    rows = asyncio.run(resolve(manifests))
    if len(rows) != 190:
        raise ValueError(f"expected 190 Python manifests, got {len(rows)}")
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
