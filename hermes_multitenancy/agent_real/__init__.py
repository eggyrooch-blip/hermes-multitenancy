"""Re-export shim for the agent_real package (pure-move god-node split).

Every original top-level symbol of ``agent_real.py`` remains importable from
``hermes_multitenancy.agent_real``.  Mutable module state and monkeypatch-able
symbols have a single canonical owner submodule and are proxied live (read AND
write) by ``_ShimModule`` below -- never snapshot-copied -- so that
``monkeypatch.setattr(agent_real, name, x)`` and ``global name = x`` inside the
owner stay consistent with every reader.
"""
from __future__ import annotations

import sys as _sys
from types import ModuleType as _ModuleType

from . import _core
from . import cred_signal
from . import expert_scope
from . import subprocess_env
from . import warm_worker
from . import run
from . import streaming

_SUBMODULES = (
    _core, cred_signal, expert_scope, subprocess_env, warm_worker, run, streaming,
)

# name -> canonical owner submodule. NEVER snapshot-copied into the shim dict;
# reads miss the shim dict and fall through to ``__getattr__`` (live), writes
# route to the owner via ``__setattr__``.
_LIVE_STATE_OWNERS = {
    # --- monkeypatch-able symbols owned by _core ---
    "_BWRAP_ARGS_FILE": _core,
    "_BWRAP_EXEC": _core,
    "_SANDBOX_EXEC": _core,
    "_SANDBOX_POLICY_FILE": _core,
    "_aiagent_subprocess_env_scope": _core,
    "_analyze_webui_uploaded_image": _core,
    "_lark_cli_auth_broker_scope": _core,
    "_legacy_real_run_agent": _core,
    "_load_profile_config": _core,
    "_resolve_shared_hermes_home": _core,
    "_role_override_block_for_event": _core,
    "_run_aiagent_subprocess": _core,
    "real_run_agent": _core,
    "stream_run_agent": _core,
    "start_lark_cli_auth_broker_server": _core,
    # --- monkeypatch-able symbols owned by domain submodules ---
    "_apply_expert_skill_scope_for_aiagent": expert_scope,
    "_resolve_enabled_toolsets": subprocess_env,
    "_run_with_aiagent": run,
    "_stream_aiagent_subprocess": streaming,
    "_stream_loop": streaming,
    # --- reassigned-via-global module state ---
    "_EXECUTE_CODE_PROFILE_CHILD_ENV_PATCH_REFS": _core,
    # --- identity-sensitive locks ---
    "_EXECUTE_CODE_PROFILE_CHILD_ENV_LOCK": _core,
    "_EXPERT_SKILL_SCOPE_LOCK": expert_scope,
    "_AIAGENT_WARM_WORKERS_GUARD": warm_worker,
    # --- reassigned/cleared mutable containers ---
    "_AIAGENT_WARM_WORKERS": warm_worker,
    "_AIAGENT_WARM_PROFILE_LOCKS": warm_worker,
}

_g = globals()
_PKG_NAME = __name__  # "hermes_multitenancy.agent_real"
_SUBMODULE_NAMES = {_sm.__name__ for _sm in _SUBMODULES}


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


# 0) faithful re-export: every function/class physically moved into a submodule
#    reports the package as its ``__module__`` (as it did before the split), so
#    introspection (inspect, repr, pickling) is unchanged for external callers.
for _sm in _SUBMODULES:
    for _obj in vars(_sm).values():
        if getattr(_obj, "__module__", None) in _SUBMODULE_NAMES:
            try:
                _obj.__module__ = _PKG_NAME
            except (AttributeError, TypeError):
                pass

# 1) snapshot-copy every non-live-state symbol from each submodule onto the shim,
#    so `from agent_real import <anything>` and `agent_real.<anything>` work.
for _sm in _SUBMODULES:
    for _name, _val in vars(_sm).items():
        if _is_dunder(_name) or _name in _LIVE_STATE_OWNERS:
            continue
        _g[_name] = _val

# 2) give every submodule the full flat namespace (minus live-state) so bare
#    cross-module references resolve exactly as they did in the single module.
_MERGED = {k: v for k, v in _g.items() if not _is_dunder(k)}
for _sm in _SUBMODULES:
    _d = _sm.__dict__
    for _k, _v in _MERGED.items():
        if _k not in _d:
            _d[_k] = _v


class _ShimModule(_ModuleType):
    def __getattr__(self, name):  # only fires on normal-lookup miss
        owner = _LIVE_STATE_OWNERS.get(name)
        if owner is not None:
            return getattr(owner, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        owner = _LIVE_STATE_OWNERS.get(name)
        if owner is not None:
            setattr(owner, name, value)
        else:
            super().__setattr__(name, value)


_sys.modules[__name__].__class__ = _ShimModule
