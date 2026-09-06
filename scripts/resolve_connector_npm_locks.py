#!/usr/bin/env python3
"""Freeze complete npm dependency locks for resolved catalog MCP packages."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path


_OFFICIAL_LOCKS = {
    "graphlit-mcp-server": "graphlit/graphlit-mcp-server",
}


def _normalize_official_lock(row: dict, lock: dict) -> dict:
    root = (lock.get("packages") or {}).get("") or {}
    if lock.get("lockfileVersion") != 3 or root.get("dependencies") != row.get("dependencies"):
        raise ValueError("official npm lock does not match published dependencies")
    for path, package in (lock.get("packages") or {}).items():
        if not path or package.get("link"):
            continue
        resolved, integrity = str(package.get("resolved") or ""), str(package.get("integrity") or "")
        if not resolved.startswith("https://registry.npmjs.org/") or not integrity.startswith("sha512-"):
            raise ValueError("official npm lock contains an unpinned dependency")
    lock.pop("name", None)
    lock.pop("version", None)
    root.pop("name", None)
    root.pop("version", None)
    return lock


def _official_lock(row: dict) -> dict | None:
    repository = _OFFICIAL_LOCKS.get(row["package"])
    commit = str(row.get("git_head") or "")
    if not repository or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        return None
    request = urllib.request.Request(
        f"https://raw.githubusercontent.com/{repository}/{commit}/package-lock.json",
        headers={"User-Agent": "hermes-connector-locker/1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return _normalize_official_lock(row, json.loads(response.read(8 * 1024 * 1024)))


async def resolve(npm: str, row: dict, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        if row["package"] in _OFFICIAL_LOCKS:
            try:
                lock = await asyncio.to_thread(_official_lock, row)
            except Exception as exc:
                return {"package": row["package"], "version": row["version"], "state": "unavailable", "reason": type(exc).__name__}
            if lock is not None:
                serialized = json.dumps(lock, sort_keys=True, separators=(",", ":")) + "\n"
                return {
                    "package": row["package"], "version": row["version"], "state": "resolved",
                    "package_lock": serialized,
                    "package_lock_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
                }
        with tempfile.TemporaryDirectory(prefix="connector-npm-lock-") as raw:
            root = Path(raw)
            (root / "package.json").write_text('{"private":true}\n', encoding="utf-8")
            process = await asyncio.create_subprocess_exec(
                npm, "install", "--package-lock-only", "--ignore-scripts", "--omit=dev",
                "--no-audit", "--no-fund", "--save-exact", "--legacy-peer-deps",
                "--registry=https://registry.npmjs.org",
                f"{row['package']}@{row['version']}",
                cwd=root,
                env={
                    "HOME": str(root), "LANG": "C.UTF-8",
                    "PATH": f"{Path(npm).parent}:/usr/bin:/bin", "TZ": "UTC",
                },
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
            except TimeoutError:
                process.kill()
                await process.communicate()
                stderr = b"npm dependency resolution timed out"
            base = {"package": row["package"], "version": row["version"]}
            lock_path = root / "package-lock.json"
            if process.returncode or not lock_path.is_file():
                return {**base, "state": "unavailable", "reason": stderr.decode(errors="replace")[-500:]}
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            lock.pop("name", None)
            lock.pop("version", None)
            root_package = lock.get("packages", {}).get("") or {}
            root_package.pop("name", None)
            root_package.pop("version", None)
            serialized = json.dumps(lock, sort_keys=True, separators=(",", ":")) + "\n"
            return {
                **base,
                "state": "resolved",
                "package_lock": serialized,
                "package_lock_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
            }


async def run(source: Path, output: Path) -> None:
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm is required to resolve dependency locks")
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line]
    unique = {
        (row["package"], row["version"]): row for row in rows if row.get("state") == "resolved"
    }
    existing = {}
    if output.is_file():
        existing = {
            (row["package"], row["version"]): row
            for line in output.read_text(encoding="utf-8").splitlines()
            if line and (row := json.loads(line))
            and (row["package"], row["version"]) in unique
            and row.get("state") == "resolved"
        }
    semaphore = asyncio.Semaphore(min(8, max(1, os.cpu_count() or 1)))
    results = list(existing.values())
    results.extend(await asyncio.gather(*(
        resolve(npm, row, semaphore) for key, row in unique.items() if key not in existing
    )))
    results.sort(key=lambda item: (item["package"], item["version"]))
    output.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in results), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(
        "hermes_multitenancy/connector_catalog_data/stdio_npm_resolutions.jsonl"
    ))
    parser.add_argument("--output", type=Path, default=Path(
        "hermes_multitenancy/connector_catalog_data/stdio_npm_locks.jsonl"
    ))
    args = parser.parse_args()
    asyncio.run(run(args.source, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
