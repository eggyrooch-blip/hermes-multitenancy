"""Broker slash rewrite is scoped per-profile (no cross-profile skill/alias leak).

Webui/cron enter via RunBroker; the slash rewrite must resolve against the REQUEST's
profile skills, not a stale process-global cache from whichever profile ran last.
"""
from __future__ import annotations

import pytest

from hermes_multitenancy import run_broker
from hermes_multitenancy.run_models import RunRequest

# the rewrite delegates to core agent.skill_commands; skip cleanly if a thin env lacks it
try:
    import agent.skill_commands  # noqa: F401
    _HAS_AGENT = True
except Exception:
    _HAS_AGENT = False
requires_agent = pytest.mark.skipif(not _HAS_AGENT, reason="agent.skill_commands not installed")


def _install_skill(profile_home, name, *, slash_aliases=None):
    d = profile_home / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    fm = f"name: {name}\n"
    if slash_aliases:
        fm += f"slash_aliases: [{', '.join(slash_aliases)}]\n"
    (d / "SKILL.md").write_text(f"---\n{fm}description: d\n---\n# {name}\n", encoding="utf-8")


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    a, b = root / "pA", root / "pB"
    (a / "skills").mkdir(parents=True)
    (b / "skills").mkdir(parents=True)
    # profile A has the strategy skill (with its short alias); B has none
    _install_skill(a, "kep-trevi-strategy-recommend", slash_aliases=["strategy"])

    from hermes_multitenancy import router
    monkeypatch.setattr(router, "_profile_name_to_home", lambda name: root / name)
    # start from a clean global skill cache + neutral SKILLS_DIR
    import agent.skill_commands as sc
    import tools.skills_tool as st
    monkeypatch.setattr(sc, "_skill_commands", {}, raising=False)
    from hermes_multitenancy import skill_slash
    skill_slash._ALIAS_SCAN_CACHE.clear()
    return root, st, sc


def _req(profile, content):
    return RunRequest(channel="webui", profile_name=profile, user_key="u", content=content)


@requires_agent
def test_alias_resolves_for_owning_profile(two_profiles):
    out = run_broker._rewrite_skill_slash_request(_req("pA", "/strategy 首页大卡"))
    assert out.content != "/strategy 首页大卡"
    assert "kep-trevi-strategy-recommend" in out.content


@requires_agent
def test_alias_does_not_leak_to_other_profile(two_profiles):
    # resolve for A first (populates the global cache), then B must NOT inherit it
    run_broker._rewrite_skill_slash_request(_req("pA", "/strategy x"))
    out_b = run_broker._rewrite_skill_slash_request(_req("pB", "/strategy x"))
    assert out_b.content == "/strategy x"  # B has no strategy skill → unresolved, not A's


@requires_agent
def test_full_name_command_also_per_profile(two_profiles):
    # the pre-existing /full-name path is fixed by the same scoping
    out_a = run_broker._rewrite_skill_slash_request(_req("pA", "/kep-trevi-strategy-recommend x"))
    assert "kep-trevi-strategy-recommend" in out_a.content
    out_b = run_broker._rewrite_skill_slash_request(_req("pB", "/kep-trevi-strategy-recommend x"))
    assert out_b.content == "/kep-trevi-strategy-recommend x"


@requires_agent
def test_global_loader_state_restored_after_rewrite(two_profiles):
    _root, st, sc = two_profiles
    before_dir = getattr(st, "SKILLS_DIR", None)
    before_cmds = getattr(sc, "_skill_commands", None)
    run_broker._rewrite_skill_slash_request(_req("pA", "/strategy x"))
    # no side-effect leak: the global loader state is restored to what it was
    assert getattr(st, "SKILLS_DIR", None) == before_dir
    assert getattr(sc, "_skill_commands", None) == before_cmds


def test_no_profile_is_safe_noop(monkeypatch):
    # a rewrite with an unresolvable profile must not raise (scoping fails soft)
    from hermes_multitenancy import router
    def _boom(_name):
        raise RuntimeError("no home")
    monkeypatch.setattr(router, "_profile_name_to_home", _boom)
    out = run_broker._rewrite_skill_slash_request(_req("pX", "hello world"))
    assert out.content == "hello world"
