"""Diagnostics reply/card subsystem — split out of router god-node (pure move).

Shim helpers/state routed through ``_m`` for monkeypatch fidelity.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
import os

from .. import router as _m


def _diagnostics_subject_context(
    event: Any, sender: Optional[str] = None, routed_profile: Optional[str] = None
):
    """Resolve (subject_open_id, identity, multitenancy) for the invoking user.

    Lets /doctor /diagnose identify WHO is asking and check THEIR real
    credential validity instead of "no subject". Prefers the already-resolved
    command ``sender`` (the routed user key), falling back to event resolution.
    ``routed_profile`` is the profile the live message actually routed to
    (multitenancy_router → feishu_<userid>); it's authoritative for display,
    used even when the routing table has no separate row. Fail-soft.
    """
    subject_open_id = None
    candidate = (sender or "").strip()
    if candidate and candidate.lower() != "unknown":
        subject_open_id = _m._normalize_feishu_open_id(candidate) or candidate
    if not subject_open_id:
        try:
            resolved = _m._resolve_sender_for_routing(event, fallback="")
            subject_open_id = _m._normalize_feishu_open_id(resolved) or (resolved or None)
        except Exception:
            subject_open_id = None
    if not subject_open_id:
        return None, None, None

    routed = (routed_profile or "").strip() or None
    identity: dict[str, Any] = {"open_id": subject_open_id, "profile": routed}
    multitenancy: dict[str, Any] = {
        "kind": "user",
        "profile": routed,
        "owner_open_id": None,
        "agent_id": None,
    }
    try:
        table = _m._get_routing_table()
    except Exception:
        table = None
    if table is not None:
        try:
            # Feishu open_id is per-APP; the router's ``sender`` is a short
            # routing key, not the vault key. Resolve the user's REAL open_id
            # (+ name) from the routed profile's row so the credential check
            # targets the right subject and the verdict is accurate.
            row = None
            if routed and hasattr(table, "lookup_by_profile_name"):
                row = table.lookup_by_profile_name(routed)
            if row is None:
                row = table.lookup_by_open_id(subject_open_id)
            if row is not None:
                real_open_id = str(getattr(row, "open_id", "") or "").strip()
                if real_open_id:
                    subject_open_id = real_open_id  # vault key for credential check
                identity["open_id"] = real_open_id or subject_open_id
                identity["name"] = (
                    getattr(row, "display_label", None)
                    or getattr(row, "user_id", None)
                    or getattr(row, "profile_name", None)
                )
                identity["profile"] = routed or getattr(row, "profile_name", None)
                multitenancy = {
                    "kind": getattr(row, "kind", None) or "user",
                    "profile": routed or getattr(row, "profile_name", None),
                    "owner_open_id": getattr(row, "owner_open_id", None),
                    "agent_id": getattr(row, "agent_id", None),
                }
            # Enumerate the agents this user owns (群聊 agents / 智能体 …),
            # rendering readable names: 智能体 → display_label; 群聊 → group name
            # resolved from chat_id via UAT (fail-soft to chat_id).
            try:
                owned = table.list_by_owner(subject_open_id)
                agents = []
                for r in owned or []:
                    kind = getattr(r, "kind", None) or "agent"
                    if kind == "user":
                        continue  # the user's own root row is not an "agent"
                    chat_id = getattr(r, "chat_id", None)
                    if kind == "group":
                        name = chat_id  # replaced below once chat names resolve
                    else:
                        name = (
                            getattr(r, "display_label", None)
                            or getattr(r, "profile_name", None)
                            or getattr(r, "agent_id", None)
                        )
                    agents.append(
                        {
                            "kind": kind,
                            "profile": getattr(r, "profile_name", None),
                            "chat_id": chat_id,
                            "name": name,
                        }
                    )
                # Resolve group chat names (cap to keep /diagnose responsive).
                group_chat_ids = [
                    a["chat_id"] for a in agents if a["kind"] == "group" and a.get("chat_id")
                ][:15]
                if group_chat_ids:
                    try:
                        from ..feishu_merge_forward_api import _resolve_chat_names_blocking

                        chat_names = _resolve_chat_names_blocking(
                            group_chat_ids, subject_open_id
                        )
                        for a in agents:
                            if a["kind"] == "group" and chat_names.get(a.get("chat_id")):
                                a["name"] = chat_names[a["chat_id"]]
                    except Exception:
                        _m.logger.debug(
                            "multitenancy: diagnostics chat-name resolution failed",
                            exc_info=True,
                        )
                multitenancy["agents"] = agents
            except Exception:
                _m.logger.debug("multitenancy: diagnostics agents listing failed", exc_info=True)
        except Exception:
            _m.logger.debug("multitenancy: diagnostics identity lookup failed", exc_info=True)

    # Resolve Feishu display names (never show raw open_id to the user).
    try:
        from ..feishu_merge_forward_api import _resolve_names_blocking

        owner_oid = (multitenancy or {}).get("owner_open_id")
        want = [subject_open_id] + ([owner_oid] if owner_oid else [])
        names = _resolve_names_blocking(want, subject_open_id)
        if names:
            if names.get(subject_open_id):
                identity["name"] = names[subject_open_id]
            if owner_oid and names.get(owner_oid) and multitenancy is not None:
                multitenancy["owner_name"] = names[owner_oid]
    except Exception:
        _m.logger.debug("multitenancy: diagnostics name resolution failed", exc_info=True)
    identity["hide_open_id"] = True
    return subject_open_id, identity, multitenancy


def _render_diagnostics_reply(
    cmd: str, event: Any, sender: Optional[str] = None, routed_profile: Optional[str] = None
) -> str:
    """Build the /doctor or /diagnose Markdown reply (read-only, fail-soft)."""
    locale = _m._event_locale(event)
    try:
        from .. import diagnostics

        subject_open_id, identity, multitenancy = _diagnostics_subject_context(
            event, sender, routed_profile
        )
        if cmd == "doctor":
            return diagnostics.render_doctor(
                locale=locale, subject_open_id=subject_open_id, identity=identity
            )
        return diagnostics.render_diagnose(
            locale=locale,
            subject_open_id=subject_open_id,
            identity=identity,
            multitenancy=multitenancy,
        )
    except Exception as exc:  # diagnostics is read-only; never crash the command
        _m.logger.warning("multitenancy: /%s diagnostics failed (%s)", cmd, exc)
        return f"诊断暂不可用：{exc.__class__.__name__}"


def _build_diagnostics_card(markdown: str, cmd: str, locale: str) -> dict[str, Any]:
    """Build a locale-labeled diagnostics card whose body is the rendered markdown."""
    del locale
    titles = {
        "doctor": {"zh_cn": "飞书机器人体检", "en_us": "Feishu Bot Doctor"},
        "diagnose": {"zh_cn": "飞书机器人诊断", "en_us": "Feishu Bot Diagnose"},
    }
    title_map = titles["doctor"] if cmd == "doctor" else titles["diagnose"]
    return {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": False,
            "update_multi": True,
            "locales": ["zh_cn", "en_us"],
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": title_map["en_us"],
                "i18n_content": {
                    "zh_cn": title_map["zh_cn"],
                    "en_us": title_map["en_us"],
                },
            },
            "subtitle": {"tag": "plain_text", "content": ""},
            "template": "blue",
            "padding": "12px 12px 12px 12px",
            "icon": {"tag": "standard_icon", "token": "info_filled"},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": markdown,
                }
            ]
        },
    }


async def _render_diagnostics_in_profile_scope(
    cmd: str,
    event: Any,
    *,
    sender: Optional[str] = None,
    routed_profile: Optional[str] = None,
    profile_home: Optional[Path] = None,
) -> str:
    """Render diagnostics with HERMES_HOME scoped to the routed profile so the
    profile-materialized credential resolves correctly. Shared home (routing db)
    is unchanged. Fail-soft: on any scoping error, render unscoped."""
    if profile_home is None:
        return _m._render_diagnostics_reply(
            cmd, event, sender=sender, routed_profile=routed_profile
        )
    from ..runtime import _get_env_lock

    async with _get_env_lock():
        old_home = os.environ.get("HERMES_HOME")
        had_home = "HERMES_HOME" in os.environ
        os.environ["HERMES_HOME"] = str(profile_home)
        try:
            return _m._render_diagnostics_reply(
                cmd, event, sender=sender, routed_profile=routed_profile
            )
        finally:
            if had_home:
                os.environ["HERMES_HOME"] = old_home  # type: ignore[arg-type]
            else:
                os.environ.pop("HERMES_HOME", None)


async def _send_diagnostics_card(
    adapter: Any,
    chat_id: str,
    cmd: str,
    event: Any,
    *,
    sender: Optional[str] = None,
    routed_profile: Optional[str] = None,
    profile_home: Optional[Path] = None,
) -> None:
    """Prefer sending diagnostics via interactive card, with text fallback."""
    if adapter is None:
        return

    # The user's UAT credential is profile-materialized, so credential_status
    # only reads "valid" from the routed profile's HERMES_HOME (from the router
    # home it reads "missing"). Scope the render to profile_home; the shared
    # home (routing db) stays ~/.hermes either way, so routing lookups are
    # unaffected.
    markdown = await _render_diagnostics_in_profile_scope(
        cmd, event, sender=sender, routed_profile=routed_profile, profile_home=profile_home
    )
    locale = _m._event_locale(event)
    card = _build_diagnostics_card(markdown, cmd, locale)

    sent = None
    try:
        from ..feishu_auth_cards import send_auth_card

        sent = await send_auth_card(adapter=adapter, chat_id=chat_id, card=card)
    except Exception as exc:
        _m.logger.warning("multitenancy: /%s diagnostics card send failed (%s)", cmd, exc)

    if sent:
        return
    try:
        await _m._safe_call(adapter.send, chat_id, markdown)
    except Exception as exc:
        _m.logger.warning("multitenancy: /%s diagnostics text fallback failed (%s)", cmd, exc)
