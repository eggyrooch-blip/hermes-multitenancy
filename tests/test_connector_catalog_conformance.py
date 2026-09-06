import json
from pathlib import Path

import pytest


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_manifest_lint_is_one_to_one_deterministic_and_non_executing(tmp_path: Path):
    from hermes_multitenancy.connector_catalog_conformance import lint_catalog

    marker = tmp_path / "must-not-exist"
    source = tmp_path / "source.jsonl"
    rows = [
        {
            "product": "WorkBuddy",
            "catalog_id": "remote",
            "transport": "streamableHttp",
            "endpoint": "https://example.test/mcp",
            "auth_mode": None,
        },
        {
            "product": "TRAE",
            "catalog_id": "stdio",
            "transport": "stdio",
            "command": f"touch {marker}",
            "auth_mode": "config_keys",
            "credential_key_names": ["AMBIENT_SECRET"],
        },
    ]
    _write_jsonl(source, rows)

    first = lint_catalog(source)
    second = lint_catalog(source)

    assert first == second
    assert len(first) == len(rows)
    assert [item["row_key"] for item in first] == [
        "workbuddy:remote",
        "trae:stdio",
    ]
    assert {item["verdict"] for item in first} <= {
        "pass",
        "needs_auth",
        "needs_sandbox",
        "incompatible",
        "rejected",
    }
    assert all(item["stage"] == "manifest" for item in first)
    assert all(item["complete"] is False for item in first)
    assert all(item["reason_code"] and item["next_action"] for item in first)
    assert first[0]["verdict"] == "pass"
    assert first[1]["verdict"] == "needs_sandbox"
    assert not marker.exists()
    assert "AMBIENT_SECRET" not in json.dumps(first)


def test_manifest_lint_rejects_duplicate_source_rows(tmp_path: Path):
    from hermes_multitenancy.connector_catalog_conformance import lint_catalog

    source = tmp_path / "source.jsonl"
    row = {"product": "TRAE", "catalog_id": "duplicate", "transport": "stdio"}
    _write_jsonl(source, [row, row])

    with pytest.raises(ValueError, match="duplicate row_key"):
        lint_catalog(source)


def test_manifest_lint_treats_explicit_public_auth_modes_as_unauthenticated(tmp_path: Path):
    from hermes_multitenancy.connector_catalog_conformance import lint_catalog

    source = tmp_path / "source.jsonl"
    _write_jsonl(source, [
        {"product": "Vendor", "catalog_id": mode, "transport": "http", "endpoint": "https://example.test/mcp", "auth_mode": mode}
        for mode in ("none", "open", "public")
    ])

    assert [item["verdict"] for item in lint_catalog(source)] == ["pass", "pass", "pass"]


def test_remote_sweep_preserves_source_rows_and_skips_stdio(tmp_path: Path):
    from hermes_multitenancy.connector_remote_sweep import sweep_remote_catalog

    source = tmp_path / "source.jsonl"
    _write_jsonl(
        source,
        [
            {
                "product": "WorkBuddy",
                "catalog_id": "safe",
                "name": "Safe",
                "transport": "streamableHttp",
                "endpoint": "https://safe.example/mcp",
            },
            {
                "product": "WorkBuddy",
                "catalog_id": "plain-http",
                "name": "Plain",
                "transport": "http",
                "endpoint": "http://plain.example/mcp",
            },
            {
                "product": "TRAE",
                "catalog_id": "stdio",
                "transport": "stdio",
                "command": "npx unsafe@latest",
            },
        ],
    )
    called = []

    async def probe(url: str, **_kwargs):
        called.append(url)
        return {
            "verdict": "needs_auth",
            "complete": True,
            "reason_code": "remote_auth_required",
            "evidence": [],
            "tool_count": None,
        }

    results = __import__("asyncio").run(sweep_remote_catalog(source, probe=probe))

    assert [item["row_key"] for item in results] == [
        "workbuddy:safe",
        "workbuddy:plain-http",
    ]
    assert called == ["https://safe.example/mcp"]
    assert results[0]["verdict"] == "needs_auth"
    assert results[1]["verdict"] == "rejected"
    assert results[1]["reason_code"] == "unsafe_endpoint"


def test_remote_result_diff_only_reports_stable_status_changes():
    from hermes_multitenancy.connector_remote_sweep import conservative_results, diff_results

    previous = [
        {"row_key": "vendor:same", "verdict": "pass", "reason_code": "ok", "tool_count": 2, "evidence": [1]},
        {"row_key": "vendor:changed", "verdict": "needs_auth", "reason_code": "auth", "tool_count": None},
    ]
    current = [
        {"row_key": "vendor:same", "verdict": "pass", "reason_code": "ok", "tool_count": 2, "evidence": [2]},
        {"row_key": "vendor:changed", "verdict": "pass", "reason_code": "ok", "tool_count": 3},
    ]

    assert [item["row_key"] for item in diff_results(previous, current)] == ["vendor:changed"]

    unsafe = [{"row_key": "vendor:changed", "verdict": "rejected", "reason_code": "unsafe_endpoint"}]
    assert conservative_results(unsafe, current[1:])[0]["verdict"] == "rejected"


def test_final_results_merge_stage_evidence_and_import_existing_icons(tmp_path: Path):
    from hermes_multitenancy.connector_conformance_report import build_final_results

    source = tmp_path / "source.jsonl"
    _write_jsonl(
        source,
        [
            {"product": "WorkBuddy", "catalog_id": "alpha", "name": "Alpha", "transport": "http"},
            {"product": "TRAE", "catalog_id": "vendor.alpha", "name": "Alpha", "transport": "stdio"},
            {"product": "DoubaoWork", "catalog_id": "private", "name": "Private"},
        ],
    )
    remote = tmp_path / "remote.jsonl"
    _write_jsonl(remote, [{"row_key": "workbuddy:alpha", "verdict": "pass", "reason_code": "tools_list_ok", "evidence": []}])
    stdio = tmp_path / "stdio.jsonl"
    _write_jsonl(stdio, [{"row_key": "trae:vendor.alpha", "verdict": "needs_sandbox", "reason_code": "package_identity_missing", "evidence": {}}])
    sandbox = tmp_path / "sandbox.jsonl"
    _write_jsonl(sandbox, [{"row_key": "trae:vendor.alpha", "verdict": "pass", "reason_code": "sandbox_tools_list_ok", "tool_count": 2}])
    credentials = tmp_path / "credentials.jsonl"
    _write_jsonl(credentials, [])
    icons = tmp_path / "icons"
    icons.mkdir()
    (icons / "alpha.svg").write_text("<svg/>", encoding="utf-8")
    vendor_icon = tmp_path / "vendor.png"
    vendor_icon.write_bytes(b"\x89PNG\r\n\x1a\n")
    vendor_map = tmp_path / "vendor-icons.jsonl"
    _write_jsonl(vendor_map, [{"row_key": "trae:vendor.alpha", "path": str(vendor_icon), "source": "official_test"}])
    fallback = tmp_path / "fallback.png"
    fallback.write_bytes(b"\x89PNG\r\n\x1a\n-fallback")

    results = build_final_results(
        source,
        remote_results=remote,
        stdio_results=stdio,
        sandbox_results=sandbox,
        vendor_icon_map=vendor_map,
        vendor_icon_root=tmp_path,
        product_fallbacks={"DoubaoWork": fallback},
        credential_schemas=credentials,
        workbuddy_icons=icons,
        output_icons=tmp_path / "imported-icons",
    )

    assert len(results) == 3
    assert [item["final_verdict"] for item in results] == [
        "pass",
        "pass",
        "incompatible",
    ]
    assert results[0]["icon"]["status"] == "imported"
    assert results[1]["icon"]["status"] == "imported"
    assert results[1]["tool_count"] == 2
    assert results[1]["risks"] == []
    assert results[2]["next_action"]
    assert results[0]["icon"]["path"] != results[1]["icon"]["path"]
    assert results[2]["icon"]["status"] == "product_fallback"


def test_final_report_rejects_missing_remote_stage_userinfo_and_bad_stage_rows(tmp_path: Path):
    from hermes_multitenancy.connector_conformance_report import _safe_endpoint, build_final_results

    source = tmp_path / "source.jsonl"
    _write_jsonl(source, [{"product": "Vendor", "catalog_id": "remote", "transport": "http", "endpoint": "https://user:secret@example.test/mcp"}])
    empty = tmp_path / "empty.jsonl"
    _write_jsonl(empty, [])
    icons = tmp_path / "icons"
    icons.mkdir()

    assert _safe_endpoint("https://user:secret@example.test/mcp") is None
    with pytest.raises(ValueError, match="missing terminal stage result"):
        build_final_results(source, remote_results=empty, stdio_results=empty, credential_schemas=empty, workbuddy_icons=icons, output_icons=tmp_path / "out")

    bad = tmp_path / "bad.jsonl"
    _write_jsonl(bad, [{"row_key": "foreign:row", "verdict": "pass", "reason_code": "bad"}])
    with pytest.raises(ValueError, match="foreign stage"):
        build_final_results(source, remote_results=bad, stdio_results=empty, credential_schemas=empty, workbuddy_icons=icons, output_icons=tmp_path / "out2")


def test_final_report_rejects_cross_transport_stage_and_icon_outside_root(tmp_path: Path):
    from hermes_multitenancy.connector_conformance_report import build_final_results

    source = tmp_path / "source.jsonl"
    _write_jsonl(source, [{"product": "Vendor", "catalog_id": "stdio", "transport": "stdio", "command": "python"}])
    stage = tmp_path / "stage.jsonl"
    _write_jsonl(stage, [{"row_key": "vendor:stdio", "verdict": "pass", "reason_code": "bad"}])
    empty = tmp_path / "empty.jsonl"
    _write_jsonl(empty, [])
    icons = tmp_path / "icons"
    icons.mkdir()

    with pytest.raises(ValueError, match="foreign stage"):
        build_final_results(source, remote_results=stage, stdio_results=empty, credential_schemas=empty, workbuddy_icons=icons, output_icons=tmp_path / "out")

    icon_map = tmp_path / "icon-map.jsonl"
    _write_jsonl(icon_map, [{"row_key": "vendor:stdio", "path": "/etc/hosts", "source": "bad"}])
    with pytest.raises(ValueError, match="outside the approved"):
        build_final_results(source, remote_results=empty, stdio_results=stage, credential_schemas=empty, workbuddy_icons=icons, output_icons=tmp_path / "out2", vendor_icon_map=icon_map, vendor_icon_root=tmp_path)
