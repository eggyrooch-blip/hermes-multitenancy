"""Export Hermes skill usage to the Keep kep-telemetry Hub (skill-runs).

Why this exists
---------------
kep-telemetry (Keep's skill/expert usage collector, ``kep-cli install telemetry``)
hooks Claude Code / Codex / Cursor on employee Macs and uploads *lifecycle runs*
to ``/api/kep-cli-hub-admin/api/v1/skill-runs``. Its installer is macOS-only
(LaunchAgent) and its hooks have no host on hermes-1. Hermes already records the
same fact — one ``skill_view`` tool call per skill use — in the conversation
audit stream, so this module projects those facts onto the kep-telemetry upload
contract and posts them from the server with the hermes service identity.

Contract (kep-telemetry 0.1.5, upload contract revision 9)
----------------------------------------------------------
* ``POST {hub}/api/v1/skill-runs`` with ``Authorization: Bearer <token>``,
  ``X-Operator: <operator>`` (both from ``kep-auth --env <env> inject``) and body
  ``{"batch_id", "sent_at", "runs": [...]}``.
* Response ``200 {"accepted": n, "rejected": m, "errors": [{"index", "reason",
  "run_id"?}]}`` with ``accepted + rejected == len(runs)``; 400 quarantines the
  batch; 401/403 pause for auth; 429/5xx/network → retry the whole batch.
* Lifecycle payload keys are a closed set (``LIFECYCLE_V9_KEYS``). Anything
  outside is never sent. Strings never carry home/absolute paths.

Honesty rules
-------------
* ``client`` is ``hermes`` — never a Mac client name. If the gateway rejects the
  enum the record lands in dead-letter with the server's reason; that is the
  signal for the platform side to open the enum, not for us to masquerade.
* One ``skill_view`` call with a resolved ``args.name`` = one closed lifecycle
  record. ``status`` is always ``closed``; the terminal boundary is a *candidate*
  (``prompt_stop`` / medium) when a Run Broker terminal event for the same
  profile+platform follows within the match window, ``unresolved`` otherwise.
* No conversation content, chat ids, open ids or profile names leave the host.
  A local sidecar keeps ``run_id → profile`` so actor attribution can be
  backfilled once the gateway grows an actor dimension.
"""
from __future__ import annotations

import base64
import fcntl
import gzip
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

DEFAULT_AUDIT_PATH = Path("/var/log/hermes/conversation-audit.jsonl")
HUB_SKILL_RUNS_URL = {
    "online": "https://proxy.cms.example.com/api/kep-cli-hub-admin/api/v1/skill-runs",
    "pre": "https://proxy.cms.pre.example.com/api/kep-cli-hub-admin/api/v1/skill-runs",
}

CLIENT = "hermes"
SURFACE = "cloud"
RECORD_KIND = "lifecycle"
STATUS_CLOSED = "closed"
CLIENT_REVISION = 1
UPLOAD_CONTRACT_REVISION = 9
BATCH_SIZE = 100
HTTP_TIMEOUT_S = 15
DEFAULT_BACKFILL_DAYS = 7
MAX_READ_BYTES = 64 * 1024 * 1024  # per-pass read budget; the rest waits for the next tick
TERMINAL_MATCH_WINDOW = timedelta(minutes=30)
TERMINAL_RETENTION = timedelta(hours=2)
# Local diagnostic log (never uploaded): one NDJSON row per settlement so a telemetry
# developer can answer "why is this row not in Kibana / why is this field wrong" without
# the payload, the Hub's own answer and the batch id all being gone by then.
DIAGNOSTICS_DIRNAME = "diagnostics"
DIAGNOSTICS_RETENTION_DAYS = 14
DIAGNOSTICS_MAX_DAY_BYTES = 64 * 1024 * 1024
DIAGNOSTICS_BODY_CAP = 2048  # bytes of the Hub response kept verbatim per row
DIAGNOSE_GZIP_OVER_BYTES = 8 * 1024 * 1024  # a hand-off file bigger than this ships gzipped
# Keys that would mean conversation text got into a diagnostic row. The row builders use
# an explicit whitelist, so this is the second line of defence: it is checked against
# whatever the Hub sends back, where we control neither the shape nor the contents.
FORBIDDEN_CONTENT_KEYS = frozenset({
    "content", "prompt", "answer", "message", "messages", "text", "preview", "tool_calls",
    "input", "output", "completion", "body_text", "transcript", "chat_id", "open_id",
    "session_id", "message_id", "user_id", "union_id", "email", "mobile"})

# ── reject grading (kep-telemetry lib/outbox.mjs classifyRejectReason, :248-258) ──
# Why this exists: the first production rounds put every server rejection into one
# terminal state, so 500 records rejected with ``invalid_enum:client "hermes"`` — a
# contract problem that the Hub side later fixed — sat in dead-letter for two days and
# would never have been resent. Grading is the fix; the class name and the four values
# are kep's, so a record's disposition reads the same on both sides.
REJECT_RETRYABLE = "retryable"      # server-side/transient: resend, no human needed
REJECT_REPAIRABLE = "repairable"    # contract/mapping: resend on a backoff, it may start passing
REJECT_PERMANENT = "permanent"      # the record itself is unacceptable: never resend
REJECT_CONFLICT = "conflict"        # revision conflict: needs adjudication, never auto-resent
RETRYABLE_REASON_PREFIXES = frozenset({
    "internal_error", "storage_unavailable", "store_unavailable", "dependency_unavailable",
    "timeout", "temporarily_unavailable", "try_again", "rate_limited", "too_many_requests"})
PERMANENT_REASON_PREFIXES = frozenset({
    "retention_exceeded", "run_too_old", "forbidden_project", "operator_forbidden",
    "rejected_permanently"})
AUTO_RETRY_CLASSES = frozenset({REJECT_RETRYABLE, REJECT_REPAIRABLE})
# Backoff: one tick, then doubling, capped. 8 attempts ≈ 24h of trying before it rests.
DEAD_LETTER_MAX_ATTEMPTS = 8
DEAD_LETTER_BACKOFF_BASE = timedelta(minutes=10)
DEAD_LETTER_BACKOFF_CAP = timedelta(hours=6)
_SHANGHAI = timezone(timedelta(hours=8))
# Audit timestamps outside this span are treated as corrupt: every datetime arithmetic
# the exporter does (window, retention, cutoff) must be representable for a row to be
# processed, otherwise one poisoned line would freeze the cursor forever.
_TS_MIN = datetime(2000, 1, 1, tzinfo=timezone.utc)
_TS_MAX = datetime(2200, 1, 1, tzinfo=timezone.utc)
# Skill names that would leak a filesystem location are never normalised into the
# gateway charset; they are quarantined locally instead.
_LEAKY_SKILL = re.compile(r"^[/~\\]|^[A-Za-z]:[\\/]|(^|[/\\])\.\.([/\\]|$)|/Users/|/home/|\\\\|//")

# The subset of kep-telemetry lib/upload-map.mjs LIFECYCLE_V9_KEYS this exporter may
# emit. Every name here exists in the upstream v9 set; anything outside is never sent.
LIFECYCLE_V9_KEYS: frozenset[str] = frozenset({
    "record_id", "run_id", "client_revision", "record_kind", "client", "skill",
    "skill_version", "project_id", "opened_observed_at", "ended_at", "status",
    "model_name", "expert", "terminal_reason", "attempt_count",
    "stage_at_open", "surface", "token_metrics",
    "boundary_status", "boundary_source", "boundary_confidence",
})
# Gateway closed set for ``status``: open / closed / timed_out / incomplete.
GATEWAY_STATUS = frozenset({"open", "closed", "timed_out", "incomplete"})
# lib/upload-map.mjs V9_BOUNDARY['turn_closed'] and the unresolved fallback.
BOUNDARY_TURN_CLOSED = ("candidate", "prompt_stop", "medium")
BOUNDARY_UNRESOLVED = ("unresolved", "unresolved", "unresolved")
# Gateway charsets: lib/schemas/skill-run.mjs:404 (skill) and lib/expert.mjs normalizeExpert (expert).
SKILL_CHARSET = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
EXPERT_CHARSET = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def classify_reject_reason(reason: str) -> str:
    """Grade one server rejection. Unknown prefixes are ``repairable``, never permanent:
    a reason we have not seen must not silently become "lost forever"."""
    prefix = str(reason or "").split(":", 1)[0].strip()
    if prefix == "local":
        # our own refusals (charset, processing) never pass on a retry. Exact match: a
        # server reason like ``localization_unavailable`` is not ours and must not be
        # buried as permanent — unknown reasons always stay repairable.
        return REJECT_PERMANENT
    if prefix == "revision_conflict":
        return REJECT_CONFLICT
    if prefix in RETRYABLE_REASON_PREFIXES:
        return REJECT_RETRYABLE
    if prefix in PERMANENT_REASON_PREFIXES:
        return REJECT_PERMANENT
    return REJECT_REPAIRABLE


def retry_backoff(attempt: int) -> timedelta:
    """``base * 2^(attempt-1)``, capped. Attempt 1 waits one timer tick."""
    n = max(1, int(attempt))
    delay = DEAD_LETTER_BACKOFF_BASE * (2 ** (n - 1))
    return min(delay, DEAD_LETTER_BACKOFF_CAP)


class InvalidSkillName(ValueError):
    """Skill name cannot be expressed in the gateway charset even after normalisation."""


class UploadSafetyError(ValueError):
    """Payload violates the outbound contract (unknown key or leaky string)."""


class AuthUnavailable(RuntimeError):
    """kep-auth could not hand us a token/operator pair."""


class StateBusy(RuntimeError):
    """Another exporter process holds the state directory lock."""


class EnvMismatch(RuntimeError):
    """The state directory was bound to a different Hub environment."""


# ── facts from the audit stream ───────────────────────────────────────────


class AuditLine(str):
    """A raw audit line that also knows the byte offset it starts at.

    A ``str`` subclass on purpose: every existing reader keeps working (and keeps
    comparing equal to a plain string), while the diagnostic log can record where the
    line lives so ``run_id`` — a one-way hash — no longer has to be brute-forced back
    onto a 400 MB audit file. ``sed -n '<offset>p'`` needs a line number; a byte offset
    is what lets ``dd skip=<offset>`` land on it directly.
    """

    offset: int

    def __new__(cls, text: str, offset: int) -> "AuditLine":
        obj = super().__new__(cls, text)
        obj.offset = int(offset)
        return obj


@dataclass(frozen=True)
class SkillCall:
    profile: str
    platform: str
    session_id: str
    message_id: str
    skill: str
    observed_at: str  # RFC3339 as written by the audit (+08:00)
    # Where this call was read from. Defaulted so pending.json rows written by an
    # earlier version still rehydrate (``_rebuild`` drops rows that raise TypeError).
    source_inode: Optional[int] = None
    source_offset: Optional[int] = None

    @property
    def observed_dt(self) -> datetime:
        return parse_ts(self.observed_at)

    @property
    def locator(self) -> dict[str, Any]:
        """The audit row this call came from: ``dd bs=1 skip=<offset>`` reaches it."""
        return {"inode": self.source_inode, "offset": self.source_offset, "observed_at": self.observed_at}


@dataclass(frozen=True)
class RunTerminal:
    profile: str
    platform: str
    at: str
    terminal_status: str
    expert_id: Optional[str]

    @property
    def at_dt(self) -> datetime:
        return parse_ts(self.at)


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_SHANGHAI)
    return dt


def parse_ts_checked(value: str) -> Optional[datetime]:
    """parse_ts, but None for anything the exporter's arithmetic could not represent."""
    try:
        dt = parse_ts(value)
        if not (_TS_MIN <= dt <= _TS_MAX):
            return None
        dt.astimezone(_SHANGHAI)
        return dt
    except (ValueError, OverflowError, OSError):
        return None


_TS_FIELD = re.compile(rb'"@timestamp"\s*:\s*"([^"]{10,40})"')


def peek_timestamp(line: bytes) -> Optional[datetime]:
    """Cheap timestamp probe used to place the backfill start without json-decoding a line."""
    m = _TS_FIELD.search(line)
    if not m:
        return None
    return parse_ts_checked(m.group(1).decode("ascii", errors="replace"))


def rfc3339(dt: datetime) -> str:
    """kep-telemetry localRfc3339: ``YYYY-MM-DDTHH:MM:SS.mmm+08:00`` (millis + numeric offset, never ``Z``)."""
    return dt.astimezone(_SHANGHAI).isoformat(timespec="milliseconds")


def normalize_skill_name(name: str) -> str:
    """Hermes skill names may be namespaced with ``ns/skill``; the gateway charset allows ``:`` instead.

    Anything that looks like a filesystem location (absolute, home, drive, ``..``, ``//``)
    is refused *before* the slash rewrite so a path can never be smuggled out as
    ``:home:hermes:private-skill``.
    """
    raw = name.strip()
    if not raw or _LEAKY_SKILL.search(raw):
        raise InvalidSkillName(name)
    candidate = raw.replace("/", ":")
    if not SKILL_CHARSET.match(candidate):
        raise InvalidSkillName(name)
    return candidate


def make_batch_id(now: datetime) -> str:
    """flush.mjs batch id shape: ``v1-<epoch ms base36>-<6 hex>``."""
    ms = int(now.timestamp() * 1000)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while ms:
        ms, rem = divmod(ms, 36)
        out = digits[rem] + out
    return f"v1-{out or '0'}-{secrets.token_hex(3)}"


def parse_audit_line(line: str, *, inode: Optional[int] = None) -> SkillCall | RunTerminal | None:
    """Return the fact a raw audit line carries, or None when irrelevant/corrupt.

    ``inode`` and the line's own byte offset (when it is an ``AuditLine``) ride along on
    the ``SkillCall`` so the local diagnostic log can point back at this exact row.
    """
    offset = getattr(line, "offset", None)
    line = line.strip()
    if not line:
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if not isinstance(row, dict):
        return None
    ts = row.get("@timestamp")
    if not isinstance(ts, str) or not ts:
        return None
    kind = row.get("event_type")
    if kind not in ("run_terminal", "conversation_message"):
        return None
    if parse_ts_checked(ts) is None:
        return None  # unparseable or out-of-range timestamp: corrupt row, never a cursor stall
    if kind == "run_terminal":
        return RunTerminal(
            profile=str(row.get("profile") or ""),
            platform=str(row.get("platform") or ""),
            at=ts,
            terminal_status=str(row.get("terminal_status") or ""),
            expert_id=(str(row["expert_id"]) if row.get("expert_id") else None),
        )
    if kind != "conversation_message" or row.get("tool_name") != "skill_view":
        return None
    raw_calls = row.get("tool_calls")
    if not isinstance(raw_calls, str) or not raw_calls:
        return None
    try:
        call = json.loads(raw_calls)
    except ValueError:
        return None
    args = call.get("args") if isinstance(call, dict) else None
    name = args.get("name") if isinstance(args, dict) else None
    if not isinstance(name, str) or not name.strip():
        return None  # "generating arguments" placeholder rows carry no skill
    session_id = row.get("session_id")
    message_id = row.get("message_id")
    if session_id in (None, "") or message_id in (None, ""):
        return None
    return SkillCall(
        profile=str(row.get("profile") or ""),
        platform=str(row.get("platform") or ""),
        session_id=str(session_id),
        message_id=str(message_id),
        skill=name.strip(),
        observed_at=ts,
        source_inode=inode,
        source_offset=offset,
    )


# ── projection onto the upload contract ────────────────────────────────────


def run_id_for(profile: str, session_id: str, message_id: str) -> str:
    """Deterministic 32-hex run id: replaying the same audit row yields the same id."""
    digest = hashlib.sha256(f"hermes|{profile}|{session_id}|{message_id}".encode("utf-8")).hexdigest()
    return digest[:32]


def match_terminal(call: SkillCall, terminals: Iterable[RunTerminal]) -> Optional[RunTerminal]:
    """Nearest run_terminal for the same profile+platform inside [observed, observed+window]."""
    start = call.observed_dt
    end = start + TERMINAL_MATCH_WINDOW
    best: Optional[RunTerminal] = None
    for term in terminals:
        if term.profile != call.profile or term.platform != call.platform:
            continue
        at = term.at_dt
        if at < start or at > end:
            continue
        if best is None or at < best.at_dt:
            best = term
    return best


def assert_upload_safe(record: dict[str, Any], allowed: frozenset[str] = LIFECYCLE_V9_KEYS) -> dict[str, Any]:
    for key in record:
        if key not in allowed:
            raise UploadSafetyError(f"unexpected upload key: {key}")

    def _inspect(value: Any, at: str) -> None:
        if isinstance(value, list):
            for i, item in enumerate(value):
                _inspect(item, f"{at}[{i}]")
            return
        if isinstance(value, dict):
            for k, item in value.items():
                _inspect(item, f"{at}.{k}")
            return
        if not isinstance(value, str):
            return
        if "/Users/" in value or "/home/" in value:
            raise UploadSafetyError(f"{at} carries a home path")
        if value.startswith("/") and len(value) > 1:
            raise UploadSafetyError(f"{at} is an absolute path")

    _inspect(record, "$")
    return record


def build_record(call: SkillCall, terminal: Optional[RunTerminal]) -> dict[str, Any]:
    run_id = run_id_for(call.profile, call.session_id, call.message_id)
    boundary = BOUNDARY_TURN_CLOSED if terminal is not None else BOUNDARY_UNRESOLVED
    record: dict[str, Any] = {
        "record_id": run_id,
        "run_id": run_id,
        "client_revision": CLIENT_REVISION,
        "record_kind": RECORD_KIND,
        "client": CLIENT,
        "skill": normalize_skill_name(call.skill),
        "project_id": None,
        "opened_observed_at": rfc3339(call.observed_dt),
        "ended_at": rfc3339(terminal.at_dt) if terminal is not None else rfc3339(call.observed_dt),
        "status": STATUS_CLOSED,
        "model_name": None,
        "surface": SURFACE,
        "boundary_status": boundary[0],
        "boundary_source": boundary[1],
        "boundary_confidence": boundary[2],
    }
    if terminal is not None and terminal.expert_id and EXPERT_CHARSET.match(terminal.expert_id):
        record["expert"] = terminal.expert_id
    if record["status"] not in GATEWAY_STATUS:
        raise UploadSafetyError(f"status outside the gateway closed set: {record['status']}")
    return assert_upload_safe(record)


def content_hash(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ── persistent state ──────────────────────────────────────────────────────


def default_state_dir() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "state" / "kep-telemetry-export"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except ValueError:
        logger.warning("kep-telemetry-export: %s is corrupt; starting it fresh", path)
        return default


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _append_ndjson(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.chmod(path, 0o600)


def _rebuild(cls: Any, raw: Any, path: Path) -> list[Any]:
    """Rehydrate dataclass rows, dropping shapes this version cannot read.

    A single row written by another version must not raise on every later run —
    that would freeze the exporter until someone deletes the state file by hand.
    """
    out: list[Any] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            out.append(cls(**item))
        except TypeError:
            logger.warning("kep-telemetry-export: dropping unreadable row in %s", path)
    return out


class DeadLetterView:
    """The dead-letter file read as state, not as a graveyard.

    One row per rejection is appended forever (append-only stays append-only); this
    folds them to the latest disposition per ``run_id`` and answers the only three
    questions the upload path has:

      * is this run finished for good (``permanent`` / ``conflict`` / attempts spent)?
      * is it waiting out a backoff?
      * is it due for another try, and what payload do I resend?

    Rows written before grading existed carry no ``class`` and no ``payload``. They are
    counted as ``legacy`` and left alone: without the payload a resend would mean
    rebuilding the record from a 400 MB audit, and that scan is exactly what we refuse
    to do. Nothing about them is silently presented as retried.
    """

    def __init__(self, rows: Iterable[dict[str, Any]]):
        self.corrupt = 0
        self.latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            rid = row.get("run_id")
            if not isinstance(rid, str) or not rid:
                continue
            prev = self.latest.get(rid)
            if prev is None or _attempt_of(row) >= _attempt_of(prev):
                self.latest[rid] = row

    def grade(self, row: dict[str, Any]) -> str:
        klass = row.get("class")
        if isinstance(klass, str) and klass:
            return klass
        return classify_reject_reason(str(row.get("reason", "")))

    def _is_legacy(self, row: dict[str, Any]) -> bool:
        """No payload on file ⇒ nothing to resend, whatever the grade says."""
        return not isinstance(row.get("payload"), dict)

    def due(self, now: datetime) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """``[(payload, row)]`` for every run whose backoff has elapsed.

        The payload comes off disk, so it is validated before it can be queued: a row
        with a missing or mismatched ``run_id`` would otherwise raise inside the pass and
        take every healthy record down with it, every ten minutes, forever.
        """
        out = []
        for run_id, row in self.latest.items():
            if self.grade(row) not in AUTO_RETRY_CLASSES or self._is_legacy(row):
                continue
            attempt = _attempt_of(row)
            if attempt >= DEAD_LETTER_MAX_ATTEMPTS:
                continue
            payload = self._validated_payload(run_id, row)
            if payload is None:
                self.corrupt += 1
                continue
            last = parse_ts_checked(str(row.get("at", "")))
            if last is None or now >= last + retry_backoff(attempt):
                out.append((payload, row))
        return out

    def _validated_payload(self, run_id: str, row: dict[str, Any]) -> Optional[dict[str, Any]]:
        """The stored payload, only if it is still a legal upload for this run."""
        payload = row.get("payload")
        if not isinstance(payload, dict) or payload.get("run_id") != run_id:
            return None
        try:
            return assert_upload_safe(project_payload(payload))
        except UploadSafetyError:
            return None

    def counts(self, now: datetime) -> dict[str, int]:
        tally = {"waiting": 0, "exhausted": 0, "permanent": 0, "legacy": 0,
                 "corrupt": self.corrupt, "by_class": {}}
        for row in self.latest.values():
            grade = self.grade(row)
            _bump(tally["by_class"], grade)
            if grade not in AUTO_RETRY_CLASSES:
                tally["permanent"] += 1
                continue
            if self._is_legacy(row):
                tally["legacy"] += 1
                continue
            attempt = _attempt_of(row)
            if attempt >= DEAD_LETTER_MAX_ATTEMPTS:
                tally["exhausted"] += 1
                continue
            last = parse_ts_checked(str(row.get("at", "")))
            if last is not None and now < last + retry_backoff(attempt):
                tally["waiting"] += 1
        return tally

    def closed_ids(self, now: datetime, due_ids: set[str]) -> set[str]:
        """Runs the audit-build path must not queue again this pass: everything with a
        rejection on file except the ones being resent right now."""
        return {rid for rid in self.latest if rid not in due_ids}


def _seq_of(line: bytes) -> Optional[int]:
    """The ``seq`` of one raw line, or None when it is not a usable diagnostic row."""
    if not line.strip():
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if not isinstance(row, dict):
        return None  # a bare scalar line must not raise on .get
    seq = row.get("seq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else None


def _attempt_of(row: dict[str, Any]) -> int:
    value = row.get("attempt")
    return max(1, int(value)) if isinstance(value, int) else 1
class ExportState:
    """Everything the exporter remembers between timer runs, all under one dir."""

    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.cursor_path = self.dir / "cursor.json"
        self.pending_path = self.dir / "pending.json"
        self.terminals_path = self.dir / "terminals.json"
        self.outbox_path = self.dir / "outbox.json"
        self.ledger_path = self.dir / "ledger.json"
        self.dead_letter_path = self.dir / "dead-letter.ndjson"
        self.profile_map_path = self.dir / "profile-map.json"
        self.locator_map_path = self.dir / "locator-map.json"
        self.last_run_path = self.dir / "last-run.json"
        self.env_path = self.dir / "env.json"
        self.lock_path = self.dir / "lock"
        self.diagnostics_dir = self.dir / DIAGNOSTICS_DIRNAME
        self._lock_fh: Any = None

    # cross-process lock ----------------------------------------------------
    def acquire_lock(self) -> None:
        """Exclusive, non-blocking flock on the state dir; held until ``release_lock``.

        A manual CLI run overlapping the timer must not race it: whichever finishes
        last would otherwise overwrite the other's outbox/ledger.
        """
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fh = self.lock_path.open("a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise StateBusy(f"another exporter holds {self.lock_path}") from exc
        self._lock_fh = fh

    def release_lock(self) -> None:
        fh, self._lock_fh = self._lock_fh, None
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

    # environment binding --------------------------------------------------
    def load_env(self) -> Optional[str]:
        raw = _read_json(self.env_path, None)
        env = raw.get("env") if isinstance(raw, dict) else None
        return str(env) if isinstance(env, str) and env else None

    def bind_env(self, env: str) -> None:
        """Bind the directory to one Hub environment; a different env later is refused.

        pre and online share nothing: a cursor advanced against pre would silently mark
        production records as already processed.
        """
        bound = self.load_env()
        if bound is None:
            _write_json_atomic(self.env_path, {"env": env})
        elif bound != env:
            raise EnvMismatch(f"state dir {self.dir} is bound to env={bound}, refusing env={env}; "
                              "use a separate --state-dir per environment")

    # cursor ---------------------------------------------------------------
    def load_cursor(self) -> dict[str, Any]:
        raw = _read_json(self.cursor_path, {})
        if not isinstance(raw, dict):
            return {}
        out: dict[str, Any] = {k: int(v) for k, v in raw.items() if k in ("inode", "offset") and isinstance(v, int)}
        for key in ("backfill_cutoff", "high_water"):
            if isinstance(raw.get(key), str) and parse_ts_checked(raw[key]) is not None:
                out[key] = raw[key]
        return out

    def save_cursor(self, cursor: dict[str, Any]) -> None:
        payload = {k: cursor[k] for k in ("inode", "offset", "backfill_cutoff", "high_water") if cursor.get(k) is not None}
        _write_json_atomic(self.cursor_path, payload)

    # pending calls / recent terminals -------------------------------------
    def load_pending(self) -> list[SkillCall]:
        return _rebuild(SkillCall, _read_json(self.pending_path, []), self.pending_path)

    def save_pending(self, calls: list[SkillCall]) -> None:
        _write_json_atomic(self.pending_path, [asdict(c) for c in calls])

    def load_terminals(self) -> list[RunTerminal]:
        return _rebuild(RunTerminal, _read_json(self.terminals_path, []), self.terminals_path)

    def save_terminals(self, terminals: list[RunTerminal]) -> None:
        _write_json_atomic(self.terminals_path, [asdict(t) for t in terminals])

    # outbox / ledger / dead-letter ----------------------------------------
    def load_outbox(self) -> list[dict[str, Any]]:
        raw = _read_json(self.outbox_path, [])
        return [item for item in raw if isinstance(item, dict) and "run_id" in item]

    def save_outbox(self, records: list[dict[str, Any]]) -> None:
        _write_json_atomic(self.outbox_path, records)

    def load_ledger(self) -> dict[str, dict[str, Any]]:
        raw = _read_json(self.ledger_path, {})
        confirmed = raw.get("confirmed") if isinstance(raw, dict) else None
        return dict(confirmed) if isinstance(confirmed, dict) else {}

    def save_ledger(self, confirmed: dict[str, dict[str, Any]]) -> None:
        _write_json_atomic(self.ledger_path, {"format_version": 1, "confirmed": confirmed})

    def append_dead_letter(self, record: dict[str, Any], reason: str, at: str, *,
                           klass: Optional[str] = None, attempt: int = 1,
                           first_at: Optional[str] = None, payload: Optional[dict[str, Any]] = None,
                           locator: Optional[dict[str, Any]] = None) -> None:
        """One rejection, graded, with the payload that was refused.

        Field names follow the kep-telemetry dead-letter row (``flush.mjs:654-666``):
        ``at / reason / class / run_id / content_hash / attempt / first_at``. ``payload``
        is our extension and it is what makes an automatic retry possible at all — the
        outbox row is gone by the time a rejection is recorded, and rebuilding it would
        mean re-reading the audit.
        """
        grade = klass or classify_reject_reason(reason)
        # Same content rule as the diagnostic log, and for the same reason: this file is
        # read back out through the admin endpoint, so the guarantee has to hold in the
        # file. ``skill`` on a local rejection is the audit's own string — which is
        # exactly the path-shaped name the gate refused — so it is validated whole and
        # withheld when it fails, never truncated-then-trusted.
        raw_skill = record.get("skill")
        safe_skill = raw_skill if isinstance(raw_skill, str) and SKILL_CHARSET.match(raw_skill) else None
        row: dict[str, Any] = {
            "run_id": record["run_id"], "reason": sanitize_reason(reason), "at": at, "class": grade,
            "skill": safe_skill, "skill_withheld": safe_skill is None and raw_skill is not None,
            "attempt": max(1, int(attempt)), "first_at": first_at or at,
        }
        if payload is not None:
            # whitelisted the same way as the diagnostic row, never dumped wholesale
            row["payload"] = project_payload(payload)
            row["content_hash"] = content_hash(payload)
        # The locator has to survive with the rejection: the record leaves the outbox
        # here, and a resend two hours later still has to be traceable back to its audit
        # line — that is the whole point of carrying an offset.
        safe_locator = _validated_locator(locator)
        if safe_locator is not None:
            row["locator"] = safe_locator
        _append_ndjson(self.dead_letter_path, row)

    def load_dead_letter(self) -> list[dict[str, Any]]:
        try:
            lines = self.dead_letter_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def settled_run_ids(self) -> set[str]:
        """Every run_id that already has a final disposition: confirmed or dead-lettered.

        Kept as it was for callers that only ask "has this been dealt with at all".
        The upload path uses ``dead_letter_view`` instead, because a graded rejection is
        not necessarily final.
        """
        ids = set(self.load_ledger())
        for row in self.load_dead_letter():
            rid = row.get("run_id") if isinstance(row, dict) else None
            if isinstance(rid, str) and rid:
                ids.add(rid)
        return ids

    def dead_letter_view(self) -> "DeadLetterView":
        return DeadLetterView(self.load_dead_letter())

    def load_profile_map(self) -> dict[str, str]:
        raw = _read_json(self.profile_map_path, {})
        return dict(raw) if isinstance(raw, dict) else {}

    def save_profile_map(self, mapping: dict[str, str]) -> None:
        _write_json_atomic(self.profile_map_path, mapping)

    def load_locator_map(self) -> dict[str, dict[str, Any]]:
        """``run_id → {inode, offset, observed_at}`` for records still awaiting settlement.

        Kept next to the outbox rather than inside them: an outbox row is posted to the
        Hub verbatim, so it can never carry a local-only field. Pruned to the queue at
        the end of every pass — this sidecar must not become a second unbounded file.
        """
        raw = _read_json(self.locator_map_path, {})
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for rid, value in raw.items():
            if not isinstance(rid, str) or not rid or not isinstance(value, dict):
                continue
            safe = _validated_locator(value) if set(value) <= {"inode", "offset", "observed_at"} else None
            out[rid] = safe if safe is not None else {"reason": "invalid_locator"}
        return out

    def save_locator_map(self, mapping: dict[str, dict[str, Any]]) -> None:
        _write_json_atomic(self.locator_map_path, mapping)

    def save_last_run(self, report: dict[str, Any]) -> None:
        _write_json_atomic(self.last_run_path, report)


# ── local diagnostic log ──────────────────────────────────────────────────


class DiagnosticsLog:
    """Append-only local evidence for one exporter pass. Never uploaded, never fatal.

    What it exists for: until now a settled record left nothing behind that a telemetry
    developer could read. An accepted record kept only its sha256 in the ledger, a
    rejected one kept four fields, and the request body, the ``batch_id`` and the Hub's
    own answer were discarded the moment the pass ended. So "why is this row not in
    Kibana / why is this field wrong" had no local answer.

    Non-negotiable: nothing in here may change what the exporter uploads or settles.
    Every method is wrapped in its own error boundary — building the row, encoding the
    response and writing the line all count a failure and return. A diagnostic that can
    abort a pass between the POST and ``save_ledger`` would cause a re-upload, which is
    strictly worse than having no log.

    Disk discipline (hermes-1 has a write-throttling incident history):
      * one ``open``/``close`` per pass, binary append, **no per-line fsync** — this is
        evidence, not the settlement record; ledger/dead-letter remain the durable ones;
      * one file per day, retention enforced *here*, in the same pass — a declared
        retention nobody executes is how the other collector kept everything forever;
      * a hard per-day byte cap: past it the log stops taking rows for good (the marker
        is written once per day, not once per pass) and says so in the file and report.

    Content rule: this file is local, 0600, and may carry the host's own paths and the
    Hermes profile (sunke 2026-09-20). It must never carry the bearer token, the session
    or message id, conversation text, or an audit-supplied string that failed the
    gateway charset — that last one is attacker-influenced, so it is dropped and flagged
    rather than echoed.
    """

    def __init__(self, directory: Path, now: datetime, *, retention_days: int = DIAGNOSTICS_RETENTION_DAYS,
                 max_day_bytes: int = DIAGNOSTICS_MAX_DAY_BYTES, body_cap: int = DIAGNOSTICS_BODY_CAP,
                 secret: Optional[str] = None):
        self.secret = secret or None  # the live bearer token, held only to scrub it back out
        self.dir = Path(directory)
        self.now = now
        self.retention_days = max(0, int(retention_days))
        self.max_day_bytes = max(1, int(max_day_bytes))
        self.body_cap = max(0, int(body_cap))
        self.path = self.dir / f"export-{now.astimezone(_SHANGHAI):%Y%m%d}.ndjson"
        self.rows = 0
        self.dropped = 0
        self.errors = 0
        self.pruned: list[str] = []
        self.stopped: Optional[str] = None
        self._fh: Any = None
        self._size = 0
        self._marked = False
        self._seq = 0  # last sequence number in today's file; recovered on open

    # lifecycle ------------------------------------------------------------
    def prune(self) -> None:
        """Delete day files older than the retention window. Filename date, not mtime.

        The name carries the day the rows belong to; mtime is whatever last touched the
        file. A name we cannot read is left alone rather than guessed at.
        """
        cutoff = (self.now.astimezone(_SHANGHAI) - timedelta(days=self.retention_days)).date()
        try:
            entries = sorted(self.dir.glob("export-*.ndjson"))
        except OSError:
            self.errors += 1
            return
        for entry in entries:
            stamp = entry.name[len("export-"):-len(".ndjson")]
            try:
                day = datetime.strptime(stamp, "%Y%m%d").date()
            except ValueError:
                continue
            if day >= cutoff:
                continue
            try:
                entry.unlink()
                self.pruned.append(entry.name)
            except OSError:
                self.errors += 1

    def _open(self) -> Any:
        if self._fh is not None:
            return self._fh
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._size = self.path.stat().st_size
        except FileNotFoundError:
            self._size = 0
        self._seq = self._recover_seq()
        has_marker = self._tail_is_marker()
        if self._size >= self.max_day_bytes or has_marker:
            # Today already hit the cap — in this pass or an earlier one. The marker is a
            # property of the day file, not of this process, so it is added exactly once;
            # a file that is full but carries no marker (marker write failed, or it was
            # pre-filled) still gets one, or it would stop writing without saying why.
            self.stopped = "size_cap"
            self._marked = has_marker
        self._fh = self.path.open("ab")
        os.chmod(self.path, 0o600)
        if self._needs_newline():
            # A crash can leave a line without its terminator. Appending straight onto it
            # would glue two records into one unparsable row and lose both.
            self._fh.write(b"\n")
            self._size += 1
        if self.stopped == "size_cap":
            self._mark_truncated()
        return self._fh

    def _next_id(self) -> tuple[str, int]:
        """``(id, seq)`` for the next row.

        The downstream reader pages with ``since=<at>&after_id=<id>``; a timestamp alone
        stalls on rows sharing a millisecond, which is exactly what the relay review
        caught. ``<YYYYMMDD>-<seq:08d>`` sorts lexicographically in append order, is
        unique within the day file, and never changes once written.
        """
        self._seq += 1
        return f"{self.now.astimezone(_SHANGHAI):%Y%m%d}-{self._seq:08d}", self._seq

    def _recover_seq(self) -> int:
        """Highest sequence already in today's file, so ids keep climbing across passes.

        Reads the tail first — appends mean the last rows carry the highest seq. Only a
        tail we cannot parse at all falls back to reading the file, and the per-day cap
        bounds how much that can ever be.
        """
        if self._size == 0:
            return 0
        try:
            with self.path.open("rb") as fh:
                fh.seek(max(0, self._size - 8192))
                tail = fh.read(8192)
            for line in reversed(tail.split(b"\n")):
                seq = _seq_of(line)
                if seq is not None:
                    return seq
            highest = 0
            with self.path.open("rb") as fh:
                for line in fh:
                    seq = _seq_of(line)
                    if seq is not None and seq > highest:
                        highest = seq
            return highest
        except OSError:
            self.errors += 1
            return 0

    def _needs_newline(self) -> bool:
        if self._size == 0:
            return False
        try:
            with self.path.open("rb") as fh:
                fh.seek(self._size - 1)
                return fh.read(1) != b"\n"
        except OSError:
            self.errors += 1
            return False

    def _tail_is_marker(self) -> bool:
        """Did an earlier pass already close this day file with the cap marker?"""
        if self._size == 0:
            return False
        try:
            with self.path.open("rb") as fh:
                fh.seek(max(0, self._size - 512))
                tail = fh.read(512)
        except OSError:
            return False
        last = tail.rstrip(b"\n").rsplit(b"\n", 1)[-1]
        return b'"kind": "truncated"' in last

    def _write(self, row: dict[str, Any], *, force: bool = False) -> None:
        try:
            if self.stopped is not None and not force:
                self.dropped += 1
                return
            fh = self._open()  # opening first is what makes the sequence recoverable
            row_id, seq = self._next_id()
            row = dict(row, id=row_id, seq=seq)
            # ensure_ascii on the whole line: escaping preserves every value byte-for-byte
            # after json.loads. Encoding with errors='replace' would rewrite a lone
            # surrogate inside a structured error while response.body kept it — two
            # contradictory pieces of evidence for the same rejection.
            buf = (json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n").encode("utf-8")
            if not force:
                if self.stopped is not None:  # _open may have discovered the cap
                    self.dropped += 1
                    return
                if self._size + len(buf) > self.max_day_bytes:
                    self.dropped += 1
                    self.stopped = "size_cap"
                    self._mark_truncated()
                    return
            fh.write(buf)
            self._size += len(buf)
            if not force:
                self.rows += 1
        except Exception:  # noqa: BLE001 — a diagnostic must never take the pass down
            self.errors += 1

    def _mark_truncated(self) -> None:
        """One self-disclosing row per day file, so the file itself admits it stopped."""
        if self._marked:
            return
        self._marked = True
        self._write({"at": rfc3339(self.now), "kind": "truncated", "reason": "size_cap",
                     "max_day_bytes": self.max_day_bytes}, force=True)

    def close(self) -> None:
        """Close the handle. A close failure means the tail may not have reached disk,
        so it is counted like any other write failure rather than passed over."""
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.close()
        except OSError:
            self.errors += 1

    def summary(self) -> dict[str, Any]:
        return {"path": str(self.path), "rows": self.rows, "dropped": self.dropped,
                "errors": self.errors, "pruned": self.pruned, "stopped": self.stopped}

    # rows -----------------------------------------------------------------
    def upload_attempt(self, record: dict[str, Any], *, batch_id: str, operator: str, url: str,
                       sent_at: str, status: Optional[int], body: Any, outcome: str,
                       reason: Optional[str], klass: Optional[str], tries: int, index: Optional[int],
                       profile: Optional[str], locator: Optional[dict[str, Any]],
                       first_at: Optional[str] = None) -> None:
        """One POSTed record: what went out, what came back, how it settled.

        Field names are kep-telemetry's where an equivalent exists (``at``, ``reason``,
        ``class``, ``run_id``, ``content_hash``, ``attempt``, ``first_at``); ``request``,
        ``response``, ``batch_id``, ``operator``, ``url``, ``skill`` and the
        ``source_*`` locator are our extensions, appended, never redefining his.
        """
        try:
            row = {
                "kind": "upload_attempt",
                "run_id": record.get("run_id"),
                "content_hash": content_hash(record),
                "skill": record.get("skill"),
                "profile": self._safe_operator(profile),
                "profile_withheld": self._safe_operator(profile) is None and profile is not None,
                "operator": self._safe_operator(operator),
                "batch_id": batch_id,
                "url": url,
                "attempt": max(1, int(tries)),
                "first_at": first_at,
                "reason": sanitize_reason(reason) if reason is not None else None,
                "class": klass,
                "request": project_payload(record),
                "response": self._response(status, body, index=index),
                "verdict": {"outcome": outcome,
                            "reason": sanitize_reason(reason) if reason is not None else None},
            }
            row.update(self._stamp())
            row.update(_source_of(locator))
            row["sent_at"] = sent_at
        except Exception:  # noqa: BLE001 — building the row is inside the boundary too
            self.errors += 1
            return
        self._write(row)

    def local_reject(self, *, run_id: str, skill: Optional[str], reason: str, klass: str,
                     profile: Optional[str], locator: Optional[dict[str, Any]]) -> None:
        """A record the exporter itself refused: it never reaches the Hub, so nothing
        server-side will ever explain its absence from Kibana.

        The skill string is the reason these rows exist — and it comes from the audit, so
        it may be exactly the path-shaped name the upload gate refused. The **full** value
        is validated (never a truncated slice, which would let a 70-character name through)
        and withheld when it fails; ``source_offset`` is how the original row is retrieved.
        """
        try:
            safe = skill if isinstance(skill, str) and SKILL_CHARSET.match(skill) else None
            row = {
                "kind": "local_reject",
                "run_id": run_id,
                "skill": safe,
                "skill_withheld": safe is None,
                "profile": self._safe_operator(profile),
                "profile_withheld": self._safe_operator(profile) is None and profile is not None,
                "operator": None,
                "batch_id": None,
                "attempt": 1,
                "reason": reason,
                "class": klass,
                "request": None,
                "response": None,
                "verdict": {"outcome": "dead_letter", "reason": reason},
            }
            row.update(self._stamp())
            row.update(_source_of(locator))
        except Exception:  # noqa: BLE001
            self.errors += 1
            return
        self._write(row)

    def pass_summary(self, report: dict[str, Any]) -> None:
        """The whole pass report, including the rounds that sent nothing at all.

        ``last-run.json`` is overwritten every ten minutes, so without this row an auth
        stop or a rate-limit stop leaves no trace anyone can look back at. The caller
        strips the report's own ``diagnostics`` block: counting this row inside itself
        would always be stale, and a stale count reads as a bug.
        """
        row = {"kind": "pass_summary", "report": project_report(report)}
        row.update(self._stamp())
        self._write(row)

    # helpers --------------------------------------------------------------
    def _stamp(self) -> dict[str, Any]:
        """``at`` in the exporter's RFC3339 (as the shipped dead-letter already writes it)
        plus the epoch milliseconds kep's own rows carry, so either reader works."""
        return {"at": rfc3339(self.now), "at_epoch_ms": int(self.now.timestamp() * 1000)}

    def _safe_operator(self, value: Optional[str]) -> Optional[str]:
        """A profile/operator is an employee account name. Anything outside that charset
        came from somewhere we do not control and is withheld rather than echoed."""
        if isinstance(value, str) and OPERATOR_CHARSET.match(value):
            return value
        return None

    def _scrub(self, text: str) -> tuple[str, bool]:
        """Strip anything credential-shaped a proxy may have echoed back at us.

        A gateway that mirrors the request headers into its error body would otherwise
        put the live bearer token into this file. The token is known here only for the
        length of the call.
        """
        redacted = False
        if self.secret:
            for form in (self.secret, self.secret[:24]):
                if form and len(form) >= 8 and form in text:
                    text = text.replace(form, "«redacted»")
                    redacted = True
        scrubbed, n = _CREDENTIAL_ECHO.subn("«redacted»", text)
        return (scrubbed, redacted or n > 0)

    def _response(self, status: Optional[int], body: Any, *, index: Optional[int] = None) -> dict[str, Any]:
        """The Hub's answer, up to ``body_cap`` bytes, plus this record's own error entry.

        Truncation is measured in bytes and cut on a character boundary, and the flag is
        set here — the only place that knows the real length. ``error`` is projected out
        of the parsed body by index, so the reason for *this* record survives even when a
        batch of 100 rejections pushes the errors array past the cap.

        A parsed JSON body is re-serialized with ``ensure_ascii=True``: escaping keeps
        every field byte-equal after ``json.loads`` (``errors="replace"`` would rewrite a
        lone surrogate into ``?`` and quietly change the evidence). Key order and
        whitespace are not preserved; values are.
        """
        error: Any = None
        if isinstance(body, dict) and isinstance(index, int):
            # the gateway may wrap the settlement: {"ok": true, "data": {accepted, ...}}
            envelope = body
            if isinstance(body.get("data"), dict) and "accepted" in body["data"]:
                envelope = body["data"]
            rows = envelope.get("errors")
            if isinstance(rows, list):
                for item in rows:
                    if isinstance(item, dict) and item.get("index") == index:
                        error = item
                        break
        if body is None:
            text = ""
        elif isinstance(body, str):
            text = body
        else:
            try:
                text = json.dumps(body, ensure_ascii=True, sort_keys=True)
            except (TypeError, ValueError):
                text = repr(body)
        text, redacted = self._scrub(text)
        # The Hub's body is the one thing here we do not author. If it ever echoes a
        # request back — or anything else content-shaped — the settlement counters are
        # kept and the body is dropped. A diagnostic is not worth carrying user text.
        withheld = contains_forbidden_content(body) or _CONTENT_KEY_ECHO.search(text) is not None
        if withheld:
            keep = {k: _scalar_or_none(body.get(k)) for k in ("accepted", "rejected", "ok", "errorCode")
                    if isinstance(body, dict) and k in body}
            keep = {k: v for k, v in keep.items() if v is not None}
            text = json.dumps(keep, ensure_ascii=True, sort_keys=True) if keep else ""
        raw = text.encode("utf-8", errors="replace")  # lone surrogates must not raise here
        truncated = len(raw) > self.body_cap
        if truncated:
            raw = raw[: self.body_cap]
        out = {"status": status, "body": raw.decode("utf-8", errors="ignore"), "truncated": truncated}
        if redacted:
            out["redacted"] = True
        if withheld:
            out["body_withheld"] = "content_shaped"
        if error is not None and not contains_forbidden_content(error):
            projected = dict(error)
            if "reason" in projected:
                projected["reason"] = sanitize_reason(projected["reason"])
            safe, _ = self._scrub(json.dumps(projected, ensure_ascii=True, sort_keys=True))
            out["error"] = json.loads(safe) if safe.startswith("{") else None
        return out


# A gateway echoing our own request headers back in an error body is the one way the
# bearer token could reach this file. Matched loosely on purpose.
_CREDENTIAL_ECHO = re.compile(r"(?i)(?:bearer\s+|authorization\s*[:=]\s*)[A-Za-z0-9._~+/=-]{8,}")
# Same guard for a body that arrived as text rather than parsed JSON.
_CONTENT_KEY_ECHO = re.compile(
    r'(?i)["\']?\b(?:' + "|".join(sorted(FORBIDDEN_CONTENT_KEYS)) + r')["\']?\s*[:=]')


REASON_MAX_CHARS = 512
# A reason prefix becomes a counter KEY in the report, and the report is logged. A server
# reason of ``content:...`` would otherwise put a forbidden key into our own structure.
_REASON_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def reason_key(reason: Any) -> str:
    prefix = str(reason if reason is not None else "").split(":", 1)[0].strip()
    if not _REASON_KEY.match(prefix) or prefix.lower() in FORBIDDEN_CONTENT_KEYS:
        return "unclassified"
    return prefix


def sanitize_reason(reason: Any) -> str:
    """A rejection reason is the server's free text. Keep it verbatim — it is the whole
    point of the row — but scrub anything credential-shaped, cap the length, and refuse
    it outright if it carries conversation-shaped keys."""
    text = str(reason if reason is not None else "")
    text = _CREDENTIAL_ECHO.sub("«redacted»", text)
    if _CONTENT_KEY_ECHO.search(text):
        return "«withheld: content_shaped»"
    return text if len(text) <= REASON_MAX_CHARS else text[:REASON_MAX_CHARS] + "…«truncated»"


def _validated_locator(value: Any) -> Optional[dict[str, Any]]:
    """A locator we are willing to write down: exact shape, non-negative ints, real time.

    ``dd`` at an offset we cannot vouch for hands the reader a different record's line,
    which is worse than admitting the offset is unknown.
    """
    if not isinstance(value, dict):
        return None
    inode, offset, seen = value.get("inode"), value.get("offset"), value.get("observed_at")
    if isinstance(inode, bool) or isinstance(offset, bool):
        return None
    if not (isinstance(inode, int) and inode >= 0 and isinstance(offset, int) and offset >= 0):
        return None
    if not (isinstance(seen, str) and parse_ts_checked(seen) is not None):
        return None
    return {"inode": inode, "offset": offset, "observed_at": seen}


def project_payload(record: dict[str, Any]) -> dict[str, Any]:
    """The payload, rebuilt key by key from the upload contract — never dumped wholesale.

    A dump-then-drop projection leaks silently the day ``build_record`` grows a field;
    this one can only ever emit keys that are in ``LIFECYCLE_V9_KEYS``, which is the same
    closed set ``assert_upload_safe`` enforces on the wire.
    """
    return {k: record[k] for k in sorted(LIFECYCLE_V9_KEYS) if k in record}


REPORT_KEYS = ("at", "env", "dry_run", "audit", "read", "built", "upload", "retry")


def project_report(report: dict[str, Any]) -> dict[str, Any]:
    """The pass report, whitelisted the same way. Counters and our own strings only."""
    return {k: report[k] for k in REPORT_KEYS if k in report}


def contains_forbidden_content(value: Any, depth: int = 0, budget: Optional[list[int]] = None) -> bool:
    """Does this structure carry anything that would be conversation text or an identity?

    Fails **closed**: running out of depth or of node budget answers "yes, withhold".
    A guard that gives up quietly is how content-shaped data gets through, and the thing
    being inspected is the one structure here we do not author.
    """
    if budget is None:
        budget = [5000]
    if depth > 6 or budget[0] <= 0:
        return True
    budget[0] -= 1
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_CONTENT_KEYS:
                return True
            if contains_forbidden_content(item, depth + 1, budget):
                return True
        return False
    if isinstance(value, list):
        return any(contains_forbidden_content(item, depth + 1, budget) for item in value)
    if isinstance(value, str):
        # a content key can hide inside a string the server built by hand
        return _CONTENT_KEY_ECHO.search(value) is not None
    return False


def _scalar_or_none(value: Any) -> Any:
    """Only plain scalars survive into a withheld body's counters — and a string only if
    it is itself free of content-shaped markers."""
    if isinstance(value, str):
        return None if _CONTENT_KEY_ECHO.search(value) else value
    return value if isinstance(value, (int, float, bool)) else None


def _source_of(locator: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Where the audit row lives, flat, or an explicit statement that we do not know.

    ``dd bs=1 skip=<source_offset> count=<n>`` on the audit reaches the row. Records
    queued by an earlier version — or a sidecar that failed validation — have no usable
    offset. We do NOT go looking: recovering it would mean rehashing a 400 MB audit per
    run_id, which is the scan this field exists to avoid. It is flagged instead, so an
    absent locator is never mistaken for a broken one.
    """
    src = locator if isinstance(locator, dict) else None
    if not src:
        return {"source_offset": None, "source_inode": None, "source_available": False,
                "source_reason": "queued_before_locator", "observed_at": None}
    offset, inode = src.get("offset"), src.get("inode")
    if not isinstance(offset, int) or offset < 0 or not isinstance(inode, int):
        return {"source_offset": None, "source_inode": None, "source_available": False,
                "source_reason": str(src.get("reason") or "invalid_locator"),
                "observed_at": src.get("observed_at")}
    return {"source_offset": offset, "source_inode": inode, "source_available": True,
            "source_reason": None, "observed_at": src.get("observed_at")}


# ── incremental audit reading ─────────────────────────────────────────────


def locate_backfill_start(audit_path: Path, cutoff: datetime, *, chunk_bytes: int = MAX_READ_BYTES) -> int:
    """Byte offset (line-aligned) from which every row at or after ``cutoff`` is reachable.

    Walks backwards from the tail one chunk at a time, probing only the first complete
    line of each chunk. The audit is append-only and time-ordered, so the first chunk
    whose leading row predates the cutoff is a safe place to start reading forward;
    rows before the cutoff are then discarded by the caller's window filter. Cost is
    one seek+readline per chunk, never a full read.
    """
    size = audit_path.stat().st_size
    step = max(1, int(chunk_bytes))
    pos = size - step
    with audit_path.open("rb") as fh:
        while pos > 0:
            fh.seek(pos)
            partial = fh.readline()  # step over the line the seek landed inside
            aligned = pos + len(partial)
            probe = fh.readline()
            if not probe:
                pos -= step  # the seek landed inside the final line; look further back
                continue
            at = peek_timestamp(probe)
            if at is None or at < cutoff:
                # unreadable probe → be conservative and start here (earlier rows are
                # filtered by the cutoff anyway); dated probe before cutoff → start here
                return aligned
            pos -= step
    return 0


def read_new_audit_lines(audit_path: Path, cursor: dict[str, Any], *,
                         max_bytes: int = MAX_READ_BYTES,
                         backfill_start: Optional[datetime] = None) -> tuple[list[str], dict[str, Any], bool, bool]:
    """Read lines appended since ``cursor``; on rotation/truncation restart the backfill.

    At most ``max_bytes`` are consumed per pass: the production audit carries every
    conversation message, and one pass must not pull the whole file into memory.
    Whatever is left over is picked up by the next pass from the saved offset.

    When there is no usable cursor and ``backfill_start`` is given, reading begins at
    the line-aligned offset that still covers ``backfill_start`` (see
    ``locate_backfill_start``) rather than at byte 0 or at an arbitrary tail budget.

    Returns (lines, new_cursor, restarted, caught_up). ``caught_up`` is True when the
    pass consumed the file up to the size observed at its start.
    """
    st = audit_path.stat()
    inode = int(st.st_ino)
    offset = int(cursor.get("offset", 0)) if cursor.get("inode") == inode else 0
    restarted = cursor.get("inode") not in (None, inode) or offset > st.st_size
    if offset > st.st_size:
        offset = 0
    budget = max(1, int(max_bytes))
    fresh = (not cursor) or restarted
    if fresh and backfill_start is not None and st.st_size > budget:
        offset = locate_backfill_start(audit_path, backfill_start, chunk_bytes=budget)
    with audit_path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read(budget)
    end = offset + len(data)
    more_ahead = end < st.st_size
    # never consume a partial trailing line — leave it for the next run
    if data and not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        if cut < 0:
            if not more_ahead:
                return [], {"inode": inode, "offset": offset}, restarted, True
            # a single line longer than the read budget: step over it instead of
            # stalling the cursor on it forever
            logger.warning("kep-telemetry-export: audit line exceeds %d bytes; skipping ahead", len(data))
            return [], {"inode": inode, "offset": end}, restarted, False
        end = offset + cut + 1
        data = data[: cut + 1]
    # Split on the raw bytes so each line's offset is its true position in the file;
    # decoding first would drift the arithmetic on any byte the codec replaces.
    lines: list[AuditLine] = []
    pos = offset
    for raw in data.splitlines(keepends=True):
        lines.append(AuditLine(raw.decode("utf-8", errors="replace").rstrip("\r\n"), pos))
        pos += len(raw)
    return lines, {"inode": inode, "offset": end}, restarted, end >= st.st_size


# ── upload ────────────────────────────────────────────────────────────────


def jwt_expiry(token: str) -> Optional[datetime]:
    """``exp`` of a JWS compact token, or None when the token is not a readable JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeEncodeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None
    try:
        return datetime.fromtimestamp(float(exp), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def read_credentials(env: str, kep_auth_bin: str = "kep-auth", *, now: Optional[datetime] = None) -> tuple[str, str]:
    """``kep-auth --env <env> inject`` → (token, operator). Never logs the token.

    ``kep-auth check`` reports a token as valid from its local file alone; the gateway
    still answers 401 once the JWT ``exp`` has passed. The expiry is checked here so an
    expired service token stops the round *before* a batch is sent.
    """
    try:
        out = subprocess.run(
            [kep_auth_bin, "--env", env, "inject"],
            check=True, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuthUnavailable(f"kep-auth inject failed: {type(exc).__name__}") from exc
    parts = out.split("\n")
    token = parts[0].strip() if parts else ""
    operator = parts[1].strip() if len(parts) > 1 else ""
    if not token or not operator:
        raise AuthUnavailable("kep-auth inject returned an incomplete token/operator pair")
    exp = jwt_expiry(token)
    if exp is not None and exp <= (now or datetime.now(tz=timezone.utc)):
        raise AuthUnavailable(f"kep-auth token expired at {rfc3339(exp)}; run `kep-auth --env {env} login`")
    return token, operator


HttpPost = Callable[[str, dict[str, str], bytes], tuple[Optional[int], Any]]


def _urllib_post(url: str, headers: dict[str, str], body: bytes) -> tuple[Optional[int], Any]:
    req = urlrequest.Request(url, data=body, headers=headers, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310 — fixed https host
            status = resp.status
            raw = resp.read()
    except urlerror.HTTPError as exc:
        status = exc.code
        raw = exc.read() if hasattr(exc, "read") else b""
    except (urlerror.URLError, OSError, TimeoutError):
        return None, None
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        # Not JSON: hand the text back instead of None. ``settle_batch`` treats both the
        # same way (``invalid_response_shape``), but the diagnostic log can only show the
        # operator what the Hub actually said if we keep it. Not truncated here — the log
        # is the only place that knows the cap, and truncating twice reports it as whole.
        return status, raw.decode("utf-8", errors="replace")


def post_batch(url: str, token: str, operator: str, runs: list[dict[str, Any]], *, sent_at: str,
               batch_id: str, http_post: HttpPost = _urllib_post) -> tuple[Optional[int], Any]:
    if not token or not operator:
        raise ValueError("token and operator are required")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Operator": operator,
        "Content-Type": "application/json",
    }
    body = json.dumps({"batch_id": batch_id, "sent_at": sent_at, "runs": runs}, ensure_ascii=False).encode("utf-8")
    return http_post(url, headers, body)


# X-Operator is an HTTP header: latin-1, no control characters. Profile names are
# employee account names / feishu_group_<hex>; anything outside this set cannot be
# carried as an operator and rides under the service identity instead.
OPERATOR_CHARSET = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")


def header_safe_operator(profile: Any, fallback_operator: str) -> tuple[str, bool]:
    """(operator, used_fallback): the profile when it is a legal header value, else the fallback."""
    if isinstance(profile, str):
        candidate = profile.strip()
        if candidate and OPERATOR_CHARSET.match(candidate):
            return candidate, False
    return fallback_operator, True


def plan_batches(outbox: list[dict[str, Any]], profile_map: dict[str, str], fallback_operator: str, *,
                 batch_size: int = BATCH_SIZE) -> tuple[list[tuple[str, list[dict[str, Any]]]], int]:
    """Group queued records by actor and cut each group to ``batch_size``.

    Returns ``([(operator, batch), ...], fallback_count)`` ordered by operator name so a
    run is reproducible. The operator is the Hermes profile the record came from
    (``profile_map[run_id]``) when that is a legal header value; records with no
    usable profile ride under ``fallback_operator`` (the kep-auth service identity)
    and are counted, never dropped and never allowed to abort the pass.
    """
    size = max(1, int(batch_size))
    groups: dict[str, list[dict[str, Any]]] = {}
    fallbacks = 0
    for rec in outbox:
        operator, used_fallback = header_safe_operator(profile_map.get(rec.get("run_id", "")), fallback_operator)
        fallbacks += int(used_fallback)
        groups.setdefault(operator, []).append(rec)
    plan: list[tuple[str, list[dict[str, Any]]]] = []
    for operator in sorted(groups):
        recs = groups[operator]
        for start in range(0, len(recs), size):
            plan.append((operator, recs[start:start + size]))
    return plan, fallbacks

def settle_batch(batch: list[dict[str, Any]], status: Optional[int], body: Any) -> dict[str, Any]:
    """Mirror of kep-telemetry lib/outbox.mjs settleBatch."""

    def retry_all(reason: str, action: str = "retry") -> dict[str, Any]:
        return {"confirmed": [], "dead_letter": [], "retry": list(batch), "action": action, "reason": reason}

    if status is None:
        return retry_all("network_error")
    if status in (401, 403):
        return retry_all("auth_required", action="pause_auth")
    if status == 429:
        return retry_all("rate_limited", action="retry_later")
    if status >= 500:
        return retry_all(f"http_{status}", action="retry_later")
    if status == 400:
        return {"confirmed": [], "dead_letter": [(r, "invalid_batch") for r in batch], "retry": [],
                "action": "quarantine_batch", "reason": "invalid_batch"}
    if status != 200:
        return retry_all(f"unexpected_http_{status}")
    # The Keep API gateway in front of the Hub answers HTTP 200 with its own envelope
    # ``{"ok": false, "errorCode": 401, "data": "<message>"}`` for auth failures and
    # other gateway-level errors (observed 2026-09-20). Map it onto the same actions
    # the raw HTTP statuses take, and unwrap ``{"ok": true, "data": {...}}`` if the
    # settlement ever arrives wrapped.
    if isinstance(body, dict) and "ok" in body:
        if body.get("ok") is False:
            code = body.get("errorCode")
            if code in (401, 403):
                return retry_all("auth_required", action="pause_auth")
            return retry_all(f"gateway_error_{code if isinstance(code, int) else 'unknown'}", action="retry_later")
        if isinstance(body.get("data"), dict) and "accepted" in body["data"]:
            body = body["data"]
    accepted = body.get("accepted") if isinstance(body, dict) else None
    rejected = body.get("rejected") if isinstance(body, dict) else None
    errors = body.get("errors") if isinstance(body, dict) else None
    if (not isinstance(accepted, int) or accepted < 0 or not isinstance(rejected, int) or rejected < 0
            or not isinstance(errors, list) or accepted + rejected != len(batch) or rejected != len(errors)):
        return retry_all("invalid_response_shape")
    by_index: dict[int, dict[str, Any]] = {}
    for err in errors:
        idx = err.get("index") if isinstance(err, dict) else None
        reason = err.get("reason") if isinstance(err, dict) else None
        if not isinstance(idx, int) or idx < 0 or idx >= len(batch) or idx in by_index \
                or not isinstance(reason, str) or not reason:
            return retry_all("invalid_response_indices")
        server_run_id = err.get("run_id")
        if server_run_id not in (None, "") and server_run_id != batch[idx]["run_id"]:
            return retry_all("invalid_response_run_id")
        by_index[idx] = err
    confirmed, dead = [], []
    for i, rec in enumerate(batch):
        err = by_index.get(i)
        if err is None:
            confirmed.append(rec)
        else:
            dead.append((rec, str(err["reason"])))
    return {"confirmed": confirmed, "dead_letter": dead, "retry": [], "action": "settled", "reason": None}


# ── orchestration ─────────────────────────────────────────────────────────


def _bump(counter: dict[str, int], key: str, n: int = 1) -> None:
    counter[key] = counter.get(key, 0) + n


def _log_attempt(diag: Optional[DiagnosticsLog], record: dict[str, Any], *, outcome: str,
                 reason: Optional[str], klass: Optional[str] = None, tries: int = 1,
                 profile_map: dict[str, str], locator_map: dict[str, dict[str, Any]],
                 index_of: dict[str, int], first_at_of: Optional[dict[str, Any]] = None,
                 batch_id: str, operator: str, url: str,
                 sent_at: str, status: Optional[int], body: Any) -> None:
    """Record one settled upload locally. A no-op on a dry run (``diag is None``)."""
    if diag is None:
        return
    run_id = record.get("run_id", "")
    prior = (first_at_of or {}).get(run_id) or {}
    diag.upload_attempt(record, batch_id=batch_id, operator=operator, url=url, sent_at=sent_at,
                        status=status, body=body, outcome=outcome, reason=reason, klass=klass,
                        tries=tries, index=index_of.get(run_id),
                        profile=profile_map.get(run_id), locator=locator_map.get(run_id),
                        first_at=prior.get("first_at") if isinstance(prior, dict) else None)


def run_export(*, audit_path: Path = DEFAULT_AUDIT_PATH, state_dir: Optional[Path] = None, env: str = "online",
               dry_run: bool = False, now: Optional[datetime] = None, backfill_days: int = DEFAULT_BACKFILL_DAYS,
               batch_size: int = BATCH_SIZE, credentials: Optional[tuple[str, str]] = None,
               http_post: HttpPost = _urllib_post, kep_auth_bin: str = "kep-auth",
               hub_url: Optional[str] = None, max_read_bytes: int = MAX_READ_BYTES) -> dict[str, Any]:
    """One exporter pass. ``dry_run`` reads the audit and reports the would-be batch without
    touching state or the network.

    Durability order (a crash between any two steps must never lose or duplicate a fact):
    outbox + profile map first, then pending/terminals, then the cursor — the cursor is
    the last thing committed, so an interrupted pass re-reads and dedups instead of
    skipping. The whole pass runs under an exclusive lock on the state directory.
    """
    now = now or datetime.now(tz=_SHANGHAI)
    state = ExportState(state_dir or default_state_dir())
    url = hub_url or HUB_SKILL_RUNS_URL[env]
    report: dict[str, Any] = {
        "at": rfc3339(now), "env": env, "dry_run": dry_run, "audit": str(audit_path),
        "read": {"lines": 0, "skill_calls": 0, "terminals": 0, "restarted": False, "backfill_cutoff": None,
                 "caught_up": None, "high_water": None},
        "built": {"records": 0, "with_terminal": 0, "unresolved": 0, "still_pending": 0, "already_confirmed": 0,
                  "already_rejected": 0, "skipped_invalid_skill": 0, "skipped_error": 0},
        "upload": {"batches": 0, "sent": 0, "accepted": 0, "rejected_by_reason": {},
                   "rejected_by_class": {}, "retry": 0, "stopped": None, "queued": 0,
                   "operators": 0, "operator_fallback": 0},
        "retry": {"requeued": 0, "rescheduled": 0, "waiting": 0, "exhausted": 0, "permanent": 0,
                  "legacy": 0, "corrupt": 0, "by_class": {}},
        "diagnostics": {"path": None, "rows": 0, "dropped": 0, "errors": 0, "pruned": [], "stopped": None},
    }

    # A dry run only reads: it must not create the state dir, so it takes no lock and
    # never binds the env. A real run refuses to share a state dir across envs or with
    # a concurrent exporter.
    bound_env = state.load_env()
    if bound_env is not None and bound_env != env:
        report["upload"]["stopped"] = f"env_mismatch: state dir bound to env={bound_env}"
        return report
    if not dry_run:
        try:
            state.acquire_lock()
        except StateBusy as exc:
            report["upload"]["stopped"] = f"locked: {exc}"
            return report
    try:
        return _run_export_locked(state, report, audit_path=audit_path, env=env, url=url, dry_run=dry_run, now=now,
                                  backfill_days=backfill_days, batch_size=batch_size, credentials=credentials,
                                  http_post=http_post, kep_auth_bin=kep_auth_bin, max_read_bytes=max_read_bytes)
    finally:
        state.release_lock()


def _run_export_locked(state: ExportState, report: dict[str, Any], *, audit_path: Path, env: str, url: str,
                       dry_run: bool, now: datetime, backfill_days: int, batch_size: int,
                       credentials: Optional[tuple[str, str]], http_post: HttpPost, kep_auth_bin: str,
                       max_read_bytes: int) -> dict[str, Any]:
    cursor = state.load_cursor()
    fresh = not cursor.get("inode")
    cutoff: Optional[datetime] = None
    if fresh:
        cutoff = now - timedelta(days=max(0, backfill_days))
    elif cursor.get("backfill_cutoff"):
        cutoff = parse_ts(cursor["backfill_cutoff"])  # a backfill still in flight from earlier passes

    lines, new_cursor, restarted, caught_up = read_new_audit_lines(
        audit_path, cursor, max_bytes=max_read_bytes,
        backfill_start=(cutoff - TERMINAL_MATCH_WINDOW) if cutoff is not None else None,
    )
    if restarted and not fresh:
        # rotation/truncation: the backfill window restarts from now
        cutoff = now - timedelta(days=max(0, backfill_days))
        lines, new_cursor, restarted, caught_up = read_new_audit_lines(
            audit_path, {}, max_bytes=max_read_bytes, backfill_start=cutoff - TERMINAL_MATCH_WINDOW)
        restarted = True
    report["read"]["lines"] = len(lines)
    report["read"]["restarted"] = restarted
    report["read"]["caught_up"] = caught_up
    if cutoff is not None:
        report["read"]["backfill_cutoff"] = rfc3339(cutoff)

    # High-water mark: the latest audit timestamp seen so far. Unresolved calls are
    # finalised against it, not against the wall clock, so a call read in this chunk
    # cannot be shipped as "unresolved" while its terminal still sits in the next chunk.
    high_water: Optional[datetime] = parse_ts(cursor["high_water"]) if cursor.get("high_water") else None
    pending = state.load_pending()
    terminals = state.load_terminals()
    audit_inode = new_cursor.get("inode")
    for line in lines:
        try:
            fact = parse_audit_line(line, inode=audit_inode)
        except Exception:  # noqa: BLE001 — one corrupt row must never stall the cursor
            _bump(report["built"], "skipped_error")
            continue
        if fact is None:
            continue
        at = fact.at_dt if isinstance(fact, RunTerminal) else fact.observed_dt
        if high_water is None or at > high_water:
            high_water = at
        if isinstance(fact, RunTerminal):
            report["read"]["terminals"] += 1
            if cutoff is None or at >= cutoff - TERMINAL_MATCH_WINDOW:
                terminals.append(fact)
        else:
            report["read"]["skill_calls"] += 1
            if cutoff is None or at >= cutoff:
                pending.append(fact)
    if high_water is not None:
        report["read"]["high_water"] = rfc3339(high_water)

    # Emit calls whose terminal arrived, or whose window has provably closed: the audit
    # itself has moved past window end, or we are caught up with the file and the wall
    # clock has (quiet audit). Anything else waits for the next pass.
    # A rejection is a grade, not a grave. Records the Hub refused for a retryable or
    # repairable reason come back on a backoff, automatically, with no file to delete by
    # hand — that is the whole point: 500 records rejected on a contract problem must not
    # need a human to notice before they can be resent.
    ledger = state.load_ledger()
    dead = state.dead_letter_view()
    # The locator sidecar is diagnostic only: every read and write of it is isolated, so
    # a permission or disk problem on it can never stop a record from being uploaded.
    # Loaded before the retry requeue, which restores locators carried by dead-letter rows.
    locator_io_errors = 0
    try:
        locator_map = state.load_locator_map()
    except OSError:
        locator_map, locator_io_errors = {}, locator_io_errors + 1
    retry_rows = [(payload, row) for payload, row in dead.due(now) if payload.get("run_id") not in ledger]
    retry_meta = {payload["run_id"]: row for payload, row in retry_rows if isinstance(payload.get("run_id"), str)}
    settled = set(ledger) | dead.closed_ids(now, set(retry_meta))
    outbox = [r for r in state.load_outbox() if r["run_id"] not in settled]
    outbox_ids = {r["run_id"] for r in outbox}
    for payload, row in retry_rows:
        if payload["run_id"] not in outbox_ids:
            outbox.append(payload)
            outbox_ids.add(payload["run_id"])
        # the offset travelled with the rejection, so a resend stays traceable to its line
        restored = _validated_locator(row.get("locator"))
        if restored is not None:
            locator_map.setdefault(payload["run_id"], restored)
    report["retry"] = dict(dead.counts(now), requeued=len(retry_meta), rescheduled=0)
    profile_map = state.load_profile_map()
    # A dry run writes nothing at all, diagnostics included; retention runs on every
    # real pass, in this pass, so nothing else has to remember to do it.
    diag: Optional[DiagnosticsLog] = None
    if not dry_run:
        # Limits read at call time, not bound as defaults: the cap and the retention
        # window are the two knobs an operator (or a test) needs to be able to move.
        diag = DiagnosticsLog(state.diagnostics_dir, now, retention_days=DIAGNOSTICS_RETENTION_DAYS,
                              max_day_bytes=DIAGNOSTICS_MAX_DAY_BYTES, body_cap=DIAGNOSTICS_BODY_CAP,
)
        diag.prune()
    still_pending: list[SkillCall] = []
    seen_pending: set[tuple[str, str, str]] = set()
    for call in pending:
        key = (call.profile, call.session_id, call.message_id)
        if key in seen_pending:
            continue
        seen_pending.add(key)
        try:
            window_end = call.observed_dt + TERMINAL_MATCH_WINDOW
            terminal = match_terminal(call, terminals)
            if terminal is None:
                audit_closed = high_water is not None and high_water >= window_end
                clock_closed = caught_up and now >= window_end
                if not (audit_closed or clock_closed):
                    still_pending.append(call)
                    continue
            rid = run_id_for(call.profile, call.session_id, call.message_id)
            if rid in settled:
                _bump(report["built"], "already_confirmed" if rid in ledger else "already_rejected")
                continue
            if rid in outbox_ids:
                continue
            try:
                record = build_record(call, terminal)
            except InvalidSkillName:
                _bump(report["built"], "skipped_invalid_skill")
                if not dry_run:
                    state.append_dead_letter({"run_id": rid, "skill": call.skill},
                                             "local:invalid_skill_charset", rfc3339(now),
                                             klass=REJECT_PERMANENT, locator=call.locator)
                    settled.add(rid)
                    if diag is not None:
                        # full value, not the dead-letter's [:64] slice: validating a
                        # truncated string would pass a 70-char name as legal
                        diag.local_reject(run_id=rid, skill=call.skill, reason="local:invalid_skill_charset",
                                          klass=REJECT_PERMANENT, profile=call.profile, locator=call.locator)
                continue
        except Exception:  # noqa: BLE001 — quarantine the row, keep the pass alive
            _bump(report["built"], "skipped_error")
            logger.warning("kep-telemetry-export: dropping unprocessable call %s/%s", call.session_id, call.message_id)
            if not dry_run:
                state.append_dead_letter({"run_id": run_id_for(*key), "skill": call.skill},
                                         "local:processing_error", rfc3339(now),
                                         klass=REJECT_PERMANENT, locator=call.locator)
                if diag is not None:
                    diag.local_reject(run_id=run_id_for(*key), skill=call.skill,
                                      reason="local:processing_error", klass=REJECT_PERMANENT,
                                      profile=call.profile, locator=call.locator)
            continue
        outbox.append(record)
        outbox_ids.add(rid)
        profile_map[rid] = call.profile
        locator_map[rid] = call.locator
        _bump(report["built"], "records")
        _bump(report["built"], "with_terminal" if terminal is not None else "unresolved")
    report["built"]["still_pending"] = len(still_pending)
    report["upload"]["queued"] = len(outbox)

    # Keep every terminal a still-pending call could match, plus a retention tail
    # behind the high-water mark for calls not yet read.
    anchor = min([c.observed_dt for c in still_pending] + [(high_water or now) - TERMINAL_RETENTION])
    terminals = [t for t in terminals if t.at_dt >= anchor]

    if dry_run:
        report["upload"]["would_send"] = len(outbox)
        report["upload"]["sample"] = outbox[:3]
        return report

    def _finish(queued_ids: Optional[set[str]] = None) -> dict[str, Any]:
        """Close the pass: prune the locator sidecar, log the pass row, persist the report.

        Every exit below this point goes through here, so a round that sent nothing —
        auth stop, rate limit, empty queue — still leaves a durable trace. ``last-run.json``
        is overwritten each tick; the diagnostic day file is not.
        """
        errors = locator_io_errors
        if queued_ids is not None:
            kept = {rid: loc for rid, loc in locator_map.items() if rid in queued_ids}
            if kept != locator_map:
                try:
                    state.save_locator_map(kept)
                except OSError:
                    logger.warning("kep-telemetry-export: could not prune the locator sidecar")
                    errors += 1
        if diag is not None:
            # The report's own diagnostics block is stripped from the logged copy: a count
            # that cannot include the row carrying it would always read as stale.
            diag.pass_summary({k: v for k, v in report.items() if k != "diagnostics"})
            diag.close()  # close first — a failed close is a failed write, and it counts
            summary = diag.summary()
            summary["errors"] += errors
            report["diagnostics"] = summary
        state.save_last_run(report)
        return report

    # Durability order: facts first, progress last.
    state.bind_env(env)
    state.save_profile_map(profile_map)  # attribution first: an outbox row without its profile would upload as the service operator
    try:
        state.save_locator_map(locator_map)  # the locator must outlive the pass that queued the record
    except OSError:
        logger.warning("kep-telemetry-export: could not persist the locator sidecar")
        locator_io_errors += 1
    state.save_outbox(outbox)
    state.save_pending(still_pending)
    state.save_terminals(terminals)
    cursor_out = dict(new_cursor)
    if high_water is not None:
        cursor_out["high_water"] = rfc3339(high_water)
    if cutoff is not None and not caught_up:
        cursor_out["backfill_cutoff"] = rfc3339(cutoff)  # backfill continues next pass
    state.save_cursor(cursor_out)

    if outbox:
        try:
            token, operator = credentials or read_credentials(env, kep_auth_bin, now=now)
        except AuthUnavailable as exc:
            report["upload"]["stopped"] = f"auth_unavailable: {exc}"
            return _finish(outbox_ids)
        if diag is not None:
            diag.secret = token or None  # held only so an echoed header can be redacted
        remaining: list[dict[str, Any]] = []
        sent_at = rfc3339(now)
        stopped = False

        def tries_for(run_id: str) -> int:
            """Which attempt this is: one more than the rejection on file, else the first."""
            prior = retry_meta.get(run_id)
            return _attempt_of(prior) + 1 if prior else 1

        # One batch stream per actor: ``X-Operator`` carries the Hermes profile the
        # skill call belongs to (profile names are employee account names, the same
        # dimension the Mac collector fills from the kep-auth login). Records without
        # a profile mapping fall back to the service operator. sunke 2026-09-20:
        # 「打log 就打 profile 的即可」.
        if not OPERATOR_CHARSET.match(operator):
            report["upload"]["stopped"] = "auth_unavailable: service operator is not a legal header value"
            return _finish(outbox_ids)
        batches, fallbacks = plan_batches(outbox, profile_map, operator, batch_size=batch_size)
        report["upload"]["operators"] = len({op for op, _ in batches})
        report["upload"]["operator_fallback"] = fallbacks
        if fallbacks:
            logger.warning("kep-telemetry-export: %d record(s) carry the service operator (profile missing or not header-safe)", fallbacks)
        for i, (batch_operator, batch) in enumerate(batches):
            if stopped:
                remaining.extend(batch)
                continue
            batch_id = make_batch_id(now)
            status, body = post_batch(url, token, batch_operator, batch, sent_at=sent_at,
                                      batch_id=batch_id, http_post=http_post)
            settle = settle_batch(batch, status, body)
            report["upload"]["batches"] += 1
            report["upload"]["sent"] += len(batch)
            # The diagnostic row is written per record, carrying the batch id it went out
            # under: that id is the only key that lines a local row up against the Hub's
            # own server-side log, and it used to be generated at the call site and lost.
            attempt = {"batch_id": batch_id, "operator": batch_operator, "url": url,
                       "sent_at": sent_at, "status": status, "body": body,
                       "index_of": {r["run_id"]: i for i, r in enumerate(batch)},
                       "first_at_of": retry_meta}
            for rec in settle["confirmed"]:
                _log_attempt(diag, rec, outcome="confirmed", reason=None, tries=tries_for(rec["run_id"]),
                             profile_map=profile_map, locator_map=locator_map, **attempt)
                if rec["run_id"] in ledger:
                    continue
                ledger[rec["run_id"]] = {"revision": CLIENT_REVISION, "content_hash": content_hash(rec),
                                         "confirmed_at": sent_at, "operator": batch_operator}
                report["upload"]["accepted"] += 1
            for rec, reason in settle["dead_letter"]:
                prior = retry_meta.get(rec["run_id"])
                grade = classify_reject_reason(reason)
                tries = tries_for(rec["run_id"])
                state.append_dead_letter(rec, reason, sent_at, klass=grade, attempt=tries,
                                         first_at=(prior or {}).get("first_at") or sent_at,
                                         payload=rec, locator=locator_map.get(rec["run_id"]))
                _log_attempt(diag, rec, outcome="dead_letter", reason=reason, klass=grade,
                             tries=tries, profile_map=profile_map, locator_map=locator_map, **attempt)
                _bump(report["upload"]["rejected_by_reason"], reason_key(reason))
                _bump(report["upload"]["rejected_by_class"], grade)
            if settle["retry"]:
                remaining.extend(settle["retry"])
                report["upload"]["retry"] += len(settle["retry"])
                for rec in settle["retry"]:
                    _log_attempt(diag, rec, outcome="retry", reason=settle["reason"],
                                 klass=REJECT_RETRYABLE, tries=tries_for(rec["run_id"]),
                                 profile_map=profile_map, locator_map=locator_map, **attempt)
            if settle["action"] in ("pause_auth", "retry_later", "retry"):
                stopped = True
                report["upload"]["stopped"] = settle["reason"]
            # settle each batch durably before the next POST
            state.save_ledger(ledger)
            state.save_outbox(remaining + [r for _, b in batches[i + 1:] for r in b])
        state.save_ledger(ledger)
        # A record pulled back out of dead-letter is scheduled by the dead-letter file,
        # never by the outbox. If this pass did not settle it (batch-level retry, auth
        # stop, a later batch never sent), its attempt is written back and it leaves the
        # queue — otherwise the outbox would resend it every tick, past the backoff and
        # past the attempt ceiling, which is exactly the loop this feature exists to stop.
        unsettled_retries = [r for r in remaining if r["run_id"] in retry_meta]
        if unsettled_retries:
            stop_reason = report["upload"].get("stopped") or "unsettled"
            for rec in unsettled_retries:
                prior = retry_meta[rec["run_id"]]
                state.append_dead_letter(
                    rec, sanitize_reason(f"retry_unsettled:{stop_reason}"), sent_at,
                    klass=REJECT_RETRYABLE, attempt=tries_for(rec["run_id"]),
                    first_at=prior.get("first_at") or sent_at, payload=rec,
                    locator=locator_map.get(rec["run_id"]))
            report["retry"]["rescheduled"] = len(unsettled_retries)
            remaining = [r for r in remaining if r["run_id"] not in retry_meta]
        state.save_outbox(remaining)
        report["upload"]["queued"] = len(remaining)
        return _finish({r["run_id"] for r in remaining})

    return _finish(outbox_ids)


def _diagnostics_status(directory: Path) -> dict[str, Any]:
    """What the local diagnostic log currently holds: days kept, rows, bytes on disk."""
    try:
        files = sorted(Path(directory).glob("export-*.ndjson"))
    except OSError:
        return {"dir": str(directory), "days": 0, "bytes": 0, "files": [], "retention_days": DIAGNOSTICS_RETENTION_DAYS}
    total = 0
    names = []
    for entry in files:
        try:
            total += entry.stat().st_size
        except OSError:
            continue
        names.append(entry.name)
    return {"dir": str(directory), "days": len(names), "bytes": total, "files": names[-3:],
            "retention_days": DIAGNOSTICS_RETENTION_DAYS}


# ── hand-off export ───────────────────────────────────────────────────────

DIAGNOSE_SCHEMA: dict[str, str] = {
    "id": "<YYYYMMDD>-<seq> — stable, unique, sorts in append order; page with since=<at>&after_id=<id>",
    "seq": "per-day sequence behind the id; gaps are possible, order is not",
    "at": "RFC3339 +08:00 — when the exporter settled this record",
    "at_epoch_ms": "same instant in epoch milliseconds (kep-telemetry rows use this form)",
    "kind": "upload_attempt | local_reject | pass_summary | truncated | diagnose_header",
    "run_id": "sha256('hermes|profile|session_id|message_id')[:32] — the Hub's run id",
    "content_hash": "sha256 of the payload as sent",
    "skill": "skill name as uploaded (null + skill_withheld when it failed the charset)",
    "profile": "Hermes profile the skill call belongs to (employee account name)",
    "operator": "value sent in the X-Operator header",
    "batch_id": "id of the POST this record rode in — the key to the Hub's own server log",
    "url": "skill-runs endpoint the batch was posted to",
    "attempt": "which attempt this was (1 = first)",
    "first_at": "when this record was first rejected (dead-letter rows)",
    "reason": "the server's own rejection reason, verbatim",
    "class": "retryable | repairable | permanent | conflict (kep-telemetry grading)",
    "request": "the lifecycle payload exactly as POSTed",
    "response": "{status, body (<=2KiB, truncated flag), error: this record's errors[] entry}",
    "verdict": "{outcome: confirmed|dead_letter|retry, reason}",
    "source_offset": "byte offset of the originating audit line (dd bs=1 skip=<offset>)",
    "source_inode": "inode of the audit file that offset belongs to",
    "source_available": "false when the offset is unknown; source_reason says why",
    "observed_at": "audit timestamp of the originating line",
    "report": "pass_summary only: the exporter's own counters for that round",
}
DIAGNOSE_GUARANTEES = (
    "No conversation text: every row is built from an explicit key whitelist, the request "
    "is projected onto the upload contract, and a Hub body carrying content-shaped keys is "
    "withheld (body_withheld). source_offset locates the originating audit line without "
    "copying it. Credentials are scrubbed before write."
)
DIAGNOSE_NOTES = (
    "One JSON object per line (NDJSON). Every row is local evidence from the Hermes "
    "kep-telemetry exporter; no conversation content, no chat/open ids and no credentials "
    "are recorded. Read it with `jq -c .` — no Hermes code needed."
)
_SINCE_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_since(value: str) -> Optional[timedelta]:
    """``3d`` / ``12h`` / ``90m`` / ``all``. Returns None for "everything on disk"."""
    raw = str(value or "").strip().lower()
    if raw in ("all", "", "0"):
        return None
    if raw[-1] in _SINCE_UNITS and raw[:-1].isdigit():
        return timedelta(**{_SINCE_UNITS[raw[-1]]: int(raw[:-1])})
    raise ValueError(f"unreadable --since {value!r}: use 3d / 12h / 90m / all")


def default_diagnose_out(now: datetime) -> Path:
    return Path(f"/tmp/kep-telemetry-diag-{now.astimezone(_SHANGHAI):%Y%m%d}.ndjson")


def diagnose(*, state_dir: Optional[Path] = None, since: str = "3d", out: Optional[Path] = None,
             now: Optional[datetime] = None, env: Optional[str] = None,
             gzip_over: int = DIAGNOSE_GZIP_OVER_BYTES) -> dict[str, Any]:
    """Collect the local diagnostic rows into ONE self-describing file to hand over.

    Read-only by construction: it takes no lock, advances no cursor and touches neither
    ledger nor dead-letter. The point is that "next time something is wrong, send them a
    file" is one command — not a hunt through a state directory, and not a format the
    recipient has to learn: the first line declares every field.
    """
    now = now or datetime.now(tz=_SHANGHAI)
    state = ExportState(state_dir or default_state_dir())
    window = parse_since(since)
    start = (now - window) if window is not None else None
    target = Path(out) if out is not None else default_diagnose_out(now)

    # A read-only promise has to be enforced, not just documented: --out pointing at
    # ledger.json would otherwise destroy the settlement record on the way out.
    resolved_state = state.dir.resolve()
    probe = target if target.is_absolute() else Path.cwd() / target
    resolved_target = probe.resolve() if probe.exists() else probe.parent.resolve() / probe.name
    if resolved_target == resolved_state or resolved_state in resolved_target.parents:
        raise ValueError(f"--out must not write inside the exporter state dir ({state.dir})")

    files = sorted(state.diagnostics_dir.glob("export-*.ndjson"))
    rows: list[str] = []
    kept = 0
    unreadable = 0
    skipped_old = 0
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            unreadable += 1
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                unreadable += 1
                continue
            if not isinstance(row, dict):
                unreadable += 1
                continue
            at = parse_ts_checked(str(row.get("at", "")))
            if start is not None and (at is None or at < start):
                skipped_old += 1
                continue
            # Defence in depth: the writer already scrubs credentials, so this only ever
            # fires if a row predates that. Redact, never drop — evidence stays evidence.
            text, hit = _CREDENTIAL_ECHO.subn("«redacted»", json.dumps(row, ensure_ascii=True, sort_keys=True))
            rows.append(text)
            kept += 1

    header = {
        "kind": "diagnose_header",
        "at": rfc3339(now),
        "at_epoch_ms": int(now.timestamp() * 1000),
        "client": CLIENT,
        "source": "hermes-multitenancy kep-telemetry-export",
        "client_revision": CLIENT_REVISION,
        "upload_contract_revision": UPLOAD_CONTRACT_REVISION,
        "host": socket.gethostname(),
        "env": env or state.load_env(),
        "state_dir": str(state.dir),
        "since": since,
        "window_start": rfc3339(start) if start is not None else None,
        "rows": kept,
        "day_files": [p.name for p in files],
        "unreadable_lines": unreadable,
        "schema": DIAGNOSE_SCHEMA,
        "guarantees": DIAGNOSE_GUARANTEES,
        "cursor": {"page_by": ["at", "id"], "order": "id ascending",
                   "skip_kinds": ["diagnose_header"]},
        "notes": DIAGNOSE_NOTES,
    }
    payload = ("\n".join([json.dumps(header, ensure_ascii=False, sort_keys=True)] + rows) + "\n").encode("utf-8")
    gzipped = len(payload) > max(1, int(gzip_over))
    if gzipped and target.suffix != ".gz":
        target = target.with_name(target.name + ".gz")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write a fresh 0600 file beside the target and rename: never follow a symlink that
    # is already sitting at the destination.
    tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    if gzipped:
        with gzip.open(tmp, "wb") as fh:
            fh.write(payload)
    else:
        tmp.write_bytes(payload)
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    return {"out": str(target), "rows": kept, "bytes": target.stat().st_size, "gzipped": gzipped,
            "day_files": len(files), "unreadable_lines": unreadable, "skipped_outside_window": skipped_old,
            "since": since, "window_start": header["window_start"]}


def status_report(state_dir: Optional[Path] = None) -> dict[str, Any]:
    state = ExportState(state_dir or default_state_dir())
    ledger = state.load_ledger()
    dead = state.load_dead_letter()
    reasons: dict[str, int] = {}
    for row in dead:
        _bump(reasons, str(row.get("reason", "?")).split(":", 1)[0])
    return {
        "state_dir": str(state.dir),
        "env": state.load_env(),
        "diagnostics": _diagnostics_status(state.diagnostics_dir),
        "confirmed": len(ledger),
        "dead_letter": len(dead),
        "dead_letter_by_reason": reasons,
        "outbox": len(state.load_outbox()),
        "pending": len(state.load_pending()),
        "cursor": state.load_cursor(),
        "last_run": _read_json(state.last_run_path, None),
    }
