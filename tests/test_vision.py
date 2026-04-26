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
async def test_no_media_returns_none():
    from hermes_multitenancy.router import _enrich_with_vision
    result = await _enrich_with_vision(_make_event(text="hi"))
    assert result is None


@pytest.mark.asyncio
async def test_image_description_prepended(monkeypatch):
    _install_fake_vision_module(monkeypatch, {
        "success": True,
        "analysis": "A cat sitting on a keyboard",
    })
    from hermes_multitenancy.router import _enrich_with_vision

    event = _make_event(
        text="what's this?",
        media_urls=["/tmp/cat.jpg"],
        media_types=["image/jpeg"],
    )
    result = await _enrich_with_vision(event)
    assert result is not None
    assert "A cat sitting on a keyboard" in result
    assert result.endswith("what's this?"), "user text should be appended after description"


@pytest.mark.asyncio
async def test_vision_failure_emits_diagnostic(monkeypatch):
    _install_fake_vision_module(monkeypatch, {
        "success": False,
        "error": "rate limited",
    })
    from hermes_multitenancy.router import _enrich_with_vision
    event = _make_event(text="ok", media_urls=["/tmp/x.png"], media_types=["image/png"])
    result = await _enrich_with_vision(event)
    assert result is not None
    assert "vision analysis failed" in result
    assert "rate limited" in result


@pytest.mark.asyncio
async def test_non_image_media_skipped(monkeypatch):
    _install_fake_vision_module(monkeypatch, {"success": True, "analysis": "should not be called"})
    from hermes_multitenancy.router import _enrich_with_vision

    event = _make_event(
        text="here's a doc",
        media_urls=["/tmp/file.pdf"],
        media_types=["application/pdf"],
    )
    result = await _enrich_with_vision(event)
    # No images → nothing enriched
    assert result is None


@pytest.mark.asyncio
async def test_vision_module_missing_returns_none(monkeypatch):
    """If hermes' vision tools aren't available, vision enrichment is silently skipped."""
    # Ensure no tools.vision_tools module exists
    monkeypatch.setitem(sys.modules, "tools.vision_tools", None)
    from hermes_multitenancy.router import _enrich_with_vision

    event = _make_event(text="x", media_urls=["/tmp/i.jpg"], media_types=["image/jpeg"])
    # The lazy import will hit the None entry and raise ImportError → caught → None
    result = await _enrich_with_vision(event)
    assert result is None
