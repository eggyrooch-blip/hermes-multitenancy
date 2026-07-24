"""Feishu CardKit bridge for the core AIAgent clarify callback."""
from __future__ import annotations

import functools
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .agent_real import _clarify_bridge_dir, _configure_webui_clarify_bridge
from .feishu_adapter_compat import load_feishu_adapter, log_feishu_adapter_load_error
from .feishu_auth_cards import send_auth_card

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
_CLARIFY_MAP_MAX = 256  # ponytail: in-flight clarify ceiling per process; oldest-first eviction


def _remember(store: dict[str, str], key: str, value: str) -> None:
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
        await send_auth_card(
            adapter=adapter,
            chat_id=chat_id,
            card=build_clarify_card(
                clarify_id=clarify_id,
                question=data.get("question"),
                choices=data.get("choices"),
            ),
        )
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


def _patch_card_action(adapter_class: Any) -> bool:
    original = getattr(adapter_class, "_on_card_action_trigger", None)
    if original is None:
        return False
    if getattr(original, _CARD_ACTION_FLAG, False):
        return True

    @functools.wraps(original)
    def wrapped(self: Any, data: Any) -> Any:
        try:
            event = _read_value(data, "event")
            action = _read_value(event, "action")
            value = _read_action_value(_read_value(action, "value"))
            if not isinstance(value, dict) or value.get("hermes_action") != "clarify":
                return original(self, data)
            clarify_id = str(value.get("clarify_id") or "")
            answer = _clarify_answer(_read_value(action, "form_value"))
            if not _CLARIFY_ID_RE.fullmatch(clarify_id) or not answer:
                return _toast_response("回答无效，请重新提交。", level="error")
            issued_chat = _CLARIFY_CHAT_BY_ID.get(clarify_id)
            signed_chat = str(_read_value(_read_value(event, "context"), "open_chat_id") or "").strip()
            if issued_chat and signed_chat and signed_chat != issued_chat:
                # Feishu signs event.context — a click can't spoof it (same trust
                # anchor as feishu_auth_hub_actions._signed_chat_id). Cross-chat
                # submits with a leaked clarify id are rejected; unknown ids
                # (process restart) or missing context stay fail-open.
                return _toast_response("该卡片不属于当前会话。", level="error")
            if _is_stale_clarify(clarify_id):
                return _toast_response("该卡片已过期，请在最新的卡片上回答。", level="error")
            if not _write_clarify_response(clarify_id, answer):
                return _toast_response("已提交，请勿重复操作。", level="info")
            return _toast_response("已提交，正在继续。")
        except Exception:
            logger.debug(
                "[multitenancy] clarify card action failed; delegating to original",
                exc_info=True,
            )
            return original(self, data)

    setattr(wrapped, _CARD_ACTION_FLAG, True)
    adapter_class._on_card_action_trigger = wrapped
    logger.info("[multitenancy] installed clarify card-action hook on %s.FeishuAdapter", adapter_class.__module__)
    return True


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
