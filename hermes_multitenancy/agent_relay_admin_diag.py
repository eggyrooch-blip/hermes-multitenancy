"""Read-only admin resource: the kep-telemetry exporter's local diagnostic log.

Two endpoints on the relay admin plane, shaped by ``agent_relay_admin``:

* ``GET /v1/admin/telemetry-diagnostics`` — the NDJSON rows the exporter writes
  per settlement, paged by ``since`` + ``after_id``.
* ``GET /v1/admin/telemetry-stats`` — counts for the same window plus the
  window-independent state of the log on disk.

It sits on the relay because that is where this estate's admin plane already is:
one path grammar, one auth story, one error body, one ingress
(``relay.example.com/v1/*``, already routed — no Caddy change). The relay
process reaches the exporter's directory through a single read-only bind mount
its unit declares; it cannot write there, and nothing else under /home is
visible to it.

Who this is for: the kep-telemetry owner on the platform side, so "why is this
row not in Kibana / why is this field wrong" is a request he makes himself
rather than a file we export and hand over. The evidence itself is produced by
``analytics/kep_telemetry_export.DiagnosticsLog``; this module only reads it.

Three properties this module exists to hold
-------------------------------------------
1. **No conversation content leaves the host.** The response is *constructed*
   from an allowlist — and the allowlist reaches all the way down. A permitted
   key is not a permitted subtree: every value is rebuilt against a declared
   type, nested mappings are walked against their own allowlists, and anything
   else is dropped and counted (``dropped_keys``). The first draft copied
   permitted values wholesale and a review walked ``report.upload.content``
   straight through a counter block; that is the failure this shape prevents.
   ``source_offset``/``source_inode`` locate the originating audit line for
   whoever asks us; the line itself is never served.
2. **One attribution field, not two.** ``profile`` and ``operator`` are the same
   employee account name (``header_safe_operator`` decides which one is filled).
   Only ``operator`` is returned; ``profile``/``profile_withheld`` never are.
3. **Bounded work.** Every read runs in a thread, every byte read counts against
   one budget — including the probe read and the state file — no single line can
   be read without a ceiling, the scan never takes the exporter's lock, and it
   never writes.

The state directory is resolved from this module's own env, never from
``default_state_dir()``: that helper reads ``HERMES_HOME``, and the gateway unit
sets it to ``…/profiles/multitenancy_router`` while the exporter unit sets it to
``/home/hermes/.hermes`` — reusing it would point at an empty directory and read
as "no data" forever.
"""
from __future__ import annotations

import asyncio
import json
import math
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .agent_relay_admin import (
    ADMIN_PAGE_LIMIT,
    ADMIN_SCAN_BYTE_BUDGET,
    RateLimiter,
    admin_after_key,
    admin_error,
    admin_window,
    token_key,
)

logger = logging.getLogger(__name__)

#: Resource names on the admin plane; ``agent_relay_admin.ADMIN_TOKEN_SCOPES``
#: decides which bearer may read them.
RESOURCE_ROWS = "telemetry-diagnostics"
RESOURCE_STATS = "telemetry-stats"
#: Where the exporter's log is bind-mounted into the relay's namespace. The unit
#: declares the same path in ``BindReadOnlyPaths=``; the env is the override for
#: tests and for a host that lays it out differently.
DIR_ENV = "HERMES_TELEMETRY_DIAG_DIR"
DEFAULT_DIR = "/home/hermes/.hermes/state/kep-telemetry-export/diagnostics"

_SHANGHAI = timezone(timedelta(hours=8))
#: The same representable range the window helper enforces (1970 .. 2100). A row
#: outside it is corrupt, not early or late.
_MIN_TS_MS = 0
_MAX_TS_MS = 4_102_444_800_000
_EPOCH = date(1970, 1, 1)
_DAY_FILE = re.compile(r"^export-(\d{8})\.ndjson$")
#: The producer's own row id: ``<YYYYMMDD>-<seq:08d>``, stamped by ``DiagnosticsLog``
#: and declared by it as the paging key (``DIAGNOSE_SCHEMA["id"]``). It is stable,
#: unique and sorts lexicographically in append order, so this resource pages on it
#: rather than inventing a second numbering.
_ROW_ID = re.compile(r"^\d{8}-\d{8}$")
_ROW_ID_SHAPE = "<YYYYMMDD>-<8 digits>"

#: A diagnostic row is ~1.5 KiB. Anything past this is corrupt, and reading it to
#: find out costs exactly what the budget exists to prevent.
_MAX_LINE_BYTES = 1024 * 1024
#: Bytes one request may spend on lines it cannot use (no id, unparsable, out of
#: window). Separate from the page budget on purpose: a prefix of unusable rows
#: must not be able to consume the page's whole allowance and hand back a cursor
#: that has not moved. Past this, the day file is declared corrupt and skipped —
#: a file that is mostly unreadable *is* broken, and saying so beats stalling.
_SKIP_BYTE_BUDGET = 2 * 1024 * 1024
#: The locator's own ceiling. Its probes are O(log n) line reads, so they are
#: bounded by construction — but they must not eat the page budget, or a request
#: that only needed to *find* its place comes back empty with the same cursor.
_LOCATE_BYTE_CAP = 2 * 1024 * 1024
#: ``last-run.json`` is a few hundred bytes; the cap is what stops a replaced file
#: from turning a status probe into an unbounded read.
_STATE_FILE_CAP = 64 * 1024
#: The Hub's own response text, already capped at 2 KiB by the producer; capped
#: again here so the wire size does not depend on the writer's constant.
_BODY_CAP = 2048
#: Server-supplied strings that ride as values (a reason, a status label).
_LABEL_MAX = 200
#: A category label: what an aggregation key is allowed to look like.
_TOKEN_LABEL = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
#: What a *server-supplied* string may look like. Not "short", not "ASCII" —
#: both of those carried a private sentence through in a review. A machine reason
#: is a dotted/colonned identifier, optionally with one quoted identifier after a
#: colon: ``invalid_enum``, ``invalid_enum:client``, ``invalid_enum:client: "hermes"``.
#: An English sentence has spaces between words and does not match, which is the
#: whole point — free text is not a category, and only categories travel.
_SERVER_TEXT = re.compile(r'^[A-Za-z0-9_.:\-]+(?::\s*"?[A-Za-z0-9_.\-]{0,64}"?)?$')
#: Fields that are structurally not identifiers (timestamps, URLs) have their own
#: shapes; everything else on a row is an id, an enum or a reason.
_TIMESTAMP_TEXT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:.]{5,15}(?:Z|[+-][0-9:]{4,5})?$")
_URL_TEXT = re.compile(r"^https?://[A-Za-z0-9_.:/\-]{1,200}$")
_MAX_STAT_KEYS = 50
#: A counter label (``stopped``, a reject class, a kind) is a short token, never
#: prose. Longer than this and it is not a label, so it does not travel.
_COUNTER_LABEL_MAX = 64
#: Row kinds that are not records. ``diagnose_header`` is the self-describing
#: first line of a hand-off bundle: a schema dictionary, the host name, the state
#: directory and the day-file inventory. The producer's own cursor contract says
#: to skip it, and none of what it carries belongs on a wire.
SKIP_KINDS = frozenset({"diagnose_header"})
#: Only these kinds are records a caller may read.
RECORD_KINDS = frozenset({"upload_attempt", "local_reject", "pass_summary", "truncated"})

# ── the allowlist ─────────────────────────────────────────────────────────
# Everything below is what may leave the host, and each entry declares a type.
# Adding a name here is a privacy decision, so each group is pinned by a test.

#: Flat row fields → the type their value must have to be emitted.
#: ``profile``/``profile_withheld`` are deliberately absent.
ROW_FIELDS: dict[str, tuple] = {
    "id": (str,), "kind": (str,), "at": (str,), "at_epoch_ms": (int,),
    "run_id": (str,), "content_hash": (str,), "skill": (str,), "skill_withheld": (bool,),
    "batch_id": (str,), "url": (str,), "attempt": (int,), "reason": (str,),
    "class": (str,), "sent_at": (str,),
    # Added by the producer in its final round: when this record was *first*
    # rejected, as opposed to ``at`` which is this attempt. Served — it is an
    # RFC3339 timestamp with no identity in it, it is the field that answers "how
    # long has this been failing", and it carries the same name on the platform
    # side's own dead-letter rows, so the two join without translation.
    "first_at": (str,),
    "source_offset": (int,), "source_inode": (int,), "source_available": (bool,),
    "source_reason": (str,), "observed_at": (str,),
}
#: Withheld on purpose — dropped without counting as schema drift. ``seq`` is the
#: numeric tail of ``id``; one form of the same fact is enough on the wire.
WITHHELD_FIELDS = ("profile", "profile_withheld", "operator", "seq")
#: The lifecycle payload, narrowed to the keys ``build_record`` actually emits
#: (machine-checked: 16, none of them content-bearing). Deliberately *narrower*
#: than ``LIFECYCLE_V9_KEYS``: the upload contract may grow a key long before
#: anyone decides that key belongs on this wire, and a test pins the relationship
#: so growth shows up as a red test rather than as a wider API.
REQUEST_FIELDS = (
    "boundary_confidence", "boundary_source", "boundary_status", "client",
    "client_revision", "ended_at", "expert", "model_name", "opened_observed_at",
    "project_id", "record_id", "record_kind", "run_id", "skill", "status", "surface",
)
#: Payload keys whose value is a mapping. Numbers only — a token count is a
#: number, and "it is inside token_metrics" is not a reason to forward a string.
#: Empty today (``token_metrics`` is not among the emitted keys); kept as the
#: declared handling for the day it is.
REQUEST_NUMERIC_MAPS = ("token_metrics",)
RESPONSE_FIELDS = ("status", "body", "truncated", "redacted", "body_withheld")
RESPONSE_ERROR_FIELDS = ("index", "reason", "run_id", "code", "message", "field")
VERDICT_FIELDS = ("outcome", "reason")
#: ``pass_summary`` rows carry the exporter's whole round report. Only these keys,
#: and inside the blocks only counters — see ``_project_counters``.
REPORT_SCALARS = ("at", "at_epoch_ms", "env", "dry_run")
REPORT_BLOCKS = ("read", "built", "upload", "retry")
#: Inside a counter block a value is a number. These are the only keys whose value
#: may be a string, because the exporter genuinely stores a label there. Without
#: this list "a short string" was enough to ride through — which is how a review
#: got ``report.upload.content`` onto the wire.
REPORT_LABEL_KEYS = ("stopped", "backfill_cutoff", "high_water", "caught_up", "env", "at")
#: Counter-block keys, by block. A map key is upstream-controlled text that would
#: otherwise become a response key, so the *names* are an allowlist too — limiting
#: only their length let ``{"私密句子": 1}`` through a review's fixture.
REPORT_BLOCK_KEYS: dict[str, tuple[str, ...]] = {
    "read": ("lines", "skill_calls", "terminals", "restarted", "backfill_cutoff",
             "caught_up", "high_water"),
    "built": ("records", "with_terminal", "unresolved", "still_pending", "already_confirmed",
              "already_rejected", "skipped_invalid_skill", "skipped_error"),
    "upload": ("batches", "sent", "accepted", "rejected_by_reason", "rejected_by_class",
               "retry", "stopped", "queued", "operators", "operator_fallback"),
    "retry": ("requeued", "waiting", "exhausted", "permanent", "legacy", "by_class"),
}
#: ``token_metrics`` is the only mapping inside the payload; these are its keys.
TOKEN_METRIC_KEYS = ("input", "output", "total", "cache_read", "cache_write", "reasoning")
#: ``last-run.json``'s diagnostics block, projected for the stats payload.
LAST_PASS_KEYS = ("rows", "dropped", "errors", "stopped")
#: The Hub's JSON answer, rebuilt field by field. Anything else about its body is
#: reported by shape (``body_unparsed``) rather than echoed: it is an external
#: system's text, and "our payload has no conversation in it" is an argument about
#: our side of the exchange, not about what a gateway may put in an error body.
BODY_COUNT_FIELDS = ("accepted", "rejected")
#: ``message`` is deliberately absent: a gateway's human-readable message is the
#: one field in its答复 designed to hold prose, and a category is what a reader
#: needs. Its presence is reported by ``message_withheld``.
BODY_LABEL_FIELDS = ("code", "error", "status", "reason")
BODY_ERROR_FIELDS = ("index", "reason", "run_id", "code", "message", "field")
_BODY_MAX_ERRORS = 20
#: Aggregation labels that are not in a known set are folded here rather than
#: becoming response keys of their own.
_OTHER = "other"
#: A counter block may hold one level of ``label -> number`` maps (``by_class``,
#: ``rejected_by_reason``). Deeper than that is not a counter.
_REPORT_MAX_DEPTH = 1

_limiter = RateLimiter()

#: Where a given cursor landed in a given file, remembered between requests.
#:
#: This is a cache, not a contract. The public cursor is an id and only an id —
#: an earlier design put a byte offset in the caller's hands and three review
#: findings came out of it (a hint that skipped rows, one that landed mid-line,
#: one that walked the cursor backwards). A memo the server owns has none of
#: those: a miss costs a binary search, a stale entry is rejected by the same
#: line-boundary check, and nothing a caller sends can steer it.
#:
#: It exists for the one case a stateless reader cannot do well: rows so large
#: that even a binary search runs out of its budget, where re-reading the served
#: prefix would consume the page and hand back a cursor that had not moved.
_MEMO_CAP = 64
_position_memo: "dict[tuple[str, int, str], int]" = {}
_position_lock = threading.Lock()


def _memo_get(key: tuple[str, int, str]) -> Optional[int]:
    with _position_lock:
        return _position_memo.get(key)


def _memo_put(key: tuple[str, int, str], offset: int) -> None:
    with _position_lock:
        if key in _position_memo:
            _position_memo.pop(key)
        elif len(_position_memo) >= _MEMO_CAP:
            _position_memo.pop(next(iter(_position_memo)))
        _position_memo[key] = offset


# ── paths ─────────────────────────────────────────────────────────────────


def diagnostics_dir() -> Path:
    raw = os.environ.get(DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path(DEFAULT_DIR)


def _day_ordinal(name: str) -> Optional[int]:
    match = _DAY_FILE.match(name)
    if match is None:
        return None
    try:
        day = datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None
    return (day - _EPOCH).days


def _ms_to_ordinal(ms: int) -> int:
    return (datetime.fromtimestamp(ms / 1000.0, tz=_SHANGHAI).date() - _EPOCH).days


def _row_ts(row: dict[str, Any]) -> Optional[int]:
    """Epoch milliseconds for a row: the stamped value, else the RFC3339 ``at``."""
    stamped = row.get("at_epoch_ms")
    if isinstance(stamped, int) and not isinstance(stamped, bool):
        # A row's own timestamp is not a reason to trust it: a 400-digit int here
        # once made the reader treat the rest of the window as "past until" and
        # hide every later row.
        return stamped if _MIN_TS_MS <= stamped <= _MAX_TS_MS else None
    raw = row.get("at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    try:
        value = int(parsed.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return None
    return value if _MIN_TS_MS <= value <= _MAX_TS_MS else None


# ── projection ────────────────────────────────────────────────────────────


#: int has no ceiling in Python and ``json.loads`` will happily build a 400-digit
#: one; ``math.isfinite`` on it raises OverflowError, and no JSON consumer wants
#: it either. A counter that does not fit in float64 is not a counter.
_MAX_ABS_NUMBER = 2 ** 53


def _number(value: Any) -> bool:
    """A JSON-safe, representable number.

    Three ways this went wrong before: ``Infinity``/``NaN`` are valid Python and
    valid ``json.dumps`` output but invalid JSON; a huge ``int`` makes
    ``math.isfinite`` itself raise; and two finite floats can still sum to
    ``inf``. Everything numeric on the wire goes through here, sums included.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return -_MAX_ABS_NUMBER <= value <= _MAX_ABS_NUMBER
    if isinstance(value, float):
        return math.isfinite(value) and abs(value) <= float(_MAX_ABS_NUMBER)
    return False


def _typed(value: Any, types: tuple) -> bool:
    """``isinstance`` with bool never passing as int, and int never as bool."""
    if bool in types:
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    return isinstance(value, types)


def _label(value: Any, dropped: list[int], *, limit: int = _COUNTER_LABEL_MAX) -> Optional[str]:
    """A short **machine** label, or nothing.

    Short is not the same as safe: the constraint is printable ASCII within a
    length bound, which is what an enum name, a status or a reject reason looks
    like and what free text does not.
    """
    if not isinstance(value, str) or len(value) > limit:
        if value is not None:
            dropped[0] += 1
        return None
    if not (_SERVER_TEXT.match(value) or _TIMESTAMP_TEXT.match(value) or _URL_TEXT.match(value)):
        dropped[0] += 1
        return None
    return value


def _project_counters(block: Any, dropped: list[int], *, allowed: tuple[str, ...]) -> dict[str, Any]:
    """A report block: counts under *named* keys, and one level of ``label -> count``.

    Both the key and the value are checked. A key that is not in ``allowed`` is
    dropped: map keys come from upstream, and a projection that keeps "any short
    key" turns the response schema into whatever the writer felt like — a review
    put a whole sentence on the wire as a key that way.
    """
    out: dict[str, Any] = {}
    if not isinstance(block, dict):
        return out
    for key, value in block.items():
        if not isinstance(key, str) or key not in allowed:
            dropped[0] += 1
            continue
        if value is None or isinstance(value, bool) or _number(value):
            out[key] = value
        elif isinstance(value, (int, float)):
            dropped[0] += 1
        elif isinstance(value, str):
            kept = _label(value, dropped) if key in REPORT_LABEL_KEYS else None
            if kept is None:
                dropped[0] += 1
            else:
                out[key] = kept
        elif isinstance(value, dict):
            out[key] = _project_counts(value, dropped)
        else:
            dropped[0] += 1
    return out


def _project_counts(mapping: Any, dropped: list[int]) -> dict[str, Any]:
    """``label -> number`` and nothing else: the shape of ``rejected_by_reason``.

    Labels here are server-supplied strings, so they are constrained to a token
    charset and folded into ``other`` when they are anything else — a rejection
    reason is a category, and a category is not free-form text.
    """
    out: dict[str, Any] = {}
    if not isinstance(mapping, dict):
        return out
    for key, value in mapping.items():
        if not _number(value):
            dropped[0] += 1
            continue
        label = key if (isinstance(key, str) and _TOKEN_LABEL.match(key)) else None
        if label is None:
            dropped[0] += 1
            label = _OTHER
        if len(out) >= _MAX_STAT_KEYS and label not in out:
            label = _OTHER
        out[label] = out.get(label, 0) + value
    return out


def _project_report(report: Any, dropped: list[int]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(report, dict):
        return out
    for key in REPORT_SCALARS:
        if key not in report:
            continue
        value = report[key]
        if value is None or isinstance(value, bool) or _number(value):
            out[key] = value
        elif isinstance(value, (int, float)):
            dropped[0] += 1  # NaN / out of range: a report field, not a free pass
        else:
            kept = _label(value, dropped, limit=_LABEL_MAX)
            if kept is not None:
                out[key] = kept
    for key in REPORT_BLOCKS:
        if key in report:
            out[key] = _project_counters(report[key], dropped, allowed=REPORT_BLOCK_KEYS[key])
    if isinstance(report.get("audit"), str):
        # The audit path is the host's own filesystem layout; the basename is the
        # only part that identifies *which* stream the round read.
        out["audit"] = Path(report["audit"]).name
    dropped[0] += sum(
        1 for key in report
        if key not in REPORT_SCALARS and key not in REPORT_BLOCKS
        and key not in ("audit", "diagnostics")
    )
    return out


def _project_request(payload: Any, dropped: list[int]) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    out: dict[str, Any] = {}
    for key in REQUEST_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if key in REQUEST_NUMERIC_MAPS:
            if isinstance(value, dict):
                numbers: dict[str, Any] = {}
                for metric, count in value.items():
                    if isinstance(metric, str) and metric in TOKEN_METRIC_KEYS and _number(count):
                        numbers[metric] = count
                    else:
                        dropped[0] += 1
                out[key] = numbers
            elif value is not None:
                dropped[0] += 1
            continue
        if value is None or isinstance(value, bool) or _number(value):
            out[key] = value
        else:
            kept = _label(value, dropped, limit=_LABEL_MAX)
            if kept is not None:
                out[key] = kept
    dropped[0] += sum(1 for key in payload if key not in REQUEST_FIELDS)
    return out


def _project_body(raw: Any, dropped: list[int]) -> tuple[Any, Optional[dict[str, Any]]]:
    """The Hub's body, rebuilt — never echoed.

    The producer stores whatever the gateway sent back. Forwarding that string
    verbatim was this module's last unallowlisted path: a review fed it a gateway
    answer of ``{"accepted": {"content": "PRIVATE_CONVERSATION"}}`` and watched it
    come out of the API. So the body is parsed and rebuilt against the skill-runs
    response contract; anything that does not fit is reported by shape instead of
    by content, and the raw text stays on the host where it is still readable.
    """
    if not isinstance(raw, str) or not raw:
        return None, None
    size = len(raw.encode("utf-8", "replace"))
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return None, {"reason": "not_json", "bytes": size}
    if not isinstance(parsed, dict):
        return None, {"reason": "not_an_object", "bytes": size}
    out: dict[str, Any] = {}
    for key in BODY_COUNT_FIELDS:
        if key in parsed:
            if _number(parsed[key]):
                out[key] = parsed[key]
            else:
                dropped[0] += 1
    for key in BODY_LABEL_FIELDS:
        if key in parsed:
            value = parsed[key]
            if _number(value) or isinstance(value, bool):
                out[key] = value
            else:
                kept = _label(value, dropped, limit=_LABEL_MAX)
                if kept is not None:
                    out[key] = kept
    if isinstance(parsed.get("message"), str) and parsed["message"]:
        out["message_withheld"] = True
    errors = parsed.get("errors")
    if isinstance(errors, list):
        entries = []
        for item in errors[:_BODY_MAX_ERRORS]:
            if not isinstance(item, dict):
                dropped[0] += 1
                continue
            entry: dict[str, Any] = {}
            for key in BODY_ERROR_FIELDS:
                if key not in item:
                    continue
                value = item[key]
                if _number(value) or isinstance(value, bool) or value is None:
                    entry[key] = value
                else:
                    kept = _label(value, dropped, limit=_LABEL_MAX)
                    if kept is not None:
                        entry[key] = kept
            dropped[0] += sum(1 for key in item if key not in BODY_ERROR_FIELDS)
            entries.append(entry)
        out["errors"] = entries
        if len(errors) > _BODY_MAX_ERRORS:
            out["errors_truncated"] = True
    elif errors is not None:
        dropped[0] += 1
    dropped[0] += sum(
        1 for key in parsed
        if key not in BODY_COUNT_FIELDS and key not in BODY_LABEL_FIELDS and key != "errors"
    )
    return out, None


def _project_response(value: Any, dropped: list[int]) -> Optional[dict[str, Any]]:
    """The Hub's own answer: a status, its rebuilt body, and this record's entry
    from its ``errors[]``."""
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    status = value.get("status")
    out["status"] = status if (status is None or _typed(status, (int,))) else None
    body, unparsed = _project_body(value.get("body"), dropped)
    out["body"] = body
    if unparsed is not None:
        # Shape, not content: how big it was and why it could not be rebuilt. The
        # bytes themselves remain in the host's 0600 log for whoever asks us.
        out["body_unparsed"] = unparsed
    out["truncated"] = bool(value.get("truncated"))
    if value.get("redacted") is True:
        out["redacted"] = True
    if value.get("body_withheld"):
        # The producer already refused this body (a content-shaped key came back
        # from the Hub). Carry the flag so the reader knows why it is thin, and
        # never try to second-guess it.
        out["body_withheld"] = True
        out["body"] = None
    error = value.get("error")
    if isinstance(error, dict):
        entry: dict[str, Any] = {}
        for key in RESPONSE_ERROR_FIELDS:
            if key not in error:
                continue
            item = error[key]
            if item is None or isinstance(item, bool) or _number(item):
                entry[key] = item
            else:
                kept = _label(item, dropped, limit=_LABEL_MAX)
                if kept is not None:
                    entry[key] = kept
        dropped[0] += sum(1 for key in error if key not in RESPONSE_ERROR_FIELDS)
        out["error"] = entry
    elif error is not None:
        dropped[0] += 1
    dropped[0] += sum(1 for key in value if key not in RESPONSE_FIELDS and key != "error")
    return out


def project_row(row: dict[str, Any], dropped: list[int]) -> dict[str, Any]:
    """Build the public row from an allowlist. Never filter, always construct."""
    out: dict[str, Any] = {}
    for key, types in ROW_FIELDS.items():
        if key not in row:
            continue
        value = row[key]
        if value is None:
            out[key] = None
        elif isinstance(value, str) and str in types:
            # Every string on a row is machine-generated: an id, an enum, a
            # timestamp, a URL, a reject reason. Truncating to a length was not a
            # boundary — a review put a whole private sentence through ``reason``.
            kept = _label(value, dropped, limit=_LABEL_MAX)
            if kept is not None:
                out[key] = kept
        elif _typed(value, types):
            out[key] = value
        else:
            dropped[0] += 1
    # One attribution field. ``operator`` is the X-Operator value the Hub already
    # received; ``profile`` is the same account name and is never sent twice.
    owner = row.get("operator") or row.get("profile")
    out["operator"] = _label(owner, [0], limit=128) if isinstance(owner, str) else None
    if "request" in row:
        out["request"] = _project_request(row["request"], dropped)
    if "response" in row:
        out["response"] = _project_response(row["response"], dropped)
    if isinstance(row.get("verdict"), dict):
        entry: dict[str, Any] = {}
        for key in VERDICT_FIELDS:
            if key in row["verdict"]:
                kept = _label(row["verdict"][key], dropped, limit=_LABEL_MAX)
                if kept is not None:
                    entry[key] = kept
        dropped[0] += sum(1 for key in row["verdict"] if key not in VERDICT_FIELDS)
        out["verdict"] = entry
    if row.get("kind") == "pass_summary" and "report" in row:
        out["report"] = _project_report(row["report"], dropped)
    known = set(ROW_FIELDS) | set(WITHHELD_FIELDS) | {"request", "response", "verdict", "report"}
    dropped[0] += sum(1 for key in row if key not in known)
    return out


# ── reading ───────────────────────────────────────────────────────────────


class _Scan:
    """One bounded pass over the day files, oldest row first."""

    def __init__(self, directory: Path, since: int, until: int, after_id: str = "", *,
                 budget: int = ADMIN_SCAN_BYTE_BUDGET):
        self.directory = Path(directory)
        self.since = since
        self.until = until
        self.after_id = str(after_id or "")
        self.budget = max(0, int(budget))
        self.scanned = 0
        self.skipped_bytes = 0
        self.unreadable = 0
        self.unpageable = 0
        self.skipped = 0
        self.truncated = False
        self.gap = False
        self.available = True
        #: The furthest point the scan can safely resume from, whether or not the
        #: row there was returned. Without it, a window whose budget is eaten by
        #: out-of-window rows answers ``items: [] / truncated: true / next: null``
        #: and the caller retries the same prefix forever.
        #: The continuation. It is *only* an id — no byte offset, no moving
        #: window. Four separate review findings came from a cursor that also
        #: carried a physical position: a hint that skipped rows, a locator that
        #: regressed it, a prefix that stalled it, and a row whose clock ran
        #: backwards shrinking the window. An id that only ever moves forward,
        #: with the caller's original ``since`` kept as the floor, has none of
        #: those failure modes.
        self.last_id: str = self.after_id
        #: Day files abandoned because they hold a line past the cap.
        self.corrupt_days: list[str] = []
        #: Bytes the locator spent finding its place, reported but not charged to
        #: the page budget (see ``_LOCATE_BYTE_CAP``).
        self.located = 0

    def _unusable(self, consumed: int, path: Path) -> bool:
        """Count an unreadable line and charge it to the skip budget."""
        self.unreadable += 1
        self.scanned -= consumed
        return self._charge_skip(consumed, path)

    def _charge_skip(self, consumed: int, path: Path) -> bool:
        """``True`` when this file has spent its skip allowance and must be left."""
        self.skipped_bytes += consumed
        if self.skipped_bytes <= _SKIP_BYTE_BUDGET:
            return False
        if path.name not in self.corrupt_days:
            self.corrupt_days.append(path.name)
        return True

    def _advance(self, row_id: str) -> None:
        """Move the cursor, never backwards.

        Any row with a usable id advances it — in-window or not. A window's worth
        of budget spent on rows that are merely *outside* the window must still
        leave the caller further along than it started, or the next request reads
        the same prefix again.
        """
        if row_id > self.last_id:
            self.last_id = row_id

    @property
    def progress(self) -> Optional[tuple[str, int]]:
        """``(after_id, since)`` for the next request, or ``None`` if nothing moved.

        ``since`` is the caller's own floor, returned unchanged. Deriving it from
        the last row's timestamp looked tidier and quietly lost every row behind a
        clock that stepped backwards.
        """
        if self.last_id == self.after_id:
            return None
        return (self.last_id, self.since)

    @property
    def after_day(self) -> Optional[int]:
        if not self.after_id:
            return None
        return _day_ordinal(f"export-{self.after_id[:8]}.ndjson")

    def _files(self) -> list[tuple[int, Path]]:
        try:
            entries = sorted(self.directory.iterdir())
        except OSError:
            self.available = False
            return []
        # A row's ``at`` always falls inside the Shanghai day its file is named
        # for, so the window bounds the file list. ±1 day is slack for a round
        # that straddles midnight.
        low = _ms_to_ordinal(self.since) - 1
        high = _ms_to_ordinal(self.until) + 1
        after_day = self.after_day
        out = []
        for entry in entries:
            ordinal = _day_ordinal(entry.name)
            if ordinal is None or not (low <= ordinal <= high):
                continue
            if after_day is not None and ordinal < after_day:
                continue
            out.append((ordinal, entry))
        if (after_day is not None and low <= after_day <= high
                and not any(ordinal == after_day for ordinal, _ in out)):
            # The day the cursor points into is inside the window but no longer on
            # disk (retention) — say so rather than silently resuming elsewhere. A
            # cursor whose day simply falls outside the requested window is not a
            # gap: the caller moved the window on purpose.
            self.gap = True
        return out

    def _read_line(self, fh: Any, cap: int = _MAX_LINE_BYTES) -> tuple[str, bytes, int]:
        """``(kind, line, bytes_consumed)`` with ``kind`` one of
        ``line`` / ``eof`` / ``partial`` / ``oversize``.

        Every branch is bounded. A file with no newline in it must not be read
        into one string — that is how a byte budget gets bypassed — so a line
        longer than the cap is reported as ``oversize`` after at most the cap,
        and the caller abandons that file rather than hunting for the next
        newline through megabytes of garbage.
        """
        limit = max(1, min(_MAX_LINE_BYTES, cap))
        chunk = fh.readline(limit)
        if not chunk:
            return "eof", b"", 0
        if chunk.endswith(b"\n"):
            return "line", chunk, len(chunk)
        if len(chunk) >= limit:
            return "oversize", b"", len(chunk)
        return "partial", b"", len(chunk)

    def _line_at(self, fh: Any, offset: int, budget_left: int) -> tuple[int, Optional[dict[str, Any]], int]:
        """The first whole line at or after ``offset``: ``(line_start, row, consumed)``.

        ``line_start`` is ``-1`` when there is no whole line there (a truncated
        tail, or a read that would cost more than the caller is willing to spend).
        Every read is capped by ``budget_left``, because the locator's ceiling is
        only a ceiling if each of its reads respects it — a file of 900 KB lines
        otherwise walked straight past it.
        """
        consumed = 0
        if budget_left <= 0:
            return -1, None, 0
        if offset:
            fh.seek(offset - 1)
            skip = fh.readline(min(_MAX_LINE_BYTES, budget_left))
            consumed += len(skip)
            if not skip.endswith(b"\n"):
                return -1, None, consumed
            offset = offset - 1 + len(skip)
        else:
            fh.seek(0)
        if consumed >= budget_left:
            return -1, None, consumed
        kind, raw, used = self._read_line(fh, budget_left - consumed)
        consumed += used
        if kind != "line":
            return -1, None, consumed
        try:
            row = json.loads(raw)
        except (ValueError, RecursionError):
            return offset, None, consumed
        return offset, (row if isinstance(row, dict) else None), consumed

    def _locate(self, fh: Any, size: int) -> int:
        """Binary-search the first line whose id is past the cursor.

        Resuming must not depend on the caller echoing an offset back: a client
        that pages on ``since`` + ``after_id`` alone would otherwise re-read the
        day's prefix every time, and on a big day that prefix is the whole budget.

        Two failure modes this has to keep apart. Running off the end means the
        cursor is already at the file's tail, and the answer is ``size`` — not
        ``0``, which would walk the caller backwards through rows it has read.
        Not being able to read means give up on the shortcut and start at the top.
        """
        lo, hi, guard, spent = 0, size, 0, 0
        found_beyond = False
        while lo < hi and guard < 64 and spent < _LOCATE_BYTE_CAP:
            guard += 1
            mid = (lo + hi) // 2
            line_start, row, consumed = self._line_at(fh, mid, _LOCATE_BYTE_CAP - spent)
            spent += consumed
            if line_start < 0:
                hi = mid          # no whole line at/after mid: the answer is earlier
                continue
            row_id = row.get("id") if isinstance(row, dict) else None
            if isinstance(row_id, str) and row_id <= self.after_id:
                lo = max(line_start + 1, mid + 1)
            else:
                found_beyond = True
                hi = min(mid, line_start)
        self.located += spent
        if lo >= size and not found_beyond:
            # The cursor is at or past the last row: resume at EOF, never at 0.
            return size
        line_start, _, consumed = self._line_at(fh, lo, max(0, _LOCATE_BYTE_CAP - spent))
        self.located += consumed
        return line_start if line_start >= 0 else min(lo, size)

    def rows(self) -> Iterator[dict[str, Any]]:
        files = self._files()
        for index, (ordinal, path) in enumerate(files):
            newest = index == len(files) - 1
            stamp = path.name[len("export-"):-len(".ndjson")]
            try:
                handle = path.open("rb")
            except OSError:
                self.unreadable += 1
                continue
            with handle as fh:
                try:
                    size = os.fstat(fh.fileno()).st_size
                except OSError:
                    size = 0
                try:
                    inode = os.fstat(fh.fileno()).st_ino
                except OSError:
                    inode = 0
                offset = self._start_of(fh, ordinal, size, path.name, inode)
                fh.seek(offset)
                while True:
                    if self.scanned > self.budget:
                        # Checked before the *next* read, never after the current
                        # one: stopping after a read leaves the resume point a row
                        # behind, and a budget smaller than two rows then hands out
                        # the same cursor forever. One row of overshoot is the
                        # price of a cursor that always advances.
                        self.truncated = True
                        return
                    row_start = offset
                    kind, raw, consumed = self._read_line(fh)
                    self.scanned += consumed
                    offset += consumed
                    if kind == "eof":
                        break
                    if kind == "partial":
                        # A row still being written. In the newest file that is the
                        # exporter mid-append: stop everything and leave the cursor
                        # before it. In an older file nothing will ever finish it,
                        # so count it and move on — otherwise one crashed write
                        # hides every later day forever.
                        if newest:
                            return
                        self.unreadable += 1
                        break
                    if kind == "oversize":
                        # A line past the cap is not a diagnostic row. Abandoning
                        # the file costs nothing further and keeps both the budget
                        # and the cursor intact; hunting for its end is what turns
                        # one corrupt byte range into a permanent stall.
                        self.unreadable += 1
                        self.corrupt_days.append(path.name)
                        break
                    try:
                        row = json.loads(raw)
                    except (ValueError, RecursionError):
                        # RecursionError is not a ValueError: a deeply nested blob
                        # would otherwise be a 500 for the whole page.
                        if self._unusable(consumed, path):
                            break
                        continue
                    if not isinstance(row, dict):
                        if self._unusable(consumed, path):
                            break
                        continue
                    ts = _row_ts(row)
                    row_id = row.get("id")
                    if ts is None:
                        if self._unusable(consumed, path):
                            break
                        continue
                    kind = row.get("kind")
                    if not isinstance(kind, str):
                        # ``[] in SKIP_KINDS`` raises TypeError: unhashable. One
                        # malformed line must not take the endpoint down with it.
                        if self._unusable(consumed, path):
                            break
                        continue
                    if kind in SKIP_KINDS or kind not in RECORD_KINDS:
                        # Not a record: the bundle header (schema, host, state dir,
                        # file inventory) and anything else the producer grows that
                        # this resource has not been taught to read.
                        self.skipped += 1
                        continue
                    if not isinstance(row_id, str) or not _ROW_ID.match(row_id):
                        # Without the producer's id a row cannot take part in the
                        # cursor: serving it would either repeat it forever or
                        # stall the page. Counted, never silently dropped, and
                        # charged to the skip budget so a long prefix of them ends
                        # as a declared-corrupt file instead of a stalled cursor.
                        self.unpageable += 1
                        self.scanned -= consumed
                        if self._charge_skip(consumed, path):
                            break
                        continue
                    # ``_advance`` first: an out-of-window row with a good id is
                    # still progress, and skipping it without recording that is
                    # how a page of them stalls the cursor.
                    self._advance(row_id)
                    _memo_put((path.name, inode, row_id), offset)
                    if ts > self.until or ts < self.since:
                        continue
                    # Resume point: the newest row safely consumed. ``since`` only
                    # ever moves forward, so an older row advances the id alone.
                    self._advance(row_id)
                    # The cursor predicate. ``since`` is inclusive and the id is
                    # authoritative:  since <= ts <= until AND id > after_id.
                    # The relay's disjunction (exclusive since, tie-break on id)
                    # cannot be used here: it makes the id non-authoritative, so a
                    # reader may not skip a prefix or a day file — and without
                    # those skips a file-backed resource re-reads its way through
                    # the whole window on every page.
                    if row_id <= self.after_id:
                        continue
                    yield row

    def _start_of(self, fh: Any, ordinal: int, size: int, path_name: str, inode: int) -> int:
        """Where this day file's scan begins: the caller's hint if it holds up,
        otherwise a binary search, otherwise the top of the file."""
        if not self.after_id or ordinal != self.after_day:
            return 0
        remembered = _memo_get((path_name, inode, self.after_id))
        if remembered is not None and 0 < remembered <= size:
            fh.seek(remembered - 1)
            self.scanned += 1
            if fh.read(1) == b"\n":
                return remembered
        return self._locate(fh, size)

def read_page(directory: Path, since: int, until: int, after_id: str = "", *,
              limit: int = ADMIN_PAGE_LIMIT,
              budget: int = ADMIN_SCAN_BYTE_BUDGET) -> dict[str, Any]:
    scan = _Scan(directory, since, until, after_id, budget=budget)
    dropped = [0]
    items: list[dict[str, Any]] = []
    for row in scan.rows():
        # Append first, then stop. Breaking *before* appending would leave the scan's
        # resume point on a row the caller never received — the cursor would step
        # over it and it would be lost for good.
        items.append(project_row(row, dropped))
        if len(items) >= limit:
            scan.truncated = True
            break
    page: dict[str, Any] = {
        "items": items,
        "truncated": scan.truncated,
        "unreadable_lines": scan.unreadable,
        "unpageable_lines": scan.unpageable,
        "skipped_lines": scan.skipped,
        "dropped_keys": dropped[0],
        "gap": scan.gap,
        "corrupt_files": scan.corrupt_days,
    }
    # Additive convenience: the exact continuation, so a caller cannot build the
    # timestamp-only cursor that never advances. Derived from the last row
    # *scanned*, not the last row returned — a page that returned nothing because
    # the budget went on out-of-window rows still has to move.
    page["next"] = (
        {"since": scan.progress[1], "after_id": scan.progress[0]}
        if (scan.truncated and scan.progress) else None
    )
    return page


def read_stats(directory: Path, since: int, until: int, *,
               budget: int = ADMIN_SCAN_BYTE_BUDGET) -> dict[str, Any]:
    scan = _Scan(directory, since, until, budget=budget)
    dropped = [0]
    by_kind: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    by_class: dict[str, int] = {}
    total = 0

    def bump(bucket: dict[str, int], key: Any) -> None:
        """Aggregation keys come from the server's own strings — bounded in length
        and in cardinality, so a hostile reason cannot become the response."""
        label = _label(key, dropped)
        if label is None or (len(bucket) >= _MAX_STAT_KEYS and label not in bucket):
            return
        bucket[label] = bucket.get(label, 0) + 1

    for row in scan.rows():
        total += 1
        bump(by_kind, row.get("kind", "?"))
        verdict = row.get("verdict")
        if isinstance(verdict, dict) and isinstance(verdict.get("outcome"), str):
            bump(verdicts, verdict["outcome"])
            if verdict["outcome"] == "dead_letter":
                reason = row.get("reason") or verdict.get("reason") or "?"
                bump(by_reason, str(reason).split(":", 1)[0])
        if isinstance(row.get("class"), str):
            bump(by_class, row["class"])
    return {
        "rows": {"total": total, "by_kind": by_kind},
        "verdicts": verdicts,
        "rejected_by_reason": by_reason,
        "rejected_by_class": by_class,
        "truncated": scan.truncated,
        "unreadable_lines": scan.unreadable,
        "unpageable_lines": scan.unpageable,
        "skipped_lines": scan.skipped,
        "corrupt_files": scan.corrupt_days,
        "state": _state(Path(directory), available=scan.available),
    }


def _state(directory: Path, *, available: bool) -> dict[str, Any]:
    """Window-independent facts: what is on disk, and how the last round went.

    Mirrors the relay's ``enrolled_users`` habit — a stats payload may carry
    state that does not belong to the window, as long as it says so by name.
    """
    files: list[str] = []
    total = 0
    try:
        for entry in sorted(directory.iterdir()):
            if _day_ordinal(entry.name) is None:
                continue
            try:
                total += entry.stat().st_size
            except OSError:
                continue
            files.append(entry.name)
    except OSError:
        available = False
    state: dict[str, Any] = {
        "available": available and bool(files),
        "days": len(files),
        "bytes": total,
        "files": files[-3:],
        "retention_days": 14,  # enforced by the producer; this resource never prunes
    }
    state["last_pass"] = _last_pass(directory, files)
    return state


def _last_pass(directory: Path, files: list[str]) -> Optional[dict[str, Any]]:
    """The newest ``pass_summary`` row, read from **inside** the mounted directory.

    The obvious source is the exporter's ``last-run.json``, one level up — and the
    unit mounts only ``diagnostics/``, so on the approved deployment that file is
    simply not there. A status field that is always absent in production is worse
    than no field, so this reads the tail of the newest day file instead, which is
    the same information the exporter writes there every round.
    """
    if not files:
        return None
    path = directory / files[-1]
    try:
        with path.open("rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            fh.seek(max(0, size - _STATE_FILE_CAP))
            tail = fh.read(_STATE_FILE_CAP)
    except OSError:
        return None
    dropped = [0]
    for line in reversed(tail.split(b"\n")):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(row, dict) or row.get("kind") != "pass_summary":
            continue
        report = row.get("report") if isinstance(row.get("report"), dict) else {}
        diag = report.get("diagnostics") if isinstance(report.get("diagnostics"), dict) else {}
        out = _project_counters(
            {"rows": diag.get("rows"), "dropped": diag.get("dropped"),
             "errors": diag.get("errors"), "stopped": diag.get("stopped")},
            dropped, allowed=LAST_PASS_KEYS)
        at = row.get("at")
        out["at"] = _label(at, dropped, limit=_LABEL_MAX) if isinstance(at, str) else None
        out["upload"] = _project_counters(report.get("upload"), dropped,
                                          allowed=REPORT_BLOCK_KEYS["upload"])
        return out
    return None


# ── handlers ──────────────────────────────────────────────────────────────


def _strict_dumps(payload: Any) -> str:
    """``allow_nan=False`` as the last line of defence: ``Infinity``/``NaN`` are
    what ``json.dumps`` emits by default and what no strict JSON parser accepts.
    The projections already refuse non-finite numbers; this catches the path that
    forgets to."""
    try:
        return json.dumps(payload, allow_nan=False)
    except ValueError:
        logger.error("[telemetry-diagnostics] non-finite number reached serialisation")
        raise


def _rate_limited(request):
    retry = _limiter.check(token_key(request))
    if retry is None:
        return None
    return admin_error("rate_limited", "too many admin reads; retry later", 429, retry_after=retry)


async def handle_telemetry_diagnostics(request):
    from aiohttp import web

    window, denied = admin_window(request, RESOURCE_ROWS, allow_empty_window=True)
    if denied is not None:
        return denied
    after_id, invalid = admin_after_key(request, _ROW_ID, expected=_ROW_ID_SHAPE)
    if invalid is not None:
        return invalid
    limited = _rate_limited(request)
    if limited is not None:
        return limited
    try:
        page = await asyncio.to_thread(
            read_page, diagnostics_dir(), window[0], window[1], after_id)
        # Serialisation is inside the boundary on purpose: ``allow_nan=False`` is
        # the last check for a number the projections should have refused, and a
        # ValueError escaping here would bypass the plane's error body.
        return web.json_response(page, dumps=_strict_dumps)
    except Exception:  # noqa: BLE001 — a reader fault is a 500, never a partial page
        logger.exception("[telemetry-diagnostics] read failed")
        return admin_error("internal_error", "could not read the diagnostic log", 500)


async def handle_telemetry_stats(request):
    from aiohttp import web

    window, denied = admin_window(request, RESOURCE_STATS, allow_empty_window=True)
    if denied is not None:
        return denied
    limited = _rate_limited(request)
    if limited is not None:
        return limited
    try:
        stats = await asyncio.to_thread(read_stats, diagnostics_dir(), window[0], window[1])
        return web.json_response(stats, dumps=_strict_dumps)
    except Exception:  # noqa: BLE001
        logger.exception("[telemetry-diagnostics] stats failed")
        return admin_error("internal_error", "could not read the diagnostic log", 500)


def register_telemetry_diagnostics_routes(app) -> None:
    """Attach the read-only telemetry diagnostics endpoints to the relay app."""
    app.router.add_get("/v1/admin/telemetry-diagnostics", handle_telemetry_diagnostics)
    app.router.add_get("/v1/admin/telemetry-stats", handle_telemetry_stats)
    # Startup self-check. The directory reaches this process through the unit's
    # ``BindReadOnlyPaths=``; if that line is missing or points elsewhere the
    # resource answers "no data" forever, so say at boot which it is.
    #
    # Every probe is inside one try: under ``ProtectHome=true`` — the unit's state
    # before the bind is added — ``Path.is_dir()`` raises ``PermissionError``
    # rather than returning False, and an exception here runs during app
    # construction, i.e. it stops the whole relay from starting. A log line must
    # never be able to do that: the code has to be safe to deploy in any order
    # relative to the unit change.
    directory = diagnostics_dir()
    try:
        exists = directory.is_dir()
        readable = os.access(directory, os.R_OK) if exists else False
    except OSError as exc:
        exists, readable = f"unreadable ({exc.errno})", False
    logger.info(
        "[telemetry-diagnostics] dir=%s exists=%s readable=%s scoped_token_configured=%s",
        directory, exists, readable,
        bool(os.environ.get("HERMES_TELEMETRY_DIAG_TOKEN", "").strip()),
    )
