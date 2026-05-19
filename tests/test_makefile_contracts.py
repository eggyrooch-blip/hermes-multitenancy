from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_makefile_exposes_skills_uat_targets_with_strict_completion_gate():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert ".PHONY:" in makefile
    assert "skills-uat" in makefile
    assert "skills-uat-strict" in makefile
    assert "scripts/skills_uat_matrix.py" in makefile
    assert "scripts/skills_second_problem_trace.py" in makefile
    assert "scripts/gateway_process_evidence.py" in makefile
    assert "scripts/skills_uat_completion_audit.py" in makefile
    assert "--feedback-artifact-label" in makefile
    assert "--feedback-artifact-scenario" in makefile
    assert "--require-complete" in makefile


def test_makefile_skills_uat_stops_after_first_required_command_failure():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "skills-uat:" in makefile
    assert "set -e;" in makefile
