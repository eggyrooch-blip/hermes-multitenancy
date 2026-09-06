from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_makefile_exposes_skills_uat_targets_with_strict_completion_gate():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert ".PHONY:" in makefile
    assert "skills-uat" in makefile
    assert "skills-uat-strict" in makefile
    assert "scripts/skills_uat_matrix.py" in makefile
    assert "scripts/skills_second_problem_trace.py" in makefile
    assert "scripts/historical_feedback_image_review.py" in makefile
    assert "scripts/gateway_process_evidence.py" in makefile
    assert "scripts/skills_uat_completion_audit.py" in makefile
    assert "--feedback-artifact-label" in makefile
    assert "--feedback-artifact-scenario" in makefile
    assert "HERMES_HISTORICAL_FEEDBACK_IMAGE_REJECTION_SOURCE" in makefile
    assert "HERMES_HISTORICAL_FEEDBACK_IMAGE_REJECTION_LABEL ?= Image \\#1" in makefile
    assert "historical-image-reviews.json" in makefile
    assert "--require-complete" in makefile


def test_makefile_skills_uat_stops_after_first_required_command_failure():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "skills-uat:" in makefile
    assert "set -e;" in makefile


def _executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def test_canonical_test_runner_forwards_targets_and_preserves_ci_non_root(tmp_path):
    runner = ROOT / "scripts" / "run_tests.sh"
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "test:\n\tscripts/run_tests.sh" in makefile

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "argv.log"
    _executable(fake_bin / "uv", f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {log!s}\n")
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    env.pop("CI", None)
    env.pop("HERMES_HOME", None)
    subprocess.run([runner, "tests/test_feishu_trusted_ingress.py"], cwd=ROOT, env=env, check=True)
    assert log.read_text(encoding="utf-8").splitlines() == [
        "run", "--extra", "test", "pytest", "-q", "tests/test_feishu_trusted_ingress.py",
    ]

    _executable(fake_bin / "id", "#!/bin/sh\necho 0\n")
    handoff = tmp_path / "handoff.log"
    _executable(fake_bin / "chown", f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {handoff!s}\n")
    _executable(fake_bin / "su", f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {log!s}\n")
    subprocess.run([runner], cwd=ROOT, env={**env, "CI": "true"}, check=True)
    ci_call = log.read_text(encoding="utf-8")
    assert handoff.read_text(encoding="utf-8").splitlines()[0] == "ci"
    assert ci_call.startswith("ci\n-c\n")
    assert "--ignore=tests/test_billing_readiness.py" in ci_call
    assert "--deselect tests/test_aiagent_subprocess.py::test_session_search_proxy_covers_real_agent_tool_dispatch" in ci_call
