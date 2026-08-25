"""Phase 5 — Vision enrichment via hermes' vision_analyze_tool.

These tests assert behavior of ``router._enrich_with_vision`` without
hitting a real vision API — we monkeypatch the upstream tool so the test
runs offline and reproducibly.
"""
from __future__ import annotations

import json
import asyncio
import os
import sys
import types
from types import SimpleNamespace

import pytest


def _make_event(text="hi", media_urls=None, media_types=None, message_type="text"):
    return SimpleNamespace(
        text=text,
        media_urls=media_urls or [],
        media_types=media_types or [],
        message_type=message_type,
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
            captured["media_urls"] = list(event.media_urls)
            captured["message_type"] = event.message_type
            captured["source_user"] = source.user_id
            captured["history"] = history
            return "ENRICHED: " + event.text

    event = _make_event(text="hi from user", media_urls=["/tmp/x.png"], media_types=["image/png"])
    result = await _enrich_via_hermes_pipeline(event, FakeGateway())
    assert result == "ENRICHED: hi from user"
    assert captured["event_text"] == "hi from user"
    assert captured["media_urls"] == ["/tmp/x.png"]
    assert captured["message_type"] == "text"
    assert captured["history"] == []


@pytest.mark.asyncio
async def test_pipeline_scopes_custom_profile_runtime_for_gateway_image_prep(monkeypatch, tmp_path):
    """Feishu image pre-analysis must not pass Hermes custom model ids to LiteLLM."""
    from hermes_multitenancy.router import _enrich_via_hermes_pipeline

    shared_home = tmp_path / ".hermes"
    profile_home = shared_home / "profiles" / "owner"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "\n".join(
            [
                "model:",
                "  default: custom:litellm-sre/tencent-sonnet-4-6",
                "  provider: custom:litellm-sre",
                "  base_url: https://litellm.sre.example/v1",
                "custom_providers:",
                "- name: litellm-sre",
                "  base_url: https://litellm.sre.example/v1",
                "  api_key: sk-test",
                "  model: tencent-sonnet-4-6",
            ]
        ),
        encoding="utf-8",
    )

    fake_aux = types.ModuleType("agent.auxiliary_client")
    fake_aux._RUNTIME_MAIN_PROVIDER = ""
    fake_aux._RUNTIME_MAIN_MODEL = ""
    fake_aux._RUNTIME_MAIN_BASE_URL = ""
    fake_aux._RUNTIME_MAIN_API_KEY = ""
    fake_aux._RUNTIME_MAIN_API_MODE = ""
    runtime_calls = []

    def set_runtime_main(provider, model, *, base_url="", api_key="", api_mode=""):
        runtime_calls.append(
            {
                "provider": provider,
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
                "api_mode": api_mode,
            }
        )
        fake_aux._RUNTIME_MAIN_PROVIDER = provider
        fake_aux._RUNTIME_MAIN_MODEL = model
        fake_aux._RUNTIME_MAIN_BASE_URL = base_url
        fake_aux._RUNTIME_MAIN_API_KEY = api_key
        fake_aux._RUNTIME_MAIN_API_MODE = api_mode

    fake_aux.set_runtime_main = set_runtime_main
    agent_pkg = sys.modules.get("agent") or types.ModuleType("agent")
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake_aux)
    monkeypatch.setenv("HERMES_HOME", str(shared_home / "profiles" / "multitenancy_router"))

    class FakeGateway:
        async def _prepare_inbound_message_text(self, *, event, source, history):
            assert os.environ["HERMES_HOME"] == str(profile_home)
            assert fake_aux._RUNTIME_MAIN_PROVIDER == "custom:litellm-sre"
            assert fake_aux._RUNTIME_MAIN_MODEL == "tencent-sonnet-4-6"
            assert fake_aux._RUNTIME_MAIN_BASE_URL == "https://litellm.sre.example/v1"
            assert fake_aux._RUNTIME_MAIN_API_KEY == "sk-test"
            return "ENRICHED IMAGE"

    event = _make_event(text="", media_urls=[str(profile_home / "cache" / "images" / "x.jpg")], media_types=["image/jpeg"])
    result = await _enrich_via_hermes_pipeline(event, FakeGateway(), profile_home=profile_home)

    assert result == "ENRICHED IMAGE"
    assert runtime_calls == [
        {
            "provider": "custom:litellm-sre",
            "model": "tencent-sonnet-4-6",
            "base_url": "https://litellm.sre.example/v1",
            "api_key": "sk-test",
            "api_mode": "",
        }
    ]
    assert os.environ["HERMES_HOME"] == str(shared_home / "profiles" / "multitenancy_router")


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


def test_local_fallback_surfaces_vision_provider_failure(monkeypatch):
    """Screenshot failures must stay visible and recoverable, not disappear."""
    _install_fake_vision_module(
        monkeypatch,
        {
            "success": False,
            "error": "Error code: 429 - subscription does not include vision",
        },
    )
    from hermes_multitenancy.router import _local_enrich_with_vision_only

    event = _make_event(
        text="我该如何在这个web页面中给你发送截图呢?",
        media_urls=["/Users/dev/.hermes/profiles/coder/images/clip.png"],
        media_types=["image/png"],
    )
    result = asyncio.run(_local_enrich_with_vision_only(event))

    assert result is not None
    assert "Image analysis unavailable" in result
    assert "clip.png" in result
    assert "vision-capable model or permission is required" in result
    assert result.endswith("我该如何在这个web页面中给你发送截图呢?")


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
