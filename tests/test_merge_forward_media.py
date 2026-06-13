from __future__ import annotations

import importlib
import json
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest


def _install_fake_feishu() -> tuple[type[Any], tuple[dict[str, Any], Any]]:
    class FakeFeishuAdapter:
        def __init__(self, original_result: tuple[Any, ...] | None = None) -> None:
            self.original_result = original_result or (
                "placeholder",
                "text",
                ["/core/existing.txt"],
                ["text/plain"],
                ["@alice"],
            )
            self.downloaded_images: list[tuple[str, str]] = []
            self.downloaded_files: list[tuple[str, str, str, str]] = []
            self.fail_image_keys: set[str] = set()
            self.fail_file_keys: set[str] = set()

        async def _extract_message_content(self, message: Any, *_args: Any, **_kwargs: Any) -> tuple[Any, ...]:
            return self.original_result

        async def _download_feishu_image(self, *, message_id: str, image_key: str) -> tuple[str, str]:
            self.downloaded_images.append((message_id, image_key))
            if image_key in self.fail_image_keys:
                raise RuntimeError(f"download failed for {image_key}")
            return f"/tmp/{message_id}-{image_key}.jpg", "image/jpeg"

        async def _download_feishu_message_resource(
            self,
            *,
            message_id: str,
            file_key: str,
            resource_type: str,
            fallback_filename: str,
        ) -> tuple[str, str]:
            self.downloaded_files.append((message_id, file_key, resource_type, fallback_filename))
            if file_key in self.fail_file_keys:
                raise RuntimeError(f"download failed for {file_key}")
            return f"/tmp/{message_id}-{file_key}", "application/pdf"

    def normalize_feishu_message(*, message_type: str, raw_content: str, **_kwargs: Any) -> Any:
        try:
            payload = json.loads(raw_content) if raw_content else {}
        except Exception:
            payload = {}
        text = ""
        if message_type == "image":
            text = "[image]"
        elif message_type == "file":
            text = "[file]"
        elif isinstance(payload, dict):
            for key in ("text", "content", "title"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    text = value.strip()
                    break
        image_keys: list[str] = []
        if isinstance(payload, dict) and isinstance(payload.get("image_key"), str):
            image_keys.append(payload["image_key"])
        media_refs: list[Any] = []
        if isinstance(payload, dict) and isinstance(payload.get("file_key"), str):
            media_refs.append(
                SimpleNamespace(
                    file_key=payload["file_key"],
                    resource_type=str(payload.get("resource_type") or "file"),
                    file_name=str(payload.get("file_name") or ""),
                )
            )
        return SimpleNamespace(text_content=text, image_keys=image_keys, media_refs=media_refs, metadata={})

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


def _load_modules() -> tuple[Any, Any]:
    sys.modules.pop("hermes_multitenancy.feishu_reply_quote_api", None)
    sys.modules.pop("hermes_multitenancy.feishu_merge_forward_api", None)
    merge_module = importlib.import_module("hermes_multitenancy.feishu_merge_forward_api")
    merge_module = importlib.reload(merge_module)
    reply_module = importlib.import_module("hermes_multitenancy.feishu_reply_quote_api")
    reply_module = importlib.reload(reply_module)
    return merge_module, reply_module


def _text_item(*, message_id: str, upper_message_id: str, sender_id: str, text: str, create_time: int = 1) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "upper_message_id": upper_message_id,
        "create_time": str(create_time),
        "msg_type": "text",
        "body": {"content": json.dumps({"text": text}, ensure_ascii=False)},
        "sender": {"id": sender_id},
    }


def _image_item(
    *,
    message_id: str,
    upper_message_id: str,
    sender_id: str,
    image_key: str,
    create_time: int = 1,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "upper_message_id": upper_message_id,
        "create_time": str(create_time),
        "msg_type": "image",
        "body": {"content": json.dumps({"image_key": image_key}, ensure_ascii=False)},
        "sender": {"id": sender_id},
    }


def _file_item(
    *,
    message_id: str,
    upper_message_id: str,
    sender_id: str,
    file_key: str,
    file_name: str,
    create_time: int = 1,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "upper_message_id": upper_message_id,
        "create_time": str(create_time),
        "msg_type": "file",
        "body": {
            "content": json.dumps(
                {
                    "file_key": file_key,
                    "file_name": file_name,
                    "resource_type": "file",
                },
                ensure_ascii=False,
            )
        },
        "sender": {"id": sender_id},
    }


async def test_expand_merge_forward_with_media_uses_sub_item_message_id_for_image_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        merge_module, _reply_module = _load_modules()
        adapter = FakeFeishuAdapter()
        items = [
            _image_item(
                message_id="om_sub_1",
                upper_message_id="om_container",
                sender_id="ou_sender_1",
                image_key="img_1",
            )
        ]
        monkeypatch.setattr(merge_module, "_fetch_sub_messages_blocking", lambda _mid, _oid: items)
        monkeypatch.setattr(merge_module, "_resolve_names_blocking", lambda _ids, _oid: {"ou_sender_1": "Alice"})

        transcript, media = await merge_module._expand_merge_forward_with_media(
            adapter,
            "om_container",
            "ou_forwarder",
        )

        assert adapter.downloaded_images == [("om_sub_1", "img_1")]
        assert media == [("/tmp/om_sub_1-img_1.jpg", "image/jpeg")]
        assert transcript is not None
        assert "[图片]" in transcript
        assert "[image]" not in transcript
    finally:
        _restore(saved)


async def test_merge_forward_patch_appends_downloaded_media_and_file_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        merge_module, _reply_module = _load_modules()
        merge_module.install_feishu_merge_forward_api_patch()
        adapter = FakeFeishuAdapter(
            original_result=("placeholder", "merge", ["/core/keep.txt"], ["text/plain"], ["@core"])
        )
        merge_module._sender_map(adapter)["om_container"] = "ou_forwarder"
        monkeypatch.setattr(
            merge_module,
            "_fetch_sub_messages_blocking",
            lambda _mid, _oid: [
                _file_item(
                    message_id="om_file_1",
                    upper_message_id="om_container",
                    sender_id="ou_sender_1",
                    file_key="file_1",
                    file_name="report.pdf",
                )
            ],
        )
        monkeypatch.setattr(merge_module, "_resolve_names_blocking", lambda _ids, _oid: {"ou_sender_1": "Alice"})

        result = await adapter._extract_message_content(
            SimpleNamespace(message_type="merge_forward", message_id="om_container")
        )

        assert result[0] is not None
        assert "[文件:report.pdf]" in result[0]
        assert result[1] == "merge"
        assert result[2] == ["/core/keep.txt", "/tmp/om_file_1-file_1"]
        assert result[3] == ["text/plain", "application/pdf"]
        assert result[4] == ["@core"]
    finally:
        _restore(saved)


async def test_expand_merge_forward_with_media_continues_when_one_resource_download_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        merge_module, _reply_module = _load_modules()
        adapter = FakeFeishuAdapter()
        adapter.fail_image_keys.add("img_bad")
        items = [
            _image_item(
                message_id="om_bad",
                upper_message_id="om_container",
                sender_id="ou_sender_1",
                image_key="img_bad",
                create_time=1,
            ),
            _image_item(
                message_id="om_good",
                upper_message_id="om_container",
                sender_id="ou_sender_2",
                image_key="img_good",
                create_time=2,
            ),
        ]
        monkeypatch.setattr(merge_module, "_fetch_sub_messages_blocking", lambda _mid, _oid: items)
        monkeypatch.setattr(
            merge_module,
            "_resolve_names_blocking",
            lambda _ids, _oid: {"ou_sender_1": "Alice", "ou_sender_2": "Bob"},
        )

        transcript, media = await merge_module._expand_merge_forward_with_media(
            adapter,
            "om_container",
            "ou_forwarder",
        )

        assert transcript is not None
        assert "[资源已过期(14005)，请直接发送]" in transcript
        assert "[图片]" in transcript
        assert media == [("/tmp/om_good-img_good.jpg", "image/jpeg")]
    finally:
        _restore(saved)


@pytest.mark.parametrize(
    ("error_message", "should_hint"),
    [
        ("im/v1/messages code=14005 msg=Resource Has Been Deleted", True),
        ("im/v1/messages code=230002 msg=permission denied", True),
        ("network down", False),
    ],
)
async def test_merge_forward_container_fetch_error_handling(
    monkeypatch: pytest.MonkeyPatch,
    error_message: str,
    should_hint: bool,
) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        merge_module, _reply_module = _load_modules()
        merge_module.install_feishu_merge_forward_api_patch()
        adapter = FakeFeishuAdapter()
        original_result = adapter.original_result
        merge_module._sender_map(adapter)["om_container"] = "ou_forwarder"
        monkeypatch.setattr(
            merge_module,
            "_fetch_sub_messages_blocking",
            lambda _mid, _oid: (_ for _ in ()).throw(RuntimeError(error_message)),
        )

        wrapped = await adapter._extract_message_content(
            SimpleNamespace(message_type="merge_forward", message_id="om_container")
        )

        if should_hint:
            transcript, media = await merge_module._expand_merge_forward_with_media(
                adapter,
                "om_container",
                "ou_forwarder",
            )
            assert transcript is not None
            assert "[转发内容含已过期资源(14005)，请直接发送]" in transcript
            assert media == []
            assert wrapped[0] == transcript
        else:
            with pytest.raises(RuntimeError, match="network down"):
                await merge_module._expand_merge_forward_with_media(
                    adapter,
                    "om_container",
                    "ou_forwarder",
                )
            assert wrapped is original_result
    finally:
        _restore(saved)


async def test_expand_merge_forward_with_media_caps_total_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        merge_module, reply_module = _load_modules()
        adapter = FakeFeishuAdapter()
        items = [
            _image_item(
                message_id=f"om_sub_{idx}",
                upper_message_id="om_container",
                sender_id=f"ou_sender_{idx}",
                image_key=f"img_{idx}",
                create_time=idx,
            )
            for idx in range(1, reply_module._QUOTE_MEDIA_MAX + 4)
        ]
        monkeypatch.setattr(merge_module, "_fetch_sub_messages_blocking", lambda _mid, _oid: items)
        monkeypatch.setattr(merge_module, "_resolve_names_blocking", lambda _ids, _oid: {})

        _transcript, media = await merge_module._expand_merge_forward_with_media(
            adapter,
            "om_container",
            "ou_forwarder",
        )

        assert len(media) == reply_module._QUOTE_MEDIA_MAX
    finally:
        _restore(saved)


async def test_merge_forward_text_only_matches_existing_render_and_non_merge_forward_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeFeishuAdapter, saved = _install_fake_feishu()
    try:
        merge_module, _reply_module = _load_modules()
        nested_items = [
            _text_item(
                message_id="om_parent",
                upper_message_id="om_container",
                sender_id="ou_sender_1",
                text="parent text",
                create_time=1,
            ),
            _text_item(
                message_id="om_child",
                upper_message_id="om_parent",
                sender_id="ou_sender_2",
                text="child text",
                create_time=2,
            ),
        ]
        monkeypatch.setattr(merge_module, "_fetch_sub_messages_blocking", lambda _mid, _oid: nested_items)
        monkeypatch.setattr(
            merge_module,
            "_resolve_names_blocking",
            lambda _ids, _oid: {"ou_sender_1": "Alice", "ou_sender_2": "Bob"},
        )

        expected = merge_module._expand_merge_forward_blocking("om_container", "ou_forwarder")
        adapter = FakeFeishuAdapter()
        transcript, media = await merge_module._expand_merge_forward_with_media(
            adapter,
            "om_container",
            "ou_forwarder",
        )

        assert transcript == expected
        assert media == []
        assert "[图片]" not in (transcript or "")
        assert "[文件:" not in (transcript or "")

        merge_module.install_feishu_merge_forward_api_patch()
        untouched = await adapter._extract_message_content(
            SimpleNamespace(message_type="text", message_id="om_container")
        )
        assert untouched is adapter.original_result
    finally:
        _restore(saved)
