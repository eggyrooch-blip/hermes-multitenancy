"""Deterministic, non-executing lint for connector catalog manifests."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


VERDICTS = {"pass", "needs_auth", "needs_sandbox", "incompatible", "rejected"}
REMOTE_TRANSPORTS = {"http", "streamablehttp", "streamable-http", "sse"}


def _identity(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"missing {field}")
    return text.casefold()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _canonical_key(row: dict[str, Any], row_key: str) -> str:
    endpoint = str(row.get("endpoint") or "").strip()
    if endpoint:
        parsed = urlsplit(endpoint)
        safe_endpoint = urlunsplit(
            (parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path or "/", "", "")
        )
        return f"remote:{_digest(safe_endpoint)}"
    command = row.get("command")
    if command:
        rendered = json.dumps(command, ensure_ascii=False, sort_keys=True)
        return f"stdio:{_digest(rendered)}"
    return f"catalog:{_digest(row_key)}"


def _classify(row: dict[str, Any]) -> tuple[str, str, str, list[str]]:
    transport = str(row.get("transport") or "").strip().casefold()
    auth_mode = str(row.get("auth_mode") or "").strip().casefold()
    portability = str(row.get("portability") or "").strip().casefold()

    if transport == "stdio":
        return (
            "needs_sandbox",
            "stdio_admission_required",
            "verify pinned source, digest, license and permissions before sandbox launch",
            ["untrusted_process"],
        )
    if transport in REMOTE_TRANSPORTS:
        if not str(row.get("endpoint") or "").strip():
            return "rejected", "missing_endpoint", "repair the source manifest", ["invalid_manifest"]
        if auth_mode not in {"", "none", "open", "public"}:
            return (
                "needs_auth",
                "credential_binding_required",
                "bind an owner-scoped credential before a read-only remote probe",
                ["external_credential"],
            )
        return "pass", "manifest_admitted", "run the unauthenticated remote safety probe", []
    if "vendor" in portability or not transport:
        return (
            "incompatible",
            "vendor_runtime_only",
            "use a documented public MCP surface or keep this connector product-managed",
            ["private_runtime"],
        )
    return (
        "incompatible",
        "unsupported_transport",
        "add an explicit adapter only after a public protocol surface is documented",
        ["unsupported_transport"],
    )


def read_catalog(path: str | Path, *, expected_sha256: str | None = None) -> list[dict[str, Any]]:
    source = Path(path)
    raw = source.read_bytes()
    if expected_sha256 and hashlib.sha256(raw).hexdigest() != expected_sha256.casefold():
        raise ValueError("catalog sha256 mismatch")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on line {line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"catalog row {line_number} must be an object")
        row_key = f"{_identity(row.get('product'), 'product')}:{_identity(row.get('catalog_id'), 'catalog_id')}"
        if row_key in seen:
            raise ValueError(f"duplicate row_key: {row_key}")
        seen.add(row_key)
        rows.append(row)
    return rows


def lint_catalog(path: str | Path, *, expected_sha256: str | None = None) -> list[dict[str, Any]]:
    """Read catalog JSONL and return safe stage results without external I/O."""
    results: list[dict[str, Any]] = []
    for row in read_catalog(path, expected_sha256=expected_sha256):
        row_key = f"{_identity(row.get('product'), 'product')}:{_identity(row.get('catalog_id'), 'catalog_id')}"
        verdict, reason_code, next_action, risks = _classify(row)
        results.append(
            {
                "row_key": row_key,
                "canonical_key": _canonical_key(row, row_key),
                "stage": "manifest",
                "verdict": verdict,
                "complete": False,
                "reason_code": reason_code,
                "evidence": [{"check": "manifest", "status": "pass"}],
                "risks": risks,
                "next_action": next_action,
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--expect-sha256", required=True)
    parser.add_argument("--expect-count", type=int, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    results = lint_catalog(args.catalog, expected_sha256=args.expect_sha256)
    if len(results) != args.expect_count:
        raise ValueError("catalog row count mismatch")
    rendered = "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
        for item in results
    )
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
