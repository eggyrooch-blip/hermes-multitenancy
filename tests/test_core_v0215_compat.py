"""Compat pins for the hermes-agent v0.21.5 core (slug ``mt-core-0215-compat``).

Two core moves this repo has to survive, one hard and one soft:

1. HARD — upstream deleted ``tools.mcp_tool_discovery._enabled`` (09-22, ``3e00a356a4``);
   the single reader of ``mcp_servers.<name>.enabled`` moved to
   ``tools.mcp_tool_common.mcp_server_enabled``. ``agent_real.mcp_servers`` imported the
   old name inside ``_allowed_server_names``, so every MCP availability computation
   ImportError'd on v0.21.5. The reader must resolve from EITHER generation.

2. SOFT — upstream main (post-v0.21.5, ``0180cace40``) widened
   ``tools.skills_tool._is_skill_disabled`` to ``(*names, platform=None)`` and
   ``skill_view`` now passes the declared name AND the owned rel path in one call.
   MT's expert-scope wrapper took ``(name, platform=None)``, so the second name landed
   in ``platform`` and a hidden expert skill stayed invocable via its rel path.

The ``tools.*`` seam is stubbed with each generation's real module shape (v0.21.4's
``mcp_tool_common`` exists but has no ``mcp_server_enabled``; v0.21.5's discovery has
no ``_enabled``), so these tests fail loudly the day MT's resolution drifts from either
core again.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from hermes_multitenancy import agent_real
from hermes_multitenancy.agent_real import mcp_servers

# reuse the hermetic repo/home builders from the sibling test module
from tests.test_expert_overlay import _plugin_repo, _shared_home, _ingest


def _event(expert_id=None):
    metadata = {}
    if expert_id is not None:
        metadata["expert_id"] = expert_id
    return SimpleNamespace(text="hi", raw_event={"metadata": metadata})


def _install_tools_seam(monkeypatch, *, common_has_reader, consulted):
    """Put each core generation's ``tools`` shape into ``sys.modules``.

    ``consulted`` receives ``("<which reader>", <server name>)`` so a test can prove
    WHICH generation's reader decided, not merely that the call returned.
    """
    pkg = ModuleType("tools")
    pkg.__path__ = []  # package marker so submodule imports resolve against the fakes

    common = ModuleType("tools.mcp_tool_common")
    if common_has_reader:

        def mcp_server_enabled(cfg):
            consulted.append(("common", cfg["__name"]))
            return cfg.get("enabled", True)

        common.mcp_server_enabled = mcp_server_enabled
    # else: v0.21.4 shape — module present, reader absent (raises ImportError on from-import)

    discovery = ModuleType("tools.mcp_tool_discovery")

    def _enabled(cfg):
        consulted.append(("discovery", cfg["__name"]))
        return cfg.get("enabled", True)

    discovery._enabled = _enabled

    for name, module in {
        "tools": pkg,
        "tools.mcp_tool_common": common,
        "tools.mcp_tool_discovery": discovery,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


_CONFIGURED = {
    "alpha": {"__name": "alpha", "enabled": True},
    "beta": {"__name": "beta", "enabled": False},
    "not-a-dict": "nope",
}
_DECLARED = {"alpha", "beta", "not-a-dict", "undeclared-server"}


def test_allowed_server_names_reads_mcp_tool_common_reader_on_v0215_core(monkeypatch):
    """v0.21.5: ``mcp_tool_common.mcp_server_enabled`` exists, ``discovery._enabled`` is
    gone → the availability set must come from the new reader, ImportError-free."""
    consulted: list = []
    _install_tools_seam(monkeypatch, common_has_reader=True, consulted=consulted)
    # Poison the old seat: if the code resolved discovery._enabled instead of the
    # common reader, every server would come back disabled and the test fails.
    discovery = sys.modules["tools.mcp_tool_discovery"]

    def _poison_enabled(cfg):
        consulted.append(("discovery", cfg["__name"]))
        return False

    discovery._enabled = _poison_enabled

    allowed = mcp_servers._allowed_server_names(_CONFIGURED, None, _DECLARED)

    assert allowed == {"alpha"}  # new reader decides: alpha on, beta off
    assert ("common", "alpha") in consulted
    assert ("common", "beta") in consulted
    assert all(which == "common" for which, _name in consulted)


def test_allowed_server_names_falls_back_to_discovery_enabled_on_v0214_core(monkeypatch):
    """v0.21.4: ``mcp_tool_common`` exists but lacks ``mcp_server_enabled`` → the
    from-import raises ImportError and the old ``discovery._enabled`` reader must
    keep deciding (this is the branch the current lock actually exercises)."""
    consulted: list = []
    _install_tools_seam(monkeypatch, common_has_reader=False, consulted=consulted)
    assert not hasattr(sys.modules["tools.mcp_tool_common"], "mcp_server_enabled")

    allowed = mcp_servers._allowed_server_names(_CONFIGURED, None, _DECLARED)

    assert allowed == {"alpha"}
    assert consulted == [("discovery", "alpha"), ("discovery", "beta")]
    assert all(which == "discovery" for which, _name in consulted)


def test_expert_scope_is_skill_disabled_checks_every_name(tmp_path, monkeypatch):
    """Upstream main calls ``_is_skill_disabled(resolved_name, rel)`` — two names in
    one call. The expert-scope wrapper must hide the skill when ANY name is an
    expert-hidden one (here: the second), and stay a no-op for names that are all
    visible, while keeping the old single-name call shape working on the installed
    core (which is still single-name on v0.21.4 AND on the v0.21.5 wheel)."""
    import tools.skills_tool as skills_tool

    repo = _plugin_repo(tmp_path / "plug")
    shared = _shared_home(tmp_path)
    _ingest(repo, shared)
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared))
    profile_home = shared / "profiles" / "feishu_test"

    cleanup = agent_real._apply_expert_skill_scope_for_aiagent(_event(), profile_home)
    try:
        # multi-name call, hidden expert skill as the SECOND name (the rel-path seat)
        assert skills_tool._is_skill_disabled("ordinary-skill", "using-resource-delivery") is True
        # and the reverse seat, for good measure
        assert skills_tool._is_skill_disabled("using-resource-delivery", "ordinary-skill") is True
        # old single-name call shape still works
        assert skills_tool._is_skill_disabled("using-resource-delivery") is True
        assert skills_tool._is_skill_disabled("ordinary-skill") is False
        # multi-name call with NO expert-hidden name delegates to the core without
        # raising (old single-name core gets per-name calls via the TypeError fallback)
        assert skills_tool._is_skill_disabled("ordinary-skill", "another-ordinary-skill") is False
    finally:
        cleanup()

    # cleanup restores the original gate (no leak of the patch)
    assert skills_tool._is_skill_disabled("using-resource-delivery") is False
