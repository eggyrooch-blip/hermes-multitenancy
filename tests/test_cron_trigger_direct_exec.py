"""Deterministic in-chat cron trigger (SPEC cron-trigger-deterministic-exec).

Prod evidence 2026-08-03 10:56 / 2026-08-02 15:25: the model answered a
"触发一下这个job <id>" DM with tool_turns=0 — it *claimed* the trigger without
calling any tool, so the job never ran and the user waited for a result that
could not exist. These tests pin the harness-level guarantee: a short trigger
instruction for the sender's own job is executed deterministically (zero model
calls), the "已触发" reply is only sent after ``cron_api.trigger_job`` returns,
and every non-matching / not-owned / erroring path falls through to the model
unchanged.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

import pytest

from hermes_multitenancy import cron_api, cron_trigger_direct, router


# ---------------------------------------------------------------------------
# Pure matcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "触发一下这个job 2515da283456",
        "触发 2515da283456",
        "trigger job 2515da283456",
        "  帮我触发job 2515da283456 ",
    ],
)
def test_matcher_accepts_short_trigger_instruction(text: str) -> None:
    assert cron_trigger_direct.match_trigger_text(text) == "2515da283456"


@pytest.mark.parametrize(
    "text",
    [
        "手动触发 2515da283456",
        "请触发一下这个job 2515da283456",
        "please trigger 2515da283456",
    ],
)
def test_matcher_accepts_prefixed_imperatives(text: str) -> None:
    assert cron_trigger_direct.match_trigger_text(text) == "2515da283456"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "触发一下",  # no job id
        "2515da283456",  # id but no trigger verb
        "这个job 2515da283456 昨天为什么没跑",  # discussion, no verb
        "为什么没触发这个job 2515da283456",  # question marker
        "触发这个job 2515da283456 吗？",  # question mark
        "别触发这个job 2515da283456",  # negation
        "不要触发 2515da283456",  # negation
        "do not trigger 2515da283456",  # en negation (codex round-1)
        "don't trigger 2515da283456",  # en negation
        "why did trigger 2515da283456",  # en question
        "triggered 2515da283456",  # past tense = status text (codex round-2)
        "triggering 2515da283456",  # progressive = status text
        "触发器 2515da283456",  # noun 触发器, not the verb
        "job 2515da283456 will trigger tomorrow",  # verb not anchored
        "note trigger 2515da283456",  # verb not anchored
        "触发 2515da283456 还有 8f19c786ba34",  # ambiguous: two ids
        "触发一下这个job 2515da283456，" + "顺便说说" * 20,  # too long / discussion
    ],
)
def test_matcher_rejects_non_instruction_text(text: str) -> None:
    assert cron_trigger_direct.match_trigger_text(text) is None


# ---------------------------------------------------------------------------
# Routing entry (adapter + cron_api seam, same seam test_webui_broker_server uses)
# ---------------------------------------------------------------------------


class _Adapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


@pytest.fixture(autouse=True)
def _clear_dedup_store() -> None:
    cron_trigger_direct._seen_message_ids.clear()
    yield
    cron_trigger_direct._seen_message_ids.clear()


def _job(job_id: str = "2515da283456") -> dict[str, Any]:
    return {"id": job_id, "name": "双周会前一天提醒", "next_run_at": "now"}


@pytest.mark.asyncio
async def test_trigger_reply_only_after_trigger_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_trigger(profile_name: str, job_id: str) -> dict[str, Any]:
        assert not adapter.sent, "reply must not be sent before trigger_job returns"
        calls.append((profile_name, job_id))
        return _job(job_id)

    adapter = _Adapter()
    monkeypatch.setattr(cron_api, "trigger_job", fake_trigger)

    handled = await cron_trigger_direct.try_route_cron_trigger(
        adapter,
        chat_id="oc_dm",
        profile_name="sunke",
        text="触发一下这个job 2515da283456",
    )

    assert handled is True
    assert calls == [("sunke", "2515da283456")]
    assert len(adapter.sent) == 1
    chat_id, reply = adapter.sent[0]
    assert chat_id == "oc_dm"
    assert "已触发" in reply
    assert "2515da283456" in reply
    assert "双周会前一天提醒" in reply


@pytest.mark.asyncio
async def test_redelivered_message_id_is_consumed_without_second_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_trigger(profile_name: str, job_id: str) -> dict[str, Any]:
        calls.append((profile_name, job_id))
        return _job(job_id)

    adapter = _Adapter()
    monkeypatch.setattr(cron_api, "trigger_job", fake_trigger)

    kwargs = dict(
        chat_id="oc_dm",
        profile_name="sunke",
        text="触发一下这个job 2515da283456",
        message_id="om_dup",
    )
    first = await cron_trigger_direct.try_route_cron_trigger(adapter, **kwargs)
    second = await cron_trigger_direct.try_route_cron_trigger(adapter, **kwargs)

    assert first is True and second is True
    assert calls == [("sunke", "2515da283456")], "redelivery must not re-fire the job"
    assert len(adapter.sent) == 1, "redelivery must not re-ack"


@pytest.mark.asyncio
async def test_concurrent_redelivery_fires_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex round-2 repro: check and mark were separated by an await, so two
    concurrent deliveries of one message_id both fired. The reservation must be
    taken before the first await."""
    import asyncio
    import time as _time

    calls: list[tuple[str, str]] = []

    def slow_trigger(profile_name: str, job_id: str) -> dict[str, Any]:
        _time.sleep(0.05)  # keep the first call in-flight while the second races
        calls.append((profile_name, job_id))
        return _job(job_id)

    adapter = _Adapter()
    monkeypatch.setattr(cron_api, "trigger_job", slow_trigger)

    kwargs = dict(
        chat_id="oc_dm",
        profile_name="sunke",
        text="触发一下这个job 2515da283456",
        message_id="om_race",
    )
    results = await asyncio.gather(
        cron_trigger_direct.try_route_cron_trigger(adapter, **kwargs),
        cron_trigger_direct.try_route_cron_trigger(adapter, **kwargs),
    )

    assert results == [True, True]
    assert calls == [("sunke", "2515da283456")], "concurrent redelivery double-fired"
    assert len(adapter.sent) == 1, "concurrent redelivery double-acked"


@pytest.mark.asyncio
async def test_failed_trigger_releases_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed fire must not leave the message_id reserved — a later redelivery
    still has to reach the model path (fail-open)."""
    def boom(profile_name: str, job_id: str) -> dict[str, Any]:
        raise RuntimeError("transient")

    adapter = _Adapter()
    monkeypatch.setattr(cron_api, "trigger_job", boom)

    kwargs = dict(
        chat_id="oc_dm",
        profile_name="sunke",
        text="触发 2515da283456",
        message_id="om_fail",
    )
    assert await cron_trigger_direct.try_route_cron_trigger(adapter, **kwargs) is False
    assert "om_fail" not in cron_trigger_direct._seen_message_ids


@pytest.mark.asyncio
async def test_not_owned_job_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_trigger(profile_name: str, job_id: str) -> dict[str, Any]:
        raise cron_api.CronApiError("Job not found", 404)

    adapter = _Adapter()
    monkeypatch.setattr(cron_api, "trigger_job", fake_trigger)

    handled = await cron_trigger_direct.try_route_cron_trigger(
        adapter, chat_id="oc_dm", profile_name="sunke", text="触发 8f19c786ba34"
    )

    assert handled is False
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_unexpected_error_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_trigger(profile_name: str, job_id: str) -> dict[str, Any]:
        raise RuntimeError("disk on fire")

    adapter = _Adapter()
    monkeypatch.setattr(cron_api, "trigger_job", fake_trigger)

    handled = await cron_trigger_direct.try_route_cron_trigger(
        adapter, chat_id="oc_dm", profile_name="sunke", text="触发 2515da283456"
    )

    assert handled is False
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_unrouted_profile_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_trigger(profile_name: str, job_id: str) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("trigger_job must not be called without a profile")

    adapter = _Adapter()
    monkeypatch.setattr(cron_api, "trigger_job", fake_trigger)

    handled = await cron_trigger_direct.try_route_cron_trigger(
        adapter, chat_id="oc_dm", profile_name=None, text="触发 2515da283456"
    )

    assert handled is False
    assert adapter.sent == []


# ---------------------------------------------------------------------------
# handle_async wiring: DM trigger instruction never reaches the agent path;
# everything else keeps flowing exactly as before.
# ---------------------------------------------------------------------------


def _event(
    *,
    text: str,
    chat_id: str = "oc_dm",
    chat_type: str = "p2p",
    message_id: str = "om_trigger",
    sender: str = "ou_sunke",
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        message_id=message_id,
        sender_open_id=sender,
        source=SimpleNamespace(
            chat_id=chat_id,
            message_id=message_id,
            user_id=sender,
            user_id_alt=None,
            open_id=sender,
            chat_type=chat_type,
            platform=SimpleNamespace(value="feishu"),
        ),
        raw_event={"event": {"message": {"message_id": message_id}}},
    )


@pytest.fixture()
def _wired(monkeypatch: pytest.MonkeyPatch):
    """Wire handle_async with a recording adapter and an agent-path tripwire."""
    adapter = _Adapter()
    agent_path: list[str] = []
    trigger_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(router, "_get_feishu_adapter", lambda gateway: adapter)
    monkeypatch.setattr(
        router, "_resolve_route", lambda sender, alt_id=None: ("sunke", "/tmp/profiles/sunke")
    )
    monkeypatch.setattr(
        router,
        "_resolve_or_auto_provision_route",
        lambda sender, alt_id=None: ("sunke", "/tmp/profiles/sunke"),
    )
    monkeypatch.setattr(router, "_capture_pending_auth_replay", lambda *a, **k: None)
    monkeypatch.setattr(router, "_materialize_inbound_media_for_profile", lambda *a, **k: None)

    def _tripwire(*a: Any, **k: Any) -> Any:
        agent_path.append("agent")
        raise RuntimeError("stop at agent path (test tripwire)")

    monkeypatch.setattr(router, "_make_routed_run_broker", _tripwire)

    async def _no_push_confirm(gateway: Any, event: Any) -> bool:
        return False

    async def _no_push_card(adapter_: Any, event_: Any) -> bool:
        return False

    from hermes_multitenancy import push_card_confirm, push_card_matcher

    monkeypatch.setattr(push_card_confirm, "try_route_push_confirm_synthetic", _no_push_confirm)
    monkeypatch.setattr(push_card_matcher, "try_route_push_card_reply", _no_push_card)

    def fake_trigger(profile_name: str, job_id: str) -> dict[str, Any]:
        trigger_calls.append((profile_name, job_id))
        return _job(job_id)

    monkeypatch.setattr(cron_api, "trigger_job", fake_trigger)
    return adapter, agent_path, trigger_calls


@pytest.mark.asyncio
async def test_handle_async_intercepts_dm_trigger(_wired) -> None:
    adapter, agent_path, trigger_calls = _wired

    await router.handle_async(
        event=_event(text="触发一下这个job 2515da283456"),
        gateway=SimpleNamespace(adapters={}),
    )

    assert trigger_calls == [("sunke", "2515da283456")]
    assert agent_path == [], "intercepted trigger must never reach the agent path"
    assert any("已触发" in text for _, text in adapter.sent)


@pytest.mark.asyncio
async def test_handle_async_passes_normal_dm_to_agent(_wired) -> None:
    adapter, agent_path, trigger_calls = _wired

    await router.handle_async(
        event=_event(text="这个job 2515da283456 昨天为什么没跑"),
        gateway=SimpleNamespace(adapters={}),
    )

    assert trigger_calls == []
    assert agent_path == ["agent"], "non-instruction text must keep flowing to the agent"


@pytest.mark.asyncio
async def test_handle_async_never_intercepts_group_chat(_wired, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, agent_path, trigger_calls = _wired
    monkeypatch.setattr(
        router,
        "resolve_or_auto_provision_group_route",
        _async_group_route(("grp", "/tmp/profiles/grp")),
    )

    await router.handle_async(
        event=_event(text="触发一下这个job 2515da283456", chat_type="group", chat_id="oc_group"),
        gateway=SimpleNamespace(adapters={}),
    )

    assert trigger_calls == []
    assert agent_path == ["agent"], "group chat must keep current behavior"


def _async_group_route(result: tuple[Optional[str], Optional[str]]):
    async def _resolver(*, chat_id: str, gateway: Any) -> tuple[Optional[str], Optional[str]]:
        return result

    return _resolver


@pytest.mark.asyncio
async def test_handle_async_skips_yolo_rewritten_text(_wired) -> None:
    """codex round-1: a push-card yolo rewrite is the card loop's payload, not a
    user instruction — even if it contains 'trigger <id>' it must reach the agent."""
    adapter, agent_path, trigger_calls = _wired

    event = _event(text="触发一下这个job 2515da283456")
    from hermes_multitenancy.push_card_matcher import _YOLO_ROUTED_ATTR

    setattr(event, _YOLO_ROUTED_ATTR, "scene1")

    await router.handle_async(event=event, gateway=SimpleNamespace(adapters={}))

    assert trigger_calls == []
    assert agent_path == ["agent"]


@pytest.mark.asyncio
async def test_handle_async_skips_fixed_expert_dm(_wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """codex round-1: the read-only expert entrance must not fire the canonical
    profile's jobs through the deterministic trigger."""
    from pathlib import Path

    from hermes_multitenancy import expert_bot_route

    adapter, agent_path, trigger_calls = _wired
    context = expert_bot_route.FixedExpertContext(
        profile_name="sunke",
        profile_home=Path("/tmp/profiles/sunke"),
        canonical_open_id="ou_sunke",
        union_id="on_sunke",
        expert_id="expert1",
        role_override_block="",
        metadata={},
    )
    monkeypatch.setattr(expert_bot_route, "fixed_expert_id_from_env", lambda: "expert1")
    monkeypatch.setattr(
        expert_bot_route, "resolve_fixed_expert_context", lambda *a, **k: context
    )
    monkeypatch.setattr(expert_bot_route, "apply_fixed_expert_context", lambda *a, **k: None)

    await router.handle_async(
        event=_event(text="触发一下这个job 2515da283456"),
        gateway=SimpleNamespace(adapters={}),
    )

    assert trigger_calls == []
    assert agent_path == ["agent"]
