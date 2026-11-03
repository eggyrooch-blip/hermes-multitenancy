"""Lark CardKit / IM API error + response helpers.

Phase 4 — typed exception hierarchy mirroring openclaw-lark's
``CardKitApiError`` ancestry. ``_raise_on_lark_error`` now classifies the
response code and raises the most-specific subclass so callers can switch on
type instead of regex-grepping the error string. All subclasses remain
``RuntimeError`` so legacy ``except Exception`` / ``except RuntimeError``
blocks keep catching them — backward compatible.

Class hierarchy::

    RuntimeError
    └── CardKitApiError              (base — any non-zero Lark code)
        ├── RateLimitError           (code 230020 — CardKit rate limit)
        ├── StreamingClosedError     (codes 200850 / 300309)
        ├── TableLimitError          (code 230099 sub_code 11310 — table limit)
        └── UnavailableError         (code 99991663 recalled, 230006 deleted)
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, List, Optional

try:  # pragma: no cover - import shape depends on the installed Hermes agent.
    from gateway.platforms.base import SendResult  # type: ignore
except Exception:  # pragma: no cover
    SendResult = None  # type: ignore


_RATE_LIMIT_CODE = 230020
_STREAMING_CLOSED_CODES = frozenset({200850, 300309})
_TABLE_LIMIT_CODE = 230099
_TABLE_LIMIT_SUB_CODE = 11310
_RECALLED_CODE = 99991663
_DELETED_CODE = 230006

# Empirical Feishu CardKit limit: a single card renders at most this many
# markdown tables natively; the 4th+ trips code 230099 / sub_code 11310
# ("table number over limit"). openclaw-lark pins the same constant
# (card-error.ts ``FEISHU_CARD_TABLE_LIMIT = 3``, "2026-03 实测"). The table
# DEGRADATION that keeps a card under this limit lives in
# ``markdown_style._degrade_limited_markdown_tables``; this constant is the
# shared source of truth both that path and ``should_use_card`` reason about.
_FEISHU_CARD_TABLE_LIMIT = 3


class CardKitApiError(RuntimeError):
    """Base — any non-zero response code from the Lark CardKit / IM API.

    Carries the structured ``code`` / ``sub_code`` / ``msg`` / ``api`` fields
    callers may want to dispatch on, alongside the legacy formatted string
    in ``str(exc)`` for log compatibility.
    """

    def __init__(self, api: str, code: int, msg: str = "", sub_code: Optional[int] = None) -> None:
        self.api = api
        self.code = int(code)
        self.sub_code = int(sub_code) if sub_code is not None else None
        self.msg = str(msg or "")
        super().__init__(self._render())

    def _render(self) -> str:
        if self.sub_code is not None:
            return f"{self.api} failed: code={self.code}, sub_code={self.sub_code}, msg={self.msg}"
        return f"{self.api} failed: code={self.code}, msg={self.msg}"


class RateLimitError(CardKitApiError):
    """Lark CardKit rate limit (code 230020)."""


class StreamingClosedError(CardKitApiError):
    """Card streaming mode has closed and must be enabled again."""


class TableLimitError(CardKitApiError):
    """Card hit a table-element shape limit (code 230099, sub_code 11310)."""


class UnavailableError(CardKitApiError):
    """Source message no longer reachable — recalled (99991663) or deleted (230006).

    Phase 3's ``UnavailableGuard.mark_unavailable`` is the appropriate handler
    in the streaming-controller exception path: a single mark causes all
    subsequent ``should_proceed`` checks for that message_id to return False,
    silencing the retry storm Lark used to produce for recalled cards.
    """


def _classify_lark_error(api: str, code: int, msg: str, sub_code: Optional[int]) -> CardKitApiError:
    """Return the most-specific ``CardKitApiError`` subclass for the response.

    Routing order is deliberate — table-limit takes precedence over the
    generic 230099 base, and unavailable takes precedence over arbitrary
    non-zero codes.
    """
    if code in {_RECALLED_CODE, _DELETED_CODE}:
        return UnavailableError(api, code, msg, sub_code)
    if code == _RATE_LIMIT_CODE:
        return RateLimitError(api, code, msg, sub_code)
    if code in _STREAMING_CLOSED_CODES:
        return StreamingClosedError(api, code, msg, sub_code)
    if code == _TABLE_LIMIT_CODE and sub_code == _TABLE_LIMIT_SUB_CODE:
        return TableLimitError(api, code, msg, sub_code)
    return CardKitApiError(api, code, msg, sub_code)


def _raise_on_lark_error(response: Any, api: str) -> None:
    code = getattr(response, "code", None)
    if code is None or code == 0:
        return
    msg = getattr(response, "msg", "")
    sub_code = getattr(response, "sub_code", None)
    raise _classify_lark_error(api, int(code), str(msg or ""), sub_code)


def _response_succeeded(adapter: Any, response: Any, default_message: str) -> bool:
    finalizer = getattr(adapter, "_finalize_send_result", None)
    if callable(finalizer):
        try:
            return bool(getattr(finalizer(response, default_message), "success", False))
        except Exception:
            pass
    succeeded = getattr(adapter, "_response_succeeded", None)
    if callable(succeeded):
        return bool(succeeded(response))
    code = getattr(response, "code", None)
    return code is None or code == 0


def _finalize(adapter: Any, response: Any, default_message: str) -> Any:
    finalizer = getattr(adapter, "_finalize_send_result", None)
    if callable(finalizer):
        return finalizer(response, default_message)
    message_id = _extract_response_field(response, "message_id")
    if message_id:
        return _result(True, message_id=message_id, raw_response=response)
    return _result(False, error=default_message, raw_response=response)


def _extract_response_field(response: Any, name: str) -> Optional[str]:
    for source in (
        response,
        getattr(response, "data", None),
        getattr(response, "message", None),
    ):
        if isinstance(source, dict):
            value = source.get(name)
        else:
            value = getattr(source, name, None)
        if value:
            return str(value)
    return None


def _result(
    success: bool,
    *,
    message_id: Optional[str] = None,
    error: Optional[str] = None,
    raw_response: Any = None,
) -> Any:
    if SendResult is not None:
        return SendResult(success=success, message_id=message_id, error=error, raw_response=raw_response)
    return SimpleNamespace(success=success, message_id=message_id, error=error, raw_response=raw_response)


# ---------------------------------------------------------------------------
# Card-vs-text decision predicate
# ---------------------------------------------------------------------------
#
# Ports openclaw-lark's ``shouldUseCard`` (src/card/reply-mode.ts) +
# ``findMarkdownTablesOutsideCodeBlocks`` (src/card/card-error.ts),
# re-implemented Pythonically. The plugin's table-DEGRADATION path
# (markdown_style._degrade_limited_markdown_tables) already exists; this is the
# upstream GATE that decides whether a reply's text warrants an interactive card
# at all (fenced code or markdown tables) versus a plain static text message.
#
# Table detection mirrors markdown_style.py's line conditions EXACTLY so the
# gate and the degrader agree on what a "table" is: a header line matched by
# ``^\|.*\|`` immediately followed by a separator line matched by ``^\|[-|: ]+\|``
# (anchored, on the raw line). Tables that appear only inside ``` / ~~~ fences
# are documentation — Feishu never renders them as card table elements — so they
# are excluded from the count, matching findMarkdownTablesOutsideCodeBlocks.

# Both ``` and ~~~ fences are stripped before table scanning. Feishu CardKit
# treats either fence as a code block; a table drawn inside one is never a
# renderable card table element, so it must not be counted.
_CODE_FENCE_RE = re.compile(r"(?:```|~~~)[\s\S]*?(?:```|~~~)")

# Mirror markdown_style.py's _CORE_TABLE_LINE1 / _CORE_TABLE_LINE2 verbatim:
# core (gateway/platforms/feishu.py) fires its table→plain-text path on these
# two anchored line shapes, so the gate must recognise exactly the same set.
_TABLE_HEADER_LINE_RE = re.compile(r"^\|.*\|")
_TABLE_SEPARATOR_LINE_RE = re.compile(r"^\|[-|: ]+\|")


def find_markdown_tables_outside_code_blocks(text: Optional[str]) -> List[str]:
    """Return each markdown table NOT inside a ``` / ~~~ code fence.

    A table is a header line (``^\\|.*\\|``) immediately followed by a separator
    line (``^\\|[-|: ]+\\|``); the returned string for each match is the header +
    separator + any contiguous trailing pipe rows. Code-fenced pseudo-tables are
    excluded so a documentation table never inflates the count. Mirrors
    openclaw-lark ``findMarkdownTablesOutsideCodeBlocks``.
    """
    if not text:
        return []

    # Replace fenced regions with newline-preserving blanks so a fenced table
    # cannot be matched, while line numbering for any later use stays stable.
    def _blank_fence(match: "re.Match[str]") -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    scanned = _CODE_FENCE_RE.sub(_blank_fence, str(text))

    lines = scanned.splitlines()
    tables: List[str] = []
    index = 0
    while index < len(lines):
        header = lines[index]
        separator = lines[index + 1] if index + 1 < len(lines) else ""
        if _TABLE_HEADER_LINE_RE.match(header) and _TABLE_SEPARATOR_LINE_RE.match(separator):
            end = index + 2
            while end < len(lines) and _is_pipe_row(lines[end]):
                end += 1
            tables.append("\n".join(lines[index:end]))
            index = end
            continue
        index += 1
    return tables


def _is_pipe_row(line: str) -> bool:
    stripped = str(line or "").strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def should_use_card(text: Optional[str]) -> bool:
    """Decide whether a reply's text should be sent as an interactive card.

    Returns True when the text contains markdown elements that benefit from /
    require a card — fenced code blocks or markdown tables — and False for plain
    text that renders fine as a static message.

    Tables exceeding ``_FEISHU_CARD_TABLE_LIMIT`` STILL warrant a card: the
    markdown_style degrade path wraps the overflow so the card stays under
    230099/11310, and the first N tables still render natively. Falling back to
    plain text here would lose that native rendering. (This differs from
    openclaw's early ``shouldUseCard``, which once returned False on overflow
    before the degrade path was reliable; the multitenancy plugin's degrader
    makes the card the correct choice in every table case.) Mirrors the intent
    of openclaw-lark ``shouldUseCard``.
    """
    if not text:
        return False
    body = str(text)
    if _CODE_FENCE_RE.search(body):
        return True
    if find_markdown_tables_outside_code_blocks(body):
        return True
    return False
