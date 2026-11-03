"""The 08-12 registry self-heal is gone: WebUI model picks pass through.

The profile ``custom_providers`` registry is a stale single-model snapshot,
not a catalog, so validating against it silently demoted every non-default
model pick back to the profile default (slug
``webui-model-pick-registry-false-positive``).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

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


def test_unregistered_session_model_passes_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = _event({"provider": "custom:litellm-sre", "model": "GPT-5.5-standard"})
    with caplog.at_level(logging.WARNING):
        assert (
            _model_spec_for_event(DEFAULT, event, CONFIG)
            == "custom:litellm-sre/GPT-5.5-standard"
        )
    assert "custom_providers registry" not in caplog.text


def test_default_model_is_kept() -> None:
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
    event = _event({"provider": "custom:litellm-sre", "model": "tencent-sonnet-4-6"})
    assert (
        _model_spec_for_event(DEFAULT, event)
        == "custom:litellm-sre/tencent-sonnet-4-6"
    )


def test_empty_registry_still_passes_through() -> None:
    event = _event({"provider": "custom:litellm-sre", "model": "tencent-sonnet-4-6"})
    assert (
        _model_spec_for_event(DEFAULT, event, {"custom_providers": []})
        == "custom:litellm-sre/tencent-sonnet-4-6"
    )


def test_missing_model_returns_default() -> None:
    assert _model_spec_for_event(DEFAULT, _event({}), CONFIG) == DEFAULT


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({"model": "/"}, id="both-empty"),
        pytest.param({"model": "custom:litellm-sre/"}, id="empty-model"),
        pytest.param({"model": "/GPT-5.5-standard"}, id="empty-provider"),
    ],
)
def test_malformed_spec_falls_back_to_default(
    metadata: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Real dirty metadata, not a monkeypatched parser.

    An empty provider or model component must take the same warning/fallback
    branch as a spec that fails to parse at all, or ``run.py`` resolves an
    empty provider and surfaces ``Run is unavailable`` instead of the default.
    """
    with caplog.at_level(logging.WARNING):
        assert _model_spec_for_event(DEFAULT, _event(metadata), CONFIG) == DEFAULT
    assert "unparsable" in caplog.text


def test_whitespace_model_returns_default_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """All-whitespace model strips to empty before the parse, so it returns the
    default on the early ``if not model`` branch — no WARNING is expected."""
    event = _event({"provider": "custom:litellm-sre", "model": "   "})
    with caplog.at_level(logging.WARNING):
        assert _model_spec_for_event(DEFAULT, event, CONFIG) == DEFAULT
    assert caplog.text == ""
