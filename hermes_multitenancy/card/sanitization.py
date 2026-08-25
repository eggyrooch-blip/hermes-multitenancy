"""Text sanitization + summary helpers.

Pure text utilities — strip invalid image keys, clip, plain-summary, and
``_format`` which routes through the adapter's optional ``format_message`` hook.
"""
from __future__ import annotations

import re
from typing import Any

_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")

#: Literal newline escapes that reach the card as text instead of whitespace.
#: ``\r\n`` first so the pair collapses to one break, not two.
#:
#: The trailing lookahead is the whole safety story: an escape that is glued to a
#: word is almost certainly NOT whitespace — ``C:\Users\test``, ``\note``,
#: ``re.sub(r"\n+")`` — while a real escaped line break is followed by
#: whitespace, CJK, punctuation, another escape, or end of string. ``\t`` is
#: deliberately absent: it collides with far too many ordinary words for a
#: cosmetic gain, and a stray tab was never part of the reported damage.
_ESCAPED_NL_RE = re.compile(r"(?:\\r\\n|\\n|\\r)(?![A-Za-z0-9_])")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _unescape_display_whitespace(text: str) -> str:
    """Turn literal ``\\n`` sequences into real line breaks, for display only.

    Reasoning that arrives through a provider's structured ``reasoning_content``
    field is raw model text, not markdown the gateway produced — and the
    reasoning models behind the gateway's ``auto`` route (deepseek / qwen / kimi)
    emit escaped newlines as the two characters ``\\`` + ``n``. The card then
    renders a wall of literal ``\\n`` instead of line breaks: that is exactly
    what the 2026-08-20 Adobe-provisioning card showed for ~160s before the real
    answer replaced it.

    Display-only on purpose. The stored assistant message was already correct in
    that incident, so nothing here touches content that is persisted, replayed,
    or sent back to a provider — only what the reasoning panel shows. Panel
    length is already bounded by ``builder``'s ``_clip(reasoning, 1200)``.
    """
    raw = str(text or "")
    if "\\" not in raw:
        return raw
    unescaped = _ESCAPED_NL_RE.sub("\n", raw)
    # A stream escaped end-to-end turns into long blank runs once unescaped.
    return _BLANK_RUN_RE.sub("\n\n", unescaped).strip()


def _format(adapter: Any, content: str) -> str:
    formatter = getattr(adapter, "format_message", None)
    if callable(formatter):
        try:
            return str(formatter(content or ""))
        except Exception:
            pass
    return str(content or "")


def _strip_invalid_image_keys(text: str) -> str:
    if "![" not in text:
        return text

    def replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(2).startswith("img_") else ""

    return _IMAGE_RE.sub(replace, text)


def _plain_summary(text: str) -> str:
    summary = re.sub(r"[*_`#>\[\]()~]", "", str(text or "")).strip()
    return summary[:120] if summary else "Hermes"


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "\n...[truncated]"
