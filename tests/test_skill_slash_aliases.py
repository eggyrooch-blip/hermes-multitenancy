"""Tests for skill-declared, dynamically-scanned slash aliases (per-profile)."""
from __future__ import annotations

from hermes_multitenancy import skill_slash
from hermes_multitenancy.skill_registry import list_profile_skill_slash_commands, read_skill_slash_aliases


def _skill_md(root, name, aliases):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    block = "[" + ", ".join(aliases) + "]"
    (d / "SKILL.md").write_text(f"---\nname: {name}\nslash_aliases: {block}\ndescription: d\n---\n# {name}\n", encoding="utf-8")
    return d / "SKILL.md"


# ─────────────────────── read_skill_slash_aliases (frontmatter) ──────────

def test_read_aliases_list(tmp_path):
    p = _skill_md(tmp_path, "kep-trevi-strategy-recommend", ["strategy"])
    assert read_skill_slash_aliases(p) == ["strategy"]


def test_read_aliases_absent(tmp_path):
    d = tmp_path / "x"; d.mkdir()
    (d / "SKILL.md").write_text("---\nname: x\ndescription: d\n---\n# x\n", encoding="utf-8")
    assert read_skill_slash_aliases(d / "SKILL.md") == []


def test_read_aliases_sanitizes(tmp_path):
    p = _skill_md(tmp_path, "y", ["a_b", "ok"])  # underscores → hyphens
    assert read_skill_slash_aliases(p) == ["a-b", "ok"]


# ─────────────────────── dynamic per-profile scan ───────────────────────

def test_scan_aliases_dynamic_from_installed_skills(tmp_path, monkeypatch):
    p1 = _skill_md(tmp_path, "kep-trevi-strategy-recommend", ["strategy"])
    p2 = _skill_md(tmp_path, "kep-trevi-analysis", ["recap"])
    fake = {
        "/kep-trevi-strategy-recommend": {"name": "kep-trevi-strategy-recommend", "skill_md_path": str(p1)},
        "/kep-trevi-analysis": {"name": "kep-trevi-analysis", "skill_md_path": str(p2)},
    }
    import agent.skill_commands as sc
    monkeypatch.setattr(sc, "get_skill_commands", lambda: fake)
    skill_slash._ALIAS_SCAN_CACHE.clear()
    aliases = skill_slash._scan_slash_aliases()
    assert aliases == {"strategy": "kep-trevi-strategy-recommend", "recap": "kep-trevi-analysis"}
    assert skill_slash._resolve_alias("strategy") == "kep-trevi-strategy-recommend"
    assert skill_slash._resolve_alias("hades") == "kep-hades-cli"  # hardcoded base
    assert skill_slash._resolve_alias("nope") is None


def test_scan_aliases_per_profile_isolation(tmp_path, monkeypatch):
    # profile WITHOUT the strategy skill → no /strategy alias
    p2 = _skill_md(tmp_path, "kep-trevi-analysis", ["recap"])
    fake = {"/kep-trevi-analysis": {"name": "kep-trevi-analysis", "skill_md_path": str(p2)}}
    import agent.skill_commands as sc
    monkeypatch.setattr(sc, "get_skill_commands", lambda: fake)
    skill_slash._ALIAS_SCAN_CACHE.clear()
    assert skill_slash._resolve_alias("strategy") is None
    assert skill_slash._resolve_alias("recap") == "kep-trevi-analysis"


# ─────────────────────── picker list includes aliases ───────────────────

def test_list_profile_commands_includes_aliases(tmp_path):
    sk = tmp_path / "profiles" / "p" / "skills" / "kep-trevi-strategy-recommend"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: kep-trevi-strategy-recommend\nslash_aliases: [strategy]\ndescription: d\n---\n# x\n", encoding="utf-8")
    cmds = list_profile_skill_slash_commands(profile_home=tmp_path / "profiles" / "p")
    slashes = {c["slash"] for c in cmds}
    assert "/kep-trevi-strategy-recommend" in slashes
    assert "/strategy" in slashes
    alias = next(c for c in cmds if c["slash"] == "/strategy")
    assert alias["skill"] == "kep-trevi-strategy-recommend" and alias["source"] == "skill-alias"
