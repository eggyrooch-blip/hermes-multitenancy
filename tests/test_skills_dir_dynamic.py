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


# ── pending-activation hint (skill-install-visible-in-session) ──────────────
# Mid-session installs appear as dangling symlinks / empty stub dirs in the
# sandbox; the wrapper must surface "installed, activates in a new session"
# instead of silently skipping (zhaofanrong 2026-08-25).

def _pending_home(tmp_path: Path) -> Path:
    home = tmp_path / "profileP"
    (home / "skills").mkdir(parents=True, exist_ok=True)
    return home


def test_dangling_symlink_reported_pending(tmp_path, monkeypatch):
    import tools.skills_tool as st

    home = _pending_home(tmp_path)
    (home / "skills" / "freshskill").symlink_to(tmp_path / "nowhere" / "freshskill")

    install_dynamic_skills_dir_patch()
    monkeypatch.setenv("HERMES_HOME", str(home))

    listed = json.loads(st.skills_list())
    pending = {row["name"]: row["note"] for row in listed.get("pending_activation", [])}
    assert "freshskill" in pending
    assert "新开一个会话" in pending["freshskill"]

    viewed = json.loads(st.skill_view("freshskill", preprocess=False))
    assert viewed["success"] is False
    assert viewed.get("pending_activation") is True
    assert "新开一个会话" in viewed["error"]


def test_empty_stub_dir_reported_pending(tmp_path, monkeypatch):
    import tools.skills_tool as st

    home = _pending_home(tmp_path)
    (home / "skills" / "stubskill").mkdir()

    install_dynamic_skills_dir_patch()
    monkeypatch.setenv("HERMES_HOME", str(home))

    listed = json.loads(st.skills_list())
    assert any(row["name"] == "stubskill" for row in listed.get("pending_activation", []))

    viewed = json.loads(st.skill_view("stubskill", preprocess=False))
    assert viewed["success"] is False
    assert viewed.get("pending_activation") is True


def test_category_nested_pending_and_base_name_match(tmp_path, monkeypatch):
    import tools.skills_tool as st

    home = _pending_home(tmp_path)
    _make_profile_with_skill(home, "Keep/realskill")  # category has a real skill
    (home / "skills" / "Keep" / "newcli").symlink_to(tmp_path / "gone" / "newcli")

    install_dynamic_skills_dir_patch()
    monkeypatch.setenv("HERMES_HOME", str(home))

    listed = json.loads(st.skills_list())
    names = [row["name"] for row in listed.get("pending_activation", [])]
    assert names == ["Keep/newcli"]

    viewed = json.loads(st.skill_view("newcli", preprocess=False))
    assert viewed.get("pending_activation") is True


def test_no_pending_output_byte_identical(tmp_path, monkeypatch):
    import tools.skills_tool as st

    home = _make_profile_with_skill(tmp_path / "profileQ", "steadyskill")

    install_dynamic_skills_dir_patch()
    monkeypatch.setenv("HERMES_HOME", str(home))

    wrapped_list = st.skills_list()
    assert st.skills_list.__wrapped__() == wrapped_list
    assert "pending_activation" not in json.loads(wrapped_list)

    wrapped_view = st.skill_view("steadyskill", preprocess=False)
    assert st.skill_view.__wrapped__("steadyskill", preprocess=False) == wrapped_view
    assert json.loads(wrapped_view).get("success", True) is not False

    # a genuinely-missing skill stays a plain not-found (no pending noise)
    missing = json.loads(st.skill_view("no-such-skill", preprocess=False))
    assert missing["success"] is False
    assert "pending_activation" not in missing
