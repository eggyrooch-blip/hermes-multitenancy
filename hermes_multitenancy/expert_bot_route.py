"""Fail-closed Feishu fixed-expert bot routing helpers."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .expert_overlay import role_override_block_for

logger = logging.getLogger(__name__)

FIXED_EXPERT_ENV = "HERMES_MULTITENANCY_FIXED_EXPERT"
READONLY_ENV = "HERMES_MULTITENANCY_FEISHU_EXPERT_READONLY"
# Session-control slash commands — always safe in the fixed-expert bot.
SAFE_FIXED_EXPERT_SLASH_COMMANDS = frozenset({"new", "reset", "status", "stop"})
# Credential-authorization slash commands (normalized names from
# ``commands.py``: ``auth`` covers auth/creds/credentials, ``feishu-auth``
# covers feishu_auth/feishu_reauth). Allowed because command dispatch in the
# fixed-expert bot already routes through ``fixed_context`` — the OAuth flow
# binds the token to the CANONICAL mapped profile + open_id (the user's own
# main-bot-domain identity via union_id), NOT the expert-bot-domain shadow
# open_id. So authorizing here stores creds in the right profile, shared with
# the main bot / WebUI — no credential-domain split. Destructive-write
# ``approve`` and quick-exec / plugin commands stay denied (M2 / readonly).
AUTH_FIXED_EXPERT_SLASH_COMMANDS = frozenset({"auth", "feishu-auth"})
ALLOWED_FIXED_EXPERT_SLASH_COMMANDS = (
    SAFE_FIXED_EXPERT_SLASH_COMMANDS | AUTH_FIXED_EXPERT_SLASH_COMMANDS
)
READONLY_DISABLED_TOOLSETS = frozenset({"delegation", "execute_code", "terminal"})


@dataclass(frozen=True)
class FixedExpertContext:
    profile_name: str
    profile_home: Path
    canonical_open_id: str
    union_id: str
    expert_id: str
    role_override_block: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class FixedExpertRejection:
    reason: str
    message: str


def fixed_expert_id_from_env() -> str:
    return str(os.getenv(FIXED_EXPERT_ENV) or "").strip()


def feishu_expert_readonly_enabled() -> bool:
    return str(os.getenv(READONLY_ENV) or "").strip() == "1"


def is_fixed_expert_slash_allowed(command: str) -> bool:
    return str(command or "").strip().lower() in ALLOWED_FIXED_EXPERT_SLASH_COMMANDS


def fixed_expert_slash_deny_message(command: str) -> str:
    cmd = str(command or "").strip().lower() or "unknown"
    return f"只读专家入口不支持 /{cmd}。可用命令：/auth、/status、/stop、/new。"


def _reject(reason: str) -> FixedExpertRejection:
    return FixedExpertRejection(
        reason=reason,
        message=(
            "这个只读专家入口仅支持已绑定且在投放专家白名单内的飞书用户。"
            "请联系管理员确认账号同步和专家权限。"
        ),
    )


def _nested_get(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if current is None:
            return None
        current = current.get(key) if isinstance(current, dict) else getattr(current, key, None)
    return current


def _normalize_union_id(value: Any) -> str:
    raw = str(value or "").strip()
    return raw if raw.startswith("on_") else ""


def extract_union_id(event: Any) -> str:
    source = getattr(event, "source", None)
    direct = (
        getattr(event, "sender_union_id", None),
        getattr(source, "union_id", None) if source is not None else None,
        getattr(source, "user_id_alt", None) if source is not None else None,
    )
    for candidate in direct:
        union_id = _normalize_union_id(candidate)
        if union_id:
            return union_id

    raw_candidates = (
        getattr(event, "raw", None),
        getattr(event, "raw_event", None),
        getattr(event, "event", None),
    )
    paths = (
        ("sender", "sender_id", "union_id"),
        ("event", "sender", "sender_id", "union_id"),
        ("event", "message", "sender", "sender_id", "union_id"),
        ("message", "sender", "sender_id", "union_id"),
        ("sender_id", "union_id"),
    )
    for raw in raw_candidates:
        for path in paths:
            union_id = _normalize_union_id(_nested_get(raw, path))
            if union_id:
                return union_id
    return ""


def _server_metadata(*, expert_id: str, union_id: str, canonical_open_id: str) -> dict[str, Any]:
    return {
        "expert_id": expert_id,
        "fixed_expert_union_id": union_id,
        "fixed_expert_open_id": canonical_open_id,
    }


def resolve_fixed_expert_context(
    event: Any,
    *,
    routing_table: Any,
    profile_home_resolver: Callable[[str], Path],
    expert_id: str | None = None,
) -> FixedExpertContext | FixedExpertRejection:
    fixed_expert = str(expert_id or fixed_expert_id_from_env()).strip()
    if not fixed_expert:
        return _reject("disabled")
    union_id = extract_union_id(event)
    if not union_id:
        return _reject("missing_union_id")
    if routing_table is None:
        return _reject("routing_unavailable")

    try:
        rows = list(routing_table.lookup_users_by_union_id(union_id))
    except Exception:
        logger.warning("multitenancy: fixed expert union lookup failed", exc_info=True)
        return _reject("routing_error")
    if not rows:
        return _reject("unknown_union_id")
    if len(rows) != 1:
        return _reject("duplicate_union_id")

    row = rows[0]
    canonical_open_id = str(getattr(row, "open_id", None) or "").strip()
    profile_name = str(getattr(row, "profile_name", None) or "").strip()
    if not canonical_open_id or not profile_name:
        return _reject("malformed_route")
    try:
        profile_home = Path(profile_home_resolver(profile_name))
        block = role_override_block_for(profile_home, fixed_expert, open_id=canonical_open_id)
    except Exception:
        logger.warning("multitenancy: fixed expert audience resolution failed", exc_info=True)
        return _reject("audience_error")
    if not block:
        return _reject("audience_denied")

    return FixedExpertContext(
        profile_name=profile_name,
        profile_home=profile_home,
        canonical_open_id=canonical_open_id,
        union_id=union_id,
        expert_id=fixed_expert,
        role_override_block=block,
        metadata=_server_metadata(
            expert_id=fixed_expert,
            union_id=union_id,
            canonical_open_id=canonical_open_id,
        ),
    )


def apply_fixed_expert_context(event: Any, context: FixedExpertContext) -> None:
    setattr(event, "sender_open_id", context.canonical_open_id)
    raw_event = getattr(event, "raw_event", None)
    if not isinstance(raw_event, dict):
        raw_event = {}
    else:
        raw_event = dict(raw_event)
    raw_event["metadata"] = dict(context.metadata)
    setattr(event, "raw_event", raw_event)
    setattr(
        event,
        "broker_role_override",
        {"expert_id": context.expert_id, "block": context.role_override_block},
    )
