from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from tests.conftest import card_toast


class _CardAdapter:
    def __init__(self) -> None:
        self.original_calls: list[object] = []

    def _on_card_action_trigger(self, data):
        self.original_calls.append(data)
        return "ORIGINAL_CALLED"


def _card_data(*, action_value, answer="", choice="", chat=""):
    event = SimpleNamespace(
        action=SimpleNamespace(
            value=action_value,
            form_value={"clarify_answer": answer, "clarify_choice": choice},
        )
    )
    if chat:
        event.context = SimpleNamespace(open_chat_id=chat)
    return SimpleNamespace(event=event)


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
    form = sent[0]["body"]["elements"][-1]
    assert form["tag"] == "form"
    assert form["name"] == "clarify_form"
    submit = form["elements"][-1]
    assert submit["behaviors"] == [{"type": "callback", "value": submit["value"]}]
    response_files = list(_clarify_bridge_dir().glob("clarify_*.json"))
    assert len(response_files) == 1
    assert json.loads(response_files[0].read_text(encoding="utf-8")) == {"response": "brief"}
    assert _read_clarify_response(response_files[0]) == "brief"


def test_dispatcher_routing_is_identical_in_both_install_orders(monkeypatch):
    """WP02: whichever retired installer runs first, ONE dispatcher is live and
    routing is identical — a truly unknown action is consumed as unsupported
    (never handed to the core generic ``/card`` path), an allowlisted Agent core
    action delegates exactly once, and cred_auth reaches its built-in."""
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
        unknown = adapter._on_card_action_trigger(_card_data(action_value={"hermes_action": "third_party"}))
        # The DISPATCHER's own unsupported answer — SDK-shaped when lark_oapi is
        # installed, a plain dict when it is not.
        assert card_toast(unknown)["type"] == "info"
        assert adapter.original_calls == []
        assert adapter._on_card_action_trigger(_card_data(action_value={"hermes_action": "cred_auth"})) == "AUTH_HUB_CALLED"
        assert adapter.original_calls == []
        assert adapter._on_card_action_trigger(_card_data(action_value={"hermes_action": "feishu_auth"})) == "ORIGINAL_CALLED"
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


def test_clarify_handler_failure_is_consumed_never_delegated(monkeypatch):
    """WP02 P0: a RECOGNIZED handler that raises returns a generic error and is
    CONSUMED. It must not fall back to the original — that path synthesizes a
    ``/card`` command out of the callback JSON and feeds it to the model."""
    from hermes_multitenancy import feishu_clarify_cards

    _patch = feishu_clarify_cards._patch_card_action
    _patch(_CardAdapter)
    monkeypatch.setattr(feishu_clarify_cards, "_write_clarify_response", lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")))
    adapter = _CardAdapter()

    response = adapter._on_card_action_trigger(
        _card_data(
            action_value={"hermes_action": "clarify", "clarify_id": "clarify_0123456789abcdef0123456789abcdef"},
            answer="answer",
        )
    )
    # The DISPATCHER's own error answer (the clarify handler never returned),
    # so this is the SDK-shaped response whenever lark_oapi is installed.
    assert card_toast(response)["type"] == "error"
    assert adapter.original_calls == []


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


def test_card_uses_text_input_for_non_list_choices():
    from hermes_multitenancy.feishu_clarify_cards import build_clarify_card

    card = build_clarify_card(
        clarify_id="clarify_0123456789abcdef0123456789abcdef",
        question="Which?",
        choices=42,
    )

    assert card["body"]["elements"][-1]["elements"][0]["tag"] == "input"


def test_stringified_action_value_is_accepted(monkeypatch, tmp_path):
    from hermes_multitenancy.agent_real import _clarify_bridge_dir, _read_clarify_response
    from hermes_multitenancy.feishu_clarify_cards import _patch_card_action

    monkeypatch.setenv("HERMES_MULTITENANCY_CLARIFY_DIR", str(tmp_path))
    _patch_card_action(_CardAdapter)
    clarify_id = "clarify_0123456789abcdef0123456789abcdef"
    response = _CardAdapter()._on_card_action_trigger(
        _card_data(action_value=json.dumps({"hermes_action": "clarify", "clarify_id": clarify_id}), answer="answer")
    )

    assert response["toast"]["type"] == "success"
    assert _read_clarify_response(_clarify_bridge_dir() / f"{clarify_id}.json") == "answer"


def test_install_retries_after_adapter_method_appears(monkeypatch):
    from hermes_multitenancy import feishu_clarify_cards

    synthetic = types.ModuleType("hermes_plugins.feishu_platform.adapter")

    class LateAdapter:
        pass

    synthetic.FeishuAdapter = LateAdapter
    monkeypatch.setitem(sys.modules, "hermes_plugins.feishu_platform.adapter", synthetic)
    monkeypatch.setattr(feishu_clarify_cards, "_HOOK_INSTALLED", False)
    feishu_clarify_cards.install_feishu_clarify_card_action_patch()
    assert feishu_clarify_cards._HOOK_INSTALLED is False

    LateAdapter._on_card_action_trigger = _CardAdapter._on_card_action_trigger
    feishu_clarify_cards.install_feishu_clarify_card_action_patch()
    assert getattr(LateAdapter._on_card_action_trigger, "_hermes_multitenancy_clarify_card_action_patched", False)


def test_undeliverable_clarify_card_unblocks_agent(monkeypatch, tmp_path):
    """FIX 1: a failed card send writes a fallback response so the agent isn't stranded."""
    import asyncio

    from hermes_multitenancy.agent_real import _clarify_bridge_dir, _read_clarify_response
    from hermes_multitenancy.feishu_clarify_cards import handle_feishu_clarify_required

    monkeypatch.setenv("HERMES_MULTITENANCY_CLARIFY_DIR", str(tmp_path))

    async def boom(*, adapter, chat_id, card):
        raise RuntimeError("delivery down")

    monkeypatch.setattr("hermes_multitenancy.feishu_clarify_cards.send_auth_card", boom)
    clarify_id = "clarify_" + "a" * 32
    payload = {"clarify_id": clarify_id, "question": "Which?", "choices": ["x", "y"]}

    # Must not raise — the stream consumer has to survive an undeliverable card.
    asyncio.run(handle_feishu_clarify_required("adapter", "oc_undeliverable", payload))

    response = _read_clarify_response(_clarify_bridge_dir() / f"{clarify_id}.json")
    assert response is not None
    assert "could not be delivered" in response
    assert "best judgement" in response


def test_clarify_card_and_answer_enforce_size_caps():
    """FIX 2: oversized question/choices/answer are truncated and choices capped at 10."""
    from hermes_multitenancy.feishu_clarify_cards import _clarify_answer, build_clarify_card

    card = build_clarify_card(
        clarify_id="clarify_" + "0" * 32,
        question="q" * 3000,
        choices=["y" * 200] + [f"c{i}" for i in range(20)],
    )
    question_el = card["body"]["elements"][0]
    assert question_el["tag"] == "markdown"
    assert len(question_el["content"]) == 2000

    form = card["body"]["elements"][-1]
    select = form["elements"][0]
    assert select["tag"] == "select_static"
    options = select["options"]
    assert len(options) == 10
    assert all(len(opt["text"]["content"]) <= 100 for opt in options)
    assert len(options[0]["text"]["content"]) == 100  # the "y" * 200 choice, truncated

    assert len(_clarify_answer({"clarify_answer": "z" * 3000})) == 2000


def test_stale_clarify_card_is_rejected_but_unknown_stays_fail_open(monkeypatch, tmp_path):
    """FIX 3: an older card for a re-clarified chat is rejected; unknown ids stay fail-open."""
    import asyncio

    from hermes_multitenancy import feishu_clarify_cards
    from hermes_multitenancy.agent_real import _clarify_bridge_dir, _read_clarify_response
    from hermes_multitenancy.feishu_clarify_cards import (
        _patch_card_action,
        handle_feishu_clarify_required,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_CLARIFY_DIR", str(tmp_path))
    feishu_clarify_cards._CLARIFY_CHAT_BY_ID.clear()
    feishu_clarify_cards._LATEST_CLARIFY_BY_CHAT.clear()

    async def ok(*, adapter, chat_id, card):
        return {"message_id": "om_x"}

    monkeypatch.setattr("hermes_multitenancy.feishu_clarify_cards.send_auth_card", ok)
    _patch_card_action(_CardAdapter)

    chat = "oc_restale"
    old_id = "clarify_" + "0" * 31 + "1"
    new_id = "clarify_" + "0" * 31 + "2"
    asyncio.run(handle_feishu_clarify_required("adapter", chat, {"clarify_id": old_id, "question": "Q1"}))
    asyncio.run(handle_feishu_clarify_required("adapter", chat, {"clarify_id": new_id, "question": "Q2"}))

    adapter = _CardAdapter()

    stale = adapter._on_card_action_trigger(
        _card_data(action_value={"hermes_action": "clarify", "clarify_id": old_id}, answer="late")
    )
    assert stale["toast"]["type"] == "error"
    assert stale["toast"]["content"] == "该卡片已过期，请在最新的卡片上回答。"
    assert not (_clarify_bridge_dir() / f"{old_id}.json").exists()

    fresh = adapter._on_card_action_trigger(
        _card_data(action_value={"hermes_action": "clarify", "clarify_id": new_id}, answer="onnew")
    )
    assert fresh["toast"]["type"] == "success"
    assert _read_clarify_response(_clarify_bridge_dir() / f"{new_id}.json") == "onnew"

    unknown_id = "clarify_" + "f" * 32
    unknown = adapter._on_card_action_trigger(
        _card_data(action_value={"hermes_action": "clarify", "clarify_id": unknown_id}, answer="anon")
    )
    assert unknown["toast"]["type"] == "success"
    assert _read_clarify_response(_clarify_bridge_dir() / f"{unknown_id}.json") == "anon"


def test_cross_chat_clarify_submit_is_rejected(monkeypatch, tmp_path):
    """grok review: a clarify answer must come from the chat the card was issued
    to — event.context.open_chat_id is Feishu-signed and can't be spoofed by a
    click. Same-chat and missing-context submits keep working (fail-open)."""
    import asyncio

    from hermes_multitenancy import feishu_clarify_cards
    from hermes_multitenancy.agent_real import _clarify_bridge_dir, _read_clarify_response
    from hermes_multitenancy.feishu_clarify_cards import (
        _patch_card_action,
        handle_feishu_clarify_required,
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_CLARIFY_DIR", str(tmp_path))
    feishu_clarify_cards._CLARIFY_CHAT_BY_ID.clear()
    feishu_clarify_cards._LATEST_CLARIFY_BY_CHAT.clear()

    async def ok(*, adapter, chat_id, card):
        return {"message_id": "om_x"}

    monkeypatch.setattr("hermes_multitenancy.feishu_clarify_cards.send_auth_card", ok)
    _patch_card_action(_CardAdapter)

    owner_chat = "oc_owner"
    cid = "clarify_" + "b" * 32
    asyncio.run(handle_feishu_clarify_required("adapter", owner_chat, {"clarify_id": cid, "question": "Q"}))

    adapter = _CardAdapter()
    foreign = adapter._on_card_action_trigger(
        _card_data(
            action_value={"hermes_action": "clarify", "clarify_id": cid},
            answer="inject",
            chat="oc_attacker",
        )
    )
    assert foreign["toast"]["type"] == "error"
    assert foreign["toast"]["content"] == "该卡片不属于当前会话。"
    assert not (_clarify_bridge_dir() / f"{cid}.json").exists()

    same = adapter._on_card_action_trigger(
        _card_data(
            action_value={"hermes_action": "clarify", "clarify_id": cid},
            answer="mine",
            chat=owner_chat,
        )
    )
    assert same["toast"]["type"] == "success"
    assert _read_clarify_response(_clarify_bridge_dir() / f"{cid}.json") == "mine"


def test_streaming_clarify_resolved_writes_terminal_card_without_leaking_payload(
    monkeypatch, tmp_path
):
    """Regression: answering a clarify card must retire it out of 「等待你的选择」.

    main only swallowed ``clarify_resolved`` so the payload dict could not reach
    ``piece = str(delta)``; the form card was left forever showing its pending
    state. Both halves are pinned here: exactly one terminal card is written, and
    the reply text is still clean.
    """
    import asyncio

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import feishu_clarify_cards
    from hermes_multitenancy import router as router_mod
    from tests.test_streaming_card_transport import _CardCapableAdapter

    clarify_id = "clarify_0123456789abcdef0123456789abcdef"
    adapter = _CardCapableAdapter()
    updated: list[dict] = []

    async def fake_stream(*_args, **_kwargs):
        yield "clarify_required", {
            "clarify_id": clarify_id,
            "question": "选择城市",
            "choices": ["北京", "上海"],
        }
        yield "clarify_resolved", {
            "clarify_id": clarify_id,
            "session_key": "multitenancy:feishu:feishu_x:oc_y:ou_z",
            "response": "北京",
            "timed_out": False,
        }
        yield "content", "北京明天多云"
        yield "done", "北京明天多云"

    async def fake_send(*, adapter, chat_id, card):
        return {"transport": "cardkit", "card_id": "cid_1", "message_id": "om_clarify", "sequence": 1}

    async def fake_update(*, adapter, auth_card, card):
        updated.append(card)
        return True

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)
    monkeypatch.setattr(feishu_clarify_cards, "send_auth_card", fake_send)
    monkeypatch.setattr(feishu_clarify_cards, "update_auth_card", fake_update)
    monkeypatch.setattr(feishu_clarify_cards, "_CLARIFY_CARD_BY_ID", {})
    monkeypatch.setattr(router_mod, "GatewayStreamConsumer", None)

    result = asyncio.run(
        router_mod._stream_into_feishu(
            adapter,
            "oc_test",
            "profile",
            tmp_path,
            SimpleNamespace(source=SimpleNamespace(chat_type="dm")),
        )
    )

    assert len(updated) == 1
    assert updated[0]["header"]["title"]["content"] == "已回答"
    assert result == "北京明天多云"
    assert clarify_id not in result


def test_shared_consumer_clarify_resolved_writes_terminal_card_without_leaking_payload(
    monkeypatch, tmp_path
):
    """Same guard on the branch production actually takes first.

    The sibling test above pins the legacy edit transport (GatewayStreamConsumer
    forced to None). When the core exposes a card-capable consumer, streaming.py
    routes into `_stream_into_feishu_shared_consumer` instead and never reaches
    that code, so deleting the terminal-card write there stays green without
    this test.
    """
    import asyncio

    from hermes_multitenancy import agent_real
    from hermes_multitenancy import feishu_clarify_cards
    from hermes_multitenancy import router as router_mod
    from tests.test_streaming_card_transport import _CardCapableAdapter

    clarify_id = "clarify_0123456789abcdef0123456789abcdef"
    updated: list[dict] = []
    consumers = []

    async def fake_stream(event, home, *, messages=None):
        yield "clarify_required", {
            "clarify_id": clarify_id,
            "question": "选择城市",
            "choices": ["北京", "上海"],
        }
        yield "clarify_resolved", {
            "clarify_id": clarify_id,
            "session_key": "multitenancy:feishu:feishu_x:oc_y:ou_z",
            "response": "北京",
            "timed_out": False,
        }
        yield "content", "北京明天多云"
        yield "done", "北京明天多云"

    async def fake_send(*, adapter, chat_id, card):
        return {
            "transport": "cardkit",
            "card_id": "cid_1",
            "message_id": "om_clarify",
            "sequence": 1,
        }

    async def fake_update(*, adapter, auth_card, card):
        updated.append(card)
        return True

    class StubConsumer:
        """Minimal stand-in for the core's card-capable GatewayStreamConsumer."""

        def __init__(
            self, adapter, chat_id, config=None, metadata=None, initial_reply_to_id=None
        ):
            self.deltas: list[str] = []
            self.statuses: list[str] = []
            self._done = asyncio.Event()
            consumers.append(self)

        async def ensure_streaming_card_started(self):
            return True

        async def run(self):
            await self._done.wait()

        def on_delta(self, text):
            self.deltas.append(text)

        async def update_streaming_card_status(self, content):
            self.statuses.append(content)
            return True

        async def update_streaming_card_reasoning(self, content):
            return True

        async def update_streaming_card_tool_started(self, tool_name, *, preview=None, args=None):
            return True

        async def update_streaming_card_tool_completed(
            self, tool_name, *, duration=None, is_error=False
        ):
            return True

        def finish(self):
            self._done.set()

    monkeypatch.setattr(agent_real, "stream_run_agent", fake_stream)
    monkeypatch.setattr(feishu_clarify_cards, "send_auth_card", fake_send)
    monkeypatch.setattr(feishu_clarify_cards, "update_auth_card", fake_update)
    monkeypatch.setattr(feishu_clarify_cards, "_CLARIFY_CARD_BY_ID", {})
    monkeypatch.setattr(router_mod, "GatewayStreamConsumer", StubConsumer, raising=False)
    monkeypatch.setattr(
        router_mod,
        "StreamConsumerConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
        raising=False,
    )

    result = asyncio.run(
        router_mod._stream_into_feishu(
            _CardCapableAdapter(),
            "oc_test",
            "profile",
            tmp_path,
            SimpleNamespace(source=SimpleNamespace(chat_type="dm")),
        )
    )

    assert consumers, "shared-consumer branch was not taken"
    assert "等待你的选择" in consumers[0].statuses
    assert len(updated) == 1
    assert updated[0]["header"]["title"]["content"] == "已回答"
    assert result == "北京明天多云"
    assert clarify_id not in result
    assert clarify_id not in "".join(consumers[0].deltas)
