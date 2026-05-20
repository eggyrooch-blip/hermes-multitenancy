"""Read-only provider credential adapter for multitenancy profiles.

This module lets thin profiles inherit model-provider credentials from the
multitenancy vault without copying ``.env`` or ``auth.json`` into every profile.
It deliberately ignores Feishu credentials: Feishu app/UAT ownership stays on
the exact multitenancy vault paths in ``feishu_uat_auth`` and ``agent_real``.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable

from .credentials import CredentialStore


logger = logging.getLogger(__name__)

CONFIG_FILENAMES = (
    "provider-adapter.yaml",
    "provider-adapter.yml",
    "provider-credentials.yaml",
    "provider-credentials.yml",
)
ORG_PROFILE = "__org__"
FALLBACK_PROFILE = "__global__"
SECRET_KIND = "api_key"

_STATIC_PROVIDER_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "zai": ("GLM_API_KEY", "ZAI_API_KEY"),
    "moonshot": ("MOONSHOT_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
}

_SECRET_KEYS = (
    "api_key",
    "access_token",
    "token",
    "secret",
    "value",
)


def provider_env_for_aiagent(
    profile_home: Path,
    *,
    existing_env: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return provider env vars selected from the vault for this profile."""
    existing_env = existing_env or {}
    env: dict[str, str] = {}
    for item in _resolve_profile_provider_status(profile_home, existing_env=existing_env, include_secret=True):
        env_name = str(item.get("env") or "").strip()
        secret = str(item.get("_secret") or "").strip()
        if item.get("status") == "valid" and env_name and secret and not _has_existing_env(env_name, existing_env):
            env[env_name] = secret
    return env


def plan_provider_credentials(
    profile_home: Path,
    *,
    existing_env: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a secret-free provider credential plan for status/canary use."""
    profile_home = Path(profile_home).expanduser()
    config = _load_adapter_config(_resolve_shared_home(profile_home))
    if not _adapter_enabled(config):
        return {
            "enabled": False,
            "profile": profile_home.name,
            "secret_free": True,
            "providers": [],
        }
    providers = [
        _redact_item(item)
        for item in _resolve_profile_provider_status(
            profile_home,
            existing_env=existing_env or {},
            config=config,
            include_secret=False,
        )
    ]
    return {
        "enabled": True,
        "profile": profile_home.name,
        "secret_free": True,
        "providers": providers,
    }


def status_for_provider(
    profile_home: Path,
    *,
    provider: str,
    existing_env: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return one provider's redacted adapter status, if the adapter is enabled."""
    provider = _clean_provider(provider)
    if not provider or provider == "feishu":
        return None
    plan = plan_provider_credentials(profile_home, existing_env=existing_env)
    if not plan.get("enabled"):
        return None
    for item in plan.get("providers") or []:
        if item.get("provider") == provider:
            return item
    return {
        "provider": provider,
        "status": "missing",
        "selected_source": "missing",
        "source_profile": None,
        "subject_id": provider,
        "secret_kind": SECRET_KIND,
        "env": _provider_env_names(provider, {}).get("primary"),
        "has_credential": False,
        "scopes": [],
        "missing_scopes": [],
        "expires_at": None,
        "storage": "multitenancy_db",
    }


def _resolve_profile_provider_status(
    profile_home: Path,
    *,
    existing_env: dict[str, Any],
    config: dict[str, Any] | None = None,
    include_secret: bool,
) -> list[dict[str, Any]]:
    profile_home = Path(profile_home).expanduser()
    shared_home = _resolve_shared_home(profile_home)
    config = config if config is not None else _load_adapter_config(shared_home)
    if not _adapter_enabled(config):
        return []

    providers = _required_providers(profile_home)
    provider_config = config.get("providers") if isinstance(config.get("providers"), dict) else {}
    providers.update(_clean_provider(name) for name in provider_config.keys())
    providers.discard("")
    providers.discard("feishu")

    store = None
    try:
        store = CredentialStore(shared_home / "multitenancy.db")
        return [
            _resolve_one_provider(
                store,
                profile_home=profile_home,
                provider=provider,
                provider_config=provider_config.get(provider) if isinstance(provider_config, dict) else None,
                config=config,
                existing_env=existing_env,
                include_secret=include_secret,
            )
            for provider in sorted(providers)
        ]
    except Exception as exc:
        logger.debug("[multitenancy] provider adapter lookup skipped: %s", exc, exc_info=True)
        return [
            {
                "provider": provider,
                "status": "adapter_unavailable",
                "selected_source": "unavailable",
                "source_profile": None,
                "subject_id": provider,
                "secret_kind": SECRET_KIND,
                "env": _provider_env_names(provider, {}).get("primary"),
                "has_credential": False,
                "scopes": [],
                "missing_scopes": [],
                "expires_at": None,
                "storage": "multitenancy_db",
            }
            for provider in sorted(providers)
        ]
    finally:
        if store is not None:
            store.close()


def _resolve_one_provider(
    store: CredentialStore,
    *,
    profile_home: Path,
    provider: str,
    provider_config: Any,
    config: dict[str, Any],
    existing_env: dict[str, Any],
    include_secret: bool,
) -> dict[str, Any]:
    entry = provider_config if isinstance(provider_config, dict) else {}
    env_info = _provider_env_names(provider, entry)
    env_names = env_info["all"]
    env_name = env_info["primary"]
    subject_id = str(entry.get("subject_id") or provider).strip()
    secret_kind = str(entry.get("secret_kind") or SECRET_KIND).strip()
    required_scopes = entry.get("required_scopes") or []

    if env_names and any(_has_existing_env(name, existing_env) for name in env_names):
        return {
            "provider": provider,
            "status": "valid",
            "selected_source": "ambient_env",
            "source_profile": profile_home.name,
            "subject_id": subject_id,
            "secret_kind": secret_kind,
            "env": env_name,
            "has_credential": True,
            "scopes": [],
            "missing_scopes": [],
            "expires_at": None,
            "storage": "process_env",
        }

    for source_name, source_profile in _source_order(profile_home.name, config, entry):
        try:
            status = store.get_status(
                profile_name=source_profile,
                subject_id=subject_id,
                provider=provider,
                secret_kind=secret_kind,
                required_scopes=required_scopes,
            )
        except Exception:
            continue
        if status.get("status") != "valid":
            continue
        secret = ""
        if include_secret:
            try:
                payload = store.get_secret_for_runtime(
                    profile_name=source_profile,
                    subject_id=subject_id,
                    provider=provider,
                    secret_kind=secret_kind,
                )
            except Exception:
                payload = {}
            secret = _payload_secret(payload, env_name)
            if not secret:
                continue
        item = {
            "provider": provider,
            "status": "valid",
            "selected_source": source_name,
            "source_profile": source_profile,
            "subject_id": subject_id,
            "secret_kind": secret_kind,
            "env": env_name,
            "has_credential": True,
            "scopes": status.get("scopes") or [],
            "missing_scopes": status.get("missing_scopes") or [],
            "expires_at": status.get("expires_at"),
            "storage": "multitenancy_db",
        }
        if include_secret:
            item["_secret"] = secret
        return item

    return {
        "provider": provider,
        "status": "missing",
        "selected_source": "missing",
        "source_profile": None,
        "subject_id": subject_id,
        "secret_kind": secret_kind,
        "env": env_name,
        "has_credential": False,
        "scopes": [],
        "missing_scopes": [],
        "expires_at": None,
        "storage": "multitenancy_db",
    }


def _source_order(profile_name: str, config: dict[str, Any], entry: dict[str, Any]) -> list[tuple[str, str]]:
    org_profile = str(entry.get("org_profile") or config.get("org_profile") or ORG_PROFILE).strip()
    fallback_profile = str(entry.get("fallback_profile") or config.get("fallback_profile") or FALLBACK_PROFILE).strip()
    order = [("profile", profile_name), ("org", org_profile), ("fallback", fallback_profile)]
    return [(label, name) for label, name in order if name]


def _required_providers(profile_home: Path) -> set[str]:
    config = _load_effective_profile_config(profile_home)
    providers: set[str] = set()

    model_cfg = config.get("model") if isinstance(config.get("model"), dict) else {}
    default_model = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
    provider = str(model_cfg.get("provider") or "").strip()
    providers.add(_provider_from_model(default_model) or _clean_provider(provider))

    fallback = config.get("fallback") or config.get("fallback_models") or []
    if isinstance(fallback, str):
        fallback = [fallback]
    if isinstance(fallback, list):
        for item in fallback:
            if isinstance(item, str):
                providers.add(_provider_from_model(item))
            elif isinstance(item, dict):
                providers.add(_provider_from_model(str(item.get("model") or "")) or _clean_provider(str(item.get("provider") or "")))

    auxiliary = config.get("auxiliary")
    if isinstance(auxiliary, dict):
        for value in auxiliary.values():
            if not isinstance(value, dict):
                continue
            aux_provider = _clean_provider(str(value.get("provider") or ""))
            aux_model_provider = _provider_from_model(str(value.get("model") or ""))
            if aux_provider not in {"", "auto", "main"}:
                providers.add(aux_provider)
            elif aux_model_provider:
                providers.add(aux_model_provider)

    return {provider for provider in providers if provider and provider not in {"auto", "main"}}


def _provider_env_names(provider: str, entry: dict[str, Any]) -> dict[str, Any]:
    configured = entry.get("env") or entry.get("env_name") or entry.get("api_key_env")
    names: list[str] = []
    if configured:
        names.append(str(configured).strip())
    names.extend(_upstream_provider_env_names(provider))
    names.extend(_STATIC_PROVIDER_ENV_KEYS.get(provider, ()))
    normalized = []
    seen = set()
    for name in names:
        clean = str(name or "").strip()
        if clean and clean not in seen:
            normalized.append(clean)
            seen.add(clean)
    return {
        "primary": normalized[0] if normalized else "",
        "all": tuple(normalized),
    }


def _upstream_provider_env_names(provider: str) -> tuple[str, ...]:
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
        cfg = PROVIDER_REGISTRY.get(provider)
        values = getattr(cfg, "api_key_env_vars", None) if cfg is not None else None
        if values:
            return tuple(str(value).strip() for value in values if str(value).strip())
    except Exception:
        return ()
    return ()


def _payload_secret(payload: dict[str, Any], env_name: str) -> str:
    keys: list[str] = []
    if env_name:
        keys.extend([env_name, env_name.lower()])
    keys.extend(_SECRET_KEYS)
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _redact_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "_secret"}


def _adapter_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("enabled"))


def _load_adapter_config(shared_home: Path) -> dict[str, Any]:
    for filename in CONFIG_FILENAMES:
        path = shared_home / filename
        if not path.exists():
            continue
        raw = _load_yaml(path)
        if not isinstance(raw, dict):
            return {"enabled": False}
        nested = raw.get("provider_adapter")
        if isinstance(nested, dict):
            merged = dict(raw)
            merged.update(nested)
            merged.pop("provider_adapter", None)
            return merged
        return raw
    return {"enabled": False}


def _load_effective_profile_config(profile_home: Path) -> dict[str, Any]:
    shared_home = _resolve_shared_home(profile_home)
    return _merge_dicts(
        _load_yaml(shared_home / "config.yaml"),
        _load_yaml(profile_home / "config.yaml"),
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        logger.debug("[multitenancy] failed to read provider adapter yaml: %s", path, exc_info=True)
        return {}


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or {})
    for key, value in (override or {}).items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_dicts(current, value)
        else:
            merged[key] = value
    return merged


def _resolve_shared_home(profile_home: Path) -> Path:
    explicit = os.getenv("HERMES_SHARED_HOME")
    if explicit:
        return Path(explicit).expanduser()
    profile_home = Path(profile_home).expanduser()
    if profile_home.parent.name == "profiles":
        return profile_home.parent.parent
    return profile_home


def _provider_from_model(model_spec: str) -> str:
    value = str(model_spec or "").strip()
    if "/" not in value:
        return ""
    return _clean_provider(value.split("/", 1)[0])


def _clean_provider(value: str) -> str:
    return str(value or "").strip().lower().replace(" ", "-")


def _has_existing_env(name: str, existing_env: dict[str, Any]) -> bool:
    return bool(existing_env.get(name) or os.environ.get(name))
