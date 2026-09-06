"""Command dispatch helpers split out of router god-node (pure move).

Shim helpers/state routed through ``_m`` for monkeypatch fidelity.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from .. import router as _m
from .. import turn_tool_context
from ..feishu_message_trace import reset_trace_context, set_trace_context
from .feishu_completion import (
    deferred_completion_is_covered,
    has_deferred_completion_claim,
)
from .feishu_execution import (
    _carry_admission,
    _carry_session_id,
    _complete_feishu_processing,
    execute_admitted_feishu_run,
)
from .vision_admission import (
    attach_vision_block,
    send_vision_block_before_admission,
)


async def handle_async(*, event: Any, gateway: Any) -> None:
    """Async dispatch — orchestrates routing + pool + adapter calls + commands."""
    from .. import expert_bot_route
    from ..commands import parse_command

    trace_tokens = None
    try:
        trusted_admission = getattr(event, "trusted_feishu_ingress_admission", None)
        if trusted_admission is not None:
            from ..trusted_feishu_ingress import validate_admitted_feishu_event

            if not validate_admitted_feishu_event(event, gateway):
                _m.logger.warning("multitenancy: trusted Feishu context changed before execution")
                return
            trusted_profile_name = trusted_admission.profile_name
            trusted_profile_home = _m._profile_name_to_home(trusted_profile_name)
            if not trusted_profile_home.is_dir():
                _m.logger.warning("multitenancy: trusted Feishu profile became unavailable")
                return
        else:
            trusted_profile_name = None
            trusted_profile_home = None

        source = getattr(event, "source", None)
        chat_id = (
            trusted_admission.chat_id
            if trusted_admission is not None
            else (getattr(source, "chat_id", "unknown") if source else "unknown")
        )
        try:
            # Trace setup must never affect message handling: a raw_event whose
            # "event" is truthy-but-not-a-dict would AttributeError in
            # _event_message_id and silently swallow the whole message.
            trace_tokens = set_trace_context(_m._event_message_id(event), chat_id)
        except Exception:
            pass
        fallback_sender = getattr(source, "user_id", "unknown") if source else "unknown"
        sender = (
            trusted_admission.actor_subject
            if trusted_admission is not None
            else _m._resolve_sender_for_routing(event, fallback=fallback_sender)
        )
        if _m._is_feishu_open_id(sender):
            setattr(event, "sender_open_id", sender)
        text = getattr(event, "text", "") or ""
        chat_type = (
            trusted_admission.chat_type
            if trusted_admission is not None
            else (
                _m._extract_chat_type(event)
            )
        )
        fixed_context: expert_bot_route.FixedExpertContext | None = None
        fixed_expert_id = expert_bot_route.fixed_expert_id_from_env()
        if fixed_expert_id:
            adapter = _m._get_feishu_adapter(gateway)
            if _m._is_group_chat_type(chat_type):
                if adapter is not None:
                    await _m._safe_call(adapter.send, chat_id, "只读专家入口仅支持与专家 bot 私聊。")
                return
            fixed_result = expert_bot_route.resolve_fixed_expert_context(
                event,
                routing_table=_m._get_routing_table(),
                profile_home_resolver=_m._profile_name_to_home,
                expert_id=fixed_expert_id,
            )
            if isinstance(fixed_result, expert_bot_route.FixedExpertRejection):
                _m.logger.info(
                    "multitenancy: fixed expert route rejected reason=%s chat_id=%s",
                    fixed_result.reason,
                    chat_id,
                )
                if adapter is not None:
                    await _m._safe_call(adapter.send, chat_id, fixed_result.message)
                return
            fixed_context = fixed_result
            if trusted_admission is not None and (
                fixed_context.profile_name != trusted_profile_name
                or fixed_context.canonical_open_id != trusted_admission.actor_subject
            ):
                _m.logger.warning("multitenancy: fixed expert route disagrees with trusted admission")
                return
            expert_bot_route.apply_fixed_expert_context(event, fixed_context)
            sender = fixed_context.canonical_open_id

        if _m._is_reaction_synthetic_event(event, text):
            _m.logger.info(
                "multitenancy: skipping Feishu reaction synthetic event "
                "text=%r message_id=%s chat_id=%s",
                text,
                _m._event_message_id(event) or "",
                chat_id,
            )
            return

        sender_alt = (
            fixed_context.union_id
            if fixed_context is not None
            else (getattr(source, "user_id_alt", None) if source else None)
        )
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
        is_group_chat = _m._is_group_chat_type(chat_type)
        group_profile_name: Optional[str] = None
        group_profile_home: Optional[Path] = None
        if trusted_admission is not None and is_group_chat:
            group_profile_name, group_profile_home = (
                trusted_profile_name,
                trusted_profile_home,
            )
        elif fixed_context is None and is_group_chat and chat_id and chat_id != "unknown":
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

        # Push-card confirm (LIVE synthetic-command path). This gateway routes a
        # card BUTTON click as a synthetic "/card <tag> [value]" COMMAND event
        # (adapter._handle_card_action_event → handle_message →
        # pre_gateway_dispatch → handle_async), so a confirm click lands HERE, not
        # on _on_card_action_trigger — cmd_pair is ("card", …), which would fall
        # through to the "命令但无调度器" reply (zero write, re-clickable button).
        # Detect the push-confirm submit inside the synthetic event's raw_message
        # and drive handle_confirm before parse_command dispatch, then short-
        # circuit. A non-confirm event fast-returns False with no adapter cost
        # (zero impact on normal chat / other card actions).
        from ..push_card_confirm import try_route_push_confirm_synthetic
        if await try_route_push_confirm_synthetic(gateway, event):
            return

        # Push-card fill loop (LIVE inbound path). The multitenancy router owns
        # inbound via pre_gateway_dispatch → handle_async and NEVER calls
        # FeishuAdapter._dispatch_inbound_event, so the matcher patch installed
        # on that method can't fire here. Route a DM reply that hits one of this
        # user's open push cards into the fill loop BEFORE it becomes a normal
        # agent turn, then short-circuit. Slash commands (cmd_pair) and reactions
        # (already returned above) never reach this; a non-push-card message
        # matches nothing and continues untouched (zero impact on normal chat).
        if cmd_pair is None and not is_group_chat:
            from ..push_card_matcher import try_route_push_card_reply
            if await try_route_push_card_reply(_m._get_feishu_adapter(gateway), event):
                return
            # A yolo-scene match passes through (returns False) but rewrites
            # event.text to inject the business-skill slash (/skill <reply>). Re-
            # sync the local text snapshot so the agent turn below runs the
            # injected skill. A non-yolo passthrough leaves event.text unchanged,
            # so this is a no-op there. cmd_pair stays None (it was parsed from
            # the pre-injection text) → the /skill lands as agent content, not a
            # router command, which is exactly the "由 agent 跑 skill" contract.
            text = getattr(event, "text", text) or text

            # Deterministic cron trigger (SPEC cron-trigger-deterministic-exec).
            # A short "触发…<job id>" DM must not depend on the model choosing to
            # call the trigger tool (prod 2026-08-03: tool_turns=0 + "已触发" =
            # nothing ran). Resolve the route read-only — an unrouted sender
            # falls through to the normal path below, which owns provisioning.
            # Skipped for a yolo-rewritten text (the injected `/skill …` line is
            # the push-card loop's payload, not a user instruction) and for
            # fixed-expert DMs (read-only entrance must not fire the canonical
            # profile's jobs) — codex review round-1.
            from ..cron_trigger_direct import try_route_cron_trigger
            from ..push_card_matcher import _YOLO_ROUTED_ATTR

            if trusted_admission is not None:
                trigger_profile_name, trigger_profile_home = (
                    trusted_profile_name,
                    trusted_profile_home,
                )
            else:
                trigger_profile_name, trigger_profile_home = _m._resolve_route(
                    sender, alt_id=sender_alt
                )
            if (
                trigger_profile_home is not None
                and fixed_context is None
                and getattr(event, _YOLO_ROUTED_ATTR, None) is None
            ) and await try_route_cron_trigger(
                _m._get_feishu_adapter(gateway),
                chat_id=chat_id,
                profile_name=trigger_profile_name,
                text=text,
                message_id=_m._event_message_id(event),
            ):
                trigger_adapter = _m._get_feishu_adapter(gateway)
                if trigger_adapter is not None and hasattr(
                    trigger_adapter, "on_processing_complete"
                ):
                    await _complete_feishu_processing(
                        trigger_adapter, event, failed=False
                    )
                return

        if cmd_pair is not None:
            if fixed_context is not None and not expert_bot_route.is_fixed_expert_slash_allowed(cmd_pair[0]):
                adapter = _m._get_feishu_adapter(gateway)
                if adapter is not None:
                    await _m._safe_call(
                        adapter.send,
                        chat_id,
                        expert_bot_route.fixed_expert_slash_deny_message(cmd_pair[0]),
                    )
                return

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
            elif trusted_admission is not None:
                cmd_profile_name = trusted_profile_name
                cmd_profile_home = trusted_profile_home
            elif fixed_context is not None:
                cmd_profile_name = fixed_context.profile_name
                cmd_profile_home = fixed_context.profile_home
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
        if trusted_admission is not None:
            profile_name, profile_home = trusted_profile_name, trusted_profile_home
        elif fixed_context is not None:
            profile_name, profile_home = fixed_context.profile_name, fixed_context.profile_home
        elif is_group_chat:
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

        # Resolve the trusted employee identity before any model call, including
        # image preprocessing. This happens before admission so a transient
        # LiteLLM management failure remains retryable with the same message.
        _m._materialize_inbound_media_for_profile(event, profile_home)
        seed_content = text or (
            "[media attachment]" if getattr(event, "media_urls", None) else ""
        )
        run_request = _m._run_request_for_routed_event(
            event=event,
            profile_name=profile_name,
            sender=sender,
            sender_alt=sender_alt,
            chat_id=chat_id,
            text=seed_content,
        )
        from ..billing_identity import prepare_billing_request
        from ..run_broker import RunRejected

        run_broker = _m._make_routed_run_broker(
            prepare_request=prepare_billing_request,
        )

        async def _enrich_prepared(prepared_run):
            prepared_request = prepared_run.request
            enriched = await _m._call_enrich_via_hermes_pipeline(
                event,
                gateway,
                profile_home=profile_home,
                billing_metadata=prepared_request.metadata,
            )
            run_content = enriched or text
            if not run_content and getattr(event, "media_urls", None):
                run_content = "[media attachment]"
            dispatch_request = _m._run_request_for_routed_event(
                event=event,
                profile_name=profile_name,
                sender=sender,
                sender_alt=sender_alt,
                chat_id=chat_id,
                text=run_content,
            )
            dispatch_request = replace(
                dispatch_request,
                metadata=prepared_request.metadata,
            )
            return attach_vision_block(
                prepared_run,
                dispatch_request,
                event=event,
                text=run_content,
            )

        async def _send_vision_block_before_admission(prepared_run):
            await send_vision_block_before_admission(
                prepared_run,
                adapter=adapter,
                chat_id=chat_id,
                profile_name=profile_name,
                event=event,
            )

        async def _execute_admitted(admitted_run):
            return await execute_admitted_feishu_run(
                admitted_run,
                run_broker=run_broker,
                event=event,
                gateway=gateway,
                adapter=adapter,
                chat_id=chat_id,
                profile_name=profile_name,
                profile_home=profile_home,
                sender=sender,
                sender_alt=sender_alt,
                text=text,
                feishu_full=feishu_full,
                completion_failed=completion_failed,
            )

        execution_owned = asyncio.Event()
        entry_completion_owned = asyncio.Event()
        entry_completion_started = asyncio.Event()
        completion_failed = asyncio.Event()

        async def _complete_shared_entry(entry_failed: bool) -> None:
            if feishu_full:
                await _complete_feishu_processing(
                    adapter,
                    event,
                    failed=entry_failed or completion_failed.is_set(),
                )

        pre_admission_failed = False
        try:
            try:
                run_admission = await run_broker.prepare_and_execute(
                    run_request,
                    execute=_execute_admitted,
                    transform_request=_enrich_prepared,
                    before_admit=_send_vision_block_before_admission,
                    execution_owned=execution_owned,
                    entry_completion_owned=entry_completion_owned,
                    entry_completion_started=entry_completion_started,
                    on_entry_done=_complete_shared_entry if feishu_full else None,
                )
            except asyncio.CancelledError:
                pre_admission_failed = True
                raise
            except RunRejected as exc:
                pre_admission_failed = True
                _m.logger.warning(
                    "multitenancy: employee billing preparation rejected "
                    "profile=%s sender=%s: %s",
                    profile_name,
                    sender,
                    exc,
                )
                if adapter is not None:
                    await _m._safe_call(
                        adapter.send,
                        chat_id,
                        "当前无法确认员工计费身份，请稍后重试。",
                    )
                return
            except Exception as exc:
                pre_admission_failed = True
                _m.logger.exception(
                    "multitenancy: durable run admission failed profile=%s sender=%s: %s",
                    profile_name,
                    sender,
                    exc,
                )
                if adapter is not None:
                    await _m._safe_call(
                        adapter.send,
                        chat_id,
                        "请求状态暂时无法保存，请稍后重试。",
                    )
                return
            if run_admission.duplicate:
                _m.logger.info(
                    "multitenancy: duplicate inbound event skipped profile=%s sender=%s message_id=%s",
                    profile_name,
                    sender,
                    _m._event_message_id(event) or "",
                )
                return
        finally:
            complete_locally = not entry_completion_owned.is_set()
            if (
                entry_completion_owned.is_set()
                and entry_completion_started.is_set()
                and has_deferred_completion_claim(adapter, event)
                and not deferred_completion_is_covered(adapter, event)
            ):
                # This hook deferred after the old shared finalizer had already
                # snapshotted its covered generations but before the entry was
                # removed from the inflight map. It needs its own completion.
                complete_locally = True
            if feishu_full and complete_locally:
                await _complete_feishu_processing(
                    adapter,
                    event,
                    failed=pre_admission_failed,
                )

        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _m.logger.exception("multitenancy: handle_async failed: %s", exc)
    finally:
        # Restore the caller's trace context. Matters for the nested
        # ``await handle_async(...)`` auth-complete replay, which runs in the
        # long-lived outer task's context (see _dispatch_synthetic_auth_complete).
        if trace_tokens is not None:
            reset_trace_context(trace_tokens)


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
            # Same reset, same breath: drop the carried tool transcript AND bump
            # the generation so a turn still in flight cannot repopulate it.
            reset_admission = _carry_admission(event, profile_name)
            if reset_admission is not None:
                turn_tool_context.invalidate(
                    profile_name,
                    reset_admission.actor_subject,
                    _carry_session_id(key, reset_admission),
                )
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


async def _handle_jit_auth_required(
    *,
    gateway: Any,
    adapter: Any,
    chat_id: str,
    profile_name: Optional[str],
    profile_home: Optional[Path],
    event: Any,
    payload: Any = None,
) -> None:
    """JIT device-code auth: a tool failed mid-run because the user lacks a Feishu
    scope (99991672 app-scope / 99991679 user-scope). Push the SAME device-flow
    auth card as ``/feishu_auth`` — but with the verification link wrapped as a
    sidebar applink — and reuse its poll task. On success the poll calls
    ``_dispatch_synthetic_auth_complete``, which replays the user's ORIGINAL
    request (captured in ``_capture_pending_auth_replay`` at inbound), so the
    stuck operation finishes itself with no re-typing.

    Non-blocking + refusable: the run's answer already streamed before this fires;
    ignoring the card just leaves the op undone (device code expires on its own).
    Identity is bound to the INITIATOR's open_id, so ``poll_session`` rejects a
    different account authorizing (group anti-spoof, free via the existing
    mismatch card). ``payload`` (broker scope_status) is unused for now — a
    full-grant card is enough (precise scope mapping is a follow-up slug)."""
    del profile_home
    from .. import feishu_uat_auth
    from ..feishu_auth_cards import (
        auth_text_fallback,
        build_auth_card,
        send_auth_card,
        to_in_app_web_url,
    )

    if adapter is None or not profile_name:
        return
    # GitLab credential delegation: a GROUP profile missing a GitLab credential
    # asks the INITIATOR (DM) to lend their personal token — never the Feishu
    # UAT device flow. Personal profiles keep their existing GitLab lane
    # unchanged (return without action).
    if isinstance(payload, dict) and str(payload.get("provider") or "") == "gitlab":
        if _m.is_group_profile_name(profile_name):
            from ..feishu_cred_delegation import handle_gitlab_delegation_required

            await handle_gitlab_delegation_required(
                gateway=gateway,
                adapter=adapter,
                chat_id=chat_id,
                profile_name=profile_name,
                event=event,
                payload=payload,
            )
        return
    del payload
    # 群/智能体 profile 只用应用(bot)身份——群里不弹「授权你个人 lark-cli」的卡
    # (sunke 设定)。双保险：broker 的 permission sink 已按 identity!=user 拦下，
    # 这里再从 profile 维度挡一次(防其它信号源/未来改动漏进群)。
    if _m.is_group_profile_name(profile_name):
        return
    open_id = (
        _m._normalize_feishu_open_id(getattr(event, "sender_open_id", None))
        or _m._normalize_feishu_open_id(
            getattr(getattr(event, "source", None), "user_id", None)
        )
        or _m._profile_open_id_for_auth(profile_name)
        or ""
    )
    if not _m._is_feishu_open_id(open_id):
        return
    try:
        # Reuse one live device code per (profile, open_id) so a run that fails on
        # several tool calls (or an expiry + permission in the same turn) shows ONE
        # card / drives ONE poll instead of racing duplicate sessions.
        session = feishu_uat_auth.find_active_session(
            profile_name=profile_name, open_id=open_id
        )
        if not session:
            session = feishu_uat_auth.start_session(
                profile_name=profile_name, open_id=open_id
            )
    except feishu_uat_auth.FeishuUatAuthError as exc:
        _m.logger.info("multitenancy: JIT auth session start failed: %s", exc.message)
        return

    session_id = str(session.get("session_id") or "")
    verification_uri = str(session.get("verification_uri") or "")
    user_code = str(session.get("user_code") or "")
    expires_at = float(session.get("expires_at") or 0)
    expires_min = max(1, int((expires_at - time.time() + 59) // 60)) if expires_at else 10

    card = build_auth_card(
        verification_uri=to_in_app_web_url(verification_uri),
        user_code=user_code,
        expires_min=expires_min,
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


# One live poller per device-code session. find_active_session already de-dups the
# device code across tool failures in a turn, but each JIT trigger (a concurrent run,
# or a run that both expires creds and hits a permission error) would still spawn its
# OWN poll task on that shared session_id → duplicate polls + double synthetic replay
# on success. Single asyncio loop, so this set's check+add is atomic (no await between).
# ponytail: process-local set, no lock — one event loop.
_ACTIVE_AUTH_POLLS: set[str] = set()


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
    if session_id in _ACTIVE_AUTH_POLLS:
        _m.logger.debug(
            "Feishu auth poll already live for session %s — skip duplicate", session_id
        )
        return
    _ACTIVE_AUTH_POLLS.add(session_id)
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

    def _on_poll_done(t: "asyncio.Task[Any]") -> None:
        _ACTIVE_AUTH_POLLS.discard(session_id)
        _m.logger.debug("Feishu auth poll task ended: %s", t.get_name())

    task.add_done_callback(_on_poll_done)


async def _dispatch_synthetic_auth_complete(
    *,
    event: Any,
    gateway: Any,
    chat_id: str,
    profile_name: str,
    open_id: str,
    text: str = _m.SYNTHETIC_AUTH_COMPLETE_TEXT,
    delegation_id: str = "",
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

        from ..runtime import strict_context_enabled

        if strict_context_enabled():
            await asyncio.to_thread(
                _m._resume_pending_lark_cli_step,
                _m._profile_name_to_home(clean_profile_name),
                str(open_id or "").strip(),
                clean_chat_id,
            )
            adapter = _m._get_feishu_adapter(gateway)
            if adapter is None:
                return False
            await _m._safe_call(
                adapter.send,
                clean_chat_id,
                "飞书授权已完成。为保护消息内容，原请求未保存也不会自动重放，请重新发送原请求。",
            )
            return True

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

        from ..feishu_group_topic_session import group_topic_conversation_id

        topic_conversation_id = group_topic_conversation_id(event)
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
            thread_id=getattr(source, "thread_id", None) if source is not None else None,
            chat_topic=getattr(source, "chat_topic", None) if source is not None else None,
            hermes_group_topic=getattr(source, "hermes_group_topic", False) if source is not None else False,
            hermes_raw_thread_id=getattr(source, "hermes_raw_thread_id", None) if source is not None else None,
            hermes_root_id=topic_conversation_id,
        )
        # Credential delegation: a 允许一次 grant is bound to THIS replay. Carried
        # both as an attribute and in raw_event metadata so it survives whichever
        # carrier the downstream run path reads.
        delegation_id = str(delegation_id or "").strip()
        synthetic_event = SimpleNamespace(
            text=text,
            message_id=synthetic_message_id,
            sender_open_id=sender_open_id or sender_id,
            source=synthetic_source,
            hermes_delegation_id=delegation_id,
            raw_event={
                **({"metadata": {"delegation_id": delegation_id}} if delegation_id else {}),
                "event": {
                    "message": {
                        "message_id": synthetic_message_id,
                        "chat_id": clean_chat_id,
                        "chat_type": getattr(synthetic_source, "chat_type", None),
                        "thread_id": getattr(synthetic_source, "thread_id", None),
                        "root_id": getattr(synthetic_source, "hermes_root_id", None),
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
        # Record this card's signed MESSAGE id as DM-originated: the click handler
        # only mints for a card it can prove was sent into a private chat (this
        # send site is group-blocked). A copy forwarded into a group gets a new
        # message_id and is rejected. Message id ONLY — a CardKit card_id survives
        # forwarding, so recording it would re-open the leak (codex round-7). See
        # feishu_auth_hub_actions.record_dm_auth_card.
        from ..feishu_auth_hub_actions import record_dm_auth_card
        record_dm_auth_card(sent.get("message_id"))


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
