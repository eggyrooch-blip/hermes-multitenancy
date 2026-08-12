from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "build_lark_cli_authsidecar.sh"


def test_build_script_requires_source_dir(tmp_path):
    missing = tmp_path / "missing"
    out = tmp_path / "bin" / "lark-cli-authsidecar"

    proc = subprocess.run(
        [str(SCRIPT)],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "LARK_CLI_SOURCE_DIR": str(missing),
            "HERMES_LARK_CLI_BIN": str(out),
        },
        check=False,
    )

    assert proc.returncode == 2
    assert "source directory not found" in proc.stderr


def test_build_script_uses_authsidecar_tag_and_checks_binary(tmp_path):
    source = tmp_path / "larksuite-cli"
    source.mkdir()
    (source / "go.mod").write_text("module github.com/larksuite/cli\n", encoding="utf-8")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    git = fake_bin / "git"
    git.write_text("#!/bin/sh\necho 4a45e00\n", encoding="utf-8")
    git.chmod(0o755)
    capture = tmp_path / "go-args.txt"
    go = fake_bin / "go"
    go.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$@" > {capture}
out=""
prev=""
for arg in "$@"; do
  if [[ "$prev" == "-o" ]]; then
    out="$arg"
    break
  fi
  prev="$arg"
done
cat > "$out" <<'EOF'
#!/usr/bin/env bash
echo "lark-cli version 1.0.31"
EOF
chmod +x "$out"
""",
        encoding="utf-8",
    )
    go.chmod(0o755)

    out = tmp_path / "bin" / "lark-cli-authsidecar"
    proc = subprocess.run(
        [str(SCRIPT)],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "LARK_CLI_SOURCE_DIR": str(source),
            "HERMES_LARK_CLI_BIN": str(out),
            "LARK_CLI_EXPECTED_VERSION": "1.0.31",
            "LARK_CLI_EXPECTED_SOURCE_HEAD": "4a45e00",
        },
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert out.exists()
    args = capture.read_text(encoding="utf-8")
    assert "build" in args
    assert "-tags" in args
    assert "authsidecar" in args
    assert "-ldflags" in args
    assert "Version=1.0.31" in args
    assert "-o" in args


def test_build_script_rejects_wrong_source_head(tmp_path):
    source = tmp_path / "larksuite-cli"
    source.mkdir()
    (source / "go.mod").write_text("module github.com/larksuite/cli\n", encoding="utf-8")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    git = fake_bin / "git"
    git.write_text("#!/bin/sh\necho wrong-head\n", encoding="utf-8")
    git.chmod(0o755)
    go = fake_bin / "go"
    go.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
out=""
prev=""
for arg in "$@"; do
  if [[ "$prev" == "-o" ]]; then
    out="$arg"
    break
  fi
  prev="$arg"
done
cat > "$out" <<'EOF'
#!/usr/bin/env bash
echo "lark-cli version v0.0.0-20260515081849-4a45e0013945"
EOF
chmod +x "$out"
""",
        encoding="utf-8",
    )
    go.chmod(0o755)

    proc = subprocess.run(
        [str(SCRIPT)],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "LARK_CLI_SOURCE_DIR": str(source),
            "HERMES_LARK_CLI_BIN": str(tmp_path / "bin" / "lark-cli-authsidecar"),
            "LARK_CLI_EXPECTED_VERSION": "1.0.31",
            "LARK_CLI_EXPECTED_SOURCE_HEAD": "4a45e00",
        },
        check=False,
    )

    assert proc.returncode == 3
    assert "source HEAD mismatch" in proc.stderr
