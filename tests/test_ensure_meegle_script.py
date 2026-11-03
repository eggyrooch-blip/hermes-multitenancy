from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "ensure-meegle.sh"


def _native_name() -> str:
    return {
        ("Darwin", "arm64"): "meegle-darwin-arm64",
        ("Darwin", "x86_64"): "meegle-darwin-x64",
        ("Linux", "x86_64"): "meegle-linux-x64",
        ("Linux", "aarch64"): "meegle-linux-arm64",
    }[(os.uname().sysname, os.uname().machine)]


def _binary(path: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
    path.chmod(0o755)


def test_ensure_meegle_upgrades_stale_direct_binary(tmp_path):
    prefix = tmp_path / "prefix"
    meegle = prefix / "bin/meegle"
    _binary(meegle, "1.0.12")
    stage = tmp_path / "stage"
    staged_bin = stage / "lib/node_modules/@lark-project/meegle/bin" / _native_name()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "npm-calls"
    npm = fake_bin / "npm"
    npm.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {calls!s}\n"
        f"mkdir -p {staged_bin.parent!s}\n"
        f"printf '#!/bin/sh\\necho 1.0.19\\n' > {staged_bin!s}\n"
        f"chmod +x {staged_bin!s}\n",
        encoding="utf-8",
    )
    npm.chmod(0o755)

    proc = subprocess.run(
        [str(SCRIPT)], text=True, capture_output=True, check=False,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
             "HERMES_MEEGLE_PREFIX": str(prefix), "HERMES_MEEGLE_STAGE": str(stage)},
    )

    assert proc.returncode == 0, proc.stderr
    assert subprocess.check_output([str(meegle), "version"], text=True).strip() == "1.0.19"
    assert calls.read_text(encoding="utf-8").splitlines() == [
        f"i -g --prefix {stage} @lark-project/meegle@1.0.19"
    ]


def test_ensure_meegle_exact_version_is_noop(tmp_path):
    prefix = tmp_path / "prefix"
    meegle = prefix / "bin/meegle"
    _binary(meegle, "1.0.19")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "npm-calls"
    npm = fake_bin / "npm"
    npm.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {calls!s}\nexit 0\n", encoding="utf-8")
    npm.chmod(0o755)

    proc = subprocess.run(
        [str(SCRIPT)], capture_output=True, check=False,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
             "HERMES_MEEGLE_PREFIX": str(prefix)},
    )

    assert proc.returncode == 0
    assert not calls.exists()


def test_ensure_meegle_failed_upgrade_preserves_existing_binary(tmp_path):
    prefix = tmp_path / "prefix"
    meegle = prefix / "bin/meegle"
    old = b"#!/bin/sh\necho 1.0.12\n"
    _binary(meegle, "1.0.12")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    npm = fake_bin / "npm"
    npm.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    npm.chmod(0o755)

    proc = subprocess.run(
        [str(SCRIPT)], capture_output=True, check=False,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
             "HERMES_MEEGLE_PREFIX": str(prefix)},
    )

    assert proc.returncode == 0
    assert meegle.read_bytes() == old


def test_ensure_meegle_wrong_staged_version_preserves_existing_binary(tmp_path):
    prefix = tmp_path / "prefix"
    meegle = prefix / "bin/meegle"
    old = b"#!/bin/sh\necho 1.0.12\n"
    _binary(meegle, "1.0.12")
    stage = tmp_path / "stage"
    staged_bin = stage / "lib/node_modules/@lark-project/meegle/bin" / _native_name()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    npm = fake_bin / "npm"
    npm.write_text(
        f"#!/bin/sh\nmkdir -p {staged_bin.parent!s}\n"
        f"printf '#!/bin/sh\\necho 9.9.9\\n' > {staged_bin!s}\n"
        f"chmod +x {staged_bin!s}\n",
        encoding="utf-8",
    )
    npm.chmod(0o755)

    proc = subprocess.run(
        [str(SCRIPT)], capture_output=True, check=False,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
             "HERMES_MEEGLE_PREFIX": str(prefix), "HERMES_MEEGLE_STAGE": str(stage)},
    )

    assert proc.returncode == 0
    assert meegle.read_bytes() == old


def test_ensure_meegle_check_never_installs(tmp_path):
    prefix = tmp_path / "prefix"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "npm-calls"
    npm = fake_bin / "npm"
    npm.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {calls!s}\nexit 1\n", encoding="utf-8")
    npm.chmod(0o755)

    proc = subprocess.run(
        [str(SCRIPT), "--check"], capture_output=True, check=False,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
             "HERMES_MEEGLE_PREFIX": str(prefix)},
    )

    assert proc.returncode != 0
    assert not calls.exists()
