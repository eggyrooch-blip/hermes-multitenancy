"""Group/route auto-provisioning split out of router god-node (pure move).

Shim helpers/constants/state routed through ``_m`` for monkeypatch fidelity;
all mutable routing state (inviter cache + lock, routing table) stays in the
shim as the single owner.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from .. import router as _m


def _resolve_or_auto_provision_route(
    sender: str,
    *,
    alt_id: Optional[str] = None,
) -> tuple[str, Optional[Path]]:
    """Resolve an existing route, or create a dedicated profile for a new sender."""
    alt_lookup = None if _m._is_feishu_open_id(sender) else alt_id
    profile_name, profile_home = _m._resolve_route(sender, alt_id=alt_lookup)
    if profile_home is not None:
        _m._repair_auto_profile(profile_name, profile_home, route_key=sender, sender=sender)
        return profile_name, profile_home
    if _m._auto_provision_enabled():
        provisioned = _m._auto_provision_route(sender, alt_id=alt_id)
        if provisioned is not None:
            return provisioned
        return profile_name, None
    return _m._resolve_route(sender, alt_id=alt_id)


def _auto_provision_enabled() -> bool:
    # P0-3: in strict mode, auto-provision defaults OFF (a new/unknown open_id
    # must not silently create a profile, bypassing allowlist/org-sync). It can
    # only be on if explicit dev mode is set. Non-strict keeps the legacy
    # default-on behavior.
    from ..runtime import strict_context_enabled

    raw = os.environ.get("HERMES_MULTITENANCY_AUTO_PROVISION")
    if strict_context_enabled() and not _m._dev_mode_enabled():
        # Strict: only an explicit truthy opt-in enables it; default + unset = off.
        if raw is None:
            return False
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    value = (raw if raw is not None else "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _auto_provision_route(
    sender: str,
    *,
    alt_id: Optional[str] = None,
) -> Optional[tuple[str, Path]]:
    """Create a route/profile for an unseen Feishu user, then return it."""
    if not _m._auto_provision_enabled():
        return None
    table = _m._get_routing_table()
    if table is None:
        return None

    route_key = sender
    if not route_key or route_key == "unknown":
        return None

    profile_name = _m._auto_profile_name(route_key)
    profile_home = _m._profile_name_to_home(profile_name)
    try:
        _m._ensure_auto_profile(profile_name, profile_home, route_key=route_key, sender=sender)
        table.upsert(
            user_id=route_key,
            profile_name=profile_name,
            open_id=route_key,
            union_id=alt_id if alt_id and alt_id != sender else None,
        )
    except Exception as exc:
        _m.logger.warning(
            "multitenancy: auto-provision failed sender=%s alt=%s: %s",
            sender,
            alt_id,
            exc,
        )
        return None

    _m.logger.info(
        "multitenancy: auto-provisioned sender=%s alt=%s profile=%s",
        sender,
        alt_id,
        profile_name,
    )
    return profile_name, profile_home


def _is_group_chat_type(chat_type: str) -> bool:
    return chat_type.lower() in _m._GROUP_CHAT_TYPES


def _is_blocked_group_command(command: str) -> bool:
    """True when ``command`` is an auth-family command barred inside groups.

    Normalises before comparing: lowercase, strip a ``@bot`` suffix Feishu
    appends to at-mentioned commands, and drop zero-width characters. An
    exact ``== "feishu_auth"`` check let ``/feishu_auth@bot`` and
    zero-width-padded variants slip through.
    """
    if not command:
        return False
    normalized = command.translate(_m._INVISIBLE_CHARS).strip().lower()
    normalized = normalized.split("@", 1)[0]
    return normalized in _m._GROUP_BLOCKED_COMMANDS


def _make_group_profile_name(chat_id: str) -> str:
    return f"{_m._GROUP_PROFILE_PREFIX}{_m._short_chat_id(chat_id)}"


def is_group_profile_name(name: Optional[str]) -> bool:
    return bool(name) and str(name).startswith(_m._GROUP_PROFILE_PREFIX)


def _group_display_label(
    *,
    owner_open_id: str,
    chat_id: str,
    chat_name: Optional[str] = None,
    inviter_display: Optional[str] = None,
) -> str:
    owner_label = (inviter_display or owner_open_id or "").strip()
    chat_label = (chat_name or chat_id or "").strip()
    return f"{owner_label}-{chat_label}".strip("-") or chat_id or owner_open_id


def _persist_group_route_from_inviter(
    *,
    chat_id: str,
    inviter_open_id: str,
    chat_name: Optional[str] = None,
    inviter_display: Optional[str] = None,
    table: Any = None,
) -> tuple[Optional[str], Optional[Path]]:
    if not chat_id or not _m._is_feishu_open_id(inviter_open_id):
        return None, None
    table = table or _m._get_routing_table()
    if table is None:
        return None, None

    existing = table.lookup_by_chat_id(chat_id)
    display_label = _m._group_display_label(
        owner_open_id=inviter_open_id,
        chat_id=chat_id,
        chat_name=chat_name,
        inviter_display=inviter_display,
    )
    existing_owner = existing.owner_open_id if existing is not None else ""
    if existing is not None and existing_owner and existing_owner != inviter_open_id:
        display_label = existing.display_label or existing.profile_name

    profile_name = (
        existing.profile_name if existing is not None else _m._make_group_profile_name(chat_id)
    )
    profile_home = _m._profile_name_to_home(profile_name)
    _m._ensure_group_profile(
        profile_name=profile_name,
        profile_home=profile_home,
        chat_id=chat_id,
        owner_open_id=existing_owner or inviter_open_id,
        display_label=display_label,
    )
    table.upsert_group(
        chat_id=chat_id,
        profile_name=profile_name,
        owner_open_id=inviter_open_id,
        display_label=display_label,
    )
    _m.logger.info(
        "multitenancy: persisted group route from bot_added chat_id=%s profile=%s owner=%s",
        chat_id,
        profile_name,
        inviter_open_id,
    )
    return profile_name, profile_home


def _resolve_group_inviter_from_cache(chat_id: str) -> Optional[dict[str, Any]]:
    with _m._chat_inviter_cache_lock:
        entry = _m._chat_inviter_cache.get(chat_id)
        if entry is None:
            return None
        if time.time() - entry.get("_ts", 0) > _m._CHAT_INVITER_CACHE_TTL_S:
            _m._chat_inviter_cache.pop(chat_id, None)
            return None
        return dict(entry)


async def resolve_or_auto_provision_group_route(
    *,
    chat_id: str,
    gateway: Any,
) -> tuple[Optional[str], Optional[Path]]:
    """Resolve a group route by chat_id, auto-provisioning on first use.

    Group provisioning is intentionally independent from the unknown-user
    auto-provision toggle: a group route can only be created after a trusted
    ``bot_added`` event captures the inviter, so it does not open the same
    attack surface as creating user profiles for arbitrary senders. Returns
    ``(profile_name, profile_home)`` on success, ``(None, None)`` if no route
    exists and no inviter was captured for this chat. Inviter identity MUST
    come from the
    ``im.chat.member.bot.added_v1.operator_id`` cache entry (see
    ``register_chat_inviter``); we never fall back to the group's current
    owner_id because that is not the same person who added the bot.
    """
    if not chat_id:
        return None, None
    table = _m._get_routing_table()
    if table is None:
        return None, None
    row = table.lookup_by_chat_id(chat_id)
    if row is not None:
        profile_home = _m._profile_name_to_home(row.profile_name)
        display_label = row.display_label or row.profile_name
        cached = _m._resolve_group_inviter_from_cache(chat_id)
        if (
            cached
            and row.owner_open_id
            and cached.get("inviter_open_id") == row.owner_open_id
        ):
            chat_name = cached.get("chat_name")
            inviter_display = cached.get("inviter_display")
            if not chat_name:
                chat_name = await _m._fetch_chat_name(chat_id, gateway)
            if not inviter_display:
                inviter_display = await _m._fetch_user_display(row.owner_open_id, gateway)
            if chat_name or inviter_display:
                refreshed_label = _m._group_display_label(
                    owner_open_id=row.owner_open_id,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    inviter_display=inviter_display,
                )
                if refreshed_label != display_label:
                    try:
                        table.update_display_label(chat_id, refreshed_label)
                        display_label = refreshed_label
                    except Exception as exc:
                        _m.logger.debug(
                            "multitenancy: failed to refresh group display_label chat_id=%s: %s",
                            chat_id,
                            exc,
                        )
        try:
            _m._ensure_group_profile(
                profile_name=row.profile_name,
                profile_home=profile_home,
                chat_id=chat_id,
                owner_open_id=row.owner_open_id or "",
                display_label=display_label,
            )
        except Exception as exc:
            _m.logger.debug(
                "multitenancy: failed to normalize existing group profile chat_id=%s profile=%s: %s",
                chat_id,
                row.profile_name,
                exc,
            )
        _m._pop_chat_inviter(chat_id)
        try:
            table.clear_pending_inviter(chat_id)
        except Exception as exc:
            _m.logger.debug(
                "multitenancy: failed to clear pending inviter for existing group chat_id=%s: %s",
                chat_id,
                exc,
            )
        return row.profile_name, profile_home
    return await _m._provision_group_route(chat_id=chat_id, gateway=gateway)


async def _provision_group_route(
    *,
    chat_id: str,
    gateway: Any,
) -> tuple[Optional[str], Optional[Path]]:
    """Create a new group routing row + profile skeleton.

    Returns ``(None, None)`` if no inviter is known for the chat — that is
    the explicit "refuse silently" path. The caller is responsible for
    surfacing a user-facing message.
    """
    table = _m._get_routing_table()
    if table is None:
        return None, None
    inviter_open_id: Optional[str] = None
    chat_name: Optional[str] = None
    inviter_display: Optional[str] = None
    try:
        table.prune_pending_inviters(
            int(time.time()),
            _m._CHAT_INVITER_CACHE_TTL_S,
        )
        pending_inviter = table.get_pending_inviter(chat_id)
    except Exception as exc:
        _m.logger.debug(
            "multitenancy: pending inviter lookup failed chat_id=%s: %s",
            chat_id,
            exc,
        )
        pending_inviter = None
    if _m._is_feishu_open_id(pending_inviter):
        inviter_open_id = str(pending_inviter)
    else:
        cached = _m._resolve_group_inviter_from_cache(chat_id)
        if cached and _m._is_feishu_open_id(cached.get("inviter_open_id")):
            inviter_open_id = cached["inviter_open_id"]
            chat_name = cached.get("chat_name")
            inviter_display = cached.get("inviter_display")
    if not inviter_open_id:
        _m.logger.warning(
            "multitenancy: cannot auto-provision group route chat_id=%s "
            "(bot_added inviter not captured — re-add the bot to retry)",
            chat_id,
        )
        return None, None

    if not chat_name:
        chat_name = await _m._fetch_chat_name(chat_id, gateway) or chat_id
    if not inviter_display:
        inviter_display = (
            await _m._fetch_user_display(inviter_open_id, gateway) or inviter_open_id
        )

    try:
        profile_name, profile_home = _m._persist_group_route_from_inviter(
            table=table,
            chat_id=chat_id,
            inviter_open_id=inviter_open_id,
            chat_name=chat_name,
            inviter_display=inviter_display,
        )
    except Exception as exc:
        _m.logger.warning(
            "multitenancy: failed to provision group route chat_id=%s: %s",
            chat_id,
            exc,
        )
        return None, None
    if profile_name is None or profile_home is None:
        return None, None

    _m.logger.info(
        "multitenancy: auto-provisioned group profile chat_id=%s profile=%s owner=%s",
        chat_id,
        profile_name,
        inviter_open_id,
    )
    # Eagerly evict the cache entry so a later bot-removal + re-add cycle
    # repopulates from the fresh event rather than reusing stale chat_name.
    _m._pop_chat_inviter(chat_id)
    try:
        table.clear_pending_inviter(chat_id)
    except Exception as exc:
        _m.logger.debug(
            "multitenancy: failed to clear pending inviter chat_id=%s: %s",
            chat_id,
            exc,
        )
    return profile_name, profile_home


def _ensure_group_profile(
    *,
    profile_name: str,
    profile_home: Path,
    chat_id: str,
    owner_open_id: str,
    display_label: str,
) -> None:
    """Create the on-disk profile skeleton for a group profile.

    Mirrors ``_ensure_auto_profile`` for shared config + SOUL.md, but adds a
    ``group_profile.json`` marker so downstream tools can detect the group
    context and refuse UAT-dependent operations.
    """
    profile_home.mkdir(parents=True, exist_ok=True)
    shared_home = _m._shared_home_for_profile(profile_home)

    config_path = profile_home / "config.yaml"
    if config_path.exists():
        _m._normalize_profile_config_file(config_path, shared_home=shared_home)
    else:
        config_path.write_text(
            _m._dump_profile_config(_m._profile_config_from_shared_home(shared_home)),
            encoding="utf-8",
        )

    soul_path = profile_home / "SOUL.md"
    if not soul_path.exists():
        soul_path.write_text(
            "\n".join(
                [
                    f"# Hermes Group Profile {profile_name}",
                    "",
                    f"You are a Feishu group-chat agent for chat `{_m._sanitize_soul_field(chat_id)}`.",
                    f"This profile is owned by Feishu user `{_m._sanitize_soul_field(owner_open_id)}` "
                    f"(display label: `{_m._sanitize_soul_field(display_label)}`).",
                    "Identity rules (strict):",
                    "- 你不能以群成员任何一个个人的身份操作飞书数据。",
                    "- /feishu_auth 在群聊模式下被禁用，任何用户的 UAT 不会被加载。",
                    "- 该群 profile 使用 `lark_cli` 时默认走 bot identity。",
                    "- 仅响应被 @ 的消息；群里的旁白不要回复。",
                    "- 该群的对话和记忆与其它群、与个人私聊完全隔离。",
                    "",
                    _m._LARK_CLI_SOUL_GUIDANCE,
                    "",
                    _m._GROUP_EXTERNAL_TOOL_SOUL_GUIDANCE,
                    "",
                ]
            ),
            encoding="utf-8",
        )
    else:
        _m._ensure_soul_guidance(soul_path, _m._LARK_CLI_SOUL_GUIDANCE)
        _m._ensure_soul_guidance(soul_path, _m._GROUP_EXTERNAL_TOOL_SOUL_GUIDANCE)

    # Group profiles get an empty feishu_uat/ directory but no per-user JSON;
    # the marker file below tells UAT helpers to refuse to load user tokens.
    (profile_home / "feishu_uat").mkdir(parents=True, exist_ok=True, mode=0o700)
    upstream_profile_home = _m._upstream_profile_home_for_owner(owner_open_id, shared_home)
    _m._sync_default_skills_for_profile(
        profile_home,
        shared_home,
        include_default_skills=True,
        upstream_profile_home=upstream_profile_home,
    )

    marker_path = profile_home / "group_profile.json"
    if not marker_path.exists():
        marker_path.write_text(
            json.dumps(
                {
                    "kind": "group",
                    "chat_id": chat_id,
                    "owner_open_id": owner_open_id,
                    "display_label": display_label,
                    "feishu_auth_disabled": True,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # The group profile's home channel IS the group itself. Pre-write
    # FEISHU_HOME_CHANNEL into .env so the gateway's "no home channel set"
    # onboarding prompt never fires for a group profile.
    _m._write_group_profile_env(profile_home, shared_home, chat_id)

    # .env is owned by us above; auth.json still follows the shared symlink.
    _m._ensure_shared_profile_file(profile_home, shared_home, "auth.json")


def _ensure_webui_agent_profile(
    *,
    profile_name: str,
    profile_home: Path,
    owner_open_id: str,
    display_label: str,
    agent_id: str,
    upstream_profile: str | None = None,
) -> None:
    """Create the on-disk skeleton for a WebUI group-chat agent profile.

    WebUI group-chat agents are not real Feishu ``oc_*`` groups, but they must
    run with the same tokenless/bot-only boundary as Feishu group profiles.
    """
    profile_home.mkdir(parents=True, exist_ok=True)
    shared_home = _m._shared_home_for_profile(profile_home)

    config_path = profile_home / "config.yaml"
    if config_path.exists():
        _m._normalize_profile_config_file(config_path, shared_home=shared_home)
        _m._disable_webui_agent_feishu_platform_file(config_path)
    else:
        config = _m._profile_config_from_shared_home(shared_home)
        _m._disable_webui_agent_feishu_platform(config)
        config_path.write_text(_m._dump_profile_config(config), encoding="utf-8")

    soul_path = profile_home / "SOUL.md"
    if not soul_path.exists():
        soul_path.write_text(
            "\n".join(
                [
                    f"# Hermes WebUI Group Agent Profile {profile_name}",
                    "",
                    f"You are a WebUI group-chat agent named `{_m._sanitize_soul_field(display_label)}`.",
                    f"This profile is owned by Feishu user `{_m._sanitize_soul_field(owner_open_id)}`.",
                    "Identity rules (strict):",
                    "- 你不能以 WebUI 群成员任何一个个人的身份操作飞书数据。",
                    "- /feishu_auth 在 WebUI 群聊 agent 模式下被禁用，任何用户的 UAT 不会被加载。",
                    "- 该 profile 使用 `lark_cli` 时默认走 bot identity。",
                    "- 该 profile 的对话和记忆与其它 profile 完全隔离。",
                    "",
                    _m._LARK_CLI_SOUL_GUIDANCE,
                    "",
                    _m._GROUP_EXTERNAL_TOOL_SOUL_GUIDANCE,
                    "",
                ]
            ),
            encoding="utf-8",
        )
    else:
        _m._ensure_soul_guidance(soul_path, _m._LARK_CLI_SOUL_GUIDANCE)
        _m._ensure_soul_guidance(soul_path, _m._GROUP_EXTERNAL_TOOL_SOUL_GUIDANCE)

    (profile_home / "feishu_uat").mkdir(parents=True, exist_ok=True, mode=0o700)

    upstream_profile_home: Path | None = None
    if upstream_profile:
        candidate = shared_home / "profiles" / upstream_profile
        upstream_profile_home = candidate if candidate.exists() else None
    if upstream_profile_home is None:
        upstream_profile_home = _m._upstream_profile_home_for_owner(owner_open_id, shared_home)
    _m._sync_default_skills_for_profile(
        profile_home,
        shared_home,
        include_default_skills=True,
        upstream_profile_home=upstream_profile_home,
    )

    marker_path = profile_home / "group_profile.json"
    marker_payload = {
        "kind": "group",
        "chat_id": agent_id,
        "owner_open_id": owner_open_id,
        "display_label": display_label,
        "feishu_auth_disabled": True,
        "source": "webui-agent",
    }
    if not marker_path.exists():
        marker_path.write_text(
            json.dumps(marker_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    _m._write_group_profile_env(profile_home, shared_home, None)
    _m._ensure_shared_profile_file(profile_home, shared_home, "auth.json")


def _write_group_profile_env(profile_home: Path, shared_home: Path, chat_id: str | None) -> None:
    """Materialize <profile>/.env with ONLY model keys and optional FEISHU_HOME_CHANNEL.

    The subprocess env is whitelisted (``_SUBPROCESS_ENV_ALLOWLIST``) and
    does NOT pass model provider API keys, so the AIAgent child reads them
    from ``<profile>/.env`` via its own dotenv path. But the previous
    "copy every shared line" approach also duplicated the credential-vault
    master key, gateway config, and unrelated tool tokens into every group
    profile dir — secret sprawl flagged in code review. We now copy only
    the model provider key/base-url names (``_MODEL_ENV_ALLOWLIST``, the
    same source of truth the model resolver uses) plus the per-group
    FEISHU_HOME_CHANNEL override. ``HERMES_MULTITENANCY_CREDENTIAL_KEY``
    already flows through the allowlisted subprocess env, so it never
    needs to land on disk in a tenant dir.

    Real Feishu group profiles also preempt the gateway's home-channel
    onboarding prompt by declaring the group chat itself as the home channel.
    WebUI group agents pass no chat_id because they are not real Feishu chats.
    Re-runs are idempotent.
    """
    from ..agent_real import _MODEL_ENV_ALLOWLIST

    shared_env = shared_home / ".env"
    target = profile_home / ".env"
    base_lines: list[str] = []
    if shared_env.exists():
        try:
            shared_text = shared_env.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _m.logger.debug(
                "multitenancy: shared .env unreadable (%s); group .env will only set FEISHU_HOME_CHANNEL",
                exc,
            )
            shared_text = ""
        for line in shared_text.splitlines():
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in _MODEL_ENV_ALLOWLIST:
                base_lines.append(line)
    if chat_id:
        base_lines.append(f"FEISHU_HOME_CHANNEL={chat_id}")
    # Atomic write: a crash between unlink() and write_text() used to leave
    # the profile with NO .env (all inherited creds gone until reprovision).
    # Write a sibling temp file then os.replace() — the rename is atomic on
    # POSIX and also transparently replaces a stale symlink without ever
    # following it into the shared .env.
    if target.is_symlink():
        target.unlink()
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text("\n".join(base_lines) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def _disable_webui_agent_feishu_platform_file(config_path: Path) -> None:
    try:
        import yaml

        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        _m.logger.debug("multitenancy: failed to read WebUI agent config %s: %s", config_path, exc)
        return
    if not isinstance(loaded, dict):
        return
    before = json.dumps(loaded, sort_keys=True, ensure_ascii=True)
    _m._disable_webui_agent_feishu_platform(loaded)
    normalized = _m._normalize_profile_config(loaded)
    after = json.dumps(normalized, sort_keys=True, ensure_ascii=True)
    if after != before:
        config_path.write_text(_m._dump_profile_config(normalized), encoding="utf-8")


def _disable_webui_agent_feishu_platform(config: dict[str, Any]) -> None:
    """WebUI group agents use lark-cli bot auth but must not connect Feishu."""
    platforms = config.setdefault("platforms", {})
    if isinstance(platforms, dict):
        platforms["feishu"] = {"enabled": False}
