"""Deterministic in-chat cron trigger (SPEC cron-trigger-deterministic-exec).

Prod 2026-08-03 10:56 / 2026-08-02 15:25: a "触发一下这个job <id>" DM reached
the model, which answered "已触发…运行中" with tool_turns=0 — no tool call, no
fire, no result, and the user could not tell. An instruction this mechanical
must not depend on the model choosing to call a tool (doctrine: 确定性注入,
same family as lark_cli_guard's direct-exec block).

Matcher is deliberately narrow — trigger verb + exactly one 12-hex job id in a
short DM, minus question/negation markers. Ownership needs no extra check:
``cron_api.trigger_job`` only sees the sender's own profile jobs and 404s
otherwise. Every non-match, miss, or error falls open to the normal agent
turn, so the worst case is exactly today's behavior.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_JOB_ID_RE = re.compile(r"\b([0-9a-f]{12})\b")
# Imperative anchor (codex review round-1): the trigger verb must OPEN the
# instruction (after at most a polite/manual prefix), so discussion like
# "do not trigger <id>" / "why did trigger <id>" / forwarded prose never fires.
_ANCHORED_VERB_RE = re.compile(
    r"^(?:请|麻烦|帮我|帮忙|手动|再|please\s+)*\s*(?:触发(?!器)|trigger\b)",
    re.IGNORECASE,
)
# Question / negation markers (zh + en) → discussion, not an instruction.
_BAIL_RE = re.compile(
    r"[?？]|为什么|为啥|吗|别|不要|没"
    r"|\b(?:don'?t|do\s+not|not|never|stop|cancel|why|how|what|when|did)\b",
    re.IGNORECASE,
)
_MAX_INSTRUCTION_LEN = 64

# Feishu redelivers a WS event that wasn't acked in time; this interception sits
# BEFORE the run broker's durable message-id dedup, so it needs its own gate or a
# redelivery re-fires the job (codex review round-1). ponytail: process-local
# TTL dict, single event loop, single prod gateway — move to the durable store
# only if multi-replica ever becomes real.
_SEEN_MESSAGE_TTL_SECONDS = 15 * 60
_seen_message_ids: dict[str, float] = {}


def _reserve_message(message_id: Optional[str]) -> bool:
    """Atomically reserve ``message_id`` (single event loop, no await inside).

    Reserving BEFORE the trigger await closes the concurrent-redelivery window
    codex round-2 reproduced (check and mark separated by an await → double
    fire). Returns False when the id is already reserved/handled.
    """
    if not message_id:
        return True
    now = time.monotonic()
    for key, stamp in list(_seen_message_ids.items()):
        if now - stamp > _SEEN_MESSAGE_TTL_SECONDS:
            del _seen_message_ids[key]
    if message_id in _seen_message_ids:
        return False
    _seen_message_ids[message_id] = now
    return True


def _release_message(message_id: Optional[str]) -> None:
    """A failed trigger must not block the model path for a later redelivery."""
    if message_id:
        _seen_message_ids.pop(message_id, None)


def match_trigger_text(text: Any) -> Optional[str]:
    """Return the job id iff ``text`` is a short, unambiguous trigger instruction."""
    value = str(text or "").strip()
    if not value or len(value) > _MAX_INSTRUCTION_LEN:
        return None
    if _BAIL_RE.search(value):
        return None
    if not _ANCHORED_VERB_RE.search(value):
        return None
    ids = _JOB_ID_RE.findall(value)
    if len(ids) != 1:
        return None
    return ids[0]


async def try_route_cron_trigger(
    adapter: Any,
    *,
    chat_id: str,
    profile_name: Optional[str],
    text: Any,
    message_id: Optional[str] = None,
) -> bool:
    """Trigger the sender's own cron job deterministically; True = consumed.

    The "已触发" reply is only sent AFTER ``cron_api.trigger_job`` returns — the
    claim is machine-backed, never a promise. Any miss or error returns False so
    the message keeps flowing to the normal agent turn (fail-open). A redelivered
    ``message_id`` that already fired is consumed silently (no second trigger,
    no second ack).
    """
    try:
        job_id = match_trigger_text(text)
        if job_id is None or not profile_name:
            return False
        if not _reserve_message(message_id):
            logger.info(
                "[multitenancy] cron trigger duplicate delivery consumed message_id=%s",
                message_id,
            )
            return True

        from . import cron_api

        try:
            job = await asyncio.to_thread(cron_api.trigger_job, profile_name, job_id)
        except cron_api.CronApiError:
            # Not this sender's job (or invalid) — let the model handle the text.
            _release_message(message_id)
            return False
        except Exception:
            _release_message(message_id)
            raise

        logger.info(
            "[multitenancy] deterministic cron trigger job=%s profile=%s",
            job_id,
            profile_name,
        )
        name = str(job.get("name") or job_id)
        reply = (
            f"✅ 已触发「{name}」（job_id: {job_id}），将在下一个调度周期内执行，"
            "结果会自动回传到这里。"
        )
        if adapter is not None:
            try:
                await adapter.send(chat_id, reply)
            except Exception:
                # The fire already happened; a lost ack must not un-consume it.
                logger.warning(
                    "[multitenancy] cron trigger ack send failed job=%s", job_id,
                    exc_info=True,
                )
        return True
    except Exception:
        logger.exception("[multitenancy] cron trigger interception failed — fail-open")
        return False
