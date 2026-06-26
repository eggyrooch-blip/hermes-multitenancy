"""Skill distribution, personal installs, and audit helpers.

Organization-managed skills are maintained by Feishu sync.  User-installed
skills are allowed, but tracked separately so sync never deletes them by
mistake and audits can distinguish managed code from local additions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Optional

MANAGED_SKILL_MANIFEST = ".hermes-managed.json"
PERSONAL_SKILL_MANIFEST = ".hermes-personal-installs.json"
_SKILL_IGNORED_DIRS = {".git", ".github", ".hub", ".archive", "__pycache__"}
_SKILL_LINKED_DEP_DIRS = {"node_modules", ".venv", "venv"}
_SKILL_SECRET_FILE_NAMES = {".env", ".env.local", ".npmrc", ".netrc", "auth.json", "feishu_uat.json"}
_SKILL_SECRET_NAME_PARTS = ("token", "secret", "credential", "password", "passwd", "apikey", "api_key")
_SKILL_SOURCE_FILE_SUFFIXES = (
    ".py",
    ".pyi",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".rb",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".swift",
    ".php",
    ".pl",
    ".lua",
    ".md",
    ".txt",
)


def install_shared_skill_for_profile(
    *,
    shared_home: Path,
    profile_home: Path,
    skill_path: str,
    source: str | Path | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """Install a shared skill into one profile as a personal symlink."""
    shared = Path(shared_home).expanduser()
    profile = Path(profile_home).expanduser()
    rel_path = _safe_skill_relative_path(skill_path)
    src = _safe_source_path(shared, source) if source is not None else shared / "skills" / rel_path
    if not src.is_dir():
        raise FileNotFoundError(str(src))

    dst = profile / "skills" / rel_path
    install_meta = _install_personal_skill(src, dst)
    manifest = _read_manifest(profile, PERSONAL_SKILL_MANIFEST)
    manifest[str(rel_path)] = {
        "source": "personal",
        "target": str(src),
        **install_meta,
    }
    if version:
        manifest[str(rel_path)]["version"] = version
    _write_manifest(profile, PERSONAL_SKILL_MANIFEST, manifest)
    return {
        "installed": True,
        "skill_path": str(rel_path),
        "target": str(src),
        "source": "personal",
        "version": version,
        **install_meta,
    }


def uninstall_personal_skill_for_profile(
    *,
    profile_home: Path,
    skill_path: str,
) -> dict[str, Any]:
    """Remove a personal install without touching organization-managed skills."""
    profile = Path(profile_home).expanduser()
    rel_path = _safe_skill_relative_path(skill_path)
    personal = _read_manifest(profile, PERSONAL_SKILL_MANIFEST)
    managed = _read_manifest(profile, MANAGED_SKILL_MANIFEST)
    removed = False
    if str(rel_path) in personal and str(rel_path) not in managed:
        target = profile / "skills" / rel_path
        if target.exists() or target.is_symlink():
            shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
            removed = True
    personal.pop(str(rel_path), None)
    _write_manifest(profile, PERSONAL_SKILL_MANIFEST, personal)
    return {
        "removed": removed,
        "skill_path": str(rel_path),
    }


def list_installed_skills(*, profile_home: Path) -> dict[str, dict[str, Any]]:
    profile = Path(profile_home).expanduser()
    managed = _read_manifest(profile, MANAGED_SKILL_MANIFEST)
    personal = _read_manifest(profile, PERSONAL_SKILL_MANIFEST)
    result: dict[str, dict[str, Any]] = {}
    for rel_path, meta in managed.items():
        result[rel_path] = {
            **(meta if isinstance(meta, dict) else {}),
            "source": "managed",
        }
    for rel_path, meta in personal.items():
        if rel_path in result:
            continue
        result[rel_path] = {
            **(meta if isinstance(meta, dict) else {}),
            "source": "personal",
        }
    skills_root = profile / "skills"
    if skills_root.is_dir():
        for skill_md in _iter_skill_files(skills_root):
            rel = str(skill_md.parent.relative_to(skills_root))
            result.setdefault(rel, {"source": "unknown"})
    return dict(sorted(result.items()))


def list_profile_skill_slash_commands(*, profile_home: Path) -> list[dict[str, Any]]:
    """Return redacted slash-command metadata for one profile's installed skills."""
    profile = Path(profile_home).expanduser()
    skills_root = profile / "skills"
    if not skills_root.is_dir():
        return []

    commands: dict[str, dict[str, Any]] = {}
    for skill_md in _iter_skill_files(skills_root):
        try:
            rel = skill_md.parent.relative_to(skills_root)
        except ValueError:
            continue
        meta = _read_skill_doc_metadata(skill_md)
        name = _slash_command_name(meta.get("name") or rel.name)
        if not name:
            continue
        category = rel.parts[0] if len(rel.parts) > 1 else ""
        _add_profile_slash_command(commands, {
            "name": name,
            "slash": f"/{name}",
            "title": meta.get("title") or name,
            "description": meta.get("description") or "",
            "source": "skill",
            "type": "skill",
            "category": category,
        })
        # short-command aliases this skill self-declares (frontmatter `slash_aliases`)
        for alias in read_skill_slash_aliases(skill_md):
            if alias == name:
                continue  # skip self-aliases
            _add_profile_slash_command(commands, {
                "name": alias,
                "slash": f"/{alias}",
                "title": meta.get("title") or alias,
                "description": meta.get("description") or "",
                "source": "skill-alias",
                "type": "skill",
                "category": category,
                "skill": name,
            })
    return sorted(commands.values(), key=lambda item: (str(item.get("category") or ""), str(item.get("name") or "")))


def _profile_slash_command_score(command: dict[str, Any]) -> tuple[int, int, int, int]:
    """Prefer the most useful one-line picker row for duplicate slash names."""
    source = str(command.get("source") or "")
    category = str(command.get("category") or "")
    title = str(command.get("title") or "")
    name = str(command.get("name") or "")
    description = str(command.get("description") or "")
    return (
        2 if source == "skill" else 1,
        1 if category else 0,
        1 if title and title != name else 0,
        len(description),
    )


def _add_profile_slash_command(commands: dict[str, dict[str, Any]], command: dict[str, Any]) -> None:
    slash = str(command.get("slash") or "")
    if not slash:
        return
    current = commands.get(slash)
    if current is None or _profile_slash_command_score(command) > _profile_slash_command_score(current):
        commands[slash] = command


def read_skill_slash_aliases(path: Path) -> list[str]:
    """A skill's self-declared `slash_aliases` (frontmatter list or str) → sanitized
    short-command tokens. Used both by the picker list and the slash resolver so a
    skill's short commands are per-profile and zero-maintenance.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end < 0:
        return []
    try:
        import yaml

        frontmatter = yaml.safe_load(text[4:end]) or {}
    except Exception:
        return []
    if not isinstance(frontmatter, dict):
        return []
    value = frontmatter.get("slash_aliases")
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        token = str(item or "").strip().lstrip("/").replace("_", "-")
        if token and all(ch.isalnum() or ch == "-" for ch in token):
            out.append(token)
    return out


def _slash_command_name(value: Any) -> str:
    raw = str(value or "").strip().lstrip("/")
    if not raw:
        return ""
    return raw if all(ch.isalnum() or ch in {"-", "_"} for ch in raw) else ""


def _read_skill_doc_metadata(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    frontmatter: dict[str, str] = {}
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            raw_frontmatter = text[4:end]
            frontmatter = _parse_simple_frontmatter(raw_frontmatter)

    title = str(frontmatter.get("title") or "").strip()
    description = str(frontmatter.get("description") or "").strip()

    return {
        "name": str(frontmatter.get("name") or "").strip(),
        "title": title,
        "description": description,
    }


def _parse_simple_frontmatter(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line or line[:1].isspace():
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key in {"name", "title", "description"}:
            result[key] = value
    return result


def audit_installed_skills(
    *,
    shared_home: Path,
    profiles_root: Path | None = None,
) -> dict[str, Any]:
    """Scan profiles and report skill inventory without reading secret content."""
    shared = Path(shared_home).expanduser()
    profiles = Path(profiles_root).expanduser() if profiles_root is not None else shared / "profiles"
    report: dict[str, Any] = {
        "version": 1,
        "profiles": {},
    }
    if not profiles.is_dir():
        return report
    from .curator_adapter import build_curator_dry_run_plan, load_usage

    for profile_home in sorted(p for p in profiles.iterdir() if p.is_dir()):
        skills_root = profile_home / "skills"
        installed = list_installed_skills(profile_home=profile_home)
        curator_usage = load_usage(profile_home=profile_home)
        rows: list[dict[str, Any]] = []
        for rel_path, meta in installed.items():
            target = skills_root / rel_path
            row = _audit_one_skill(
                profile_home=profile_home,
                shared_home=shared,
                rel_path=rel_path,
                target=target,
                meta=meta,
                curator_usage=curator_usage,
            )
            rows.append(row)
        report["profiles"][profile_home.name] = {
            "curator": {
                "dry_run_plan": build_curator_dry_run_plan(profile_home=profile_home),
            },
            "skills": rows,
        }
    return report


def _audit_one_skill(
    *,
    profile_home: Path,
    shared_home: Path,
    rel_path: str,
    target: Path,
    meta: dict[str, Any],
    curator_usage: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    is_symlink = target.is_symlink()
    resolved: Optional[Path] = None
    if target.exists() or target.is_symlink():
        try:
            resolved = target.resolve(strict=True)
        except FileNotFoundError:
            warnings.append("broken_symlink")
    if is_symlink and resolved is not None:
        try:
            resolved.relative_to((shared_home / "skills").resolve())
        except ValueError:
            try:
                resolved.relative_to(shared_home.resolve())
            except ValueError:
                warnings.append("symlink_target_outside_shared_skills")

    skill_md = target / "SKILL.md"
    digest = _hash_file(skill_md) if skill_md.is_file() else None
    token_present = _token_files_present(profile_home, rel_path)
    row = {
        "skill_path": rel_path,
        "source": str(meta.get("source") or "unknown"),
        "install_mode": "symlink" if is_symlink else "directory",
        "target": str(resolved or target),
        "version": meta.get("version"),
        "skill_md_sha256": digest,
        "token_files_present": token_present,
        "warnings": warnings,
    }
    from .curator_adapter import curator_metadata_for_skill

    row["curator"] = curator_metadata_for_skill(
        profile_home=profile_home,
        skill_path=rel_path,
        skill_md=skill_md,
        source=row["source"],
        usage=curator_usage,
    )
    return row


def _token_files_present(profile_home: Path, rel_path: str) -> bool:
    names = {
        Path(rel_path).name,
        Path(rel_path).name.replace("_", "-"),
        Path(rel_path).name.replace("-", "_"),
    }
    tokens = profile_home / "tokens"
    if not tokens.is_dir():
        return False
    for item in tokens.iterdir():
        if not item.is_file():
            continue
        stem = item.name.rsplit(".", 1)[0]
        if stem in names:
            return True
    return False


def _hash_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _iter_skill_files(skills_root: Path) -> list[Path]:
    matches: list[Path] = []
    seen_dirs: set[Path] = set()
    for root, dirs, files in os.walk(skills_root, followlinks=True):
        try:
            real_root = Path(root).resolve(strict=True)
        except OSError:
            dirs[:] = []
            continue
        if real_root in seen_dirs:
            dirs[:] = []
            continue
        seen_dirs.add(real_root)

        keep_dirs: list[str] = []
        for dirname in dirs:
            if dirname in {".git", ".github", ".hub", ".archive"}:
                continue
            child = Path(root) / dirname
            try:
                real_child = child.resolve(strict=True)
            except OSError:
                continue
            if real_child in seen_dirs:
                continue
            keep_dirs.append(dirname)
        dirs[:] = [d for d in dirs if d not in {".git", ".github", ".hub", ".archive"}]
        dirs[:] = keep_dirs
        if "SKILL.md" in files:
            matches.append(Path(root) / "SKILL.md")
    return sorted(matches, key=lambda p: str(p))


def _safe_skill_relative_path(value: Any) -> Path:
    raw = str(value or "").strip()
    path = Path(raw)
    if not raw or path.is_absolute():
        raise ValueError("invalid skill path")
    if any(part in ("", ".", "..") or part.startswith(".") for part in path.parts):
        raise ValueError("invalid skill path")
    return path


def _safe_source_path(shared_home: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    rel = _safe_skill_relative_path(str(path))
    return shared_home / rel


def _link_skill(src: Path, dst: Path) -> None:
    if _same_symlink(dst, src):
        return
    if dst.exists() or dst.is_symlink():
        shutil.rmtree(dst) if dst.is_dir() and not dst.is_symlink() else dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(src, target_is_directory=True)


def _install_personal_skill(src: Path, dst: Path) -> dict[str, Any]:
    if _skill_tree_has_secret_files(src):
        _copy_skill_tree(src, dst)
        return {
            "install_mode": "copy",
            "requested_install_mode": "symlink",
            "secret_guard": "copy_filtered",
        }
    _link_skill(src, dst)
    return {"install_mode": "symlink"}


def _copy_skill_tree(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        shutil.rmtree(dst) if dst.is_dir() and not dst.is_symlink() else dst.unlink()
    for root, dirs, files in os.walk(src):
        root_path = Path(root)
        root_rel = root_path.relative_to(src)
        target_root = dst / root_rel if root_rel.parts else dst
        target_root.mkdir(parents=True, exist_ok=True)

        keep_dirs: list[str] = []
        for dirname in dirs:
            item = root_path / dirname
            rel = item.relative_to(src)
            target = dst / rel
            if item.is_symlink() or dirname in _SKILL_IGNORED_DIRS:
                continue
            if dirname in _SKILL_LINKED_DEP_DIRS:
                if not _same_symlink(target, item):
                    if target.exists() or target.is_symlink():
                        shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(item, target_is_directory=True)
                continue
            keep_dirs.append(dirname)
        dirs[:] = keep_dirs

        for filename in files:
            item = root_path / filename
            if item.is_symlink():
                continue
            rel = item.relative_to(src)
            if _is_secret_skill_file(rel):
                continue
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _skill_tree_has_secret_files(src: Path) -> bool:
    for root, dirs, files in os.walk(src):
        root_path = Path(root)
        keep_dirs: list[str] = []
        for dirname in dirs:
            item = root_path / dirname
            if item.is_symlink() or dirname in _SKILL_IGNORED_DIRS:
                continue
            keep_dirs.append(dirname)
        dirs[:] = keep_dirs
        for filename in files:
            item = root_path / filename
            if item.is_symlink():
                continue
            if _is_secret_skill_file(item.relative_to(src)):
                return True
    return False


def _is_secret_skill_file(rel_path: Path) -> bool:
    name = rel_path.name.lower()
    if name in _SKILL_SECRET_FILE_NAMES:
        return True
    if name.endswith((".token", ".secret", ".key")):
        return True
    if name.endswith(_SKILL_SOURCE_FILE_SUFFIXES):
        return False
    return any(part in name for part in _SKILL_SECRET_NAME_PARTS)


def _same_symlink(path: Path, target: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        return path.resolve(strict=True) == target.resolve(strict=True)
    except FileNotFoundError:
        return False


def _read_manifest(profile_home: Path, filename: str) -> dict[str, Any]:
    path = profile_home / "skills" / filename
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    skills = raw.get("skills") if isinstance(raw, dict) else None
    return skills if isinstance(skills, dict) else {}


def _write_manifest(profile_home: Path, filename: str, skills: dict[str, Any]) -> None:
    path = profile_home / "skills" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "skills": skills}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-multitenancy-skills")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install", help="Install shared skill into one profile as a personal symlink")
    p_install.add_argument("skill_path")
    p_install.add_argument("--home", type=Path, default=Path.home() / ".hermes")
    p_install.add_argument("--profile", required=True)
    p_install.add_argument("--source", default=None)
    p_install.add_argument("--version", default=None)

    p_audit = sub.add_parser("audit", help="Audit installed skills across profiles")
    p_audit.add_argument("--home", type=Path, default=Path.home() / ".hermes")
    p_audit.add_argument("--profiles-root", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.cmd == "install":
        result = install_shared_skill_for_profile(
            shared_home=args.home,
            profile_home=args.home / "profiles" / args.profile,
            skill_path=args.skill_path,
            source=args.source,
            version=args.version,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "audit":
        result = audit_installed_skills(shared_home=args.home, profiles_root=args.profiles_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
