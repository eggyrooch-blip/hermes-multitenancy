"""Tests for plugin-contributed slash-command aliases (skill_slash loader)."""
from __future__ import annotations

from hermes_multitenancy import skill_slash


def test_load_aliases_dict_and_bare_string(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "skill-slash-aliases.yaml").write_text(
        "aliases:\n"
        "  strategy: {skill: kep-trevi-strategy-recommend, plugin: keep-resource-delivery}\n"
        "  recap: kep-trevi-analysis\n",
        encoding="utf-8")
    a = skill_slash._load_plugin_slash_aliases()
    assert a["strategy"] == "kep-trevi-strategy-recommend"
    assert a["recap"] == "kep-trevi-analysis"


def test_resolve_alias_hardcoded_base_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "skill-slash-aliases.yaml").write_text(
        "aliases:\n  hades: SOMETHING-ELSE\n  strategy: kep-trevi-strategy-recommend\n", encoding="utf-8")
    assert skill_slash._resolve_alias("hades") == "kep-hades-cli"  # base beats plugin override
    assert skill_slash._resolve_alias("strategy") == "kep-trevi-strategy-recommend"
    assert skill_slash._resolve_alias("nope") is None


def test_missing_config_is_safe_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # no file written
    assert skill_slash._load_plugin_slash_aliases() == {}


def test_broken_config_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "skill-slash-aliases.yaml").write_text(": : not valid yaml : :\n", encoding="utf-8")
    assert skill_slash._load_plugin_slash_aliases() == {}  # the /commands path must never break


def test_load_skips_unsafe_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "skill-slash-aliases.yaml").write_text(
        "aliases:\n  'a/b': x\n  good: kep-x\n", encoding="utf-8")
    a = skill_slash._load_plugin_slash_aliases()
    assert "a/b" not in a and a.get("good") == "kep-x"
