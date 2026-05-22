"""Tool-call card rendering: rows, panel, args summary, narration strip.

Renders the openclaw-lark style ``Tool calls`` ``collapsible_panel`` plus the
inner ``- `tool_name` (Nms)`` rows, last 5 only. Secret-aware args summary
(masks ``token``/``secret``/``password``/``credential``/``authorization``)
and host-path narration scrubbing for the tool-process body cleanup.
"""
from __future__ import annotations

import json
import re
from typing import Any

from hermes_multitenancy.card.sanitization import _clip
from hermes_multitenancy.card.tool_use_config import _HIDDEN_TOOL_NAMES

_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*([\s\S]*?)\s*</tool_call>", re.IGNORECASE)


def _render_tool_calls_section(tools: list[Any]) -> str:
    normalized = _normalize_tool_rows(tools)[-5:]
    if not normalized:
        return ""
    lines = [_render_tool_call_line(tool) for tool in normalized]
    return "\n".join(lines)


def _render_tool_calls_panel(tool_section: str) -> dict[str, Any]:
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {"tag": "markdown", "content": "Tool calls"},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "5px"},
        "vertical_spacing": "8px",
        "padding": "8px 8px 8px 8px",
        "elements": [{"tag": "markdown", "content": tool_section, "text_size": "notation"}],
    }


def _normalize_tool_rows(tools: list[Any]) -> list[dict[str, Any]]:
    rows = [
        tool
        for tool in tools
        if isinstance(tool, dict)
        and str(tool.get("name") or "") not in _HIDDEN_TOOL_NAMES
    ]
    result: list[dict[str, Any]] = []
    for index, tool in enumerate(rows):
        name = str(tool.get("name") or "")
        preview = str(tool.get("preview") or "").strip().lower()
        has_later_concrete = any(
            str(later.get("name") or "") == name
            and (
                later.get("duration") is not None
                or later.get("status") in {"done", "error"}
                or later.get("args")
            )
            for later in rows[index + 1 :]
        )
        if preview == "generating arguments" and has_later_concrete:
            continue
        result.append(tool)
    return result


def _render_tool_call_line(tool: dict[str, Any]) -> str:
    name = str(tool.get("name") or "tool")
    status = str(tool.get("status") or "running")
    extra = " running" if status == "running" else " failed" if status == "error" else ""
    if tool.get("duration") is not None:
        extra = f" ({_format_tool_duration(tool['duration'])})"
    if name == "terminal":
        return f"- `{name}`{extra}"
    args_summary = _format_tool_args_summary(tool.get("args"))
    preview = str(tool.get("preview") or "").strip()
    if args_summary:
        return f"- `{name}`{extra}: {args_summary}"
    if tool.get("duration") is None and preview and preview.lower() != "generating arguments":
        extra = f": {_clip(preview, 160)}"
    return f"- `{name}`{extra}"


def _format_tool_args_summary(args: Any) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    parts: list[str] = []
    for key in sorted(args):
        value = args.get(key)
        key_text = str(key)
        if re.search(r"(token|secret|password|passwd|credential|authorization)", key_text, re.IGNORECASE):
            value_text = "[已隐藏]"
        else:
            value_text = _format_tool_arg_value(value)
        if value_text:
            parts.append(f"{key_text}={value_text}")
        if len(parts) >= 4:
            break
    return _clip(", ".join(parts), 220)


def _format_tool_arg_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        items = [_format_tool_arg_value(item) for item in list(value)[:4]]
        suffix = ", ..." if len(value) > 4 else ""
        return "[" + ", ".join(item for item in items if item) + suffix + "]"
    if isinstance(value, dict):
        keys = list(value.keys())[:4]
        inner = ", ".join(f"{key}:{_format_tool_arg_value(value.get(key))}" for key in keys)
        if len(value) > 4:
            inner += ", ..."
        return "{" + inner + "}"
    text = str(value).replace("\n", " ").strip()
    if not text:
        return ""
    if any(char.isspace() for char in text) or "," in text:
        return json.dumps(_clip(text, 120), ensure_ascii=False)
    return _clip(text, 120)


def _format_tool_duration(value: Any) -> str:
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
        return str(value)
    return f"{int(round(seconds * 1000))} ms"


def _extract_raw_tool_call_intents(text: str) -> tuple[list[dict[str, Any]], str]:
    raw = str(text or "")
    intents: list[dict[str, Any]] = []
    for match in _TOOL_CALL_BLOCK_RE.finditer(raw):
        payload = match.group(1).strip()
        try:
            parsed = json.loads(payload)
        except Exception:
            parsed = {}
        name = str(parsed.get("name") or "").strip() if isinstance(parsed, dict) else ""
        if name:
            intents.append({"name": name, "status": "error"})
    return intents, _strip_tool_call_blocks(raw)


def _merge_raw_tool_intents(state: dict[str, Any], intents: list[dict[str, Any]]) -> None:
    if not intents:
        return
    tools = state.setdefault("tools", [])
    if not isinstance(tools, list):
        state["tools"] = tools = []
    existing_names = {str(tool.get("name") or "") for tool in tools if isinstance(tool, dict)}
    for intent in intents:
        name = str(intent.get("name") or "")
        if name and name not in existing_names:
            tools.append(intent)
            existing_names.add(name)


def _strip_tool_call_blocks(text: str) -> str:
    return _TOOL_CALL_BLOCK_RE.sub("", str(text or ""))


_TOOL_PROCESS_NARRATION_RE = re.compile(
    r"^\s*(?:"
    r"我需要先|"
    r"让我先|"
    r"现在让我|"
    r"接下来(?:我)?(?:来|会|将)?|"
    r"找到了[\s\S]*(?:现在让我|让我|接下来)|"
    r"文件已下载[，,]?\s*(?:让我|现在让我|接下来)|"
    r"I need to|"
    r"Let me|"
    r"I'll (?:now|first)|"
    r"Now I'll|"
    r"Next(?:,| I)"
    r")",
    re.IGNORECASE,
)


def _strip_tool_process_narration(text: str, tools: list[Any]) -> str:
    raw = str(text or "").strip()
    if not raw or not tools:
        return raw
    paragraphs = re.split(r"\n\s*\n", raw)
    kept = [
        paragraph.strip()
        for paragraph in paragraphs
        if paragraph.strip() and not _TOOL_PROCESS_NARRATION_RE.match(paragraph.strip())
    ]
    if not kept:
        return raw
    return "\n\n".join(kept).strip()
