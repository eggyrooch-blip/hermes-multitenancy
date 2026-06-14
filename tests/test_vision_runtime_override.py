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
