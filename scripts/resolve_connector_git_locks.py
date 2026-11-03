#!/usr/bin/env python3
"""Freeze pinned Git Python MCPs into hashed Linux dependency/source locks."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import re
import shutil
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path
from typing import Any


PYTHON_RUNTIME = {
    "kind": "system",
    "version": "3.12",
    "executable": "/usr/bin/python3.12",
}
_RDS_MSSQL = (
    "https://github.com/volcengine/mcp-server",
    "server/mcp_server_rds_mssql/src",
)
_RDS_MSSQL_BROKEN = '''dependencies = [
    "mcp[cli],"
    "mcp>=1.12.0,"
    "volcengine-python-sdk>=4.0.34,"
]'''
_RDS_MSSQL_FIXED = '''dependencies = [
    "mcp[cli]>=1.12.0",
    "volcengine-python-sdk>=4.0.34",
]'''


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-connector-resolver/1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(512 * 1024 * 1024 + 1)
    if len(data) > 512 * 1024 * 1024:
        raise ValueError("source archive is too large")
    return data


def _archive_url(repository: str, commit: str) -> str:
    owner_repo = repository.removeprefix("https://github.com/")
    return f"https://codeload.github.com/{owner_repo}/tar.gz/{commit}"


def _extract(data: bytes, destination: Path) -> Path:
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        archive.extractall(destination, filter="data")
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise ValueError("source archive root is ambiguous")
    return roots[0]


async def _compile(uv: str, row: dict[str, Any], source: Path, semaphore: asyncio.Semaphore) -> dict[str, Any]:
    base = {
        "pinned_source": row["pinned_source"],
        "repository": row["repository"],
        "commit": row["commit"],
        "subdirectory": row["subdirectory"],
        "python_runtime": PYTHON_RUNTIME,
    }
    project = source / str(row["subdirectory"] or "")
    pyproject = project / "pyproject.toml"
    if not pyproject.is_file():
        return {**base, "state": "unavailable", "reason": "pyproject_missing"}
    source_patch = None
    original = pyproject.read_text(encoding="utf-8")
    if (row["repository"], row["subdirectory"]) == _RDS_MSSQL:
        before_sha256 = hashlib.sha256(original.encode()).hexdigest()
        if before_sha256 != "d8b0f79c0ec2783342dc957f34ae1745b4db332e0e5fd19eefdf6e72d2aaa0f4":
            return {**base, "state": "unavailable", "reason": "pyproject_repair_source_drift"}
        repaired = original.replace(_RDS_MSSQL_BROKEN, _RDS_MSSQL_FIXED)
        if repaired == original:
            return {**base, "state": "unavailable", "reason": "pyproject_repair_not_applicable"}
        source_patch = {
            "path": "pyproject.toml",
            "before_sha256": before_sha256,
            "content": repaired,
            "content_sha256": hashlib.sha256(repaired.encode()).hexdigest(),
        }
        pyproject.write_text(repaired, encoding="utf-8")
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return {**base, "state": "unavailable", "reason": "pyproject_invalid"}
    build_requires = [str(value) for value in (document.get("build-system") or {}).get("requires") or []]
    dependencies = list((document.get("project") or {}).get("dependencies") or [])
    direct_names = {
        match.group(0).casefold().replace("_", "-")
        for value in [*dependencies, *build_requires]
        if (match := re.match(r"[A-Za-z0-9_.-]+", str(value)))
    }
    official_lock = project / "uv.lock"
    constraints = []
    official_lock_sha256 = ""
    if official_lock.is_file():
        official_lock_sha256 = hashlib.sha256(official_lock.read_bytes()).hexdigest()
        locked = tomllib.loads(official_lock.read_text(encoding="utf-8"))
        project_name = str((document.get("project") or {}).get("name") or "").casefold().replace("_", "-")
        constraints = [
            f"{item['name']}=={item['version']}"
            for item in locked.get("package") or []
            if isinstance(item, dict)
            and item.get("name") and item.get("version")
            and isinstance(item.get("source"), dict)
            and item["source"].get("registry")
            and str(item["name"]).casefold().replace("_", "-") != project_name
            and str(item["name"]).casefold().replace("_", "-") in direct_names
        ]
        if row["repository"] == "https://github.com/YanxingLiu/dify-mcp-server":
            constraints = ["mcp==1.1.2"]
    async with semaphore:
        with tempfile.TemporaryDirectory(prefix="connector-git-lock-") as raw:
            root = Path(raw)
            input_path, output_path = root / "in.txt", root / "out.txt"
            constraints_path = root / "constraints.txt"
            constraints_path.write_text("\n".join(constraints) + "\n", encoding="utf-8")
            stderr = b""
            used_official_lock = False
            source_build = False
            for allow_source in (False, True):
                input_path.write_text(
                    "\n".join([
                        *(str(value) for value in dependencies), *build_requires,
                        *(["setuptools>=75", "wheel>=0.45"] if allow_source else []),
                    ]) + "\n",
                    encoding="utf-8",
                )
                for selected in ([constraints, []] if constraints else [[]]):
                    output_path.unlink(missing_ok=True)
                    process = await asyncio.create_subprocess_exec(
                        uv, "pip", "compile", str(input_path), "--output-file", str(output_path),
                        "--generate-hashes", *(() if allow_source else ("--only-binary", ":all:")),
                        "--python-version", "3.12", "--python-platform", "x86_64-unknown-linux-gnu",
                        "--exclude-newer", "2026-09-01T00:00:00Z", "--no-header", "--no-annotate", "--quiet",
                        *(["--constraint", str(constraints_path)] if selected else []),
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                    )
                    _stdout, stderr = await process.communicate()
                    if not process.returncode and output_path.is_file():
                        used_official_lock = bool(selected)
                        source_build = allow_source
                        break
                if not process.returncode and output_path.is_file():
                    break
            if process.returncode or not output_path.is_file():
                return {**base, "state": "unavailable", "reason": stderr.decode(errors="replace")[-1000:]}
            lines = [
                line for line in output_path.read_text(encoding="utf-8").splitlines()
                if " @ file://" not in line
            ]
            requirements = "\n".join(lines).strip() + "\n"
            if not requirements or " @ file://" in requirements:
                return {**base, "state": "unavailable", "reason": "dependency_lock_incomplete"}
            return {
                **base,
                "state": "resolved",
                "source_build": source_build,
                **({"source_patch": source_patch} if source_patch else {}),
                "build_requires": build_requires,
                "official_lock_sha256": official_lock_sha256 if used_official_lock else "",
                "requirements": requirements,
                "requirements_sha256": hashlib.sha256(requirements.encode()).hexdigest(),
            }


async def run(source_path: Path, output: Path) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to resolve Git locks")
    rows = [json.loads(line) for line in source_path.read_text(encoding="utf-8").splitlines() if line]
    unique = {row["pinned_source"]: row for row in rows if row.get("state") == "git_resolved"}
    existing = {}
    if output.is_file():
        existing = {
            row["pinned_source"]: row
            for line in output.read_text(encoding="utf-8").splitlines()
            if line and (row := json.loads(line)) and row["pinned_source"] in unique
            and row.get("state") == "resolved"
        }
    pending = {key: row for key, row in unique.items() if key not in existing}
    repositories = {(row["repository"], row["commit"]) for row in pending.values()}
    with tempfile.TemporaryDirectory(prefix="connector-git-sources-") as raw:
        root = Path(raw)
        sources: dict[tuple[str, str], tuple[Path, str, str]] = {}
        for index, (repository, commit) in enumerate(sorted(repositories)):
            url = _archive_url(repository, commit)
            data = await asyncio.to_thread(_download, url)
            source = _extract(data, root / str(index))
            sources[(repository, commit)] = (source, url, hashlib.sha256(data).hexdigest())
        semaphore = asyncio.Semaphore(4)
        results = await asyncio.gather(*(
            _compile(uv, row, sources[(row["repository"], row["commit"])][0], semaphore)
            for row in pending.values()
        ))
        for result in results:
            _source, url, digest = sources[(result["repository"], result["commit"])]
            result["source_archive_url"] = url
            result["source_archive_sha256"] = digest
    results = [*existing.values(), *results]
    results.sort(key=lambda item: (item["repository"], item["subdirectory"]))
    output.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in results), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(
        "hermes_multitenancy/connector_catalog_data/stdio_python_resolutions.jsonl"
    ))
    parser.add_argument("--output", type=Path, default=Path(
        "hermes_multitenancy/connector_catalog_data/stdio_python_git_locks.jsonl"
    ))
    args = parser.parse_args()
    asyncio.run(run(args.source, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
