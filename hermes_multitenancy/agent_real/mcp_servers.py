"""Connect this profile's own ``mcp_servers`` inside the AIAgent subprocess.

Why this module exists
----------------------
In hermes-agent, MCP discovery is a **host startup** step, never something
``AIAgent.__init__`` does: the CLI runs it in ``hermes_cli/mcp_startup.py``, the
gateway in ``gateway/run.py::_discover_gateway_mcp_tools``, cron in
``cron/scheduler.py``, the TUI in ``tui_gateway/methods_tools.py``. Multitenancy
is a fourth host — ``aiagent_subprocess.py`` shells out to a fresh interpreter and
calls ``run_agent.AIAgent`` directly — and it never had that step. Measured on a
real WebUI turn (2026-09-21, profile ``feishu_g41a5b5g``): the child's
``_load_mcp_config()`` returned both configured servers, ``enabled_toolsets``
carried both names into ``AIAgent``, and yet every server sat at
``status="configured"`` with ``_servers == {}`` and zero registered MCP tools.
``enabled_toolsets`` is only an allow-filter over tools that already exist; a
server nobody connected contributes no tools to filter.

Boundary rules (these are the security contract, not style)
-----------------------------------------------------------
* **Only this profile's own servers.** The name list comes from
  ``_load_mcp_config()``, which reads ``HERMES_HOME`` — pinned to the profile home
  by ``agent_real/run.py``. A toolset name that is not in *this* profile's
  ``config.yaml`` can never start a server, so a leaked/forged toolset entry
  cannot reach another tenant's MCP process or token.
* **Allowlist, never "everything configured".** ``discover_mcp_tools`` is called
  with an explicit ``allowed_mcp_names``, so a profile in ``explicit`` toolsets
  mode that names a subset gets exactly that subset.
* **Stale connections are torn down first.** The warm worker is a long-lived
  process pinned to ONE profile (``warm_worker._aiagent_warm_worker_key``), so a
  server that has since been revoked (Figma drops its whole ``mcp_servers`` entry
  on revoke) would otherwise stay live and callable for later turns.
* **The platform gate tears down, it does not merely skip.** The same warm worker
  serves consecutive turns from different platforms, so an out-of-scope platform
  has to hand back what an earlier in-scope turn registered. See
  ``_release_registered_servers``.
* **A changed config is a new server.** Core keys "already registered" on the
  server NAME, so a same-named server whose config changed must be deregistered
  before discovery or the change silently never applies. See ``_config_fingerprint``.
* **A teardown that did not complete is not a reconciliation.** Core's
  ``shutdown_mcp_servers`` returns on a 15s ``future.result`` timeout and on a
  10s per-task cancel, so "it returned" proves nothing. A server still listed
  after teardown keeps its OLD config snapshot (so the next turn retries) and has
  its tool registrations revoked (so it cannot be listed or dispatched in THIS
  turn). See ``_drop_servers`` / ``_revoke_tool_registrations``.
* **Plugin-provided MCP servers are not this profile's declarations.** Core's
  ``_load_mcp_config()`` MERGES portable plugin servers into what it returns
  (``mcp_tool_config._portable_mcp_servers``) and ``enabled_mcp_server_names()``
  folds those names into the default toolset. The authoritative name scope is the
  profile's RAW ``config.yaml`` ``mcp_servers`` keys, handed in by
  ``agent_real/run.py``. See ``_declared_server_names``.
* **Never blocking.** Every failure path logs and returns; a server that cannot
  connect is parked by core with a warning and the rest of the turn proceeds with
  the remaining tools, per SPEC.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Sentinel understood by core's ``_merge_mcp_servers``: "this platform wants no
# MCP at all". It is normally stripped before we see it; honored here anyway so
# the two layers can never disagree about what it means.
_NO_MCP_SENTINEL = "no_mcp"

# Platforms whose AIAgent runs register profile MCP servers. WebUI only for now:
# the defect is platform-independent, but the approved SPEC scope for this slug is
# the WebUI turn, and the Feishu channel needs its own live verification before it
# is switched on. A follow-up slug adds "feishu" here — no other change.
_MCP_ENABLED_PLATFORMS = frozenset({"webui"})

# Config snapshot of every server THIS module has registered in THIS process,
# keyed by server name. Process-global on purpose: a warm worker is one
# long-lived process pinned to ONE profile (``warm_worker._aiagent_warm_worker_key``)
# that serves many consecutive turns, so this dict has exactly the lifetime of
# core's own ``_core._servers`` / ``_core._lazy_server_configs`` ledgers. It is
# what lets a later turn know what an earlier turn left connected.
_REGISTERED_CONFIGS: dict[str, str] = {}


def _mcp_registration_enabled(platform_key: str) -> bool:
    return _registration_gate(platform_key)[0]


def _registration_gate(platform_key: str) -> tuple[bool, str]:
    """``(enabled, reason)``. The reason is logged on every teardown, so it names
    which gate closed rather than leaving an unexplained disconnect in prod logs."""
    if os.environ.get("HERMES_MULTITENANCY_DISABLE_PROFILE_MCP") == "1":
        return False, "kill switch HERMES_MULTITENANCY_DISABLE_PROFILE_MCP=1"
    if str(platform_key or "").strip().lower() not in _MCP_ENABLED_PLATFORMS:
        return False, f"platform={platform_key!r} is out of this slug's WebUI-only scope"
    return True, ""


def _config_fingerprint(config: Any) -> str:
    """Stable digest of a server's WHOLE config entry.

    Core's ``_select_new_servers`` decides "already registered" on the server NAME
    alone, so every field that changes what a server IS has to take part in the
    comparison: ``command``/``args``/``env``/``cwd``/``url``/``headers`` (connection
    params), ``tools`` (the permission filter), ``trust``, ``lazy``,
    ``supports_parallel_tool_calls``. Hashing the whole entry is the only version
    that cannot go stale when core grows another key.
    """
    try:
        return json.dumps(config, sort_keys=True, default=repr)
    except Exception:  # pragma: no cover - non-JSONable YAML scalars are pathological
        return repr(config)


def _declared_server_names(declared: Any) -> set[str]:
    """Names of the servers this profile's OWN ``config.yaml`` declares.

    This is NOT ``_load_mcp_config()``. Core's loader ends with
    ``_portable_mcp_servers(safe_servers)``, which MERGES every plugin-provided
    (portable) MCP server into the dict it returns, and
    ``hermes_cli.tools_config.enabled_mcp_server_names()`` folds those same names
    into the default toolset. Taking the loader's keys as "what this profile
    declared" therefore lets an employee who installs such a plugin start a server
    their ``config.yaml`` never names — exactly the source boundary this slug
    exists to hold. ``agent_real/run.py`` hands in the raw
    ``config["mcp_servers"]`` mapping; the VALUES still come from the core loader,
    so its suspicious-server filtering and ``${VAR}`` interpolation are untouched.

    Anything else (``None``, a scalar, an unreadable config) is an EMPTY scope:
    "we do not know what this profile declared" must fail closed, and MCP is an
    enhancement to a turn, never a precondition for it.
    """
    if isinstance(declared, dict):
        return {str(name) for name in declared}
    if isinstance(declared, (list, tuple, set, frozenset)):
        return {str(item) for item in declared}
    return set()


def _allowed_server_names(
    configured: dict[str, Any],
    enabled_toolsets: Optional[Iterable[str]],
    declared: set[str],
) -> set[str]:
    """Server names this run may start.

    ``declared`` ∩ configured-and-enabled ∩ resolved toolsets. ``enabled_toolsets
    is None`` means "core decides" (no profile list, no resolver), which for MCP
    means every server this profile itself DECLARED and left enabled.
    """
    from tools.mcp_tool_discovery import _enabled as _server_enabled

    available = {
        name for name, cfg in configured.items()
        if name in declared and isinstance(cfg, dict) and _server_enabled(cfg)
    }
    if enabled_toolsets is None:
        return available
    names = {str(item) for item in enabled_toolsets}
    if _NO_MCP_SENTINEL in names:
        return set()
    return available & names


def _live_server_names() -> set[str]:
    """Names of MCP servers this process has connected or lazily registered."""
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_scope import _key_name

    with _core._lock:
        keys = list(_core._servers) + list(_core._lazy_server_configs)
    return {_key_name(key) for key in keys}


def _connection_keys_for(names: set[str]) -> dict[str, list]:
    """``{server name: [core connection keys]}`` for the live/lazy ledgers.

    Core keys a connection by ``(scope, name)`` tuples as often as by a bare
    name (``mcp_tool_scope._key_name``), and its deregistration helper wants the
    KEY, not the name — a name-only guess drops the wrong profile's overlay.
    """
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_scope import _key_name

    with _core._lock:
        keys = list(_core._servers) + list(_core._lazy_server_configs)
    found: dict[str, list] = {}
    for key in keys:
        name = _key_name(key)
        if name in names:
            found.setdefault(name, []).append(key)
    return found


def _revoke_tool_registrations(names: set[str]) -> None:
    """Make every tool of ``names`` unlistable and undispatchable for THIS turn.

    Closing a connection and revoking a capability are two different things, and
    only the second one is what the turn's agent sees. Core's
    ``shutdown_mcp_servers`` schedules each server's own ``shutdown`` — which is
    where ``registry.deregister`` runs — on the MCP loop and then waits with
    ``future.result(timeout=15)``; ``MCPServerTask.shutdown`` itself gives up on
    its task after ``asyncio.wait_for(..., timeout=10)`` and cancels it. Either
    path RETURNS while the tools are still in the process-global registry, so the
    turn would list and dispatch a server the admin already revoked.

    Revoking the registration is the part this host can do synchronously and
    verify. The live connection stays tracked (see ``_drop_servers``) so the next
    turn retries the real teardown; every OTHER tool in the registry is untouched.
    """
    if not names:
        return
    try:
        from tools.registry import registry
    except Exception:
        logger.error(
            "[multitenancy] MCP: tool registry unavailable; cannot revoke tools of %s",
            ",".join(sorted(names)), exc_info=True,
        )
        return
    try:
        from tools.mcp_tool_registration import _deregister_mcp_tool_all_scopes
    except Exception:
        _deregister_mcp_tool_all_scopes = None
    try:
        keys_by_name = _connection_keys_for(set(names))
    except Exception:
        keys_by_name = {}

    for name in sorted(names):
        # Core registers every tool of a server under the ``mcp-<name>`` toolset
        # (``mcp_tool_registration._remove_server_scope`` reads it back the same way).
        toolset = f"mcp-{name}"
        try:
            tool_names = list(registry.get_tool_names_for_toolset(toolset) or ())
        except Exception:
            logger.warning(
                "[multitenancy] MCP: listing registered tools of %s failed", name, exc_info=True
            )
            continue
        keys = keys_by_name.get(name) or [name]
        for tool_name in tool_names:
            dropped = False
            if _deregister_mcp_tool_all_scopes is not None:
                for key in keys:
                    try:
                        _deregister_mcp_tool_all_scopes(key, tool_name)
                        dropped = True
                    except Exception:
                        logger.debug(
                            "[multitenancy] MCP: all-scope deregister of %s failed", tool_name,
                            exc_info=True,
                        )
            if not dropped:
                try:
                    registry.deregister(tool_name)
                except Exception:
                    logger.warning(
                        "[multitenancy] MCP: deregistering %s failed", tool_name, exc_info=True
                    )
        # Read the registry BACK: an unrevoked tool is still callable, and saying
        # so in the log is the only way ops can tell this apart from a clean drop.
        try:
            still_registered = list(registry.get_tool_names_for_toolset(toolset) or ())
        except Exception:
            still_registered = []
        if still_registered:
            logger.error(
                "[multitenancy] MCP: tool(s) %s of server %s are STILL registered after "
                "revocation; this turn can still dispatch them",
                ",".join(sorted(still_registered)), name,
            )
        elif tool_names:
            logger.info(
                "[multitenancy] MCP: revoked %d tool registration(s) of server %s",
                len(tool_names), name,
            )


def _drop_servers(names: set[str], reason: str) -> set[str]:
    """Deregister ``names`` and forget their snapshots. Returns the names still live.

    An empty return value is the success case: nothing under these names is listed
    or callable any more. A NON-empty return value is a teardown that did not
    complete: those names keep their old snapshot (the caller must not record the
    new config as effective) and lose their tool registrations here, so the turn
    cannot dispatch them even though the process is still up.
    """
    if not names:
        return set()

    from tools.mcp_tool_common import _core
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.mcp_tool_scope import _key_name

    logger.info("[multitenancy] MCP: dropping server(s) %s — %s", ", ".join(sorted(names)), reason)
    shutdown_mcp_servers(names=set(names))
    # ``shutdown_mcp_servers`` only knows live connections. A schema-cache (lazy)
    # registration keeps its cached tools callable and would respawn the server on
    # first use, so drop those too.
    try:
        from tools.mcp_tool_discovery import _forget_lazy_server

        with _core._lock:
            lazy_keys = [key for key in _core._lazy_server_configs if _key_name(key) in names]
        for key in lazy_keys:
            _forget_lazy_server(key)
    except Exception:
        logger.debug("[multitenancy] MCP: lazy-registration pruning unavailable", exc_info=True)

    # Read the ledgers BACK. A snapshot is only forgotten for a name that is really
    # gone, so a failed teardown stays visible to the next turn instead of becoming
    # an untracked live server nothing will ever clean up or refresh.
    remaining = _live_server_names() & set(names)
    for name in set(names) - remaining:
        _REGISTERED_CONFIGS.pop(name, None)
    if remaining:
        # The connection survived; the CAPABILITY must not. Snapshots for these
        # names are deliberately left in place so the next turn retries teardown.
        _revoke_tool_registrations(remaining)
    return remaining


def _release_registered_servers(reason: str) -> None:
    """Gate closed: hand back everything this module registered in this process.

    Skipping discovery is NOT enough. Warm workers are keyed by profile + event loop
    only (``warm_worker._aiagent_warm_worker_key``) and ``streaming.py`` hands a
    worker its next turn without looking at the platform, while the Feishu default
    toolset contains the profile's configured MCP names. A worker that served a
    WebUI turn would therefore hand the MCP tools registered for WebUI straight to
    the following Feishu turn — outside this slug's approved WebUI-only scope. The
    teardown runs here, before ``AIAgent(**agent_kwargs)``, because core resolves
    the turn's tool list inside ``AIAgent.__init__``.
    """
    tracked = set(_REGISTERED_CONFIGS)
    if not tracked:
        return
    try:
        remaining = _drop_servers(tracked, reason)
    except Exception:
        logger.warning(
            "[multitenancy] MCP: releasing registered server(s) failed — %s", reason, exc_info=True
        )
        return
    if remaining:
        # ``_drop_servers`` has already revoked their tool registrations, so the
        # turn cannot list or dispatch them; the PROCESS is what survived, and it
        # stays tracked so the next turn retries the real teardown.
        logger.error(
            "[multitenancy] MCP: server(s) %s survived teardown — %s; their tools are "
            "revoked for this turn and teardown is retried on the next one",
            ", ".join(sorted(remaining)), reason,
        )


def register_profile_mcp_servers(
    enabled_toolsets: Optional[Iterable[str]],
    *,
    platform_key: str,
    declared_mcp_servers: Any,
) -> list[str]:
    """Connect and register this profile's MCP servers. Returns registered tool names.

    ``declared_mcp_servers`` is the profile's RAW ``config.yaml`` ``mcp_servers``
    mapping (``agent_real/run.py`` reads it from the same profile config it hands
    ``AIAgent``). It is a REQUIRED keyword on purpose: it is the authoritative name
    scope, and a caller that forgets it must fail loudly here rather than silently
    fall back to core's plugin-merged loader. See ``_declared_server_names``.

    Never raises: MCP is an enhancement to a turn, not a precondition for it.
    """
    enabled, gate_reason = _registration_gate(platform_key)
    if not enabled:
        _release_registered_servers(gate_reason)
        return []
    try:
        from tools.mcp_tool_config import _load_mcp_config
        from tools.mcp_tool_discovery import discover_mcp_tools
        from tools.mcp_oauth import suppress_interactive_oauth
    except Exception:
        logger.debug("[multitenancy] MCP: core MCP modules unavailable; skipping", exc_info=True)
        return []

    try:
        configured = _load_mcp_config() or {}
    except Exception:
        logger.warning("[multitenancy] MCP: reading profile mcp_servers failed", exc_info=True)
        return []

    try:
        declared = _declared_server_names(declared_mcp_servers)
        allowed = _allowed_server_names(configured, enabled_toolsets, declared)
    except Exception:
        logger.warning("[multitenancy] MCP: resolving allowed servers failed", exc_info=True)
        return []

    undeclared = set(configured) - declared
    if undeclared:
        # Loud, because this is the merge core does behind ``_load_mcp_config()``:
        # a portable plugin can put a server in front of the loader that the
        # profile never declared. Naming it makes the boundary auditable in prod.
        logger.info(
            "[multitenancy] MCP: ignoring %d server(s) not declared in this profile's "
            "config.yaml (plugin/merged config): %s",
            len(undeclared), ",".join(sorted(undeclared)),
        )

    residual: set[str] = set()
    targets: set[str] = set()
    try:
        live = _live_server_names()
        stale = live - allowed
        # Same-name config drift. ``_select_new_servers`` (core v0.21.3) drops any
        # candidate whose name is already in ``_servers`` / ``_server_connecting`` /
        # ``_lazy_server_configs``, so a repeat ``discover_mcp_tools`` NEVER updates a
        # still-allowed server's own connection args or its ``tools`` permission
        # filter. An admin who narrows a still-enabled server's filter to drop a write
        # tool would otherwise keep handing that tool to every later WebUI turn, with
        # the restriction silently not taking effect. Deregister first, then discover.
        drifted = {
            name
            for name in (allowed & live)
            if name in _REGISTERED_CONFIGS
            and _REGISTERED_CONFIGS[name] != _config_fingerprint(configured.get(name))
        }
        targets = stale | drifted
        if targets:
            reasons = []
            if stale:
                reasons.append("no longer allowed: " + ",".join(sorted(stale)))
            if drifted:
                reasons.append("config changed since registration: " + ",".join(sorted(drifted)))
            residual = _drop_servers(targets, "; ".join(reasons))
    except Exception:
        # A teardown whose OUTCOME is unknown is a failed teardown. Treating every
        # target as residual is the only reading that cannot silently record an
        # unreconciled server as reconciled.
        logger.warning("[multitenancy] MCP: pruning stale servers failed", exc_info=True)
        residual = set(targets)
        try:
            _revoke_tool_registrations(residual)
        except Exception:
            logger.error(
                "[multitenancy] MCP: revoking tools of %s failed after a failed prune",
                ",".join(sorted(residual)), exc_info=True,
            )

    if residual:
        # Not reconciled: the server is still live under its OLD config. It must
        # not be handed to discovery this turn (core's ``_select_new_servers``
        # skips a name it already holds, so discovery would silently keep the old
        # connection) and its snapshot must stay OLD so the next turn retries.
        logger.error(
            "[multitenancy] MCP: teardown incomplete for %s; excluded from this turn and "
            "retried next turn",
            ",".join(sorted(residual)),
        )
        allowed = allowed - residual

    if not allowed:
        logger.debug("[multitenancy] MCP: no allowed servers for platform=%s", platform_key)
        return []

    started = time.monotonic()
    try:
        # No multitenancy run can complete a browser OAuth flow — nobody watches
        # the subprocess' stdout. An expired token must park the server with an
        # actionable warning, exactly as the gateway does, never open a tab.
        with suppress_interactive_oauth():
            tool_names = discover_mcp_tools(allowed_mcp_names=sorted(allowed)) or []
    except Exception:
        logger.warning(
            "[multitenancy] MCP: discovery failed for server(s) %s; the turn continues without them",
            ", ".join(sorted(allowed)),
            exc_info=True,
        )
        return []

    # Snapshot what each server was registered WITH, so the next turn can tell a
    # config change from a no-op. Recorded for every allowed name: a server that
    # failed to connect is not live, so the drift check above skips it anyway and
    # core retries it on the next turn with whatever config is on disk then.
    #
    # ``allowed`` no longer contains any residual name, so a config whose teardown
    # did not complete is never recorded as the effective one — that overwrite is
    # what turned a failed teardown into a permanent, unnoticed capability.
    for name in allowed:
        _REGISTERED_CONFIGS[name] = _config_fingerprint(configured.get(name))

    # ``elapsed`` is the per-turn cost this host pays for MCP. It is the only
    # place the number is readable in production, and a cold stdio spawn or an
    # unreachable HTTP server shows up here first.
    logger.info(
        "[multitenancy] MCP: platform=%s servers=%s registered_tools=%d elapsed=%.3fs",
        platform_key, ",".join(sorted(allowed)), len(tool_names), time.monotonic() - started,
    )
    return list(tool_names)
