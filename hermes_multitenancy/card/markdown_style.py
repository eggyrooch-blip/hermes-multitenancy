"""Markdown style normalization + oversized-table degradation.

Mirrors openclaw-lark's safe-card markdown shape: card-v2 spacing rules
and the table-limit fallback (code-mode shape rather than openclaw's plain
text — see plan `card-table-reactive-fallback` for the reactive variant).
"""
from __future__ import annotations

import re
from typing import Any

from .sanitization import _strip_invalid_image_keys

_MARKDOWN_TABLE_MAX_NATIVE_COLUMNS = 4
_MARKDOWN_TABLE_MAX_NATIVE_ROWS = 12
_MARKDOWN_TABLE_MAX_NATIVE_CHARS = 1200
_MARKDOWN_TABLE_MAX_NATIVE_LINE_CHARS = 120


def _optimize_markdown_style(text: str, card_version: int = 2) -> str:
    try:
        return _strip_invalid_image_keys(_optimize_markdown_style_inner(text, card_version))
    except Exception:
        return str(text or "")


def _optimize_markdown_style_inner(text: str, card_version: int = 2) -> str:
    original = str(text or "")
    code_blocks: list[str] = []

    def stash_code_text(block: str) -> str:
        code_blocks.append(block)
        return f"___CB_{len(code_blocks) - 1}___"

    def stash_code_block(match: re.Match[str]) -> str:
        return stash_code_text(match.group(0))

    result = re.sub(r"```[\s\S]*?```", stash_code_block, original)
    result = _degrade_limited_markdown_tables(result, stash_code_text)
    if re.search(r"^#{1,3} ", original, flags=re.MULTILINE):
        result = re.sub(r"^#{2,6} (.+)$", r"##### \1", result, flags=re.MULTILINE)
        result = re.sub(r"^# (.+)$", r"#### \1", result, flags=re.MULTILINE)

    if card_version >= 2:
        result = re.sub(r"^(#{4,5} .+)\n{1,2}(#{4,5} )", r"\1\n<br>\n\2", result, flags=re.MULTILINE)
        result = re.sub(r"^([^|\n].*)\n(\|.+\|)", r"\1\n\n\2", result, flags=re.MULTILINE)
        result = re.sub(r"\n\n((?:\|.+\|[^\S\n]*\n?)+)", r"\n\n<br>\n\n\1", result)
        result = re.sub(r"((?:^\|.+\|[^\S\n]*\n?)+)", r"\1\n<br>\n", result, flags=re.MULTILINE)
        result = re.sub(r"^((?!#{4,5} )(?!\*\*).+)\n\n(<br>)\n\n(\|)", r"\1\n\2\n\3", result, flags=re.MULTILINE)
        result = re.sub(r"^(\*\*.+)\n\n(<br>)\n\n(\|)", r"\1\n\2\n\n\3", result, flags=re.MULTILINE)
        result = re.sub(r"(\|[^\n]*\n)\n(<br>\n)((?!#{4,5} )(?!\*\*))", r"\1\2\3", result, flags=re.MULTILINE)
        for index, block in enumerate(code_blocks):
            result = result.replace(f"___CB_{index}___", f"\n<br>\n{block}\n<br>\n")
    else:
        for index, block in enumerate(code_blocks):
            result = result.replace(f"___CB_{index}___", block)

    return re.sub(r"\n{3,}", "\n\n", result)


def _degrade_limited_markdown_tables(text: str, stash_code_text: Any) -> str:
    """Downgrade oversized markdown tables using openclaw-lark's code-mode shape."""
    lines = str(text or "").splitlines()
    result: list[str] = []
    index = 0
    while index < len(lines):
        if _is_markdown_table_start(lines, index):
            end = index + 2
            while end < len(lines) and _is_markdown_table_row(lines[end]):
                end += 1
            block_lines = lines[index:end]
            if _should_degrade_markdown_table(block_lines):
                result.append(stash_code_text(_render_markdown_table_code_block(block_lines)))
            else:
                result.extend(block_lines)
            index = end
            continue
        result.append(lines[index])
        index += 1
    return "\n".join(result)


def _is_markdown_table_start(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and _is_markdown_table_row(lines[index])
        and _is_markdown_table_separator(lines[index + 1])
    )


def _is_markdown_table_row(line: str) -> bool:
    stripped = str(line or "").strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _is_markdown_table_separator(line: str) -> bool:
    if not _is_markdown_table_row(line):
        return False
    cells = _split_markdown_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _split_markdown_table_row(line: str) -> list[str]:
    stripped = str(line or "").strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _should_degrade_markdown_table(block_lines: list[str]) -> bool:
    rows = [_split_markdown_table_row(line) for line in block_lines if _is_markdown_table_row(line)]
    columns = max((len(row) for row in rows), default=0)
    data_rows = max(0, len(rows) - 2)
    return (
        columns > _MARKDOWN_TABLE_MAX_NATIVE_COLUMNS
        or data_rows > _MARKDOWN_TABLE_MAX_NATIVE_ROWS
        or sum(len(line) for line in block_lines) > _MARKDOWN_TABLE_MAX_NATIVE_CHARS
        or any(len(line) > _MARKDOWN_TABLE_MAX_NATIVE_LINE_CHARS for line in block_lines)
    )


def _render_markdown_table_code_block(block_lines: list[str]) -> str:
    rows = [
        [_plain_table_cell(cell) for cell in _split_markdown_table_row(line)]
        for line in block_lines
        if _is_markdown_table_row(line)
    ]
    if len(rows) < 2:
        return "```\n" + "\n".join(block_lines) + "\n```"

    column_count = max(len(row) for row in rows)
    normalized_rows = [row + [""] * (column_count - len(row)) for row in rows]
    widths = [
        max(3, *(len(row[column]) for row in normalized_rows))
        for column in range(column_count)
    ]

    rendered: list[str] = []
    for row_index, row in enumerate(normalized_rows):
        if row_index == 1:
            cells = ["-" * width for width in widths]
        else:
            cells = [cell.ljust(widths[column]) for column, cell in enumerate(row)]
        rendered.append("| " + " | ".join(cells) + " |")
    return "```\n" + "\n".join(rendered) + "\n```"


def _plain_table_cell(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_([^_]+)_(?!_)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()
