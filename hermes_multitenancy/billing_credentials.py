"""AI Gateway contract client and encrypted per-payer LiteLLM credentials."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from dataclasses import dataclass
import re
import sqlite3
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .billing_employee_key import (
    _MIN_LIFETIME_MS,
    _NO_REDIRECT_OPENER,
    AccountDriftError,
    needs_new_key,
    EmployeeKeyError,
    IssuedKey,
    check_account_drift,
    next_credential_version,
    to_vault_payload,
)
from .credentials import CredentialStore
from .run_broker import RunRejected


_EMPLOYEE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# A syntactically valid hostname (or bare IPv4). IPv6 literals never get here:
# urlparse() itself raises on a malformed one, and a well-formed one is
# returned by .hostname already stripped of its brackets, so it would fail
# this test — the broker is always a DNS name in practice, and a literal
# address in the EnvironmentFile is a misconfiguration we want to catch.
_HOSTNAME_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_OPAQUE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}")
_CONTRACT_MAJOR = "1"
_CONTRACT_VERSION = "1.0"
_MAX_BROKER_RESPONSE_BYTES = 1024 * 1024
_PROVIDER = "litellm"
_SECRET_KIND = "hermes_api_key"
_DAY_MS = 24 * 60 * 60 * 1000
_RENEW_WINDOW_MS = 30 * _DAY_MS
_RENEW_JITTER_MS = 7 * _DAY_MS
_ACK_RETRY_BACKOFF_MS = 5 * 60 * 1000
_RENEW_RETRY_BACKOFF_MS = 6 * 60 * 60 * 1000

@dataclass(frozen=True)
class BillingIdentity:
    employee_user_id: str
    profile_name: str = ""
    email: str = ""
    litellm_user_id: str = ""
    team_id: str = ""
    team_alias: str = ""
    key_id: str = ""
    credential_version: int = 0
    expires_at: int = 0
    migration_state: str = "legacy"


@dataclass(frozen=True)
class _ResolvedPayer:
    employee_user_id: str
    profile_name: str
    email: str
    department_alias: str = ""


class BillingUnavailable(RunRejected):
    """We could not OBTAIN a usable credential for this payer.

    Deliberately distinct from a plain :class:`RunRejected`, which means the
    data we already hold is inconsistent (drift, mismatch, conflict). The
    caller degrades on this one — billing is bookkeeping, and failing to bill
    must not cost the employee their service — but never on the other, because
    falling back on an inconsistency would hide a real defect inside normal
    traffic.
    """


class _GatewayError(RuntimeError):
    def __init__(self, status: int, code: str, retryable: bool = False) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.retryable = retryable


class BillingGatewayClient:
    """Minimal V1 AI Gateway client.  It never calls LiteLLM directly."""

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        *,
        timeout: float = 5.0,
        # Same shared no-follow opener EmployeeKeyClient._post and
        # BillingCredentialManager._probe_key use (defined once in
        # billing_employee_key.py). This request carries the broker bearer
        # that mints/renews any employee's credential via legacy ensure/ack
        # — the exact same class of leak ecc9b16 fixed on the employee-key
        # transport, just on this one. A redirect here must fail, not follow.
        opener: Callable[..., Any] = _NO_REDIRECT_OPENER.open,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.token = str(token or "").strip()
        self.timeout = max(0.1, min(float(timeout), 30.0))
        self._opener = opener

    def ensure(
        self,
        *,
        employee_id: str,
        enterprise_email: str,
        department_alias: str,
        reason: str,
        current_key_id: str = "",
        current_credential_version: int = 0,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "employee_id": employee_id,
            "enterprise_email": enterprise_email,
            "reason": reason,
        }
        if department_alias and not current_key_id:
            body["department_alias"] = department_alias
        if bool(current_key_id) != bool(current_credential_version):
            raise RunRejected("billing credential state is inconsistent")
        if current_key_id:
            body.update({
                "current_key_id": current_key_id,
                "current_credential_version": int(current_credential_version),
            })
        return self._post(
            "/internal/v1/hermes/credentials/ensure",
            body,
            idempotency_key=_idempotency_key("ensure", body),
        )

    def ack(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = str(payload.get("api_key") or "")
        body = {
            "employee_id": str(payload.get("employee_id") or ""),
            "key_id": str(payload.get("key_id") or ""),
            "credential_version": int(payload.get("credential_version") or 0),
            "key_sha256": hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
        }
        return self._post(
            "/internal/v1/hermes/credentials/ack",
            body,
            idempotency_key=_idempotency_key("ack", body),
        )

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        try:
            parsed = urlparse(self.base_url)
            # ``.port`` forces port-syntax validation (out-of-range or
            # non-numeric) that urlparse() alone skips; a malformed IPv6
            # authority raises inside urlparse() itself, caught by the same
            # except. codex r3 #1: previously a malformed authority reached
            # the real opener() and came back as either a degradable
            # broker_unavailable (URLError) or an uncaught
            # http.client.InvalidURL — neither is right, this is OUR config
            # being syntactically broken, never sent over the wire.
            _ = parsed.port
            # codex 终审 #1: `.port` only validates the PORT. A host carrying a
            # space, backslash or control char still parsed fine, reached the
            # opener, and came back as a degradable broker_unavailable — our
            # own broken config wearing an outage's clothes. A hostname is
            # letters/digits/dot/hyphen (and brackets/colons for IPv6, already
            # covered by urlparse raising above); anything else is a typo in
            # the EnvironmentFile, never something to put on the wire.
            host = parsed.hostname or ""
            # Structurally dangerous characters are rejected outright, ahead
            # of the IDN fallback below — `idna` happily encodes a backslash,
            # and some URL parsers treat `\` as `/`, so letting it through
            # would turn a typo into a different destination.
            if any(ch in host for ch in "\\<>\"{}|^`"):
                raise ValueError("broker hostname contains unsafe characters")
            if host and not _HOSTNAME_RE.fullmatch(host):
                # ASCII didn't match — before calling it malformed, give a
                # Unicode IDN its chance (codex 终审: the ASCII-only regex
                # over-rejected internationalized domains, i.e. refused a
                # LEGITIMATE broker as misconfiguration). If it encodes to
                # punycode cleanly it is a real hostname; if not, it isn't.
                try:
                    host.encode("idna")
                except (UnicodeError, UnicodeDecodeError):
                    raise ValueError("malformed broker hostname") from None
            # And validate the RAW string too, not just the parsed view:
            # urlparse() follows WHATWG and silently STRIPS tab/CR/LF, so a
            # base_url with an embedded tab yields a perfectly clean
            # .hostname — while the request below is built from
            # ``self.base_url`` verbatim, tab and all, and blows up out at the
            # opener as a "broker unavailable" outage. Checking the parsed
            # view alone cannot see this class at all.
            if any(ch.isspace() or ord(ch) < 0x20 for ch in self.base_url):
                raise ValueError("broker url contains whitespace or control characters")
        except ValueError:
            raise _GatewayError(503, "broker_not_configured", True)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not self.token
        ):
            raise _GatewayError(503, "broker_not_configured", True)
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_BROKER_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raw = exc.read(_MAX_BROKER_RESPONSE_BYTES + 1)
            payload = _decode_json(raw)
            _require_contract(payload)
            error = payload.get("error")
            if not isinstance(error, dict):
                raise _GatewayError(int(exc.code), "invalid_error_envelope") from exc
            code = str(error.get("code") or "").strip()
            message = str(error.get("message") or "").strip()
            retryable = error.get("retryable")
            if not code or not message or not isinstance(retryable, bool):
                raise _GatewayError(int(exc.code), "invalid_error_envelope") from exc
            raise _GatewayError(int(exc.code), code, retryable) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise _GatewayError(503, "broker_unavailable", True) from exc
        if len(raw) > _MAX_BROKER_RESPONSE_BYTES:
            raise _GatewayError(502, "invalid_response")
        payload = _decode_json(raw)
        _require_contract(payload)
        return payload


# Whitelist, not blacklist (codex r2 p1-2): only the two SQLite result codes
# that mean "busy right now, try later" degrade. Everything else that raises
# sqlite3.OperationalError — corrupted schema ("no such table"), a
# read-only mount, an unopenable file — is broken or misconfigured, not
# transient, even though they share the exception CLASS. Checked via
# ``sqlite_errorcode``, never the message text (Python 3.11+).
#
# SQLITE_FULL (codex 终审 #2): a genuinely full disk surfaces from SQLite as
# result code 13, NOT as OSError(ENOSPC) — the errno set below never sees it.
# Without this entry the "disk filled up" case refused, which is precisely the
# outcome the ENOSPC entry was added to prevent ("the billing disk is full,
# therefore employees cannot use AI"). The rule is unchanged — data might be
# wrong -> refuse; cannot write right now -> degrade — this just makes the
# implementation actually reach it.
_SQLITE_TRANSIENT_ERRORCODES = frozenset(
    {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_FULL}
)

# Same idea for bare OSError (e.g. a non-sqlite3 I/O layer): only errno
# values that mean "resource busy right now" degrade. ENOENT (path doesn't
# exist), EISDIR, EROFS (read-only), EACCES/EPERM (permission) are
# configuration/deployment defects, not outages — PermissionError's errno
# is never in this set, so it is correctly excluded without a separate
# isinstance check.
_OSERROR_TRANSIENT_ERRNOS = frozenset({errno.EAGAIN, errno.EBUSY, errno.ENOSPC})


def _is_vault_unavailable(exc: Exception) -> bool:
    """True only for a genuine transient failure to reach/use the vault
    store itself — never for the vault being reachable but broken,
    misconfigured, or denying access."""
    if isinstance(exc, sqlite3.OperationalError):
        return getattr(exc, "sqlite_errorcode", None) in _SQLITE_TRANSIENT_ERRORCODES
    if isinstance(exc, OSError):
        return exc.errno in _OSERROR_TRANSIENT_ERRNOS
    return False


class BillingCredentialManager:
    """Per-payer single-flight around the encrypted vault and broker V1."""

    def __init__(
        self,
        *,
        vault: CredentialStore,
        gateway: BillingGatewayClient,
        model_base_url: str,
        now_ms: Callable[[], int] | None = None,
        probe: Callable[[str], None] | None = None,
    ) -> None:
        self._vault = vault
        self._gateway = gateway
        self._model_base_url = str(model_base_url or "").strip().rstrip("/")
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._probe = probe or self._probe_key
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}
        # CredentialStore owns one check_same_thread=False SQLite connection.
        # Per-payer locks do not protect two different employees running in
        # parallel, so serialize the short vault transactions on that shared
        # connection while keeping Gateway/model I/O concurrent by payer.
        self._vault_lock = threading.RLock()

    def ensure_available(
        self,
        payer: _ResolvedPayer,
        existing: BillingIdentity | None,
        *,
        force_reason: str = "",
        allow_mint: bool = True,
    ) -> BillingIdentity:
        """Resolve this payer's live credential.

        ``allow_mint=False`` makes this method READ-ONLY with respect to the AI
        Gateway: it will serve a stored credential that is still usable, and
        raise :class:`BillingUnavailable` rather than ask the gateway to issue
        or rotate one. The employee request path passes False — minting on that
        path turns "the gateway had a bad minute" into "this person cannot use
        AI", and it contradicts the timer-only minting promise the refresh sweep
        exists to keep. The sweep itself (and shadow/replay callers) keep the
        default, so their behaviour is unchanged.
        """
        with self._payer_lock(payer.employee_user_id):
            return self._ensure_locked(
                payer,
                existing,
                force_reason=force_reason,
                gone_reissues_remaining=1,
                allow_mint=allow_mint,
            )

    def adopt_employee_key(
        self, payer: _ResolvedPayer, issued: IssuedKey
    ) -> BillingIdentity:
        """Store a credential minted by the gateway's employee-key endpoint.

        The counterpart to :meth:`ensure_available` for the auto-provisioning
        path: that one speaks the ensure/ack protocol, this one takes a key the
        gateway already minted and makes it the live credential for this payer.

        Fails closed. A drift or shape problem raises BEFORE anything is
        written, so a rejected issuance never half-lands — the previous
        credential stays exactly as it was.
        """
        if issued.employee_id != payer.employee_user_id:
            raise RunRejected("billing credential is for a different employee")
        if issued.email.casefold() != payer.email.casefold():
            raise RunRejected("billing credential is for a different email")
        # NOTE on the two checks above: both sides come from the request we
        # made, so they catch a caller wiring the wrong payer — they do NOT
        # authenticate the gateway's answer. The only field that does is
        # `account_identity_verified`, which is why an unverified credential is
        # refused outright rather than stored: a binding we cannot attribute
        # would bill somebody. Under-counting (no binding, run unattributed) is
        # a finance gap; mis-counting is charging the wrong person.
        if not issued.account_identity_verified:
            # Retyped on rebase, exactly as the key-client slug's handoff note
            # asked (codex 终审 #3). Two separate decisions live here and they
            # do NOT have to match: we still refuse to STORE an unattributable
            # binding (mis-counting is charging the wrong person), but the RUN
            # degrades — the employee gets served on the shared key, unbilled,
            # with an audit line. Refusing the run would mean "the gateway had
            # a verification hiccup, therefore this person cannot use AI",
            # which is the gate this slug exists to remove. Nothing suspect is
            # persisted either way.
            raise BillingUnavailable(
                "gateway could not verify the credential's account identity"
            )
        # Defense in depth: EmployeeKeyClient._validate already enforces this
        # floor on the HTTP response, but that check lives on the client,
        # not on this method — the actual vault-write boundary. Anything
        # that builds an IssuedKey directly and calls adopt_employee_key
        # (a future caller, a test helper mistake, a bug in a client
        # subclass) would otherwise land a below-floor key — inside its own
        # refresh window the instant it is stored — with no defense left.
        # Same constant as the client: one floor, checked at both the
        # network boundary and the storage boundary.
        if issued.expires_at_ms - self._now_ms() < _MIN_LIFETIME_MS:
            raise RunRejected("billing credential lifetime is implausible")
        with self._payer_lock(payer.employee_user_id):
            stored = self._load_payload(payer.profile_name, payer.employee_user_id)
            try:
                check_account_drift(issued, stored)
                payload = to_vault_payload(
                    issued,
                    profile_name=payer.profile_name,
                    credential_version=next_credential_version(stored),
                )
            except AccountDriftError as exc:
                raise RunRejected(str(exc)) from exc
            except EmployeeKeyError as exc:
                raise RunRejected(f"billing credential is unusable: {exc}") from exc
            # A key that is syntactically fine (right shape, right subject,
            # plausible lifetime) can still be unusable: wrong LiteLLM
            # cluster, revoked the instant it was minted, whatever the
            # gateway got wrong on its side. Nothing upstream of this line
            # calls LiteLLM, so an unprobed key would overwrite the sole
            # valid credential on faith alone. Probe it against the real
            # endpoint before it is allowed to become the live row; a probe
            # failure must leave the previous credential exactly as it was.
            try:
                self._probe(issued.api_key)
            except Exception as exc:
                raise RunRejected(
                    f"billing credential failed activation probe: {exc}"
                ) from exc
            self._save_payload(payer.profile_name, payer.employee_user_id, payload)
            return _binding_from_payload(payload)

    def credential_source(self, payer: _ResolvedPayer) -> str:
        """Which protocol currently owns this payer's stored credential.

        Empty string for "nothing stored" or a pre-migration row that predates
        the ``source`` marker. Read-only, used to route 401 repair to the
        right protocol instead of always falling back to legacy ensure/ack.
        """
        with self._payer_lock(payer.employee_user_id):
            stored = self._load_payload(payer.profile_name, payer.employee_user_id)
        return str((stored or {}).get("source") or "")

    def employee_key_needed(self, payer: _ResolvedPayer) -> bool:
        """Read-only: does this payer need a key minted right now?

        The caller mints only when this says so, then hands the result to
        :meth:`adopt_employee_key`. Keeping the decision separate from the
        minting is what lets a failed refresh be harmless — nothing is written,
        so the still-valid stored credential keeps serving.
        """
        with self._payer_lock(payer.employee_user_id):
            stored = self._load_payload(payer.profile_name, payer.employee_user_id)
        return needs_new_key(stored, self._now_ms())

    def runtime_api_key(self, metadata: dict[str, Any]) -> str:
        employee_id = str(metadata.get("litellm_billing_employee_user_id") or "")
        profile_name = str(metadata.get("litellm_billing_profile_name") or "")
        if not _EMPLOYEE_ID_RE.fullmatch(employee_id) or not profile_name:
            raise RunRejected("billing payer metadata is invalid")
        with self._payer_lock(employee_id):
            payload = self._load_payload(profile_name, employee_id)
            if payload is None:
                raise RunRejected("billing credential is unavailable")
            self._validate_local_payload(payload, metadata=metadata)
            if payload.get("invalid") or int(payload["expires_at"]) <= self._now_ms():
                raise RunRejected("billing credential is unavailable")
            return str(payload["api_key"])

    def mark_invalid(self, metadata: dict[str, Any]) -> None:
        employee_id = str(metadata.get("litellm_billing_employee_user_id") or "")
        profile_name = str(metadata.get("litellm_billing_profile_name") or "")
        if not employee_id or not profile_name:
            return
        with self._payer_lock(employee_id):
            payload = self._load_payload(profile_name, employee_id)
            if payload is None:
                return
            self._validate_local_payload(payload, metadata=metadata)
            payload["invalid"] = True
            self._save_payload(profile_name, employee_id, payload)

    def _ensure_locked(
        self,
        payer: _ResolvedPayer,
        existing: BillingIdentity | None,
        *,
        force_reason: str,
        gone_reissues_remaining: int = 1,
        allow_mint: bool = True,
    ) -> BillingIdentity:
        payload = self._load_payload(payer.profile_name, payer.employee_user_id)
        if not allow_mint:
            # THE gate, and it sits here rather than at the `gateway.ensure`
            # call because `_finish_pending` below reaches the gateway too:
            # it ACKs a pending generation, which is a WRITE on the employee's
            # own request (codex #p1, 2026-08-07). "The employee triggers
            # nothing" has to mean the whole gateway, not just issuance.
            #
            # So: serve a usable stored credential — a pending-but-real key
            # included, exactly as the existing ack-backoff windows already do —
            # and degrade otherwise. Completing the pending lifecycle, rotating,
            # and issuing are all maintenance-sweep work.
            if payload is not None:
                self._validate_local_payload(payload, payer=payer, existing=existing)
                # `probe_pending` is a crash-recovery row: it was written
                # before the activation probe confirmed the key works, so it has
                # never been proven usable. Completing that probe is maintenance
                # work (it can delete the row or fall back), so on the request
                # path such a row counts as NOT provisioned and degrades —
                # serving it would hand the runtime a key that may be dead
                # (codex #4 notes, 2026-08-07). `ack_pending` is different: that
                # key DID pass its probe, only the handshake is outstanding.
                if (
                    not payload.get("invalid")
                    and not payload.get("probe_pending")
                    and int(payload["expires_at"]) > self._now_ms()
                ):
                    return _binding_from_payload(payload)
            raise BillingUnavailable(
                "billing credential is not provisioned yet;"
                " the refresh sweep owns minting"
            )
        if payload is not None:
            self._validate_local_payload(payload, payer=payer, existing=existing)
            payload = self._finish_pending(payer, payload)
            if payload is not None:
                binding = _binding_from_payload(payload)
                if payload.pop("_probe_fallback", False):
                    return binding
                now = self._now_ms()
                reason = force_reason or (
                    "invalid_401" if payload.get("invalid") else ""
                )
                if (
                    not reason
                    and payload.get("source") == "employee_key"
                    and int(payload["expires_at"]) > now
                ):
                    # Maintained by the refresh sweep (one day before expiry),
                    # not by this protocol's 23-30 day window. Both firing on
                    # the same row would have them re-minting over each other.
                    # `not reason` means this only guards the PASSIVE renewal
                    # check: a forced reason (401 repair, "missing", ...) is
                    # always non-empty and never reaches this branch. 401
                    # repair for a source=employee_key row is routed by
                    # BillingIdentityPreparer.repair_metadata to the
                    # employee-key client before `ensure_available` is ever
                    # called — this method sees invalid_401 only for
                    # legacy-protocol rows.
                    return binding
                if not reason and int(payload["expires_at"]) > now:
                    jitter = _renew_jitter_ms(payer.employee_user_id)
                    if int(payload["expires_at"]) - now > _RENEW_WINDOW_MS - jitter:
                        return binding
                    if int(payload.get("renew_retry_after") or 0) > now:
                        return binding
                    reason = "renewal"
                elif not reason:
                    reason = "renewal"
                current = payload
            else:
                reason = "missing"
                current = None
        else:
            reason = force_reason or "missing"
            current = None

        try:
            response = self._gateway.ensure(
                employee_id=payer.employee_user_id,
                enterprise_email=payer.email,
                department_alias=payer.department_alias,
                reason=reason,
                current_key_id=str((current or {}).get("key_id") or ""),
                current_credential_version=int(
                    (current or {}).get("credential_version") or 0
                ),
            )
        except _GatewayError as exc:
            if (
                exc.retryable
                and current is not None
                and reason == "renewal"
                and not current.get("invalid")
                and int(current["expires_at"]) > self._now_ms()
            ):
                current["renew_retry_after"] = (
                    self._now_ms() + _RENEW_RETRY_BACKOFF_MS
                )
                self._save_payload(
                    payer.profile_name, payer.employee_user_id, current
                )
                return _binding_from_payload(current)
            raise _gateway_rejection(exc) from exc

        action = str(response.get("action") or "")
        state = str(response.get("state") or "")
        common = self._validate_ensure_response(
            response,
            payer=payer,
            existing=existing,
        )
        if action == "unchanged" and state == "active":
            if reason in {"missing", "invalid_401"} or current is None:
                raise RunRejected("AI Gateway returned an invalid credential state")
            if (
                common["key_id"] != current["key_id"]
                or common["credential_version"] != current["credential_version"]
            ):
                raise RunRejected("AI Gateway returned credential drift")
            self._validate_response_matches_local(common, current)
            current["invalid"] = False
            if reason == "renewal":
                current["renew_retry_after"] = (
                    self._now_ms() + _RENEW_RETRY_BACKOFF_MS
                )
            self._save_payload(payer.profile_name, payer.employee_user_id, current)
            return _binding_from_payload(current)

        if action not in {"issued", "rotated"} or state != "pending":
            raise RunRejected("AI Gateway returned an unsupported credential state")
        api_key = response.get("api_key")
        if not isinstance(api_key, str) or not api_key or len(api_key) > 4096:
            raise RunRejected("AI Gateway returned an invalid credential")
        if current is not None and int(common["credential_version"]) <= int(
            current["credential_version"]
        ):
            raise RunRejected("AI Gateway returned a stale credential")

        new_payload = {
            **common,
            "contract_version": _CONTRACT_VERSION,
            "api_key": api_key,
            "ack_pending": True,
            "probe_pending": True,
            "invalid": False,
        }
        if current is not None:
            new_payload["previous_credential"] = current
        self._save_payload(payer.profile_name, payer.employee_user_id, new_payload)
        finished = self._finish_pending(payer, new_payload)
        if finished is None:
            # credential_gone deleted the generation we just issued.  Reissue
            # once; a broker that keeps discarding fresh generations is an
            # outage, not something to hammer in an unbounded ensure loop.
            if gone_reissues_remaining <= 0:
                # Same class as _gateway_rejection's retryable branch: the
                # broker keeps discarding fresh generations, which is an
                # outage of OUR ability to obtain a credential, not a defect
                # in data we already hold. Must degrade, not fail closed.
                raise BillingUnavailable(
                    "employee billing initialization is temporarily unavailable"
                )
            return self._ensure_locked(
                payer,
                existing,
                force_reason="missing",
                gone_reissues_remaining=gone_reissues_remaining - 1,
            )
        return _binding_from_payload(finished)

    def _finish_pending(
        self,
        payer: _ResolvedPayer,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if payload.get("probe_pending"):
            try:
                self._probe(str(payload["api_key"]))
            except Exception:
                previous = payload.get("previous_credential")
                if (
                    isinstance(previous, dict)
                    and not previous.get("invalid")
                    and int(previous.get("expires_at") or 0) > self._now_ms()
                ):
                    self._validate_local_payload(previous, payer=payer)
                    previous["renew_retry_after"] = (
                        self._now_ms() + _RENEW_RETRY_BACKOFF_MS
                    )
                    self._save_payload(
                        payer.profile_name, payer.employee_user_id, previous
                    )
                    fallback = dict(previous)
                    fallback["_probe_fallback"] = True
                    return fallback
                else:
                    self._delete_payload(payer.profile_name, payer.employee_user_id)
                raise RunRejected("billing credential validation failed")
            payload.pop("previous_credential", None)
            payload["probe_pending"] = False
            self._save_payload(payer.profile_name, payer.employee_user_id, payload)
        if not payload.get("ack_pending"):
            return payload
        if int(payload.get("ack_retry_after") or 0) > self._now_ms():
            return payload
        try:
            response = self._gateway.ack(payload)
        except _GatewayError as exc:
            if exc.status == 410 and exc.code == "credential_gone":
                self._delete_payload(payer.profile_name, payer.employee_user_id)
                return None
            if exc.retryable:
                payload["ack_retry_after"] = (
                    self._now_ms() + _ACK_RETRY_BACKOFF_MS
                )
                self._save_payload(
                    payer.profile_name, payer.employee_user_id, payload
                )
                return payload
            raise _gateway_rejection(exc) from exc
        if (
            str(response.get("employee_id") or "") != payer.employee_user_id
            or str(response.get("key_id") or "") != str(payload["key_id"])
            or int(response.get("credential_version") or 0)
            != int(payload["credential_version"])
            or str(response.get("state") or "") != "active"
            or str(response.get("action") or "")
            not in {"activated", "already_active"}
        ):
            raise RunRejected("AI Gateway returned an invalid ACK")
        payload["ack_pending"] = False
        payload.pop("ack_retry_after", None)
        self._save_payload(payer.profile_name, payer.employee_user_id, payload)
        return payload

    def _validate_ensure_response(
        self,
        response: dict[str, Any],
        *,
        payer: _ResolvedPayer,
        existing: BillingIdentity | None,
    ) -> dict[str, Any]:
        if str(response.get("employee_id") or "") != payer.employee_user_id:
            raise RunRejected("AI Gateway returned a mismatched employee identity")
        email = str(response.get("enterprise_email") or "").strip()
        if email.lower() != payer.email.lower():
            raise RunRejected("AI Gateway returned a mismatched employee email")
        values = {
            "employee_id": payer.employee_user_id,
            "profile_name": payer.profile_name,
            "enterprise_email": email,
            "litellm_user_id": str(response.get("litellm_user_id") or "").strip(),
            "team_id": str(response.get("team_id") or "").strip(),
            "team_alias": str(response.get("team_alias") or "").strip(),
            "key_id": str(response.get("key_id") or "").strip(),
            "key_alias": str(response.get("key_alias") or "").strip(),
            "credential_version": response.get("credential_version"),
            "expires_at": response.get("expires_at"),
        }
        for key in ("litellm_user_id", "team_id", "key_id"):
            if not _OPAQUE_ID_RE.fullmatch(str(values[key])):
                raise RunRejected(f"AI Gateway returned an invalid {key}")
        if not values["team_alias"] or not values["key_alias"]:
            raise RunRejected("AI Gateway returned incomplete credential identity")
        if (
            not isinstance(values["credential_version"], int)
            or isinstance(values["credential_version"], bool)
            or int(values["credential_version"]) <= 0
            or not isinstance(values["expires_at"], int)
            or isinstance(values["expires_at"], bool)
            or int(values["expires_at"]) <= self._now_ms()
        ):
            raise RunRejected("AI Gateway returned an invalid credential lifetime")
        if existing is not None:
            if (
                existing.profile_name
                and existing.profile_name != payer.profile_name
            ):
                raise RunRejected("billing payer profile drift detected")
            if (
                existing.litellm_user_id
                and existing.litellm_user_id != values["litellm_user_id"]
            ):
                raise RunRejected("AI Gateway returned LiteLLM identity drift")
            if existing.team_id and existing.team_id != values["team_id"]:
                raise RunRejected("AI Gateway returned LiteLLM team drift")
        return values

    def _validate_local_payload(
        self,
        payload: dict[str, Any],
        *,
        payer: _ResolvedPayer | None = None,
        existing: BillingIdentity | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        version = str(payload.get("contract_version") or "").strip()
        if not version or version.split(".", 1)[0] != _CONTRACT_MAJOR:
            raise RunRejected("billing credential contract version is unsupported")
        required = (
            "employee_id", "profile_name", "enterprise_email",
            "litellm_user_id", "team_id", "team_alias", "key_id",
            "key_alias", "credential_version", "expires_at", "api_key",
        )
        if any(not payload.get(key) for key in required):
            raise RunRejected("billing credential vault entry is invalid")
        if payer is not None and (
            payload["employee_id"] != payer.employee_user_id
            or payload["profile_name"] != payer.profile_name
            or str(payload["enterprise_email"]).lower() != payer.email.lower()
        ):
            raise RunRejected("billing credential identity drift detected")
        if existing is not None and existing.migration_state == "enforced":
            if (
                payload["litellm_user_id"] != existing.litellm_user_id
                or payload["team_id"] != existing.team_id
            ):
                raise RunRejected("billing credential tuple drift detected")
        if metadata is not None:
            expected = {
                "employee_id": metadata.get("litellm_billing_employee_user_id"),
                "profile_name": metadata.get("litellm_billing_profile_name"),
                "litellm_user_id": metadata.get("litellm_billing_user_id"),
                "team_id": metadata.get("litellm_billing_team_id"),
                "key_id": metadata.get("litellm_billing_key_id"),
                "credential_version": metadata.get(
                    "litellm_billing_credential_version"
                ),
            }
            if any(str(payload[key]) != str(value) for key, value in expected.items()):
                raise RunRejected("billing runtime credential drift detected")

    @staticmethod
    def _validate_response_matches_local(
        response: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        for key in (
            "employee_id", "enterprise_email", "litellm_user_id", "team_id",
            "team_alias", "key_id", "key_alias", "credential_version",
            "expires_at",
        ):
            if str(response.get(key)) != str(payload.get(key)):
                raise RunRejected("AI Gateway returned credential drift")

    def _load_payload(self, profile_name: str, employee_id: str) -> dict[str, Any] | None:
        try:
            with self._vault_lock:
                status = self._vault.get_status(
                    profile_name=profile_name,
                    subject_id=employee_id,
                    provider=_PROVIDER,
                    secret_kind=_SECRET_KIND,
                )
                if status.get("status") == "missing":
                    return None
                payload = self._vault.get_secret_for_runtime(
                    profile_name=profile_name,
                    subject_id=employee_id,
                    provider=_PROVIDER,
                    secret_kind=_SECRET_KIND,
                )
        except Exception as exc:
            if _is_vault_unavailable(exc):
                # Genuinely transient: locked/busy DB, resource-busy I/O.
                raise BillingUnavailable("billing credential vault is unavailable") from exc
            # Everything else here is "we reached the vault and something is
            # wrong with what's in it or how we're allowed to read it":
            # HMAC/decrypt failure (tamper or key drift — a security event),
            # missing encryption key (config error), a corrupted schema, a
            # read-only mount, a row that vanished between the status check
            # and the read. Never degrade past this — falling back would
            # hide exactly the kind of defect this slug's boundary exists to
            # keep visible.
            raise RunRejected("billing credential vault entry is invalid") from exc
        if not isinstance(payload, dict):
            raise RunRejected("billing credential vault entry is invalid")
        return dict(payload)

    def _save_payload(
        self, profile_name: str, employee_id: str, payload: dict[str, Any]
    ) -> None:
        try:
            with self._vault_lock:
                self._vault.put_credential(
                    profile_name=profile_name,
                    subject_id=employee_id,
                    provider=_PROVIDER,
                    secret_kind=_SECRET_KIND,
                    payload=payload,
                    expires_at=int(payload["expires_at"]),
                )
        except Exception as exc:
            if _is_vault_unavailable(exc):
                raise BillingUnavailable("billing credential vault is unavailable") from exc
            # Config error (no encryption key configured), corrupted schema,
            # read-only mount, or a permission/integrity failure — not
            # "could not obtain", stays closed.
            raise RunRejected("billing credential vault write was rejected") from exc

    def _delete_payload(self, profile_name: str, employee_id: str) -> None:
        try:
            with self._vault_lock:
                self._vault.delete_credential(
                    profile_name=profile_name,
                    subject_id=employee_id,
                    provider=_PROVIDER,
                    secret_kind=_SECRET_KIND,
                )
        except Exception as exc:
            if _is_vault_unavailable(exc):
                raise BillingUnavailable("billing credential vault is unavailable") from exc
            raise RunRejected("billing credential vault delete was rejected") from exc

    def _probe_key(self, api_key: str) -> None:
        if not _allowed_billing_endpoint(self._model_base_url, self._model_base_url):
            raise RunRejected("LiteLLM billing endpoint is not configured")
        request = urllib.request.Request(
            f"{self._model_base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            method="GET",
        )
        try:
            # Same class of leak ecc9b16 fixed on EmployeeKeyClient._post: the
            # default opener follows 3xx and replays Authorization on the new
            # origin, including https->http. This header carries a live
            # per-employee billing key, so a redirect (misconfigured ingress,
            # hijacked DNS) would hand it to whoever controls the redirect
            # target. Reuse the same refuse-every-redirect opener; a redirect
            # here must fail the probe, not follow it.
            with _NO_REDIRECT_OPENER.open(request, timeout=5) as response:
                response.read(1)
                if int(getattr(response, "status", 200)) != 200:
                    raise RunRejected("billing credential validation failed")
        except urllib.error.HTTPError as exc:
            body = exc.read(8192).decode("utf-8", errors="replace").lower()
            if int(exc.code) == 429 and _is_budget_exceeded_text(body):
                return
            raise RunRejected("billing credential validation failed") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RunRejected("LiteLLM credential probe is unavailable") from exc

    def _payer_lock(self, employee_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(employee_id)
            if lock is None:
                # credential_gone immediately re-enters ensure for the same
                # payer after deleting the vanished generation.
                lock = threading.RLock()
                self._locks[employee_id] = lock
            return lock


def _binding_from_payload(payload: dict[str, Any]) -> BillingIdentity:
    return BillingIdentity(
        employee_user_id=str(payload["employee_id"]),
        profile_name=str(payload["profile_name"]),
        email=str(payload["enterprise_email"]),
        litellm_user_id=str(payload["litellm_user_id"]),
        team_id=str(payload["team_id"]),
        team_alias=str(payload.get("team_alias") or ""),
        key_id=str(payload["key_id"]),
        credential_version=int(payload["credential_version"]),
        expires_at=int(payload["expires_at"]),
        migration_state="enforced",
    )


def _require_contract(payload: dict[str, Any]) -> None:
    version = str(payload.get("contract_version") or "").strip()
    if not version or version.split(".", 1)[0] != _CONTRACT_MAJOR:
        raise _GatewayError(502, "unsupported_contract_version")


def _decode_json(raw: bytes) -> dict[str, Any]:
    if len(raw) > _MAX_BROKER_RESPONSE_BYTES:
        raise _GatewayError(502, "invalid_response")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _GatewayError(502, "invalid_json") from exc
    if not isinstance(payload, dict):
        raise _GatewayError(502, "invalid_response")
    return payload


def _idempotency_key(kind: str, body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"hermes-v1:{kind}:{canonical}".encode("utf-8")).hexdigest()


_GATEWAY_UNAVAILABLE_STATUSES = frozenset({502, 503, 504})

# _post() mints these itself when the RESPONSE is broken/mismatched — not
# when the gateway is unreachable — even though it sometimes labels them
# with a 502/503 status. Must never degrade even if the status looks
# transport-shaped.
_GATEWAY_DATA_INTEGRITY_CODES = frozenset({
    "invalid_response",
    "invalid_json",
    "invalid_error_envelope",
    "unsupported_contract_version",
})

# Minted by US (grepped every ``_GatewayError(`` call site in this file —
# these are the only two that never reach the network at all, both from
# _post()'s own precondition check). A malformed/missing broker URL or
# token is OUR configuration being broken, not the gateway being
# unreachable — codex r2 p1-1: this must stay closed even though _post()
# happens to label it 503, the same status a genuine outage uses.
# ``broker_unavailable`` is deliberately NOT here: that one fires from a
# real connection attempt (URLError/OSError) and is a genuine "could not
# obtain" signal.
_GATEWAY_CONFIG_CODES = frozenset({"broker_not_configured"})


def _gateway_rejection(exc: _GatewayError) -> RunRejected:
    # A conflict means the gateway holds data that contradicts ours — a human
    # has to look.
    if exc.code in {"identity_conflict", "account_topology_conflict"}:
        return RunRejected("employee LiteLLM account requires manual reconciliation")
    # "We got an answer and it's wrong" (contract drift, malformed JSON, a
    # broken error envelope) is never "we couldn't get one" — degrading here
    # would bury a real contract break inside normal traffic.
    if exc.code in _GATEWAY_DATA_INTEGRITY_CODES:
        return RunRejected(f"AI Gateway response was invalid ({exc.code})")
    # Our own misconfiguration, never sent over the wire — stays closed so
    # ops notices instead of every request silently degrading forever.
    if exc.code in _GATEWAY_CONFIG_CODES:
        return RunRejected(f"AI Gateway is misconfigured ({exc.code})")
    # Whitelist by transport/service-availability HTTP status ONLY — never
    # by the gateway's self-declared ``retryable`` flag. That flag is
    # remote-supplied data; trusting it let auth failures (401/403) and
    # unrecognized 4xx rejections degrade past whenever the gateway (bug or
    # not) happened to mark them retryable. 502/503/504 are the actual
    # "could not obtain" shapes: bad gateway, service unavailable, timeout.
    if exc.status in _GATEWAY_UNAVAILABLE_STATUSES:
        return BillingUnavailable(
            "employee billing initialization is temporarily unavailable"
        )
    return RunRejected("employee billing initialization was rejected")


def _renew_jitter_ms(employee_id: str) -> int:
    digest = hashlib.sha256(employee_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % (_RENEW_JITTER_MS + 1)


def _is_budget_exceeded_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "budget_exceeded", "budget exceeded", "max budget",
            "monthly budget", "budget has been exceeded",
        )
    )


def _allowed_billing_endpoint(left: str, right: str) -> bool:
    try:
        configured = urlparse(str(left or "").strip())
        destination = urlparse(str(right or "").strip())
    except ValueError:
        return False
    # This endpoint receives a per-employee Bearer credential.  Never permit
    # plaintext transport, including loopback/private-network URLs: an
    # accidentally relaxed production URL must fail before the key is put on
    # the wire.
    if configured.scheme != "https" or destination.scheme != "https":
        return False
    if any(
        (
            configured.username,
            configured.password,
            configured.query,
            configured.fragment,
            destination.username,
            destination.password,
            destination.query,
            destination.fragment,
        )
    ):
        return False
    if not configured.netloc or configured.netloc.lower() != destination.netloc.lower():
        return False
    allowed = {
        item.strip().rstrip("/") or "/"
        for item in os.environ.get(
            "HERMES_LITELLM_BILLING_ALLOWED_PATHS", "/v1,/anthropic"
        ).split(",")
        if item.strip()
    }
    path = destination.path.rstrip("/") or "/"
    return any(path == prefix or path.startswith(prefix + "/") for prefix in allowed)
