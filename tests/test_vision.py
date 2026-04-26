"""Phase 5 — Vision enrichment via hermes' vision_analyze_tool.

These tests assert behavior of ``router._enrich_with_vision`` without
hitting a real vision API — we monkeypatch the upstream tool so the test
runs offline and reproducibly.
"""
from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest


def _make_event(text="hi", media_urls=None, media_types=None):
    return SimpleNamespace(
        text=text,
        media_urls=media_urls or [],
        media_types=media_types or [],
        reply_to_text=None,
        source=SimpleNamespace(
            chat_id="c", user_id="u", user_id_alt=None,
            user_name="t", chat_type="dm",
            platform=SimpleNamespace(value="feishu"),
        ),
    )


def _install_fake_vision_module(monkeypatch, fake_response):
    """Inject a fake `tools.vision_tools` module so the lazy import succeeds."""
    fake_mod = types.ModuleType("tools.vision_tools")
    async def fake_tool(*, image_url, user_prompt):
        return json.dumps(fake_response)
    fake_mod.vision_analyze_tool = fake_tool

    parent = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", parent)
    monkeypatch.setitem(sys.modules, "tools.vision_tools", fake_mod)


@pytest.mark.asyncio
async def test_pipeline_uses_gateway_prepare_when_available():
    """If gateway exposes _prepare_inbound_message_text, the plugin reuses it
    (covering vision + STT + file inject + reply context in one call)."""
    from hermes_multitenancy.router import _enrich_via_hermes_pipeline

    captured = {}

    class FakeGateway:
        async def _prepare_inbound_message_text(self, *, event, source, history):
            captured["event_text"] = event.text
            captured["source_user"] = source.user_id
            captured["history"] = history
            return "ENRICHED: " + event.text

    event = _make_event(text="hi from user", media_urls=["/tmp/x.png"], media_types=["image/png"])
    result = await _enrich_via_hermes_pipeline(event, FakeGateway())
    assert result == "ENRICHED: hi from user"
    assert captured["event_text"] == "hi from user"
    assert captured["history"] == []


@pytest.mark.asyncio
async def test_pipeline_falls_back_when_gateway_lacks_helper():
    """If gateway doesn't expose _prepare_inbound_message_text, fall back to
    the local vision-only enrichment so images still get described."""
    from hermes_multitenancy.router import _enrich_via_hermes_pipeline

    class BareGateway:  # no _prepare_inbound_message_text attribute
        pass

    event = _make_event(text="no media")
    result = await _enrich_via_hermes_pipeline(event, BareGateway())
    # No media → local fallback also returns None
    assert result is None


@pytest.mark.asyncio
async def test_local_fallback_image_only(monkeypatch):
    """Local fallback handles images even when hermes pipeline absent."""
    _install_fake_vision_module(monkeypatch, {"success": True, "analysis": "A cat"})
    from hermes_multitenancy.router import _local_enrich_with_vision_only

    event = _make_event(text="?", media_urls=["/tmp/cat.jpg"], media_types=["image/jpeg"])
    result = await _local_enrich_with_vision_only(event)
    assert result is not None
    assert "A cat" in result
    assert result.endswith("?")


@pytest.mark.asyncio
async def test_pipeline_fallback_on_gateway_exception(monkeypatch):
    """If gateway's helper raises, the plugin still tries local vision."""
    _install_fake_vision_module(monkeypatch, {"success": True, "analysis": "A cat"})
    from hermes_multitenancy.router import _enrich_via_hermes_pipeline

    class BoomGateway:
        async def _prepare_inbound_message_text(self, *, event, source, history):
            raise RuntimeError("hermes pipeline crashed")

    event = _make_event(text="?", media_urls=["/tmp/cat.jpg"], media_types=["image/jpeg"])
    result = await _enrich_via_hermes_pipeline(event, BoomGateway())
    assert result is not None
    assert "A cat" in result


@pytest.mark.asyncio
async def test_pipeline_with_no_gateway_returns_none():
    from hermes_multitenancy.router import _enrich_via_hermes_pipeline
    assert await _enrich_via_hermes_pipeline(_make_event(), None) is None
