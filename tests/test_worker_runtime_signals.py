"""Worker runtime signals (2026-08-31 debt slug worker-strict-context-durable-audit).

Two production facts drive every fixture here:
- The worker env never carries HERMES_MULTITENANCY_STRICT_CONTEXT (and must not:
  flipping it activates the dormant strict write allowlist and would reject every
  non-IM Feishu write — wf_46aff7d5 risk analysis, adversarially verified).
- HERMES_LARK_CLI_RUN_TOKEN is minted only by the strict build path, so token
  presence is the worker's strict-runtime proof.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _worker_shape_tool_env(monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "profile"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("LARKSUITE_CLI_AUTH_PROXY", "http://127.0.0.1:16384")
    monkeypatch.setenv("LARKSUITE_CLI_PROXY_KEY", "proxy-secret-value")
    monkeypatch.setenv("LARKSUITE_CLI_APP_ID", "cli_public")
    monkeypatch.delenv("HERMES_MULTITENANCY_STRICT_CONTEXT", raising=False)


class _Completed:
    returncode = 0
    stdout = '{"code":0,"data":{}}'
    stderr = ""


def test_s2_authorized_passthrough_anchors_on_run_token(monkeypatch, tmp_path):
    from hermes_multitenancy import lark_cli_tool

    _worker_shape_tool_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_LARK_CLI_RUN_TOKEN", "tok-123")
    captured = {}

    def fake_run(command, **kwargs):
        captured["env"] = kwargs.get("env")
        return _Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)
    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["GET", "/open-apis/authen/v1/user_info"],
            "identity": "user",
            "risk": "read",
            "reason": "worker-shape authorized passthrough",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)
    assert result["ok"] is True
    assert captured["env"]["HERMES_LARK_CLI_AUTHORIZED"] == "tok-123"


def test_s2b_without_run_token_authorized_is_not_injected(monkeypatch, tmp_path):
    from hermes_multitenancy import lark_cli_tool

    _worker_shape_tool_env(monkeypatch, tmp_path)
    monkeypatch.delenv("HERMES_LARK_CLI_RUN_TOKEN", raising=False)
    captured = {}

    def fake_run(command, **kwargs):
        captured["env"] = kwargs.get("env")
        return _Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)
    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["GET", "/open-apis/authen/v1/user_info"],
            "identity": "user",
            "risk": "read",
            "reason": "no token",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)
    assert result["ok"] is True
    assert "HERMES_LARK_CLI_AUTHORIZED" not in captured["env"]


def test_s3_non_im_write_still_reaches_connector_in_worker_shape(monkeypatch, tmp_path):
    # Complement of test_strict_unsupported_write_is_blocked_before_connector:
    # with strict absent (production worker shape) the :2282 gate must stay off
    # and a non-IM api write must reach the connector unchanged.
    from hermes_multitenancy import lark_cli_tool

    _worker_shape_tool_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_LARK_CLI_RUN_TOKEN", "tok-123")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)
    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["POST", "/open-apis/docx/v1/documents"],
            "identity": "user",
            "risk": "write",
            "reason": "non-IM write must not be strict-blocked",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)
    assert result["ok"] is True, result
    assert calls, "connector was never invoked — strict write gate leaked into worker shape"


def _gateway_strict_build(monkeypatch, tmp_path: Path, extra: dict | None = None):
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "alice"
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    node_bin = tmp_path / "node_modules" / ".bin"
    node_bin.mkdir(parents=True)
    real_binary = shared_home / "bin" / "lark-cli-authsidecar"
    real_binary.parent.mkdir(parents=True, exist_ok=True)
    real_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real_binary.chmod(0o755)
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setenv("PATH", os.pathsep.join([str(node_bin), "/usr/bin"]))
    monkeypatch.setenv("HERMES_LARK_CLI_AUTH_PROXY", "http://127.0.0.1:16384")
    monkeypatch.setenv("HERMES_LARK_CLI_PROXY_KEY", "proxy-secret-value")
    monkeypatch.setenv("HERMES_LARK_CLI_APP_ID", "cli_public")
    monkeypatch.delenv("HERMES_MT_SECURITY_AUDIT_PATH", raising=False)
    env = agent_real._build_subprocess_env(
        profile_home, approval_dir=approval_dir, extra=extra
    )
    return env, profile_home


def test_s4_worker_env_carries_audit_signals_but_never_strict_flag(monkeypatch, tmp_path):
    env, profile_home = _gateway_strict_build(monkeypatch, tmp_path)

    assert env["HERMES_MT_SECURITY_AUDIT_ENABLED"] == "1"
    assert env["HERMES_MT_SECURITY_AUDIT_PATH"] == str(
        profile_home / "logs" / "security-audit.jsonl"
    )
    # DECISION PIN (wf_46aff7d5): injecting the strict flag into workers would
    # activate the two-entry IM write allowlist and hard-block every other
    # Feishu write across all tenants. This assertion is the tripwire.
    assert "HERMES_MULTITENANCY_STRICT_CONTEXT" not in env


def test_s5_subprocess_audit_default_prefers_explicit_env(monkeypatch, tmp_path):
    from hermes_multitenancy.agent_real import _core

    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(tmp_path / "custom.jsonl"))
    assert _core._default_security_audit_path_for_subprocess(tmp_path / "p") == (
        tmp_path / "custom.jsonl"
    )
    monkeypatch.delenv("HERMES_MT_SECURITY_AUDIT_PATH")
    assert _core._default_security_audit_path_for_subprocess(tmp_path / "p") == (
        tmp_path / "p" / "logs" / "security-audit.jsonl"
    )


def test_s1_script_channel_grant_lands_on_builder_default_path(monkeypatch, tmp_path):
    # End-to-end over the real seam: the path the builder computes for the
    # worker env is where the script channel's forced granted event must land —
    # a profile-tree file that is RW-bound (durable) inside the sandbox.
    from hermes_multitenancy.agent_real import _core
    from hermes_multitenancy.lark_cli_guard import install_lark_cli_shim
    from hermes_multitenancy import lark_cli_tool

    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "alice"
    workspace = profile_home / "workspace"
    workspace.mkdir(parents=True)
    managed_sources = shared_home / ".hermes-plugin-managed" / ".sources"
    script = managed_sources / "demo" / "scripts" / "probe.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')\n", encoding="utf-8")
    real_binary = shared_home / "bin" / "lark-cli-authsidecar"
    real_binary.parent.mkdir(parents=True, exist_ok=True)
    real_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real_binary.chmod(0o755)
    shim_dir = profile_home / "tmp" / "lark-cli-shim"
    install_lark_cli_shim(shim_dir, real_binary=real_binary)

    monkeypatch.delenv("HERMES_MULTITENANCY_STRICT_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_MT_SECURITY_AUDIT_PATH", raising=False)
    audit_path = _core._default_security_audit_path_for_subprocess(profile_home)
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_PROFILE", "alice")
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    monkeypatch.setenv("LARKSUITE_CLI_AUTH_PROXY", "http://127.0.0.1:16384")
    monkeypatch.setenv("LARKSUITE_CLI_PROXY_KEY", "proxy-secret-value")
    monkeypatch.setenv("LARKSUITE_CLI_APP_ID", "cli_public")
    monkeypatch.setenv("HERMES_LARK_CLI_RUN_TOKEN", "run-token-value")
    monkeypatch.setenv("HERMES_LARK_CLI_REAL_BIN", str(real_binary))

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "script",
            "argv": [str(script)],
            "risk": "read",
            "reason": "durable audit e2e",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)
    assert result.get("exit_code") == 0, result
    assert audit_path == profile_home / "logs" / "security-audit.jsonl"
    events = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(e["event_type"] == "lark_cli.script_channel.granted" for e in events)


def test_s4b_gateway_explicit_audit_off_override_survives_into_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "0")
    env, _profile_home = _gateway_strict_build(monkeypatch, tmp_path)
    assert env["HERMES_MT_SECURITY_AUDIT_ENABLED"] == "0"


def test_s2c_stale_ambient_authorized_never_reaches_child_without_token(
    monkeypatch, tmp_path
):
    # Codex review P1 (stale_authorized_survives_without_token): a dirty ambient
    # HERMES_LARK_CLI_AUTHORIZED must not ride _safe_env into connector children
    # when no run token was minted for this dispatch.
    from hermes_multitenancy import lark_cli_tool

    _worker_shape_tool_env(monkeypatch, tmp_path)
    monkeypatch.delenv("HERMES_LARK_CLI_RUN_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_LARK_CLI_AUTHORIZED", "stale-secret-from-old-run")
    captured = {}

    def fake_run(command, **kwargs):
        captured["env"] = kwargs.get("env")
        return _Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)
    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "api",
            "argv": ["GET", "/open-apis/authen/v1/user_info"],
            "identity": "user",
            "risk": "read",
            "reason": "stale ambient authorized",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)
    assert result["ok"] is True
    assert "HERMES_LARK_CLI_AUTHORIZED" not in captured["env"]


def test_s4c_strict_flag_reintroduced_by_profile_env_or_extra_is_popped(
    monkeypatch, tmp_path
):
    # Codex review P1 (strict_reintroduced_after_allowlist): profile .env and
    # `extra` merge AFTER the parent allowlist copy, so each could resurrect
    # HERMES_MULTITENANCY_STRICT_CONTEXT and activate the dormant strict write
    # regime. The end-of-function pop must hold against both sources.
    profile_home = tmp_path / ".hermes" / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text(
        "HERMES_MULTITENANCY_STRICT_CONTEXT=1\n", encoding="utf-8"
    )
    env, _ = _gateway_strict_build(
        monkeypatch, tmp_path, extra={"HERMES_MULTITENANCY_STRICT_CONTEXT": "1"}
    )
    assert "HERMES_MULTITENANCY_STRICT_CONTEXT" not in env


def test_s4e_inprocess_runtime_overlay_cannot_clobber_builder_owned_controls(
    monkeypatch, tmp_path
):
    # Codex review P0 (profile_control_reload): _apply_runtime_env_for_aiagent
    # reloads the tenant-writable profile .env AFTER the builder sealed the
    # worker controls. A dirty .env must not flip STRICT back on nor blank or
    # redirect the audit sink in os.environ.
    from hermes_multitenancy.agent_real import _core

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    (profile_home / ".env").write_text(
        "HERMES_MULTITENANCY_STRICT_CONTEXT=1\n"
        "HERMES_MT_SECURITY_AUDIT_PATH=   \n"
        "HERMES_MT_SECURITY_AUDIT_ENABLED=\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("HERMES_MULTITENANCY_STRICT_CONTEXT", raising=False)
    sealed_path = str(profile_home / "logs" / "security-audit.jsonl")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", sealed_path)
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")

    cleanup = _core._apply_runtime_env_for_aiagent(profile_home)
    try:
        assert "HERMES_MULTITENANCY_STRICT_CONTEXT" not in os.environ
        assert os.environ["HERMES_MT_SECURITY_AUDIT_PATH"] == sealed_path
        assert os.environ["HERMES_MT_SECURITY_AUDIT_ENABLED"] == "1"
    finally:
        cleanup()


def test_s4f_profile_env_cannot_redirect_or_disable_audit(monkeypatch, tmp_path):
    # Codex review P0 round-5 (profile_audit_override): tenant-writable profile
    # .env setting nonblank PATH=/dev/null + ENABLED=0 must NOT survive into the
    # worker env — the end-of-function seal only trusts extra/gateway sources.
    profile_home = tmp_path / ".hermes" / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text(
        "HERMES_MT_SECURITY_AUDIT_PATH=/dev/null\n"
        "HERMES_MT_SECURITY_AUDIT_ENABLED=0\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("HERMES_MT_SECURITY_AUDIT_ENABLED", raising=False)
    env, ph = _gateway_strict_build(monkeypatch, tmp_path)
    assert env["HERMES_MT_SECURITY_AUDIT_PATH"] == str(
        ph / "logs" / "security-audit.jsonl"
    )
    assert env["HERMES_MT_SECURITY_AUDIT_ENABLED"] == "1"


def test_s4d_blank_audit_values_are_normalized_not_trusted(monkeypatch, tmp_path):
    # Codex review P1 (blank_audit_values_bypass_defaults): whitespace-only
    # inherited values satisfy setdefault but leave the child pointing at
    # /var/log (evaporates in bwrap) with audit effectively off. Blank must
    # normalize to the profile-local default + enabled; s4b pins that a real
    # explicit value ("0") still wins.
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "   ")
    env, profile_home = _gateway_strict_build(monkeypatch, tmp_path)
    assert env["HERMES_MT_SECURITY_AUDIT_ENABLED"] == "1"
    assert env["HERMES_MT_SECURITY_AUDIT_PATH"] == str(
        profile_home / "logs" / "security-audit.jsonl"
    )
