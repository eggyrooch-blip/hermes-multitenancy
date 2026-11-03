"""JIT (just-in-time) Feishu permission auth card — end-to-end in three segments.

A tool fails mid-run because the user lacks a Feishu scope (99991672 app-scope /
99991679 user-scope). The three segments the feature must chain:

  1. DETECT   — the lark-cli auth broker sees the scope-missing code in the
                forwarded Feishu response and fires the permission sink (which is
                wired to the run-scope signal → yields ("auth_required", …)).
  2. CARD     — the router consumes auth_required and pushes a device-code auth
                card whose button opens the verification link in the Feishu
                in-app SIDE PANEL (sidebar applink).
  3. CONTINUE — on successful authorization the poll replays the user's ORIGINAL
                request, so the stuck operation finishes with no re-typing.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest


# --------------------------------------------------------------------------- #
# Segment 1 — DETECT: scope-missing code in a forwarded response fires the sink
# --------------------------------------------------------------------------- #

def _broker(sink) -> Any:
    from hermes_multitenancy.lark_cli_auth_broker import (
        LarkCliAuthBroker,
        LarkCliAuthBrokerContext,
    )

    return LarkCliAuthBroker(
        LarkCliAuthBrokerContext(
            shared_home=Path("/nonexistent"),
            profile_name="alice",
            user_open_id="ou_alice",
            hmac_key="k",
            permission_denied_sink=sink,
        )
    )


@pytest.mark.parametrize(
    "body,expected",
    [
        (b'{"code":99991679,"msg":"no permission"}', "user_scope_insufficient"),
        (b'{"code":99991672,"msg":"app scope missing"}', "app_scope_missing"),
    ],
)
def test_detect_fires_sink_on_permission_code(body: bytes, expected: str) -> None:
    captured: list[dict] = []
    _broker(captured.append)._maybe_signal_permission_denied("user", body)
    assert len(captured) == 1
    assert captured[0]["scope_status"] == expected
    assert captured[0]["connector_id"] == "lark-cli"


def test_detect_silent_on_success_and_coincidental_digits() -> None:
    captured: list[dict] = []
    broker = _broker(captured.append)
    # Success — top-level code 0.
    broker._maybe_signal_permission_denied("user", b'{"code":0,"data":{"ok":true}}')
    # Coincidental digit run buried in data, NOT a top-level permission code —
    # structured classify_lark_error must reject it (no text scanning).
    broker._maybe_signal_permission_denied(
        "user", b'{"code":0,"data":{"chat_id":"99991679"}}'
    )
    # A different definitive top-level code (rate limit) is not permission.
    broker._maybe_signal_permission_denied("user", b'{"code":230020}')
    assert captured == []


def test_detect_sink_none_and_broken_sink_never_raise() -> None:
    # No sink configured → no-op.
    _broker(None)._maybe_signal_permission_denied("user", b'{"code":99991679}')

    def boom(_payload: dict) -> None:
        raise RuntimeError("sink exploded")

    # A broken sink must never turn a proxied response into a 500.
    _broker(boom)._maybe_signal_permission_denied("user", b'{"code":99991679}')


# --------------------------------------------------------------------------- #
# Segment 2 — CARD: auth_required → device-code card with a sidebar applink
# --------------------------------------------------------------------------- #

def _find_button_url(card: dict) -> str:
    for element in card["body"]["elements"]:
        if element.get("tag") == "column_set":
            for column in element["columns"]:
                for child in column["elements"]:
                    if child.get("tag") == "button":
                        return str(child["multi_url"]["url"])
    return ""


def test_to_in_app_web_url_is_sidebar_applink() -> None:
    from hermes_multitenancy.feishu_auth_cards import to_in_app_web_url

    wrapped = to_in_app_web_url("https://open.feishu.cn/authen?device=abc")
    assert wrapped.startswith("https://applink.feishu.cn/client/web_url/open?")
    assert "mode=sidebar-semi" in wrapped
    # The original verification URL is carried url-encoded in the ?url= param.
    assert "open.feishu.cn" in wrapped
    assert to_in_app_web_url("") == ""  # empty in → empty out


@pytest.mark.asyncio
async def test_card_pushes_sidebar_applink_and_schedules_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import feishu_uat_auth, router
    from hermes_multitenancy import feishu_auth_cards

    monkeypatch.setattr(
        feishu_uat_auth, "find_active_session", lambda **_kw: None
    )
    monkeypatch.setattr(
        feishu_uat_auth,
        "start_session",
        lambda **kw: {
            "session_id": "sess-1",
            "verification_uri": "https://open.feishu.cn/authen?device=abc",
            "user_code": "WXYZ",
            "expires_at": 0,
            "interval": 3,
            "_open_id": kw.get("open_id"),
        },
    )

    sent: dict[str, Any] = {}

    async def fake_send_auth_card(*, adapter, chat_id, card, metadata=None):
        sent["chat_id"] = chat_id
        sent["card"] = card
        return {"message_id": "om_card"}

    monkeypatch.setattr(feishu_auth_cards, "send_auth_card", fake_send_auth_card)

    scheduled: dict[str, Any] = {}

    def fake_start_poll(**kwargs):
        scheduled.update(kwargs)

    monkeypatch.setattr(router, "_start_feishu_auth_poll_task", fake_start_poll)

    adapter = SimpleNamespace(send=lambda *a, **k: None)
    event = SimpleNamespace(
        sender_open_id="ou_alice",
        source=SimpleNamespace(user_id="ou_alice"),
    )

    await router._handle_jit_auth_required(
        gateway=SimpleNamespace(),
        adapter=adapter,
        chat_id="oc_chat",
        profile_name="alice",
        profile_home=Path("/tmp/alice"),
        event=event,
        payload={"scope_status": "user_scope_insufficient"},
    )

    assert sent["chat_id"] == "oc_chat"
    button_url = _find_button_url(sent["card"])
    assert button_url.startswith("https://applink.feishu.cn/client/web_url/open?")
    assert "mode=sidebar-semi" in button_url
    # Session + poll are bound to the INITIATOR's open_id (group anti-spoof).
    assert scheduled["open_id"] == "ou_alice"
    assert scheduled["session_id"] == "sess-1"
    assert scheduled["chat_id"] == "oc_chat"


@pytest.mark.asyncio
async def test_card_noop_without_adapter_or_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import feishu_uat_auth, router

    def _should_not_run(**_kw):
        raise AssertionError("must not start a session without adapter/profile")

    monkeypatch.setattr(feishu_uat_auth, "start_session", _should_not_run)
    monkeypatch.setattr(feishu_uat_auth, "find_active_session", _should_not_run)

    # No adapter → silent no-op (nothing to render into).
    await router._handle_jit_auth_required(
        gateway=SimpleNamespace(), adapter=None, chat_id="c",
        profile_name="alice", profile_home=None,
        event=SimpleNamespace(sender_open_id="ou_alice", source=None),
    )
    # No profile → silent no-op.
    await router._handle_jit_auth_required(
        gateway=SimpleNamespace(), adapter=SimpleNamespace(), chat_id="c",
        profile_name=None, profile_home=None,
        event=SimpleNamespace(sender_open_id="ou_alice", source=None),
    )


# --------------------------------------------------------------------------- #
# Segment 3 — CONTINUE: successful auth replays the ORIGINAL request
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_continue_replays_original_request_after_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_multitenancy import router

    captured: list[Any] = []

    async def fake_handle_async(*, event: Any, gateway: Any) -> None:
        captured.append(event)

    monkeypatch.setattr(router, "handle_async", fake_handle_async)

    # The original inbound request text was captured at admission (router does
    # this for every non-command message); after auth it must replay verbatim.
    router._capture_pending_auth_replay("alice", "ou_alice", "帮我查下这个月的考勤")

    event = SimpleNamespace(
        text=router.SYNTHETIC_AUTH_COMPLETE_TEXT,
        message_id="om_real",
        sender_open_id="ou_alice",
        source=SimpleNamespace(
            chat_id="oc_chat",
            message_id="om_real",
            user_id="ou_alice",
            open_id="ou_alice",
            chat_type="p2p",
        ),
        raw_event={"event": {"message": {"message_id": "om_real"}}},
    )

    ok = await router._dispatch_synthetic_auth_complete(
        event=event,
        gateway=SimpleNamespace(adapters={}),
        chat_id="oc_chat",
        profile_name="alice",
        open_id="ou_alice",
    )

    assert ok is True
    assert len(captured) == 1
    # The replayed turn carries the ORIGINAL request, not the placeholder text —
    # so the stuck operation resumes exactly, with no "model misremembers" risk.
    assert captured[0].text == "帮我查下这个月的考勤"


# --------------------------------------------------------------------------- #
# Segment 4 — GROUP GUARD: groups use the APP (bot) identity by design, so the
# JIT USER-auth card must never fire there (sunke: 群里只用应用身份、不授权个人
# lark-cli). Two layers: the broker sink ignores non-user identity, and the
# handler refuses a group profile.
# --------------------------------------------------------------------------- #

def test_detect_silent_for_bot_identity() -> None:
    # A bot-identity call (groups / webui-agent) that hits a scope code must NOT
    # raise a user-authorization signal — the user can't/shouldn't authorize a
    # personal token for a group agent.
    captured: list[dict] = []
    _broker(captured.append)._maybe_signal_permission_denied(
        "bot", b'{"code":99991679,"msg":"no permission"}'
    )
    assert captured == []


@pytest.mark.asyncio
async def test_card_noop_for_group_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_multitenancy import feishu_uat_auth, router

    def _should_not_run(**_kw):
        raise AssertionError("must not start a user auth session for a group profile")

    monkeypatch.setattr(feishu_uat_auth, "start_session", _should_not_run)
    monkeypatch.setattr(feishu_uat_auth, "find_active_session", _should_not_run)

    # A group profile (feishu_group_*) uses the app/bot identity → never push a
    # "authorize your personal lark-cli" card.
    await router._handle_jit_auth_required(
        gateway=SimpleNamespace(), adapter=SimpleNamespace(), chat_id="c",
        profile_name="feishu_group_abc123", profile_home=None,
        event=SimpleNamespace(sender_open_id="ou_alice", source=None),
    )


@pytest.mark.asyncio
async def test_second_jit_does_not_double_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two JIT triggers for the SAME device-code session (a concurrent run, or a turn
    # that both expires creds and hits a permission error) must drive ONE poll task,
    # not two — otherwise a successful auth fires the synthetic replay twice.
    from hermes_multitenancy import router
    from hermes_multitenancy.router import commands

    commands._ACTIVE_AUTH_POLLS.clear()

    started = 0
    release = asyncio.Event()

    async def fake_poll(**_kw):
        nonlocal started
        started += 1
        await release.wait()

    monkeypatch.setattr(router, "_poll_feishu_auth_session_until_done", fake_poll)

    common = dict(
        profile_name="feishu_g1", open_id="ou_alice", chat_id="c",
        gateway=SimpleNamespace(), event=SimpleNamespace(), interval=1, auth_card=None,
    )
    router._start_feishu_auth_poll_task(session_id="sess-1", **common)
    router._start_feishu_auth_poll_task(session_id="sess-1", **common)  # duplicate → dropped
    await asyncio.sleep(0)  # let the first task body reach its await
    assert started == 1
    assert "sess-1" in commands._ACTIVE_AUTH_POLLS

    # First poller finishes → session frees → a later JIT can start a fresh poll.
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)  # drain task completion + done-callback
    assert "sess-1" not in commands._ACTIVE_AUTH_POLLS

    router._start_feishu_auth_poll_task(session_id="sess-1", **common)
    await asyncio.sleep(0)
    assert started == 2


@pytest.mark.asyncio
async def test_same_run_emits_one_safe_auth_required_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from hermes_multitenancy import agent_real

    official = {"provider": "feishu", "connector_id": "lark-cli"}
    unsafe_duplicate = {
        "provider": "feishu",
        "connector_id": "lark-cli",
        "hint": "paste token here",
        "access_token": "must-not-surface",
    }

    async def duplicate_auth_stream(*_args, **_kwargs):
        agent_real._CREDENTIAL_EXPIRY_SIGNAL.get().set(official)
        agent_real._PERMISSION_AUTH_SIGNAL.get().set(unsafe_duplicate)
        yield "done", "OAuth stopped"

    monkeypatch.setattr(agent_real, "_stream_aiagent_subprocess", duplicate_auth_stream)
    event = SimpleNamespace(text="read calendar", message_id="om-1", source=None)

    chunks = [item async for item in agent_real.stream_run_agent(event, tmp_path)]
    auth_signals = [payload for kind, payload in chunks if kind == "auth_required"]

    assert auth_signals == [official]
    visible = str(auth_signals).lower()
    assert "paste token" not in visible
    assert "must-not-surface" not in visible
