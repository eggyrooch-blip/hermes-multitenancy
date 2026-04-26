"""Pre-gateway-dispatch hook callback (sync) + async dispatch entry point.

Wires together: SQLite RoutingTable (production) + in-memory _SPIKE_ROUTING
(fallback) + LRU RuntimePool (cached profile runtimes) + slash command
dispatch (/stop, /status, /new).
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Per-user in-flight dispatch tasks — used by /stop to cancel the right task.
_user_inflight_tasks: dict[str, asyncio.Task] = {}

# Multi-turn session memory: maps (profile_name, user_key) → list of messages.
# user_key prefers union_id (cross-app stable) over the sender id, falling
# back to whichever the event carries. Stored in-memory only; Phase 4 will
# persist via per-profile SessionStore.
_SESSION_HISTORY_MAX = 20  # keep last N messages (user + assistant alternating)
_session_history: dict[tuple[str, str], list[dict]] = {}
# Tracks which (profile, user_key) pairs we've lazy-loaded from SessionStore
# so we only hit SQLite once per pair per process lifetime.
_session_loaded: set[tuple[str, str]] = set()


def _history_key(profile_name: str, sender: str, sender_alt: Optional[str]) -> tuple[str, str]:
    """Return the per-(profile, user) key used to look up conversation history."""
    return (profile_name, sender_alt or sender)


def _trim_history(history: list[dict]) -> list[dict]:
    """Keep at most ``_SESSION_HISTORY_MAX`` most recent messages."""
    if len(history) <= _SESSION_HISTORY_MAX:
        return history
    return history[-_SESSION_HISTORY_MAX:]


def _load_history(key: tuple[str, str]) -> list[dict]:
    """Get history for ``key`` — first call hydrates from SessionStore, subsequent calls hit cache."""
    if key in _session_loaded:
        return list(_session_history.get(key, []))
    store = _get_session_store()
    if store is not None:
        try:
            persisted = store.load_recent(key[0], key[1], _SESSION_HISTORY_MAX)
        except Exception as exc:
            logger.debug("multitenancy: SessionStore.load_recent failed (%s)", exc)
            persisted = []
        if persisted:
            _session_history[key] = persisted
    _session_loaded.add(key)
    return list(_session_history.get(key, []))


def _persist_turn(key: tuple[str, str], user_msg: dict, assistant_text: str) -> None:
    """Append a (user, assistant) turn to both in-memory cache and SessionStore."""
    new_history = _session_history.get(key, []) + [
        user_msg,
        {"role": "assistant", "content": assistant_text},
    ]
    _session_history[key] = _trim_history(new_history)
    store = _get_session_store()
    if store is None:
        return
    try:
        # _build_user_message always sets content as a str — no cast needed.
        store.append(key[0], key[1], user_msg["role"], user_msg["content"])
        store.append(key[0], key[1], "assistant", assistant_text)
    except Exception as exc:
        logger.debug("multitenancy: SessionStore.append failed (%s)", exc)


def _clear_history(key: tuple[str, str]) -> bool:
    """Drop a user's history from cache + store. Returns True if anything was cleared."""
    had_cache = _session_history.pop(key, None) is not None
    _session_loaded.discard(key)
    store = _get_session_store()
    if store is None:
        return had_cache
    try:
        rows = store.clear(key[0], key[1])
    except Exception as exc:
        logger.debug("multitenancy: SessionStore.clear failed (%s)", exc)
        rows = 0
    return had_cache or rows > 0


def _build_user_message(event: Any, *, text_override: Optional[str] = None) -> dict:
    """Construct the OpenAI-format user message, splicing in reply context if any.

    Reply context: hermes mainstream sets ``event.reply_to_text`` when the
    user is quoting an earlier message. We surface it inline so the model
    knows what's being replied to.

    ``text_override`` lets ``_enrich_with_vision`` rewrite the text before
    the message is built (so reply context still wraps the enriched text).
    """
    text = text_override if text_override is not None else (getattr(event, "text", "") or "")
    reply_to_text = getattr(event, "reply_to_text", None)
    if reply_to_text:
        text = f"(replying to: {reply_to_text})\n{text}"
    return {"role": "user", "content": text}


async def _enrich_via_hermes_pipeline(event: Any, gateway: Any) -> Optional[str]:
    """Delegate inbound preprocessing to hermes' ``_prepare_inbound_message_text``.

    This is the single call that mainstream uses to:
      - run vision_analyze_tool on attached images
      - run transcribe_audio on voice messages
      - inject text-file content (.txt / .md / .csv / etc.)
      - prepend reply-quoted context
      - attribute multi-user shared sessions

    By calling the same gateway method, the plugin behaves *identically* to
    mainstream for every multimodal input — no re-implementation, no drift.

    Caveat: this depends on a private GatewayRunner method. If hermes-agent
    refactors ``_prepare_inbound_message_text``, swap to local fallbacks
    (``_local_enrich_with_vision_only`` below as a minimal safety net).

    Returns:
        Enriched text string, or None on failure (caller falls back to event.text).
    """
    if gateway is None:
        return None
    prep = getattr(gateway, "_prepare_inbound_message_text", None)
    if prep is None or not callable(prep):
        logger.debug("multitenancy: gateway._prepare_inbound_message_text unavailable")
        return await _local_enrich_with_vision_only(event)
    source = getattr(event, "source", None)
    if source is None:
        return None
    try:
        return await prep(event=event, source=source, history=[])
    except Exception as exc:
        logger.debug("multitenancy: gateway._prepare_inbound_message_text failed (%s)", exc)
        return await _local_enrich_with_vision_only(event)


async def _local_enrich_with_vision_only(event: Any) -> Optional[str]:
    """Local fallback if hermes' ``_prepare_inbound_message_text`` is unavailable.

    Only handles images (the most common multimodal input). Audio / files /
    reply context degrade gracefully — the model will see ``event.text`` only.
    """
    media_urls = getattr(event, "media_urls", None) or []
    media_types = getattr(event, "media_types", None) or []
    if not media_urls:
        return None
    try:
        from tools.vision_tools import vision_analyze_tool  # type: ignore
    except ImportError:
        return None
    import json as _json
    descriptions: list[str] = []
    for path, mtype in zip(media_urls, media_types or [""] * len(media_urls)):
        if mtype and not mtype.startswith("image"):
            continue
        try:
            result_json = await vision_analyze_tool(
                image_url=path,
                user_prompt="Describe this image in thorough detail.",
            )
            result = _json.loads(result_json) if isinstance(result_json, str) else result_json
            if result.get("success"):
                descriptions.append(f"[Image: {result.get('analysis', '')}]")
        except Exception as exc:
            logger.debug("multitenancy: local vision fallback error on %s: %s", path, exc)
    if not descriptions:
        return None
    base = getattr(event, "text", "") or ""
    return "\n".join(descriptions) + ("\n" + base if base else "")


# -- Hook entry point --------------------------------------------------------


def on_pre_gateway_dispatch(*, event: Any, gateway: Any, session_store: Any = None, **_kwargs) -> dict:
    """Sync hook callback (registered to ``pre_gateway_dispatch``).

    Schedules the async work as a background task and returns immediately
    with ``action: skip`` so the gateway main flow halts for this event.
    """
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(handle_async(event=event, gateway=gateway))
        task.add_done_callback(_log_task_failure)
    except RuntimeError:
        # Test-only fallback: hook called from sync context (no running loop).
        if os.environ.get("PYTEST_CURRENT_TEST"):
            try:
                asyncio.run(handle_async(event=event, gateway=gateway))
            except Exception as exc:
                logger.warning("multitenancy: sync fallback dispatch failed: %s", exc)
        else:
            logger.error(
                "multitenancy: pre_gateway_dispatch invoked without a running loop "
                "and not in pytest — dropping event"
            )
    except Exception as exc:
        logger.warning("multitenancy: failed to schedule handle_async: %s", exc)
    return {"action": "skip", "reason": "multitenancy router took over"}


# -- Async dispatch ----------------------------------------------------------


async def handle_async(*, event: Any, gateway: Any) -> None:
    """Async dispatch — orchestrates routing + pool + adapter calls + commands."""
    from .commands import parse_command

    try:
        source = getattr(event, "source", None)
        chat_id = getattr(source, "chat_id", "unknown") if source else "unknown"
        sender = getattr(source, "user_id", "unknown") if source else "unknown"
        text = getattr(event, "text", "") or ""

        sender_alt = getattr(source, "user_id_alt", None) if source else None

        # Slash command short-circuit (resolve route first so /status / /new
        # know which profile's history to inspect). When _resolve_route signals
        # a miss with profile_home=None, surface profile_name=None so command
        # handlers reply "未路由" instead of leaking the sender id.
        cmd_pair = parse_command(text)
        if cmd_pair is not None:
            cmd_profile_name, cmd_profile_home = _resolve_route(sender, alt_id=sender_alt)
            cmd_profile = cmd_profile_name if cmd_profile_home is not None else None
            await _handle_command(cmd_pair, sender, sender_alt, cmd_profile, chat_id, gateway)
            return

        # Routing: SQLite table first, then in-memory spike fallback.
        profile_name, profile_home = _resolve_route(sender, alt_id=sender_alt)
        if profile_home is None:
            logger.info("multitenancy: no route for sender=%s, ignoring", sender)
            return

        # Register self in the user's in-flight slot (replace previous)
        current = asyncio.current_task()
        prev = _user_inflight_tasks.get(sender)
        if prev is not None and not prev.done() and prev is not current:
            prev.cancel()
        if current is not None:
            _user_inflight_tasks[sender] = current

        adapter = _get_feishu_adapter(gateway)
        # Detect whether adapter supports the streaming/reaction APIs we use.
        # Real FeishuAdapter does; unit-test mocks typically don't.
        feishu_full = (
            adapter is not None
            and hasattr(adapter, "edit_message")
            and hasattr(adapter, "on_processing_start")
            and hasattr(adapter, "on_processing_complete")
        )

        outcome_failed = False
        if feishu_full:
            try:
                await adapter.on_processing_start(event)
            except Exception as exc:
                logger.debug("multitenancy: on_processing_start failed: %s", exc)

        # Multi-modal enrichment — delegate to hermes' canonical pipeline so
        # vision (images), STT (audio), text-file inject (.txt/.md/.csv etc.),
        # reply-context wrapping, and multi-user attribution all behave EXACTLY
        # like mainstream. Falls back to local vision-only on missing API.
        enriched_text = await _enrich_via_hermes_pipeline(event, gateway)

        # Build the conversation: prior history + current user message (with
        # reply context spliced in). The runner prepends the profile's SOUL.
        # First lookup for a (profile, user) pair hydrates from SessionStore.
        hist_key = _history_key(profile_name, sender, sender_alt)
        prior = _load_history(hist_key)
        user_msg = _build_user_message(event, text_override=enriched_text)
        conversation = prior + [user_msg]

        try:
            if feishu_full:
                # Streaming path — placeholder + edit_message typewriter
                response_text = await _stream_into_feishu(
                    adapter, chat_id, profile_name, profile_home, event,
                    messages=conversation,
                )
            else:
                # Mock / minimal adapter — old non-stream path (send_typing + pool.dispatch + send)
                if adapter is not None:
                    await _safe_call(adapter.send_typing, chat_id)
                response_text = await _get_pool().dispatch(profile_name, profile_home, event)
                if adapter is not None:
                    await _safe_call(adapter.send, chat_id, response_text)

            # Record turn into history + persist to SessionStore.
            if response_text and isinstance(response_text, str):
                _persist_turn(hist_key, user_msg, response_text)

            _touch_route(sender, sender_alt)
        except Exception:
            outcome_failed = True
            raise
        finally:
            if feishu_full:
                try:
                    from gateway.platforms.base import ProcessingOutcome  # type: ignore
                    out = ProcessingOutcome.FAILURE if outcome_failed else ProcessingOutcome.SUCCESS
                    await adapter.on_processing_complete(event, out)
                except Exception as exc:
                    logger.debug("multitenancy: on_processing_complete failed: %s", exc)
            if _user_inflight_tasks.get(sender) is current:
                _user_inflight_tasks.pop(sender, None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("multitenancy: handle_async failed: %s", exc)


# -- Command dispatch --------------------------------------------------------


async def _handle_command(
    pair: tuple[str, str],
    sender: str,
    sender_alt: Optional[str],
    profile_name: Optional[str],
    chat_id: str,
    gateway: Any,
) -> None:
    """Execute a parsed slash command and reply via the shared adapter."""
    cmd, _args = pair
    adapter = _get_feishu_adapter(gateway)

    if cmd == "stop":
        task = _user_inflight_tasks.pop(sender, None)
        if task is not None and not task.done():
            task.cancel()
            reply = "已停止当前任务"
        else:
            reply = "没有进行中的任务"
    elif cmd == "status":
        task = _user_inflight_tasks.get(sender)
        running = task is not None and not task.done()
        # Surface session memory size + profile so the user knows their context.
        if profile_name:
            hist = _session_history.get(_history_key(profile_name, sender, sender_alt), [])
            hist_len = len(hist)
        else:
            hist_len = 0
        reply = (
            f"状态: {'运行中' if running else '空闲'}\n"
            f"profile: {profile_name or '(未路由)'}\n"
            f"会话历史: {hist_len} 条消息"
        )
    elif cmd in ("new", "reset"):
        # Clear this user's per-profile history (cache + persistent SessionStore).
        if profile_name:
            key = _history_key(profile_name, sender, sender_alt)
            had = _clear_history(key)
            reply = "会话已重置 ✅" if had else "会话已重置 (本来也是空的)"
        else:
            reply = "(未路由的用户) 没有历史可重置"
    elif cmd == "help":
        reply = (
            "📖 可用命令\n"
            "/help    — 显示这条帮助\n"
            "/status  — 查看当前 profile + 历史长度\n"
            "/new     — 重置会话历史 (开始新对话)\n"
            "/reset   — /new 的别名\n"
            "/stop    — 取消正在运行的任务\n"
        )
    else:
        return

    if adapter is not None:
        await _safe_call(adapter.send, chat_id, reply)


# -- Routing resolution ------------------------------------------------------


def _resolve_route(sender: str, *, alt_id: Optional[str] = None) -> tuple[str, Optional[Path]]:
    """Resolve sender → (profile_name, profile_home).

    The routing table's ``open_id`` column is overloaded as "any stable user
    identifier" — it can hold a real Feishu open_id (``ou_xxx``), a union_id
    (``on_xxx``), or any other tenant-stable token chosen by feishu-sync.

    Lookup order:
      1. SQLite RoutingTable WHERE open_id = sender (typical: source.user_id)
      2. SQLite RoutingTable WHERE open_id = alt_id (typical: source.user_id_alt = union_id)
      3. In-memory ``_SPIKE_ROUTING`` dict (Phase 1 compat / unit tests)

    Returns (sender, None) when no route hits.
    """
    from .runtime import resolve_profile_home as _spike_resolve

    table = _get_routing_table()
    candidates: list[str] = [sender]
    if alt_id and alt_id != sender:
        candidates.append(alt_id)

    if table is not None:
        for candidate in candidates:
            try:
                row = table.lookup_by_open_id(candidate)
            except Exception as exc:
                logger.debug("multitenancy: routing lookup failed (%s)", exc)
                continue
            if row is not None:
                return (row.profile_name, _profile_name_to_home(row.profile_name))

    # Fallback: in-memory spike routing dict
    for candidate in candidates:
        spike_home = _spike_resolve(candidate)
        if spike_home is not None:
            return (spike_home.name, spike_home)
    return (sender, None)


def _profile_name_to_home(profile_name: str) -> Path:
    """Map profile_name to its on-disk profile home directory.

    Mirrors ``hermes_cli/profiles.py`` convention: ``~/.hermes/profiles/<name>``.
    """
    return Path.home() / ".hermes" / "profiles" / profile_name


def _touch_route(sender: str, sender_alt: Optional[str] = None) -> None:
    """Best-effort last_active_at update; no-op if no SQLite table or row.

    Mirrors ``_resolve_route`` lookup strategy: prefer the alt id (union_id
    on Feishu) since that's what the routing table is keyed by in production.
    Falls back to ``sender`` only when no alt is available.
    """
    table = _get_routing_table()
    if table is None:
        return
    key = sender_alt or sender
    try:
        table.touch_active(key)
    except Exception as exc:
        logger.debug("multitenancy: touch_active failed: %s", exc)


# -- Lazy singletons (RoutingTable + RuntimePool) ----------------------------


_routing_table: Any = None
_routing_db_path: Optional[str] = None
_pool: Any = None
_session_store: Any = None
_session_db_path: Optional[str] = None  # None → DEFAULT_DB_PATH inside SessionStore


def _get_session_store():
    """Lazy-init module-level SessionStore. Returns None if init fails (in-memory only)."""
    global _session_store
    if _session_store is None:
        try:
            from .sessions import SessionStore
            _session_store = SessionStore(_session_db_path)
        except Exception as exc:
            logger.debug("multitenancy: SessionStore init failed (%s)", exc)
            return None
    return _session_store


def override_session_store(store_or_path) -> None:
    """Test helper — inject a SessionStore (or db path string, or None to disable)."""
    global _session_store, _session_db_path, _session_loaded
    if _session_store is not None and _session_store is not store_or_path:
        try:
            _session_store.close()
        except Exception:
            pass
    _session_loaded.clear()
    if store_or_path is None or isinstance(store_or_path, (str, Path)):
        _session_store = None
        _session_db_path = str(store_or_path) if store_or_path is not None else None
    else:
        _session_store = store_or_path
        _session_db_path = None


def _get_routing_table():
    """Lazy-init module-level RoutingTable. Returns None if init fails."""
    global _routing_table
    if _routing_table is None:
        try:
            from .routing import RoutingTable
            _routing_table = RoutingTable(_routing_db_path)
        except Exception as exc:
            logger.debug("multitenancy: RoutingTable init failed (%s)", exc)
            return None
    return _routing_table


def _get_pool():
    """Lazy-init module-level RuntimePool."""
    global _pool
    if _pool is None:
        from .pool import RuntimePool
        _pool = RuntimePool()
    return _pool


def override_routing_table(db_path: Optional[str | Path]) -> None:
    """Test helper — reset the routing-table singleton, optionally pointing it at a different db."""
    global _routing_table, _routing_db_path
    if _routing_table is not None:
        try:
            _routing_table.close()
        except Exception:
            pass
    _routing_table = None
    _routing_db_path = str(db_path) if db_path is not None else None


def override_pool(pool) -> None:
    """Test helper — inject a custom RuntimePool (or None to reset)."""
    global _pool
    _pool = pool


# -- Adapter resolution ------------------------------------------------------


def _get_feishu_adapter(gateway: Any) -> Any:
    """Pull the FeishuAdapter from the gateway, returning None if unavailable."""
    if gateway is None:
        return None
    adapters = getattr(gateway, "adapters", None)
    if adapters is None:
        return None
    try:
        from gateway.platforms.base import Platform  # type: ignore
        result = adapters.get(Platform.FEISHU)
        if result is not None:
            return result
    except Exception as exc:  # pragma: no cover — only triggers when import fails
        logger.debug("multitenancy: Platform import unavailable (%s)", exc)
    if isinstance(adapters, dict):
        return adapters.get("feishu")
    return None


async def _safe_call(fn, *args, **kwargs):
    """Await fn(*args, **kwargs) whether it is sync or async."""
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return await result
    return result


# Detect Feishu rate-limit errors. Strings vary across SDK versions, so we
# match on broad hints rather than exact codes.
_RATE_LIMIT_HINTS = ("429", "rate limit", "too many requests", "ratelimit")


def _is_rate_limit_error(exc: BaseException) -> bool:
    msg = (str(exc) or "").lower()
    return any(h in msg for h in _RATE_LIMIT_HINTS)


async def _edit_with_retry(adapter, chat_id, message_id, content, *, finalize=False):
    """Wrap adapter.edit_message with exponential backoff on 429.

    On 429: backoff 0.5 → 1.0 → 2.0s, max 3 retries (4 total attempts), then
    log a warning and return None so the caller can continue streaming.
    On non-429: 1 retry after 0.2s, then propagate.
    """
    backoffs = (0.5, 1.0, 2.0)
    for attempt in range(4):
        try:
            if finalize:
                try:
                    return await adapter.edit_message(chat_id, message_id, content, finalize=True)
                except TypeError:
                    return await adapter.edit_message(chat_id, message_id, content)
            return await adapter.edit_message(chat_id, message_id, content)
        except Exception as exc:
            if _is_rate_limit_error(exc):
                if attempt < len(backoffs):
                    logger.debug(
                        "multitenancy: edit_message 429, backoff %ss (attempt %d)",
                        backoffs[attempt], attempt + 1,
                    )
                    await asyncio.sleep(backoffs[attempt])
                    continue
                logger.warning("multitenancy: edit_message rate-limited 4x, giving up")
                return None
            if attempt == 0:
                logger.debug("multitenancy: edit_message non-429 retry: %s", exc)
                await asyncio.sleep(0.2)
                continue
            raise
    return None


# Throttle edit_message calls. Hermes mainstream uses 1.5s between edits
# (run.py:9502 _PROGRESS_EDIT_INTERVAL); we mirror that as the floor for the
# content phase. Reasoning phase uses an even-wider 2s heartbeat — the
# preview is decorative, paying RTT every 0.25s makes the bot *feel* slow
# because Feishu edit_message has visible per-call latency.
_STREAM_CONTENT_MIN_CHARS = 60
_STREAM_CONTENT_MIN_SECONDS = 1.0
_STREAM_THINKING_MIN_SECONDS = 2.0


async def _stream_into_feishu(
    adapter, chat_id, profile_name, profile_home, event, *, messages: Optional[list[dict]] = None
) -> str:
    """Stream LLM tokens into a Feishu placeholder via adapter.edit_message.

    Falls back to a single non-streamed send() if streaming returns empty or
    the placeholder send fails. Returns the final concatenated text.

    ``messages`` (optional): full conversation including prior history. When
    omitted the runner builds a single-turn system+user prompt from the event.
    """
    import time
    from .agent_real import stream_run_agent, real_run_agent
    from .runtime import _PROFILE_HOME_VAR

    # Without an adapter we can still produce text (used in unit tests).
    if adapter is None:
        token = _PROFILE_HOME_VAR.set(profile_home)
        try:
            content_parts: list[str] = []
            async for kind, c in stream_run_agent(event, profile_home, messages=messages):
                if kind == "content":
                    content_parts.append(c)
            return (
                "".join(content_parts)
                or await real_run_agent(event, profile_home, messages=messages)
            )
        finally:
            _PROFILE_HOME_VAR.reset(token)

    # 1. Send a placeholder so we have a message_id to edit.
    placeholder = "..."
    placeholder_send = await adapter.send(chat_id, placeholder)
    placeholder_id = placeholder_send.message_id if getattr(placeholder_send, "success", False) else None

    if placeholder_id is None:
        # Couldn't get a message to edit — degrade to one-shot non-stream.
        text = await real_run_agent(event, profile_home, messages=messages)
        await adapter.send(chat_id, text)
        return text

    # 2. Stream into the placeholder, throttled.
    #    Two phases: reasoning ("thinking") shows a heartbeat preview every
    #    ~2s so the user knows the model is alive; once real content starts,
    #    we switch to a 1s / 60-char throttle (mirrors hermes mainstream).
    thinking = ""
    content = ""
    last_edit_time = 0.0
    last_render_len = 0
    content_started = False

    def render() -> str:
        if content:
            return content
        preview = thinking[-160:].strip() if thinking else ""
        return f"💭 思考中…\n{preview}" if preview else "💭 思考中…"

    token = _PROFILE_HOME_VAR.set(profile_home)
    try:
        try:
            async for kind, delta in stream_run_agent(event, profile_home, messages=messages):
                if kind == "thinking":
                    thinking += delta
                else:
                    content += delta
                    if not content_started:
                        # Force an immediate edit on phase transition so the user
                        # sees the answer start the moment reasoning ends.
                        content_started = True
                        try:
                            await _edit_with_retry(adapter, chat_id, placeholder_id, render())
                        except Exception as exc:
                            logger.debug("multitenancy: phase-transition edit failed: %s", exc)
                        last_edit_time = time.monotonic()
                        last_render_len = len(render())
                        continue

                now = time.monotonic()
                rendered = render()
                if content_started:
                    should_edit = (
                        len(rendered) - last_render_len >= _STREAM_CONTENT_MIN_CHARS
                        or now - last_edit_time >= _STREAM_CONTENT_MIN_SECONDS
                    )
                else:
                    # Reasoning phase — heartbeat-only edits, no char threshold.
                    should_edit = now - last_edit_time >= _STREAM_THINKING_MIN_SECONDS
                if should_edit:
                    try:
                        await _edit_with_retry(adapter, chat_id, placeholder_id, rendered)
                    except Exception as exc:
                        logger.debug("multitenancy: edit_message mid-stream failed: %s", exc)
                    last_edit_time = now
                    last_render_len = len(rendered)
        except Exception as exc:
            logger.info("multitenancy: streaming failed (%s) — falling back to non-stream", exc)
            try:
                content = await real_run_agent(event, profile_home, messages=messages)
            except Exception as fallback_exc:
                # Both stream + non-stream LLM paths failed (e.g. region block,
                # exhausted credentials). Surface a user-visible error instead
                # of leaving the "..." placeholder hanging.
                logger.warning("multitenancy: LLM fully unavailable: %s", fallback_exc)
                content = (
                    "⚠️ 模型暂时不可用 (LLM provider rejected the request).\n"
                    "请检查 profile 的 config.yaml 模型/凭据, 或稍后再试。"
                )

    finally:
        _PROFILE_HOME_VAR.reset(token)

    full = content if content else (thinking if thinking else "(empty response)")

    # 3. Final commit edit (finalize=True signals end of stream to Feishu).
    #    _edit_with_retry handles both 429 backoff AND finalize-arg signature drift.
    try:
        await _edit_with_retry(adapter, chat_id, placeholder_id, full, finalize=True)
    except Exception as exc:
        logger.debug("multitenancy: final edit_message failed: %s", exc)

    return full


def _log_task_failure(task: asyncio.Task) -> None:
    """Done-callback for fire-and-forget tasks — surfaces silent exceptions."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("multitenancy: background task crashed: %r", exc)
