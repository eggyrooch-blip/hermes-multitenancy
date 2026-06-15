"""Feishu Helpdesk ticket-message event handler — routes a guest's question to the
helpdesk RAG and (optionally) replies into the ticket.

Schema (verified against the official helpdesk.ticket_message.created_v1 doc):
  event.text                      -> message text (or event.content.content for rich)
  event.ticket.ticket_id          -> ticket id
  event.sender_id.open_id         -> sender
  event.sender_type               -> 1=bot, 2=guest, 3=agent
  (there is NO helpdesk_id in the event payload)

SAFETY: the event subscription is app-level, so the real company IT helpdesk's ticket
events arrive on the SAME app channel — and the event carries no helpdesk_id to filter
on. So membership is confirmed by querying the ticket with the ALLOWED (test) helpdesk's
token (``membership_check``): a ticket that belongs to any other helpdesk fails that
query and is dropped. ``post`` defaults to False (shadow) — nothing is written to a
ticket until explicitly enabled.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

TICKET_MESSAGE_EVENTS = ("helpdesk.ticket_message.created_v1",)
SENDER_TYPE_BOT = 1
SENDER_TYPE_GUEST = 2
SENDER_TYPE_AGENT = 3


def _event_type(payload: dict[str, Any]) -> str:
    header = payload.get("header")
    if isinstance(header, dict) and header.get("event_type"):
        return str(header.get("event_type"))
    return str(payload.get("event_type") or "")


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def extract(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields we need from a helpdesk ticket_message event (real schema)."""
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    ticket = event.get("ticket") if isinstance(event.get("ticket"), dict) else {}
    ticket_id = str(ticket.get("ticket_id") or event.get("ticket_id") or "")

    # text: event.text (plain) or event.content.content (rich); tolerate a JSON string
    text = str(event.get("text") or "")
    if not text:
        content = event.get("content")
        if isinstance(content, dict):
            text = str(content.get("content") or content.get("text") or "")
        elif isinstance(content, str):
            try:
                decoded = json.loads(content)
                text = str(decoded.get("content") or decoded.get("text") or "") if isinstance(decoded, dict) else content
            except json.JSONDecodeError:
                text = content

    sender = event.get("sender_id") if isinstance(event.get("sender_id"), dict) else {}
    sender_open_id = str(sender.get("open_id") or sender.get("user_id") or "")
    sender_type = _coerce_int(event.get("sender_type"))
    return {
        "event_type": _event_type(payload),
        "ticket_id": ticket_id,
        "text": text,
        "sender_open_id": sender_open_id,
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
    membership_check: Callable[[str], bool],
    reply_fn: Callable[[str, str], Any] | None = None,
    composer: Callable[[str, list[dict[str, Any]]], str] = compose_answer_with_inference,
    k: int = 3,
    post: bool = False,
) -> dict[str, Any]:
    """Process one helpdesk ticket_message event (shadow-safe).

    ``membership_check(ticket_id) -> bool`` confirms the ticket belongs to the allowed
    (test) helpdesk; non-members are dropped before any RAG/compose/reply.
    """
    info = extract(payload)
    if info["event_type"] not in TICKET_MESSAGE_EVENTS:
        return {"action": "skip", "reason": f"event {info['event_type']} not handled", **info}
    # only answer GUEST messages — bot(1) is our own (loop guard), agent(3) is a human
    if info["sender_type"] != SENDER_TYPE_GUEST:
        return {"action": "skip", "reason": f"sender_type {info['sender_type']} not guest", **info}
    question = info["text"].strip()
    if not question:
        return {"action": "skip", "reason": "empty text", **info}
    if not info["ticket_id"]:
        return {"action": "skip", "reason": "no ticket_id", **info}
    # HARD SAFETY GATE: event has no helpdesk_id — confirm the ticket is in the allowed
    # (test) helpdesk by querying it with that helpdesk's token. Real IT-helpdesk tickets
    # fail this and are dropped, never answered.
    if not membership_check(info["ticket_id"]):
        return {"action": "drop", "reason": "ticket not in allowed helpdesk", "ticket_id": info["ticket_id"]}

    hits = index.search(question, k=k)
    answer = composer(question, hits)
    result = {
        "action": "reply" if post else "draft",
        "ticket_id": info["ticket_id"],
        "question": question,
        "hits": [{"source": d.get("source"), "title": d.get("title"), "score": d.get("score")} for d in hits],
        "answer": answer,
        "posted": False,
    }
    if post and reply_fn and info["ticket_id"]:
        reply_fn(info["ticket_id"], answer)
        result["posted"] = True
    return result
