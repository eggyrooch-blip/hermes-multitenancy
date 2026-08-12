from __future__ import annotations

from types import SimpleNamespace

from hermes_multitenancy.agent_real._core import _model_spec_for_event

DEFAULT = "custom:litellm-sre/tencent/claude-sonnet-5"
CONFIG = {
    "custom_providers": [
        {
            "name": "litellm-sre",
            "model": "tencent/claude-sonnet-5",
            "models": {"tencent/claude-sonnet-5": {"context_length": 256000}},
        }
    ]
}


def _event(metadata: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(raw_event={"metadata": metadata})


def test_delisted_session_model_falls_back_to_default() -> None:
    event = _event(
        {"provider": "custom:litellm-sre", "model": "tencent-sonnet-4-6"}
    )
    assert _model_spec_for_event(DEFAULT, event, CONFIG) == DEFAULT


def test_registered_session_model_is_kept() -> None:
    event = _event(
        {"provider": "custom:litellm-sre", "model": "tencent/claude-sonnet-5"}
    )
    assert (
        _model_spec_for_event(DEFAULT, event, CONFIG)
        == "custom:litellm-sre/tencent/claude-sonnet-5"
    )


def test_non_custom_provider_override_untouched() -> None:
    event = _event({"provider": "anthropic", "model": "claude-sonnet-4-5"})
    assert (
        _model_spec_for_event(DEFAULT, event, CONFIG)
        == "anthropic/claude-sonnet-4-5"
    )


def test_no_config_keeps_legacy_behavior() -> None:
    event = _event(
        {"provider": "custom:litellm-sre", "model": "tencent-sonnet-4-6"}
    )
    assert (
        _model_spec_for_event(DEFAULT, event)
        == "custom:litellm-sre/tencent-sonnet-4-6"
    )


def test_empty_registry_fails_closed_to_default() -> None:
    event = _event(
        {"provider": "custom:litellm-sre", "model": "tencent-sonnet-4-6"}
    )
    assert _model_spec_for_event(DEFAULT, event, {"custom_providers": []}) == DEFAULT


def test_stale_legacy_model_field_is_not_registered() -> None:
    config = {
        "custom_providers": [
            {
                "name": "litellm-sre",
                "model": "tencent-sonnet-4-6",
                "models": {"tencent/claude-sonnet-5": {}},
            }
        ]
    }
    event = _event(
        {"provider": "custom:litellm-sre", "model": "tencent-sonnet-4-6"}
    )
    assert _model_spec_for_event(DEFAULT, event, config) == DEFAULT
