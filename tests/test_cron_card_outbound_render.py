"""Regression: rich outbound markdown must render as a Feishu interactive card.

Core's ``FeishuAdapter._build_outbound_payload`` returns ``("text", ...)`` for
table content (and other cases), which Feishu shows as flattened plain text —
``##`` headings and ``| |`` tables appear literally (verified on prod). Like
openclaw-lark, we convert headings->bold and tables->bullet key/values, then
deliver rich content as an interactive card whose markdown element renders it.

These tests fail without the converter/upgrade (content stays flattened text).
"""
from __future__ import annotations

import json

from hermes_multitenancy.cron_worker import (
    _cardify_markdown_for_feishu,
    _convert_md_tables_for_card,
    _is_rich_outbound_markdown,
    _render_outbound_text_card,
)

_WEATHER = """## 🌤️ 天气预报
| 项目 | 详情 |
|------|------|
| ☀️ 天气 | **晴** |
| 🌡️ 气温 | 最高 24°C |

## 🗺️ 出行建议
- ✅ 好天气
- 👕 薄外套"""


# --------------------------------------------------------------------------- #
# rich detection                                                              #
# --------------------------------------------------------------------------- #
def test_heading_is_rich():
    assert _is_rich_outbound_markdown("## 标题\n正文") is True


def test_table_is_rich():
    assert _is_rich_outbound_markdown("| a | b |\n|---|---|\n| 1 | 2 |") is True


def test_two_list_items_is_rich():
    assert _is_rich_outbound_markdown("- 一\n- 二") is True


def test_single_line_not_rich():
    assert _is_rich_outbound_markdown("提醒：记得开会") is False


def test_one_list_item_not_rich():
    assert _is_rich_outbound_markdown("- 只有一条") is False


# --------------------------------------------------------------------------- #
# heading + table conversion                                                  #
# --------------------------------------------------------------------------- #
def test_heading_becomes_bold():
    out = _cardify_markdown_for_feishu("## 🚗 限行尾号\n正文")
    assert "**🚗 限行尾号**" in out
    assert "## " not in out  # no literal heading marker left


def test_two_col_table_becomes_key_value_bullets():
    out = _convert_md_tables_for_card("| 项目 | 详情 |\n|------|------|\n| 天气 | 晴 |\n| 气温 | 24°C |")
    assert "- **天气**：晴" in out
    assert "- **气温**：24°C" in out
    assert "|------" not in out and "| 项目 |" not in out  # no raw table syntax


def test_multi_col_table_renders_each_row():
    out = _convert_md_tables_for_card("| 排名 | 笔记 | 信号 |\n|---|---|---|\n| #1 | 高考 | 强 |")
    assert "**#1**" in out
    assert "笔记: 高考" in out and "信号: 强" in out
    assert "|---|" not in out


def test_full_weather_has_no_raw_markdown_syntax():
    out = _cardify_markdown_for_feishu(_WEATHER)
    assert "## " not in out          # headings converted
    assert "|------|" not in out     # table separator gone
    assert "| 项目 |" not in out     # table header gone
    assert "**🌤️ 天气预报**" in out  # heading -> bold
    assert "- **☀️ 天气**：**晴**" in out  # table row -> bullet
    assert "- ✅ 好天气" in out       # existing list preserved


# --------------------------------------------------------------------------- #
# card output shape                                                           #
# --------------------------------------------------------------------------- #
def test_render_outbound_text_card_shape():
    card = _render_outbound_text_card(_WEATHER)
    assert card["config"]["wide_screen_mode"] is True
    md = [e for e in card["elements"] if e.get("tag") == "markdown"]
    assert md, "card must have a markdown element"
    body = md[0]["content"]
    assert "## " not in body and "|------|" not in body  # rendered, not raw
    # card serializes cleanly (what we hand to Feishu as interactive content)
    json.loads(json.dumps(card, ensure_ascii=False))
