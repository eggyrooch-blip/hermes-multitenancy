"""Regression: markdown links must be clickable in Feishu plain-text (table) messages.

Upstream ``_build_outbound_payload`` force-sends markdown-table content as raw
plain text, so ``[label](url)`` arrives literally and is NOT clickable. The
multitenancy patch ``_patch_feishu_outbound_link_render`` rewrites it to
``label (url)`` so Feishu auto-linkifies the bare URL. ``post`` payloads already
render markdown links and are left untouched.

These tests fail without the patch (the link conversion never happens).
"""
from __future__ import annotations

import json
import sys
import types

from hermes_multitenancy.cron_worker import (
    _linkify_markdown_links_in_text,
    _patch_feishu_outbound_link_render,
)
from hermes_multitenancy.feishu_adapter_compat import _PLUGIN_LOADER_MODULE_NAME

_TABLE = (
    "| 排名 | 笔记 | 核心信号 |\n"
    "|---|---|---|\n"
    "| #6 | [高考安检](https://www.douyin.com/search/%E9%AB%98%E8%80%83) | 信号 |"
)


def test_linkify_converts_markdown_link_to_bare_url():
    src = "[高考安检](https://www.douyin.com/search/%E9%AB%98%E8%80%83)"
    out = _linkify_markdown_links_in_text(src)
    assert out == "高考安检 (https://www.douyin.com/search/%E9%AB%98%E8%80%83)"
    assert "](" not in out  # no leftover markdown link syntax


def test_linkify_leaves_image_syntax_untouched():
    # Markdown images ![alt](url) must NOT become "!alt (url)".
    src = "![cover](https://img.example.com/a.png)"
    assert _linkify_markdown_links_in_text(src) == src


def _install_fake_feishu(build_impl):
    class FakeFeishuAdapter:
        _build_outbound_payload = build_impl

    fake_module = types.ModuleType("gateway.platforms.feishu")
    fake_module.FeishuAdapter = FakeFeishuAdapter  # type: ignore[attr-defined]
    parents = ("gateway", "gateway.platforms")
    saved_parents = {name: sys.modules.get(name) for name in parents}
    saved_module = sys.modules.get("gateway.platforms.feishu")
    saved_synthetic = sys.modules.get(_PLUGIN_LOADER_MODULE_NAME)
    for name in parents:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["gateway.platforms.feishu"] = fake_module
    sys.modules[_PLUGIN_LOADER_MODULE_NAME] = fake_module
    return FakeFeishuAdapter, (saved_parents, saved_module, saved_synthetic)


def _restore(saved):
    saved_parents, saved_module, saved_synthetic = saved
    if saved_synthetic is None:
        sys.modules.pop(_PLUGIN_LOADER_MODULE_NAME, None)
    else:
        sys.modules[_PLUGIN_LOADER_MODULE_NAME] = saved_synthetic
    if saved_module is None:
        sys.modules.pop("gateway.platforms.feishu", None)
    else:
        sys.modules["gateway.platforms.feishu"] = saved_module
    for name, mod in saved_parents.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


def test_patch_linkifies_text_payload_but_leaves_post_untouched():
    # Mirror core behavior: table content -> raw "text"; markdown-only -> "post".
    def core_build(self, content):
        if "\n|" in content:
            return "text", json.dumps({"text": content}, ensure_ascii=False)
        return "post", json.dumps({"post": content}, ensure_ascii=False)

    FakeFeishuAdapter, saved = _install_fake_feishu(core_build)
    try:
        _patch_feishu_outbound_link_render()
        adapter = FakeFeishuAdapter()

        # text/table path: markdown link rewritten to a bare (auto-linkified) URL
        msg_type, payload = FakeFeishuAdapter._build_outbound_payload(adapter, _TABLE)
        assert msg_type == "text"
        text = json.loads(payload)["text"]
        assert "https://www.douyin.com/search/%E9%AB%98%E8%80%83" in text
        assert "](" not in text  # would fail WITHOUT the patch

        # post path: link syntax preserved (post renders it clickable already)
        mt2, pl2 = FakeFeishuAdapter._build_outbound_payload(
            adapter, "[t](https://x.com/s?k=a)"
        )
        assert mt2 == "post"
        assert "[t](https://x.com/s?k=a)" in json.loads(pl2)["post"]
    finally:
        _restore(saved)


def test_patch_forwards_new_core_keyword_arguments():
    seen = {}

    def core_build(self, content, *, prefer_post=False):
        seen["prefer_post"] = prefer_post
        return "text", json.dumps({"text": content}, ensure_ascii=False)

    FakeFeishuAdapter, saved = _install_fake_feishu(core_build)
    try:
        _patch_feishu_outbound_link_render()
        _, payload = FakeFeishuAdapter()._build_outbound_payload(
            "[docs](https://example.com)", prefer_post=True
        )
        assert seen["prefer_post"] is True
        assert json.loads(payload)["text"] == "docs (https://example.com)"
    finally:
        _restore(saved)


def test_patch_is_idempotent_and_json_safe():
    def core_build(self, content):
        if content == "__bad_json__":
            return "text", "{not valid json"
        return "text", json.dumps({"text": content}, ensure_ascii=False)

    FakeFeishuAdapter, saved = _install_fake_feishu(core_build)
    try:
        _patch_feishu_outbound_link_render()
        wrapped = FakeFeishuAdapter._build_outbound_payload
        # A second install must NOT re-wrap the already-patched method.
        _patch_feishu_outbound_link_render()
        assert FakeFeishuAdapter._build_outbound_payload is wrapped

        adapter = FakeFeishuAdapter()
        # Malformed JSON from core is passed through unchanged, no exception.
        mt, pl = FakeFeishuAdapter._build_outbound_payload(adapter, "__bad_json__")
        assert (mt, pl) == ("text", "{not valid json")
    finally:
        _restore(saved)
