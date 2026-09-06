"""Expert-mode overlay — session-scoped domain guidance.

When a Hermes run carries an ``expert_id`` (e.g. the WebUI's expert square hands
the conversation to "资源投放专家"), this module resolves that expert's persona
and builds an **expert-mode block** that is injected into the run's *system*
layer for THIS run only.

Design redlines (cross-model reviewed — see this slug's SPEC):
  * The overlay is EPHEMERAL — it is never written to ``SOUL.md``, memory, or
    ``USER.md``.  The profile's default agent persona on disk is untouched; the
    override lives only in the in-flight request and is handed to hermes-agent
    through its upstream ``ephemeral_system_prompt`` constructor seam (present in
    NousResearch origin/main — re-appended into the system message every turn at
    API-call time, never written to the cached/DB-stored prompt or trajectories).
    ZERO hermes-agent change is required.
  * Expert selection is built in the MULTITENANCY layer. hermes-agent core only
    exposes generic runtime seams; it does not know expert catalogs or tenants.
  * Credentials are unchanged: the caller's per-profile kep-auth tokens still
    apply. Expert skills stay installed as plugin resources and are only made
    visible for the active run; identity-of-record and credentials are not shared.
  * Fail-safe: an unknown / missing ``expert_id`` resolves to ``None`` and the run
    proceeds with the normal SOUL persona — this module never raises into the run.

Composition: the expert-mode block = Hermes host context + expert ``agent.md`` +
the existing credential/write safety reminder. The AIAgent path passes it via
``ephemeral_system_prompt``; the legacy/stream paths compose it override-first
into the system text (``_compose_system_text``). The overlay stays instruction-tier
instead of being appended as user/context data, while presenting the expert as a
Hermes-hosted mode rather than attempting to override the platform identity.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Mirrors plugin_ingest.MANAGED_DIR — the dir of per-plugin managed manifests.
MANAGED_DIR = ".hermes-plugin-managed"


# Keep Hermes as the trusted host. Adversarial "ignore/override/deny identity"
# wording is indistinguishable from prompt injection and causes correct models
# to reject the selected expert instead of using it.
ROLE_OVERRIDE_PREAMBLE = (
    "## Hermes 专家模式\n"
    "本次对话已由用户选择下方专家。你仍由 Hermes 提供，并以该专家的名称、职责和能力"
    "帮助用户完成领域任务："
)

ROLE_OVERRIDE_TAIL = (
    "当用户问「你是谁」或你的角色时，先说明你是上述专家，并可说明这是 Hermes 提供的"
    "专家模式。凭证始终属于当前用户本人；所有高风险写操作仍需按上述门禁取得显式确认。"
)


@dataclass
class ExpertOverlay:
    """Resolved, ready-to-compose expert persona for one run."""

    expert_id: str
    name: str
    agent_md: str
    plugin_id: str = ""
    skills: list[str] = field(default_factory=list)
    skill_dirs: list[Path] = field(default_factory=list)
    governance: dict[str, Any] = field(default_factory=dict)
    audience: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "expert_id": self.expert_id,
            "name": self.name,
            "plugin_id": self.plugin_id,
            "skills": list(self.skills),
            "governance": dict(self.governance),
            "audience": dict(self.audience),
        }


# ─────────────────────────── shared-home resolution ──────────────────────────

def _shared_home_for(profile_home: Path) -> Path:
    """Return the Hermes root that owns the cross-profile managed manifests.

    Mirrors ``agent_real._resolve_shared_hermes_home`` without importing it (keeps
    this module import-light and testable in isolation): honor ``HERMES_SHARED_HOME``
    then climb out of ``<root>/profiles/<id>`` to ``<root>``.
    """
    explicit = os.getenv("HERMES_SHARED_HOME")
    if explicit:
        return Path(explicit).expanduser()
    profile_home = Path(profile_home).expanduser()
    if profile_home.parent.name == "profiles":
        return profile_home.parent.parent
    return profile_home


def _managed_manifest_dirs(profile_home: Path) -> list[Path]:
    """Dirs that may hold ``.hermes-plugin-managed/*.json`` manifests.

    ONLY the shared root — where the ingester / skillhub installer write — is a
    trusted manifest source. The per-``PROFILE_HOME`` dir is deliberately NOT
    read: PROFILE_HOME is bwrap RW-mounted (tenant-writable), so honoring a
    manifest there let a tenant plant a forged expert (empty audience → bypass
    the audience gate) with an attacker-chosen ``repo``/``agent_md`` that
    ``_read_agent_md`` would then read PARENT-side (gateway, outside the sandbox)
    — a host-file disclosure. Prod manifests live only in shared-home; the
    per-profile layout was an unused "future" hook. If per-profile manifests are
    ever needed, store them outside the RW mount and validate repo against a
    trusted registry — never re-add PROFILE_HOME here.
    """
    profile_home = Path(profile_home).expanduser()
    return [_shared_home_for(profile_home) / MANAGED_DIR]


def _iter_managed_manifests(profile_home: Path, *, strict: bool = False):
    """Yield ``(manifest_dict, manifest_path)`` for every readable managed manifest."""
    seen: set[Path] = set()
    for d in _managed_manifest_dirs(profile_home):
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.json")):
            rp = path.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                if strict:
                    raise
                logger.debug("[multitenancy] skip unreadable managed manifest %s", path, exc_info=True)
                continue
            if isinstance(data, dict):
                yield data, path


def _manifest_is_active(manifest: dict[str, Any]) -> bool:
    return str(manifest.get("status") or "active").strip().lower() == "active"


# ─────────────────────────── agent.md resolution ─────────────────────────────

def _read_agent_md(manifest: dict[str, Any], expert: dict[str, Any]) -> Optional[str]:
    """Load the expert's persona markdown from the plugin repo.

    ``expert['agent_md']`` is a repo-relative path; the managed manifest records the
    plugin ``repo`` (persisted by the ingester). The path is constrained to stay
    inside the repo (an untrusted manifest must not read arbitrary files).
    """
    rel = str(expert.get("agent_md") or "").strip()
    if not rel:
        return None
    repo_raw = str(manifest.get("repo") or "").strip()
    if not repo_raw:
        return None
    repo = Path(repo_raw).expanduser()
    # Strip a leading "./" and reject absolute / traversal paths.
    rel_clean = rel[2:] if rel.startswith("./") else rel
    candidate = (repo / rel_clean)
    try:
        resolved = candidate.resolve()
        repo_resolved = repo.resolve()
        # Defense-in-depth: a repo root of "/" (or any filesystem root) makes the
        # commonpath containment check below vacuously true, turning agent_md into
        # an arbitrary-host-file read. A real plugin repo is never the fs root.
        if repo_resolved.parent == repo_resolved:
            logger.warning("[multitenancy] expert repo root is a filesystem root; refusing: %r", repo_raw)
            return None
        if os.path.commonpath([str(resolved), str(repo_resolved)]) != str(repo_resolved):
            logger.warning("[multitenancy] expert agent_md escapes repo: %r", rel)
            return None
    except (OSError, ValueError):
        return None
    try:
        text = resolved.read_text(encoding="utf-8").strip()
    except OSError:
        logger.debug("[multitenancy] expert agent_md unreadable %s", resolved, exc_info=True)
        return None
    return text or None


def _repo_root(manifest: dict[str, Any]) -> Optional[Path]:
    repo_raw = str(manifest.get("repo") or "").strip()
    if not repo_raw:
        return None
    try:
        return Path(repo_raw).expanduser().resolve()
    except OSError:
        return None


def _expert_skill_dirs(manifest: dict[str, Any], skills: list[str]) -> list[Path]:
    """Return declared expert skill directories, constrained to the plugin repo."""
    repo = _repo_root(manifest)
    if repo is None:
        return []
    out: list[Path] = []
    seen: set[Path] = set()
    for skill in skills:
        rel = str(skill or "").strip()
        if not rel:
            continue
        candidate = repo / "skills" / rel
        try:
            resolved = candidate.resolve()
            if os.path.commonpath([str(resolved), str(repo)]) != str(repo):
                logger.warning("[multitenancy] expert skill path escapes repo: %r", rel)
                continue
        except (OSError, ValueError):
            continue
        if resolved in seen:
            continue
        if (resolved / "SKILL.md").is_file():
            seen.add(resolved)
            out.append(resolved)
    return out


# ─────────────────────────── public API ──────────────────────────────────────

def _caller_profile_name(profile_home: Path) -> str:
    """Derive the CALLER's profile_name from its on-disk home.

    Convention (``router._profile_name_to_home``): a profile lives at
    ``<root>/profiles/<profile_name>``, so the directory name IS the profile name.
    Used to enforce a manifest's ``audience.profiles`` against the ACTIVATING
    caller — without it, a known expert id is activatable by any profile.
    """
    return Path(profile_home).expanduser().name


# ─────────────────────────── group-agent owner mirroring ─────────────────────
# A group/agent profile (Feishu group or WebUI group-chat agent) is not an
# employee: it has no org-snapshot record (department-scoped audiences fail
# closed) and profile-scoped audiences name the owner's PERSONAL profile, never
# the group profile dir. Per sunke's 2026-08-24 decision the group agent
# mirrors its OWNER's expert entitlements (visibility + activation + skills,
# credentials excluded), so every audience check ALSO admits the group's owner.
#
# SECURITY (F1): the owner identity is read ONLY from the TRUSTED routing table
# (multitenancy.db), NEVER from ``group_profile.json``. That marker lives in
# tenant-writable profile storage — bwrap binds ``PROFILE_HOME`` read-WRITE and
# does not mask the marker — so any agent run with a file tool can forge one.
# Reading the owner from the marker would let any profile (a plain personal
# profile included) plant ``{"owner_open_id": "<victim>"}`` and inherit that
# employee's expert visibility, activation, persona, and skill materialization:
# a cross-tenant authorization bypass. The routing DB is deliberately NOT mounted
# into the sandbox and records the owner written at provisioning from a trusted
# signal (Feishu ``bot_added`` inviter / owner-asserted WebUI agent creation), so
# it is the only safe source for OWNER identity. An unresolvable owner (no
# routing table, a non-mirroring ``kind``, or an empty ``owner_open_id``) keeps
# the fail-closed personal identity.
#
# CAVEAT (tracked separately, pre-existing — see this slug's DEBT.md "manifest
# source"): this hardens only OWNER resolution. Expert DEFINITIONS (audience,
# agent_md) are still read by ``_iter_managed_manifests`` from BOTH shared-home
# and the tenant-writable ``<PROFILE_HOME>/.hermes-plugin-managed`` — a separate
# trust hole not introduced here and out of this task's scope.


def _routing_row_for_profile(profile_home: Path):
    """Trusted routing row for this profile, or None. Never raises.

    Function-local import keeps ``expert_overlay`` import-light and avoids a
    module-load cycle with ``router`` (same lazy-import style feishu_org uses).
    """
    try:
        from . import router as router_mod

        table = router_mod._get_routing_table()
        if table is None:
            return None
        return table.lookup_by_profile_name(Path(profile_home).expanduser().name)
    except Exception:
        logger.debug("[multitenancy] routing lookup failed for %s", profile_home, exc_info=True)
        return None


# Routing kinds whose rows carry a trusted ``owner_open_id`` and therefore
# mirror that owner's expert entitlements: Feishu group chats (``group``, keyed
# by chat_id) and WebUI-owned agents (``agent``, keyed by agent_id). ``user``
# rows never mirror. Both are provisioned server-side from a trusted signal
# (bot_added inviter / owner-asserted agent creation), so the owner they record
# is authoritative — unlike the tenant-writable ``group_profile.json`` marker.
_OWNER_MIRRORING_KINDS = frozenset({"group", "agent"})


def _routing_group_owner_open_id(profile_home: Path) -> Optional[str]:
    """The group/agent profile's OWNER open_id from the TRUSTED routing table.

    Covers BOTH mirroring kinds — Feishu ``group`` and WebUI ``agent`` — since
    the SPEC Done line spans both surfaces. Returns None for a ``user`` profile
    (or any non-mirroring kind), a row with no owner, or when routing is
    unavailable (e.g. the sandboxed child) — owner-mirroring then does not apply.
    NEVER reads the tenant-writable ``group_profile.json`` (security F1).
    """
    row = _routing_row_for_profile(profile_home)
    if row is None:
        return None
    if str(getattr(row, "kind", "") or "").strip().lower() not in _OWNER_MIRRORING_KINDS:
        return None
    oid = str(getattr(row, "owner_open_id", "") or "").strip()
    return oid or None


def _group_owner_profile_name(profile_home: Path) -> Optional[str]:
    """The OWNER's personal profile_name for a group profile, from routing."""
    owner_oid = _routing_group_owner_open_id(profile_home)
    if not owner_oid:
        return None
    try:
        from . import router as router_mod

        table = router_mod._get_routing_table()
        if table is None:
            return None
        owner = table.resolve_owner_root(owner_oid) or table.lookup_by_open_id(owner_oid)
    except Exception:
        logger.debug("[multitenancy] owner profile lookup failed for %s", profile_home, exc_info=True)
        return None
    if owner is None:
        return None
    pname = str(getattr(owner, "profile_name", "") or "").strip()
    return pname or None


def _audience_profile_names(profile_home: Path) -> set[str]:
    """profile_names an audience check admits for this caller.

    The profile's OWN dir name is always included so a profile-mode audience
    naming the group directly still matches (F2 — union, not replace); a group
    profile ADDS its trusted owner's personal profile name so it mirrors the
    owner's profile-scoped grants.
    """
    names = {_caller_profile_name(profile_home)}
    owner_pname = _group_owner_profile_name(profile_home)
    if owner_pname:
        names.add(owner_pname)
    return names


def resolve_expert(
    profile_home: Path,
    expert_id: str,
    *,
    department_ids: Optional[list[str]] = None,
) -> Optional[ExpertOverlay]:
    """Resolve ``expert_id`` to a ready ``ExpertOverlay`` for ``profile_home``.

    Returns ``None`` (never raises) when the id is empty / unknown, its persona
    markdown can't be loaded, OR the manifest audience does not admit the caller —
    the caller then runs with the normal SOUL persona.

    Authorization (fail-CLOSED): an expert is activatable ONLY when the EFFECTIVE
    audience admits this caller — the PLUGIN INSTALL audience (``manifest["audience"]``,
    the PRIMARY gate since managed manifests are shared-home GLOBAL) ANDed with the
    OPTIONAL per-expert ``expert["audience"]`` (further narrowing). An expert with no
    per-expert audience INHERITS the install audience; it is public only when BOTH are
    empty. ``profiles[]`` is matched against the caller's profile_name (derived from
    ``profile_home``); ``department_ids`` is matched against the caller's departments
    (resolved server-side). An expert whose effective audience excludes the caller
    returns ``None`` even when its id is known.
    """
    eid = str(expert_id or "").strip()
    if not eid:
        return None
    resolved: Optional[ExpertOverlay] = None
    try:
        for manifest, expert in authorized_expert_records(
            profile_home, eid, department_ids=department_ids
        ):
            agent_md = _read_agent_md(manifest, expert)
            if not agent_md:
                logger.warning(
                    "[multitenancy] expert %r found but persona markdown missing/unreadable", eid
                )
                return None
            skills = expert.get("skills")
            gov = expert.get("governance")
            aud = expert.get("audience")
            skill_names = [str(s) for s in skills] if isinstance(skills, list) else []
            overlay = ExpertOverlay(
                expert_id=eid,
                name=str(expert.get("name") or expert.get("title") or eid),
                agent_md=agent_md,
                plugin_id=str(manifest.get("plugin_id") or ""),
                skills=skill_names,
                skill_dirs=_expert_skill_dirs(manifest, skill_names),
                governance=dict(gov) if isinstance(gov, dict) else {},
                audience=dict(aud) if isinstance(aud, dict) else {},
            )
            if resolved is not None:
                logger.warning("[multitenancy] duplicate visible expert id %r", eid)
                return None
            resolved = overlay
    except Exception:  # absolute fail-safe — overlay must never break a run
        logger.warning("[multitenancy] resolve_expert(%r) failed; running without overlay", eid, exc_info=True)
        return None
    return resolved


def authorized_expert_records(
    profile_home: Path,
    expert_id: str,
    *,
    department_ids: Optional[list[str]] = None,
    include_inactive: bool = False,
    strict: bool = False,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return registry records admitted by the shared Expert audience policy."""
    eid = str(expert_id or "").strip()
    if not eid:
        return []
    profile_names = _audience_profile_names(profile_home)
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for manifest, _path in _iter_managed_manifests(profile_home, strict=strict):
        experts = manifest.get("experts")
        if not isinstance(experts, list):
            continue
        for expert in experts:
            if not isinstance(expert, dict) or str(expert.get("id") or "").strip() != eid:
                continue
            if not _effective_audience_allows(
                manifest.get("audience"),
                expert.get("audience"),
                profile_names=profile_names,
                department_ids=department_ids,
            ):
                continue
            active = _manifest_is_active(manifest) and str(
                expert.get("status") or "active"
            ).strip().lower() == "active"
            if include_inactive or active:
                rows.append((manifest, expert))
    return rows


def build_role_override_block(overlay: ExpertOverlay) -> str:
    """Compose the session-scoped Hermes expert-mode system block.

    host context → expert agent.md → optional skills → safety reminder.
    """
    parts: list[str] = [ROLE_OVERRIDE_PREAMBLE, f"当前专家：{overlay.name}", "", overlay.agent_md]
    if overlay.skills:
        parts += ["", "## 本专家可用能力（Skills）", "、".join(overlay.skills) + "。"]
    parts += ["", ROLE_OVERRIDE_TAIL]
    return "\n".join(parts).strip()


def _audience_allows(
    audience: Any,
    *,
    profile_name: Optional[str] = None,
    profile_names: Optional[set[str]] = None,
    department_ids: Optional[list[str]] = None,
) -> bool:
    """Audience filter for ONE audience scope — covers BOTH persisted modes.

    Both the manifest-level INSTALL audience and the optional per-expert audience
    share this shape (persisted by plugin_ingest.py):
    ``{"mode": "profile"|"department_ids", "profiles": [...], "department_ids": [...]}``.
    Enforcement is fail-CLOSED for scoped audiences:

      * No audience / empty ``profiles`` AND empty ``department_ids`` → this scope
        admits everyone (it imposes no restriction of its own).
      * ``profiles`` non-empty → allowed ONLY if ANY of the caller's identity
        profile names is in it. ``profile_names`` (a set) is the general form; the
        singular ``profile_name`` is a convenience that folds into it. Empty/None
        identities fail closed.
      * ``department_ids`` non-empty → allowed ONLY if the caller's resolved
        departments intersect it. Unknown caller departments (None) fail closed.

    When BOTH scopes are present, EITHER admitting the caller is sufficient (a
    union — the same OR semantics ``feishu_org._skill_audience_matches`` uses for
    profiles/departments). Never raises.

    NOTE: an empty audience here means "this single scope adds no restriction",
    NOT "public". A managed manifest lives in shared-home and is read by EVERY
    profile, so visibility is gated by the EFFECTIVE audience — the manifest
    install audience ANDed with the per-expert audience (see
    ``_effective_audience_allows``). Never call this in isolation to decide
    expert visibility; that was the authorization bug.
    """
    if not isinstance(audience, dict):
        return True
    have_profiles = {str(p) for p in (profile_names or set()) if str(p).strip()}
    if profile_name and str(profile_name).strip():
        have_profiles.add(str(profile_name))
    want_profiles = {str(p) for p in (audience.get("profiles") or []) if str(p).strip()}
    want_depts = {str(d) for d in (audience.get("department_ids") or []) if str(d).strip()}
    if not want_profiles and not want_depts:  # this scope imposes no restriction
        return True
    if want_profiles and (want_profiles & have_profiles):
        return True
    if want_depts and department_ids:
        have = {str(d) for d in department_ids}
        if want_depts & have:
            return True
    return False


def _effective_audience_allows(
    manifest_audience: Any,
    expert_audience: Any,
    *,
    profile_name: Optional[str] = None,
    profile_names: Optional[set[str]] = None,
    department_ids: Optional[list[str]] = None,
) -> bool:
    """Fail-CLOSED visibility gate combining BOTH audience levels.

    There are two independent audience levels, and BOTH must admit the caller:

      1. ``manifest_audience`` — the PLUGIN INSTALL audience (plugin_ingest.py
         persists ``manifest["audience"]`` = ``{"mode","profiles","department_ids"}``).
         This is the PRIMARY gate: managed manifests live in shared-home and are
         read by ALL profiles, so an install scoped to profile A must NOT leak to
         any other profile — even when the expert carries no per-expert audience.
      2. ``expert_audience`` — the OPTIONAL per-expert audience that FURTHER
         narrows within the install audience.

    Rule (AND of both scopes):
      * manifest install audience ALWAYS gates — deny if it excludes the caller.
      * if a per-expert audience is present it must ALSO admit — deny if it excludes.
      * absent/empty per-expert audience → the expert INHERITS the manifest install
        audience (it is NOT public on its own).
      * only when BOTH the manifest install audience AND the per-expert audience are
        empty/absent is the expert truly public.

    Because ``_audience_allows`` returns True for an empty/absent scope, ANDing the
    two scopes yields exactly the inheritance + further-narrowing semantics above.
    """
    if not _audience_allows(
        manifest_audience,
        profile_name=profile_name,
        profile_names=profile_names,
        department_ids=department_ids,
    ):
        return False
    return _audience_allows(
        expert_audience,
        profile_name=profile_name,
        profile_names=profile_names,
        department_ids=department_ids,
    )


def list_experts(
    profile_home: Path, *, department_ids: Optional[list[str]] = None
) -> list[dict[str, Any]]:
    """Aggregate audience-visible experts across this profile's managed manifests.

    Returns redacted display rows (no persona body, no repo path) suitable for the
    expert-square UI. De-duped by expert id (first manifest wins). Fail-safe: an
    unreadable manifest is skipped, never fatal.

    Visibility is the EFFECTIVE audience (fail-CLOSED): the plugin INSTALL audience
    (``manifest["audience"]``, the PRIMARY gate since managed manifests are
    shared-home GLOBAL) ANDed with the OPTIONAL per-expert ``ex["audience"]``. An
    expert with no per-expert audience INHERITS the install audience; it is listed
    for everyone only when BOTH are empty.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    profile_names = _audience_profile_names(profile_home)
    try:
        for manifest, _path in _iter_managed_manifests(profile_home):
            if not _manifest_is_active(manifest):
                continue
            experts = manifest.get("experts")
            if not isinstance(experts, list):
                continue
            plugin_id = str(manifest.get("plugin_id") or "")
            manifest_audience = manifest.get("audience")
            release_version = manifest.get("release_version")
            release_installed_at = manifest.get("release_installed_at")
            if not (isinstance(release_installed_at, int) and release_installed_at > 0):
                # pre-2026-07-24 ingests were never stamped with release metadata;
                # ingested_at is the honest "last updated" for those plugins
                ingested_at = manifest.get("ingested_at")
                if isinstance(ingested_at, int) and ingested_at > 0:
                    release_installed_at = ingested_at
            for ex in experts:
                if not isinstance(ex, dict):
                    continue
                eid = str(ex.get("id") or "").strip()
                if not eid or eid in seen:
                    continue
                if not _effective_audience_allows(
                    manifest_audience,
                    ex.get("audience"),
                    profile_names=profile_names,
                    department_ids=department_ids,
                ):
                    continue
                seen.add(eid)
                row = {
                        "id": eid,
                        "name": str(ex.get("name") or ex.get("title") or eid),
                        "title": str(ex.get("title") or ex.get("name") or eid),
                        "tagline": str(ex.get("tagline") or ""),
                        "avatar": str(ex.get("avatar") or ""),
                        "category": str(ex.get("category") or ""),
                        "display_tags": [str(t) for t in ex.get("display_tags") or []],
                        "featured": bool(ex.get("featured")),
                        "team": ex.get("team"),
                        "plugin_id": plugin_id,
                        # experts live in managed manifests = ingested via the AiHub
                        # plugin pipeline → drives the WebUI "来自 AiHub" badge.
                        "source": "aihub",
                        "skills": [str(s) for s in ex.get("skills") or []],
                    }
                if isinstance(release_version, str) and release_version.strip():
                    row["release_version"] = release_version.strip()
                if isinstance(release_installed_at, int) and release_installed_at > 0:
                    row["release_installed_at"] = release_installed_at
                rows.append(row)
    except Exception:
        logger.warning("[multitenancy] list_experts failed for %s", profile_home, exc_info=True)
    return sorted(rows, key=lambda r: (not r["featured"], r["category"], r["name"]))


def active_expert_declared_skills(profile_home: Path, expert_id: str) -> set[str]:
    """Declared skill names of ONE expert by id, WITHOUT audience filtering.

    Used by the disabled-skill scope inside the sandboxed child, where the
    trusted routing table is unavailable so owner/audience cannot be re-resolved.
    Callers MUST gate this on the parent's authorization (the presence of a valid
    ``broker_role_override`` for this ``expert_id``, resolved gateway-side against
    the routing table) — this only un-hides that already-authorized expert's OWN
    skills so the run can use them. It is not an authorization surface on its own:
    un-hiding a skill whose directory was never materialized (materialization is
    routing-gated in the parent/sync) still cannot load it.
    """
    eid = str(expert_id or "").strip()
    if not eid:
        return set()
    names: set[str] = set()
    for manifest, _path in _iter_managed_manifests(profile_home):
        if not _manifest_is_active(manifest):
            continue
        experts = manifest.get("experts")
        if not isinstance(experts, list):
            continue
        for ex in experts:
            if not isinstance(ex, dict) or str(ex.get("id") or "").strip() != eid:
                continue
            for skill in ex.get("skills") or []:
                name = str(skill).strip()
                if name:
                    names.add(name)
    return names


def all_expert_skill_names(profile_home: Path) -> set[str]:
    """Return every skill declared by installed expert manifests.

    Runtime skill hiding should fail closed, so this intentionally ignores
    audience filtering: a hidden or unauthorized expert's private skills should
    not be advertised unless that expert is the active run overlay.
    """
    names: set[str] = set()
    for manifest, _path in _iter_managed_manifests(profile_home, strict=True):
        experts = manifest.get("experts")
        if not isinstance(experts, list):
            continue
        for ex in experts:
            if not isinstance(ex, dict):
                continue
            for skill in ex.get("skills") or []:
                name = str(skill).strip()
                if name:
                    names.add(name)
    return names


def expert_skill_sync_specs(profile_home: Path) -> list[dict[str, Any]]:
    """Skill-sync specs for every expert visible to this profile's audience identity.

    Consumed by the group-profile skill sync (``feishu_org._profile_skill_specs``)
    to materialize owner-visible expert skills into the group profile's
    ``skills/`` scan root — expert runs resolve their skills from there
    (``expert_scope`` only HIDES, never adds). Rows use the profile-skill-spec
    shape ``{"path", "install_mode", "source_path", "share_with_children"}``;
    secret filtering and manifest bookkeeping stay with that sync (copies always
    drop secret-named files, so credentials never travel). Fail-safe: [] on any
    resolution error.
    """
    specs: dict[str, dict[str, Any]] = {}
    try:
        profile_names = _audience_profile_names(profile_home)
        department_ids = resolve_caller_departments(profile_home)
        for manifest, _path in _iter_managed_manifests(profile_home):
            if not _manifest_is_active(manifest):
                continue
            experts = manifest.get("experts")
            if not isinstance(experts, list):
                continue
            manifest_audience = manifest.get("audience")
            install_mode = str(manifest.get("install_mode") or "copy").strip().lower()
            if install_mode not in {"copy", "symlink"}:
                install_mode = "copy"
            for ex in experts:
                if not isinstance(ex, dict):
                    continue
                if not _effective_audience_allows(
                    manifest_audience,
                    ex.get("audience"),
                    profile_names=profile_names,
                    department_ids=department_ids,
                ):
                    continue
                skill_names = [str(s) for s in ex.get("skills") or []]
                for skill_dir in _expert_skill_dirs(manifest, skill_names):
                    specs.setdefault(
                        skill_dir.name,
                        {
                            "path": Path(skill_dir.name),
                            "install_mode": install_mode,
                            "source_path": skill_dir,
                            "share_with_children": False,
                        },
                    )
    except Exception:
        logger.warning(
            "[multitenancy] expert skill sync specs failed for %s", profile_home, exc_info=True
        )
        return []
    return sorted(specs.values(), key=lambda spec: str(spec["path"]))


def readable_expert_skill_names(profile_home: Path) -> set[str]:
    """Best-effort expert skill names from readable manifests only."""
    names: set[str] = set()
    for manifest, _path in _iter_managed_manifests(profile_home):
        experts = manifest.get("experts")
        if not isinstance(experts, list):
            continue
        for ex in experts:
            if not isinstance(ex, dict):
                continue
            for skill in ex.get("skills") or []:
                name = str(skill).strip()
                if name:
                    names.add(name)
    return names


# ─────────────────────────── caller department resolution ────────────────────
# Department-scoped audiences must be enforced against the caller's REAL
# departments, resolved server-side from the trusted tenant — never from a
# caller-supplied value. The org snapshot (feishu_org.save_snapshot) is the only
# place department membership is recorded; the routing DB does not carry it. We
# read the newest persisted ``org-*.json`` and pull the matching employee's
# departments. ANY failure (no snapshot, unreadable, employee not found) returns
# None → department-scoped experts then fail CLOSED. This is intentionally a
# single chokepoint so both the activation path (agent_real) and the catalog path
# (webui_broker_server) resolve departments identically and are monkeypatchable.

# Env override + conventional locations for the persisted org snapshot directory.
_ORG_SNAPSHOT_DIR_ENV = "HERMES_ORG_SNAPSHOT_DIR"


def _org_snapshot_dirs(profile_home: Path) -> list[Path]:
    dirs: list[Path] = []
    explicit = os.getenv(_ORG_SNAPSHOT_DIR_ENV)
    if explicit:
        dirs.append(Path(explicit).expanduser())
    shared = _shared_home_for(Path(profile_home).expanduser())
    for sub in ("org-snapshots", "snapshots", "."):
        d = shared / sub
        if d not in dirs:
            dirs.append(d)
    return dirs


def _latest_org_snapshot(profile_home: Path) -> Optional[dict[str, Any]]:
    newest: Optional[tuple[float, Path]] = None
    for d in _org_snapshot_dirs(profile_home):
        if not d.is_dir():
            continue
        for path in d.glob("org-*.json"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest[0]:
                newest = (mtime, path)
    if newest is None:
        return None
    try:
        data = json.loads(newest[1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.debug("[multitenancy] unreadable org snapshot %s", newest[1], exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def resolve_caller_departments(
    profile_home: Path,
    *,
    profile_name: Optional[str] = None,
    open_id: Optional[str] = None,
) -> Optional[list[str]]:
    """Resolve the CALLER's real departments from the trusted org snapshot.

    Returns a list of department identifiers (``dept_id`` + ``dept_name``).
    An explicitly supplied ``open_id`` is a TRUSTED caller identity and matches
    EXCLUSIVELY: a shared-Agent run resolves departments against its profile
    home, whose derived profile name is the AGENT's — an unrelated employee
    record matching that name must never supply the caller's departments (codex
    review: explicit-open-id-is-not-authoritative). ``profile_name`` matching is
    the fallback only when no open_id is available. Returns ``None`` when no
    snapshot exists or the caller can't be found — department-scoped experts
    then fail CLOSED. Never raises.

    Group/agent profiles always resolve as their OWNER, whose open_id comes from
    the TRUSTED routing table (never the tenant-writable marker — see
    ``_routing_group_owner_open_id``). The supplied ``open_id`` (e.g. the
    group-message sender) is deliberately ignored so the group agent mirrors
    exactly the owner's entitlements, no more (a sender's departments must not
    widen the group's expert set) and no less. When routing is unavailable (the
    sandboxed child) the owner is unresolvable and this fails CLOSED.
    """
    pname = str(profile_name or _caller_profile_name(profile_home)).strip()
    oid = str(open_id or "").strip()
    owner_oid = _routing_group_owner_open_id(profile_home)
    if owner_oid:
        oid = owner_oid
    try:
        snap = _latest_org_snapshot(profile_home)
        if not snap:
            return None
        employees = snap.get("employees")
        if not isinstance(employees, dict):
            return None
        for emp in employees.values():
            if not isinstance(emp, dict):
                continue
            if oid:
                matched = str(emp.get("open_id") or "") == oid
            else:
                matched = bool(pname) and str(emp.get("profile_name") or "") == pname
            if matched:
                depts = [
                    str(v).strip()
                    for v in (emp.get("dept_id"), emp.get("dept_name"))
                    if str(v or "").strip()
                ]
                return depts or None
    except Exception:
        logger.warning("[multitenancy] resolve_caller_departments failed", exc_info=True)
    return None


def role_override_block_for(
    profile_home: Path,
    expert_id: str,
    *,
    department_ids: Optional[list[str]] = None,
    open_id: Optional[str] = None,
) -> Optional[str]:
    """Convenience: resolve + build in one call. ``None`` when no overlay applies.

    Threads the caller's departments into the audience check (fail-closed). When
    ``department_ids`` is not supplied, they are resolved server-side from the
    trusted org snapshot so department-scoped experts can still activate for the
    right caller without trusting any caller-supplied value.
    """
    depts = department_ids
    if depts is None:
        depts = resolve_caller_departments(profile_home, open_id=open_id)
    overlay = resolve_expert(profile_home, expert_id, department_ids=depts)
    if overlay is None:
        return None
    try:
        return build_role_override_block(overlay)
    except Exception:
        logger.warning("[multitenancy] build_role_override_block failed for %r", expert_id, exc_info=True)
        return None
