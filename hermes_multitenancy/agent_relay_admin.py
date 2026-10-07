"""The relay admin plane: shared shape for every read-only admin resource.

Why this module exists
----------------------
``GET /v1/admin/logs`` + ``/v1/admin/stats`` (shipped 2026-08-26 for the 云驿
side) settled a shape: a dedicated bearer that fails closed, a ``since``/``until``
millisecond window with a hard cap, a keyset cursor that is **not** the
timestamp, and one error body. The second resource on this plane (the
kep-telemetry diagnostic log) must not fork that shape, so it lives here rather
than in whichever handler needed it first.

The family law, in one place (``docs/admin-readonly-api-conventions.md`` is the
prose version):

* path        ``/v1/admin/<resource>``.
* auth        one bearer per *consumer*, each scoped to the resources that
              consumer may read. Missing header ⇒ 401. A token that is unknown,
              or known but not scoped to this resource ⇒ 403. No configured
              token for a resource ⇒ every request to it is 403.
* window      ``since`` + ``until`` in epoch milliseconds, span ≤ 7 days, both
              representable as real dates.
* cursor      ``since`` + ``after_id``; the continuation is
              ``since=<last row's ts>&after_id=<last row's id>``.
* errors      ``{"error": {"code", "message"}}``.
* retention   declared and enforced by whoever *produces* the data; a read-only
              resource reports it and never prunes.

Two consumers, two tokens, one plane. 云驿 reading relay logs and the platform
side reading telemetry diagnostics are different teams: sharing one credential
would mean either team's leak hands over both datasets, and revoking one would
cut off the other. The scope map is the smallest thing that keeps them apart
while keeping the plane single.

Imports: stdlib only, plus ``aiohttp`` inside functions. The relay runtime is a
flat package assembled by the release scanner from ``agent_relay*.py`` and its
level-1 imports, so this module must not reach into the wider ``hermes_multitenancy``
package.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from typing import Any, Callable, Optional

#: Window ceiling shared by every resource on the plane.
ADMIN_MAX_WINDOW_MS = 7 * 86_400_000
#: Page ceiling shared by every resource on the plane. ``agent_relay`` re-exports
#: it as ``ADMIN_LOG_LIMIT`` for its own call sites and tests.
ADMIN_PAGE_LIMIT = 5000
ADMIN_LOG_LIMIT_DEFAULT = ADMIN_PAGE_LIMIT
#: Bytes one request may read off disk before it stops and says ``truncated``.
#: The relay is a single asyncio loop that also carries the live Feishu card
#: path; an unbounded scan here is an outage there, not a slow query.
ADMIN_SCAN_BYTE_BUDGET = 8 * 1024 * 1024
#: Timestamps must be real dates, not merely int64: a reader that turns these
#: into a calendar day would raise, and an OverflowError deep in a scan is a 500
#: for what is plainly a bad request. 1970-01-01 .. 2100-01-01.
ADMIN_MIN_MS = 0
ADMIN_MAX_MS = 4_102_444_800_000

_BEARER = "Bearer "

#: env var → the resources that token may read. The relay's original admin token
#: keeps every resource, so the 云驿 endpoints behave exactly as they did before
#: this module existed; new consumers get their own env and their own subset.
ADMIN_TOKEN_SCOPES: dict[str, frozenset[str]] = {
    "HERMES_AGENT_RELAY_ADMIN_TOKEN": frozenset({"logs", "stats"}),
    "HERMES_TELEMETRY_DIAG_TOKEN": frozenset({"telemetry-diagnostics", "telemetry-stats"}),
}


def admin_error(code: str, message: str, status: int, retry_after: Optional[int] = None):
    """``{"error": {"code", "message"}}`` — the plane's one error body."""
    from aiohttp import web

    error: dict[str, Any] = {"code": code, "message": message}
    headers = None
    if retry_after is not None:
        error["retry_after"] = retry_after
        headers = {"Retry-After": str(retry_after)}
    return web.json_response({"error": error}, status=status, headers=headers)


def bearer_token(request: Any) -> Optional[str]:
    """The presented token, canonicalised — ``None`` when there is no Bearer header.

    Every consumer of the token (the comparison, the rate-limit key) goes through
    this one function. When they disagree, the difference *is* the bypass:
    ``Bearer  tok`` and ``Bearer tok`` authenticate as the same caller but would
    hash to two different rate-limit buckets.
    """
    header = str(request.headers.get("Authorization", "") or "")
    if not header.startswith(_BEARER):
        return None
    return header[len(_BEARER):].strip()


def _scopes_for(offered: str) -> frozenset[str]:
    """Every resource the presented token is allowed to read.

    Compares against all configured tokens rather than stopping at the first
    match: a deployment that (wrongly) gives two consumers the same string should
    grant the union, not whichever env happened to be listed first.
    ``compare_digest`` on UTF-8 bytes because it raises ``TypeError`` on a
    non-ASCII ``str`` — a token with one non-ASCII character must be a 403, not
    a 500.
    """
    granted: set[str] = set()
    probe = offered.encode("utf-8", "surrogatepass")
    for env, resources in ADMIN_TOKEN_SCOPES.items():
        expected = os.environ.get(env, "").strip()
        if expected and hmac.compare_digest(probe, expected.encode("utf-8")):
            granted |= set(resources)
    return frozenset(granted)


def admin_denied(request: Any, resource: str):
    """Fail closed: no header → 401; unknown, unset or out-of-scope token → 403.

    This is the whole authorisation story for the plane. It deliberately does not
    consult any other credential: the relay's own actor tokens are not admin
    tokens, and an admin token for one resource is not an admin token for another.
    """
    offered = bearer_token(request)
    if offered is None:
        return admin_error("unauthorized", "admin token required", 401)
    if resource not in _scopes_for(offered):
        return admin_error("forbidden", "admin access is not configured for this token", 403)
    return None


def _valid_ms(value: int) -> bool:
    return ADMIN_MIN_MS <= value <= ADMIN_MAX_MS


def admin_window(request: Any, resource: str, *, allow_empty_window: bool = False):
    """Auth then range; returns ``(window, error)`` with exactly one of them set.

    ``allow_empty_window`` lets a resource accept ``since == until``. A resource
    whose ``since`` is an *inclusive* bound needs it: a page boundary landing on
    the window's last instant continues with exactly that pair, and rejecting it
    strands the tail. ``logs``/``stats`` keep the original exclusive behaviour,
    so nothing moves for the consumer they were built for.
    """
    denied = admin_denied(request, resource)
    if denied is not None:
        return None, denied
    try:
        since = int(request.query["since"])
        until = int(request.query["until"])
    except (KeyError, ValueError):
        return None, admin_error("invalid_range", "since and until must be integer milliseconds", 400)
    if not (_valid_ms(since) and _valid_ms(until)):
        return None, admin_error(
            "invalid_range", "since and until must be milliseconds between 1970 and 2100", 400
        )
    too_narrow = until < since if allow_empty_window else until <= since
    if too_narrow or until - since > ADMIN_MAX_WINDOW_MS:
        return None, admin_error("invalid_range", "until must be after since and within 7 days", 400)
    return (since, until), None


def admin_after_id(request: Any):
    """The second half of the cursor, integer form; returns ``(after_id, error)``.

    A window plus a timestamp is not a cursor: rows that share one millisecond
    make a timestamp-only continuation loop in place. That was this plane's
    round-one review finding and it is why both halves are required.
    """
    try:
        after_id = int(request.query.get("after_id", "0"))
        if not 0 <= after_id < 2**63:
            raise ValueError
    except ValueError:
        return None, admin_error("invalid_range", "after_id must be a nonnegative integer", 400)
    return after_id, None


def admin_after_key(request: Any, pattern: Any, *, expected: str):
    """The same cursor half when the resource's id is an ordered **string**.

    A resource whose producer already stamps a stable, append-ordered id pages on
    that id rather than inventing a second numbering; the parameter name and the
    continuation stay identical either way, which is the part callers learn.
    ``0`` means "from the start", the integer plane's default, so a client built
    against ``/v1/admin/logs`` is not answered with a 400.
    """
    raw = str(request.query.get("after_id", "") or "").strip()
    if raw == "0":
        return "", None
    if raw and not pattern.match(raw):
        return None, admin_error("invalid_range", f"after_id must be {expected}", 400)
    return raw, None


def admin_seek_hint(request: Any, name: str = "after_offset"):
    """An optional, non-authoritative resume hint; returns ``(offset, error)``.

    It only ever saves work: the row predicate still decides what is returned and
    a hint that cannot be trusted makes the reader locate its place by id instead.
    """
    try:
        offset = int(request.query.get(name, "0"))
        if not 0 <= offset < 2**63:
            raise ValueError
    except ValueError:
        return None, admin_error("invalid_range", f"{name} must be a nonnegative integer", 400)
    return offset, None


class RateLimiter:
    """Per-token sliding-window limiter: N per second and M per hour.

    Read-only does not mean free. Each request can read megabytes off a disk that
    this host had a write-throttling incident on, and the relay's event loop is
    the same one answering Feishu callbacks. The limiter keys on a hash of the
    canonicalised token, never the raw header and never the token itself.
    """

    def __init__(self, *, per_second: int = 1, per_hour: int = 120,
                 clock: Callable[[], float] = time.monotonic):
        self.per_second = max(1, int(per_second))
        self.per_hour = max(1, int(per_hour))
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str) -> Optional[int]:
        """``None`` when allowed, else the ``Retry-After`` seconds to report."""
        now = self._clock()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < 3600.0]
            if len([t for t in hits if now - t < 1.0]) >= self.per_second:
                self._hits[key] = hits
                return 1
            if len(hits) >= self.per_hour:
                self._hits[key] = hits
                return max(1, int(3600.0 - (now - hits[0])) + 1)
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 256:  # bounded: stale keys cannot accumulate
                for stale in [k for k, v in self._hits.items() if not v or now - v[-1] > 3600.0]:
                    self._hits.pop(stale, None)
            return None


def token_key(request: Any) -> str:
    """A stable, non-reversible rate-limit key for one caller.

    Derived from the same canonicalised token ``admin_denied`` compares, so
    padding the header cannot mint a fresh quota bucket.
    """
    offered = bearer_token(request) or ""
    return hashlib.sha256(offered.encode("utf-8", "surrogatepass")).hexdigest()[:16]
