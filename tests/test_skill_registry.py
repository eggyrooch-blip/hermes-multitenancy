from pathlib import Path

import pytest


def test_hermes_skill_loader_discovers_profile_symlink(monkeypatch, tmp_path: Path):
    """Hermes differs from OpenClaw: profile skills may be symlinks."""
    shared = tmp_path / ".hermes"
    source = shared / "skills" / "lark-calendar"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        """
---
name: lark-calendar
description: Calendar skill
---
# Calendar
""",
        encoding="utf-8",
    )
    profile = shared / "profiles" / "alice"
    (profile / "skills").mkdir(parents=True)
    (profile / "skills" / "lark-calendar").symlink_to(source, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))

    skill_utils = pytest.importorskip("agent.skill_utils")

    matches = list(skill_utils.iter_skill_index_files(profile / "skills", "SKILL.md"))

    assert matches == [profile / "skills" / "lark-calendar" / "SKILL.md"]


def test_install_shared_skill_for_profile_creates_personal_symlink(tmp_path: Path):
    from hermes_multitenancy.skill_registry import install_shared_skill_for_profile, list_installed_skills

    shared = tmp_path / ".hermes"
    source = shared / "skills" / "hub" / "personal-tool"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# Personal Tool\n", encoding="utf-8")
    profile = shared / "profiles" / "alice"
    (profile / "skills").mkdir(parents=True)

    result = install_shared_skill_for_profile(
        shared_home=shared,
        profile_home=profile,
        skill_path="hub/personal-tool",
    )

    target = profile / "skills" / "hub" / "personal-tool"
    assert result["installed"] is True
    assert target.is_symlink()
    assert target.resolve() == source.resolve()
    installed = list_installed_skills(profile_home=profile)
    assert installed["hub/personal-tool"]["source"] == "personal"
    assert installed["hub/personal-tool"]["install_mode"] == "symlink"


def test_install_shared_skill_copies_filtered_tree_when_source_contains_secret_files(tmp_path: Path):
    from hermes_multitenancy.skill_registry import install_shared_skill_for_profile, list_installed_skills

    shared = tmp_path / ".hermes"
    source = shared / "skills" / "hub" / "secret-tool"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (source / "SKILL.md").write_text("# Secret Tool\n", encoding="utf-8")
    (source / ".env").write_text("TOKEN=do-not-link\n", encoding="utf-8")
    (source / "auth.json").write_text('{"access_token":"do-not-copy"}', encoding="utf-8")
    (nested / "api.token").write_text("do-not-copy\n", encoding="utf-8")
    (nested / "README.md").write_text("safe docs\n", encoding="utf-8")
    profile = shared / "profiles" / "alice"
    stale_target = profile / "skills" / "hub" / "secret-tool"
    stale_target.mkdir(parents=True)
    (stale_target / "old.token").write_text("old leaked token\n", encoding="utf-8")

    result = install_shared_skill_for_profile(
        shared_home=shared,
        profile_home=profile,
        skill_path="hub/secret-tool",
    )

    target = profile / "skills" / "hub" / "secret-tool"
    assert result["install_mode"] == "copy"
    assert result["requested_install_mode"] == "symlink"
    assert result["secret_guard"] == "copy_filtered"
    assert target.is_dir()
    assert not target.is_symlink()
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# Secret Tool\n"
    assert (target / "nested" / "README.md").read_text(encoding="utf-8") == "safe docs\n"
    assert not (target / ".env").exists()
    assert not (target / "auth.json").exists()
    assert not (target / "nested" / "api.token").exists()
    assert not (target / "old.token").exists()
    installed = list_installed_skills(profile_home=profile)
    assert installed["hub/secret-tool"]["install_mode"] == "copy"
    assert installed["hub/secret-tool"]["secret_guard"] == "copy_filtered"


def test_install_shared_skill_rejects_unsafe_path(tmp_path: Path):
    from hermes_multitenancy.skill_registry import install_shared_skill_for_profile

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "alice"
    (profile / "skills").mkdir(parents=True)

    with pytest.raises(ValueError, match="invalid skill path"):
        install_shared_skill_for_profile(
            shared_home=shared,
            profile_home=profile,
            skill_path="../other-profile/secret",
        )


def test_audit_installed_skills_marks_managed_personal_unknown_and_warnings(tmp_path: Path):
    from hermes_multitenancy.skill_registry import audit_installed_skills

    shared = tmp_path / ".hermes"
    shared_skill = shared / "skills" / "Keep" / "keep-record"
    personal_skill = shared / "skills" / "hub" / "personal-tool"
    shared_skill.mkdir(parents=True)
    personal_skill.mkdir(parents=True)
    (shared_skill / "SKILL.md").write_text("# Keep\n", encoding="utf-8")
    (personal_skill / "SKILL.md").write_text("# Personal\n", encoding="utf-8")

    profile = shared / "profiles" / "alice"
    skills = profile / "skills"
    skills.mkdir(parents=True)
    (skills / ".hermes-managed.json").write_text(
        """
{
  "version": 1,
  "skills": {
    "Keep/keep-record": {
      "install_mode": "symlink",
      "source": "managed"
    }
  }
}
""",
        encoding="utf-8",
    )
    (skills / ".hermes-personal-installs.json").write_text(
        """
{
  "version": 1,
  "skills": {
    "hub/personal-tool": {
      "source": "personal"
    }
  }
}
""",
        encoding="utf-8",
    )
    (skills / "Keep").mkdir()
    (skills / "Keep" / "keep-record").symlink_to(shared_skill, target_is_directory=True)
    (skills / "hub").mkdir()
    (skills / "hub" / "personal-tool").symlink_to(personal_skill, target_is_directory=True)
    unknown = skills / "scratch"
    unknown.mkdir()
    (unknown / "SKILL.md").write_text("# Scratch\n", encoding="utf-8")
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# Outside\n", encoding="utf-8")
    (skills / "outside").symlink_to(outside, target_is_directory=True)
    (profile / "tokens").mkdir()
    (profile / "tokens" / "keep-record.json").write_text("do-not-read", encoding="utf-8")

    report = audit_installed_skills(shared_home=shared, profiles_root=shared / "profiles")

    rows = {row["skill_path"]: row for row in report["profiles"]["alice"]["skills"]}
    assert rows["Keep/keep-record"]["source"] == "managed"
    assert rows["hub/personal-tool"]["source"] == "personal"
    assert rows["scratch"]["source"] == "unknown"
    assert rows["Keep/keep-record"]["token_files_present"] is True
    assert rows["outside"]["warnings"] == ["symlink_target_outside_shared_skills"]


def test_audit_installed_skills_tolerates_nested_symlink_loop(tmp_path: Path):
    from hermes_multitenancy.skill_registry import audit_installed_skills

    shared = tmp_path / ".hermes"
    profile = shared / "profiles" / "alice"
    loop_skill = profile / "skills" / "loop"
    nested = loop_skill / "nested"
    nested.mkdir(parents=True)
    (loop_skill / "SKILL.md").write_text("# Loop\n", encoding="utf-8")
    (nested / "back").symlink_to(loop_skill, target_is_directory=True)

    report = audit_installed_skills(shared_home=shared, profiles_root=shared / "profiles")

    rows = report["profiles"]["alice"]["skills"]
    assert [row["skill_path"] for row in rows] == ["loop"]
