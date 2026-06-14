"""Integration finding (issue 1): security audit defaults OFF so a default-off
deploy is byte-identical to pre-merge prod (no new /var/log JSONL side-effect).
It turns on via an explicit flag OR under strict context."""
from __future__ import annotations

from pathlib import Path

import pytest


def _write_one(audit_path: Path) -> None:
    from hermes_multitenancy.security_audit import append_security_event

    append_security_event(
        event_type="group.everyone.ignored",
        chat_id="oc_x",
        open_id="ou_y",
        reason="at_everyone_broadcast",
    )


def test_audit_disabled_by_default_writes_nothing(monkeypatch, tmp_path):
    # Override the conftest opt-in: no explicit flag, strict OFF → disabled.
    monkeypatch.delenv("HERMES_MT_SECURITY_AUDIT_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_STRICT_CONTEXT", raising=False)
    from hermes_multitenancy.security_audit import security_audit_enabled

    assert security_audit_enabled() is False
    audit = tmp_path / "sec.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))
    _write_one(audit)
    assert not audit.exists()  # default-off → byte-identical, no JSONL written


def test_audit_enabled_by_explicit_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_MULTITENANCY_STRICT_CONTEXT", raising=False)
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")
    from hermes_multitenancy.security_audit import security_audit_enabled

    assert security_audit_enabled() is True
    audit = tmp_path / "sec.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit))
    _write_one(audit)
    assert audit.exists() and audit.read_text().strip()


def test_audit_enabled_under_strict_context(monkeypatch, tmp_path):
    # No explicit flag, but strict on → audit follows the hardened posture.
    monkeypatch.delenv("HERMES_MT_SECURITY_AUDIT_ENABLED", raising=False)
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    from hermes_multitenancy.security_audit import security_audit_enabled

    assert security_audit_enabled() is True


def test_explicit_off_wins_even_under_strict(monkeypatch):
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "0")
    from hermes_multitenancy.security_audit import security_audit_enabled

    assert security_audit_enabled() is False  # operator override beats strict
