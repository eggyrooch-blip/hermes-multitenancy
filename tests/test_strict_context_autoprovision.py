"""P0-1 (strict = subprocess-only real runner) + P0-3 (strict auto-provision off)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "HERMES_MULTITENANCY_STRICT_CONTEXT",
        "HERMES_MULTITENANCY_AUTO_PROVISION",
        "HERMES_MULTITENANCY_DEV_MODE",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


# --------------------------------------------------------------------------- #
# P0-1 — in strict mode the in-process legacy real runner must fail closed
# --------------------------------------------------------------------------- #

def test_strict_disables_in_process_legacy_real_runner(monkeypatch, tmp_path):
    from hermes_multitenancy import agent_real
    from hermes_multitenancy import security_audit

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    audit = tmp_path / "sec.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")
    monkeypatch.setattr(security_audit, "security_audit_path", lambda: audit)

    with pytest.raises(RuntimeError, match="STRICT_CONTEXT|subprocess|in-process"):
        asyncio.run(agent_real._legacy_real_run_agent(object(), tmp_path))

    # Failed closed BEFORE doing any model work, and audited the denial.
    rows = [r for r in audit.read_text().splitlines() if r.strip()] if audit.exists() else []
    assert any("route.denied" in r and "strict_in_process_real_runner_disabled" in r for r in rows)


def test_non_strict_legacy_runner_not_blocked_by_strict_guard(monkeypatch, tmp_path):
    """Regression: with strict off, the strict guard must NOT fire (the runner
    proceeds past it — it may fail later for lack of real creds, but NOT with the
    strict in-process refusal)."""
    from hermes_multitenancy import agent_real

    # strict unset (default off). The guard must not raise its strict message.
    with pytest.raises(Exception) as exc:
        asyncio.run(agent_real._legacy_real_run_agent(object(), tmp_path))
    assert "STRICT_CONTEXT" not in str(exc.value)


# --------------------------------------------------------------------------- #
# P0-3 — strict mode defaults auto-provision OFF unless explicit dev mode
# --------------------------------------------------------------------------- #

def test_auto_provision_default_on_when_non_strict(monkeypatch):
    from hermes_multitenancy.router import _auto_provision_enabled

    assert _auto_provision_enabled() is True  # legacy default-on


def test_auto_provision_default_off_in_strict(monkeypatch):
    from hermes_multitenancy.router import _auto_provision_enabled

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    assert _auto_provision_enabled() is False  # strict default OFF (unknown user denied)


def test_auto_provision_explicit_on_in_strict(monkeypatch):
    from hermes_multitenancy.router import _auto_provision_enabled

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setenv("HERMES_MULTITENANCY_AUTO_PROVISION", "1")
    assert _auto_provision_enabled() is True  # explicit opt-in still honored


def test_auto_provision_dev_mode_restores_default_in_strict(monkeypatch):
    from hermes_multitenancy.router import _auto_provision_enabled

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setenv("HERMES_MULTITENANCY_DEV_MODE", "1")
    assert _auto_provision_enabled() is True  # dev mode = legacy default-on


def test_auto_provision_off_explicit_in_strict(monkeypatch):
    from hermes_multitenancy.router import _auto_provision_enabled

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setenv("HERMES_MULTITENANCY_AUTO_PROVISION", "0")
    assert _auto_provision_enabled() is False
