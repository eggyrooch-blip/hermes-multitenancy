from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class AuditLoadResult:
    rows: list[dict[str, Any]]
    total_lines: int
    bad_lines: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    terminal_rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Turn:
    timestamp: datetime
    date: str
    profile: str
    platform: str
    chat_type: str
    session_id: str
    text: str
    tools: set[str] = field(default_factory=set)
    skills: set[str] = field(default_factory=set)
    lark_commands: list[str] = field(default_factory=list)
    lark_modes: list[str] = field(default_factory=list)
    terminal_themes: set[str] = field(default_factory=set)
    final_content: str = ""
    has_final_stop: bool = False
    success_signal: bool = False
    explicit_failure: bool = False
    failure_category: str | None = None
    scenario: str = "General chat/Q&A"


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _message_sort_key(row: dict[str, Any]) -> tuple[datetime, int, str]:
    timestamp = row.get("_dt")
    if not isinstance(timestamp, datetime):
        timestamp = datetime.min.replace(tzinfo=timezone.utc)
    raw_id = row.get("message_id")
    try:
        numeric_id = int(raw_id)
    except Exception:
        numeric_id = 0
    return timestamp, numeric_id, str(raw_id or "")


def _session_group_key(row: dict[str, Any]) -> str:
    session_id = str(row.get("session_id") or "")
    if session_id:
        return session_id
    return "missing:" + "|".join(
        [
            str(row.get("profile") or ""),
            str(row.get("platform") or ""),
            str(row.get("chat_type") or ""),
        ]
    )


def load_audit_rows(path: Path) -> AuditLoadResult:
    rows: list[dict[str, Any]] = []
    terminal_rows: list[dict[str, Any]] = []
    total = 0
    bad = 0
    first: datetime | None = None
    last: datetime | None = None
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return AuditLoadResult([], 0, 0, None, None)
    with fh:
        for line in fh:
            total += 1
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if not isinstance(row, dict):
                bad += 1
                continue
            event_type = row.get("event_type")
            if event_type == "run_terminal":
                if row.get("schema_version") != 1:
                    continue
                timestamp = parse_timestamp(row.get("@timestamp"))
                if timestamp is None:
                    continue
                row["_dt"] = timestamp
                terminal_rows.append(row)
                first = timestamp if first is None or timestamp < first else first
                last = timestamp if last is None or timestamp > last else last
                continue
            if event_type != "conversation_message":
                continue
            timestamp = parse_timestamp(row.get("@timestamp"))
            if timestamp is None:
                bad += 1
                continue
            row["_dt"] = timestamp
            rows.append(row)
            first = timestamp if first is None or timestamp < first else first
            last = timestamp if last is None or timestamp > last else last
    rows.sort(key=_message_sort_key)
    terminal_rows.sort(key=lambda row: (row["_dt"], str(row.get("terminal_event_id") or "")))
    return AuditLoadResult(rows, total, bad, first, last, terminal_rows)


def _parse_tool_call(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


_SAFE_LARK_PLUS_ARG_RE = re.compile(r"^\+[A-Za-z0-9-]{1,40}$")
_LARK_COMMAND_GROUPS = {
    "api",
    "approval",
    "base",
    "calendar",
    "contact",
    "docs",
    "drive",
    "get",
    "im",
    "mail",
    "schema",
    "sheets",
    "vc",
    "wiki",
}
_LARK_SUBCOMMAND_WORDS = {"delete", "get", "images", "patch", "post", "put", "schema", "spaces"}


def _safe_lark_command_token(value: str, *, position: int) -> bool:
    token = value.strip()
    if not token:
        return False
    lowered = token.lower()
    if lowered.startswith(("ou_", "oc_", "om_", "img_")):
        return False
    if "://" in token or any(char in token for char in "=?&/"):
        return False
    if len(token) >= 32:
        return False
    if position == 0:
        return lowered in _LARK_COMMAND_GROUPS
    if token.startswith("+"):
        return bool(_SAFE_LARK_PLUS_ARG_RE.fullmatch(token))
    return lowered in _LARK_SUBCOMMAND_WORDS


def _lark_command(argv: Any) -> str | None:
    if not isinstance(argv, list) or not argv:
        return None
    parts = [
        str(item).strip()
        for position, item in enumerate(argv[:2])
        if _safe_lark_command_token(str(item), position=position)
    ]
    if not parts:
        return None
    return " ".join(parts)


def _terminal_themes(command: str) -> set[str]:
    text = command.lower()
    themes: set[str] = set()
    checks = {
        "lark-cli direct": ("lark-cli",),
        "python/script": ("python", ".py", "pandas", "csv", "json"),
        "node/npm": ("node", "npm", "pnpm", "bun", "tsx"),
        "http/curl": ("curl", "http://", "https://"),
        "file ops": ("ls ", "cat ", "find ", "sed ", "awk ", "cp ", "mv ", "tar "),
        "git/deploy": ("git ", "systemctl", "journalctl", "pytest", "make test"),
        "keep/internal api": ("kep", "keep", "example", "proxy.cms", "euler", "ark.example"),
    }
    for name, needles in checks.items():
        if any(needle in text for needle in needles):
            themes.add(name)
    return themes


def _collect_tool_metadata(row: dict[str, Any], turn: Turn) -> None:
    if row.get("tool_name"):
        turn.tools.add(str(row["tool_name"]))
    parsed = _parse_tool_call(row.get("tool_calls"))
    if not parsed:
        return
    name = parsed.get("name")
    if name:
        turn.tools.add(str(name))
    args = parsed.get("args")
    if not isinstance(args, dict):
        return
    if name == "skill_view":
        skill = args.get("name")
        if skill:
            turn.skills.add(str(skill))
    if name == "lark_cli":
        mode = args.get("mode")
        if mode:
            turn.lark_modes.append(str(mode))
        command = _lark_command(args.get("argv"))
        if command:
            turn.lark_commands.append(command)
    if name == "terminal":
        command = str(args.get("command") or "")
        turn.terminal_themes.update(_terminal_themes(command))


def build_turns(rows: Iterable[dict[str, Any]]) -> list[Turn]:
    by_session: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        session_id = _session_group_key(row)
        by_session.setdefault(session_id, []).append(row)

    turns: list[Turn] = []
    for session_id, session_rows in by_session.items():
        session_rows.sort(key=_message_sort_key)
        for index, row in enumerate(session_rows):
            if row.get("role") != "user":
                continue
            timestamp = row.get("_dt")
            if not isinstance(timestamp, datetime):
                continue
            next_user_index = len(session_rows)
            for cursor in range(index + 1, len(session_rows)):
                if session_rows[cursor].get("role") == "user":
                    next_user_index = cursor
                    break
            turn = Turn(
                timestamp=timestamp,
                date=timestamp.date().isoformat(),
                profile=str(row.get("profile") or ""),
                platform=str(row.get("platform") or ""),
                chat_type=str(row.get("chat_type") or ""),
                session_id=session_id,
                text=str(row.get("content") or ""),
            )
            for assistant in session_rows[index + 1:next_user_index]:
                if assistant.get("role") != "assistant":
                    continue
                _collect_tool_metadata(assistant, turn)
                if assistant.get("finish_reason") == "stop":
                    turn.has_final_stop = True
                    turn.final_content = str(assistant.get("content") or "")
            turns.append(turn)
    turns.sort(key=lambda item: item.timestamp)
    return turns
