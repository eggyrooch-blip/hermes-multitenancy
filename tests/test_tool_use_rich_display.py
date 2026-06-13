from __future__ import annotations

from hermes_multitenancy.card import tool_use_display


def test_resolve_tool_descriptor_supports_exact_and_prefix_aliases() -> None:
    descriptor = tool_use_display.resolve_tool_descriptor("read")
    assert descriptor is not None
    assert descriptor.title == "Read"

    prefixed = tool_use_display.resolve_tool_descriptor("read_file")
    assert prefixed is not None
    assert prefixed.title == "Read"


def test_unknown_tool_falls_back_to_humanized_title_and_default_icon() -> None:
    display = tool_use_display.normalize_tool_use_display(
        [{"tool_name": "custom_tool_runner", "status": "success"}]
    )

    assert display["steps"][0]["title"] == "Custom tool runner"
    assert display["steps"][0]["icon_token"] == "setting-inter_outlined"


def test_extracts_read_detail_from_params_as_basename() -> None:
    display = tool_use_display.normalize_tool_use_display(
        [
            {
                "tool_name": "read",
                "params": {"file_path": "/tmp/projects/demo/readme.md"},
                "status": "success",
            }
        ]
    )

    assert display["steps"][0]["detail"] == "readme.md"
    assert display["content"] == "- Read: readme.md"


def test_extracts_search_detail_from_summary_when_params_missing() -> None:
    display = tool_use_display.normalize_tool_use_display(
        [
            {
                "tool_name": "web_search",
                "summary": 'Search web for "OpenAI API models"',
                "status": "success",
            }
        ]
    )

    assert display["steps"][0]["detail"] == "OpenAI API models"


def test_build_tool_use_title_suffix_is_bilingual_and_pluralized() -> None:
    assert tool_use_display.build_tool_use_title_suffix(1) == {
        "zh": "查看 1 个步骤",
        "en": "Show 1 step",
    }
    assert tool_use_display.build_tool_use_title_suffix(3) == {
        "zh": "查看 3 个步骤",
        "en": "Show 3 steps",
    }


def test_build_tool_use_panel_renders_title_detail_result_and_error_blocks() -> None:
    panel = tool_use_display.build_tool_use_panel(
        [
            {
                "tool_name": "exec",
                "params": {"command": 'python manage.py migrate --token "secret-value"'},
                "status": "success",
                "duration_ms": 250,
                "result": {"ok": True},
            },
            {
                "tool_name": "exec",
                "params": {"command": "echo hello"},
                "status": "error",
                "error": "permission denied",
            },
        ],
        show_result_details=True,
    )

    assert panel["tag"] == "collapsible_panel"
    title = panel["header"]["title"]
    assert title["content"] == "🛠️ Tool use · Show 2 steps"
    assert title["i18n"]["zh_cn"] == "🛠️ 工具执行 · 查看 2 个步骤"

    elements = panel["elements"]
    assert elements[0]["text"]["content"] == (
        "**Run command (250 ms)** · <font color='green'>Succeeded</font>"
    )
    assert elements[1]["text"]["content"] == "migrate --token [redacted]"
    assert "**Result**" in elements[2]["text"]["content"]
    assert "```json" in elements[2]["text"]["content"]
    assert "\"ok\": true" in elements[2]["text"]["content"]
    assert "**Error**" in elements[5]["text"]["content"]
    assert "permission denied" in elements[5]["text"]["content"]


def test_build_tool_use_panel_uses_placeholder_when_empty() -> None:
    panel = tool_use_display.build_tool_use_panel([])

    assert panel["elements"] == [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": tool_use_display.EMPTY_TOOL_USE_PLACEHOLDER,
                "i18n": {
                    "zh_cn": "暂无工具步骤",
                    "en_us": tool_use_display.EMPTY_TOOL_USE_PLACEHOLDER,
                },
            },
        }
    ]
