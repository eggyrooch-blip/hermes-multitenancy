"""Profile skill scan + the "skill → credential requirement" inference engine.

Mirrors the WebUI ``detectSkillCredentialRequirements`` semantics. None of the
helpers here call a monkeypatchable sibling across a module boundary, so they use
plain intra-module references; the functions that ARE patched by tests
(``scan_profile_skills``, ``_requirements_by_id``, ``_has_skill``,
``_kep_skill_env_policy``) are only ever invoked by callers that resolve them
through the package namespace.
"""
from __future__ import annotations
from hermes_multitenancy import credential_hub as _hub  # route patchable helpers via package namespace

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ._io import _read_small_text
from .model import FEISHU_PROJECT, GITLAB, KEEP_RECORD, KEP_CLI, LARK_CLI


@dataclass
class _ProfileSkill:
    name: str
    text: str
    tags: list[str]
    source: Optional[str] = None  # 'hub' when installed via SkillHub provenance


def _skillhub_installed_names(skills_root: Path) -> set[str]:
    """Names installed via SkillHub — mirrors readSkillHubInstalledNames."""
    names: set[str] = set()
    for rel in (Path(".hub") / "lock.json", Path(".hermes-skillhub.json")):
        raw = _hub._read_small_text(skills_root / rel)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        installed = data.get("installed")
        if isinstance(installed, dict):
            names.update(k for k in installed.keys() if k)
    return names


def scan_profile_skills(profile_dir: Path) -> list[_ProfileSkill]:
    """Walk ``<profile_dir>/skills`` for SKILL.md files (mirrors the WebUI scan)."""
    root = Path(profile_dir) / "skills"
    out: list[_ProfileSkill] = []
    if not root.is_dir():
        return out
    hub_names = _hub._skillhub_installed_names(root)

    def visit(d: Path, depth: int) -> None:
        if depth > 8:
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if name == "node_modules" or name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                visit(Path(entry.path), depth + 1)
                continue
            if name == "SKILL.md":
                text = _hub._read_small_text(Path(entry.path))
                if not text:
                    continue
                skill_name = _hub._parse_skill_name(text) or Path(entry.path).parent.name
                dir_name = Path(entry.path).parent.name
                source = "hub" if (skill_name in hub_names or dir_name in hub_names) else None
                out.append(_ProfileSkill(name=skill_name, text=text,
                                         tags=_hub._parse_skill_tags(text), source=source))

    visit(root, 0)
    return out


def _parse_skill_name(text: str) -> str:
    m = re.search(r'^name:\s*["\']?([^"\'\n]+)["\']?', text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _parse_skill_tags(text: str) -> list[str]:
    m = re.search(r"tags:\s*\[([^\]]+)\]", text, re.MULTILINE)
    if not m:
        return []
    return [t.strip().strip("\"'").lower() for t in m.group(1).split(",") if t.strip()]


def _is_resource_delivery_skill_name(name: str) -> bool:
    normalized = str(name or "").strip().lower()
    return normalized == "using-resource-delivery" or normalized.startswith("kep-trevi-")


def _kep_skill_env_policy(skills: list[_ProfileSkill]) -> tuple[str, tuple[str, ...]]:
    """Return the primary kep-cli env plus envs worth reporting.

    Resource-delivery skills default to ``--env pre`` for rehearsal. The
    connector row should therefore gate on pre instead of showing a misleading
    all-good online status, while still reporting online when present so the
    user can see what is and is not logged in.
    """
    target_env = "online"
    envs: set[str] = {"online"}
    for skill in skills:
        text = skill.text.lower()
        is_resource_delivery = _hub._is_resource_delivery_skill_name(skill.name)
        is_kep = (
            is_resource_delivery
            or skill.name == "kep-hades-cli"
            or "kep-cli" in skill.tags
            or "kep-auth" in text
        )
        if not is_kep:
            continue
        if is_resource_delivery or re.search(r"--env\s+pre\b", text) or "env_default: pre" in text:
            target_env = "pre"
            envs.add("pre")
        if re.search(r"--env\s+online\b", text):
            envs.add("online")
    ordered = tuple(env for env in ("pre", "online") if env in envs)
    return target_env, ordered


def _configured_domain_patterns(env_var: str) -> tuple["re.Pattern[str]", ...]:
    """Deployment-specific internal domains used for credential detection.

    Kept out of source so this plugin ships no site-specific hostnames. A
    deployment can restore host-based detection by exporting a comma-separated
    list, e.g. ``HERMES_MT_KEP_DOMAINS="kep.example.com,cms.example.com/aidock"``.
    Matched case-insensitively against the (already lower-cased) skill text.
    """
    raw = os.environ.get(env_var, "")
    return tuple(
        re.compile(re.escape(d.strip().lower()))
        for d in raw.split(",")
        if d.strip()
    )


def detect_skill_requirements(skill: _ProfileSkill) -> list[str]:
    """Which credential ids a skill needs (mirrors detectSkillCredentialRequirements)."""
    text = f"{skill.name}\n{chr(10).join(skill.tags)}\n{skill.text}".lower()
    source = str(skill.source or "").strip().lower()
    required: list[str] = []

    if any(p.search(text) for p in (
        re.compile(r"\blark[-_ ]?cli\b"),
        re.compile(r"\blarksuite\b"),
        re.compile(r"open\.feishu\.cn"),
        re.compile(r"feishu\.cn/(docx|docs|sheets|wiki|base|minutes|file)"),
        re.compile(r"\bwiki:wiki:readonly\b"),
        re.compile(r"\b(feishu|lark|larksuite)\b.{0,80}\b(docx|docs|base|sheets?|bitable|wiki)\b"),
        re.compile(r"\b(docx|docs|base|sheets?|bitable|wiki)\b.{0,80}\b(feishu|lark|larksuite)\b"),
        re.compile(r"\b(im:message|contact:user|drive:drive|wiki:wiki)\b"),
    )):
        required.append(LARK_CLI)

    if any(p.search(text) for p in (
        re.compile(r"\bmeegle\b"), re.compile(r"\bmeego\b"),
        re.compile(r"\bfeishu[-_ ]?project\b"), re.compile(r"project\.feishu\.cn"),
        re.compile("飞书项目"),
    )):
        required.append(FEISHU_PROJECT)

    # SkillHub-sourced skills always need kep-cli (parity with the TS hubSourced short-circuit).
    hub_sourced = source in ("hub", "aidock-skillhub")
    if hub_sourced or _hub._is_resource_delivery_skill_name(skill.name) or any(p.search(text) for p in (
        re.compile(r"\bkep[-_ ]?cli\b"), re.compile(r"\bkep[-_ ]?auth\b"),
        re.compile(r"\baidock\b"), re.compile(r"\bskillhub\b"),
        re.compile(r"\bkeep[-_ ]?login\b"), re.compile(r"\bproxy[-_ ]?cms\b"),
        re.compile(r"skill/zipfile"),
        re.compile(r"\bkep_profile\b"), re.compile(r"\bkep_no_auto_login\b"),
        *_hub._configured_domain_patterns("HERMES_MT_KEP_DOMAINS"),
    )):
        required.append(KEP_CLI)

    if any(p.search(text) for p in (
        re.compile(r"\bkeep-record\b"), re.compile(r"\bkeep_auth_token\b"),
        re.compile(r"\bget_qrcode\b"), re.compile(r"\bpersist_auth\b"),
    )):
        required.append(KEEP_RECORD)

    if any(p.search(text) for p in (
        re.compile(r"\bgitlab_token\b"),
        re.compile(r"oauth2:\$\{?gitlab_token\}?@"),
        *_hub._configured_domain_patterns("HERMES_MT_GITLAB_DOMAINS"),
    )):
        required.append(GITLAB)

    return required


def _requirements_by_id(skills: list[_ProfileSkill]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for skill in skills:
        for cid in _hub.detect_skill_requirements(skill):
            lst = out.setdefault(cid, [])
            if skill.name not in lst:
                lst.append(skill.name)
    for lst in out.values():
        lst.sort()
    return out


def _has_skill(
    skills: list[_ProfileSkill],
    *,
    name: str | None = None,
    tags: tuple[str, ...] = (),
    needles: tuple[str, ...] = (),
) -> bool:
    for skill in skills:
        if name and skill.name == name:
            return True
        if tags and any(t in skill.tags for t in tags):
            return True
        low = skill.text.lower()
        if needles and all(n in low for n in needles):
            return True
    return False
