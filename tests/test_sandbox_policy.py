from __future__ import annotations

import json
from pathlib import Path

import pytest


def _audit_events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_no_raw_path_leak(path: Path, *, secret: str = "secret-token") -> None:
    raw = path.read_text(encoding="utf-8")
    assert "/Users" not in raw
    assert "/home" not in raw
    assert secret not in raw
    for event in _audit_events(path):
        assert "path_hash" in event
        assert isinstance(event["path_hash"], str)
        assert len(event["path_hash"]) == 12
        assert "path_kind" in event
        assert "path" not in event


def test_require_sandbox_macos_policy_missing_raises_and_audits(tmp_path: Path, monkeypatch):
    from hermes_multitenancy import agent_real

    audit_path = tmp_path / "audit.jsonl"
    missing_policy = tmp_path / "secret-token-policy.sb"
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_MULTITENANCY_REQUIRE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    monkeypatch.setattr(agent_real.sys, "platform", "darwin")
    monkeypatch.setattr(agent_real, "_SANDBOX_POLICY_FILE", missing_policy)

    with pytest.raises(RuntimeError):
        agent_real._wrap_with_sandbox(["python", "-V"], tmp_path / "profile")

    events = _audit_events(audit_path)
    assert events[-1]["event_type"] == "sandbox.denied"
    assert events[-1]["reason"] == "macos_policy_missing"
    assert events[-1]["path_kind"] == ".sb"
    _assert_no_raw_path_leak(audit_path)


def test_require_sandbox_macos_sandbox_exec_not_executable_raises_and_audits(
    tmp_path: Path, monkeypatch
):
    from hermes_multitenancy import agent_real

    audit_path = tmp_path / "audit.jsonl"
    policy = tmp_path / "profile-default.sb"
    policy.write_text("(version 1)\n", encoding="utf-8")
    sandbox_exec = tmp_path / "secret-token-sandbox-exec"
    sandbox_exec.write_text("#!/bin/sh\n", encoding="utf-8")
    sandbox_exec.chmod(0o644)
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_MULTITENANCY_REQUIRE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    monkeypatch.setattr(agent_real.sys, "platform", "darwin")
    monkeypatch.setattr(agent_real, "_SANDBOX_POLICY_FILE", policy)
    monkeypatch.setattr(agent_real, "_SANDBOX_EXEC", str(sandbox_exec))

    with pytest.raises(RuntimeError):
        agent_real._wrap_with_sandbox(["python", "-V"], tmp_path / "profile")

    events = _audit_events(audit_path)
    assert events[-1]["event_type"] == "sandbox.denied"
    assert events[-1]["reason"] == "macos_sandbox_exec_not_executable"
    assert events[-1]["path_kind"] == "<no-ext>"
    _assert_no_raw_path_leak(audit_path)


def test_require_sandbox_unsupported_platform_raises_and_audits(tmp_path: Path, monkeypatch):
    from hermes_multitenancy import agent_real

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_MULTITENANCY_REQUIRE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    monkeypatch.setattr(agent_real.sys, "platform", "sunos5")

    with pytest.raises(RuntimeError):
        agent_real._wrap_with_sandbox(["python", "-V"], tmp_path / "profile")

    events = _audit_events(audit_path)
    assert events[-1]["event_type"] == "sandbox.denied"
    assert events[-1]["reason"] == "unsupported_platform:sunos5"


def test_sandbox_default_macos_policy_missing_falls_back_unchanged(tmp_path: Path, monkeypatch):
    from hermes_multitenancy import agent_real

    cmd = ["python", "-V"]
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.delenv("HERMES_MULTITENANCY_REQUIRE_SANDBOX", raising=False)
    monkeypatch.setattr(agent_real.sys, "platform", "darwin")
    monkeypatch.setattr(agent_real, "_SANDBOX_POLICY_FILE", tmp_path / "missing.sb")

    wrapped = agent_real._wrap_with_sandbox(cmd, tmp_path / "profile")

    assert wrapped == cmd


def test_linux_bwrap_missing_still_raises_and_audits(tmp_path: Path, monkeypatch):
    from hermes_multitenancy import agent_real

    audit_path = tmp_path / "audit.jsonl"
    policy = tmp_path / "bwrap-default.args"
    policy.write_text("# args\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.delenv("HERMES_MULTITENANCY_REQUIRE_SANDBOX", raising=False)
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    monkeypatch.setattr(agent_real.sys, "platform", "linux")
    monkeypatch.setattr(agent_real, "_BWRAP_ARGS_FILE", policy)
    monkeypatch.setattr(agent_real, "_BWRAP_EXEC", str(tmp_path / "secret-token-bwrap"))

    with pytest.raises(RuntimeError):
        agent_real._wrap_with_sandbox(["python", "-V"], tmp_path / "profile")

    events = _audit_events(audit_path)
    assert events[-1]["event_type"] == "sandbox.denied"
    assert events[-1]["reason"] == "linux_bwrap_not_executable"
    assert events[-1]["path_kind"] == "<no-ext>"
    _assert_no_raw_path_leak(audit_path)


def test_upstream_health_flags_required_sandbox_unavailable(tmp_path: Path, monkeypatch):
    from hermes_multitenancy import agent_real, upstream_health

    shared = tmp_path / ".hermes"
    router = shared / "profiles" / "multitenancy_router"
    router.mkdir(parents=True)
    monkeypatch.setenv("HERMES_MULTITENANCY_REQUIRE_SANDBOX", "1")
    monkeypatch.setattr(upstream_health.sys, "platform", "darwin")
    monkeypatch.setattr(agent_real, "_SANDBOX_POLICY_FILE", tmp_path / "missing.sb")

    report = upstream_health.upstream_capability_health(
        shared_home=shared,
        router_profile_home=router,
    )

    checks = {check["name"]: check for check in report["checks"]}
    assert checks["required_sandbox"]["ok"] is False
    assert "required_sandbox" in report["attention"]
    assert report["ready"] is False


def test_upstream_health_skips_required_sandbox_when_flag_off(tmp_path: Path, monkeypatch):
    from hermes_multitenancy import upstream_health

    shared = tmp_path / ".hermes"
    router = shared / "profiles" / "multitenancy_router"
    router.mkdir(parents=True)
    monkeypatch.delenv("HERMES_MULTITENANCY_REQUIRE_SANDBOX", raising=False)
    monkeypatch.setattr(upstream_health.sys, "platform", "darwin")

    report = upstream_health.upstream_capability_health(
        shared_home=shared,
        router_profile_home=router,
    )

    checks = {check["name"]: check for check in report["checks"]}
    assert checks["required_sandbox"]["ok"] is True
    assert checks["required_sandbox"]["status"] == "skipped"
    assert "required_sandbox" not in report["attention"]
    assert report["ready"] is True
