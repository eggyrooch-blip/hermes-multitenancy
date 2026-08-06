"""Client for the AI Gateway's auto-provisioning employee-key endpoint.

The gateway owns everything hard about minting a billing credential: it resolves
the employee from Feishu, maps their first-level department onto a LiteLLM team
(creating it when absent), opens the account with its monthly budget, and issues
the key. Hermes is a client with exactly two reasons to ask: the employee has no
key yet, or their key stopped working.

Why this exists next to :class:`BillingGatewayClient` rather than replacing it:
that client speaks the ensure/ack contract (with ``key_id``, ``credential_version``
and an ACK handshake) which this endpoint does not have. The trade is deliberate
and documented in the slug SPEC — the gateway is the only place that should
resolve departments, and duplicating that in Hermes off a leaf-only ``dept_name``
would file people under the wrong billing team.

Identity drift: the gateway returns ``litellm_user_id`` and ``team_id`` (added
2026-08-06) together with ``account_identity_verified``, which says whether it
could actually check them against what LiteLLM bound the key to. Two rules follow,
both enforced in :func:`check_account_drift`:

* Anchor drift on ``litellm_user_id`` ONLY. ``team_id`` is routing — every
  issuance re-resolves the employee's CURRENT department, so a transfer
  legitimately changes it. Comparing it as an invariant turns every transfer
  into a false alarm.
* Never anchor on ids the gateway flagged unverified; treat them as absent.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

_EMPLOYEE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_KEY_ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}")
_MAX_RESPONSE_BYTES = 64 * 1024
_KEY_PATH = "/internal/v1/employee/key"
# Stamped into the stored payload so the legacy ensure/ack path can tell
# whose credential this is and leave its renewal alone.
CREDENTIAL_SOURCE = "employee_key"
# The endpoint is configured for 30 days; allow slack for clock skew and a
# future config bump, but refuse anything absurd in either direction.
#
# The floor MUST clear the refresh lead window (_REFRESH_LEAD_MS, below) by a
# comfortable margin. A key that lands inside that window the moment it is
# issued is due for replacement on the very next sweep pass — every pass
# re-mints it, forever, and if the sweep ever falls behind by more than its
# own lifetime the employee is left with nothing. 1 hour used to be the floor
# here; a 2-hour key would have cleared it while still being far shorter than
# the 1-day refresh lead, which is exactly that failure mode.
_MIN_LIFETIME_MS = 2 * 24 * 60 * 60 * 1000    # 2 days: > 1-day refresh lead
_MAX_LIFETIME_MS = 400 * 24 * 60 * 60 * 1000  # a bit over a year


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect instead of following it.

    urllib follows 301/302/303 by default AND replays ``Authorization`` on the
    new origin — including an https -> http downgrade. This request carries a
    bearer that can mint a key for ANY employee, so a single redirect (a
    misconfigured ingress, a hijacked DNS answer) would hand that token to
    somebody else in cleartext. There is no legitimate redirect on this
    endpoint, so the safe behaviour is to fail loudly.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect refused on a credential endpoint",
            headers, fp,
        )


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirects)


class EmployeeKeyError(RuntimeError):
    """The gateway did not hand back a credential we are willing to store."""

    def __init__(self, code: str, *, status: int = 0, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class IssuedKey:
    """One freshly minted credential, already validated and normalized."""

    employee_id: str
    email: str
    api_key: str
    base_url: str
    key_alias: str
    team_alias: str
    expires_at_ms: int
    litellm_user_id: str
    team_id: str
    # False when the gateway could not check the key's binding against LiteLLM.
    # Such ids must NOT be used as a drift anchor — see check_account_drift.
    account_identity_verified: bool


def _parse_expires_at(raw: Any) -> int:
    """ISO-8601 (the endpoint's shape) -> epoch ms (what the vault stores).

    Refuses anything unparseable instead of defaulting to 0, which would store a
    credential that reads as already-expired and silently deny the employee.
    """
    text = str(raw or "").strip()
    if not text:
        raise EmployeeKeyError("employee_key_expires_at_missing")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EmployeeKeyError("employee_key_expires_at_invalid") from exc
    if parsed.tzinfo is None:
        raise EmployeeKeyError("employee_key_expires_at_naive")
    return int(parsed.timestamp() * 1000)


class EmployeeKeyClient:
    """Minimal client. Holds no state and never talks to LiteLLM directly."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        opener: Callable[..., Any] | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._base_url = str(base_url or "").strip().rstrip("/")
        self._token = str(token or "").strip()
        self._timeout = max(1.0, min(float(timeout), 60.0))
        # Default to the non-following opener; tests may inject their own.
        self._opener = opener or _NO_REDIRECT_OPENER.open
        import time

        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    def issue(
        self, *, employee_id: str, enterprise_email: str, idempotency_key: str
    ) -> IssuedKey:
        if not _EMPLOYEE_ID_RE.fullmatch(str(employee_id or "")):
            raise EmployeeKeyError("employee_key_employee_id_invalid")
        email = str(enterprise_email or "").strip()
        if not email or "@" not in email:
            raise EmployeeKeyError("employee_key_email_invalid")
        if not (8 <= len(str(idempotency_key or "")) <= 200):
            raise EmployeeKeyError("employee_key_idempotency_invalid")
        if not self._base_url.startswith("https://"):
            # The response carries key PLAINTEXT; never put it on cleartext.
            raise EmployeeKeyError("employee_key_endpoint_not_https")
        if not self._token:
            raise EmployeeKeyError("employee_key_token_missing")

        payload = self._post(employee_id, idempotency_key)
        return self._validate(payload, employee_id=employee_id, email=email)

    def _post(self, employee_id: str, idempotency_key: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self._base_url + _KEY_PATH,
            data=b"",  # the endpoint takes no body; identity is in the header
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "X-Trusted-Employee-Id": employee_id,
                "Idempotency-Key": idempotency_key,
            },
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read(_MAX_RESPONSE_BYTES + 1) if hasattr(exc, "read") else b""
        except Exception as exc:  # transport: retryable, nothing was stored
            raise EmployeeKeyError("employee_key_unreachable", retryable=True) from exc

        if len(raw) > _MAX_RESPONSE_BYTES:
            raise EmployeeKeyError("employee_key_response_too_large", status=status)
        if int(status) != 200:
            raise EmployeeKeyError(
                "employee_key_rejected",
                status=int(status),
                # 5xx may clear on its own; 401/403 will not until the
                # gateway-side token or IP allowlist is corrected.
                retryable=int(status) >= 500,
            )
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EmployeeKeyError("employee_key_response_not_json") from exc
        if not isinstance(body, dict):
            raise EmployeeKeyError("employee_key_response_not_object")
        return body

    def _validate(
        self, body: dict[str, Any], *, employee_id: str, email: str
    ) -> IssuedKey:
        api_key = str(body.get("api_key") or "").strip()
        base_url = str(body.get("base_url") or "").strip()
        key_alias = str(body.get("key_alias") or "").strip()
        team_alias = str(body.get("team_alias") or "").strip()
        if not api_key:
            raise EmployeeKeyError("employee_key_response_missing_api_key")
        if not base_url.startswith("https://"):
            raise EmployeeKeyError("employee_key_response_base_url_invalid")
        if not _KEY_ALIAS_RE.fullmatch(key_alias):
            raise EmployeeKeyError("employee_key_response_key_alias_invalid")
        if not team_alias:
            raise EmployeeKeyError("employee_key_response_team_alias_missing")
        # The gateway mints the alias as
        #     auto-<employee_id>-<YYYYMMDD>-<HHMMSS>-<rand>
        # Employee ids may themselves contain hyphens, so a PREFIX test is
        # ambiguous: `auto-sun-ke-…` (employee `sun-ke`) starts with
        # `auto-sun-` and would satisfy a request for `sun`. Split the three
        # fixed trailing segments off instead and compare the remainder
        # exactly — that is unambiguous whatever the id contains.
        alias_head = key_alias.casefold().rsplit("-", 3)[0]
        if alias_head != f"auto-{employee_id.casefold()}":
            raise EmployeeKeyError("employee_key_response_subject_mismatch")
        expires_at_ms = _parse_expires_at(body.get("expires_at"))
        now = self._now_ms()
        if expires_at_ms <= now:
            raise EmployeeKeyError("employee_key_response_already_expired")
        # The gateway mints 30-day keys. Accepting any future instant lets a
        # malformed one-minute issuance overwrite a healthy key AND stay inside
        # the refresh window forever — a credential gap plus an endless
        # re-issue loop. Bound it on both sides instead.
        lifetime = expires_at_ms - now
        if lifetime < _MIN_LIFETIME_MS or lifetime > _MAX_LIFETIME_MS:
            raise EmployeeKeyError("employee_key_response_lifetime_implausible")
        litellm_user_id = str(body.get("litellm_user_id") or "").strip()
        team_id = str(body.get("team_id") or "").strip()
        if not litellm_user_id or not team_id:
            raise EmployeeKeyError("employee_key_response_account_identity_missing")
        return IssuedKey(
            employee_id=employee_id,
            email=email,
            api_key=api_key,
            base_url=base_url,
            key_alias=key_alias,
            team_alias=team_alias,
            expires_at_ms=expires_at_ms,
            litellm_user_id=litellm_user_id,
            team_id=team_id,
            # `is True`, not bool(): JSON can carry the STRING "false", and
            # bool("false") is True — which would read an explicitly
            # unverified response as verified.
            account_identity_verified=body.get("account_identity_verified") is True,
        )


class AccountDriftError(RuntimeError):
    """The gateway handed back a credential for a different LiteLLM account."""


def check_account_drift(issued: IssuedKey, stored: dict[str, Any] | None) -> None:
    """Refuse a credential whose account identity moved under us.

    Anchored on ``litellm_user_id`` ALONE. ``team_id`` is deliberately not
    compared: every issuance re-resolves the employee's current Feishu
    department, so a transfer — or a team being recreated — legitimately
    changes it for the same person. Treating it as an invariant would turn
    every department transfer into a false drift alarm and deny a valid
    credential.

    Ids the gateway could not verify are treated as absent rather than as
    evidence: comparing two unverified values proves nothing, and acting on
    them would be exactly the "verifying our own intent" failure this pair of
    fields exists to prevent.
    """
    if stored is None or not issued.account_identity_verified:
        return
    previous = str(stored.get("litellm_user_id") or "")
    if not previous or not stored.get("account_identity_verified", True):
        return
    if previous != issued.litellm_user_id:
        raise AccountDriftError(
            "billing credential account changed: stored="
            f"{previous} issued={issued.litellm_user_id}"
        )


def to_vault_payload(
    issued: IssuedKey, *, profile_name: str, credential_version: int
) -> dict[str, Any]:
    """Shape an :class:`IssuedKey` into the payload the billing vault stores.

    ``key_id`` carries the alias: this endpoint does not return a separate key
    id, and the alias is unique per issuance (``auto-<employee>-<stamp>-<rand>``),
    so it is the stable handle for this credential. ``credential_version`` is
    ours to keep — the endpoint has no version concept, it just mints.
    """
    if credential_version <= 0:
        raise EmployeeKeyError("employee_key_credential_version_invalid")
    if not profile_name:
        raise EmployeeKeyError("employee_key_profile_name_missing")
    return {
        "contract_version": "1.0",
        "employee_id": issued.employee_id,
        "profile_name": profile_name,
        "enterprise_email": issued.email,
        "litellm_user_id": issued.litellm_user_id,
        "team_id": issued.team_id,
        "team_alias": issued.team_alias,
        "key_id": issued.key_alias,
        "key_alias": issued.key_alias,
        "credential_version": int(credential_version),
        "expires_at": issued.expires_at_ms,
        "api_key": issued.api_key,
        "base_url": issued.base_url,
        "account_identity_verified": issued.account_identity_verified,
        # Which protocol owns this credential. Without it the legacy
        # ensure/ack renewal (23-30 days out) and the sweep (1 day out) both
        # think they are responsible for the same row.
        "source": CREDENTIAL_SOURCE,
    }


def next_credential_version(stored: dict[str, Any] | None) -> int:
    """Versions are local bookkeeping — the endpoint has no notion of them."""
    if not stored:
        return 1
    try:
        current = int(stored.get("credential_version") or 0)
    except (TypeError, ValueError):
        current = 0
    return max(current, 0) + 1


# Refresh one day before the key dies. Deliberately simple: the gateway mints
# 30-day keys, so the one being replaced expires on its own a day later —
# nothing accumulates and nothing needs revoking (Hermes holds no LiteLLM admin
# credential anyway; only the gateway could revoke). No jitter either: even if
# every employee were issued on the same day, ~1300 refreshes spread over that
# day is about one request per minute.
_REFRESH_LEAD_MS = 24 * 60 * 60 * 1000


def needs_new_key(stored: dict[str, Any] | None, now_ms: int) -> bool:
    """Should we ask the gateway for a key for this payer right now?

    True when there is none, when the stored one was marked invalid, or when it
    is inside the refresh lead window. Refreshing is an OPTIMISATION: a caller
    that fails to get a new key must keep using the stored one until it truly
    expires — turning "refresh early" into "deny early" would be worse than
    not refreshing at all.
    """
    if not stored:
        return True
    if stored.get("invalid"):
        return True
    try:
        expires_at = int(stored.get("expires_at") or 0)
    except (TypeError, ValueError):
        return True  # unreadable expiry: treat as needing a fresh one
    return expires_at - now_ms <= _REFRESH_LEAD_MS


@dataclass
class SweepResult:
    """What one maintenance pass did. Printed by the timer, kept in the audit."""

    checked: int = 0
    issued: int = 0
    skipped_current: int = 0
    failed: int = 0
    deferred_budget: int = 0
    failures: list[tuple[str, str]] | None = None

    def summary(self) -> str:
        return (
            "checked=%d issued=%d already_current=%d failed=%d deferred=%d"
            % (
                self.checked,
                self.issued,
                self.skipped_current,
                self.failed,
                self.deferred_budget,
            )
        )


def sweep_cohort(
    cohort: list[Any],
    *,
    needs: Callable[[Any], bool],
    issue: Callable[[Any], Any],
    store: Callable[[Any, Any], None],
    max_issues: int = 200,
    on_event: Callable[[str, str, str], None] | None = None,
) -> SweepResult:
    """Keep every cohort member holding a usable credential.

    Runs on a timer, not on the employee's request path: a credential that is
    minted while somebody waits is a credential whose failure becomes that
    person's failure. Here a failure is an alert, and the employee — who still
    holds a valid key until it actually expires — notices nothing.

    One member's failure never aborts the pass: the whole point is that the
    other 1281 still get maintained. ``max_issues`` paces the first run after a
    cohort is switched on, when everybody needs a key at once, so the sweep
    does not hammer Feishu and LiteLLM in one burst — the remainder is picked
    up by the next pass.
    """
    result = SweepResult(failures=[])
    attempted = 0
    for member in cohort:
        result.checked += 1
        try:
            if not needs(member):
                result.skipped_current += 1
                continue
            # Count the ATTEMPT, not the success. Counting successes would let
            # a run whose storage keeps failing mint for the whole cohort while
            # the counter stays at zero — the opposite of a cap, and every one
            # of those keys is a live orphan.
            if attempted >= max_issues:
                result.deferred_budget += 1
                continue
            attempted += 1
            issued = issue(member)
            store(member, issued)
            result.issued += 1
            if on_event:
                on_event("issued", str(member), "")
        except Exception as exc:  # one bad member must not stop the sweep
            result.failed += 1
            result.failures.append((str(member), type(exc).__name__))
            if on_event:
                on_event("failed", str(member), type(exc).__name__)
    return result



def store_binding(preparer: Any, payer: Any, issued: IssuedKey) -> Any:
    """Persist a freshly issued credential to BOTH stores, or to neither.

    The vault holds the key; ``BillingIdentityStore`` holds the durable
    identity the request path validates against. They are two separate
    connections (even when they share a db file), so there is no real cross-
    store transaction — writing the vault and then discovering the identity
    write failed used to leave the vault ahead: a fresh key on a row whose
    identity still names the old team. The request path keeps rejecting the
    old team, `employee_key_needed` sees the fresh vault key and reports
    "current", and the sweep skips the employee on every future pass — a
    split brain that nothing self-heals. That gap was proved live: fail the
    second write and the pre-fix code left exactly that state (see the
    regression test for this function).

    So: on an identity-write failure, roll the vault back to whatever it held
    immediately before this call (delete it if there was nothing). The
    visible state after a failed `store_binding` is then byte-for-byte the
    state before it was called — the next sweep pass sees the same "needs a
    key" (or "has a key") answer it would have without this attempt, and
    converges instead of drifting.
    """
    manager = preparer._credentials
    with manager._payer_lock(payer.employee_user_id):
        previous = manager._load_payload(payer.profile_name, payer.employee_user_id)
        binding = manager.adopt_employee_key(payer, issued)
        try:
            preparer._store.put(binding)
        except Exception:
            if previous is not None:
                manager._save_payload(
                    payer.profile_name, payer.employee_user_id, previous
                )
            else:
                manager._delete_payload(
                    payer.profile_name, payer.employee_user_id
                )
            raise
        return binding


def run_refresh(*, dry_run: bool = False, max_issues: int = 200) -> dict[str, Any]:
    """One maintenance pass over the billing cohort. Timer entry point.

    ``dry_run`` **mints nothing and writes no billing state** — no key is
    requested, no binding is stored. Be precise about what it is NOT, though:
    opening the credential store still runs its ``CREATE TABLE IF NOT EXISTS``
    and chmod, so on a host without the database yet it creates the file. It is
    "changes no billing state", not "touches no bytes".

    (The AI Gateway ships a command *called* ``dry-run`` whose docstring reads
    "provision a single user end-to-end (LiteLLM + notify)" — it really opens
    accounts and pushes cards. Do not reach for that one expecting this.)
    """
    import os
    import sqlite3

    from .billing_credentials import _ResolvedPayer
    from .billing_identity import (
        _billing_model_base_url,
        _default_preparer,
        _is_canonical_employee_id,
    )
    from .billing_readiness import _cohort_ids

    cohort_ids = _cohort_ids(os.environ.get("HERMES_LITELLM_BILLING_PAYER_IDS", ""))
    domain = str(
        os.environ.get("HERMES_LITELLM_EMPLOYEE_EMAIL_DOMAIN", "keep.com")
    ).strip()
    db_path = str(os.environ.get("HERMES_MULTITENANCY_DB", "")).strip()
    if not db_path:
        raise EmployeeKeyError("employee_key_refresh_db_unset")

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        profiles = {
            str(row[0]): str(row[1] or row[0])
            for row in conn.execute(
                "SELECT user_id, profile_name FROM multitenancy_routing "
                "WHERE kind='user' AND active=1 AND provenance='sync'"
            )
        }

    from .billing_identity import _employee_org_fields

    payers, unrouted = [], []
    for employee_id in cohort_ids:
        # Same canonical-subject guard the live request path enforces via
        # `_employee_row` (regex shape, never an `ou_*` Feishu open_id — those
        # are chat identities, not LiteLLM billing subjects). The routing
        # query above filters on kind/active/provenance but NOT on shape, so
        # without this a synthetic `ou_*` row injected into
        # `multitenancy_routing` — or simply typo'd into
        # HERMES_LITELLM_BILLING_PAYER_IDS — would sail through the sweep
        # even though the normal request path would refuse to ever resolve
        # it as a payer. Report it the same way an unrouted id is reported.
        if not _is_canonical_employee_id(employee_id):
            unrouted.append(employee_id)
            continue
        profile = profiles.get(employee_id)
        if not profile:
            # In the cohort but not in routing: minting would file the key
            # under a profile that does not exist. Report, never guess.
            unrouted.append(employee_id)
            continue
        # The org snapshot is the same source the request path uses. Deriving
        # `<id>@<domain>` here instead would hand a key to anyone whose
        # canonical address differs — and the next request would reject it as
        # identity drift, so the sweep would re-mint forever.
        try:
            email, _department = _employee_org_fields(employee_id)
        except Exception:
            email = ""
        if not email:
            email = f"{employee_id}@{domain}"
        payers.append(_ResolvedPayer(employee_id, profile, email, ""))

    preparer = _default_preparer()
    credentials = preparer._credentials

    def _store_binding(payer: _ResolvedPayer, issued: IssuedKey) -> None:
        store_binding(preparer, payer, issued)
    client = EmployeeKeyClient(
        os.environ.get("HERMES_EMPLOYEE_KEY_BASE_URL", "")
        or os.environ.get("HERMES_AI_GATEWAY_BROKER_URL", ""),
        os.environ.get("HERMES_EMPLOYEE_KEY_SILENT_TOKEN", ""),
    )

    def _issue(payer: _ResolvedPayer) -> IssuedKey:
        import uuid

        return client.issue(
            employee_id=payer.employee_user_id,
            enterprise_email=payer.email,
            idempotency_key=f"refresh-{payer.employee_user_id}-{uuid.uuid4().hex[:12]}",
        )

    if dry_run:
        due = [p.employee_user_id for p in payers
               if credentials.employee_key_needed(p)]
        return {
            "dry_run": True,
            "cohort": len(cohort_ids),
            "unrouted": unrouted,
            "would_issue": due,
            "base_url": _billing_model_base_url(),
        }

    result = sweep_cohort(
        payers,
        needs=credentials.employee_key_needed,
        issue=_issue,
        store=_store_binding,
        max_issues=max_issues,
    )
    return {
        "dry_run": False,
        "cohort": len(cohort_ids),
        "unrouted": unrouted,
        **{
            k: getattr(result, k)
            for k in ("checked", "issued", "skipped_current", "failed",
                      "deferred_budget")
        },
        "failures": result.failures or [],
    }


def refresh_main(argv: list[str] | None = None) -> int:
    import argparse
    import json as _json
    import sys as _sys

    parser = argparse.ArgumentParser(
        prog="hermes-multitenancy-billing-refresh",
        description="Keep every billing-cohort member holding a usable key.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report who would be issued; mints nothing, stores nothing",
    )
    parser.add_argument("--max-issues", type=int, default=200)
    args = parser.parse_args(argv)
    try:
        summary = run_refresh(dry_run=args.dry_run, max_issues=args.max_issues)
    except Exception as exc:  # noqa: BLE001 — a timer wants an exit code
        print(_json.dumps({"ok": False, "error": str(exc)}), file=_sys.stderr)
        return 1
    print(_json.dumps({"ok": True, **summary}, indent=2, sort_keys=True,
                      ensure_ascii=False))
    return 0 if not summary.get("failed") else 2
