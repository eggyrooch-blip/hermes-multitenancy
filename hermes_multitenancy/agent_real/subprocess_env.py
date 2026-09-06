from __future__ import annotations

import sys as _sys
_pkg = _sys.modules[__package__]

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
import shutil
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
    delegation_enabled: bool = True,
) -> dict[str, str]:
    """Build a sanitized env for the AIAgent subprocess (档 A isolation).

    ``delegation_enabled=False`` is for env builds that are NOT one user's run —
    notably the warm worker's long-lived base env, which is shared by every
    subsequent run of the profile and must therefore never contain any user's
    borrowed credential.

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
    local_harness = str((extra or {}).get("HERMES_LOCAL_HARNESS") or "") == "1"
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
            include_profile_secrets=not shared_agent_run and not local_harness,
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
    # NOTE: KEP_AGENT_MODE / KEP_WORKSPACE_DIR are NOT set here. They are
    # profile anchors (`_profile_anchor_env_for_aiagent`), applied above with
    # HOME/WORKSPACE/KEP_PROFILE and mirrored through the force channel below —
    # so the in-process run path gets them too, which a local injection missed.
    # terminal/execute_code apply a second subprocess env scrub.  Force only
    # non-secret profile anchors through that boundary so profile-scoped CLIs
    # such as kep-auth/ocean-cli read the same HOME and KEP_PROFILE as the
    # routed AIAgent process.
    env.update(forced_profile_anchor_env)
    lark_cli_env = _lark_cli_sidecar_env_for_aiagent(profile_home)
    env.update(lark_cli_env)
    env.update(_force_env_for_terminal_passthrough(lark_cli_env))
    if not local_harness:
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
        from ..credential_hub.readers.feishu_project import (
            _meegle_invocation,
            _meegle_search_path,
        )
        from ..oauth_cli_guard import (
            install_meegle_npx_oauth_guard,
            install_meegle_oauth_guard,
            require_registered_oauth_cli_gates,
        )
        from ..connectors.builtin import BUILTIN_CONNECTORS

        require_registered_oauth_cli_gates(BUILTIN_CONNECTORS)

        real_bin = _resolve_lark_cli_authsidecar_binary(profile_home)
        shim_dir = profile_home / "tmp" / "lark-cli-shim"
        install_lark_cli_shim(shim_dir, real_binary=real_bin)
        env[HERMES_LARK_CLI_REAL_BIN] = str(real_bin)
        env[HERMES_LARK_CLI_RUN_TOKEN] = generate_lark_cli_run_token()
        # Audit controls are SEALED at the end of this function from trusted
        # sources only (extra > gateway parent env > profile-local default) —
        # never set here, where a profile .env value merged above would win a
        # setdefault. See the builder-owned seal block near the return.
        # DECISION (2026-08-31, wf_46aff7d5 risk analysis): we deliberately do
        # NOT inject HERMES_MULTITENANCY_STRICT_CONTEXT into the worker env.
        # Flipping it would activate the dormant strict write regime inside
        # workers — lark_cli_tool _prepare_resumable_write allowlists only
        # im +messages-send/reply, so every api-mode non-GET, schema write and
        # non-IM shortcut write across all tenants would be rejected with
        # FEISHU_OPERATION_NOT_RESUMABLE, and checkpoint claiming would
        # identity_unbound cron IM sends. Enabling that regime is a product
        # decision requiring a real-traffic allowlist, not an env tweak.
        # Worker-side gates that ARE meant to hold anchor on the run token
        # instead (lark_cli_tool script channel + AUTHORIZED passthrough).
        real_bins = {
            name: str(shared_bin_dir / name)
            for name in kep_cli_guard.KEP_SHIM_NAMES
            if (shared_bin_dir / name).exists()
        }
        if real_bins:
            kep_cli_guard.install_kep_cli_shim(
                shim_dir,
                real_bins=real_bins,
                expected_profile=profile_home.name,
            )
            for name, real_path in real_bins.items():
                env[f"HERMES_KEP_CLI_REAL_BIN_{name.replace('-', '_').upper()}"] = real_path
        resolver_env = dict(env)
        resolver_env["PATH"] = os.pathsep.join(
            part
            for part in resolver_env.get("PATH", "").split(os.pathsep)
            if part and Path(part).resolve(strict=False) != shim_dir.resolve(strict=False)
        )
        explicit_meegle = resolver_env.get("HERMES_MEEGLE_BIN", "")
        if explicit_meegle:
            try:
                if Path(explicit_meegle).resolve(strict=False).is_relative_to(shim_dir):
                    resolver_env.pop("HERMES_MEEGLE_BIN", None)
            except OSError:
                resolver_env.pop("HERMES_MEEGLE_BIN", None)
        meegle_invocation = _meegle_invocation(allow_npx=True, environ=resolver_env)
        meegle_wrapper = install_meegle_oauth_guard(
            shim_dir,
            real_command=meegle_invocation,
        )
        env["HERMES_MEEGLE_BIN"] = str(meegle_wrapper)
        npx_binary = shutil.which(
            "npx",
            path=_meegle_search_path(environ=resolver_env),
        )
        if npx_binary:
            install_meegle_npx_oauth_guard(shim_dir, real_binary=Path(npx_binary))
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

    if local_harness:
        # Harness keeps run-scoped capabilities but never ambient vault keys.
        for name in (
            "HERMES_MULTITENANCY_CREDENTIAL_KEY",
            "HERMES_CREDENTIAL_KEY",
        ):
            env.pop(name, None)
        env.pop("HERMES_YOLO_MODE", None)
        env.pop("HERMES_SANDBOX_HOST", None)

    from . import executor_map

    mapped_codex = (
        str((extra or {}).get(EXECUTOR_RUNTIME_ENV) or "").strip()
        == executor_map.CODEX_APP_SERVER
    )

    # GitLab credential delegation (group profiles only): inject the initiator's
    # leased personal token at RUN level. Env-only by contract — never written to
    # the shared profile's config/, vault, or workspace/credentials/.
    #
    # The sender comes ONLY from this spawn's explicit `extra`. There is
    # deliberately NO ambient-ContextVar fallback: env builds that are not a
    # user's run (the warm worker's shared base env) pass no `extra`, so an
    # ambient identity would have baked whoever ran last into a long-lived
    # process env — readable by the NEXT user's tool children through
    # /proc/<pid>/environ, and it silently burned that user's once-lease.
    # `delegation_enabled=False` closes the same hole positively rather than by
    # name-blacklisting the resulting variables.
    if delegation_enabled and not shared_agent_run and (not mapped_codex or local_harness):
        try:
            from ..credential_delegation import (
                DELEGATION_ID_ENV,
                delegation_env_for_run,
            )

            _delegation_sender = str((extra or {}).get("HERMES_FEISHU_USER_OPEN_ID") or "")
            delegation_env = delegation_env_for_run(
                profile_home,
                sender_open_id=_delegation_sender,
                delegation_id=str((extra or {}).get(DELEGATION_ID_ENV) or ""),
                existing_env_names=env,  # mapping: an empty GITLAB_TOKEN= is NOT a token
            )
            delegation_env = {
                key: value
                for key, value in delegation_env.items()
                # An EMPTY existing value is not a value: a bare `GITLAB_TOKEN=`
                # in the group .env must not shadow the delegated token.
                if not str(env.get(key) or "").strip()
            }
            if delegation_env:
                env.update(delegation_env)
                env.update(_force_env_for_terminal_passthrough(delegation_env))
        except Exception:
            logger.debug(
                "[multitenancy] delegation env injection failed", exc_info=True
            )

    if sandboxed_profile and not local_harness:
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

    # LAST WORD on the strict flag — positioned after every env source (parent
    # allowlist, profile .env via _profile_env_for_aiagent, `extra`) for the
    # same reason as the broker-key pop below: an earlier exclusion can be
    # silently undone by a later merge. The DECISION comment in the strict
    # build block above pins strict OFF in workers; this pop enforces it
    # against a dirty profile .env or a caller-supplied extra.
    env.pop("HERMES_MULTITENANCY_STRICT_CONTEXT", None)
    if strict_context_enabled() and not local_harness:
        # SEAL builder-owned audit controls from trusted sources only. The env
        # dict at this point may carry values merged from the tenant-writable
        # profile .env — for these keys that is an attack surface, not config:
        # PATH=/dev/null or ENABLED=0 there would silently discard forced
        # script_channel.granted events and disable worker audit. Authority
        # order: caller-constructed `extra` > explicit gateway parent env >
        # profile-local durable default. Profile .env has no say.
        _audit_path_default = str(
            _default_security_audit_path_for_subprocess(profile_home)
        )
        for _audit_key, _audit_default in (
            ("HERMES_MT_SECURITY_AUDIT_PATH", _audit_path_default),
            ("HERMES_MT_SECURITY_AUDIT_ENABLED", "1"),
        ):
            _trusted = (
                str((extra or {}).get(_audit_key) or "").strip()
                or str(parent.get(_audit_key) or "").strip()
            )
            env[_audit_key] = _trusted or _audit_default

    # LAST WORD on the RunBroker bearer — must stay at the very end.
    #
    # The SHARED master key must never reach a tenant child: owner identity on
    # that seam was caller-asserted, so holding it was enough to act as any
    # colleague (2026-08-04 security review). Only the run-scoped token that
    # `_aiagent_subprocess_env_scope` minted for THIS spawn (handed in via
    # `extra`) may survive; anything else is dropped, and with no minted token
    # the child correctly gets no broker bearer at all.
    #
    # Deliberately positioned after every env source rather than up top: an
    # earlier pop was silently undone by `_profile_env_for_aiagent`, which loads
    # the profile `.env` and re-injected both master names verbatim (codex review
    # RBOS-MASTER-REINTRODUCTION, probed: the final child env came back with
    # {'HERMES_RUN_BROKER_KEY': 'master-from-profile', ...}). Filtering by value
    # here closes the whole class — parent env, profile dotenv, or any future
    # source — instead of chasing each one.
    _minted = str((extra or {}).get("HERMES_RUN_BROKER_KEY") or "").strip()
    for _broker_key_name in ("HERMES_RUN_BROKER_KEY", "HERMES_MULTITENANCY_RUN_BROKER_KEY"):
        if not _minted or str(env.get(_broker_key_name) or "").strip() != _minted:
            env.pop(_broker_key_name, None)

    # Codex app-server plumbing is per-run, so THIS spawn's `extra` is the only
    # source of it. A run that was not mapped to codex must be byte-identical to
    # today: a CODEX_HOME inherited from the gateway env would aim it at the
    # operator's own ~/.codex (the isolation `codex_home.materialize` exists to
    # give), and an inherited key would hand it a credential no one billed. The
    # warm worker's shared base env passes no `extra` and is cleaned the same way.
    if not mapped_codex:
        for _codex_env_name in _CODEX_RUNTIME_ENV_NAMES:
            env.pop(_codex_env_name, None)
    else:
        # The mapped run already cloned with its actor-bound read credential in
        # <wf>/.git-credentials. Do not re-expose that literal through the
        # profile's generic GitLab env compatibility path.
        if not local_harness:
            for _gitlab_env_name in _gitlab_runtime_env_names_for_aiagent(
                profile_home, env
            ):
                env.pop(_gitlab_env_name, None)
        # Codex itself may invoke Git for plugin catalogs. Keep it away from
        # the operator's config/keychain even though the run's clone is done.
        env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            }
        )

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
