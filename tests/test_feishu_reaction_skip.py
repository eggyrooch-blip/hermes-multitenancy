"""Regression tests for Feishu reaction-routed synthetic events."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_multitenancy import router as router_mod


def _predicate():
    predicate = getattr(router_mod, "_is_reaction_synthetic_event", None)
    if not callable(predicate):
        pytest.fail("router must expose _is_reaction_synthetic_event")
    return predicate


def _reaction_event(text: str = "reaction:added:THUMBSUP") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        message_type=SimpleNamespace(name="TEXT"),
        message_id="om_reaction",
        source=SimpleNamespace(
            chat_id="oc_chat",
            user_id="ou_user",
            user_id_alt=None,
            chat_type="dm",
            platform=SimpleNamespace(value="feishu"),
            message_id="om_reaction",
        ),
        media_urls=None,
        media_types=None,
        raw_event=None,
    )


def test_reaction_synthetic_predicate_matches_added_and_removed():
    event = _reaction_event()

    assert _predicate()(event, "reaction:added:THUMBSUP") is True
    assert _predicate()(event, "reaction:removed:OK") is True


def test_reaction_synthetic_predicate_does_not_match_regular_text():
    event = _reaction_event("hello")

    assert _predicate()(event, "hello") is False
    assert _predicate()(event, "reaction without colon") is False
    assert _predicate()(event, "") is False


def test_reaction_synthetic_predicate_requires_text_message_type_when_known():
    event = _reaction_event()
    event.message_type = SimpleNamespace(name="MEDIA")

    assert _predicate()(event, "reaction:added:THUMBSUP") is False


def test_reaction_synthetic_predicate_handles_missing_message_type():
    event = _reaction_event()
    delattr(event, "message_type")

    assert _predicate()(event, "reaction:added:HEART") is True


def test_handle_async_short_circuits_reaction_added_event():
    event = _reaction_event("reaction:added:THUMBSUP")

    with tempfile.TemporaryDirectory() as profile_home:
        with (
            patch(
                "hermes_multitenancy.router._resolve_or_auto_provision_route",
                return_value=("profile", Path(profile_home)),
            ),
            patch(
                "hermes_multitenancy.router._get_feishu_adapter",
                side_effect=AssertionError("reaction synthetic must not reach adapter dispatch"),
            ),
        ):
            asyncio.run(router_mod.handle_async(event=event, gateway=SimpleNamespace()))


def test_handle_async_short_circuits_reaction_removed_event():
    event = _reaction_event("reaction:removed:OK")

    with tempfile.TemporaryDirectory() as profile_home:
        with (
            patch(
                "hermes_multitenancy.router._resolve_or_auto_provision_route",
                return_value=("profile", Path(profile_home)),
            ),
            patch(
                "hermes_multitenancy.router._get_feishu_adapter",
                side_effect=AssertionError("reaction synthetic must not reach adapter dispatch"),
            ),
        ):
            asyncio.run(router_mod.handle_async(event=event, gateway=SimpleNamespace()))


def test_handle_async_keeps_regular_text_on_dispatch_path():
    event = _reaction_event("hello bot")
    adapter_seen: list[bool] = []
    run_request_seen: list[bool] = []

    def fake_adapter(gateway):
        del gateway
        adapter_seen.append(True)
        return None

    def fake_run_request(**kwargs):
        run_request_seen.append(kwargs["text"] == "hello bot")
        raise RuntimeError("stop after proving regular dispatch path")

    with tempfile.TemporaryDirectory() as profile_home:
        with (
            patch(
                "hermes_multitenancy.router._resolve_or_auto_provision_route",
                return_value=("profile", Path(profile_home)),
            ),
            patch("hermes_multitenancy.router._get_feishu_adapter", side_effect=fake_adapter),
            patch(
                "hermes_multitenancy.router._run_request_for_routed_event",
                side_effect=fake_run_request,
            ),
        ):
            asyncio.run(router_mod.handle_async(event=event, gateway=SimpleNamespace()))

    assert adapter_seen == [True]
    assert run_request_seen == [True]
