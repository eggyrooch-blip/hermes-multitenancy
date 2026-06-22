from __future__ import annotations

from dataclasses import dataclass, field
import json
import sys
import types
from typing import Any

from hermes_multitenancy import cron_worker
from hermes_multitenancy import feishu_adapter_compat
from hermes_multitenancy.feishu_inbound_richtext import install_feishu_inbound_richtext_patch


@dataclass
class FakeNormalizedMessage:
    raw_type: str = ""
    text_content: str = ""
    image_keys: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class FakeFeishuAdapter:
    async def _send_raw_message(
        self,
        *,
        chat_id: str,
        msg_type: str,
        payload: str,
        reply_to: str | None,
        metadata: dict[str, Any] | None,
    ) -> str:
        return f"old:{chat_id}:{msg_type}:{payload}:{reply_to}:{metadata}"

    def _build_outbound_payload(self, content: str) -> tuple[str, str]:
        return "text", json.dumps({"text": content}, ensure_ascii=False)


def _install_plugin_feishu_module(monkeypatch) -> types.ModuleType:
    for name in (
        "gateway.platforms.feishu",
        "plugins.platforms.feishu.adapter",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)

    fake_module = types.ModuleType("plugins.platforms.feishu.adapter")
    fake_module.FeishuAdapter = FakeFeishuAdapter  # type: ignore[attr-defined]

    def normalize_feishu_message(**kwargs: Any) -> FakeNormalizedMessage:
        return FakeNormalizedMessage(raw_type=kwargs.get("message_type") or "")

    fake_module.normalize_feishu_message = normalize_feishu_message  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "plugins.platforms.feishu.adapter", fake_module)

    real_import_module = feishu_adapter_compat.import_module

    def import_module(name: str) -> types.ModuleType:
        if name == "gateway.platforms.feishu":
            raise ModuleNotFoundError(name)
        if name == "plugins.platforms.feishu.adapter":
            return fake_module
        return real_import_module(name)

    monkeypatch.setattr(feishu_adapter_compat, "import_module", import_module)
    return fake_module


def test_inbound_richtext_patch_installs_with_plugin_adapter_layout(monkeypatch) -> None:
    fake_module = _install_plugin_feishu_module(monkeypatch)

    install_feishu_inbound_richtext_patch()

    assert getattr(fake_module.normalize_feishu_message, "_hermes_multitenancy_inbound_patched", False)
    result = fake_module.normalize_feishu_message(
        message_type="email",
        raw_content=json.dumps({"subject": "告警", "body": "支付失败"}, ensure_ascii=False),
    )
    assert "告警" in result.text_content
    assert "支付失败" in result.text_content


def test_cron_feishu_patches_install_with_plugin_adapter_layout(monkeypatch) -> None:
    _install_plugin_feishu_module(monkeypatch)

    cron_worker._patch_feishu_open_id_send()
    cron_worker._patch_feishu_outbound_link_render()

    assert getattr(FakeFeishuAdapter._send_raw_message, "_hermes_multitenancy_patched", False)
    assert getattr(FakeFeishuAdapter._build_outbound_payload, "_hermes_multitenancy_patched", False)
