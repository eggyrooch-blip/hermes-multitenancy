from __future__ import annotations

import sys as _sys
_pkg = _sys.modules[__package__]

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


def _persistable_tool_call(tool_name: str, preview: Any, args: Any) -> dict[str, Any]:
    if tool_name == "lark_cli":
        return {
            "name": "lark_cli",
            "args": {"redacted": True},
            "preview": "lark_cli",
        }
    return {"name": str(tool_name), "args": args, "preview": preview}


class _ProtocolPlaceholderStreamSanitizer:
    """Remove the empty-message marker even when it crosses stream chunks."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, value: Any) -> str:
        text = (self._pending + str(value or "")).replace(
            _EMPTY_MESSAGE_PROTOCOL_PLACEHOLDER,
            "",
        )
        self._pending = ""
        max_overlap = min(
            len(text),
            len(_EMPTY_MESSAGE_PROTOCOL_PLACEHOLDER) - 1,
        )
        for size in range(max_overlap, 0, -1):
            if text.endswith(_EMPTY_MESSAGE_PROTOCOL_PLACEHOLDER[:size]):
                self._pending = text[-size:]
                return text[:-size]
        return text

    def finish(self) -> str:
        pending = self._pending
        self._pending = ""
        return pending


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

    config = _pkg._load_profile_config(profile_home)
    auth = _load_json(profile_home / "auth.json")
    env_overrides = (
        dotenv_values(profile_home / ".env") if (profile_home / ".env").exists() else {}
    )

    primary = config.get("model", {}).get("default")
    fallback_models = config.get("fallback") or []
    candidates: list[str] = [primary] if primary else []
    candidates.extend(fallback_models)

    soul_text = _load_soul(profile_home)
    # Expert mode (ephemeral, this run only): the Hermes-hosted expert block leads
    # the single system message. SOUL.md remains unchanged.
    system_text = _compose_system_text(event, profile_home, soul_text)
    user_text = getattr(event, "text", "") or ""

    # Caller can override the message list (used for multi-turn history).
    # Default: system prompt + single user message.
    if messages is None:
        effective_messages: list[dict] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]
    else:
        # Caller supplies the conversation. We still inject SOUL (+ any expert
        # overlay) as system to guarantee the active persona stays in force.
        effective_messages = [
            {"role": "system", "content": system_text},
            *messages,
        ]

    last_error: Optional[BaseException] = None

    for model_spec in candidates:
        if not model_spec:
            continue
        try:
            provider, model_name = _split_model_spec(
                model_spec,
                strip_custom_context_suffix=True,
            )
        except ValueError:
            continue
        api_key = _resolve_api_key(provider, env_overrides, auth) or _resolve_custom_provider_api_key(config, provider)
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


class AiagentToolStallTimeout(RuntimeError):
    """The child produced no stream events for ``timeout_s`` and was killed.

    Carries the tool that was in flight (if any) so the log line and the user
    notice can say WHICH tool stalled instead of the generic 中途出错 — prod
    2026-09-03: a 5-minute ``./oup adobe init`` died at 300s with zero log
    lines and the next turn's model invented "沙箱受限" from the dangling call.
    The message keeps the historical "produced no stream events" prefix.
    """

    def __init__(
        self,
        timeout_s: float,
        *,
        tool_call_id: str = "",
        tool_name: str = "",
        elapsed_s: float | None = None,
    ) -> None:
        self.timeout_s = float(timeout_s)
        self.tool_call_id = tool_call_id or ""
        self.tool_name = tool_name or ""
        self.elapsed_s = elapsed_s
        detail = ""
        if self.tool_name and self.elapsed_s is not None:
            detail = f" (tool {self.tool_name} in flight for {self.elapsed_s:.0f}s)"
        super().__init__(
            f"AIAgent subprocess produced no stream events for {self.timeout_s:g}s{detail}"
        )

    @property
    def user_notice(self) -> str:
        if self.tool_name:
            secs = f"{self.elapsed_s:.0f}" if self.elapsed_s is not None else f"{self.timeout_s:g}"
            return f"⚠️ 工具 {self.tool_name} 已运行 {secs} 秒没有输出，本轮已中止。请重新发送指令。"
        return f"⚠️ 后台执行 {self.timeout_s:g} 秒没有任何输出，本轮已中止。请重新发送指令。"


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
    sender_open_id = _resolve_subprocess_sender_open_id(event)
    _canonical_session_id = _resolve_aiagent_session_id(event, profile_home, sender_open_id)
    user_text = getattr(event, "text", "") or ""
    _state_db_path = profile_home / "state.db"
    _source_for_display = getattr(event, "source", None)
    _preserve_reasoning_in_state = _resolve_platform_value(_source_for_display) != "webui"
    try:
        from ..conversation_audit import (
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
    _is_session_start = (
        messages is None or len(messages) <= 1
    ) and not getattr(event, "_hermes_billing_retry", False)
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
        from ..feishu_group_topic_session import group_topic_epoch_actor

        epoch_actor = group_topic_epoch_actor(event, sender_open_id)
        return (
            profile_home
            / "mirror_epochs"
            / f"{_safe(_chat_id_for_epoch)}__{_safe(epoch_actor)}.txt"
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
    from . import executor_map as _executor_map

    _stage_codex_output = (
        _executor_map.runtime_for_event(event, profile_home)
        == _executor_map.CODEX_APP_SERVER
    )

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
            self.stage_output = _stage_codex_output
            self.staged_operations: list[tuple[Any, ...]] = []

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
                    columns = {
                        str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")
                    }
                    if "tool_call_id" not in columns:
                        conn.execute("ALTER TABLE messages ADD COLUMN tool_call_id TEXT")
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
            if self.stage_output:
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
            if self.stage_output:
                if self.assistant_content or self.assistant_reasoning:
                    self.staged_operations.append(
                        (
                            "assistant",
                            self.assistant_content,
                            self.assistant_reasoning,
                            finish_reason,
                        )
                    )
                self.active_assistant_id = None
                self.active_assistant_timestamp = None
                self.assistant_content = ""
                self.assistant_reasoning = ""
                return
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

        def insert_tool_call(
            self,
            tool_call_id: str,
            tool_name: str,
            preview: Any,
            args: Any,
        ) -> None:
            if not tool_call_id or not tool_name:
                return
            if self.stage_output:
                self.staged_operations.append(
                    ("tool", tool_call_id, tool_name, preview, args)
                )
                return
            self.ensure_session()
            try:
                payload = json.dumps(
                    _persistable_tool_call(str(tool_name), preview, args),
                    ensure_ascii=False,
                    default=str,
                )
            except Exception:
                payload = None
            try:
                with closing(self._conn()) as conn, conn:
                    ts = _time.time()
                    cur = conn.execute(
                        "INSERT INTO messages"
                        " (session_id, role, content, tool_name, tool_calls, tool_call_id, timestamp)"
                        " VALUES (?, 'assistant', '', ?, ?, ?, ?)",
                        (
                            str(session_id),
                            str(tool_name),
                            payload,
                            str(tool_call_id),
                            ts,
                        ),
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

        def insert_tool_outcome(self, payload: dict[str, Any]) -> None:
            tool_call_id = str(payload.get("tool_call_id") or "").strip()
            tool_name = str(payload.get("name") or "").strip()
            if not tool_call_id or not tool_name:
                return
            if self.stage_output:
                self.staged_operations.append(("tool_outcome", dict(payload)))
                return
            safe = {
                "failure_subsystem": payload.get("failure_subsystem"),
                "error_code": payload.get("error_code"),
                "retryable": payload.get("retryable") is True,
                "is_error": payload.get("is_error") is True,
            }
            try:
                content = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
                with closing(self._conn()) as conn, conn:
                    conn.execute(
                        "INSERT INTO messages"
                        " (session_id, role, content, tool_name, tool_call_id, timestamp)"
                        " VALUES (?, 'tool', ?, ?, ?, ?)",
                        (
                            str(session_id),
                            content,
                            tool_name,
                            tool_call_id,
                            _time.time(),
                        ),
                    )
            except Exception:
                logger.exception("[multitenancy] mirror insert_tool_outcome failed")

        def retag_source(self) -> None:
            if self.stage_output:
                self.staged_operations.append(("retag",))
                return
            if self.retagged:
                return
            try:
                _mark_session_source_feishu(profile_home, str(session_id))
                self.retagged = True
            except Exception:
                logger.exception("[multitenancy] mirror retag_source failed")

        def dedupe(self) -> None:
            if self.stage_output:
                self.staged_operations.append(("dedupe",))
                return
            try:
                with closing(self._conn()) as conn, conn:
                    conn.execute(
                        "DELETE FROM messages WHERE session_id = ? AND id NOT IN ("
                        "SELECT MIN(id) FROM messages WHERE session_id = ? "
                        "GROUP BY role, IFNULL(content,''), IFNULL(tool_name,''),"
                        " IFNULL(tool_call_id,''))",
                        (str(session_id), str(session_id)),
                    )
            except Exception:
                logger.exception("[multitenancy] mirror dedupe failed")

        def record_source_refs(self, source_refs: list[dict[str, Any]]) -> None:
            if self.stage_output:
                self.staged_operations.append(("source_refs", source_refs))
                return
            from ..run_broker import record_current_run_source_refs

            record_current_run_source_refs(source_refs)

        def commit_staged(self) -> None:
            operations = tuple(self.staged_operations)
            self.staged_operations.clear()
            self.stage_output = False
            for operation in operations:
                kind, *args = operation
                if kind == "assistant":
                    content, reasoning, finish_reason = args
                    self.assistant_content = content
                    self.assistant_reasoning = reasoning
                    self.upsert_assistant("", "")
                    self.seal_assistant(finish_reason=finish_reason)
                elif kind == "tool":
                    self.insert_tool_call(*args)
                elif kind == "tool_outcome":
                    self.insert_tool_outcome(*args)
                elif kind == "retag":
                    self.retag_source()
                elif kind == "dedupe":
                    self.dedupe()
                elif kind == "source_refs":
                    self.record_source_refs(args[0])

    _mirror = _StateDbMirror()
    if _stage_codex_output:
        event._trusted_codex_state_commit = _mirror.commit_staged
    # Pre-write the user message so the web-ui shows the question instantly,
    # even if the run later times out / aborts before any reply.
    _mirror.insert_user(user_text)

    # Before the payload snapshot: a codex-mapped run's cwd only reaches the
    # child through `raw_event["workspace"]` (PLAN C6).
    codex_workspace = await asyncio.to_thread(
        _bind_codex_run_workspace, event, profile_home
    )
    # Previous turns' tool transcript (WebUI + trusted actor only; the binder in
    # webui_broker/periphery decides). Resolved HERE, in the parent, because the
    # parent is the only side that knows the runtime, the resume-thread state and
    # the carried counts — so agent.log gets exactly ONE line per run, and the
    # child stays a dumb injector.
    from .. import turn_tool_context as _turn_tool_context
    from . import executor_map as _executor_map_for_carry

    _turn_tool_context.begin_attempt(event)  # new attempt id; supersedes any replay
    # ponytail: presence, not value. `_core.py:3393` (`if local_harness:`) is the
    # ONLY place this attribute is set (and 3455-3456 the only delattr), while
    # the harness's FIRST turn carries `resume_thread_id is None` — so a value
    # test would let turn 1 capture tool bodies into a store the harness can
    # never read back.
    _harness_thread = hasattr(event, "_harness_resume_thread_id")
    _stamped_runtime = ""
    _raw_event_for_runtime = getattr(event, "raw_event", None)
    if isinstance(_raw_event_for_runtime, dict):
        _stamped_runtime = str(
            _raw_event_for_runtime.get(_executor_map_for_carry.EVENT_RUNTIME_KEY) or ""
        )
    event._turn_tool_context_text = _turn_tool_context.resolve_carry_text(
        event,
        runtime=(
            "codex"
            if _stamped_runtime == _executor_map_for_carry.CODEX_APP_SERVER
            else "hermes"
        ),
        # A local-harness Codex thread carries its own full history on the Codex
        # side; injecting would duplicate it and capturing would only fill a
        # store that is never read.
        harness_thread=_harness_thread,
    )
    payload = json.dumps(
        _event_to_subprocess_payload(event, profile_home, messages=messages),
        ensure_ascii=False,
    ).encode("utf-8")
    timeout_s = float(os.getenv("HERMES_AIAGENT_SUBPROCESS_TIMEOUT", "300"))
    # tool_call_id -> (name, monotonic start); named in the stall kill below.
    inflight_tools: dict[str, tuple[str, float]] = {}
    approval_dir = Path(tempfile.mkdtemp(prefix="hermes-mt-approval-"))
    warm_run = None
    warm_worker_requested = _aiagent_warm_worker_enabled()
    if warm_worker_requested:
        try:
            warm_run = await _get_aiagent_warm_worker(profile_home).acquire_run()
        except Exception:
            logger.warning(
                "[multitenancy] AIAgent warm worker slot unavailable; falling back to one-shot subprocess",
                exc_info=True,
            )
            warm_worker_requested = False
    env_scope_entered = False
    try:
        env_scope_kwargs: dict[str, Any] = {
            "approval_dir": approval_dir,
            "event_stream": True,
        }
        if codex_workspace is not None:
            env_scope_kwargs["codex_workspace"] = codex_workspace
        env_scope = _pkg._aiagent_subprocess_env_scope(
            event,
            profile_home,
            **env_scope_kwargs,
        )
        env = env_scope.__enter__()
        env_scope_entered = True
    except Exception:
        if warm_run is not None:
            await warm_run.close()
        try:
            import shutil

            shutil.rmtree(approval_dir, ignore_errors=True)
        except Exception:
            pass
        raise
    # Resolve symlinks so sandbox-exec's path-based allow rules match.
    # The plugin is typically loaded via a profile-local symlink
    # (~/.hermes/profiles/<p>/plugins/multitenancy → ~/code/hermes-multitenancy/),
    # but the sandbox policy only whitelists the resolved repo path.
    # Without .resolve() the child python sees an [Errno 1] Operation not
    # permitted when trying to open aiagent_subprocess.py through the symlink.
    child_script = Path(__file__).parent.with_name("aiagent_subprocess.py").resolve()
    cmd = _wrap_with_sandbox([sys.executable, str(child_script)], profile_home)

    started_at = time.monotonic()
    wall_started_at = time.time()
    proc = None
    stderr_task = None
    using_warm_worker = False
    saw_done = False
    first_event_logged = False

    async def _start_one_shot_reader():
        nonlocal proc, stderr_task
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
            limit=_AIAGENT_STREAM_LIMIT,
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
        return proc.stdout.readline

    async def _start_warm_reader():
        nonlocal warm_run, using_warm_worker
        if warm_run is None:
            warm_run = await _get_aiagent_warm_worker(profile_home).start_run(payload, env, timeout_s)
        else:
            await warm_run.start(payload, env, timeout_s)
        using_warm_worker = True
        logger.info(
            "[multitenancy] AIAgent warm worker dispatched profile_home=%s elapsed=%.3fs",
            profile_home,
            time.monotonic() - started_at,
        )
        return warm_run.readline

    try:
        if warm_worker_requested:
            try:
                read_line = await _start_warm_reader()
            except Exception:
                logger.warning(
                    "[multitenancy] AIAgent warm worker unavailable; falling back to one-shot subprocess",
                    exc_info=True,
                )
                await _discard_aiagent_warm_worker(profile_home)
                if warm_run is not None:
                    await warm_run.close()
                using_warm_worker = False
                warm_run = None
                read_line = await _start_one_shot_reader()
        else:
            read_line = await _start_one_shot_reader()

        first_heartbeat_s = float(os.getenv("HERMES_AIAGENT_FIRST_EVENT_HEARTBEAT_SECONDS", "1"))
        heartbeat_s = float(os.getenv("HERMES_AIAGENT_WAIT_HEARTBEAT_SECONDS", "15"))
        heartbeat_count = 0
        content_sanitizer = _ProtocolPlaceholderStreamSanitizer()
        while True:
            read_started = time.monotonic()
            read_task = asyncio.create_task(read_line())
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
                    if inflight_tools:
                        _tool_name, _tool_started = min(
                            inflight_tools.values(), key=lambda entry: entry[1]
                        )
                        phase = f"工具 {_tool_name} 已运行 {time.monotonic() - _tool_started:.0f} 秒"
                    elif first_event_logged:
                        phase = "等待当前工具或子任务输出"
                    else:
                        phase = "准备响应"
                    # The status event itself carries only an animation marker
                    # (_animated_stream_status drops the phase by design); the
                    # running tool + elapsed seconds are observable HERE.
                    logger.info(
                        "[multitenancy] waiting for AIAgent subprocess stream event elapsed=%.3fs heartbeat=%s phase=%s",
                        total_elapsed,
                        heartbeat_count,
                        phase,
                    )
                    yield (
                        "status",
                        _animated_stream_status(phase, heartbeat_count),
                    )
                try:
                    line = read_task.result()
                except Exception:
                    if using_warm_worker and not first_event_logged:
                        logger.warning(
                            "[multitenancy] AIAgent warm worker failed before first stream event; falling back to one-shot subprocess",
                            exc_info=True,
                        )
                        await _discard_aiagent_warm_worker(profile_home)
                        if warm_run is not None:
                            await warm_run.close()
                        warm_run = None
                        using_warm_worker = False
                        read_line = await _start_one_shot_reader()
                        continue
                    raise
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
                logger.debug(
                    "[multitenancy] ignoring non-json child stream line: %r",
                    _redact_billing_runtime_text(text, event, env)[-500:],
                )
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
                    raise _subprocess_failure(
                        "AIAgent subprocess failed: "
                        f"{_redact_billing_runtime_text(data['error'], event, env)}",
                        retry_safe=data.get("billing_retry_safe") is True,
                        error_code=data.get("error_code"),
                        failure_subsystem=data.get("failure_subsystem"),
                        retryable=data.get("retryable"),
                    )
                logger.info(
                    "[multitenancy] AIAgent subprocess done elapsed=%.3fs result_len=%s",
                    time.monotonic() - started_at,
                    len(str(data.get("result") or "")),
                )
                raw_done_text = _redact_billing_runtime_text(
                    data.get("result"), event, env
                )
                pending_text = content_sanitizer.finish()
                if (
                    pending_text
                    and _EMPTY_MESSAGE_PROTOCOL_PLACEHOLDER
                    not in pending_text + raw_done_text
                ):
                    _mirror.upsert_assistant(pending_text, "")
                    yield "content", pending_text
                # Seal any trailing assistant chunk, retag source to feishu,
                # and dedupe against whatever Hermes core's own end-of-run
                # write inserted.
                _mirror.seal_assistant(finish_reason="stop")
                _mirror.retag_source()
                _mirror.dedupe()
                usage = data.get("usage")
                if isinstance(usage, dict):
                    event._trusted_codex_usage = dict(usage)
                _write_token_ledger_from_child(event, profile_home, usage)
                try:  # shim injects the name (agent_real/__init__ step-2); guard even NameError
                    _bump_expert_usage_from_event(event)
                except Exception:
                    logger.debug("expert usage bump call failed", exc_info=True)
                yield "done", _strip_empty_message_protocol_placeholder(raw_done_text)
                continue
            if event_name == "content":
                content_text = content_sanitizer.feed(
                    _redact_billing_runtime_text(data.get("text"), event, env)
                )
                if content_text:
                    _mirror.upsert_assistant(content_text, "")
                    yield "content", content_text
            elif event_name == "source_refs":
                from ..source_envelope import normalize_tool_source_refs

                source_refs = normalize_tool_source_refs(
                    {"source_refs": data.get("source_refs")},
                    profile_home,
                )
                if source_refs:
                    _mirror.record_source_refs(source_refs)
            elif event_name == "thinking":
                thinking_text = _redact_billing_runtime_text(
                    data.get("text"), event, env
                )
                _mirror.upsert_assistant("", thinking_text)
                yield "thinking", thinking_text
            elif event_name == "tool_transcript":
                # PRIVATE. Consumed into gateway memory for the next turn and
                # NEVER yielded: periphery forwards whatever this generator
                # yields straight into RunEvent/SSE, so yielding here would put
                # a tool body on the wire. Also never mirrored to state.db.
                _turn_tool_context.record_transcript(
                    event, {k: v for k, v in data.items() if k != "event"}
                )
            elif event_name in {
                "tool_started",
                "tool_completed",
                "approval_required",
                "approval_resolved",
                "gate_required",
                "gate_resolved",
                "workflow_stage",
                "auth_required",
                "auth_resolved",
                "clarify_required",
                "clarify_resolved",
                "harness_thread_bound",
            }:
                payload_data = _redact_ingest_runtime_value(
                    {k: v for k, v in data.items() if k != "event"},
                    event,
                )
                if event_name == "tool_started":
                    pending_text = content_sanitizer.finish()
                    if pending_text:
                        _mirror.upsert_assistant(pending_text, "")
                        yield "content", pending_text
                    # Seal any pre-tool assistant text into its own row, then
                    # mirror the tool invocation. First tool-start is also
                    # the safest moment to retag — Hermes core has had time
                    # to insert the sessions row by now.
                    _mirror.seal_assistant(finish_reason="tool_calls")
                    _mirror.insert_tool_call(
                        str(payload_data.get("tool_call_id") or ""),
                        str(payload_data.get("name") or ""),
                        payload_data.get("preview"),
                        payload_data.get("args"),
                    )
                    _mirror.retag_source()
                    if payload_data.get("tool_call_id"):
                        # setdefault: a duplicate start must not reset the clock.
                        inflight_tools.setdefault(
                            str(payload_data["tool_call_id"]),
                            (str(payload_data.get("name") or ""), time.monotonic()),
                        )
                elif event_name == "tool_completed":
                    inflight_tools.pop(str(payload_data.get("tool_call_id") or ""), None)
                    # Tool finished — subsequent assistant text is a new
                    # bubble. Seal so upsert starts a fresh row.
                    _mirror.seal_assistant()
                    _mirror.insert_tool_outcome(payload_data)
                yield str(event_name), payload_data
            elif event_name == "tool_heartbeat":
                # PRIVATE liveness from agent_real/tool_heartbeat.py: arriving
                # at all is what resets the watchdog above. Never yielded,
                # never mirrored — the card already animates its own status.
                logger.debug("[multitenancy] tool heartbeat: %s", data.get("inflight"))
            else:
                logger.debug("[multitenancy] ignoring unknown child stream event: %s", event_name)

        if not using_warm_worker:
            returncode = await asyncio.wait_for(proc.wait(), timeout=5)
            stderr_text = (await stderr_task).decode("utf-8", errors="replace").strip()
            redacted_stderr_text = _redact_billing_runtime_text(
                stderr_text, event, env
            )
            if stderr_text:
                logger.debug("[multitenancy] AIAgent subprocess stderr: %s", redacted_stderr_text[-4000:])
            logger.info(
                "[multitenancy] AIAgent subprocess exited returncode=%s elapsed=%.3fs",
                returncode,
                time.monotonic() - started_at,
            )
            if returncode != 0:
                # ponytail: the ``done`` event IS the child's terminal outcome
                # report — a failed turn arrives as done+error and already
                # raised above. A non-zero exit *after* done therefore carries
                # no user-facing meaning: the answer was produced and streamed.
                # Raising anyway turned self-healed turns (core strips a
                # rejected encrypted-reasoning replay and retries, then the
                # child is SIGTERMed by an external process-group signal during
                # teardown) into red error bubbles AND discarded the finished
                # answer, because stream_run_agent drops ``final_text`` on any
                # exception. Log loudly, surface nothing.
                if saw_done:
                    # Keeping the turn is right either way, but the two signs
                    # mean very different things operationally: a signal is
                    # routine gateway teardown, while a non-zero *code* after a
                    # successful done means the child crashed during its own
                    # teardown — a real defect someone must look at. Full
                    # stderr in both cases so triage never needs DEBUG.
                    log_post_done = logger.warning if returncode < 0 else logger.error
                    log_post_done(
                        "[multitenancy] AIAgent subprocess exited %s AFTER delivering its "
                        "done event; keeping the completed turn instead of surfacing an "
                        "error. Full stderr: %s",
                        returncode,
                        redacted_stderr_text or "<empty>",
                    )
                elif returncode < 0:
                    # Killed by signal -returncode, with no result delivered.
                    # In prod this is always the gateway going down: systemd
                    # SIGTERMs the whole process group on deploy/restart and
                    # every in-flight child dies with it (the child is spawned
                    # without start_new_session, so it shares the group). A
                    # *sandboxed* child that dies by signal surfaces as bwrap's
                    # 128+N, i.e. positive — a negative code really does mean
                    # our own direct child was signalled.
                    #
                    # The stderr tail is unrelated output from seconds earlier,
                    # so splicing it into the user-facing message lies about the
                    # cause: on 2026-07-30 a stale "Encrypted reasoning replay
                    # was rejected" warning sent everyone chasing a model-compat
                    # bug that did not exist. Full stderr stays in the log.
                    logger.warning(
                        "[multitenancy] AIAgent subprocess killed by signal %s "
                        "(gateway restart/shutdown kills in-flight runs); the full "
                        "stderr below is stale output, NOT the cause of death: %s",
                        -returncode,
                        redacted_stderr_text or "<empty>",
                    )
                    raise _gateway_restart_interrupted(-returncode)
                else:
                    raise RuntimeError(
                        f"AIAgent subprocess exited {returncode}: {redacted_stderr_text[-1000:]}"
                    )
        if not saw_done:
            raise RuntimeError("AIAgent subprocess stream ended without done event")
        # GitLab credential-delegation marker: the child (credential_tool) writes
        # into the profile's own tmp/ because the parent's mkdtemp approval dir is
        # invisible inside bwrap. Surface it as the standard auth_required delta so
        # the Feishu router pushes the delegation card to the initiator's DM.
        try:
            from ..credential_delegation import (
                DELEGATION_NONCE_ENV,
                take_auth_required_marker,
            )

            # Only THIS run's marker — the nonce was minted for this spawn, so a
            # sibling run of the same group profile can neither be read nor
            # unlinked here.
            _delegation_signal = take_auth_required_marker(
                profile_home,
                since=wall_started_at,
                nonce=env.get(DELEGATION_NONCE_ENV, ""),
            )
        except Exception:
            _delegation_signal = None
            logger.debug(
                "[multitenancy] delegation marker read failed", exc_info=True
            )
        if _delegation_signal:
            yield "auth_required", _delegation_signal
    except asyncio.TimeoutError as exc:
        killed_at = time.monotonic()
        # Oldest first: the oldest call names the kill; every call gets closed.
        stalled_calls = sorted(inflight_tools.items(), key=lambda kv: kv[1][1])
        stall = AiagentToolStallTimeout(timeout_s)
        if stalled_calls:
            call_id, (name, started) = stalled_calls[0]
            stall = AiagentToolStallTimeout(
                timeout_s,
                tool_call_id=call_id,
                tool_name=name,
                elapsed_s=killed_at - started,
            )
        if using_warm_worker:
            await _discard_aiagent_warm_worker(profile_home)
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        # The only durable trace of a stall kill: _core swallows the exception
        # into a user notice once content has streamed (prod 2026-09-03 had
        # zero log lines for a 6-minute dead turn). Contracted logger name for
        # incident tooling: hermes_multitenancy.agent_real (SPEC 我拍的数).
        logging.getLogger("hermes_multitenancy.agent_real").warning(
            "[multitenancy] AIAgent subprocess stalled and was killed: timeout=%gs tool=%s tool_call_id=%s elapsed=%s inflight=%d profile=%s",
            timeout_s,
            stall.tool_name or "<none>",
            stall.tool_call_id or "-",
            f"{stall.elapsed_s:.0f}s" if stall.elapsed_s is not None else "-",
            len(stalled_calls),
            profile_home.name,
        )
        if stalled_calls:
            # Close EVERY dangling tool_call in state.db so the next turn's
            # model sees interrupted results instead of inventing why they
            # are missing (concurrent tool calls can all be in flight).
            _mirror.seal_assistant()
            for call_id, (name, _started) in stalled_calls:
                if not name:
                    continue
                _mirror.insert_tool_outcome(
                    {
                        "tool_call_id": call_id,
                        "name": name,
                        "is_error": True,
                        "failure_subsystem": "runtime",
                        "error_code": "TOOL_INTERRUPTED",
                        "retryable": True,
                    }
                )
        raise stall from exc
    except asyncio.CancelledError:
        if using_warm_worker:
            await _discard_aiagent_warm_worker(profile_home)
        if proc is not None:
            proc.kill()
            await proc.wait()
        raise
    except Exception:
        if using_warm_worker and not saw_done:
            await _discard_aiagent_warm_worker(profile_home)
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    finally:
        try:
            # A run that never reported done has lost its consumer, but the warm
            # worker keeps executing it: GeneratorExit from an abandoned stream
            # (the user sends a new message, or the generator is GC-aclosed)
            # skips every except branch above and lands straight here. Exiting
            # the env scope below closes this turn's lark-cli auth broker — a
            # random-port localhost server — so an orphan run would keep calling
            # lark-cli against a dead port for the rest of its life and surface
            # as `dial tcp 127.0.0.1:<port>: connect: connection refused`.
            #
            # Kill the orphan instead of keeping its broker alive: the broker
            # carries the turn's frozen identity (sender open_id, allowed bot
            # chats), so outliving the run it was minted for would widen that
            # authorization window. Slot release still happens after the scope
            # exits, so a profile never has two live scopes at once — and the
            # profile lock is still held here, so the worker being discarded is
            # necessarily this run's own (no timeout/steal path to acquire it).
            #
            # The discard sits in its own try/finally: if it raises (a cancelled
            # teardown, a dead loop), the scope must still exit, or the broker
            # leaks and the authorization window survives with no owner — worse
            # than the orphan. Shielded so a cancel landing mid-teardown cannot
            # leave the run half-killed.
            try:
                if using_warm_worker and not saw_done:
                    await asyncio.shield(
                        _discard_aiagent_warm_worker(profile_home)
                    )
            finally:
                if env_scope_entered:
                    await _pkg._exit_aiagent_subprocess_env_scope(
                        env_scope,
                        sys.exc_info(),
                    )
        finally:
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
            if warm_run is not None:
                await warm_run.close()
