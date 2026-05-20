"""Multitenancy guardrail adapter for upstream Hermes Kanban dispatch.

This module does not start Kanban from the gateway.  It exposes an explicit
sidecar entrypoint that can plan or run one upstream dispatcher pass after the
multitenancy profile boundary has been checked.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_NAME = "kanban-sidecar.yaml"
ROUTER_PROFILE = "multitenancy_router"
_SECRET_KEY_PARTS = ("secret", "token", "password", "api_key", "credential")


@dataclass(frozen=True)
class KanbanSidecarConfig:
    enabled: bool = False
    board: str = "default"
    tenant: str | None = None
    sidecar_profile: str | None = None
    allowed_profiles: tuple[str, ...] = ()
    max_spawn: int | None = None
    max_in_progress: int | None = None
    execute: bool = False


def plan_kanban_sidecar(
    *,
    shared_home: str | Path | None = None,
    current_profile: str | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    """Return a secret-free Kanban sidecar plan or one-pass execution result."""

    resolved_shared_home, resolved_profile = _resolve_runtime(shared_home, current_profile)
    config = load_config(resolved_shared_home)
    problems = _guard_problems(config, resolved_profile)

    base = _base_result(
        config,
        shared_home=resolved_shared_home,
        current_profile=resolved_profile,
        problems=problems,
    )

    if not config.enabled:
        base.update(
            {
                "status": "disabled",
                "reason": f"{CONFIG_NAME} missing or enabled=false",
                "would_execute": False,
                "will_execute": False,
            }
        )
        return base

    if problems:
        base.update(
            {
                "status": "blocked",
                "would_execute": False,
                "will_execute": False,
            }
        )
        return base

    will_execute = bool(execute and config.execute)
    if will_execute and not _profile_allowed(config, resolved_profile):
        base.update(
            {
                "status": "blocked",
                "would_execute": False,
                "will_execute": False,
                "problems": [
                    *problems,
                    f"profile {resolved_profile!r} is not in Kanban sidecar allowlist",
                ],
            }
        )
        return base

    dispatch_result = _dispatch_once(config, dry_run=not will_execute)
    base.update(_summarize_dispatch(dispatch_result))
    base.update(
        {
            "status": "executed" if will_execute else "dry_run",
            "would_execute": True,
            "will_execute": will_execute,
        }
    )
    return base


def load_config(shared_home: str | Path) -> KanbanSidecarConfig:
    path = Path(shared_home) / CONFIG_NAME
    if not path.exists():
        return KanbanSidecarConfig()
    raw = _load_yaml_like(path)
    if not isinstance(raw, dict):
        return KanbanSidecarConfig()
    return KanbanSidecarConfig(
        enabled=_bool(raw.get("enabled")),
        board=str(raw.get("board") or "default").strip() or "default",
        tenant=_optional_str(raw.get("tenant")),
        sidecar_profile=_optional_str(raw.get("sidecar_profile")),
        allowed_profiles=tuple(
            str(item).strip()
            for item in _as_list(raw.get("allowed_profiles"))
            if str(item).strip()
        ),
        max_spawn=_optional_int(raw.get("max_spawn")),
        max_in_progress=_optional_int(raw.get("max_in_progress")),
        execute=_bool(raw.get("execute")),
    )


def _resolve_runtime(
    shared_home: str | Path | None,
    current_profile: str | None,
) -> tuple[Path, str | None]:
    if shared_home is not None:
        return Path(shared_home).expanduser().resolve(), current_profile

    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()
    profile = current_profile or os.environ.get("HERMES_PROFILE")
    if hermes_home.parent.name == "profiles":
        profile = profile or hermes_home.name
        return hermes_home.parent.parent, profile
    return hermes_home, profile


def _base_result(
    config: KanbanSidecarConfig,
    *,
    shared_home: Path,
    current_profile: str | None,
    problems: list[str],
) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "status": "unknown",
        "shared_home": str(shared_home),
        "board": config.board,
        "tenant": config.tenant,
        "current_profile": current_profile,
        "sidecar_profile": config.sidecar_profile,
        "allowed_profiles": list(config.allowed_profiles),
        "execute_configured": config.execute,
        "max_spawn": config.max_spawn,
        "max_in_progress": config.max_in_progress,
        "problems": problems,
        "summary": {},
        "spawned": [],
        "secret_free": True,
    }


def _guard_problems(config: KanbanSidecarConfig, current_profile: str | None) -> list[str]:
    problems: list[str] = []
    if current_profile == ROUTER_PROFILE:
        problems.append("router profile must not run Kanban sidecar dispatch")
    if config.sidecar_profile == ROUTER_PROFILE:
        problems.append("router profile must not be configured as Kanban sidecar profile")
    if ROUTER_PROFILE in config.allowed_profiles:
        problems.append("router profile must not be allowlisted for Kanban sidecar dispatch")
    return problems


def _profile_allowed(config: KanbanSidecarConfig, current_profile: str | None) -> bool:
    if current_profile is None:
        return False
    if config.sidecar_profile and current_profile != config.sidecar_profile:
        return False
    if config.allowed_profiles and current_profile not in config.allowed_profiles:
        return False
    return True


def _dispatch_once(config: KanbanSidecarConfig, *, dry_run: bool) -> Any:
    kanban_db = importlib.import_module("hermes_cli.kanban_db")
    return kanban_db.dispatch_once(
        board=config.board,
        dry_run=dry_run,
        max_spawn=config.max_spawn,
        max_in_progress=config.max_in_progress,
    )


def _summarize_dispatch(result: Any) -> dict[str, Any]:
    fields = {
        "reclaimed": _get(result, "reclaimed", []),
        "promoted": _get(result, "promoted", []),
        "spawned": _get(result, "spawned", []),
        "skipped_unassigned": _get(result, "skipped_unassigned", []),
        "skipped_nonspawnable": _get(result, "skipped_nonspawnable", []),
        "crashed": _get(result, "crashed", []),
        "auto_blocked": _get(result, "auto_blocked", []),
        "timed_out": _get(result, "timed_out", []),
        "stale": _get(result, "stale", []),
        "respawn_guarded": _get(result, "respawn_guarded", []),
    }
    safe_fields = {key: _safe_value(value) for key, value in fields.items()}
    summary = {
        f"{key}_count": _count(value)
        for key, value in safe_fields.items()
    }
    return {
        **safe_fields,
        "summary": summary,
    }


def _get(value: Any, key: str, default: Any) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (str, bytes, dict)):
        return 1
    try:
        return len(value)
    except TypeError:
        return int(value) if isinstance(value, int) else 1


def _safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in _SECRET_KEY_PARTS):
                safe[key_text] = "<redacted>"
            else:
                safe[key_text] = _safe_value(item)
        return safe
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _load_yaml_like(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except Exception:
        return _parse_simple_yaml(text)


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- ") and current_list_key:
            data.setdefault(current_list_key, []).append(_scalar(line[2:].strip()))
            continue
        if ":" not in line:
            current_list_key = None
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not value:
            data[key] = []
            current_list_key = key
        else:
            data[key] = _scalar(value)
            current_list_key = None
    return data


def _scalar(value: str) -> Any:
    value = value.strip().strip("\"'")
    lower = value.lower()
    if lower in {"true", "yes", "on"}:
        return True
    if lower in {"false", "no", "off"}:
        return False
    if lower in {"null", "none", "~"}:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or run one multitenancy Kanban sidecar pass")
    parser.add_argument("--shared-home", type=Path, default=None)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    result = plan_kanban_sidecar(
        shared_home=args.shared_home,
        current_profile=args.profile,
        execute=args.execute,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] not in {"blocked"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
