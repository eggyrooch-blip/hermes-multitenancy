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

    def _require_mention_for(self, chat_id: str) -> bool:
        return True

    def _should_accept_group_message(self, message: Any, sender_id: str, chat_id: str) -> bool:
        return False

    def _allow_group_message(self, sender_id: str, chat_id: str) -> bool:
        return True

    def _admit(self, sender: Any, message: Any) -> str:
        return "ok"

    def _on_card_action_trigger(self, data: Any) -> None:
        return None

    def _on_bot_added_to_chat(self, data: Any) -> None:
        return None

    def _build_event_handler(self) -> Any:
        return types.SimpleNamespace(_processorMap={})

    async def on_processing_complete(self, event: Any, outcome: Any) -> None:
        return None

    async def _process_inbound_message(self, *args: Any, **kwargs: Any) -> str:
        return "processed"

    async def _extract_message_content(self, message: Any, *args: Any, **kwargs: Any) -> tuple[str, str, list, list]:
        return "text", "text", [], []

    async def _fetch_message_text(self, message_id: str, *args: Any, **kwargs: Any) -> str:
        return "parent"

    async def _dispatch_inbound_event(self, event: Any, *args: Any, **kwargs: Any) -> None:
        return None

    async def _enqueue_text_event(self, event: Any, *args: Any, **kwargs: Any) -> None:
        return None


def _install_plugin_feishu_module(monkeypatch) -> types.ModuleType:
    for name in (
        "gateway.platforms.feishu",
        "plugins.platforms.feishu.adapter",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)

    fake_module = types.ModuleType("plugins.platforms.feishu.adapter")
    fake_module.FeishuAdapter = type("PluginFeishuAdapter", (FakeFeishuAdapter,), {})  # type: ignore[attr-defined]

    def normalize_feishu_message(**kwargs: Any) -> FakeNormalizedMessage:
        return FakeNormalizedMessage(raw_type=kwargs.get("message_type") or "")

    fake_module.normalize_feishu_message = normalize_feishu_message  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "plugins.platforms.feishu.adapter", fake_module)

    real_import_module = feishu_adapter_compat.import_module

    def import_module(name: str) -> types.ModuleType:
        if name == "gateway.platforms.feishu":
            raise ModuleNotFoundError(f"No module named '{name}'", name="gateway")
        if name == "plugins.platforms.feishu.adapter":
            return fake_module
        return real_import_module(name)

    monkeypatch.setattr(feishu_adapter_compat, "import_module", import_module)
    return fake_module


def test_feishu_module_prefers_legacy_gateway_adapter_layout(monkeypatch) -> None:
    legacy_module = types.ModuleType("gateway.platforms.feishu")
    plugin_module = types.ModuleType("plugins.platforms.feishu.adapter")
    seen: list[str] = []

    def import_module(name: str) -> types.ModuleType:
        seen.append(name)
        if name == "gateway.platforms.feishu":
            return legacy_module
        if name == "plugins.platforms.feishu.adapter":
            return plugin_module
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(feishu_adapter_compat, "import_module", import_module)

    assert feishu_adapter_compat.load_feishu_module() is legacy_module
    assert seen == ["gateway.platforms.feishu"]


def test_feishu_module_does_not_hide_legacy_import_errors(monkeypatch) -> None:
    plugin_module = types.ModuleType("plugins.platforms.feishu.adapter")
    seen: list[str] = []

    def import_module(name: str) -> types.ModuleType:
        seen.append(name)
        if name == "gateway.platforms.feishu":
            raise ModuleNotFoundError("No module named 'legacy_dependency'", name="legacy_dependency")
        if name == "plugins.platforms.feishu.adapter":
            return plugin_module
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(feishu_adapter_compat, "import_module", import_module)

    try:
        feishu_adapter_compat.load_feishu_module()
    except ModuleNotFoundError as exc:
        assert exc.name == "legacy_dependency"
    else:
        raise AssertionError("expected legacy import error to be raised")
    assert seen == ["gateway.platforms.feishu"]


def test_feishu_module_falls_back_on_bare_candidate_module_errors(monkeypatch) -> None:
    plugin_module = types.ModuleType("plugins.platforms.feishu.adapter")
    seen: list[str] = []

    def import_module(name: str) -> types.ModuleType:
        seen.append(name)
        if name == "gateway.platforms.feishu":
            raise ModuleNotFoundError(name)
        if name == "plugins.platforms.feishu.adapter":
            return plugin_module
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(feishu_adapter_compat, "import_module", import_module)

    assert feishu_adapter_compat.load_feishu_module() is plugin_module
    assert seen == ["gateway.platforms.feishu", "plugins.platforms.feishu.adapter"]


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
    fake_module = _install_plugin_feishu_module(monkeypatch)
    adapter_cls = fake_module.FeishuAdapter

    cron_worker._patch_feishu_open_id_send()
    cron_worker._patch_feishu_outbound_link_render()

    assert getattr(adapter_cls._send_raw_message, "_hermes_multitenancy_patched", False)
    assert getattr(adapter_cls._build_outbound_payload, "_hermes_multitenancy_patched", False)


def test_feishu_patch_installers_use_plugin_adapter_layout(monkeypatch) -> None:
    fake_module = _install_plugin_feishu_module(monkeypatch)
    adapter_cls = fake_module.FeishuAdapter

    from hermes_multitenancy import feishu_group_valve
    from hermes_multitenancy import feishu_helpdesk_events
    from hermes_multitenancy import feishu_merge_forward_api
    from hermes_multitenancy import feishu_reaction_lifecycle
    from hermes_multitenancy import feishu_reply_quote_api
    from hermes_multitenancy import group_inviter_hook

    feishu_group_valve._HOOK_INSTALLED = False
    feishu_merge_forward_api._HOOK_INSTALLED = False
    feishu_reaction_lifecycle._HOOK_INSTALLED = False
    feishu_reply_quote_api._HOOK_INSTALLED = False
    group_inviter_hook._HOOK_INSTALLED = False

    feishu_group_valve.install_feishu_group_valve_patch()
    feishu_helpdesk_events.install_feishu_helpdesk_events_patch()
    feishu_merge_forward_api.install_feishu_merge_forward_api_patch()
    feishu_reaction_lifecycle.install_feishu_reaction_lifecycle_patch()
    feishu_reply_quote_api.install_feishu_reply_quote_api_patch()
    group_inviter_hook.install_feishu_bot_added_hook()

    assert getattr(adapter_cls._require_mention_for, "_hermes_multitenancy_group_valve_require_mention_patched", False)
    assert getattr(adapter_cls._build_event_handler, "_hermes_mt_helpdesk_events_patched", False)
    assert getattr(adapter_cls._extract_message_content, "_hermes_multitenancy_merge_forward_api_patched", False)
    assert getattr(adapter_cls.on_processing_complete, "_hermes_multitenancy_reaction_lifecycle_patched", False)
    assert getattr(adapter_cls._fetch_message_text, "_hermes_multitenancy_reply_quote_fetch_patched", False)
    assert getattr(adapter_cls._on_card_action_trigger, "_hermes_multitenancy_group_valve_card_action_patched", False)
    assert getattr(adapter_cls._on_bot_added_to_chat, "_hermes_multitenancy_bot_added_class_patched", False)
