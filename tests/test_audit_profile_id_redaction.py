"""issue-2: embedded ou_/oc_ ids in safe text fields (mainly `profile`) must be
hashed before reaching the audit JSONL — they otherwise leak open_id/chat_id raw."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


def _rows(p: Path):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def test_group_profile_embedded_chat_id_is_hashed(monkeypatch, tmp_path):
    from hermes_multitenancy.security_audit import append_security_event

    audit = tmp_path / "sec.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))

    append_security_event(event_type="route.denied", profile="feishu_group_oc_1914a6e2c17a197a", reason="x")

    raw = audit.read_text()
    assert "oc_1914a6e2c17a197a" not in raw  # raw chat_id never logged
    row = _rows(audit)[-1]
    assert row["profile"] == f"feishu_group_oc_{_h('oc_1914a6e2c17a197a')}"


def test_user_profile_embedded_open_id_is_hashed(monkeypatch, tmp_path):
    from hermes_multitenancy.security_audit import append_security_event

    audit = tmp_path / "sec.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))

    append_security_event(event_type="credential.lease.denied", profile="feishu_ou_cf23e7c262afa4b7", decision="denied")

    raw = audit.read_text()
    assert "ou_cf23e7c262afa4b7" not in raw
    row = _rows(audit)[-1]
    assert row["profile"] == f"feishu_ou_{_h('ou_cf23e7c262afa4b7')}"


def test_plain_profile_name_passes_through(monkeypatch, tmp_path):
    from hermes_multitenancy.security_audit import append_security_event

    audit = tmp_path / "sec.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))

    for p in ("feishu_alice", "sunke", "wangweiming_v"):
        append_security_event(event_type="route.denied", profile=p, reason="x")
    profiles = [r["profile"] for r in _rows(audit)]
    assert profiles == ["feishu_alice", "sunke", "wangweiming_v"]  # ops-readable, untouched


def test_same_id_hashes_identically_across_rows(monkeypatch, tmp_path):
    from hermes_multitenancy.security_audit import append_security_event

    audit = tmp_path / "sec.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))

    append_security_event(event_type="route.denied", profile="feishu_oc_g1", reason="a")
    append_security_event(event_type="sandbox.denied", profile="feishu_oc_g1", reason="b")
    profiles = [r["profile"] for r in _rows(audit)]
    assert profiles[0] == profiles[1]  # correlatable across lines


def test_existing_open_id_chat_id_hashing_not_regressed(monkeypatch, tmp_path):
    from hermes_multitenancy.security_audit import append_security_event

    audit = tmp_path / "sec.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))

    append_security_event(event_type="group.everyone.ignored", open_id="ou_x", chat_id="oc_y", reason="z")
    row = _rows(audit)[-1]
    assert row["open_id_hash"] == _h("ou_x")
    assert row["chat_id_hash"] == _h("oc_y")
    assert "ou_x" not in audit.read_text() and "oc_y" not in audit.read_text()


def test_redaction_unit():
    from hermes_multitenancy.security_audit import _redact_embedded_ids

    assert _redact_embedded_ids("feishu_alice") == "feishu_alice"
    assert _redact_embedded_ids("plain text no id") == "plain text no id"
    out = _redact_embedded_ids("feishu_group_oc_ABC123")
    assert "oc_ABC123" not in out and out.startswith("feishu_group_oc_")
    # underscore-inclusive body (SPEC): a hypothetical id with '_' is fully hashed
    out2 = _redact_embedded_ids("feishu_group_oc_secret_chat_id")
    assert "secret_chat_id" not in out2


def test_conversation_audit_profile_is_redacted(monkeypatch, tmp_path):
    """Review concern: the sibling conversation-audit sink also wrote `profile`
    raw — same leak class, folded into this fix."""
    from hermes_multitenancy.conversation_audit import append_conversation_audit_event

    audit = tmp_path / "conv.jsonl"
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_ENABLED", "1")
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit))

    append_conversation_audit_event(
        profile_name="feishu_group_oc_1914a6e2c17a197a",
        platform="feishu", chat_type="group", session_id="s1",
        message_id=1, role="user", content="hi", timestamp=1700000000,
    )
    raw = audit.read_text()
    assert "oc_1914a6e2c17a197a" not in raw
    row = _rows(audit)[-1]
    assert row["profile"] == f"feishu_group_oc_{_h('oc_1914a6e2c17a197a')}"


def test_shim_denied_path_redacts_profile(monkeypatch, tmp_path):
    """Finding #1: the self-contained lark-cli shim writes its own audit line
    (no plugin import), so it must redact HERMES_PROFILE itself."""
    import subprocess
    import sys

    from hermes_multitenancy.lark_cli_guard import install_lark_cli_shim

    shim_dir = tmp_path / "shim"
    real = tmp_path / "real-lark-cli"
    real.write_text("#!/bin/sh\necho TOOL\n")
    real.chmod(0o755)
    install_lark_cli_shim(shim_dir, real_binary=real)

    audit = tmp_path / "sec.jsonl"
    env = {
        "PATH": "/usr/bin:/bin",
        "HERMES_PROFILE": "feishu_group_oc_1914a6e2c17a197a",
        "HERMES_FEISHU_USER_OPEN_ID": "ou_cf23e7c262afa4b7",
        "HERMES_MT_SECURITY_AUDIT_ENABLED": "1",
        "HERMES_MT_SECURITY_AUDIT_PATH": str(audit),
        # no run token → denied path
    }
    proc = subprocess.run([sys.executable, str(shim_dir / "lark-cli"), "im"], env=env, capture_output=True)
    assert proc.returncode == 126
    raw = audit.read_text()
    assert "oc_1914a6e2c17a197a" not in raw          # group chat_id not leaked
    assert "ou_cf23e7c262afa4b7" not in raw          # open_id not leaked (hashed)
    row = _rows(audit)[-1]
    assert row["event_type"] == "lark_cli.direct_exec.denied"
    assert row["profile"] == f"feishu_group_oc_{_h('oc_1914a6e2c17a197a')}"
