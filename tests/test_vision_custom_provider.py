"""`_install_vision_task_endpoint_override` forces the vision auxiliary task
at a profile's custom (litellm) endpoint, regardless of what ``provider``
the caller passes to `resolve_vision_provider_client` — fixing the
auto-detect-by-name chain, which fails for named custom providers.

Root cause (live-reproduced against a real custom-provider profile):
`_resolve_task_provider_model("vision", ...)` only reads
`auxiliary.vision.*` config (unset for a plain profile). Its own
`if provider: return provider, ...` short-circuit means that ANY caller
passing an explicit `provider` — including the literal string `"auto"`,
which `resolve_vision_provider_client`'s own aggregator-fallback and
`vision_tools.check_vision_requirements()` both do — skips the per-task
config branch entirely, so patching config alone doesn't reach every
caller. The vision auto-detect chain then calls
`resolve_provider_client("custom:litellm-sre", ..., is_vision=True)` *by
name*, which doesn't match any registered `custom_providers` entry, logs
"unknown provider", and falls through to the (unavailable) openrouter/nous
aggregators → `client=None` → upstream raises
`RuntimeError: No LLM provider configured for task=vision provider=auto`.

Confirmed empirically against the real hermes-agent core
(agent/auxiliary_client.py): `_resolve_task_provider_model`'s `if base_url:`
check runs *before* its `if provider:` check, so injecting base_url
(regardless of the incoming `provider` value) forces the explicit-base_url
branch unconditionally. This is why the fix wraps
`resolve_vision_provider_client` itself rather than the config getter.

Uses a fake `agent.auxiliary_client` (no core in test env, matching the
existing `test_vision_runtime_override.py` / `test_vision.py` convention)
with a faithful subset of the real `_resolve_task_provider_model` /
`resolve_vision_provider_client` logic — just enough to prove the patch
mechanism without duplicating the whole provider router.
"""
from __future__ import annotations

import asyncio
import sys
import types

from hermes_multitenancy import router


class _FakeClient:
    def __init__(self, base_url):
        self.base_url = base_url


_RUNTIME = {
    "provider": "custom:litellm-sre",
    "model": "tencent-sonnet-4-6",
    "base_url": "https://litellm.sre.gotokeep.com/v1",
    "api_key": "sk-test",
    "api_mode": "",
}


def _install_fake_aux(monkeypatch):
    fake = types.ModuleType("agent.auxiliary_client")
    fake._PROVIDER_VISION_MODELS = {}

    def _resolve_task_provider_model(task=None, provider=None, model=None, base_url=None, api_key=None):
        # Faithful subset of the real function: base_url wins over an
        # explicit provider (checked FIRST in the real core), forcing
        # provider="custom". No base_url + no provider + unconfigured
        # auxiliary.<task> → "auto" with nothing to build a client from.
        if base_url:
            return "custom", model, base_url, api_key, None
        if provider:
            return provider, model, base_url, api_key, None
        return "auto", model, None, None, None

    def resolve_vision_provider_client(provider=None, model=None, *, base_url=None, api_key=None, async_mode=False):
        requested, resolved_model, resolved_base_url, _resolved_api_key, _api_mode = fake._resolve_task_provider_model(
            "vision", provider, model, base_url, api_key
        )
        if resolved_base_url:
            return requested, _FakeClient(resolved_base_url), resolved_model
        # No base_url resolved: real core falls through the openrouter/nous
        # aggregators, which are also unavailable in the reproduced bug →
        # None (matches the observed prod symptom).
        return requested, None, None

    fake._resolve_task_provider_model = _resolve_task_provider_model
    fake.resolve_vision_provider_client = resolve_vision_provider_client

    parent = sys.modules.get("agent") or types.ModuleType("agent")
    monkeypatch.setitem(sys.modules, "agent", parent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake)
    return fake


def test_vision_endpoint_override_fixes_explicit_auto_call(monkeypatch):
    """Mirrors the exact reproduction probe: `resolve_vision_provider_client(
    provider="auto", ...)` — the case config-only patching cannot reach,
    since `provider="auto"` short-circuits before any config lookup."""
    fake = _install_fake_aux(monkeypatch)

    # FAILS WITHOUT THE FIX: mirrors the reproduced prod bug (client=None).
    # Deleting `_install_vision_task_endpoint_override`'s call in
    # `_profile_image_prep_runtime` (or the function itself) collapses this
    # test to only the "before"/"restored" assertions passing.
    _, client_before, _ = fake.resolve_vision_provider_client(provider="auto", async_mode=True)
    assert client_before is None

    client, saved = router._install_vision_task_endpoint_override(_RUNTIME)
    try:
        _, client_after, _ = fake.resolve_vision_provider_client(provider="auto", async_mode=True)
        assert client_after is not None
        assert client_after.base_url == _RUNTIME["base_url"]
    finally:
        router._restore_auxiliary_main_runtime_patch(client, saved)

    _, client_restored, _ = fake.resolve_vision_provider_client(provider="auto", async_mode=True)
    assert client_restored is None


def test_vision_endpoint_override_fixes_default_provider_call(monkeypatch):
    """Also covers the real production path: `async_call_llm(task="vision")`
    calls `resolve_vision_provider_client(provider=None, ...)` internally
    when the caller supplied no override."""
    fake = _install_fake_aux(monkeypatch)
    client, saved = router._install_vision_task_endpoint_override(_RUNTIME)
    try:
        _, client_after, _ = fake.resolve_vision_provider_client(async_mode=True)
        assert client_after is not None
        assert client_after.base_url == _RUNTIME["base_url"]
    finally:
        router._restore_auxiliary_main_runtime_patch(client, saved)


def test_vision_endpoint_override_respects_caller_supplied_base_url(monkeypatch):
    """A caller that already knows its own endpoint (e.g. a future explicit
    per-task override) must not be clobbered by the profile's runtime."""
    fake = _install_fake_aux(monkeypatch)
    client, saved = router._install_vision_task_endpoint_override(_RUNTIME)
    try:
        _, client_after, _ = fake.resolve_vision_provider_client(
            base_url="https://caller-owned.example/v1", async_mode=True
        )
        assert client_after.base_url == "https://caller-owned.example/v1"
    finally:
        router._restore_auxiliary_main_runtime_patch(client, saved)


def test_vision_endpoint_override_noop_for_non_custom_provider(monkeypatch):
    _install_fake_aux(monkeypatch)
    client, saved = router._install_vision_task_endpoint_override(
        {"provider": "zai", "model": "glm-4.6", "base_url": "", "api_key": "", "api_mode": ""}
    )
    assert client is None
    assert saved == {}


def test_vision_endpoint_override_noop_for_empty_runtime(monkeypatch):
    _install_fake_aux(monkeypatch)
    client, saved = router._install_vision_task_endpoint_override(None)
    assert client is None
    assert saved == {}


def test_context_manager_installs_and_restores_vision_endpoint_override(monkeypatch, tmp_path):
    """End-to-end through `_profile_image_prep_runtime`: a custom-provider
    profile's runtime scopes the vision task's endpoint for the duration of
    the `async with` block and restores it afterwards."""
    fake = _install_fake_aux(monkeypatch)
    monkeypatch.setattr(router, "_profile_main_runtime_for_image_prep", lambda _h: dict(_RUNTIME))

    seen = {}

    async def drive():
        async with router._profile_image_prep_runtime(tmp_path):
            _, client, _ = fake.resolve_vision_provider_client(provider="auto", async_mode=True)
            seen["inside_base_url"] = client.base_url if client else None
        _, client_after, _ = fake.resolve_vision_provider_client(provider="auto", async_mode=True)
        seen["after_base_url"] = client_after.base_url if client_after else None

    asyncio.run(drive())
    assert seen["inside_base_url"] == _RUNTIME["base_url"]
    assert seen["after_base_url"] is None
