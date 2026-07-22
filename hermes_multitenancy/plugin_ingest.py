"""Reusable plugin ingester — distribute a `.hermes-plugin` plugin to Feishu profiles.

Input: a plugin repo carrying `.hermes-plugin/plugin.json` (the Hermes
distribution contract, compiled from the plugin's single source of truth — e.g.
keep-rd's `experts/expert.yaml`).  One command lands its skills + business CLIs +
connector validation onto N Feishu employee profiles, idempotently, with a
`.hermes-plugin-managed` manifest for clean `--uninstall` rollback.

This is the productized form of the manual "A 测" (hand-`cp` skills/CLI + webui
`/auth`).  It reuses existing multitenancy seams and **never forks
hermes-agent core**:

  - skills  → `skill_registry.install_shared_skill_for_profile` (profile mode)
              or a `skill-distribution.yaml` audience entry (department mode).
  - CLIs    → `kep-cli install <system>` then copy the resolved binary into the
              shared `<HERMES_HOME>/bin` (self-contained: the sandbox pivots HOME
              to profile_home, so a symlink into ~/.kep-cli would dangle).
  - connectors → validated against `connectors.builtin.get_definition`.
  - credentials → NOT touched here.  Tokens are per-profile and arrive via the
              connector path (webui `/auth` + credential materializer).  Capability
              is shared; credentials are never shared.

Design redlines (cross-model reviewed — see vault PLAN §4):
  * persona is NOT a distribution product — it never lands in SOUL.md (would
    pollute the profile's default agent).  The plugin.json carries no persona.
  * audience must be numeric `department_ids` (the org matcher ignores `dept`/
    name keys → silent 0-match).  Unknown audience → hard error, never silent.
  * online-by-default is forbidden: governance.env_default must be `pre`.

Usage:
    python3 -m hermes_multitenancy.plugin_ingest <plugin-repo> --audience <ids|profile>
    python3 -m hermes_multitenancy.plugin_ingest <plugin-repo> --audience X --dry-run
    python3 -m hermes_multitenancy.plugin_ingest --uninstall <plugin-id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import yaml

from .connectors import builtin as connector_builtin
from .skill_registry import (
    install_shared_skill_for_profile,
    uninstall_personal_skill_for_profile,
)

PLUGIN_MANIFEST_REL = ".hermes-plugin/plugin.json"
SUPPORTED_SCHEMA = "hermes-plugin/v1"
MANAGED_DIR = ".hermes-plugin-managed"  # under shared_home; distinct from skill_registry's .hermes-managed.json
MANAGED_ASSETS_DIR = ".hermes-plugin-assets"
PLUGIN_ASSET_URL_PREFIX = "/api/run-broker/plugin-assets"
SKILL_DISTRIBUTION_FILE = "skill-distribution.yaml"
ENV_KEP_NO_AUTO_LOGIN = {"KEP_NO_AUTO_LOGIN": "1"}
_ASSET_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class PluginIngestError(RuntimeError):
    """Any operator-facing failure (bad manifest, unknown audience, missing connector)."""


def _safe_skill_name(name: Any) -> str:
    """Reject path-traversal / absolute skill names from an (untrusted) plugin manifest.

    The ingester copies `repo/<skills.dir>/<name>` → `~/.hermes/skills/<name>`; an
    unsanitized `name` like `../../x` would escape both trees. Allow only relative,
    non-empty, dot-free path segments.
    """
    raw = str(name or "").strip()
    p = PurePosixPath(raw)
    if not raw or raw.startswith("/") or p.is_absolute() or any(part in ("", ".", "..") for part in p.parts):
        raise PluginIngestError(f"unsafe skill path in manifest: {name!r}")
    return raw


def _safe_component(value: Any, *, kind: str) -> str:
    """Reject ids used as FILENAMES (plugin_id → manifest file; cli id → shared_bin/<id>).

    No path separators, no `..`, no leading dot — an id from an untrusted manifest must
    not let us write/unlink outside `.hermes-plugin-managed/` or `<shared>/bin`.
    """
    raw = str(value or "").strip()
    if (not raw or "/" in raw or "\\" in raw or raw in (".", "..") or raw.startswith(".")
            or "\x00" in raw):
        raise PluginIngestError(f"unsafe {kind} in manifest: {value!r}")
    return raw


def _safe_url_path_component(value: Any, *, kind: str) -> str:
    """Reject ids that would produce unusable `/api/run-broker/.../<id>/...` URLs."""
    raw = _safe_component(value, kind=kind)
    if len(raw) > 180 or not all(c.isascii() and (c.isalnum() or c in "-_.:") for c in raw):
        raise PluginIngestError(
            f"unsafe {kind} URL component in manifest: {value!r} "
            "(ASCII alnum plus -_.: only)"
        )
    return raw


def _normalize_skills_dir(value: Any) -> str:
    """Honor the manifest's `skills.dir` contract (default 'skills'); reject escapes."""
    raw = str(value or "skills").strip()
    if raw.startswith("/") or PurePosixPath(raw).is_absolute():  # reject absolute, don't relativize it
        raise PluginIngestError(f"unsafe skills.dir in manifest: {value!r}")
    raw = (raw[2:] if raw.startswith("./") else raw).strip("/")
    p = PurePosixPath(raw or "skills")
    if any(part in ("", "..") for part in p.parts):
        raise PluginIngestError(f"unsafe skills.dir in manifest: {value!r}")
    return str(p)


def _safe_repo_relative_file(
    repo: Path,
    rel: Any,
    *,
    manifest_path: Path,
    kind: str,
    suffix_mimes: dict[str, str] | None = None,
) -> Path:
    """Resolve a repo-relative manifest file path and reject traversal/absolute paths."""
    raw = str(rel or "").strip()
    if not raw:
        raise PluginIngestError(f"{manifest_path}: {kind} is required")
    rel_clean = raw[2:] if raw.startswith("./") else raw
    if raw.startswith("/") or PurePosixPath(rel_clean).is_absolute() or any(
        part in ("", "..") for part in PurePosixPath(rel_clean).parts
    ):
        raise PluginIngestError(f"{manifest_path}: unsafe {kind} {raw!r}")
    candidate = repo / rel_clean
    if not candidate.is_file():
        raise PluginIngestError(f"{manifest_path}: {kind} {raw!r} not found at {candidate}")
    if suffix_mimes is not None and candidate.suffix.lower() not in suffix_mimes:
        raise PluginIngestError(
            f"{manifest_path}: {kind} {raw!r} must be one of {sorted(suffix_mimes)}"
        )
    return candidate


def _external_or_web_asset_uri(raw: str) -> bool:
    lowered = raw.strip().lower()
    return (
        lowered.startswith("http://")
        or lowered.startswith("https://")
        or lowered.startswith("data:")
        or lowered.startswith("/api/")
    )


def _experts_have_local_assets(experts: Any) -> bool:
    if not isinstance(experts, list):
        return False
    for ex in experts:
        if not isinstance(ex, dict):
            continue
        avatar = str(ex.get("avatar") or "").strip()
        if avatar and not _external_or_web_asset_uri(avatar):
            return True
    return False


# ─────────────────────────── manifest loading ────────────────────────────

def load_plugin_manifest(repo: Path) -> dict[str, Any]:
    """Read + schema-validate `<repo>/.hermes-plugin/plugin.json`."""
    repo = Path(repo).expanduser()
    path = repo / PLUGIN_MANIFEST_REL
    if not path.is_file():
        raise PluginIngestError(f"no plugin manifest at {path} (run the plugin's compile step first)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PluginIngestError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PluginIngestError(f"{path}: top-level must be an object")
    if data.get("schema") != SUPPORTED_SCHEMA:
        raise PluginIngestError(f"{path}: unsupported schema {data.get('schema')!r}, need {SUPPORTED_SCHEMA!r}")
    if not data.get("id"):
        raise PluginIngestError(f"{path}: missing plugin id")
    _safe_component(data["id"], kind="plugin id")  # used as the managed-manifest filename

    skills = data.get("skills") or {}
    if not isinstance(skills, dict) or not isinstance(skills.get("list"), list) or not skills["list"]:
        raise PluginIngestError(f"{path}: skills.list must be a non-empty array")
    # validate every skill path (untrusted manifest → no traversal/absolute escapes)
    for name in skills["list"]:
        _safe_skill_name(name)
    data["_skills_dir"] = _normalize_skills_dir(skills.get("dir"))
    entry = data.get("entry_skill")
    if entry:
        _safe_skill_name(entry)

    audience = data.get("audience") or {}
    if not isinstance(audience, dict):
        raise PluginIngestError(f"{path}: audience must be an object")
    unknown = [k for k in audience if k != "department_ids"]
    if unknown:
        raise PluginIngestError(
            f"{path}: audience has unknown key(s) {unknown}; only `department_ids` "
            "(numeric IDs) is honored by the org matcher — `dept`/部门名 silently match 0 people"
        )

    for cli in data.get("clis") or []:
        if not isinstance(cli, dict) or "id" not in cli or "install" not in cli:
            raise PluginIngestError(f"{path}: each clis[] entry needs id+install, got {cli!r}")
        _safe_component(cli["id"], kind="cli id")  # used as <shared>/bin/<id>
        inst = str(cli["install"])  # passed as `kep-cli install <inst>` — block flag injection
        if not inst or inst.startswith("-") or not all(c.isalnum() or c in "-_." for c in inst):
            raise PluginIngestError(f"{path}: unsafe clis[].install token: {inst!r}")
    for con in data.get("connectors") or []:
        if not isinstance(con, dict) or "id" not in con:
            raise PluginIngestError(f"{path}: each connectors[] entry needs id, got {con!r}")

    gov = data.get("governance") or {}
    if not isinstance(gov, dict):
        raise PluginIngestError(f"{path}: governance must be an object")
    if gov.get("env_default") not in (None, "pre"):
        raise PluginIngestError(
            f"{path}: governance.env_default={gov.get('env_default')!r} — online-by-default is "
            "forbidden; a plugin distributed to many employees must default to `pre`"
        )

    _validate_experts(data.get("experts"), repo=repo, path=path)
    if _experts_have_local_assets(data.get("experts")):
        _safe_url_path_component(data["id"], kind="plugin id")

    data["_repo"] = str(repo)
    return data


def _validate_experts(experts: Any, *, repo: Path, path: Path) -> None:
    """Validate the optional ``experts[]`` array (the expert-square contract).

    Each entry is a session-scoped Role-Override persona: it carries an ``id``
    (stable, used to select the expert at run time) and ``agent_md`` (a repo-relative
    path to the persona markdown). ``agent_md`` must stay inside the repo and exist —
    a declared expert with no persona file would silently fail to overlay at run time.
    Optional: ``name``/``skills``/``governance``/``audience`` + UI display fields.
    Absent ``experts`` is fine (a skills/CLI-only plugin).
    """
    if experts is None:
        return
    if not isinstance(experts, list):
        raise PluginIngestError(f"{path}: experts must be an array")
    seen: set[str] = set()
    for ex in experts:
        if not isinstance(ex, dict):
            raise PluginIngestError(f"{path}: each experts[] entry must be an object, got {ex!r}")
        eid = str(ex.get("id") or "").strip()
        if not eid:
            raise PluginIngestError(f"{path}: each experts[] entry needs a non-empty id")
        if not all(c.isalnum() or c in "-_.:" for c in eid):
            raise PluginIngestError(f"{path}: unsafe experts[].id {eid!r} (alnum / -_.: only)")
        if eid in seen:
            raise PluginIngestError(f"{path}: duplicate experts[].id {eid!r}")
        seen.add(eid)
        rel = str(ex.get("agent_md") or "").strip()
        if not rel:
            raise PluginIngestError(f"{path}: experts[{eid}] needs an agent_md (persona markdown path)")
        _safe_repo_relative_file(repo, rel, manifest_path=path, kind=f"experts[{eid}].agent_md")
        avatar = str(ex.get("avatar") or "").strip()
        if avatar and not _external_or_web_asset_uri(avatar):
            _safe_repo_relative_file(
                repo,
                avatar,
                manifest_path=path,
                kind=f"experts[{eid}].avatar",
                suffix_mimes=_ASSET_MIME_BY_SUFFIX,
            )
        skills = ex.get("skills")
        if skills is not None and not isinstance(skills, list):
            raise PluginIngestError(f"{path}: experts[{eid}].skills must be an array")
        gov = ex.get("governance")
        if gov is not None and not isinstance(gov, dict):
            raise PluginIngestError(f"{path}: experts[{eid}].governance must be an object")


# ─────────────────────────── audience resolution ─────────────────────────

@dataclass
class Audience:
    mode: str  # "profile" | "department_ids" | "all"
    profiles: list[str] = field(default_factory=list)
    department_ids: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.mode == "profile":
            return f"profiles={self.profiles}"
        if self.mode == "all":
            return "audience=all"
        return f"department_ids={self.department_ids}"


def resolve_audience(value: str, *, profiles_root: Path) -> Audience:
    """Map a `--audience` string to a concrete target set.

    A token is a profile id if a matching profile dir exists; otherwise the whole
    value must be a comma list of all-numeric department ids.  Anything else
    (a department NAME, a typo) is a hard error — never a silent 0-match.
    """
    raw = (value or "").strip()
    if not raw:
        raise PluginIngestError("--audience is required (a profile id or numeric department_ids)")
    if raw.lower() in {"all", "*", "everyone", "__all__"}:
        return Audience(mode="all")
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    for t in tokens:  # a profile token becomes `profiles_root / t` — block traversal
        if "/" in t or "\\" in t or t in (".", "..") or "\x00" in t:
            raise PluginIngestError(f"unsafe --audience token: {t!r}")

    profile_hits = [t for t in tokens if (profiles_root / t).is_dir()]
    if profile_hits and len(profile_hits) == len(tokens):
        return Audience(mode="profile", profiles=profile_hits)
    if profile_hits:
        missing = [t for t in tokens if t not in profile_hits]
        raise PluginIngestError(f"--audience mixes known profiles with unknown tokens {missing}")

    if all(t.isdigit() for t in tokens):
        return Audience(mode="department_ids", department_ids=tokens)

    bad = [t for t in tokens if not t.isdigit()]
    raise PluginIngestError(
        f"--audience {bad!r}: not a profile dir under {profiles_root} and not numeric "
        "department_ids. Use a profile id (e.g. feishu_xxxx) or numeric dept ids — "
        "department NAMES are rejected (the org matcher would silently match 0 people)."
    )


# ─────────────────────────── CLI installation ────────────────────────────

def _kep_cli_available() -> Optional[str]:
    return shutil.which("kep-cli")


def install_clis(
    clis: list[dict[str, Any]],
    *,
    shared_bin: Path,
    dry_run: bool,
    force: bool,
) -> list[dict[str, Any]]:
    """`kep-cli install <system>` then copy the self-contained binary into shared_bin."""
    results: list[dict[str, Any]] = []
    if clis and not dry_run and _kep_cli_available() is None:
        raise PluginIngestError("kep-cli not on PATH — cannot install business CLIs")
    if clis and not dry_run:
        shared_bin.mkdir(parents=True, exist_ok=True)
    for cli in clis:
        cid, system = str(cli["id"]), str(cli["install"])
        target = shared_bin / cid
        if target.exists() and os.access(target, os.X_OK) and not force:
            results.append({"id": cid, "action": "skipped", "reason": "already present", "path": str(target)})
            continue
        if dry_run:
            results.append({"id": cid, "action": "would-install", "cmd": f"kep-cli install {system}", "path": str(target)})
            continue
        env = {**os.environ, **ENV_KEP_NO_AUTO_LOGIN}
        proc = subprocess.run(
            ["kep-cli", "install", system, *(["--force"] if force else [])],
            capture_output=True, text=True, env=env,
        )
        if proc.returncode != 0:
            raise PluginIngestError(f"kep-cli install {system} failed (exit {proc.returncode}): {proc.stderr.strip()}")
        src = _resolve_installed_binary(cid, env=env)
        if src is None:
            raise PluginIngestError(f"installed {system} but could not locate binary {cid} to copy into shared bin")
        shutil.copy2(src, target)
        target.chmod(0o755)
        results.append({"id": cid, "action": "installed", "from": str(src), "path": str(target)})
    return results


def _resolve_installed_binary(cli_id: str, *, env: dict[str, str]) -> Optional[Path]:
    """Find the real (deref'd) binary kep-cli just installed for `cli_id`."""
    found = shutil.which(cli_id, path=env.get("PATH"))
    if found:
        return Path(found).resolve()
    # fall back to kep-cli's default systems layout
    for base in (Path(env.get("HOME", "~")).expanduser() / ".kep-cli" / "systems",):
        for pattern in (f"*/bin/{cli_id}", f"*/{cli_id}"):
            for cand in base.glob(pattern):
                if os.access(cand, os.X_OK):
                    return cand.resolve()
    return None


# ─────────────────────────── skill distribution ──────────────────────────

def _register_shared_skill_source(
    repo: Path, shared_skills: Path, name: str, *, skills_dir: str, dry_run: bool, force: bool
) -> str:
    """Copy the plugin's skill dir into the shared distribution source."""
    _safe_skill_name(name)  # defense in depth (already validated at manifest load)
    src = repo / skills_dir / name
    if not src.is_dir():
        raise PluginIngestError(f"plugin declares skill {name!r} but {src} is missing")
    dst = shared_skills / name
    if dst.exists() and not force:
        return "present"
    if dry_run:
        return "would-register"
    dst.parent.mkdir(parents=True, exist_ok=True)  # nested (category/skill) skill paths
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return "registered"


def _personal_install_target(profile_home: Path, name: str) -> Optional[str]:
    """The `target` (source path) recorded for a personal skill install, or None."""
    mf = profile_home / "skills" / ".hermes-personal-installs.json"
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    skills = data.get("skills") if isinstance(data, dict) else None
    entry = skills.get(name) if isinstance(skills, dict) else None
    return entry.get("target") if isinstance(entry, dict) else None


def _managed_skill_present(profile_home: Path, name: str) -> bool:
    """True if the org-managed manifest (`.hermes-managed.json`) already owns this skill.

    Overwriting a managed/default skill is unsafe: `_link_skill` would delete it, but
    `uninstall_personal_skill_for_profile` refuses to remove a managed path, so it could
    not be rolled back. Treat a managed skill as foreign → never hijack it.
    """
    mf = profile_home / "skills" / ".hermes-managed.json"
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    skills = data.get("skills") if isinstance(data, dict) else None
    return isinstance(skills, dict) and name in skills


def _install_skills_to_profile(
    plugin: dict[str, Any],
    audience: Audience,
    *,
    shared_home: Path,
    profiles_root: Path,
    dry_run: bool,
    force: bool,
) -> dict[str, Any]:
    repo = Path(plugin["_repo"])
    shared_skills = shared_home / "skills"
    names = list(plugin["skills"]["list"])
    version = str(plugin.get("version") or "")
    skills_dir = plugin["_skills_dir"]
    source_actions = {name: _register_shared_skill_source(repo, shared_skills, name, skills_dir=skills_dir, dry_run=dry_run, force=force) for name in names}

    installed: list[dict[str, Any]] = []
    owned: dict[str, list[str]] = {}
    for profile in audience.profiles:
        profile_home = profiles_root / profile
        if not profile_home.is_dir():
            raise PluginIngestError(f"profile home {profile_home} does not exist")
        for name in names:
            my_source = str(shared_skills / name)
            existing = _personal_install_target(profile_home, name)
            # COEXISTENCE GUARD: never hijack a skill this profile already has from
            # someone else — a personal install from a DIFFERENT source (employee upload
            # / another plugin) OR an org-managed/default skill (which uninstall can't
            # roll back). Skip it, and never record it as ours.
            if _managed_skill_present(profile_home, name):
                installed.append({"profile": profile, "skill": name, "action": "skipped-managed"})
                continue
            if existing is not None and existing != my_source:
                installed.append({"profile": profile, "skill": name, "action": "skipped-foreign", "target": existing})
                continue
            if dry_run:
                installed.append({"profile": profile, "skill": name, "action": "would-install"})
                owned.setdefault(profile, []).append(name)
                continue
            install_shared_skill_for_profile(
                shared_home=shared_home,
                profile_home=profile_home,
                skill_path=name,
                source=shared_skills / name,
                version=version,
            )
            installed.append({"profile": profile, "skill": name, "action": "ensured"})
            owned.setdefault(profile, []).append(name)
    return {"source_actions": source_actions, "installed": installed, "owned": owned}


def _register_department_distribution(
    plugin: dict[str, Any],
    audience: Audience,
    *,
    shared_home: Path,
    dry_run: bool,
    allow_create: bool = False,
) -> dict[str, Any]:
    """Add audience-scoped entries to shared `skill-distribution.yaml` for the org fan-out."""
    config_path = shared_home / SKILL_DISTRIBUTION_FILE
    # SAFETY: when this file is absent, `_default_profile_skill_specs` sources every
    # profile's default skills from the OTHER (profile-skill-defaults) config or the
    # curator. CREATING skill-distribution.yaml here flips that switch and would drop
    # all profiles' defaults to whatever audience-scoped entries we write. So only
    # APPEND to an existing config unless the operator explicitly opts in.
    if not config_path.exists() and not allow_create:
        raise PluginIngestError(
            f"no {SKILL_DISTRIBUTION_FILE} at {shared_home} — refusing to create it "
            "(creating it would override every profile's default-skill source; this env "
            "is likely curator-driven). Use --allow-create-distribution to override, or "
            "target an explicit profile with --audience <profile-id>."
        )
    repo = Path(plugin["_repo"])
    shared_skills = shared_home / "skills"
    names = list(plugin["skills"]["list"])
    install_mode = plugin.get("install_mode") or "copy"
    for name in names:  # ensure source registered so the fan-out has something to copy
        _register_shared_skill_source(repo, shared_skills, name, skills_dir=plugin["_skills_dir"], dry_run=dry_run, force=False)

    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    skills_list = raw.get("skills")
    if not isinstance(skills_list, list):
        skills_list = []
    plugin_id = plugin["id"]
    # NON-DESTRUCTIVE merge: keep EVERY other entry — including same-path entries owned
    # by other plugins/departments. The loader (_default_profile_skill_specs) keeps
    # multiple entries and filters them PER EMPLOYEE by audience, so collapsing by path
    # here would silently delete another distribution's audience. Replace only THIS
    # plugin's prior entries (idempotent re-ingest), then append ours.
    kept = [it for it in skills_list if not (isinstance(it, dict) and it.get("plugin") == plugin_id)]
    entries = [
        {
            "path": name,
            "install_mode": install_mode,
            "audience": {"department_ids": list(audience.department_ids)},
            "plugin": plugin_id,
        }
        for name in names
    ]
    raw["skills"] = sorted(kept + entries, key=lambda it: (str(it.get("path")), str(it.get("plugin") or "")))

    if not dry_run:
        config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"config_path": str(config_path), "entries": entries, "written": not dry_run}


def _register_global_distribution(
    plugin: dict[str, Any],
    *,
    shared_home: Path,
    dry_run: bool,
    allow_create: bool = False,
) -> dict[str, Any]:
    """Add all-employee entries to shared `skill-distribution.yaml` for the org fan-out."""
    config_path = shared_home / SKILL_DISTRIBUTION_FILE
    if not config_path.exists() and not allow_create:
        raise PluginIngestError(
            f"no {SKILL_DISTRIBUTION_FILE} at {shared_home} — refusing to create it "
            "(creating it would override every profile's default-skill source; this env "
            "is likely curator-driven). Use --allow-create-distribution to override, or "
            "target an explicit profile with --audience <profile-id>."
        )
    repo = Path(plugin["_repo"])
    shared_skills = shared_home / "skills"
    names = list(plugin["skills"]["list"])
    install_mode = plugin.get("install_mode") or "copy"
    for name in names:
        _register_shared_skill_source(repo, shared_skills, name, skills_dir=plugin["_skills_dir"], dry_run=dry_run, force=False)

    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    skills_list = raw.get("skills")
    if not isinstance(skills_list, list):
        skills_list = []
    plugin_id = plugin["id"]
    kept = [it for it in skills_list if not (isinstance(it, dict) and it.get("plugin") == plugin_id)]
    entries = [
        {
            "path": name,
            "install_mode": install_mode,
            "audience": "all",
            "plugin": plugin_id,
        }
        for name in names
    ]
    raw["skills"] = sorted(kept + entries, key=lambda it: (str(it.get("path")), str(it.get("plugin") or "")))

    if not dry_run:
        config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"config_path": str(config_path), "entries": entries, "written": not dry_run}


# ─────────────────────────── connector validation ────────────────────────

def validate_connectors(connectors: list[dict[str, Any]]) -> dict[str, Any]:
    ok, missing = [], []
    for con in connectors:
        cid = str(con["id"])
        definition = connector_builtin.get_definition(cid)
        if definition is None:
            if con.get("required"):
                missing.append(cid)
            else:
                ok.append({"id": cid, "registered": False, "required": False})
        else:
            ok.append({"id": cid, "registered": True, "required": bool(con.get("required"))})
    if missing:
        raise PluginIngestError(
            f"required connector(s) not in registry: {missing}. Register them in "
            "connectors/builtin.py (or mark required:false) before distributing."
        )
    return {"connectors": ok}


# ─────────────────────────── governance assertion ────────────────────────

def assert_governance(plugin: dict[str, Any]) -> dict[str, Any]:
    """Contract-level governance check (the plugin declaration)."""
    gov = plugin.get("governance") or {}
    env_default = gov.get("env_default") or "pre"
    if env_default != "pre":
        raise PluginIngestError(f"governance.env_default={env_default!r} must be 'pre' for distribution")
    gates = list(gov.get("approval_required") or [])
    if not gates:
        # not fatal, but loud: a plugin with high-risk writes and no gates is suspicious
        sys.stderr.write("WARN: plugin declares no approval_required gates\n")
    return {"env_default": env_default, "approval_gates": gates, "online_requires": gov.get("online_requires") or "explicit_action"}


def assert_profile_governance(plugin: dict[str, Any], profile_home: Path) -> dict[str, Any]:
    """Post-ingest assertion: the governance-bearing skills actually landed in the
    target profile, and the high-risk approval gates are present in the installed
    content (not just declared in the manifest). This is what makes "目标 profile
    门禁存活" checkable rather than asserted from the plugin JSON.
    """
    gov = plugin.get("governance") or {}
    gates = list(gov.get("approval_required") or [])
    skills_root = profile_home / "skills"

    # the orchestrator skill (its SKILL.md enforces the staged gates) must be live.
    # Resolve by the plugin's own (possibly nested) skill path, not a flat dir name.
    orchestrate = next((s for s in plugin["skills"]["list"] if "orchestrat" in s), None)
    entry = plugin.get("entry_skill")
    def _installed(name: str) -> bool:
        p = skills_root / name
        return p.is_dir() or p.is_symlink()
    missing_skills = [s for s in (orchestrate, entry) if s and not _installed(s)]
    if missing_skills:
        raise PluginIngestError(
            f"governance check failed: required governance skill(s) {missing_skills} "
            f"not installed in {profile_home} — gates cannot be enforced"
        )

    # each declared gate must appear verbatim in some installed skill doc
    corpus = ""
    for name in (orchestrate, entry):
        if not name:
            continue
        doc = skills_root / name / "SKILL.md"
        try:
            corpus += doc.read_text(encoding="utf-8")
        except OSError:
            pass
    ungoverned = [g for g in gates if g and g not in corpus]
    # "3 门禁存活" — if the plugin declares high-risk gates but NONE of them survive in
    # the installed governance skills, the gates are effectively gone: fail hard.
    if gates and len(ungoverned) == len(gates):
        raise PluginIngestError(
            f"governance check failed: none of the {len(gates)} declared approval gate(s) "
            f"appear in the installed governance skills for {profile_home.name} — gates not enforced"
        )
    return {
        "profile": profile_home.name,
        "env_default": gov.get("env_default") or "pre",
        "gates_declared": len(gates),
        "gates_present_in_installed_skills": len(gates) - len(ungoverned),
        "gates_missing_from_content": ungoverned,
        "governance_skills_live": [s for s in (orchestrate, entry) if s],
    }


# ─────────────────────────── managed manifest ────────────────────────────

def _managed_path(shared_home: Path, plugin_id: str) -> Path:
    return shared_home / MANAGED_DIR / f"{plugin_id}.json"


def _write_managed_manifest(shared_home: Path, manifest: dict[str, Any], *, dry_run: bool) -> Path:
    path = _managed_path(shared_home, manifest["plugin_id"])
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _asset_slug(value: Any) -> str:
    raw = str(value or "").strip()
    chars = [
        c if c.isascii() and (c.isalnum() or c in "-_.") else "-"
        for c in raw
    ]
    slug = "".join(chars).strip("-_.")
    return slug or "asset"


def _plugin_asset_url(plugin_id: str, asset_name: str) -> str:
    plugin_id = _safe_url_path_component(plugin_id, kind="plugin id")
    asset_name = _safe_url_path_component(asset_name, kind="asset name")
    return f"{PLUGIN_ASSET_URL_PREFIX}/{plugin_id}/{asset_name}"


def _materialize_expert_assets(
    plugin: dict[str, Any],
    *,
    shared_home: Path,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Copy declared local expert assets into shared-home and rewrite web-facing URLs.

    Only repo-relative local avatar paths are copied. Remote/data/API URLs are left
    untouched for forward compatibility; local files become broker asset URLs.
    """
    experts = plugin.get("experts") or []
    if not isinstance(experts, list):
        return [], {}, []
    repo = Path(str(plugin.get("_repo") or "")).expanduser()
    plugin_id = _safe_component(plugin["id"], kind="plugin id")
    manifest_path = repo / PLUGIN_MANIFEST_REL
    out_experts: list[dict[str, Any]] = []
    assets: dict[str, dict[str, Any]] = {}
    report: list[dict[str, Any]] = []

    for ex in experts:
        if not isinstance(ex, dict):
            continue
        copied = dict(ex)
        avatar = str(copied.get("avatar") or "").strip()
        if avatar and not _external_or_web_asset_uri(avatar):
            src = _safe_repo_relative_file(
                repo,
                avatar,
                manifest_path=manifest_path,
                kind=f"experts[{copied.get('id')}].avatar",
                suffix_mimes=_ASSET_MIME_BY_SUFFIX,
            )
            digest = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
            suffix = src.suffix.lower()
            asset_name = f"{_asset_slug(copied.get('id'))}-{digest}{suffix}"
            dest = shared_home / MANAGED_ASSETS_DIR / plugin_id / asset_name
            mime = _ASSET_MIME_BY_SUFFIX[suffix]
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            copied["avatar"] = _plugin_asset_url(plugin_id, asset_name)
            assets[asset_name] = {
                "kind": "expert_avatar",
                "expert_id": str(copied.get("id") or ""),
                "mime": mime,
                "path": str(dest),
                "source": avatar,
            }
            report.append(
                {
                    "expert_id": str(copied.get("id") or ""),
                    "source": avatar,
                    "asset": asset_name,
                    "url": copied["avatar"],
                    "action": "would-copy" if dry_run else "copied",
                }
            )
        out_experts.append(copied)
    return out_experts, assets, report


# ─────────────────────────── top-level ingest ────────────────────────────

def _default_shared_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()


def ingest(
    repo: Path,
    *,
    audience: str,
    shared_home: Optional[Path] = None,
    profiles_root: Optional[Path] = None,
    dry_run: bool = False,
    force: bool = False,
    allow_create_distribution: bool = False,
) -> dict[str, Any]:
    shared_home = (shared_home or _default_shared_home()).expanduser()
    profiles_root = (profiles_root or shared_home / "profiles").expanduser()
    plugin = load_plugin_manifest(repo)
    aud = resolve_audience(audience, profiles_root=profiles_root)
    existing_status = "active"
    try:
        existing = json.loads(_managed_path(shared_home, plugin["id"]).read_text(encoding="utf-8"))
        if isinstance(existing, dict) and existing.get("status") in {"active", "inactive"}:
            existing_status = str(existing["status"])
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        pass

    report: dict[str, Any] = {
        "plugin_id": plugin["id"],
        "version": plugin.get("version"),
        "dry_run": dry_run,
        "audience": aud.describe(),
        "shared_home": str(shared_home),
    }
    report["governance"] = assert_governance(plugin)
    report["connectors"] = validate_connectors(plugin.get("connectors") or [])
    report["clis"] = install_clis(plugin.get("clis") or [], shared_bin=shared_home / "bin", dry_run=dry_run, force=force)

    if aud.mode == "profile":
        report["skills"] = _install_skills_to_profile(
            plugin, aud, shared_home=shared_home, profiles_root=profiles_root, dry_run=dry_run, force=force
        )
    elif aud.mode == "all":
        report["skills"] = _register_global_distribution(
            plugin, shared_home=shared_home, dry_run=dry_run, allow_create=allow_create_distribution
        )
    else:
        report["skills"] = _register_department_distribution(
            plugin, aud, shared_home=shared_home, dry_run=dry_run, allow_create=allow_create_distribution
        )

    experts, assets, asset_report = _materialize_expert_assets(
        plugin, shared_home=shared_home, dry_run=dry_run
    )
    if asset_report:
        report["assets"] = asset_report

    manifest = {
        "plugin_id": plugin["id"],
        "version": plugin.get("version"),
        "status": existing_status,
        "schema": SUPPORTED_SCHEMA,
        "ingested_at": int(time.time()),
        "audience": {"mode": aud.mode, "profiles": aud.profiles, "department_ids": aud.department_ids},
        "skills": list(plugin["skills"]["list"]),
        # per-profile skills we actually OWN (installed, not skipped-foreign) — uninstall
        # removes ONLY these, never a coexisting employee/other-plugin install.
        "owned_skills": report["skills"].get("owned", {}) if aud.mode == "profile" else {},
        "clis": [c["id"] for c in plugin.get("clis") or []],
        "connectors": [c["id"] for c in plugin.get("connectors") or []],
        "install_mode": plugin.get("install_mode") or "copy",
        # repo + experts[]: the expert Role-Override overlay (expert_overlay.py)
        # reads the persona markdown from `repo`/agent_md at run time and selects
        # by experts[].id. Persona is NEVER copied into SOUL.md (would pollute the
        # default agent); only the pointer + repo path live here.
        "repo": str(plugin.get("_repo") or ""),
        "experts": experts,
        "assets": assets,
    }
    report["managed_manifest"] = str(_write_managed_manifest(shared_home, manifest, dry_run=dry_run))

    # Governance assertion runs AFTER the managed manifest is persisted: if it raises,
    # the partial install is already tracked, so `--uninstall <plugin_id>` cleanly rolls
    # back. (It must run post-install because it inspects the installed skill content.)
    if aud.mode == "profile" and not dry_run:
        report["profile_governance"] = [
            assert_profile_governance(plugin, profiles_root / p) for p in aud.profiles
        ]

    report["note"] = (
        "skills cached in .skills_prompt_snapshot.json — restart the gateway for the "
        "target profile to see the new slash commands."
    )
    return report


def uninstall(
    plugin_id: str,
    *,
    shared_home: Optional[Path] = None,
    profiles_root: Optional[Path] = None,
    dry_run: bool = False,
    purge_clis: bool = False,
    profiles: Optional[list[str]] = None,
) -> dict[str, Any]:
    _safe_component(plugin_id, kind="plugin id")  # CLI arg → managed-manifest filename
    shared_home = (shared_home or _default_shared_home()).expanduser()
    profiles_root = (profiles_root or shared_home / "profiles").expanduser()
    path = _managed_path(shared_home, plugin_id)
    if not path.is_file():
        raise PluginIngestError(f"no managed manifest for plugin {plugin_id!r} at {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))

    report: dict[str, Any] = {"plugin_id": plugin_id, "dry_run": dry_run, "removed": []}
    aud = manifest.get("audience") or {}
    skills = manifest.get("skills") or []
    manifest_kept = False

    if aud.get("mode") == "profile":
        # remove ONLY skills we own; fall back to the flat list for manifests written
        # before owned_skills existed.
        owned = manifest.get("owned_skills")
        if not isinstance(owned, dict) or not owned:
            owned = {p: list(skills) for p in (aud.get("profiles") or [])}
        profile_filter = set(profiles) if profiles is not None else None
        for profile, names in owned.items():
            if profile_filter is not None and profile not in profile_filter:
                continue
            profile_home = profiles_root / profile
            for name in names:
                # COEXISTENCE GUARD: if the personal install no longer points at our
                # source (employee re-installed over it), leave it alone.
                tgt = _personal_install_target(profile_home, name)
                my_source = str(shared_home / "skills" / name)
                if tgt is not None and tgt != my_source:
                    report["removed"].append({"profile": profile, "skill": name, "action": "kept-foreign"})
                    continue
                if dry_run:
                    report["removed"].append({"profile": profile, "skill": name, "action": "would-remove"})
                    continue
                res = uninstall_personal_skill_for_profile(profile_home=profile_home, skill_path=name)
                report["removed"].append({"profile": profile, "skill": name, "action": "removed" if res.get("removed") else "absent"})
        if profile_filter is not None:
            remaining = {p: names for p, names in owned.items() if p not in profile_filter}
            if remaining:
                kept_manifest = dict(manifest)
                kept_audience = dict(aud)
                kept_audience["profiles"] = [p for p in (aud.get("profiles") or []) if p not in profile_filter]
                kept_manifest["audience"] = kept_audience
                kept_manifest["owned_skills"] = remaining
                report["remaining_profiles"] = list(remaining)
                if not dry_run:
                    path.write_text(json.dumps(kept_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                manifest_kept = True
    else:
        # department mode → strip this plugin's entries from skill-distribution.yaml
        config_path = shared_home / SKILL_DISTRIBUTION_FILE
        if config_path.exists():
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            kept = [it for it in (raw.get("skills") or []) if not (isinstance(it, dict) and it.get("plugin") == plugin_id)]
            raw["skills"] = kept
            if not dry_run:
                config_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
            report["removed"].append({"distribution_config": str(config_path), "action": "stripped"})
        # Department-mode skills materialize into profiles via the org sync, not this
        # tool. Removing the distribution entry makes them UNDESIRED; the next
        # `_sync_default_profile_skills` prunes them via `_prune_removed_managed_skills`.
        # Rollback is therefore entry-removal here + prune-on-next-sync (the same
        # lifecycle that distributed them) — report it instead of silently leaving copies.
        report["fanout_rollback"] = (
            "distribution entries removed; already fanned-out copies are now UNDESIRED and "
            "are pruned by the managed sync (`_prune_removed_managed_skills`) on its next run "
            "for each affected profile — run the org skill-sync to complete rollback immediately"
        )

    if purge_clis:
        shared_bin = shared_home / "bin"
        for cid in manifest.get("clis") or []:
            target = shared_bin / cid
            if target.exists():
                if not dry_run:
                    target.unlink()
                report["removed"].append({"cli": cid, "action": "purged"})
    else:
        report["clis_note"] = "shared CLIs left intact (capability shared across plugins); pass --purge-clis to remove"
    if skills:
        report["skills_note"] = (
            "shared skill sources left intact under shared_home/skills because manifest ownership "
            "is not sufficient to prove they belong only to this plugin"
        )

    if not dry_run and not manifest_kept:
        path.unlink()
    report["managed_manifest"] = str(path)
    return report


# ─────────────────────────── CLI entrypoint ──────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="hermes_multitenancy.plugin_ingest", description=__doc__)
    ap.add_argument("repo", nargs="?", help="plugin repo path (carrying .hermes-plugin/plugin.json)")
    ap.add_argument("--audience", help="target: a profile id (feishu_xxxx) or numeric department_ids (comma-separated)")
    ap.add_argument("--uninstall", metavar="PLUGIN_ID", help="roll back a previously ingested plugin by id")
    ap.add_argument("--dry-run", action="store_true", help="print all actions, write nothing")
    ap.add_argument("--force", action="store_true", help="reinstall CLIs/skills even if present")
    ap.add_argument("--allow-create-distribution", action="store_true",
                    help="(department mode) permit CREATING skill-distribution.yaml when absent "
                         "(DANGEROUS: overrides every profile's default-skill source)")
    ap.add_argument("--purge-clis", action="store_true", help="(uninstall) also remove shared CLIs")
    ap.add_argument("--shared-home", type=Path, help="override HERMES_HOME (default ~/.hermes)")
    ap.add_argument("--profiles-root", type=Path, help="override profiles root (default <shared-home>/profiles)")
    ap.add_argument("--json", action="store_true", help="emit the action report as JSON")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.uninstall:
            report = uninstall(
                args.uninstall, shared_home=args.shared_home, profiles_root=args.profiles_root,
                dry_run=args.dry_run, purge_clis=args.purge_clis,
            )
        else:
            if not args.repo or not args.audience:
                raise PluginIngestError("need <repo> and --audience (or --uninstall <plugin-id>)")
            report = ingest(
                Path(args.repo), audience=args.audience, shared_home=args.shared_home,
                profiles_root=args.profiles_root, dry_run=args.dry_run, force=args.force,
                allow_create_distribution=args.allow_create_distribution,
            )
    except PluginIngestError as exc:
        sys.stderr.write(f"plugin-ingest: {exc}\n")
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0


def _print_human(report: dict[str, Any]) -> None:
    tag = "DRY-RUN" if report.get("dry_run") else "DONE"
    if "removed" in report:  # uninstall
        print(f"[{tag}] uninstall {report['plugin_id']}")
        for item in report["removed"]:
            print(f"  - {item}")
        if report.get("clis_note"):
            print(f"  · {report['clis_note']}")
        if report.get("skills_note"):
            print(f"  · {report['skills_note']}")
        return
    print(f"[{tag}] ingest {report['plugin_id']} v{report.get('version')} → {report['audience']}")
    print(f"  governance: env={report['governance']['env_default']}, gates={len(report['governance']['approval_gates'])}")
    print(f"  connectors: {report['connectors']['connectors']}")
    print(f"  clis: {[c['action'] + ':' + c['id'] for c in report['clis']]}")
    if report.get("assets"):
        print(f"  assets: {[a['action'] + ':' + a['asset'] for a in report['assets']]}")
    skills = report["skills"]
    if "installed" in skills:
        print(f"  skills: {len(skills['installed'])} profile-installs; sources={skills['source_actions']}")
    else:
        print(f"  skills: {len(skills['entries'])} distribution entries → {skills['config_path']}")
    print(f"  manifest: {report['managed_manifest']}")
    print(f"  ! {report['note']}")


if __name__ == "__main__":
    raise SystemExit(main())
