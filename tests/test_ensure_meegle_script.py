from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "ensure-meegle.sh"


def test_ensure_meegle_upgrades_stale_direct_binary(tmp_path):
    prefix = tmp_path / "prefix"
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    meegle = bin_dir / "meegle"
    meegle.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    meegle.chmod(0o755)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "npm-calls"
    npm = fake_bin / "npm"
    npm.write_text(
        f"""#!/bin/sh
printf '%s\\n' "$*" >> {calls!s}
if [ "$1" = "ls" ]; then exit 1; fi
mkdir -p {bin_dir!s}
printf '#!/bin/sh\\nexit 0\\n' > {meegle!s}
chmod +x {meegle!s}
""",
        encoding="utf-8",
    )
    npm.chmod(0o755)
    proc = subprocess.run(
        [str(SCRIPT)], text=True, capture_output=True, check=False,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}", "HERMES_MEEGLE_PREFIX": str(prefix)},
    )
    assert proc.returncode == 0, proc.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        f"ls -g --prefix {prefix} --depth=0 @lark-project/meegle@1.0.19",
        f"i -g --prefix {prefix} @lark-project/meegle@1.0.19",
    ]


def test_ensure_meegle_exact_version_is_noop(tmp_path):
    prefix = tmp_path / "prefix"
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    meegle = bin_dir / "meegle"
    meegle.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    meegle.chmod(0o755)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls = tmp_path / "npm-calls"
    npm = fake_bin / "npm"
    npm.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {calls!s}\nexit 0\n", encoding="utf-8")
    npm.chmod(0o755)

    proc = subprocess.run(
        [str(SCRIPT)], capture_output=True, check=False,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}", "HERMES_MEEGLE_PREFIX": str(prefix)},
    )

    assert proc.returncode == 0
    assert meegle.exists()
    assert calls.read_text(encoding="utf-8").splitlines() == [
        f"ls -g --prefix {prefix} --depth=0 @lark-project/meegle@1.0.19"
    ]


def test_ensure_meegle_failed_upgrade_preserves_existing_binary(tmp_path):
    prefix = tmp_path / "prefix"
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    meegle = bin_dir / "meegle"
    old = b"#!/bin/sh\necho old\n"
    meegle.write_bytes(old)
    meegle.chmod(0o755)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    npm = fake_bin / "npm"
    npm.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    npm.chmod(0o755)

    proc = subprocess.run(
        [str(SCRIPT)], capture_output=True, check=False,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}", "HERMES_MEEGLE_PREFIX": str(prefix)},
    )

    assert proc.returncode == 0
    assert meegle.read_bytes() == old
