"""Markdown -> Feishu card rendering for cron deliveries.

Split out of the historical ``cron_worker`` god-node. Pure move + re-export:
``cron_worker`` re-exports every symbol so import paths and ``monkeypatch.setattr(
cron_worker, ...)`` targets are unchanged. Cross-module and monkeypatched helpers
are reached through the ``cron_worker`` shim (``_cw``) so patches applied to
``cron_worker.<name>`` are honoured by callers that live in a sibling module.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
from concurrent.futures import TimeoutError as FuturesTimeout
import functools
import json
import math
import signal
import logging
import os
import re
import subprocess
import sqlite3
import sys
import threading
import time
import uuid
import copy
from urllib import error as urllib_error
from urllib import request as urllib_request
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None

from .. import cron_worker as _cw

# Keep the historical logger name so log records are attributed to
# ``hermes_multitenancy.cron_worker`` exactly as before the split.
logger = logging.getLogger("hermes_multitenancy.cron_worker")



def _cron_delivery_payload_for_adapter(job: dict, content: str) -> tuple[str, list]:
    wrap_response = True
    try:
        from cron.config import load_config

        user_cfg = load_config()
        wrap_response = user_cfg.get("cron", {}).get("wrap_response", True)
    except Exception:
        pass

    if wrap_response:
        task_name = job.get("name", job.get("id", "scheduled task"))
        job_id = job.get("id", "")
        delivery_content = (
            f"Cronjob Response: {task_name}\n"
            f"(job_id: {job_id})\n"
            f"-------------\n\n"
            f"{content}\n\n"
            f"To stop or manage this job, send me a new message (e.g. \"stop reminder {task_name}\")."
        )
    else:
        delivery_content = content

    try:
        from gateway.platforms.base import BasePlatformAdapter

        media_files, cleaned = BasePlatformAdapter.extract_media(delivery_content)
        media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
        return cleaned.strip(), media_files
    except Exception:
        return delivery_content.strip(), []


def _cron_card_response_enabled() -> bool:
    """Whether cron deliveries should render as interactive cards (default on)."""
    try:
        from cron.config import load_config

        return bool(load_config().get("cron", {}).get("card_response", True))
    except Exception:
        return True


def _build_cron_card(job: dict, content: str) -> tuple[Optional[dict[str, Any]], list]:
    """Build the Feishu interactive card for a cron delivery.

    Returns ``(card, media_files)``. ``card`` is ``None`` when the body is empty
    (caller then falls back to the plain-text path). Visuals are pinned to the
    historical streaming-card look sunke signed off on (cron-card-style-restore):
    ``_build_cron_card_body`` composes bold ``**⏰ task**`` title in the body,
    cardified markdown, and the Chinese stop-hint — no blue header bar, no
    English "To stop or manage" footer. ``wrap_response=False`` sends just the
    cardified body.
    """
    wrap_response = True
    try:
        from cron.config import load_config

        wrap_response = load_config().get("cron", {}).get("wrap_response", True)
    except Exception:
        pass

    try:
        from gateway.platforms.base import BasePlatformAdapter

        media_files, cleaned = BasePlatformAdapter.extract_media(content)
        media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
        body = cleaned.strip()
    except Exception:
        body = content.strip()
        media_files = []

    if not body:
        return None, media_files

    try:
        from ..card.sanitization import _plain_summary

        summary = _plain_summary(body)
    except Exception:
        summary = "Hermes"

    if wrap_response:
        body_md = _cw._build_cron_card_body(job, body)
    else:
        body_md = _cw._cardify_markdown_for_feishu(body)

    card: dict[str, Any] = {
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
            "locales": ["zh_cn", "en_us"],
            "summary": {"content": summary},
        },
        "elements": [{"tag": "markdown", "content": body_md}],
    }
    return card, media_files


# Matches a markdown link ``[label](url)`` but NOT an image ``![label](url)``
# (the ``(?<!!)`` lookbehind skips the image form so we don't emit ``!label``).
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")


def _linkify_markdown_links_in_text(text: str) -> str:
    """Rewrite ``[label](url)`` -> ``label (url)`` so bare URLs survive.

    Feishu auto-linkifies bare URLs in plain-text messages but does NOT render
    markdown link syntax. Upstream ``_build_outbound_payload`` force-sends any
    table-containing content as raw plain text, so ``[label](url)`` arrives
    literally and is not clickable. Converting to ``label (url)`` exposes the
    bare URL, which Feishu turns into a working link.

    Known edges (acceptable for the social-radar report, whose URLs are
    percent-encoded and contain no literal ``)`` or images):
    - This is a regex, not a full markdown parser; a URL containing a literal
      ``)`` would be truncated at the first paren.
    - A ``[label](url)`` split across upstream message-truncation chunks is not
      repaired (the split happens before this runs); report rows are short so
      this does not occur in practice.
    """
    return _cw._MD_LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2).strip()})", text)


_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$")
_TABLE_ROW_RE = re.compile(r"(?m)^\s*\|.*\|\s*$")
def _split_table_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _convert_md_tables_for_card(text: str) -> str:
    """Convert GFM pipe tables to bullet lines (Feishu cards don't render tables).

    2-column tables (the common key/value shape, e.g. weather) become
    ``- **key**：value``; wider tables become ``- **c0** · h1: c1 · h2: c2``.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if (
            _TABLE_ROW_RE.match(line)
            and i + 1 < n
            and _TABLE_SEP_RE.match(lines[i + 1])
        ):
            header = _cw._split_table_row(line)
            j = i + 2
            rows: list[list[str]] = []
            while j < n and _TABLE_ROW_RE.match(lines[j]):
                rows.append(_cw._split_table_row(lines[j]))
                j += 1
            for r in rows:
                if len(header) == 2 and len(r) >= 2:
                    out.append(f"- **{r[0]}**：{r[1]}")
                else:
                    parts: list[str] = []
                    for k, cell in enumerate(r):
                        if k == 0:
                            parts.append(f"**{cell}**")
                        else:
                            h = header[k] if k < len(header) else ""
                            parts.append(f"{h + ': ' if h else ''}{cell}")
                    out.append("- " + " · ".join(p for p in parts if p))
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _cardify_markdown_for_feishu(text: str) -> str:
    """Render markdown into the subset a Feishu card ``markdown`` element shows.

    Headings -> bold; pipe tables -> bullet lines. Bold / lists / links / emoji
    are already rendered by the card, so they pass through unchanged.
    """
    converted = _HEADING_RE.sub(lambda m: f"**{m.group(1).strip()}**", text)
    converted = _cw._convert_md_tables_for_card(converted)
    return converted


def _build_cron_card_body(job: dict, content: str) -> str:
    """Build the streaming-card body for a cron delivery.

    A clean title (the task name) + the agent content converted to the
    Feishu-card-renderable markdown subset (headings -> bold, tables -> bullet
    key/values) + a small stop-job hint. No "Cronjob Response:" plain wrapper —
    the card already frames it like a normal reply.
    """
    task_name = str(job.get("name") or job.get("id") or "").strip()
    body = _cw._cardify_markdown_for_feishu(content.strip())
    parts: list[str] = []
    if task_name:
        parts.append(f"**⏰ {task_name}**")
    parts.append(body)
    if task_name:
        parts.append(f'_停止该任务：回复 “stop reminder {task_name}”_')
    return "\n\n".join(p for p in parts if p)
