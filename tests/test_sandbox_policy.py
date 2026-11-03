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


# ── issue #10/#11/#12: host creds, control-plane db, fail-closed ──────────
# The three published security issues. Both policy files are asserted, not just
# the Linux one the issues named — the macOS .sb carried identical holes.

import pytest

_POLICY_DIR = Path(__file__).resolve().parent.parent / "hermes_multitenancy" / "sandbox"
_FORBIDDEN_MOUNTS = (
    "/.aws",
    "/.config/gh",
    "/.config/git",
    "/.gitconfig",
    "multitenancy.db",
)


def _granting_text(path: Path) -> str:
    """Only the parts of a policy that GRANT access, comments stripped.

    bwrap args are flat lines. The macOS .sb is S-expressions whose deny block
    spans several lines, so a line-by-line filter would misread lines inside
    `(deny ...)` as grants — split on top-level forms and keep the allows.
    """
    body = "\n".join(
        raw for raw in path.read_text(encoding="utf-8").splitlines()
        if raw.strip() and not raw.strip().startswith(("#", ";"))
    )
    if path.suffix != ".sb":
        return body
    forms = body.split("\n(")
    return "\n".join(f for f in forms if f.lstrip("(").startswith("allow"))


@pytest.mark.parametrize("policy", ["bwrap-default.args", "profile-default.sb"])
@pytest.mark.parametrize("needle", _FORBIDDEN_MOUNTS)
def test_policy_grants_no_host_creds_or_control_plane_db(policy, needle):
    """#11 + #12: no grant line may reference host credentials or multitenancy.db.

    Read-only was never enough for ~/.aws or ~/.config/gh — it stops writes, not
    theft. multitenancy.db holds routing, principals, agent shares and encrypted
    credential rows, and no sandboxed child reads it.
    """
    granting = [
        line for line in _granting_text(_POLICY_DIR / policy).splitlines()
        if needle in line
    ]
    assert granting == [], f"{policy} still grants {needle}: {granting}"


def test_macos_policy_denies_host_credential_dirs():
    """#12: removing the allow is not enough — a typo must not re-widen it."""
    text = (_POLICY_DIR / "profile-default.sb").read_text(encoding="utf-8")
    deny_block = text.split("(deny file-read*")[1].split(")\n\n")[0]
    for needle in ("/.aws", "/.config/gh", "/.config/git", "/.gitconfig"):
        assert needle in deny_block, f"{needle} missing from the macOS deny block"


def test_require_sandbox_refuses_when_toggle_off(monkeypatch, tmp_path):
    """#10: REQUIRE_SANDBOX=1 + sandbox off must raise, not return a bare cmd."""
    from hermes_multitenancy.agent_real import _core

    monkeypatch.delenv("HERMES_USE_SANDBOX", raising=False)
    monkeypatch.delenv("HERMES_SANDBOX_PROFILES", raising=False)
    monkeypatch.setenv("HERMES_MULTITENANCY_REQUIRE_SANDBOX", "1")
    profile = tmp_path / "profiles" / "feishu_x"
    profile.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="refusing to spawn"):
        _core._wrap_with_sandbox(["/bin/echo", "hi"], profile)


def test_require_sandbox_refuses_profile_gated_out_of_allowlist(monkeypatch, tmp_path):
    """#10: a profile left out of the pilot allowlist is the exposure, not an excuse."""
    from hermes_multitenancy.agent_real import _core

    monkeypatch.setenv("HERMES_USE_SANDBOX", "1")
    monkeypatch.setenv("HERMES_SANDBOX_PROFILES", "spike_test")
    monkeypatch.setenv("HERMES_MULTITENANCY_REQUIRE_SANDBOX", "1")
    profile = tmp_path / "profiles" / "feishu_prod"
    profile.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="profile_not_in_allowlist"):
        _core._wrap_with_sandbox(["/bin/echo", "hi"], profile)


def test_default_stays_fail_open_when_require_not_set(monkeypatch, tmp_path):
    """Regression guard: without REQUIRE_SANDBOX the old contract is untouched."""
    from hermes_multitenancy.agent_real import _core

    monkeypatch.delenv("HERMES_USE_SANDBOX", raising=False)
    monkeypatch.delenv("HERMES_SANDBOX_PROFILES", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_REQUIRE_SANDBOX", raising=False)
    profile = tmp_path / "profiles" / "feishu_x"
    profile.mkdir(parents=True)
    cmd = ["/bin/echo", "hi"]
    assert _core._wrap_with_sandbox(cmd, profile) == cmd
