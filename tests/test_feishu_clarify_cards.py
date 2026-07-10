from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace


class _CardAdapter:
    def __init__(self) -> None:
        self.original_calls: list[object] = []

    def _on_card_action_trigger(self, data):
        self.original_calls.append(data)
        return "ORIGINAL_CALLED"


def _card_data(*, action_value, answer="", choice=""):
    return SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value=action_value,
                form_value={"clarify_answer": answer, "clarify_choice": choice},
            )
        )
    )


def test_feishu_clarify_callback_card_submit_writes_webui_protocol(monkeypatch, tmp_path):
    """Callback → CardKit form → action writes the exact core response protocol."""
    from hermes_multitenancy.agent_real import _clarify_bridge_dir, _read_clarify_response
    from hermes_multitenancy.feishu_clarify_cards import (
        _configure_feishu_clarify_bridge,
        _patch_card_action,
        handle_feishu_clarify_required,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_CLARIFY_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_MULTITENANCY_CLARIFY_TIMEOUT", "0")
    sent: list[dict] = []

    async def fake_send(*, adapter, chat_id, card):
        assert adapter == "adapter"
        assert chat_id == "oc_demo"
        sent.append(card)
        return {"message_id": "om_demo"}

    monkeypatch.setattr("hermes_multitenancy.feishu_clarify_cards.send_auth_card", fake_send)
    _patch_card_action(_CardAdapter)

    def sink(event_name, **payload):
        assert event_name == "clarify_required"
        import asyncio

        asyncio.run(handle_feishu_clarify_required("adapter", "oc_demo", payload))
        action_value = sent[0]["body"]["elements"][-1]["elements"][-1]["value"]
        response = _CardAdapter()._on_card_action_trigger(
            _card_data(action_value=action_value, choice="brief")
        )
        assert response["toast"]["content"] == "已提交，正在继续。"

    answer = _configure_feishu_clarify_bridge(sink, "feishu-session")(
        "Which report style?", ["brief", "detailed"]
    )

    assert answer == "brief"
    assert sent[0]["schema"] == "2.0"
    assert sent[0]["body"]["elements"][0]["content"] == "Which report style?"
    assert sent[0]["body"]["elements"][-1]["tag"] == "form"
    response_files = list(_clarify_bridge_dir().glob("clarify_*.json"))
    assert len(response_files) == 1
    assert json.loads(response_files[0].read_text(encoding="utf-8")) == {"response": "brief"}
    assert _read_clarify_response(response_files[0]) == "brief"


def test_clarify_patch_passes_auth_and_unknown_actions_through_in_both_install_orders(monkeypatch):
    from hermes_multitenancy import feishu_auth_hub_actions
    from hermes_multitenancy.feishu_auth_hub_actions import _patch_card_action as patch_auth
    from hermes_multitenancy.feishu_clarify_cards import _patch_card_action as patch_clarify
    from hermes_multitenancy.feishu_group_valve import _patch_on_card_action_trigger as patch_group

    monkeypatch.setattr(
        feishu_auth_hub_actions,
        "_handle_cred_auth_action",
        lambda *_args: "AUTH_HUB_CALLED",
    )
    for patches in ((patch_group, patch_auth, patch_clarify), (patch_clarify, patch_group, patch_auth)):
        class Adapter(_CardAdapter):
            pass

        for patch in patches:
            patch(Adapter)
        adapter = Adapter()
        assert adapter._on_card_action_trigger(_card_data(action_value={"hermes_action": "third_party"})) == "ORIGINAL_CALLED"
        assert adapter._on_card_action_trigger(_card_data(action_value={"hermes_action": "cred_auth"})) == "AUTH_HUB_CALLED"
        assert len(adapter.original_calls) == 1
        assert getattr(Adapter._on_card_action_trigger, "_hermes_multitenancy_clarify_card_action_patched")
        assert getattr(Adapter._on_card_action_trigger, "_hermes_multitenancy_cred_auth_card_action_patched")


def test_clarify_install_uses_synthetic_adapter_module(monkeypatch):
    from hermes_multitenancy import feishu_clarify_cards

    synthetic = types.ModuleType("hermes_plugins.feishu_platform.adapter")
    synthetic.FeishuAdapter = type("SyntheticAdapter", (_CardAdapter,), {})
    monkeypatch.setitem(sys.modules, "hermes_plugins.feishu_platform.adapter", synthetic)
    monkeypatch.setattr(feishu_clarify_cards, "_HOOK_INSTALLED", False)

    feishu_clarify_cards.install_feishu_clarify_card_action_patch()

    assert getattr(
        synthetic.FeishuAdapter._on_card_action_trigger,
        "_hermes_multitenancy_clarify_card_action_patched",
        False,
    )


def test_feishu_clarify_timeout_reuses_core_response(monkeypatch, tmp_path):
    from hermes_multitenancy.agent_real import _clarify_timeout_response
    from hermes_multitenancy.feishu_clarify_cards import _configure_feishu_clarify_bridge

    monkeypatch.setenv("HERMES_MULTITENANCY_CLARIFY_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_MULTITENANCY_CLARIFY_TIMEOUT", "0")

    assert _configure_feishu_clarify_bridge(lambda *_args, **_kwargs: None, "s")("Which?") == _clarify_timeout_response(0)


def test_clarify_patch_error_delegates_to_original(monkeypatch):
    from hermes_multitenancy import feishu_clarify_cards

    _patch = feishu_clarify_cards._patch_card_action
    _patch(_CardAdapter)
    monkeypatch.setattr(feishu_clarify_cards, "_write_clarify_response", lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")))
    adapter = _CardAdapter()

    assert adapter._on_card_action_trigger(
        _card_data(
            action_value={"hermes_action": "clarify", "clarify_id": "clarify_0123456789abcdef0123456789abcdef"},
            answer="answer",
        )
    ) == "ORIGINAL_CALLED"
    assert len(adapter.original_calls) == 1


def test_clarify_invalid_submit_is_not_a_success_toast():
    from hermes_multitenancy.feishu_clarify_cards import _patch_card_action

    _patch_card_action(_CardAdapter)

    response = _CardAdapter()._on_card_action_trigger(
        _card_data(
            action_value={"hermes_action": "clarify", "clarify_id": "clarify_0123456789abcdef0123456789abcdef"},
        )
    )

    assert response["toast"]["type"] == "error"


def test_duplicate_clarify_submit_keeps_the_first_answer(monkeypatch, tmp_path):
    from hermes_multitenancy.agent_real import _clarify_bridge_dir, _read_clarify_response
    from hermes_multitenancy.feishu_clarify_cards import _patch_card_action

    monkeypatch.setenv("HERMES_MULTITENANCY_CLARIFY_DIR", str(tmp_path))
    _patch_card_action(_CardAdapter)
    action = {"hermes_action": "clarify", "clarify_id": "clarify_0123456789abcdef0123456789abcdef"}
    adapter = _CardAdapter()

    assert adapter._on_card_action_trigger(_card_data(action_value=action, answer="first"))["toast"]["type"] == "success"
    assert adapter._on_card_action_trigger(_card_data(action_value=action, answer="second"))["toast"]["type"] == "info"
    assert _read_clarify_response(_clarify_bridge_dir() / f"{action['clarify_id']}.json") == "first"
