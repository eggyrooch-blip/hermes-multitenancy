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
        stream = (
            _stream_aiagent_subprocess(event, profile_home, messages=messages)
            if messages is not None
            else _stream_aiagent_subprocess(event, profile_home)
        )
        async for kind, payload in stream:
            if kind == "done":
                final_text = str(payload or "")
                continue
            if kind == "content":
                text = str(payload or "")
                if text:
                    content_parts.append(text)
                    yield "content", text
                continue
            yield kind, payload
        if final_text and not "".join(content_parts).strip():
            yield "content", final_text
        if final_text or content_parts:
            return
    except Exception as exc:
        logger.warning(
            "[multitenancy] streaming AIAgent path failed (%s); falling back to legacy stream",
            exc, exc_info=True,
        )

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
    return shared_home


def _configure_cron_home(shared_home: Path) -> None:
    """Make cronjob tool writes visible to the shared gateway scheduler.

    The gateway cron ticker imports ``cron.jobs`` under the shared Hermes home
    and only scans ``<shared>/cron/jobs.json``. Multitenant AIAgent subprocesses
    run with ``HERMES_HOME=<profile>``, so importing ``cron.jobs`` there would
    otherwise bind cron storage to ``<profile>/cron/jobs.json``. That creates
    jobs that remain forever ``scheduled`` because no gateway ticker watches
    that profile-local file.
    """
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
        logger.info("[multitenancy] cron jobs bound to shared Hermes home: %s", cron_jobs.JOBS_FILE)
    except Exception as exc:
        logger.warning("[multitenancy] failed to bind cron jobs to shared Hermes home: %s", exc)
    finally:
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home


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
# Secrets (API keys, tokens) are NEVER on this list. They live in
# ``<profile_home>/.env`` or ``auth.json`` and are loaded inside the child by
# ``_run_with_aiagent``'s own dotenv path.
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
    "HERMES_APPROVAL_GATEWAY_TIMEOUT",
})


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

      2. **XDG / TMPDIR pivot** — XDG cache/config/state/data and TMPDIR
         redirect into the profile's own directory tree, so a skill that
         caches via ``XDG_CACHE_HOME`` / ``tempfile.gettempdir()`` writes to
         ``<profile>/cache/...`` instead of the gateway user's shared cache.

    HOME is intentionally **not** pivoted. The plugin runs as a host process
    (not inside a container), so the user's home directory is the real
    ``/Users/<user>`` and host-installed CLIs (``kep-cli``, ``aws``, etc.)
    rely on ``Path.home() / ".kep-cli"`` style paths to find their state.
    Rebinding HOME would break those tools — see the kep-cli profile
    convention note (``架构 — Hermes Profiles 安装 kep-cli 2026-05-07.md``)
    in the Second Brain vault for the lookup pattern. A previous iteration
    of this helper did rebind HOME (modelled after the OpenClaw
    ``keep-record`` workspace-bridge pattern), but that pattern applies only
    inside Docker containers where the workspace IS the sandbox boundary;
    here HOME stays at the user.

    Skill-level token isolation is therefore enforced explicitly by
    :mod:`hermes_multitenancy.skill_storage` (writes always go to
    ``<HERMES_HOME>/tokens/``) rather than implicitly via HOME redirection.

    The directories ``cache/``, ``config/``, ``state/``, ``data/`` and
    ``tmp/`` are created on first use (mode 0700).

    Profile-local secrets are loaded inside the child by
    :func:`_run_with_aiagent` from ``<profile_home>/.env`` and ``auth.json``;
    they are intentionally NOT injected here so the child never sees a parent
    env-derived key as an "ambient" credential.
    """
    parent = os.environ
    env: dict[str, str] = {
        key: parent[key] for key in _SUBPROCESS_ENV_ALLOWLIST if key in parent
    }

    profile_home = profile_home.expanduser()
    # HOME is NOT pivoted (see docstring). Only XDG/TMPDIR redirect.
    pivot = {
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
    env["HERMES_GATEWAY_SESSION"]           = "1"
    env["HERMES_EXEC_ASK"]                  = "1"
    env["HERMES_MULTITENANCY_APPROVAL_DIR"] = str(approval_dir)
    if event_stream:
        env["HERMES_AIAGENT_EVENT_STREAM"] = "1"

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

    if extra:
        env.update(extra)
    return env


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
    wrapped = [_BWRAP_EXEC, *bwrap_args, "--", *cmd]
    logger.info(
        "[multitenancy] bwrap wrap: policy=%s profile=%s agent_repo=%s",
        _BWRAP_ARGS_FILE.name, profile_home_resolved.name, agent_repo,
    )
    return wrapped


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
    timeout_s = float(os.getenv("HERMES_AIAGENT_SUBPROCESS_TIMEOUT", "300"))
    approval_dir = Path(tempfile.mkdtemp(prefix="hermes-mt-approval-"))
    env = _build_subprocess_env(profile_home, approval_dir=approval_dir)
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
        sender_open_id = _resolve_sender_open_id(event)
    _canonical_session_id = _resolve_aiagent_session_id(event, profile_home, sender_open_id)
    user_text = getattr(event, "text", "") or ""
    _state_db_path = profile_home / "state.db"

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
            self.assistant_content: str = ""
            self.assistant_reasoning: str = ""
            self.session_ensured: bool = False
            self.user_inserted: bool = False
            self.retagged: bool = False

        def _conn(self):
            return _sqlite3.connect(str(_state_db_path), timeout=2.0)

        def ensure_session(self) -> None:
            if self.session_ensured or not _state_db_path.exists():
                self.session_ensured = True
                return
            try:
                with self._conn() as conn:
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
                with self._conn() as conn:
                    conn.execute(
                        "INSERT INTO messages (session_id, role, content, timestamp) "
                        "VALUES (?, 'user', ?, ?)",
                        (str(session_id), text or "", _time.time()),
                    )
                self.user_inserted = True
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
                with self._conn() as conn:
                    if self.active_assistant_id is None:
                        cur = conn.execute(
                            "INSERT INTO messages (session_id, role, content, reasoning, timestamp) "
                            "VALUES (?, 'assistant', ?, ?, ?)",
                            (
                                str(session_id),
                                self.assistant_content,
                                self.assistant_reasoning or None,
                                _time.time(),
                            ),
                        )
                        self.active_assistant_id = cur.lastrowid
                    else:
                        conn.execute(
                            "UPDATE messages SET content=?, reasoning=? WHERE id=?",
                            (
                                self.assistant_content,
                                self.assistant_reasoning or None,
                                self.active_assistant_id,
                            ),
                        )
            except Exception:
                logger.exception("[multitenancy] mirror upsert_assistant failed")

        def seal_assistant(self) -> None:
            self.active_assistant_id = None
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
                with self._conn() as conn:
                    conn.execute(
                        "INSERT INTO messages (session_id, role, content, tool_name, tool_calls, timestamp) "
                        "VALUES (?, 'assistant', '', ?, ?, ?)",
                        (str(session_id), str(tool_name), payload, _time.time()),
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
                with self._conn() as conn:
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
    timeout_s = float(os.getenv("HERMES_AIAGENT_SUBPROCESS_TIMEOUT", "300"))
    approval_dir = Path(tempfile.mkdtemp(prefix="hermes-mt-approval-"))
    env = _build_subprocess_env(profile_home, approval_dir=approval_dir, event_stream=True)
    # Resolve symlinks so sandbox-exec's path-based allow rules match.
    # The plugin is typically loaded via a profile-local symlink
    # (~/.hermes/profiles/<p>/plugins/multitenancy → ~/code/hermes-multitenancy/),
    # but the sandbox policy only whitelists the resolved repo path.
    # Without .resolve() the child python sees an [Errno 1] Operation not
    # permitted when trying to open aiagent_subprocess.py through the symlink.
    child_script = Path(__file__).with_name("aiagent_subprocess.py").resolve()
    cmd = _wrap_with_sandbox([sys.executable, str(child_script)], profile_home)

    started_at = time.monotonic()
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
    )
    logger.info(
        "[multitenancy] AIAgent subprocess spawned pid=%s elapsed=%.3fs",
        proc.pid,
        time.monotonic() - started_at,
    )
    stderr_task = asyncio.create_task(proc.stderr.read())
    saw_done = False
    first_event_logged = False
    try:
        assert proc.stdin is not None
        proc.stdin.write(payload)
        await proc.stdin.drain()
        proc.stdin.close()
        try:
            await proc.stdin.wait_closed()
        except Exception:
            pass

        assert proc.stdout is not None
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout_s)
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
                _mirror.seal_assistant()
                _mirror.retag_source()
                _mirror.dedupe()
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
            }:
                payload_data = {k: v for k, v in data.items() if k != "event"}
                if event_name == "tool_started":
                    # Seal any pre-tool assistant text into its own row, then
                    # mirror the tool invocation. First tool-start is also
                    # the safest moment to retag — Hermes core has had time
                    # to insert the sessions row by now.
                    _mirror.seal_assistant()
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
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    except Exception:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if not stderr_task.done():
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


def _run_with_aiagent(
    event: Any,
    profile_home: Path,
    *,
    messages: Optional[list[dict]] = None,
    event_sink=None,
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
    config = _load_yaml(profile_home / "config.yaml")
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
    fallback_models = config.get("fallback") or []

    provider, model_only = _split_model_spec(primary)
    api_key = _resolve_api_key(provider, env_overrides, auth)
    if not api_key:
        raise RuntimeError(f"no API key for primary provider {provider!r}")

    base_url = _resolve_base_url(provider, True, config)

    # 3) Lazy-import hermes core (only when this code path is hit).
    from run_agent import AIAgent
    from tools import feishu_oapi_client as feishu_oapi
    sender_open_id_scope = feishu_oapi.sender_open_id_scope
    current_sender_open_id = feishu_oapi.current_sender_open_id
    shared_hermes_home = _configure_feishu_uat_home(feishu_oapi, profile_home)
    _configure_cron_home(shared_hermes_home)
    if shared_hermes_home != profile_home:
        logger.info(
            "[multitenancy] using shared Feishu UAT dir: %s",
            shared_hermes_home / "feishu_uat",
        )
    try:
        from hermes_cli.tools_config import _get_platform_tools
    except Exception:
        _get_platform_tools = None  # graceful: fall back to None toolsets

    # 4) Resolve toolsets enabled for this platform on this profile.
    platform_key = "feishu"
    enabled_toolsets = _resolve_enabled_toolsets(
        config,
        platform_key,
        platform_tools_resolver=_get_platform_tools,
    )

    # 5) Sender's real Feishu open_id (ou_*) for per-user UAT routing.
    # The feishu adapter already set this contextvar in
    # _process_inbound_message before dispatching us — prefer that value
    # because it comes straight from sender_id.open_id (the SDK gives the
    # ou_* form). Only fall back to event.source on weird code paths
    # (e.g., synthetic events constructed without going through the adapter).
    sender_open_id = (current_sender_open_id.get() or "") or _resolve_sender_open_id(event)

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

    # 7) Wrap the agent run in sender_open_id_scope so per-user UAT tools
    #    pick up the right token from ~/.hermes/feishu_uat/<open_id>.json.
    logger.info(
        "[multitenancy] running AIAgent for sender=%s profile=%s toolsets=%s",
        sender_open_id, profile_home.name,
        enabled_toolsets if enabled_toolsets is not None else "<default>",
    )
    logger.info(
        "[multitenancy] Feishu UAT lookup sender=%s dir=%s",
        sender_open_id,
        shared_hermes_home / "feishu_uat",
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
            except Exception:
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
                )
            elif event_type == "tool.completed":
                _emit(
                    "tool_completed",
                    name=str(tool_name or ""),
                    duration=float(kwargs.get("duration") or 0.0),
                    is_error=bool(kwargs.get("is_error")),
                )
            elif event_type in {"reasoning.available", "_thinking"}:
                text = str(preview or tool_name or "")
                if text:
                    _emit("thinking", text=text)

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
        agent = AIAgent(**agent_kwargs)
        try:
            run_kwargs: dict[str, Any] = {
                "user_message": user_text,
                "task_id": str(session_id),
            }
            if conversation_history is not None:
                run_kwargs["conversation_history"] = conversation_history
            result = agent.run_conversation(**run_kwargs)
        finally:
            # Best-effort retag from inside the sandbox; if it fails the
            # parent process re-runs it post-done with full write access.
            _retag_source_now("finally-pre-close")
            approval_cleanup()
            if clear_session_vars is not None and session_tokens is not None:
                clear_session_vars(session_tokens)
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

    return (result or {}).get("final_response", "") or ""


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
    mode = _toolsets_mode(config)

    if explicit_toolsets and mode in {"explicit", "strict", "replace"}:
        logger.info(
            "[multitenancy] platform_toolsets explicit mode for %s: %s",
            platform_key, explicit_toolsets,
        )
        return explicit_toolsets

    default_toolsets: list[str] = []
    if platform_tools_resolver is not None:
        resolver_config = config
        if explicit_toolsets:
            import copy

            resolver_config = copy.deepcopy(config)
            platform_toolsets = resolver_config.get("platform_toolsets")
            if isinstance(platform_toolsets, dict):
                platform_toolsets.pop(platform_key, None)
        try:
            try:
                resolved = platform_tools_resolver(
                    resolver_config,
                    platform_key,
                    include_default_mcp_servers=("no_mcp" not in explicit_toolsets),
                )
            except TypeError:
                resolved = platform_tools_resolver(resolver_config, platform_key)
            default_toolsets = _normalize_toolset_list(resolved)
        except Exception as exc:
            logger.warning(
                "[multitenancy] _get_platform_tools failed for %s: %s",
                platform_key, exc,
            )

    if explicit_toolsets:
        merged = sorted(set(default_toolsets) | set(explicit_toolsets))
        logger.info(
            "[multitenancy] platform_toolsets merged for %s: explicit=%s default=%s merged=%s",
            platform_key, explicit_toolsets, default_toolsets, merged,
        )
        return merged

    return default_toolsets or None


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


def _toolsets_mode(config: dict[str, Any]) -> str:
    """Return multitenancy toolset resolution mode."""
    env_mode = os.getenv("HERMES_MULTITENANCY_TOOLSETS_MODE")
    if env_mode:
        return env_mode.strip().lower()
    plugin_cfg = config.get("multitenancy") or {}
    if isinstance(plugin_cfg, dict):
        mode = plugin_cfg.get("toolsets_mode")
        if mode:
            return str(mode).strip().lower()
    return "merge_default"
