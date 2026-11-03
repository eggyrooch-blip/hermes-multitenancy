"""Connector requirement detection — the eventual source of truth.

Phase 1 keeps behavior IDENTICAL to the existing Python detection: this module
delegates to ``credential_hub.detect_skill_requirements`` so the registry never
changes which connectors a skill is reported to need. The convergence with the
TS ``detectSkillCredentialRequirements`` (moving any TS-correct behavior into a
registry fixture before flipping WebUI traffic) is a later phase — see the Phase
0 parity table in ``docs/connector-registry/requirement-parity.md``.

Detection precedence the registry will grow into (Phase 3):
  1. explicit ``requires_connectors`` manifest/frontmatter   (not yet)
  2. existing tags/frontmatter                                (via legacy)
  3. legacy regex fallback                                    (THIS module today)
Only step 3 is active in Phase 1; steps 1-2 land with skill manifests later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class SkillRequirementInput:
    """A skill's text used for requirement detection (parity with the TS input)."""

    name: str = ""
    text: str = ""
    tags: tuple[str, ...] = ()
    source: Optional[str] = None


@dataclass
class ConnectorRequirementSet:
    """Which skills require which connector ids, keyed by connector id."""

    by_connector: dict[str, list[str]] = field(default_factory=dict)

    def connectors_for(self, skill_name: str) -> list[str]:
        return sorted(cid for cid, names in self.by_connector.items() if skill_name in names)


def detect_for_skill(skill: SkillRequirementInput) -> list[str]:
    """Connector ids a single skill needs — delegates to the legacy detector.

    Wrapping (not reimplementing) is deliberate: it makes the registry's
    detection provably equal to ``credential_hub.detect_skill_requirements`` so
    Phase 1 introduces zero requirement drift. The parity test asserts this
    equality across the cross-repo fixture.
    """
    from .. import credential_hub

    legacy_skill = credential_hub._ProfileSkill(
        name=skill.name,
        text=skill.text,
        tags=list(skill.tags),
        source=skill.source,
    )
    return credential_hub.detect_skill_requirements(legacy_skill)


def detect_requirements(skills: list[SkillRequirementInput]) -> ConnectorRequirementSet:
    """Aggregate connector requirements across a profile's skills."""
    by_connector: dict[str, list[str]] = {}
    for skill in skills:
        for cid in detect_for_skill(skill):
            names = by_connector.setdefault(cid, [])
            if skill.name not in names:
                names.append(skill.name)
    for names in by_connector.values():
        names.sort()
    return ConnectorRequirementSet(by_connector=by_connector)
