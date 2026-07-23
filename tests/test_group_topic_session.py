from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _strict_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")


def _topic_event(
    *,
    thread_id: str = "omt_topic",
    sender: str = "ou_a",
    sender_name: str = "Alice",
) -> SimpleNamespace:
    return SimpleNamespace(
        text="do the thing",
        message_id="om_message",
        source=SimpleNamespace(
            platform=SimpleNamespace(value="feishu"),
            chat_id="oc_group",
            chat_type="group",
            user_id=sender,
            user_name=sender_name,
            thread_id=thread_id,
            hermes_group_topic=True,
        ),
    )


def test_group_topic_shares_history_but_not_inflight_across_senders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import router

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setattr(router, "_route_version_for", lambda *args, **kwargs: 7)

    alice = router._dispatch_session_scope(
        "security", "ou_a", None, "oc_group", _topic_event(sender="ou_a")
    )
    bob = router._dispatch_session_scope(
        "security", "ou_b", None, "oc_group", _topic_event(sender="ou_b")
    )

    assert alice.history_key == bob.history_key
    assert alice.inflight_key != bob.inflight_key


def test_group_topic_keeps_distinct_real_threads_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import router

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setattr(router, "_route_version_for", lambda *args, **kwargs: 0)

    first = router._dispatch_session_scope(
        "security", "ou_a", None, "oc_group", _topic_event(thread_id="omt_one")
    )
    second = router._dispatch_session_scope(
        "security", "ou_b", None, "oc_group", _topic_event(thread_id="omt_two")
    )

    assert first.history_key != second.history_key


def test_plain_group_and_dm_remain_sender_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import router

    monkeypatch.setenv("HERMES_MULTITENANCY_STRICT_CONTEXT", "1")
    monkeypatch.setattr(router, "_route_version_for", lambda *args, **kwargs: 0)
    event = _topic_event()
    event.source.hermes_group_topic = False

    alice = router._dispatch_session_scope("security", "ou_a", None, "oc_group", event)
    bob = router._dispatch_session_scope("security", "ou_b", None, "oc_group", event)

    assert alice.history_key != bob.history_key


def test_strict_off_group_topic_remains_legacy_sender_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import router

    monkeypatch.delenv("HERMES_MULTITENANCY_STRICT_CONTEXT", raising=False)
    alice = router._dispatch_session_scope(
        "security", "ou_a", None, "oc_group", _topic_event(sender="ou_a")
    )
    bob = router._dispatch_session_scope(
        "security", "ou_b", None, "oc_group", _topic_event(sender="ou_b")
    )

    assert alice.history_key == ("security", "ou_a")
    assert bob.history_key == ("security", "ou_b")
    profile_home = Path("/tmp/profiles/security")
    assert router._dispatch_session_scope(
        "security", "ou_a", None, "oc_group", _topic_event(sender="ou_a")
    ).history_key != router._dispatch_session_scope(
        "security", "ou_b", None, "oc_group", _topic_event(sender="ou_b")
    ).history_key

    from hermes_multitenancy import agent_real
    from hermes_multitenancy.feishu_group_topic_session import group_topic_epoch_actor

    assert agent_real._resolve_aiagent_session_id(
        _topic_event(sender="ou_a"), profile_home, "ou_a"
    ) != agent_real._resolve_aiagent_session_id(
        _topic_event(sender="ou_b"), profile_home, "ou_b"
    )
    assert group_topic_epoch_actor(_topic_event(sender="ou_a"), "ou_a") == "ou_a"
    assert group_topic_epoch_actor(_topic_event(sender="ou_b"), "ou_b") == "ou_b"


def test_group_topic_aiagent_session_matches_across_senders(tmp_path: Path) -> None:
    from hermes_multitenancy import agent_real

    profile_home = tmp_path / "profiles" / "security"
    event = _topic_event()

    alice = agent_real._resolve_aiagent_session_id(event, profile_home, "ou_a")
    bob = agent_real._resolve_aiagent_session_id(event, profile_home, "ou_b")
    other_topic = agent_real._resolve_aiagent_session_id(
        _topic_event(thread_id="omt_other"), profile_home, "ou_b"
    )

    assert alice == bob
    assert alice != other_topic
    assert "thread:omt_topic" in alice
    assert "user:" not in alice


def test_group_topic_user_turn_is_attributed() -> None:
    from hermes_multitenancy import router

    message = router._build_user_message(
        _topic_event(sender_name="Bob"),
        text_override="ban the domain",
    )

    assert message["role"] == "user"
    assert message["content"].startswith("[Sender: Bob; id:")
    assert message["content"].endswith("]\nban the domain")
    assert "ou_a" not in message["content"]


def test_group_topic_keeps_current_sender_as_credential_subject() -> None:
    from hermes_multitenancy import router

    alice = router._run_request_for_routed_event(
        event=_topic_event(sender="ou_a"),
        profile_name="security",
        sender="ou_a",
        sender_alt=None,
        chat_id="oc_group",
        text="investigate",
    )
    bob = router._run_request_for_routed_event(
        event=_topic_event(sender="ou_b"),
        profile_name="security",
        sender="ou_b",
        sender_alt=None,
        chat_id="oc_group",
        text="block",
    )

    assert alice.credential_subject == "ou_a"
    assert bob.credential_subject == "ou_b"
    assert alice.metadata["sender_open_id"] == "ou_a"
    assert bob.metadata["sender_open_id"] == "ou_b"


def test_group_topic_epoch_is_topic_bound_not_sender_bound() -> None:
    from hermes_multitenancy.feishu_group_topic_session import group_topic_epoch_actor

    event = _topic_event(thread_id="omt_topic")

    assert group_topic_epoch_actor(event, "ou_a") == "omt_topic"
    assert group_topic_epoch_actor(event, "ou_b") == "omt_topic"
    assert group_topic_epoch_actor(_topic_event(thread_id="omt_other"), "ou_b") == "omt_other"


def test_subprocess_payload_preserves_attested_topic_marker(tmp_path: Path) -> None:
    from hermes_multitenancy import agent_real

    payload = agent_real._event_to_subprocess_payload(
        _topic_event(),
        tmp_path / "profiles" / "security",
    )

    assert payload["event"]["source"]["thread_id"] == "omt_topic"
    assert payload["event"]["source"]["hermes_group_topic"] is True


class _FakeMessageApi:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls = 0

    def get(self, request: Any) -> Any:
        self.calls += 1
        return self.response


class _FakeChatApi(_FakeMessageApi):
    pass


class _FakeAdapter:
    def __init__(self, response: Any, chat_response: Any = None) -> None:
        message_api = _FakeMessageApi(response)
        chat_api = _FakeChatApi(chat_response or _chat_response())
        self._client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(message=message_api, chat=chat_api)
            )
        )
        self.message_api = message_api
        self.chat_api = chat_api
        self.last_event: Any = None

    @staticmethod
    def _build_get_message_request(message_id: str) -> str:
        return message_id

    @staticmethod
    def _build_get_chat_request(chat_id: str) -> str:
        return chat_id

    @staticmethod
    async def _run_blocking(fn: Any, request: Any) -> Any:
        return fn(request)

    async def _process_inbound_message(
        self,
        *,
        data: Any,
        message: Any,
        sender_id: Any,
        chat_type: str,
        message_id: str,
    ) -> None:
        source = SimpleNamespace(
            chat_id=message.chat_id,
            chat_type="group",
            user_id=sender_id.open_id,
            user_name="Alice",
            thread_id=message.thread_id or message.root_id or None,
        )
        await self._dispatch_inbound_event(
            SimpleNamespace(message_id=message_id, source=source)
        )

    async def _dispatch_inbound_event(self, event: Any) -> None:
        self.last_event = event

    def _text_batch_key(self, event: Any) -> str:
        return f"batch:{event.source.chat_id}:{event.source.thread_id}"

    def _media_batch_key(self, event: Any) -> str:
        return f"media:{event.source.chat_id}:{event.source.thread_id}"


def _message_response(
    *,
    thread_id: str | None,
    chat_id: str = "oc_group",
    success: bool = True,
) -> Any:
    item = SimpleNamespace(thread_id=thread_id, chat_id=chat_id)
    return SimpleNamespace(
        success=lambda: success,
        data=SimpleNamespace(items=[item]),
    )


def _chat_response(
    *,
    chat_mode: str = "topic",
    group_message_type: str = "chat",
    success: bool = True,
) -> Any:
    return SimpleNamespace(
        success=lambda: success,
        data=SimpleNamespace(
            chat_mode=chat_mode,
            group_message_type=group_message_type,
        ),
    )


def test_root_only_event_is_hydrated_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import feishu_group_topic_session as topic_patch

    monkeypatch.setattr(topic_patch, "_HOOK_INSTALLED", False)
    monkeypatch.setattr(topic_patch, "load_feishu_adapter", lambda: _FakeAdapter)
    topic_patch.install_feishu_group_topic_session_patch()
    adapter = _FakeAdapter(_message_response(thread_id="omt_topic"))
    message = SimpleNamespace(
        chat_id="oc_group",
        thread_id=None,
        root_id="om_root",
    )

    asyncio.run(
        adapter._process_inbound_message(
            data=SimpleNamespace(),
            message=message,
            sender_id=SimpleNamespace(open_id="ou_a"),
            chat_type="group",
            message_id="om_current",
        )
    )

    assert adapter.last_event.source.thread_id == "omt_topic"
    assert adapter.last_event.source.hermes_group_topic is True
    assert adapter.last_event.source.hermes_raw_thread_id is None
    assert adapter.last_event.source.hermes_root_id == "om_root"


def test_hydration_failure_keeps_narrow_legacy_scope(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from hermes_multitenancy import feishu_group_topic_session as topic_patch

    monkeypatch.setattr(topic_patch, "_HOOK_INSTALLED", False)
    monkeypatch.setattr(topic_patch, "load_feishu_adapter", lambda: _FakeAdapter)
    topic_patch.install_feishu_group_topic_session_patch()
    adapter = _FakeAdapter(_message_response(thread_id=None))
    message = SimpleNamespace(
        chat_id="oc_group",
        thread_id=None,
        root_id="om_root",
    )

    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            adapter._process_inbound_message(
                data=SimpleNamespace(),
                message=message,
                sender_id=SimpleNamespace(open_id="ou_a"),
                chat_type="group",
                message_id="om_current",
            )
        )

    assert adapter.last_event.source.thread_id == "om_root"
    assert adapter.last_event.source.hermes_group_topic is False
    assert "unavailable" in caplog.text
    assert "om_root" not in caplog.text
    assert "om_current" not in caplog.text


def test_hydration_rejects_cross_chat_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import feishu_group_topic_session as topic_patch

    monkeypatch.setattr(topic_patch, "_HOOK_INSTALLED", False)
    monkeypatch.setattr(topic_patch, "load_feishu_adapter", lambda: _FakeAdapter)
    topic_patch.install_feishu_group_topic_session_patch()
    adapter = _FakeAdapter(
        _message_response(thread_id="omt_wrong", chat_id="oc_other")
    )
    message = SimpleNamespace(
        chat_id="oc_group",
        thread_id=None,
        root_id="om_root",
    )

    asyncio.run(
        adapter._process_inbound_message(
            data=SimpleNamespace(),
            message=message,
            sender_id=SimpleNamespace(open_id="ou_a"),
            chat_type="group",
            message_id="om_current",
        )
    )

    assert adapter.last_event.source.thread_id == "om_root"
    assert adapter.last_event.source.hermes_group_topic is False


def test_raw_thread_id_needs_no_api_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import feishu_group_topic_session as topic_patch

    monkeypatch.setattr(topic_patch, "_HOOK_INSTALLED", False)
    monkeypatch.setattr(topic_patch, "load_feishu_adapter", lambda: _FakeAdapter)
    topic_patch.install_feishu_group_topic_session_patch()
    adapter = _FakeAdapter(_message_response(thread_id=None))
    message = SimpleNamespace(
        chat_id="oc_group",
        thread_id="omt_direct",
        root_id="om_root",
    )

    asyncio.run(
        adapter._process_inbound_message(
            data=SimpleNamespace(),
            message=message,
            sender_id=SimpleNamespace(open_id="ou_a"),
            chat_type="group",
            message_id="om_current",
        )
    )

    assert adapter.last_event.source.thread_id == "omt_direct"
    assert adapter.last_event.source.hermes_group_topic is True
    assert adapter.message_api.calls == 0
    assert adapter.chat_api.calls == 1


def test_plain_group_raw_thread_is_not_topic_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import feishu_group_topic_session as topic_patch

    monkeypatch.setattr(topic_patch, "_HOOK_INSTALLED", False)
    monkeypatch.setattr(topic_patch, "load_feishu_adapter", lambda: _FakeAdapter)
    topic_patch.install_feishu_group_topic_session_patch()
    adapter = _FakeAdapter(
        _message_response(thread_id="omt_direct"),
        _chat_response(chat_mode="group", group_message_type="chat"),
    )
    message = SimpleNamespace(
        chat_id="oc_group",
        thread_id="omt_direct",
        root_id="om_root",
    )

    asyncio.run(
        adapter._process_inbound_message(
            data=SimpleNamespace(),
            message=message,
            sender_id=SimpleNamespace(open_id="ou_a"),
            chat_type="group",
            message_id="om_current",
        )
    )

    assert adapter.last_event.source.thread_id == "omt_direct"
    assert adapter.last_event.source.hermes_group_topic is False


def test_topic_batching_stays_sender_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import feishu_group_topic_session as topic_patch

    monkeypatch.setattr(topic_patch, "_HOOK_INSTALLED", False)
    monkeypatch.setattr(topic_patch, "load_feishu_adapter", lambda: _FakeAdapter)
    topic_patch.install_feishu_group_topic_session_patch()
    adapter = _FakeAdapter(_message_response(thread_id=None))
    alice = _topic_event(sender="ou_a")
    bob = _topic_event(sender="ou_b")

    assert adapter._text_batch_key(alice) != adapter._text_batch_key(bob)
    assert adapter._media_batch_key(alice) != adapter._media_batch_key(bob)


def test_pending_attestations_are_not_cleared_at_cache_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import feishu_group_topic_session as topic_patch

    monkeypatch.setattr(topic_patch, "_HOOK_INSTALLED", False)
    monkeypatch.setattr(topic_patch, "load_feishu_adapter", lambda: _FakeAdapter)
    topic_patch.install_feishu_group_topic_session_patch()
    adapter = _FakeAdapter(_message_response(thread_id="omt_topic"))
    pending = {f"om_{index}": object() for index in range(256)}
    adapter._mt_group_topic_by_message = pending
    message = SimpleNamespace(chat_id="oc_group", thread_id=None, root_id="om_root")

    asyncio.run(
        adapter._process_inbound_message(
            data=SimpleNamespace(),
            message=message,
            sender_id=SimpleNamespace(open_id="ou_a"),
            chat_type="group",
            message_id="om_current",
        )
    )

    assert all(f"om_{index}" in pending for index in range(256))


def test_auth_complete_replay_preserves_topic_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import router
    from hermes_multitenancy.router import commands

    seen: list[Any] = []

    async def handle_async(**kwargs: Any) -> None:
        seen.append(kwargs["event"])

    monkeypatch.setattr(router, "handle_async", handle_async)
    original = _topic_event()
    original.source.chat_topic = None
    original.source.hermes_raw_thread_id = None
    original.source.hermes_root_id = "om_root"

    assert asyncio.run(
        commands._dispatch_synthetic_auth_complete(
            event=original,
            gateway=SimpleNamespace(),
            chat_id="oc_group",
            profile_name="security",
            open_id="ou_a",
            text="resume",
        )
    )

    replay = seen[0]
    assert replay.source.thread_id == "omt_topic"
    assert replay.source.hermes_group_topic is True
    assert replay.source.hermes_root_id == "om_root"
    assert router._run_request_for_routed_event(
        event=replay,
        profile_name="security",
        sender="ou_a",
        sender_alt=None,
        chat_id="oc_group",
        text="resume",
    ).credential_subject == "ou_a"
