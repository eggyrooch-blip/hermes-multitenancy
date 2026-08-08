import json
from pathlib import Path

from scripts.verify_feishu_openclaw_capabilities import validate_capabilities


def test_feishu_openclaw_capabilities_are_complete():
    root = Path(__file__).resolve().parents[1]
    assert validate_capabilities(root) == []


def test_feishu_openclaw_capabilities_reject_unknown_state(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = root / "hermes_multitenancy/feishu_openclaw_capabilities.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    data["contract_entries"][0]["enabled_state"] = "UNKNOWN"
    mutated = tmp_path / "capabilities.json"
    mutated.write_text(json.dumps(data), encoding="utf-8")
    assert any("unknown enabled state" in error for error in validate_capabilities(root, mutated))
