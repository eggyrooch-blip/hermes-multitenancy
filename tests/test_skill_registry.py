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


def test_profile_skill_slash_commands_do_not_fallback_metadata_to_body(tmp_path: Path):
    from hermes_multitenancy.skill_registry import list_profile_skill_slash_commands

    skill = tmp_path / ".hermes" / "profiles" / "alice" / "skills" / "internal" / "runbook"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "\n".join([
            "---",
            "name: runbook",
            "---",
            "# Internal Runbook",
            "",
            "INTERNAL_BODY_MARKER should not be registry metadata",
        ]),
        encoding="utf-8",
    )

    commands = list_profile_skill_slash_commands(profile_home=tmp_path / ".hermes" / "profiles" / "alice")

    assert commands == [
        {
            "name": "runbook",
            "slash": "/runbook",
            "title": "runbook",
            "description": "",
            "source": "skill",
            "type": "skill",
            "category": "internal",
        }
    ]
    assert "INTERNAL_BODY_MARKER" not in str(commands)
    assert "Internal Runbook" not in str(commands)


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

    assert report["profiles"]["alice"]["curator"] == {
        "dry_run_plan": {
            "command": ["hermes", "curator", "run", "--dry-run"],
            "env": {"HERMES_HOME": str(profile)},
            "executes": False,
        }
    }
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


def test_audit_installed_skills_adds_curator_policy_without_exposing_tokens(tmp_path: Path):
    from hermes_multitenancy.skill_registry import audit_installed_skills

    shared = tmp_path / ".hermes"
    shared_skill = shared / "skills" / "Keep" / "keep-record"
    personal_skill = shared / "skills" / "scratch-agent"
    shared_skill.mkdir(parents=True)
    personal_skill.mkdir(parents=True)
    (shared_skill / "SKILL.md").write_text(
        "---\nname: keep-record\n---\n# Keep\n",
        encoding="utf-8",
    )
    (personal_skill / "SKILL.md").write_text(
        "---\nname: scratch-agent\n---\n# Scratch Agent\n",
        encoding="utf-8",
    )

    profile = shared / "profiles" / "alice"
    skills = profile / "skills"
    skills.mkdir(parents=True)
    (skills / ".hermes-managed.json").write_text(
        """
{
  "version": 1,
  "skills": {
    "Keep/keep-record": {
      "source": "managed",
      "version": "2026.05.20"
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
    "scratch-agent": {
      "source": "personal"
    }
  }
}
""",
        encoding="utf-8",
    )
    (skills / "Keep").mkdir()
    (skills / "Keep" / "keep-record").symlink_to(shared_skill, target_is_directory=True)
    (skills / "scratch-agent").symlink_to(personal_skill, target_is_directory=True)
    (profile / "tokens").mkdir()
    (profile / "tokens" / "scratch-agent.json").write_text(
        '{"access_token":"secret-token"}',
        encoding="utf-8",
    )
    (skills / ".usage.json").write_text(
        """
{
  "keep-record": {
    "created_by": "agent",
    "use_count": 9,
    "last_used_at": "2026-05-20T01:00:00+00:00",
    "state": "active",
    "pinned": false
  },
  "scratch-agent": {
    "created_by": "agent",
    "use_count": 2,
    "last_used_at": "2026-05-20T02:00:00+00:00",
    "state": "stale",
    "pinned": true,
    "access_token": "must-not-leak"
  }
}
""",
        encoding="utf-8",
    )

    report = audit_installed_skills(shared_home=shared, profiles_root=shared / "profiles")

    rows = {row["skill_path"]: row for row in report["profiles"]["alice"]["skills"]}
    assert rows["Keep/keep-record"]["curator"] == {
        "eligible": False,
        "reason": "managed_by_multitenancy",
        "skill_name": "keep-record",
        "state": "active",
        "pinned": False,
        "usage": {
            "use_count": 9,
            "view_count": 0,
            "patch_count": 0,
            "last_used_at": "2026-05-20T01:00:00+00:00",
            "last_viewed_at": None,
            "last_patched_at": None,
            "latest_activity_at": "2026-05-20T01:00:00+00:00",
            "activity_count": 9,
        },
    }
    assert rows["scratch-agent"]["curator"]["eligible"] is True
    assert rows["scratch-agent"]["curator"]["reason"] == "agent_created_personal_skill"
    assert rows["scratch-agent"]["curator"]["pinned"] is True
    assert rows["scratch-agent"]["curator"]["usage"]["activity_count"] == 2
    assert "access_token" not in rows["scratch-agent"]["curator"]["usage"]
    assert "secret-token" not in str(report)


def test_audit_installed_skills_keeps_skillhub_installs_advisory_by_default(tmp_path: Path):
    from hermes_multitenancy.skill_registry import audit_installed_skills

    shared = tmp_path / ".hermes"
    source = shared / "skills" / "hub" / "weather"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: weather\n---\n# Weather\n", encoding="utf-8")
    profile = shared / "profiles" / "alice"
    (profile / "skills" / "hub").mkdir(parents=True)
    (profile / "skills" / "hub" / "weather").symlink_to(source, target_is_directory=True)
    (profile / "skills" / ".hermes-personal-installs.json").write_text(
        """
{
  "version": 1,
  "skills": {
    "hub/weather": {
      "source": "personal",
      "install_mode": "symlink"
    }
  }
}
""",
        encoding="utf-8",
    )
    (profile / "skills" / ".usage.json").write_text(
        '{"weather":{"use_count":4,"last_used_at":"2026-05-20T03:00:00+00:00"}}',
        encoding="utf-8",
    )

    report = audit_installed_skills(shared_home=shared, profiles_root=shared / "profiles")

    row = report["profiles"]["alice"]["skills"][0]
    assert row["curator"]["eligible"] is False
    assert row["curator"]["reason"] == "personal_not_agent_created"
    assert row["curator"]["usage"]["activity_count"] == 4


def test_curator_dry_run_plan_is_profile_scoped_and_non_executing(tmp_path: Path):
    from hermes_multitenancy.curator_adapter import build_curator_dry_run_plan

    profile = tmp_path / ".hermes" / "profiles" / "alice"

    plan = build_curator_dry_run_plan(profile_home=profile)

    assert plan == {
        "command": ["hermes", "curator", "run", "--dry-run"],
        "env": {"HERMES_HOME": str(profile)},
        "executes": False,
    }


def test_curator_metadata_blocks_org_shared_sources_even_when_agent_created(tmp_path: Path):
    from hermes_multitenancy.curator_adapter import curator_metadata_for_skill

    profile = tmp_path / ".hermes" / "profiles" / "alice"
    skill = profile / "skills" / "org" / "default-profile"
    skill.mkdir(parents=True)
    skill_md = skill / "SKILL.md"
    skill_md.write_text("---\nname: default-profile\n---\n# Default\n", encoding="utf-8")
    usage = {
        "default-profile": {
            "created_by": "agent",
            "use_count": 5,
        }
    }

    for source in ("org", "shared"):
        metadata = curator_metadata_for_skill(
            profile_home=profile,
            skill_path="org/default-profile",
            skill_md=skill_md,
            source=source,
            usage=usage,
        )

        assert metadata["eligible"] is False
        assert metadata["reason"] == f"{source}_skill_not_eligible"
