import io
import json
import urllib.error

import pytest

from hermes_multitenancy.billing_employee_key import (
    EmployeeKeyClient,
    EmployeeKeyError,
)

BASE = "https://itsms.example.com"
TOKEN = "hks_test_token"
EMAIL = "sunke@example.com"
# Real shape observed in production 2026-08-06.
ALIAS = "auto-sunke-20260806-012059-f60f9a"


def _expiry_iso(days: int = 30) -> str:
    """A realistic expiry: the endpoint mints 30-day keys."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace(
        "+00:00", "Z"
    )


def _expiry_iso_hours(hours: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat().replace(
        "+00:00", "Z"
    )


def _body(**overrides):
    payload = {
        "api_key": "sk-test-plaintext",
        "base_url": "https://litellm.sre.example.com/v1",
        "key_alias": ALIAS,
        "team_alias": "技术平台部",
        "expires_at": _expiry_iso(),
        "litellm_user_id": "42552313-ee31-4132-b6e8-b56b927c3769",
        "team_id": "13b7bdf4-97f5-45e2-9c57-abc3ea3949aa",
        "account_identity_verified": True,
    }
    payload.update(overrides)
    return payload


class _Response:
    def __init__(self, body, status=200):
        self._raw = json.dumps(body).encode() if isinstance(body, dict) else body
        self.status = status

    def read(self, n=-1):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client(response, **kw):
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    client = EmployeeKeyClient(BASE, TOKEN, opener=opener, **kw)
    return client, calls


def _issue(client):
    return client.issue(
        employee_id="sunke", enterprise_email=EMAIL, idempotency_key="idem-12345678"
    )


def test_happy_path_normalizes_the_response():
    client, calls = _client(_Response(_body()))
    issued = _issue(client)

    assert issued.api_key == "sk-test-plaintext"
    assert issued.team_alias == "技术平台部"
    assert issued.key_alias == ALIAS
    assert issued.email == EMAIL
    # ISO -> epoch ms, the unit the vault stores.
    assert isinstance(issued.expires_at_ms, int)
    assert issued.expires_at_ms > 1_000_000_000_000

    request = calls[0]
    assert request.full_url == BASE + "/internal/v1/employee/key"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("X-trusted-employee-id") == "sunke"
    assert request.data == b""  # body stays empty: identity is header-only


@pytest.mark.parametrize(
    "status,retryable",
    [(401, False), (403, False), (503, True), (500, True)],
)
def test_non_200_never_yields_a_credential(status, retryable):
    error = urllib.error.HTTPError(
        BASE, status, "nope", {}, io.BytesIO(b'{"error":{"code":"x"}}')
    )
    client, _ = _client(error)
    with pytest.raises(EmployeeKeyError) as excinfo:
        _issue(client)
    assert excinfo.value.code == "employee_key_rejected"
    assert excinfo.value.status == status
    assert excinfo.value.retryable is retryable


def test_transport_failure_is_retryable_and_yields_nothing():
    client, _ = _client(OSError("connection reset"))
    with pytest.raises(EmployeeKeyError) as excinfo:
        _issue(client)
    assert excinfo.value.code == "employee_key_unreachable"
    assert excinfo.value.retryable is True


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"api_key": ""}, "employee_key_response_missing_api_key"),
        ({"base_url": "http://litellm.example.com/v1"},
         "employee_key_response_base_url_invalid"),
        ({"key_alias": ""}, "employee_key_response_key_alias_invalid"),
        ({"team_alias": ""}, "employee_key_response_team_alias_missing"),
        ({"expires_at": ""}, "employee_key_expires_at_missing"),
        ({"expires_at": "not-a-date"}, "employee_key_expires_at_invalid"),
        ({"expires_at": "2036-09-04T17:21:07"}, "employee_key_expires_at_naive"),
        ({"expires_at": _expiry_iso(3650)},
         "employee_key_response_lifetime_implausible"),
        ({"expires_at": _expiry_iso(0)},
         "employee_key_response_already_expired"),
        ({"expires_at": "2020-01-01T00:00:00Z"},
         "employee_key_response_already_expired"),
        ({"litellm_user_id": ""}, "employee_key_response_account_identity_missing"),
        ({"team_id": ""}, "employee_key_response_account_identity_missing"),
    ],
)
def test_malformed_response_is_refused_not_coerced(overrides, expected):
    """A missing/unparseable field must raise, never default to 0 — a stored
    zero expiry reads as already-expired and silently denies the employee."""
    client, _ = _client(_Response(_body(**overrides)))
    with pytest.raises(EmployeeKeyError) as excinfo:
        _issue(client)
    assert excinfo.value.code == expected


def test_alias_naming_another_employee_is_refused():
    """The only structural link between credential and subject, absent
    litellm_user_id. A key minted for someone else must not be stored."""
    client, _ = _client(_Response(_body(key_alias="auto-caowenrui-20260806-01-aaa")))
    with pytest.raises(EmployeeKeyError) as excinfo:
        _issue(client)
    assert excinfo.value.code == "employee_key_response_subject_mismatch"


def test_plaintext_endpoint_is_refused_before_the_token_goes_out():
    client, calls = _client(_Response(_body()))
    client._base_url = "http://itsms.example.com"
    with pytest.raises(EmployeeKeyError) as excinfo:
        _issue(client)
    assert excinfo.value.code == "employee_key_endpoint_not_https"
    assert calls == [], "must fail before any request is made"


def test_missing_token_is_refused_before_the_request():
    client, calls = _client(_Response(_body()))
    client._token = ""
    with pytest.raises(EmployeeKeyError):
        _issue(client)
    assert calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"employee_id": "bad id", "enterprise_email": EMAIL, "idempotency_key": "idem-12345678"},
        {"employee_id": "sunke", "enterprise_email": "not-an-email", "idempotency_key": "idem-12345678"},
        {"employee_id": "sunke", "enterprise_email": EMAIL, "idempotency_key": "short"},
    ],
)
def test_bad_arguments_never_reach_the_gateway(kwargs):
    client, calls = _client(_Response(_body()))
    with pytest.raises(EmployeeKeyError):
        client.issue(**kwargs)
    assert calls == []


def test_non_json_body_is_refused():
    client, _ = _client(_Response(b"<html>403</html>"))
    with pytest.raises(EmployeeKeyError) as excinfo:
        _issue(client)
    assert excinfo.value.code == "employee_key_response_not_json"


def test_lifetime_below_the_refresh_lead_window_is_refused():
    """codex r2: the old 1-hour floor let a 2-hour key through — shorter than
    the 1-day refresh lead, so it is due for replacement the instant it is
    issued. Every sweep pass would re-mint it, and any pass that falls a day
    behind leaves the employee with nothing. No test pinned this boundary at
    all before now (only the 3650-day MAX case was covered)."""
    client, _ = _client(_Response(_body(expires_at=_expiry_iso_hours(2))))
    with pytest.raises(EmployeeKeyError) as excinfo:
        _issue(client)
    assert excinfo.value.code == "employee_key_response_lifetime_implausible"


def test_min_lifetime_clears_the_refresh_lead_window_with_margin():
    """The floor is meaningless as a number alone — pin the RELATIONSHIP so a
    future edit that raises _REFRESH_LEAD_MS without raising _MIN_LIFETIME_MS
    to match reintroduces the same gap."""
    from hermes_multitenancy.billing_employee_key import (
        _MIN_LIFETIME_MS,
        _REFRESH_LEAD_MS,
    )

    assert _MIN_LIFETIME_MS > _REFRESH_LEAD_MS


def test_rotation_returns_a_distinct_credential():
    """Two calls = two keys; the endpoint never returns a cached one."""
    first = _Response(_body())
    second = _Response(_body(key_alias="auto-sunke-20260806-012200-bc6658",
                             api_key="sk-test-second"))
    responses = [first, second]

    def opener(request, timeout=None):
        return responses.pop(0)

    client = EmployeeKeyClient(BASE, TOKEN, opener=opener)
    a = client.issue(employee_id="sunke", enterprise_email=EMAIL,
                     idempotency_key="idem-first-0001")
    b = client.issue(employee_id="sunke", enterprise_email=EMAIL,
                     idempotency_key="idem-second-002")
    assert a.api_key != b.api_key
    assert a.key_alias != b.key_alias


# ------------------------------------------------ drift + vault payload

from hermes_multitenancy.billing_employee_key import (  # noqa: E402
    AccountDriftError,
    check_account_drift,
    next_credential_version,
    to_vault_payload,
)

USER_A = "42552313-ee31-4132-b6e8-b56b927c3769"
USER_B = "99999999-0000-0000-0000-000000000000"


def _issued(**over):
    client, _ = _client(_Response(_body(**over)))
    return _issue(client)


def test_same_account_is_not_drift():
    stored = {"litellm_user_id": USER_A, "account_identity_verified": True}
    check_account_drift(_issued(), stored)  # must not raise


def test_different_account_is_drift():
    stored = {"litellm_user_id": USER_B, "account_identity_verified": True}
    with pytest.raises(AccountDriftError):
        check_account_drift(_issued(), stored)


def test_department_transfer_changes_team_but_is_not_drift():
    """team_id is ROUTING: every issuance re-resolves the CURRENT department,
    so a transfer legitimately changes it. Comparing it as an invariant would
    deny a valid credential to everyone who ever moves teams."""
    stored = {
        "litellm_user_id": USER_A,
        "team_id": "old-team-id",
        "account_identity_verified": True,
    }
    issued = _issued(team_id="brand-new-team-id", team_alias="新部门")
    check_account_drift(issued, stored)  # must not raise
    assert issued.team_id != stored["team_id"]


@pytest.mark.parametrize(
    "issued_over,stored",
    [
        # gateway could not verify THIS issuance
        ({"account_identity_verified": False},
         {"litellm_user_id": USER_B, "account_identity_verified": True}),
        # the STORED row was never verified either
        ({}, {"litellm_user_id": USER_B, "account_identity_verified": False}),
    ],
)
def test_unverified_ids_are_never_used_as_a_drift_anchor(issued_over, stored):
    """Comparing two values nobody checked proves nothing — acting on them is
    the same 'verifying our own intent' failure the fields exist to prevent."""
    check_account_drift(_issued(**issued_over), stored)  # must not raise


def test_first_issuance_has_nothing_to_compare():
    check_account_drift(_issued(), None)


def test_vault_payload_satisfies_the_stored_contract():
    payload = to_vault_payload(_issued(), profile_name="sunke", credential_version=1)
    required = (
        "employee_id", "profile_name", "enterprise_email", "litellm_user_id",
        "team_id", "team_alias", "key_id", "key_alias", "credential_version",
        "expires_at", "api_key",
    )
    assert all(payload.get(k) for k in required), payload
    assert payload["contract_version"].split(".", 1)[0] == "1"
    # expires_at must be the epoch-ms int the vault indexes on, not a string
    assert isinstance(payload["expires_at"], int)
    # no separate key id exists on this endpoint; the alias is the handle
    assert payload["key_id"] == payload["key_alias"]


@pytest.mark.parametrize("bad", [0, -1])
def test_vault_payload_refuses_a_nonsense_version(bad):
    with pytest.raises(EmployeeKeyError):
        to_vault_payload(_issued(), profile_name="sunke", credential_version=bad)


def test_vault_payload_refuses_a_missing_profile():
    with pytest.raises(EmployeeKeyError):
        to_vault_payload(_issued(), profile_name="", credential_version=1)


@pytest.mark.parametrize(
    "stored,expected",
    [(None, 1), ({}, 1), ({"credential_version": 1}, 2),
     ({"credential_version": 7}, 8), ({"credential_version": "junk"}, 1)],
)
def test_credential_version_increments_locally(stored, expected):
    assert next_credential_version(stored) == expected


# ------------------------------------------- storage: adopt_employee_key

_VAULT_KEY = "employee-key-adopt-test"
# Anchored to real now: the response fixture computes its expiry from real
# time too, and a fake clock in a different era reads fresh keys as expired.
_NOW_MS = int(__import__("time").time() * 1000)


def _manager(tmp_path):
    from hermes_multitenancy.billing_identity import BillingCredentialManager
    from hermes_multitenancy.credentials import CredentialStore

    return BillingCredentialManager(
        vault=CredentialStore(tmp_path / "vault.db", encryption_key=_VAULT_KEY),
        gateway=None,  # this path never calls ensure/ack
        model_base_url="https://litellm.example/v1",
        now_ms=lambda: _NOW_MS,
        probe=lambda _key: None,
    )


def _payer(employee_id="sunke"):
    from hermes_multitenancy.billing_identity import _ResolvedPayer

    return _ResolvedPayer(employee_id, employee_id, f"{employee_id}@example.com", "FD")


def _metadata(binding):
    """What a run carries. runtime_api_key re-checks the WHOLE tuple against
    the stored row, so a caller must thread all of it — not just the ids."""
    return {
        "litellm_billing_employee_user_id": binding.employee_user_id,
        "litellm_billing_profile_name": binding.profile_name,
        "litellm_billing_user_id": binding.litellm_user_id,
        "litellm_billing_team_id": binding.team_id,
        "litellm_billing_key_id": binding.key_id,
        "litellm_billing_credential_version": binding.credential_version,
    }


def test_adopted_key_is_stored_and_retrievable_at_runtime(tmp_path):
    """The Done line: stored as a usable binding, and runtime_api_key returns it."""
    manager = _manager(tmp_path)
    issued = _issued()

    binding = manager.adopt_employee_key(_payer(), issued)

    assert binding.employee_user_id == "sunke"
    assert binding.litellm_user_id == USER_A
    assert binding.credential_version == 1
    assert binding.migration_state == "enforced"
    assert manager.runtime_api_key(_metadata(binding)) == issued.api_key


def test_rotation_bumps_the_version_and_replaces_the_live_key(tmp_path):
    manager = _manager(tmp_path)
    first = _issued()
    manager.adopt_employee_key(_payer(), first)

    second = _issued(key_alias="auto-sunke-20260806-012200-bc6658",
                     api_key="sk-rotated")
    binding = manager.adopt_employee_key(_payer(), second)

    assert binding.credential_version == 2
    assert manager.runtime_api_key(_metadata(binding)) == "sk-rotated"


def test_account_drift_is_refused_and_leaves_the_old_key_live(tmp_path):
    """Fail-closed must mean the PREVIOUS credential survives untouched — a
    half-landed rejection would deny the employee a key that still works."""
    from hermes_multitenancy.run_broker import RunRejected

    manager = _manager(tmp_path)
    original = manager.adopt_employee_key(_payer(), _issued())

    drifted = _issued(litellm_user_id=USER_B,
                      key_alias="auto-sunke-20260806-013000-cccccc",
                      api_key="sk-from-another-account")
    with pytest.raises(RunRejected):
        manager.adopt_employee_key(_payer(), drifted)

    # untouched: still the original key, still version 1
    assert manager.runtime_api_key(_metadata(original)) == "sk-test-plaintext"


def test_department_transfer_is_adopted_not_rejected(tmp_path):
    """The false-alarm case: same person, same account, new team."""
    manager = _manager(tmp_path)
    manager.adopt_employee_key(_payer(), _issued())

    moved = _issued(team_id="new-team-uuid", team_alias="新部门",
                    key_alias="auto-sunke-20260806-014000-dddddd",
                    api_key="sk-after-transfer")
    binding = manager.adopt_employee_key(_payer(), moved)

    assert binding.team_alias == "新部门"
    assert manager.runtime_api_key(_metadata(binding)) == "sk-after-transfer"


def test_unusable_key_fails_probe_and_never_overwrites_the_live_key(tmp_path):
    """codex r2: adopt_employee_key never validated the new key against
    LiteLLM before storing it — a syntactically fine but unusable key (wrong
    cluster, revoked, whatever the gateway got wrong on its side) would
    immediately overwrite the sole valid vault row with no probe and no
    fallback. Probe it first; a probe failure must leave the previous
    credential exactly as it was."""
    from hermes_multitenancy.run_broker import RunRejected

    probed: list[str] = []

    def _refusing_probe(api_key):
        probed.append(api_key)
        raise RunRejected("LiteLLM said no")

    manager = _manager(tmp_path)
    original = manager.adopt_employee_key(_payer(), _issued())  # default probe passes

    manager._probe = _refusing_probe
    bad = _issued(key_alias="auto-sunke-20260806-020000-eeeeee", api_key="sk-unusable")
    with pytest.raises(RunRejected):
        manager.adopt_employee_key(_payer(), bad)

    assert probed == ["sk-unusable"], "the NEW key must be what gets probed"
    assert manager.runtime_api_key(_metadata(original)) == "sk-test-plaintext"
    assert manager.employee_key_needed(_payer()) is False  # old key is still current


def test_probe_never_replays_the_bearer_on_a_redirect(tmp_path, monkeypatch):
    """codex r3 (P0 recurrence): _probe_key (the DEFAULT probe used by
    adopt_employee_key and ensure_available alike) called plain
    urllib.request.urlopen — the exact same class of leak ecc9b16 fixed on
    EmployeeKeyClient._post, just on a different code path that r2's fix
    never touched. A 302 from the configured LiteLLM endpoint would replay
    this payer's live billing key on whatever the redirect points to.
    Verified against a live loopback redirector, not a mock."""
    from hermes_multitenancy import billing_credentials as bc
    from hermes_multitenancy.billing_identity import BillingCredentialManager
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.run_broker import RunRejected

    # _allowed_billing_endpoint requires https on both sides — a separate,
    # already-tested guard (test_http_configured_probe_never_puts_key_on_the_wire).
    # Bypass ONLY that guard so this test isolates the redirect-following
    # behaviour of the opener itself, on a plain-http loopback pair.
    monkeypatch.setattr(bc, "_allowed_billing_endpoint", lambda *_a, **_k: True)

    src, sink, received = _redirect_loopback_pair()
    try:
        manager = BillingCredentialManager(
            vault=CredentialStore(tmp_path / "v.db", encryption_key=_VAULT_KEY),
            gateway=None,
            model_base_url=f"http://127.0.0.1:{src.server_port}",
            now_ms=lambda: _NOW_MS,
        )
        with pytest.raises(RunRejected):
            manager._probe_key("sk-redirect-secret")
        assert received.get("auth") is None, "the bearer reached another origin"
    finally:
        sink.shutdown()
        src.shutdown()


def test_store_binding_rolls_back_the_vault_when_identity_write_fails(tmp_path):
    """codex r2: the r1 'vault first' reorder does not make the two writes
    atomic — it only made the FIRST failure look harmless. Fail the identity
    write on a SECOND call (e.g. a department transfer) and the pre-fix code
    left the vault on the fresh key while BillingIdentityStore stayed on the
    old team: the request path keeps rejecting the new team,
    `employee_key_needed` reads the fresh vault key as current, and the
    sweep skips the employee forever — a split brain nothing self-heals.
    After a failed store_binding, the vault must read exactly as it did
    immediately before the call."""
    from hermes_multitenancy.billing_employee_key import store_binding

    class _FailingStore:
        def __init__(self, fail_on):
            self._fail_on = fail_on
            self.calls = 0

        def put(self, binding):
            self.calls += 1
            if self.calls == self._fail_on:
                raise RuntimeError("identity store down")

    class _Preparer:
        def __init__(self, credentials, store):
            self._credentials = credentials
            self._store = store

    manager = _manager(tmp_path)
    store = _FailingStore(fail_on=2)
    preparer = _Preparer(manager, store)

    store_binding(preparer, _payer(), _issued())  # call 1: both stores agree

    moved = _issued(
        team_id="new-team-uuid", team_alias="新部门",
        key_alias="auto-sunke-20260806-014000-dddddd", api_key="sk-after-transfer",
    )
    with pytest.raises(RuntimeError):
        store_binding(preparer, _payer(), moved)  # call 2: identity write fails

    payload = manager._load_payload(_payer().profile_name, _payer().employee_user_id)
    assert payload["team_alias"] == "技术平台部", "vault must not be left on the moved team"
    assert payload["api_key"] == "sk-test-plaintext"
    assert manager.employee_key_needed(_payer()) is False  # unchanged: still the old, fresh key


def test_credential_filed_under_another_subject_is_refused(tmp_path):
    """Belt and braces over the client-side alias check: the store refuses a
    credential whose employee/email do not match the payer it is filed under."""
    from hermes_multitenancy.run_broker import RunRejected

    manager = _manager(tmp_path)
    with pytest.raises(RunRejected):
        manager.adopt_employee_key(_payer("bob"), _issued())  # issued for sunke


def test_adopt_layer_refuses_a_below_floor_key_even_when_hand_built(tmp_path):
    """codex r3 #3: the min-lifetime floor (raised in commit 195f1ce) only
    lived in EmployeeKeyClient._validate — the HTTP response boundary.
    adopt_employee_key (the actual vault-write boundary) trusted whatever
    IssuedKey it was handed and never re-checked. codex built a 2-hour
    IssuedKey directly — bypassing the client entirely — and called
    adopt_employee_key straight: it landed in the vault, and
    employee_key_needed was True the instant it was stored (inside its own
    refresh window from the moment it was issued)."""
    from hermes_multitenancy.billing_employee_key import IssuedKey
    from hermes_multitenancy.run_broker import RunRejected

    manager = _manager(tmp_path)
    original = manager.adopt_employee_key(_payer(), _issued())

    short = IssuedKey(
        employee_id="sunke", email="sunke@example.com", api_key="sk-2-hour",
        base_url="https://litellm.example/v1",
        key_alias="auto-sunke-20260806-050000-short", team_alias="技术平台部",
        expires_at_ms=_NOW_MS + 2 * 60 * 60 * 1000,  # 2 hours: below the floor
        litellm_user_id=USER_A, team_id="13b7bdf4-97f5-45e2-9c57-abc3ea3949aa",
        account_identity_verified=True,
    )
    with pytest.raises(RunRejected):
        manager.adopt_employee_key(_payer(), short)

    assert manager.runtime_api_key(_metadata(original)) == "sk-test-plaintext"
    assert manager.employee_key_needed(_payer()) is False  # old key still current


# ------------------------------------------------ refresh policy (expiry-1d)

_DAY = 24 * 60 * 60 * 1000


@pytest.mark.parametrize(
    "remaining_ms,expected",
    [
        (2 * _DAY, False),        # comfortably alive — leave it alone
        (_DAY + 60_000, False),   # just outside the lead window
        (_DAY, True),             # exactly at the lead window
        (_DAY // 2, True),        # inside it
        (0, True),                # expired
        (-_DAY, True),            # long expired
    ],
)
def test_refresh_fires_one_day_before_expiry(remaining_ms, expected):
    now = 1_800_000_000_000
    stored = {"expires_at": now + remaining_ms}
    from hermes_multitenancy.billing_employee_key import needs_new_key

    assert needs_new_key(stored, now) is expected


@pytest.mark.parametrize(
    "stored",
    [None, {}, {"invalid": True, "expires_at": 9_999_999_999_999},
     {"expires_at": "not-a-number"}],
)
def test_missing_invalid_or_unreadable_always_needs_a_key(stored):
    from hermes_multitenancy.billing_employee_key import needs_new_key

    assert needs_new_key(stored, 1_800_000_000_000) is True


def test_manager_reports_refresh_needed_only_inside_the_window(tmp_path):
    """Same decision through the manager, against a real stored credential."""
    clock = {"now": _NOW_MS}
    from hermes_multitenancy.billing_identity import BillingCredentialManager
    from hermes_multitenancy.credentials import CredentialStore

    manager = BillingCredentialManager(
        vault=CredentialStore(tmp_path / "v.db", encryption_key=_VAULT_KEY),
        gateway=None,
        model_base_url="https://litellm.example/v1",
        now_ms=lambda: clock["now"],
        probe=lambda _key: None,
    )
    assert manager.employee_key_needed(_payer()) is True  # nothing stored yet

    issued = _issued()
    manager.adopt_employee_key(_payer(), issued)
    assert manager.employee_key_needed(_payer()) is False  # fresh

    clock["now"] = issued.expires_at_ms - 2 * _DAY
    assert manager.employee_key_needed(_payer()) is False  # still not due
    clock["now"] = issued.expires_at_ms - _DAY + 1
    assert manager.employee_key_needed(_payer()) is True   # due


def test_failed_refresh_leaves_the_still_valid_key_serving(tmp_path):
    """Refresh is an optimisation, not a gate. If the gateway call fails, the
    stored credential must keep working — 'refresh early' must never become
    'deny early'."""
    from hermes_multitenancy.run_broker import RunRejected

    clock = {"now": _NOW_MS}
    from hermes_multitenancy.billing_identity import BillingCredentialManager
    from hermes_multitenancy.credentials import CredentialStore

    manager = BillingCredentialManager(
        vault=CredentialStore(tmp_path / "v.db", encryption_key=_VAULT_KEY),
        gateway=None,
        model_base_url="https://litellm.example/v1",
        now_ms=lambda: clock["now"],
        probe=lambda _key: None,
    )
    binding = manager.adopt_employee_key(_payer(), _issued())

    # Enter the refresh window; the gateway is down, so no new IssuedKey arrives.
    clock["now"] = binding.expires_at - _DAY + 1
    assert manager.employee_key_needed(_payer()) is True
    client, _ = _client(OSError("gateway down"))
    with pytest.raises(EmployeeKeyError):
        _issue(client)

    # Nothing was written, so the old credential still serves.
    assert manager.runtime_api_key(_metadata(binding)) == "sk-test-plaintext"


# ------------------------------------------- cohort sweep (timer-driven)

from hermes_multitenancy.billing_employee_key import sweep_cohort  # noqa: E402


def _sweep(cohort, needs, issue, **kw):
    stored = []
    r = sweep_cohort(
        cohort,
        needs=needs,
        issue=issue,
        store=lambda m, i: stored.append((m, i)),
        **kw,
    )
    return r, stored


def test_sweep_only_touches_members_that_need_a_key():
    due = {"b", "d"}
    r, stored = _sweep(list("abcde"), lambda m: m in due, lambda m: f"key-{m}")
    assert r.checked == 5
    assert r.issued == 2
    assert r.skipped_current == 3
    assert [m for m, _ in stored] == ["b", "d"]


def test_one_members_failure_never_aborts_the_sweep():
    """The point of a sweep is the other 1281 — a single bad member must not
    take the pass down with it."""

    def issue(m):
        if m == "c":
            raise RuntimeError("gateway said no")
        return f"key-{m}"

    r, stored = _sweep(list("abcde"), lambda m: True, issue)
    assert r.issued == 4
    assert r.failed == 1
    assert ("c", "RuntimeError") in r.failures
    assert [m for m, _ in stored] == ["a", "b", "d", "e"]


def test_first_run_after_switch_on_is_paced_not_a_burst():
    """Everybody needs a key at once when a cohort is switched on. The pass
    takes a bounded bite and leaves the rest for the next one."""
    cohort = [f"emp{i}" for i in range(50)]
    r, stored = _sweep(cohort, lambda m: True, lambda m: f"key-{m}", max_issues=20)
    assert r.issued == 20
    assert r.deferred_budget == 30
    assert len(stored) == 20
    assert r.checked == 50  # still inspected everyone


def test_a_second_pass_picks_up_where_the_first_left_off():
    cohort = [f"emp{i}" for i in range(50)]
    done: set[str] = set()

    def needs(m):
        return m not in done

    def issue(m):
        done.add(m)
        return f"key-{m}"

    first, _ = _sweep(cohort, needs, issue, max_issues=20)
    second, _ = _sweep(cohort, needs, issue, max_issues=20)
    third, _ = _sweep(cohort, needs, issue, max_issues=20)
    assert (first.issued, second.issued, third.issued) == (20, 20, 10)
    assert third.deferred_budget == 0
    assert len(done) == 50


def test_a_storage_failure_counts_as_failed_not_issued():
    """Minted-but-unstored is the orphan case; it must not read as success."""
    r = sweep_cohort(
        ["a", "b"],
        needs=lambda m: True,
        issue=lambda m: f"key-{m}",
        store=lambda m, i: (_ for _ in ()).throw(RuntimeError("vault down")),
    )
    assert r.issued == 0
    assert r.failed == 2


def test_a_quiet_pass_reports_nothing_to_do():
    r, stored = _sweep(list("abc"), lambda m: False, lambda m: "never")
    assert (r.issued, r.failed, r.skipped_current) == (0, 0, 3)
    assert stored == []
    assert "issued=0" in r.summary()


# ------------------------------------------- refresh CLI: dry-run must be dry


def test_dry_run_mints_nothing(monkeypatch, tmp_path):
    """The gateway ships a command called dry-run that really provisions and
    notifies. Ours must not repeat that: --dry-run reports and mints nothing."""
    import sqlite3

    from hermes_multitenancy import billing_employee_key as bek

    db = tmp_path / "routing.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE multitenancy_routing (user_id TEXT, profile_name TEXT, "
        "active INTEGER, kind TEXT, provenance TEXT)"
    )
    conn.execute(
        "INSERT INTO multitenancy_routing VALUES ('sunke','sunke',1,'user','sync')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "sunke")
    monkeypatch.setenv("HERMES_MULTITENANCY_DB", str(db))
    monkeypatch.setenv("HERMES_EMPLOYEE_KEY_SILENT_TOKEN", "tok")
    monkeypatch.setenv("HERMES_EMPLOYEE_KEY_BASE_URL", "https://gw.example")
    # run_refresh now needs the vault key in BOTH modes (see guard).
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-vault-key")

    minted: list[str] = []

    class _NeverMints:
        def issue(self, **kw):
            minted.append(kw["employee_id"])
            raise AssertionError("dry-run must not mint")

    monkeypatch.setattr(bek, "EmployeeKeyClient", lambda *a, **k: _NeverMints())

    class _Creds:
        def employee_key_needed(self, payer):
            return True

    monkeypatch.setattr(
        bek, "sweep_cohort",  # a dry run must never reach the sweep
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no sweep in dry-run")),
    )
    import hermes_multitenancy.billing_identity as bi

    monkeypatch.setattr(
        bi, "_default_preparer", lambda: type("P", (), {"_credentials": _Creds()})()
    )

    out = bek.run_refresh(dry_run=True)

    assert out["dry_run"] is True
    assert out["would_issue"] == ["sunke"]
    assert minted == [], "dry-run minted a key"


def test_cohort_member_missing_from_routing_is_reported_not_guessed(
    monkeypatch, tmp_path
):
    """Filing a key under a profile that does not exist is worse than skipping."""
    import sqlite3

    from hermes_multitenancy import billing_employee_key as bek

    db = tmp_path / "routing.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE multitenancy_routing (user_id TEXT, profile_name TEXT, "
        "active INTEGER, kind TEXT, provenance TEXT)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "ghost")
    monkeypatch.setenv("HERMES_MULTITENANCY_DB", str(db))
    monkeypatch.setenv("HERMES_EMPLOYEE_KEY_SILENT_TOKEN", "tok")
    monkeypatch.setenv("HERMES_EMPLOYEE_KEY_BASE_URL", "https://gw.example")
    # run_refresh now needs the vault key in BOTH modes (see guard).
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-vault-key")
    import hermes_multitenancy.billing_identity as bi

    monkeypatch.setattr(
        bi, "_default_preparer",
        lambda: type("P", (), {"_credentials": type("C", (), {
            "employee_key_needed": lambda self, p: True})()})(),
    )

    out = bek.run_refresh(dry_run=True)
    assert out["unrouted"] == ["ghost"]
    assert out["would_issue"] == []


def test_synthetic_ou_id_in_cohort_is_rejected_not_swept(monkeypatch, tmp_path):
    """codex r2: the sweep queries multitenancy_routing directly and never
    ran the canonical-subject guard the live request path enforces via
    `_employee_row` (regex shape + never an `ou_*` Feishu open_id). A
    synthetic `ou_*` row — injected via a sync bug or a typo'd
    HERMES_LITELLM_BILLING_PAYER_IDS — sailed straight through the sweep even
    though the normal request path would refuse to ever resolve it as a
    payer. Reject it the same way an unrouted id is reported."""
    import sqlite3

    from hermes_multitenancy import billing_employee_key as bek

    db = tmp_path / "routing.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE multitenancy_routing (user_id TEXT, profile_name TEXT, "
        "active INTEGER, kind TEXT, provenance TEXT)"
    )
    conn.execute(
        "INSERT INTO multitenancy_routing VALUES "
        "('ou_synthetic','ou_synthetic',1,'user','sync')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "ou_synthetic")
    monkeypatch.setenv("HERMES_MULTITENANCY_DB", str(db))
    monkeypatch.setenv("HERMES_EMPLOYEE_KEY_SILENT_TOKEN", "tok")
    monkeypatch.setenv("HERMES_EMPLOYEE_KEY_BASE_URL", "https://gw.example")
    # run_refresh now needs the vault key in BOTH modes (see guard).
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-vault-key")
    import hermes_multitenancy.billing_identity as bi

    monkeypatch.setattr(
        bi, "_default_preparer",
        lambda: type("P", (), {"_credentials": type("C", (), {
            "employee_key_needed": lambda self, p: True})()})(),
    )

    out = bek.run_refresh(dry_run=True)
    assert out["would_issue"] == [], "an ou_* row must never reach would_issue"
    assert out["unrouted"] == ["ou_synthetic"]


@pytest.mark.parametrize(
    "alias_owner,asked_for",
    [("sunke", "sun"), ("sun", "sunke")],
)
def test_prefix_colliding_employee_ids_cannot_borrow_each_others_key(
    alias_owner, asked_for
):
    """`auto-sunke-...` must not satisfy a request for `sun`. The trailing
    hyphen in the prefix is what makes that true — drop it and one employee's
    credential silently passes the subject check for another."""
    alias = f"auto-{alias_owner}-20260806-012059-f60f9a"
    client, _ = _client(_Response(_body(key_alias=alias)))
    with pytest.raises(EmployeeKeyError) as excinfo:
        client.issue(
            employee_id=asked_for,
            enterprise_email=f"{asked_for}@example.com",
            idempotency_key="prefix-collision-01",
        )
    assert excinfo.value.code == "employee_key_response_subject_mismatch"


def _redirect_loopback_pair():
    """Two loopback HTTP servers: `src` answers every GET or POST with a 302
    to `sink`. Returns (src, sink, received); received['auth'] is set to
    whatever Authorization header sink actually got, or stays absent if the
    redirect was correctly refused. Caller must shutdown() both servers.

    Both verbs on both servers: a redirector that only implements do_POST
    silently 501s a GET probe instead of ever redirecting it, which makes a
    GET-based caller's test pass whether or not the redirect is followed —
    exactly the kind of decorative pass this helper exists to prevent."""
    import http.server
    import threading

    received: dict[str, str | None] = {}

    class Sink(http.server.BaseHTTPRequestHandler):
        def _capture(self):
            received["auth"] = self.headers.get("Authorization")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        do_GET = do_POST = _capture

        def log_message(self, *a):
            pass

    sink = http.server.HTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()

    class Redirector(http.server.BaseHTTPRequestHandler):
        def _redirect(self):
            body = b"{}"
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/stolen")
            # A bodyless 302 relies on connection-close to signal end-of-body
            # (BaseHTTPRequestHandler defaults to HTTP/1.0). A caller that
            # refuses the redirect and then reads the 302's own response body
            # (as _post's HTTPError handler does, to surface it in the error)
            # races that close — intermittent ConnectionResetError instead of
            # a clean empty read, flaking the test independent of whichever
            # opener is under test. An explicit Content-Length makes the read
            # deterministic.
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _redirect

        def log_message(self, *a):
            pass

    src = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    threading.Thread(target=src.serve_forever, daemon=True).start()
    return src, sink, received


def test_a_redirect_never_replays_the_bearer_on_another_origin():
    """urllib follows 3xx by default AND replays Authorization on the new
    origin, including https->http. This bearer mints keys for ANY employee, so
    one redirect would hand it to somebody else in cleartext. Verified against
    a live loopback redirector, not a mock."""
    import urllib.error
    import urllib.request

    from hermes_multitenancy.billing_employee_key import _NO_REDIRECT_OPENER

    src, sink, received = _redirect_loopback_pair()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{src.server_port}/x",
            data=b"", method="POST",
            headers={"Authorization": "Bearer minting-token"},
        )
        with pytest.raises(urllib.error.HTTPError):
            _NO_REDIRECT_OPENER.open(request, timeout=5)
        assert received.get("auth") is None, "the bearer reached another origin"
    finally:
        sink.shutdown()
        src.shutdown()


def test_default_client_construction_actually_refuses_redirects():
    """codex r2 (decorative test): every OTHER redirect test either drives
    _NO_REDIRECT_OPENER directly, or builds EmployeeKeyClient through the
    `_client()` helper, which always injects its own opener= kwarg. None of
    them exercise the constructor's OWN wiring — `self._opener = opener or
    _NO_REDIRECT_OPENER.open`. Deleting that fallback in favour of a plain
    urllib.request.urlopen would leave every other test in this file green.
    Build the client the way production does — no opener kwarg — and drive
    it against a live redirecting loopback pair."""
    from hermes_multitenancy.billing_employee_key import EmployeeKeyClient

    src, sink, received = _redirect_loopback_pair()
    try:
        client = EmployeeKeyClient(BASE, TOKEN)  # no opener= : production wiring
        client._base_url = f"http://127.0.0.1:{src.server_port}"  # reach the loopback
        with pytest.raises(EmployeeKeyError) as excinfo:
            client._post("employee-x", "idem-redirect-wiring-01")
        assert excinfo.value.code == "employee_key_rejected"
        assert excinfo.value.status == 302
        assert received.get("auth") is None, "the bearer reached another origin"
    finally:
        sink.shutdown()
        src.shutdown()


def _drive_employee_key_transport(monkeypatch, src_port):
    from hermes_multitenancy.billing_employee_key import EmployeeKeyClient

    client = EmployeeKeyClient(BASE, TOKEN)  # default construction, no opener=
    client._base_url = f"http://127.0.0.1:{src_port}"
    # _post has no scheme guard of its own (that lives one layer up, in
    # .issue()), so calling it directly already reaches the opener — no
    # bypass needed on this transport. `pytest.raises` here is NOT the
    # discriminator (a redirect that WAS followed and hit `sink` also raises
    # EmployeeKeyError, just for "not JSON" reasons — sink is not a real
    # employee/key endpoint) — the discriminator is `received['auth']` back
    # in the caller. This just keeps the call from erroring loudly.
    with pytest.raises(EmployeeKeyError):
        client._post("employee-x", "idem-crosstransport-01")


def _drive_legacy_ensure_transport(monkeypatch, src_port):
    from hermes_multitenancy import billing_credentials as bc
    from urllib.parse import urlparse as real_urlparse

    # Unlike EmployeeKeyClient, BillingGatewayClient._post has its OWN
    # https-only pre-flight gate inline (checked separately by
    # test_gateway_rejects_credential_or_query_in_control_plane_url) that
    # would reject an http:// loopback before self._opener is ever called —
    # first attempt at this test proved that: it "passed" while never
    # reaching the network at all. Bypass ONLY that gate, the same trick
    # used for _probe_key's https guard, so this isolates the opener's
    # redirect-following behaviour instead.
    monkeypatch.setattr(
        bc, "urlparse", lambda url: real_urlparse(url)._replace(scheme="https")
    )
    client = bc.BillingGatewayClient("https://gw.example", "legacy-bearer")
    client.base_url = f"http://127.0.0.1:{src_port}"
    # Same non-discriminator note as above: _GatewayError fires either way
    # (a followed redirect hits `sink`, whose dummy {} body fails the
    # contract-version check) — `received['auth']` is what actually proves
    # the redirect was refused rather than followed.
    with pytest.raises(bc._GatewayError):
        client._post(
            "/internal/v1/hermes/credentials/ensure",
            {"employee_id": "x"},
            idempotency_key="idem-crosstransport-02",
        )


@pytest.mark.parametrize(
    "drive", [_drive_employee_key_transport, _drive_legacy_ensure_transport],
    ids=["employee_key", "legacy_ensure_ack"],
)
def test_no_billing_transport_replays_the_bearer_on_a_cross_origin_redirect(
    drive, monkeypatch
):
    """codex r4: legacy BillingGatewayClient._post (ensure/ack) defaulted to
    plain urlopen — the same class of leak ecc9b16 fixed on
    EmployeeKeyClient._post, just on the OTHER transport this feature owns.
    Once billing is enabled, a legacy-sourced row's 401 repair walks this
    exact code path (and billing-degrade-not-refuse depends on its
    behaviour), so it cannot be left unfixed in this slug. Both transports
    now share the same no-follow opener (_NO_REDIRECT_OPENER, defined once in
    billing_employee_key.py) — this drives BOTH through a live loopback
    redirect and asserts neither ever lets the bearer reach the second
    origin."""
    src, sink, received = _redirect_loopback_pair()
    try:
        drive(monkeypatch, src.server_port)
        assert received.get("auth") is None, "the bearer reached another origin"
    finally:
        sink.shutdown()
        src.shutdown()


@pytest.mark.parametrize("raw", ["false", "0", 0, "", None, "true", 1])
def test_only_a_real_json_true_counts_as_verified(raw):
    """bool("false") is True. Anything but a genuine JSON true must read as
    unverified, or an explicitly-unverified response passes as verified."""
    client, _ = _client(_Response(_body(account_identity_verified=raw)))
    issued = _issue(client)
    assert issued.account_identity_verified is False


def test_a_genuine_true_is_verified():
    client, _ = _client(_Response(_body(account_identity_verified=True)))
    assert _issue(client).account_identity_verified is True


def test_unverified_credential_is_refused_not_stored(tmp_path):
    """Under-count, never mis-count: a binding we cannot attribute would bill
    somebody. Refusing leaves the run unattributed (a finance gap); storing it
    would charge the wrong person."""
    from hermes_multitenancy.run_broker import RunRejected

    manager = _manager(tmp_path)
    unverified = _issued(account_identity_verified=False)
    with pytest.raises(RunRejected):
        manager.adopt_employee_key(_payer(), unverified)
    assert manager.employee_key_needed(_payer()) is True  # nothing was stored


def test_a_verified_credential_after_an_unverified_one_still_adopts(tmp_path):
    """Negative control: the refusal must not poison the payer."""
    from hermes_multitenancy.run_broker import RunRejected

    manager = _manager(tmp_path)
    with pytest.raises(RunRejected):
        manager.adopt_employee_key(_payer(), _issued(account_identity_verified=False))
    binding = manager.adopt_employee_key(_payer(), _issued())
    assert binding.credential_version == 1


@pytest.mark.parametrize(
    "alias_owner,asked_for,ok",
    [
        ("sun-ke", "sun", False),   # codex's repro: prefix test would accept this
        ("sun", "sun-ke", False),
        ("sun-ke", "sun-ke", True),  # a hyphenated id must still work for itself
        ("sunke", "sunke", True),
    ],
)
def test_hyphenated_ids_cannot_borrow_each_others_key(alias_owner, asked_for, ok):
    """Employee ids may contain hyphens, so a prefix test is ambiguous:
    `auto-sun-ke-…` starts with `auto-sun-`. Exact segmentation is not."""
    alias = f"auto-{alias_owner}-20260806-012059-f60f9a"
    client, _ = _client(_Response(_body(key_alias=alias)))
    call = lambda: client.issue(  # noqa: E731
        employee_id=asked_for,
        enterprise_email=f"{asked_for}@example.com",
        idempotency_key="hyphen-collision-01",
    )
    if ok:
        assert call().key_alias == alias
    else:
        with pytest.raises(EmployeeKeyError) as excinfo:
            call()
        assert excinfo.value.code == "employee_key_response_subject_mismatch"


def test_max_issues_caps_minting_even_when_every_store_fails():
    """Counting successes instead of attempts turns the cap into no cap: a run
    whose storage keeps failing would mint for the whole cohort while the
    counter sits at zero, and every one of those keys is a live orphan."""
    minted: list[str] = []

    def issue(m):
        minted.append(m)
        return f"key-{m}"

    result = sweep_cohort(
        [f"emp{i}" for i in range(50)],
        needs=lambda m: True,
        issue=issue,
        store=lambda m, i: (_ for _ in ()).throw(RuntimeError("vault down")),
        max_issues=10,
    )
    assert len(minted) == 10, f"minted {len(minted)} keys despite a cap of 10"
    assert result.issued == 0
    assert result.failed == 10
    assert result.deferred_budget == 40


def test_a_department_transfer_updates_both_stores(tmp_path):
    """codex r1 #p1: writing only the vault leaves BillingIdentityStore on the
    old team. The request path then rejects the new team while later sweeps see
    a fresh key and skip the employee — permanently stuck."""
    from hermes_multitenancy.billing_employee_key import store_binding

    put: list = []

    class _Preparer:
        def __init__(self, credentials):
            self._credentials = credentials
            self._store = type("S", (), {"put": lambda _s, b: put.append(b)})()

    manager = _manager(tmp_path)
    preparer = _Preparer(manager)

    store_binding(preparer, _payer(), _issued())
    moved = _issued(
        team_id="new-team-uuid",
        team_alias="新部门",
        key_alias="auto-sunke-20260806-014000-dddddd",
        api_key="sk-after-transfer",
    )
    store_binding(preparer, _payer(), moved)

    assert [b.team_alias for b in put] == ["技术平台部", "新部门"]
    assert put[-1].team_id == "new-team-uuid"
    # and the vault agrees with it
    assert manager.runtime_api_key(_metadata(put[-1])) == "sk-after-transfer"


def test_401_repair_never_mints_on_the_employee_request_path(monkeypatch, tmp_path):
    """`billing-runtime-never-mints`: a 401 must not re-issue anything.

    Supersedes the earlier contract (re-issue through the employee-key client).
    Minting here ran on the employee's OWN request path, which is what the
    hourly sweep exists to prevent — and codex's own comment in
    billing_identity called it "the sweep's timer-only minting promise" while
    the code broke it. Now repair raises the degradable error; the caller in
    agent_real._core marks the credential invalid and serves the run
    unattributed. NEITHER gateway may be touched: legacy ensure or the
    employee-key client both count as minting on the request path.
    """
    from hermes_multitenancy.billing_credentials import BillingUnavailable
    from hermes_multitenancy.billing_employee_key import store_binding
    from hermes_multitenancy.billing_identity import (
        BillingIdentityPreparer,
        BillingIdentityStore,
    )
    import hermes_multitenancy.billing_identity as bi

    manager = _manager(tmp_path)

    class _ExplodingGateway:
        def ensure(self, **kw):
            raise AssertionError("legacy ensure must not be called on a 401")

    manager._gateway = _ExplodingGateway()
    identity_store = BillingIdentityStore(tmp_path / "identity.db")
    preparer = BillingIdentityPreparer(
        routing=None, store=identity_store, credentials=manager
    )
    store_binding(preparer, _payer(), _issued())
    before = identity_store.get("sunke").key_id

    class _ExplodingClient:
        def __init__(self, *a, **kw):
            pass

        def issue(self, **kw):
            raise AssertionError("employee-key client must not mint on a 401")

    monkeypatch.setattr(bi, "EmployeeKeyClient", _ExplodingClient)

    metadata = {
        "litellm_billing_employee_user_id": "sunke",
        "litellm_billing_profile_name": "sunke",
        "litellm_billing_email": "sunke@example.com",
    }
    with pytest.raises(BillingUnavailable):
        preparer.repair_metadata(metadata)

    # Nothing was rotated behind the caller's back either.
    assert identity_store.get("sunke").key_id == before


def test_degrade_billing_metadata_strips_attribution_and_audits(caplog):
    """The 401 answer: serve the retry unattributed, and say so once.

    `_clean_metadata` is what makes the retry run on the shared key; the fixed
    `billing_degraded_unattributed` token is what makes the attribution gap
    countable. Both have to happen, or the gap is either invisible or the run
    is still billed to a dead key.
    """
    import logging

    from hermes_multitenancy.billing_identity import degrade_billing_metadata

    metadata = {
        "litellm_billing_enforced": True,
        "litellm_billing_employee_user_id": "sunke",
        "litellm_billing_profile_name": "sunke",
        "litellm_billing_email": "sunke@example.com",
        "litellm_billing_key_id": "key-dead",
        "chat_type": "p2p",
    }
    with caplog.at_level(logging.WARNING):
        out = degrade_billing_metadata(metadata)

    assert not [k for k in out if k.startswith("litellm_billing")], out
    assert out["chat_type"] == "p2p", "non-billing metadata must survive"
    assert any(
        "billing_degraded_unattributed" in r.getMessage() and "employee=sunke" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_401_repair_rejects_a_non_canonical_identity(monkeypatch, tmp_path):
    """codex r3: 74661ac wired _is_canonical_employee_id into `_employee_row`
    and `run_refresh`, but 04a3246's repair_metadata -> _repair_employee_key
    branch never went through it. codex seeded a BillingIdentityStore /
    vault row keyed on employee_id='ou_synthetic' (an ou_* Feishu open_id,
    never a real billing subject) and got a real key minted through the 401
    repair path alone — the ONE path that skipped the shared guard."""
    from hermes_multitenancy.billing_employee_key import IssuedKey
    from hermes_multitenancy.billing_identity import (
        BillingIdentityPreparer,
        BillingIdentityStore,
        _ResolvedPayer,
    )
    import hermes_multitenancy.billing_identity as bi
    from hermes_multitenancy.run_broker import RunRejected

    manager = _manager(tmp_path)
    identity_store = BillingIdentityStore(tmp_path / "identity.db")
    preparer = BillingIdentityPreparer(
        routing=None, store=identity_store, credentials=manager
    )

    # Seed BOTH stores with a source=employee_key row for a non-canonical id
    # — the premise: some other bug already got one in. adopt_employee_key
    # has no shape guard of its own (it never resolved this id through the
    # live routing path), so this succeeds exactly like a real
    # store_binding() would for a legitimately-resolved payer.
    bad_payer = _ResolvedPayer("ou_synthetic", "ou_synthetic", "ou_synthetic@example.com")
    seed = IssuedKey(
        employee_id="ou_synthetic", email="ou_synthetic@example.com",
        api_key="sk-seed", base_url="https://litellm.example/v1",
        key_alias="auto-ou_synthetic-20260806-040000-seed", team_alias="技术平台部",
        expires_at_ms=_NOW_MS + 30 * _DAY, litellm_user_id="llm-ou-synthetic",
        team_id="team-fd", account_identity_verified=True,
    )
    binding = manager.adopt_employee_key(bad_payer, seed)
    identity_store.put(binding)

    minted: list[str] = []

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def issue(self, **kw):
            minted.append(kw["employee_id"])
            raise AssertionError("must never mint for a non-canonical id")

    monkeypatch.setattr(bi, "EmployeeKeyClient", _FakeClient)

    metadata = {
        "litellm_billing_employee_user_id": "ou_synthetic",
        "litellm_billing_profile_name": "ou_synthetic",
        "litellm_billing_email": "ou_synthetic@example.com",
    }
    with pytest.raises(RunRejected):
        preparer.repair_metadata(metadata)

    assert minted == [], "must never reach EmployeeKeyClient.issue"


def test_sweep_owned_credentials_are_left_alone_by_the_legacy_renewal(tmp_path):
    """codex r1 #p1: both protocols share the vault. Legacy renewal fires when
    the remaining life drops under (30d - jitter); the sweep fires one day
    before expiry. Without a marker they re-mint over each other on the row.

    The clock is advanced 20 days on purpose: a FRESH 30-day key still has
    ~30d left, which is above the legacy threshold for any jitter, so it
    returns early and the legacy path is never reached — a test written at day
    zero passes whether or not the marker works (verified: it did)."""
    from hermes_multitenancy.billing_employee_key import CREDENTIAL_SOURCE

    clock = {"now": _NOW_MS}
    from hermes_multitenancy.billing_identity import BillingCredentialManager
    from hermes_multitenancy.credentials import CredentialStore

    manager = BillingCredentialManager(
        vault=CredentialStore(tmp_path / "v.db", encryption_key=_VAULT_KEY),
        gateway=None,
        model_base_url="https://litellm.example/v1",
        now_ms=lambda: clock["now"],
        probe=lambda _key: None,
    )
    binding = manager.adopt_employee_key(_payer(), _issued())
    payload = manager._load_payload(_payer().profile_name, _payer().employee_user_id)
    assert payload["source"] == CREDENTIAL_SOURCE

    clock["now"] = _NOW_MS + 20 * _DAY  # ~10 days left: inside the legacy window

    class _ExplodingGateway:
        def ensure(self, **kw):
            raise AssertionError("legacy renewal must not touch a swept credential")

    manager._gateway = _ExplodingGateway()
    again = manager.ensure_available(_payer(), binding)
    assert again.key_id == binding.key_id


# --------------------------------------- run_refresh: real non-dry-run path


def test_run_refresh_uses_the_snapshot_email_and_writes_both_stores(
    monkeypatch, tmp_path
):
    """codex r2 (decorative test): no test exercised run_refresh's real
    non-dry-run path at all — only sweep_cohort in isolation, and only
    dry_run=True through run_refresh. That left two real wiring facts
    unpinned: (a) the sweep must use the org snapshot's canonical email, not
    the fabricated <id>@domain — the two commonly differ, and a fabricated
    email produces a fresh vault row the next real request rejects as
    identity drift; (b) run_refresh's real _store_binding must write BOTH the
    vault and BillingIdentityStore, not just the vault (the split-brain
    finding above, exercised through the actual entry point instead of the
    store_binding helper directly). Exercise the real, un-mocked pipeline."""
    import json
    import sqlite3

    from hermes_multitenancy import billing_employee_key as bek
    from hermes_multitenancy.billing_employee_key import IssuedKey
    from hermes_multitenancy.billing_identity import (
        BillingIdentityPreparer,
        BillingIdentityStore,
    )

    db = tmp_path / "routing.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE multitenancy_routing (user_id TEXT, profile_name TEXT, "
        "active INTEGER, kind TEXT, provenance TEXT)"
    )
    conn.execute(
        "INSERT INTO multitenancy_routing VALUES ('sunke','sunke',1,'user','sync')"
    )
    conn.commit()
    conn.close()

    # A snapshot email that deliberately differs from the fabricated fallback
    # (sunke@example.com) so the assertion cannot pass by accident.
    snap_dir = tmp_path / "org-snapshots"
    snap_dir.mkdir()
    (snap_dir / "org-1.json").write_text(json.dumps({
        "employees": {
            "sunke": {"user_id": "sunke", "enterprise_email": "sun.ke@example.com",
                      "dept_id": "d1"},
        },
        "departments": [{"dept_id": "d1", "name": "技术平台部", "parent_id": "0"}],
    }))

    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "sunke")
    monkeypatch.setenv("HERMES_MULTITENANCY_DB", str(db))
    monkeypatch.setenv("HERMES_ORG_SNAPSHOT_DIR", str(snap_dir))
    monkeypatch.setenv("HERMES_EMPLOYEE_KEY_SILENT_TOKEN", "tok")
    monkeypatch.setenv("HERMES_EMPLOYEE_KEY_BASE_URL", "https://gw.example")
    # A sweep ends in a vault write, which production cannot do without this.
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-vault-key")

    manager = _manager(tmp_path)
    identity_store = BillingIdentityStore(tmp_path / "identity.db")
    preparer = BillingIdentityPreparer(
        routing=None, store=identity_store, credentials=manager
    )
    import hermes_multitenancy.billing_identity as bi

    monkeypatch.setattr(bi, "_default_preparer", lambda: preparer)

    seen_emails: list[str] = []

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def issue(self, *, employee_id, enterprise_email, idempotency_key):
            seen_emails.append(enterprise_email)
            return IssuedKey(
                employee_id=employee_id,
                email=enterprise_email,
                api_key=f"sk-{employee_id}",
                base_url="https://litellm.example/v1",
                key_alias=f"auto-{employee_id}-20260806-020000-refr",
                team_alias="技术平台部",
                expires_at_ms=_NOW_MS + 30 * _DAY,
                litellm_user_id=f"llm-{employee_id}",
                team_id="team-fd",
                account_identity_verified=True,
            )

    monkeypatch.setattr(bek, "EmployeeKeyClient", _FakeClient)

    out = bek.run_refresh(dry_run=False)

    assert seen_emails == ["sun.ke@example.com"], (
        "must use the org snapshot's canonical email, not the fabricated one"
    )
    assert out["issued"] == 1
    assert out["failed"] == 0

    binding = identity_store.get("sunke")
    assert binding is not None, "BillingIdentityStore was never written"
    assert manager.runtime_api_key(_metadata(binding)) == "sk-sunke"


def _routing_db_with_sunke(tmp_path):
    import sqlite3

    db = tmp_path / "routing.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE multitenancy_routing (user_id TEXT, profile_name TEXT, "
        "active INTEGER, kind TEXT, provenance TEXT)"
    )
    conn.execute(
        "INSERT INTO multitenancy_routing VALUES ('sunke','sunke',1,'user','sync')"
    )
    conn.commit()
    conn.close()
    return db


def _refresh_env_without_vault_key(monkeypatch, db):
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "sunke")
    monkeypatch.setenv("HERMES_MULTITENANCY_DB", str(db))
    monkeypatch.setenv("HERMES_EMPLOYEE_KEY_SILENT_TOKEN", "tok")
    monkeypatch.setenv("HERMES_EMPLOYEE_KEY_BASE_URL", "https://gw.example")
    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)


@pytest.mark.parametrize("dry_run", [False, True])
def test_refresh_without_a_vault_key_stops_before_the_gateway(
    monkeypatch, tmp_path, dry_run
):
    """No vault key => stop before LiteLLM issues anything, in BOTH modes.

    The sweep needs the key to store what it mints: finding out at the vault
    write would leave a real, live key at LiteLLM that nothing can store or
    attribute — one orphan per cohort member, per run. `--dry-run` mints
    nothing, but it still DECRYPTS existing rows via `employee_key_needed`, so
    on a populated vault it fails anyway; exempting it only looked safe against
    an empty vault (codex review, 2026-08-06).
    """
    from hermes_multitenancy import billing_employee_key as bek

    db = _routing_db_with_sunke(tmp_path)
    _refresh_env_without_vault_key(monkeypatch, db)

    minted: list[str] = []

    class _RecordsMints:
        def issue(self, **kw):
            minted.append(kw["employee_id"])
            raise AssertionError("must not mint without a vault key")

    monkeypatch.setattr(bek, "EmployeeKeyClient", lambda *a, **k: _RecordsMints())

    class _Creds:
        def employee_key_needed(self, payer):
            return True

    import hermes_multitenancy.billing_identity as bi

    monkeypatch.setattr(
        bi, "_default_preparer", lambda: type("P", (), {"_credentials": _Creds()})()
    )

    with pytest.raises(EmployeeKeyError) as excinfo:
        bek.run_refresh(dry_run=dry_run)
    assert "credential_key_unset" in str(excinfo.value)
    assert minted == [], "reached the gateway before the guard fired"


def test_vault_key_check_agrees_with_the_vault_itself(monkeypatch, tmp_path):
    """The guard must ask the same question CredentialStore asks.

    `_resolve_optional_key` takes the first TRUTHY env value with NO strip(), so
    a whitespace-only primary IS this host's key material as far as the vault is
    concerned. A hand-rolled check that strip()s each name would call the very
    same host unconfigured and refuse to run — the guard and the vault
    disagreeing about one host. Discriminating case: whitespace-only primary and
    NO legacy name (with a legacy name present both spellings happen to agree,
    which is why an earlier version of this test had no power to fail).
    """
    from hermes_multitenancy import billing_employee_key as bek
    from hermes_multitenancy.credentials import _resolve_optional_key

    db = _routing_db_with_sunke(tmp_path)
    _refresh_env_without_vault_key(monkeypatch, db)
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "   ")

    assert _resolve_optional_key(None) is not None, "vault considers this host keyed"

    class _Creds:
        def employee_key_needed(self, payer):
            return False

    import hermes_multitenancy.billing_identity as bi

    monkeypatch.setattr(
        bi, "_default_preparer", lambda: type("P", (), {"_credentials": _Creds()})()
    )
    monkeypatch.setattr(bek, "EmployeeKeyClient", lambda *a, **k: object())

    # Must NOT raise: refusing here would strand a host the vault can serve.
    out = bek.run_refresh(dry_run=True)
    assert out["would_issue"] == []


# --- billing-runtime-never-mints: the employee request path asks for nothing ---

class _ExplodingEnsureGateway:
    """Any ensure() call is a test failure: that IS request-path minting."""

    def __init__(self):
        self.calls = 0

    def ensure(self, **kw):
        self.calls += 1
        raise AssertionError(f"gateway.ensure called on the request path: {kw}")


def test_request_path_with_no_credential_degrades_without_calling_the_gateway(tmp_path):
    """A cohort member who has not been provisioned yet: degrade, never mint.

    This is the case that fires for EVERY not-yet-provisioned person the moment
    the cohort opens, so it is the one that decides whether "the employee never
    triggers issuance" is true. Verified in production 2026-08-06 to have been
    FALSE before this slug: three employees each got a key minted on their own
    request path.
    """
    from hermes_multitenancy.billing_credentials import BillingUnavailable

    manager = _manager(tmp_path)
    gateway = _ExplodingEnsureGateway()
    manager._gateway = gateway

    with pytest.raises(BillingUnavailable) as excinfo:
        manager.ensure_available(_payer(), None, allow_mint=False)

    assert "not provisioned yet" in str(excinfo.value)
    assert gateway.calls == 0


def test_request_path_serves_a_usable_stored_credential_unchanged(tmp_path):
    """Having a key is the normal case and must be byte-identical to before."""
    from hermes_multitenancy.billing_employee_key import store_binding
    from hermes_multitenancy.billing_identity import (
        BillingIdentityPreparer,
        BillingIdentityStore,
    )

    manager = _manager(tmp_path)
    manager._gateway = _ExplodingEnsureGateway()
    preparer = BillingIdentityPreparer(
        routing=None, store=BillingIdentityStore(tmp_path / "identity.db"),
        credentials=manager,
    )
    store_binding(preparer, _payer(), _issued())

    binding = manager.ensure_available(_payer(), None, allow_mint=False)

    assert binding.employee_user_id == "sunke"
    assert binding.credential_version == 1
    assert manager._gateway.calls == 0


def test_request_path_inside_the_rotation_window_keeps_the_old_key(tmp_path):
    """Rotation belongs to the sweep; the request path must not race it.

    A key deep inside the legacy renew window is exactly when `_ensure_locked`
    used to rotate. It must now keep serving the existing key — expiry is still
    days away, and the hourly sweep rotates one day before it.
    """
    from hermes_multitenancy.billing_employee_key import store_binding
    from hermes_multitenancy.billing_identity import (
        BillingIdentityPreparer,
        BillingIdentityStore,
    )

    manager = _manager(tmp_path)
    manager._gateway = _ExplodingEnsureGateway()
    preparer = BillingIdentityPreparer(
        routing=None, store=BillingIdentityStore(tmp_path / "identity.db"),
        credentials=manager,
    )
    # Deep inside the legacy 30-day renew window, still comfortably valid.
    # The expiry is written straight into the vault instead of minting a
    # short-lived key: `_MIN_LIFETIME_MS` is exactly 2 days, so a fixture that
    # asked the client for a 2-day key sat ON the floor and failed whenever any
    # wall-clock time had passed since `_NOW_MS` was captured at import — green
    # on a fast laptop, red in CI (which is how this was caught).
    store_binding(preparer, _payer(), _issued())
    payload = manager._load_payload("sunke", "sunke")
    payload["expires_at"] = _NOW_MS + 20 * _DAY
    # `source=ensure` on purpose: a `source=employee_key` row returns early via
    # its own guard, so it would pass this test even with the no-mint gate
    # removed (verified by mutation — the assertion had no power). The legacy
    # protocol is the one whose 23–30 day renewal actually reaches the gateway,
    # so that is the row that proves the request path no longer rotates.
    payload["source"] = "ensure"
    manager._save_payload("sunke", "sunke", payload)

    binding = manager.ensure_available(_payer(), None, allow_mint=False)

    assert binding.credential_version == 1
    assert manager._gateway.calls == 0


def test_the_sweep_itself_still_mints(tmp_path):
    """Negative control for the whole slug: allow_mint defaults to True.

    If this went red the change would have disarmed the ONLY provisioning path
    instead of just the request path — nobody would ever get a key.
    """
    calls = []

    class _RecordingGateway:
        def ensure(self, **kw):
            calls.append(kw)
            raise RuntimeError("stop here — reaching the gateway is the assertion")

    manager = _manager(tmp_path)
    manager._gateway = _RecordingGateway()

    with pytest.raises(Exception):
        manager.ensure_available(_payer(), None)  # default allow_mint=True

    assert len(calls) == 1, "the sweep path must still be allowed to mint"


class _UntouchableGateway:
    """ANY attribute access is a failure: proves gateway-read-only, not just
    "no ensure()". codex #p1 (2026-08-07) found `_finish_pending` reaching
    `gateway.ack()` — a WRITE — before the original gate, so asserting only on
    ensure() would have passed while the employee's request still drove the
    gateway."""

    def __getattr__(self, name):
        raise AssertionError(f"request path touched the gateway: .{name}")


def test_request_path_never_touches_the_gateway_at_all(tmp_path):
    """Not one gateway call from the employee's own request — ensure OR ack."""
    from hermes_multitenancy.billing_credentials import BillingUnavailable

    manager = _manager(tmp_path)
    manager._gateway = _UntouchableGateway()

    with pytest.raises(BillingUnavailable):
        manager.ensure_available(_payer(), None, allow_mint=False)


def test_request_path_serves_a_pending_row_without_acking_it(tmp_path):
    """A pending-but-real key is served; completing the handshake is the sweep's.

    This is the case codex caught: the row is usable, its ack backoff has
    elapsed, and the old code would have ACKed it from inside the employee's
    request. Serving it is correct — it is a live key — and the maintenance
    sweep finishes the lifecycle.
    """
    from hermes_multitenancy.billing_employee_key import store_binding
    from hermes_multitenancy.billing_identity import (
        BillingIdentityPreparer,
        BillingIdentityStore,
    )

    manager = _manager(tmp_path)
    preparer = BillingIdentityPreparer(
        routing=None, store=BillingIdentityStore(tmp_path / "identity.db"),
        credentials=manager,
    )
    store_binding(preparer, _payer(), _issued())

    # Make it look like a legacy pending generation whose backoff has elapsed.
    payload = manager._load_payload("sunke", "sunke")
    payload["ack_pending"] = True
    payload["ack_retry_after"] = 0
    payload["source"] = "ensure"
    manager._save_payload("sunke", "sunke", payload)

    manager._gateway = _UntouchableGateway()
    binding = manager.ensure_available(_payer(), None, allow_mint=False)

    assert binding.employee_user_id == "sunke"
    assert manager._load_payload("sunke", "sunke")["ack_pending"] is True, (
        "the request path must not complete the pending handshake"
    )


def test_request_path_degrades_on_a_probe_pending_row(tmp_path):
    """An unactivated crash-recovery row is NOT a usable credential.

    `probe_pending` means the row was saved before the activation probe proved
    the key works. Serving it unprobed would hand the runtime a possibly-dead
    key; completing the probe is maintenance work (it may delete the row or fall
    back to the previous generation). So the request path degrades instead —
    contrast `ack_pending`, which did pass its probe and IS served.
    """
    from hermes_multitenancy.billing_credentials import BillingUnavailable
    from hermes_multitenancy.billing_employee_key import store_binding
    from hermes_multitenancy.billing_identity import (
        BillingIdentityPreparer,
        BillingIdentityStore,
    )

    manager = _manager(tmp_path)
    preparer = BillingIdentityPreparer(
        routing=None, store=BillingIdentityStore(tmp_path / "identity.db"),
        credentials=manager,
    )
    store_binding(preparer, _payer(), _issued())
    payload = manager._load_payload("sunke", "sunke")
    payload["probe_pending"] = True
    manager._save_payload("sunke", "sunke", payload)

    manager._gateway = _UntouchableGateway()
    with pytest.raises(BillingUnavailable):
        manager.ensure_available(_payer(), None, allow_mint=False)

    # And the row is left exactly as maintenance will find it.
    assert manager._load_payload("sunke", "sunke")["probe_pending"] is True


# ------------------------------------------------- @routing cohort sentinel


def _routing_db(tmp_path, rows):
    import sqlite3

    db = tmp_path / "routing.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE multitenancy_routing (user_id TEXT, profile_name TEXT, "
        "active INTEGER, kind TEXT, provenance TEXT)"
    )
    conn.executemany("INSERT INTO multitenancy_routing VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


def _refresh_env(monkeypatch, db, payer_ids):
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", payer_ids)
    monkeypatch.setenv("HERMES_MULTITENANCY_DB", str(db))
    monkeypatch.setenv("HERMES_EMPLOYEE_KEY_SILENT_TOKEN", "tok")
    monkeypatch.setenv("HERMES_EMPLOYEE_KEY_BASE_URL", "https://gw.example")
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-vault-key")
    import hermes_multitenancy.billing_identity as bi

    monkeypatch.setattr(
        bi, "_default_preparer",
        lambda: type("P", (), {"_credentials": type("C", (), {
            "employee_key_needed": lambda self, p: True})()})(),
    )


def test_routing_sentinel_enrolls_whoever_sync_routes(monkeypatch, tmp_path):
    """The 2026-08-14 gap: lisi joined, feishu-sync routed him, and the
    frozen HERMES_LITELLM_BILLING_PAYER_IDS list still didn't know him — so the
    sweep never minted. With "@routing" the routing table IS the cohort."""
    from hermes_multitenancy import billing_employee_key as bek

    db = _routing_db(tmp_path, [
        ("sunke", "sunke", 1, "user", "sync"),
        ("lisi", "lisi", 1, "user", "sync"),
        ("departed", "departed", 0, "user", "sync"),      # inactive: retired
        ("svc-bot", "svc-bot", 1, "user", "manual"),      # not sync: excluded
        ("grp", "grp", 1, "group", "sync"),               # not a user: excluded
    ])
    _refresh_env(monkeypatch, db, "@routing")

    out = bek.run_refresh(dry_run=True)
    assert out["would_issue"] == ["lisi", "sunke"]
    assert out["cohort"] == 2


def test_routing_sentinel_reports_noncanonical_ids_instead_of_minting(
    monkeypatch, tmp_path
):
    """A synthetic ou_* row in routing is a chat identity, not a billing
    subject — the shape guard must file it under unrouted, never mint."""
    from hermes_multitenancy import billing_employee_key as bek

    db = _routing_db(tmp_path, [
        ("sunke", "sunke", 1, "user", "sync"),
        ("ou_eeeeeeeeeeeeeeee0000000000000001", "ghost", 1, "user", "sync"),
    ])
    _refresh_env(monkeypatch, db, "@routing")

    out = bek.run_refresh(dry_run=True)
    assert out["would_issue"] == ["sunke"]
    assert out["unrouted"] == ["ou_eeeeeeeeeeeeeeee0000000000000001"]


def test_a_list_containing_the_sentinel_stays_static(monkeypatch, tmp_path):
    """"sunke,@routing" is a static list with a bogus member, not auto mode —
    the bogus member is reported unrouted and nobody else sneaks in."""
    from hermes_multitenancy import billing_employee_key as bek

    db = _routing_db(tmp_path, [
        ("sunke", "sunke", 1, "user", "sync"),
        ("lisi", "lisi", 1, "user", "sync"),
    ])
    _refresh_env(monkeypatch, db, "sunke,@routing")

    out = bek.run_refresh(dry_run=True)
    assert out["would_issue"] == ["sunke"]
    assert "lisi" not in out["would_issue"]
    assert out["unrouted"] == ["@routing"]


def test_routing_sentinel_refuses_an_empty_routing_table(monkeypatch, tmp_path):
    """Static mode refuses an empty cohort (billing_canary_cohort_invalid);
    auto mode must refuse too, or an empty/unreadable routing table becomes a
    quiet zero-member sweep and 1291 keys age toward expiry with exit 0."""
    import pytest

    from hermes_multitenancy import billing_employee_key as bek

    db = _routing_db(tmp_path, [
        ("departed", "departed", 0, "user", "sync"),  # nobody active
    ])
    _refresh_env(monkeypatch, db, "@routing")

    with pytest.raises(bek.EmployeeKeyError, match="routing_cohort_empty"):
        bek.run_refresh(dry_run=True)


def test_routing_sentinel_refuses_a_cohort_with_no_mintable_member(
    monkeypatch, tmp_path
):
    """grok review #p1: rows can exist yet ALL fail the canonical-shape gate
    (mass ou_* subjects after a sync bug) — that is the same silent zero-member
    sweep, so the refusal keys on post-filter payers, not raw row count."""
    import pytest

    from hermes_multitenancy import billing_employee_key as bek

    db = _routing_db(tmp_path, [
        ("ou_eeeeeeeeeeeeeeee0000000000000001", "ghost", 1, "user", "sync"),
    ])
    _refresh_env(monkeypatch, db, "@routing")

    with pytest.raises(bek.EmployeeKeyError, match="routing_cohort_empty"):
        bek.run_refresh(dry_run=True)
