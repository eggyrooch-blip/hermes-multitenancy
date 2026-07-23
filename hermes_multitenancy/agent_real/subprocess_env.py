from __future__ import annotations

import sys as _sys
_pkg = _sys.modules["hermes_multitenancy.agent_real"]

import json
import logging
import os
import sys
import time
import hashlib
import tempfile
import uuid
import re
import secrets
import importlib
import threading
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from ..runtime import sandbox_profile_enabled


def _build_subprocess_env(
    profile_home: Path,
    *,
    approval_dir: Path,
    event_stream: bool = False,
    extra: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Build a sanitized env for the AIAgent subprocess (档 A isolation).

    Two guarantees enforced here:

      1. **Env whitelist** — the parent gateway's full env is NOT inherited.
         Only keys in :data:`_SUBPROCESS_ENV_ALLOWLIST` carry over. This stops
         API keys / OAuth tokens / shell secrets exported into the gateway
         process from leaking into a profile's tool execution.

      2. **Profile compatibility pivots** — HOME, WORKSPACE, XDG
         cache/config/state/data and TMPDIR redirect into the profile's own
         directory tree. Token-oriented skills and CLIs that were written for
         OpenClaw-style ``$HOME`` or ``/workspace`` semantics can therefore run
         unchanged: ``Path.home() / ".keepai"`` lands in the current profile,
         ``/workspace/credentials/gitlab.token`` maps to the profile workspace,
         and CLIs/MCP servers see stable profile identity via ``HERMES_PROFILE``
         plus ``KEP_PROFILE`` for existing Keep tooling.

    Skills that already use :mod:`hermes_multitenancy.skill_storage` continue
    to work, but it is no longer the only safe path. The multitenancy runtime
    itself provides the compatibility boundary for unmodified upstream skills.

    The directories ``home/``, ``workspace/``, ``cache/``, ``config/``,
    ``state/``, ``data/`` and ``tmp/`` are created on first use (mode 0700).

    Profile-local secrets are loaded inside the child by
    :func:`_run_with_aiagent` from ``<profile_home>/.env`` and ``auth.json``;
    they are intentionally NOT injected here so the child never sees a parent
    env-derived key as an "ambient" credential.
    """
    parent = os.environ
    env: dict[str, str] = {
        key: parent[key] for key in _SUBPROCESS_ENV_ALLOWLIST if key in parent
    }
    if strict_context_enabled():
        env.pop("HERMES_MULTITENANCY_CREDENTIAL_KEY", None)
        env.pop("HERMES_CREDENTIAL_KEY", None)

    profile_home = profile_home.expanduser()
    share_role = str((extra or {}).get("HERMES_AGENT_SHARE_ROLE") or "").strip()
    shared_agent_run = share_role in _AGENT_SHARED_ROLES
    env.update(
        _profile_env_for_aiagent(
            profile_home,
            include_profile_secrets=not shared_agent_run,
        )
    )
    credential_env = {} if shared_agent_run else _credential_env_for_aiagent(profile_home)
    env.update(credential_env)
    env.update(_force_env_for_terminal_passthrough(credential_env))

    # OpenClaw-compatible token boundary: HOME and /workspace-style variables
    # point into the routed profile so unmodified token skills do not write to
    # the shared service user's home.
    sandboxed_profile = sandbox_profile_enabled(profile_home.name)
    profile_anchor_env, forced_profile_anchor_env = (
        _profile_anchor_env_layers_for_aiagent(
            profile_home,
            short_parent_tmp=(
                sandboxed_profile and sys.platform.startswith("linux")
            ),
        )
    )
    env.update(profile_anchor_env)
    env["HERMES_GATEWAY_SESSION"]           = "1"
    env["HERMES_EXEC_ASK"]                  = "1"
    env["HERMES_MULTITENANCY_APPROVAL_DIR"] = str(approval_dir)
    # terminal/execute_code apply a second subprocess env scrub.  Force only
    # non-secret profile anchors through that boundary so profile-scoped CLIs
    # such as kep-auth/ocean-cli read the same HOME and KEP_PROFILE as the
    # routed AIAgent process.
    env.update(forced_profile_anchor_env)
    lark_cli_env = _lark_cli_sidecar_env_for_aiagent(profile_home)
    env.update(lark_cli_env)
    env.update(_force_env_for_terminal_passthrough(lark_cli_env))
    env.update(_browser_env_for_aiagent(profile_home))
    if event_stream:
        env["HERMES_AIAGENT_EVENT_STREAM"] = "1"

    shared_bin_dir = _pkg._resolve_shared_hermes_home(profile_home) / "bin"
    shared_bin = str(shared_bin_dir)
    existing_path = env.get("PATH", "")
    path_parts = [part for part in existing_path.split(os.pathsep) if part]
    deduped = [part for part in path_parts if part != shared_bin]
    env["PATH"] = os.pathsep.join([shared_bin, *deduped])
    if strict_context_enabled():
        real_bin = _resolve_lark_cli_authsidecar_binary(profile_home)
        shim_dir = profile_home / "tmp" / "lark-cli-shim"
        install_lark_cli_shim(shim_dir, real_binary=real_bin)
        env[HERMES_LARK_CLI_REAL_BIN] = str(real_bin)
        env[HERMES_LARK_CLI_RUN_TOKEN] = generate_lark_cli_run_token()
        env.setdefault(
            "HERMES_MT_SECURITY_AUDIT_PATH",
            str(_default_security_audit_path_for_subprocess(profile_home)),
        )
        real_bins = {
            name: str(shared_bin_dir / name)
            for name in kep_cli_guard.KEP_SHIM_NAMES
            if (shared_bin_dir / name).exists()
        }
        if real_bins:
            kep_cli_guard.install_kep_cli_shim(shim_dir, real_bins=real_bins)
            for name, real_path in real_bins.items():
                env[f"HERMES_KEP_CLI_REAL_BIN_{name.replace('-', '_').upper()}"] = real_path
        strict_path_parts = [part for part in env["PATH"].split(os.pathsep) if part]
        env["PATH"] = os.pathsep.join(
            [str(shim_dir), *[part for part in strict_path_parts if part != str(shim_dir)]]
        )

    # Mirror _wrap_with_sandbox's toggle + per-profile gate so the subprocess
    # knows it's running inside a sandbox host. tools/approval.py reads
    # HERMES_SANDBOX_HOST to bypass dangerous-command approval: the sandbox
    # already enforces filesystem/network isolation at the kernel layer, so
    # asking the user about `python -c ...` inside it is duplicate friction.
    # The hardline blocklist (rm -rf /, mkfs, dd /dev/sd, shutdown, fork bomb)
    # is checked BEFORE the bypass in approval.py and remains in effect.
    if extra:
        env.update(extra)

    if sandboxed_profile:
        env["HERMES_SANDBOX_HOST"] = "1"
        env.setdefault("HERMES_YOLO_MODE", "1")
        if sys.platform.startswith("linux"):
            # bwrap mounts a private tmpfs at /tmp. Keep Hermes' parent RPC
            # socket short while tool children remain pinned to profile tmp.
            env["TMPDIR"] = "/tmp"
            env["_HERMES_FORCE_TMPDIR"] = str(profile_home / "tmp")

    # Feishu per-user UAT identity: forward sender open_id so that AIAgent
    # tool-worker threads (which lose ContextVar across ThreadPoolExecutor
    # workers per run_agent.py:1104 / 8479) can recover identity via
    # os.environ. The contextvar is set by sender_open_id_scope in the
    # feishu adapter; asyncio.create_task copies context so it is still
    # alive here even when called from a batched / deferred flush task.
    # Note: MessageEvent dataclass (gateway/platforms/base.py:785) has no
    # sender_open_id field, so a caller-driven getattr would always return
    # empty. ContextVar is the reliable source.
    # Must come AFTER extra so an explicit caller-supplied value wins.
    if "HERMES_FEISHU_USER_OPEN_ID" not in env:
        try:
            from tools import feishu_oapi_client as _foc
            _sender = _foc.current_sender_open_id.get()
            if _sender:
                env["HERMES_FEISHU_USER_OPEN_ID"] = str(_sender)
        except Exception:
            pass
    return env


def _readonly_enabled_toolsets(toolsets: Optional[list[str]]) -> Optional[list[str]]:
    try:
        from ..expert_bot_route import READONLY_DISABLED_TOOLSETS, feishu_expert_readonly_enabled

        readonly = feishu_expert_readonly_enabled()
        readonly_disabled_toolsets = READONLY_DISABLED_TOOLSETS
    except Exception:
        readonly = False
        readonly_disabled_toolsets = frozenset()
    if not readonly:
        return toolsets
    # terminal is removed structurally here, so terminal-run lark-cli/kep writes
    # cannot bypass the registered lark_cli and kep/hades guards below.
    filtered = [
        item
        for item in list(toolsets or [])
        if str(item or "").strip() not in readonly_disabled_toolsets
    ]
    return filtered or None


def _resolve_enabled_toolsets(
    config: dict[str, Any],
    platform_key: str,
    *,
    platform_tools_resolver: Any,
    profile_home: Path | None = None,
    shared_home: Path | None = None,
    user_key: str | None = None,
    departments: list[str] | tuple[str, ...] | None = None,
    xai_credentials: dict[str, Any] | None = None,
) -> Optional[list[str]]:
    """Resolve profile toolsets without dropping core non-Feishu abilities.

    A plain Hermes Feishu gateway defaults to the composite ``hermes-feishu``
    toolset, which includes web/search/browser/file/etc. During multitenant
    UAT it is common to add ``platform_toolsets.feishu`` only for Feishu user
    token helpers; treating that list as a hard replacement makes the agent
    look competent inside Feishu but unable to search the web.

    Default mode therefore merges explicit profile entries with the platform
    default. Set ``multitenancy.toolsets_mode: explicit`` or
    ``HERMES_MULTITENANCY_TOOLSETS_MODE=explicit`` to preserve the old strict
    replacement behavior for providers that need a smaller schema.
    """
    explicit = (config.get("platform_toolsets") or {}).get(platform_key)
    explicit_toolsets = _normalize_toolset_list(explicit)
    mode = _toolsets_mode(config, platform_key)

    def apply_discovery_policy(toolsets: list[str] | None) -> list[str] | None:
        if not toolsets or not (profile_home or shared_home):
            return toolsets
        try:
            from ..discovery_policy import apply_toolset_policy

            profile_name = profile_home.name if profile_home is not None else str(config.get("profile_name") or "")
            if not profile_name:
                return [item for item in toolsets if item != "x_search"]
            root = shared_home
            if root is None and profile_home is not None:
                root = profile_home.parent.parent if profile_home.parent.name == "profiles" else profile_home
            if root is None:
                return [item for item in toolsets if item != "x_search"]
            return apply_toolset_policy(
                toolsets,
                shared_home=root,
                profile_name=profile_name,
                user_key=user_key,
                departments=departments,
                xai_credentials=xai_credentials,
            )
        except Exception as exc:
            logger.warning("[multitenancy] discovery policy filter failed: %s", exc)
            return [item for item in toolsets if item != "x_search"]

    def apply_profile_tool_policies(toolsets: list[str] | None) -> list[str] | None:
        filtered = apply_discovery_policy(toolsets)
        if profile_home is None:
            return filtered
        try:
            from ..browser_policy import browser_decision, browser_toolsets_for_policy

            return browser_toolsets_for_policy(
                filtered,
                browser_decision(config, profile_home),
            )
        except Exception as exc:
            logger.warning("[multitenancy] browser toolset policy failed: %s", exc)
            return [item for item in filtered or [] if item != "browser"] or None

    if explicit_toolsets and mode in {"explicit", "strict", "replace"}:
        logger.info(
            "[multitenancy] platform_toolsets explicit mode for %s: %s",
            platform_key, explicit_toolsets,
        )
        return _readonly_enabled_toolsets(apply_profile_tool_policies(explicit_toolsets))

    default_toolsets: list[str] = []
    resolver_platform_key = "api_server" if platform_key == "webui" else platform_key
    if platform_tools_resolver is not None:
        resolver_config = config
        if explicit_toolsets:
            import copy

            resolver_config = copy.deepcopy(config)
            platform_toolsets = resolver_config.get("platform_toolsets")
            if isinstance(platform_toolsets, dict):
                platform_toolsets.pop(platform_key, None)
                if resolver_platform_key != platform_key:
                    platform_toolsets.pop(resolver_platform_key, None)
        try:
            try:
                resolved = platform_tools_resolver(
                    resolver_config,
                    resolver_platform_key,
                    include_default_mcp_servers=("no_mcp" not in explicit_toolsets),
                )
            except TypeError:
                resolved = platform_tools_resolver(resolver_config, resolver_platform_key)
            default_toolsets = _normalize_toolset_list(resolved)
        except Exception as exc:
            logger.warning(
                "[multitenancy] _get_platform_tools failed for %s: %s",
                platform_key, exc,
            )

    if explicit_toolsets and not default_toolsets:
        default_toolsets = _fallback_default_toolsets(platform_key)

    if explicit_toolsets:
        merged = sorted(set(default_toolsets) | set(explicit_toolsets))
        logger.info(
            "[multitenancy] platform_toolsets merged for %s: explicit=%s default=%s merged=%s",
            platform_key, explicit_toolsets, default_toolsets, merged,
        )
        return _readonly_enabled_toolsets(apply_profile_tool_policies(merged))

    return _readonly_enabled_toolsets(apply_profile_tool_policies(default_toolsets) or None)
