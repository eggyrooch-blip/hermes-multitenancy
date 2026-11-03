"""Merge connector conformance stages into portable JSONL/CSV/Markdown output."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .connector_catalog_conformance import REMOTE_TRANSPORTS, VERDICTS, lint_catalog, read_catalog


_NEXT = {
    "pass": ([], "ready for read-only adapter validation"),
    "needs_auth": (["external_credential"], "complete owner-bound authorization and repeat the read-only probe"),
    "needs_sandbox": (["untrusted_process"], "supply immutable package identity and pass the sandbox gate"),
    "incompatible": (["protocol_incompatible"], "repair or document a public MCP/CLI protocol surface"),
    "rejected": (["security_or_manifest_rejection"], "keep disabled until the rejection reason is remediated"),
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico"}


def _jsonl(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _safe_endpoint(value: Any) -> str | None:
    try:
        parsed = urlsplit(str(value or "").strip())
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return urlunsplit((parsed.scheme, f"{host}:{port}" if port else host, parsed.path, "", ""))


def _stage_map(paths: list[str | Path], allowed: set[str]) -> dict[str, dict[str, Any]]:
    stages: dict[str, dict[str, Any]] = {}
    for path in paths:
        for item in _jsonl(path):
            key = item.get("row_key")
            if key not in allowed or key in stages or item.get("verdict") not in VERDICTS:
                raise ValueError("invalid, duplicate, or foreign stage result")
            stages[key] = item
    return stages


def _import_icon(source: Path, output: Path, *, provenance: str = "workbuddy_official_local_market_snapshot") -> dict[str, Any]:
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    target = output / f"{digest}{source.suffix.casefold()}"
    output.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copyfile(source, target)
    return {
        "status": "imported",
        "path": f"icons/{target.name}",
        "sha256": digest,
        "source": provenance,
        "redistribution_status": "review_required",
    }


def build_final_results(
    catalog: str | Path,
    *,
    remote_results: str | Path,
    stdio_results: str | Path,
    sandbox_results: str | Path | None = None,
    vendor_icon_map: str | Path | None = None,
    vendor_icon_root: str | Path | None = None,
    product_fallbacks: dict[str, str | Path] | None = None,
    credential_schemas: str | Path,
    workbuddy_icons: str | Path,
    output_icons: str | Path,
    expected_sha256: str | None = None,
) -> list[dict[str, Any]]:
    rows = read_catalog(catalog, expected_sha256=expected_sha256)
    manifest = {item["row_key"]: item for item in lint_catalog(catalog, expected_sha256=expected_sha256)}
    transports = {
        f"{str(row['product']).strip().casefold()}:{str(row['catalog_id']).strip().casefold()}": str(row.get("transport") or "").casefold()
        for row in rows
    }
    remote_keys = {key for key, transport in transports.items() if transport in REMOTE_TRANSPORTS}
    stdio_keys = {key for key, transport in transports.items() if transport == "stdio"}
    stages = _stage_map([remote_results], remote_keys)
    stages.update(_stage_map([stdio_results], stdio_keys))
    if sandbox_results:
        for key, item in _stage_map([sandbox_results], stdio_keys).items():
            stages[key] = item
    schemas = {item["row_key"]: item for item in _jsonl(credential_schemas)}
    vendor_icons = {item["row_key"]: item for item in _jsonl(vendor_icon_map)} if vendor_icon_map else {}
    vendor_root = Path(vendor_icon_root).resolve() if vendor_icon_root else None
    if vendor_icons and vendor_root is None:
        raise ValueError("vendor icon root is required")
    fallbacks = {key.casefold(): Path(value) for key, value in (product_fallbacks or {}).items()}
    icon_root = Path(workbuddy_icons).resolve()
    icon_files = {
        path.stem.casefold(): path
        for path in sorted(icon_root.iterdir())
        if path.is_file() and path.suffix.casefold() in _IMAGE_SUFFIXES
    }
    imported: dict[tuple[Path, str], dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for row in rows:
        product = str(row["product"]).strip()
        catalog_id = str(row["catalog_id"]).strip()
        row_key = f"{product.casefold()}:{catalog_id.casefold()}"
        stage = stages.get(row_key)
        if stage is None:
            stage = manifest[row_key]
            if stage["verdict"] not in {"incompatible", "rejected"}:
                raise ValueError(f"missing terminal stage result: {row_key}")
        risks, next_action = _NEXT[stage["verdict"]]
        icon_source = icon_files.get(catalog_id.casefold()) if product.casefold() == "workbuddy" else None
        provenance = "workbuddy_official_local_market_snapshot"
        vendor_icon = vendor_icons.get(row_key)
        if icon_source is None and vendor_icon:
            icon_source = Path(vendor_icon["path"]).resolve()
            if not icon_source.is_relative_to(vendor_root) or icon_source.suffix.casefold() not in _IMAGE_SUFFIXES:
                raise ValueError("vendor icon path is outside the approved image root")
            provenance = str(vendor_icon.get("source") or "vendor_official_market")
        fallback = False
        if icon_source is None and product.casefold() in fallbacks:
            icon_source = fallbacks[product.casefold()]
            provenance = "vendor_official_product_fallback"
            fallback = True
        if icon_source:
            cache_key = (icon_source, provenance)
            if cache_key not in imported:
                imported[cache_key] = _import_icon(icon_source, Path(output_icons), provenance=provenance)
            icon = imported[cache_key]
            icon = {**icon, "status": "product_fallback" if fallback else "imported"}
        else:
            icon = {"status": "missing_official_asset"}
        results.append(
            {
                "row_key": row_key,
                "canonical_key": manifest[row_key]["canonical_key"],
                "product": product,
                "catalog_id": catalog_id,
                "name": str(row.get("name") or ""),
                "description": str(row.get("description") or ""),
                "transport": str(row.get("transport") or ""),
                "endpoint": _safe_endpoint(row.get("endpoint")),
                "download_count": row.get("download_count"),
                "download_count_status": row.get("download_count_status"),
                "final_verdict": stage["verdict"],
                "complete": True,
                "reason_code": stage["reason_code"],
                "risks": stage.get("risks", risks),
                "next_action": stage.get("next_action", next_action),
                "evidence": stage.get("evidence"),
                "tool_count": stage.get("tool_count", row.get("tool_count")),
                "credential_schema": schemas.get(row_key),
                "icon": icon,
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--remote-results", type=Path, required=True)
    parser.add_argument("--stdio-results", type=Path, required=True)
    parser.add_argument("--sandbox-results", type=Path)
    parser.add_argument("--vendor-icon-map", type=Path)
    parser.add_argument("--vendor-icon-root", type=Path)
    parser.add_argument("--product-fallback", action="append", default=[], metavar="PRODUCT=PATH")
    parser.add_argument("--credential-schemas", type=Path, required=True)
    parser.add_argument("--workbuddy-icons", type=Path, required=True)
    parser.add_argument("--expect-sha256", required=True)
    parser.add_argument("--expect-count", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    fallbacks = dict(value.split("=", 1) for value in args.product_fallback)
    results = build_final_results(
        args.catalog,
        remote_results=args.remote_results,
        stdio_results=args.stdio_results,
        sandbox_results=args.sandbox_results,
        vendor_icon_map=args.vendor_icon_map,
        vendor_icon_root=args.vendor_icon_root,
        product_fallbacks=fallbacks,
        credential_schemas=args.credential_schemas,
        workbuddy_icons=args.workbuddy_icons,
        output_icons=args.output_dir / "icons",
        expected_sha256=args.expect_sha256,
    )
    if len(results) != args.expect_count:
        raise ValueError("catalog row count mismatch")
    jsonl = args.output_dir / "connectors.jsonl"
    jsonl.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in results),
        encoding="utf-8",
    )
    with (args.output_dir / "connectors.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "row_key", "canonical_key", "product", "catalog_id", "name", "transport",
            "final_verdict", "reason_code", "tool_count", "download_count", "icon_status", "icon_path",
        ])
        writer.writeheader()
        for item in results:
            writer.writerow({
                **{key: item.get(key) for key in writer.fieldnames if not key.startswith("icon_")},
                "icon_status": item["icon"]["status"],
                "icon_path": item["icon"].get("path"),
            })
    verdicts = Counter(item["final_verdict"] for item in results)
    icons = Counter(item["icon"]["status"] for item in results)
    canonical: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        canonical.setdefault(item["canonical_key"], []).append(item)
    (args.output_dir / "canonical.jsonl").write_text(
        "".join(
            json.dumps({
                "canonical_key": key,
                "source_row_count": len(items),
                "products": sorted({item["product"] for item in items}),
                "verdicts": dict(sorted(Counter(item["final_verdict"] for item in items).items())),
            }, ensure_ascii=False, sort_keys=True) + "\n"
            for key, items in sorted(canonical.items())
        ),
        encoding="utf-8",
    )
    (args.output_dir / "action-required.jsonl").write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
            for item in results if item["final_verdict"] != "pass"
        ),
        encoding="utf-8",
    )
    known_downloads = [item["download_count"] for item in results if isinstance(item["download_count"], int)]
    (args.output_dir / "README.md").write_text(
        "# Connector conformance report\n\n"
        f"- Source rows: {len(results)}\n"
        f"- Verdicts: {json.dumps(dict(sorted(verdicts.items())), ensure_ascii=False)}\n"
        f"- Canonical entries: {len(canonical)} (source rows remain 642)\n"
        f"- Download counts: known rows={len(known_downloads)}, unknown rows={len(results) - len(known_downloads)}, known total={sum(known_downloads)}\n"
        f"- Icons: {json.dumps(dict(sorted(icons.items())), ensure_ascii=False)}\n"
        "- Icon redistribution: imported vendor assets remain review_required before production serving.\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
