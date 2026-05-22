"""Text sanitization + summary helpers.

Pure text utilities — strip invalid image keys, clip, plain-summary, and
``_format`` which routes through the adapter's optional ``format_message`` hook.
"""
from __future__ import annotations

import re
from typing import Any

_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


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
