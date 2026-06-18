"""Requirement-detection parity: registry wrapper == legacy detector.

``connectors.requirements.detect_requirements`` wraps
``credential_hub.detect_skill_requirements`` (Phase 1 introduces zero
requirement drift). This file proves that equality across a 7-skill fixture set
spanning single-connector, multi-connector, near-miss-no-trigger, and the
SkillHub ``source='hub'`` short-circuit.
"""
from __future__ import annotations

import pytest


def _legacy_ids(name, text, *, tags=(), source=None):
    """The legacy single-skill result, sorted (matches connectors_for ordering)."""
    from hermes_multitenancy import credential_hub

    skill = credential_hub._ProfileSkill(
        name=name, text=text, tags=list(tags), source=source
    )
    return sorted(credential_hub.detect_skill_requirements(skill))


# (name, text, tags, source, expected_legacy_ids) — expected is computed live so
# the test asserts registry==legacy rather than against a hand-frozen list.
_FIXTURES = [
    ("only-lark", "this skill uses lark-cli to read docs", (), None),
    ("only-keep-record", "keep_auth_token get_qrcode persist_auth", (), None),
    ("only-kep-cli", "kep-cli kep-auth --env online login", (), None),
    ("lark-and-keep-record", "lark-cli plus keep_auth_token get_qrcode", (), None),
    ("gitlab-token", "clone via oauth2:${gitlab_token}@host", (), None),
    ("plain-no-connector", "a perfectly innocuous helper that does math", (), None),
    # Near-miss words chosen so the word-boundary regexes do NOT fire:
    # "larkspur" != \blark[-_ ]?cli\b ; "keeping" != \bkeep-record\b / keep_auth_token.
    ("fuzzy-no-trigger", "larkspur flowers while keeping a tidy desk", (), None),
]


@pytest.mark.parametrize("name,text,tags,source", _FIXTURES)
def test_registry_detect_equals_legacy_per_skill(name, text, tags, source):
    from hermes_multitenancy.connectors import registry
    from hermes_multitenancy.connectors.requirements import SkillRequirementInput

    expected = _legacy_ids(name, text, tags=tags, source=source)
    result = registry.detect_requirements(
        [SkillRequirementInput(name=name, text=text, tags=tuple(tags), source=source)]
    )
    assert result.connectors_for(name) == expected


def test_fuzzy_skill_triggers_nothing():
    """Explicit guard: the near-miss fixture must map to zero connectors."""
    from hermes_multitenancy.connectors import registry
    from hermes_multitenancy.connectors.requirements import SkillRequirementInput

    name = "fuzzy-no-trigger"
    text = "larkspur flowers while keeping a tidy desk"
    assert _legacy_ids(name, text) == []
    result = registry.detect_requirements([SkillRequirementInput(name=name, text=text)])
    assert result.connectors_for(name) == []


def test_multi_connector_skill_aggregates_both(monkeypatch):
    from hermes_multitenancy.connectors import registry
    from hermes_multitenancy.connectors.requirements import SkillRequirementInput

    name = "lark-and-keep-record"
    text = "lark-cli plus keep_auth_token get_qrcode"
    result = registry.detect_requirements([SkillRequirementInput(name=name, text=text)])
    ids = result.connectors_for(name)
    assert "lark-cli" in ids
    assert "keep-record" in ids
    assert ids == _legacy_ids(name, text)


def test_skillhub_sourced_skill_requires_kep_cli_through_registry():
    """source='hub' short-circuits to kep-cli even with no kep text (TS parity)."""
    from hermes_multitenancy.connectors import registry
    from hermes_multitenancy.connectors.requirements import SkillRequirementInput

    result = registry.detect_requirements(
        [SkillRequirementInput(name="some-hub-skill", text="just a plain skill", source="hub")]
    )
    assert "kep-cli" in result.connectors_for("some-hub-skill")
