from __future__ import annotations

import os
from pathlib import Path
import subprocess


def test_installer_mounts_optional_litellm_billing_environment(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    hermes_python = fake_bin / "python"
    systemctl = fake_bin / "systemctl"
    meegle = home / ".local" / "bin" / "meegle"
    for executable in (hermes_python, systemctl, meegle):
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)

    subprocess.run(
        [str(repo / "deploy" / "install-gateway-dropins.sh")],
        check=True,
        env={
            **os.environ,
            "HOME": str(home),
            "HERMES_PYTHON": str(hermes_python),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )

    dropin = (
        home
        / ".config"
        / "systemd"
        / "user"
        / "hermes-gateway.service.d"
        / "55-litellm-billing.conf"
    )
    assert dropin.read_text() == (
        "[Service]\n"
        "EnvironmentFile=-%h/.hermes/litellm-billing.env\n"
    )
