"""mode="script" channel: AiDock-distributed skill scripts may shell out to lark-cli.

Trust boundary (sunke 2026-08-31): a script whose symlink-resolved bytes live
under a read-only AiDock/plugin distribution root is internally reviewed and runs
without content restriction. A script that resolves into the RW profile tree,
workspace, or tmp is NOT trusted — that keeps run-authored terminal code from
borrowing the credential grant. The registered lark_cli tool (which holds the run
token) launches it with a trusted interpreter and a freshly-written shim.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_multitenancy.lark_cli_guard import install_lark_cli_shim
from hermes_multitenancy.lark_cli_tool import LARK_CLI_SCHEMA, _handle_lark_cli_execute

PROBE_BODY = """\
import subprocess
import sys

r = subprocess.run(["lark-cli", "auth", "status"], capture_output=True, text=True)
sys.stdout.write(r.stdout)
sys.stderr.write(r.stderr)
sys.exit(0 if r.returncode == 0 else 7)
"""


def _write(path: Path, body: str, *, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


def _make_real_binary(path: Path) -> Path:
    return _write(path, "#!/bin/sh\nprintf 'REAL:%s\\n' \"$*\"\n", mode=0o755)


@pytest.fixture()
def script_env(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "alice"
    workspace = profile_home / "workspace"
    workspace.mkdir(parents=True)
    # AiDock distribution roots (read-only in prod; the trust boundary).
    managed_sources = shared_home / ".hermes-plugin-managed" / ".sources"
    shared_skills = shared_home / "skills"
    managed_sources.mkdir(parents=True)
    shared_skills.mkdir(parents=True)
    real_binary = _make_real_binary(shared_home / "bin" / "lark-cli-authsidecar")
    shim_dir = profile_home / "tmp" / "lark-cli-shim"
    install_lark_cli_shim(shim_dir, real_binary=real_binary)

    audit_path = tmp_path / "audit" / "security.jsonl"

    # Production worker shape (2026-08-31 incident): the tool process carries
    # the run token but NOT HERMES_MULTITENANCY_STRICT_CONTEXT — that var lives
    # only in the gateway process. The channel must work in exactly this env.
    monkeypatch.delenv("HERMES_MULTITENANCY_STRICT_CONTEXT", raising=False)
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
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    monkeypatch.delenv("HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY", raising=False)
    monkeypatch.delenv("HERMES_LARK_CLI_AUTHORIZED", raising=False)

    return {
        "shared_home": shared_home,
        "profile_home": profile_home,
        "workspace": workspace,
        "shim_dir": shim_dir,
        "managed_sources": managed_sources,
        "shared_skills": shared_skills,
        "audit_path": audit_path,
    }


def _run_script_tool(script_path: Path | str, *args: str, risk: str = "write") -> dict:
    raw = _handle_lark_cli_execute(
        {
            "mode": "script",
            "argv": [str(script_path), *args],
            "risk": risk,
            "reason": "script channel test",
        }
    )
    return json.loads(raw)


def _audit_events(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    return [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]


def test_schema_routes_ordinary_packaged_script_commands_to_script_mode() -> None:
    schema = json.dumps(LARK_CLI_SCHEMA, ensure_ascii=False)

    assert "running any file it distributes" in schema
    assert "any interpreter or direct execution" in schema
    assert "in any subdirectory" in schema
    assert "resolve that path relative to its SKILL.md" in schema
    assert "do not use terminal/execute_code" in schema


def test_s1_plugin_managed_symlinked_skill_is_granted(script_env) -> None:
    # The kep-ub-gen shape: profile skills symlink → managed plugin source.
    pool_script = _write(
        script_env["managed_sources"] / "keep-product" / "kep-ub-gen" / "scripts" / "probe.py",
        PROBE_BODY,
    )
    skill_link = script_env["profile_home"] / "skills" / "kep-ub-gen"
    skill_link.parent.mkdir(parents=True, exist_ok=True)
    skill_link.symlink_to(pool_script.parent.parent)
    result = _run_script_tool(skill_link / "scripts" / "probe.py")
    assert result.get("exit_code") == 0, result
    assert "REAL:auth status" in result.get("stdout_redacted", "")


def test_s1b_shared_skills_direct_path_is_granted_and_audited(script_env) -> None:
    script = _write(script_env["shared_skills"] / "demo" / "scripts" / "probe.py", PROBE_BODY)
    result = _run_script_tool(script)
    assert result.get("exit_code") == 0, result
    events = _audit_events(script_env["audit_path"])
    granted = [e for e in events if e["event_type"] == "lark_cli.script_channel.granted"]
    assert granted, events
    ev = granted[-1]
    # P1: audit carries the path (hash) and a content hash, not just a basename.
    assert ev.get("command_name") == "probe.py"
    assert ev.get("path_hash") and ev.get("command_hash")


def test_profile_tree_script_is_refused(script_env) -> None:
    # P0-2: profile skills dir is RW in the sandbox; a plain file there is
    # terminal-writable and must NOT be trusted even though it is "in skills".
    inside = _write(
        script_env["profile_home"] / "skills" / "demo" / "scripts" / "probe.py", PROBE_BODY
    )
    result = _run_script_tool(inside)
    assert "AiDock" in result.get("error", "")
    assert "exit_code" not in result


def test_s3_workspace_script_is_refused(script_env) -> None:
    outside = _write(script_env["workspace"] / "probe.py", PROBE_BODY)
    result = _run_script_tool(outside)
    assert "AiDock" in result.get("error", "")
    assert "exit_code" not in result


def test_s4_symlink_escape_to_workspace_is_refused(script_env) -> None:
    target = _write(script_env["workspace"] / "evil.py", PROBE_BODY)
    link = script_env["profile_home"] / "skills" / "demo" / "evil.py"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    result = _run_script_tool(link)
    assert "AiDock" in result.get("error", "")
    assert "exit_code" not in result


def test_s5_sh_script_runs_via_bash(script_env) -> None:
    # No content/extension restriction (sunke 2026-08-31): a non-executable .sh
    # from a distribution root runs through /bin/bash and reaches lark-cli.
    script = _write(
        script_env["managed_sources"] / "demo" / "scripts" / "probe.sh",
        "#!/bin/sh\nlark-cli auth status\n",
        mode=0o644,
    )
    result = _run_script_tool(script)
    assert result.get("exit_code") == 0, result
    assert "REAL:auth status" in result.get("stdout_redacted", "")


@pytest.mark.parametrize(
    ("source_rel", "copy_rel"),
    [
        (Path("skills/demo/scripts/probe.py"), Path("skills/demo/scripts/probe.py")),
        (Path("scripts/probe.py"), Path("plugins/demo/scripts/probe.py")),
    ],
)
def test_codex_copy_maps_to_identical_trusted_plugin_source(
    script_env, monkeypatch, source_rel: Path, copy_rel: Path
) -> None:
    plugin = script_env["managed_sources"] / "demo"
    body = """\
import os
import subprocess
import sys

r = subprocess.run(["lark-cli", "auth", "status"], capture_output=True, text=True)
sys.stdout.write(r.stdout)
sys.stderr.write(r.stderr)
print(f"ANCHORS:{bool(os.getenv('CODEX_HOME'))}:{bool(os.getenv('HERMES_CODEX_PLUGIN_SOURCE'))}")
sys.exit(0 if r.returncode == 0 else 7)
"""
    _write(plugin / source_rel, body)
    codex_home = script_env["profile_home"] / "codex-home"
    copied = _write(codex_home / copy_rel, body)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("HERMES_CODEX_PLUGIN_SOURCE", str(plugin))

    result = _run_script_tool(copied)

    assert result.get("exit_code") == 0, result
    assert "REAL:auth status" in result.get("stdout_redacted", "")
    assert "ANCHORS:False:False" in result.get("stdout_redacted", "")

    copied.write_text("print('tampered')\n", encoding="utf-8")
    rejected = _run_script_tool(copied)
    assert rejected.get("exit_code") is None
    assert rejected.get("error_code") == "FEISHU_REQUEST_INVALID"


def test_s5b_executable_no_suffix_runs_directly(script_env) -> None:
    script = _write(
        script_env["managed_sources"] / "demo" / "scripts" / "probe",
        "#!/bin/sh\nlark-cli auth status\n",
        mode=0o755,
    )
    result = _run_script_tool(script)
    assert result.get("exit_code") == 0, result
    assert "REAL:auth status" in result.get("stdout_redacted", "")


def test_s1c_strict_env_present_also_granted(script_env, monkeypatch) -> None:
    # Both env shapes pass: gateway-shaped (strict var present) and
    # worker-shaped (absent, the fixture default that pins the incident).
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    script = _write(script_env["managed_sources"] / "demo" / "scripts" / "probe.py", PROBE_BODY)
    result = _run_script_tool(script)
    assert result.get("exit_code") == 0, result


def test_p0_1_readonly_expert_denied_script(script_env, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY", "1")
    script = _write(script_env["managed_sources"] / "demo" / "scripts" / "probe.py", PROBE_BODY)
    result = _run_script_tool(script, risk="read")
    assert "read-only" in result.get("error", "")
    assert "exit_code" not in result


def test_p0_2_planted_python3_is_ignored(script_env, monkeypatch) -> None:
    # A decoy `python3` on the inherited PATH must never run: the handler pins
    # sys.executable. If it were used, the sentinel would appear and probe would
    # not print REAL.
    decoy_dir = script_env["profile_home"] / "tmp" / "decoy"
    decoy_dir.mkdir(parents=True)
    _write(
        decoy_dir / "python3",
        "#!/bin/sh\necho PLANTED_INTERPRETER_RAN\n",
        mode=0o755,
    )
    monkeypatch.setenv("PATH", os.pathsep.join([str(decoy_dir), "/usr/bin", "/bin"]))
    script = _write(script_env["managed_sources"] / "demo" / "scripts" / "probe.py", PROBE_BODY)
    result = _run_script_tool(script)
    assert result.get("exit_code") == 0, result
    assert "PLANTED_INTERPRETER_RAN" not in result.get("stdout_redacted", "")
    assert "REAL:auth status" in result.get("stdout_redacted", "")


def test_s2_bare_shim_exec_still_denied(script_env) -> None:
    completed = subprocess.run(
        [str(script_env["shim_dir"] / "lark-cli"), "auth", "status"],
        capture_output=True,
        text=True,
        env={**os.environ},
        check=False,
    )
    assert completed.returncode == 126
    assert "Direct execution denied" in completed.stderr
    assert 'mode="script"' in completed.stderr


def test_s6_without_run_token_channel_fails_closed(script_env, monkeypatch) -> None:
    monkeypatch.delenv("HERMES_LARK_CLI_RUN_TOKEN", raising=False)
    script = _write(script_env["managed_sources"] / "demo" / "scripts" / "probe.py", PROBE_BODY)
    result = _run_script_tool(script)
    assert "strict profile runtime" in result.get("error", "")
    assert "exit_code" not in result


def test_s6b_script_without_grant_hits_shim_126(script_env) -> None:
    # Mutation-equivalent: same script + shim, but no AUTHORIZED → in-script
    # lark-cli call is denied. Proves S1 passes because of the injected grant.
    script = _write(script_env["managed_sources"] / "demo" / "scripts" / "probe.py", PROBE_BODY)
    env = {**os.environ}
    env.pop("HERMES_LARK_CLI_AUTHORIZED", None)
    env["PATH"] = os.pathsep.join([str(script_env["shim_dir"]), env.get("PATH", "")])
    completed = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, env=env, check=False
    )
    assert completed.returncode == 7
    assert "Direct execution denied" in completed.stderr


def test_relative_path_resolves_against_profile_skills_root(script_env) -> None:
    # 2026-09-04 kep-ub-gen: the tool schema says "resolve relative to its SKILL.md", so the
    # model spells the script as `<skill>/scripts/x.py`; that must resolve via the profile
    # skills symlink into the trusted plugin source, not only against the workspace.
    pool_script = _write(
        script_env["managed_sources"] / "keep-product" / "kep-ub-gen" / "scripts" / "probe.py",
        PROBE_BODY,
    )
    skill_link = script_env["profile_home"] / "skills" / "kep-ub-gen"
    skill_link.parent.mkdir(parents=True, exist_ok=True)
    skill_link.symlink_to(pool_script.parent.parent)
    assert not (script_env["workspace"] / "kep-ub-gen").exists()  # workspace lookup would miss

    result = _run_script_tool("kep-ub-gen/scripts/probe.py")
    assert result.get("exit_code") == 0, result
    assert "REAL:auth status" in result.get("stdout_redacted", "")


def test_relative_path_resolves_against_shared_skills_root(script_env) -> None:
    pool_script = _write(
        script_env["shared_skills"] / "kep-ub-archive" / "scripts" / "probe.py",
        PROBE_BODY,
    )
    assert pool_script.is_file()
    result = _run_script_tool("kep-ub-archive/scripts/probe.py")
    assert result.get("exit_code") == 0, result


def test_relative_path_outside_every_root_still_not_found(script_env) -> None:
    # workspace-planted code must not become reachable through the skills fallbacks
    _write(script_env["workspace"] / "evil" / "scripts" / "probe.py", PROBE_BODY)
    result = _run_script_tool("nope/scripts/probe.py")
    assert result.get("error") == "script not found", result
