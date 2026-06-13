"""Tool-use rendering configuration."""
from __future__ import annotations

from dataclasses import dataclass

_HIDDEN_TOOL_NAMES = {"skill_view"}
_TOOL_ROW_MAX = 20


@dataclass(frozen=True)
class ToolDescriptor:
    aliases: tuple[str, ...]
    icon_token: str
    title: str
    sanitizer: str
    param_keys: tuple[str, ...] = ()
    summary_patterns: tuple[str, ...] = ()
    summary_preference: tuple[str, ...] | None = None
    detail_from: str | None = None


DEFAULT_SUMMARY_PREFERENCE = ("matched", "code", "quoted", "url", "line")

TOOL_DESCRIPTORS: tuple[ToolDescriptor, ...] = (
    ToolDescriptor(
        aliases=("skill",),
        icon_token="app-default_outlined",
        title="Load skill",
        sanitizer="skill",
        param_keys=("skill", "name"),
        summary_patterns=(r"^(?:load|use)\s+skill\s+(.+)$",),
    ),
    ToolDescriptor(
        aliases=("read", "open"),
        icon_token="file-link-text_outlined",
        title="Read",
        sanitizer="path",
        param_keys=("file_path", "path", "file"),
        summary_patterns=(r"^(?:read|open)\s+(?:file\s+)?(.+)$",),
        summary_preference=("code", "quoted", "matched", "line"),
    ),
    ToolDescriptor(
        aliases=("write", "edit"),
        icon_token="edit_outlined",
        title="Edit",
        sanitizer="path",
        param_keys=("file_path", "path", "file"),
        summary_patterns=(r"^(?:edit|write)\s+(?:file\s+)?(.+)$",),
        summary_preference=("code", "quoted", "matched", "line"),
    ),
    ToolDescriptor(
        aliases=("web_search", "web-search", "search"),
        icon_token="search_outlined",
        title="Search web",
        sanitizer="search",
        param_keys=("query", "q"),
        summary_patterns=(r"^(?:search\s+(?:web\s+)?(?:for|about)|query)\s+(.+)$",),
        summary_preference=("quoted", "matched", "line"),
    ),
    ToolDescriptor(
        aliases=("web_fetch", "web-fetch", "fetch"),
        icon_token="language_outlined",
        title="Fetch web page",
        sanitizer="url",
        param_keys=("url",),
        summary_patterns=(r"^(?:fetch|open)\s+(?:web\s+page\s+)?(?:from\s+)?(.+)$",),
        summary_preference=("url", "matched", "quoted", "line"),
    ),
    ToolDescriptor(
        aliases=("grep",),
        icon_token="doc-search_outlined",
        title="Search text",
        sanitizer="generic",
        summary_patterns=(r"^(?:search\s+text(?:\s+by\s+pattern)?|grep)\s+(.+)$",),
        detail_from="pattern",
    ),
    ToolDescriptor(
        aliases=("glob",),
        icon_token="folder_outlined",
        title="Search files",
        sanitizer="generic",
        param_keys=("pattern",),
        summary_patterns=(r"^(?:search\s+files(?:\s+by\s+pattern)?|glob)\s+(.+)$",),
    ),
    ToolDescriptor(
        aliases=("exec", "bash", "command", "run"),
        icon_token="setting_outlined",
        title="Run command",
        sanitizer="command",
        param_keys=("description", "command", "script"),
        summary_patterns=(r"^(?:run|execute)\s+(?:command|script)?\s*(.+)$",),
        summary_preference=("code", "quoted", "matched", "line"),
    ),
    ToolDescriptor(
        aliases=("browser", "playwright", "navigate"),
        icon_token="browser-mac_outlined",
        title="Browser",
        sanitizer="url",
        param_keys=("url",),
        summary_patterns=(r"^(?:open|browse|visit|navigate\s+to)\s+(.+)$",),
        summary_preference=("url", "quoted", "matched", "line"),
    ),
    ToolDescriptor(
        aliases=("agent", "task", "spawn"),
        icon_token="robot_outlined",
        title="Run sub-agent",
        sanitizer="generic",
        param_keys=("task", "description", "prompt"),
        summary_patterns=(r"^(?:run\s+sub-?agent|spawn\s+agent)\s+(.+)$",),
    ),
    ToolDescriptor(
        aliases=("check", "determine", "verify"),
        icon_token="list-check_outlined",
        title="Check",
        sanitizer="generic",
        param_keys=("target", "subject", "description"),
    ),
    ToolDescriptor(
        aliases=("summarize", "analyze", "prepare"),
        icon_token="report_outlined",
        title="Analyze",
        sanitizer="generic",
        param_keys=("target", "subject", "description"),
    ),
)
