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
import sys
import time
import hashlib
import tempfile
import uuid
import re
import secrets
import importlib
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .credential_broker import lease_signing_secret, mint_lease
from .lark_cli_guard import (
    HERMES_LARK_CLI_REAL_BIN,
    HERMES_LARK_CLI_RUN_TOKEN,
    generate_lark_cli_run_token,
    install_lark_cli_shim,
)
from .lark_cli_auth_broker import (
    LarkCliAuthBrokerContext,
    start_lark_cli_auth_broker_server,
)
from .runtime import strict_context_enabled
from .security_audit import DEFAULT_AUDIT_PATH as DEFAULT_SECURITY_AUDIT_PATH
from . import lark_cli_tool as _lark_cli_tool  # noqa: F401 - registers lark_cli toolset

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

_PROVIDER_BASE_URL_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_BASE_URL",),
    "openai": ("OPENAI_BASE_URL",),
    "openrouter": ("OPENROUTER_BASE_URL",),
    "zai": ("ZAI_BASE_URL", "GLM_BASE_URL"),
    "moonshot": ("MOONSHOT_BASE_URL",),
    "deepseek": ("DEEPSEEK_BASE_URL",),
}

_MODEL_ENV_ALLOWLIST: frozenset[str] = frozenset(
    key
    for names in (*_PROVIDER_ENV_KEYS.values(), *_PROVIDER_BASE_URL_ENV_KEYS.values())
    for key in names
)

_AIAGENT_TOOL_ENV_ALLOWLIST: frozenset[str] = frozenset({
    "FAL_KEY",
    "TAVILY_API_KEY",
    "TAVILY_BASE_URL",
    "TENCENTCLOUD_SECRET_ID",
    "TENCENTCLOUD_SECRET_KEY",
    "VOD_SUBAPP_ID",
    "VOD_REGION",
    "VOD_STORAGE_MODE",
    "VOD_POLL_TIMEOUT",
    "VOD_POLL_INTERVAL",
    "VOD_ENDPOINT",
})

_SHARED_AIAGENT_ENV_ALLOWLIST: frozenset[str] = (
    _MODEL_ENV_ALLOWLIST | _AIAGENT_TOOL_ENV_ALLOWLIST
)

_FEISHU_ENV_BLOCKLIST: frozenset[str] = frozenset({
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_DOMAIN",
    "FEISHU_UAT_ACCESS_TOKEN",
    "FEISHU_UAT_REFRESH_TOKEN",
})
_CREDENTIAL_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_STREAM_STATUS_ANIMATION_MARKERS = ("\u200b", "\u200c", "\u200d", "\ufeff")


def _animated_stream_status(phase: str, tick: int) -> str:
    del phase
    marker = _STREAM_STATUS_ANIMATION_MARKERS[(max(1, int(tick)) - 1) % len(_STREAM_STATUS_ANIMATION_MARKERS)]
    return marker


def _strip_stream_status_animation_markers(text: str) -> str:
    result = str(text or "")
    for marker in _STREAM_STATUS_ANIMATION_MARKERS:
        result = result.replace(marker, "")
    return result


def _maybe_budget_footer(content: str, tool_turns: int, cap: int) -> str:
    """Append a budget-exhaustion footer when the tool-turn cap is reached."""
    if cap <= 0 or tool_turns < cap:
        return content
    footer = f"\n\n---\n⚠️ 已用满 {cap} 步工具预算，以上为目前进展；回复“继续”可接着查。"
    base = content or ""
    if base.endswith(footer):
        return base
    return base + footer


async def stream_run_agent(  # type: ignore[override]
    event: Any,
    profile_home: Path,
    *,
    messages: Optional[list[dict]] = None,
):
    """Yields ``(kind, text)`` tuples — Phase 4 routes through the real AIAgent.

    With the AIAgent path enabled, the LLM gets the full toolset and can
    actually call PR-added UAT tools. The subprocess bridge emits NDJSON
    events so the parent can forward tool/reasoning/text deltas into the
    Feishu streaming card while the synchronous AIAgent loop is still running.

    Falls back to the legacy streaming path on AIAgent failure (preserves
    the visible-typing UX even when tools cannot fire). ``messages`` is the
    router-provided conversation history; the subprocess receives prior turns
    as ``conversation_history`` and the current event text as the active user
    message.
    """
    try:
        content_parts: list[str] = []
        final_text = ""
        tool_started_count = 0
        stream = (
            _stream_aiagent_subprocess(event, profile_home, messages=messages)
            if messages is not None
            else _stream_aiagent_subprocess(event, profile_home)
        )
        async for kind, payload in stream:
            if kind == "done":
                final_text = str(payload or "")
                continue
            if kind == "tool_started":
                tool_started_count += 1
            if kind == "content":
                text = str(payload or "")
                if text:
                    content_parts.append(text)
                    yield "content", text
                continue
            yield kind, payload
        content_text = "".join(content_parts)
        if final_text and not content_text.strip():
            yield "content", final_text
            content_text = final_text
        elif final_text == _TRUNCATION_NOTICE and _TRUNCATION_NOTICE not in content_text:
            # Output-length truncation AFTER partial content already streamed into
            # the card: the streamed text is incomplete, so append the recovery
            # hint as a trailing delta (the normal-completion branch above only
            # fires when nothing streamed, dropping the notice otherwise).
            tail = "\n\n" + _TRUNCATION_NOTICE
            yield "content", tail
            content_text += tail
        cap = int(os.getenv("HERMES_MAX_ITERATIONS", "30"))
        footer_text = _maybe_budget_footer(content_text, tool_started_count, cap)
        footer_tail = footer_text[len(content_text):]
        if footer_tail:
            yield "content", footer_tail
        if final_text or content_parts:
            return
    except Exception as exc:
        logger.warning(
            "[multitenancy] streaming AIAgent path failed (%s); falling back to legacy stream",
            exc, exc_info=True,
        )

    async for kind, text in _stream_loop(event, profile_home, messages=messages):
        yield kind, text


def _normalize_reasoning_compare(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _reasoning_for_state_db(
    content: str,
    reasoning: str,
    *,
    preserve_reasoning: bool = True,
) -> str | None:
    """Return reasoning safe to persist for WebUI history rendering."""
    reasoning_text = str(reasoning or "")
    if not preserve_reasoning or not reasoning_text.strip():
        return None
    content_norm = _normalize_reasoning_compare(content)
    reasoning_norm = _normalize_reasoning_compare(reasoning_text)
    if content_norm and content_norm == reasoning_norm:
        return None
    return reasoning_text


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

    config = _load_profile_config(profile_home)
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
        base_url = _resolve_base_url(provider, model_spec == primary, config, env_overrides)

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
    """Run the inbound event through hermes' real AIAgent (with tool-loop).

    Phase 4 — replaces the spike one-shot ``chat.completions.create`` call
    with a full ``AIAgent.run_conversation()`` loop, so PR-added UAT tools
    (e.g. feishu_calendar_list_events) actually fire. Sets
    ``sender_open_id_scope`` so per-user UAT files are loaded correctly.

    Falls back to the legacy thin LLM call (kept below as
    ``_legacy_real_run_agent``) on any AIAgent failure so the spike-style
    fallback path still answers the user — without tools, but at least with
    a coherent reply.
    """
    try:
        if messages is not None:
            return await _run_aiagent_subprocess(event, profile_home, messages=messages)
        return await _run_aiagent_subprocess(event, profile_home)
    except Exception as exc:
        logger.warning(
            "[multitenancy] AIAgent path failed (%s); falling back to legacy spike",
            exc, exc_info=True,
        )
    # Legacy / fallback path — no tool-loop, but still answers.
    return await _legacy_real_run_agent(event, profile_home, messages=messages)


async def _legacy_real_run_agent(
    event: Any,
    profile_home: Path,
    *,
    messages: Optional[list[dict]] = None,
) -> str:
    """Original spike implementation — kept as a fallback for the AIAgent path."""
    from openai import AsyncOpenAI
    from dotenv import dotenv_values

    config = _load_profile_config(profile_home)
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

        base_url = _resolve_base_url(provider, model_spec == primary, config, env_overrides)

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


def _event_metadata(event: Any) -> dict[str, Any]:
    raw_event = getattr(event, "raw_event", None)
    if not isinstance(raw_event, dict):
        return {}
    metadata = raw_event.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _model_spec_for_event(default_spec: str, event: Any) -> str:
    """Return per-run model override from WebUI broker metadata when present."""
    metadata = _event_metadata(event)
    model = str(metadata.get("model") or "").strip()
    if not model:
        return default_spec
    if "/" in model:
        return model
    provider = str(metadata.get("provider") or "").strip()
    if provider:
        return f"{provider}/{model}"
    default_provider, _default_model = _split_model_spec(default_spec)
    return f"{default_provider}/{model}"


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


def _resolve_custom_provider_api_key(
    config: dict[str, Any],
    provider: str,
) -> Optional[str]:
    """Inline ``api_key`` for a ``custom:<name>`` provider from config.yaml.

    ``_resolve_api_key`` only checks env vars + auth.json's inline tokens.
    Multi-tenant profiles configure their LLM via the ``custom_providers:`` list
    (the model migration to litellm.sre stores the key inline there; the secret
    in auth.json's credential_pool lives in an encrypted vault, NOT as an inline
    access_token), so ``_resolve_api_key`` returned None and every turn raised
    "no API key for primary provider 'custom:...'". Match the custom provider by
    its ``custom:<normalized-name>`` slug or by base_url and return the inline key.
    """
    # Only the custom-provider family. The vault-based credential_pool resolver
    # is intentionally NOT used here: the encrypted vault is masked inside the
    # bwrap sandbox where this runs, so the inline config.yaml key is the only
    # one readable in-context. Revisit if the vault-in-sandbox masking is fixed.
    pl = provider.lower() if provider else ""
    if not (pl == "custom" or pl.startswith("custom:")):
        return None
    custom_providers = config.get("custom_providers")
    if not isinstance(custom_providers, list):
        return None
    want_name = provider.split(":", 1)[1].strip().lower() if ":" in provider else ""
    model_base_url = str((config.get("model") or {}).get("base_url") or "").strip().rstrip("/")
    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip().lower().replace(" ", "-")
        base_url = str(
            entry.get("base_url") or entry.get("url") or entry.get("api") or ""
        ).strip().rstrip("/")
        # When the provider carries a :name slug, match by name ONLY — never by
        # base_url, which is the default model's URL and would wrongly match a
        # different entry. Bare "custom" (no slug) falls back to base_url.
        if want_name:
            matched = name == want_name
        else:
            matched = bool(model_base_url) and base_url == model_base_url
        if matched:
            key = str(entry.get("api_key") or "").strip()
            if key:
                return key
    return None


def _resolve_base_url(
    provider: str,
    is_primary: bool,
    config: dict[str, Any],
    env_overrides: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve the API base URL for *provider*. Primary model honors profile config/env."""
    if is_primary:
        explicit = config.get("model", {}).get("base_url")
        if explicit:
            return explicit
        for env_name in _PROVIDER_BASE_URL_ENV_KEYS.get(provider, ()):
            value = (env_overrides or {}).get(env_name) or os.environ.get(env_name)
            if value:
                return str(value)
    return _PROVIDER_BASE_URLS.get(provider)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    import yaml
    return yaml.safe_load(path.read_text()) or {}


def _merge_profile_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge shared Hermes config with profile-local overrides."""
    merged = dict(base or {})
    for key, value in (override or {}).items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_profile_config(current, value)
        else:
            merged[key] = value
    return merged


def _normalize_model_spec_inplace(config: dict[str, Any]) -> None:
    """Runtime safety net: if ``model.default`` is a bare model name (no provider
    prefix) but ``model.provider`` is set, prepend it so ``_split_model_spec`` can
    parse it.

    This mirrors router._normalize_profile_config but runs at every config READ,
    not only at provision time. Root-cause fix for the recurring
    "model spec missing provider prefix: 'tencent-sonnet-4-6'" failures: the
    provision-time normalization can be bypassed by some write paths, leaving a
    bare default on disk; without a runtime net, every turn for that profile then
    fails. Applying it here makes a bare-but-providered config self-heal at read
    time, so no write path can ever fail a turn this way again.
    """
    model = config.get("model")
    if not isinstance(model, dict):
        return
    default_model = str(model.get("default") or "").strip()
    provider = str(model.get("provider") or "").strip()
    if default_model and provider and "/" not in default_model:
        model["default"] = f"{provider}/{default_model}"


def _load_profile_config(profile_home: Path) -> dict[str, Any]:
    shared_home = _resolve_shared_hermes_home(profile_home)
    shared = _load_yaml(shared_home / "config.yaml")
    profile = _load_yaml(profile_home / "config.yaml")
    merged = _merge_profile_config(shared, profile)
    _normalize_model_spec_inplace(merged)
    return merged


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


def _resolve_shared_hermes_home(profile_home: Path) -> Path:
    """Return the default Hermes root that stores cross-profile shared auth."""
    explicit = os.getenv("HERMES_SHARED_HOME")
    if explicit:
        return Path(explicit).expanduser()
    profile_home = Path(profile_home).expanduser()
    if profile_home.parent.name == "profiles":
        return profile_home.parent.parent
    return profile_home


def _configure_feishu_uat_home(feishu_oapi_module: Any, profile_home: Path) -> Path:
    """Bind Feishu UAT lookups to the profile's own ``feishu_uat/`` subdir.

    Previously pointed at ``<shared>/feishu_uat/`` (one directory shared by
    every profile). That layout meant every profile's subprocess could
    enumerate every tenant's UAT JSON via ``os.listdir(FEISHU_UAT_DIR)``;
    the per-open_id filename was the only thing keeping reads separate.

    With this change, ``FEISHU_UAT_DIR`` is rebound to ``<profile>/feishu_uat/``
    so a subprocess sees only the JSON files that belong to its tenant.
    Combined with the 0700 profile_home (commit "Harden profile directory
    tree at provision time") and the env whitelist (commit "Isolate AIAgent
    subprocess env"), cross-tenant UAT enumeration becomes a filesystem-level
    deny under档 A.

    Caveat: the gateway-process OAuth callback handler lives in the upstream
    hermes-agent repo and still writes new bindings to ``<shared>/feishu_uat/``.
    The org-sync pass copies those forward into the right profile via
    :func:`hermes_multitenancy.sync.feishu_org._migrate_feishu_uat_for_employee`.
    New bindings captured between sync passes are invisible to the AIAgent
    subprocess until the next sync — TODO: route OAuth callbacks through the
    multitenancy router so writes land profile-scoped from the start.

    Returns the shared_home (still used for cron jobs and snapshot cache).
    """
    shared_home = _resolve_shared_hermes_home(profile_home)
    profile_uat_dir = profile_home / "feishu_uat"
    profile_uat_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    feishu_oapi_module.FEISHU_UAT_PATH = profile_home / "feishu_uat.json"
    feishu_oapi_module.FEISHU_UAT_DIR = profile_uat_dir
    _install_feishu_app_db_broker(feishu_oapi_module, shared_home)
    _install_feishu_uat_db_broker(feishu_oapi_module, profile_home, shared_home)
    return shared_home


class _MissingCurrentSenderOpenId:
    def get(self) -> str:
        return ""


@contextmanager
def _missing_sender_open_id_scope(_value: Optional[str]) -> Iterator[None]:
    yield


def _load_feishu_oapi_runtime(profile_home: Path) -> tuple[Any, Any, Path]:
    """Load legacy Feishu OAPI context when Hermes still provides it.

    Hermes v0.14 upstream no longer carries the local fork's
    ``tools.feishu_oapi_client`` module. WebUI/API Run Broker traffic can still
    run with core tools and lark-cli, so missing legacy OAPI support should not
    abort AIAgent startup.
    """
    shared_home = _resolve_shared_hermes_home(profile_home)
    try:
        from tools import feishu_oapi_client as feishu_oapi
    except Exception as exc:
        logger.info("[multitenancy] legacy Feishu OAPI client unavailable; skipping OAPI credential patch: %s", exc)
        _configure_cron_home(shared_home)
        return _missing_sender_open_id_scope, _MissingCurrentSenderOpenId(), shared_home

    sender_open_id_scope = feishu_oapi.sender_open_id_scope
    current_sender_open_id = feishu_oapi.current_sender_open_id
    shared_home = _configure_feishu_uat_home(feishu_oapi, profile_home)
    _install_legacy_feishu_refresh_bridge(shared_home)
    _configure_cron_home(shared_home)
    return sender_open_id_scope, current_sender_open_id, shared_home


def _install_legacy_feishu_refresh_bridge(shared_home: Path) -> None:
    """Route old Hermes UAT refresh calls through multitenancy.

    Legacy fork modules refresh per-user UAT by calling
    ``hermes_cli.feishu_auth.refresh_uat_for_user(open_id, app_id, app_secret)``.
    In the multitenancy target state, that function must not own token
    lifecycle.  Patch it to resolve the routed profile and delegate to
    ``feishu_uat_auth.refresh_uat_for_user`` instead.
    """
    try:
        feishu_auth = importlib.import_module("hermes_cli.feishu_auth")
    except Exception:
        return

    if getattr(feishu_auth, "_hermes_mt_refresh_bridge_installed", False):
        return
    original = getattr(feishu_auth, "refresh_uat_for_user", None)
    if original is None:
        return
    setattr(feishu_auth, "_hermes_mt_original_refresh_uat_for_user", original)

    def _refresh_uat_for_user_via_multitenancy(
        open_id: str,
        app_id: str | None = None,
        app_secret: str | None = None,
    ) -> dict[str, Any] | None:
        refresh_mod = importlib.import_module("hermes_multitenancy.feishu_uat_auth")
        return refresh_mod.refresh_uat_for_user(
            open_id=str(open_id),
            client_id=app_id,
            client_secret=app_secret,
            shared_home=shared_home,
            force=True,
        )

    feishu_auth.refresh_uat_for_user = _refresh_uat_for_user_via_multitenancy
    feishu_auth._hermes_mt_refresh_bridge_installed = True


def _install_feishu_app_db_broker(feishu_oapi_module: Any, shared_home: Path) -> None:
    """Patch Feishu app credential resolution to prefer the credential vault."""
    original = getattr(feishu_oapi_module, "_hermes_mt_original_resolve_feishu_credentials", None)
    if original is None:
        original = getattr(feishu_oapi_module, "_resolve_feishu_credentials", None)
        if original is None:
            return
        setattr(feishu_oapi_module, "_hermes_mt_original_resolve_feishu_credentials", original)

    def _resolve_feishu_credentials_with_broker() -> tuple[str, str, str]:
        from .credentials import CredentialStore

        store = None
        try:
            store = CredentialStore(shared_home / "multitenancy.db")
            status = store.get_status(
                profile_name=_FEISHU_APP_CREDENTIAL_PROFILE,
                subject_id=_FEISHU_APP_CREDENTIAL_SUBJECT,
                provider="feishu",
                secret_kind="app",
            )
            if status.get("status") == "valid":
                payload = store.get_secret_for_runtime(
                    profile_name=_FEISHU_APP_CREDENTIAL_PROFILE,
                    subject_id=_FEISHU_APP_CREDENTIAL_SUBJECT,
                    provider="feishu",
                    secret_kind="app",
                )
                app_id = str(payload.get("app_id") or payload.get("FEISHU_APP_ID") or "").strip()
                app_secret = str(payload.get("app_secret") or payload.get("FEISHU_APP_SECRET") or "").strip()
                domain = str(payload.get("domain") or payload.get("FEISHU_DOMAIN") or "feishu").strip().lower()
                if app_id and app_secret:
                    logger.info("[multitenancy] loaded Feishu app credential from credential vault")
                    return app_id, app_secret, domain or "feishu"
        except Exception:
            logger.debug("[multitenancy] Feishu app credential vault lookup skipped", exc_info=True)
        finally:
            if store is not None:
                store.close()
        return original()

    feishu_oapi_module._resolve_feishu_credentials = _resolve_feishu_credentials_with_broker


def _install_feishu_uat_db_broker(feishu_oapi_module: Any, profile_home: Path, shared_home: Path) -> None:
    """Patch Feishu UAT loading to prefer the multitenancy credential vault.

    This is a read-through migration bridge: existing profile-local JSON files
    keep working, but successful reads are copied into ``multitenancy.db`` as
    sealed credential rows.  Once production canaries prove parity, JSON can be
    demoted to a migration-only fallback.
    """
    original = getattr(feishu_oapi_module, "_hermes_mt_original_load_uat", None)
    if original is None:
        original = getattr(feishu_oapi_module, "_load_uat", None)
        if original is None:
            return
        setattr(feishu_oapi_module, "_hermes_mt_original_load_uat", original)

    def _load_uat_with_broker(open_id: Optional[str] = None) -> dict:
        if open_id:
            from .credentials import CredentialStore

            try:
                store = CredentialStore(shared_home / "multitenancy.db")
                status = store.get_status(
                    profile_name=profile_home.name,
                    subject_id=open_id,
                    provider="feishu",
                    secret_kind="uat",
                )
                if status.get("status") == "valid":
                    payload = store.get_secret_for_runtime(
                        profile_name=profile_home.name,
                        subject_id=open_id,
                        provider="feishu",
                        secret_kind="uat",
                    )
                    if status.get("expires_at") is not None and "expires_at" not in payload:
                        payload["expires_at"] = status["expires_at"]
                    store.close()
                    logger.info(
                        "[multitenancy] loaded Feishu UAT from credential vault profile=%s subject=%s",
                        profile_home.name,
                        open_id,
                    )
                    return payload
                store.close()
            except Exception:
                logger.debug(
                    "[multitenancy] Feishu UAT credential vault lookup skipped",
                    exc_info=True,
                )

        data = original(open_id)
        if open_id and isinstance(data, dict) and data.get("access_token"):
            _store_feishu_uat_payload(shared_home, profile_home.name, open_id, data)
        return data

    feishu_oapi_module._load_uat = _load_uat_with_broker


def _store_feishu_uat_payload(shared_home: Path, profile_name: str, open_id: str, data: dict[str, Any]) -> None:
    try:
        from .credentials import CredentialStore

        scopes = data.get("scopes") or data.get("scope") or []
        if isinstance(scopes, str):
            scopes = [part for part in scopes.replace(",", " ").split() if part]
        expires_at = data.get("expires_at")
        store = CredentialStore(shared_home / "multitenancy.db")
        store.put_credential(
            profile_name=profile_name,
            subject_id=open_id,
            provider="feishu",
            secret_kind="uat",
            payload=data,
            scopes=scopes,
            expires_at=int(expires_at) if expires_at else None,
        )
        store.close()
        logger.info(
            "[multitenancy] imported profile Feishu UAT into credential vault profile=%s subject=%s",
            profile_name,
            open_id,
        )
    except Exception:
        logger.debug("[multitenancy] failed to import Feishu UAT into credential vault", exc_info=True)


def _configure_cron_home(shared_home: Path) -> None:
    """Bind cron storage path for AIAgent subprocesses.

    Behavior depends on HERMES_HOME layout:
    - Multitenancy nested layout (``<root>/profiles/<name>``): no-op. Cron
      writes go to the profile default ``<profile>/cron/jobs.json``; a
      dedicated multi-profile worker in this plugin (see ``cron_worker``)
      scans all profiles and dispatches due jobs.
    - Single-profile or legacy layout: rebind to shared Hermes home so the
      gateway's built-in cron ticker (which scans ``<shared>/cron/jobs.json``)
      can see the jobs.
    """
    current_home = os.environ.get("HERMES_HOME")
    if current_home:
        current_path = Path(current_home).expanduser()
        if current_path.parent.name == "profiles":
            _install_profile_cron_owner_patch(current_path)
            logger.info(
                "[multitenancy] cron jobs stay in profile-default location: %s/cron/",
                current_path,
            )
            return

    import importlib

    shared_home = Path(shared_home).expanduser()
    old_home = os.environ.get("HERMES_HOME")
    try:
        os.environ["HERMES_HOME"] = str(shared_home)
        cron_jobs = importlib.import_module("cron.jobs")
        cron_jobs.HERMES_DIR = shared_home.resolve()
        cron_jobs.CRON_DIR = cron_jobs.HERMES_DIR / "cron"
        cron_jobs.JOBS_FILE = cron_jobs.CRON_DIR / "jobs.json"
        cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"

        cronjob_tools = importlib.import_module("tools.cronjob_tools")

        def _validate_shared_cron_script_path(script: Optional[str]) -> Optional[str]:
            if not script or not str(script).strip():
                return None
            raw = str(script).strip()
            if raw.startswith(("/", "~")) or (len(raw) >= 2 and raw[1] == ":"):
                return (
                    "Script path must be relative to shared ~/.hermes/scripts/. "
                    f"Got absolute or home-relative path: {raw!r}."
                )
            from tools.path_security import validate_within_dir

            scripts_dir = shared_home / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            containment_error = validate_within_dir(scripts_dir / raw, scripts_dir)
            if containment_error:
                return f"Script path escapes the shared scripts directory via traversal: {raw!r}"
            return None

        cronjob_tools._validate_cron_script_path = _validate_shared_cron_script_path
        _install_profile_cron_owner_patch(shared_home)
        logger.info("[multitenancy] cron jobs bound to shared Hermes home: %s", cron_jobs.JOBS_FILE)
    except Exception as exc:
        logger.warning("[multitenancy] failed to bind cron jobs to shared Hermes home: %s", exc)
    finally:
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home


def _install_profile_cron_owner_patch(default_profile_home: Path) -> None:
    """Patch native cronjob tools so Feishu-created jobs persist owner context."""
    import importlib

    try:
        cronjob_tools = importlib.import_module("tools.cronjob_tools")
    except Exception:
        logger.debug("[multitenancy] cronjob owner patch skipped", exc_info=True)
        return
    if getattr(cronjob_tools, "_hermes_multitenancy_owner_patch", False):
        return

    original_create_job = getattr(cronjob_tools, "create_job", None)
    original_update_job = getattr(cronjob_tools, "update_job", None)
    original_trigger_job = getattr(cronjob_tools, "trigger_job", None)
    if original_create_job is None or original_update_job is None:
        return

    def _profile_home() -> Path:
        raw = os.environ.get("HERMES_HOME", "").strip()
        return Path(raw).expanduser() if raw else Path(default_profile_home).expanduser()

    def _owner_updates(job: dict) -> dict[str, str]:
        from .cron_worker import infer_cron_owner_context

        return infer_cron_owner_context(job, profile_home=_profile_home())

    def create_job_with_owner(*args: Any, **kwargs: Any) -> Any:
        job = original_create_job(*args, **kwargs)
        if not isinstance(job, dict):
            return job
        updates = _owner_updates(job)
        if not updates:
            return job
        updated = original_update_job(job["id"], updates)
        return updated or {**job, **updates}

    def update_job_with_owner(job_id: str, updates: dict[str, Any]) -> Any:
        job = original_update_job(job_id, updates)
        if not isinstance(job, dict):
            return job
        owner_updates = _owner_updates(job)
        if not owner_updates:
            return job
        if (
            job.get("owner_open_id") == owner_updates.get("owner_open_id")
            and job.get("owner_profile") == owner_updates.get("owner_profile")
        ):
            return job
        refreshed = original_update_job(job_id, owner_updates)
        return refreshed or {**job, **owner_updates}

    def trigger_job_via_run_broker(job_id: str) -> Any:
        from . import cron_worker

        resolver = getattr(cronjob_tools, "resolve_job_ref", None)
        job = resolver(job_id) if callable(resolver) else None
        if not isinstance(job, dict):
            raise RuntimeError(
                f"Cron job '{job_id}' not found. Use cronjob(action='list') to inspect jobs."
            )
        updates = _owner_updates(job)
        owner_open_id = str(job.get("owner_open_id") or updates.get("owner_open_id") or "").strip()
        if not owner_open_id.startswith("ou_"):
            raise RuntimeError("cron owner_open_id is required for RunBroker trigger")
        return cron_worker.trigger_profile_cron_job_via_run_broker(
            job_id=str(job.get("id") or job_id),
            profile_home=_profile_home(),
            owner_open_id=owner_open_id,
        )

    cronjob_tools.create_job = create_job_with_owner
    cronjob_tools.update_job = update_job_with_owner
    if original_trigger_job is not None:
        cronjob_tools.trigger_job = trigger_job_via_run_broker
    cronjob_tools._hermes_multitenancy_owner_patch = True
    logger.info("[multitenancy] patched native cronjob tool owner context")


def _log_aiagent_tool_progress(
    event_type: str,
    tool_name: str,
    preview: Any = None,
    args: Any = None,
    **kwargs: Any,
) -> None:
    """Persist AIAgent tool progress for gateway stress-test observability."""
    if event_type == "tool.started":
        logger.info("[multitenancy] tool.started %s preview=%s", tool_name, preview or "")
    elif event_type == "tool.completed":
        logger.info(
            "[multitenancy] tool.completed %s duration=%.2fs error=%s",
            tool_name,
            float(kwargs.get("duration") or 0.0),
            bool(kwargs.get("is_error")),
        )


# ---------------------------------------------------------------------------
# Isolated AIAgent subprocess bridge
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Return a JSON-safe representation for dataclass/enum-ish event fields."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    return str(value)


def _jsonable_deep(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable_deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_deep(v) for v in value]
    return _jsonable(value)


def _get_nested_value(obj: Any, path: tuple[str, ...]) -> Any:
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            cur = getattr(cur, key, None)
        if cur is None:
            return None
    return cur


def _find_ou_value(obj: Any) -> str:
    """Best-effort recursive search for a Feishu open_id in raw event data."""
    if isinstance(obj, str):
        return obj if obj.startswith("ou_") else ""
    if isinstance(obj, dict):
        for key in ("open_id", "openId"):
            value = obj.get(key)
            if isinstance(value, str) and value.startswith("ou_"):
                return value
        for value in obj.values():
            found = _find_ou_value(value)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            found = _find_ou_value(value)
            if found:
                return found
    return ""


def _resolve_subprocess_sender_open_id(event: Any) -> str:
    """Resolve sender ou_* for the child process after Feishu batching."""
    try:
        from tools.feishu_oapi_client import current_sender_open_id
        current = current_sender_open_id.get()
        if current and str(current).startswith("ou_"):
            return str(current)
    except Exception:
        pass

    source = getattr(event, "source", None)
    for candidate in (
        getattr(event, "sender_open_id", None),
        getattr(source, "open_id", None) if source is not None else None,
        getattr(source, "user_id", None) if source is not None else None,
        getattr(source, "user_id_alt", None) if source is not None else None,
    ):
        if candidate and str(candidate).startswith("ou_"):
            return str(candidate)

    raw = getattr(event, "raw_message", None)
    for path in (
        ("event", "sender", "sender_id", "open_id"),
        ("event", "message", "sender", "sender_id", "open_id"),
        ("sender", "sender_id", "open_id"),
        ("message", "sender", "sender_id", "open_id"),
        ("sender_id", "open_id"),
    ):
        value = _get_nested_value(raw, path)
        if value and str(value).startswith("ou_"):
            return str(value)
    return _find_ou_value(raw)


def _jsonable_messages(messages: Optional[list[dict]]) -> list[dict] | None:
    if not messages:
        return None
    payload_messages: list[dict] = []
    for message in messages:
        if isinstance(message, dict):
            jsonable = _jsonable_deep(message)
            if isinstance(jsonable, dict):
                payload_messages.append(jsonable)
    return payload_messages


def _event_to_subprocess_payload(
    event: Any,
    profile_home: Path,
    *,
    messages: Optional[list[dict]] = None,
) -> dict[str, Any]:
    """Serialize the small MessageEvent surface needed by the child runner."""
    source = getattr(event, "source", None)
    source_payload: dict[str, Any] = {}
    if source is not None:
        for key in (
            "platform",
            "chat_id",
            "chat_name",
            "chat_type",
            "user_id",
            "user_name",
            "thread_id",
            "chat_topic",
            "user_id_alt",
            "chat_id_alt",
            "is_bot",
            "guild_id",
            "parent_chat_id",
            "message_id",
        ):
            if hasattr(source, key):
                source_payload[key] = _jsonable(getattr(source, key))

    message_id = (
        getattr(event, "message_id", None)
        or source_payload.get("message_id")
        or ""
    )
    payload = {
        "event": {
            "text": getattr(event, "text", "") or "",
            "message_id": _jsonable(message_id),
            "sender_open_id": _resolve_subprocess_sender_open_id(event),
            "source": source_payload,
        },
        "profile_home": str(profile_home),
    }
    payload_messages = _jsonable_messages(messages)
    if payload_messages is not None:
        payload["messages"] = payload_messages
    return payload


# Whitelisted parent-process env keys carried into AIAgent subprocesses.
#
# Anything not listed here is dropped before spawning the child — this is the
# core of profile isolation档 A. New HERMES_* plumbing variables MUST be added
# explicitly so a future feature does not accidentally widen the surface.
#
# Provider API keys/tokens are NEVER on this list. They live in
# ``<profile_home>/.env`` or ``auth.json`` and are loaded inside the child by
# ``_run_with_aiagent``'s own dotenv path. Credential-vault encryption keys
# are explicit Hermes plumbing so the sandboxed Feishu client can decrypt only
# its own DB credential row; terminal/code subprocesses apply secret-name env
# filtering before executing model-generated commands.
#
_SUBPROCESS_ENV_ALLOWLIST: frozenset[str] = frozenset({
    # POSIX basics
    "PATH", "USER", "LOGNAME", "SHELL", "TERM",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    # Python runtime
    "PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONIOENCODING",
    "PYTHONDONTWRITEBYTECODE",
    # macOS Cocoa text-encoding marker (some Python builds require it)
    "__CF_USER_TEXT_ENCODING",
    # SSL trust stores — some skills call HTTPS endpoints and need CA bundles
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    # Hermes plumbing the child reads directly. Add new keys here when wiring
    # additional configuration through to the subprocess.
    "HERMES_AIAGENT_SUBPROCESS_TIMEOUT",
    "HERMES_MAX_ITERATIONS",
    "HERMES_MULTITENANCY_APPROVAL_TIMEOUT",
    "HERMES_MULTITENANCY_TOOLSETS_MODE",
    "HERMES_MULTITENANCY_CREDENTIAL_KEY",
    "HERMES_CREDENTIAL_KEY",
    "HERMES_APPROVAL_GATEWAY_TIMEOUT",
    "HERMES_VOD_IMAGE_MODEL_OVERRIDE",
    # RunBroker auth so the sandboxed agent's cronjob(action=run) tool can
    # authenticate to the router-owned RunBroker (:8766). Shared infra key
    # (server enforces per-profile scope via X-Hermes-Profile / X-Hermes-User-Key),
    # same class as the credential keys above — not a per-user secret. Without
    # it the child sends no Bearer token and the cron trigger gets 401.
    "HERMES_RUN_BROKER_KEY",
    "HERMES_MULTITENANCY_RUN_BROKER_KEY",
    "HERMES_RUN_BROKER_URL",
    "HERMES_MULTITENANCY_RUN_BROKER_URL",
    "HERMES_MULTITENANCY_CRED_BROKER_TOKEN",
    "HERMES_MULTITENANCY_CRED_BROKER_URL",
    "HERMES_MULTITENANCY_CRED_LEASE",
    "HERMES_MULTITENANCY_RUN_ID",
    "HERMES_LARK_CLI_RUN_TOKEN",
    "HERMES_LARK_CLI_AUTHORIZED",
    "HERMES_LARK_CLI_REAL_BIN",
    "HERMES_MT_SECURITY_AUDIT_PATH",
})

_FEISHU_APP_CREDENTIAL_PROFILE = "__global__"
_FEISHU_APP_CREDENTIAL_SUBJECT = "feishu_app"


def _build_subprocess_env(
    profile_home: Path,
    *,
    approval_dir: Path,
    event_stream: bool = False,
    extra: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Build a sanitized env for the AIAgent subprocess (档 A isolation).

    Two guarantees enforced here:

      1. **Env whitelist** — the parent gateway's full env is NOT inherited.
         Only keys in :data:`_SUBPROCESS_ENV_ALLOWLIST` carry over. This stops
         API keys / OAuth tokens / shell secrets exported into the gateway
         process from leaking into a profile's tool execution.

      2. **Profile compatibility pivots** — HOME, WORKSPACE, XDG
         cache/config/state/data and TMPDIR redirect into the profile's own
         directory tree. Token-oriented skills and CLIs that were written for
         OpenClaw-style ``$HOME`` or ``/workspace`` semantics can therefore run
         unchanged: ``Path.home() / ".keepai"`` lands in the current profile,
         ``/workspace/credentials/gitlab.token`` maps to the profile workspace,
         and CLIs/MCP servers see stable profile identity via ``HERMES_PROFILE``
         plus ``KEP_PROFILE`` for existing Keep tooling.

    Skills that already use :mod:`hermes_multitenancy.skill_storage` continue
    to work, but it is no longer the only safe path. The multitenancy runtime
    itself provides the compatibility boundary for unmodified upstream skills.

    The directories ``home/``, ``workspace/``, ``cache/``, ``config/``,
    ``state/``, ``data/`` and ``tmp/`` are created on first use (mode 0700).

    Profile-local secrets are loaded inside the child by
    :func:`_run_with_aiagent` from ``<profile_home>/.env`` and ``auth.json``;
    they are intentionally NOT injected here so the child never sees a parent
    env-derived key as an "ambient" credential.
    """
    parent = os.environ
    env: dict[str, str] = {
        key: parent[key] for key in _SUBPROCESS_ENV_ALLOWLIST if key in parent
    }
    if strict_context_enabled():
        env.pop("HERMES_MULTITENANCY_CREDENTIAL_KEY", None)
        env.pop("HERMES_CREDENTIAL_KEY", None)

    profile_home = profile_home.expanduser()
    env.update(_profile_env_for_aiagent(profile_home))
    credential_env = _credential_env_for_aiagent(profile_home)
    env.update(credential_env)
    env.update(_force_env_for_terminal_passthrough(credential_env))

    # OpenClaw-compatible token boundary: HOME and /workspace-style variables
    # point into the routed profile so unmodified token skills do not write to
    # the shared service user's home.
    pivot = {
        "HOME":            profile_home / "home",
        "WORKSPACE":       profile_home / "workspace",
        "XDG_CACHE_HOME":  profile_home / "cache",
        "XDG_CONFIG_HOME": profile_home / "config",
        "XDG_STATE_HOME":  profile_home / "state",
        "XDG_DATA_HOME":   profile_home / "data",
        "TMPDIR":          profile_home / "tmp",
    }
    for path in pivot.values():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    env.update({key: str(path) for key, path in pivot.items()})

    env["HERMES_HOME"]                      = str(profile_home)
    env["HERMES_SHARED_HOME"]               = str(_resolve_shared_hermes_home(profile_home))
    env["HERMES_PROFILE"]                   = profile_home.name
    env["KEP_PROFILE"]                      = profile_home.name
    env["HERMES_GATEWAY_SESSION"]           = "1"
    env["HERMES_EXEC_ASK"]                  = "1"
    env["HERMES_MULTITENANCY_APPROVAL_DIR"] = str(approval_dir)
    lark_cli_env = _lark_cli_sidecar_env_for_aiagent(profile_home)
    env.update(lark_cli_env)
    env.update(_force_env_for_terminal_passthrough(lark_cli_env))
    env.update(_browser_env_for_aiagent(profile_home))
    if event_stream:
        env["HERMES_AIAGENT_EVENT_STREAM"] = "1"

    shared_bin = str(_resolve_shared_hermes_home(profile_home) / "bin")
    existing_path = env.get("PATH", "")
    path_parts = [part for part in existing_path.split(os.pathsep) if part]
    deduped = [part for part in path_parts if part != shared_bin]
    env["PATH"] = os.pathsep.join([shared_bin, *deduped])
    if strict_context_enabled():
        real_bin = _resolve_lark_cli_authsidecar_binary(profile_home)
        shim_dir = profile_home / "tmp" / "lark-cli-shim"
        install_lark_cli_shim(shim_dir, real_binary=real_bin)
        env[HERMES_LARK_CLI_REAL_BIN] = str(real_bin)
        env[HERMES_LARK_CLI_RUN_TOKEN] = generate_lark_cli_run_token()
        env.setdefault(
            "HERMES_MT_SECURITY_AUDIT_PATH",
            str(_default_security_audit_path_for_subprocess(profile_home)),
        )
        strict_path_parts = [part for part in env["PATH"].split(os.pathsep) if part]
        env["PATH"] = os.pathsep.join(
            [str(shim_dir), *[part for part in strict_path_parts if part != str(shim_dir)]]
        )

    # Mirror _wrap_with_sandbox's toggle + per-profile gate so the subprocess
    # knows it's running inside a sandbox host. tools/approval.py reads
    # HERMES_SANDBOX_HOST to bypass dangerous-command approval: the sandbox
    # already enforces filesystem/network isolation at the kernel layer, so
    # asking the user about `python -c ...` inside it is duplicate friction.
    # The hardline blocklist (rm -rf /, mkfs, dd /dev/sd, shutdown, fork bomb)
    # is checked BEFORE the bypass in approval.py and remains in effect.
    if os.environ.get("HERMES_USE_SANDBOX") == "1":
        allowlist_raw = os.environ.get("HERMES_SANDBOX_PROFILES", "").strip()
        if not allowlist_raw or profile_home.name in {
            p.strip() for p in allowlist_raw.split(",") if p.strip()
        }:
            env["HERMES_SANDBOX_HOST"] = "1"
            env.setdefault("HERMES_YOLO_MODE", "1")

    if extra:
        env.update(extra)

    # Feishu per-user UAT identity: forward sender open_id so that AIAgent
    # tool-worker threads (which lose ContextVar across ThreadPoolExecutor
    # workers per run_agent.py:1104 / 8479) can recover identity via
    # os.environ. The contextvar is set by sender_open_id_scope in the
    # feishu adapter; asyncio.create_task copies context so it is still
    # alive here even when called from a batched / deferred flush task.
    # Note: MessageEvent dataclass (gateway/platforms/base.py:785) has no
    # sender_open_id field, so a caller-driven getattr would always return
    # empty. ContextVar is the reliable source.
    # Must come AFTER extra so an explicit caller-supplied value wins.
    if "HERMES_FEISHU_USER_OPEN_ID" not in env:
        try:
            from tools import feishu_oapi_client as _foc
            _sender = _foc.current_sender_open_id.get()
            if _sender:
                env["HERMES_FEISHU_USER_OPEN_ID"] = str(_sender)
        except Exception:
            pass
    return env


def _browser_env_for_aiagent(profile_home: Path) -> dict[str, str]:
    """Expose profile-scoped env for Hermes native browser tools."""
    try:
        from .browser_policy import browser_decision, browser_env

        config = _load_yaml(profile_home / "config.yaml")
        return browser_env(browser_decision(config, profile_home))
    except Exception:
        logger.debug("[multitenancy] failed to resolve browser env for subprocess", exc_info=True)
        return {}


def _lark_cli_sidecar_env_for_aiagent(profile_home: Path) -> dict[str, str]:
    """Expose lark-cli authsidecar plumbing without exposing Feishu tokens."""
    parent = os.environ
    proxy = str(parent.get("HERMES_LARK_CLI_AUTH_PROXY") or "").strip()
    key = str(parent.get("HERMES_LARK_CLI_PROXY_KEY") or "").strip()
    app_id = _resolve_lark_cli_app_id(profile_home)
    if not (proxy and key and app_id):
        return {}

    binary = _resolve_lark_cli_authsidecar_binary(profile_home)
    if not binary.exists():
        return {}

    return {
        "HERMES_LARK_CLI_BIN": str(binary),
        "LARKSUITE_CLI_AUTH_PROXY": proxy,
        "LARKSUITE_CLI_PROXY_KEY": key,
        "LARKSUITE_CLI_APP_ID": app_id,
        "LARKSUITE_CLI_BRAND": str(parent.get("HERMES_LARK_CLI_BRAND") or "feishu").strip() or "feishu",
        "LARKSUITE_CLI_DEFAULT_AS": _lark_cli_default_identity(profile_home, ""),
        "LARKSUITE_CLI_STRICT_MODE": str(parent.get("HERMES_LARK_CLI_STRICT_MODE") or "off").strip() or "off",
    }


def _resolve_lark_cli_authsidecar_binary(profile_home: Path) -> Path:
    configured_bin = str(os.environ.get("HERMES_LARK_CLI_BIN") or "").strip()
    if configured_bin:
        return Path(configured_bin).expanduser()
    return _resolve_shared_hermes_home(profile_home) / "bin" / "lark-cli-authsidecar"


def _default_security_audit_path_for_subprocess(profile_home: Path) -> Path:
    configured = str(os.environ.get("HERMES_MT_SECURITY_AUDIT_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()
    try:
        DEFAULT_SECURITY_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        return DEFAULT_SECURITY_AUDIT_PATH
    except OSError:
        return profile_home / "tmp" / DEFAULT_SECURITY_AUDIT_PATH.name


def _resolve_lark_cli_app_id(profile_home: Path) -> str:
    _load_lark_cli_shared_env(profile_home)
    app_id = str(os.environ.get("HERMES_LARK_CLI_APP_ID") or "").strip()
    if app_id:
        return app_id
    app_id = _resolve_lark_cli_app_id_from_profile_uat(profile_home)
    if app_id:
        return app_id
    try:
        from .credentials import CredentialStore

        store = CredentialStore(_resolve_shared_hermes_home(profile_home) / "multitenancy.db")
        try:
            payload = store.get_secret_for_runtime(
                profile_name=_FEISHU_APP_CREDENTIAL_PROFILE,
                subject_id=_FEISHU_APP_CREDENTIAL_SUBJECT,
                provider="feishu",
                secret_kind="app",
            )
        finally:
            store.close()
        return str(payload.get("app_id") or payload.get("FEISHU_APP_ID") or "").strip()
    except Exception:
        return ""


def _resolve_lark_cli_app_id_from_profile_uat(profile_home: Path) -> str:
    """Read the public app_id from the current profile's UAT JSON if present."""
    uat_dir = Path(profile_home).expanduser() / "feishu_uat"
    try:
        candidates = sorted(
            (path for path in uat_dir.glob("*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return ""
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        app_id = str(payload.get("app_id") or payload.get("client_id") or "").strip()
        if app_id:
            return app_id
    return ""


def _is_group_profile_home(profile_home: Path) -> bool:
    profile_home = Path(profile_home).expanduser()
    if profile_home.name.startswith("feishu_group_"):
        return True
    marker = profile_home / "group_profile.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return False
    return str(payload.get("kind") or "").strip().lower() == "group"


def _group_profile_chat_id(profile_home: Path) -> str:
    marker = Path(profile_home).expanduser() / "group_profile.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(payload.get("chat_id") or "").strip()


_LARK_CLI_SHARED_ENV_KEYS = frozenset(
    {
        "HERMES_MULTITENANCY_CREDENTIAL_KEY",
        "HERMES_CREDENTIAL_KEY",
        "HERMES_LARK_CLI_APP_ID",
        "HERMES_LARK_CLI_BRAND",
        "HERMES_LARK_CLI_DEFAULT_AS",
        "HERMES_LARK_CLI_STRICT_MODE",
    }
)


def _load_lark_cli_shared_env(profile_home: Path) -> dict[str, str]:
    """Load only lark-cli broker control-plane env from the shared .env file."""
    shared_home = _resolve_shared_hermes_home(profile_home)
    env_path = shared_home / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    parsed: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key not in _LARK_CLI_SHARED_ENV_KEYS or os.environ.get(key):
            continue
        value = value.strip().strip("'\"")
        if value:
            os.environ[key] = value
            parsed[key] = value
    return parsed


def _payload_has_live_access_token(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    token = str(payload.get("access_token") or payload.get("user_access_token") or payload.get("token") or "").strip()
    if not token:
        return False
    expires_raw = payload.get("expires_at") or payload.get("expire_at") or payload.get("access_token_expires_at")
    try:
        expires = int(expires_raw or 0)
    except (TypeError, ValueError):
        return True
    if expires <= 0:
        return True
    now_ms = int(time.time() * 1000)
    expires_ms = expires if expires > 10_000_000_000 else expires * 1000
    return expires_ms > now_ms + 60_000


def _profile_has_lark_cli_user_credential(profile_home: Path, open_id: str) -> bool:
    open_id = str(open_id or "").strip()
    if not open_id or _is_group_profile_home(profile_home):
        return False

    shared_home = _resolve_shared_hermes_home(profile_home)
    try:
        from .feishu_uat_auth import refresh_uat_if_needed

        refreshed = refresh_uat_if_needed(
            profile_name=Path(profile_home).name,
            open_id=open_id,
            shared_home=shared_home,
            headroom_seconds=300,
        )
        if _payload_has_live_access_token(refreshed):
            return True
    except Exception:
        pass

    try:
        data = json.loads((Path(profile_home).expanduser() / "feishu_uat" / f"{open_id}.json").read_text(encoding="utf-8"))
        if _payload_has_live_access_token(data):
            return True
    except Exception:
        pass

    try:
        from .credentials import CredentialStore

        store = CredentialStore(shared_home / "multitenancy.db")
        try:
            payload = store.get_secret_for_runtime(
                profile_name=Path(profile_home).name,
                subject_id=open_id,
                provider="feishu",
                secret_kind="uat",
            )
        finally:
            store.close()
        return _payload_has_live_access_token(payload)
    except Exception:
        return False


def _lark_cli_default_identity(profile_home: Path, open_id: str) -> str:
    """Return the identity lark-cli should use when a tool call says auto.

    Group profiles never load member UAT, so they default to bot identity.
    Personal profiles use user identity only when a live UAT exists for the
    current sender; otherwise they fall back to bot identity.
    """
    explicit = str(os.environ.get("HERMES_LARK_CLI_DEFAULT_AS") or "").strip().lower()
    if explicit in {"user", "bot"}:
        return explicit
    if _profile_has_lark_cli_user_credential(profile_home, open_id):
        return "user"
    return "bot"


def _log_feishu_identity_context(
    *,
    profile_home: Path,
    shared_home: Path,
    sender_open_id: str,
) -> None:
    """Log Feishu token lookup paths without implying group profiles use member UAT."""
    profile_home = Path(profile_home).expanduser()
    shared_home = Path(shared_home).expanduser()
    logger.info(
        "[multitenancy] Feishu identity context sender=%s profile=%s "
        "profile_uat_dir=%s legacy Feishu UAT compatibility dir=%s "
        "lark_cli_default_identity=%s group_profile=%s",
        sender_open_id,
        profile_home.name,
        profile_home / "feishu_uat",
        shared_home / "feishu_uat",
        _lark_cli_default_identity(profile_home, sender_open_id),
        _is_group_profile_home(profile_home),
    )


def _owner_mapped_bot_chat_ids(profile_home: Path, sender_open_id: str) -> frozenset[str]:
    sender_open_id = str(sender_open_id or "").strip()
    if not sender_open_id or _is_group_profile_home(profile_home):
        return frozenset()
    table = None
    try:
        from .routing import KIND_GROUP, RoutingTable

        table = RoutingTable(_resolve_shared_hermes_home(profile_home) / "multitenancy.db")
        rows = table.list_by_owner(sender_open_id, kind=KIND_GROUP)
    except Exception:
        logger.debug("[multitenancy] failed to resolve owner-mapped bot chat ids", exc_info=True)
        return frozenset()
    finally:
        if table is not None:
            try:
                table.close()
            except Exception:
                logger.debug("[multitenancy] failed to close owner-mapped routing table", exc_info=True)
    return frozenset(str(row.chat_id or "").strip() for row in rows if str(row.chat_id or "").strip())


def _profile_owner_open_id(profile_home: Path) -> str:
    profile_name = Path(profile_home).name
    table = None
    try:
        from .routing import RoutingTable

        table = RoutingTable(_resolve_shared_hermes_home(profile_home) / "multitenancy.db")
        row = table.lookup_by_profile_name(profile_name)
    except Exception:
        logger.debug("[multitenancy] failed to resolve profile owner open_id", exc_info=True)
        return ""
    finally:
        if table is not None:
            try:
                table.close()
            except Exception:
                logger.debug("[multitenancy] failed to close profile-owner routing table", exc_info=True)
    return str(row.owner_open_id or "").strip() if row is not None else ""


@contextmanager
def _lark_cli_auth_broker_scope(
    profile_home: Path,
    sender_open_id: str,
) -> Iterator[dict[str, str]]:
    """Start a per-run lark-cli auth broker and return child env overrides."""
    app_id = _resolve_lark_cli_app_id(profile_home)
    sender_open_id = str(sender_open_id or "").strip()
    if not sender_open_id:
        sender_open_id = _profile_owner_open_id(profile_home)
    binary = _resolve_lark_cli_authsidecar_binary(profile_home)
    is_group_profile = _is_group_profile_home(profile_home)
    if not (app_id and binary.exists() and (sender_open_id or is_group_profile)):
        yield {}
        return

    default_as = _lark_cli_default_identity(profile_home, sender_open_id)
    allowed_bot_chat_ids = _owner_mapped_bot_chat_ids(profile_home, sender_open_id)
    # Always permit the bot identity: the broker is the authoritative gate and now
    # live-re-checks routing per send (see _personal_bot_identity_policy_error), so a
    # sender's freshly-created own group works even when the turn-start cache was
    # empty. Restricting identities to {"user"} here would reject `--as bot` at the
    # broker identity gate before that policy check ever runs (freshness race).
    allowed_identities = frozenset({"user", "bot"})
    key = secrets.token_urlsafe(32)
    server = start_lark_cli_auth_broker_server(
        LarkCliAuthBrokerContext(
            shared_home=_resolve_shared_hermes_home(profile_home),
            profile_name=profile_home.name,
            user_open_id=sender_open_id,
            hmac_key=key,
            allowed_identities=allowed_identities,
            profile_kind="group" if is_group_profile else "user",
            current_chat_id=_group_profile_chat_id(profile_home) if is_group_profile else "",
            allowed_bot_chat_ids=allowed_bot_chat_ids,
        )
    )
    try:
        env = {
            "HERMES_LARK_CLI_BIN": str(binary),
            "HERMES_FEISHU_USER_OPEN_ID": sender_open_id,
            "LARKSUITE_CLI_AUTH_PROXY": server.url,
            "LARKSUITE_CLI_PROXY_KEY": key,
            "LARKSUITE_CLI_APP_ID": app_id,
            "LARKSUITE_CLI_BRAND": str(os.environ.get("HERMES_LARK_CLI_BRAND") or "feishu").strip() or "feishu",
            "LARKSUITE_CLI_DEFAULT_AS": default_as,
            "LARKSUITE_CLI_STRICT_MODE": str(os.environ.get("HERMES_LARK_CLI_STRICT_MODE") or "off").strip() or "off",
        }
        if allowed_bot_chat_ids:
            env["HERMES_FEISHU_BOT_ALLOWED_CHAT_IDS"] = ",".join(sorted(allowed_bot_chat_ids))
        yield env
    finally:
        server.close()


@contextmanager
def _aiagent_subprocess_env_scope(
    event: Any,
    profile_home: Path,
    *,
    approval_dir: Path,
    event_stream: bool = False,
    extra: Optional[dict[str, str]] = None,
) -> Iterator[dict[str, str]]:
    """Build child env while keeping per-run broker lifetime scoped to spawn."""
    from .webui_broker_server import (
        credential_broker_url,
        register_credential_broker_token,
        unregister_credential_broker_token,
    )

    sender_open_id = _resolve_subprocess_sender_open_id(event)
    merged_extra = dict(extra or {})
    if sender_open_id and "HERMES_FEISHU_USER_OPEN_ID" not in merged_extra:
        merged_extra["HERMES_FEISHU_USER_OPEN_ID"] = sender_open_id
    broker_token = ""
    if strict_context_enabled():
        scoped_open_id = str(
            merged_extra.get("HERMES_FEISHU_USER_OPEN_ID")
            or sender_open_id
            or _profile_owner_open_id(profile_home)
        ).strip()
        if scoped_open_id:
            run_id = secrets.token_urlsafe(24)
            broker_token = secrets.token_urlsafe(32)
            merged_extra.update(
                {
                    "HERMES_MULTITENANCY_CRED_BROKER_TOKEN": broker_token,
                    "HERMES_MULTITENANCY_CRED_BROKER_URL": credential_broker_url(),
                    "HERMES_MULTITENANCY_CRED_LEASE": mint_lease(
                        profile_name=profile_home.name,
                        open_id=scoped_open_id,
                        run_id=run_id,
                        secret=lease_signing_secret(),
                    ),
                    "HERMES_MULTITENANCY_RUN_ID": run_id,
                }
            )
            register_credential_broker_token(
                token=broker_token,
                profile_name=profile_home.name,
                open_id=scoped_open_id,
                run_id=run_id,
            )
    with _lark_cli_auth_broker_scope(
        profile_home,
        str(merged_extra.get("HERMES_FEISHU_USER_OPEN_ID") or sender_open_id),
    ) as lark_cli_env:
        try:
            merged_extra.update(lark_cli_env)
            yield _build_subprocess_env(
                profile_home,
                approval_dir=approval_dir,
                event_stream=event_stream,
                extra=merged_extra,
            )
        finally:
            if broker_token:
                unregister_credential_broker_token(broker_token)


def _profile_env_for_aiagent(profile_home: Path) -> dict[str, str]:
    """Load profile-local env for the AIAgent process before sandboxing.

    The bwrap policy masks ``.env`` and ``auth.json`` from tool-visible file
    paths, so credentials needed by the provider/tool clients must be injected
    into the AIAgent process environment instead. Terminal/code subprocesses
    apply their own secret-name env filter before running model-generated code.
    """
    loaded: dict[str, str] = {}
    profile_env = profile_home / ".env"
    shared_env = _resolve_shared_hermes_home(profile_home) / ".env"
    try:
        if shared_env.exists():
            loaded.update(_dotenv_values_for_aiagent(shared_env, allowed_keys=_SHARED_AIAGENT_ENV_ALLOWLIST))

        profile_allowed_keys: Optional[frozenset[str]] = None
        try:
            if (
                profile_env != shared_env
                and profile_env.exists()
                and shared_env.exists()
                and profile_env.resolve() == shared_env.resolve()
            ):
                profile_allowed_keys = _SHARED_AIAGENT_ENV_ALLOWLIST
        except OSError:
            pass
        loaded.update(_dotenv_values_for_aiagent(profile_env, allowed_keys=profile_allowed_keys))
    except Exception:
        logger.debug("[multitenancy] failed to load profile .env for subprocess", exc_info=True)

    try:
        from .provider_adapter import provider_env_for_aiagent

        for key, value in provider_env_for_aiagent(profile_home, existing_env=loaded).items():
            if key not in loaded:
                loaded[key] = value
    except Exception:
        logger.debug("[multitenancy] failed to load provider adapter env for subprocess", exc_info=True)

    try:
        # Use the MERGED+normalized config (not profile-local only): provider may be
        # inherited from shared config, and _load_profile_config self-heals a bare
        # default so _split_model_spec below can never raise on it.
        config = _load_profile_config(profile_home)
        primary = ((config.get("model") or {}).get("default") or "").strip()
        provider = _split_model_spec(primary)[0] if primary else ""
        if provider:
            env_names = _PROVIDER_ENV_KEYS.get(provider, ())
            if env_names and not any(loaded.get(name) for name in env_names):
                auth = _load_json(profile_home / "auth.json")
                pool = auth.get("credential_pool", {}).get(provider)
                if isinstance(pool, list):
                    for cred in pool:
                        if not isinstance(cred, dict):
                            continue
                        if cred.get("last_status") == "exhausted":
                            continue
                        token = cred.get("access_token")
                        if token:
                            loaded[env_names[0]] = str(token)
                            break
    except Exception:
        logger.debug("[multitenancy] failed to load profile auth env for subprocess", exc_info=True)

    return loaded


def _credential_env_for_aiagent(profile_home: Path) -> dict[str, str]:
    """Load configured profile credential env vars from the multitenancy vault.

    ``credential-materialization.yaml`` can expose a credential as a file for
    legacy tools and as an env var for skills that already expect conventional
    names such as ``GITLAB_TOKEN``. The secret enters only the routed AIAgent
    process; terminal/code subprocesses still require explicit passthrough
    registration in the child runtime.
    """
    loaded: dict[str, str] = {}
    try:
        from .credential_materializer import (
            DEFAULT_SHARED_PROFILE,
            _payload_content,
            _resolve_config_path,
            _target_profiles,
        )
        from .credentials import CredentialStore

        shared_home = _resolve_shared_hermes_home(profile_home)
        config = _resolve_config_path(shared_home, None)
        if config is None:
            return loaded
        import yaml

        raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        entries = raw.get("credentials") or []
        if not isinstance(entries, list):
            return loaded
        store = CredentialStore(shared_home / "multitenancy.db")
        try:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                env_name = str(entry.get("env") or entry.get("env_name") or "").strip()
                if not env_name:
                    continue
                if not _CREDENTIAL_ENV_NAME_RE.match(env_name):
                    logger.warning(
                        "[multitenancy] skipped invalid credential env name %r",
                        env_name,
                    )
                    continue
                if profile_home.name not in _target_profiles(entry, shared_home=shared_home):
                    continue
                try:
                    payload = store.get_secret_for_runtime(
                        profile_name=str(entry.get("vault_profile") or DEFAULT_SHARED_PROFILE),
                        subject_id=str(entry["subject_id"]),
                        provider=str(entry["provider"]),
                        secret_kind=str(entry.get("secret_kind") or "token"),
                    )
                except PermissionError:
                    continue
                loaded[env_name] = _payload_content(payload, entry.get("payload_key")).rstrip("\n")
        finally:
            store.close()
    except Exception:
        logger.debug("[multitenancy] failed to load credential env for subprocess", exc_info=True)
    return loaded


def _force_env_for_terminal_passthrough(env: dict[str, str]) -> dict[str, str]:
    """Mirror credential env through Hermes' subprocess force-prefix channel.

    Terminal/code tools apply a second secret-name scrub before spawning model
    generated commands.  The force-prefix is consumed by Hermes' local
    environment builder and emitted only as the real key name in the child
    process, so the `_HERMES_FORCE_*` plumbing variable is not visible to the
    shell command.
    """
    return {f"_HERMES_FORCE_{key}": value for key, value in env.items()}


def _install_credential_env_passthrough(profile_home: Path) -> None:
    """Allow configured credential env vars through terminal/code sandboxes."""
    env_names = sorted(_credential_env_for_aiagent(profile_home))
    if not env_names:
        return
    try:
        from tools import env_passthrough as env_passthrough_mod

        env_passthrough_mod.register_env_passthrough(env_names)
        # Hermes' passthrough registry is ContextVar-backed. Tool execution can
        # hop into worker threads, where that context is not always inherited, so
        # credential env vars also need a process-level allowlist entry.
        config_passthrough = getattr(env_passthrough_mod, "_config_passthrough", None)
        merged = set(config_passthrough or ())
        merged.update(env_names)
        setattr(env_passthrough_mod, "_config_passthrough", frozenset(merged))
        logger.info(
            "[multitenancy] registered credential env passthrough profile=%s count=%d",
            profile_home.name,
            len(env_names),
        )
    except Exception:
        logger.debug("[multitenancy] credential env passthrough skipped", exc_info=True)


def _apply_runtime_env_for_aiagent(profile_home: Path):
    """Temporarily expose profile/credential env for in-process AIAgent runs."""
    runtime_env = _profile_env_for_aiagent(profile_home)
    credential_env = _credential_env_for_aiagent(profile_home)
    runtime_env.update(credential_env)
    runtime_env.update(_force_env_for_terminal_passthrough(credential_env))
    runtime_env.update(_browser_env_for_aiagent(profile_home))
    if not runtime_env:
        return lambda: None
    old_env = {key: os.environ.get(key) for key in runtime_env}
    os.environ.update(runtime_env)

    def _cleanup() -> None:
        for key, old_value in old_env.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value

    return _cleanup


def _apply_vod_image_model_override_for_aiagent(user_text: str):
    """Expose a one-run VOD image model override parsed from natural language."""
    try:
        from .tencent_vod_image_gen import VOD_MODEL_OVERRIDE_ENV, detect_vod_model_override
    except Exception:
        return lambda: None

    model = detect_vod_model_override(user_text)
    if not model:
        return lambda: None
    old_value = os.environ.get(VOD_MODEL_OVERRIDE_ENV)
    os.environ[VOD_MODEL_OVERRIDE_ENV] = model

    def _cleanup() -> None:
        if old_value is None:
            os.environ.pop(VOD_MODEL_OVERRIDE_ENV, None)
        else:
            os.environ[VOD_MODEL_OVERRIDE_ENV] = old_value

    return _cleanup


def _register_aiagent_process_image_gen_providers() -> None:
    """Register multitenancy image providers inside routed AIAgent children."""

    class _ImageGenRegistryContext:
        def register_image_gen_provider(self, provider: Any) -> None:
            from agent.image_gen_registry import register_provider

            register_provider(provider)

    try:
        from .tencent_vod_image_gen import register_vod_image_gen_provider

        registered = register_vod_image_gen_provider(_ImageGenRegistryContext())
        if registered:
            logger.info("[multitenancy] registered AIAgent image_gen provider: tencent-vod")
    except Exception:
        logger.debug("[multitenancy] AIAgent image_gen provider registration skipped", exc_info=True)


def _install_skill_runtime_compat(profile_home: Path) -> None:
    """Install profile-scoped skill template compatibility in the AIAgent child.

    Some OpenClaw/ClawHub-style skills use ``{baseDir}`` to refer to the skill
    root. Upstream hermes-agent uses ``${HERMES_SKILL_DIR}``. Keep this bridge
    inside the multitenancy-routed runtime so shared skills and hermes-agent do
    not need local compatibility patches.
    """
    try:
        import agent.skill_preprocessing as skill_preprocessing
    except Exception:
        logger.debug("[multitenancy] skill runtime compat skipped", exc_info=True)
        return

    original = getattr(skill_preprocessing, "substitute_template_vars", None)
    if not callable(original):
        return
    if getattr(original, "_hermes_multitenancy_base_dir_compat", False):
        return

    def _substitute_with_base_dir(content, skill_dir, session_id=None):
        rendered = original(content, skill_dir, session_id)
        if not isinstance(rendered, str) or "{baseDir}" not in rendered or not skill_dir:
            return rendered
        return rendered.replace("{baseDir}", str(Path(skill_dir).expanduser()))

    _substitute_with_base_dir._hermes_multitenancy_base_dir_compat = True
    skill_preprocessing.substitute_template_vars = _substitute_with_base_dir

    skill_commands = sys.modules.get("agent.skill_commands")
    if skill_commands is not None and hasattr(skill_commands, "_substitute_template_vars"):
        skill_commands._substitute_template_vars = _substitute_with_base_dir

    logger.info(
        "[multitenancy] installed skill runtime compatibility for profile=%s",
        profile_home.name,
    )


def _dotenv_values_for_aiagent(
    path: Path,
    *,
    allowed_keys: Optional[frozenset[str]] = None,
) -> dict[str, str]:
    """Read an env file for AIAgent-only injection with explicit secret filtering."""
    values: dict[str, str] = {}
    try:
        from dotenv import dotenv_values
        for key, value in dotenv_values(path).items():
            if not key or value is None:
                continue
            key = str(key)
            if key in _FEISHU_ENV_BLOCKLIST:
                continue
            if allowed_keys is not None and key not in allowed_keys:
                continue
            values[key] = str(value)
    except Exception:
        logger.debug("[multitenancy] failed to load env file for subprocess: %s", path, exc_info=True)
    return values


# ─────────────────────────────────────────────────────────────────────────
# 档 B — sandbox-exec wrapper (kernel-level filesystem + network deny)
# ─────────────────────────────────────────────────────────────────────────

_SANDBOX_POLICY_FILE = Path(__file__).parent / "sandbox" / "profile-default.sb"
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Linux backend — bubblewrap. Policy is a sibling file written one bwrap arg
# per line, with ${KEY} placeholders substituted at runtime. Kept parallel to
# the macOS .sb file rather than compiled from a shared DSL (see
# sandbox-cross-platform-design.md §6 for the trade-off).
_BWRAP_EXEC = "/usr/bin/bwrap"
_BWRAP_ARGS_FILE = Path(__file__).parent / "sandbox" / "bwrap-default.args"
_SANDBOX_SKILL_IGNORED_DIRS = {".git", ".github", ".hub", ".archive", "__pycache__"}
_SANDBOX_SKILL_SECRET_FILE_NAMES = {".env", ".env.local", ".npmrc", ".netrc", "auth.json", "feishu_uat.json"}
_SANDBOX_SKILL_SECRET_NAME_PARTS = ("token", "secret", "credential", "password", "passwd", "apikey", "api_key")
_SANDBOX_SKILL_SOURCE_FILE_SUFFIXES = (
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


def _resolve_hermes_agent_repo() -> Path:
    """Best-effort resolution of the hermes-agent source repo path.

    Used to grant the sandboxed subprocess file-read* on the agent source
    tree. Falls back to the venv's site-packages parent if not findable.
    """
    explicit = os.getenv("HERMES_AGENT_REPO")
    if explicit:
        return Path(explicit).expanduser()
    # gateway typically runs with cwd == hermes-agent repo root (see plist
    # WorkingDirectory). os.getcwd() is the most reliable signal when
    # HERMES_AGENT_REPO is unset.
    cwd = Path(os.getcwd()).resolve()
    if (cwd / "hermes_cli").is_dir() or (cwd / "gateway").is_dir():
        return cwd
    # Fallback: walk up from sys.prefix (venv root → repo root)
    venv_parent = Path(sys.prefix).parent
    if (venv_parent / "hermes_cli").is_dir() or (venv_parent / "gateway").is_dir():
        return venv_parent
    return cwd  # Best guess; sandbox will deny if wrong, easy to spot.


def _wrap_with_sandbox(cmd: list[str], profile_home: Path) -> list[str]:
    """Wrap ``cmd`` with ``sandbox-exec`` when ``HERMES_USE_SANDBOX=1``.

    When the toggle is off (default for now), returns ``cmd`` unchanged so
    docker-style isolation can be rolled out gradually. When on, builds
    the parameterised invocation for ``hermes_multitenancy/sandbox/
    profile-default.sb``.

    Falls back to unsandboxed exec with a loud WARNING if the policy
    file is missing — better to keep the bot working than to fail closed
    in a way that masks the cause.
    """
    if os.environ.get("HERMES_USE_SANDBOX") != "1":
        return cmd

    # Per-profile gate. If HERMES_SANDBOX_PROFILES is set, the sandbox only
    # wraps subprocesses for profiles named in that comma-separated list.
    # Unset → all profiles are sandboxed (when the master toggle is on).
    # This lets operators dial sandboxing up gradually:
    #
    #   HERMES_USE_SANDBOX=1 HERMES_SANDBOX_PROFILES=spike_test
    #     → only spike_test routes get sandbox-exec; production profiles
    #       (feishu_g41a5b5g etc.) continue unsandboxed during pilot.
    #
    #   HERMES_USE_SANDBOX=1
    #     → every routed profile is sandboxed (final state after pilot).
    allowlist_raw = os.environ.get("HERMES_SANDBOX_PROFILES", "").strip()
    if allowlist_raw:
        allowed = {p.strip() for p in allowlist_raw.split(",") if p.strip()}
        if profile_home.name not in allowed:
            logger.debug(
                "[multitenancy] sandbox gated: profile=%s not in HERMES_SANDBOX_PROFILES=%s",
                profile_home.name, allowlist_raw,
            )
            return cmd

    # Platform dispatch. Each backend owns its own preflight checks and
    # failure semantics (macOS keeps a pilot-era WARNING+fallback; Linux is
    # post-pilot and raises on missing bwrap so systemd surfaces it).
    if sys.platform == "darwin":
        return _wrap_macos_sandbox(cmd, profile_home)
    if sys.platform.startswith("linux"):
        return _wrap_linux_bwrap(cmd, profile_home)
    logger.info(
        "[multitenancy] HERMES_USE_SANDBOX=1 but platform %s has no sandbox "
        "backend — spawning subprocess unsandboxed.",
        sys.platform,
    )
    return cmd


def _wrap_macos_sandbox(cmd: list[str], profile_home: Path) -> list[str]:
    """macOS backend: wrap with /usr/bin/sandbox-exec + profile-default.sb.

    Behaviour identical to the pre-2026-05-11 monolithic implementation —
    same preflight checks, same WARNING+fallback when policy/binary missing,
    same -D parameter set, same wrapped argv order. Existing tests in
    tests/test_aiagent_subprocess.py:1099-1222 assert this exact shape.
    """
    if not _SANDBOX_POLICY_FILE.is_file():
        logger.warning(
            "[multitenancy] HERMES_USE_SANDBOX=1 but policy %s is missing — "
            "spawning subprocess unsandboxed. Investigate the deployment.",
            _SANDBOX_POLICY_FILE,
        )
        return cmd
    if not os.access(_SANDBOX_EXEC, os.X_OK):
        logger.warning(
            "[multitenancy] HERMES_USE_SANDBOX=1 but %s is not executable on "
            "this platform — spawning subprocess unsandboxed.",
            _SANDBOX_EXEC,
        )
        return cmd

    venv = Path(sys.prefix).resolve()
    # venv is typically installed as <agent_install>/venv/, with the
    # hermes-agent source tree (run_agent.py, gateway/, tools/) living
    # one directory up. Editable installs (`pip install -e .`) cause
    # `import run_agent` to resolve to that source path, so the sandbox
    # must allow reads on the install root, not just venv/.
    agent_install = venv.parent
    shared_home = _resolve_shared_hermes_home(profile_home).resolve()
    agent_repo = _resolve_hermes_agent_repo().resolve()
    mt_repo = Path(__file__).resolve().parent.parent
    user_home = Path.home().resolve()
    profile_home_resolved = profile_home.expanduser().resolve()

    wrapped = [
        _SANDBOX_EXEC,
        "-f", str(_SANDBOX_POLICY_FILE),
        "-D", f"PROFILE_HOME={profile_home_resolved}",
        "-D", f"SHARED_HOME={shared_home}",
        "-D", f"USER_HOME={user_home}",
        "-D", f"HERMES_VENV={venv}",
        "-D", f"HERMES_AGENT_INSTALL={agent_install}",
        "-D", f"HERMES_AGENT_REPO={agent_repo}",
        "-D", f"HERMES_MT_REPO={mt_repo}",
    ] + list(cmd)
    logger.info(
        "[multitenancy] sandbox-exec wrap: policy=%s profile=%s agent_repo=%s",
        _SANDBOX_POLICY_FILE.name, profile_home_resolved.name, agent_repo,
    )
    return wrapped


def _render_bwrap_args(text: str, substitutions: dict[str, str]) -> list[str]:
    """Render bwrap-default.args text into a list of CLI tokens.

    Strips '#'-led comments and blank lines, then substitutes ${KEY} with
    values from ``substitutions``. Each remaining line is split on
    whitespace so a single line can carry multiple tokens
    (e.g. ``--ro-bind /usr /usr``).
    """
    tokens: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        for key, value in substitutions.items():
            line = line.replace(f"${{{key}}}", value)
        tokens.extend(line.split())
    return tokens


def _shared_skill_symlink_bwrap_args(profile_home: Path, shared_home: Path) -> list[str]:
    """Expose only installed shared-skill symlink targets inside bwrap.

    Profile skills are often stable symlinks to shared managed skill releases.
    The base bwrap policy deliberately does not mount ``SHARED_HOME`` wholesale,
    so those symlinks otherwise resolve to missing targets inside the sandbox.
    We bind only symlink targets that the current profile already references,
    and only when the target remains under approved shared skill roots.
    """
    skills_root = profile_home.expanduser().resolve(strict=False) / "skills"
    if not skills_root.is_dir():
        return []
    shared = shared_home.expanduser().resolve(strict=False)
    allowed_roots = [
        _absolute_path_without_following_final_symlink(shared / "skills"),
        _absolute_path_without_following_final_symlink(shared / "skill-releases"),
    ]
    bindings: set[tuple[Path, Path]] = set()

    for root, dirs, files in os.walk(skills_root, followlinks=False):
        root_path = Path(root)
        entries = list(dirs) + list(files)
        for name in entries:
            item = root_path / name
            if not item.is_symlink():
                continue
            try:
                resolved = item.resolve(strict=True)
            except OSError:
                continue
            mount_path = _shared_skill_symlink_mount_path(item, allowed_roots)
            if mount_path is None:
                continue
            if resolved.is_dir() and not (resolved / "SKILL.md").is_file():
                continue
            if resolved.is_dir() and _sandbox_skill_tree_has_secret_files(resolved):
                logger.warning(
                    "[multitenancy] skipping shared skill sandbox bind with secret-like files: %s",
                    resolved,
                )
                continue
            if resolved.is_file() and _is_sandbox_secret_skill_file(resolved.name):
                logger.warning(
                    "[multitenancy] skipping shared skill sandbox bind for secret-like file: %s",
                    resolved,
                )
                continue
            bindings.add((resolved, mount_path))
        dirs[:] = [
            name
            for name in dirs
            if not (root_path / name).is_symlink()
            and name not in _SANDBOX_SKILL_IGNORED_DIRS
        ]

    args: list[str] = []
    created_dirs: set[Path] = set()
    for source, mount_path in sorted(bindings, key=lambda pair: (str(pair[1]), str(pair[0]))):
        for parent in _shared_skill_target_parent_dirs(mount_path, shared):
            if parent in created_dirs:
                continue
            args.extend(["--dir", str(parent)])
            created_dirs.add(parent)
        args.extend(["--ro-bind", str(source), str(mount_path)])
    return args


def _shared_skill_symlink_mount_path(item: Path, allowed_roots: list[Path]) -> Path | None:
    try:
        raw_target = Path(os.readlink(item))
    except OSError:
        return None
    if raw_target.is_absolute():
        target = raw_target
    else:
        target = item.parent / raw_target
    target = _absolute_path_without_following_final_symlink(target)
    if _is_safe_shared_skill_symlink_target(target, allowed_roots):
        return target
    return None


def _absolute_path_without_following_final_symlink(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_safe_shared_skill_symlink_target(target: Path, allowed_roots: list[Path]) -> bool:
    try:
        resolved = _absolute_path_without_following_final_symlink(target)
    except OSError:
        return False
    for root in allowed_roots:
        if resolved == root or root in resolved.parents:
            return True
    return False


def _shared_skill_target_parent_dirs(target: Path, shared_home: Path) -> list[Path]:
    dirs: list[Path] = []
    parent = target.parent if target.is_file() else target.parent
    try:
        rel = parent.relative_to(shared_home)
    except ValueError:
        return dirs
    current = shared_home
    for part in rel.parts:
        current = current / part
        dirs.append(current)
    return dirs


def _sandbox_skill_tree_has_secret_files(src: Path) -> bool:
    for root, dirs, files in os.walk(src, followlinks=False):
        root_path = Path(root)
        dirs[:] = [
            dirname
            for dirname in dirs
            if not (root_path / dirname).is_symlink()
            and dirname not in _SANDBOX_SKILL_IGNORED_DIRS
        ]
        for filename in files:
            item = root_path / filename
            if item.is_symlink():
                continue
            rel = item.relative_to(src)
            if _is_sandbox_secret_skill_file(rel):
                return True
    return False


def _is_sandbox_secret_skill_file(rel_path: str | Path) -> bool:
    path = Path(rel_path)
    name = path.name.lower()
    if name in _SANDBOX_SKILL_SECRET_FILE_NAMES:
        return True
    if name.endswith((".token", ".secret", ".key")):
        return True
    if name.endswith(_SANDBOX_SKILL_SOURCE_FILE_SUFFIXES):
        return False
    return any(part in name for part in _SANDBOX_SKILL_SECRET_NAME_PARTS)


def _wrap_linux_bwrap(cmd: list[str], profile_home: Path) -> list[str]:
    """Linux backend: wrap with /usr/bin/bwrap + bwrap-default.args.

    Fail-closed: missing policy or binary raises RuntimeError. Rationale:
    Linux rollout is post-pilot, operator intent is "must be sandboxed".
    Silently falling back to unsandboxed would mask the misconfiguration.
    systemd Restart=on-failure will surface the crash for ops.
    """
    if not _BWRAP_ARGS_FILE.is_file():
        msg = (
            f"[multitenancy] HERMES_USE_SANDBOX=1 but bwrap policy "
            f"{_BWRAP_ARGS_FILE} is missing — refusing to spawn unsandboxed."
        )
        logger.error(msg)
        raise RuntimeError(msg)
    if not os.access(_BWRAP_EXEC, os.X_OK):
        msg = (
            f"[multitenancy] HERMES_USE_SANDBOX=1 but bwrap is not "
            f"executable at {_BWRAP_EXEC} — install via "
            f"`dnf install -y bubblewrap` (or apt equivalent)."
        )
        logger.error(msg)
        raise RuntimeError(msg)

    venv = Path(sys.prefix).resolve()
    agent_install = venv.parent
    shared_home = _resolve_shared_hermes_home(profile_home).resolve()
    agent_repo = _resolve_hermes_agent_repo().resolve()
    mt_repo = Path(__file__).resolve().parent.parent
    user_home = Path.home().resolve()
    profile_home_resolved = profile_home.expanduser().resolve()

    substitutions = {
        "PROFILE_HOME": str(profile_home_resolved),
        "SHARED_HOME": str(shared_home),
        "USER_HOME": str(user_home),
        "HERMES_VENV": str(venv),
        "HERMES_AGENT_INSTALL": str(agent_install),
        "HERMES_AGENT_REPO": str(agent_repo),
        "HERMES_MT_REPO": str(mt_repo),
    }
    bwrap_args = _render_bwrap_args(_BWRAP_ARGS_FILE.read_text(), substitutions)
    bwrap_args.extend(_shared_skill_symlink_bwrap_args(profile_home_resolved, shared_home))
    wrapped = [_BWRAP_EXEC, *bwrap_args, "--", *cmd]
    logger.info(
        "[multitenancy] bwrap wrap: policy=%s profile=%s agent_repo=%s",
        _BWRAP_ARGS_FILE.name, profile_home_resolved.name, agent_repo,
    )
    return wrapped


def _aiagent_subprocess_cwd(profile_home: Path) -> str:
    """Start child processes from the routed workspace so sandbox getcwd is allowed."""
    workspace = profile_home.expanduser() / "workspace"
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    return str(workspace)


def _write_token_ledger_from_child(event: Any, profile_home: Path, usage: Any) -> None:
    """工件1a：父进程侧写 token 台账（子进程沙箱不能写 /var/log/hermes）。

    ``usage`` 是子进程透传上来的 {model, input/output/total_tokens}；触发人身份与平台
    维度在父进程从 ``event`` 解析（与 conversation_audit 同源）。整段 best-effort，
    任何失败只 debug、绝不影响回复。开关关闭时 append_token_usage 内部直接 no-op。
    """
    if not isinstance(usage, dict):
        return
    try:
        from .token_usage_ledger import append_token_usage, token_usage_ledger_enabled

        if not token_usage_ledger_enabled():
            return
        source = getattr(event, "source", None)
        # Use the router's canonical chat_type/chat_id extraction (same fallback
        # chain feishu.py/routing use), so the recorded chat_id matches the key
        # routing.lookup_by_chat_id expects — covers 'topic' groups and the
        # parent_chat_id / chat_id_alt / event.message.chat_id variants that a
        # bare source.chat_id read would miss (would mis-bill group turns).
        from .router import _extract_chat_id, _extract_chat_type

        append_token_usage(
            sender_open_id=_resolve_subprocess_sender_open_id(event),
            profile=profile_home.name,
            platform=_resolve_platform_value(source),
            chat_type=_extract_chat_type(event),
            chat_id=_extract_chat_id(event),
            model=usage.get("model"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
        )
    except Exception:
        logger.debug("[multitenancy] token usage ledger (parent) skipped", exc_info=True)


async def _run_aiagent_subprocess(
    event: Any,
    profile_home: Path,
    *,
    messages: Optional[list[dict]] = None,
) -> str:
    """Run the sync AIAgent body in a fresh Python process.

    The gateway stays fully async while the child process owns the synchronous
    AIAgent/tool loop. This avoids the gateway event-loop deadlock observed
    when ``_run_with_aiagent`` runs through ``asyncio.to_thread``.
    """
    import asyncio

    payload = json.dumps(
        _event_to_subprocess_payload(event, profile_home, messages=messages),
        ensure_ascii=False,
    ).encode("utf-8")
    timeout_s = float(os.getenv("HERMES_AIAGENT_SUBPROCESS_TIMEOUT", "3600"))
    approval_dir = Path(tempfile.mkdtemp(prefix="hermes-mt-approval-"))
    env_scope = _aiagent_subprocess_env_scope(event, profile_home, approval_dir=approval_dir)
    env_scope_entered = False
    env = env_scope.__enter__()
    env_scope_entered = True
    # Resolve symlinks so sandbox-exec's path-based allow rules match.
    # The plugin is typically loaded via a profile-local symlink
    # (~/.hermes/profiles/<p>/plugins/multitenancy → ~/code/hermes-multitenancy/),
    # but the sandbox policy only whitelists the resolved repo path.
    # Without .resolve() the child python sees an [Errno 1] Operation not
    # permitted when trying to open aiagent_subprocess.py through the symlink.
    child_script = Path(__file__).with_name("aiagent_subprocess.py").resolve()
    cmd = _wrap_with_sandbox([sys.executable, str(child_script)], profile_home)

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=_aiagent_subprocess_cwd(profile_home),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout_s)
    except asyncio.TimeoutError as exc:
        if proc is not None:
            proc.kill()
            await proc.wait()
        raise RuntimeError(f"AIAgent subprocess timed out after {timeout_s:g}s") from exc
    except asyncio.CancelledError:
        if proc is not None:
            proc.kill()
            await proc.wait()
        raise
    finally:
        if env_scope_entered:
            env_scope.__exit__(*sys.exc_info())
        try:
            import shutil

            shutil.rmtree(approval_dir, ignore_errors=True)
        except Exception:
            pass

    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    if stderr_text:
        logger.debug("[multitenancy] AIAgent subprocess stderr: %s", stderr_text[-4000:])

    try:
        data = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "AIAgent subprocess returned invalid JSON "
            f"(exit={proc.returncode}, stdout={stdout_text[-1000:]!r}, stderr={stderr_text[-1000:]!r})"
        ) from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"AIAgent subprocess exited {proc.returncode}: "
            f"{data.get('error') or stderr_text or stdout_text}"
        )
    if data.get("error"):
        raise RuntimeError(f"AIAgent subprocess failed: {data['error']}")
    _write_token_ledger_from_child(event, profile_home, data.get("usage"))
    return str(data.get("result") or "")


async def _stream_aiagent_subprocess(
    event: Any,
    profile_home: Path,
    *,
    messages: Optional[list[dict]] = None,
):
    """Run AIAgent in a child process and yield its NDJSON progress events.

    State.db mirroring (user/assistant/tool rows + source retag) is done here
    in the parent process — the subprocess sandbox blocks sqlite WAL opens on
    PROFILE_HOME/state.db, so any write logic running inside the sandbox fails
    silently with ``sqlite3.OperationalError: unable to open database file``.
    The parent has no sandbox and can write freely.
    """
    import asyncio
    import sqlite3 as _sqlite3
    import time as _time

    # Resolve identifiers the mirror needs. All derivable from ``event`` and
    # ``profile_home`` so we don't have to push them across the NDJSON pipe.
    # current_sender_open_id contextvar is set by the feishu adapter on the
    # gateway loop; lazy-import it so this module still imports if
    # tools.feishu_oapi_client is unavailable in tests / non-feishu
    # deployments.
    sender_open_id = ""
    try:
        from tools.feishu_oapi_client import current_sender_open_id as _cv
        sender_open_id = _cv.get() or ""
    except Exception:
        pass
    if not sender_open_id:
        sender_open_id = _resolve_subprocess_sender_open_id(event)
    _canonical_session_id = _resolve_aiagent_session_id(event, profile_home, sender_open_id)
    user_text = getattr(event, "text", "") or ""
    _state_db_path = profile_home / "state.db"
    _source_for_display = getattr(event, "source", None)
    _preserve_reasoning_in_state = _resolve_platform_value(_source_for_display) != "webui"
    try:
        from .conversation_audit import (
            append_conversation_audit_event as _append_conversation_audit_event,
            build_conversation_audit_context as _build_conversation_audit_context,
        )
        _audit_context = _build_conversation_audit_context(event, profile_home)
    except Exception:
        logger.exception("[multitenancy] conversation audit context init failed")
        _append_conversation_audit_event = None
        _audit_context = {
            "profile_name": Path(profile_home).name,
            "platform": _resolve_platform_value(_source_for_display),
            "chat_type": "",
        }

    # ── Session-boundary epoch ────────────────────────────────────────────
    # The canonical session_id is keyed only by (chat_id, user_id), so it
    # stays the same forever — including across ``/new`` resets. That
    # collapses every turn from the same DM into a single web-ui sidebar
    # entry, which is wrong UX after the user explicitly asked for a fresh
    # session.
    #
    # The router (``router.py:_clear_history``) wipes its in-process history
    # dict on ``/new``, so the next turn arrives with ``messages`` either
    # None or containing only the current user message. We use that signal
    # as the session boundary: on a fresh-start turn we rotate the epoch
    # (written to a per-(chat,user) text file in ``profile_home``), on a
    # continuation turn we reuse it. Appending ``:epoch:<ts>`` to the
    # canonical id yields a new session row in state.db after each ``/new``
    # while preserving session continuity within a chat-history run.
    _is_session_start = messages is None or len(messages) <= 1
    _chat_id_for_epoch = ""
    _source_for_epoch = getattr(event, "source", None)
    if _source_for_epoch is not None:
        _chat_id_for_epoch = str(
            getattr(_source_for_epoch, "chat_id", "")
            or getattr(_source_for_epoch, "parent_chat_id", "")
            or getattr(_source_for_epoch, "chat_id_alt", "")
            or ""
        )

    def _epoch_path() -> Optional[Path]:
        if not _chat_id_for_epoch:
            return None
        def _safe(s: str) -> str:
            return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in s)[:80]
        return (
            profile_home
            / "mirror_epochs"
            / f"{_safe(_chat_id_for_epoch)}__{_safe(sender_open_id or 'unknown')}.txt"
        )

    def _resolve_epoch() -> str:
        ep = _epoch_path()
        if ep is None:
            return str(int(_time.time()))
        try:
            if _is_session_start or not ep.exists():
                try:
                    ep.parent.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass
                value = str(int(_time.time()))
                try:
                    ep.write_text(value, encoding="utf-8")
                except Exception:
                    pass
                return value
            current = ep.read_text(encoding="utf-8").strip()
            return current or str(int(_time.time()))
        except Exception:
            return str(int(_time.time()))

    session_id = f"{_canonical_session_id}:epoch:{_resolve_epoch()}"

    class _StateDbMirror:
        """Parent-side write-through to ``profile_home/state.db`` for web-ui visibility.

        Holds the in-flight assistant row id so streaming deltas update the
        same row instead of creating a new one per chunk. Tool calls seal the
        active assistant row so the next assistant text starts a new bubble.
        """

        def __init__(self) -> None:
            self.active_assistant_id: Optional[int] = None
            self.active_assistant_timestamp: Optional[float] = None
            self.assistant_content: str = ""
            self.assistant_reasoning: str = ""
            self.session_ensured: bool = False
            self.user_inserted: bool = False
            self.retagged: bool = False

        def _audit(
            self,
            *,
            message_id: int | str | None,
            role: str,
            content: str | None,
            timestamp: float,
            tool_name: str | None = None,
            tool_calls: str | None = None,
            finish_reason: str | None = None,
        ) -> None:
            if _append_conversation_audit_event is None:
                return
            _append_conversation_audit_event(
                profile_name=str(_audit_context.get("profile_name") or ""),
                platform=str(_audit_context.get("platform") or ""),
                chat_type=str(_audit_context.get("chat_type") or ""),
                session_id=str(session_id),
                message_id=message_id,
                role=role,
                content=content,
                timestamp=timestamp,
                tool_name=tool_name,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
            )

        def _conn(self):
            return _sqlite3.connect(str(_state_db_path), timeout=2.0)

        def ensure_session(self) -> None:
            if self.session_ensured:
                return
            try:
                from hermes_state import SessionDB
                SessionDB(_state_db_path).close()
            except Exception:
                logger.exception("[multitenancy] mirror schema init failed")
            try:
                with closing(self._conn()) as conn, conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO sessions (id, source, started_at) "
                        "VALUES (?, 'feishu', ?)",
                        (str(session_id), _time.time()),
                    )
                self.session_ensured = True
            except Exception:
                logger.exception("[multitenancy] mirror ensure_session failed")

        def insert_user(self, text: str) -> None:
            if self.user_inserted:
                return
            self.ensure_session()
            try:
                with closing(self._conn()) as conn, conn:
                    ts = _time.time()
                    cur = conn.execute(
                        "INSERT INTO messages (session_id, role, content, timestamp) "
                        "VALUES (?, 'user', ?, ?)",
                        (str(session_id), text or "", ts),
                    )
                    message_id = cur.lastrowid
                self.user_inserted = True
                self._audit(
                    message_id=message_id,
                    role="user",
                    content=text or "",
                    timestamp=ts,
                )
            except Exception:
                logger.exception("[multitenancy] mirror insert_user failed")

        def upsert_assistant(self, text_delta: str, reasoning_delta: str) -> None:
            if text_delta:
                self.assistant_content += text_delta
            if reasoning_delta:
                self.assistant_reasoning += reasoning_delta
            if not self.assistant_content and not self.assistant_reasoning:
                return
            self.ensure_session()
            try:
                with closing(self._conn()) as conn, conn:
                    if self.active_assistant_id is None:
                        ts = _time.time()
                        cur = conn.execute(
                            "INSERT INTO messages (session_id, role, content, reasoning, timestamp) "
                            "VALUES (?, 'assistant', ?, ?, ?)",
                            (
                                str(session_id),
                                self.assistant_content,
                                _reasoning_for_state_db(
                                    self.assistant_content,
                                    self.assistant_reasoning,
                                    preserve_reasoning=_preserve_reasoning_in_state,
                                ),
                                ts,
                            ),
                        )
                        self.active_assistant_id = cur.lastrowid
                        self.active_assistant_timestamp = ts
                    else:
                        conn.execute(
                            "UPDATE messages SET content=?, reasoning=? WHERE id=?",
                            (
                                self.assistant_content,
                                _reasoning_for_state_db(
                                    self.assistant_content,
                                    self.assistant_reasoning,
                                    preserve_reasoning=_preserve_reasoning_in_state,
                                ),
                                self.active_assistant_id,
                            ),
                        )
            except Exception:
                logger.exception("[multitenancy] mirror upsert_assistant failed")

        def seal_assistant(self, finish_reason: str | None = None) -> None:
            if self.active_assistant_id is not None:
                self._audit(
                    message_id=self.active_assistant_id,
                    role="assistant",
                    content=self.assistant_content,
                    timestamp=self.active_assistant_timestamp or _time.time(),
                    finish_reason=finish_reason,
                )
            self.active_assistant_id = None
            self.active_assistant_timestamp = None
            self.assistant_content = ""
            self.assistant_reasoning = ""

        def insert_tool_call(self, tool_name: str, preview: Any, args: Any) -> None:
            if not tool_name:
                return
            self.ensure_session()
            try:
                payload = json.dumps(
                    {"name": str(tool_name), "args": args, "preview": preview},
                    ensure_ascii=False,
                    default=str,
                )
            except Exception:
                payload = None
            try:
                with closing(self._conn()) as conn, conn:
                    ts = _time.time()
                    cur = conn.execute(
                        "INSERT INTO messages (session_id, role, content, tool_name, tool_calls, timestamp) "
                        "VALUES (?, 'assistant', '', ?, ?, ?)",
                        (str(session_id), str(tool_name), payload, ts),
                    )
                    message_id = cur.lastrowid
                self._audit(
                    message_id=message_id,
                    role="assistant",
                    content="",
                    timestamp=ts,
                    tool_name=str(tool_name),
                    tool_calls=payload,
                )
            except Exception:
                logger.exception("[multitenancy] mirror insert_tool_call failed")

        def retag_source(self) -> None:
            if self.retagged:
                return
            try:
                _mark_session_source_feishu(profile_home, str(session_id))
                self.retagged = True
            except Exception:
                logger.exception("[multitenancy] mirror retag_source failed")

        def dedupe(self) -> None:
            try:
                with closing(self._conn()) as conn, conn:
                    conn.execute(
                        "DELETE FROM messages WHERE session_id = ? AND id NOT IN ("
                        "SELECT MIN(id) FROM messages WHERE session_id = ? "
                        "GROUP BY role, IFNULL(content,''), IFNULL(tool_name,''))",
                        (str(session_id), str(session_id)),
                    )
            except Exception:
                logger.exception("[multitenancy] mirror dedupe failed")

    _mirror = _StateDbMirror()
    # Pre-write the user message so the web-ui shows the question instantly,
    # even if the run later times out / aborts before any reply.
    _mirror.insert_user(user_text)

    payload = json.dumps(
        _event_to_subprocess_payload(event, profile_home, messages=messages),
        ensure_ascii=False,
    ).encode("utf-8")
    timeout_s = float(os.getenv("HERMES_AIAGENT_SUBPROCESS_TIMEOUT", "3600"))
    approval_dir = Path(tempfile.mkdtemp(prefix="hermes-mt-approval-"))
    env_scope = _aiagent_subprocess_env_scope(
        event,
        profile_home,
        approval_dir=approval_dir,
        event_stream=True,
    )
    env_scope_entered = False
    env = env_scope.__enter__()
    env_scope_entered = True
    # Resolve symlinks so sandbox-exec's path-based allow rules match.
    # The plugin is typically loaded via a profile-local symlink
    # (~/.hermes/profiles/<p>/plugins/multitenancy → ~/code/hermes-multitenancy/),
    # but the sandbox policy only whitelists the resolved repo path.
    # Without .resolve() the child python sees an [Errno 1] Operation not
    # permitted when trying to open aiagent_subprocess.py through the symlink.
    child_script = Path(__file__).with_name("aiagent_subprocess.py").resolve()
    cmd = _wrap_with_sandbox([sys.executable, str(child_script)], profile_home)

    started_at = time.monotonic()
    proc = None
    stderr_task = None
    saw_done = False
    first_event_logged = False
    try:
        logger.info(
            "[multitenancy] AIAgent subprocess spawning profile_home=%s timeout=%.1fs",
            profile_home,
            timeout_s,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=_aiagent_subprocess_cwd(profile_home),
        )
        logger.info(
            "[multitenancy] AIAgent subprocess spawned pid=%s elapsed=%.3fs",
            proc.pid,
            time.monotonic() - started_at,
        )
        stderr_task = asyncio.create_task(proc.stderr.read())
        assert proc.stdin is not None
        proc.stdin.write(payload)
        await proc.stdin.drain()
        proc.stdin.close()
        try:
            await proc.stdin.wait_closed()
        except Exception:
            pass

        assert proc.stdout is not None
        first_heartbeat_s = float(os.getenv("HERMES_AIAGENT_FIRST_EVENT_HEARTBEAT_SECONDS", "1"))
        heartbeat_s = float(os.getenv("HERMES_AIAGENT_WAIT_HEARTBEAT_SECONDS", "15"))
        heartbeat_count = 0
        while True:
            read_started = time.monotonic()
            read_task = asyncio.create_task(proc.stdout.readline())
            try:
                while not read_task.done():
                    elapsed = time.monotonic() - read_started
                    remaining = timeout_s - elapsed
                    if remaining <= 0:
                        read_task.cancel()
                        try:
                            await read_task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass
                        raise asyncio.TimeoutError()
                    next_heartbeat_s = first_heartbeat_s if heartbeat_count == 0 and not first_event_logged else heartbeat_s
                    wait_seconds = min(next_heartbeat_s, remaining) if next_heartbeat_s > 0 else remaining
                    done, _pending = await asyncio.wait({read_task}, timeout=wait_seconds)
                    if done:
                        break
                    heartbeat_count += 1
                    total_elapsed = time.monotonic() - started_at
                    logger.info(
                        "[multitenancy] waiting for AIAgent subprocess stream event elapsed=%.3fs heartbeat=%s",
                        total_elapsed,
                        heartbeat_count,
                    )
                    phase = "等待当前工具或子任务输出" if first_event_logged else "准备响应"
                    yield (
                        "status",
                        _animated_stream_status(phase, heartbeat_count),
                    )
                line = read_task.result()
            finally:
                if not read_task.done():
                    read_task.cancel()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.debug("[multitenancy] ignoring non-json child stream line: %r", text[-500:])
                continue
            event_name = data.get("event")
            if not first_event_logged:
                first_event_logged = True
                logger.info(
                    "[multitenancy] AIAgent subprocess first event kind=%s elapsed=%.3fs",
                    event_name,
                    time.monotonic() - started_at,
                )
            if event_name == "done":
                saw_done = True
                if data.get("error"):
                    raise RuntimeError(f"AIAgent subprocess failed: {data['error']}")
                logger.info(
                    "[multitenancy] AIAgent subprocess done elapsed=%.3fs result_len=%s",
                    time.monotonic() - started_at,
                    len(str(data.get("result") or "")),
                )
                # Seal any trailing assistant chunk, retag source to feishu,
                # and dedupe against whatever Hermes core's own end-of-run
                # write inserted.
                _mirror.seal_assistant(finish_reason="stop")
                _mirror.retag_source()
                _mirror.dedupe()
                _write_token_ledger_from_child(event, profile_home, data.get("usage"))
                yield "done", str(data.get("result") or "")
                continue
            if event_name == "content":
                _mirror.upsert_assistant(str(data.get("text") or ""), "")
                yield "content", str(data.get("text") or "")
            elif event_name == "thinking":
                _mirror.upsert_assistant("", str(data.get("text") or ""))
                yield "thinking", str(data.get("text") or "")
            elif event_name in {
                "tool_started",
                "tool_completed",
                "approval_required",
                "approval_resolved",
                "clarify_required",
                "clarify_resolved",
            }:
                payload_data = {k: v for k, v in data.items() if k != "event"}
                if event_name == "tool_started":
                    # Seal any pre-tool assistant text into its own row, then
                    # mirror the tool invocation. First tool-start is also
                    # the safest moment to retag — Hermes core has had time
                    # to insert the sessions row by now.
                    _mirror.seal_assistant(finish_reason="tool_calls")
                    _mirror.insert_tool_call(
                        str(payload_data.get("name") or ""),
                        payload_data.get("preview"),
                        payload_data.get("args"),
                    )
                    _mirror.retag_source()
                elif event_name == "tool_completed":
                    # Tool finished — subsequent assistant text is a new
                    # bubble. Seal so upsert starts a fresh row.
                    _mirror.seal_assistant()
                yield str(event_name), payload_data
            else:
                logger.debug("[multitenancy] ignoring unknown child stream event: %s", event_name)

        returncode = await asyncio.wait_for(proc.wait(), timeout=5)
        stderr_text = (await stderr_task).decode("utf-8", errors="replace").strip()
        if stderr_text:
            logger.debug("[multitenancy] AIAgent subprocess stderr: %s", stderr_text[-4000:])
        logger.info(
            "[multitenancy] AIAgent subprocess exited returncode=%s elapsed=%.3fs",
            returncode,
            time.monotonic() - started_at,
        )
        if returncode != 0:
            raise RuntimeError(f"AIAgent subprocess exited {returncode}: {stderr_text[-1000:]}")
        if not saw_done:
            raise RuntimeError("AIAgent subprocess stream ended without done event")
    except asyncio.TimeoutError as exc:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise RuntimeError(
            f"AIAgent subprocess produced no stream events for {timeout_s:g}s"
        ) from exc
    except asyncio.CancelledError:
        if proc is not None:
            proc.kill()
            await proc.wait()
        raise
    except Exception:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    finally:
        if env_scope_entered:
            env_scope.__exit__(*sys.exc_info())
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
        try:
            import shutil

            shutil.rmtree(approval_dir, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Phase 4 — real AIAgent runner with tool-loop (replaces the spike one-shot)
# ---------------------------------------------------------------------------


def _resolve_sender_open_id(event: Any) -> str:
    """Pick the real Feishu open_id (ou_*) from the event source for UAT lookup.

    Prefer source.user_id (typical Feishu open_id); fall back to user_id_alt
    when the former is a union_id (on_*). Returns "" if nothing usable is found.
    """
    source = getattr(event, "source", None)
    event_sender = getattr(event, "sender_open_id", None)
    if event_sender and str(event_sender).startswith("ou_"):
        return str(event_sender)
    if source is None:
        return ""
    for candidate in (
        getattr(source, "open_id", None),
        getattr(source, "user_id", None),
        getattr(source, "user_id_alt", None),
    ):
        if candidate and str(candidate).startswith("ou_"):
            return str(candidate)
    # Fallback: any non-empty user_id, even if not ou_-prefixed
    return str(getattr(source, "user_id", "") or "")


def _session_part(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip() or default
    safe = "".join(ch if (ch.isalnum() or ch in "._:-") else "_" for ch in text)
    return safe[:160] or default


def _resolve_platform_value(source: Any, default: str = "feishu") -> str:
    if source is None:
        return default
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", None) or platform or default)


def _resolve_aiagent_session_id(
    event: Any,
    profile_home: Path,
    sender_open_id: str = "",
) -> str:
    """Build a stable, per-profile/per-user AIAgent session id.

    Feishu ``message_id`` changes every turn, so it must only be a last-ditch
    fallback. Keeping profile and sender in the key prevents cross-tenant or
    cross-user history bleed when multiple Feishu users hit the same bot.
    """
    source = getattr(event, "source", None)
    raw_event = getattr(event, "raw_event", None)
    if isinstance(raw_event, dict):
        channel = str(raw_event.get("channel") or "").strip()
        webui_session_id = str(raw_event.get("session_id") or "").strip()
        if channel == "webui" and webui_session_id:
            parts = [
                "agent",
                "profile",
                profile_home.name,
                "platform",
                "webui",
                "session",
                webui_session_id,
            ]
            return ":".join(_session_part(part) for part in parts)

    platform = _resolve_platform_value(source)
    chat_type = getattr(source, "chat_type", "") if source else ""
    chat_id = (
        getattr(source, "chat_id", None)
        or getattr(source, "parent_chat_id", None)
        or getattr(source, "chat_id_alt", None)
        if source
        else ""
    )
    thread_id = (
        getattr(source, "thread_id", None)
        or getattr(source, "chat_topic", None)
        if source
        else ""
    )
    user_id = (
        sender_open_id
        or (getattr(source, "user_id", None) if source else "")
        or (getattr(source, "user_id_alt", None) if source else "")
    )
    message_id = (
        getattr(event, "message_id", None)
        or (getattr(source, "message_id", None) if source else "")
    )

    parts = [
        "agent",
        "profile",
        profile_home.name,
        "platform",
        platform,
        "chat_type",
        chat_type or "unknown",
    ]
    if chat_id:
        parts.extend(["chat", chat_id])
    if thread_id:
        parts.extend(["thread", thread_id])
    if user_id:
        parts.extend(["user", user_id])
    elif message_id:
        parts.extend(["message", message_id])
    else:
        parts.append("fallback")

    session_id = ":".join(_session_part(part) for part in parts)
    if len(session_id) <= 220:
        return session_id
    digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:12]
    return f"{session_id[:200]}:{digest}"


def _resolve_multitenant_gateway_session_key(
    event: Any,
    profile_home: Path,
    sender_open_id: str = "",
) -> str:
    """Return the parent gateway session key used by multitenancy slash commands."""
    source = getattr(event, "source", None)
    platform = _resolve_platform_value(source)
    chat_id = ""
    if source is not None:
        chat_id = str(
            getattr(source, "chat_id", None)
            or getattr(source, "parent_chat_id", None)
            or getattr(source, "chat_id_alt", None)
            or ""
        )
    user_key = str(
        sender_open_id
        or (getattr(source, "user_id", None) if source is not None else "")
        or (getattr(source, "user_id_alt", None) if source is not None else "")
        or "unknown"
    )
    return f"multitenancy:{platform}:{profile_home.name}:{chat_id or 'unknown'}:{user_key}"


def _conversation_history_for_aiagent(
    messages: Optional[list[dict]],
    user_text: str,
) -> Optional[list[dict]]:
    if not messages:
        return None
    history = [dict(message) for message in messages if isinstance(message, dict)]
    if (
        history
        and history[-1].get("role") == "user"
        and str(history[-1].get("content") or "") == user_text
    ):
        history = history[:-1]
    return history or None


def _approval_bridge_timeout() -> float:
    raw = os.getenv("HERMES_MULTITENANCY_APPROVAL_TIMEOUT")
    if raw is None:
        raw = os.getenv("HERMES_APPROVAL_GATEWAY_TIMEOUT", "300")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 300.0


def _approval_bridge_dir() -> Path:
    raw = os.getenv("HERMES_MULTITENANCY_APPROVAL_DIR")
    if raw:
        root = Path(raw).expanduser()
    else:
        root = Path(tempfile.gettempdir()) / "hermes-multitenancy-approvals"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _read_approval_choice(path: Path) -> Optional[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.debug("[multitenancy] approval decision read failed: %s", exc)
        return "deny"
    choice = str(data.get("choice") or "").strip().lower()
    return choice if choice in {"once", "session", "always", "deny"} else "deny"


def _clarify_bridge_timeout() -> float:
    raw = os.getenv("HERMES_MULTITENANCY_CLARIFY_TIMEOUT")
    if raw is None:
        raw = os.getenv("HERMES_CLARIFY_TIMEOUT", "300")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 300.0


def _clarify_timeout_response(timeout_s: float) -> str:
    return (
        f"The user did not provide a response within {timeout_s:g}s. "
        "Use your best judgement to make the choice and proceed."
    )


def _clarify_bridge_dir() -> Path:
    raw = os.getenv("HERMES_MULTITENANCY_CLARIFY_DIR")
    if raw:
        root = Path(raw).expanduser()
    else:
        root = Path(tempfile.gettempdir()) / "hermes-multitenancy-clarify"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _read_clarify_response(path: Path) -> Optional[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.debug("[multitenancy] clarify response read failed: %s", exc)
        return ""
    return str(data.get("response") or data.get("answer") or "").strip()


def _configure_webui_clarify_bridge(event_sink, session_key: str):
    """Return an AIAgent clarify callback for WebUI-scoped runs."""
    if event_sink is None:
        return None

    clarify_dir = _clarify_bridge_dir()

    def _emit_bridge_event(event_name: str, **payload: Any) -> None:
        try:
            event_sink(event_name, **payload)
        except Exception:
            logger.debug("[multitenancy] clarify bridge event emit failed", exc_info=True)

    def _clarify_callback(question: Any, choices: Any = None) -> str:
        clarify_id = f"clarify_{uuid.uuid4().hex}"
        response_path = clarify_dir / f"{clarify_id}.json"
        normalized_choices = []
        if isinstance(choices, list):
            normalized_choices = [str(choice).strip() for choice in choices if str(choice).strip()]
        question_text = str(question or "").strip()
        _emit_bridge_event(
            "clarify_required",
            clarify_id=clarify_id,
            session_key=session_key,
            question=question_text,
            choices=normalized_choices,
            response_path=str(response_path),
        )

        timeout_s = _clarify_bridge_timeout()
        deadline = time.monotonic() + timeout_s
        response: Optional[str] = None
        timed_out = False
        while True:
            response = _read_clarify_response(response_path)
            if response is not None:
                break
            if time.monotonic() >= deadline:
                response = _clarify_timeout_response(timeout_s)
                timed_out = True
                break
            time.sleep(0.1)

        _emit_bridge_event(
            "clarify_resolved",
            clarify_id=clarify_id,
            session_key=session_key,
            response=str(response or ""),
            timed_out=timed_out,
        )
        return str(response or "")

    return _clarify_callback


def _configure_gateway_approval_bridge(event_sink, session_key: str):
    """Register child-process approval notify and return a cleanup callback."""
    try:
        from tools.approval import (
            register_gateway_notify,
            reset_current_session_key,
            resolve_gateway_approval,
            set_current_session_key,
            unregister_gateway_notify,
        )
    except Exception as exc:
        logger.debug("[multitenancy] approval bridge unavailable: %s", exc)
        return lambda: None

    token = set_current_session_key(session_key)
    old_env = {
        "HERMES_SESSION_KEY": os.environ.get("HERMES_SESSION_KEY"),
        "HERMES_GATEWAY_SESSION": os.environ.get("HERMES_GATEWAY_SESSION"),
        "HERMES_EXEC_ASK": os.environ.get("HERMES_EXEC_ASK"),
    }
    # Terminal/process guards may execute in worker threads that do not inherit
    # contextvars. The child subprocess is single-turn, so process-local env is
    # the safest compatibility bridge for those thread-local tool paths.
    os.environ["HERMES_SESSION_KEY"] = session_key
    os.environ["HERMES_GATEWAY_SESSION"] = "1"
    os.environ["HERMES_EXEC_ASK"] = "1"
    registered = False

    def _emit_bridge_event(event_name: str, **payload: Any) -> None:
        if event_sink is None:
            return
        try:
            event_sink(event_name, **payload)
        except Exception:
            logger.debug("[multitenancy] approval bridge event emit failed", exc_info=True)

    if event_sink is not None:
        approval_dir = _approval_bridge_dir()

        def _approval_notify_sync(approval_data: dict) -> None:
            approval_id = f"approval_{uuid.uuid4().hex}"
            decision_path = approval_dir / f"{approval_id}.json"
            command = str(approval_data.get("command") or "")
            description = str(approval_data.get("description") or "dangerous command")
            _emit_bridge_event(
                "approval_required",
                approval_id=approval_id,
                session_key=session_key,
                command=command,
                description=description,
                pattern_keys=approval_data.get("pattern_keys") or [],
                decision_path=str(decision_path),
            )

            timeout_s = _approval_bridge_timeout()
            deadline = time.monotonic() + timeout_s
            choice: Optional[str] = None
            timed_out = False
            while True:
                choice = _read_approval_choice(decision_path)
                if choice is not None:
                    break
                if time.monotonic() >= deadline:
                    choice = "deny"
                    timed_out = True
                    break
                time.sleep(0.1)

            try:
                resolve_gateway_approval(session_key, choice)
            except Exception as exc:
                logger.debug("[multitenancy] child approval resolve failed: %s", exc)
            _emit_bridge_event(
                "approval_resolved",
                approval_id=approval_id,
                session_key=session_key,
                choice=choice,
                timed_out=timed_out,
            )

        register_gateway_notify(session_key, _approval_notify_sync)
        registered = True

    def _cleanup() -> None:
        if registered:
            try:
                unregister_gateway_notify(session_key)
            except Exception:
                pass
        try:
            reset_current_session_key(token)
        except Exception:
            pass
        for key, old_value in old_env.items():
            try:
                if old_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_value
            except Exception:
                pass

    return _cleanup


def _finalize_aiagent_result(result: Optional[dict]) -> str:
    """Turn the core conversation result into the user-facing reply string.

    Dead-air fix: core's conversation_loop returns ``{failed:True, error:...}``
    (and ``final_response=None``) on a non-retryable failure, e.g. a provider
    HTTP 400. Silently flattening that to ``""`` hid the failure, so
    ``real_run_agent``'s legacy fallback and the router's "⚠️ 模型暂时不可用"
    message could never fire — the user got complete silence in Feishu.

    Raise on a failed/empty turn so those existing fallbacks activate; return
    the text on success. A genuine empty-but-successful turn (``final_response``
    is ``""`` with no ``failed`` flag) still returns ``""`` unchanged.

    Exception — output-length truncation is RECOVERABLE, not a provider failure:
    core rolls back to the last complete state (``partial:True``) and, in
    streaming, the user has usually already seen the partial text. Raising would
    fire the "⚠️ 模型暂时不可用" fallback + a legacy-spike retry that just truncates
    again, turning a long answer into a hard failure. So on truncation we return a
    graceful notice instead. Genuine failures (provider 400 etc.) still raise.
    """
    res = result or {}
    final_response = res.get("final_response")
    if final_response is not None and not res.get("failed"):
        return final_response or ""
    err = res.get("error") or "agent turn failed without a final response"
    if res.get("partial") or _is_output_truncation_error(err):
        return _TRUNCATION_NOTICE
    raise RuntimeError(f"AIAgent turn failed: {err}")


_TRUNCATION_NOTICE = (
    "⚠️ 这次回复内容太长，超出了单条输出的长度上限被截断了。\n"
    "你可以回复「继续」让我接着往下说，或把问题拆小一点、分几次问，我就能完整回答。"
)


def _is_output_truncation_error(err: Any) -> bool:
    text = str(err or "").lower()
    return "truncat" in text and "output length" in text


def _run_with_aiagent(
    event: Any,
    profile_home: Path,
    *,
    messages: Optional[list[dict]] = None,
    event_sink=None,
    usage_sink: Optional[dict] = None,
) -> str:
    """Synchronous body — runs hermes' real AIAgent against the profile config.

    Constructs an AIAgent with the profile's enabled toolsets + LLM
    credentials, sets the per-user open_id contextvar so UAT tools load the
    right token file, and runs a full tool-loop conversation.

    Designed to run inside ``aiagent_subprocess.py`` so the parent gateway
    keeps its async event loop isolated from the synchronous AIAgent/tool loop.
    """
    # 1) Anchor HERMES_HOME so any module that reads it sees the profile.
    os.environ["HERMES_HOME"] = str(profile_home)

    # 2) Read profile LLM config + credentials (mirrors the spike loader).
    config = _load_profile_config(profile_home)
    auth = _load_json(profile_home / "auth.json")
    from dotenv import dotenv_values
    env_overrides = dict(
        dotenv_values(profile_home / ".env")
        if (profile_home / ".env").exists()
        else {}
    )

    primary = (config.get("model") or {}).get("default")
    if not primary:
        raise RuntimeError("profile config missing model.default")
    primary = _model_spec_for_event(str(primary), event)
    fallback_models = config.get("fallback") or []

    provider, model_only = _split_model_spec(primary)
    api_key = _resolve_api_key(provider, env_overrides, auth) or _resolve_custom_provider_api_key(config, provider)
    if not api_key:
        raise RuntimeError(f"no API key for primary provider {provider!r}")

    base_url = _resolve_base_url(provider, True, config, env_overrides)

    # 3) Lazy-import hermes core (only when this code path is hit).
    _install_credential_env_passthrough(profile_home)
    _install_skill_runtime_compat(profile_home)
    try:
        from .browser_policy import install_browser_guard

        install_browser_guard(config, profile_home)
    except Exception:
        logger.debug("[multitenancy] browser guard install skipped", exc_info=True)
    from run_agent import AIAgent
    sender_open_id_scope, current_sender_open_id, shared_hermes_home = _load_feishu_oapi_runtime(profile_home)
    try:
        from hermes_cli.tools_config import _get_platform_tools
    except Exception:
        _get_platform_tools = None  # graceful: fall back to None toolsets

    # 4) Resolve platform and sender identity for toolset policy.
    platform_key = _resolve_platform_value(getattr(event, "source", None))
    # 5) Sender's real Feishu open_id (ou_*) for per-user UAT routing.
    # The feishu adapter already set this contextvar in
    # _process_inbound_message before dispatching us — prefer that value
    # because it comes straight from sender_id.open_id (the SDK gives the
    # ou_* form). Only fall back to event.source on weird code paths
    # (e.g., synthetic events constructed without going through the adapter).
    sender_open_id = (current_sender_open_id.get() or "") or _resolve_sender_open_id(event)

    try:
        enabled_toolsets = _resolve_enabled_toolsets(
            config,
            platform_key,
            platform_tools_resolver=_get_platform_tools,
            profile_home=profile_home,
            shared_home=shared_hermes_home,
            user_key=sender_open_id or None,
            xai_credentials={"available": True, "source": "upstream_toolset"},
        )
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        enabled_toolsets = _resolve_enabled_toolsets(
            config,
            platform_key,
            platform_tools_resolver=_get_platform_tools,
        )

    # 6) Pull source / session metadata for AIAgent kwargs.
    source = getattr(event, "source", None)
    user_text = getattr(event, "text", "") or ""
    session_id = _resolve_aiagent_session_id(event, profile_home, sender_open_id)
    gateway_session_key = _resolve_multitenant_gateway_session_key(
        event,
        profile_home,
        sender_open_id,
    )
    conversation_history = _conversation_history_for_aiagent(messages, user_text)

    runtime_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        runtime_kwargs["base_url"] = base_url
    if provider:
        # Forward provider name so AIAgent.__init__ selects the correct
        # transport (anthropic_messages for anthropic, codex_responses for
        # openai-codex / xai, etc.). Without this, AIAgent falls back to
        # chat_completions and ignores ANTHROPIC_BASE_URL, breaking
        # Anthropic-compatible providers like Tencent TokenHub.
        runtime_kwargs["provider"] = provider

    fallback_model = fallback_models[0] if fallback_models else None

    # 7) Wrap the agent run in sender_open_id_scope so legacy Feishu tools
    #    pick up the right token from the profile-local UAT directory.
    logger.info(
        "[multitenancy] running AIAgent for sender=%s profile=%s toolsets=%s",
        sender_open_id, profile_home.name,
        enabled_toolsets if enabled_toolsets is not None else "<default>",
    )
    _log_feishu_identity_context(
        profile_home=profile_home,
        shared_home=shared_hermes_home,
        sender_open_id=sender_open_id,
    )
    with sender_open_id_scope(sender_open_id or None):
        try:
            from gateway.session_context import clear_session_vars, set_session_vars
        except Exception:
            clear_session_vars = None
            set_session_vars = None

        platform_value = str(
            getattr(getattr(source, "platform", ""), "value", None)
            or getattr(source, "platform", "")
            or platform_key
        )
        session_tokens = None
        if set_session_vars is not None:
            session_tokens = set_session_vars(
                platform=platform_value,
                chat_id=str(getattr(source, "chat_id", "") or "") if source else "",
                chat_name=str(getattr(source, "chat_name", "") or "") if source else "",
                thread_id=str(getattr(source, "thread_id", "") or "") if source else "",
                user_id=str(getattr(source, "user_id", "") or "") if source else "",
                user_name=str(getattr(source, "user_name", "") or "") if source else "",
                session_key=str(gateway_session_key),
            )
        def _emit(event_name: str, **payload: Any) -> None:
            if event_sink is None:
                return
            try:
                event_sink(event_name, **payload)
            except Exception:
                logger.debug("[multitenancy] event_sink failed", exc_info=True)

        # In-flight workaround — see _mark_session_source_feishu docstring for
        # why we need to opportunistically retag. Called at multiple lifecycle
        # points (tool.started during run, finally pre-close, finally
        # post-close) because Hermes core may insert the sessions row at any
        # of those phases. UPDATE is idempotent so multiple calls are cheap.
        def _retag_source_now(reason: str) -> None:
            try:
                _mark_session_source_feishu(profile_home, str(session_id))
            except Exception as exc:
                # In sandboxed subprocesses PROFILE_HOME/state.db may be visible
                # but sqlite cannot open the WAL files. The parent streaming
                # mirror has the authoritative retag path; keep this best-effort
                # child-side attempt from polluting user-visible error logs.
                if exc.__class__.__name__ == "OperationalError" and "unable to open database file" in str(exc):
                    logger.debug(
                        "[multitenancy] skipped child-side session.source retag (reason=%s): %s",
                        reason,
                        exc,
                    )
                    return
                logger.exception(
                    "[multitenancy] failed to rewrite session.source -> feishu (reason=%s)",
                    reason,
                )

        # NOTE: state.db.messages live mirror lives in the parent process
        # (_stream_aiagent_subprocess), NOT here. The subprocess sandbox
        # blocks sqlite WAL opens on PROFILE_HOME/state.db, so every write
        # attempt failed with sqlite3.OperationalError: unable to open
        # database file. Moving the mirror to the parent — which has no
        # sandbox — sidesteps that. _retag_source_now below remains as a
        # best-effort second writer; it fails silently in sandboxed runs.

        def _tool_progress_event_callback(
            event_type: str,
            tool_name: str,
            preview: Any = None,
            args: Any = None,
            **kwargs: Any,
        ) -> None:
            _log_aiagent_tool_progress(event_type, tool_name, preview, args, **kwargs)
            if event_type == "tool.started":
                # First tool-start is the earliest deterministic signal that
                # Hermes core has inserted the session row into state.db with
                # source='api_server'. Flip it now so a long-running tool
                # (especially one blocked on user approval) doesn't strand the
                # row in the wrong source bucket while we wait.
                _retag_source_now("tool.started")
                _emit(
                    "tool_started",
                    name=str(tool_name or ""),
                    preview=str(preview or "") if preview is not None else None,
                    args=args,
                )
            elif event_type == "tool.completed":
                _emit(
                    "tool_completed",
                    name=str(tool_name or ""),
                    duration=float(kwargs.get("duration") or 0.0),
                    is_error=bool(kwargs.get("is_error")),
                )
            elif event_type == "_thinking":
                text = str(preview or tool_name or "")
                if text:
                    _emit("thinking", text=text)
            elif event_type == "reasoning.available":
                # Hermes upstream uses reasoning.available as a coarse preview
                # signal and often passes visible answer text in `preview`.
                # WebUI already treats this event as "thinking ended", not as
                # reasoning content. Do not turn it into a thinking delta here,
                # otherwise the UI shows duplicated/extra reasoning bubbles.
                return

        def _stream_delta_event_callback(text: Any) -> None:
            if text is None:
                return
            text = str(text)
            if text:
                _emit("content", text=text)

        def _reasoning_event_callback(text: Any) -> None:
            if text is None:
                return
            text = str(text)
            if text:
                _emit("thinking", text=text)

        def _tool_gen_event_callback(tool_name: str) -> None:
            if tool_name:
                _emit("tool_started", name=str(tool_name), preview="generating arguments")

        clarify_callback = (
            _configure_webui_clarify_bridge(event_sink, str(gateway_session_key))
            if platform_key == "webui"
            else None
        )

        agent_kwargs: dict[str, Any] = {
            # AIAgent expects the bare model name (e.g. "glm-5.1"), not the
            # provider-prefixed form. Provider was already used above to
            # resolve api_key + base_url; the prefix would otherwise be
            # forwarded verbatim to the OpenAI client and rejected with
            # `1211 Unknown Model`.
            "model": model_only,
            **runtime_kwargs,
            "max_iterations": int(os.getenv("HERMES_MAX_ITERATIONS", "30")),
            "quiet_mode": True,
            "verbose_logging": False,
            "session_id": str(session_id),
            "platform": platform_key,
            "user_id": str(getattr(source, "user_id", "") or "") if source else "",
            "user_name": str(getattr(source, "user_name", "") or "") if source else "",
            "chat_id": str(getattr(source, "chat_id", "") or "") if source else "",
            "chat_name": str(getattr(source, "chat_name", "") or "") if source else "",
            "chat_type": str(getattr(source, "chat_type", "") or "") if source else "",
            "gateway_session_key": str(gateway_session_key),
            "tool_progress_callback": _tool_progress_event_callback,
            "stream_delta_callback": _stream_delta_event_callback if event_sink is not None else None,
            "reasoning_callback": _reasoning_event_callback if event_sink is not None else None,
            "clarify_callback": clarify_callback,
            "tool_gen_callback": _tool_gen_event_callback if event_sink is not None else None,
        }
        if enabled_toolsets is not None:
            agent_kwargs["enabled_toolsets"] = enabled_toolsets
        if fallback_model:
            agent_kwargs["fallback_model"] = fallback_model

        approval_cleanup = _configure_gateway_approval_bridge(
            event_sink,
            str(gateway_session_key),
        )
        runtime_env_cleanup = _apply_runtime_env_for_aiagent(profile_home)
        vod_image_override_cleanup = _apply_vod_image_model_override_for_aiagent(user_text)
        agent = None
        try:
            _register_aiagent_process_image_gen_providers()
            agent = AIAgent(**agent_kwargs)
            run_kwargs: dict[str, Any] = {
                "user_message": user_text,
                "task_id": str(session_id),
            }
            if conversation_history is not None:
                run_kwargs["conversation_history"] = conversation_history
            result = agent.run_conversation(**run_kwargs)
            # 工件1a：捕获本回合 token 用量到 usage_sink，由父进程（非沙箱）写台账。
            # 不能在这里(子进程)直接写 /var/log/hermes —— 沙箱策略不允许该路径，
            # 写会静默失败。token 计数器只在子进程的 agent 上，故在此读出、透传出去；
            # 真正的台账落盘在 _run_aiagent_subprocess / _stream_aiagent_subprocess 的
            # 父进程侧完成（与 conversation_audit 同样的「父进程写」规避沙箱）。
            if usage_sink is not None:
                try:
                    from .token_usage_ledger import read_agent_session_tokens

                    _ut = read_agent_session_tokens(agent)
                    usage_sink.update({
                        "model": model_only,
                        "input_tokens": _ut["input_tokens"],
                        "output_tokens": _ut["output_tokens"],
                        "total_tokens": _ut["total_tokens"],
                    })
                except Exception:
                    logger.debug("[multitenancy] token usage capture skipped", exc_info=True)
        finally:
            # Best-effort retag from inside the sandbox; if it fails the
            # parent process re-runs it post-done with full write access.
            _retag_source_now("finally-pre-close")
            approval_cleanup()
            vod_image_override_cleanup()
            runtime_env_cleanup()
            if clear_session_vars is not None and session_tokens is not None:
                clear_session_vars(session_tokens)
            if agent is not None:
                try:
                    close_agent = getattr(agent, "close", None)
                    cleanup_agent = getattr(agent, "cleanup", None)
                    if callable(close_agent):
                        close_agent()
                    elif callable(cleanup_agent):
                        cleanup_agent()
                except Exception:
                    pass
            _retag_source_now("finally-post-close")

    return _finalize_aiagent_result(result)


def _mark_session_source_feishu(profile_home: Path, session_id: str) -> None:
    """Re-tag every ``source='api_server'`` row in this profile to ``'feishu'``.

    Hermes-agent always writes ``sessions.source = 'api_server'`` for
    AIAgent.run_conversation() (even with ``platform='feishu'``), and the
    SQLite row id is unrelated to the AIAgent session_id we hold in this
    scope, so we can't target one row. The per-user Feishu profile only
    ever receives Feishu-routed traffic from the multitenancy router, so
    it is safe to rewrite the whole api_server bucket in this profile.

    ``session_id`` is kept in the signature for future per-row targeting
    (e.g. via a session-id ↔ row-id mapping table). For now it's unused.
    """
    import sqlite3

    db_path = profile_home / "state.db"
    if not db_path.exists():
        return
    with sqlite3.connect(str(db_path)) as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
        ).fetchone() is None:
            return
        cur = conn.execute(
            "UPDATE sessions SET source = 'feishu' WHERE source = 'api_server'"
        )
        if cur.rowcount:
            logger.info(
                "[multitenancy] rewrote %d session(s) source=api_server -> feishu in %s",
                cur.rowcount, db_path,
            )
        conn.commit()


def _resolve_enabled_toolsets(
    config: dict[str, Any],
    platform_key: str,
    *,
    platform_tools_resolver: Any,
    profile_home: Path | None = None,
    shared_home: Path | None = None,
    user_key: str | None = None,
    departments: list[str] | tuple[str, ...] | None = None,
    xai_credentials: dict[str, Any] | None = None,
) -> Optional[list[str]]:
    """Resolve profile toolsets without dropping core non-Feishu abilities.

    A plain Hermes Feishu gateway defaults to the composite ``hermes-feishu``
    toolset, which includes web/search/browser/file/etc. During multitenant
    UAT it is common to add ``platform_toolsets.feishu`` only for Feishu user
    token helpers; treating that list as a hard replacement makes the agent
    look competent inside Feishu but unable to search the web.

    Default mode therefore merges explicit profile entries with the platform
    default. Set ``multitenancy.toolsets_mode: explicit`` or
    ``HERMES_MULTITENANCY_TOOLSETS_MODE=explicit`` to preserve the old strict
    replacement behavior for providers that need a smaller schema.
    """
    explicit = (config.get("platform_toolsets") or {}).get(platform_key)
    explicit_toolsets = _normalize_toolset_list(explicit)
    mode = _toolsets_mode(config, platform_key)

    def apply_discovery_policy(toolsets: list[str] | None) -> list[str] | None:
        if not toolsets or not (profile_home or shared_home):
            return toolsets
        try:
            from .discovery_policy import apply_toolset_policy

            profile_name = profile_home.name if profile_home is not None else str(config.get("profile_name") or "")
            if not profile_name:
                return [item for item in toolsets if item != "x_search"]
            root = shared_home
            if root is None and profile_home is not None:
                root = profile_home.parent.parent if profile_home.parent.name == "profiles" else profile_home
            if root is None:
                return [item for item in toolsets if item != "x_search"]
            return apply_toolset_policy(
                toolsets,
                shared_home=root,
                profile_name=profile_name,
                user_key=user_key,
                departments=departments,
                xai_credentials=xai_credentials,
            )
        except Exception as exc:
            logger.warning("[multitenancy] discovery policy filter failed: %s", exc)
            return [item for item in toolsets if item != "x_search"]

    def apply_profile_tool_policies(toolsets: list[str] | None) -> list[str] | None:
        filtered = apply_discovery_policy(toolsets)
        if profile_home is None:
            return filtered
        try:
            from .browser_policy import browser_decision, browser_toolsets_for_policy

            return browser_toolsets_for_policy(
                filtered,
                browser_decision(config, profile_home),
            )
        except Exception as exc:
            logger.warning("[multitenancy] browser toolset policy failed: %s", exc)
            return [item for item in filtered or [] if item != "browser"] or None

    if explicit_toolsets and mode in {"explicit", "strict", "replace"}:
        logger.info(
            "[multitenancy] platform_toolsets explicit mode for %s: %s",
            platform_key, explicit_toolsets,
        )
        return apply_profile_tool_policies(explicit_toolsets)

    default_toolsets: list[str] = []
    resolver_platform_key = "api_server" if platform_key == "webui" else platform_key
    if platform_tools_resolver is not None:
        resolver_config = config
        if explicit_toolsets:
            import copy

            resolver_config = copy.deepcopy(config)
            platform_toolsets = resolver_config.get("platform_toolsets")
            if isinstance(platform_toolsets, dict):
                platform_toolsets.pop(platform_key, None)
                if resolver_platform_key != platform_key:
                    platform_toolsets.pop(resolver_platform_key, None)
        try:
            try:
                resolved = platform_tools_resolver(
                    resolver_config,
                    resolver_platform_key,
                    include_default_mcp_servers=("no_mcp" not in explicit_toolsets),
                )
            except TypeError:
                resolved = platform_tools_resolver(resolver_config, resolver_platform_key)
            default_toolsets = _normalize_toolset_list(resolved)
        except Exception as exc:
            logger.warning(
                "[multitenancy] _get_platform_tools failed for %s: %s",
                platform_key, exc,
            )

    if explicit_toolsets and not default_toolsets:
        default_toolsets = _fallback_default_toolsets(platform_key)

    if explicit_toolsets:
        merged = sorted(set(default_toolsets) | set(explicit_toolsets))
        logger.info(
            "[multitenancy] platform_toolsets merged for %s: explicit=%s default=%s merged=%s",
            platform_key, explicit_toolsets, default_toolsets, merged,
        )
        return apply_profile_tool_policies(merged)

    return apply_profile_tool_policies(default_toolsets) or None


def _fallback_default_toolsets(platform_key: str) -> list[str]:
    """Core toolsets to preserve when Hermes' platform resolver is unavailable."""
    if platform_key in {"api_server", "webui"}:
        return ["file", "terminal", "web"]
    return []


def _normalize_toolset_list(value: Any) -> list[str]:
    """Return a sorted list of non-empty string toolset names."""
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, (set, tuple)):
        items = list(value)
    else:
        return []
    return sorted({str(item).strip() for item in items if str(item).strip()})


def _toolsets_mode(config: dict[str, Any], platform_key: str | None = None) -> str:
    """Return multitenancy toolset resolution mode."""
    env_mode = os.getenv("HERMES_MULTITENANCY_TOOLSETS_MODE")
    if env_mode:
        return env_mode.strip().lower()
    plugin_cfg = config.get("multitenancy") or {}
    if isinstance(plugin_cfg, dict):
        if platform_key:
            platform_modes = plugin_cfg.get("platform_toolsets_mode")
            if isinstance(platform_modes, dict):
                mode = platform_modes.get(platform_key)
                if mode:
                    return str(mode).strip().lower()
        mode = plugin_cfg.get("toolsets_mode")
        if mode:
            return str(mode).strip().lower()
    return "merge_default"
