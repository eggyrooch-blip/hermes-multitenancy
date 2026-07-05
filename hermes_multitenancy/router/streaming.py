"""Feishu streaming-delivery subsystem — split out of router god-node (pure move).

All package-level helpers/constants/state are referenced through ``_m`` (the
router shim) at call time so that ``monkeypatch.setattr(router, ...)`` in tests
still takes effect. Sibling stream functions that are patched in tests
(``_update_feishu_stream_target``) are likewise routed through ``_m``.
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Optional

from .. import router as _m


def _clean_stream_display_text(text: str, profile_home: Optional[Path] = None) -> str:
    """Hide native media-delivery directives from visible streaming text."""
    try:
        from gateway.stream_consumer import GatewayStreamConsumer  # type: ignore

        cleaned = GatewayStreamConsumer._clean_for_display(text)
        cleaned = _m._ARTIFACT_JSON_RE.sub("", cleaned)
    except Exception:
        cleaned = str(text or "").replace("[[audio_as_voice]]", "")
        cleaned = _m._ARTIFACT_JSON_RE.sub("", cleaned)
        cleaned = re.sub(r'''[`"']?MEDIA:\s*\S+[`"']?''', "", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = cleaned.rstrip()
    cleaned = cleaned.replace("[[as_document]]", "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).rstrip()
    cleaned = _m._linkify_feishu_document_ids(cleaned)
    if profile_home is not None:
        cleaned = _m._strip_plain_profile_file_paths_for_display(cleaned, profile_home)
        if not Path(profile_home).name.startswith(_m._GROUP_PROFILE_PREFIX):
            cleaned = _m._strip_wrong_lark_cli_bot_identity_note(cleaned)
    return cleaned


def _clean_stream_delta_text(text: str, profile_home: Optional[Path] = None) -> str:
    """Clean one streamed content delta without dropping token boundary spaces."""
    raw = str(text or "")
    cleaned = _clean_stream_display_text(raw, profile_home)
    if cleaned and raw[:1].isspace() and not cleaned[:1].isspace():
        leading = re.match(r"^\s+", raw)
        if leading:
            cleaned = leading.group(0) + cleaned
    return cleaned


def _start_hub_flow_poll(
    *,
    profile_name: str,
    open_id: str,
    profile_dir: Path,
    shared_home: Path,
    chat_id: str,
    gateway: Any,
    hub_card: dict[str, Any],
    flows: dict[str, dict[str, Any]],
    auth_urls: dict[str, str],
    qr_image_keys: dict[str, str],
    ctx: Optional[dict[str, Any]] = None,
) -> None:
    if not flows:
        return
    task = asyncio.create_task(
        _poll_hub_flows(
            profile_name=profile_name,
            open_id=open_id,
            profile_dir=profile_dir,
            shared_home=shared_home,
            chat_id=chat_id,
            gateway=gateway,
            hub_card=hub_card,
            flows=flows,
            auth_urls=auth_urls,
            qr_image_keys=qr_image_keys,
            ctx=ctx,
        ),
        name=f"auth-hub:{profile_name}:{open_id}:{','.join(sorted(flows))}",
    )
    task.add_done_callback(lambda t: _m.logger.debug("auth hub poll task ended: %s", t.get_name()))


async def _poll_hub_flows(
    *,
    profile_name: str,
    open_id: str,
    profile_dir: Path,
    shared_home: Path,
    chat_id: str,
    gateway: Any,
    hub_card: dict[str, Any],
    flows: dict[str, dict[str, Any]],
    auth_urls: dict[str, str],
    qr_image_keys: dict[str, str],
    ctx: Optional[dict[str, Any]] = None,
    adapter: Optional[Any] = None,
) -> None:
    """Poll the started auth flows. ONLY a credential the user actually completes
    produces feedback: its row flips to ✅已认证 via an in-place card update, and
    its button is dropped — every OTHER credential's button is preserved. Flows
    the user never acts on stay silent (no "未完成" noise); a failed attempt keeps
    its button so the user can retry."""
    from .. import credential_hub, credential_hub_auth as cha, feishu_uat_auth
    from ..feishu_auth_cards import send_auth_card, update_auth_card
    from ..feishu_credential_hub_cards import build_hub_card, build_success_card

    # The card-action hub already holds the adapter (SDK callback thread, no
    # gateway handle); the /auth-command path passes gateway and resolves it.
    if adapter is None:
        adapter = _m._get_feishu_adapter(gateway)
    if adapter is None:
        return

    titles = {credential_hub.LARK_CLI: "Lark-cli", credential_hub.FEISHU_PROJECT: "飞书项目",
              credential_hub.KEEP_RECORD: "Keep-record",
              credential_hub.KEP_CLI_ONLINE: "kep-cli online",
              credential_hub.KEP_CLI_PRE: "kep-cli pre"}
    # Entries still offered on re-render. A credential drops out only once it
    # SUCCEEDS — so re-rendering after one completion keeps the others' buttons/QRs.
    remaining_urls = dict(auth_urls or {})
    remaining_qr = dict(qr_image_keys or {})

    async def _fresh_rows() -> list:
        try:
            rows = await asyncio.to_thread(
                credential_hub.collect_credential_statuses,
                profile_name=profile_name, open_id=open_id, home_dir=profile_dir / "home",
            )
            return _m._filter_hub_rows_for_auth(rows)
        except Exception as exc:  # pragma: no cover - defensive
            _m.logger.debug("multitenancy: /auth hub refresh failed (%s)", exc)
            return []

    async def _rerender(rows: list) -> None:
        # When ``ctx`` is set (unified card-action hub), rows without a still-open
        # inline entry render their collapsed 认证/重新认证 callback button; the
        # completed row flips to ✅ and offers 重新认证. Legacy callers pass no
        # ctx and keep the original inline-only re-render.
        await update_auth_card(adapter=adapter, auth_card=hub_card,
                               card=build_hub_card(rows=rows, auth_urls=remaining_urls,
                                                   pending_note={}, qr_image_keys=remaining_qr,
                                                   ctx=ctx))

    pending = dict(flows)
    # Each flow keys off THIS attempt so re-auth of an already-authed credential
    # reports real completion, not a stale "already logged in". ~200 iterations
    # (10 min) — lark's device-flow OAuth needs the user to switch to a
    # browser and approve, which routinely takes well over the old 120s
    # window; keep's login-wait blocks ~15s/iter (QR window is minutes).
    for _ in range(200):
        if not pending:
            break
        succeeded: list[str] = []
        for cid, desc in list(pending.items()):
            try:
                if desc["kind"] == "lark":
                    s = await asyncio.to_thread(feishu_uat_auth.poll_session,
                                                session_id=desc["session_id"],
                                                profile_name=profile_name, open_id=open_id)
                    st = str(s.get("status") or "")
                    if st == "success":
                        succeeded.append(cid); pending.pop(cid, None)
                    elif st != "pending":  # expired/error: stop polling, keep button for retry
                        pending.pop(cid, None)
                elif desc["kind"] == "keep":
                    r = await asyncio.to_thread(cha.poll_keep_record_once, profile_dir, desc["qrcode_id"])
                    if r.get("status") == "authorized":
                        succeeded.append(cid); pending.pop(cid, None)
                    # not-yet-scanned → still pending; keep polling, no message
                elif desc["kind"] == "kep":
                    proc = desc.get("proc")
                    rc = proc.poll() if proc is not None else 0
                    if rc is not None:  # login proc exited
                        ok = await asyncio.to_thread(
                            cha.kep_cli_logged_in,
                            profile_dir,
                            profile_name,
                            shared_home,
                            env_name=str(desc.get("env") or "online"),
                        )
                        if rc == 0 and ok:
                            succeeded.append(cid)
                        pending.pop(cid, None)  # proc finished either way; keep button if it failed
            except Exception as exc:  # stop polling a broken flow; keep its button, stay silent
                _m.logger.debug("multitenancy: /auth flow %s poll error (%s)", cid, exc)
                pending.pop(cid, None)
        if succeeded:
            for cid in succeeded:
                remaining_urls.pop(cid, None)  # drop only the completed credential's entry
                remaining_qr.pop(cid, None)
            rows = await _fresh_rows()
            # Push a green 认证成功 card per completed credential (card-style feedback)…
            for cid in succeeded:
                row = next((r for r in rows if r.id == cid), None)
                expiry = credential_hub.human_expiry(row.expires_at) if row else ""
                await send_auth_card(adapter=adapter, chat_id=chat_id,
                                     card=build_success_card(titles.get(cid, cid), expiry_zh=expiry))
            # …and update the hub in place (completed row → ✅已认证, other buttons kept).
            await _rerender(rows)
        if pending:
            await asyncio.sleep(3)
    # Un-acted flows: kill any lingering kep login proc so it doesn't run forever.
    for desc in pending.values():
        proc = desc.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def _adapter_supports_streaming_card(adapter) -> bool:
    """Return True when the shared Feishu adapter can drive card streaming."""
    if adapter is None:
        return False
    try:
        from ..feishu_cardkit_compat import ensure_feishu_cardkit_streaming

        ensure_feishu_cardkit_streaming(adapter)
    except Exception as exc:
        _m.logger.debug("multitenancy: Feishu CardKit compat install skipped: %s", exc)
    supports = getattr(adapter, "supports_streaming_card", None)
    if callable(supports):
        try:
            return bool(supports())
        except Exception as exc:
            _m.logger.debug("multitenancy: supports_streaming_card failed: %s", exc)
            return False
    return bool(getattr(adapter, "SUPPORTS_STREAMING_CARD", False))


async def _start_feishu_stream_target(
    adapter,
    chat_id,
    *,
    reply_to: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> tuple[str, Optional[str]]:
    """Start a card stream when possible, otherwise create the text placeholder.

    Reply-mode note: transport here is chosen up-front by CAPABILITY
    (``_adapter_supports_streaming_card``), before the reply text exists — the
    card is opened immediately so the user sees a "Generating…" surface while
    content streams in. The content-based predicate ``should_use_card``
    (card/card_error.py) therefore cannot gate this path: it needs the complete
    text, which only exists after the stream finishes. Sending a plain reply as a
    STATIC message instead of a card would require a CORE seam in
    ``gateway.stream_consumer.GatewayStreamConsumer.ensure_streaming_card_started``
    (or the core ``FeishuAdapter``) to defer transport until the buffer is known.
    Until core exposes that, ``should_use_card`` stays unit-tested but unwired.
    See tests/test_streaming_card_transport.py
    ::test_plaintext_reply_still_uses_card_because_core_owns_reply_mode.
    """
    if _adapter_supports_streaming_card(adapter):
        starter = getattr(adapter, "start_streaming_card", None)
        updater = getattr(adapter, "update_streaming_card", None)
        if callable(starter) and callable(updater):
            try:
                result = await starter(chat_id=chat_id, reply_to=reply_to, metadata=metadata)
            except Exception as exc:
                _m.logger.debug("multitenancy: start_streaming_card failed: %s", exc)
            else:
                message_id = getattr(result, "message_id", None)
                if getattr(result, "success", False) and message_id:
                    _m.logger.info("multitenancy: streaming_card started message_id=%s", message_id)
                    return ("card", str(message_id))
                _m.logger.debug(
                    "multitenancy: start_streaming_card unsuccessful: %s",
                    getattr(result, "error", None),
                )

    placeholder_send = await adapter.send(chat_id, _m._STREAM_INVISIBLE_PLACEHOLDER, reply_to=reply_to, metadata=metadata)
    message_id = (
        placeholder_send.message_id
        if getattr(placeholder_send, "success", False)
        else None
    )
    return ("edit", str(message_id) if message_id else None)


async def _update_feishu_stream_target(
    adapter, chat_id, message_id, content, *, mode: str, finalize: bool = False
):
    """Update the current Feishu streaming surface."""
    if mode == "card":
        result = await adapter.update_streaming_card(
            chat_id=chat_id,
            message_id=message_id,
            content=content,
            finalize=finalize,
        )
        if not getattr(result, "success", False):
            _m.logger.debug(
                "multitenancy: update_streaming_card unsuccessful: %s",
                getattr(result, "error", None),
            )
        return result
    return await _m._edit_with_retry(
        adapter, chat_id, message_id, content, finalize=finalize
    )


async def _abort_feishu_stream_target(
    adapter, chat_id, message_id, content, *, mode: str
):
    """Force the current streaming surface into an aborted terminal state."""
    if mode == "card":
        aborter = getattr(adapter, "abort_streaming_card", None)
        if callable(aborter):
            result = await aborter(
                chat_id=chat_id,
                message_id=message_id,
                content=content,
            )
            if not getattr(result, "success", False):
                _m.logger.debug(
                    "multitenancy: abort_streaming_card unsuccessful: %s",
                    getattr(result, "error", None),
                )
            return result
    return await _m._update_feishu_stream_target(
        adapter,
        chat_id,
        message_id,
        content or "Aborted.",
        mode=mode,
        finalize=True,
    )


async def _run_terminal_stream_update(update_coro, *, label: str):
    """Run terminal card/edit update even if the caller is being cancelled."""
    task = asyncio.create_task(update_coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            result = await task
            _m.logger.info("multitenancy: %s completed while task was cancelling", label)
            return result
        except Exception as exc:
            _m.logger.debug("multitenancy: %s failed while task was cancelling: %s", label, exc)
        raise


def _resolve_stream_footer_model_name(profile_home: Path) -> Optional[str]:
    from ..agent_real import _load_profile_config

    config = _load_profile_config(Path(profile_home).expanduser())
    default_spec = str(((config.get("model") or {}).get("default") or "")).strip()
    if not default_spec:
        return None
    model_name = default_spec.rsplit("/", 1)[-1].strip()
    return model_name or None


def _merge_stream_footer_metrics(
    adapter: Any,
    *,
    mode: str,
    message_id: Optional[str],
    profile_home: Path,
) -> None:
    if mode != "card" or not message_id:
        return
    if not callable(getattr(adapter, "update_streaming_card_metrics", None)):
        return
    try:
        adapter.update_streaming_card_metrics(
            message_id=message_id,
            model_name=_resolve_stream_footer_model_name(profile_home),
        )
    except Exception as exc:
        _m.logger.debug("multitenancy: stream footer metrics merge failed: %s", exc)


async def _update_feishu_stream_reasoning(
    adapter, chat_id, message_id, content, *, mode: str
):
    """Update reasoning/status text without polluting the final answer block."""
    if mode == "card":
        updater = getattr(adapter, "update_streaming_card_reasoning", None)
        if callable(updater):
            return await updater(
                chat_id=chat_id,
                message_id=message_id,
                content=content,
            )
    return await _m._update_feishu_stream_target(
        adapter, chat_id, message_id, content, mode=mode
    )


async def _update_feishu_stream_status(
    adapter, chat_id, message_id, content, *, mode: str
):
    """Update an ephemeral in-progress status without retaining it as reasoning."""
    if mode == "card":
        updater = getattr(adapter, "update_streaming_card_status", None)
        if callable(updater):
            return await updater(
                chat_id=chat_id,
                message_id=message_id,
                content=content,
            )
    return await _update_feishu_stream_reasoning(
        adapter, chat_id, message_id, content, mode=mode
    )


async def _update_feishu_stream_tool_event(
    adapter, chat_id, message_id, payload, *, mode: str, completed: bool, profile_home: Optional[Path] = None
):
    """Update active/completed tool state on the streaming surface."""
    payload = _m._sanitize_tool_event_payload(payload, profile_home)
    tool_name = str(payload.get("name") or payload.get("tool_name") or "tool")
    if mode == "card":
        method_name = (
            "update_streaming_card_tool_completed"
            if completed
            else "update_streaming_card_tool_started"
        )
        updater = getattr(adapter, method_name, None)
        if callable(updater):
            if completed:
                return await updater(
                    chat_id=chat_id,
                    message_id=message_id,
                    tool_name=tool_name,
                    duration=payload.get("duration"),
                    is_error=bool(payload.get("is_error")),
                )
            return await updater(
                chat_id=chat_id,
                message_id=message_id,
                tool_name=tool_name,
                preview=payload.get("preview"),
                args=payload.get("args"),
            )
    if mode == "card":
        return None

    status = (
        f"✅ 工具完成: {tool_name}"
        if completed and not payload.get("is_error")
        else f"⚠️ 工具失败: {tool_name}"
        if completed
        else f"🔧 正在调用工具: {tool_name}"
    )
    return await _m._update_feishu_stream_target(
        adapter, chat_id, message_id, status, mode=mode
    )


def _is_aiagent_stream_idle_timeout(exc: BaseException) -> bool:
    return "AIAgent subprocess produced no stream events" in str(exc)


def _aiagent_stream_timeout_notice(exc: BaseException) -> str:
    return (
        "\n\n⚠️ 当前任务长时间没有新的运行事件，已停止本次流式执行。\n"
        f"{exc}"
    )


def _stream_card_idle_status(tick: int) -> str:
    """Return a changing pre-token status marker with no visible waiting text."""
    marker = _m._STREAM_STATUS_ANIMATION_MARKERS[(max(1, int(tick)) - 1) % len(_m._STREAM_STATUS_ANIMATION_MARKERS)]
    return marker


def _strip_stream_status_animation_markers(text: str) -> str:
    result = str(text or "")
    for marker in _m._STREAM_STATUS_ANIMATION_MARKERS:
        result = result.replace(marker, "")
    return result


async def _stream_into_feishu_shared_consumer(
    adapter,
    chat_id,
    profile_name,
    profile_home,
    event,
    *,
    gateway: Any = None,
    messages: Optional[list[dict]] = None,
) -> Optional[str]:
    """Stream Feishu card output through Hermes' shared GatewayStreamConsumer.

    Returns None when the shared card surface cannot be started, allowing the
    caller to fall back to the legacy text-edit transport.
    """
    if _m.GatewayStreamConsumer is None or _m.StreamConsumerConfig is None:
        return None
    required_methods = (
        "ensure_streaming_card_started",
        "run",
        "on_delta",
        "finish",
        "update_streaming_card_status",
        "update_streaming_card_reasoning",
        "update_streaming_card_tool_started",
        "update_streaming_card_tool_completed",
    )
    if not all(hasattr(_m.GatewayStreamConsumer, method) for method in required_methods):
        _m.logger.debug("multitenancy: shared GatewayStreamConsumer lacks card methods; using adapter surface")
        return None

    import time
    from ..agent_real import stream_run_agent, real_run_agent
    from ..runtime import _PROFILE_HOME_VAR

    stream_started_at = time.monotonic()
    metadata = _m._thread_metadata_for_media_delivery(gateway, event) if gateway is not None else None
    reply_to = _m._event_reply_to_message_id(event)
    consumer = _m.GatewayStreamConsumer(
        adapter,
        chat_id,
        _m.StreamConsumerConfig(
            edit_interval=_m._STREAM_CARDKIT_CONTENT_MIN_SECONDS,
            buffer_threshold=_m._STREAM_CARDKIT_CONTENT_MIN_CHARS,
            cursor=" ▉",
        ),
        metadata=metadata,
        initial_reply_to_id=reply_to,
    )
    required_consumer_methods = (
        "ensure_streaming_card_started",
        "update_streaming_card_status",
        "update_streaming_card_reasoning",
        "update_streaming_card_tool_started",
        "update_streaming_card_tool_completed",
    )
    if not all(hasattr(consumer, name) for name in required_consumer_methods):
        _m.logger.debug(
            "multitenancy: GatewayStreamConsumer lacks shared-card methods; falling back"
        )
        return None
    consumer_task: Optional[asyncio.Task] = None
    idle_heartbeat_task: Optional[asyncio.Task] = None
    terminal_update_sent = False
    first_agent_event_seen = False
    content_delta_seen = False
    content = ""
    thinking = ""
    last_reasoning_edit = 0.0
    last_reasoning_len = 0

    async def _idle_card_heartbeat() -> None:
        tick = 2
        while True:
            await asyncio.sleep(_m._STREAM_CARD_IDLE_HEARTBEAT_SECONDS)
            if content_delta_seen:
                return
            try:
                await consumer.update_streaming_card_status(_stream_card_idle_status(tick))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _m.logger.debug("multitenancy: shared card idle heartbeat failed: %s", exc)
            tick += 1

    async def _stop_idle_card_heartbeat() -> None:
        if idle_heartbeat_task is None or idle_heartbeat_task.done():
            return
        idle_heartbeat_task.cancel()
        try:
            await idle_heartbeat_task
        except asyncio.CancelledError:
            pass

    def _abort_content() -> str:
        raw = content if content else (thinking if thinking else _m._STREAM_ABORT_FALLBACK)
        return _clean_stream_display_text(raw, profile_home)

    async def _finish_consumer() -> None:
        if consumer_task is None:
            return
        consumer.finish()
        await consumer_task

    try:
        start_task = asyncio.create_task(consumer.ensure_streaming_card_started())
        try:
            started = await asyncio.shield(start_task)
        except asyncio.CancelledError:
            try:
                started = await start_task
            except Exception as exc:
                _m.logger.debug("multitenancy: shared card start failed while cancelling: %s", exc)
                started = False
            if started:
                aborter = getattr(consumer, "abort_streaming_card", None)
                if aborter is not None:
                    try:
                        await aborter(_m._STREAM_ABORT_FALLBACK)
                    except Exception as exc:
                        _m.logger.debug("multitenancy: shared card abort-after-start failed: %s", exc)
            raise

        if not started:
            _m.logger.debug("multitenancy: shared card start unavailable; falling back")
            return None

        _m.logger.info(
            "multitenancy: shared stream card ready elapsed=%.3fs",
            time.monotonic() - stream_started_at,
        )
        consumer_task = asyncio.create_task(consumer.run())

        prime_task = asyncio.create_task(
            consumer.update_streaming_card_status(_stream_card_idle_status(1))
        )
        try:
            await asyncio.shield(prime_task)
        except asyncio.CancelledError:
            try:
                await prime_task
            except Exception as exc:
                _m.logger.debug("multitenancy: shared card prime failed while cancelling: %s", exc)
            aborter = getattr(consumer, "abort_streaming_card", None)
            if aborter is not None:
                try:
                    await aborter(_m._STREAM_ABORT_FALLBACK)
                except Exception as exc:
                    _m.logger.debug("multitenancy: shared card abort-after-prime failed: %s", exc)
            raise

        idle_heartbeat_task = asyncio.create_task(_idle_card_heartbeat())

        token = _PROFILE_HOME_VAR.set(profile_home)
        try:
            try:
                async for kind, delta in stream_run_agent(event, profile_home, messages=messages):
                    if not first_agent_event_seen:
                        first_agent_event_seen = True
                        _m.logger.info(
                            "multitenancy: shared stream first agent event kind=%s total=%.3fs",
                            kind,
                            time.monotonic() - stream_started_at,
                        )

                    if kind == "thinking":
                        thinking += str(delta or "")
                        now = time.monotonic()
                        if (
                            not last_reasoning_len
                            or len(thinking) - last_reasoning_len >= _m._STREAM_CARD_REASONING_MIN_CHARS
                            or now - last_reasoning_edit >= _m._STREAM_CARD_REASONING_MIN_SECONDS
                        ):
                            await consumer.update_streaming_card_reasoning(thinking)
                            last_reasoning_len = len(thinking)
                            last_reasoning_edit = now
                        continue

                    if kind == "status":
                        text = str(delta or "").strip()
                        if text:
                            try:
                                await consumer.update_streaming_card_status(text)
                            except Exception as exc:
                                _m.logger.debug("multitenancy: shared card status update failed: %s", exc)
                        continue

                    if kind == "tool_started":
                        payload = _m._sanitize_tool_event_payload(delta, profile_home)
                        await consumer.update_streaming_card_tool_started(
                            str(payload.get("name") or payload.get("tool_name") or "tool"),
                            preview=payload.get("preview"),
                            args=payload.get("args"),
                        )
                        continue

                    if kind == "tool_completed":
                        payload = _m._sanitize_tool_event_payload(delta, profile_home)
                        await consumer.update_streaming_card_tool_completed(
                            str(payload.get("name") or payload.get("tool_name") or "tool"),
                            duration=payload.get("duration"),
                            is_error=bool(payload.get("is_error")),
                        )
                        continue

                    if kind == "approval_required":
                        await _m._handle_child_approval_required(adapter, chat_id, delta)
                        try:
                            await consumer.update_streaming_card_status("等待用户审批: /approve 或 /deny")
                        except Exception as exc:
                            _m.logger.debug("multitenancy: approval status update failed: %s", exc)
                        continue

                    if kind == "approval_resolved":
                        if isinstance(delta, dict):
                            _m._clear_pending_approval(delta)
                        continue

                    if kind == "done":
                        continue

                    piece = str(delta or "")
                    if not piece:
                        continue
                    if thinking and len(thinking) > last_reasoning_len:
                        await consumer.update_streaming_card_reasoning(thinking)
                        last_reasoning_len = len(thinking)
                        last_reasoning_edit = time.monotonic()
                    content += piece
                    consumer.on_delta(_clean_stream_delta_text(piece, profile_home))
                    content_delta_seen = True
            except Exception as exc:
                if _is_aiagent_stream_idle_timeout(exc):
                    _m.logger.warning("multitenancy: shared streaming stopped on idle timeout: %s", exc)
                    timeout_notice = _aiagent_stream_timeout_notice(exc)
                    content += timeout_notice
                    consumer.on_delta(_clean_stream_display_text(timeout_notice, profile_home))
                    content_delta_seen = True
                else:
                    _m.logger.info("multitenancy: shared streaming failed (%s) — falling back to non-stream", exc)
                    try:
                        content = await real_run_agent(event, profile_home, messages=messages)
                    except Exception as fallback_exc:
                        _m.logger.warning("multitenancy: LLM fully unavailable: %s", fallback_exc)
                        content = (
                            "⚠️ 模型暂时不可用 (LLM provider rejected the request).\n"
                            "请检查 profile 的 config.yaml 模型/凭据, 或稍后再试。"
                        )
                    if not content_delta_seen:
                        consumer.on_delta(_clean_stream_display_text(content, profile_home))
                        content_delta_seen = True
        finally:
            _PROFILE_HOME_VAR.reset(token)
            await _stop_idle_card_heartbeat()

        full = content if content else (thinking if thinking else "(empty response)")
        if not content_delta_seen:
            consumer.on_delta(_clean_stream_display_text(full, profile_home))

        await _finish_consumer()
        terminal_update_sent = True
        return full
    except asyncio.CancelledError:
        await _stop_idle_card_heartbeat()
        if not terminal_update_sent:
            aborter = getattr(consumer, "abort_streaming_card", None)
            if aborter is not None:
                try:
                    await _run_terminal_stream_update(
                        aborter(_abort_content()),
                        label="shared stream abort update",
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as abort_exc:
                    _m.logger.debug("multitenancy: shared stream abort update failed: %s", abort_exc)
        if consumer_task is not None:
            consumer.finish()
            try:
                await consumer_task
            except Exception:
                pass
        raise


async def _stream_into_feishu(
    adapter,
    chat_id,
    profile_name,
    profile_home,
    event,
    *,
    gateway: Any = None,
    messages: Optional[list[dict]] = None,
) -> str:
    """Stream LLM tokens into Feishu.

    Uses OpenClaw-style interactive card streaming when the shared Feishu
    adapter supports it. Falls back to the legacy text placeholder +
    ``edit_message`` loop for older adapters.

    Falls back to a single non-streamed send() if streaming returns empty or
    the initial stream target fails. Returns the final concatenated text.

    ``messages`` (optional): full conversation including prior history. When
    omitted the runner builds a single-turn system+user prompt from the event.
    """
    import time
    from ..agent_real import stream_run_agent, real_run_agent
    from ..runtime import _PROFILE_HOME_VAR

    stream_started_at = time.monotonic()
    reply_to = _m._event_reply_to_message_id(event)
    metadata = _m._thread_metadata_for_media_delivery(gateway, event) if gateway is not None else None

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

    if _adapter_supports_streaming_card(adapter):
        shared_response = await _stream_into_feishu_shared_consumer(
            adapter,
            chat_id,
            profile_name,
            profile_home,
            event,
            gateway=gateway,
            messages=messages,
        )
        if shared_response is not None:
            return shared_response

    stream_mode = "edit"
    placeholder_id: Optional[str] = None
    target_ready_at = stream_started_at
    thinking = ""
    content = ""
    full_content = ""
    last_edit_time = 0.0
    last_render_len = 0
    last_reasoning_render_len = 0
    content_started = False
    first_agent_event_seen = False
    terminal_update_sent = False
    card_reasoning_sent = False
    idle_heartbeat_task: Optional[asyncio.Task] = None

    async def _idle_card_heartbeat() -> None:
        tick = 2
        while True:
            await asyncio.sleep(_m._STREAM_CARD_IDLE_HEARTBEAT_SECONDS)
            if content_started or placeholder_id is None:
                return
            try:
                await _update_feishu_stream_status(
                    adapter,
                    chat_id,
                    placeholder_id,
                    _stream_card_idle_status(tick),
                    mode=stream_mode,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _m.logger.debug("multitenancy: card idle heartbeat failed: %s", exc)
            tick += 1

    async def _stop_idle_card_heartbeat() -> None:
        if idle_heartbeat_task is None or idle_heartbeat_task.done():
            return
        idle_heartbeat_task.cancel()
        try:
            await idle_heartbeat_task
        except asyncio.CancelledError:
            pass

    def render() -> str:
        if content:
            return _clean_stream_display_text(content, profile_home)
        preview = thinking[-160:].strip() if thinking else ""
        return _clean_stream_display_text(preview, profile_home) if preview else _m._STREAM_INVISIBLE_PLACEHOLDER

    def abort_content() -> str:
        raw = content if content else (thinking if thinking else _m._STREAM_ABORT_FALLBACK)
        return _clean_stream_display_text(raw, profile_home)

    async def _flush_current_segment(*, finalize: bool) -> None:
        nonlocal last_edit_time, last_render_len, terminal_update_sent
        if placeholder_id is None:
            return
        rendered = render()
        try:
            await _run_terminal_stream_update(
                _m._update_feishu_stream_target(
                    adapter,
                    chat_id,
                    placeholder_id,
                    rendered,
                    mode=stream_mode,
                    finalize=finalize,
                ),
                label="stream segment update",
            )
            last_edit_time = time.monotonic()
            last_render_len = len(rendered)
            if finalize:
                terminal_update_sent = True
        except Exception as exc:
            _m.logger.debug("multitenancy: stream segment update failed: %s", exc)

    async def _start_next_stream_segment() -> None:
        nonlocal stream_mode, placeholder_id, content, content_started
        nonlocal last_edit_time, last_render_len, terminal_update_sent
        if placeholder_id is not None:
            await _flush_current_segment(finalize=True)
        stream_mode, placeholder_id = await _start_feishu_stream_target(
            adapter,
            chat_id,
            reply_to=reply_to,
            metadata=metadata,
        )
        terminal_update_sent = False
        content = ""
        content_started = False
        last_edit_time = time.monotonic()
        last_render_len = 0
        if placeholder_id is None:
            return
        if stream_mode == "card":
            try:
                await _update_feishu_stream_status(
                    adapter,
                    chat_id,
                    placeholder_id,
                    _m._STREAM_INVISIBLE_PLACEHOLDER,
                    mode=stream_mode,
                )
            except Exception as exc:
                _m.logger.debug("multitenancy: continuation card prime update failed: %s", exc)

    try:
        # Create/send can complete remotely after this task is cancelled. Shield
        # it so we can still obtain the message_id and close the card instead of
        # leaving a Generating card behind.
        start_task = asyncio.create_task(
            _start_feishu_stream_target(
                adapter,
                chat_id,
                reply_to=reply_to,
                metadata=metadata,
            )
        )
        try:
            stream_mode, placeholder_id = await asyncio.shield(start_task)
        except asyncio.CancelledError:
            try:
                stream_mode, placeholder_id = await start_task
                _m.logger.info(
                    "multitenancy: stream target start completed while task was cancelling "
                    "mode=%s message_id=%s",
                    stream_mode,
                    placeholder_id,
                )
            except Exception as exc:
                _m.logger.debug("multitenancy: stream target start failed while cancelling: %s", exc)
            raise

        target_ready_at = time.monotonic()
        _m.logger.info(
            "multitenancy: stream target ready mode=%s message_id=%s elapsed=%.3fs",
            stream_mode,
            placeholder_id,
            target_ready_at - stream_started_at,
        )

        if placeholder_id is None:
            # Couldn't get a message to edit — degrade to one-shot non-stream.
            text = await real_run_agent(event, profile_home, messages=messages)
            await adapter.send(chat_id, text, reply_to=reply_to, metadata=metadata)
            return text

        if stream_mode == "card":
            try:
                await _update_feishu_stream_status(
                    adapter,
                    chat_id,
                    placeholder_id,
                    _stream_card_idle_status(1),
                    mode=stream_mode,
                )
                _m.logger.info(
                    "multitenancy: stream card primed message_id=%s elapsed=%.3fs",
                    placeholder_id,
                    time.monotonic() - target_ready_at,
                )
                idle_heartbeat_task = asyncio.create_task(_idle_card_heartbeat())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _m.logger.debug("multitenancy: card prime update failed: %s", exc)

        token = _PROFILE_HOME_VAR.set(profile_home)
        try:
            try:
                async for kind, delta in stream_run_agent(event, profile_home, messages=messages):
                    if not first_agent_event_seen:
                        first_agent_event_seen = True
                        _m.logger.info(
                            "multitenancy: stream first agent event kind=%s "
                            "since_target=%.3fs total=%.3fs",
                            kind,
                            time.monotonic() - target_ready_at,
                            time.monotonic() - stream_started_at,
                        )
                    if kind == "thinking":
                        thinking += str(delta or "")
                        if stream_mode == "card" and thinking:
                            now = time.monotonic()
                            should_update_card_reasoning = (
                                not card_reasoning_sent
                                or len(thinking) - last_reasoning_render_len >= _m._STREAM_CARD_REASONING_MIN_CHARS
                                or now - last_edit_time >= _m._STREAM_CARD_REASONING_MIN_SECONDS
                            )
                            if should_update_card_reasoning:
                                try:
                                    await _update_feishu_stream_reasoning(
                                        adapter,
                                        chat_id,
                                        placeholder_id,
                                        thinking,
                                        mode=stream_mode,
                                    )
                                    card_reasoning_sent = True
                                    last_reasoning_render_len = len(thinking)
                                except Exception as exc:
                                    _m.logger.debug("multitenancy: card reasoning update failed: %s", exc)
                                last_edit_time = now
                                last_render_len = len(render())
                            continue
                    elif kind == "tool_started":
                        try:
                            await _update_feishu_stream_tool_event(
                                adapter,
                                chat_id,
                                placeholder_id,
                                delta,
                                mode=stream_mode,
                                completed=False,
                                profile_home=profile_home,
                            )
                        except Exception as exc:
                            _m.logger.debug("multitenancy: card tool-start update failed: %s", exc)
                        last_edit_time = time.monotonic()
                        last_render_len = len(render())
                        continue
                    elif kind == "tool_completed":
                        try:
                            await _update_feishu_stream_tool_event(
                                adapter,
                                chat_id,
                                placeholder_id,
                                delta,
                                mode=stream_mode,
                                completed=True,
                                profile_home=profile_home,
                            )
                        except Exception as exc:
                            _m.logger.debug("multitenancy: card tool-complete update failed: %s", exc)
                        last_edit_time = time.monotonic()
                        last_render_len = len(render())
                        continue
                    elif kind == "approval_required":
                        await _m._handle_child_approval_required(adapter, chat_id, delta)
                        try:
                            await _update_feishu_stream_status(
                                adapter,
                                chat_id,
                                placeholder_id,
                                "等待用户审批: /approve 或 /deny",
                                mode=stream_mode,
                            )
                        except Exception as exc:
                            _m.logger.debug("multitenancy: approval status update failed: %s", exc)
                        last_edit_time = time.monotonic()
                        last_render_len = len(render())
                        continue
                    elif kind == "approval_resolved":
                        if isinstance(delta, dict):
                            _m._clear_pending_approval(delta)
                        continue
                    elif kind == "status":
                        status_text = str(delta or "").strip()
                        if status_text:
                            try:
                                await _update_feishu_stream_status(
                                    adapter,
                                    chat_id,
                                    placeholder_id,
                                    status_text,
                                    mode=stream_mode,
                                )
                            except Exception as exc:
                                _m.logger.debug("multitenancy: stream status update failed: %s", exc)
                            last_edit_time = time.monotonic()
                            last_render_len = len(render())
                        continue
                    elif kind == "done":
                        continue
                    else:
                        piece = str(delta or "")
                        if not piece:
                            continue
                        # Compensation flush (issue #2): a short/fast reasoning
                        # burst can be throttled so state["reasoning"] freezes at
                        # the first token ("The"). Before the answer renders,
                        # push the full accumulated thinking so the collapsed
                        # reasoning panel shows it all. Mirrors the shared-consumer
                        # loop above (single-flush before content).
                        if stream_mode == "card" and thinking and len(thinking) > last_reasoning_render_len:
                            try:
                                await _update_feishu_stream_reasoning(
                                    adapter,
                                    chat_id,
                                    placeholder_id,
                                    thinking,
                                    mode=stream_mode,
                                )
                                last_reasoning_render_len = len(thinking)
                            except Exception as exc:
                                _m.logger.debug(
                                    "multitenancy: card reasoning compensation flush failed: %s", exc
                                )
                        full_content += piece
                        while piece:
                            remaining = _m._STREAM_MAX_VISIBLE_CHARS - len(content)
                            if remaining <= 0:
                                _m.logger.info(
                                    "multitenancy: stream content segment finalized "
                                    "message_id=%s max_chars=%s",
                                    placeholder_id,
                                    _m._STREAM_MAX_VISIBLE_CHARS,
                                )
                                await _start_next_stream_segment()
                                if placeholder_id is None:
                                    break
                                remaining = _m._STREAM_MAX_VISIBLE_CHARS
                            segment_piece = piece[:remaining]
                            piece = piece[remaining:]
                            content += segment_piece
                            rendered = render()
                            now = time.monotonic()
                            if not content_started:
                                # Force an immediate edit on phase transition so the user
                                # sees the answer start the moment reasoning ends.
                                content_started = True
                                try:
                                    await _m._update_feishu_stream_target(
                                        adapter,
                                        chat_id,
                                        placeholder_id,
                                        rendered,
                                        mode=stream_mode,
                                    )
                                except Exception as exc:
                                    _m.logger.debug(
                                        "multitenancy: phase-transition stream update failed: %s",
                                        exc,
                                    )
                                last_edit_time = time.monotonic()
                                last_render_len = len(rendered)
                            elif (
                                piece
                                or len(rendered) - last_render_len >= _m._STREAM_CONTENT_MIN_CHARS
                                or now - last_edit_time >= _m._STREAM_CONTENT_MIN_SECONDS
                            ):
                                try:
                                    await _m._update_feishu_stream_target(
                                        adapter,
                                        chat_id,
                                        placeholder_id,
                                        rendered,
                                        mode=stream_mode,
                                    )
                                except Exception as exc:
                                    _m.logger.debug("multitenancy: stream update mid-stream failed: %s", exc)
                                last_edit_time = now
                                last_render_len = len(rendered)
                            if piece:
                                _m.logger.info(
                                    "multitenancy: stream content segment split "
                                    "message_id=%s max_chars=%s",
                                    placeholder_id,
                                    _m._STREAM_MAX_VISIBLE_CHARS,
                                )
                                await _start_next_stream_segment()
                                if placeholder_id is None:
                                    break
                        continue

                    now = time.monotonic()
                    rendered = render()
                    if content_started:
                        should_edit = (
                            len(rendered) - last_render_len >= _m._STREAM_CONTENT_MIN_CHARS
                            or now - last_edit_time >= _m._STREAM_CONTENT_MIN_SECONDS
                        )
                    else:
                        # Reasoning phase — heartbeat-only edits, no char threshold.
                        should_edit = now - last_edit_time >= _m._STREAM_THINKING_MIN_SECONDS
                    if should_edit:
                        try:
                            await _m._update_feishu_stream_target(
                                adapter,
                                chat_id,
                                placeholder_id,
                                rendered,
                                mode=stream_mode,
                            )
                        except Exception as exc:
                            _m.logger.debug("multitenancy: stream update mid-stream failed: %s", exc)
                        last_edit_time = now
                        last_render_len = len(rendered)
            except Exception as exc:
                if _is_aiagent_stream_idle_timeout(exc):
                    _m.logger.warning("multitenancy: streaming stopped on idle timeout: %s", exc)
                    timeout_notice = _aiagent_stream_timeout_notice(exc)
                    content += timeout_notice
                    full_content += timeout_notice
                    try:
                        await _update_feishu_stream_status(
                            adapter,
                            chat_id,
                            placeholder_id,
                            "任务长时间没有新的运行事件，已停止。",
                            mode=stream_mode,
                        )
                    except Exception as status_exc:
                        _m.logger.debug("multitenancy: idle-timeout status update failed: %s", status_exc)
                else:
                    _m.logger.info("multitenancy: streaming failed (%s) — falling back to non-stream", exc)
                    try:
                        content = await real_run_agent(event, profile_home, messages=messages)
                        full_content = content
                    except Exception as fallback_exc:
                        # Both stream + non-stream LLM paths failed (e.g. region block,
                        # exhausted credentials). Surface a user-visible error instead
                        # of leaving the "..." placeholder hanging.
                        _m.logger.warning("multitenancy: LLM fully unavailable: %s", fallback_exc)
                        content = (
                            "⚠️ 模型暂时不可用 (LLM provider rejected the request).\n"
                            "请检查 profile 的 config.yaml 模型/凭据, 或稍后再试。"
                        )
                        full_content = content
        finally:
            _PROFILE_HOME_VAR.reset(token)
            await _stop_idle_card_heartbeat()

        full = full_content or content or (thinking if thinking else "(empty response)")
        display_current = _clean_stream_display_text(content or full, profile_home)

        # 3. Final commit. finalize=True signals end of stream to Feishu.
        _merge_stream_footer_metrics(
            adapter,
            mode=stream_mode,
            message_id=placeholder_id,
            profile_home=profile_home,
        )
        try:
            await _run_terminal_stream_update(
                _m._update_feishu_stream_target(
                    adapter,
                    chat_id,
                    placeholder_id,
                    display_current,
                    mode=stream_mode,
                    finalize=True,
                ),
                label="stream final update",
            )
            terminal_update_sent = True
        except Exception as exc:
            _m.logger.debug("multitenancy: final stream update failed: %s", exc)

        return full
    except asyncio.CancelledError:
        if placeholder_id is not None and not terminal_update_sent:
            full = abort_content()
            _m.logger.info(
                "multitenancy: stream cancelled; aborting target mode=%s message_id=%s content_len=%s",
                stream_mode,
                placeholder_id,
                len(full),
            )
            _merge_stream_footer_metrics(
                adapter,
                mode=stream_mode,
                message_id=placeholder_id,
                profile_home=profile_home,
            )
            try:
                await _run_terminal_stream_update(
                    _abort_feishu_stream_target(
                        adapter,
                        chat_id,
                        placeholder_id,
                        full,
                        mode=stream_mode,
                    ),
                    label="stream abort update",
                )
            except asyncio.CancelledError:
                raise
            except Exception as abort_exc:
                _m.logger.debug("multitenancy: stream abort update failed: %s", abort_exc)
        raise
