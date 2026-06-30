from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _write_fake_real_bin(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

MODE = os.environ.get("FAKE_KEP_MODE", "success")
if MODE == "argv":
    sys.stdout.write(json.dumps(sys.argv[1:], ensure_ascii=False))
    raise SystemExit(0)
if MODE == "exit77":
    sys.stderr.write("state: not logged in\\n")
    raise SystemExit(77)
if MODE == "phrase":
    sys.stdout.write("run kep-auth login\\n")
    raise SystemExit(3)
if MODE == "forbidden403":
    sys.stderr.write("Error: 认证失败: 接口禁止访问 (HTTP 403)\\n")
    raise SystemExit(3)
sys.stdout.buffer.write(b"payload\\x00ok\\n")
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_install_kep_cli_shim_inserts_profile_before_subcommand(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "ocean-cli")
    shim_dir = tmp_path / "shim"
    [wrapper] = install_kep_cli_shim(shim_dir, real_bins={"ocean-cli": str(real_bin)})

    env = os.environ.copy()
    env["KEP_PROFILE"] = "alice"
    env["FAKE_KEP_MODE"] = "argv"
    result = subprocess.run(
        [str(wrapper), "status", "--env", "pre"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == ["--profile", "alice", "status", "--env", "pre"]
    assert "【Hermes】" not in result.stderr


def test_install_kep_cli_shim_preserves_explicit_profile(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "hades-cli")
    shim_dir = tmp_path / "shim"
    [wrapper] = install_kep_cli_shim(shim_dir, real_bins={"hades-cli": str(real_bin)})

    env = os.environ.copy()
    env["KEP_PROFILE"] = "alice"
    env["FAKE_KEP_MODE"] = "argv"
    result = subprocess.run(
        [str(wrapper), "--profile", "manual", "status"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == ["--profile", "manual", "status"]


def test_install_kep_cli_shim_relays_auth_failure_with_stable_exit_code(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "kep-auth")
    shim_dir = tmp_path / "shim"
    [wrapper] = install_kep_cli_shim(shim_dir, real_bins={"kep-auth": str(real_bin)})

    env = os.environ.copy()
    env["FAKE_KEP_MODE"] = "exit77"
    result = subprocess.run(
        [str(wrapper), "--env", "pre", "status"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 77
    assert "【Hermes】kep-cli pre 需要授权" in result.stderr


def test_install_kep_cli_shim_detects_auth_failure_from_output(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "kep-dune-cli")
    shim_dir = tmp_path / "shim"
    [wrapper] = install_kep_cli_shim(shim_dir, real_bins={"kep-dune-cli": str(real_bin)})

    env = os.environ.copy()
    env["FAKE_KEP_MODE"] = "phrase"
    result = subprocess.run(
        [str(wrapper), "status"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 3
    assert "【Hermes】kep-cli online 需要授权" in result.stderr


def test_install_kep_cli_shim_relays_forbidden_as_permission_denied(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "ocean-cli")
    shim_dir = tmp_path / "shim"
    [wrapper] = install_kep_cli_shim(shim_dir, real_bins={"ocean-cli": str(real_bin)})

    env = os.environ.copy()
    env["FAKE_KEP_MODE"] = "forbidden403"
    result = subprocess.run(
        [str(wrapper), "--env", "pre", "jd-adjust", "jd-adjust-list"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 3
    assert "接口禁止访问 (HTTP 403)" in result.stderr
    assert "【Hermes】kep-cli pre 接口禁止访问" in result.stderr
    assert "当前账号已登录但没有该接口/数据权限" in result.stderr
    assert "需要授权" not in result.stderr


def test_install_kep_cli_shim_success_stdout_matches_real_binary(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    real_bin = _write_fake_real_bin(tmp_path / "real-bin" / "kep-badge-cli")
    shim_dir = tmp_path / "shim"
    [wrapper] = install_kep_cli_shim(shim_dir, real_bins={"kep-badge-cli": str(real_bin)})

    direct = subprocess.run(
        [str(real_bin), "fetch", "--id", "42"],
        capture_output=True,
        check=False,
    )
    wrapped = subprocess.run(
        [str(wrapper), "fetch", "--id", "42"],
        capture_output=True,
        check=False,
    )

    assert wrapped.returncode == direct.returncode == 0
    assert wrapped.stdout == direct.stdout
    assert wrapped.stderr == direct.stderr
    assert "【Hermes】".encode("utf-8") not in wrapped.stderr


def test_install_kep_cli_shim_blocks_recursive_real_binary(tmp_path: Path):
    from hermes_multitenancy.kep_cli_guard import install_kep_cli_shim

    shim_dir = tmp_path / "shim"
    [wrapper] = install_kep_cli_shim(
        shim_dir,
        real_bins={"kep-auth": str(shim_dir / "kep-auth")},
    )

    result = subprocess.run(
        [str(wrapper), "status"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 127
    assert "shim" in result.stderr.lower()
    assert "real" in result.stderr.lower()
