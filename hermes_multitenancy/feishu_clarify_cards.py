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


def _configure_feishu_clarify_bridge(event_sink, session_key: str):
    """Reuse the core's response-file and timeout protocol for Feishu runs."""
    return _configure_webui_clarify_bridge(event_sink, session_key)


def build_clarify_card(*, clarify_id: str, question: Any, choices: Any) -> dict[str, Any]:
    normalized_choices = [str(choice).strip() for choice in choices or [] if str(choice).strip()]
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
    })
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "需要你的选择"}, "template": "blue"},
        "body": {"elements": [
            {"tag": "markdown", "content": str(question or "").strip()},
            {"tag": "form", "elements": fields},
        ]},
    }


async def handle_feishu_clarify_required(adapter: Any, chat_id: str, payload: Any) -> None:
    """Send the emitted clarify request as a CardKit form to this Feishu chat."""
    data = payload if isinstance(payload, dict) else {}
    clarify_id = str(data.get("clarify_id") or "")
    if not _CLARIFY_ID_RE.fullmatch(clarify_id):
        logger.warning("[multitenancy] ignored malformed clarify id")
        return
    await send_auth_card(
        adapter=adapter,
        chat_id=chat_id,
        card=build_clarify_card(
            clarify_id=clarify_id,
            question=data.get("question"),
            choices=data.get("choices"),
        ),
    )


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
    _patch_card_action(adapter_class)
    _HOOK_INSTALLED = True


def _patch_card_action(adapter_class: Any) -> None:
    original = getattr(adapter_class, "_on_card_action_trigger", None)
    if original is None or getattr(original, _CARD_ACTION_FLAG, False):
        return

    @functools.wraps(original)
    def wrapped(self: Any, data: Any) -> Any:
        try:
            event = _read_value(data, "event")
            action = _read_value(event, "action")
            value = _read_value(action, "value") or {}
            if not isinstance(value, dict) or value.get("hermes_action") != "clarify":
                return original(self, data)
            clarify_id = str(value.get("clarify_id") or "")
            answer = _clarify_answer(_read_value(action, "form_value"))
            if not _CLARIFY_ID_RE.fullmatch(clarify_id) or not answer:
                return _toast_response("回答无效，请重新提交。")
            _write_clarify_response(clarify_id, answer)
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


def _read_value(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _clarify_answer(form_value: Any) -> str:
    if not isinstance(form_value, dict):
        return ""
    return str(form_value.get("clarify_answer") or form_value.get("clarify_choice") or "").strip()


def _write_clarify_response(clarify_id: str, answer: str) -> Path:
    path = _clarify_bridge_dir() / f"{clarify_id}.json"
    fd, temporary = tempfile.mkstemp(prefix=f".{clarify_id}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"response": answer}, handle, ensure_ascii=False)
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def _toast_response(content: str) -> dict[str, Any]:
    return {"toast": {"type": "success", "content": content}}
