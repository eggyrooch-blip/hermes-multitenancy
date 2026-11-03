from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_multitenancy.agent_real._core import _model_spec_for_event


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            {"provider": "custom:litellm-sre", "model": "kimi/k3"},
            "custom:litellm-sre/kimi/k3",
        ),
        (
            {"provider": "anthropic", "model": "claude-sonnet-4-5"},
            "anthropic/claude-sonnet-4-5",
        ),
        (
            {"model": "custom:litellm-sre/kimi/k3"},
            "custom:litellm-sre/kimi/k3",
        ),
        (
            {
                "provider": "custom:litellm-sre",
                "model": "custom:litellm-sre/kimi/k3",
            },
            "custom:litellm-sre/kimi/k3",
        ),
    ],
)
def test_model_spec_for_event_preserves_provider_and_model_boundaries(
    metadata: dict[str, str],
    expected: str,
) -> None:
    event = SimpleNamespace(raw_event={"metadata": metadata})

    assert _model_spec_for_event("openai/profile-default", event) == expected
