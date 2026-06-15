"""Feishu Helpdesk ticket-event handler — routes a helpdesk ticket event to the
helpdesk RAG and (optionally) replies into the ticket.

Transport reuse: the Feishu WS long-connection for the helpdesk app is owned by
Hermes (one long-connection per app — we never open a second). This module is the
*business* leg: given a raw helpdesk event payload (already delivered through that
tunnel and forwarded by the broker), it filters to the allowed helpdesk, retrieves
from the per-profile RAG index, composes a grounded answer, and replies.

SAFETY: the event subscription is app-level, so the real company IT helpdesk's
ticket events arrive on the SAME app channel. The FIRST thing we do is hard-filter
to the allowed helpdesk id; everything else is dropped. ``post`` defaults to False
(shadow mode) — nothing is written to a ticket until explicitly enabled.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

# Hard allow-list: only this helpdesk's events are ever acted on. The real IT
# helpdesk (1259 employees) shares the app channel and MUST be ignored here.
ALLOWED_HELPDESK_IDS = tuple(
    h.strip()
    for h in os.environ.get("HERMES_HELPDESK_ALLOWED_IDS", "7651445701632691164").split(",")
    if h.strip()
)

# Helpdesk ticket events we react to.
TICKET_MESSAGE_EVENTS = ("helpdesk.ticket_message.created_v1",)
TICKET_CREATED_EVENTS = ("helpdesk.ticket.created_v1",)


def _g(d: Any, *path: str, default: Any = None) -> Any:
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


def _event_type(payload: dict[str, Any]) -> str:
    return str(_g(payload, "header", "event_type") or payload.get("event_type") or "")


def _message_text_from_content(content: Any) -> str:
    """Same shape as inbound ticket messages: content is JSON string {"content": ...}."""
    if isinstance(content, dict):
        return str(content.get("content") or content.get("text") or "")
    if isinstance(content, str):
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            return content
        if isinstance(decoded, dict):
            return str(decoded.get("content") or decoded.get("text") or "")
        return content if not isinstance(decoded, str) else decoded
    return ""


def extract(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields we need from a helpdesk event payload (tolerant of shape)."""
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    ticket = event.get("ticket") if isinstance(event.get("ticket"), dict) else event
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    helpdesk_id = str(
        _g(ticket, "helpdesk_id")
        or event.get("helpdesk_id")
        or _g(message, "helpdesk_id")
        or ""
    )
    ticket_id = str(ticket.get("ticket_id") or event.get("ticket_id") or _g(message, "ticket_id") or "")
    text = _message_text_from_content(message.get("content"))
    # sender: ignore the bot's own messages to avoid reply loops
    sender_type = str(
        _g(message, "user_type") or _g(message, "message_type") or event.get("user_type") or ""
    )
    sender_id = str(_g(message, "user_id") or event.get("user_id") or "")
    return {
        "event_type": _event_type(payload),
        "helpdesk_id": helpdesk_id,
        "ticket_id": ticket_id,
        "text": text,
        "sender_id": sender_id,
        "sender_type": sender_type,
    }


def compose_answer_with_inference(question: str, hits: list[dict[str, Any]]) -> str:
    """Default composer: PAI Inference (sonnet) grounded in retrieved docs."""
    import subprocess

    ctx = "\n".join(
        f"- [{d.get('source')}] {str(d.get('title') or '').replace(chr(10), ' ')[:40]}: "
        f"{str(d.get('body') or '').replace(chr(10), ' ')[:240]}"
        for d in hits
    )
    system = (
        "你是公司 IT 服务台的智能客服。只依据【知识库命中】回答员工 IT 问题，给简洁可操作的步骤；"
        "知识库没有就直说不知道并建议输入[人工]转工程师。中文，不超过140字。"
    )
    user = f"员工问题：{question}\n\n【知识库命中】：\n{ctx}\n\n请基于上面知识回答。"
    inf = os.path.expanduser("~/.claude/PAI/TOOLS/Inference.ts")
    out = subprocess.run(
        ["bun", inf, "--level", "standard", system, user],
        capture_output=True, text=True, timeout=60,
    )
    return (out.stdout or out.stderr).strip()


def handle_helpdesk_event(
    payload: dict[str, Any],
    *,
    index: Any,
    reply_fn: Callable[[str, str], Any] | None = None,
    composer: Callable[[str, list[dict[str, Any]]], str] = compose_answer_with_inference,
    k: int = 3,
    post: bool = False,
) -> dict[str, Any]:
    """Process one helpdesk event. Returns a structured result (shadow-safe).

    ``index``   : a HelpdeskRagIndex (has .search(query, k)).
    ``reply_fn``: callable(ticket_id, text) -> None, used only when post=True.
    ``post``    : when False (default) compose the draft but DO NOT write to the ticket.
    """
    info = extract(payload)
    # 1) HARD SAFETY FILTER — only the allowed (test) helpdesk.
    if info["helpdesk_id"] not in ALLOWED_HELPDESK_IDS:
        return {"action": "drop", "reason": "helpdesk_id not allowed", "helpdesk_id": info["helpdesk_id"]}
    # 2) only react to user ticket messages with text
    if info["event_type"] not in TICKET_MESSAGE_EVENTS:
        return {"action": "skip", "reason": f"event {info['event_type']} not handled", **info}
    # 3) ignore the bot's own messages (loop guard)
    bot_open_id = os.environ.get("HERMES_HELPDESK_BOT_OPEN_ID", "").strip()
    if bot_open_id and info["sender_id"] == bot_open_id:
        return {"action": "skip", "reason": "own message (loop guard)", **info}
    question = info["text"].strip()
    if not question:
        return {"action": "skip", "reason": "empty text", **info}
    # 4) RAG retrieve
    hits = index.search(question, k=k)
    # 5) compose grounded answer
    answer = composer(question, hits)
    result = {
        "action": "reply" if post else "draft",
        "ticket_id": info["ticket_id"],
        "helpdesk_id": info["helpdesk_id"],
        "question": question,
        "hits": [{"source": d.get("source"), "title": d.get("title"), "score": d.get("score")} for d in hits],
        "answer": answer,
        "posted": False,
    }
    if post and reply_fn and info["ticket_id"]:
        reply_fn(info["ticket_id"], answer)
        result["posted"] = True
    return result
