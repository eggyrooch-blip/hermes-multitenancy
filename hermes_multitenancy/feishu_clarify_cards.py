"""Feishu CardKit bridge for the core AIAgent clarify callback."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .agent_real import _clarify_bridge_dir, _configure_webui_clarify_bridge
from .feishu_adapter_compat import load_feishu_adapter, log_feishu_adapter_load_error
from .feishu_auth_cards import send_auth_card, update_auth_card

logger = logging.getLogger(__name__)

_HOOK_INSTALLED = False
_CARD_ACTION_FLAG = "_hermes_multitenancy_clarify_card_action_patched"
_CLARIFY_ID_RE = re.compile(r"clarify_[0-9a-f]{32}")

# Truncate, never reject — a hostile/huge clarify payload must not bloat the card or the response file.
_MAX_QUESTION_CHARS = 2000  # ponytail: question markdown ceiling
_MAX_CHOICE_CHARS = 100  # ponytail: per-choice option text ceiling
_MAX_CHOICES = 10  # ponytail: rendered-choice ceiling, extras dropped
_MAX_ANSWER_CHARS = 2000  # ponytail: submitted-answer ceiling

# Same-chat stale-card invalidation. In-process only; a restart forgets these and
# falls back to first-writer-wins, which is acceptable.
_CLARIFY_CHAT_BY_ID: dict[str, str] = {}  # clarify_id -> chat_id
_LATEST_CLARIFY_BY_CHAT: dict[str, str] = {}  # chat_id -> latest clarify_id
_CLARIFY_CARD_BY_ID: dict[str, Any] = {}  # clarify_id -> send_auth_card handle
_CLARIFY_MAP_MAX = 256  # ponytail: in-flight clarify ceiling per process; oldest-first eviction


def _remember(store: dict[str, Any], key: str, value: Any) -> None:
    """Record key->value with most-recent at the end, bounded oldest-first."""
    if key in store:
        del store[key]
    store[key] = value
    while len(store) > _CLARIFY_MAP_MAX:
        del store[next(iter(store))]


def _is_stale_clarify(clarify_id: str) -> bool:
    """True only when this clarify is known AND a newer card superseded it for its chat.

    Unknown ids (process restart, never registered) and chats whose latest entry was
    evicted stay fail-open, so a legitimate answer is never wrongly rejected.
    """
    chat_id = _CLARIFY_CHAT_BY_ID.get(clarify_id)
    if chat_id is None:
        return False
    latest = _LATEST_CLARIFY_BY_CHAT.get(chat_id)
    return latest is not None and latest != clarify_id


def _configure_feishu_clarify_bridge(event_sink, session_key: str):
    """Reuse the core's response-file and timeout protocol for Feishu runs."""
    return _configure_webui_clarify_bridge(event_sink, session_key)


def build_clarify_card(*, clarify_id: str, question: Any, choices: Any) -> dict[str, Any]:
    raw_choices = choices if isinstance(choices, list) else []
    normalized_choices = [
        str(choice).strip()[:_MAX_CHOICE_CHARS]
        for choice in raw_choices
        if str(choice).strip()
    ][:_MAX_CHOICES]
    fields: list[dict[str, Any]]
    if normalized_choices:
        fields = [{
            "tag": "select_static",
            "name": "clarify_choice",
            "required": True,
            "placeholder": {"tag": "plain_text", "content": "请选择"},
            "options": [
                {"text": {"tag": "plain_text", "content": choice}, "value": choice}
                for choice in normalized_choices
            ],
        }]
    else:
        fields = [{
            "tag": "input",
            "name": "clarify_answer",
            "required": True,
            "placeholder": {"tag": "plain_text", "content": "请输入你的回答"},
        }]
    fields.append({
        "tag": "button",
        "name": "clarify_submit",
        "text": {"tag": "plain_text", "content": "提交"},
        "type": "primary",
        "form_action_type": "submit",
        "value": {"hermes_action": "clarify", "clarify_id": clarify_id},
        "behaviors": [{
            "type": "callback",
            "value": {"hermes_action": "clarify", "clarify_id": clarify_id},
        }],
    })
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "需要你的选择"}, "template": "blue"},
        "body": {"elements": [
            {"tag": "markdown", "content": str(question or "").strip()[:_MAX_QUESTION_CHARS]},
            {"tag": "form", "name": "clarify_form", "elements": fields},
        ]},
    }


async def handle_feishu_clarify_required(adapter: Any, chat_id: str, payload: Any) -> None:
    """Send the emitted clarify request as a CardKit form to this Feishu chat."""
    data = payload if isinstance(payload, dict) else {}
    clarify_id = str(data.get("clarify_id") or "")
    if not _CLARIFY_ID_RE.fullmatch(clarify_id):
        logger.warning("[multitenancy] ignored malformed clarify id")
        return
    # Register before sending so a later card for this chat supersedes this one.
    _remember(_CLARIFY_CHAT_BY_ID, clarify_id, chat_id)
    _remember(_LATEST_CLARIFY_BY_CHAT, chat_id, clarify_id)
    try:
        auth_card = await send_auth_card(
            adapter=adapter,
            chat_id=chat_id,
            card=build_clarify_card(
                clarify_id=clarify_id,
                question=data.get("question"),
                choices=data.get("choices"),
            ),
        )
        if auth_card:
            # Keep the handle so clarify_resolved can retire this exact card.
            _remember(_CLARIFY_CARD_BY_ID, clarify_id, auth_card)
    except Exception:
        logger.warning(
            "[multitenancy] clarify card delivery failed; unblocking agent with fallback",
            exc_info=True,
        )
        # Unblock the polling agent instead of stranding it for the full timeout.
        # The fallback write must ALSO never propagate — killing the stream here
        # is exactly the failure this except exists to prevent.
        try:
            _write_clarify_response(
                clarify_id,
                "The clarify card could not be delivered to the user. "
                "Use your best judgement to make the choice and proceed.",
            )
        except Exception:
            logger.warning("[multitenancy] clarify fallback write failed", exc_info=True)


def _clarify_final_card(*, timed_out: bool) -> dict[str, Any]:
    title, body, template = (
        ("问题已过期", "<font color='grey'>等待回答已超时，Hermes 将按最佳判断继续。</font>", "yellow")
        if timed_out
        else ("已回答", "已收到你的回答。", "green")
    )
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "body": {"elements": [{"tag": "markdown", "content": body}]},
    }


async def handle_feishu_clarify_resolved(adapter: Any, payload: Any) -> None:
    """Retire the pending clarify form to its terminal state, never blocking the run."""
    data = payload if isinstance(payload, dict) else {}
    clarify_id = str(data.get("clarify_id") or "")
    if not _CLARIFY_ID_RE.fullmatch(clarify_id):
        return
    # The pop is the write-once guard: a replayed bridge event, or a card whose
    # send returned no handle, is a no-op rather than a second edit.
    auth_card = _CLARIFY_CARD_BY_ID.pop(clarify_id, None)
    if not auth_card:
        return
    try:
        await update_auth_card(
            adapter=adapter,
            auth_card=auth_card,
            card=_clarify_final_card(timed_out=bool(data.get("timed_out"))),
        )
    except Exception:
        # Self-protecting like handle_feishu_clarify_required: the streaming loop
        # must never die because a card edit failed.
        logger.warning("[multitenancy] clarify terminal card update failed", exc_info=True)


def install_feishu_clarify_card_action_patch() -> None:
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    try:
        adapter_class = load_feishu_adapter()
    except Exception as exc:
        log_feishu_adapter_load_error(
            logger,
            "[multitenancy] FeishuAdapter not importable yet; clarify card-action hook deferred",
            exc,
        )
        return
    _HOOK_INSTALLED = _patch_card_action(adapter_class)


def handle_clarify_card_action(adapter: Any, cb: Any) -> Any:
    """Built-in clarify handler, owned by the one card-action dispatcher.

    Every outcome is CONSUMED: an invalid/stale/cross-chat submit answers with a
    toast instead of falling back to the core generic handler (which would
    synthesize a ``/card`` command out of the callback JSON)."""
    del adapter
    clarify_id = str(cb.value.get("clarify_id") or "")
    answer = _clarify_answer(cb.form_value)
    if not _CLARIFY_ID_RE.fullmatch(clarify_id) or not answer:
        return _toast_response("回答无效，请重新提交。", level="error")
    issued_chat = _CLARIFY_CHAT_BY_ID.get(clarify_id)
    signed_chat = cb.chat_id
    if issued_chat and signed_chat and signed_chat != issued_chat:
        # Feishu signs event.context — a click can't spoof it (same trust anchor
        # as feishu_auth_hub_actions._signed_chat_id). Cross-chat submits with a
        # leaked clarify id are rejected; unknown ids (process restart) or
        # missing context stay permissive.
        return _toast_response("该卡片不属于当前会话。", level="error")
    if _is_stale_clarify(clarify_id):
        return _toast_response("该卡片已过期，请在最新的卡片上回答。", level="error")
    if not _write_clarify_response(clarify_id, answer):
        return _toast_response("已提交，请勿重复操作。", level="info")
    return _toast_response("已提交，正在继续。")


def _patch_card_action(adapter_class: Any) -> bool:
    """Retired wrapper entry point — installs THE dispatcher instead."""
    from .feishu_card_action_dispatcher import install_feishu_card_action_dispatcher

    return install_feishu_card_action_dispatcher(adapter_class)


def _read_value(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _read_action_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _clarify_answer(form_value: Any) -> str:
    if not isinstance(form_value, dict):
        return ""
    answer = str(form_value.get("clarify_answer") or form_value.get("clarify_choice") or "").strip()
    return answer[:_MAX_ANSWER_CHARS]  # ponytail: cap oversized submitted answer


def _write_clarify_response(clarify_id: str, answer: str) -> bool:
    path = _clarify_bridge_dir() / f"{clarify_id}.json"
    fd, temporary = tempfile.mkstemp(prefix=f".{clarify_id}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"response": answer}, handle, ensure_ascii=False)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def _toast_response(content: str, *, level: str = "success") -> dict[str, Any]:
    return {"toast": {"type": level, "content": content}}
