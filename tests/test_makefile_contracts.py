from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import warnings

import pytest


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


class CoreSourceProbeLog(UserWarning):
    """Carries the probe's stdout into the pytest warnings summary (the CI log)."""


PROBE = ROOT / "scripts" / "ci_core_source_probe.py"


def test_core_source_probe_pins_the_production_core_line():
    source = PROBE.read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert 'CORE_COMMIT = "2082ff0c1717b3ed2dc534a8eb54e00e7e657d1e"' in source
    assert 'CORE_VERSION = "0.21.4"' in source
    assert 'WHEEL_SHA256 = "d76c90ca409d1b7a8eb7ce6c37b39f6c79c9188aeb4a068c9f15fc17cfc96f13"' in source
    assert 'PROJECT_ID = "2765"' in source
    assert "ci-core-source-probe:\n\tpython3 scripts/ci_core_source_probe.py" in makefile


def test_core_source_probe_fails_loudly_when_uv_step_times_out(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(fake_bin / "uv", "#!/bin/sh\nexit 0\n")
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE.read_text(encoding="utf-8"), encoding="utf-8")
    code = (
        "import runpy, subprocess, sys\n"
        "real = subprocess.run\n"
        "def fake(cmd, **kw):\n"
        "    raise subprocess.TimeoutExpired(cmd, kw.get('timeout'))\n"
        "subprocess.run = fake\n"
        f"sys.argv = [{str(probe)!r}]\n"
        f"mod = runpy.run_path({str(probe)!r})\n"
        "try:\n"
        "    mod['_run'](['uv', 'venv', 'x'], timeout=1)\n"
        "except SystemExit as exc:\n"
        "    sys.exit(exc.code)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "core 来源不可达: uv venv x timed out after 1s" in result.stdout


def test_core_source_probe_fails_loudly_without_credentials():
    env = {k: v for k, v in os.environ.items() if k not in {"CI_JOB_TOKEN", "HERMES_CORE_PKG_TOKEN"}}
    result = subprocess.run([sys.executable, str(PROBE)], env=env, capture_output=True, text=True, timeout=30)

    assert result.returncode == 2
    assert "core 来源不可达" in result.stdout


def test_core_source_probe_installs_production_core_from_registry():
    """Network probe of the core registry — merge-request pipelines (or an explicit local token) only.

    main / nightly pipelines (test-full) stay network-free: the registry is not a
    dependency of main's TEST gate until uv.lock itself resolves core from it
    (mt-merge-release-core-v0214). Inside an MR pipeline a missing token is a
    failure, not a skip.
    """
    local = bool(os.environ.get("HERMES_CORE_PKG_TOKEN"))
    in_mr = os.environ.get("CI_PIPELINE_SOURCE") == "merge_request_event"
    if not local and not in_mr:
        pytest.skip("registry probe runs in GitLab merge-request pipelines or with HERMES_CORE_PKG_TOKEN")
    if in_mr and not os.environ.get("CI_JOB_TOKEN"):
        pytest.fail("core 来源不可达: merge-request pipeline but CI_JOB_TOKEN is not visible to the test process")

    result = subprocess.run([sys.executable, str(PROBE)], capture_output=True, text=True, timeout=240)
    log = (result.stdout + result.stderr).strip()

    assert result.returncode == 0, log
    assert "core-source: OK hermes-agent 0.21.4" in result.stdout, log
    assert "sha256=d76c90ca409d1b7a8eb7ce6c37b39f6c79c9188aeb4a068c9f15fc17cfc96f13" in result.stdout, log
    warnings.warn(CoreSourceProbeLog(log))
