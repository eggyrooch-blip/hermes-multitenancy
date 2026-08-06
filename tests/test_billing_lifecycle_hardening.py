"""Billing credential lifecycle hardening (adapted from 25ba89a to main).

Origin: branch ``feat/hermes-user-key-probe-fix`` (codex review hardening,
2026-07-22).  Main's billing implementation diverged after the branch was cut
(no scope-binding store, no ``before_ack`` hook, no cross-process payer lock),
so every test here asserts the same security semantics through main's current
seams.  Assertions that have no seam on main are recorded in the SPEC's Dead
ends section — never silently dropped.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest


FIXTURE = json.loads(
    (Path(__file__).parent / "contract_fixtures/hermes_credentials_v1.json").read_text()
)
NOW_MS = 1_800_000_000_000
VAULT_KEY = "billing-lifecycle-test-key"
BASE_URL = "https://litellm.example/v1"


class FakeGateway:
    def __init__(self, ensure_responses, ack_response=None):
        self.ensure_responses = list(ensure_responses)
        self.ack_response = ack_response
        self.ensure_calls: list[dict] = []
        self.ack_calls: list[dict] = []

    def ensure(self, **kwargs):
        self.ensure_calls.append(dict(kwargs))
        value = self.ensure_responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return dict(value)

    def ack(self, payload):
        self.ack_calls.append(dict(payload))
        value = self.ack_response or {
            **FIXTURE["ack_activated_response"],
            "key_id": payload["key_id"],
            "credential_version": payload["credential_version"],
        }
        if isinstance(value, BaseException):
            raise value
        return dict(value)


def _payer(employee_id="alice", profile_name="alice"):
    from hermes_multitenancy.billing_identity import _ResolvedPayer

    return _ResolvedPayer(
        employee_id,
        profile_name,
        f"{employee_id}@keep.com",
        "FD",
    )


def _manager(db_path: Path, gateway, *, probe=None, model_base_url=BASE_URL):
    from hermes_multitenancy.billing_identity import BillingCredentialManager
    from hermes_multitenancy.credentials import CredentialStore

    return BillingCredentialManager(
        vault=CredentialStore(db_path, encryption_key=VAULT_KEY),
        gateway=gateway,
        model_base_url=model_base_url,
        now_ms=lambda: NOW_MS,
        probe=probe or (lambda _key: None),
    )


def _request(sender="ou_alice", profile_name="alice"):
    from hermes_multitenancy.run_models import RunRequest

    return RunRequest(
        channel="feishu",
        profile_name=profile_name,
        user_key=sender,
        content="hello",
        chat_id=f"dm-{sender}",
        metadata={"chat_type": "p2p", "sender_open_id": sender},
    )


class ToggleRouting:
    """Production-shape routing rows (sync-owned, active, user kind)."""

    def __init__(self, users=None):
        self.active = True
        self.users = users or {"ou_alice": ("alice", "alice")}

    def lookup_by_profile_name(self, profile_name):
        if not self.active:
            return None
        for open_id, (_employee, routed_profile) in self.users.items():
            if routed_profile == profile_name:
                return SimpleNamespace(owner_open_id=open_id, open_id=open_id)
        return None

    def lookup_by_chat_id(self, _chat_id):
        return None

    def resolve_owner_root(self, open_id):
        value = self.users.get(open_id) if self.active else None
        if value is None:
            return None
        return SimpleNamespace(
            user_id=value[0],
            profile_name=value[1],
            open_id=open_id,
            active=True,
            kind="user",
            provenance="sync",
        )

    def lookup_by_open_id(self, open_id):
        return self.resolve_owner_root(open_id)

    def lookup_by_user_id(self, user_id):
        return next(
            (
                self.resolve_owner_root(open_id)
                for open_id, (employee, _profile) in self.users.items()
                if employee == user_id
            ),
            None,
        )


def _billing_env(monkeypatch, tmp_path, payer_ids="alice"):
    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", payer_ids)
    monkeypatch.setenv("HERMES_LITELLM_BILLING_BASE_URL", BASE_URL)
    monkeypatch.setenv("HERMES_ORG_SNAPSHOT_DIR", str(tmp_path / "org-snapshots"))


# ---------------------------------------------------------------------------
# https-only endpoint gate (#p0: employee Bearer keys must never ride http)
# ---------------------------------------------------------------------------


def test_billing_endpoint_rejects_plaintext_http():
    from hermes_multitenancy.billing_identity import billing_endpoint_allowed

    assert billing_endpoint_allowed(BASE_URL, BASE_URL)
    assert billing_endpoint_allowed(BASE_URL, "https://litellm.example/v1/models")
    # http is rejected on either side, even when both sides agree on it.
    assert not billing_endpoint_allowed(
        "http://litellm.example/v1", "http://litellm.example/v1"
    )
    assert not billing_endpoint_allowed("http://litellm.example/v1", BASE_URL)
    assert not billing_endpoint_allowed(BASE_URL, "http://litellm.example/v1")
    # loopback/private URLs get no plaintext exemption.
    assert not billing_endpoint_allowed(
        "http://127.0.0.1:4000/v1", "http://127.0.0.1:4000/v1"
    )


def test_http_configured_probe_never_puts_key_on_the_wire(tmp_path, monkeypatch):
    from hermes_multitenancy.run_broker import RunRejected

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail(
            "http endpoint must be rejected before any network I/O"
        ),
    )
    from hermes_multitenancy.billing_identity import BillingCredentialManager
    from hermes_multitenancy.credentials import CredentialStore

    gateway = FakeGateway([FIXTURE["ensure_issued_response"]])
    # No probe override: the real ``_probe_key`` must fail closed on the http
    # endpoint before any network I/O happens.
    manager = BillingCredentialManager(
        vault=CredentialStore(tmp_path / "vault.db", encryption_key=VAULT_KEY),
        gateway=gateway,
        model_base_url="http://litellm.example/v1",
        now_ms=lambda: NOW_MS,
    )

    with pytest.raises(RunRejected):
        manager.ensure_available(_payer(), None)


def test_http_runtime_env_is_consumed_and_rejected(monkeypatch):
    import os

    from hermes_multitenancy.billing_identity import (
        billing_runtime_from_environment,
    )

    monkeypatch.setenv("HERMES_LITELLM_RUNTIME_API_KEY", "employee-key")
    monkeypatch.setenv(
        "HERMES_LITELLM_RUNTIME_BASE_URL", "http://litellm.example/v1"
    )
    monkeypatch.setenv("HERMES_LITELLM_RUNTIME_EMPLOYEE_ID", "alice")

    with pytest.raises(RuntimeError, match="incomplete"):
        billing_runtime_from_environment()

    # The one-shot key is consumed even on rejection so model-visible tools
    # can never read it from the environment afterwards.
    assert os.environ.get("HERMES_LITELLM_RUNTIME_API_KEY") is None
    assert os.environ.get("HERMES_LITELLM_RUNTIME_BASE_URL") is None
    assert os.environ.get("HERMES_LITELLM_RUNTIME_EMPLOYEE_ID") is None


# ---------------------------------------------------------------------------
# lifecycle ordering and fail-closed guarantees
# ---------------------------------------------------------------------------


def test_pre_ack_vault_persist_failure_never_calls_ack(tmp_path: Path):
    import sqlite3

    from hermes_multitenancy.billing_identity import BillingCredentialManager
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.run_broker import RunRejected

    db_path = tmp_path / "vault.db"

    class JournalFailingVault(CredentialStore):
        def put_credential(self, **kwargs):
            # A REAL SQLITE_BUSY (codex r3 #2: a hand-built exception with a
            # manually-assigned ``sqlite_errorcode`` attribute proves
            # nothing about what SQLite actually raises). A second
            # connection holds a genuine EXCLUSIVE lock on this same file,
            # so the underlying INSERT below genuinely fails.
            blocker = sqlite3.connect(db_path, timeout=0.05)
            blocker.execute("BEGIN EXCLUSIVE")
            try:
                return super().put_credential(**kwargs)
            finally:
                blocker.rollback()
                blocker.close()

    gateway = FakeGateway([FIXTURE["ensure_issued_response"]])
    vault = JournalFailingVault(db_path, encryption_key=VAULT_KEY)
    vault._conn.execute("PRAGMA busy_timeout = 50")  # keep the test fast
    manager = BillingCredentialManager(
        vault=vault,
        gateway=gateway,
        model_base_url=BASE_URL,
        now_ms=lambda: NOW_MS,
        probe=lambda _key: None,
    )

    with pytest.raises(RunRejected, match="vault is unavailable"):
        manager.ensure_available(_payer(), None)

    assert len(gateway.ensure_calls) == 1
    assert gateway.ack_calls == []


def test_ack_loss_keeps_enforcement_and_route_loss_fails_closed(
    tmp_path: Path,
    monkeypatch,
):
    from hermes_multitenancy.billing_identity import (
        BillingIdentityPreparer,
        BillingIdentityStore,
        _GatewayError,
    )
    from hermes_multitenancy.run_broker import RunRejected

    db_path = tmp_path / "multitenancy.db"
    routing = ToggleRouting()
    store = BillingIdentityStore(db_path)
    gateway = FakeGateway(
        [FIXTURE["ensure_issued_response"]],
        ack_response=_GatewayError(503, "broker_unavailable", True),
    )
    manager = _manager(db_path, gateway)
    preparer = BillingIdentityPreparer(
        routing=routing,
        store=store,
        credentials=manager,
    )
    _billing_env(monkeypatch, tmp_path)

    first = preparer.prepare(_request())
    assert first.metadata["litellm_billing_enforced"] is True
    assert store.get("alice").migration_state == "enforced"

    # Losing the route (and the global switch) must never downgrade an
    # enforced payer back to shared/legacy billing: fail closed instead.
    routing.active = False
    monkeypatch.delenv("HERMES_LITELLM_BILLING_ENABLED")
    monkeypatch.delenv("HERMES_LITELLM_BILLING_PAYER_IDS")
    with pytest.raises(RunRejected, match="could not be resolved"):
        preparer.prepare(_request())

    assert len(gateway.ensure_calls) == 1


def test_second_sender_on_shared_profile_never_bills_the_first_payer(
    tmp_path: Path,
    monkeypatch,
):
    from hermes_multitenancy.billing_identity import (
        BillingIdentityPreparer,
        BillingIdentityStore,
    )
    from hermes_multitenancy.run_broker import RunRejected

    db_path = tmp_path / "multitenancy.db"
    routing = ToggleRouting(
        {
            "ou_alice": ("alice", "guest"),
            "ou_bob": ("bob", "guest"),
        }
    )
    store = BillingIdentityStore(db_path)
    gateway = FakeGateway([FIXTURE["ensure_issued_response"]])
    preparer = BillingIdentityPreparer(
        routing=routing,
        store=store,
        credentials=_manager(db_path, gateway),
    )
    _billing_env(monkeypatch, tmp_path, payer_ids="alice,bob")

    alice = preparer.prepare(_request("ou_alice", "guest"))
    assert alice.metadata["litellm_billing_employee_user_id"] == "alice"

    # main binds one enforced payer per profile: a second employee on the
    # same profile is rejected outright — never silently billed as alice.
    with pytest.raises(RunRejected):
        preparer.prepare(_request("ou_bob", "guest"))

    assert store.get_by_profile("guest").employee_user_id == "alice"
    assert store.get("bob") is None
    assert len(gateway.ensure_calls) == 1


def test_unknown_migration_state_is_rejected_at_the_store(tmp_path: Path):
    from hermes_multitenancy.billing_identity import (
        BillingIdentity,
        BillingIdentityStore,
    )

    store = BillingIdentityStore(tmp_path / "multitenancy.db")
    with pytest.raises(ValueError, match="invalid billing migration state"):
        store.put(
            BillingIdentity(
                employee_user_id="alice",
                profile_name="alice",
                email="alice@keep.com",
                litellm_user_id="llm-alice",
                team_id="team-fd",
                team_alias="FD",
                key_id="key-1",
                credential_version=1,
                expires_at=NOW_MS + 1,
                migration_state="future",
            )
        )


@pytest.mark.parametrize("contract_version", [None, "2.0"])
def test_unknown_local_vault_contract_fails_before_gateway(
    tmp_path: Path,
    contract_version,
):
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.run_broker import RunRejected

    db_path = tmp_path / "multitenancy.db"
    gateway = FakeGateway([FIXTURE["ensure_issued_response"]])
    manager = _manager(db_path, gateway)
    binding = manager.ensure_available(_payer(), None)
    gateway.ensure_calls.clear()
    vault = CredentialStore(db_path, encryption_key=VAULT_KEY)
    payload = vault.get_secret_for_runtime(
        profile_name="alice",
        subject_id="alice",
        provider="litellm",
        secret_kind="hermes_api_key",
    )
    if contract_version is None:
        payload.pop("contract_version", None)
    else:
        payload["contract_version"] = contract_version
    vault.put_credential(
        profile_name="alice",
        subject_id="alice",
        provider="litellm",
        secret_kind="hermes_api_key",
        payload=payload,
        expires_at=payload["expires_at"],
    )

    with pytest.raises(RunRejected, match="contract version is unsupported"):
        manager.runtime_api_key(
            {
                "litellm_billing_employee_user_id": "alice",
                "litellm_billing_profile_name": "alice",
                "litellm_billing_user_id": binding.litellm_user_id,
                "litellm_billing_team_id": binding.team_id,
                "litellm_billing_key_id": binding.key_id,
                "litellm_billing_credential_version": binding.credential_version,
            }
        )
    with pytest.raises(RunRejected, match="contract version is unsupported"):
        manager.ensure_available(_payer(), binding)
    assert gateway.ensure_calls == []


def test_continuous_credential_gone_reissues_once_then_fails(tmp_path: Path):
    from hermes_multitenancy.billing_identity import _GatewayError
    from hermes_multitenancy.run_broker import RunRejected

    gateway = FakeGateway(
        [FIXTURE["ensure_issued_response"], FIXTURE["ensure_rotated_response"]],
        ack_response=_GatewayError(410, "credential_gone", False),
    )
    manager = _manager(tmp_path / "multitenancy.db", gateway)

    with pytest.raises(RunRejected, match="temporarily unavailable"):
        manager.ensure_available(_payer(), None)

    assert len(gateway.ensure_calls) == 2
    assert len(gateway.ack_calls) == 2


def test_independent_managers_share_one_generation_via_vault(tmp_path: Path):
    db_path = tmp_path / "multitenancy.db"
    gateway = FakeGateway([FIXTURE["ensure_issued_response"]])

    first = _manager(db_path, gateway).ensure_available(_payer(), None)
    second = _manager(db_path, gateway).ensure_available(_payer(), None)

    assert len(gateway.ensure_calls) == 1
    assert len(gateway.ack_calls) == 1
    assert (
        first.key_id
        == second.key_id
        == FIXTURE["ensure_issued_response"]["key_id"]
    )


def test_profile_conflict_is_rejected_before_credential_issue(
    tmp_path: Path,
    monkeypatch,
):
    from hermes_multitenancy.billing_identity import (
        BillingIdentity,
        BillingIdentityPreparer,
        BillingIdentityStore,
    )
    from hermes_multitenancy.run_broker import RunRejected

    db_path = tmp_path / "multitenancy.db"
    store = BillingIdentityStore(db_path)
    store.put(
        BillingIdentity(
            employee_user_id="alice",
            profile_name="guest",
            email="alice@keep.com",
            litellm_user_id="llm-alice",
            team_id="team-fd",
            team_alias="FD",
            key_id="key-1",
            credential_version=1,
            expires_at=NOW_MS + 1,
            migration_state="enforced",
        )
    )
    routing = ToggleRouting({"ou_bob": ("bob", "guest")})
    preparer = BillingIdentityPreparer(
        routing=routing,
        store=store,
        credentials=SimpleNamespace(
            ensure_available=lambda *_args, **_kwargs: pytest.fail(
                "profile conflict must fail before credential issuance"
            )
        ),
    )
    _billing_env(monkeypatch, tmp_path, payer_ids="bob")

    with pytest.raises(RunRejected, match="profile drift"):
        preparer.prepare(_request("ou_bob", "guest"))
