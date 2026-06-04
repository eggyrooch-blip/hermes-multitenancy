"""Markdown style normalization + oversized-table degradation.

Mirrors openclaw-lark's safe-card markdown shape: card-v2 spacing rules
and the table-limit fallback (code-mode shape rather than openclaw's plain
text — see plan `card-table-reactive-fallback` for the reactive variant).
"""
from __future__ import annotations

import re
from typing import Any

from .sanitization import _strip_invalid_image_keys

# Boundary between the two card-safe shapes a table degrades into. A narrow
# table renders as an aligned monospace code block (clean, compact in the card);
# a wide one (too many columns or a too-long rendered line that would wrap into a
# mess on mobile) renders as a vertical key-value list instead. Either way the
# native ``|---|`` table form never reaches core, which would otherwise force the
# whole message to plain text and drop the card (feishu.py — md cards can't render
# tables).
_TABLE_CODEBLOCK_MAX_COLUMNS = 5
_TABLE_CODEBLOCK_MAX_LINE_CHARS = 100


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
    """Convert EVERY markdown table into a card-safe shape.

    Any native ``|---|`` table that survives to core makes core force the whole
    message to plain text (md cards can't render tables), dropping the card. So
    every table is degraded before core sees it: narrow → aligned code block,
    wide → vertical key-value list. (Previously only *oversized* tables were
    degraded; small ones leaked through and silently lost the card.)
    """
    lines = str(text or "").splitlines()
    result: list[str] = []
    index = 0
    while index < len(lines):
        if _is_markdown_table_start(lines, index):
            end = index + 2
            while end < len(lines) and _is_markdown_table_row(lines[end]):
                end += 1
            block_lines = lines[index:end]
            result.append(_render_markdown_table_for_card(block_lines, stash_code_text))
            index = end
            continue
        result.append(lines[index])
        index += 1
    return "\n".join(result)


# core (gateway/platforms/feishu.py) forces the whole message to plain text when
# `^\|.*\|\n\|[-|: ]+\|` (MULTILINE, no ``` stripping) matches the outbound
# content. We mirror those two line conditions EXACTLY so we detect — and degrade
# — every block core would catch. Earlier rounds leaked the card by under-matching
# (e.g. requiring "all cells are dashes" when core fires on a single first cell of
# `[-: ]`, colon-only, blank, or a missing trailing pipe). Matched on the RAW line
# (no strip): a leading space already defeats core's `^\|`, and so it should defeat
# ours too — that symmetry is what makes the indented code-block output safe.
_CORE_TABLE_LINE1 = re.compile(r"^\|.*\|")
_CORE_TABLE_LINE2 = re.compile(r"^\|[-|: ]+\|")


def _is_markdown_table_start(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and _CORE_TABLE_LINE1.match(str(lines[index] or "")) is not None
        and _is_markdown_table_separator(lines[index + 1])
    )


def _is_markdown_table_row(line: str) -> bool:
    stripped = str(line or "").strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _is_markdown_table_separator(line: str) -> bool:
    # core's second line: `\|[-|: ]+\|` anchored at the start of the raw line. NOT
    # "every cell is dashes" — core also fires when only the FIRST cell is
    # dash/colon/space (the rest can be data) and even without a trailing pipe.
    return _CORE_TABLE_LINE2.match(str(line or "")) is not None


def _split_markdown_table_row(line: str) -> list[str]:
    stripped = str(line or "").strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _render_markdown_table_code_block(block_lines: list[str]) -> str:
    rows = [
        [_plain_table_cell(cell) for cell in _split_markdown_table_row(line)]
        for line in block_lines
        if _is_markdown_table_row(line)
    ]
    if len(rows) < 2:
        body = list(block_lines)
    else:
        column_count = max(len(row) for row in rows)
        normalized_rows = [row + [""] * (column_count - len(row)) for row in rows]
        widths = [
            max(3, *(len(row[column]) for row in normalized_rows))
            for column in range(column_count)
        ]
        body = []
        for row_index, row in enumerate(normalized_rows):
            if row_index == 1:
                cells = ["-" * width for width in widths]
            else:
                cells = [cell.ljust(widths[column]) for column, cell in enumerate(row)]
            body.append("| " + " | ".join(cells) + " |")

    # Indent EVERY line by one space so no line starts with '|'. core's table
    # regex is `^\|.*\|\n\|[-|: ]+\|` (MULTILINE, re.search, NO ``` fence stripping):
    # the leading space defeats the `^\|` anchor for every line — the separator
    # AND any data row whose cells are only dashes/colons (e.g. a table that
    # documents markdown). So a fenced table can never trip core and drop the
    # card, while the ``` fence keeps it visually tabular.
    indented = "\n".join(" " + line for line in body)
    return "```\n" + indented + "\n```"


def _render_markdown_table_for_card(block_lines: list[str], stash_code_text: Any) -> str:
    """Pick a card-safe shape for one table block.

    Narrow (few columns AND no overly-long rendered line) → aligned monospace
    code block, stashed so the card renders it verbatim. Wide → vertical
    key-value list (``- **header**：value`` bullets) which reads cleanly on
    mobile and carries no table syntax. Degenerate header-only tables keep the
    aligned code-block form.
    """
    rows = [
        _split_markdown_table_row(line)
        for line in block_lines
        if _is_markdown_table_row(line)
    ]
    columns = max((len(row) for row in rows), default=0)
    code_block = _render_markdown_table_code_block(block_lines)
    inner_lines = code_block.split("\n")[1:-1]  # drop the ``` fences
    max_line = max((len(line) for line in inner_lines), default=0)
    if columns <= _TABLE_CODEBLOCK_MAX_COLUMNS and max_line <= _TABLE_CODEBLOCK_MAX_LINE_CHARS:
        return stash_code_text(code_block)
    kv = _render_markdown_table_kv_list(block_lines)
    return kv if kv else stash_code_text(code_block)


def _render_markdown_table_kv_list(block_lines: list[str]) -> str:
    """Render a table as vertical key-value bullets — no ``|---|`` table syntax.

    Each data row becomes a ``- **header**：value`` block; rows are separated by a
    blank line. Empty cells are skipped. Returns ``""`` when there is no data row
    (caller then keeps the code-block form).
    """
    rows = [
        [_plain_table_cell(cell) for cell in _split_markdown_table_row(line)]
        for line in block_lines
        if _is_markdown_table_row(line)
    ]
    if len(rows) < 3:  # header + separator + at least one data row
        return ""
    column_count = max(len(row) for row in rows)
    headers = rows[0] + [""] * (column_count - len(rows[0]))
    blocks: list[str] = []
    for row in rows[2:]:  # skip header (0) + separator (1)
        row = row + [""] * (column_count - len(row))
        out: list[str] = []
        for col in range(column_count):
            head = headers[col].strip()
            val = row[col].strip()
            if not val:
                continue
            out.append(f"- **{head}**：{val}" if head else f"- {val}")
        if out:
            blocks.append("\n".join(out))
    return "\n\n".join(blocks)


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
