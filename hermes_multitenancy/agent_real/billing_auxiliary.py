"""Run-scoped enforcement for Hermes auxiliary model calls."""
from __future__ import annotations

import inspect
from typing import Any, Callable


def install_billing_auxiliary_runtime(
    auxiliary_client: Any,
    *,
    model: str,
    base_url: str,
    api_key: str,
    api_mode: str = "",
) -> Callable[[], None]:
    """Force title/compression/vision helpers through one billed endpoint."""
    task_resolver = getattr(auxiliary_client, "_resolve_task_provider_model", None)
    provider_resolver = getattr(auxiliary_client, "resolve_provider_client", None)
    if not callable(task_resolver) or not callable(provider_resolver):
        raise RuntimeError("Hermes auxiliary billing runtime is unavailable")
    if not model or not base_url or not api_key:
        raise RuntimeError("Hermes auxiliary billing runtime is incomplete")

    def resolve_task(*args, **kwargs):
        resolved = task_resolver(*args, **kwargs)
        task_model = resolved[1] if isinstance(resolved, tuple) and len(resolved) > 1 else None
        task_mode = resolved[4] if isinstance(resolved, tuple) and len(resolved) > 4 else None
        return (
            "custom",
            task_model or model,
            base_url,
            api_key,
            api_mode or task_mode,
        )

    provider_signature = inspect.signature(provider_resolver)
    base_parameter = (
        "explicit_base_url"
        if "explicit_base_url" in provider_signature.parameters
        else "base_url"
    )
    key_parameter = (
        "explicit_api_key"
        if "explicit_api_key" in provider_signature.parameters
        else "api_key"
    )

    def resolve_provider(provider, selected_model=None, *args, **kwargs):
        bound = provider_signature.bind_partial(
            provider, selected_model, *args, **kwargs
        )
        bound.arguments["provider"] = "custom"
        bound.arguments["model"] = selected_model or model
        bound.arguments[base_parameter] = base_url
        bound.arguments[key_parameter] = api_key
        if api_mode and "api_mode" in provider_signature.parameters:
            bound.arguments["api_mode"] = api_mode
        return provider_resolver(*bound.args, **bound.kwargs)

    auxiliary_client._resolve_task_provider_model = resolve_task
    auxiliary_client.resolve_provider_client = resolve_provider

    def cleanup() -> None:
        if auxiliary_client._resolve_task_provider_model is resolve_task:
            auxiliary_client._resolve_task_provider_model = task_resolver
        if auxiliary_client.resolve_provider_client is resolve_provider:
            auxiliary_client.resolve_provider_client = provider_resolver
        evict = getattr(auxiliary_client, "_evict_cached_clients", None)
        if callable(evict):
            try:
                evict("custom")
            except Exception:
                pass

    return cleanup
