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
    python -m hermes_multitenancy.plugin_ingest <plugin-repo> --audience <ids|profile>
    python -m hermes_multitenancy.plugin_ingest <plugin-repo> --audience X --dry-run
    python -m hermes_multitenancy.plugin_ingest --uninstall <plugin-id>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
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
SKILL_DISTRIBUTION_FILE = "skill-distribution.yaml"
ENV_KEP_NO_AUTO_LOGIN = {"KEP_NO_AUTO_LOGIN": "1"}


class PluginIngestError(RuntimeError):
    """Any operator-facing failure (bad manifest, unknown audience, missing connector)."""


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

    skills = data.get("skills") or {}
    if not isinstance(skills, dict) or not isinstance(skills.get("list"), list) or not skills["list"]:
        raise PluginIngestError(f"{path}: skills.list must be a non-empty array")

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
    for con in data.get("connectors") or []:
        if not isinstance(con, dict) or "id" not in con:
            raise PluginIngestError(f"{path}: each connectors[] entry needs id, got {con!r}")

    gov = data.get("governance") or {}
    if gov.get("env_default") not in (None, "pre"):
        raise PluginIngestError(
            f"{path}: governance.env_default={gov.get('env_default')!r} — online-by-default is "
            "forbidden; a plugin distributed to many employees must default to `pre`"
        )

    data["_repo"] = str(repo)
    return data


# ─────────────────────────── audience resolution ─────────────────────────

@dataclass
class Audience:
    mode: str  # "profile" | "department_ids"
    profiles: list[str] = field(default_factory=list)
    department_ids: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.mode == "profile":
            return f"profiles={self.profiles}"
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
    tokens = [t.strip() for t in raw.split(",") if t.strip()]

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

def _register_shared_skill_source(repo: Path, shared_skills: Path, name: str, *, dry_run: bool, force: bool) -> str:
    """Copy the plugin's skill dir into the shared distribution source."""
    src = repo / "skills" / name
    if not src.is_dir():
        raise PluginIngestError(f"plugin declares skill {name!r} but {src} is missing")
    dst = shared_skills / name
    if dst.exists() and not force:
        return "present"
    if dry_run:
        return "would-register"
    shared_skills.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return "registered"


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
    source_actions = {name: _register_shared_skill_source(repo, shared_skills, name, dry_run=dry_run, force=force) for name in names}

    installed: list[dict[str, Any]] = []
    for profile in audience.profiles:
        profile_home = profiles_root / profile
        if not profile_home.is_dir():
            raise PluginIngestError(f"profile home {profile_home} does not exist")
        for name in names:
            if dry_run:
                installed.append({"profile": profile, "skill": name, "action": "would-install"})
                continue
            install_shared_skill_for_profile(
                shared_home=shared_home,
                profile_home=profile_home,
                skill_path=name,
                source=shared_skills / name,
                version=version,
            )
            installed.append({"profile": profile, "skill": name, "action": "ensured"})
    return {"source_actions": source_actions, "installed": installed}


def _register_department_distribution(
    plugin: dict[str, Any],
    audience: Audience,
    *,
    shared_home: Path,
    dry_run: bool,
) -> dict[str, Any]:
    """Add audience-scoped entries to shared `skill-distribution.yaml` for the org fan-out."""
    repo = Path(plugin["_repo"])
    shared_skills = shared_home / "skills"
    names = list(plugin["skills"]["list"])
    install_mode = plugin.get("install_mode") or "copy"
    for name in names:  # ensure source registered so the fan-out has something to copy
        _register_shared_skill_source(repo, shared_skills, name, dry_run=dry_run, force=False)

    config_path = shared_home / SKILL_DISTRIBUTION_FILE
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    skills_list = raw.get("skills")
    if not isinstance(skills_list, list):
        skills_list = []
    by_path = {str(item.get("path")): item for item in skills_list if isinstance(item, dict)}

    plugin_id = plugin["id"]
    entries = []
    for name in names:
        entry = {
            "path": name,
            "install_mode": install_mode,
            "audience": {"department_ids": list(audience.department_ids)},
            "plugin": plugin_id,
        }
        entries.append(entry)
        by_path[name] = entry
    raw["skills"] = sorted(by_path.values(), key=lambda it: str(it.get("path")))

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
    gov = plugin.get("governance") or {}
    env_default = gov.get("env_default") or "pre"
    if env_default != "pre":
        raise PluginIngestError(f"governance.env_default={env_default!r} must be 'pre' for distribution")
    gates = list(gov.get("approval_required") or [])
    if not gates:
        # not fatal, but loud: a plugin with high-risk writes and no gates is suspicious
        sys.stderr.write("WARN: plugin declares no approval_required gates\n")
    return {"env_default": env_default, "approval_gates": gates, "online_requires": gov.get("online_requires") or "explicit_action"}


# ─────────────────────────── managed manifest ────────────────────────────

def _managed_path(shared_home: Path, plugin_id: str) -> Path:
    return shared_home / MANAGED_DIR / f"{plugin_id}.json"


def _write_managed_manifest(shared_home: Path, manifest: dict[str, Any], *, dry_run: bool) -> Path:
    path = _managed_path(shared_home, manifest["plugin_id"])
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


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
) -> dict[str, Any]:
    shared_home = (shared_home or _default_shared_home()).expanduser()
    profiles_root = (profiles_root or shared_home / "profiles").expanduser()
    plugin = load_plugin_manifest(repo)
    aud = resolve_audience(audience, profiles_root=profiles_root)

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
    else:
        report["skills"] = _register_department_distribution(plugin, aud, shared_home=shared_home, dry_run=dry_run)

    manifest = {
        "plugin_id": plugin["id"],
        "version": plugin.get("version"),
        "schema": SUPPORTED_SCHEMA,
        "ingested_at": int(time.time()),
        "audience": {"mode": aud.mode, "profiles": aud.profiles, "department_ids": aud.department_ids},
        "skills": list(plugin["skills"]["list"]),
        "clis": [c["id"] for c in plugin.get("clis") or []],
        "connectors": [c["id"] for c in plugin.get("connectors") or []],
        "install_mode": plugin.get("install_mode") or "copy",
    }
    report["managed_manifest"] = str(_write_managed_manifest(shared_home, manifest, dry_run=dry_run))
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
) -> dict[str, Any]:
    shared_home = (shared_home or _default_shared_home()).expanduser()
    profiles_root = (profiles_root or shared_home / "profiles").expanduser()
    path = _managed_path(shared_home, plugin_id)
    if not path.is_file():
        raise PluginIngestError(f"no managed manifest for plugin {plugin_id!r} at {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))

    report: dict[str, Any] = {"plugin_id": plugin_id, "dry_run": dry_run, "removed": []}
    aud = manifest.get("audience") or {}
    skills = manifest.get("skills") or []

    if aud.get("mode") == "profile":
        for profile in aud.get("profiles") or []:
            profile_home = profiles_root / profile
            for name in skills:
                if dry_run:
                    report["removed"].append({"profile": profile, "skill": name, "action": "would-remove"})
                    continue
                res = uninstall_personal_skill_for_profile(profile_home=profile_home, skill_path=name)
                report["removed"].append({"profile": profile, "skill": name, "action": "removed" if res.get("removed") else "absent"})
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

    if not dry_run:
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
    skills = report["skills"]
    if "installed" in skills:
        print(f"  skills: {len(skills['installed'])} profile-installs; sources={skills['source_actions']}")
    else:
        print(f"  skills: {len(skills['entries'])} distribution entries → {skills['config_path']}")
    print(f"  manifest: {report['managed_manifest']}")
    print(f"  ! {report['note']}")


if __name__ == "__main__":
    raise SystemExit(main())
