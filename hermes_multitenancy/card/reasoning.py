"""Reasoning / thinking detection + label helpers.

Recognizes ``<think>`` / ``<thinking>`` / ``<thought>`` / ``<antthinking>``
tags (including unclosed tails), and ``Reasoning:`` text prefix. Produces
the 思考了 / Thought for Ns dual-language collapsed-panel header label.
"""
from __future__ import annotations

import re
import time
from typing import Any

_REASONING_TAG_RE = re.compile(
    r"<(think|thinking|thought|antthinking)\b[^>]*>([\s\S]*?)</\1>",
    re.IGNORECASE,
)
_UNCLOSED_REASONING_TAG_RE = re.compile(
    r"<(think|thinking|thought|antthinking)\b[^>]*>([\s\S]*)$",
    re.IGNORECASE,
)


def _split_reasoning_text(text: str) -> tuple[str, str]:
    raw = str(text or "")
    reasoning_parts = [_clean_reasoning_prefix(match.group(2)) for match in _REASONING_TAG_RE.finditer(raw)]
    unclosed = _UNCLOSED_REASONING_TAG_RE.search(raw)
    if unclosed and not _REASONING_TAG_RE.search(raw[unclosed.start() :]):
        reasoning_parts.append(_clean_reasoning_prefix(unclosed.group(2)))
    answer = _strip_reasoning_tags(raw).strip()

    prefix = re.match(r"^Reasoning:\s*([\s\S]*)$", answer, flags=re.IGNORECASE)
    if prefix:
        body = prefix.group(1).strip()
        if "\n\n" in body:
            reasoning, remaining = body.split("\n\n", 1)
            reasoning_parts.insert(0, _clean_reasoning_prefix(reasoning))
            answer = remaining.strip()
        else:
            reasoning_parts.insert(0, _clean_reasoning_prefix(body))
            answer = ""

    reasoning = "\n\n".join(part for part in reasoning_parts if part).strip()
    return answer, reasoning


def _strip_reasoning_tags(text: str) -> str:
    without_closed = _REASONING_TAG_RE.sub("", str(text or ""))
    without_unclosed = _UNCLOSED_REASONING_TAG_RE.sub("", without_closed)
    return without_unclosed


def _clean_reasoning_prefix(text: str) -> str:
    cleaned = re.sub(r"^Reasoning:\s*", "", str(text or ""), flags=re.IGNORECASE)
    cleaned = "\n".join(re.sub(r"^_(.+)_$", r"\1", line) for line in cleaned.splitlines())
    return cleaned.strip()


def _format_reasoning_label(state: dict[str, Any]) -> tuple[str, str]:
    elapsed = state.get("reasoning_elapsed")
    if not isinstance(elapsed, (int, float)) and state.get("reasoning_started_at"):
        try:
            elapsed = max(0.0, time.monotonic() - float(state["reasoning_started_at"]))
        except (TypeError, ValueError):
            elapsed = None
    if isinstance(elapsed, (int, float)):
        duration = _format_elapsed(float(elapsed))
        return f"思考了 {duration}", f"Thought for {duration}"
    return "思考", "Thought"


def _format_elapsed(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {round(seconds % 60)}s"
