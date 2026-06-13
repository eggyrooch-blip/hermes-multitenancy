"""Model-visible credential diagnostics for the multitenancy credential vault."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .credentials import CredentialStore


CREDENTIAL_STATUS_SCHEMA = {
    "name": "multitenancy_credential_status",
    "description": (
        "Check whether the current profile has usable provider credentials. "
        "Returns only redacted status, expiry, and scope information."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "description": "Credential provider, for example feishu.",
                "default": "feishu",
            },
            "subject_id": {
                "type": "string",
                "description": "Optional current-profile subject id. Defaults to the current Feishu open_id.",
            },
            "credential_kind": {
                "type": "string",
                "description": "Credential kind, for example uat or api_key.",
                "default": "uat",
            },
            "required_scopes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional scopes to verify.",
            },
        },
        "required": [],
    },
}


def credential_status(args: dict[str, Any] | None = None, **_kwargs: Any) -> str:
    """Return current-profile credential status without exposing raw secrets."""
    args = args or {}
    try:
        profile_name = _current_profile_name()
        requested_profile = str(args.get("profile") or profile_name).strip()
        if requested_profile != profile_name:
            return _error("credential status is limited to the current profile only")

        provider = str(args.get("provider") or "feishu").strip()
        secret_kind = str(args.get("credential_kind") or args.get("secret_kind") or "uat").strip()
        required_scopes = args.get("required_scopes") or []
        if isinstance(required_scopes, str):
            required_scopes = [required_scopes]

        if provider and provider != "feishu" and secret_kind in {"api_key", "token"}:
            adapter_status = _provider_adapter_status(provider)
            if adapter_status is not None:
                return json.dumps(
                    {
                        "profile": profile_name,
                        "provider": provider,
                        "subject_id": adapter_status.get("subject_id") or provider,
                        "credential_kind": secret_kind,
                        "status": adapter_status.get("status"),
                        "storage": adapter_status.get("storage"),
                        "expires_at": adapter_status.get("expires_at"),
                        "scopes": adapter_status.get("scopes") or [],
                        "missing_scopes": adapter_status.get("missing_scopes") or [],
                        "has_credential": bool(adapter_status.get("has_credential")),
                        "selected_source": adapter_status.get("selected_source"),
                        "source_profile": adapter_status.get("source_profile"),
                        "env": adapter_status.get("env"),
                        "sandbox_note": ".env/auth.json are masked by design",
                    },
                    ensure_ascii=False,
                )

        subject_id = str(
            args.get("subject_id")
            or os.getenv("HERMES_FEISHU_USER_OPEN_ID")
            or ""
        ).strip()
        if not subject_id:
            return _error("subject_id is required when HERMES_FEISHU_USER_OPEN_ID is unset")

        store = CredentialStore(_shared_home() / "multitenancy.db")
        status = store.get_status(
            profile_name=profile_name,
            subject_id=subject_id,
            provider=provider,
            secret_kind=secret_kind,
            required_scopes=required_scopes,
        )
        store.close()
        return json.dumps(
            {
                "profile": profile_name,
                "provider": provider,
                "subject_id": subject_id,
                "credential_kind": secret_kind,
                "status": status["status"],
                "storage": status["storage"],
                "expires_at": status["expires_at"],
                "refresh_expires_at": status.get("refresh_expires_at"),
                "scopes": status["scopes"],
                "missing_scopes": status["missing_scopes"],
                "has_credential": status["has_payload"],
                "sandbox_note": ".env/auth.json are masked by design",
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return _error(str(exc))


def register_credential_status_tool(ctx: Any) -> None:
    """Register the redacted status tool when the host PluginContext supports tools."""
    register_tool = getattr(ctx, "register_tool", None)
    if not callable(register_tool):
        return
    register_tool(
        name="multitenancy_credential_status",
        toolset="multitenancy_diagnostics",
        schema=CREDENTIAL_STATUS_SCHEMA,
        handler=credential_status,
        check_fn=lambda: True,
        requires_env=[],
        is_async=False,
        description="Check current-profile credential status without exposing secrets.",
        emoji="🔐",
    )


def _current_profile_name() -> str:
    home = Path(os.getenv("HERMES_HOME") or "").expanduser()
    if not str(home) or str(home) == ".":
        raise RuntimeError("HERMES_HOME is required")
    return home.name


def _shared_home() -> Path:
    explicit = os.getenv("HERMES_SHARED_HOME")
    if explicit:
        return Path(explicit).expanduser()
    home = Path(os.getenv("HERMES_HOME") or "").expanduser()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _provider_adapter_status(provider: str) -> dict[str, Any] | None:
    try:
        from .provider_adapter import status_for_provider

        return status_for_provider(_current_profile_home(), provider=provider)
    except Exception:
        return None


def _current_profile_home() -> Path:
    home = Path(os.getenv("HERMES_HOME") or "").expanduser()
    if not str(home) or str(home) == ".":
        raise RuntimeError("HERMES_HOME is required")
    return home


def _error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)
