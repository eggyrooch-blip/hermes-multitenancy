"""Tests for the kep-cli family profile shim."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from hermes_multitenancy.kep_cli_guard import KEP_CLI_SHIM_NAMES, install_kep_cli_shim


def test_install_writes_executable_shims_for_present_binaries(tmp_path):
    real_bin = tmp_path / "bin"
    real_bin.mkdir()
    for name in KEP_CLI_SHIM_NAMES:  # the profile "has" all of them
        (real_bin / name).write_text("#!/bin/sh\n", encoding="utf-8")
        (real_bin / name).chmod(0o755)
    shim_dir = tmp_path / "shim"
    install_kep_cli_shim(shim_dir, real_bin_dir=real_bin)
    for name in KEP_CLI_SHIM_NAMES:
        p = shim_dir / name
        assert p.is_file() and os.access(p, os.X_OK)
    assert "kep-cli" not in KEP_CLI_SHIM_NAMES  # the installer has no --profile flag
    assert len(KEP_CLI_SHIM_NAMES) == 5


def test_only_shims_binaries_that_exist(tmp_path):
    # a profile with only hades-cli installed → only hades-cli gets a shim
    real_bin = tmp_path / "bin"
    real_bin.mkdir()
    (real_bin / "hades-cli").write_text("#!/bin/sh\n", encoding="utf-8")
    (real_bin / "hades-cli").chmod(0o755)
    shim_dir = tmp_path / "shim"
    install_kep_cli_shim(shim_dir, real_bin_dir=real_bin)
    assert (shim_dir / "hades-cli").is_file()
    assert not (shim_dir / "kep-auth").exists()  # not installed → not shimmed


def _fake_real_bin(tmp_path):
    """A stub 'real' kep-auth that just echoes the args it received."""
    real_bin = tmp_path / "bin"
    real_bin.mkdir()
    stub = real_bin / "kep-auth"
    stub.write_text("#!/bin/sh\necho \"ARGS: $*\"\n", encoding="utf-8")
    stub.chmod(0o755)
    return real_bin


def _run_shim(shim_dir, *, env):
    return subprocess.run([str(shim_dir / "kep-auth"), "status"], env=env, capture_output=True, text=True)


def test_injects_profile_from_kep_profile_env(tmp_path):
    real_bin = _fake_real_bin(tmp_path)
    shim_dir = tmp_path / "shim"
    install_kep_cli_shim(shim_dir, real_bin_dir=real_bin)
    out = _run_shim(shim_dir, env={**os.environ, "KEP_PROFILE": "p_kep"}).stdout
    assert "ARGS: --profile p_kep status" in out


def test_falls_back_to_hermes_profile(tmp_path):
    real_bin = _fake_real_bin(tmp_path)
    shim_dir = tmp_path / "shim"
    install_kep_cli_shim(shim_dir, real_bin_dir=real_bin)
    env = {k: v for k, v in os.environ.items() if k != "KEP_PROFILE"}
    env["HERMES_PROFILE"] = "p_hermes"
    out = _run_shim(shim_dir, env=env).stdout
    assert "ARGS: --profile p_hermes status" in out


def test_recovers_profile_from_home_when_envs_dropped(tmp_path):
    # the crux: no KEP_PROFILE / HERMES_PROFILE → recover from $HOME=<profiles>/<name>/home
    real_bin = _fake_real_bin(tmp_path)
    shim_dir = tmp_path / "shim"
    install_kep_cli_shim(shim_dir, real_bin_dir=real_bin)
    home = tmp_path / "profiles" / "feishu_xyz" / "home"
    home.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k not in ("KEP_PROFILE", "HERMES_PROFILE")}
    env["HOME"] = str(home)
    out = _run_shim(shim_dir, env=env).stdout
    assert "ARGS: --profile feishu_xyz status" in out


def test_does_not_double_inject_when_profile_already_present(tmp_path):
    real_bin = _fake_real_bin(tmp_path)
    shim_dir = tmp_path / "shim"
    install_kep_cli_shim(shim_dir, real_bin_dir=real_bin)
    env = {**os.environ, "KEP_PROFILE": "p_kep"}
    r = subprocess.run([str(shim_dir / "kep-auth"), "--profile", "explicit", "status"],
                       env=env, capture_output=True, text=True)
    assert "ARGS: --profile explicit status" in r.stdout
    assert r.stdout.count("--profile") == 1  # no second --profile injected


def test_shim_calls_real_by_absolute_path_no_recursion(tmp_path):
    # the shim's REAL path is absolute → it never re-resolves itself via PATH
    real_bin = _fake_real_bin(tmp_path)
    shim_dir = tmp_path / "shim"
    install_kep_cli_shim(shim_dir, real_bin_dir=real_bin)
    body = (shim_dir / "kep-auth").read_text()
    assert f"REAL='{real_bin / 'kep-auth'}'" in body or str(real_bin / "kep-auth") in body
