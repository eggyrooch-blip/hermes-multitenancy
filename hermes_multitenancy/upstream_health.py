"""Secret-free upstream capability health checks for multitenancy upgrades."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from .runtime import require_sandbox_enabled


_SECRET_KEYS = {
    "access_token",
    "refresh_token",
    "app_secret",
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "credential",
}


def upstream_capability_health(
    *,
    shared_home: Path,
    profile_home: Path | None = None,
    router_profile_home: Path | None = None,
) -> dict[str, Any]:
    """Return a read-only, secret-free upgrade health report.

    The report intentionally classifies upstream surfaces as boundaries instead
    of enabling them. Runtime ownership remains with existing multitenancy
    vault, routing, cron, and sandbox code.
    """
    shared_home = shared_home.expanduser()
    profile_home = profile_home.expanduser() if profile_home else None
    router_profile_home = (
        router_profile_home.expanduser()
        if router_profile_home
        else shared_home / "profiles" / "multitenancy_router"
    )
    shared_config = _read_yaml(shared_home / "config.yaml")
    router_config = _read_yaml(router_profile_home / "config.yaml")
    profile_config = _read_yaml(profile_home / "config.yaml") if profile_home else {}

    checks = [
        _lark_cli_registration_check(),
        _cardkit_transport_check(),
        _skill_slash_rewrite_check(profile_home=profile_home),
        _skill_curator_check(shared_config, shared_home=shared_home),
        _skill_bundles_check(shared_config, shared_home=shared_home),
        _profile_scoped_cron_check(profile_config),
        _credential_provider_boundary_check(shared_config),
        _kanban_gateway_boundary_check(router_config),
        _profile_capability_discovery_check(shared_config),
        _profile_browser_runtime_check(
            profile_config,
            profile_home=profile_home,
            router_config=router_config,
            router_profile_home=router_profile_home,
        ),
        _required_sandbox_check(),
    ]
    attention = [check["name"] for check in checks if not check.get("ok")]
    return {
        "ready": not attention,
        "shared_home": str(shared_home),
        "profile_home": str(profile_home) if profile_home else None,
        "router_profile_home": str(router_profile_home),
        "checks": checks,
        "attention": attention,
        "secret_free": True,
    }


def _required_sandbox_check() -> dict[str, Any]:
    if not require_sandbox_enabled():
        return {
            "name": "required_sandbox",
            "ok": True,
            "status": "skipped",
            "owner": "multitenancy_subprocess_sandbox",
            "reason": "HERMES_MULTITENANCY_REQUIRE_SANDBOX is off",
        }
    try:
        from . import agent_real

        if sys.platform == "darwin":
            available = agent_real._SANDBOX_POLICY_FILE.is_file() and os.access(
                agent_real._SANDBOX_EXEC, os.X_OK
            )
            status = "available" if available else "unavailable"
        elif sys.platform.startswith("linux"):
            available = agent_real._BWRAP_ARGS_FILE.is_file() and os.access(
                agent_real._BWRAP_EXEC, os.X_OK
            )
            status = "available" if available else "unavailable"
        else:
            available = False
            status = "unsupported_platform"
    except Exception as exc:
        return {
            "name": "required_sandbox",
            "ok": False,
            "status": "unavailable",
            "owner": "multitenancy_subprocess_sandbox",
            "reason": exc.__class__.__name__,
        }
    return {
        "name": "required_sandbox",
        "ok": available,
        "status": status,
        "owner": "multitenancy_subprocess_sandbox",
        "reason": "required sandbox backend is not available"
        if not available
        else "required sandbox backend is available",
    }


def _lark_cli_registration_check() -> dict[str, Any]:
    try:
        from . import lark_cli_tool

        schema_ok = (lark_cli_tool.LARK_CLI_SCHEMA or {}).get("name") == "lark_cli"
        handler_ok = callable(getattr(lark_cli_tool, "_handle_lark_cli_execute", None))
    except Exception as exc:
        return {
            "name": "lark_cli_registration",
            "ok": False,
            "status": "unavailable",
            "reason": exc.__class__.__name__,
        }
    return {
        "name": "lark_cli_registration",
        "ok": bool(schema_ok and handler_ok),
        "status": "registered" if schema_ok and handler_ok else "incomplete",
        "tool": "lark_cli",
        "alias": "lark-cli",
        "owner": "multitenancy_runtime",
    }


def _cardkit_transport_check() -> dict[str, Any]:
    try:
        from .feishu_cardkit_compat import ensure_feishu_cardkit_streaming
    except Exception as exc:
        return {
            "name": "cardkit_transport",
            "ok": False,
            "status": "unavailable",
            "reason": exc.__class__.__name__,
        }
    return {
        "name": "cardkit_transport",
        "ok": callable(ensure_feishu_cardkit_streaming),
        "status": "compat_available",
        "owner": "multitenancy_feishu_adapter_patch",
    }


def _skill_slash_rewrite_check(*, profile_home: Path | None) -> dict[str, Any]:
    try:
        from .skill_slash import rewrite_skill_slash_text
    except Exception as exc:
        return {
            "name": "skill_slash_rewrite",
            "ok": False,
            "status": "unavailable",
            "reason": exc.__class__.__name__,
        }
    return {
        "name": "skill_slash_rewrite",
        "ok": callable(rewrite_skill_slash_text),
        "status": "target_profile_bound" if profile_home else "helper_available",
        "owner": "multitenancy_router",
    }


def _skill_curator_check(config: dict[str, Any], *, shared_home: Path) -> dict[str, Any]:
    state = _read_curator_state(shared_home)
    if state.get("paused") is True:
        return {
            "name": "skill_curator",
            "ok": True,
            "status": "paused",
            "source": "runtime_state",
            "owner": "upstream_observed_only",
            "reason": "curator paused; multitenancy skill distribution remains owner",
        }
    curator = _as_dict(config.get("skill_curator") or config.get("curator"))
    status = str(curator.get("status") or "").strip().lower()
    enabled = curator.get("enabled")
    paused = status == "paused" or enabled is False
    if not curator:
        return {
            "name": "skill_curator",
            "ok": True,
            "status": "not_configured",
            "owner": "upstream_observed_only",
        }
    return {
        "name": "skill_curator",
        "ok": paused,
        "status": "paused" if paused else "active",
        "owner": "upstream_observed_only",
        "reason": "curator should stay paused while multitenancy owns profile skill distribution"
        if not paused
        else "curator paused; multitenancy skill distribution remains owner",
    }


def _read_curator_state(shared_home: Path) -> dict[str, Any]:
    path = shared_home / "skills" / ".curator_state"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _skill_bundles_check(config: dict[str, Any], *, shared_home: Path) -> dict[str, Any]:
    bundles = _as_dict(config.get("skill_bundles") or config.get("skillBundles"))
    config_path = _first_existing(
        shared_home,
        ("skill-bundles.yaml", "skill-bundles.yml", "skill-bundles.json"),
    )
    observed = bool(bundles) or config_path is not None
    return {
        "name": "skill_bundles",
        "ok": True,
        "status": "observed" if observed else "not_configured",
        "owner": "upstream_model_multitenancy_policy",
        "reason": "use bundles as default capability package input; multitenancy still enforces audience and token policy",
    }


def _profile_scoped_cron_check(config: dict[str, Any]) -> dict[str, Any]:
    upstream_cron = _as_dict(config.get("profile_scoped_cron") or config.get("cron"))
    return {
        "name": "profile_scoped_cron",
        "ok": True,
        "status": "observed" if upstream_cron else "planned_alignment",
        "owner": "multitenancy_cron_worker",
        "reason": "do not replace multitenancy cron worker until delivery, Feishu mirror, and profile context parity are proven",
    }


def _credential_provider_boundary_check(config: dict[str, Any]) -> dict[str, Any]:
    provider = _as_dict(config.get("provider") or config.get("providers"))
    pool = _as_dict(provider.get("credential_pool") or provider.get("credentialPool"))
    auxiliary = _as_dict(provider.get("auxiliary") or provider.get("auxiliary_provider"))
    observed = bool(pool or auxiliary)
    return {
        "name": "credential_provider_boundary",
        "ok": True,
        "status": "observed" if observed else "not_configured",
        "owner": "multitenancy_vault",
        "reason": "upstream provider pool can inform inheritance/fallback, but Feishu UAT remains vault-owned",
    }


def _kanban_gateway_boundary_check(config: dict[str, Any]) -> dict[str, Any]:
    kanban = _as_dict(config.get("kanban"))
    raw = kanban.get("dispatch_in_gateway")
    ok = raw is not True
    return {
        "name": "kanban_gateway_boundary",
        "ok": ok,
        "status": "disabled_in_router" if ok else "enabled_in_router",
        "owner": "multitenancy_long_task_policy",
        "reason": "router profile must not run upstream Kanban dispatcher by default"
        if not ok
        else "Kanban dispatcher is not enabled in router profile",
    }


def _profile_capability_discovery_check(config: dict[str, Any]) -> dict[str, Any]:
    discovery = _as_dict(config.get("capability_discovery") or config.get("capabilityDiscovery"))
    enabled = {
        "browseshsource": _as_dict(discovery.get("browseshsource") or discovery.get("BrowseShSource")).get("enabled"),
        "skills_hub": _as_dict(discovery.get("skills_hub") or discovery.get("skillsHub")).get("enabled"),
        "xai_web_search": _as_dict(discovery.get("xai_web_search") or discovery.get("xAI Web Search")).get("enabled"),
    }
    risky = [name for name, value in enabled.items() if value is True]
    return {
        "name": "profile_capability_discovery",
        "ok": not risky,
        "status": "guarded" if not risky else "requires_policy",
        "owner": "multitenancy_audience_audit_secret_guard",
        "enabled_surfaces": sorted(risky),
        "reason": "capability discovery must pass audience, audit, and secret guard before profile exposure",
    }


def _profile_browser_runtime_check(
    config: dict[str, Any],
    *,
    profile_home: Path | None,
    router_config: dict[str, Any],
    router_profile_home: Path,
) -> dict[str, Any]:
    try:
        from .browser_policy import browser_decision
    except Exception as exc:
        return {
            "name": "profile_browser_runtime",
            "ok": False,
            "status": "unavailable",
            "reason": exc.__class__.__name__,
        }

    router_decision = browser_decision(router_config, router_profile_home)
    if router_decision.enabled:
        return {
            "name": "profile_browser_runtime",
            "ok": False,
            "status": "router_enabled",
            "owner": "multitenancy_profile_policy",
            "reason": "router profile must not run browser tasks",
        }

    if profile_home is None:
        return {
            "name": "profile_browser_runtime",
            "ok": True,
            "status": "not_checked",
            "owner": "multitenancy_profile_policy",
            "reason": "pass profile_home to check profile browser runtime policy",
        }

    decision = browser_decision(config, profile_home)
    runtime_dir = decision.runtime_dir
    return {
        "name": "profile_browser_runtime",
        "ok": True,
        "status": "enabled" if decision.enabled else "disabled",
        "owner": "multitenancy_profile_policy",
        "backend": decision.backend,
        "allow_private_urls": decision.allow_private_urls,
        "runtime_dir": str(runtime_dir) if runtime_dir else None,
        "reason": decision.reason,
    }


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    return _sanitize_mapping(raw)


def _sanitize_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, raw in value.items():
        key_str = str(key)
        if _is_secret_key(key_str):
            continue
        if isinstance(raw, dict):
            result[key_str] = _sanitize_mapping(raw)
        elif isinstance(raw, list):
            result[key_str] = [
                _sanitize_mapping(item) if isinstance(item, dict) else item
                for item in raw
                if not _looks_secret_value(item)
            ]
        elif not _looks_secret_value(raw):
            result[key_str] = raw
    return result


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_existing(base: Path, names: tuple[str, ...]) -> Path | None:
    return next((base / name for name in names if (base / name).exists()), None)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SECRET_KEYS)


def _looks_secret_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return any(marker in lowered for marker in ("secret", "token", "apikey", "api-key", "password"))
