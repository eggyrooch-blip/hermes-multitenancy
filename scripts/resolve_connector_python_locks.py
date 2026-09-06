#!/usr/bin/env python3
"""Freeze Linux/Python 3.11 wheel-only dependency locks for PyPI catalog MCPs."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import tempfile
from pathlib import Path


async def resolve(uv: str, row: dict, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        with tempfile.TemporaryDirectory(prefix="connector-lock-") as raw:
            root = Path(raw)
            source, output = root / "in.txt", root / "out.txt"
            stderr = b""
            python_version = ""
            source_build = False
            for allow_source in (False, True):
                source.write_text(
                    f"{row['package']}=={row['version']}\n"
                    + ("setuptools>=75\nwheel>=0.45\n" if allow_source else ""),
                    encoding="utf-8",
                )
                for candidate in ("3.11", "3.12"):
                    output.unlink(missing_ok=True)
                    process = await asyncio.create_subprocess_exec(
                        uv, "pip", "compile", str(source), "--output-file", str(output),
                        "--generate-hashes",
                        *(() if allow_source else ("--only-binary", ":all:")),
                        "--python-version", candidate,
                        "--python-platform", "x86_64-unknown-linux-gnu",
                        "--exclude-newer", "2026-09-01T00:00:00Z", "--quiet",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _stdout, stderr = await process.communicate()
                    if not process.returncode and output.is_file():
                        python_version = candidate
                        source_build = allow_source
                        break
                if python_version:
                    break
            base = {
                "resolution_fingerprint": row["resolution_fingerprint"],
                "package": row["package"],
                "version": row["version"],
                "python_version": python_version or "3.12",
                "python_platform": "x86_64-unknown-linux-gnu",
            }
            if process.returncode or not output.is_file():
                return {**base, "state": "unavailable", "reason": stderr.decode(errors="replace")[-500:]}
            lines = [line for line in output.read_text(encoding="utf-8").splitlines()
                     if line.strip() and not line.lstrip().startswith("#")]
            lock = "\n".join(lines) + "\n"
            return {
                **base,
                "state": "resolved",
                "source_build": source_build,
                "requirements": lock,
                "requirements_sha256": hashlib.sha256(lock.encode()).hexdigest(),
            }


async def run(source: Path, output: Path) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to resolve Python locks")
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line]
    unique = {
        row["resolution_fingerprint"]: row for row in rows if row.get("state") == "pypi_resolved"
    }
    existing = {}
    if output.is_file():
        existing = {
            row["resolution_fingerprint"]: row
            for line in output.read_text(encoding="utf-8").splitlines()
            if line and (row := json.loads(line)) and row["resolution_fingerprint"] in unique
            and row.get("state") == "resolved"
        }
    semaphore = asyncio.Semaphore(4)
    results = list(existing.values())
    results.extend(await asyncio.gather(*(
        resolve(uv, row, semaphore) for key, row in unique.items() if key not in existing
    )))
    results.sort(key=lambda item: (item["package"], item["version"]))
    output.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in results), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(
        "hermes_multitenancy/connector_catalog_data/stdio_python_resolutions.jsonl"
    ))
    parser.add_argument("--output", type=Path, default=Path(
        "hermes_multitenancy/connector_catalog_data/stdio_python_locks.jsonl"
    ))
    args = parser.parse_args()
    asyncio.run(run(args.source, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
