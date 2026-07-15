from __future__ import annotations

import sys as _sys
_pkg = _sys.modules["hermes_multitenancy.agent_real"]

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
import threading
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional


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
    config = _pkg._load_profile_config(profile_home)
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
    _install_ingest_secret_env_passthrough()
    _install_skill_runtime_compat(profile_home)
    _install_session_search_proxy_for_aiagent()
    try:
        from ..browser_policy import install_browser_guard

        install_browser_guard(config, profile_home)
    except Exception:
        logger.debug("[multitenancy] browser guard install skipped", exc_info=True)
    from run_agent import AIAgent
    _install_session_search_recall_db_proxy(AIAgent)
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
        enabled_toolsets = _pkg._resolve_enabled_toolsets(
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
        enabled_toolsets = _pkg._resolve_enabled_toolsets(
            config,
            platform_key,
            platform_tools_resolver=_get_platform_tools,
        )
    disabled_toolsets = _resolve_disabled_toolsets(config)
    if _is_ingest_run_event(event):
        before_ingest_toolsets = list(enabled_toolsets or [])
        enabled_toolsets = _ingest_enabled_toolsets(enabled_toolsets, platform_key)
        if before_ingest_toolsets != list(enabled_toolsets or []):
            logger.info(
                "[multitenancy] ingest run filtered toolsets profile=%s before=%s after=%s",
                profile_home.name,
                before_ingest_toolsets or "<default>",
                enabled_toolsets if enabled_toolsets is not None else "<default>",
            )

    # 6) Pull source / session metadata for AIAgent kwargs.
    source = getattr(event, "source", None)
    event_metadata = _event_metadata(event)
    user_text = getattr(event, "text", "") or ""
    original_user_text = user_text
    user_text = _enrich_webui_image_attachments_for_aiagent(
        user_text,
        platform_key=platform_key,
        profile_home=profile_home,
        config=config,
        fallback_api_key=api_key,
        fallback_base_url=base_url,
        fallback_model=model_only,
        metadata_source=str(event_metadata.get("source") or ""),
    )
    session_id = _resolve_aiagent_session_id(event, profile_home, sender_open_id)
    gateway_session_key = _resolve_multitenant_gateway_session_key(
        event,
        profile_home,
        sender_open_id,
    )
    conversation_history = _conversation_history_for_aiagent(messages, original_user_text)

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
            session_var_kwargs = {
                "platform": platform_value,
                "chat_id": str(getattr(source, "chat_id", "") or "") if source else "",
                "chat_name": str(getattr(source, "chat_name", "") or "") if source else "",
                "thread_id": str(getattr(source, "thread_id", "") or "") if source else "",
                "user_id": str(getattr(source, "user_id", "") or "") if source else "",
                "user_name": str(getattr(source, "user_name", "") or "") if source else "",
                "session_key": str(gateway_session_key),
                "async_delivery": (platform_key != "webui"),
            }
            try:
                session_tokens = set_session_vars(**session_var_kwargs)
            except TypeError as exc:
                if "async_delivery" not in str(exc):
                    raise
                logger.warning(
                    "[multitenancy] Hermes gateway.session_context.set_session_vars "
                    "does not accept async_delivery; falling back to legacy "
                    "session context behavior on this runtime "
                    "(platform=%s profile=%s)",
                    platform_key,
                    profile_home.name,
                )
                session_var_kwargs.pop("async_delivery", None)
                session_tokens = set_session_vars(**session_var_kwargs)
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

        if platform_key == "webui":
            clarify_callback = _configure_webui_clarify_bridge(event_sink, str(gateway_session_key))
        elif platform_key == "feishu":
            from ..feishu_clarify_cards import _configure_feishu_clarify_bridge

            clarify_callback = _configure_feishu_clarify_bridge(event_sink, str(gateway_session_key))
        else:
            clarify_callback = None

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
        from ..billing_identity import request_overrides_for_endpoint

        billing_request_overrides = request_overrides_for_endpoint(
            event_metadata,
            base_url or "",
        )
        if billing_request_overrides:
            agent_kwargs["request_overrides"] = billing_request_overrides
        if enabled_toolsets is not None:
            agent_kwargs["enabled_toolsets"] = enabled_toolsets
        if disabled_toolsets:
            agent_kwargs["disabled_toolsets"] = disabled_toolsets
        if fallback_model:
            agent_kwargs["fallback_model"] = fallback_model

        # Expert Role-Override overlay (ephemeral, this run only). Rides the
        # UPSTREAM ``ephemeral_system_prompt`` constructor kwarg: core re-appends
        # it to the system message every turn at API-call time and NEVER writes it
        # to the cached/DB-stored prompt or trajectories (run_agent.py:6208,12746).
        # That makes the overlay switch per-run/per-expert, survive the gateway's
        # fresh-AIAgent-per-message model, and support mid-conversation switching —
        # all with ZERO hermes-agent change (``ephemeral_system_prompt`` is in
        # NousResearch origin/main, so prod honors it without any core fork).
        #
        # The block lands BELOW the SOUL/base identity tier (core builds SOUL
        # first), so precedence is carried by the bookended Role-Override WORDING
        # (ROLE_OVERRIDE_PREAMBLE/TAIL — explicit disavowal of Hermes/Nous
        # Research), not by position. The previously-wired ``identity_override``
        # kwarg required FORKING core and would RAISE on upstream (see SPEC dead
        # ends); it is gone.
        _role_override = _pkg._role_override_block_for_event(event, profile_home)
        if _role_override:
            agent_kwargs["ephemeral_system_prompt"] = _role_override

        approval_cleanup = _configure_gateway_approval_bridge(
            event_sink,
            str(gateway_session_key),
        )
        runtime_env_cleanup = _apply_runtime_env_for_aiagent(profile_home)
        # Skill scope (能力随专家私有, sunke 2026-06-26): hide every NON-active
        # expert skill (ALL of them on a non-expert run → byte-identical catalog)
        # AND expose the active expert's private dirs. Hiding is a
        # get_disabled_skill_names monkeypatch that works on UPSTREAM/prod core
        # (which ignores HERMES_DISABLED_SKILLS_EXTRA); both patches are
        # subprocess-local (fresh create_subprocess_exec child).
        expert_skill_scope_cleanup = _pkg._apply_expert_skill_scope_for_aiagent(event, profile_home)
        vod_image_override_cleanup = _apply_vod_image_model_override_for_aiagent(user_text)
        aux_runtime_cleanup = _sync_auxiliary_runtime_main_for_aiagent(
            provider=provider,
            model=model_only,
            base_url=base_url,
            api_key=api_key,
            api_mode=str(runtime_kwargs.get("api_mode") or ""),
            request_overrides=billing_request_overrides,
        )
        agent = None
        try:
            _register_aiagent_process_image_gen_providers()
            try:
                agent = AIAgent(**agent_kwargs)
            except TypeError as exc:
                # Narrow fallback (SPEC regression guard): a core that lacks the
                # upstream ``ephemeral_system_prompt`` kwarg must DROP the expert
                # overlay and run as a normal SOUL session, never hard-fail. The
                # CI signature guard (test_core_aiagent_exposes_ephemeral_system_prompt_seam)
                # catches a seamless core at build time; this is the runtime net.
                if "ephemeral_system_prompt" in agent_kwargs and "ephemeral_system_prompt" in str(exc):
                    logger.warning(
                        "[multitenancy] core lacks ephemeral_system_prompt kwarg; "
                        "dropping expert overlay, running as normal SOUL session"
                    )
                    agent_kwargs.pop("ephemeral_system_prompt", None)
                    agent = AIAgent(**agent_kwargs)
                else:
                    raise
            run_kwargs: dict[str, Any] = {
                "user_message": user_text,
                "task_id": str(session_id),
            }
            if conversation_history is not None:
                run_kwargs["conversation_history"] = conversation_history
            profile_anchor_env = _profile_anchor_env_for_aiagent(profile_home)
            os.environ.update(profile_anchor_env)
            os.environ.update(_force_env_for_terminal_passthrough(profile_anchor_env))
            result = agent.run_conversation(**run_kwargs)
            # 工件1a：捕获本回合 token 用量到 usage_sink，由父进程（非沙箱）写台账。
            # 不能在这里(子进程)直接写 /var/log/hermes —— 沙箱策略不允许该路径，
            # 写会静默失败。token 计数器只在子进程的 agent 上，故在此读出、透传出去；
            # 真正的台账落盘在 _run_aiagent_subprocess / _stream_aiagent_subprocess 的
            # 父进程侧完成（与 conversation_audit 同样的「父进程写」规避沙箱）。
            if usage_sink is not None:
                try:
                    from ..token_usage_ledger import read_agent_session_tokens

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
            aux_runtime_cleanup()
            vod_image_override_cleanup()
            expert_skill_scope_cleanup()
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

    # Raw subprocess diagnostic (child-side, BEFORE finalize collapses the dict to
    # a string): one line of the core's raw result for any non-clean turn so prod
    # can confirm the dominant failure class (#1 invalid tool name vs #2 truncated
    # tool-call args vs textless truncation). finish_reason is not part of the core
    # result dict; the error string is the discriminator.
    _r = result or {}
    if _r.get("failed") or _r.get("partial") or not (
        (_r.get("final_response") or "").strip() if isinstance(_r.get("final_response"), str) else _r.get("final_response")
    ):
        redact_runtime_text = lambda value: _redact_ingest_runtime_text(value, event)
        logger.warning(
            "[multitenancy][finalize-diag] raw child result: partial=%s failed=%s "
            "completed=%s has_text=%s error=%r",
            _r.get("partial"), _r.get("failed"), _r.get("completed"),
            bool(isinstance(_r.get("final_response"), str) and _r["final_response"].strip()),
            redact_runtime_text(_r.get("error")),
        )
    return _finalize_aiagent_result(
        result,
        redact=lambda value: _redact_ingest_runtime_text(value, event),
    )
