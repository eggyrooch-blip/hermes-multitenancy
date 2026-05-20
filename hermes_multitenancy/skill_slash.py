"""Shared skill slash-command compatibility helpers."""
from __future__ import annotations

from typing import Optional

from .commands import normalize_command_name, split_command_text


SKILL_SLASH_ALIASES = {
    "hades": "kep-hades-cli",
}


def rewrite_skill_slash_text(
    text: str,
    *,
    task_id: Optional[str] = None,
    platform: Optional[str] = None,
) -> Optional[str]:
    """Return native Hermes skill invocation text for ``/skill args``.

    The router sees Feishu slash commands before Hermes' native gateway, while
    WebUI/cron enter through Run Broker. This helper keeps skill aliases
    consistent across those entry points.
    """
    raw_text = str(text or "").strip()
    split = split_command_text(raw_text)
    if split is None:
        return None
    raw_cmd = normalize_command_name(split[0])
    args = split[1]
    if not raw_cmd or "/" in raw_cmd:
        return None
    cmd = raw_cmd.replace("_", "-")

    from agent.skill_commands import (  # type: ignore
        build_skill_invocation_message,
        get_skill_commands,
        resolve_skill_command_key,
    )

    skill_cmds = get_skill_commands()
    cmd_key = resolve_skill_command_key(cmd)
    if cmd_key is None:
        alias = SKILL_SLASH_ALIASES.get(cmd)
        if alias:
            cmd_key = resolve_skill_command_key(alias)
    if cmd_key is None:
        return None

    skill_name = (skill_cmds.get(cmd_key) or {}).get("name", "")
    if platform and skill_name:
        try:
            from agent.skill_utils import get_disabled_skill_names  # type: ignore

            if skill_name in get_disabled_skill_names(platform=platform):
                return None
        except Exception:
            pass

    msg = build_skill_invocation_message(cmd_key, args.strip(), task_id=task_id)
    return msg or None
