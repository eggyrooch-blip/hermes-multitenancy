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
    timeout_s = float(os.getenv("HERMES_AIAGENT_SUBPROCESS_TIMEOUT", "300"))
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
        env_scope = _pkg._aiagent_subprocess_env_scope(
            event,
            profile_home,
            approval_dir=approval_dir,
            event_stream=True,
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
                _write_token_ledger_from_child(event, profile_home, data.get("usage"))
                _bump_expert_usage_from_event(event)
                yield "done", _strip_empty_message_protocol_placeholder(raw_done_text)
                continue
            if event_name == "content":
                content_text = content_sanitizer.feed(
                    _redact_billing_runtime_text(data.get("text"), event, env)
                )
                if content_text:
                    _mirror.upsert_assistant(content_text, "")
                    yield "content", content_text
            elif event_name == "thinking":
                thinking_text = _redact_billing_runtime_text(
                    data.get("text"), event, env
                )
                _mirror.upsert_assistant("", thinking_text)
                yield "thinking", thinking_text
            elif event_name in {
                "tool_started",
                "tool_completed",
                "approval_required",
                "approval_resolved",
                "clarify_required",
                "clarify_resolved",
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
                raise RuntimeError(
                    f"AIAgent subprocess exited {returncode}: {redacted_stderr_text[-1000:]}"
                )
        if not saw_done:
            raise RuntimeError("AIAgent subprocess stream ended without done event")
    except asyncio.TimeoutError as exc:
        if using_warm_worker:
            await _discard_aiagent_warm_worker(profile_home)
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise RuntimeError(
            f"AIAgent subprocess produced no stream events for {timeout_s:g}s"
        ) from exc
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
            if env_scope_entered:
                env_scope.__exit__(*sys.exc_info())
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
