"""Unit tests for the ``should_use_card`` content-based decision predicate.

Ports openclaw-lark ``src/card/reply-mode.ts::shouldUseCard`` +
``card-error.ts::findMarkdownTablesOutsideCodeBlocks`` to the multitenancy
plugin. The predicate decides whether a reply's TEXT contains markdown elements
that benefit from / require an interactive card (fenced code blocks or markdown
tables), accounting for the Feishu card table limit.

Wiring reality (see FORGE REPORT): the actual static-vs-streaming reply-mode
decision lives in core hermes-agent's FeishuAdapter, which this plugin does not
own. So the predicate is exposed + exhaustively unit-tested here, and the
table-degradation path (markdown_style.py) is verified to remain consistent.
"""
from hermes_multitenancy.card.card_error import (
    _FEISHU_CARD_TABLE_LIMIT,
    find_markdown_tables_outside_code_blocks,
    should_use_card,
)

NARROW_TABLE = "| 字段 | 值 |\n| --- | --- |\n| 状态 | online |"
CODE_BLOCK = "```python\nprint('hi')\n```"


def _table(n: int) -> str:
    return f"| k{n} | v{n} |\n| --- | --- |\n| 名称{n} | 值{n} |"


# ---------------------------------------------------------------------------
# should_use_card — the predicate
# ---------------------------------------------------------------------------

def test_plain_text_no_card_needed():
    assert should_use_card("这是一段普通文本，没有表格也没有代码。") is False


def test_empty_text_no_card_needed():
    assert should_use_card("") is False


def test_none_text_no_card_needed():
    # Defensive: the reply path may hand us None on an empty model turn.
    assert should_use_card(None) is False


def test_fenced_code_block_uses_card():
    assert should_use_card(f"运行：\n{CODE_BLOCK}") is True


def test_tilde_fenced_code_block_uses_card():
    assert should_use_card("~~~\nplain fenced\n~~~") is True


def test_single_table_within_limit_uses_card():
    assert should_use_card(f"状态如下：\n\n{NARROW_TABLE}") is True


def test_exactly_limit_tables_uses_card():
    text = "\n\n".join(_table(i) for i in range(_FEISHU_CARD_TABLE_LIMIT))
    assert should_use_card(text) is True


def test_tables_exceeding_limit_still_uses_card():
    # Beyond the limit a card is STILL used — the markdown_style degrade path
    # wraps the overflow tables so the card stays under 230099/11310. The
    # predicate must NOT fall back to plain text (which would lose the native
    # rendering of the first N tables).
    n = _FEISHU_CARD_TABLE_LIMIT + 2
    text = "\n\n".join(_table(i) for i in range(n))
    assert should_use_card(text) is True


def test_table_inside_code_block_not_counted_no_card():
    # A markdown table that only appears INSIDE a code fence is documentation,
    # not a renderable card table — it must not flip the predicate to "card".
    text = "```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```"
    # The fence itself is a code block, so a card IS warranted — but for the
    # CODE reason, not the table reason. Assert the table count is zero so the
    # decision is driven by the fence, not a miscounted table.
    assert find_markdown_tables_outside_code_blocks(text) == []
    assert should_use_card(text) is True


def test_table_only_inside_code_block_with_no_real_fence_reason():
    # Strip the card-worthiness of the fence by checking the table-finder in
    # isolation: a table that exists ONLY inside a fence contributes 0 tables.
    text = "前言\n```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```\n结语"
    assert find_markdown_tables_outside_code_blocks(text) == []


def test_table_outside_and_inside_code_block_counts_only_outside():
    real = NARROW_TABLE
    fenced = "```\n| x | y |\n| --- | --- |\n| 9 | 8 |\n```"
    text = f"{real}\n\n{fenced}"
    tables = find_markdown_tables_outside_code_blocks(text)
    assert len(tables) == 1  # only the real one outside the fence


# ---------------------------------------------------------------------------
# find_markdown_tables_outside_code_blocks — the supporting finder
# ---------------------------------------------------------------------------

def test_finder_returns_empty_for_plain_text():
    assert find_markdown_tables_outside_code_blocks("just words here") == []


def test_finder_counts_multiple_tables():
    text = "\n\n".join(_table(i) for i in range(4))
    assert len(find_markdown_tables_outside_code_blocks(text)) == 4


def test_finder_handles_none():
    assert find_markdown_tables_outside_code_blocks(None) == []


def test_finder_detects_colon_aligned_separator():
    text = "| a | b |\n| :--- | ---: |\n| 1 | 2 |"
    assert len(find_markdown_tables_outside_code_blocks(text)) == 1


def test_finder_detects_two_dash_separator():
    text = "| a | b |\n| -- | -- |\n| 1 | 2 |"
    assert len(find_markdown_tables_outside_code_blocks(text)) == 1
