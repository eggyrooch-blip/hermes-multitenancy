"""Tests for vision auxiliary-model injection (_normalize_vision_aux_inplace).

Root cause: core vision auto-detect hardcodes glm-5v-turbo for zai, which our
z.ai plan does not include (HTTP 429 code 1311). We inject auxiliary.vision to a
plan-available model (glm-4.6v) at config read time.
"""
from hermes_multitenancy.agent_real import _normalize_vision_aux_inplace


def test_zai_profile_gets_glm46v_injected():
    cfg = {"model": {"provider": "zai", "default": "zai/glm-5.1"}}
    _normalize_vision_aux_inplace(cfg)
    assert cfg["auxiliary"]["vision"]["model"] == "glm-4.6v"
    assert cfg["auxiliary"]["vision"]["provider"] == "zai"


def test_zai_forces_provider_even_when_existing_is_auto():
    # The default config often ships auxiliary.vision.provider == "auto"; leaving
    # it lets core auto-detect re-pick the broken glm-5v-turbo, so we must force it.
    cfg = {
        "model": {"provider": "zai", "default": "zai/glm-5.1"},
        "auxiliary": {"vision": {"provider": "auto", "timeout": 120}},
    }
    _normalize_vision_aux_inplace(cfg)
    assert cfg["auxiliary"]["vision"]["provider"] == "zai"
    assert cfg["auxiliary"]["vision"]["model"] == "glm-4.6v"
    assert cfg["auxiliary"]["vision"]["timeout"] == 120  # preserves other keys


def test_explicit_vision_model_override_is_respected():
    cfg = {
        "model": {"provider": "zai", "default": "zai/glm-5.1"},
        "auxiliary": {"vision": {"provider": "zai", "model": "glm-4.5v"}},
    }
    _normalize_vision_aux_inplace(cfg)
    assert cfg["auxiliary"]["vision"]["model"] == "glm-4.5v"  # untouched


def test_non_mapped_provider_is_not_injected():
    cfg = {"model": {"provider": "anthropic", "default": "anthropic/custom-model-a3"}}
    _normalize_vision_aux_inplace(cfg)
    assert "auxiliary" not in cfg or "vision" not in cfg.get("auxiliary", {})


def test_no_model_section_is_noop():
    cfg = {"foo": "bar"}
    _normalize_vision_aux_inplace(cfg)
    assert cfg == {"foo": "bar"}
