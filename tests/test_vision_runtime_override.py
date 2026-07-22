"""_install_vision_model_override patches the per-provider vision model for
accounts where the core's hardcoded choice is unavailable (zai glm-5v-turbo →
glm-4.6v), independently of whether a full main-runtime can be built, and
restores it afterwards. Uses a fake agent.auxiliary_client (no core in test env)."""
import sys
import types

from hermes_multitenancy import router


def _install_fake_aux(monkeypatch, vision_map):
    fake = types.ModuleType("agent.auxiliary_client")
    fake._PROVIDER_VISION_MODELS = dict(vision_map)
    parent = sys.modules.get("agent") or types.ModuleType("agent")
    monkeypatch.setitem(sys.modules, "agent", parent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake)
    return fake


def test_vision_override_patches_zai_and_restores(monkeypatch):
    fake = _install_fake_aux(monkeypatch, {"zai": "glm-5v-turbo", "xiaomi": "mimo-v2.5"})
    client, saved = router._install_vision_model_override("zai")
    assert fake._PROVIDER_VISION_MODELS["zai"] == "glm-4.6v"      # patched
    assert fake._PROVIDER_VISION_MODELS["xiaomi"] == "mimo-v2.5"  # untouched
    assert "_PROVIDER_VISION_MODELS" in saved
    router._restore_auxiliary_main_runtime_patch(client, saved)
    assert fake._PROVIDER_VISION_MODELS["zai"] == "glm-5v-turbo"  # restored


def test_vision_override_case_insensitive(monkeypatch):
    fake = _install_fake_aux(monkeypatch, {"zai": "glm-5v-turbo"})
    client, saved = router._install_vision_model_override("ZAI")
    assert fake._PROVIDER_VISION_MODELS["zai"] == "glm-4.6v"
    router._restore_auxiliary_main_runtime_patch(client, saved)


def test_vision_override_noop_for_unmapped(monkeypatch):
    fake = _install_fake_aux(monkeypatch, {"zai": "glm-5v-turbo"})
    client, saved = router._install_vision_model_override("anthropic")
    assert saved == {}
    assert fake._PROVIDER_VISION_MODELS == {"zai": "glm-5v-turbo"}


def test_context_manager_derives_provider_from_default_only_config(monkeypatch, tmp_path):
    """Codex-flagged shape: model.default == 'zai/glm-5.1' with NO provider field
    must still trigger the override (derive provider from the default prefix)."""
    import asyncio
    fake = _install_fake_aux(monkeypatch, {"zai": "glm-5v-turbo"})
    # no complete main-runtime (the early-return path the override must survive)
    monkeypatch.setattr(router, "_profile_main_runtime_for_image_prep", lambda _h: None)
    # default-only config (no model.provider)
    from hermes_multitenancy import agent_real
    monkeypatch.setattr(
        agent_real, "_load_profile_config",
        lambda _h: {"model": {"default": "zai/glm-5.1"}},
    )
    seen = {}

    async def drive():
        async with router._profile_image_prep_runtime(tmp_path):
            seen["inside"] = fake._PROVIDER_VISION_MODELS.get("zai")
        seen["after"] = fake._PROVIDER_VISION_MODELS.get("zai")

    asyncio.run(drive())
    assert seen["inside"] == "glm-4.6v"   # derived provider 'zai' → patched
    assert seen["after"] == "glm-5v-turbo"  # restored


def test_image_prep_runtime_uses_employee_key_without_callback_headers(monkeypatch, tmp_path):
    import asyncio
    from hermes_multitenancy import billing_identity

    fake = _install_fake_aux(monkeypatch, {})
    fake._RUNTIME_MAIN = {"previous": True}
    captured = {}

    def set_runtime_main(provider, model, **kwargs):
        captured.update({"provider": provider, "model": model, **kwargs})
        fake._RUNTIME_MAIN = dict(captured)

    fake.set_runtime_main = set_runtime_main
    monkeypatch.setattr(
        router,
        "_profile_main_runtime_for_image_prep",
        lambda _home: {
            "provider": "custom:litellm",
            "model": "vision-model",
            "base_url": "https://litellm.example/v1",
            "api_key": "shared-key",
            "api_mode": "openai",
        },
    )
    monkeypatch.setattr(
        billing_identity,
        "billing_runtime_for_image_prep",
        lambda _metadata: {
            "api_key": "employee-key",
            "base_url": "https://litellm.example/v1",
        },
    )

    async def drive():
        async with router._profile_image_prep_runtime(
            tmp_path,
            billing_metadata={
                "litellm_billing_enforced": True,
                "litellm_billing_user_id": "llm-actor",
                "litellm_billing_base_url": "https://litellm.example/v1",
            },
        ):
            assert fake._RUNTIME_MAIN["api_key"] == "employee-key"
            assert fake._RUNTIME_MAIN["request_overrides"] == {}
            assert fake._RUNTIME_MAIN["request_overrides_base_url"] == ""

    asyncio.run(drive())

    assert captured["api_key"] == "employee-key"
    assert fake._RUNTIME_MAIN == {"previous": True}
