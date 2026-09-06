"""Run the credential-free remote probe for every remote catalog row."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from .connector_catalog_conformance import read_catalog
from .connector_remote_probe import probe_remote_endpoint


REMOTE_TRANSPORTS = {"http", "streamablehttp", "streamable-http", "sse"}
_STABLE_RESULT_FIELDS = ("verdict", "reason_code", "tool_count")


def diff_results(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old = {item["row_key"]: item for item in previous}
    return [
        {
            "row_key": item["row_key"],
            "previous": {key: old[item["row_key"]].get(key) for key in _STABLE_RESULT_FIELDS},
            "current": {key: item.get(key) for key in _STABLE_RESULT_FIELDS},
        }
        for item in current
        if item["row_key"] in old
        and any(old[item["row_key"]].get(key) != item.get(key) for key in _STABLE_RESULT_FIELDS)
    ]


def conservative_results(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old = {item["row_key"]: item for item in previous}
    return [
        old[item["row_key"]]
        if old.get(item["row_key"], {}).get("reason_code") == "unsafe_endpoint"
        else item
        for item in current
    ]


async def sweep_remote_catalog(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
    concurrency: int = 12,
    probe: Callable[[str], Awaitable[dict[str, Any]]] = probe_remote_endpoint,
) -> list[dict[str, Any]]:
    rows = [
        row for row in read_catalog(path, expected_sha256=expected_sha256)
        if str(row.get("transport") or "").casefold() in REMOTE_TRANSPORTS
    ]
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(row: dict[str, Any]) -> dict[str, Any]:
        product = str(row["product"]).strip()
        catalog_id = str(row["catalog_id"]).strip()
        endpoint = str(row.get("endpoint") or "").strip()
        base = {
            "row_key": f"{product.casefold()}:{catalog_id.casefold()}",
            "product": product,
            "catalog_id": catalog_id,
            "name": str(row.get("name") or ""),
            "transport": str(row.get("transport") or ""),
        }
        if urlsplit(endpoint).scheme.casefold() != "https":
            return {
                **base,
                "verdict": "rejected",
                "complete": True,
                "reason_code": "unsafe_endpoint",
                "evidence": [{"stage": "url_validation", "status": "failed"}],
                "tool_count": None,
            }
        async with semaphore:
            return {
                **base,
                **await probe(endpoint, transport=str(row.get("transport") or "")),
            }

    return list(await asyncio.gather(*(run(row) for row in rows)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--expect-sha256")
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--changes-output", type=Path)
    parser.add_argument("--aggregate-output", type=Path)
    args = parser.parse_args(argv)
    results = asyncio.run(
        sweep_remote_catalog(
            args.catalog,
            expected_sha256=args.expect_sha256,
            concurrency=args.concurrency,
        )
    )
    args.output.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in results),
        encoding="utf-8",
    )
    if args.changes_output or args.aggregate_output:
        previous = [
            json.loads(line) for line in args.previous.read_text(encoding="utf-8").splitlines() if line.strip()
        ] if args.previous else []
    if args.changes_output:
        changes = diff_results(previous, results)
        args.changes_output.write_text(
            "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in changes),
            encoding="utf-8",
        )
    if args.aggregate_output:
        aggregate = conservative_results(previous, results)
        args.aggregate_output.write_text(
            "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in aggregate),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
