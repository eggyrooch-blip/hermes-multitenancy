"""Every markdown table must leave native `|---|` form so core never forces the
whole message to plain text (which drops the card). Narrow → aligned code block,
wide → vertical key-value list."""
import re

import pytest

from hermes_multitenancy.card.markdown_style import (
    _optimize_markdown_style,
    _render_markdown_table_for_card,
    _render_markdown_table_kv_list,
)

# EXACT core regex (hermes-agent gateway/platforms/feishu.py): if this matches the
# outbound content, core forces the whole message to plain text and the card is
# dropped. The whole point of this slug is that it must NEVER match our output.
_CORE_TABLE_RE = re.compile(r"^\|.*\|\n\|[-|: ]+\|", re.MULTILINE)

NARROW = ["| 字段 | 值 |", "| --- | --- |", "| 状态 | online |", "| ID | 69df |"]
WIDE = [
    "| a | b | c | d | e | f |",
    "| --- | --- | --- | --- | --- | --- |",
    "| 1 | 2 | 3 | 4 | 5 | 6 |",
]


def test_narrow_table_goes_to_code_block_via_stash():
    stashed = []
    def stash(text):
        stashed.append(text)
        return f"<<CB{len(stashed)-1}>>"
    out = _render_markdown_table_for_card(NARROW, stash)
    assert out == "<<CB0>>"                 # code-block path (stashed)
    assert stashed[0].startswith("```")     # it IS a fenced code block
    assert "online" in stashed[0]


def test_wide_table_goes_to_kv_list_no_table_syntax():
    stashed = []
    out = _render_markdown_table_for_card(WIDE, lambda t: stashed.append(t) or "X")
    assert not stashed                       # NOT code-block
    assert "- **a**：1" in out               # key-value bullets
    assert "- **f**：6" in out
    assert "---" not in out                  # no native table separator


def test_kv_list_skips_header_separator_and_empty_cells():
    block = ["| k | v |", "| --- | --- |", "| 名称 | 测试 |", "| 备注 |  |"]
    out = _render_markdown_table_kv_list(block)
    assert "- **k**：名称" in out
    assert "- **v**：测试" in out
    assert "备注" in out                      # header present
    assert "- **v**：" not in out.split("\n\n")[-1]  # empty value cell skipped


def test_header_only_table_returns_empty_kv():
    assert _render_markdown_table_kv_list(["| a | b |", "| --- | --- |"]) == ""


def test_optimize_strips_native_table_for_narrow():
    import re
    text = "状态如下：\n\n" + "\n".join(NARROW)
    out = _optimize_markdown_style(text, card_version=2)
    assert "```" in out and "online" in out   # data kept inside a code block
    # outside the code fences, NO native `|---|` table separator may survive
    unfenced = re.sub(r"```[\s\S]*?```", "", out)
    assert not re.search(r"\n\s*\|[\s:|-]*-{3,}[\s:|-]*\|", "\n" + unfenced)


def test_optimize_wide_table_becomes_list():
    text = "数据：\n\n" + "\n".join(WIDE)
    out = _optimize_markdown_style(text, card_version=2)
    assert "- **a**：1" in out
    assert "| --- |" not in out               # no native table form survives


def test_no_table_text_unchanged_semantically():
    text = "第一行\n第二行\n- 普通列表"
    out = _optimize_markdown_style(text, card_version=2)
    assert "第一行" in out and "第二行" in out and "普通列表" in out


def test_code_fence_pseudo_table_untouched():
    text = "```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```"
    out = _optimize_markdown_style(text, card_version=2)
    assert "| 1 | 2 |" in out                 # fenced content preserved verbatim


@pytest.mark.parametrize("table", [
    NARROW,                                              # narrow 2-col
    WIDE,                                                # wide 6-col
    ["| 值 |", "| --- |", "| online |"],                # single column
    ["| a | b |", "| -- | -- |", "| 1 | 2 |"],          # 2-dash separator
    ["| a | b |", "| :--- | ---: |", "| 1 | 2 |"],      # colon-aligned
    ["| a | b | c | d | e | f |", "| - | - | - | - | - | - |", "| 1 | 2 | 3 | 4 | 5 | 6 |"],  # 1-dash wide
    ["| 语法 | 说明 |", "| --- | --- |", "| - | 列表项 |", "| : | 对齐 |"],  # data cells that are only dash/colon
    ["| a | b |", "| --- | --- |", "|   | 空首格 |"],   # blank first data cell
], ids=["narrow", "wide", "single-col", "2-dash", "colon", "1-dash-wide", "dash-data-cells", "blank-first-cell"])
def test_core_regex_never_matches_after_optimize(table):
    """The decisive guarantee: after our optimize, core's table regex must NOT
    match — otherwise core forces plain text and drops the card. Checked both
    with leading prose and at string start."""
    for text in ("\n".join(table), "前言\n\n" + "\n".join(table)):
        out = _optimize_markdown_style(text, card_version=2)
        assert _CORE_TABLE_RE.search(out) is None, f"core still forces plaintext:\n{out!r}"
