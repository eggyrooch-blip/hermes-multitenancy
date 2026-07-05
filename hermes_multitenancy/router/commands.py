"""Command dispatch helpers split out of router god-node (pure move).

Shim helpers/state routed through ``_m`` for monkeypatch fidelity.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from .. import router as _m


async def handle_async(*, event: Any, gateway: Any) -> None:
    """Async dispatch — orchestrates routing + pool + adapter calls + commands."""
    from ..commands import parse_command

    try:
        source = getattr(event, "source", None)
        chat_id = getattr(source, "chat_id", "unknown") if source else "unknown"
        fallback_sender = getattr(source, "user_id", "unknown") if source else "unknown"
        sender = _m._resolve_sender_for_routing(event, fallback=fallback_sender)
        if _m._is_feishu_open_id(sender):
            setattr(event, "sender_open_id", sender)
        text = getattr(event, "text", "") or ""

        if _m._is_reaction_synthetic_event(event, text):
            _m.logger.info(
                "multitenancy: skipping Feishu reaction synthetic event "
                "text=%r message_id=%s chat_id=%s",
                text,
                _m._event_message_id(event) or "",
                chat_id,
            )
            return

        sender_alt = getattr(source, "user_id_alt", None) if source else None
        if getattr(event, "media_urls", None):
            _m.logger.info(
                "multitenancy: handle_async media event message_id=%s text_len=%s media_urls=%s media_types=%s message_type=%s",
                _m._event_message_id(event) or "",
                len(str(text or "")),
                list(getattr(event, "media_urls", None) or []),
                list(getattr(event, "media_types", None) or []),
                str(getattr(event, "message_type", "")),
            )

        # Group-chat profile resolution — when the event is from a group/topic
        # chat, the route is keyed by chat_id (not the @-er's open_id). This
        # branch runs before slash-command short-circuit so /status & friends
        # see the group profile instead of the @-er's private profile.
        chat_type = _m._extract_chat_type(event)
        is_group_chat = _m._is_group_chat_type(chat_type)
        group_profile_name: Optional[str] = None
        group_profile_home: Optional[Path] = None
        if is_group_chat and chat_id and chat_id != "unknown":
            group_profile_name, group_profile_home = (
                await _m.resolve_or_auto_provision_group_route(
                    chat_id=chat_id, gateway=gateway,
                )
            )

        # Slash command short-circuit (resolve route first so /status / /new
        # know which profile's history to inspect). When _resolve_route signals
        # a miss with profile_home=None, surface profile_name=None so command
        # handlers reply "未路由" instead of leaking the sender id.
        # Group messages start with leading @_all / @_user_N tokens that
        # Feishu prepends; strip them before delegating to parse_command so
        # ``@bot /feishu_auth`` is recognised as a slash command.
        command_source_text = _m._strip_leading_at_mentions(text) if is_group_chat else text
        cmd_pair = parse_command(command_source_text)
        if cmd_pair is not None:
            # Group profiles do not own any UAT — the whole auth command
            # family is hard-rejected so a curious member can't trigger an
            # OAuth dance that would store a per-user token under the
            # group's profile_home. Normalise first: Feishu lets users send
            # `/feishu_auth@bot` or sneak zero-width chars, and an exact
            # string-equality gate would let those through the day
            # feishu_auth becomes gateway-dispatchable.
            if is_group_chat and _m._is_blocked_group_command(cmd_pair[0]):
                adapter = _m._get_feishu_adapter(gateway)
                if adapter is not None:
                    await _m._safe_call(
                        adapter.send,
                        chat_id,
                        "群聊模式下不支持认证类命令（/auth、/feishu_auth 等）。"
                        "如需查看或认证你本人的凭证，请在与我私聊时执行。",
                    )
                return

            if is_group_chat:
                cmd_profile_name = group_profile_name
                cmd_profile_home = group_profile_home
            else:
                cmd_profile_name, cmd_profile_home = _m._resolve_route(sender, alt_id=sender_alt)
            cmd_profile = cmd_profile_name if cmd_profile_home is not None else None
            if _m._should_check_skill_slash_command(cmd_pair[0], gateway):
                async with _m._profile_gateway_context(
                    gateway,
                    event,
                    sender=sender,
                    sender_alt=sender_alt,
                    profile_name=cmd_profile,
                    profile_home=cmd_profile_home,
                    chat_id=chat_id,
                ):
                    skill_handled, skill_reply = _m._maybe_rewrite_skill_slash_command(
                        cmd_pair,
                        event,
                        gateway,
                        sender=sender,
                        sender_alt=sender_alt,
                        profile_name=cmd_profile,
                        profile_home=cmd_profile_home,
                        chat_id=chat_id,
                    )
            else:
                skill_handled, skill_reply = False, None
            if skill_handled:
                if skill_reply:
                    adapter = _m._get_feishu_adapter(gateway)
                    if adapter is not None:
                        await _m._safe_call(adapter.send, chat_id, skill_reply)
                    return
                text = getattr(event, "text", "") or ""
            else:
                await _m._handle_command(
                    cmd_pair,
                    sender,
                    sender_alt,
                    cmd_profile,
                    cmd_profile_home,
                    chat_id,
                    gateway,
                    event,
                )
                return

        # Routing: group already resolved above; sender-based path for p2p.
        if is_group_chat:
            profile_name, profile_home = group_profile_name, group_profile_home
            if profile_home is None:
                _m.logger.info(
                    "multitenancy: no group route for chat_id=%s (inviter "
                    "not captured), ignoring",
                    chat_id,
                )
                adapter = _m._get_feishu_adapter(gateway)
                if adapter is not None:
                    await _m._safe_call(
                        adapter.send,
                        chat_id,
                        "👋 我还没有这个群的专属 Profile。"
                        "请移除我后再次拉我进群，让我捕获邀请人身份。",
                    )
                return
        else:
            profile_name, profile_home = _m._resolve_or_auto_provision_route(sender, alt_id=sender_alt)
            if profile_home is None:
                _m.logger.info("multitenancy: no route for sender=%s, ignoring", sender)
                return
            if not _m._is_interactive_or_card_event(event):
                _m._capture_pending_auth_replay(
                    profile_name,
                    _m._normalize_feishu_open_id(getattr(event, "sender_open_id", None))
                    or _m._normalize_feishu_open_id(sender)
                    or str(sender or "").strip(),
                    text,
                )

        adapter = _m._get_feishu_adapter(gateway)
        # Detect whether adapter supports the streaming/reaction APIs we use.
        # Real FeishuAdapter does; unit-test mocks typically don't.
        feishu_full = (
            adapter is not None
            and hasattr(adapter, "edit_message")
            and hasattr(adapter, "on_processing_start")
            and hasattr(adapter, "on_processing_complete")
        )

        # Multi-modal enrichment must happen before RunRequest admission because
        # file-only Feishu events have empty event.text.  The enriched content is
        # the real prompt and the dedupe/admission key should reflect it.
        _m._materialize_inbound_media_for_profile(event, profile_home)
        enriched_text = await _m._call_enrich_via_hermes_pipeline(event, gateway, profile_home=profile_home)
        vision_blocked = _m._image_vision_unavailable_response(event, enriched_text)
        if vision_blocked:
            _m.logger.info(
                "multitenancy: sending image vision unavailable response profile=%s message_id=%s",
                profile_name,
                _m._event_message_id(event) or "",
            )
            hist_key = _m._dispatch_session_scope(profile_name, sender, sender_alt, chat_id, event).history_key
            user_msg = _m._build_user_message(event, text_override=enriched_text)
            _m._persist_turn(hist_key, user_msg, vision_blocked)
            if adapter is not None:
                await _m._safe_call(adapter.send, chat_id, vision_blocked)
            return
        run_content = enriched_text or text
        if not run_content and getattr(event, "media_urls", None):
            run_content = "[media attachment]"

        run_request = _m._run_request_for_routed_event(
            event=event,
            profile_name=profile_name,
            sender=sender,
            sender_alt=sender_alt,
            chat_id=chat_id,
            text=run_content,
        )
        from ..run_broker import RunRejected

        try:
            run_admission = await _m._make_routed_run_broker().admit(run_request)
        except RunRejected as exc:
            _m.logger.warning("multitenancy: routed run rejected profile=%s sender=%s: %s", profile_name, sender, exc)
            if feishu_full:
                try:
                    out = _m._processing_outcome(failed=True)
                    complete_deferred = getattr(adapter, "complete_deferred_processing", None)
                    if callable(complete_deferred):
                        await complete_deferred(event, out)
                    else:
                        await adapter.on_processing_complete(event, out)
                except Exception as complete_exc:
                    _m.logger.debug("multitenancy: rejected processing_complete failed: %s", complete_exc)
            return

        if run_admission.duplicate:
            _m.logger.info(
                "multitenancy: duplicate inbound event skipped profile=%s sender=%s message_id=%s",
                profile_name,
                sender,
                _m._event_message_id(event) or "",
            )
            if feishu_full:
                try:
                    out = _m._processing_outcome(failed=False)
                    complete_deferred = getattr(adapter, "complete_deferred_processing", None)
                    if callable(complete_deferred):
                        await complete_deferred(event, out)
                    else:
                        await adapter.on_processing_complete(event, out)
                except Exception as exc:
                    _m.logger.debug("multitenancy: duplicate processing_complete failed: %s", exc)
            return

        # Register self in the context-scoped in-flight slot (replace previous)
        current = asyncio.current_task()
        inflight_key = _m._dispatch_session_scope(profile_name, sender, sender_alt, chat_id, event).inflight_key
        prev = _m._user_inflight_tasks.get(inflight_key)
        if prev is not None and not prev.done() and prev is not current:
            prev_hist_key = _m._user_inflight_history_keys.get(inflight_key)
            if prev_hist_key is not None:
                _m._persist_interruption_marker(prev_hist_key)
            _m._suppress_interruption_marker_tasks.add(prev)
            prev.cancel()
        if current is not None:
            _m._user_inflight_tasks[inflight_key] = current

        outcome_failed = False
        if feishu_full:
            try:
                await adapter.on_processing_start(event)
            except Exception as exc:
                _m.logger.debug("multitenancy: on_processing_start failed: %s", exc)

        # Build the conversation: prior history + current user message (with
        # reply context spliced in). The runner prepends the profile's SOUL.
        # First lookup for a (profile, user) pair hydrates from SessionStore.
        hist_key = _m._dispatch_session_scope(profile_name, sender, sender_alt, chat_id, event).history_key
        prior = _m._load_history(hist_key)
        contextual_text = _m._append_recent_profile_file_context(
            enriched_text or text,
            profile_name=profile_name,
            chat_id=chat_id,
            profile_home=profile_home,
            prior_messages=prior,
        )
        user_msg = _m._build_user_message(event, text_override=contextual_text)
        conversation = prior + [user_msg]
        _m._persist_user_message(hist_key, user_msg)
        if current is not None and _m._user_inflight_tasks.get(inflight_key) is current:
            _m._user_inflight_history_keys[inflight_key] = hist_key
        agent_event = _m._event_with_text(event, user_msg["content"])

        try:
            if feishu_full:
                # Streaming path — card stream when available; text edit fallback.
                async def _dispatch_streaming(_request):
                    stream_kwargs = {"messages": conversation}
                    try:
                        if "gateway" in inspect.signature(_m._stream_into_feishu).parameters:
                            stream_kwargs["gateway"] = gateway
                    except (TypeError, ValueError):
                        stream_kwargs["gateway"] = gateway
                    stream_response = await _m._stream_into_feishu(
                        adapter, chat_id, profile_name, profile_home, agent_event,
                        **stream_kwargs,
                    )
                    if stream_response:
                        await _m._deliver_media_from_stream_response(
                            gateway, stream_response, agent_event, adapter, profile_home
                        )
                    return stream_response

                run_result = await _m._make_routed_run_broker(
                    dispatch_agent=_dispatch_streaming,
                ).run(run_request, admitted=True)
                response_text = run_result.content
            else:
                # Mock / minimal adapter — old non-stream path (send_typing + pool.dispatch + send)
                if adapter is not None:
                    await _m._safe_call(adapter.send_typing, chat_id)
                async def _dispatch_nonstream(_request):
                    return await _m._get_pool().dispatch(profile_name, profile_home, agent_event)

                run_result = await _m._make_routed_run_broker(
                    dispatch_agent=_dispatch_nonstream,
                ).run(run_request, admitted=True)
                response_text = run_result.content
                if adapter is not None:
                    await _m._safe_call(adapter.send, chat_id, response_text)

            # Record turn into history + persist to SessionStore.
            if response_text and isinstance(response_text, str):
                _m._persist_assistant_message(hist_key, response_text)

            _m._touch_route(sender, sender_alt)
        except asyncio.CancelledError:
            if current not in _m._suppress_interruption_marker_tasks:
                _m._persist_interruption_marker(hist_key)
            raise
        except Exception:
            outcome_failed = True
            _m._persist_failure_marker(hist_key)
            raise
        finally:
            if feishu_full:
                try:
                    out = _m._processing_outcome(failed=outcome_failed)
                    complete_deferred = getattr(adapter, "complete_deferred_processing", None)
                    if callable(complete_deferred):
                        await complete_deferred(event, out)
                    else:
                        await adapter.on_processing_complete(event, out)
                except Exception as exc:
                    _m.logger.debug("multitenancy: on_processing_complete failed: %s", exc)
            if _m._user_inflight_tasks.get(inflight_key) is current:
                _m._user_inflight_tasks.pop(inflight_key, None)
                _m._user_inflight_history_keys.pop(inflight_key, None)
            if current is not None:
                _m._suppress_interruption_marker_tasks.discard(current)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _m.logger.exception("multitenancy: handle_async failed: %s", exc)


def _processing_outcome(*, failed: bool) -> Any:
    """Return Hermes' ProcessingOutcome enum, or a string-compatible fallback."""
    try:
        from gateway.platforms.base import ProcessingOutcome  # type: ignore

        return ProcessingOutcome.FAILURE if failed else ProcessingOutcome.SUCCESS
    except Exception:
        class _FallbackOutcome:
            def __str__(self) -> str:
                status = "FAILURE" if failed else "SUCCESS"
                return f"ProcessingOutcome.{status}"

        return _FallbackOutcome()


def _maybe_rewrite_skill_slash_command(
    pair: tuple[str, str],
    event: Any,
    gateway: Any,
    *,
    sender: str,
    sender_alt: Optional[str],
    profile_name: Optional[str],
    profile_home: Optional[Path],
    chat_id: str,
) -> tuple[bool, Optional[str]]:
    """Rewrite Hermes skill slash commands into the native skill invocation text.

    Native gateway treats ``/skill-name args`` as an agent turn after loading
    the skill instructions. The multitenancy router sees the slash first, so it
    must preserve that behavior instead of replying "unknown command".
    """
    cmd, args = pair
    try:
        from ..commands import is_known_command

        if is_known_command(cmd) or _m._gateway_handler_for_command(gateway, cmd) is not None:
            return False, None
        if _m._get_quick_command(gateway, cmd) is not None:
            return False, None
        if _m._get_plugin_command_handler(cmd) is not None:
            return False, None

        from agent.skill_commands import (  # type: ignore
            build_skill_invocation_message,
            get_skill_commands,
            resolve_skill_command_key,
        )

        skill_cmds = get_skill_commands()
        cmd_key = resolve_skill_command_key(cmd)
        if cmd_key is None:
            # Unify the Feishu path with the broker path on one alias source: hardcoded
            # base + the routed profile's skill-declared `slash_aliases` (dynamic scan).
            # This path already runs inside `_scope_profile_skill_loader`, so the scan
            # sees THIS profile's installed skills.
            from ..skill_slash import _resolve_alias

            alias = _resolve_alias(cmd.replace("_", "-"))
            if alias:
                cmd_key = resolve_skill_command_key(alias)
        if cmd_key is None:
            return False, None

        skill_info = skill_cmds.get(cmd_key) or {}
        skill_name = skill_info.get("name", "")
        platform = _m._event_platform_value(event)
        if platform and skill_name:
            try:
                from agent.skill_utils import get_disabled_skill_names  # type: ignore

                if skill_name in get_disabled_skill_names(platform=platform):
                    return (
                        True,
                        f"The **{skill_name}** skill is disabled for {platform}.\n"
                        "Enable it with: `hermes skills config`",
                    )
            except Exception as exc:
                _m.logger.debug("multitenancy: skill disabled check failed (%s)", exc)

        old_skill_dir = skill_info.get("skill_dir")
        relative_skill_dir = _m._profile_relative_skill_dir(skill_info, profile_home)
        if relative_skill_dir:
            skill_info["skill_dir"] = relative_skill_dir
        try:
            msg = build_skill_invocation_message(
                cmd_key,
                args.strip(),
                task_id=_m._multitenant_gateway_session_key(
                    event,
                    profile_name=profile_name,
                    sender=sender,
                    sender_alt=sender_alt,
                    chat_id=chat_id,
                ),
            )
        finally:
            if relative_skill_dir:
                skill_info["skill_dir"] = old_skill_dir
        if not msg:
            return False, None
        _m.logger.info("Hermes skill slash invocation: %s profile=%s", cmd_key, profile_name or "")
        setattr(event, "text", msg)
        return True, None
    except Exception as exc:
        _m.logger.debug("multitenancy: skill command passthrough failed (%s)", exc)
        return False, None


def _should_check_skill_slash_command(cmd: str, gateway: Any) -> bool:
    """Only unknown slash commands need profile-scoped skill alias lookup.

    Known Hermes commands such as ``/stop`` must not wait on the profile env
    lock; that lock may be held by the very in-flight run the command is trying
    to cancel.
    """
    try:
        from ..commands import is_known_command

        if is_known_command(cmd):
            return False
    except Exception:
        pass
    if _m._gateway_handler_for_command(gateway, cmd) is not None:
        return False
    if _m._get_quick_command(gateway, cmd) is not None:
        return False
    if _m._get_plugin_command_handler(cmd) is not None:
        return False
    return True


async def _handle_command(
    pair: tuple[str, str],
    sender: str,
    sender_alt: Optional[str],
    profile_name: Optional[str],
    profile_home: Optional[Path],
    chat_id: str,
    gateway: Any,
    event: Any,
) -> None:
    """Execute a parsed slash command and reply via the shared adapter."""
    cmd, _args = pair
    adapter = _m._get_feishu_adapter(gateway)

    approval_reply = _m._handle_pending_approval_command(
        cmd,
        _args,
        event,
        profile_name=profile_name,
        sender=sender,
        sender_alt=sender_alt,
        chat_id=chat_id,
    )
    if approval_reply is not None:
        reply = approval_reply
    elif cmd == "stop":
        task = _m._cancel_inflight_task(
            _m._dispatch_session_scope(profile_name, sender, sender_alt, chat_id, event).inflight_key,
            preserve_resume_marker=True,
        )
        if task is not None and not task.done():
            reply = "已停止当前任务"
        else:
            reply = "没有进行中的任务"
    elif cmd == "status":
        task = _m._user_inflight_tasks.get(_m._dispatch_session_scope(profile_name, sender, sender_alt, chat_id, event).inflight_key)
        running = task is not None and not task.done()
        # Surface session memory size + profile so the user knows their context.
        if profile_name:
            hist = _m._session_history.get(_m._dispatch_session_scope(profile_name, sender, sender_alt, chat_id, event).history_key, [])
            hist_len = len(hist)
        else:
            hist_len = 0
        reply = (
            f"状态: {'运行中' if running else '空闲'}\n"
            f"profile: {profile_name or '(未路由)'}\n"
            f"会话历史: {hist_len} 条消息"
        )
    elif cmd in ("doctor", "diagnose"):
        await _m._send_diagnostics_card(
            adapter,
            chat_id,
            cmd,
            event,
            sender=sender,
            routed_profile=profile_name,
            profile_home=profile_home,
        )
        return
    elif cmd in ("new", "reset"):
        _m._cancel_inflight_task(
            _m._dispatch_session_scope(profile_name, sender, sender_alt, chat_id, event).inflight_key,
            preserve_resume_marker=False,
        )
        # Clear this user's per-profile history (cache + persistent SessionStore).
        if profile_name:
            key = _m._dispatch_session_scope(profile_name, sender, sender_alt, chat_id, event).history_key
            _m._clear_history(key)
            reply = "会话已重置 ✅"
        else:
            reply = "(未路由的用户) 没有历史可重置"
    elif cmd == "feishu-auth":
        await _m._handle_feishu_auth_command(
            args=_args,
            sender=sender,
            sender_alt=sender_alt,
            profile_name=profile_name,
            profile_home=profile_home,
            chat_id=chat_id,
            gateway=gateway,
            event=event,
        )
        return
    elif cmd == "auth":
        await _m._handle_auth_command(
            args=_args,
            sender=sender,
            sender_alt=sender_alt,
            profile_name=profile_name,
            profile_home=profile_home,
            chat_id=chat_id,
            gateway=gateway,
            event=event,
        )
        return
    elif cmd == "help":
        reply = _m._gateway_help_text()
    else:
        dispatched = await _m._dispatch_gateway_command(
            cmd,
            event,
            gateway,
            sender=sender,
            sender_alt=sender_alt,
            profile_name=profile_name,
            profile_home=profile_home,
            chat_id=chat_id,
        )
        if dispatched is not None:
            reply = dispatched
        else:
            from ..commands import is_known_command, unknown_command_message

            if is_known_command(cmd):
                reply = (
                    f"Command `/{cmd}` is recognized by Hermes, but this gateway does not "
                    "expose a reusable command dispatcher yet."
                )
            else:
                reply = unknown_command_message(cmd)
                _m.logger.info("%s", reply)

    if adapter is not None:
        await _m._safe_call(adapter.send, chat_id, reply)


async def _handle_feishu_auth_command(
    *,
    args: str,
    sender: str,
    sender_alt: Optional[str],
    profile_name: Optional[str],
    profile_home: Optional[Path],
    chat_id: str,
    gateway: Any,
    event: Any,
) -> None:
    """Start a multitenancy-owned Feishu UAT device-flow auth session."""
    del profile_home
    from .. import feishu_uat_auth
    from ..feishu_auth_cards import auth_text_fallback, build_auth_card, send_auth_card

    adapter = _m._get_feishu_adapter(gateway)
    open_id = (
        _m._normalize_feishu_open_id(sender)
        or _m._normalize_feishu_open_id(sender_alt)
        or _m._profile_open_id_for_auth(profile_name)
        or ""
    )
    if not _m._is_feishu_open_id(open_id):
        if adapter is not None:
            await _m._safe_call(adapter.send, chat_id, "无法启动飞书授权：当前消息没有可用的 sender open_id。")
        return
    if not profile_name:
        if adapter is not None:
            await _m._safe_call(adapter.send, chat_id, "无法启动飞书授权：当前飞书用户还没有绑定 Hermes profile。")
        return

    scope = args.strip() or None
    try:
        session = feishu_uat_auth.start_session(
            profile_name=profile_name,
            open_id=open_id,
            scope=scope,
        )
    except feishu_uat_auth.FeishuUatAuthError as exc:
        if adapter is not None:
            await _m._safe_call(adapter.send, chat_id, f"无法启动飞书授权：{exc.message}")
        return

    session_id = str(session.get("session_id") or "")
    verification_uri = str(session.get("verification_uri") or "")
    user_code = str(session.get("user_code") or "")
    expires_at = float(session.get("expires_at") or 0)
    expires_min = 10
    if expires_at:
        expires_min = max(1, int((expires_at - time.time() + 59) // 60))
    auth_card = None
    if adapter is not None:
        card = build_auth_card(
            verification_uri=verification_uri,
            user_code=user_code,
            expires_min=expires_min,
            scope=scope,
        )
        auth_card = await send_auth_card(adapter=adapter, chat_id=chat_id, card=card)
        if auth_card is None:
            await _m._safe_call(
                adapter.send,
                chat_id,
                auth_text_fallback(verification_uri=verification_uri, user_code=user_code),
            )
    _m._start_feishu_auth_poll_task(
        session_id=session_id,
        profile_name=profile_name,
        open_id=open_id,
        chat_id=chat_id,
        gateway=gateway,
        event=event,
        interval=int(session.get("interval") or 3),
        auth_card=auth_card,
    )


def _profile_open_id_for_auth(profile_name: Optional[str]) -> Optional[str]:
    if not profile_name:
        return None
    table = _m._get_routing_table()
    if table is None:
        return None
    try:
        db_path = table.db_path
    except AttributeError:
        return None
    try:
        import sqlite3

        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            row = conn.execute(
                "SELECT open_id FROM multitenancy_routing "
                "WHERE profile_name = ? AND active = 1 AND kind = 'user' "
                "AND open_id LIKE 'ou_%' ORDER BY updated_at DESC LIMIT 1",
                (profile_name,),
            ).fetchone()
    except Exception as exc:
        _m.logger.debug("multitenancy: profile auth open_id lookup failed (%s)", exc)
        return None
    return str(row[0]) if row else None


def _start_feishu_auth_poll_task(
    *,
    session_id: str,
    profile_name: str,
    open_id: str,
    chat_id: str,
    gateway: Any,
    event: Any,
    interval: int,
    auth_card: Optional[dict[str, Any]] = None,
) -> None:
    if not session_id:
        return
    task = asyncio.create_task(
        _m._poll_feishu_auth_session_until_done(
            session_id=session_id,
            profile_name=profile_name,
            open_id=open_id,
            chat_id=chat_id,
            gateway=gateway,
            event=event,
            interval=interval,
            auth_card=auth_card,
        ),
        name=f"feishu-auth:{profile_name}:{open_id}:{session_id}",
    )
    task.add_done_callback(lambda t: _m.logger.debug("Feishu auth poll task ended: %s", t.get_name()))


async def _dispatch_synthetic_auth_complete(
    *,
    event: Any,
    gateway: Any,
    chat_id: str,
    profile_name: str,
    open_id: str,
    text: str = _m.SYNTHETIC_AUTH_COMPLETE_TEXT,
) -> bool:
    try:
        clean_chat_id = str(chat_id or "").strip()
        clean_profile_name = str(profile_name or "").strip()
        if not clean_chat_id or not clean_profile_name:
            _m.logger.warning(
                "multitenancy: skip synthetic auth-complete dispatch without AI context "
                "chat_id=%r profile_name=%r",
                chat_id,
                profile_name,
            )
            return False

        if text == _m.SYNTHETIC_AUTH_COMPLETE_TEXT:
            replay = _m._take_pending_auth_replay(clean_profile_name, str(open_id or "").strip())
            if replay:
                text = replay
        source = getattr(event, "source", None)
        original_message_id = _m._event_message_id(event) or f"auth-complete:{int(time.time() * 1000)}"
        synthetic_message_id = f"{original_message_id}:auth-complete"
        sender_open_id = (
            _m._normalize_feishu_open_id(getattr(event, "sender_open_id", None))
            or _m._normalize_feishu_open_id(getattr(source, "open_id", None) if source is not None else None)
            or _m._normalize_feishu_open_id(open_id)
        )
        sender_id = sender_open_id or str(open_id or "").strip()

        synthetic_source = SimpleNamespace(
            chat_id=clean_chat_id,
            message_id=synthetic_message_id,
            parent_chat_id=getattr(source, "parent_chat_id", None) if source is not None else None,
            chat_id_alt=getattr(source, "chat_id_alt", None) if source is not None else None,
            user_id=sender_id,
            user_id_alt=getattr(source, "user_id_alt", None) if source is not None else None,
            open_id=sender_open_id,
            chat_type=getattr(source, "chat_type", None) if source is not None else None,
            platform=getattr(source, "platform", None) if source is not None else None,
        )
        synthetic_event = SimpleNamespace(
            text=text,
            message_id=synthetic_message_id,
            sender_open_id=sender_open_id or sender_id,
            source=synthetic_source,
            raw_event={
                "event": {
                    "message": {
                        "message_id": synthetic_message_id,
                        "chat_id": clean_chat_id,
                        "chat_type": getattr(synthetic_source, "chat_type", None),
                    },
                    "sender": {"sender_id": {"open_id": sender_open_id or sender_id}},
                }
            },
        )

        await _m.handle_async(event=synthetic_event, gateway=gateway)
        return True
    except Exception as exc:
        _m.logger.warning("multitenancy: synthetic auth-complete dispatch failed: %s", exc)
        return False


async def _poll_feishu_auth_session_until_done(
    *,
    session_id: str,
    profile_name: str,
    open_id: str,
    chat_id: str,
    gateway: Any,
    event: Any,
    interval: int,
    auth_card: Optional[dict[str, Any]] = None,
) -> None:
    from .. import feishu_uat_auth
    from ..feishu_auth_cards import (
        build_auth_failed_card,
        build_auth_identity_mismatch_card,
        build_auth_success_card,
        update_auth_card,
    )

    adapter = _m._get_feishu_adapter(gateway)
    current_interval = max(int(interval or 3), 2)
    while True:
        await asyncio.sleep(current_interval)
        try:
            session = await asyncio.to_thread(
                feishu_uat_auth.poll_session,
                session_id=session_id,
                profile_name=profile_name,
                open_id=open_id,
            )
        except feishu_uat_auth.FeishuUatAuthError as exc:
            if adapter is not None:
                message = str(exc.message or "")
                card = build_auth_identity_mismatch_card() if "does not match" in message else build_auth_failed_card(message)
                updated = await update_auth_card(adapter=adapter, auth_card=auth_card, card=card)
                if not updated:
                    await _m._safe_call(adapter.send, chat_id, f"飞书 UAT 授权失败：{exc.message}")
            return
        status = str(session.get("status") or "")
        if status == "pending":
            current_interval = max(int(session.get("interval") or current_interval), 2)
            continue
        if adapter is None:
            return
        if status == "success":
            updated = await update_auth_card(adapter=adapter, auth_card=auth_card, card=build_auth_success_card())
            if not updated:
                await _m._safe_call(adapter.send, chat_id, "✅ 飞书 UAT 授权完成，后续 lark_cli 将优先使用你的 user 身份。")
            await _m._dispatch_synthetic_auth_complete(
                event=event,
                gateway=gateway,
                chat_id=chat_id,
                profile_name=profile_name,
                open_id=open_id,
            )
        elif status == "expired":
            updated = await update_auth_card(adapter=adapter, auth_card=auth_card, card=build_auth_failed_card("expired"))
            if not updated:
                await _m._safe_call(adapter.send, chat_id, "飞书 UAT 授权已过期，请重新发送 /feishu_auth。")
        else:
            error = str(session.get("error") or status or "unknown error")
            card = build_auth_identity_mismatch_card() if "does not match" in error else build_auth_failed_card(error)
            updated = await update_auth_card(adapter=adapter, auth_card=auth_card, card=card)
            if not updated:
                await _m._safe_call(adapter.send, chat_id, f"飞书 UAT 授权失败：{error}")
        return


def _filter_hub_rows_for_auth(rows: list) -> list:
    """Return only the 3 MVP credentials shown on /auth (lark-cli, keep-record,
    kep-cli), preserving CREDENTIAL_ORDER. feishu-project + gitlab are collected
    (other surfaces use them) but not surfaced in the Feishu hub."""
    from .. import credential_hub as _ch

    allowed = {_ch.LARK_CLI, _ch.KEEP_RECORD, *_ch.KEP_CLI_IDS}
    return [r for r in rows if r.id in allowed]


async def _handle_auth_command(
    *,
    args: str,
    sender: str,
    sender_alt: Optional[str],
    profile_name: Optional[str],
    profile_home: Optional[Path],
    chat_id: str,
    gateway: Any,
    event: Any,
) -> None:
    """Render the ``/auth`` credential hub — a collection card of all credentials.

    ``/feishu_auth`` is the lark-cli/feishu row of this hub. lark-cli is wired
    end-to-end here (reuses the device-flow session to mint its authorize URL +
    background poll). keep-record / kep-cli pre-generate their hub entry points
    here too, so the card renders static auth URLs / inline QR directly.
    """
    del args, event
    from .. import credential_hub
    from ..feishu_auth_cards import send_auth_card
    from ..feishu_credential_hub_cards import build_hub_card

    adapter = _m._get_feishu_adapter(gateway)
    open_id = (
        _m._normalize_feishu_open_id(sender)
        or _m._normalize_feishu_open_id(sender_alt)
        or _m._profile_open_id_for_auth(profile_name)
        or ""
    )
    if not _m._is_feishu_open_id(open_id):
        if adapter is not None:
            await _m._safe_call(adapter.send, chat_id, "无法打开凭证中心：当前消息没有可用的 sender open_id。")
        return
    if not profile_name:
        if adapter is not None:
            await _m._safe_call(adapter.send, chat_id, "无法打开凭证中心：当前飞书用户还没有绑定 Hermes profile。")
        return

    # Use the router's authoritative profile home when available so the hub
    # reads the same dotfile root the agent runtime writes to.
    home_dir = (Path(profile_home) / "home") if profile_home else None
    rows = await asyncio.to_thread(
        credential_hub.collect_credential_statuses,
        profile_name=profile_name,
        open_id=open_id,
        home_dir=home_dir,
    )
    if adapter is None:
        return
    rows = _m._filter_hub_rows_for_auth(rows)

    # Send the hub FAST: one unified 认证/重新认证 callback button per credential,
    # nothing pre-generated. Each credential's auth entry (device-flow session /
    # QR / kep login) is minted lazily only when the user clicks its button (see
    # feishu_auth_hub_actions._handle_cred_auth_action), which keeps /auth
    # instant and gives every credential — expired ones included — a re-auth
    # control in one unified interaction.
    # The callback payload carries NOTHING but the credential id — on click the
    # handler re-derives identity (operator), profile, AND chat from the
    # Feishu-SIGNED event, never the unsigned button value. So a forwarded /
    # stale card can't smuggle a spoofed chat_id/profile past the group-scope or
    # cross-user guards (see feishu_auth_hub_actions._handle_cred_auth_action).
    card = build_hub_card(rows=rows, ctx={})
    sent = await send_auth_card(adapter=adapter, chat_id=chat_id, card=card)
    if sent is None:
        lines = ["凭证中心："] + [
            f"- {row.title}: {'✅ 已认证' if row.authenticated else '⚠️ 未认证'}" for row in rows
        ]
        await _m._safe_call(adapter.send, chat_id, "\n".join(lines))
    else:
        # Record this card's signed ids as DM-originated: the click handler only
        # mints for a card it can prove was sent into a private chat (this send
        # site is group-blocked). A copy forwarded into a group gets a new id and
        # is rejected. See feishu_auth_hub_actions.record_dm_auth_card.
        from ..feishu_auth_hub_actions import record_dm_auth_card
        record_dm_auth_card(sent.get("message_id"), sent.get("card_id"))


def _track_kep_login_proc(proc: Any) -> None:
    """Hold a reference to a live kep-auth login proc; prune finished ones so the
    set doesn't grow unbounded across /auth invocations."""
    if proc is None:
        return
    for old in [p for p in _m._KEP_LOGIN_PROCS if getattr(p, "poll", lambda: 0)() is not None]:
        _m._KEP_LOGIN_PROCS.discard(old)
    _m._KEP_LOGIN_PROCS.add(proc)


def _handle_pending_approval_command(
    cmd: str,
    args: str,
    event: Any,
    *,
    profile_name: Optional[str],
    sender: str,
    sender_alt: Optional[str],
    chat_id: str,
) -> Optional[str]:
    """Resolve a child AIAgent approval bridge before falling back to gateway commands."""
    if cmd not in {"approve", "deny"}:
        return None
    session_key = _m._multitenant_gateway_session_key(
        event,
        profile_name=profile_name,
        sender=sender,
        sender_alt=sender_alt,
        chat_id=chat_id,
    )
    if not session_key or not _m._pending_approval_requests.get(session_key):
        return None

    if cmd == "deny":
        resolve_all = "all" in str(args or "").lower().split()
        count = _m._resolve_pending_approval_requests(session_key, "deny", resolve_all=resolve_all)
        count_msg = f" ({count} commands)" if count > 1 else ""
        return f"❌ Command{'s' if count > 1 else ''} denied{count_msg}."

    parts = str(args or "").strip().lower().split()
    resolve_all = "all" in parts
    remaining = [part for part in parts if part != "all"]
    if any(part in {"always", "permanent", "permanently"} for part in remaining):
        choice = "always"
        scope_msg = " (pattern approved permanently)"
    elif any(part in {"session", "ses"} for part in remaining):
        choice = "session"
        scope_msg = " (pattern approved for this session)"
    else:
        choice = "once"
        scope_msg = ""
    count = _m._resolve_pending_approval_requests(session_key, choice, resolve_all=resolve_all)
    count_msg = f" ({count} commands)" if count > 1 else ""
    return f"✅ Command{'s' if count > 1 else ''} approved{scope_msg}{count_msg}. The agent is resuming..."


def _resolve_pending_approval_requests(
    session_key: str,
    choice: str,
    *,
    resolve_all: bool = False,
) -> int:
    queue = _m._pending_approval_requests.get(session_key) or []
    if not queue:
        return 0
    if resolve_all:
        targets = list(queue)
        queue.clear()
    else:
        targets = [queue.pop(0)]
    if queue:
        _m._pending_approval_requests[session_key] = queue
    else:
        _m._pending_approval_requests.pop(session_key, None)
    for entry in targets:
        raw_decision_path = str(entry.get("decision_path") or "").strip()
        if not raw_decision_path:
            continue
        decision_path = Path(raw_decision_path)
        try:
            decision_path.parent.mkdir(parents=True, exist_ok=True)
            decision_path.write_text(json.dumps({"choice": choice}), encoding="utf-8")
        except Exception as exc:
            _m.logger.warning(
                "multitenancy: failed to write approval decision for %s: %s",
                entry.get("approval_id") or "?",
                exc,
            )
    return len(targets)


def _record_pending_approval(payload: dict) -> None:
    session_key = str(payload.get("session_key") or "").strip()
    decision_path = str(payload.get("decision_path") or "").strip()
    if not session_key or not decision_path:
        return
    approval_id = str(payload.get("approval_id") or decision_path)
    queue = _m._pending_approval_requests.setdefault(session_key, [])
    queue[:] = [
        item for item in queue
        if str(item.get("approval_id") or item.get("decision_path")) != approval_id
    ]
    queue.append(dict(payload))


def _clear_pending_approval(payload: dict) -> None:
    session_key = str(payload.get("session_key") or "").strip()
    approval_id = str(payload.get("approval_id") or "").strip()
    if not session_key or not approval_id:
        return
    queue = _m._pending_approval_requests.get(session_key) or []
    queue[:] = [item for item in queue if str(item.get("approval_id") or "") != approval_id]
    if queue:
        _m._pending_approval_requests[session_key] = queue
    else:
        _m._pending_approval_requests.pop(session_key, None)


async def _handle_child_approval_required(adapter: Any, chat_id: str, payload: Any) -> None:
    data = payload if isinstance(payload, dict) else {}
    _m._record_pending_approval(data)
    command = str(data.get("command") or "")
    description = str(data.get("description") or "dangerous command")
    _m.logger.info(
        "multitenancy child approval_required session=%s approval_id=%s command=%s",
        str(data.get("session_key") or ""),
        str(data.get("approval_id") or ""),
        command[:120],
    )
    # Audit only the executable basename — never a raw path or any secret
    # fragment (SPEC: 无 raw 路径/无 secret). _safe_command_kind handles quoted
    # env-assignment values via shlex. Full command stays correlatable by hash.
    _m.append_security_event(
        event_type="approval.requested",
        open_id=str(data.get("open_id") or "").strip() or None,
        command_hash=hashlib.sha256(command.encode("utf-8")).hexdigest()[:12],
        command_kind=_m._safe_command_kind(command),
        reason="dangerous_command",
        decision="requested",
    )
    preview = command[:200] + "..." if len(command) > 200 else command
    message = (
        "⚠️ Dangerous command requires approval:\n"
        f"```\n{preview}\n```\n"
        f"Reason: {description}\n\n"
        "Reply `/approve` to execute, `/approve session` to approve this pattern "
        "for the session, `/approve always` to approve permanently, or `/deny` to cancel."
    )
    if adapter is not None:
        await _m._safe_call(adapter.send, chat_id, message)


async def _dispatch_gateway_command(
    cmd: str,
    event: Any,
    gateway: Any,
    *,
    sender: str,
    sender_alt: Optional[str],
    profile_name: Optional[str],
    profile_home: Optional[Path],
    chat_id: str,
) -> Optional[str]:
    """Delegate a Hermes-known slash command to the gateway when possible."""
    _m._ensure_command_event_methods(event, cmd)

    dispatcher = getattr(gateway, "_dispatch_slash_command", None)
    if callable(dispatcher):
        async with _m._profile_gateway_context(
            gateway,
            event,
            sender=sender,
            sender_alt=sender_alt,
            profile_name=profile_name,
            profile_home=profile_home,
            chat_id=chat_id,
        ):
            try:
                result = dispatcher(event, multitenancy_context={
                    "profile_name": profile_name,
                    "profile_home": str(profile_home) if profile_home else "",
                    "sender_open_id": sender,
                    "session_key_override": _m._multitenant_gateway_session_key(
                        event,
                        profile_name=profile_name,
                        sender=sender,
                        sender_alt=sender_alt,
                        chat_id=chat_id,
                    ),
                })
            except TypeError:
                result = dispatcher(event)
            if asyncio.iscoroutine(result):
                result = await result
            _m.logger.info("Hermes gateway command handled: %s", cmd)
            return str(result) if result is not None else None

    handler = _m._gateway_handler_for_command(gateway, cmd)
    if handler is None:
        quick_result = await _m._dispatch_quick_command(
            cmd,
            event,
            gateway,
            sender=sender,
            sender_alt=sender_alt,
            profile_name=profile_name,
            profile_home=profile_home,
            chat_id=chat_id,
        )
        if quick_result is not None:
            return quick_result
        return await _m._dispatch_plugin_command(
            cmd,
            event,
            gateway,
            sender=sender,
            sender_alt=sender_alt,
            profile_name=profile_name,
            profile_home=profile_home,
            chat_id=chat_id,
        )
    async with _m._profile_gateway_context(
        gateway,
        event,
        sender=sender,
        sender_alt=sender_alt,
        profile_name=profile_name,
        profile_home=profile_home,
        chat_id=chat_id,
    ):
        result = handler(event)
        if asyncio.iscoroutine(result):
            result = await result
    _m.logger.info("Hermes gateway command handled: %s", cmd)
    return str(result) if result is not None else None


async def _dispatch_quick_command(
    cmd: str,
    event: Any,
    gateway: Any,
    *,
    sender: str,
    sender_alt: Optional[str],
    profile_name: Optional[str],
    profile_home: Optional[Path],
    chat_id: str,
) -> Optional[str]:
    """Handle Hermes config.quick_commands without copying the command list."""
    qcmd = _m._get_quick_command(gateway, cmd)
    if not isinstance(qcmd, dict):
        return None

    kind = qcmd.get("type")
    if kind == "exec":
        exec_cmd = qcmd.get("command", "")
        if not exec_cmd:
            return f"Quick command '/{cmd}' has no command defined."
        if not _m._quick_exec_allowed(gateway, qcmd):
            return (
                f"Quick command '/{cmd}' exec is disabled for Feishu multitenancy. "
                "Enable only after profile sandboxing is in place."
            )
        _m.logger.info("Hermes quick command exec: %s", cmd)
        try:
            env = os.environ.copy()
            if profile_home is not None:
                env["HERMES_HOME"] = str(profile_home)
            proc = await asyncio.create_subprocess_shell(
                exec_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = (stdout or stderr).decode().strip()
            return output if output else "Command returned no output."
        except asyncio.TimeoutError:
            return "Quick command timed out (30s)."
        except Exception as exc:
            return f"Quick command error: {exc}"

    if kind == "alias":
        target = str(qcmd.get("target", "") or "").strip()
        if not target:
            return f"Quick command '/{cmd}' has no target defined."
        target = target if target.startswith("/") else f"/{target}"
        target_command = target.lstrip("/")
        new_cmd = target_command.split()[0] if target_command else ""
        if not new_cmd or new_cmd == cmd:
            return f"Quick command '/{cmd}' has invalid target."
        user_args = _m._command_args_from_event(event).strip()
        setattr(event, "text", f"{target} {user_args}".strip())
        _m._set_command_event_methods(event, new_cmd)
        _m.logger.info("Hermes quick command alias: %s -> %s", cmd, target)
        return await _m._dispatch_gateway_command(
            new_cmd,
            event,
            gateway,
            sender=sender,
            sender_alt=sender_alt,
            profile_name=profile_name,
            profile_home=profile_home,
            chat_id=chat_id,
        )

    return f"Quick command '/{cmd}' has unsupported type (supported: 'exec', 'alias')."


def _get_quick_command(gateway: Any, cmd: str) -> Any:
    config = getattr(gateway, "config", None)
    if isinstance(config, dict):
        quick_commands = config.get("quick_commands", {}) or {}
    else:
        quick_commands = getattr(config, "quick_commands", {}) or {}
    if not isinstance(quick_commands, dict):
        return None
    return quick_commands.get(cmd)


def _quick_exec_allowed(gateway: Any, qcmd: dict[str, Any]) -> bool:
    """Return True only when multitenant Feishu quick exec is explicitly enabled."""
    qcmd_flag = qcmd.get("multitenancy_allow_exec")
    if qcmd_flag is None:
        qcmd_cfg = qcmd.get("multitenancy")
        if isinstance(qcmd_cfg, dict):
            qcmd_flag = qcmd_cfg.get("allow_exec")
    if qcmd_flag is not None:
        return _m._truthy(qcmd_flag)

    config = getattr(gateway, "config", None)
    plugin_cfg = None
    if isinstance(config, dict):
        plugin_cfg = config.get("multitenancy")
    else:
        plugin_cfg = getattr(config, "multitenancy", None)
    if isinstance(plugin_cfg, dict) and "allow_quick_exec" in plugin_cfg:
        return _m._truthy(plugin_cfg.get("allow_quick_exec"))

    env_value = os.getenv("HERMES_MULTITENANCY_ALLOW_QUICK_EXEC")
    return _m._truthy(env_value) if env_value is not None else False


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "allow", "enabled"}


async def _dispatch_plugin_command(
    cmd: str,
    event: Any,
    gateway: Any,
    *,
    sender: str,
    sender_alt: Optional[str],
    profile_name: Optional[str],
    profile_home: Optional[Path],
    chat_id: str,
) -> Optional[str]:
    """Delegate plugin-registered slash commands to Hermes' plugin manager."""
    handler = _m._get_plugin_command_handler(cmd)
    if handler is None:
        return None
    _m.logger.info("Hermes plugin slash handler: %s", cmd.replace("_", "-"))
    user_args = ""
    get_args = getattr(event, "get_command_args", None)
    if callable(get_args):
        user_args = (get_args() or "").strip()
    async with _m._profile_gateway_context(
        gateway,
        event,
        sender=sender,
        sender_alt=sender_alt,
        profile_name=profile_name,
        profile_home=profile_home,
        chat_id=chat_id,
    ):
        result = handler(user_args)
        if asyncio.iscoroutine(result):
            result = await result
    return str(result) if result else None


def _get_plugin_command_handler(cmd: str) -> Any:
    """Return Hermes' plugin command handler for ``cmd`` when available."""
    try:
        from hermes_cli.plugins import get_plugin_command_handler  # type: ignore

        return get_plugin_command_handler(cmd.replace("_", "-"))
    except Exception as exc:
        _m.logger.debug("multitenancy: plugin command lookup failed (%s)", exc)
        return None


def _gateway_handler_for_command(gateway: Any, cmd: str) -> Any:
    """Return Hermes' handler method using naming conventions, not a command table."""
    normalized = cmd.replace("-", "_")
    candidates = [f"_handle_{normalized}_command"]
    if normalized == "sethome":
        candidates.append("_handle_set_home_command")
    for name in candidates:
        handler = getattr(gateway, name, None)
        if callable(handler):
            return handler
    return None


def _ensure_command_event_methods(event: Any, cmd: str) -> None:
    """Add minimal MessageEvent command helpers for tests/fallback objects."""
    args = _m._command_args_from_event(event)
    if not callable(getattr(event, "get_command", None)):
        setattr(event, "get_command", lambda: cmd)
    if not callable(getattr(event, "get_command_args", None)):
        setattr(event, "get_command_args", lambda: args)


def _set_command_event_methods(event: Any, cmd: str) -> None:
    args = _m._command_args_from_text(getattr(event, "text", "") or "")
    setattr(event, "get_command", lambda: cmd)
    setattr(event, "get_command_args", lambda: args)


def _command_args_from_event(event: Any) -> str:
    get_args = getattr(event, "get_command_args", None)
    if callable(get_args):
        return str(get_args() or "")
    return _m._command_args_from_text(getattr(event, "text", "") or "")


def _command_args_from_text(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _event_platform_value(event: Any) -> Optional[str]:
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None) if source is not None else None
    value = getattr(platform, "value", platform)
    return str(value) if value else None


def _event_locale(event: Any) -> str:
    """Resolve a Feishu locale from the event, defaulting to zh_cn."""
    source = getattr(event, "source", None)
    raw = None
    if source is not None:
        raw = getattr(source, "locale", None) or getattr(source, "language", None)
    from ..diagnostics import _normalize_locale

    return _normalize_locale(raw)
