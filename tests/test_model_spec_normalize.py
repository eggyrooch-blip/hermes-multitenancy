from __future__ import annotations

import textwrap
from pathlib import Path

from hermes_multitenancy.agent_real import (
    _load_profile_config,
    _normalize_model_spec_inplace,
    _split_model_spec,
)


def test_normalize_prepends_provider_to_bare_default():
    cfg = {"model": {"default": "tencent-sonnet-4-6", "provider": "custom:litellm-sre"}}
    _normalize_model_spec_inplace(cfg)
    assert cfg["model"]["default"] == "custom:litellm-sre/tencent-sonnet-4-6"
    # and it now parses (the root failure is impossible)
    assert _split_model_spec(cfg["model"]["default"]) == ("custom:litellm-sre", "tencent-sonnet-4-6")


def test_split_model_spec_removes_custom_transport_context_label_only_for_chat():
    assert _split_model_spec(
        "custom:litellm-sre/kimi/k3[1m]",
        strip_custom_context_suffix=True,
    ) == (
        "custom:litellm-sre",
        "kimi/k3",
    )
    assert _split_model_spec("custom:litellm-sre/kimi/k3[1m]") == (
        "custom:litellm-sre",
        "kimi/k3[1m]",
    )
    assert _split_model_spec("openrouter/vendor/model[1m]") == (
        "openrouter",
        "vendor/model[1m]",
    )
    assert _split_model_spec("custom:proxy/vendor/model[preview]") == (
        "custom:proxy",
        "vendor/model[preview]",
    )


def test_normalize_noop_when_already_prefixed():
    cfg = {"model": {"default": "custom:litellm-sre/tencent-sonnet-4-6", "provider": "custom:litellm-sre"}}
    _normalize_model_spec_inplace(cfg)
    assert cfg["model"]["default"] == "custom:litellm-sre/tencent-sonnet-4-6"


def test_normalize_noop_when_no_provider():
    # No provider to prepend -> leave as-is (don't guess); caller handles.
    cfg = {"model": {"default": "tencent-sonnet-4-6"}}
    _normalize_model_spec_inplace(cfg)
    assert cfg["model"]["default"] == "tencent-sonnet-4-6"


def test_normalize_handles_missing_or_malformed_model():
    for bad in ({}, {"model": None}, {"model": "x"}, {"model": {}}):
        _normalize_model_spec_inplace(dict(bad))  # must not raise


def _write(p: Path, body: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")


def test_load_profile_config_self_heals_bare_default(tmp_path: Path):
    # Reproduce the prod bug: a profile config on disk with a BARE default.
    shared = tmp_path  # _resolve_shared_hermes_home: profiles/<p> -> parent.parent
    profiles = shared / "profiles"
    prof = profiles / "victim"
    _write(shared / "config.yaml", "model:\n  default: tencent-sonnet-4-6\n  provider: custom:litellm-sre\n")
    _write(prof / "config.yaml", "model:\n  default: tencent-sonnet-4-6\n  provider: custom:litellm-sre\n")

    cfg = _load_profile_config(prof)
    # Runtime safety net heals it -> _split_model_spec can no longer raise on this turn.
    assert cfg["model"]["default"] == "custom:litellm-sre/tencent-sonnet-4-6"
    assert _split_model_spec(cfg["model"]["default"])[0] == "custom:litellm-sre"


def test_load_profile_config_heals_when_provider_inherited_from_shared(tmp_path: Path):
    # Profile-local has a BARE default and NO provider; provider is inherited from
    # shared config. The merged+normalized config must still self-heal (codex review).
    shared = tmp_path
    prof = shared / "profiles" / "inherits"
    _write(shared / "config.yaml", "model:\n  provider: custom:litellm-sre\n")
    _write(prof / "config.yaml", "model:\n  default: tencent-sonnet-4-6\n")  # no provider locally
    cfg = _load_profile_config(prof)
    assert cfg["model"]["default"] == "custom:litellm-sre/tencent-sonnet-4-6"
    assert _split_model_spec(cfg["model"]["default"])[0] == "custom:litellm-sre"


def test_load_profile_config_keeps_prefixed_profile(tmp_path: Path):
    shared = tmp_path
    prof = shared / "profiles" / "good"
    _write(shared / "config.yaml", "model:\n  default: tencent-sonnet-4-6\n  provider: custom:litellm-sre\n")
    _write(prof / "config.yaml", "model:\n  default: custom:litellm-sre/tencent-sonnet-4-6\n  provider: custom:litellm-sre\n")
    cfg = _load_profile_config(prof)
    assert cfg["model"]["default"] == "custom:litellm-sre/tencent-sonnet-4-6"
