"""Shared skill slash-command compatibility helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .commands import normalize_command_name, split_command_text

# Hardcoded base aliases (first-party). Plugin short commands are NOT a maintained
# table — each skill self-declares `slash_aliases` in its SKILL.md frontmatter, and
# `_scan_slash_aliases` builds the map from the CURRENT (caller-scoped) profile's
# installed skills. Resolution still goes through core resolve_skill_command_key, so
# an alias never mis-fires for a profile whose skill isn't present.
SKILL_SLASH_ALIASES = {
    "hades": "kep-hades-cli",
}

# {frozenset(skill_md_paths) -> {alias: skill_name}} — keyed on the scoped profile's
# skill set, so it's per-profile and invalidates when the installed skills change.
_ALIAS_SCAN_CACHE: dict[frozenset, dict[str, str]] = {}


def _scan_slash_aliases() -> dict[str, str]:
    """Build {short_alias: skill_name} from the current profile's installed skills'
    `slash_aliases` frontmatter. Dynamic + per-profile: reuses core get_skill_commands()
    (already scoped to the routed profile by the caller) and reads each skill's frontmatter.
    """
    try:
        from agent.skill_commands import get_skill_commands  # type: ignore

        from .skill_registry import read_skill_slash_aliases
    except Exception:  # noqa: BLE001 — alias scan must never break the /commands path
        return {}
    cmds = get_skill_commands()
    if not isinstance(cmds, dict):
        return {}
    # cache key = (path, mtime) per skill → invalidates when an installed skill's
    # SKILL.md changes IN PLACE (e.g. a plugin upgrade edits slash_aliases), not only
    # when the set of skills changes.
    entries: list[tuple[str, str]] = []
    for value in cmds.values():
        if not isinstance(value, dict):
            continue
        md_path, skill_name = value.get("skill_md_path"), value.get("name")
        if md_path and skill_name:
            entries.append((str(md_path), str(skill_name)))
    def _mtime(p: str) -> float:
        try:
            return Path(p).stat().st_mtime
        except OSError:
            return 0.0
    key = frozenset((p, _mtime(p)) for p, _ in entries)
    cached = _ALIAS_SCAN_CACHE.get(key)
    if cached is not None:
        return cached
    out: dict[str, str] = {}
    # deterministic: process skills sorted by name so duplicate aliases resolve the
    # same way every run (first skill by name wins).
    for md_path, skill_name in sorted(entries, key=lambda e: e[1]):
        for alias in read_skill_slash_aliases(Path(md_path)):
            out.setdefault(alias, skill_name)
    _ALIAS_SCAN_CACHE[key] = out
    return out


def _resolve_alias(cmd: str) -> Optional[str]:
    """Hardcoded base wins; otherwise the profile's skill-declared short aliases."""
    return SKILL_SLASH_ALIASES.get(cmd) or _scan_slash_aliases().get(cmd)


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
