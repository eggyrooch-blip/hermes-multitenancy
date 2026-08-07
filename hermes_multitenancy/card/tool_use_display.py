"""Tool-call card rendering: rows, panel, args summary, narration strip.

Renders the openclaw-lark style ``Tool calls`` ``collapsible_panel`` plus the
inner ``- `tool_name` (Nms)`` rows. Host-path narration scrubbing keeps the
tool-process body cleanup aligned with the pinned transport contract.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .secret_redaction import redact_inline_secrets
from .tool_use_config import (
    DEFAULT_SUMMARY_PREFERENCE,
    TOOL_DESCRIPTORS,
    ToolDescriptor,
    _HIDDEN_TOOL_NAMES,
    _TOOL_ROW_MAX,
)

_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*([\s\S]*?)\s*</tool_call>", re.IGNORECASE)
_TODO_PROGRESS_ICONS = {"completed": "✅", "in_progress": "●", "pending": "○", "cancelled": "✕"}
_TODO_PROGRESS_MAX_CHARS = 120
logger = logging.getLogger("hermes_multitenancy.feishu_cardkit_compat")


def _format_todo_progress(args: Any) -> str:
    """Return a compact, trusted display string for a full todo-list write."""
    if not isinstance(args, dict) or args.get("merge") or "todos" not in args:
        return ""
    todos = args.get("todos")
    if not isinstance(todos, list) or not todos:
        if todos not in (None, []):
            logger.debug("multitenancy: malformed todo payload (todos=%r)", type(todos))
        return ""
    icons: list[str] = []
    active: list[str] = []
    completed = 0
    for item in todos:
        if not isinstance(item, dict):
            logger.debug("multitenancy: malformed todo item (%r)", type(item))
            return ""
        status = str(item.get("status") or "pending").strip().lower()
        if status not in _TODO_PROGRESS_ICONS:
            logger.debug("multitenancy: malformed todo status (%r)", status)
            return ""
        completed += status == "completed"
        if status == "in_progress":
            content = str(item.get("content") or "").strip()
            if content:
                active.append(content)
        icons.append(_TODO_PROGRESS_ICONS[status])
    text = f"任务进度 {completed}/{len(todos)} {''.join(icons)}"
    if active:
        text += " · " + "；".join(active)
    return text[:_TODO_PROGRESS_MAX_CHARS]


def _render_tool_calls_section(tools: list[Any]) -> str:
    normalized = _apply_tool_row_cap(_normalize_tool_rows(tools))
    if not normalized:
        return ""
    lines = [_render_tool_call_line(tool) for tool in normalized]
    return "\n".join(lines)


_LIVE_STATUS_TO_TRACE_STATUS = {
    "running": "running",
    "done": "success",
    "error": "error",
}


def _live_tools_to_trace_steps(tools: list[Any]) -> list[dict[str, Any]]:
    """Bridge live ``state['tools']`` rows to the rich-panel trace-step shape.

    Live rows carry ``{name, status, preview, args, duration}`` (see
    ``streaming_controller`` tool-started/completed handlers); the rich renderer
    consumes ``{tool_name, params, summary, duration_ms, status}``. The same
    dedupe / hidden-tool / row-cap filtering used by the bare-row path is applied
    first so the two paths stay behaviorally aligned.
    """
    normalized = _apply_tool_row_cap(_normalize_tool_rows(tools))
    steps: list[dict[str, Any]] = []
    for tool in normalized:
        live_status = str(tool.get("status") or "running")
        step: dict[str, Any] = {
            "tool_name": str(tool.get("name") or "tool"),
            "status": _LIVE_STATUS_TO_TRACE_STATUS.get(live_status, "success"),
        }
        args = tool.get("args")
        if isinstance(args, dict) and args:
            step["params"] = args
        todo_progress = tool.get("todo_progress")
        if isinstance(todo_progress, str) and todo_progress:
            step["summary"] = todo_progress
        # NOTE: live `preview` is intentionally NOT forwarded as `summary`.
        # The bare-row path never displayed preview text (it only used the
        # "generating arguments" preview as a collapse SIGNAL in
        # _normalize_tool_rows). Forwarding it here would leak raw API previews
        # (e.g. "GET /open-apis/...") and the "generating arguments" placeholder
        # into the rich panel as visible detail — a regression vs bare-row
        # behavior. Descriptor-driven detail comes from params/args instead.
        duration = tool.get("duration")
        if isinstance(duration, (int, float)):
            step["duration_ms"] = int(round(float(duration) * 1000))
        steps.append(step)
    return steps


def build_live_tool_use_panel(tools: list[Any]) -> dict[str, Any] | None:
    """Rich ``collapsible_panel`` for the final card, built from live tool rows.

    Returns ``None`` when there are no visible tool rows so callers can omit the
    panel entirely. Never raises for ordinary input — callers still wrap the call
    fail-open so a malformed row can never block message delivery.
    """
    steps = _live_tools_to_trace_steps(tools)
    if not steps:
        return None
    return build_tool_use_panel(steps)


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
    todo_progress = tool.get("todo_progress")
    if isinstance(todo_progress, str) and todo_progress:
        return redact_inline_secrets(f"- {todo_progress}")
    name = str(tool.get("name") or "tool")
    status = str(tool.get("status") or "running")
    extra = " running" if status == "running" else " failed" if status == "error" else ""
    if tool.get("duration") is not None:
        duration = f"({_format_tool_duration(tool['duration'])})"
        extra = f" failed {duration}" if status == "error" else f" {duration}"
    return redact_inline_secrets(f"- `{name}`{extra}")


def _apply_tool_row_cap(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if _TOOL_ROW_MAX <= 0 or len(tools) <= _TOOL_ROW_MAX:
        return tools
    capped = tools[-_TOOL_ROW_MAX:]
    progress = next((tool for tool in reversed(tools) if tool.get("todo_progress")), None)
    if progress is not None and progress not in capped:
        capped[0] = progress
    return capped


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


# ---------------------------------------------------------------------------
# Rich tool-use display (openclaw-lark parity).
#
# Additive layer on top of the bare-row rendering above. Given structured
# trace steps (see ``tool_use_trace``) it resolves per-tool descriptors and
# produces a rich ``collapsible_panel`` with per-step title, detail, and
# result/error code blocks. When no descriptor matches a tool, it degrades to
# a humanized title + default icon — the bare-row path keeps working unchanged.
# ---------------------------------------------------------------------------

EMPTY_TOOL_USE_PLACEHOLDER = "No tool steps available"
_DEFAULT_ICON_TOKEN = "setting-inter_outlined"
_NO_RESULT_BLOCK_TITLES = frozenset({"Read", "Edit", "Fetch web page", "Browser"})
_NOISE_LINE_RE = re.compile(
    r"^(?:completed|complete|done|success|succeeded|running|started|finished|ok)$",
    re.IGNORECASE,
)
_STATUS_DISPLAY = {
    "running": ("Running", "turquoise"),
    "error": ("Failed", "red"),
    "success": ("Succeeded", "green"),
}
_SKILL_PATH_RE = re.compile(r"(?:^|/)skills/([^/]+)/", re.IGNORECASE)


def normalize_tool_use_display(
    trace_steps: list[dict[str, Any]] | None,
    *,
    show_full_paths: bool = False,
    show_result_details: bool = False,
) -> dict[str, Any]:
    """Map trace steps to rich display steps (openclaw ``normalizeToolUseDisplay``)."""
    steps: list[dict[str, Any]] = []
    for source in trace_steps or []:
        if not isinstance(source, dict):
            continue
        step = _format_tool_step(
            source,
            show_full_paths=show_full_paths,
            show_result_details=show_result_details,
        )
        if step is not None:
            steps.append(step)

    content = "\n".join(
        f"- {step['title']}: {step['detail']}" if step.get("detail") else f"- {step['title']}"
        for step in steps
    )
    return {"content": content, "step_count": len(steps), "steps": steps}


def build_tool_use_title_suffix(step_count: int) -> dict[str, str]:
    plural = "" if step_count == 1 else "s"
    return {"zh": f"查看 {step_count} 个步骤", "en": f"Show {step_count} step{plural}"}


def build_tool_use_panel(
    trace_steps: list[dict[str, Any]] | None,
    *,
    elapsed_ms: int | float | None = None,
    show_full_paths: bool = False,
    show_result_details: bool = False,
) -> dict[str, Any]:
    """Build the rich tool-use ``collapsible_panel`` (openclaw ``buildToolUsePanel``)."""
    display = normalize_tool_use_display(
        trace_steps,
        show_full_paths=show_full_paths,
        show_result_details=show_result_details,
    )
    steps = display["steps"]

    if steps:
        elements: list[dict[str, Any]] = []
        for step in steps:
            elements.extend(_build_tool_use_step_elements(step))
    else:
        elements = [_build_tool_use_placeholder()]

    # Preserve the established "Tool calls" panel-header contract (pinned by
    # tests/test_streaming_card_transport.py). G7's enrichment lives in the
    # ROWS (icons + human titles + extracted detail), not a renamed header —
    # changing the header text is not worth breaking the guard tests that also
    # assert skill_view-hidden / arg-gen-collapsed / narration-filtered.
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {"tag": "markdown", "content": "Tool calls"},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "color": "grey",
                "size": "16px 16px",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "5px"},
        "vertical_spacing": "4px",
        "padding": "8px 8px 8px 8px",
        "elements": elements,
    }


def resolve_tool_descriptor(tool_name: str | None) -> ToolDescriptor | None:
    normalized = _normalize_tool_name(tool_name)
    if not normalized:
        return None
    for descriptor in TOOL_DESCRIPTORS:
        for alias in descriptor.aliases:
            if (
                normalized == alias
                or normalized.startswith(f"{alias}_")
                or normalized.startswith(f"{alias}-")
            ):
                return descriptor
    return None


def _format_tool_step(
    source: dict[str, Any],
    *,
    show_full_paths: bool,
    show_result_details: bool,
) -> dict[str, Any] | None:
    tool_name = str(source.get("tool_name") or "")
    descriptor = resolve_tool_descriptor(tool_name)
    params = source.get("params") if isinstance(source.get("params"), dict) else None
    summary = source.get("summary")

    raw_detail: str | None = None
    if descriptor is not None:
        raw_detail = _extract_detail_from_params(params, descriptor)
        if raw_detail is None:
            raw_detail = _extract_detail_from_summary(summary, descriptor)
    elif isinstance(summary, str) and summary.strip():
        raw_detail = _cleanup_line(summary) or None

    sanitizer = descriptor.sanitizer if descriptor is not None else "generic"
    detail = (
        _sanitize_tool_detail(sanitizer, raw_detail, show_full_paths=show_full_paths)
        if raw_detail
        else None
    )
    title = _build_tool_title(source, descriptor, raw_detail)
    status = _resolve_step_status(source)
    error = source.get("error")
    error_block = (
        _build_display_block(
            _sanitize_block_value(error, sanitizer), fallback_language="text"
        )
        if error
        else None
    )
    result_block = None
    if error_block is None and show_result_details:
        result_block = _build_result_block(source, descriptor)

    return {
        "title": title,
        "detail": detail,
        "icon_token": descriptor.icon_token if descriptor is not None else _DEFAULT_ICON_TOKEN,
        "status": status,
        "result_block": result_block,
        "error_block": error_block,
    }


def _build_tool_title(
    source: dict[str, Any],
    descriptor: ToolDescriptor | None,
    raw_detail: str | None,
) -> str:
    if (
        descriptor is not None
        and descriptor.title == "Read"
        and raw_detail
        and _SKILL_PATH_RE.search(raw_detail)
    ):
        base = "Skill Read"
    elif descriptor is not None:
        base = descriptor.title
    else:
        base = _humanize_tool_name(str(source.get("tool_name") or "tool"))

    duration_ms = source.get("duration_ms")
    if isinstance(duration_ms, (int, float)):
        return f"{base} ({_format_duration_label(duration_ms)})"
    return base


def _extract_detail_from_params(
    params: dict[str, Any] | None, descriptor: ToolDescriptor
) -> str | None:
    if not params:
        return None
    if descriptor.detail_from == "pattern":
        return _build_pattern_detail(params)
    for key in descriptor.param_keys:
        text = _extract_scalar_text(params.get(key))
        if text:
            return text
    return None


def _build_pattern_detail(params: dict[str, Any]) -> str | None:
    pattern = _extract_scalar_text(params.get("pattern"))
    target = _extract_scalar_text(
        params.get("glob") or params.get("path") or params.get("file_path")
    )
    if pattern and target:
        return f"{pattern} in {target}"
    return pattern or target


def _extract_detail_from_summary(
    summary: Any, descriptor: ToolDescriptor
) -> str | None:
    if not isinstance(summary, str) or not summary:
        return None
    preference = descriptor.summary_preference or DEFAULT_SUMMARY_PREFERENCE
    patterns = [re.compile(pattern, re.IGNORECASE) for pattern in descriptor.summary_patterns]
    for raw_line in summary.replace("\r\n", "\n").split("\n"):
        line = _cleanup_line(_strip_markdown(raw_line))
        if not line or _NOISE_LINE_RE.match(line):
            continue
        signals = _build_summary_signals(line, patterns)
        for key in preference:
            value = signals.get(key)
            if value:
                return value
    return None


def _build_summary_signals(line: str, patterns: list[re.Pattern[str]]) -> dict[str, str | None]:
    matched: str | None = None
    for pattern in patterns:
        match = pattern.match(line)
        if match and match.group(1).strip():
            matched = match.group(1).strip()
            break
    return {
        "line": line,
        "matched": matched,
        "code": _extract_first_code_span(line),
        "quoted": _extract_first_quoted_text(line),
        "url": _extract_first_url(line),
    }


def _build_result_block(
    source: dict[str, Any], descriptor: ToolDescriptor | None
) -> dict[str, str] | None:
    result = source.get("result")
    if result is None:
        return None
    if descriptor is not None and descriptor.title in _NO_RESULT_BLOCK_TITLES:
        return None
    sanitizer = descriptor.sanitizer if descriptor is not None else "generic"
    return _build_display_block(_sanitize_block_value(result, sanitizer))


def _sanitize_block_value(value: Any, sanitizer: str) -> Any:
    if sanitizer == "command" and isinstance(value, str):
        return redact_inline_secrets(value)
    return value


def _build_display_block(
    value: Any, *, fallback_language: str = "json"
) -> dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.replace("\r\n", "\n").strip()
        if not normalized:
            return None
        parsed = _try_parse_json(normalized)
        if isinstance(parsed, (dict, list)):
            pretty = _stringify_json(parsed)
            if pretty is not None:
                return {"language": "json", "content": pretty}
        language = "text" if fallback_language == "json" else fallback_language
        return {"language": language, "content": normalized}
    if isinstance(value, (dict, list)):
        pretty = _stringify_json(value)
        if pretty is not None:
            return {"language": "json", "content": pretty}
    normalized = str(value).strip()
    return {"language": "text", "content": normalized} if normalized else None


def _build_tool_use_step_elements(step: dict[str, Any]) -> list[dict[str, Any]]:
    elements = [_build_step_title_element(step)]
    detail_element = _build_step_detail_element(step)
    if detail_element is not None:
        elements.append(detail_element)
    output_element = _build_step_output_element(step)
    if output_element is not None:
        elements.append(output_element)
    return elements


def _build_step_title_element(step: dict[str, Any]) -> dict[str, Any]:
    label, color = _STATUS_DISPLAY.get(step.get("status", "success"), _STATUS_DISPLAY["success"])
    title_md = (
        f"**{_escape_markdown_text(step['title'])}** · "
        f"<font color='{color}'>{label}</font>"
    )
    return {
        "tag": "div",
        "icon": {"tag": "standard_icon", "token": step["icon_token"], "color": "grey"},
        "text": {"tag": "lark_md", "content": title_md, "text_size": "notation"},
    }


def _build_step_detail_element(step: dict[str, Any]) -> dict[str, Any] | None:
    detail = (step.get("detail") or "").strip()
    if not detail:
        return None
    return {
        "tag": "div",
        "margin": "0px 0px 0px 20px",
        "text": {
            "tag": "plain_text",
            "content": detail,
            "text_color": "grey",
            "text_size": "notation",
        },
    }


def _build_step_output_element(step: dict[str, Any]) -> dict[str, Any] | None:
    content = _build_step_output_markdown(step)
    if not content:
        return None
    return {
        "tag": "div",
        "margin": "0px 0px 0px 20px",
        "text": {"tag": "lark_md", "content": content, "text_size": "notation"},
    }


def _build_step_output_markdown(step: dict[str, Any]) -> str | None:
    error_block = step.get("error_block")
    result_block = step.get("result_block")
    if error_block:
        return "\n".join(
            ["**Error**", _format_code_block(error_block["content"], error_block["language"])]
        )
    if result_block:
        return "\n".join(
            ["**Result**", _format_code_block(result_block["content"], result_block["language"])]
        )
    return None


def _build_tool_use_placeholder() -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {
            "tag": "plain_text",
            "content": EMPTY_TOOL_USE_PLACEHOLDER,
            "i18n": {"zh_cn": "暂无工具步骤", "en_us": EMPTY_TOOL_USE_PLACEHOLDER},
        },
    }


def _resolve_step_status(source: dict[str, Any]) -> str:
    if source.get("error"):
        return "error"
    status = source.get("status")
    if status in _STATUS_DISPLAY:
        return str(status)
    return "success"


def _normalize_tool_name(tool_name: str | None) -> str:
    return str(tool_name or "").strip().lower()


def _extract_scalar_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip() or None
    return None


def _sanitize_tool_detail(
    kind: str, value: str, *, show_full_paths: bool
) -> str | None:
    if kind == "command":
        cleaned = re.sub(r"\s+", " ", value).strip()
        if not cleaned:
            return None
        return _sanitize_command_like(cleaned, show_full_paths=show_full_paths)

    cleaned = _sanitize_generic_text(value)
    if not cleaned:
        return None
    if kind == "skill":
        stripped = re.sub(r"^skill\s+", "", cleaned, flags=re.IGNORECASE)
        return re.sub(r"[-_]+", " ", stripped).strip() or "skill"
    if kind == "path":
        return _sanitize_path_like(cleaned, show_full_paths=show_full_paths)
    if kind == "search":
        return _strip_quotes(cleaned)
    if kind == "url":
        return re.sub(r"^from\s+", "", _strip_quotes(cleaned), flags=re.IGNORECASE)
    return cleaned


def _sanitize_path_like(value: str, *, show_full_paths: bool) -> str:
    cleaned = re.sub(
        r"^(?:from|file|path)\s+", "", _sanitize_generic_text(value), flags=re.IGNORECASE
    ).strip()
    if show_full_paths:
        return cleaned
    skill_match = _SKILL_PATH_RE.search(cleaned)
    if skill_match:
        return re.sub(r"[-_]+", " ", skill_match.group(1)).strip() or cleaned
    segments = [segment for segment in re.split(r"[\\/]", cleaned) if segment]
    return segments[-1] if segments else cleaned


def _sanitize_command_like(value: str, *, show_full_paths: bool) -> str:
    cleaned = re.sub(
        r"^(?:command|script|description)\s+", "", value.strip(), flags=re.IGNORECASE
    )
    cleaned = re.sub(r"^.*?\s+->\s+", "", cleaned, flags=re.IGNORECASE).strip()
    # Drop a leading interpreter + script-path invocation (e.g. ``python
    # manage.py``) so the displayed detail focuses on the subcommand. Only the
    # interpreter pair is dropped — never the meaningful tail of the command.
    cleaned = re.sub(
        r"^(?:python[0-9.]*|node|bun|ruby|php|perl|sh|bash|zsh|deno)\s+"
        r"[^\s]+\.(?:py|js|ts|rb|php|pl|sh)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    if not cleaned:
        return "command"
    # Redact BEFORE stripping wrapping quotes: redaction matches quoted flag
    # values (``--token "x"`` -> ``--token "[redacted]"``); stripping the outer
    # quote first would break that match and leak the secret.
    redacted = redact_inline_secrets(cleaned)
    # Strip quotes that wrap a redacted placeholder so secrets read cleanly.
    redacted = re.sub(r"""(['"])\[redacted\]\1""", "[redacted]", redacted)
    return redacted if show_full_paths else _redact_command_paths(redacted)


def _redact_command_paths(command: str) -> str:
    return "".join(
        segment if not segment.strip() else _redact_command_token(segment)
        for segment in re.split(r"(\s+)", command)
    )


def _redact_command_token(token: str) -> str:
    match = re.match(r"""^([("'`]*)(.*?)([)"'`,;:]*)$""", token)
    if not match:
        return token
    prefix, core, suffix = match.group(1), match.group(2), match.group(3)
    return f"{prefix}{_redact_path_assignment(core)}{suffix}"


def _redact_path_assignment(value: str) -> str:
    equals_index = value.find("=")
    if equals_index > 0:
        left = value[: equals_index + 1]
        right = value[equals_index + 1 :]
        return f"{left}{_redact_standalone_path(right)}"
    return _redact_standalone_path(value)


def _redact_standalone_path(value: str) -> str:
    if re.match(r"^https?://", value, re.IGNORECASE):
        return _sanitize_url_for_display(value)
    if not _looks_like_path_token(value):
        return value
    return _basename_from_path(value)


def _sanitize_url_for_display(url: str) -> str:
    return re.sub(
        r"([?&])([A-Za-z0-9_.-]*(?:secret|token|password|key|credential|bearer|auth)[A-Za-z0-9_.-]*)=[^&#]*",
        r"\1\2=[redacted]",
        url,
        flags=re.IGNORECASE,
    )


def _looks_like_path_token(value: str) -> bool:
    return (
        value.startswith("~/")
        or value.startswith("./")
        or value.startswith("../")
        or value.startswith("/")
        or "/" in value
    )


def _basename_from_path(value: str) -> str:
    cleaned = value.replace("\\", "/").rstrip("/")
    segments = [segment for segment in cleaned.split("/") if segment]
    return segments[-1] if segments else value


def _sanitize_generic_text(value: str) -> str:
    no_tags = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", no_tags).strip()


def _cleanup_line(line: str) -> str:
    stripped = re.sub(r"^[-*•]\s*", "", line)
    stripped = re.sub(r"^\d+[.)]\s*", "", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def _strip_markdown(line: str) -> str:
    stripped = re.sub(r"`([^`]+)`", r"\1", line)
    stripped = re.sub(r"\*\*([^*]+)\*\*", r"\1", stripped)
    stripped = re.sub(r"\*([^*]+)\*", r"\1", stripped)
    return re.sub(r"^>\s*", "", stripped).strip()


def _humanize_tool_name(name: str) -> str:
    cleaned = re.sub(r"[-_]+", " ", name).strip()
    if not cleaned:
        return "Tool"
    return cleaned[0].upper() + cleaned[1:]


def _format_duration_label(duration_ms: int | float) -> str:
    if duration_ms < 1000:
        return f"{int(duration_ms)} ms"
    return f"{duration_ms / 1000:.1f} s"


def _strip_quotes(value: str) -> str:
    return re.sub(r"^[`'\"]+|[`'\"]+$", "", value).strip()


def _extract_first_code_span(value: str) -> str | None:
    match = re.search(r"`([^`]+)`", value)
    return match.group(1).strip() if match and match.group(1).strip() else None


def _extract_first_quoted_text(value: str) -> str | None:
    match = re.search(r"[\"']([^\"']+)[\"']", value)
    return match.group(1).strip() if match and match.group(1).strip() else None


def _extract_first_url(value: str) -> str | None:
    match = re.search(r"\bhttps?://[^\s\"'`]+", value, re.IGNORECASE)
    return match.group(0).strip() if match and match.group(0).strip() else None


def _escape_markdown_text(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    return re.sub(r"([`*_{}\[\]<>])", r"\\\1", escaped)


def _format_code_block(content: str, language: str) -> str:
    normalized = content.replace("\r\n", "\n").strip()
    longest = max((len(run) for run in re.findall(r"`+", normalized)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{normalized}\n{fence}"


def _try_parse_json(value: str) -> Any:
    trimmed = value.strip()
    if not trimmed or trimmed[0] not in "{[":
        return None
    try:
        return json.loads(trimmed)
    except (json.JSONDecodeError, ValueError):
        return None


def _stringify_json(value: Any) -> str | None:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return None
