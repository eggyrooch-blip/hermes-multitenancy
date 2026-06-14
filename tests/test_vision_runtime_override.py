"""The image-prep runtime patch must override the per-provider vision model for
accounts where the core's hardcoded choice is unavailable (zai glm-5v-turbo →
glm-4.6v), and restore it afterwards. Uses a fake agent.auxiliary_client module
(the multitenancy test env has no hermes-agent core), mirroring test_vision.py."""
import sys
import types

from hermes_multitenancy import router


def _install_fake_aux(monkeypatch, vision_map):
    fake = types.ModuleType("agent.auxiliary_client")
    for name in router._AUX_MAIN_RUNTIME_FIELDS:
        setattr(fake, name, "")
    fake._read_main_provider = lambda: ""
    fake._read_main_model = lambda: ""
    fake._PROVIDER_VISION_MODELS = dict(vision_map)
    parent = sys.modules.get("agent") or types.ModuleType("agent")
    monkeypatch.setitem(sys.modules, "agent", parent)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake)
    return fake


def test_runtime_patch_overrides_zai_vision_model_and_restores(monkeypatch):
    fake = _install_fake_aux(monkeypatch, {"zai": "glm-5v-turbo", "xiaomi": "mimo-v2.5"})
    client, saved = router._install_auxiliary_main_runtime_patch(
        {"provider": "zai", "model": "glm-5.1"}
    )
    assert fake._PROVIDER_VISION_MODELS["zai"] == "glm-4.6v"      # patched
    assert fake._PROVIDER_VISION_MODELS["xiaomi"] == "mimo-v2.5"  # untouched
    router._restore_auxiliary_main_runtime_patch(client, saved)
    assert fake._PROVIDER_VISION_MODELS["zai"] == "glm-5v-turbo"  # restored


def test_runtime_patch_noop_for_unmapped_provider(monkeypatch):
    fake = _install_fake_aux(monkeypatch, {"zai": "glm-5v-turbo"})
    client, saved = router._install_auxiliary_main_runtime_patch(
        {"provider": "anthropic", "model": "custom-model-a3"}
    )
    assert "_PROVIDER_VISION_MODELS" not in saved
    assert fake._PROVIDER_VISION_MODELS == {"zai": "glm-5v-turbo"}
    router._restore_auxiliary_main_runtime_patch(client, saved)
