"""Shared skill slash-command compatibility helpers."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .commands import normalize_command_name, split_command_text

# Hardcoded base aliases (first-party). Plugin-contributed aliases are loaded
# additively from <HERMES_HOME>/skill-slash-aliases.yaml (written by the
# plugin_ingest tool) so a plugin's short commands (e.g. /strategy) resolve to
# its real skill WITHOUT a core change — resolution still goes through core's
# resolve_skill_command_key, which returns None when the skill is absent, so an
# alias never mis-fires for a profile that doesn't have the plugin.
SKILL_SLASH_ALIASES = {
    "hades": "kep-hades-cli",
}

PLUGIN_SLASH_ALIASES_FILE = "skill-slash-aliases.yaml"


def _shared_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()


def _load_plugin_slash_aliases() -> dict[str, str]:
    """Load additive plugin aliases (`{cmd: skill_name}`) from the shared config.

    Config shape (plugin-tagged for clean uninstall):
        aliases:
          strategy: {skill: kep-trevi-strategy-recommend, plugin: keep-resource-delivery}
    A bare string value (`strategy: kep-...`) is also accepted. Missing/broken
    config is a silent no-op (returns {}), never a hard failure on the slash path.
    """
    path = _shared_home() / PLUGIN_SLASH_ALIASES_FILE
    try:
        import yaml  # lazy: the slash path must not hard-depend on yaml import cost

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — alias config must never break the /commands path
        return {}
    entries = raw.get("aliases") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    out: dict[str, str] = {}
    for cmd, val in entries.items():
        cmd_s = str(cmd or "").strip()
        if not cmd_s or "/" in cmd_s:
            continue
        skill = val.get("skill") if isinstance(val, dict) else val
        skill_s = str(skill or "").strip()
        if skill_s:
            out[cmd_s.replace("_", "-")] = skill_s
    return out


def _resolve_alias(cmd: str) -> Optional[str]:
    """Hardcoded base wins; otherwise fall back to plugin-contributed aliases."""
    return SKILL_SLASH_ALIASES.get(cmd) or _load_plugin_slash_aliases().get(cmd)


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
        alias = _resolve_alias(cmd)
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
