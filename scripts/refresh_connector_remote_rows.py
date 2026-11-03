#!/usr/bin/env python3
"""Re-probe blocked public MCP endpoints and refresh frozen conformance rows."""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from hermes_multitenancy.connector_remote_probe import probe_remote_endpoint


_NEXT = {
    "pass": "connect through the owner-scoped remote MCP runtime",
    "needs_auth": "complete owner-scoped authorization before connecting",
    "incompatible": "repair or document a public MCP/CLI protocol surface",
    "rejected": "keep disabled until the rejection reason is remediated",
}
_MANUAL_FIELDS = {
    "workbuddy:caihui-mcp": ["x-api-key"],
}


async def refresh(rows: list[dict], selected: set[str]) -> list[dict]:
    for row in rows:
        if row["row_key"] not in selected:
            continue
        result = await probe_remote_endpoint(
            row["endpoint"], transport="sse" if row.get("transport") == "sse" else "streamable-http"
        )
        row.update(
            final_verdict=result["verdict"], reason_code=result["reason_code"],
            evidence=result["evidence"], tool_count=result.get("tool_count"),
            next_action=_NEXT[result["verdict"]],
            risks=([] if result["verdict"] == "pass" else [
                "owner_authorization_required" if result["verdict"] == "needs_auth" else
                "security_or_manifest_rejection" if result["verdict"] == "rejected" else
                "protocol_incompatible"
            ]),
        )
        if result["verdict"] == "needs_auth" and row["row_key"] in _MANUAL_FIELDS:
            fields = _MANUAL_FIELDS[row["row_key"]]
            row["credential_schema"].update(
                auth_flow="manual_fields", fields=fields, invalid_field_count=0,
                secret_kind="headers",
                field_targets={name: {"kind": "header", "name": name} for name in fields},
            )
    return rows


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connectors", type=Path, default=Path(
        "hermes_multitenancy/connector_catalog_data/connectors.jsonl"
    ))
    parser.add_argument("--canonical", type=Path, default=Path(
        "hermes_multitenancy/connector_catalog_data/canonical.jsonl"
    ))
    parser.add_argument("--row-key", action="append", required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.connectors.read_text().splitlines() if line]
    selected = set(args.row_key)
    known = {row["row_key"] for row in rows}
    if not selected <= known:
        raise ValueError("unknown connector row key")
    _write(args.connectors, asyncio.run(refresh(rows, selected)))
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["canonical_key"], []).append(row)
    _write(args.canonical, [{
        "canonical_key": key,
        "source_row_count": len(items),
        "products": sorted({item["product"] for item in items}),
        "verdicts": dict(sorted(Counter(item["final_verdict"] for item in items).items())),
    } for key, items in sorted(groups.items())])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
