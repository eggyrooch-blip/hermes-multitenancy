"""Multi-profile cron worker for multitenancy.

Hermes-agent's built-in cron ticker scans a single ``<HERMES_HOME>/cron/jobs.json``
file. In multitenancy mode each user has their own profile directory under
``<root>/profiles/<name>/``, and ``_configure_cron_home`` lets each AIAgent
subprocess write reminders to its own profile-default cron path
(``<target_profile>/cron/jobs.json``).

This module starts a single background thread inside the gateway process that
periodically scans every profile under ``<root>/profiles/*/cron/jobs.json``.
For each profile it briefly patches hermes-agent's native ``cron.jobs`` and
``cron.scheduler`` globals, claims due jobs, and advances ``next_run_at`` under
that profile's storage context. Expensive job execution then runs through a
bounded cross-profile worker pool, so one slow profile cannot delay later
profiles in the scan order.

The expensive agent run is isolated in a subprocess that only runs the cron job
body (L4 defer / RunBroker / core ``run_job`` fallback). The router parent
process still finalizes the job under the original profile context: output
write, live-adapter delivery, status update, and multitenancy session mirroring.
That keeps Feishu delivery in the process that owns the live gateway adapter.

--------------------------------------------------------------------------------
This file was a ~2600-line god-node. It is now a thin re-export shim over the
``hermes_multitenancy.cron`` package. Every public and private symbol is
re-exported here so that:

  * ``from hermes_multitenancy.cron_worker import X`` keeps working,
  * ``from hermes_multitenancy import cron_worker`` + ``cron_worker.X`` keeps working,
  * ``monkeypatch.setattr(cron_worker, "X", fake)`` keeps working — the sibling
    modules resolve cross-module / patchable helpers through this shim, so a
    patch applied here reaches the caller in another submodule.

The stdlib module imports below are retained so ``cron_worker.subprocess``,
``cron_worker.os``, ``cron_worker.urllib_request``, ``cron_worker.signal`` and
``cron_worker.RunBroker`` remain valid monkeypatch targets.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
from concurrent.futures import TimeoutError as FuturesTimeout
import functools
import json
import math
import signal
import logging
import os
import re
import subprocess
import sqlite3
import sys
import threading
import time
import uuid
import copy
from urllib import error as urllib_error
from urllib import request as urllib_request
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None

from .credential_renewal_common import (
    clear_needs_reauth_marker,
    clear_non_actionable_reauth_marker,
    clear_reauth_markers_if_uat_recovered,
    find_marker_for_open_id,
    marker_requires_reauth,
    preserve_reauth_marker_as_refresh_diagnostic,
    read_needs_reauth_marker,
)
from .feishu_adapter_compat import load_feishu_adapter, log_feishu_adapter_load_error
from .feishu_inbound_richtext import install_feishu_inbound_richtext_patch
from .run_broker import RunBroker
from .run_models import RunRequest

logger = logging.getLogger(__name__)

# Import the split submodules, then re-export every symbol they define into this
# module's namespace. The submodules reach back here (``from .. import
# cron_worker as _cw``) for cross-module and monkeypatched helpers, so patches
# applied to ``cron_worker.<name>`` are honoured everywhere.
from .cron import (  # noqa: E402
    owner_context as _owner_context,
    reauth as _reauth,
    feishu_card as _feishu_card,
    feishu_delivery as _feishu_delivery,
    run_broker_bridge as _run_broker_bridge,
    execution as _execution,
    orchestrator as _orchestrator,
    patches as _patches,
)

_CRON_SUBMODULES = (
    _owner_context,
    _reauth,
    _feishu_card,
    _feishu_delivery,
    _run_broker_bridge,
    _execution,
    _orchestrator,
    _patches,
)

# Reassigned module-level state (mutated via ``global`` in its owning submodule)
# and identity-sensitive locks must NOT be snapshot-copied into this shim — a
# copy freezes a stale value / a would-be second binding. Instead they are
# proxied live to their single canonical owner via ``__getattr__`` below, so
# ``cron_worker._worker_started`` / ``cron_worker._cron_module_patch_lock`` always
# reflect the owner's current object. ``_runtime_patches_installed`` is owned by
# this shim (see install_cron_runtime_patches) and defined directly here.
_LIVE_STATE_OWNERS = {
    "_worker_lock": _patches,
    "_worker_started": _patches,
    "_worker_thread": _patches,
    "_worker_stop": _patches,
    "_gateway_watcher_installed": _patches,
    "_cron_module_patch_lock": _orchestrator,
    "_cron_in_flight_lock": _orchestrator,
}

for _mod in _CRON_SUBMODULES:
    for _name, _value in vars(_mod).items():
        if _name.startswith("__"):
            continue
        if _name == "_cw":  # the back-reference to this shim; don't re-export
            continue
        if _name in _LIVE_STATE_OWNERS:  # proxied live via __getattr__, never snapshot-copied
            continue
        globals()[_name] = _value

del _mod, _name, _value

_runtime_patches_installed = False


# Reassigned module state / identity-locks are proxied LIVE — both reads AND writes —
# to their single canonical owner, so ``cron_worker._worker_started`` reflects the
# owner's current value and ``monkeypatch.setattr(cron_worker, "_cron_module_patch_lock",
# fake)`` mutates the owner (not a shadow attr on the shim), keeping the lock view shared
# with ``cron_api.py``. A plain module-level ``__getattr__`` only covers reads; routing
# writes needs a ModuleType subclass, installed on this module object below.
import sys as _sys
from types import ModuleType as _ModuleType


class _CronWorkerShimModule(_ModuleType):
    def __getattr__(self, name: str) -> Any:  # only fires when normal lookup misses
        owner = _LIVE_STATE_OWNERS.get(name)
        if owner is not None:
            return getattr(owner, name)
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        owner = _LIVE_STATE_OWNERS.get(name)
        if owner is not None:
            setattr(owner, name, value)  # route the write to the canonical owner
        else:
            super().__setattr__(name, value)


_sys.modules[__name__].__class__ = _CronWorkerShimModule


def install_cron_runtime_patches() -> None:
    """Install multitenancy cron delivery adapters without modifying Hermes files.

    Kept in the shim (rather than the ``cron.patches`` submodule) as the single
    top-level wiring entry point; the individual ``_patch_*`` installers live in
    ``cron.patches`` and are re-exported above.
    """
    global _runtime_patches_installed
    if _runtime_patches_installed:
        return
    _patch_cron_run_broker()
    _patch_scheduler_owner_open_id_delivery()
    _patch_cron_delivery_mirror()
    _patch_feishu_open_id_send()
    _patch_feishu_outbound_link_render()
    install_feishu_inbound_richtext_patch()
    from .feishu_merge_forward_api import install_feishu_merge_forward_api_patch
    install_feishu_merge_forward_api_patch()
    from .feishu_reply_quote_api import install_feishu_reply_quote_api_patch
    install_feishu_reply_quote_api_patch()
    from .feishu_reaction_lifecycle import install_feishu_reaction_lifecycle_patch
    install_feishu_reaction_lifecycle_patch()
    from .feishu_group_valve import install_feishu_group_valve_patch
    install_feishu_group_valve_patch()
    from .feishu_auth_hub_actions import install_feishu_auth_hub_actions
    install_feishu_auth_hub_actions()
    _runtime_patches_installed = True
