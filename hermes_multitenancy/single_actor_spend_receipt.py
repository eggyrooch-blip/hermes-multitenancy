"""Fresh signed proof that one sealed WebUI actor spent on its exact vault key."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import re
import threading
import time
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .billing_identity import _metadata_for_binding
from .trusted_runtime_principal import TrustedRuntimePrincipal


_DOMAIN = b"hermes-single-actor-spend:v1\n"
_OPAQUE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_HEX_256 = re.compile(r"[0-9a-f]{64}")
_PAGE_SIZE = 100
_MAX_TTL_MS = 10 * 60 * 1000
_MIN_TTL_MS = 60 * 1000
_SPEND_POLL_TIMEOUT_S = 15.0
_SPEND_POLL_INTERVAL_S = 0.5


class SpendReceiptRejected(ValueError):
    """The supplied identity, spend snapshot, or receipt is not trustworthy."""


_STATE_SEAL = object()


@dataclass(slots=True)
class _SpendState:
    _principal: TrustedRuntimePrincipal = field(repr=False)
    _routing: Any = field(repr=False)
    _store: Any = field(repr=False)
    _credentials: Any = field(repr=False)
    _client: Any = field(repr=False)
    _profile_is_solely_owned: Callable[[str, str], bool] = field(repr=False)
    _run_id: str
    _audience: str
    _billing_base_url: str
    _receipt_signer: Ed25519PrivateKey = field(repr=False)
    _fingerprint_key: bytes = field(repr=False)
    _api_key: str = field(repr=False)
    _spend_key: str = field(repr=False)
    _identity: Any = field(repr=False)
    _before: dict[str, Decimal] = field(repr=False)
    _period_start: str
    _period_end: str
    _started_at_ms: int
    _seal: object = field(repr=False, default=None)
    _used: bool = field(repr=False, default=False)
    _lock: threading.Lock = field(repr=False, default_factory=threading.Lock)


def _fingerprint(key: bytes, domain: str, value: str) -> str:
    return hmac.new(key, f"{domain}\0{value}".encode(), hashlib.sha256).hexdigest()


def _principal_fingerprints(
    principal: TrustedRuntimePrincipal, api_key: str, fingerprint_key: bytes
) -> tuple[str, str, str]:
    if (
        not isinstance(principal, TrustedRuntimePrincipal)
        or not principal.is_authentic()
        or principal.channel != "webui"
        or not principal.actor_subject.startswith("ou_")
        or principal.credential_subject != principal.actor_subject
        or not principal.profile_name
        or not api_key
        or len(api_key) > 4096
        or len(fingerprint_key) < 32
    ):
        raise SpendReceiptRejected("principal_or_key_invalid")
    return (
        _fingerprint(fingerprint_key, "actor", principal.actor_subject),
        _fingerprint(fingerprint_key, "profile", principal.profile_name),
        _fingerprint(fingerprint_key, "billing-key", api_key),
    )


def _unsigned(receipt: dict[str, Any]) -> bytes:
    return json.dumps(
        {key: value for key, value in receipt.items() if key != "signature"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _nonnegative_integer(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SpendReceiptRejected("spend_page_invalid")
    return value


def _spend(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SpendReceiptRejected("spend_value_invalid") from exc
    if not amount.is_finite() or amount < 0:
        raise SpendReceiptRejected("spend_value_invalid")
    return amount


def _external_request_id(value: Any) -> str:
    """Accept a provider-owned opaque ID without guessing its alphabet."""
    request_id = str(value or "")
    if (
        not request_id
        or len(request_id) > 1024
        or request_id != request_id.strip()
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in request_id)
    ):
        raise SpendReceiptRejected("request_id_invalid")
    return request_id


def _resolve_actor_key(
    *,
    principal: TrustedRuntimePrincipal,
    routing: Any,
    store: Any,
    credentials: Any,
    profile_is_solely_owned: Callable[[str, str], bool],
    billing_base_url: str,
    fingerprint_key: bytes,
    now_ms: int,
) -> tuple[Any, str]:
    if (
        not isinstance(principal, TrustedRuntimePrincipal)
        or not principal.is_authentic()
        or principal.channel != "webui"
        or not principal.actor_subject.startswith("ou_")
        or principal.credential_subject != principal.actor_subject
        or not principal.profile_name
        or len(fingerprint_key) < 32
    ):
        raise SpendReceiptRejected("principal_invalid")
    try:
        route = routing.lookup_by_open_id(principal.actor_subject)
    except Exception as exc:
        raise SpendReceiptRejected("actor_route_invalid") from exc
    try:
        solely_owned = profile_is_solely_owned(
            principal.profile_name, principal.actor_subject
        )
    except Exception as exc:
        raise SpendReceiptRejected("actor_route_invalid") from exc
    if (
        route is None
        or str(getattr(route, "open_id", "") or "") != principal.actor_subject
        or str(getattr(route, "profile_name", "") or "") != principal.profile_name
        or not bool(getattr(route, "active", False))
        or str(getattr(route, "kind", "") or "") != "user"
        or str(getattr(route, "provenance", "") or "") != "sync"
        or solely_owned is not True
    ):
        raise SpendReceiptRejected("actor_route_invalid")
    try:
        identity = store.get(str(getattr(route, "user_id", "") or ""))
    except Exception as exc:
        raise SpendReceiptRejected("billing_binding_invalid") from exc
    if (
        identity is None
        or identity.employee_user_id != route.user_id
        or identity.profile_name != principal.profile_name
        or identity.migration_state != "enforced"
        or not identity.litellm_user_id
        or not identity.key_id
        or not isinstance(identity.expires_at, int)
        or isinstance(identity.expires_at, bool)
        or identity.expires_at - now_ms < _MIN_TTL_MS
    ):
        raise SpendReceiptRejected("billing_binding_invalid")
    metadata = _metadata_for_binding(identity, billing_base_url)
    try:
        api_key = credentials.runtime_api_key(metadata)
    except Exception as exc:
        raise SpendReceiptRejected("billing_key_unavailable") from exc
    _principal_fingerprints(principal, api_key, fingerprint_key)
    return identity, api_key


def _collect_spend_rows(
    client: Any,
    spend_key: str,
    *,
    period_start: str,
    period_end: str,
) -> dict[str, Decimal]:
    """Copy of the proven cost-closeout pagination seam, narrowed to one key."""
    rows_by_request: dict[str, Decimal] = {}
    expected_total: int | None = None
    expected_pages: int | None = None
    page = 1
    while True:
        try:
            payload = client.get(
                "/spend/logs/v2",
                {
                    "start_date": period_start,
                    "end_date": period_end,
                    "page": page,
                    "page_size": _PAGE_SIZE,
                    "api_key": spend_key,
                },
            )
        except SpendReceiptRejected:
            raise
        except Exception as exc:
            raise SpendReceiptRejected("spend_get_failed") from exc
        if not isinstance(payload, dict):
            raise SpendReceiptRejected("spend_page_invalid")
        rows = payload.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise SpendReceiptRejected("spend_page_invalid")
        total = _nonnegative_integer(payload.get("total"))
        pages = _nonnegative_integer(payload.get("total_pages"))
        response_page = _nonnegative_integer(payload.get("page"))
        response_size = _nonnegative_integer(payload.get("page_size"))
        calculated_pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
        if (
            payload.get("total_is_capped") is not False
            or response_page != page
            or response_size != _PAGE_SIZE
            or pages not in ({0, 1} if total == 0 else {calculated_pages})
            or len(rows) > _PAGE_SIZE
            or (page < pages and len(rows) != _PAGE_SIZE)
        ):
            raise SpendReceiptRejected("spend_page_invalid")
        if expected_total is None:
            expected_total, expected_pages = total, pages
        elif total != expected_total or pages != expected_pages:
            raise SpendReceiptRejected("spend_page_changed")
        for row in rows:
            if not hmac.compare_digest(str(row.get("api_key") or ""), spend_key):
                raise SpendReceiptRejected("spend_filter_leak")
            request_id = _external_request_id(row.get("request_id"))
            if request_id in rows_by_request:
                raise SpendReceiptRejected("request_id_invalid")
            rows_by_request[request_id] = _spend(row.get("spend"))
        if pages <= page:
            break
        page += 1
    if expected_total is None or len(rows_by_request) != expected_total:
        raise SpendReceiptRejected("spend_count_mismatch")
    return rows_by_request


def begin_single_actor_spend_receipt(
    *,
    principal: TrustedRuntimePrincipal,
    routing: Any,
    store: Any,
    credentials: Any,
    client: Any,
    profile_is_solely_owned: Callable[[str, str], bool],
    run_id: str,
    audience: str,
    billing_base_url: str,
    signing_key: Ed25519PrivateKey,
    fingerprint_key: bytes,
    now_ms: int,
) -> _SpendState:
    """Bind one sealed actor/key and capture its complete pre-run snapshot."""
    if (
        not _OPAQUE_ID.fullmatch(str(run_id or ""))
        or not _OPAQUE_ID.fullmatch(str(audience or ""))
    ):
        raise SpendReceiptRejected("run_id_invalid")
    if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms <= 0:
        raise SpendReceiptRejected("clock_invalid")
    if not isinstance(signing_key, Ed25519PrivateKey):
        raise SpendReceiptRejected("signer_invalid")
    base_url = str(billing_base_url or "").strip()
    identity, api_key = _resolve_actor_key(
        principal=principal,
        routing=routing,
        store=store,
        credentials=credentials,
        profile_is_solely_owned=profile_is_solely_owned,
        billing_base_url=base_url,
        fingerprint_key=fingerprint_key,
        now_ms=now_ms,
    )
    today = datetime.fromtimestamp(now_ms / 1000, timezone.utc).date()
    period_start = today.isoformat()
    period_end = (today + timedelta(days=2)).isoformat()
    spend_key = hashlib.sha256(api_key.encode()).hexdigest()
    before = _collect_spend_rows(
        client, spend_key, period_start=period_start, period_end=period_end
    )
    return _SpendState(
        _principal=principal,
        _routing=routing,
        _store=store,
        _credentials=credentials,
        _client=client,
        _profile_is_solely_owned=profile_is_solely_owned,
        _run_id=run_id,
        _audience=audience,
        _billing_base_url=base_url,
        _receipt_signer=signing_key,
        _fingerprint_key=fingerprint_key,
        _api_key=api_key,
        _spend_key=spend_key,
        _identity=identity,
        _before=before,
        _period_start=period_start,
        _period_end=period_end,
        _started_at_ms=now_ms,
        _seal=_STATE_SEAL,
    )


def require_single_actor_spend_state(
    state: object,
    *,
    principal: TrustedRuntimePrincipal,
    run_id: str,
    audience: str,
) -> None:
    """Fail closed unless this is the live pre-model state for the routed run."""
    if (
        not isinstance(state, _SpendState)
        or state._seal is not _STATE_SEAL
        or state._used
        or state._principal is not principal
        or state._run_id != run_id
        or state._audience != audience
    ):
        raise SpendReceiptRejected("state_invalid")


def finish_single_actor_spend_receipt(
    state: object,
    *,
    run_id: str,
    audience: str,
    model_request_id: str = "",
    now_ms: int,
) -> dict[str, Any]:
    """Consume a begin state and prove exactly one new exact-key spend row."""
    if not isinstance(state, _SpendState) or state._seal is not _STATE_SEAL:
        raise SpendReceiptRejected("state_invalid")
    with state._lock:
        if state._used:
            raise SpendReceiptRejected("state_invalid")
        state._used = True
    if run_id != state._run_id:
        raise SpendReceiptRejected("state_run_mismatch")
    if audience != state._audience:
        raise SpendReceiptRejected("state_audience_mismatch")
    if (
        not isinstance(now_ms, int)
        or isinstance(now_ms, bool)
        or now_ms < state._started_at_ms
        or now_ms - state._started_at_ms >= _MAX_TTL_MS
    ):
        raise SpendReceiptRejected("state_stale")
    model_request_id = (
        _external_request_id(model_request_id) if model_request_id else ""
    )
    identity, current_key = _resolve_actor_key(
        principal=state._principal,
        routing=state._routing,
        store=state._store,
        credentials=state._credentials,
        profile_is_solely_owned=state._profile_is_solely_owned,
        billing_base_url=state._billing_base_url,
        fingerprint_key=state._fingerprint_key,
        now_ms=now_ms,
    )
    if identity != state._identity or not hmac.compare_digest(
        current_key, state._api_key
    ):
        raise SpendReceiptRejected("state_key_mismatch")
    deadline = time.monotonic() + _SPEND_POLL_TIMEOUT_S
    candidate: tuple[str, Decimal] | None = None
    while True:
        after = _collect_spend_rows(
            state._client,
            state._spend_key,
            period_start=state._period_start,
            period_end=state._period_end,
        )
        if any(
            request_id not in after or after[request_id] != spend
            for request_id, spend in state._before.items()
        ):
            raise SpendReceiptRejected("spend_snapshot_changed")
        new_request_ids = sorted(set(after) - set(state._before))
        delta = sum((after[request_id] for request_id in new_request_ids), Decimal())
        valid_candidate = (
            len(new_request_ids) == 1
            and (not model_request_id or new_request_ids == [model_request_id])
            and delta > 0
        )
        if len(new_request_ids) > 1 or (
            model_request_id
            and new_request_ids
            and new_request_ids != [model_request_id]
        ):
            raise SpendReceiptRejected("spend_delta_missing")
        if valid_candidate:
            current = (new_request_ids[0], delta)
            if candidate is None:
                candidate = current
                deadline = time.monotonic() + _SPEND_POLL_TIMEOUT_S
            elif current != candidate:
                raise SpendReceiptRejected("spend_snapshot_changed")
        elif candidate is not None:
            raise SpendReceiptRejected("spend_snapshot_changed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if candidate is None:
                raise SpendReceiptRejected("spend_delta_missing")
            break
        time.sleep(min(_SPEND_POLL_INTERVAL_S, remaining))
    actor_fp, profile_fp, key_fp = _principal_fingerprints(
        state._principal, state._api_key, state._fingerprint_key
    )
    expires_at = min(now_ms + _MAX_TTL_MS, int(state._identity.expires_at))
    receipt = {
        "version": 1,
        "audience": state._audience,
        "run_id": state._run_id,
        "actor_fingerprint": actor_fp,
        "profile_fingerprint": profile_fp,
        "billing_subject_fingerprint": _fingerprint(
            state._fingerprint_key,
            "billing-subject",
            state._identity.employee_user_id,
        ),
        "billing_key_fingerprint": key_fp,
        "request_id_fingerprints": [
            _fingerprint(state._fingerprint_key, "request", request_id)
            for request_id in new_request_ids
        ],
        "request_count": len(new_request_ids),
        "spend_delta": format(delta, "f"),
        "issued_at_ms": now_ms,
        "expires_at_ms": expires_at,
    }
    receipt["signature"] = base64.b64encode(
        state._receipt_signer.sign(_DOMAIN + _unsigned(receipt))
    ).decode()
    verify_single_actor_spend_receipt(
        receipt,
        public_key=state._receipt_signer.public_key(),
        expected_run_id=state._run_id,
        expected_audience=state._audience,
        expected_principal=state._principal,
        expected_api_key=state._api_key,
        fingerprint_key=state._fingerprint_key,
        now_ms=now_ms,
    )
    return receipt


def issue_single_actor_spend_receipt(
    *,
    principal: TrustedRuntimePrincipal,
    routing: Any,
    store: Any,
    credentials: Any,
    client: Any,
    model_call: Callable[[str], Any],
    profile_is_solely_owned: Callable[[str, str], bool],
    run_id: str,
    audience: str,
    billing_base_url: str,
    signing_key: Ed25519PrivateKey,
    fingerprint_key: bytes,
    now_ms: int,
) -> dict[str, Any]:
    """Compatibility wrapper for synchronous callers."""
    state = begin_single_actor_spend_receipt(
        principal=principal,
        routing=routing,
        store=store,
        credentials=credentials,
        client=client,
        profile_is_solely_owned=profile_is_solely_owned,
        run_id=run_id,
        audience=audience,
        billing_base_url=billing_base_url,
        signing_key=signing_key,
        fingerprint_key=fingerprint_key,
        now_ms=now_ms,
    )
    try:
        request_id = model_call(state._api_key)
    except Exception as exc:
        raise SpendReceiptRejected("model_call_failed") from exc
    return finish_single_actor_spend_receipt(
        state,
        run_id=run_id,
        audience=audience,
        model_request_id=str(request_id or ""),
        now_ms=now_ms,
    )


def verify_single_actor_spend_receipt(
    receipt: dict[str, Any],
    *,
    public_key: Ed25519PublicKey,
    expected_run_id: str,
    expected_audience: str,
    expected_principal: TrustedRuntimePrincipal,
    expected_api_key: str,
    fingerprint_key: bytes,
    now_ms: int,
) -> dict[str, Any]:
    """Verify freshness, signature, and caller-bound actor/profile/key."""
    expected_fields = {
        "version", "audience", "run_id", "actor_fingerprint",
        "profile_fingerprint", "billing_subject_fingerprint",
        "billing_key_fingerprint", "request_id_fingerprints", "request_count",
        "spend_delta", "issued_at_ms", "expires_at_ms", "signature",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_fields:
        raise SpendReceiptRejected("receipt_invalid")
    actor_fp, profile_fp, key_fp = _principal_fingerprints(
        expected_principal, expected_api_key, fingerprint_key
    )
    request_fps = receipt.get("request_id_fingerprints")
    try:
        issued = receipt["issued_at_ms"]
        expires = receipt["expires_at_ms"]
        request_count = receipt["request_count"]
        delta = _spend(receipt["spend_delta"])
    except (TypeError, ValueError, KeyError) as exc:
        raise SpendReceiptRejected("receipt_invalid") from exc
    if (
        not isinstance(public_key, Ed25519PublicKey)
        or receipt["version"] != 1
        or receipt["audience"] != expected_audience
        or receipt["run_id"] != expected_run_id
        or receipt["actor_fingerprint"] != actor_fp
        or receipt["profile_fingerprint"] != profile_fp
        or receipt["billing_key_fingerprint"] != key_fp
        or not isinstance(issued, int)
        or isinstance(issued, bool)
        or not isinstance(expires, int)
        or isinstance(expires, bool)
        or not isinstance(request_count, int)
        or isinstance(request_count, bool)
        or not _HEX_256.fullmatch(str(receipt["billing_subject_fingerprint"]))
        or not isinstance(request_fps, list)
        or request_count != len(request_fps)
        or request_count <= 0
        or len(set(request_fps)) != request_count
        or any(not _HEX_256.fullmatch(str(value)) for value in request_fps)
        or delta <= 0
        or issued <= 0
        or expires <= issued
        or expires - issued > _MAX_TTL_MS
        or now_ms < issued
        or now_ms >= expires
    ):
        raise SpendReceiptRejected("receipt_invalid")
    try:
        signature = base64.b64decode(str(receipt["signature"]), validate=True)
        public_key.verify(signature, _DOMAIN + _unsigned(receipt))
    except (ValueError, InvalidSignature) as exc:
        raise SpendReceiptRejected("receipt_signature_invalid") from exc
    return {key: value for key, value in receipt.items() if key != "signature"}
