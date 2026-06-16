"""Regression: skill resolution must follow the CURRENT HERMES_HOME.

Guards the cross-profile "skill not found" bug — core ``tools.skills_tool``
froze ``SKILLS_DIR`` at import to the router profile, so cron jobs owned by
other profiles never resolved their skills. The plugin patch refreshes the
skills root from ``get_hermes_home()`` on every entry, so a single process can
resolve skills for whichever profile is active.

See hermes_multitenancy/skills_dir_dynamic.py and the 2026-06-16 incident.
"""
from __future__ import annotations

import json
from pathlib import Path

from hermes_multitenancy.skills_dir_dynamic import install_dynamic_skills_dir_patch


def _make_profile_with_skill(root: Path, skill_name: str) -> Path:
    home = root / "skills" / skill_name
    home.mkdir(parents=True, exist_ok=True)
    (home / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: regression fixture skill\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return root


def _viewed_ok(name: str) -> bool:
    import tools.skills_tool as st

    raw = st.skill_view(name, preprocess=False)
    data = json.loads(raw)
    # success path returns content / success True; not-found returns an error.
    if data.get("error"):
        return False
    return data.get("success", True) is not False


def test_skill_view_follows_current_hermes_home(tmp_path, monkeypatch):
    skill = "regtestdynamicskill"
    profile_a = _make_profile_with_skill(tmp_path / "profileA", skill)
    profile_b = tmp_path / "profileB"
    (profile_b / "skills").mkdir(parents=True, exist_ok=True)  # B has NO such skill

    # No context-local override interfering; resolution falls to HERMES_HOME env.
    install_dynamic_skills_dir_patch()

    # A has the skill — note import-time HERMES_HOME was neither A nor B, so a
    # frozen SKILLS_DIR could never find it here. Success ⇒ dynamic resolution.
    monkeypatch.setenv("HERMES_HOME", str(profile_a))
    assert _viewed_ok(skill) is True

    # Switch to B (no such skill) — must now NOT resolve.
    monkeypatch.setenv("HERMES_HOME", str(profile_b))
    assert _viewed_ok(skill) is False

    # Back to A — resolves again (proves it tracks the live value, not a one-shot).
    monkeypatch.setenv("HERMES_HOME", str(profile_a))
    assert _viewed_ok(skill) is True


def test_install_is_idempotent(tmp_path, monkeypatch):
    import tools.skills_tool as st

    install_dynamic_skills_dir_patch()
    first = st.skill_view
    install_dynamic_skills_dir_patch()  # second call must not re-wrap
    assert st.skill_view is first
