from __future__ import annotations

from dataclasses import asdict
import inspect
import json
from pathlib import Path
import sys
from typing import Any

import pytest

CORE_ROOT = "/Users/kite/code/hermes-agent"
if CORE_ROOT not in sys.path:
    sys.path.insert(0, CORE_ROOT)

import gateway.platforms.feishu as feishu_core

from hermes_multitenancy import cron_worker
from hermes_multitenancy.feishu_inbound_richtext import (
    install_feishu_inbound_richtext_patch,
)


@pytest.fixture(autouse=True)
def _restore_feishu_normalize() -> None:
    original = getattr(
        feishu_core.normalize_feishu_message,
        "_hermes_multitenancy_original",
        feishu_core.normalize_feishu_message,
    )
    feishu_core.normalize_feishu_message = original
    yield
    feishu_core.normalize_feishu_message = original


def _normalize(message_type: str, payload: Any) -> Any:
    raw_content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return feishu_core.normalize_feishu_message(
        message_type=message_type,
        raw_content=raw_content,
    )


def test_install_function_exists_and_is_wired() -> None:
    assert callable(install_feishu_inbound_richtext_patch)
    source = inspect.getsource(cron_worker.install_cron_runtime_patches)
    assert "install_feishu_inbound_richtext_patch" in source


def test_install_is_idempotent() -> None:
    install_feishu_inbound_richtext_patch()
    wrapped = feishu_core.normalize_feishu_message
    install_feishu_inbound_richtext_patch()
    assert feishu_core.normalize_feishu_message is wrapped
    assert getattr(wrapped, "_hermes_multitenancy_inbound_patched", False) is True


def test_enrichment_fail_open_returns_original_result(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = _normalize(
        "email",
        {"subject": "周报", "body": "今天修了一个问题"},
    )
    install_feishu_inbound_richtext_patch()

    from hermes_multitenancy import feishu_inbound_richtext as richtext

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(richtext, "_enrich_normalized_message", _boom)

    result = _normalize(
        "email",
        {"subject": "周报", "body": "今天修了一个问题"},
    )
    assert asdict(result) == asdict(baseline)


def test_email_enrichment_surfaces_subject_and_body() -> None:
    install_feishu_inbound_richtext_patch()
    result = _normalize(
        "email",
        {
            "subject": "值班告警",
            "body": "payments-api 在凌晨 03:00 连续失败三次。",
        },
    )
    assert result.text_content
    assert "值班告警" in result.text_content
    assert "payments-api" in result.text_content


def test_share_user_enrichment_surfaces_contact_name() -> None:
    install_feishu_inbound_richtext_patch()
    result = _normalize(
        "share_user",
        {"name": "张三", "user_id": "ou_user_123"},
    )
    assert result.text_content
    assert "张三" in result.text_content


def test_todo_enrichment_surfaces_title() -> None:
    install_feishu_inbound_richtext_patch()
    result = _normalize(
        "todo",
        {
            "summary": {
                "title": "提交发布审批",
                "content": [{"text": "需要 @ops 同步确认"}],
            },
            "due_time": "2026-06-10 18:00",
        },
    )
    assert result.text_content
    assert "提交发布审批" in result.text_content


def test_vote_enrichment_surfaces_topic_and_options() -> None:
    install_feishu_inbound_richtext_patch()
    result = _normalize(
        "vote",
        {
            "topic": "午饭吃什么",
            "options": [{"text": "饺子"}, {"name": "面条"}],
        },
    )
    assert "午饭吃什么" in result.text_content
    assert "• 饺子" in result.text_content
    assert "• 面条" in result.text_content


def test_unknown_type_uses_full_tree_text_fallback() -> None:
    install_feishu_inbound_richtext_patch()
    result = _normalize(
        "mystery_type",
        {
            "outer": {
                "inner": [{"label": "审批备注"}, {"text": "需要补充发票抬头"}],
            }
        },
    )
    assert result.text_content
    assert "审批备注" in result.text_content
    assert "需要补充发票抬头" in result.text_content


def test_merge_forward_enrichment_keeps_more_than_eight_entries() -> None:
    install_feishu_inbound_richtext_patch()
    payload = {
        "title": "昨日群聊",
        "messages": [
            {"sender_name": "用户%02d" % idx, "text": "第 %d 条消息" % idx}
            for idx in range(12)
        ],
    }
    result = _normalize("merge_forward", payload)
    lines = [line for line in result.text_content.splitlines() if line.startswith("- ")]
    assert len(lines) == 12


def test_merge_forward_media_uplift_preserves_placeholders() -> None:
    install_feishu_inbound_richtext_patch()
    result = _normalize(
        "merge_forward",
        {
            "title": "转发的富媒体",
            "messages": [
                {
                    "sender_name": "Alice",
                    "message_type": "image",
                    "image_key": "img_v3_demo",
                },
                {
                    "sender_name": "Bob",
                    "message_type": "file",
                    "file_key": "file_v1_demo",
                    "file_name": "设计稿.pdf",
                },
            ],
        },
    )
    assert "[图片]" in result.text_content
    assert "[文件: 设计稿.pdf]" in result.text_content
    assert "img_v3_demo" in result.image_keys
    assert result.metadata["uplifted_media"]
    assert any(item["file_key"] == "file_v1_demo" for item in result.metadata["uplifted_media"])


def test_interactive_card_enrichment_surfaces_applicant_subject_status_and_body() -> None:
    install_feishu_inbound_richtext_patch()
    result = _normalize(
        "interactive",
        {
            "card": {
                "header": {"title": {"tag": "plain_text", "content": "请假申请"}},
                "elements": [
                    {"tag": "div", "text": {"tag": "plain_text", "content": "申请人: 张三"}},
                    {"tag": "div", "text": {"tag": "plain_text", "content": "状态: 审批中"}},
                    {"tag": "div", "text": {"tag": "plain_text", "content": "正文: 6 月 10 日下午请假半天"}},
                ],
            }
        },
    )
    assert "主题: 请假申请" in result.text_content
    assert "申请人: 张三" in result.text_content
    assert "状态: 审批中" in result.text_content
    assert "正文: 6 月 10 日下午请假半天" in result.text_content


def test_interactive_raw_card_content_extracts_only_readable_card_text() -> None:
    install_feishu_inbound_richtext_patch()
    json_card = {
        "schema": 2,
        "header": {
            "tag": "card_header",
            "property": {
                "title": {
                    "tag": "plain_text",
                    "property": {"content": "⏰ 付款单日报"},
                }
            },
        },
        "body": {
            "property": {
                "elements": [
                    {
                        "tag": "markdown",
                        "id": "element-production-noise",
                        "property": {
                            "content": "**付款单日报**\nToken 已过期\n[查看详情](https://example.com/report)",
                            "text_size": "normal",
                        },
                    }
                ]
            }
        },
    }

    result = _normalize(
        "interactive",
        {
            "json_card": json.dumps(json_card, ensure_ascii=False),
            "card_schema": 2,
        },
    )

    assert result.text_content == (
        "主题: ⏰ 付款单日报\n"
        "正文: **付款单日报**\nToken 已过期\n[查看详情](https://example.com/report)"
    )
    assert "element-production-noise" not in result.text_content
    assert "text_size" not in result.text_content
    assert "json_card" not in result.text_content


def test_interactive_malformed_raw_card_content_uses_safe_placeholder() -> None:
    install_feishu_inbound_richtext_patch()

    result = _normalize("interactive", {"json_card": "{not-json", "image_key": "img_v3_secret"})

    assert result.text_content == "[interactive 消息]"
    assert "img_v3_secret" not in result.text_content


@pytest.mark.parametrize(
    ("message_type", "payload"),
    [
        ("text", {"text": "hello world"}),
        ("post", {"zh_cn": {"title": "标题", "content": [[{"tag": "text", "text": "正文"}]]}}),
        ("image", {"image_key": "img_x", "text": "说明"}),
    ],
)
def test_readable_message_types_remain_byte_identical(
    message_type: str,
    payload: dict[str, Any],
) -> None:
    before = _normalize(message_type, payload)
    install_feishu_inbound_richtext_patch()
    after = _normalize(message_type, payload)
    assert asdict(after) == asdict(before)


def test_new_source_has_no_core_repo_path_or_write_logic() -> None:
    source = Path("hermes_multitenancy/feishu_inbound_richtext.py").read_text()
    assert "hermes-agent" not in source
    assert "hermes_agent" not in source
    assert "write_text(" not in source
    assert "write_bytes(" not in source
    assert "os.write(" not in source
