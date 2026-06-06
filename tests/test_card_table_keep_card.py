"""Feishu CardKit cards render markdown tables natively — but only up to
``_FEISHU_CARD_TABLE_LIMIT`` per card (the 4th+ trips 230099/11310). So
``_optimize_markdown_style`` keeps the first N tables as native ``|---|`` markdown
(Feishu renders them as real tables) and degrades only the OVERFLOW: narrow →
aligned code block, wide → vertical key-value list. Mirrors openclaw-lark
``sanitizeTextForCard``. The plugin sends via the CardKit API, which bypasses
core feishu.py's post/text path entirely, so core's table→plain-text forcing
(post-type 'md' only) never touches these cards — native tables are safe here.
"""
import re

import pytest

from hermes_multitenancy.card.markdown_style import (
    _FEISHU_CARD_TABLE_LIMIT,
    _optimize_markdown_style,
    _render_markdown_table_for_card,
    _render_markdown_table_kv_list,
)

# A native, card-renderable table: a `|...|` header line immediately followed by
# a `|---|` separator line, both anchored at line start (i.e. NOT indented inside
# a ``` code fence). Degraded tables are indented one space inside a fence, so
# they do not match this and are excluded from the native count.
_NATIVE_TABLE_RE = re.compile(r"^\|.*\|\n\|[-|: ]+\|", re.MULTILINE)

NARROW = ["| 字段 | 值 |", "| --- | --- |", "| 状态 | online |", "| ID | 69df |"]
WIDE = [
    "| a | b | c | d | e | f |",
    "| --- | --- | --- | --- | --- | --- |",
    "| 1 | 2 | 3 | 4 | 5 | 6 |",
]


def _native_table_count(text: str) -> int:
    """Count native (card-renderable) tables outside ``` code fences."""
    unfenced = re.sub(r"```[\s\S]*?```", "", text)
    return len(_NATIVE_TABLE_RE.findall(unfenced))


def _table(n: int) -> list[str]:
    """A small unique narrow table, tagged so its data is findable in output."""
    return [f"| k{n} | v{n} |", "| --- | --- |", f"| 名称{n} | 值{n} |"]


# ---------------------------------------------------------------------------
# Overflow degrader internals (unchanged contract — still degrades when called)
# ---------------------------------------------------------------------------

def test_narrow_overflow_goes_to_code_block_via_stash():
    stashed = []
    def stash(text):
        stashed.append(text)
        return f"<<CB{len(stashed)-1}>>"
    out = _render_markdown_table_for_card(NARROW, stash)
    assert out == "<<CB0>>"                 # code-block path (stashed)
    assert stashed[0].startswith("```")     # it IS a fenced code block
    assert "online" in stashed[0]


def test_wide_overflow_goes_to_kv_list_no_table_syntax():
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


# ---------------------------------------------------------------------------
# NEW invariant: tables within the limit stay native (render as real tables)
# ---------------------------------------------------------------------------

def test_optimize_keeps_single_narrow_table_native():
    text = "状态如下：\n\n" + "\n".join(NARROW)
    out = _optimize_markdown_style(text, card_version=2)
    assert _native_table_count(out) == 1      # kept as a real table, NOT degraded
    assert "| 状态 | online |" in out         # data row preserved verbatim
    assert "```" not in out                   # no code-block degradation


def test_optimize_keeps_single_wide_table_native():
    text = "数据：\n\n" + "\n".join(WIDE)
    out = _optimize_markdown_style(text, card_version=2)
    assert _native_table_count(out) == 1      # wide table also kept native now
    assert "| 1 | 2 | 3 | 4 | 5 | 6 |" in out
    assert "- **a**：1" not in out            # NOT degraded to kv-list


def test_optimize_keeps_exactly_limit_tables_native():
    tables = "\n\n".join("\n".join(_table(i)) for i in range(_FEISHU_CARD_TABLE_LIMIT))
    out = _optimize_markdown_style("汇总：\n\n" + tables, card_version=2)
    assert _native_table_count(out) == _FEISHU_CARD_TABLE_LIMIT  # all native
    assert "```" not in out                                      # none degraded
    for i in range(_FEISHU_CARD_TABLE_LIMIT):
        assert f"名称{i}" in out


# ---------------------------------------------------------------------------
# NEW invariant: overflow beyond the limit IS degraded (no 230099/11310)
# ---------------------------------------------------------------------------

def test_optimize_degrades_overflow_table_beyond_limit():
    n = _FEISHU_CARD_TABLE_LIMIT + 1
    tables = "\n\n".join("\n".join(_table(i)) for i in range(n))
    out = _optimize_markdown_style("很多表：\n\n" + tables, card_version=2)
    # exactly the first N stay native; the overflow is degraded into a fence
    assert _native_table_count(out) == _FEISHU_CARD_TABLE_LIMIT
    assert "```" in out                       # overflow degraded to code block
    # every table's data still reaches the user (native or degraded)
    for i in range(n):
        assert f"名称{i}" in out


def test_optimize_degrades_many_overflow_tables():
    n = _FEISHU_CARD_TABLE_LIMIT + 3
    tables = "\n\n".join("\n".join(_table(i)) for i in range(n))
    out = _optimize_markdown_style(tables, card_version=2)
    assert _native_table_count(out) == _FEISHU_CARD_TABLE_LIMIT  # never more than limit
    for i in range(n):
        assert f"名称{i}" in out


# ---------------------------------------------------------------------------
# Unchanged passthroughs
# ---------------------------------------------------------------------------

def test_no_table_text_unchanged_semantically():
    text = "第一行\n第二行\n- 普通列表"
    out = _optimize_markdown_style(text, card_version=2)
    assert "第一行" in out and "第二行" in out and "普通列表" in out


def test_code_fence_pseudo_table_untouched_and_uncounted():
    # A table inside a ``` fence is documentation, not a renderable card table:
    # it must NOT be counted toward the limit nor altered.
    text = "```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```"
    out = _optimize_markdown_style(text, card_version=2)
    assert "| 1 | 2 |" in out                  # fenced content preserved verbatim
    assert _native_table_count(out) == 0       # not counted as a real table


def test_fenced_table_does_not_consume_limit_budget():
    # 3 real tables + a fenced pseudo-table: all 3 real ones must stay native
    # (the fence must not eat a slot and push a real table into degradation).
    real = "\n\n".join("\n".join(_table(i)) for i in range(_FEISHU_CARD_TABLE_LIMIT))
    fenced = "```\n| x | y |\n| --- | --- |\n| 1 | 2 |\n```"
    out = _optimize_markdown_style(fenced + "\n\n" + real, card_version=2)
    assert _native_table_count(out) == _FEISHU_CARD_TABLE_LIMIT


@pytest.mark.parametrize("table", [
    NARROW,                                              # narrow 2-col
    ["| 值 |", "| --- |", "| online |"],                # single column
    ["| a | b |", "| -- | -- |", "| 1 | 2 |"],          # 2-dash separator
    ["| a | b |", "| :--- | ---: |", "| 1 | 2 |"],      # colon-aligned
    ["| a | b |", "| - | : |", "| 1 | 2 |"],            # mixed dash/colon separator
], ids=["narrow", "single-col", "2-dash", "colon", "mixed-sep"])
def test_single_table_of_each_shape_is_detected_and_kept_native(table):
    """A single table of each separator shape is detected as a table and kept
    native (so it would also be correctly COUNTED toward the overflow limit)."""
    out = _optimize_markdown_style("前言\n\n" + "\n".join(table), card_version=2)
    assert _native_table_count(out) == 1
    assert "```" not in out
