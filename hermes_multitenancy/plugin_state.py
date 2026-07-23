"""Authoritative plugin revocation state and runtime skill filtering."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, Callable


class PluginStateError(RuntimeError):
    pass


_MANAGED_DIR = ".hermes-plugin-managed"


def mark_inactive(shared_home: Path, plugin_id: str) -> None:
    path = shared_home / _MANAGED_DIR / f"{_component(plugin_id)}.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PluginStateError(
            f"no managed manifest for plugin {plugin_id!r} at {path}"
        ) from exc
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PluginStateError(
            f"invalid managed manifest for plugin {plugin_id!r} at {path}"
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("plugin_id") != plugin_id:
        raise PluginStateError(f"invalid managed manifest for plugin {plugin_id!r}")
    manifest["status"] = "inactive"
    _write_json_atomic(path, manifest)


def inactive_skill_paths(shared_home: Path) -> set[str]:
    managed_root = shared_home / _MANAGED_DIR
    if not managed_root.is_dir():
        return set()

    inactive: dict[str, str] = {}
    for manifest_path in sorted(managed_root.glob("*.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PluginStateError(
                f"cannot verify plugin state at {manifest_path}"
            ) from exc
        if not isinstance(manifest, dict):
            raise PluginStateError(f"cannot verify plugin state at {manifest_path}")
        status = manifest.get("status")
        if status in {None, "", "active"}:
            continue
        if status != "inactive":
            raise PluginStateError(f"unknown plugin status at {manifest_path}")
        plugin_id = _component(manifest.get("plugin_id"))
        for raw_name in manifest.get("skills") or []:
            name = _skill_path(raw_name)
            previous = inactive.setdefault(name, plugin_id)
            if previous != plugin_id:
                raise PluginStateError(
                    f"cannot prove inactive owner for shared skill {name!r}"
                )
    if not inactive:
        return set()

    registry_path = managed_root / ".locks" / "source-owners.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PluginStateError("cannot verify inactive plugin skill owners") from exc
    owners = registry.get("skills") if isinstance(registry, dict) else None
    if not isinstance(owners, dict):
        raise PluginStateError("cannot verify inactive plugin skill owners")
    for name, plugin_id in inactive.items():
        owner = owners.get(name)
        source = shared_home / "skills" / name
        if (
            not isinstance(owner, dict)
            or owner.get("plugin_id") != plugin_id
            or not source.is_dir()
            or source.is_symlink()
            or owner.get("digest") != _tree_digest(source)
        ):
            raise PluginStateError(
                f"cannot prove inactive owner for shared skill {name!r}"
            )
    return set(inactive)


def scope_active_skill_files(
    profile_home: Path,
    remember: Callable[[Any, str, Any], None],
) -> None:
    blocked = inactive_skill_paths(profile_home.parent.parent)
    if not blocked:
        return
    from agent import prompt_builder, skill_utils  # type: ignore

    original_iter = skill_utils.iter_skill_index_files
    skills_root = profile_home / "skills"

    def iter_active(skills_dir: Path, filename: str):
        for path in original_iter(skills_dir, filename):
            try:
                rel = path.relative_to(skills_root)
            except ValueError:
                yield path
                continue
            if not rel.parts or rel.parts[0] not in blocked:
                yield path

    remember(skill_utils, "iter_skill_index_files", iter_active)
    remember(prompt_builder, "iter_skill_index_files", iter_active)


def _component(value: Any) -> str:
    raw = str(value or "").strip()
    if (
        not raw
        or "/" in raw
        or "\\" in raw
        or raw in {".", ".."}
        or raw.startswith(".")
        or "\x00" in raw
    ):
        raise PluginStateError(f"unsafe plugin id: {value!r}")
    return raw


def _skill_path(value: Any) -> str:
    raw = str(value or "").strip()
    path = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PluginStateError(f"unsafe skill path: {value!r}")
    return raw


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            tmp = Path(handle.name)
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
