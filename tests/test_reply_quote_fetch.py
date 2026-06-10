"""Regression: enrich Feishu reply/quote text via the replying user's UAT.

The baseline core lookup degrades rich parent messages into a short snippet.
This test proves that behavior first, then shows the plugin patch replacing it
with a full `[message_id=...] sender: content` string without touching core.
"""
from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from pathlib import Path
import sys
import types
from typing import Any

import pytest


def _install_fake_feishu():
    class FakeFeishuAdapter:
        def __init__(self) -> None:
            self._message_text_cache: dict[str, str] = {}
            self.fetch_calls: list[str] = []
            self.downloaded_images: list[tuple[str, str]] = []
            self.last_event: Any = None
            self.last_reply_to_message_id: str | None = None
            self.last_reply_to_text: str | None = None
            self.last_media_urls: list[str] = []
            self.last_media_types: list[str] = []

        async def _fetch_message_text(self, message_id: str | None) -> str | None:
            if not message_id:
                return None
            self.fetch_calls.append(message_id)
            if message_id in self._message_text_cache:
                return self._message_text_cache[message_id]
            text = "正文: img <key> text"
            self._message_text_cache[message_id] = text
            return text

        async def _process_inbound_message(
            self,
            *,
            data: Any,
            message: Any,
            sender_id: Any,
            chat_type: str,
            message_id: str,
        ) -> None:
            reply_to_message_id = (
                getattr(message, "parent_id", None)
                or getattr(message, "upper_message_id", None)
                or getattr(message, "root_id", None)
                or None
            )
            reply_to_text = await self._fetch_message_text(reply_to_message_id) if reply_to_message_id else None
            event = SimpleNamespace(
                media_urls=[],
                media_types=[],
                reply_to_message_id=reply_to_message_id,
                reply_to_text=reply_to_text,
            )
            await self._dispatch_inbound_event(event)

        async def _dispatch_inbound_event(self, event: Any) -> None:
            self.last_event = event
            self.last_reply_to_message_id = event.reply_to_message_id
            self.last_reply_to_text = event.reply_to_text
            self.last_media_urls = list(event.media_urls)
            self.last_media_types = list(event.media_types)

        async def _download_feishu_image(self, *, message_id: str, image_key: str) -> tuple[str, str]:
            self.downloaded_images.append((message_id, image_key))
            return f"/tmp/{message_id}-{image_key}.jpg", "image/jpeg"

    def normalize_feishu_message(*, message_type: str, raw_content: str, **_: Any) -> Any:
        try:
            payload = json.loads(raw_content) if raw_content else {}
        except Exception:
            payload = {}
        text = ""
        if isinstance(payload, dict):
            for key in ("text", "content", "title"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    text = value.strip()
                    break
        image_keys = []
        if isinstance(payload, dict) and isinstance(payload.get("image_key"), str):
            image_keys.append(payload["image_key"])
        return SimpleNamespace(text_content=text, image_keys=image_keys, media_refs=[], metadata={})

    fake_module = types.ModuleType("gateway.platforms.feishu")
    fake_module.FeishuAdapter = FakeFeishuAdapter  # type: ignore[attr-defined]
    fake_module.normalize_feishu_message = normalize_feishu_message  # type: ignore[attr-defined]
    parents = ("gateway", "gateway.platforms")
    saved_parents = {name: sys.modules.get(name) for name in parents}
    saved_module = sys.modules.get("gateway.platforms.feishu")
    for name in parents:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["gateway.platforms.feishu"] = fake_module
    return FakeFeishuAdapter, (saved_parents, saved_module)


def _restore(saved: tuple[dict[str, Any], Any]) -> None:
    saved_parents, saved_module = saved
    if saved_module is None:
        sys.modules.pop("gateway.platforms.feishu", None)
    else:
        sys.modules["gateway.platforms.feishu"] = saved_module
    for name, mod in saved_parents.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


def _load_reply_module():
    sys.modules.pop("hermes_multitenancy.feishu_reply_quote_api", None)
    module = importlib.import_module("hermes_multitenancy.feishu_reply_quote_api")
    return importlib.reload(module)


@pytest.mark.asyncio
async def test_reply_quote_patch_replaces_degraded_parent_text_even_if_core_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        reply_module = _load_reply_module()
        adapter = FakeFeishuAdapter()
        message = SimpleNamespace(parent_id="om_parent_1", upper_message_id=None)
        sender_id = SimpleNamespace(open_id="ou_replying_user")

        await adapter._process_inbound_message(
            data={},
            message=message,
            sender_id=sender_id,
            chat_type="group",
            message_id="om_reply_1",
        )
        assert adapter.last_reply_to_text == "正文: img <key> text"
        assert adapter._message_text_cache["om_parent_1"] == "正文: img <key> text"

        monkeypatch.setattr(
            reply_module,
            "_fetch_parent_message_blocking",
            lambda message_id, replying_open_id: {
                "message_id": message_id,
                "msg_type": "text",
                "body": {"content": json.dumps({"text": "完整引用正文"}, ensure_ascii=False)},
                "sender": {"id": "ou_parent_author"},
            },
        )
        monkeypatch.setattr(
            reply_module,
            "_resolve_names_blocking",
            lambda open_ids, replying_open_id: {"ou_parent_author": "Alice"},
        )

        reply_module.install_feishu_reply_quote_api_patch()

        await adapter._process_inbound_message(
            data={},
            message=message,
            sender_id=sender_id,
            chat_type="group",
            message_id="om_reply_2",
        )

        assert adapter.last_reply_to_message_id == "om_parent_1"
        assert adapter.last_reply_to_text == "[message_id=om_parent_1] Alice: 完整引用正文"
        assert adapter._message_text_cache["om_parent_1"] == "正文: img <key> text"
    finally:
        _restore(saved)


@pytest.mark.asyncio
async def test_reply_quote_patch_uses_root_id_when_feishu_reply_only_sets_thread_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        reply_module = _load_reply_module()
        reply_module.install_feishu_reply_quote_api_patch()
        adapter = FakeFeishuAdapter()
        message = SimpleNamespace(parent_id=None, upper_message_id=None, root_id="om_root_parent")
        sender_id = SimpleNamespace(open_id="ou_replying_user")

        monkeypatch.setattr(
            reply_module,
            "_fetch_parent_message_blocking",
            lambda message_id, replying_open_id: {
                "message_id": message_id,
                "msg_type": "text",
                "body": {"content": json.dumps({"text": "root id 引用正文"}, ensure_ascii=False)},
                "sender": {"id": "ou_parent_author"},
            },
        )
        monkeypatch.setattr(
            reply_module,
            "_resolve_names_blocking",
            lambda open_ids, replying_open_id: {"ou_parent_author": "Alice"},
        )

        await adapter._process_inbound_message(
            data={},
            message=message,
            sender_id=sender_id,
            chat_type="group",
            message_id="om_reply_root",
        )

        assert adapter.last_reply_to_message_id == "om_root_parent"
        assert adapter.last_reply_to_text == "[message_id=om_root_parent] Alice: root id 引用正文"
    finally:
        _restore(saved)


@pytest.mark.asyncio
async def test_reply_quote_patch_materializes_quoted_image_as_inbound_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        reply_module = _load_reply_module()
        reply_module.install_feishu_reply_quote_api_patch()
        adapter = FakeFeishuAdapter()
        message = SimpleNamespace(parent_id="om_parent_img", upper_message_id=None, root_id=None)
        sender_id = SimpleNamespace(open_id="ou_replying_user")

        monkeypatch.setattr(
            reply_module,
            "_fetch_parent_message_blocking",
            lambda message_id, replying_open_id: {
                "message_id": message_id,
                "msg_type": "image",
                "body": {"content": json.dumps({"image_key": "img_parent_1"}, ensure_ascii=False)},
                "sender": {"id": "ou_parent_author"},
            },
        )
        monkeypatch.setattr(
            reply_module,
            "_resolve_names_blocking",
            lambda open_ids, replying_open_id: {"ou_parent_author": "Bob"},
        )

        await adapter._process_inbound_message(
            data={},
            message=message,
            sender_id=sender_id,
            chat_type="group",
            message_id="om_reply_img",
        )

        assert adapter.downloaded_images == [("om_parent_img", "img_parent_1")]
        assert adapter.last_media_urls == ["/tmp/om_parent_img-img_parent_1.jpg"]
        assert adapter.last_media_types == ["image/jpeg"]
        assert adapter.last_reply_to_text == (
            "[message_id=om_parent_img] Bob: [image]\n"
            "[Quoted media saved at: /tmp/om_parent_img-img_parent_1.jpg]"
        )
    finally:
        _restore(saved)


@pytest.mark.asyncio
async def test_reply_quote_patch_materializes_quoted_post_image_as_inbound_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        reply_module = _load_reply_module()
        reply_module.install_feishu_reply_quote_api_patch()
        adapter = FakeFeishuAdapter()
        message = SimpleNamespace(parent_id="om_parent_post", upper_message_id=None, root_id=None)
        sender_id = SimpleNamespace(open_id="ou_replying_user")

        monkeypatch.setattr(
            reply_module,
            "_fetch_parent_message_blocking",
            lambda message_id, replying_open_id: {
                "message_id": message_id,
                "msg_type": "post",
                "body": {
                    "content": json.dumps(
                        {
                            "content": [
                                [
                                    {"tag": "text", "text": "配图如下"},
                                    {"tag": "img", "image_key": "img_post_1"},
                                ]
                            ]
                        },
                        ensure_ascii=False,
                    )
                },
                "sender": {"id": "ou_parent_author"},
            },
        )
        monkeypatch.setattr(
            reply_module,
            "_resolve_names_blocking",
            lambda open_ids, replying_open_id: {"ou_parent_author": "Bob"},
        )

        await adapter._process_inbound_message(
            data={},
            message=message,
            sender_id=sender_id,
            chat_type="group",
            message_id="om_reply_post",
        )

        assert adapter.downloaded_images == [("om_parent_post", "img_post_1")]
        assert adapter.last_media_urls == ["/tmp/om_parent_post-img_post_1.jpg"]
        assert adapter.last_media_types == ["image/jpeg"]
        assert adapter.last_reply_to_text.endswith(
            "[Quoted media saved at: /tmp/om_parent_post-img_post_1.jpg]"
        )
    finally:
        _restore(saved)


@pytest.mark.asyncio
async def test_reply_quote_patch_fails_open_when_quoted_image_download_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        reply_module = _load_reply_module()
        reply_module.install_feishu_reply_quote_api_patch()
        adapter = FakeFeishuAdapter()
        message = SimpleNamespace(parent_id="om_parent_img_fail", upper_message_id=None, root_id=None)
        sender_id = SimpleNamespace(open_id="ou_replying_user")

        async def fail_download(**_kwargs: Any) -> tuple[str, str]:
            raise RuntimeError("resource denied")

        adapter._download_feishu_image = fail_download  # type: ignore[method-assign]

        monkeypatch.setattr(
            reply_module,
            "_fetch_parent_message_blocking",
            lambda message_id, replying_open_id: {
                "message_id": message_id,
                "msg_type": "image",
                "body": {"content": json.dumps({"image_key": "img_denied"}, ensure_ascii=False)},
                "sender": {"id": "ou_parent_author"},
            },
        )
        monkeypatch.setattr(
            reply_module,
            "_resolve_names_blocking",
            lambda open_ids, replying_open_id: {"ou_parent_author": "Bob"},
        )

        await adapter._process_inbound_message(
            data={},
            message=message,
            sender_id=sender_id,
            chat_type="group",
            message_id="om_reply_img_fail",
        )

        assert adapter.last_media_urls == []
        assert adapter.last_media_types == []
        assert adapter.last_reply_to_text == "[message_id=om_parent_img_fail] Bob: [image]"
    finally:
        _restore(saved)


@pytest.mark.asyncio
async def test_reply_quote_patch_keeps_p2p_reply_text_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        reply_module = _load_reply_module()
        reply_module.install_feishu_reply_quote_api_patch()
        adapter = FakeFeishuAdapter()
        message = SimpleNamespace(parent_id="om_parent_dm", upper_message_id=None, root_id=None)
        sender_id = SimpleNamespace(open_id="ou_replying_user")

        monkeypatch.setattr(
            reply_module,
            "_fetch_parent_message_blocking",
            lambda message_id, replying_open_id: {
                "message_id": message_id,
                "msg_type": "text",
                "body": {"content": json.dumps({"text": "单聊引用正文"}, ensure_ascii=False)},
                "sender": {"id": "ou_parent_author"},
            },
        )
        monkeypatch.setattr(
            reply_module,
            "_resolve_names_blocking",
            lambda open_ids, replying_open_id: {"ou_parent_author": "Alice"},
        )

        await adapter._process_inbound_message(
            data={},
            message=message,
            sender_id=sender_id,
            chat_type="p2p",
            message_id="om_reply_dm",
        )

        assert adapter.last_media_urls == []
        assert adapter.last_media_types == []
        assert adapter.last_reply_to_text == "[message_id=om_parent_dm] Alice: 单聊引用正文"
    finally:
        _restore(saved)


@pytest.mark.asyncio
async def test_reply_quote_patch_fails_open_to_original_text(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        reply_module = _load_reply_module()
        reply_module.install_feishu_reply_quote_api_patch()
        adapter = FakeFeishuAdapter()
        message = SimpleNamespace(parent_id="om_parent_2", upper_message_id=None)
        sender_id = SimpleNamespace(open_id="ou_replying_user")

        monkeypatch.setattr(
            reply_module,
            "_fetch_parent_message_blocking",
            lambda message_id, replying_open_id: (_ for _ in ()).throw(RuntimeError("no uat")),
        )
        await adapter._process_inbound_message(
            data={},
            message=message,
            sender_id=sender_id,
            chat_type="group",
            message_id="om_reply_3",
        )
        assert adapter.last_reply_to_text == "正文: img <key> text"

        monkeypatch.setattr(
            reply_module,
            "_fetch_parent_message_blocking",
            lambda message_id, replying_open_id: None,
        )
        await adapter._process_inbound_message(
            data={},
            message=message,
            sender_id=sender_id,
            chat_type="group",
            message_id="om_reply_4",
        )
        assert adapter.last_reply_to_text == "正文: img <key> text"
    finally:
        _restore(saved)


@pytest.mark.asyncio
async def test_reply_quote_patch_leaves_non_reply_messages_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        reply_module = _load_reply_module()
        reply_module.install_feishu_reply_quote_api_patch()
        adapter = FakeFeishuAdapter()
        message = SimpleNamespace(parent_id=None, upper_message_id=None)
        sender_id = SimpleNamespace(open_id="ou_replying_user")

        def _should_not_run(message_id: str, replying_open_id: str) -> Any:
            raise AssertionError("reply fetch should not run for non-reply messages")

        monkeypatch.setattr(reply_module, "_fetch_parent_message_blocking", _should_not_run)

        await adapter._process_inbound_message(
            data={},
            message=message,
            sender_id=sender_id,
            chat_type="group",
            message_id="om_reply_5",
        )

        assert adapter.last_reply_to_message_id is None
        assert adapter.last_reply_to_text is None
        assert adapter.fetch_calls == []
    finally:
        _restore(saved)


@pytest.mark.asyncio
async def test_reply_quote_patch_install_and_enrichment_are_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        reply_module = _load_reply_module()
        reply_module.install_feishu_reply_quote_api_patch()
        wrapped_process = FakeFeishuAdapter._process_inbound_message
        wrapped_fetch = FakeFeishuAdapter._fetch_message_text
        wrapped_dispatch = FakeFeishuAdapter._dispatch_inbound_event
        reply_module.install_feishu_reply_quote_api_patch()

        assert FakeFeishuAdapter._process_inbound_message is wrapped_process
        assert FakeFeishuAdapter._fetch_message_text is wrapped_fetch
        assert FakeFeishuAdapter._dispatch_inbound_event is wrapped_dispatch

        monkeypatch.setattr(
            reply_module,
            "_fetch_parent_message_blocking",
            lambda message_id, replying_open_id: {
                "message_id": message_id,
                "msg_type": "text",
                "body": {"content": json.dumps({"text": "ignored"}, ensure_ascii=False)},
                "sender": {"id": "ou_parent_author"},
            },
        )
        monkeypatch.setattr(
            reply_module,
            "_resolve_names_blocking",
            lambda open_ids, replying_open_id: {"ou_parent_author": "Alice"},
        )
        monkeypatch.setattr(
            reply_module,
            "_item_text",
            lambda item: "[message_id=om_parent_3] Alice: 完整引用正文",
        )

        adapter = FakeFeishuAdapter()
        message = SimpleNamespace(parent_id="om_parent_3", upper_message_id=None)
        sender_id = SimpleNamespace(open_id="ou_replying_user")

        await adapter._process_inbound_message(
            data={},
            message=message,
            sender_id=sender_id,
            chat_type="group",
            message_id="om_reply_6",
        )
        assert adapter.last_reply_to_text == "[message_id=om_parent_3] Alice: 完整引用正文"

        await adapter._process_inbound_message(
            data={},
            message=message,
            sender_id=sender_id,
            chat_type="group",
            message_id="om_reply_7",
        )
        assert adapter.last_reply_to_text == "[message_id=om_parent_3] Alice: 完整引用正文"
    finally:
        _restore(saved)


def test_wiring_adds_reply_quote_patch_next_to_merge_forward_install() -> None:
    source = Path("hermes_multitenancy/cron_worker.py").read_text()
    assert "install_feishu_merge_forward_api_patch()" in source
    assert "install_feishu_reply_quote_api_patch()" in source
