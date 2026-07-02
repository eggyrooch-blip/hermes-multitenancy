"""Multitenancy-owned update orchestration primitives.

This module is intentionally deterministic and Linux-friendly: it records
candidate/update state, runs host CLI updates through ``subprocess``-style
runners, and only swaps active shared binaries after every command and copy
step succeeds.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from .lark_cli_canary import lark_cli_canary_preflight
from . import plugin_ingest
from .skill_registry import install_shared_skill_for_profile
from .skill_registry import list_installed_skills, list_profile_skill_slash_commands


UPDATE_CENTER_DIR = "update-center"
DEFAULT_LEDGER_NAME = "ledger.jsonl"
ENV_KEP_NO_AUTO_LOGIN = {"KEP_NO_AUTO_LOGIN": "1"}

_LARK_VERSION_RE = re.compile(r"\bv?(\d+\.\d+\.\d+)\b")
_UPDATE_NOTICE_PATTERNS = [
    re.compile(r"(?im)^.*(?:new version|newer version|update available|upgrade lark-cli).*(?:\n|$)"),
    re.compile(r"(?im)^.*(?:有新版本|新版本可用|建议更新|可升级|升级 lark-cli|更新 lark-cli).*(?:\n|$)"),
    re.compile(r"(?im)^.*(?:lark-cli|kep-cli)\s+update\b.*(?:\n|$)"),
    re.compile(r"(?im)^.*(?:重新打开|重启|restart|reopen).*(?:AI Agent|agent|Skills|skills).*(?:\n|$)"),
    re.compile(r"(?im)^(?:bash|copy)\s*$\n?"),
]
_SECRET_PATTERNS = [
    re.compile(r"SECRET[_A-Z0-9-]*"),
    re.compile(r"(?i)(token|secret|authorization|cookie)\s*[:=]\s*[^,\s\"']+"),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+"),
]


def _now() -> int:
    return int(time.time())


def _redact_text(value: str) -> str:
    out = value
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {str(key): redact(item) for key, item in value.items()}
    return value


def sanitize_user_visible_output(text: str) -> str:
    """Remove host-maintenance update notices before text reaches an end user."""

    cleaned = str(text or "")
    for pattern in _UPDATE_NOTICE_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    return cleaned.strip()


def default_shared_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()


def default_ledger_path(shared_home: Path | None = None) -> Path:
    home = (shared_home or default_shared_home()).expanduser()
    return home / UPDATE_CENTER_DIR / DEFAULT_LEDGER_NAME


class UpdateLedger:
    """Append-only JSONL event ledger for update-center decisions."""

    def __init__(self, path: Path | str | None = None, *, shared_home: Path | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else default_ledger_path(shared_home)

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {
            "ts": _now(),
            "event": event,
            **fields,
        }
        public = redact(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(public, ensure_ascii=False, sort_keys=True) + "\n")
        return public

    def read_events(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        events: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events


@dataclass(frozen=True)
class KepCliSystem:
    system: str
    binary: str
    target_version: str = ""
    installed_version: str | None = None

    @property
    def needs_install(self) -> bool:
        return not self.installed_version

    @property
    def needs_update(self) -> bool:
        return bool(self.installed_version and self.target_version and self.installed_version != self.target_version)


Runner = Callable[[list[str]], Any]
BinaryResolver = Callable[[str], Path | None]


def scan_lark_cli_notice(text: str, *, ledger: UpdateLedger, current_version: str = "") -> dict[str, Any]:
    target = _extract_lark_version(text)
    candidate = {
        "component": "lark-cli",
        "current_version": current_version,
        "target_version": target,
        "risk": "manual-preflight-only",
    }
    record = ledger.append(
        "candidate_detected",
        component="lark-cli",
        target_version=target,
        current_version=current_version,
        auto_apply=False,
    )
    return {
        "candidate": candidate,
        "auto_apply": False,
        "user_visible_output": sanitize_user_visible_output(text),
        "ledger_event": record,
    }


def _extract_lark_version(text: str) -> str:
    matches = _LARK_VERSION_RE.findall(str(text or ""))
    return f"v{matches[-1]}" if matches else ""


def check_lark_cli_candidate(
    *,
    shared_home: Path,
    profile_name: str,
    open_id: str,
    target_version: str,
    ledger: UpdateLedger,
    binary_path: Path | None = None,
) -> dict[str, Any]:
    """Run lark-cli readiness checks without replacing any binary."""

    preflight = lark_cli_canary_preflight(
        shared_home=shared_home,
        profile_name=profile_name,
        open_id=open_id,
        binary_path=binary_path,
    )
    report = {
        "component": "lark-cli",
        "target_version": target_version,
        "auto_apply": False,
        "preflight": preflight,
    }
    ledger.append(
        "preflight_completed",
        component="lark-cli",
        target_version=target_version,
        auto_apply=False,
        ready=bool(preflight.get("ready")),
        missing=preflight.get("missing") or [],
    )
    return redact(report)


def apply_additive_plugin_update(
    repo: Path,
    *,
    audience: str,
    shared_home: Path,
    ledger: UpdateLedger,
    profiles_root: Path | None = None,
) -> dict[str, Any]:
    """Auto-apply only non-destructive skill-only plugin updates."""

    shared_home = shared_home.expanduser()
    profiles_root = (profiles_root or shared_home / "profiles").expanduser()
    plugin = plugin_ingest.load_plugin_manifest(repo)
    risk = _classify_plugin_for_auto_apply(plugin, shared_home=shared_home)
    if not risk["auto_apply"]:
        ledger.append(
            "candidate_quarantined",
            component="plugin",
            plugin_id=plugin["id"],
            reason=risk["reason"],
        )
        return {
            "component": "plugin",
            "plugin_id": plugin["id"],
            "auto_apply": False,
            "applied": False,
            "reason": risk["reason"],
        }

    report = plugin_ingest.ingest(
        repo,
        audience=audience,
        shared_home=shared_home,
        profiles_root=profiles_root,
        dry_run=False,
        force=False,
    )
    verification = _verify_plugin_skills(report, profiles_root=profiles_root)
    missing = [
        {"profile": profile, "missing": data["missing"]}
        for profile, data in verification["profiles"].items()
        if data["missing"]
    ]
    if missing:
        ledger.append("candidate_quarantined", component="plugin", plugin_id=plugin["id"], reason="skill-verification-failed")
        return {
            "component": "plugin",
            "plugin_id": plugin["id"],
            "auto_apply": False,
            "applied": False,
            "reason": "skill-verification-failed",
            "verification": verification,
        }
    ledger.append("plugin_applied", component="plugin", plugin_id=plugin["id"], verification=verification)
    return {
        "component": "plugin",
        "plugin_id": plugin["id"],
        "auto_apply": True,
        "applied": True,
        "report": report,
        "verification": verification,
    }


def _classify_plugin_for_auto_apply(plugin: dict[str, Any], *, shared_home: Path) -> dict[str, Any]:
    if plugin.get("clis"):
        return {"auto_apply": False, "reason": "plugin-declares-cli"}
    if plugin.get("connectors"):
        return {"auto_apply": False, "reason": "plugin-declares-connector"}
    managed_path = shared_home / plugin_ingest.MANAGED_DIR / f"{plugin['id']}.json"
    if managed_path.exists():
        try:
            existing = json.loads(managed_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"auto_apply": False, "reason": "managed-manifest-unreadable"}
        before = {str(item) for item in existing.get("skills") or []}
        after = {str(item) for item in (plugin.get("skills") or {}).get("list") or []}
        if before - after:
            return {"auto_apply": False, "reason": "skill-removal-or-rename"}
    return {"auto_apply": True, "reason": "skill-additive"}


def _verify_plugin_skills(report: dict[str, Any], *, profiles_root: Path) -> dict[str, Any]:
    owned = (report.get("skills") or {}).get("owned") or {}
    profiles: dict[str, Any] = {}
    for profile, skills in owned.items():
        profile_home = profiles_root / profile
        installed = list_installed_skills(profile_home=profile_home)
        slash = list_profile_skill_slash_commands(profile_home=profile_home)
        missing = [skill for skill in skills if skill not in installed]
        profiles[profile] = {
            "expected": list(skills),
            "missing": missing,
            "slash_count": len(slash),
        }
    return {"profiles": profiles}


def sync_kep_cli_systems(
    *,
    systems: Iterable[KepCliSystem | Mapping[str, Any]],
    shared_home: Path,
    ledger: UpdateLedger,
    profiles: Iterable[str] | None = None,
    resolve_binary: BinaryResolver | None = None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Install/update kep-cli systems and atomically sync binaries into shared bin."""

    shared_home = shared_home.expanduser()
    shared_bin = shared_home / "bin"
    shared_bin.mkdir(parents=True, exist_ok=True)
    resolve = resolve_binary or resolve_kep_binary
    run = runner or _run_kep_cli
    rows: list[dict[str, Any]] = []
    normalized_systems: list[KepCliSystem] = [
        raw if isinstance(raw, KepCliSystem) else KepCliSystem(**dict(raw))
        for raw in systems
    ]
    for system in normalized_systems:
        rows.append(_sync_one_kep_system(system, shared_bin=shared_bin, resolve_binary=resolve, runner=run, ledger=ledger))
    skill_rows = ensure_kep_cli_skills(normalized_systems, shared_home=shared_home, profiles=profiles or (), runner=run)
    report = {"component": "kep-cli", "systems": rows, "skills": skill_rows, "secret_free": True}
    ledger.append("kep_cli_sync_completed", component="kep-cli", systems=rows)
    return redact(report)


def _sync_one_kep_system(
    system: KepCliSystem,
    *,
    shared_bin: Path,
    resolve_binary: BinaryResolver,
    runner: Runner,
    ledger: UpdateLedger,
) -> dict[str, Any]:
    if not system.needs_install and not system.needs_update:
        src = resolve_binary(system.binary)
        if src and src.is_file():
            copied = _copy_binary_atomic(src, shared_bin / system.binary)
            return {
                **asdict(system),
                "action": "synced",
                "path": str(copied["path"]),
                "sha256": copied["sha256"],
            }
        return {**asdict(system), "action": "up-to-date", "sha256": ""}

    command = ["kep-cli", "install" if system.needs_install else "update", system.system]
    result = _coerce_result(runner(command))
    if result["returncode"] != 0:
        row = {
            **asdict(system),
            "action": "quarantined",
            "command": command[:2] + [system.system],
            "reason": result["stderr"] or result["stdout"] or f"exit {result['returncode']}",
        }
        ledger.append("candidate_quarantined", component="kep-cli", system=system.system, reason=row["reason"])
        return redact(row)

    src = resolve_binary(system.binary)
    if src is None or not src.is_file():
        row = {
            **asdict(system),
            "action": "quarantined",
            "command": command[:2] + [system.system],
            "reason": f"binary {system.binary} not found after kep-cli {command[1]}",
        }
        ledger.append("candidate_quarantined", component="kep-cli", system=system.system, reason=row["reason"])
        return redact(row)

    copied = _copy_binary_atomic(src, shared_bin / system.binary)
    action = "installed" if system.needs_install else "updated"
    row = {
        **asdict(system),
        "action": action,
        "command": command[:2] + [system.system],
        "path": str(copied["path"]),
        "sha256": copied["sha256"],
    }
    ledger.append("system_synced", component="kep-cli", system=system.system, action=action, sha256=copied["sha256"])
    return row


def ensure_kep_cli_skills(
    systems: Iterable[KepCliSystem],
    *,
    shared_home: Path,
    profiles: Iterable[str],
    category: str = "Keep",
    runner: Runner | None = None,
) -> list[dict[str, Any]]:
    """Ensure each kep-cli system has a shared skill (authoritative embedded content) and profile link.

    Skill content is refreshed from the system CLI's embedded SKILL.md via
    ``kep-cli skills read`` so it stays a matched pair with the binary. Curated
    (human-authored, un-marked) shared skills are never overwritten; a generated
    stub is only a fallback when the system CLI has no embedded skill yet.
    """

    rows: list[dict[str, Any]] = []
    for system in systems:
        skill_path = f"{category}/kep-{system.system}-cli"
        try:
            source = _ensure_kep_cli_skill_source(shared_home, skill_path=skill_path, system=system, runner=runner)
        except ValueError as exc:
            # Real embedded-skill read error (not old-CLI): surface it, leave the
            # existing skill untouched, and keep syncing other systems.
            rows.append({"skill_path": skill_path, "action": "quarantined", "reason": f"skill-refresh-failed: {exc}"})
            continue
        for profile in profiles:
            profile_home = shared_home / "profiles" / profile
            if not profile_home.is_dir():
                rows.append({"profile": profile, "skill_path": skill_path, "action": "quarantined", "reason": "profile missing"})
                continue
            guard = _profile_skill_install_guard(profile_home, skill_path, source)
            if guard:
                rows.append({"profile": profile, "skill_path": skill_path, "action": "quarantined", "reason": guard})
                continue
            installed = install_shared_skill_for_profile(
                shared_home=shared_home,
                profile_home=profile_home,
                skill_path=skill_path,
                source=source,
                version=system.target_version or system.installed_version,
            )
            rows.append({"profile": profile, "skill_path": skill_path, "action": "ensured", "target": installed["target"]})
    return rows


def _profile_skill_install_guard(profile_home: Path, skill_path: str, source: Path) -> str:
    managed = _read_profile_skill_manifest(profile_home / "skills" / ".hermes-managed.json")
    if skill_path in managed:
        return "profile skill already exists in managed manifest"
    target = profile_home / "skills" / skill_path
    if not target.exists() and not target.is_symlink():
        return ""
    try:
        if target.is_symlink() and target.resolve() == source.resolve():
            return ""
    except OSError:
        pass
    return "profile skill already exists"


def _read_profile_skill_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    skills = raw.get("skills") if isinstance(raw, dict) else None
    return skills if isinstance(skills, dict) else {}


KEP_CLI_MANAGED_MARKER = ".kep-cli-managed"


def read_kep_cli_skill_files(system: str, *, runner: Runner | None = None) -> list[dict[str, str]] | None:
    """Read a system CLI's embedded skill files via ``kep-cli skills list/read``.

    Returns ``[{"path", "content"}]`` for every embedded file, or ``None`` when
    the system CLI predates embedded skills (``unknown command "skills"``) or any
    read fails — the caller then leaves the existing skill untouched (graceful
    degradation while the per-system CLI rollout is in progress).
    """

    run = runner or _run_kep_cli
    listed = _coerce_result(run(["kep-cli", "skills", "list", system]))
    if listed["returncode"] != 0:
        blob = f"{listed['stderr']} {listed['stdout']}".lower()
        if "unknown command" in blob:
            return None  # older system CLI without embedded skills -> graceful skip
        # skills command exists but failed (auth/runtime): a real error, not old-CLI.
        raise ValueError(f"kep-cli skills list {system} failed: {(listed['stderr'] or listed['stdout']).strip()}")
    try:
        entries = json.loads(listed["stdout"] or "null")
    except json.JSONDecodeError as exc:
        raise ValueError(f"kep-cli skills list {system} returned non-JSON: {exc}") from exc
    if entries is None:
        return None  # explicit no-skills
    if not isinstance(entries, list):
        raise ValueError(f"kep-cli skills list {system} must return a JSON array")
    files: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rel = str(entry.get("path") or "").strip()
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            continue  # never write outside the skill source dir
        read = _coerce_result(run(["kep-cli", "skills", "read", system, rel]))
        if read["returncode"] != 0:
            # list succeeded, so the CLI advertises this file; a read failure is real, not old-CLI.
            raise ValueError(f"kep-cli skills read {system} {rel} failed: {(read['stderr'] or read['stdout']).strip()}")
        files.append({"path": rel, "content": read["stdout"]})
    return files or None


def _is_kep_cli_managed(source: Path) -> bool:
    return (source / KEP_CLI_MANAGED_MARKER).exists()


def _write_managed_skill_source(
    source: Path, files: list[dict[str, str]], *, marker_name: str = KEP_CLI_MANAGED_MARKER
) -> bool:
    """Write authoritative embedded files byte-exact; mark the source CLI-managed.

    Idempotent — only rewrites files whose content changed. Returns True if
    anything changed.
    """

    source.mkdir(parents=True, exist_ok=True)
    marker = source / marker_name
    wanted = {source / item["path"] for item in files}
    changed = False
    for item in files:
        target = source / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        content = item["content"]
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            target.write_text(content, encoding="utf-8")
            changed = True
    # Mirror the embedded set exactly: prune files that disappeared upstream.
    # Safe because this source is fully kep-cli-managed (curated dirs never reach here).
    for existing in list(source.rglob("*")):
        if existing.is_file() and existing != marker and existing not in wanted:
            existing.unlink()
            changed = True
    for stale_dir in sorted((p for p in source.rglob("*") if p.is_dir()), reverse=True):
        try:
            stale_dir.rmdir()  # only removes now-empty dirs
        except OSError:
            pass
    if not marker.exists():
        marker.write_text("", encoding="utf-8")
    return changed


def _write_stub_skill_source(source: Path, system: KepCliSystem) -> None:
    source.mkdir(parents=True, exist_ok=True)
    title = f"kep {system.system} CLI"
    (source / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                f"name: kep-{system.system}-cli",
                f"title: {title}",
                f"description: Run {system.binary} from the Hermes managed kep-cli sandbox path.",
                "---",
                f"# {title}",
                "",
                "Use this skill when the current task needs this Keep/Kep business CLI.",
                f"Run `{system.binary} --help` first when command arguments are unclear.",
                "Do not ask the user to install or update host CLIs; Multitenancy manages updates.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (source / KEP_CLI_MANAGED_MARKER).write_text("", encoding="utf-8")


def _ensure_kep_cli_skill_source(
    shared_home: Path, *, skill_path: str, system: KepCliSystem, runner: Runner | None = None
) -> Path:
    source = shared_home / "skills" / skill_path
    skill_md = source / "SKILL.md"
    # Curated (human-authored, un-marked) skill present -> never touch it.
    if skill_md.exists() and not _is_kep_cli_managed(source):
        return source
    # Authoritative path: refresh from the system CLI's embedded SKILL.md.
    files = read_kep_cli_skill_files(system.system, runner=runner)
    if files:
        _write_managed_skill_source(source, files)
        return source
    # Embedded skill not available yet (older system CLI) -> keep or stub.
    if skill_md.exists():
        return source
    _write_stub_skill_source(source, system)
    return source


def _run_kep_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **ENV_KEP_NO_AUTO_LOGIN}
    return subprocess.run(argv, capture_output=True, text=True, check=False, env=env)


def _coerce_result(result: Any) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return {
            "returncode": int(result.get("returncode", 0)),
            "stdout": str(result.get("stdout") or ""),
            "stderr": str(result.get("stderr") or ""),
        }
    return {
        "returncode": int(getattr(result, "returncode", 0)),
        "stdout": str(getattr(result, "stdout", "") or ""),
        "stderr": str(getattr(result, "stderr", "") or ""),
    }


def resolve_kep_binary(binary: str) -> Path | None:
    found = shutil.which(binary)
    if found:
        return Path(found).resolve()
    base = Path.home() / ".kep-cli" / "systems"
    for pattern in (f"*/bin/{binary}", f"*/{binary}"):
        for candidate in base.glob(pattern):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
    return None


DEFAULT_TARGET_MARKER = "latest"


def _read_installed_kep_version(system: str) -> str | None:
    """Best-effort read of the locally installed version from the kep-cli version file.

    kep-cli writes ``~/.kep-cli/systems/<system>/version`` as small YAML whose
    first ``version:`` line holds the installed version string.
    """

    version_file = Path.home() / ".kep-cli" / "systems" / system / "version"
    try:
        text = version_file.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version:"):
            value = stripped.split(":", 1)[1].strip()
            return value or None
    return None


def build_kep_systems_from_registry(
    *,
    runner: Runner | None = None,
    version_reader: Callable[[str], str | None] | None = None,
    include_developing: bool = False,
    target_marker: str = DEFAULT_TARGET_MARKER,
) -> list[KepCliSystem]:
    """Derive the kep-cli sync manifest live from ``kep-cli list --json``.

    Removes the hand-maintained systems-file: a system newly registered in the
    aidock registry surfaces here on the next sync. ``installed=false`` rows
    become install candidates (``installed_version=None``); installed rows
    become update candidates by pinning ``target_version`` to a never-equal
    marker so :class:`KepCliSystem` reports ``needs_update``.
    """

    run = runner or _run_kep_cli
    read_version = version_reader or _read_installed_kep_version
    argv = ["kep-cli", "list", "--json"]
    if include_developing:
        argv.append("--all")
    result = _coerce_result(run(argv))
    if result["returncode"] != 0:
        detail = (result["stderr"] or result["stdout"] or "no output").strip()
        raise ValueError(f"kep-cli list --json failed (exit {result['returncode']}): {detail}")
    try:
        rows = json.loads(result["stdout"] or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"kep-cli list --json returned non-JSON output: {exc}") from exc
    if not isinstance(rows, list):
        raise ValueError("kep-cli list --json must return a JSON array")

    allowed_statuses = {"active"} | ({"developing"} if include_developing else set())
    systems: list[KepCliSystem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        binary = str(row.get("bin_name") or "").strip()
        if not name or not binary:
            continue
        status = str(row.get("status") or "").strip()
        if status not in allowed_statuses:  # never touch deprecated/disabled rows
            continue
        installed = bool(row.get("installed"))
        systems.append(
            KepCliSystem(
                system=name,
                binary=binary,
                target_version=target_marker,
                installed_version=read_version(name) if installed else None,
            )
        )
    return systems


LARK_CLI_MANAGED_MARKER = ".lark-cli-managed"


def _run_lark_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _lark_payload_or_error(result: Mapping[str, Any], *, context: str) -> dict[str, Any]:
    """Parse a lark-cli skills JSON payload; lark-cli reports errors as exit 0 + {ok:false}."""

    try:
        payload = json.loads(result["stdout"] or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{context} returned non-JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ValueError(f"{context} returned not-ok payload: {str(payload)[:200]}")
    return payload


def list_lark_cli_skills(*, runner: Runner | None = None) -> list[str]:
    """List embedded skill names from the local lark-cli binary.

    Returns [] when the local lark-cli predates the `skills` verb.
    """

    run = runner or _run_lark_cli
    result = _coerce_result(run(["lark-cli", "skills", "list"]))
    if result["returncode"] != 0:
        blob = f"{result['stderr']} {result['stdout']}".lower()
        if "unknown command" in blob:
            return []
        raise ValueError(f"lark-cli skills list failed: {(result['stderr'] or result['stdout']).strip()}")
    payload = _lark_payload_or_error(result, context="lark-cli skills list")
    rows = payload.get("skills")
    if not isinstance(rows, list):
        raise ValueError("lark-cli skills list must return {'skills': [...]}")
    names: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            name = str(row.get("name") or "").strip()
            if name and "/" not in name and ".." not in name:
                names.append(name)
    return names


def read_lark_cli_skill_files(skill: str, *, runner: Runner | None = None) -> list[dict[str, str]] | None:
    """Read one skill's embedded files (SKILL.md + references/*) from lark-cli.

    Returns ``[{"path", "content"}]`` with paths relative to the skill dir, or
    ``None`` when the local lark-cli predates the ``skills`` verb. A failure on
    a file the CLI itself advertised is a real error and raises ValueError.
    """

    run = runner or _run_lark_cli
    files: list[dict[str, str]] = []
    queue = [skill]
    first = True
    while queue:
        path = queue.pop(0)
        listed = _coerce_result(run(["lark-cli", "skills", "list", path]))
        if listed["returncode"] != 0:
            blob = f"{listed['stderr']} {listed['stdout']}".lower()
            if first and "unknown command" in blob:
                return None
            raise ValueError(f"lark-cli skills list {path} failed: {(listed['stderr'] or listed['stdout']).strip()}")
        first = False
        payload = _lark_payload_or_error(listed, context=f"lark-cli skills list {path}")
        for entry in payload.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            epath = str(entry.get("path") or "").strip()
            if not epath or epath.startswith("/") or ".." in epath.split("/"):
                continue  # never write outside the skill source dir
            if entry.get("is_dir"):
                queue.append(epath)
                continue
            read = _coerce_result(run(["lark-cli", "skills", "read", epath]))
            if read["returncode"] != 0:
                raise ValueError(f"lark-cli skills read {epath} failed: {(read['stderr'] or read['stdout']).strip()}")
            body = read["stdout"]
            if body.lstrip().startswith("{"):
                # lark-cli reports read errors as exit 0 + {ok:false} JSON.
                try:
                    maybe = json.loads(body)
                except json.JSONDecodeError:
                    maybe = None
                if isinstance(maybe, dict) and maybe.get("ok") is False:
                    raise ValueError(f"lark-cli skills read {epath} failed: {str(maybe.get('error'))[:200]}")
            rel = epath[len(skill) + 1:] if epath.startswith(f"{skill}/") else epath
            files.append({"path": rel, "content": body})
    return files or None


def sync_lark_cli_skills(
    *,
    shared_home: Path,
    ledger: UpdateLedger,
    skills: Iterable[str] | None = None,
    adopt: bool = False,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Mirror lark-cli embedded skills into shared sources (pool-only, no fan-out).

    Never touches lark-cli/authsidecar binaries (update-center safety boundary).
    Existing un-adopted sources (e.g. legacy `npx skills add` snapshots) are
    skipped unless ``adopt=True`` takes them over and stamps them
    ``.lark-cli-managed``; after adoption they refresh like any managed source.
    """

    shared_home = shared_home.expanduser()
    run = runner or _run_lark_cli
    names = [str(name) for name in skills] if skills else list_lark_cli_skills(runner=run)
    rows: list[dict[str, Any]] = []
    for name in names:
        source = shared_home / "skills" / name
        skill_md = source / "SKILL.md"
        managed = (source / LARK_CLI_MANAGED_MARKER).exists()
        if skill_md.exists() and not managed and not adopt:
            rows.append({"skill": name, "action": "skipped", "reason": "existing un-adopted source (run with --adopt to take over)"})
            continue
        try:
            files = read_lark_cli_skill_files(name, runner=run)
        except ValueError as exc:
            rows.append({"skill": name, "action": "quarantined", "reason": f"skill-refresh-failed: {exc}"})
            continue
        if files is None:
            rows.append({"skill": name, "action": "skipped", "reason": "local lark-cli has no embedded skills verb"})
            continue
        changed = _write_managed_skill_source(source, files, marker_name=LARK_CLI_MANAGED_MARKER)
        rows.append({"skill": name, "action": "refreshed" if changed else "up-to-date", "files": len(files)})
    report = {"component": "lark-cli-skills", "skills": rows, "secret_free": True}
    ledger.append("lark_skill_sync_completed", component="lark-cli-skills", skills=rows)
    return redact(report)


def _copy_binary_atomic(src: Path, dst: Path) -> dict[str, Any]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.tmp-{os.getpid()}")
    try:
        shutil.copy2(src, tmp)
        tmp.chmod(0o755)
        digest = _sha256(tmp)
        os.replace(tmp, dst)
        return {"path": dst, "sha256": digest}
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
