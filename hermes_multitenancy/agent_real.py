"""Phase 3 — real (non-stub) agent runner.

Replaces ``runtime._default_run_agent`` (Phase-1 echo stub) with a thin
OpenAI-compatible LLM call that reads its config + credentials from the
profile_home directory. Designed to plug into ``ProfileRuntime`` via:

    ProfileRuntime(profile_home, run_agent_fn=real_run_agent)

Resolution order for an API key, given a model spec like ``"zai/glm-5.1"``:
  1. Environment variable ``<PROVIDER>_API_KEY`` (e.g. ``GLM_API_KEY``,
     ``ZAI_API_KEY``, ``OPENROUTER_API_KEY``) — sourced from the profile's
     ``.env`` file via ``python-dotenv``.
  2. ``auth.json`` ``credential_pool[provider]`` — first entry whose
     ``last_status`` is not ``"exhausted"``.

Fallback strategy: try the primary ``model.default``; on any error, walk the
``fallback`` list. Returns the first non-empty content string.

Spike scope: deliberate one-shot LLM call, no SessionStore, no tool-loop, no
streaming. Phase 4 will graduate to a real AIAgent loop (~1700 LOC per the
architect estimate). For end-to-end demo today, this thin runner is enough.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Map a model provider prefix to the env-var name that holds its API key.
# Keep this list short and explicit — adding a provider is one line.
_PROVIDER_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "zai": ("GLM_API_KEY", "ZAI_API_KEY"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "moonshot": ("MOONSHOT_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
}


# Each provider's API base URL when the model spec has no explicit override.
# Values mirror what ``hermes_cli/config.py`` infers for the same providers.
_PROVIDER_BASE_URLS: dict[str, str] = {
    "zai": "https://api.z.ai/api/coding/paas/v4",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "openai": "https://api.openai.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "deepseek": "https://api.deepseek.com",
}


async def stream_run_agent(  # type: ignore[override]
    event: Any,
    profile_home: Path,
    *,
    messages: Optional[list[dict]] = None,
):
    """Yields ``(kind, text)`` tuples — see ``_stream_loop`` for the actual
    implementation. ``kind`` is one of ``"thinking"`` or ``"content"``.

    Splitting reasoning vs. final content is critical for thinking models
    (e.g. GLM 5.x): without surfacing reasoning chunks the user sees no
    progress for tens of seconds while the model "thinks", then a wall of
    text. Caller (router) typically renders thinking in a folded preview
    and the final content in full.
    """
    async for kind, text in _stream_loop(event, profile_home, messages=messages):
        yield kind, text


async def _stream_loop(
    event: Any,
    profile_home: Path,
    *,
    messages: Optional[list[dict]] = None,
):
    """Streaming counterpart to ``real_run_agent`` — yields content chunks.

    Used by the multitenancy router to stream LLM tokens into a Feishu
    ``edit_message`` loop, restoring the typewriter UX that hermes' main
    flow provides natively. Falls through provider candidates the same way
    as ``real_run_agent`` — first one whose first chunk is non-empty wins.

    Yields
    ------
    str
        Each non-empty content chunk from the live model.

    Raises
    ------
    RuntimeError
        If every candidate model+credential combination fails or yields
        nothing. Caller should fall back to ``real_run_agent`` for a final
        non-streamed attempt before giving up.
    """
    import yaml
    from openai import AsyncOpenAI
    from dotenv import dotenv_values

    config = _load_yaml(profile_home / "config.yaml")
    auth = _load_json(profile_home / "auth.json")
    env_overrides = (
        dotenv_values(profile_home / ".env") if (profile_home / ".env").exists() else {}
    )

    primary = config.get("model", {}).get("default")
    fallback_models = config.get("fallback") or []
    candidates: list[str] = [primary] if primary else []
    candidates.extend(fallback_models)

    soul_text = _load_soul(profile_home)
    user_text = getattr(event, "text", "") or ""

    # Caller can override the message list (used for multi-turn history).
    # Default: system prompt + single user message.
    if messages is None:
        effective_messages: list[dict] = [
            {"role": "system", "content": soul_text},
            {"role": "user", "content": user_text},
        ]
    else:
        # Caller supplies the conversation. We still inject SOUL as system
        # to guarantee the profile's persona stays in force.
        effective_messages = [
            {"role": "system", "content": soul_text},
            *messages,
        ]

    last_error: Optional[BaseException] = None

    for model_spec in candidates:
        if not model_spec:
            continue
        try:
            provider, model_name = _split_model_spec(model_spec)
        except ValueError:
            continue
        api_key = _resolve_api_key(provider, env_overrides, auth)
        if not api_key:
            continue
        base_url = _resolve_base_url(provider, model_spec == primary, config)

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        try:
            stream = await client.chat.completions.create(
                model=model_name,
                messages=effective_messages,
                max_tokens=512,
                stream=True,
            )
            got_content = False
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if not delta:
                    continue
                # Reasoning models (e.g. GLM 5.1) stream reasoning_content
                # BEFORE content; surfacing it gives the user real-time feedback
                # instead of a 5-15s placeholder freeze.
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield "thinking", reasoning
                if delta.content:
                    got_content = True
                    yield "content", delta.content
            if got_content:
                return
            logger.info("stream_run_agent: %s yielded no content, falling back", model_spec)
        except Exception as exc:
            last_error = exc
            logger.info("stream_run_agent: %s failed (%s), falling back", model_spec, exc)

    if last_error is not None:
        raise RuntimeError(f"streaming failed; last error: {last_error}") from last_error
    raise RuntimeError("streaming exhausted (no usable provider returned content)")


async def real_run_agent(
    event: Any,
    profile_home: Path,
    *,
    messages: Optional[list[dict]] = None,
) -> str:
    """Run a single LLM completion against the profile's configured model.

    Returns the assistant text (always non-empty when a fallback succeeds).
    Raises RuntimeError if every candidate model+credential combination
    fails or returns empty content.
    """
    from openai import AsyncOpenAI
    from dotenv import dotenv_values

    config = _load_yaml(profile_home / "config.yaml")
    auth = _load_json(profile_home / "auth.json")
    env_overrides = dotenv_values(profile_home / ".env") if (profile_home / ".env").exists() else {}

    primary = config.get("model", {}).get("default")
    fallback_models = config.get("fallback") or []
    candidates: list[str] = [primary] if primary else []
    candidates.extend(fallback_models)

    soul_text = _load_soul(profile_home)
    user_text = getattr(event, "text", "") or ""

    if messages is None:
        effective_messages: list[dict] = [
            {"role": "system", "content": soul_text},
            {"role": "user", "content": user_text},
        ]
    else:
        effective_messages = [
            {"role": "system", "content": soul_text},
            *messages,
        ]

    last_error: Optional[BaseException] = None

    for model_spec in candidates:
        if not model_spec:
            continue
        try:
            provider, model_name = _split_model_spec(model_spec)
        except ValueError as exc:
            logger.debug("real_run_agent: bad model spec %r: %s", model_spec, exc)
            continue

        api_key = _resolve_api_key(provider, env_overrides, auth)
        if not api_key:
            logger.debug("real_run_agent: no API key for provider %s", provider)
            continue

        base_url = _resolve_base_url(provider, model_spec == primary, config)

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        try:
            rsp = await client.chat.completions.create(
                model=model_name,
                messages=effective_messages,
                max_tokens=512,
            )
            text = (rsp.choices[0].message.content or "").strip()
            if text:
                logger.debug(
                    "real_run_agent: %s ok (prompt=%d completion=%d)",
                    model_spec,
                    rsp.usage.prompt_tokens if rsp.usage else -1,
                    rsp.usage.completion_tokens if rsp.usage else -1,
                )
                return text
            # Empty content (often signals quota exhausted) — try next.
            logger.info("real_run_agent: %s returned empty, falling back", model_spec)
        except Exception as exc:
            last_error = exc
            logger.info("real_run_agent: %s failed (%s), falling back", model_spec, exc)

    if last_error is not None:
        raise RuntimeError(f"all providers failed; last error: {last_error}") from last_error
    raise RuntimeError("all providers exhausted (no usable key or non-empty response)")


# -- helpers ---------------------------------------------------------------


def _split_model_spec(spec: str) -> tuple[str, str]:
    """Split ``provider/model_name`` into its parts."""
    if "/" not in spec:
        raise ValueError(f"model spec missing provider prefix: {spec!r}")
    provider, name = spec.split("/", 1)
    return provider.strip().lower(), name.strip()


def _resolve_api_key(
    provider: str,
    env_overrides: dict[str, Any],
    auth: dict[str, Any],
) -> Optional[str]:
    """Find an API key for *provider* — env vars first, auth.json second."""
    for env_name in _PROVIDER_ENV_KEYS.get(provider, ()):
        key = env_overrides.get(env_name) or os.environ.get(env_name)
        if key:
            return key
    pool = auth.get("credential_pool", {}).get(provider)
    if isinstance(pool, list):
        for cred in pool:
            if not isinstance(cred, dict):
                continue
            if cred.get("last_status") == "exhausted":
                continue
            token = cred.get("access_token")
            if token:
                return token
    return None


def _resolve_base_url(provider: str, is_primary: bool, config: dict[str, Any]) -> Optional[str]:
    """Resolve the API base URL for *provider*. Primary model honors config.model.base_url."""
    if is_primary:
        explicit = config.get("model", {}).get("base_url")
        if explicit:
            return explicit
    return _PROVIDER_BASE_URLS.get(provider)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    import yaml
    return yaml.safe_load(path.read_text()) or {}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _load_soul(profile_home: Path) -> str:
    """Read SOUL.md as the system prompt; fall back to a generic prompt."""
    soul = profile_home / "SOUL.md"
    if soul.exists():
        text = soul.read_text().strip()
        if text:
            return text
    return "You are a helpful assistant."
