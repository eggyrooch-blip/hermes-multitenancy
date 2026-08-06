"""Degrade on 'could not obtain', still refuse on 'obtained but wrong'."""
from __future__ import annotations

import contextlib
import errno
import sqlite3

import pytest

from hermes_multitenancy.billing_credentials import BillingUnavailable
from hermes_multitenancy.run_broker import RunRejected


@contextlib.contextmanager
def _real_sqlite_busy(db_path):
    """Hold a genuine SQLite EXCLUSIVE lock on ``db_path`` for the block's
    duration, so any OTHER connection's read/write during the block raises
    a real SQLITE_BUSY — not a hand-built exception (codex r3 #2: a
    fabricated exception doesn't prove ``_is_vault_unavailable`` holds
    against what SQLite actually raises)."""
    blocker = sqlite3.connect(db_path, timeout=0.05)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        yield
    finally:
        blocker.rollback()
        blocker.close()


def test_unavailable_is_a_runrejected_subtype():
    """Callers that do not care about the distinction keep working unchanged."""
    assert issubclass(BillingUnavailable, RunRejected)


def test_only_the_acquisition_failures_carry_the_degradable_type():
    """The separation is by explicit transport/availability STATUS, not by
    the gateway's own self-declared ``retryable`` flag and not by matching
    error strings — trusting ``retryable`` let 401/403/unknown-4xx rejections
    (real defects) degrade whenever the gateway happened to mark them
    retryable (codex p1-1, 2026-08-06 review-fail)."""
    from hermes_multitenancy.billing_credentials import _GatewayError, _gateway_rejection

    conflict = _gateway_rejection(_GatewayError(409, "identity_conflict"))
    topology = _gateway_rejection(_GatewayError(409, "account_topology_conflict"))
    service_unavailable = _gateway_rejection(_GatewayError(503, "upstream", retryable=True))
    # A 400 the gateway happens to flag retryable is still "we got an answer
    # and it says something is wrong" — status is what decides, not the flag.
    unknown_4xx_retryable_flag = _gateway_rejection(_GatewayError(400, "invalid_request", retryable=True))
    auth_failure = _gateway_rejection(_GatewayError(401, "unauthorized", retryable=False))
    contract_drift = _gateway_rejection(_GatewayError(502, "unsupported_contract_version"))

    # conflicts = the gateway's data contradicts ours; a human must look
    assert not isinstance(conflict, BillingUnavailable)
    assert not isinstance(topology, BillingUnavailable)
    # 502/503/504 = genuine transport/service-unavailable shapes → degrade
    assert isinstance(service_unavailable, BillingUnavailable)
    # everything else = "obtained but wrong" → stays closed regardless of
    # the gateway's own retryable claim
    assert not isinstance(unknown_4xx_retryable_flag, BillingUnavailable)
    assert not isinstance(auth_failure, BillingUnavailable)
    assert not isinstance(contract_drift, BillingUnavailable)


def test_our_own_misconfiguration_is_not_degraded():
    """codex r2 p1-1: ``broker_not_configured`` is minted by _post() itself
    (malformed/missing broker URL or token) — it never reached the network.
    It carries status 503 like a genuine outage, but it IS our config being
    broken, not the gateway being unreachable. Must stay RunRejected, or a
    misconfigured deployment would degrade every single billing attempt
    forever instead of failing loud enough for ops to notice."""
    from hermes_multitenancy.billing_credentials import _GatewayError, _gateway_rejection

    misconfigured = _gateway_rejection(_GatewayError(503, "broker_not_configured", True))
    # broker_unavailable is the SIBLING code for a real connection attempt
    # that failed — must stay on the degradable side, or this fix would
    # have swept a genuine outage back into "always reject" too.
    real_outage = _gateway_rejection(_GatewayError(503, "broker_unavailable", True))

    assert not isinstance(misconfigured, BillingUnavailable)
    assert isinstance(real_outage, BillingUnavailable)


def test_malformed_gateway_authority_is_config_error_not_degraded():
    """codex r3 #1: a malformed HTTPS authority (bad port, unterminated
    IPv6 literal) bypassed the local config precheck in ``_post()`` and
    reached the real opener — where it came back as either a degradable
    ``broker_unavailable`` (URLError) or an uncaught
    ``http.client.InvalidURL``. Both are wrong: this is our own config
    being syntactically broken, never sent over the wire.

    This goes through the REAL ``BillingGatewayClient.ensure()`` — the
    test above constructs a ``_GatewayError`` directly, which is exactly
    why it never caught this: the bug lived inside ``_post()``'s own
    validation, before any ``_GatewayError`` exists to classify."""
    from hermes_multitenancy.billing_credentials import (
        BillingGatewayClient,
        _GatewayError,
        _gateway_rejection,
    )

    def _never_called(*_a, **_k):
        pytest.fail("a malformed authority must be rejected before touching the network")

    for bad_url in (
        "https://gateway.example:99999",  # port out of range
        "https://gateway.example:abc",  # non-numeric port
        "https://[::1",  # unterminated IPv6 literal
    ):
        client = BillingGatewayClient(bad_url, "token", opener=_never_called)
        with pytest.raises(_GatewayError) as excinfo:
            client.ensure(
                employee_id="alice",
                enterprise_email="alice@keep.com",
                department_alias="FD",
                reason="missing",
            )
        assert excinfo.value.code == "broker_not_configured"
        assert not isinstance(_gateway_rejection(excinfo.value), BillingUnavailable)


def test_degrade_event_token_is_stable():
    """Operators grep/alert on this exact token to answer 'how much spend went
    unattributed today'. Renaming it silently breaks that."""
    from hermes_multitenancy.billing_identity import _BILLING_DEGRADED_EVENT

    assert _BILLING_DEGRADED_EVENT == "billing_degraded_unattributed"


def test_degraded_run_is_not_marked_enforced(caplog):
    """The decision has to happen BEFORE the run is labelled enforced. A run
    marked enforced without a credential trips the runtime guard — which would
    move the refusal rather than remove it."""
    import logging
    from dataclasses import replace as _replace

    from hermes_multitenancy import billing_identity as bi

    class _Creds:
        def ensure_available(self, payer, existing, **kw):
            raise BillingUnavailable("gateway is down")

    class _Store:
        def get(self, _employee_id):
            return None

        def get_by_profile(self, _profile):
            return None

        def put(self, _binding):  # pragma: no cover - must never be called
            raise AssertionError("a degraded run must not persist a binding")

    class _Req:
        channel = "webui"
        profile_name = "sunke"
        chat_id = ""
        metadata: dict = {}

    preparer = object.__new__(bi.BillingIdentityPreparer)
    preparer._store = _Store()
    preparer._credentials = _Creds()
    preparer._routing_lock = __import__("threading").Lock()
    payer = bi._ResolvedPayer("sunke", "sunke", "sunke@keep.com", "")
    preparer._payer = lambda *_a, **_k: payer

    monkey = pytest.MonkeyPatch()
    monkey.setattr(bi, "_payer_selected", lambda _e: True)
    monkey.setattr(bi, "_employee_org_fields", lambda _e: ("sunke@keep.com", "FD"))
    monkey.setattr(bi, "replace", _replace, raising=False)
    try:
        with caplog.at_level(logging.WARNING):
            out = preparer.prepare(_Req())
    finally:
        monkey.undo()

    assert "litellm_billing_enforced" not in (out.metadata or {})
    assert "billing_degraded_unattributed" in caplog.text
    assert "sunke" in caplog.text


# ---------------------------------------------------------------------------
# Full enumeration of prepare()/runtime fail-closed sites turned up four more
# places whose OWN wording already said "unavailable" / "an outage" but
# still raised plain RunRejected instead of BillingUnavailable, so they never
# reached prepare()'s degrade branch. Fixed in billing_credentials.py; each
# gets a type-pinned test here so a future edit can't silently un-fix it.
# ---------------------------------------------------------------------------


def test_credential_gone_exhaustion_is_the_degradable_type(tmp_path):
    """A broker that keeps discarding every fresh generation is an outage of
    OUR ability to obtain a credential — the code's own comment already said
    so. Must be BillingUnavailable, not plain RunRejected."""
    import json
    from pathlib import Path

    from hermes_multitenancy.billing_credentials import (
        BillingCredentialManager,
        _GatewayError,
        _ResolvedPayer,
    )
    from hermes_multitenancy.credentials import CredentialStore

    fixture = json.loads(
        (Path(__file__).parent / "contract_fixtures/hermes_credentials_v1.json").read_text()
    )

    class _AlwaysGoneGateway:
        def __init__(self):
            self.responses = [
                dict(fixture["ensure_issued_response"]),
                dict(fixture["ensure_rotated_response"]),
            ]

        def ensure(self, **_kwargs):
            return self.responses.pop(0)

        def ack(self, _payload):
            raise _GatewayError(410, "credential_gone", False)

    manager = BillingCredentialManager(
        vault=CredentialStore(tmp_path / "vault.db", encryption_key="k"),
        gateway=_AlwaysGoneGateway(),
        model_base_url="https://litellm.example/v1",
        now_ms=lambda: 1_800_000_000_000,
        probe=lambda _key: None,
    )
    payer = _ResolvedPayer("alice", "alice", "alice@keep.com", "FD")

    with pytest.raises(BillingUnavailable, match="temporarily unavailable"):
        manager.ensure_available(payer, None)


def test_vault_load_outage_is_the_degradable_type(tmp_path):
    """Cannot even read the vault to check for an existing credential — this
    is the same shape of failure as the gateway being down.

    Real SQLITE_BUSY (codex r3 #2): the override is only the injection
    POINT (deciding when to create real contention) — the exception that
    reaches ``_is_vault_unavailable`` comes from an actual second
    connection holding a real lock, not a hand-built exception."""
    from hermes_multitenancy.billing_credentials import (
        BillingCredentialManager,
        _ResolvedPayer,
    )
    from hermes_multitenancy.credentials import CredentialStore

    db_path = tmp_path / "vault.db"

    class _BrokenReadVault(CredentialStore):
        def get_status(self, **kwargs):
            with _real_sqlite_busy(db_path):
                return super().get_status(**kwargs)

    vault = _BrokenReadVault(db_path, encryption_key="k")
    vault._conn.execute("PRAGMA busy_timeout = 50")  # keep the test fast
    manager = BillingCredentialManager(
        vault=vault,
        gateway=object(),  # never reached
        model_base_url="https://litellm.example/v1",
    )
    payer = _ResolvedPayer("alice", "alice", "alice@keep.com", "FD")

    with pytest.raises(BillingUnavailable, match="vault is unavailable"):
        manager.ensure_available(payer, None)


def test_vault_permission_error_stays_rejected_not_degraded(tmp_path):
    """Self-check catch (not from codex's review text, found while auditing
    every BillingUnavailable site per their instruction): PermissionError
    IS-A OSError in Python, so a naive ``except OSError`` would swallow the
    vault's genuine access-denial/not-found signal into the degradable
    branch — precisely the p1-3 class of bug (permission failures must stay
    closed), just not the exact line codex named."""
    from hermes_multitenancy.billing_credentials import (
        BillingCredentialManager,
        _ResolvedPayer,
    )
    from hermes_multitenancy.credentials import CredentialStore

    class _PermissionDeniedVault(CredentialStore):
        def get_status(self, **_kwargs):
            raise PermissionError("credential not found for current profile/subject/provider")

    manager = BillingCredentialManager(
        vault=_PermissionDeniedVault(tmp_path / "vault.db", encryption_key="k"),
        gateway=object(),  # never reached
        model_base_url="https://litellm.example/v1",
    )
    payer = _ResolvedPayer("alice", "alice", "alice@keep.com", "FD")

    with pytest.raises(RunRejected) as excinfo:
        manager.ensure_available(payer, None)
    assert not isinstance(excinfo.value, BillingUnavailable)


def test_vault_save_outage_is_the_degradable_type(tmp_path):
    """Gateway handed us a real credential but our own storage cannot persist
    it — still "could not obtain", not a data-integrity defect.

    Real SQLITE_BUSY (codex r3 #2), same pattern as the load test above."""
    import json
    from pathlib import Path

    from hermes_multitenancy.billing_credentials import (
        BillingCredentialManager,
        _ResolvedPayer,
    )
    from hermes_multitenancy.credentials import CredentialStore

    fixture = json.loads(
        (Path(__file__).parent / "contract_fixtures/hermes_credentials_v1.json").read_text()
    )
    db_path = tmp_path / "vault.db"

    class _BrokenWriteVault(CredentialStore):
        def put_credential(self, **kwargs):
            with _real_sqlite_busy(db_path):
                return super().put_credential(**kwargs)

    class _Gateway:
        def ensure(self, **_kwargs):
            return dict(fixture["ensure_issued_response"])

    vault = _BrokenWriteVault(db_path, encryption_key="k")
    vault._conn.execute("PRAGMA busy_timeout = 50")
    manager = BillingCredentialManager(
        vault=vault,
        gateway=_Gateway(),
        model_base_url="https://litellm.example/v1",
        now_ms=lambda: 1_800_000_000_000,
        probe=lambda _key: None,
    )
    payer = _ResolvedPayer("alice", "alice", "alice@keep.com", "FD")

    with pytest.raises(BillingUnavailable, match="vault is unavailable"):
        manager.ensure_available(payer, None)


def test_vault_missing_encryption_key_is_not_degraded(tmp_path, monkeypatch):
    """codex p1-3's literal example: ``RuntimeError("credential encryption
    key is required")`` is a config error (no key configured), not an I/O
    outage. Must stay RunRejected, never BillingUnavailable, even though the
    gateway successfully handed us a real credential to persist."""
    import json
    from pathlib import Path

    from hermes_multitenancy.billing_credentials import (
        BillingCredentialManager,
        _ResolvedPayer,
    )
    from hermes_multitenancy.credentials import CredentialStore

    monkeypatch.delenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("HERMES_CREDENTIAL_KEY", raising=False)

    fixture = json.loads(
        (Path(__file__).parent / "contract_fixtures/hermes_credentials_v1.json").read_text()
    )

    class _Gateway:
        def ensure(self, **_kwargs):
            return dict(fixture["ensure_issued_response"])

    manager = BillingCredentialManager(
        vault=CredentialStore(tmp_path / "vault.db"),  # no encryption_key, no env var
        gateway=_Gateway(),
        model_base_url="https://litellm.example/v1",
        now_ms=lambda: 1_800_000_000_000,
        probe=lambda _key: None,
    )
    payer = _ResolvedPayer("alice", "alice", "alice@keep.com", "FD")

    with pytest.raises(RunRejected) as excinfo:
        manager.ensure_available(payer, None)
    assert not isinstance(excinfo.value, BillingUnavailable)


def test_vault_delete_outage_is_the_degradable_type(tmp_path):
    """The credential_gone reissue path deletes the vanished generation
    before retrying; if that delete itself fails, it must degrade too.

    Real SQLITE_BUSY (codex r3 #2), same pattern as the load/save tests
    above."""
    import json
    from pathlib import Path

    from hermes_multitenancy.billing_credentials import (
        BillingCredentialManager,
        _GatewayError,
        _ResolvedPayer,
    )
    from hermes_multitenancy.credentials import CredentialStore

    fixture = json.loads(
        (Path(__file__).parent / "contract_fixtures/hermes_credentials_v1.json").read_text()
    )
    db_path = tmp_path / "vault.db"

    class _BrokenDeleteVault(CredentialStore):
        def delete_credential(self, **kwargs):
            with _real_sqlite_busy(db_path):
                return super().delete_credential(**kwargs)

    class _GoneOnceGateway:
        def ensure(self, **_kwargs):
            return dict(fixture["ensure_issued_response"])

        def ack(self, _payload):
            raise _GatewayError(410, "credential_gone", False)

    vault = _BrokenDeleteVault(db_path, encryption_key="k")
    vault._conn.execute("PRAGMA busy_timeout = 50")
    manager = BillingCredentialManager(
        vault=vault,
        gateway=_GoneOnceGateway(),
        model_base_url="https://litellm.example/v1",
        now_ms=lambda: 1_800_000_000_000,
        probe=lambda _key: None,
    )
    payer = _ResolvedPayer("alice", "alice", "alice@keep.com", "FD")

    with pytest.raises(BillingUnavailable, match="vault is unavailable"):
        manager.ensure_available(payer, None)


# ---------------------------------------------------------------------------
# Negative controls — profile / email / account drift must NEVER be caught
# by prepare()'s ``except BillingUnavailable``. Each asserts the RunRejected
# raised is not a BillingUnavailable instance.
#
# CORRECTION (codex p2-4, 2026-08-06 review-fail): an earlier version of this
# comment claimed these were mutation-verified by widening prepare()'s
# except to plain RunRejected. That claim was false — the drift checks below
# raise BEFORE the try/except around credentials.ensure_available even runs,
# so widening that except changes nothing for them; they stayed green with
# either except clause. They ARE correctly verified, just by a different
# mutation: retyping the drift ``raise`` statements themselves to
# BillingUnavailable turns them red (see SPEC Evidence). The except-boundary
# itself is what test_prepare_except_boundary_rejects_plain_consistency_error
# below actually pins.
# ---------------------------------------------------------------------------


def test_profile_drift_after_binding_stays_rejected_not_degraded():
    """An employee's already-enforced binding recorded one profile; the
    payer now resolves to a different one. Real defect (drift) — must stay
    closed, never silently re-attributed."""
    from hermes_multitenancy import billing_identity as bi
    from hermes_multitenancy.billing_credentials import BillingIdentity

    class _Store:
        def get_by_profile(self, _profile):
            return None

        def get(self, employee_id):
            return BillingIdentity(
                employee_user_id=employee_id,
                profile_name="old-profile",
                email="sunke@keep.com",
                litellm_user_id="llm-sunke",
                team_id="team-fd",
                migration_state="enforced",
            )

        def put(self, _binding):
            raise AssertionError("profile drift must be rejected before persisting")

    class _Creds:
        def ensure_available(self, *_a, **_k):
            raise AssertionError("profile drift must be rejected before credential issuance")

    class _Req:
        channel = "webui"
        profile_name = "new-profile"
        chat_id = ""
        metadata: dict = {}

    preparer = object.__new__(bi.BillingIdentityPreparer)
    preparer._store = _Store()
    preparer._credentials = _Creds()
    preparer._routing_lock = __import__("threading").Lock()
    payer = bi._ResolvedPayer("sunke", "new-profile", "sunke@keep.com", "")
    preparer._payer = lambda *_a, **_k: payer

    monkey = pytest.MonkeyPatch()
    monkey.setattr(bi, "_payer_selected", lambda _e: True)
    monkey.setattr(bi, "_employee_org_fields", lambda _e: ("sunke@keep.com", "FD"))
    try:
        with pytest.raises(RunRejected, match="profile drift") as excinfo:
            preparer.prepare(_Req())
    finally:
        monkey.undo()
    assert not isinstance(excinfo.value, BillingUnavailable)


def test_email_drift_after_binding_stays_rejected_not_degraded():
    """Same shape, email this time: existing.email disagrees with the
    payer's freshly-resolved org email. Must stay closed."""
    from hermes_multitenancy import billing_identity as bi
    from hermes_multitenancy.billing_credentials import BillingIdentity

    class _Store:
        def get_by_profile(self, _profile):
            return None

        def get(self, employee_id):
            return BillingIdentity(
                employee_user_id=employee_id,
                profile_name="sunke",
                email="old@keep.com",
                litellm_user_id="llm-sunke",
                team_id="team-fd",
                migration_state="enforced",
            )

        def put(self, _binding):
            raise AssertionError("email drift must be rejected before persisting")

    class _Creds:
        def ensure_available(self, *_a, **_k):
            raise AssertionError("email drift must be rejected before credential issuance")

    class _Req:
        channel = "webui"
        profile_name = "sunke"
        chat_id = ""
        metadata: dict = {}

    preparer = object.__new__(bi.BillingIdentityPreparer)
    preparer._store = _Store()
    preparer._credentials = _Creds()
    preparer._routing_lock = __import__("threading").Lock()
    payer = bi._ResolvedPayer("sunke", "sunke", "sunke@keep.com", "")
    preparer._payer = lambda *_a, **_k: payer

    monkey = pytest.MonkeyPatch()
    monkey.setattr(bi, "_payer_selected", lambda _e: True)
    monkey.setattr(bi, "_employee_org_fields", lambda _e: ("new@keep.com", "FD"))
    try:
        with pytest.raises(RunRejected, match="email drift") as excinfo:
            preparer.prepare(_Req())
    finally:
        monkey.undo()
    assert not isinstance(excinfo.value, BillingUnavailable)


def test_account_identity_drift_from_gateway_stays_rejected_not_degraded(tmp_path):
    """The AI Gateway reports a different LiteLLM account (账号漂移) for an
    employee who already has one bound. Falling back here would silently
    move spend onto a different LiteLLM account — must stay closed."""
    import json
    from pathlib import Path

    from hermes_multitenancy.billing_credentials import (
        BillingCredentialManager,
        BillingIdentity,
        _ResolvedPayer,
    )
    from hermes_multitenancy.credentials import CredentialStore

    fixture = json.loads(
        (Path(__file__).parent / "contract_fixtures/hermes_credentials_v1.json").read_text()
    )
    issued = dict(fixture["ensure_issued_response"])

    class _Gateway:
        def ensure(self, **_kwargs):
            return dict(issued)

        def ack(self, payload):
            return {
                **fixture["ack_activated_response"],
                "key_id": payload["key_id"],
                "credential_version": payload["credential_version"],
            }

    manager = BillingCredentialManager(
        vault=CredentialStore(tmp_path / "vault.db", encryption_key="k"),
        gateway=_Gateway(),
        model_base_url="https://litellm.example/v1",
        now_ms=lambda: 1_800_000_000_000,
        probe=lambda _key: None,
    )
    existing = BillingIdentity(
        employee_user_id="alice",
        profile_name="alice",
        email="alice@keep.com",
        litellm_user_id="a-different-litellm-account",
        team_id=issued["team_id"],
        migration_state="enforced",
    )
    payer = _ResolvedPayer("alice", "alice", "alice@keep.com", "FD")

    with pytest.raises(RunRejected, match="LiteLLM identity drift") as excinfo:
        manager.ensure_available(payer, existing)
    assert not isinstance(excinfo.value, BillingUnavailable)


def test_vault_key_mismatch_stays_rejected_not_degraded(tmp_path):
    """codex p1-2 (2026-08-06 review-fail): a credential written under one
    encryption key and read under another is an HMAC authentication failure
    — tamper or key drift, a security event — not 'could not obtain'. The
    previous blanket ``except Exception: raise BillingUnavailable`` in
    _load_payload degraded this. Must stay RunRejected."""
    from hermes_multitenancy.billing_credentials import (
        BillingCredentialManager,
        _PROVIDER,
        _SECRET_KIND,
        _ResolvedPayer,
    )
    from hermes_multitenancy.credentials import CredentialStore

    db_path = tmp_path / "vault.db"
    writer = CredentialStore(db_path, encryption_key="key-A")
    writer.put_credential(
        profile_name="alice",
        subject_id="alice",
        provider=_PROVIDER,
        secret_kind=_SECRET_KIND,
        payload={"contract_version": "1.0", "employee_id": "alice", "api_key": "sk-x"},
        expires_at=9_999_999_999_999,
    )

    manager = BillingCredentialManager(
        vault=CredentialStore(db_path, encryption_key="key-B"),
        gateway=object(),  # never reached — must fail before touching the gateway
        model_base_url="https://litellm.example/v1",
    )
    payer = _ResolvedPayer("alice", "alice", "alice@keep.com", "FD")

    with pytest.raises(RunRejected) as excinfo:
        manager.ensure_available(payer, None)
    assert not isinstance(excinfo.value, BillingUnavailable)


def test_prepare_except_boundary_rejects_plain_consistency_error():
    """Pins the catch boundary itself (codex p2-4): prepare() must catch
    ONLY BillingUnavailable. A plain RunRejected raised deep inside
    credentials.ensure_available (e.g. a consistency check inside the
    credential manager) must propagate untouched, never get degraded.

    Mutation: widen billing_identity.py's ``except BillingUnavailable`` to
    ``except RunRejected`` — this test goes red (prepare() would swallow the
    error and return successfully instead of raising); the drift tests
    above do NOT go red under that same mutation, which is exactly the gap
    this test closes."""
    from hermes_multitenancy import billing_identity as bi

    class _Creds:
        def ensure_available(self, *_a, **_k):
            raise RunRejected("AI Gateway returned credential drift")

    class _Store:
        def get(self, _employee_id):
            return None

        def get_by_profile(self, _profile):
            return None

        def put(self, _binding):
            raise AssertionError("must not persist after a consistency rejection")

    class _Req:
        channel = "webui"
        profile_name = "sunke"
        chat_id = ""
        metadata: dict = {}

    preparer = object.__new__(bi.BillingIdentityPreparer)
    preparer._store = _Store()
    preparer._credentials = _Creds()
    preparer._routing_lock = __import__("threading").Lock()
    payer = bi._ResolvedPayer("sunke", "sunke", "sunke@keep.com", "")
    preparer._payer = lambda *_a, **_k: payer

    monkey = pytest.MonkeyPatch()
    monkey.setattr(bi, "_payer_selected", lambda _e: True)
    monkey.setattr(bi, "_employee_org_fields", lambda _e: ("sunke@keep.com", "FD"))
    try:
        with pytest.raises(RunRejected, match="credential drift") as excinfo:
            preparer.prepare(_Req())
    finally:
        monkey.undo()
    assert not isinstance(excinfo.value, BillingUnavailable)


def test_vault_unavailable_predicate_is_a_whitelist(tmp_path):
    """codex r2 p1-2: matching by exception CLASS (sqlite3.OperationalError /
    OSError) was still too wide — a corrupted schema, a read-only mount, and
    a missing path all raise the same classes as a genuine locked/busy
    condition.

    HONEST SCOPE (codex r3 #2 caught a false claim here — the previous
    docstring said "real sqlite/OS failures, not hand-built exceptions" for
    the WHOLE test, but the OSError section below was entirely hand-built
    ``OSError(code, "message")``, which proves nothing about how
    ``_is_vault_unavailable`` behaves on what the OS actually raises):
    - SQLite section: every case is genuinely triggered (real lock
      contention, a real missing table, a real chmod'd read-only file).
    - OSError section, ENOENT/EISDIR/EACCES: genuinely triggered via real
      filesystem operations.
    - OSError section, EAGAIN/EBUSY/ENOSPC/EROFS: still hand-built. These
      require a real non-blocking I/O race, a real device-busy condition,
      an actually-full disk, or a real read-only-mounted filesystem — none
      practical to construct in a fast, portable, non-root test sandbox.
      Documented here as constructed fixtures, not claimed as real."""
    import os
    import sqlite3

    from hermes_multitenancy.billing_credentials import _is_vault_unavailable

    # --- genuinely transient (locked/busy) → True, degrade ---
    db_path = tmp_path / "locked.db"
    blocker = sqlite3.connect(db_path, timeout=0.05)
    blocker.execute("create table t(x)")
    blocker.execute("begin exclusive")
    blocker.execute("insert into t values (1)")
    other = sqlite3.connect(db_path, timeout=0.05)
    try:
        other.execute("insert into t values (2)")
        other.commit()
        pytest.fail("expected a locked-database error")
    except sqlite3.OperationalError as exc:
        assert _is_vault_unavailable(exc) is True
    finally:
        blocker.rollback()
        blocker.close()
        other.close()

    # --- schema corrupted ("no such table", codex's literal example) →
    # False, stays closed: the store is reachable, its structure is broken ---
    conn = sqlite3.connect(tmp_path / "broken_schema.db")
    try:
        conn.execute("select * from nonexistent_table")
        pytest.fail("expected a missing-table error")
    except sqlite3.OperationalError as exc:
        assert _is_vault_unavailable(exc) is False
    finally:
        conn.close()

    # --- read-only mount (codex's other literal example) → False ---
    ro_path = tmp_path / "ro.db"
    conn = sqlite3.connect(ro_path)
    conn.execute("create table t(x)")
    conn.commit()
    conn.close()
    os.chmod(ro_path, 0o444)
    try:
        conn = sqlite3.connect(ro_path)
        conn.execute("insert into t values (1)")
        conn.commit()
        pytest.fail("expected a readonly-database error")
    except sqlite3.OperationalError as exc:
        assert _is_vault_unavailable(exc) is False
    finally:
        conn.close()
        os.chmod(ro_path, 0o644)

    # --- bare OSError, real ENOENT: a genuinely missing path → False ---
    try:
        open(tmp_path / "does" / "not" / "exist", "r")
        pytest.fail("expected a missing-path error")
    except OSError as exc:
        assert exc.errno == errno.ENOENT
        assert _is_vault_unavailable(exc) is False

    # --- bare OSError, real EISDIR: opening a real directory as a file → False ---
    try:
        open(tmp_path, "r")
        pytest.fail("expected an is-a-directory error")
    except OSError as exc:
        assert exc.errno == errno.EISDIR
        assert _is_vault_unavailable(exc) is False

    # --- bare OSError, real EACCES: a real chmod'd-unreadable file → False ---
    noperm = tmp_path / "noperm"
    noperm.write_text("x")
    os.chmod(noperm, 0o000)
    try:
        open(noperm, "r")
        pytest.fail("expected a permission-denied error (are we running as root?)")
    except OSError as exc:
        assert exc.errno == errno.EACCES
        assert _is_vault_unavailable(exc) is False
    finally:
        os.chmod(noperm, 0o644)

    # --- bare OSError: whitelisted transient errnos → True. CONSTRUCTED
    # fixtures, not real-triggered — EAGAIN/EBUSY/ENOSPC and EROFS need a
    # real non-blocking-I/O race, a real device-busy condition, an
    # actually-full disk, or a real read-only mount, none practical here.
    for code in (errno.EAGAIN, errno.EBUSY, errno.ENOSPC):
        assert _is_vault_unavailable(OSError(code, "transient")) is True

    # --- bare OSError: EROFS also stays closed. CONSTRUCTED fixture, same
    # reason as above. ---
    assert _is_vault_unavailable(OSError(errno.EROFS, "broken")) is False


def test_vault_schema_corruption_stays_rejected_not_degraded(tmp_path):
    """Full wiring, not just the predicate: a table that has actually gone
    missing must surface as RunRejected through the real
    BillingCredentialManager path, not just in the unit-level predicate
    test above."""
    import sqlite3

    from hermes_multitenancy.billing_credentials import (
        BillingCredentialManager,
        _ResolvedPayer,
    )
    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(tmp_path / "vault.db", encryption_key="k")
    store._conn.execute("DROP TABLE multitenancy_credentials")
    store._conn.commit()

    manager = BillingCredentialManager(
        vault=store,
        gateway=object(),  # never reached — must fail before touching the gateway
        model_base_url="https://litellm.example/v1",
    )
    payer = _ResolvedPayer("alice", "alice", "alice@keep.com", "FD")

    with pytest.raises(RunRejected) as excinfo:
        manager.ensure_available(payer, None)
    assert not isinstance(excinfo.value, BillingUnavailable)


def test_degrade_audit_failure_never_blocks_the_request(capsys):
    """codex r2 p1-3, the most important of the three: if EMITTING the audit
    line itself raises (a broken log handler), prepare() must still return
    the unattributed result. An audit failure is never grounds to fail
    closed — that would let the audit call meant to make C observable
    silently undo C at the exact moment it's needed.

    codex r3 #3: not raising isn't enough on its own — the fallback must
    actually carry the audit content, or "never blocks the request" comes
    at the cost of losing the degrade count entirely. Assert the stderr
    fallback line carries the fixed grep token and the employee id;
    deleting the ``print`` in ``_log_billing_degraded`` must turn this red."""
    import logging
    from dataclasses import replace as _replace

    from hermes_multitenancy import billing_identity as bi

    class _ExplodingHandler(logging.Handler):
        def emit(self, record):
            raise RuntimeError("log sink is down")

    class _Creds:
        def ensure_available(self, payer, existing, **kw):
            raise BillingUnavailable("gateway is down")

    class _Store:
        def get(self, _employee_id):
            return None

        def get_by_profile(self, _profile):
            return None

        def put(self, _binding):  # pragma: no cover
            raise AssertionError("a degraded run must not persist a binding")

    class _Req:
        channel = "webui"
        profile_name = "sunke"
        chat_id = ""
        metadata: dict = {}

    preparer = object.__new__(bi.BillingIdentityPreparer)
    preparer._store = _Store()
    preparer._credentials = _Creds()
    preparer._routing_lock = __import__("threading").Lock()
    payer = bi._ResolvedPayer("sunke", "sunke", "sunke@keep.com", "")
    preparer._payer = lambda *_a, **_k: payer

    logger = logging.getLogger("hermes_multitenancy.billing_identity")
    handler = _ExplodingHandler()
    logger.addHandler(handler)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(bi, "_payer_selected", lambda _e: True)
    monkey.setattr(bi, "_employee_org_fields", lambda _e: ("sunke@keep.com", "FD"))
    monkey.setattr(bi, "replace", _replace, raising=False)
    try:
        out = preparer.prepare(_Req())  # must not raise
    finally:
        logger.removeHandler(handler)
        monkey.undo()

    assert "litellm_billing_enforced" not in (out.metadata or {})

    captured = capsys.readouterr()
    assert "billing_degraded_unattributed" in captured.err
    assert "sunke" in captured.err


def test_malformed_hostname_is_config_error_not_degraded():
    """codex 终审 #1: `.port` 只校验端口。带空格/反斜杠/控制字符的 host 照样
    parse 得过、照样打到 opener,回来变成可降级的 broker_unavailable —— 我们
    自己写错的配置披着故障的外衣。负向对照:去掉 _HOSTNAME_RE 这道判断,本用例
    会红在 isinstance 断言上(拿到 BillingUnavailable 而非 RunRejected)。"""
    from hermes_multitenancy.billing_credentials import BillingGatewayClient, _GatewayError

    for bad in ("https://host name.example", "https://host\\name.example", "https://ho\tst.example"):
        client = BillingGatewayClient(base_url=bad, token="t", timeout=1.0)
        with pytest.raises(_GatewayError) as caught:
            client._post("/x", {}, idempotency_key="idem-0001")
        assert caught.value.code == "broker_not_configured", bad


def test_real_sqlite_disk_full_degrades_not_refuses(tmp_path, monkeypatch):
    """codex 终审 #2: 真实磁盘满从 SQLite 冒出来的是 SQLITE_FULL(13),不是
    OSError(ENOSPC) —— 只写 errno 那一侧接不住它,结果'计费磁盘满 → 员工用不了
    AI',正是加 ENOSPC 想避免的后果。负向对照:把 SQLITE_FULL 从
    _SQLITE_TRANSIENT_ERRORCODES 去掉,本用例红。"""
    import sqlite3

    from hermes_multitenancy.billing_credentials import _is_vault_unavailable

    full = sqlite3.OperationalError("database or disk is full")
    full.sqlite_errorcode = sqlite3.SQLITE_FULL
    assert _is_vault_unavailable(full) is True

    # 反面:结构损坏仍然拒绝(这条不能被顺手放过)
    broken = sqlite3.OperationalError("no such table: multitenancy_credentials")
    broken.sqlite_errorcode = sqlite3.SQLITE_ERROR
    assert _is_vault_unavailable(broken) is False


def test_unverified_account_identity_degrades_the_run_but_stores_nothing(tmp_path):
    """codex 终审 #3 + key-client 的交接注释:网关核验不了账号身份时,**不落库**
    (记错人是乱扣钱)但**请求要照常完成**(核验打嗝不该让人用不了 AI)。两个决定
    互不绑定。负向对照:把 BillingUnavailable 改回 RunRejected,本用例红在
    isinstance 断言。"""
    from hermes_multitenancy.billing_credentials import (
        BillingCredentialManager,
        BillingUnavailable,
        _ResolvedPayer,
    )
    from hermes_multitenancy.billing_employee_key import IssuedKey
    from hermes_multitenancy.credentials import CredentialStore

    now = 1_800_000_000_000
    payer = _ResolvedPayer("sunke", "sunke", "sunke@keep.com", "FD")
    unverified = IssuedKey(
        "sunke", "sunke@keep.com", "sk-unverified", "https://llm.example/v1",
        "auto-sunke-20260806-010101-aaaaaa", "FD", now + 30 * 86_400_000,
        "llm-sunke", "team-fd", False,
    )
    manager = BillingCredentialManager(
        vault=CredentialStore(tmp_path / "v.db", encryption_key="sim-key"),
        gateway=None,
        model_base_url="https://llm.example/v1",
        now_ms=lambda: now,
        probe=lambda _k: None,
    )
    with pytest.raises(BillingUnavailable):
        manager.adopt_employee_key(payer, unverified)
    assert manager._load_payload("sunke", "sunke") is None


@pytest.mark.parametrize(
    "url,expect_config_error",
    [
        ("https://hermes.gotokeep.com", False),
        ("https://HERMES.GoToKeep.com", False),
        ("https://broker.example:8443/", False),
        ("https://xn--fsq.example", False),        # punycode
        ("https://例え.jp", False),                 # Unicode IDN
        ("https://host name.example", True),
        ("https://ho\tst.example", True),          # urlparse strips the tab
        ("https://host\\name.example", True),      # idna encodes it; still unsafe
    ],
)
def test_broker_url_validation_rejects_only_real_typos(url, expect_config_error):
    """codex 终审: 第一版 ASCII-only 正则把 Unicode IDN 当成配置错误拒了 ——
    误拒合法配置和放过畸形配置一样坏。改成 ASCII 正则 + IDNA 兜底,但反斜杠等
    结构危险字符走显式黑名单先拦(idna 会老老实实编码反斜杠,而部分 URL 解析器
    把 \\ 当 /,放过它等于把打字错误变成另一个目的地)。负向对照:去掉黑名单 →
    反斜杠那条红;去掉 IDNA 兜底 → 例え.jp 那条红。"""
    from hermes_multitenancy.billing_credentials import BillingGatewayClient, _GatewayError

    client = BillingGatewayClient(base_url=url, token="t", timeout=1.0)
    try:
        client._post("/x", {}, idempotency_key="idem-0001")
        got_config_error = False
    except _GatewayError as exc:
        got_config_error = exc.code == "broker_not_configured"
    except Exception:
        got_config_error = False  # 走到网络层 = 没被当成配置错误
    assert got_config_error is expect_config_error, url
