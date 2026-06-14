from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


def _write_executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_real_binary(path: Path, *, sentinel: str) -> Path:
    return _write_executable(
        path,
        "\n".join(
            [
                "#!/bin/sh",
                f"printf '{sentinel}:%s\\n' \"$*\"",
            ]
        )
        + "\n",
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _strict_env(monkeypatch, tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    from hermes_multitenancy import agent_real

    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "alice"
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    node_bin = tmp_path / "node_modules" / ".bin"
    node_bin.mkdir(parents=True)
    real_binary = _make_real_binary(shared_home / "bin" / "lark-cli-authsidecar", sentinel="REAL")
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setenv("PATH", os.pathsep.join([str(node_bin), "/usr/bin"]))
    monkeypatch.setenv("HERMES_LARK_CLI_AUTH_PROXY", "http://127.0.0.1:16384")
    monkeypatch.setenv("HERMES_LARK_CLI_PROXY_KEY", "proxy-secret-value")
    monkeypatch.setenv("HERMES_LARK_CLI_APP_ID", "cli_public")

    env = agent_real._build_subprocess_env(
        profile_home,
        approval_dir=approval_dir,
        extra={"HERMES_FEISHU_USER_OPEN_ID": "ou_secret_open_id"},
    )
    return env, profile_home, real_binary


def test_strict_on_shim_denies_direct_exec_and_audits(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy.lark_cli_guard import HERMES_LARK_CLI_RUN_TOKEN

    env, profile_home, _real_binary = _strict_env(monkeypatch, tmp_path)
    shim_dir = Path(env["PATH"].split(os.pathsep)[0])
    audit_path = Path(env["HERMES_MT_SECURITY_AUDIT_PATH"])
    token = env[HERMES_LARK_CLI_RUN_TOKEN]
    proc = subprocess.run(
        [str(shim_dir / "lark-cli"), "api", "GET", "/open-apis/authen/v1/user_info?access_token=token-secret-value"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert shim_dir.is_relative_to(profile_home)
    assert proc.returncode != 0
    assert "Use the registered lark_cli tool." in proc.stderr
    assert proc.stdout == ""
    assert token not in proc.stderr
    assert "proxy-secret-value" not in proc.stderr
    assert "ou_secret_open_id" not in proc.stderr
    rows = _read_jsonl(audit_path)
    assert rows[-1]["event_type"] == "lark_cli.direct_exec.denied"
    assert rows[-1]["command_name"] == "lark-cli"
    assert rows[-1]["profile"] == "alice"
    assert rows[-1]["open_id_hash"] == hashlib.sha256(b"ou_secret_open_id").hexdigest()[:12]
    serialized = json.dumps(rows[-1], ensure_ascii=False)
    assert "token-secret-value" not in serialized
    assert "proxy-secret-value" not in serialized
    assert token not in serialized
    assert "ou_secret_open_id" not in serialized


def test_strict_on_shim_authorized_execs_real_binary(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy.lark_cli_guard import (
        HERMES_LARK_CLI_AUTHORIZED,
        HERMES_LARK_CLI_RUN_TOKEN,
    )

    env, _profile_home, _real_binary = _strict_env(monkeypatch, tmp_path)
    shim_dir = Path(env["PATH"].split(os.pathsep)[0])
    env = dict(env)
    env[HERMES_LARK_CLI_AUTHORIZED] = env[HERMES_LARK_CLI_RUN_TOKEN]

    proc = subprocess.run(
        [str(shim_dir / "lark"), "doctor", "--verbose"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert "REAL:doctor --verbose" in proc.stdout
    assert proc.stderr == ""


def test_strict_off_build_subprocess_env_keeps_existing_path_behavior(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy import agent_real
    from hermes_multitenancy.lark_cli_guard import (
        HERMES_LARK_CLI_AUTHORIZED,
        HERMES_LARK_CLI_REAL_BIN,
        HERMES_LARK_CLI_RUN_TOKEN,
    )

    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "alice"
    approval_dir = tmp_path / "approval"
    approval_dir.mkdir()
    shared_bin = str(agent_real._resolve_shared_hermes_home(profile_home) / "bin")
    node_bin = str(tmp_path / "node_modules" / ".bin")
    monkeypatch.delenv("HERMES_MULTITENANCY_STRICT_CONTEXT", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([node_bin, shared_bin, "/usr/bin"]))

    env = agent_real._build_subprocess_env(profile_home, approval_dir=approval_dir)
    path_parts = env["PATH"].split(os.pathsep)

    assert path_parts[0] == shared_bin
    assert path_parts.count(shared_bin) == 1
    assert path_parts.index(node_bin) > path_parts.index(shared_bin)
    assert HERMES_LARK_CLI_RUN_TOKEN not in env
    assert HERMES_LARK_CLI_AUTHORIZED not in env
    assert HERMES_LARK_CLI_REAL_BIN not in env
    assert not (profile_home / "tmp" / "lark-cli-shim").exists()


def test_append_security_event_hashes_open_id_and_drops_secret_fields(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy.security_audit import append_security_event

    audit_path = tmp_path / "security-audit.jsonl"
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    # Audit now defaults OFF; this test asserts audit CONTENT so it opts in.
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_ENABLED", "1")

    append_security_event(
        event_type="lark_cli.direct_exec.denied",
        profile="alice",
        open_id="ou_secret_open_id",
        proxy_key="proxy-secret-value",
        run_token="run-token-secret",
        reason="denied",
    )

    rows = _read_jsonl(audit_path)
    assert rows == [
        {
            "@timestamp": rows[0]["@timestamp"],
            "event_type": "lark_cli.direct_exec.denied",
            "profile": "alice",
            "open_id_hash": hashlib.sha256(b"ou_secret_open_id").hexdigest()[:12],
            "reason": "denied",
        }
    ]


def test_lark_cli_tool_strict_mode_copies_run_token_into_authorized_env(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy import lark_cli_tool
    from hermes_multitenancy.lark_cli_guard import (
        HERMES_LARK_CLI_REAL_BIN,
        HERMES_LARK_CLI_RUN_TOKEN,
        install_lark_cli_shim,
    )

    profile_home = tmp_path / "profile"
    workspace = profile_home / "workspace"
    workspace.mkdir(parents=True)
    shim_dir = profile_home / "tmp" / "lark-cli-shim"
    real_binary = _make_real_binary(tmp_path / "bin" / "real-lark-cli", sentinel="TOOL")
    install_lark_cli_shim(shim_dir, real_binary=real_binary)
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("PATH", os.pathsep.join([str(shim_dir), "/usr/bin"]))
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(shim_dir / "lark-cli"))
    monkeypatch.setenv(HERMES_LARK_CLI_REAL_BIN, str(real_binary))
    monkeypatch.setenv(HERMES_LARK_CLI_RUN_TOKEN, "run-token-secret")

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["doctor"],
            "risk": "read",
            "reason": "smoke",
        }
    )
    result = raw if isinstance(raw, dict) else json.loads(raw)

    assert result["ok"] is True
    assert "TOOL:doctor" in result["stdout"]
