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
import urllib.parse

import pytest


def _install_fake_feishu():
    class FakeFeishuAdapter:
        def __init__(self) -> None:
            self._message_text_cache: dict[str, str] = {}
            self._pending_text_batches: dict[str, Any] = {}
            self._pending_text_batch_counts: dict[str, int] = {}
            self.fetch_calls: list[str] = []
            self.downloaded_images: list[tuple[str, str]] = []
            self.scheduled_batch_keys: list[str] = []
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

        def _text_batch_key(self, event: Any) -> str:
            return str(getattr(event, "batch_key", "") or "default")

        @staticmethod
        def _text_batch_is_compatible(existing: Any, incoming: Any) -> bool:
            return (
                existing.reply_to_message_id == incoming.reply_to_message_id
                and existing.reply_to_text == incoming.reply_to_text
                and existing.source.thread_id == incoming.source.thread_id
            )

        def _schedule_text_batch_flush(self, key: str) -> None:
            self.scheduled_batch_keys.append(key)

        async def _enqueue_text_event(self, event: Any) -> None:
            key = self._text_batch_key(event)
            existing = self._pending_text_batches.get(key)
            if existing is None:
                self._pending_text_batches[key] = event
                self._pending_text_batch_counts[key] = 1
                self._schedule_text_batch_flush(key)
                return
            if not self._text_batch_is_compatible(existing, event):
                self._pending_text_batches[key] = event
                self._pending_text_batch_counts[key] = 1
                self._schedule_text_batch_flush(key)
                return
            appended_text = event.text or ""
            existing.text = (
                f"{existing.text}\n{appended_text}"
                if existing.text and appended_text
                else (existing.text or appended_text)
            )
            existing.timestamp = event.timestamp
            if event.message_id:
                existing.message_id = event.message_id
            self._pending_text_batch_counts[key] = self._pending_text_batch_counts.get(key, 1) + 1
            self._schedule_text_batch_flush(key)

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


def test_parent_message_fetch_requests_raw_card_content(monkeypatch: pytest.MonkeyPatch) -> None:
    reply_module = _load_reply_module()
    requested_urls: list[str] = []

    class _Response:
        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"code":0,"data":{"items":[{"msg_type":"interactive"}]}}'

    def _urlopen(request: Any, timeout: int) -> _Response:
        assert timeout == 10
        requested_urls.append(request.full_url)
        return _Response()

    monkeypatch.setattr(reply_module, "_resolve_uat_token", lambda _open_id: "uat")
    monkeypatch.setattr(reply_module.urllib.request, "urlopen", _urlopen)

    item = reply_module._fetch_parent_message_blocking("om parent", "ou_replying_user")

    assert item == {"msg_type": "interactive"}
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(requested_urls[0]).query)
    assert query == {
        "user_id_type": ["open_id"],
        "card_msg_content_type": ["raw_card_content"],
    }


@pytest.mark.asyncio
async def test_reply_quote_skips_interactive_preview_image_download() -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        reply_module = _load_reply_module()
        adapter = FakeFeishuAdapter()
        item = {
            "msg_type": "interactive",
            "body": {
                "content": json.dumps(
                    {
                        "json_card": "{}",
                        "image_key": "img_v3_cardkit_preview",
                    },
                    ensure_ascii=False,
                )
            },
        }

        assert await reply_module._download_reply_quote_media(adapter, "om_parent", item) == []
        assert adapter.downloaded_images == []
    finally:
        _restore(saved)


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["p2p", "group"])
async def test_reply_quote_interactive_raw_card_reaches_final_context(
    monkeypatch: pytest.MonkeyPatch,
    chat_type: str,
) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        from hermes_multitenancy.feishu_inbound_richtext import _extract_interactive_card_text

        feishu_module = sys.modules["gateway.platforms.feishu"]
        base_normalize = feishu_module.normalize_feishu_message

        def _rich_normalize(*, message_type: str, raw_content: str, **kwargs: Any) -> Any:
            result = base_normalize(message_type=message_type, raw_content=raw_content, **kwargs)
            if message_type == "interactive":
                result.text_content = _extract_interactive_card_text(json.loads(raw_content))
            return result

        feishu_module.normalize_feishu_message = _rich_normalize
        reply_module = _load_reply_module()
        reply_module.install_feishu_reply_quote_api_patch()
        adapter = FakeFeishuAdapter()
        json_card = json.dumps(
            {
                "header": {
                    "property": {
                        "title": {
                            "tag": "plain_text",
                            "property": {"content": "⏰ 付款单日报"},
                        }
                    }
                },
                "body": {
                    "property": {
                        "elements": [
                            {
                                "tag": "markdown",
                                "id": "element-secret-noise",
                                "property": {"content": "Token 已过期"},
                            }
                        ]
                    }
                },
            },
            ensure_ascii=False,
        )
        monkeypatch.setattr(
            reply_module,
            "_fetch_parent_message_blocking",
            lambda message_id, _open_id: {
                "message_id": message_id,
                "msg_type": "interactive",
                "body": {
                    "content": json.dumps(
                        {"json_card": json_card, "image_key": "img_v3_cardkit_preview"},
                        ensure_ascii=False,
                    )
                },
                "sender": {"id": "ou_parent_author"},
            },
        )
        monkeypatch.setattr(
            reply_module,
            "_resolve_names_blocking",
            lambda _open_ids, _replying_open_id: {"ou_parent_author": "Hermes"},
        )

        await adapter._process_inbound_message(
            data={},
            message=SimpleNamespace(parent_id="om_parent_card", upper_message_id=None),
            sender_id=SimpleNamespace(open_id="ou_replying_user"),
            chat_type=chat_type,
            message_id="om_reply_card",
        )

        assert adapter.last_reply_to_text == (
            "[message_id=om_parent_card] Hermes: 主题: ⏰ 付款单日报\n正文: Token 已过期"
        )
        assert all(
            noise not in adapter.last_reply_to_text
            for noise in ("json_card", "element-secret-noise", "img_v3_cardkit_preview")
        )
        assert adapter.downloaded_images == []
    finally:
        _restore(saved)


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
async def test_reply_quote_patch_preserves_quoted_media_when_text_batch_merges() -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        reply_module = _load_reply_module()
        reply_module.install_feishu_reply_quote_api_patch()
        adapter = FakeFeishuAdapter()
        source = SimpleNamespace(thread_id=None)
        first = SimpleNamespace(
            batch_key="chat",
            text="first",
            media_urls=[],
            media_types=[],
            reply_to_message_id="om_parent_batch",
            reply_to_text="[message_id=om_parent_batch] Bob: [image]",
            source=source,
            timestamp=1,
            message_id="om_first",
        )
        second = SimpleNamespace(
            batch_key="chat",
            text="second",
            media_urls=["/tmp/om_parent_batch-img_1.jpg"],
            media_types=["image/jpeg"],
            reply_to_message_id="om_parent_batch",
            reply_to_text="[message_id=om_parent_batch] Bob: [image]",
            source=source,
            timestamp=2,
            message_id="om_second",
        )

        await adapter._enqueue_text_event(first)
        await adapter._enqueue_text_event(second)

        batched = adapter._pending_text_batches["chat"]
        assert batched is first
        assert batched.text == "first\nsecond"
        assert batched.message_id == "om_second"
        assert batched.media_urls == ["/tmp/om_parent_batch-img_1.jpg"]
        assert batched.media_types == ["image/jpeg"]
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
async def test_reply_quote_failed_raw_fetch_hides_card_preview_key(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        async def _preview_fallback(self: Any, message_id: str | None) -> str | None:
            del self, message_id
            return "img img_v3_02ad_secret_preview 请升级至最新版本客户端，以查看内容"

        FakeFeishuAdapter._fetch_message_text = _preview_fallback
        reply_module = _load_reply_module()
        reply_module.install_feishu_reply_quote_api_patch()
        monkeypatch.setattr(
            reply_module,
            "_fetch_parent_message_blocking",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("no uat")),
        )
        adapter = FakeFeishuAdapter()

        await adapter._process_inbound_message(
            data={},
            message=SimpleNamespace(parent_id="om_parent_preview", upper_message_id=None),
            sender_id=SimpleNamespace(open_id="ou_replying_user"),
            chat_type="group",
            message_id="om_reply_preview",
        )

        assert adapter.last_reply_to_text == (
            "[message_id=om_parent_preview] [interactive 消息内容暂不可用]"
        )
        assert "img_v3_02ad_secret_preview" not in adapter.last_reply_to_text
    finally:
        _restore(saved)


def test_reply_quote_preview_key_is_redacted_even_without_upgrade_hint() -> None:
    reply_module = _load_reply_module()

    sanitized = reply_module._sanitize_degraded_preview(
        "om_parent_preview",
        "img img_v3_02ad_secret_preview",
    )

    assert sanitized == "img [card-preview]"
    assert "img_v3_02ad_secret_preview" not in sanitized


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
        wrapped_enqueue = FakeFeishuAdapter._enqueue_text_event
        reply_module.install_feishu_reply_quote_api_patch()

        assert FakeFeishuAdapter._process_inbound_message is wrapped_process
        assert FakeFeishuAdapter._fetch_message_text is wrapped_fetch
        assert FakeFeishuAdapter._dispatch_inbound_event is wrapped_dispatch
        assert FakeFeishuAdapter._enqueue_text_event is wrapped_enqueue

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
