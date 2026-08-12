from __future__ import annotations

from dataclasses import asdict
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from hermes_multitenancy import cron_worker
from hermes_multitenancy.feishu_adapter_compat import load_feishu_module
from hermes_multitenancy.feishu_inbound_richtext import (
    _enrich_normalized_message,
    _MT_OWNED_TYPES,
    install_feishu_inbound_richtext_patch,
)
from dataclasses import fields as _dc_fields, replace as _dc_replace

feishu_core = load_feishu_module()


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


def test_interactive_raw_card_content_reads_column_set_text() -> None:
    install_feishu_inbound_richtext_patch()
    result = _normalize(
        "interactive",
        {
            "json_card": json.dumps(
                {
                    "body": {
                        "property": {
                            "elements": [
                                {
                                    "tag": "column_set",
                                    "property": {
                                        "columns": [
                                            {
                                                "tag": "column",
                                                "property": {
                                                    "elements": [
                                                        {
                                                            "tag": "markdown",
                                                            "property": {"content": "认证状态：需要重新授权"},
                                                        }
                                                    ]
                                                },
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    }
                },
                ensure_ascii=False,
            )
        },
    )

    assert result.text_content == "正文: 认证状态：需要重新授权"


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


# ── core 接管之后:这一层只许碰它自己拥有的两类 ──────────────────────────
#
# 这些用例**构造**归一化结果,不调 `_normalize`。原因是实测出来的:MT 的测试环境
# 装的是 PyPI hermes 0.14.0(main 线布局),而生产跑的是 0.19.1 发布线 —— 对着装
# 着的那个 core 断言,量的是第三条线的行为,与生产无关。这正是 2026-08-11 那次
# 回归的病根:任何"core 里有没有 X"的取证,只在一条线上做就是错的。

# 用生产发布线 core 对 24 份 converter golden 的实际输出实测得出:这 17 类
# core 已给出正文,本层原样返回。
CORE_OWNED_TEXT_TYPES = [
    "calendar", "email", "folder", "general_calendar", "hongbao", "location",
    "post", "share_calendar_event", "share_chat", "share_user", "sticker",
    "system", "text", "todo", "unknown", "video", "video_chat", "vote",
]

# core 对这 4 类**故意**留空正文,把引用放进 image_keys / metadata.placeholder_text。
# 本层过去覆盖成 "<file_key>\n<filename>",把内部 key 当成用户说的话喂给模型。
CORE_OWNED_MEDIA_TYPES = ["audio", "file", "image", "media"]


def _snapshot(raw_type: str, text_content: str, **fields: Any) -> Any:
    """构造一个「core 已经产出」的归一化结果,不依赖装着的是哪条 core 线。

    `fields` 里装不上的键会被丢弃 —— 生产发布线(0.19.1)的
    `media_refs` / `preferred_message_type` 在本仓测试环境的 0.14 dataclass 上
    可能不存在。所以保证不能只靠"我塞了这些字段",而要靠下面的**逐字段全等**
    断言:本层对非自有类型必须一个字段都不改,不管那条 core 线有哪些字段。
    """
    base = _normalize("text", {"text": "seed"})
    supported = {f.name for f in _dc_fields(base)}
    extras = {k: v for k, v in fields.items() if k in supported}
    return _dc_replace(base, raw_type=raw_type, text_content=text_content, **extras)


@pytest.mark.parametrize("raw_type", CORE_OWNED_TEXT_TYPES)
def test_core_owned_text_types_pass_through_untouched(raw_type: str) -> None:
    install_feishu_inbound_richtext_patch()
    core_text = f"<{raw_type} body produced by core>"
    # 字段要填满:全等断言只跟被填的字段一样强(变异实测,空 metadata 抓不到抹 metadata)
    src = _snapshot(
        raw_type,
        core_text,
        image_keys=["img_from_core"],
        metadata={"placeholder_text": f"[{raw_type}]", "core_owned": True},
        media_refs=[{"file_key": "k_from_core", "file_name": "n.bin"}],
        relation_kind=raw_type,
    )
    out = _enrich_normalized_message(
        result=src, message_type=raw_type, raw_content='{"subject": "SHOULD_NOT_APPEAR"}'
    )
    # 逐字段全等,不只是 text_content:一个丢掉 media_refs / 重置 relation_kind 的
    # 回归若只比正文就能溜过去(跨家族评审 #p2)。
    assert asdict(out) == asdict(src)
    assert "SHOULD_NOT_APPEAR" not in out.text_content


@pytest.mark.parametrize("raw_type", CORE_OWNED_MEDIA_TYPES)
def test_media_types_keep_cores_empty_body_contract(raw_type: str) -> None:
    """core 的契约是空正文 + 结构化引用;本层不得把 file_key 写成正文。"""
    install_feishu_inbound_richtext_patch()
    src = _snapshot(
        raw_type,
        "",
        metadata={"placeholder_text": "[Attachment: spec.pdf]"},
        media_refs=[{"file_key": "file_v3_key", "file_name": "spec.pdf"}],
        preferred_message_type="document",
        relation_kind="file",
    )
    out = _enrich_normalized_message(
        result=src,
        message_type=raw_type,
        raw_content='{"file_key": "file_v3_key", "file_name": "spec.pdf"}',
    )
    # 同上:全等断言才盖得住"附件引用被抹掉"这类回归。
    assert asdict(out) == asdict(src)
    assert out.text_content == ""
    assert "file_v3_key" not in str(out.text_content)
    assert out.metadata.get("placeholder_text") == "[Attachment: spec.pdf]"


def test_unparseable_payload_never_becomes_message_body() -> None:
    """解析残骸不是正文。core 已经拒绝这么做,本层曾经把它撤销。"""
    install_feishu_inbound_richtext_patch()
    debris = '{"text": "SENTINEL_SECRET_abc123", BROKEN'
    for raw_type in ("text", "interactive", "merge_forward"):
        src = _snapshot(raw_type, "")
        out = _enrich_normalized_message(result=src, message_type=raw_type, raw_content=debris)
        assert "SENTINEL_SECRET_abc123" not in str(out.text_content), raw_type


def test_negative_control_legitimate_card_body_still_visible() -> None:
    """上一条若因为"本层什么都不返回"而绿,这条会红 —— 证明它有判别力。"""
    install_feishu_inbound_richtext_patch()
    src = _snapshot("interactive", "")
    out = _enrich_normalized_message(
        result=src,
        message_type="interactive",
        raw_content=json.dumps({"card": {"elements": [{"tag": "div", "text": {"content": "SENTINEL_VISIBLE_xyz"}}]}}),
    )
    assert "SENTINEL_VISIBLE_xyz" in out.text_content


def test_approval_card_labels_survive_the_retirement() -> None:
    """本层唯一保留的加工面:中文审批卡字段抽取(core 零覆盖)。"""
    install_feishu_inbound_richtext_patch()
    src = _snapshot("interactive", "")
    payload = {"card": {"elements": [
        {"tag": "div", "text": {"content": "申请人: 张三"}},
        {"tag": "div", "text": {"content": "状态: 已通过"}},
    ]}}
    out = _enrich_normalized_message(
        result=src, message_type="interactive", raw_content=json.dumps(payload, ensure_ascii=False)
    )
    assert "申请人" in out.text_content
    assert "张三" in out.text_content
    assert "状态" in out.text_content


def test_negative_control_stubbing_the_extractor_kills_the_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """把抽取器常量化为空 → 上一条的断言必须失去依据(否则那条测的不是抽取器)。"""
    import hermes_multitenancy.feishu_inbound_richtext as mod

    install_feishu_inbound_richtext_patch()
    monkeypatch.setattr(mod, "_extract_interactive_card_text", lambda payload: "")
    src = _snapshot("interactive", "")
    payload = {"card": {"elements": [{"tag": "div", "text": {"content": "申请人: 张三"}}]}}
    out = _enrich_normalized_message(
        result=src, message_type="interactive", raw_content=json.dumps(payload, ensure_ascii=False)
    )
    assert "申请人" not in out.text_content


def test_owned_types_set_is_exactly_the_two_surfaces_that_still_do_work() -> None:
    assert _MT_OWNED_TYPES == frozenset({"merge_forward", "interactive", "card"})
    for retired in CORE_OWNED_TEXT_TYPES + CORE_OWNED_MEDIA_TYPES:
        assert retired not in _MT_OWNED_TYPES
