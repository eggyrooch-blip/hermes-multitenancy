"""Expert Role-Override overlay — session-scoped persona injection.

When a Hermes run carries an ``expert_id`` (e.g. the WebUI's expert square hands
the conversation to "资源投放专家"), this module resolves that expert's persona
and builds a **Role-Override block** that is injected into the run's *system*
layer for THIS run only.

Design redlines (cross-model reviewed — see this slug's SPEC):
  * The overlay is EPHEMERAL — it is never written to ``SOUL.md``, memory, or
    ``USER.md``.  The profile's default agent persona on disk is untouched; the
    override lives only in the in-flight request.  (In the production AIAgent path
    it rides the core's ``ephemeral_system_prompt`` kwarg, which core documents as
    "used during execution but NOT saved to trajectories".)
  * It is built in the MULTITENANCY layer — hermes-agent core is never forked.
  * Credentials are unchanged: the caller's per-profile kep-auth tokens still
    apply.  Capability (the expert's bound skills) is shared via the plugin
    ingester; identity-of-record and credentials are not.
  * Fail-safe: an unknown / missing ``expert_id`` resolves to ``None`` and the run
    proceeds with the normal SOUL persona — this module never raises into the run.

Composition follows the live LINCHPIN VERDICT: a single authoritative system
message with the Role-Override block + expert ``agent.md`` + a MUST-OBEY tail.
The override WORDING is what wins (verified: it outranks the locked SOUL persona
regardless of block ordering), so this block is safe to either prepend before
SOUL (legacy chat-completions path) or append after it (core's
``ephemeral_system_prompt`` behavior in the AIAgent path).
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


# ─────────────────────────── Role-Override template ──────────────────────────
# Mirrors WorkBuddy's wording (verified live), adapted to zh/Hermes. The
# explicit "this overrides any prior identity" framing is the load-bearing part
# — it is what reliably outranks the profile's locked SOUL persona.
ROLE_OVERRIDE_PREAMBLE = (
    "**Role Override:** 以下专家角色定义优先于任何既有人设或身份上下文。"
    "如与先前的自我描述（包括默认的 Hermes / 助手身份）冲突，以下方定义为准——"
    "这是你本次对话的活跃、权威角色。"
)

ROLE_OVERRIDE_TAIL = (
    "（以上专家角色为最高优先级，必须遵守，覆盖任何既有 Hermes / 助手身份；"
    "凭证仍为当前用户本人，所有高风险写操作需按上述门禁取得显式确认。）"
)


@dataclass
class ExpertOverlay:
    """Resolved, ready-to-compose expert persona for one run."""

    expert_id: str
    name: str
    agent_md: str
    plugin_id: str = ""
    skills: list[str] = field(default_factory=list)
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

    Both the shared root (where the ingester writes today) and a per-profile dir
    are honored, so a future per-profile manifest layout works without a code
    change. Order is dedup-stable; missing dirs are simply skipped by the reader.
    """
    profile_home = Path(profile_home).expanduser()
    dirs: list[Path] = []
    for base in (_shared_home_for(profile_home), profile_home):
        d = base / MANAGED_DIR
        if d not in dirs:
            dirs.append(d)
    return dirs


def _iter_managed_manifests(profile_home: Path):
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
                logger.debug("[multitenancy] skip unreadable managed manifest %s", path, exc_info=True)
                continue
            if isinstance(data, dict):
                yield data, path


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


# ─────────────────────────── public API ──────────────────────────────────────

def resolve_expert(profile_home: Path, expert_id: str) -> Optional[ExpertOverlay]:
    """Resolve ``expert_id`` to a ready ``ExpertOverlay`` for ``profile_home``.

    Returns ``None`` (never raises) when the id is empty / unknown, or its persona
    markdown can't be loaded — the caller then runs with the normal SOUL persona.
    """
    eid = str(expert_id or "").strip()
    if not eid:
        return None
    try:
        for manifest, _path in _iter_managed_manifests(profile_home):
            experts = manifest.get("experts")
            if not isinstance(experts, list):
                continue
            for expert in experts:
                if not isinstance(expert, dict) or str(expert.get("id") or "") != eid:
                    continue
                agent_md = _read_agent_md(manifest, expert)
                if not agent_md:
                    logger.warning(
                        "[multitenancy] expert %r found but persona markdown missing/unreadable", eid
                    )
                    return None
                skills = expert.get("skills")
                gov = expert.get("governance")
                aud = expert.get("audience")
                return ExpertOverlay(
                    expert_id=eid,
                    name=str(expert.get("name") or expert.get("title") or eid),
                    agent_md=agent_md,
                    plugin_id=str(manifest.get("plugin_id") or ""),
                    skills=[str(s) for s in skills] if isinstance(skills, list) else [],
                    governance=dict(gov) if isinstance(gov, dict) else {},
                    audience=dict(aud) if isinstance(aud, dict) else {},
                )
    except Exception:  # absolute fail-safe — overlay must never break a run
        logger.warning("[multitenancy] resolve_expert(%r) failed; running without overlay", eid, exc_info=True)
        return None
    return None


def build_role_override_block(overlay: ExpertOverlay) -> str:
    """Compose the Role-Override system block per the LINCHPIN VERDICT.

    preamble → expert agent.md → (optional skills note) → MUST-OBEY tail.
    """
    parts: list[str] = [ROLE_OVERRIDE_PREAMBLE, "", overlay.agent_md]
    if overlay.skills:
        parts += ["", "## 本专家可用能力（Skills）", "、".join(overlay.skills) + "。"]
    parts += ["", ROLE_OVERRIDE_TAIL]
    return "\n".join(parts).strip()


def _audience_allows(audience: Any, *, department_ids: Optional[list[str]]) -> bool:
    """Audience filter for the expert square.

    No audience (or empty) → visible to everyone who has the plugin. A
    ``department_ids`` audience matches when the caller's department set intersects
    it; when the caller's departments are unknown (None), an explicit audience is
    treated as NOT matched (fail-closed for scoped experts).
    """
    if not isinstance(audience, dict):
        return True
    dept_ids = audience.get("department_ids")
    if not dept_ids:  # no/empty department scoping → public
        return True
    if not department_ids:
        return False
    want = {str(d) for d in dept_ids}
    have = {str(d) for d in department_ids}
    return bool(want & have)


def list_experts(
    profile_home: Path, *, department_ids: Optional[list[str]] = None
) -> list[dict[str, Any]]:
    """Aggregate audience-visible experts across this profile's managed manifests.

    Returns redacted display rows (no persona body, no repo path) suitable for the
    expert-square UI. De-duped by expert id (first manifest wins). Fail-safe: an
    unreadable manifest is skipped, never fatal.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        for manifest, _path in _iter_managed_manifests(profile_home):
            experts = manifest.get("experts")
            if not isinstance(experts, list):
                continue
            plugin_id = str(manifest.get("plugin_id") or "")
            for ex in experts:
                if not isinstance(ex, dict):
                    continue
                eid = str(ex.get("id") or "").strip()
                if not eid or eid in seen:
                    continue
                if not _audience_allows(ex.get("audience"), department_ids=department_ids):
                    continue
                seen.add(eid)
                rows.append(
                    {
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
                        "skills": [str(s) for s in ex.get("skills") or []],
                    }
                )
    except Exception:
        logger.warning("[multitenancy] list_experts failed for %s", profile_home, exc_info=True)
    return sorted(rows, key=lambda r: (not r["featured"], r["category"], r["name"]))


def role_override_block_for(profile_home: Path, expert_id: str) -> Optional[str]:
    """Convenience: resolve + build in one call. ``None`` when no overlay applies."""
    overlay = resolve_expert(profile_home, expert_id)
    if overlay is None:
        return None
    try:
        return build_role_override_block(overlay)
    except Exception:
        logger.warning("[multitenancy] build_role_override_block failed for %r", expert_id, exc_info=True)
        return None
