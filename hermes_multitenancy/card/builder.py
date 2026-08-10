"""Card JSON builders: initial CardKit v2 card, final v1-shape card, panels.

Owns the element_id constants, the openclaw-style streaming_config
(``print_strategy=delay``, ``print_frequency_ms=100``), and the initial
loading indicator. Final card layout: tool panel (top) → status (while
streaming) → reasoning panel → body markdown → Done footer.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, TypedDict

from .footer_config import get_show_metrics
from .markdown_style import _optimize_markdown_style
from .reasoning import (
    _clean_reasoning_prefix,
    _format_reasoning_label,
    _split_reasoning_text,
    _strip_reasoning_tags,
)
from .sanitization import _clip, _plain_summary
from .tool_use_display import (
    _render_tool_calls_panel,
    _render_tool_calls_section,
    _strip_tool_call_blocks,
    _strip_tool_process_narration,
    build_live_tool_use_panel,
)

logger = logging.getLogger("hermes_multitenancy.feishu_cardkit_compat")

_TOOLS_ELEMENT_ID = "tool_calls"
_STREAMING_ELEMENT_ID = "streaming_content"
_LOADING_ELEMENT_ID = "loading_icon"
_LOADING_ICON_IMG_KEY = "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg"
_STREAM_CARDKIT_CONTENT_MAX_CHARS = int(os.getenv("HERMES_CARD_STREAM_MAX_CHARS", "8000"))
_TRUNCATION_SUFFIX = "\n...[truncated]"


class _I18nMarkdown(TypedDict):
    zh_cn: str
    en_us: str


def _render_cardkit_initial_card() -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "streaming_config": {
                "print_frequency_ms": {"default": 100, "android": 100, "ios": 100, "pc": 100},
                "print_step": {"default": 1, "android": 1, "ios": 1, "pc": 1},
                "print_strategy": "delay",
            },
            "locales": ["zh_cn", "en_us"],
            "summary": {
                "content": "Processing...",
                "i18n_content": {"zh_cn": "处理中...", "en_us": "Processing..."},
            },
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "",
                    "text_align": "left",
                    "text_size": "normal_v2",
                    "margin": "0px 0px 0px 0px",
                    "element_id": _TOOLS_ELEMENT_ID,
                },
                {
                    "tag": "markdown",
                    "content": "",
                    "text_align": "left",
                    "text_size": "normal_v2",
                    "margin": "0px 0px 0px 0px",
                    "element_id": _STREAMING_ELEMENT_ID,
                },
                {
                    "tag": "markdown",
                    "content": " ",
                    "icon": {
                        "tag": "custom_icon",
                        "img_key": _LOADING_ICON_IMG_KEY,
                        "size": "16px 16px",
                    },
                    "element_id": _LOADING_ELEMENT_ID,
                },
            ]
        },
    }


def _render_stream_text(state: dict[str, Any]) -> str:
    content = _optimize_markdown_style(_strip_tool_call_blocks(_strip_reasoning_tags(str(state.get("content") or ""))).strip())
    reasoning = _clean_reasoning_prefix(str(state.get("reasoning") or "")).strip()
    tools = list(state.get("tools") or [])
    tool_section = _render_tool_calls_section(tools)
    content = _strip_tool_process_narration(content, tools)
    if len(content) > _STREAM_CARDKIT_CONTENT_MAX_CHARS:
        content = _clip(content, max(0, _STREAM_CARDKIT_CONTENT_MAX_CHARS - len(_TRUNCATION_SUFFIX)))

    parts: list[str] = []
    if content:
        parts.append(content)
    elif reasoning:
        # issue #4 (openclaw parity): live PRESENT-tense thinking indicator
        # while streaming (was past-tense "💭 Thought"). The final card switches
        # to "思考了 Ns" via _render_reasoning_panel.
        parts.append(f"💭 **思考中...**\n\n{_clip(reasoning, 1200)}")
    elif tool_section:
        # Tools running but no reasoning/content yet — show the live indicator
        # instead of a blank card (previously rendered an empty placeholder).
        parts.append("💭 **思考中...**")
    else:
        parts.append(" ")
    return "\n\n".join(part for part in parts if part).strip() or " "


def _render_message_card(state: dict[str, Any]) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    status = str(state.get("status") or "").strip()
    content, inline_reasoning = _split_reasoning_text(_strip_tool_call_blocks(str(state.get("content") or "")))
    reasoning = _clean_reasoning_prefix(str(state.get("reasoning") or inline_reasoning or "")).strip()
    content = _strip_reasoning_tags(content).strip()
    tools = list(state.get("tools") or [])

    tool_panel = _render_tool_section_panel(tools)
    if tool_panel is not None:
        elements.append(tool_panel)
    if status and not state.get("finalized"):
        elements.append({"tag": "markdown", "content": status})
    if reasoning:
        elements.append(_render_reasoning_panel(reasoning, state))
    if content:
        content = _strip_tool_process_narration(content, tools)
        elements.append({"tag": "markdown", "content": _optimize_markdown_style(content)})
    elif state.get("finalized") or state.get("aborted") or not elements:
        elements.append({"tag": "markdown", "content": "..."})
    if state.get("finalized") or state.get("aborted"):
        footer = _render_done_footer(state)
        elements.append(
            {
                "tag": "markdown",
                "content": footer["zh_cn"],
                "i18n_content": footer,
                "text_size": "notation",
            }
        )

    card = {
        "config": {
            "wide_screen_mode": _should_use_wide_screen_mode(
                content=content,
                reasoning=reasoning,
                tools=tools,
            ),
            "update_multi": True,
            "locales": ["zh_cn", "en_us"],
            "summary": {"content": _plain_summary(content or status or "Hermes")},
        },
        "elements": elements,
    }
    if state.get("errored"):
        # Billing outcomes are not system errors: sniff the body (single writer,
        # router/streaming) so the header explains instead of alarming.
        from ..billing_notices import BUDGET_EXCEEDED_NOTICE, RATE_LIMIT_NOTICE

        body = content or ""
        if BUDGET_EXCEEDED_NOTICE in body:
            template, title = "orange", "额度已用完"
        elif RATE_LIMIT_NOTICE in body:
            template, title = "orange", "请求过于频繁"
        else:
            template, title = "red", "执行出错"
        card["header"] = {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        }
    return card


def _render_tool_section_panel(tools: list[Any]) -> dict[str, Any] | None:
    """Render the final-card tool section, preferring the rich panel.

    The rich ``collapsible_panel`` (icon + human title + extracted detail +
    "查看 N 个步骤" suffix) is the primary output. It is fail-open: if rich
    rendering raises for any reason we fall back to the bare-row panel so the
    card — and message delivery — never breaks. Returns ``None`` when there are
    no visible tool rows so the caller omits the section entirely.
    """
    try:
        rich_panel = build_live_tool_use_panel(tools)
        if rich_panel is not None:
            return rich_panel
    except Exception:  # noqa: BLE001 — fail-open: never block delivery on render
        logger.warning(
            "multitenancy: rich tool-use panel failed, falling back to bare rows",
            exc_info=True,
        )
    tool_section = _render_tool_calls_section(tools)
    if tool_section:
        return _render_tool_calls_panel(tool_section)
    return None


def _should_use_wide_screen_mode(*, content: str, reasoning: str, tools: list[Any]) -> bool:
    """Keep status-only cards compact while preserving width for dense answers."""
    visible = str(content or "")
    if tools:
        return True
    if len(visible) > 500 or len(str(reasoning or "")) > 300:
        return True
    dense_markers = ("\n|", "```", "<table", "<img", "![")
    return any(marker in visible for marker in dense_markers)


def _to_cardkit2(card: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "2.0",
        "config": card.get("config", {}),
        "body": {"elements": card.get("elements", [])},
    }
    if card.get("header"):
        result["header"] = card["header"]
    return result


def _render_reasoning_panel(reasoning: str, state: dict[str, Any]) -> dict[str, Any]:
    zh_label, en_label = _format_reasoning_label(state)
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {
                "tag": "markdown",
                "content": f"💭 {en_label}",
                "i18n_content": {"zh_cn": f"💭 {zh_label}", "en_us": f"💭 {en_label}"},
            },
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
        "elements": [{"tag": "markdown", "content": _clip(reasoning, 1200), "text_size": "notation"}],
    }


def _render_done_footer(state: dict[str, Any]) -> _I18nMarkdown:
    elapsed = _format_elapsed_since_start(state)
    base: _I18nMarkdown = {
        "zh_cn": _format_footer_primary_line(state, elapsed, locale="zh_cn"),
        "en_us": _format_footer_primary_line(state, elapsed, locale="en_us"),
    }
    if not get_show_metrics():
        return base
    metrics = _format_metrics_line(state)
    if not metrics:
        return base
    return {
        "zh_cn": f"{base['zh_cn']}\n{metrics}",
        "en_us": f"{base['en_us']}\n{metrics}",
    }


def _format_footer_primary_line(state: dict[str, Any], elapsed: str, *, locale: str) -> str:
    if locale == "zh_cn":
        label = "出错" if state.get("errored") else "已停止" if state.get("aborted") else "已完成"
        return f"{label} · 耗时 {elapsed}"
    label = "Error" if state.get("errored") else "Stopped" if state.get("aborted") else "Completed"
    return f"{label} · Elapsed {elapsed}"


def _format_metrics_line(state: dict[str, Any]) -> str:
    """Render the optional second-line footer with LLM observability fields.

    Only the fields actually set on ``state`` are emitted; the line is empty
    when none of ``tokens_in`` / ``tokens_out`` / ``cache_hit_pct`` /
    ``context_pct`` / ``model_name`` is populated. Caller side gates rendering
    via ``FOOTER_SHOW_METRICS`` env flag — this function does not re-check it.
    """
    parts: list[str] = []
    tokens_in = state.get("tokens_in")
    tokens_out = state.get("tokens_out")
    if tokens_in is not None or tokens_out is not None:
        in_text = f"↑{int(tokens_in)}" if isinstance(tokens_in, (int, float)) else ""
        out_text = f"↓{int(tokens_out)}" if isinstance(tokens_out, (int, float)) else ""
        token_segment = " ".join(part for part in (in_text, out_text) if part)
        if token_segment:
            parts.append(f"tokens {token_segment}")
    cache_hit = state.get("cache_hit_pct")
    if isinstance(cache_hit, (int, float)):
        parts.append(f"cache {int(cache_hit)}%")
    context_pct = state.get("context_pct")
    if isinstance(context_pct, (int, float)):
        parts.append(f"ctx {int(context_pct)}%")
    model_name = state.get("model_name")
    if isinstance(model_name, str) and model_name.strip():
        parts.append(f"model {model_name.strip()}")
    return " · ".join(parts)


def _format_elapsed_since_start(state: dict[str, Any]) -> str:
    started_at = state.get("started_at")
    if isinstance(started_at, (int, float)) and started_at > 0:
        elapsed = max(0.0, time.monotonic() - float(started_at))
        if elapsed >= 60.0:
            total_seconds = int(elapsed)
            minutes, seconds = divmod(total_seconds, 60)
            return f"{minutes}m {seconds}s"
        return f"{elapsed:.1f}s"
    return "0.0s"
