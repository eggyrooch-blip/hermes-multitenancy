#!/usr/bin/env python3
"""Resolve and freeze npm identities for official WorkBuddy CLI connectors."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

try:
    from scripts.resolve_connector_npm_manifests import resolve
except ModuleNotFoundError:  # direct script execution
    from resolve_connector_npm_manifests import resolve


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifests", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifests = [json.loads(line) for line in args.manifests.read_text(encoding="utf-8").splitlines() if line]
    rows = asyncio.run(resolve(manifests))
    if len(rows) != 19 or any(row.get("state") != "resolved" for row in rows):
        raise ValueError("expected 19 resolved WorkBuddy npm CLI packages")
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in sorted(rows, key=lambda x: x["row_key"])),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
