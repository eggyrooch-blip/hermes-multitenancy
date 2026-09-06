#!/usr/bin/env python3
"""Resolve catalog npx specs to immutable npm versions and integrity hashes."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import re
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx


_PACKAGE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
_OVERRIDES = {
    "trae solo cn:delorenj.mcp-server-trello": "@delorenj/mcp-server-trello@1.8.1",
    "trae solo cn:wonderwhy-er.claudecomputercommander": "@wonderwhy-er/desktop-commander@0.2.47",
}
_GIT_OVERRIDES = {
    "trae solo cn:byted-mcp-volcengine.fireproof": {
        "repository": "https://github.com/fireproof-storage/mcp-database-server",
        "entry": "build/index.js",
    },
    "trae solo cn:byted-mcp-volcengine.360_mcp": {
        "repository": "https://github.com/Qihoo360/ecs_mcp_server",
        "entry": "build/index.js",
        "runtime_args": ["--stdio"],
        "rebuild": ["better-sqlite3"],
    },
    "trae solo cn:bsmi021.mcp-file-context-server": {
        "repository": "https://github.com/bsmi021/mcp-file-context-server",
        "entry": "dist/index.js",
    },
    "trae solo cn:tiovikram.linear-mcp": {
        "repository": "https://github.com/tiovikram/linear-mcp",
        "entry": "build/index.js",
    },
}


def _spec(args: list[str]) -> tuple[str, list[str]]:
    index = 0
    while index < len(args):
        value = args[index]
        if value in {"-y", "--yes", "-f", "--force"}:
            index += 1
        elif value == "--registry":
            index += 2
        elif value.startswith("--registry="):
            index += 1
        else:
            return value, args[index + 1:]
    raise ValueError("npx package spec is missing")


def _split_spec(spec: str) -> tuple[str, str]:
    if spec.startswith("@"):
        separator = spec.find("@", spec.find("/") + 1)
        name, requested = (spec[:separator], spec[separator + 1:]) if separator > 0 else (spec, "latest")
    elif "@" in spec:
        name, requested = spec.rsplit("@", 1)
    else:
        name, requested = spec, "latest"
    if not _PACKAGE.fullmatch(name) or not requested:
        raise ValueError("invalid npm package spec")
    return name, requested


def _license(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("type")
    return str(value or "").strip()


def _bin(value: Any, package: str) -> dict[str, str]:
    if isinstance(value, str) and value:
        return {package.rsplit("/", 1)[-1]: value}
    if isinstance(value, dict):
        return {str(key): str(path) for key, path in value.items() if str(key) and str(path)}
    return {}


def _git_resolution(manifest: dict[str, Any], original: str, runtime_args: list[str]) -> dict[str, Any]:
    override = _GIT_OVERRIDES[manifest["row_key"]]
    repository = str(override["repository"])
    output = subprocess.check_output(["git", "ls-remote", repository, "HEAD"], text=True, timeout=30)
    commit = output.split()[0] if output.strip() else ""
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("npm Git source HEAD is unavailable")
    owner_repo = repository.removeprefix("https://github.com/")
    source_url = f"https://codeload.github.com/{owner_repo}/tar.gz/{commit}"
    request = urllib.request.Request(source_url, headers={"User-Agent": "hermes-connector-resolver/1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        archive = response.read(512 * 1024 * 1024 + 1)
    if len(archive) > 512 * 1024 * 1024:
        raise ValueError("npm Git source archive is too large")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        members = {Path(member.name).parts[1:]: member for member in source.getmembers() if len(Path(member.name).parts) > 1}
        package_member = members.get(("package.json",))
        lock_member = members.get(("package-lock.json",))
        if not package_member or not lock_member:
            raise ValueError("npm Git source package metadata is incomplete")
        package_bytes = source.extractfile(package_member).read()
        lock_bytes = source.extractfile(lock_member).read()
    package = json.loads(package_bytes)
    lock = json.loads(lock_bytes)
    root = (lock.get("packages") or {}).get("") or {}
    if (
        lock.get("lockfileVersion") != 3
        or root.get("dependencies", {}) != package.get("dependencies", {})
        or root.get("devDependencies", {}) != package.get("devDependencies", {})
    ):
        raise ValueError("npm Git source lock does not match package metadata")
    for path, dependency in (lock.get("packages") or {}).items():
        if not path or dependency.get("link"):
            continue
        resolved = str(dependency.get("resolved") or "")
        if resolved.startswith("https://registry.npmmirror.com/"):
            dependency["resolved"] = "https://registry.npmjs.org/" + resolved.split("/", 3)[-1]
        if (
            not str(dependency.get("resolved") or "").startswith("https://registry.npmjs.org/")
            or not str(dependency.get("integrity") or "").startswith("sha512-")
        ):
            raise ValueError("npm Git source lock contains an unpinned dependency")
    serialized_lock = json.dumps(lock, sort_keys=True, separators=(",", ":")) + "\n"
    result = {
        "row_key": manifest["row_key"],
        "requested_package": original,
        "runtime_args": list(override.get("runtime_args") or runtime_args),
        "state": "git_resolved",
        "repository": repository,
        "commit": commit,
        "source_archive_url": source_url,
        "source_archive_sha256": hashlib.sha256(archive).hexdigest(),
        "package": str(package.get("name") or ""),
        "version": str(package.get("version") or ""),
        "entry": str(override["entry"]),
        "rebuild": list(override.get("rebuild") or []),
        "package_json_sha256": hashlib.sha256(package_bytes).hexdigest(),
        "dependency_lock": {
            "state": "resolved",
            "package_lock": serialized_lock,
            "package_lock_sha256": hashlib.sha256(serialized_lock.encode()).hexdigest(),
            "source_package_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        },
    }
    if not result["package"] or not result["version"] or ".." in Path(result["entry"]).parts:
        raise ValueError("npm Git source package identity is incomplete")
    result["resolution_fingerprint"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


async def resolve(manifests: list[dict[str, Any]], concurrency: int = 20) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        async def one(manifest: dict[str, Any]) -> dict[str, Any]:
            original, runtime_args = _spec(list(manifest["args"]))
            if manifest["row_key"] in _GIT_OVERRIDES:
                async with semaphore:
                    return await asyncio.to_thread(_git_resolution, manifest, original, runtime_args)
            selected = _OVERRIDES.get(manifest["row_key"], original)
            package, requested = _split_spec(selected)
            base = {
                "row_key": manifest["row_key"],
                "requested_package": original,
                "runtime_args": runtime_args,
            }
            async with semaphore:
                response = await client.get(f"https://registry.npmjs.org/{quote(package, safe='')}")
            if response.status_code != 200:
                return {**base, "state": "package_unavailable", "reason": f"npm_http_{response.status_code}"}
            document = response.json()
            version = str((document.get("dist-tags") or {}).get(requested) or requested)
            release = (document.get("versions") or {}).get(version)
            if not isinstance(release, dict):
                return {**base, "state": "package_unavailable", "reason": "npm_version_unavailable"}
            dist = release.get("dist") or {}
            integrity = str(dist.get("integrity") or "")
            tarball = str(dist.get("tarball") or "")
            executable = _bin(release.get("bin"), package)
            parsed = urlparse(tarball)
            if (
                not integrity.startswith("sha512-")
                or parsed.scheme != "https"
                or parsed.hostname != "registry.npmjs.org"
                or not executable
            ):
                return {**base, "state": "package_unavailable", "reason": "npm_metadata_incomplete"}
            result = {
                **base,
                "state": "resolved",
                "package": package,
                "version": version,
                "integrity": integrity,
                "tarball": tarball,
                "license": _license(release.get("license")),
                "bin": executable,
                "dependencies": {
                    str(key): str(value) for key, value in (release.get("dependencies") or {}).items()
                },
                "git_head": str(release.get("gitHead") or ""),
            }
            result["resolution_fingerprint"] = hashlib.sha256(
                json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            return result

        return await asyncio.gather(*(one(row) for row in manifests if row.get("command") == "npx"))


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
    if len(rows) != 171:
        raise ValueError(f"expected 171 npx manifests, got {len(rows)}")
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in sorted(rows, key=lambda x: x["row_key"])),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
