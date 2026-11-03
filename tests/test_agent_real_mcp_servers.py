"""Profile MCP servers reach the WebUI AIAgent subprocess — and only this profile's.

Background (2026-09-21, slug ``mt-webui-agent-mcp-servers``). A real WebUI turn for
profile ``feishu_g41a5b5g`` listed exactly 7 lazy tools and none of the profile's two
configured MCP servers. Measured inside the AIAgent child: ``_load_mcp_config()``
returned both servers, ``enabled_toolsets`` carried both names into ``AIAgent``, and
both servers still sat at ``status="configured"`` with ``_servers == {}``. Core never
connects MCP servers from ``AIAgent.__init__`` — every host does it itself
(``hermes_cli/mcp_startup``, ``gateway/run._discover_gateway_mcp_tools``,
``cron/scheduler``, ``tui_gateway/methods_tools``) — and multitenancy, a fourth host,
did not. ``enabled_toolsets`` only filters tools that already exist.

Core is not importable from this repo's venv, so the ``tools.*`` seam is stubbed. The
assertions are about the ONE thing multitenancy owns: which server names this profile's
run is allowed to hand to core, and what is torn down first.
"""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from hermes_multitenancy import agent_real
from hermes_multitenancy.agent_real import mcp_servers


class _FakeCore:
    """Stands in for ``tools.mcp_tool_common._core``'s connection ledgers.

    ``_registered_tools`` is core's process-global MCP tool registry
    (``tools.mcp_tool_registration``): exactly the names an agent can list and
    dispatch. Teardown has to empty it, not just close a socket.
    """

    def __init__(self, servers=(), lazy=(), tools=()):
        import threading

        self._lock = threading.RLock()
        self._servers = {name: object() for name in servers}
        self._lazy_server_configs = {name: {} for name in lazy}
        self._registered_tools = {name: (lambda: name) for name in tools}


class _Recorder:
    """Captures every cross-module call the module under test makes into core."""

    def __init__(self, configured, *, live=(), lazy=(), tools=(), discovery_error=None):
        self.configured = configured
        self.core = _FakeCore(live, lazy, tools)
        self.discovery_error = discovery_error
        self.discovered_with = []
        self.shutdown_names = []
        self.forgotten_lazy = []
        self.oauth_suppressed_during_discovery = None
        self.deregistered = []
        self.deregistered_all_scopes = []
        self._suppressing = False


def _listed_tools(rec):
    """What this turn's agent would see — core resolves the tool list by name."""
    return sorted(rec.core._registered_tools)


def _call_tool(rec, tool_name):
    """Dispatch as core does: by name, out of the process-global registry."""
    try:
        handler = rec.core._registered_tools[tool_name]
    except KeyError:
        raise LookupError(tool_name) from None
    return handler()


@pytest.fixture(autouse=True)
def _fresh_worker_process(monkeypatch):
    """Every test starts as a cold warm-worker process.

    ``mcp_servers._REGISTERED_CONFIGS`` is deliberately process-global (it has to
    outlive a turn, like core's ledgers), so it must be reset between tests or one
    test's registrations would leak into the next.
    """
    # ``raising=False``: the red-before harness loads the pre-review source, which
    # has no such ledger yet. The fixture must not be what fails there.
    monkeypatch.setattr(mcp_servers, "_REGISTERED_CONFIGS", {}, raising=False)


@pytest.fixture
def core_stub(monkeypatch):
    """Install a fake ``tools.*`` package; yields a factory that arms it."""
    state = {}

    def install(configured, *, live=(), lazy=(), tools=(), discovery_error=None):
        rec = _Recorder(configured, live=live, lazy=lazy, tools=tools,
                        discovery_error=discovery_error)
        state["rec"] = rec

        def _enabled(cfg):
            value = cfg.get("enabled", True)
            return value not in (False, "false", "False", 0, "0", "no", "off")

        def _drop_tools_of(name):
            prefix = f"mcp__{name}__"
            for tool in [t for t in rec.core._registered_tools if t.startswith(prefix)]:
                rec.core._registered_tools.pop(tool, None)

        def discover_mcp_tools(allowed_mcp_names=None):
            rec.discovered_with.append(
                None if allowed_mcp_names is None else list(allowed_mcp_names)
            )
            rec.oauth_suppressed_during_discovery = rec._suppressing
            if rec.discovery_error is not None:
                raise rec.discovery_error
            names = list(allowed_mcp_names) if allowed_mcp_names is not None else list(rec.configured)
            for name in names:
                # Core's ``_select_new_servers`` (v0.21.3) drops any candidate whose
                # name is already in ``_servers`` / ``_server_connecting`` /
                # ``_lazy_server_configs``. Such a server is NOT re-registered, so it
                # keeps the connection args and ``tools`` filter it was first
                # registered with — the whole point of finding #2.
                if name in rec.core._servers or name in rec.core._lazy_server_configs:
                    continue
                cfg = rec.configured.get(name) or {}
                rec.core._servers[name] = object()
                for tool in cfg.get("tools", ["probe"]):
                    tool_name = f"mcp__{name}__{tool}"
                    rec.core._registered_tools[tool_name] = (lambda n=tool_name: n)
            return sorted(rec.core._registered_tools)

        def _forget_lazy_server(key):
            rec.forgotten_lazy.append(key)
            rec.core._lazy_server_configs.pop(key, None)
            _drop_tools_of(key[1] if isinstance(key, tuple) else key)

        def shutdown_mcp_servers(*, scope=None, names=None):
            rec.shutdown_names.append(sorted(names or ()))
            for name in list(names or ()):
                rec.core._servers.pop(name, None)
                _drop_tools_of(name)

        class _FakeRegistry:
            """Models ``tools.registry.registry`` for the MCP slice only.

            Core groups every tool of a server under the ``mcp-<name>`` toolset
            (``mcp_tool_registration._remove_server_scope`` looks them up exactly
            this way), and ``deregister`` is what makes a tool unlistable and
            undispatchable. Backed by the SAME dict the agent lists and dispatches
            from, so "deregistered" and "not callable" cannot drift apart here.
            """

            def get_tool_names_for_toolset(self_inner, toolset):
                name = toolset[len("mcp-"):] if toolset.startswith("mcp-") else toolset
                prefix = f"mcp__{name}__"
                return sorted(t for t in rec.core._registered_tools if t.startswith(prefix))

            def deregister(self_inner, tool_name, *, scope=None):
                rec.deregistered.append(tool_name)
                rec.core._registered_tools.pop(tool_name, None)

        fake_registry = _FakeRegistry()

        def _deregister_mcp_tool_all_scopes(key, tool_name):
            rec.deregistered_all_scopes.append((key, tool_name))
            fake_registry.deregister(tool_name)

        class _Suppress:
            def __enter__(self_inner):
                rec._suppressing = True
                return self_inner

            def __exit__(self_inner, *exc):
                rec._suppressing = False
                return False

        pkg = ModuleType("tools")
        pkg.__path__ = []  # mark as a package so submodule imports resolve here
        common = ModuleType("tools.mcp_tool_common")
        common._core = rec.core
        config = ModuleType("tools.mcp_tool_config")
        config._load_mcp_config = lambda: dict(rec.configured)
        discovery = ModuleType("tools.mcp_tool_discovery")
        discovery._enabled = _enabled
        discovery.discover_mcp_tools = discover_mcp_tools
        discovery._forget_lazy_server = _forget_lazy_server
        lifecycle = ModuleType("tools.mcp_tool_lifecycle")
        lifecycle.shutdown_mcp_servers = shutdown_mcp_servers
        scope = ModuleType("tools.mcp_tool_scope")
        scope._key_name = lambda key: key[1] if isinstance(key, tuple) else key
        oauth = ModuleType("tools.mcp_oauth")
        oauth.suppress_interactive_oauth = lambda: _Suppress()
        registry_mod = ModuleType("tools.registry")
        registry_mod.registry = fake_registry
        registration = ModuleType("tools.mcp_tool_registration")
        registration._deregister_mcp_tool_all_scopes = _deregister_mcp_tool_all_scopes

        modules = {
            "tools": pkg,
            "tools.mcp_tool_common": common,
            "tools.mcp_tool_config": config,
            "tools.mcp_tool_discovery": discovery,
            "tools.mcp_tool_lifecycle": lifecycle,
            "tools.mcp_tool_registration": registration,
            "tools.mcp_tool_scope": scope,
            "tools.mcp_oauth": oauth,
            "tools.registry": registry_mod,
        }
        for name, module in modules.items():
            monkeypatch.setitem(sys.modules, name, module)
        return rec

    yield install
    state.clear()


STUDIO = {"command": "/usr/bin/node", "args": ["studio.mjs"], "enabled": True}
FEISHU = {"url": "https://project.feishu.cn/mcp_server/v1", "auth": "oauth"}
FIGMA = {"url": "https://mcp.figma.com/mcp", "auth": "oauth"}


def test_configured_servers_are_connected_for_a_webui_turn(core_stub):
    rec = core_stub({"hermes-studio": STUDIO, "FeishuProjectMcp": FEISHU})

    names = mcp_servers.register_profile_mcp_servers(
        ["terminal", "web", "hermes-studio", "FeishuProjectMcp"],
        platform_key="webui",
        declared_mcp_servers=rec.configured,
    )

    assert rec.discovered_with == [["FeishuProjectMcp", "hermes-studio"]]
    assert names == ["mcp__FeishuProjectMcp__probe", "mcp__hermes-studio__probe"]


def test_discovery_runs_with_interactive_oauth_suppressed(core_stub):
    """Nobody watches a subprocess' stdout: an expired token must park, not open a tab."""
    rec = core_stub({"hermes-studio": STUDIO})

    mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured)

    assert rec.oauth_suppressed_during_discovery is True


def test_a_toolset_name_this_profile_did_not_configure_can_never_start_a_server(core_stub):
    """Identity isolation: profile B naming profile A's server starts nothing.

    The allowed set is intersected with THIS profile's own ``config.yaml``; a toolset
    list is not a capability grant. Without this, a stale or forged toolset entry
    would reach another tenant's MCP process and its token file.
    """
    rec = core_stub({"hermes-studio": STUDIO})

    mcp_servers.register_profile_mcp_servers(
        ["hermes-studio", "FeishuProjectMcp", "figma", "some-other-tenant-server"],
        platform_key="webui",
        declared_mcp_servers=rec.configured,
    )

    assert rec.discovered_with == [["hermes-studio"]]


def test_explicit_subset_is_honored_not_widened(core_stub):
    """A profile in ``explicit`` toolsets mode gets exactly the servers it named."""
    rec = core_stub({"hermes-studio": STUDIO, "FeishuProjectMcp": FEISHU, "figma": FIGMA})

    mcp_servers.register_profile_mcp_servers(["terminal", "figma"], platform_key="webui", declared_mcp_servers=rec.configured)

    assert rec.discovered_with == [["figma"]]


def test_disabled_server_is_never_started(core_stub):
    rec = core_stub({"hermes-studio": dict(STUDIO, enabled=False), "figma": FIGMA})

    mcp_servers.register_profile_mcp_servers(["hermes-studio", "figma"], platform_key="webui", declared_mcp_servers=rec.configured)

    assert rec.discovered_with == [["figma"]]


def test_no_mcp_sentinel_starts_nothing(core_stub):
    rec = core_stub({"hermes-studio": STUDIO})

    assert mcp_servers.register_profile_mcp_servers(
        ["no_mcp", "hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured
    ) == []
    assert rec.discovered_with == []


def test_none_toolsets_means_every_server_this_profile_configured(core_stub):
    """``None`` is core's "you decide" — for MCP that is this profile's own list."""
    rec = core_stub({"hermes-studio": STUDIO, "figma": FIGMA})

    mcp_servers.register_profile_mcp_servers(None, platform_key="webui", declared_mcp_servers=rec.configured)

    assert rec.discovered_with == [["figma", "hermes-studio"]]


def test_revoked_server_is_torn_down_before_discovery(core_stub):
    """Figma drops its whole ``mcp_servers`` entry on revoke.

    The warm worker is one long-lived process per profile, so a connection from an
    earlier turn survives the revoke unless it is explicitly shut down.
    """
    rec = core_stub({"hermes-studio": STUDIO}, live=["hermes-studio", "figma"])

    mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured)

    assert rec.shutdown_names == [["figma"]]
    assert "figma" not in rec.core._servers
    assert rec.discovered_with == [["hermes-studio"]]


def test_revoked_server_loses_its_lazy_schema_cache_registration(core_stub):
    """A lazy registration keeps cached tools callable and would respawn the server."""
    rec = core_stub({"hermes-studio": STUDIO}, live=["hermes-studio"], lazy=["figma"])

    mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured)

    assert rec.forgotten_lazy == ["figma"]
    assert "figma" not in rec.core._lazy_server_configs


def test_a_server_that_cannot_connect_does_not_break_the_turn(core_stub):
    """SPEC: park + warn. The remaining tools must still reach the agent."""
    rec = core_stub({"hermes-studio": STUDIO}, discovery_error=RuntimeError("stdio spawn failed"))

    assert mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured) == []
    assert rec.discovered_with == [["hermes-studio"]]


def test_unreadable_mcp_config_does_not_break_the_turn(core_stub, monkeypatch):
    rec = core_stub({"hermes-studio": STUDIO})

    def _boom():
        raise OSError("config.yaml is being rewritten")

    monkeypatch.setattr(sys.modules["tools.mcp_tool_config"], "_load_mcp_config", _boom)

    assert mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured) == []


@pytest.mark.parametrize("platform_key", ["feishu", "discord", "", None])
def test_non_webui_platforms_are_out_of_scope_for_this_slug(core_stub, platform_key):
    """Same root cause, but the Feishu channel needs its own live verification first."""
    rec = core_stub({"hermes-studio": STUDIO})

    assert mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key=platform_key, declared_mcp_servers=rec.configured) == []
    assert rec.discovered_with == []


def test_kill_switch_stops_registration(core_stub, monkeypatch):
    rec = core_stub({"hermes-studio": STUDIO})
    monkeypatch.setenv("HERMES_MULTITENANCY_DISABLE_PROFILE_MCP", "1")

    assert mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured) == []
    assert rec.discovered_with == []


def test_kill_switch_reaches_the_child_process():
    """The gate is evaluated INSIDE the AIAgent child; an un-allowlisted name is inert."""
    assert "HERMES_MULTITENANCY_DISABLE_PROFILE_MCP" in agent_real._SUBPROCESS_ENV_ALLOWLIST


def test_registration_happens_before_the_agent_is_constructed():
    """Read-back guard: core resolves the tool list in ``AIAgent.__init__``.

    Registering after construction would leave the agent with the pre-MCP tool list
    while every log line claimed the servers were connected — the exact silent
    degradation this slug exists to remove.
    """
    import inspect

    from hermes_multitenancy.agent_real import run as run_mod

    source = inspect.getsource(run_mod._run_with_aiagent)
    register_at = source.index("_register_profile_mcp_servers(")
    construct_at = source.index("agent = AIAgent(**agent_kwargs)")
    assert register_at < construct_at


def test_two_profiles_in_sequence_never_share_servers(core_stub):
    """Warm workers are per profile, but the ledgers are process-global.

    Simulates the worst case anyway: the same process serves profile A, then a
    config that has none of A's servers. A's connection must be gone and must not
    appear in B's discovery call.
    """
    rec_a = core_stub({"hermes-studio": STUDIO, "figma": FIGMA})
    mcp_servers.register_profile_mcp_servers(
        ["hermes-studio", "figma"], platform_key="webui", declared_mcp_servers=rec_a.configured
    )
    assert rec_a.discovered_with == [["figma", "hermes-studio"]]

    rec_b = core_stub({"other-tenant-server": {"url": "https://example.invalid/mcp"}},
                      live=["hermes-studio", "figma"])
    mcp_servers.register_profile_mcp_servers(
        ["hermes-studio", "figma", "other-tenant-server"],
        platform_key="webui",
        declared_mcp_servers=rec_b.configured,
    )

    assert rec_b.shutdown_names == [["figma", "hermes-studio"]]
    assert rec_b.discovered_with == [["other-tenant-server"]]
    # A's two connections are gone and B holds only its own. (Before the registry
    # stub modelled connect, this read ``_servers == {}`` — the stub's discovery
    # simply never recorded the server it connected.)
    assert set(rec_b.core._servers) == {"other-tenant-server"}
    assert _listed_tools(rec_b) == ["mcp__other-tenant-server__probe"]


def test_module_never_touches_the_filesystem_itself(core_stub):
    """The only source of server names is core's own profile-scoped config loader.

    ``_load_mcp_config()`` reads ``HERMES_HOME``, pinned to the profile home by
    ``agent_real/run.py``. Resolving a profile path here would reintroduce exactly
    the cross-tenant seam the intersection above closes, so the module body must
    contain no filesystem or profile-path primitive at all.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(mcp_servers))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & {"open", "Path", "get_hermes_home", "expanduser"}
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert "pathlib" not in imported


# ── review round 1 (codex, 2026-09-21): the two #p1 scope findings ──────────────

def test_a_feishu_turn_cannot_inherit_the_mcp_tools_a_webui_turn_registered(core_stub):
    """Finding ``platform-gate-retains-live-tools#p1``.

    Skipping discovery for a non-WebUI platform left whatever an earlier WebUI turn
    registered live and callable. ``warm_worker._aiagent_warm_worker_key`` keys a
    worker by profile + event loop only, ``streaming.py`` hands a worker its next
    turn without looking at the platform, and the Feishu default toolset contains
    the profile's configured MCP names — so "WebUI turn, then Feishu turn, same
    process" is the ordinary case. The Feishu turn must not be able to list or call
    the WebUI turn's MCP tools.
    """
    rec = core_stub({"hermes-studio": dict(STUDIO, tools=["read_file", "write_file"])})

    mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured)
    assert _listed_tools(rec) == [
        "mcp__hermes-studio__read_file",
        "mcp__hermes-studio__write_file",
    ]
    assert _call_tool(rec, "mcp__hermes-studio__write_file") == "mcp__hermes-studio__write_file"

    # Next turn, same process, same registry, different platform.
    assert mcp_servers.register_profile_mcp_servers(
        ["terminal", "hermes-studio"], platform_key="feishu", declared_mcp_servers=rec.configured
    ) == []

    assert rec.discovered_with == [["hermes-studio"]], "the Feishu turn must not discover"
    assert rec.shutdown_names == [["hermes-studio"]], "the Feishu turn must tear down"
    assert _listed_tools(rec) == [], "no MCP tool may be listed for the Feishu turn"
    assert rec.core._servers == {}
    for tool in ("mcp__hermes-studio__read_file", "mcp__hermes-studio__write_file"):
        with pytest.raises(LookupError):
            _call_tool(rec, tool)


def test_the_kill_switch_also_releases_servers_an_earlier_turn_registered(core_stub, monkeypatch):
    """Same gate, other reason: flipping the kill switch must disarm a warm worker."""
    rec = core_stub({"hermes-studio": dict(STUDIO, tools=["read_file"])})

    mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured)
    assert _listed_tools(rec) == ["mcp__hermes-studio__read_file"]

    monkeypatch.setenv("HERMES_MULTITENANCY_DISABLE_PROFILE_MCP", "1")
    assert mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured) == []

    assert rec.shutdown_names == [["hermes-studio"]]
    assert _listed_tools(rec) == []


def test_narrowing_a_still_enabled_servers_tool_filter_takes_effect_next_turn(core_stub):
    """Finding ``same-name-config-changes-ignored#p1``.

    Staleness was computed by server NAME only. Core's ``_select_new_servers`` skips
    a name it already holds live or lazy, so a second ``discover_mcp_tools`` never
    re-reads that server's own config: an admin who narrows a still-enabled server's
    ``tools`` filter to drop a write tool would keep getting the old tool on every
    later WebUI turn, with the restriction silently not applying.
    """
    configured = {"FeishuProjectMcp": dict(FEISHU, tools=["read_issue", "write_issue"])}
    rec = core_stub(configured)

    mcp_servers.register_profile_mcp_servers(["FeishuProjectMcp"], platform_key="webui", declared_mcp_servers=rec.configured)
    assert _listed_tools(rec) == [
        "mcp__FeishuProjectMcp__read_issue",
        "mcp__FeishuProjectMcp__write_issue",
    ]

    # Round two: the admin removes the write tool. Same name, still enabled.
    rec.configured["FeishuProjectMcp"] = dict(FEISHU, tools=["read_issue"])
    mcp_servers.register_profile_mcp_servers(["FeishuProjectMcp"], platform_key="webui", declared_mcp_servers=rec.configured)

    assert rec.shutdown_names == [["FeishuProjectMcp"]], "the changed server must be dropped first"
    assert rec.discovered_with == [["FeishuProjectMcp"], ["FeishuProjectMcp"]]
    assert _listed_tools(rec) == ["mcp__FeishuProjectMcp__read_issue"]
    with pytest.raises(LookupError):
        _call_tool(rec, "mcp__FeishuProjectMcp__write_issue")


def test_changed_connection_params_reconnect_rather_than_keep_the_old_process(core_stub):
    """The drift check covers connection params and trust, not only the tools filter."""
    rec = core_stub({"hermes-studio": dict(STUDIO, command="/usr/bin/node", trust=False)})

    mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured)
    assert rec.shutdown_names == []

    rec.configured["hermes-studio"] = dict(STUDIO, command="/opt/node/bin/node", trust=True)
    mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured)

    assert rec.shutdown_names == [["hermes-studio"]]
    assert rec.discovered_with == [["hermes-studio"], ["hermes-studio"]]


def test_an_unchanged_config_is_not_torn_down_and_respawned_every_turn(core_stub):
    """Guard on the fix: a cold stdio spawn costs 10-60s, so no config change, no churn."""
    rec = core_stub({"hermes-studio": dict(STUDIO, tools=["read_file"])})

    for _ in range(3):
        mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured)

    assert rec.shutdown_names == []
    assert _listed_tools(rec) == ["mcp__hermes-studio__read_file"]


def test_a_failed_teardown_keeps_the_server_tracked_for_the_next_turn(core_stub):
    """A server that survives shutdown must stay known, not become untracked and live."""
    rec = core_stub({"hermes-studio": dict(STUDIO, tools=["read_file"])})
    mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured)

    def _shutdown_that_does_nothing(*, scope=None, names=None):
        rec.shutdown_names.append(sorted(names or ()))

    lifecycle = sys.modules["tools.mcp_tool_lifecycle"]
    working_shutdown = lifecycle.shutdown_mcp_servers
    lifecycle.shutdown_mcp_servers = _shutdown_that_does_nothing

    assert mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="feishu", declared_mcp_servers=rec.configured) == []
    assert rec.shutdown_names == [["hermes-studio"]]
    assert "hermes-studio" in mcp_servers._REGISTERED_CONFIGS

    # Next non-WebUI turn retries the teardown instead of forgetting the server.
    lifecycle.shutdown_mcp_servers = working_shutdown
    assert mcp_servers.register_profile_mcp_servers(["hermes-studio"], platform_key="feishu", declared_mcp_servers=rec.configured) == []
    assert rec.shutdown_names[-1] == ["hermes-studio"]
    assert _listed_tools(rec) == []
    assert "hermes-studio" not in mcp_servers._REGISTERED_CONFIGS


# ── review round 2 (codex, 2026-09-22): the two #p1 findings ────────────────────
#
# Finding A ``teardown-failure-accepted-as-reconciled#p1``: the residual set
# ``_drop_servers`` returns was dropped on the floor and the config snapshot was
# then overwritten unconditionally, so a teardown that never completed was
# recorded as the new effective state — the old, wider capability stayed live and
# nothing would ever reconcile it again.
# Finding B ``merged-config-exceeds-profile-declarations#p1``: core's
# ``_load_mcp_config()`` merges plugin-provided (portable) MCP servers into what it
# returns, so its keys are not this profile's declarations.


def _shutdown_that_never_completes(rec):
    """A teardown that RETURNS without tearing anything down.

    Core's real one does exactly this on its 15s ``future.result`` timeout and on
    the 10s per-task cancel in ``MCPServerTask.shutdown`` — the connection stays in
    ``_servers`` and the tools stay in the registry.
    """

    def _shutdown(*, scope=None, names=None):
        rec.shutdown_names.append(sorted(names or ()))

    return _shutdown


def _set_shutdown(fn):
    lifecycle = sys.modules["tools.mcp_tool_lifecycle"]
    previous = lifecycle.shutdown_mcp_servers
    lifecycle.shutdown_mcp_servers = fn
    return previous


def test_a_narrowed_server_whose_teardown_failed_is_not_callable_that_turn(core_stub):
    """Finding A, the reviewer's three-round reproduction.

    Round 1 registers read+write. The admin drops ``write_issue``. Round 2's
    teardown returns without completing — the old connection, with the WIDE tool
    filter, is still up. Round 2 must therefore not treat the narrowed config as
    effective: ``write_issue`` may not be listed or dispatched in that turn, and
    round 3 must still see the server as drifted and redo the teardown.
    """
    configured = {"FeishuProjectMcp": dict(FEISHU, tools=["read_issue", "write_issue"])}
    rec = core_stub(configured)

    mcp_servers.register_profile_mcp_servers(
        ["FeishuProjectMcp"], platform_key="webui", declared_mcp_servers=rec.configured
    )
    wide_snapshot = mcp_servers._REGISTERED_CONFIGS["FeishuProjectMcp"]
    assert _call_tool(rec, "mcp__FeishuProjectMcp__write_issue")

    # Round 2: the admin removes the write tool, and teardown does not complete.
    rec.configured["FeishuProjectMcp"] = dict(FEISHU, tools=["read_issue"])
    working_shutdown = _set_shutdown(_shutdown_that_never_completes(rec))
    assert mcp_servers.register_profile_mcp_servers(
        ["FeishuProjectMcp"], platform_key="webui", declared_mcp_servers=rec.configured
    ) == []

    assert rec.shutdown_names == [["FeishuProjectMcp"]], "round 2 must attempt the teardown"
    assert rec.discovered_with == [["FeishuProjectMcp"]], (
        "round 2 must NOT hand an unreconciled server to discovery — core's "
        "_select_new_servers would skip the name and silently keep the old connection"
    )
    assert _listed_tools(rec) == [], "an unreconciled server contributes no tools to this turn"
    with pytest.raises(LookupError):
        _call_tool(rec, "mcp__FeishuProjectMcp__write_issue")
    assert mcp_servers._REGISTERED_CONFIGS["FeishuProjectMcp"] == wide_snapshot, (
        "the narrowed config must NOT be recorded as effective while the old server lives"
    )

    # Round 3: teardown works again. The drift is still pending, so it is redone.
    _set_shutdown(working_shutdown)
    mcp_servers.register_profile_mcp_servers(
        ["FeishuProjectMcp"], platform_key="webui", declared_mcp_servers=rec.configured
    )

    assert rec.shutdown_names == [["FeishuProjectMcp"], ["FeishuProjectMcp"]]
    assert rec.discovered_with == [["FeishuProjectMcp"], ["FeishuProjectMcp"]]
    assert _listed_tools(rec) == ["mcp__FeishuProjectMcp__read_issue"]
    with pytest.raises(LookupError):
        _call_tool(rec, "mcp__FeishuProjectMcp__write_issue")
    assert mcp_servers._REGISTERED_CONFIGS["FeishuProjectMcp"] != wide_snapshot


def test_a_revoked_server_whose_teardown_failed_loses_its_tools_anyway(core_stub):
    """Finding A, revoke path: Figma drops its whole ``mcp_servers`` entry.

    Closing the connection and revoking the capability are two different things.
    The process may survive a failed teardown; the tools must not.
    """
    rec = core_stub({"hermes-studio": dict(STUDIO, tools=["read_file", "write_file"])})
    mcp_servers.register_profile_mcp_servers(
        ["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured
    )
    assert _listed_tools(rec) == [
        "mcp__hermes-studio__read_file",
        "mcp__hermes-studio__write_file",
    ]

    rec.configured.clear()  # the admin revoked the server outright
    _set_shutdown(_shutdown_that_never_completes(rec))
    assert mcp_servers.register_profile_mcp_servers(
        [], platform_key="webui", declared_mcp_servers=rec.configured
    ) == []

    assert _listed_tools(rec) == []
    for tool in ("mcp__hermes-studio__read_file", "mcp__hermes-studio__write_file"):
        with pytest.raises(LookupError):
            _call_tool(rec, tool)
    assert rec.core._servers, "the live connection is what survived — that is the premise"
    assert "hermes-studio" in mcp_servers._REGISTERED_CONFIGS, "so the next turn retries it"


def test_the_platform_gate_revokes_tools_of_a_server_that_survived_teardown(core_stub):
    """Finding A on the gate path: ``run.py`` builds the agent regardless.

    ``_release_registered_servers`` used to only LOG a residual server, and
    ``run.py`` went straight on to ``AIAgent(**agent_kwargs)`` — so a Feishu turn
    could still list and dispatch the WebUI turn's MCP tools.
    """
    rec = core_stub({"hermes-studio": dict(STUDIO, tools=["read_file", "write_file"])})
    mcp_servers.register_profile_mcp_servers(
        ["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured
    )
    assert _call_tool(rec, "mcp__hermes-studio__write_file")

    _set_shutdown(_shutdown_that_never_completes(rec))
    assert mcp_servers.register_profile_mcp_servers(
        ["terminal", "hermes-studio"], platform_key="feishu", declared_mcp_servers=rec.configured
    ) == []

    assert _listed_tools(rec) == []
    with pytest.raises(LookupError):
        _call_tool(rec, "mcp__hermes-studio__write_file")
    assert "hermes-studio" in mcp_servers._REGISTERED_CONFIGS


def test_a_teardown_that_raises_is_a_failed_teardown_not_a_reconciliation(core_stub):
    """Finding A, unknown-outcome path: an exception proves nothing was cleaned up."""
    rec = core_stub({"hermes-studio": dict(STUDIO, tools=["read_file", "write_file"])})
    mcp_servers.register_profile_mcp_servers(
        ["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured
    )
    wide_snapshot = mcp_servers._REGISTERED_CONFIGS["hermes-studio"]

    def _shutdown_that_explodes(*, scope=None, names=None):
        rec.shutdown_names.append(sorted(names or ()))
        raise RuntimeError("MCP loop is gone")

    _set_shutdown(_shutdown_that_explodes)
    rec.configured["hermes-studio"] = dict(STUDIO, tools=["read_file"])
    assert mcp_servers.register_profile_mcp_servers(
        ["hermes-studio"], platform_key="webui", declared_mcp_servers=rec.configured
    ) == []

    assert _listed_tools(rec) == []
    with pytest.raises(LookupError):
        _call_tool(rec, "mcp__hermes-studio__write_file")
    assert rec.discovered_with == [["hermes-studio"]], "no second discovery this turn"
    assert mcp_servers._REGISTERED_CONFIGS["hermes-studio"] == wide_snapshot


PLUGIN_EXTRA = {"command": "/usr/bin/node", "args": ["plugin-extra.mjs"], "tools": ["exfil"]}


def test_a_plugin_supplied_mcp_server_is_never_started(core_stub):
    """Finding B: ``_load_mcp_config()`` is a MERGE, not this profile's declarations.

    Core's loader ends with ``_portable_mcp_servers(safe_servers)``, which folds
    every plugin-provided MCP server into its result, and
    ``tools_config.enabled_mcp_server_names()`` folds those names into the default
    toolset. An employee who installs such a plugin would otherwise get a server
    started that their ``config.yaml`` never declared.
    """
    rec = core_stub({"plugin-extra": PLUGIN_EXTRA})  # what the merged loader returns

    assert mcp_servers.register_profile_mcp_servers(
        ["terminal", "plugin-extra"], platform_key="webui", declared_mcp_servers={}
    ) == []

    assert rec.discovered_with == []
    assert _listed_tools(rec) == []


def test_a_toolset_entry_naming_a_plugin_server_does_not_widen_the_scope(core_stub):
    """Finding B: the default toolset carries the plugin's name; it is not a grant."""
    rec = core_stub({"hermes-studio": STUDIO, "plugin-extra": PLUGIN_EXTRA})

    mcp_servers.register_profile_mcp_servers(
        ["terminal", "hermes-studio", "plugin-extra"],
        platform_key="webui",
        declared_mcp_servers={"hermes-studio": STUDIO},
    )

    assert rec.discovered_with == [["hermes-studio"]]
    assert _listed_tools(rec) == ["mcp__hermes-studio__probe"]


def test_no_declarations_means_no_servers_even_with_core_deciding_toolsets(core_stub):
    """Finding B: ``enabled_toolsets is None`` is "core decides", not "start the merge"."""
    rec = core_stub({"plugin-extra": PLUGIN_EXTRA})

    assert mcp_servers.register_profile_mcp_servers(
        None, platform_key="webui", declared_mcp_servers=None
    ) == []

    assert rec.discovered_with == []


def test_a_plugin_server_a_previous_turn_started_is_torn_down(core_stub):
    """Finding B, warm worker: a pre-fix turn's plugin server must not survive."""
    rec = core_stub({"plugin-extra": PLUGIN_EXTRA}, live=["plugin-extra"],
                    tools=["mcp__plugin-extra__exfil"])

    assert mcp_servers.register_profile_mcp_servers(
        ["plugin-extra"], platform_key="webui", declared_mcp_servers={}
    ) == []

    assert rec.shutdown_names == [["plugin-extra"]]
    assert _listed_tools(rec) == []


def test_run_py_hands_over_the_profiles_raw_declarations():
    """Finding B wiring: the authoritative scope must come from ``config.yaml``.

    Read back from ``run.py``'s AST rather than from a log line: the whole finding
    is that a plausible-looking source is the wrong one, so the test has to name
    WHICH expression is passed. The merged ``config`` dict in scope right there is
    itself wrong — ``_load_profile_config`` deep-merges the shared home into it.
    """
    import ast
    import inspect

    from hermes_multitenancy.agent_real import run as run_mod

    tree = ast.parse(inspect.getsource(run_mod._run_with_aiagent))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_register_profile_mcp_servers"
    ]
    assert len(calls) == 1
    declared = {kw.arg: kw.value for kw in calls[0].keywords}.get("declared_mcp_servers")
    assert declared is not None, "run.py must pass the profile's own declarations"
    assert isinstance(declared, ast.Call) and isinstance(declared.func, ast.Name)
    assert declared.func.id == "_profile_declared_mcp_servers"
    assert [arg.id for arg in declared.args] == ["profile_home"]


def test_profile_declarations_come_from_the_profiles_own_config_yaml(tmp_path):
    """Finding B source: only ``<profile>/config.yaml`` may declare a server.

    Neither core's plugin-merged loader nor ``_load_profile_config``'s shared-home
    deep merge is an acceptable stand-in, so the reader takes the profile file and
    nothing else.
    """
    from hermes_multitenancy.agent_real import run as run_mod

    (tmp_path / "config.yaml").write_text(
        "model:\n  default: openai/gpt-4o\n"
        "mcp_servers:\n  hermes-studio:\n    command: /usr/bin/node\n"
    )

    assert run_mod._profile_declared_mcp_servers(tmp_path) == {
        "hermes-studio": {"command": "/usr/bin/node"}
    }


def test_a_profile_with_no_config_file_declares_nothing(tmp_path):
    """Unknown scope fails closed: no declarations, no servers."""
    from hermes_multitenancy.agent_real import run as run_mod

    assert run_mod._profile_declared_mcp_servers(tmp_path) is None


def test_a_non_mapping_config_declares_nothing(tmp_path):
    """A config.yaml caught mid-rewrite must not raise out of the turn either."""
    from hermes_multitenancy.agent_real import run as run_mod

    (tmp_path / "config.yaml").write_text("just a string\n")

    assert run_mod._profile_declared_mcp_servers(tmp_path) is None
