from __future__ import annotations

import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import urllib.error

import pytest


FIXTURE = json.loads(
    (Path(__file__).parent / "contract_fixtures/hermes_credentials_v1.json").read_text()
)
NOW_MS = 1_800_000_000_000
VAULT_KEY = "billing-test-vault-key"


class _Routing:
    def __init__(self):
        self.group_owner = "ou_owner"
        self.profile_owner = "ou_actor"
        self.users = {
            "ou_actor": ("actor", "actor"),
            "ou_owner": ("owner", "owner"),
            "ou_member": ("member", "member"),
        }

    def lookup_by_chat_id(self, chat_id):
        return SimpleNamespace(owner_open_id=self.group_owner) if chat_id == "oc_group" else None

    def lookup_by_profile_name(self, _profile_name):
        return SimpleNamespace(owner_open_id=self.profile_owner, open_id=self.profile_owner)

    def resolve_owner_root(self, open_id):
        value = self.users.get(open_id)
        return SimpleNamespace(user_id=value[0], profile_name=value[1]) if value else None

    def lookup_by_open_id(self, open_id):
        return self.resolve_owner_root(open_id)


def _request(*, chat_type="p2p", sender="ou_actor", chat_id="oc_dm"):
    from hermes_multitenancy.run_models import RunRequest

    return RunRequest(
        channel="feishu",
        profile_name="actor",
        user_key=sender,
        content="hello",
        chat_id=chat_id,
        metadata={"chat_type": chat_type, "sender_open_id": sender},
    )


class _FakeCredentials:
    def __init__(self):
        self.calls = []

    def ensure_available(self, payer, existing, *, force_reason=""):
        from hermes_multitenancy.billing_identity import BillingIdentity

        self.calls.append((payer, existing, force_reason))
        return BillingIdentity(
            employee_user_id=payer.employee_user_id,
            profile_name=payer.profile_name,
            email=payer.email,
            litellm_user_id=f"llm-{payer.employee_user_id}",
            team_id="team-fd",
            team_alias="FD",
            key_id=f"key-{payer.employee_user_id}",
            credential_version=1,
            expires_at=FIXTURE["ensure_issued_response"]["expires_at"],
            migration_state="enforced",
        )


def _identity_preparer(tmp_path, credentials=None):
    from hermes_multitenancy.billing_identity import (
        BillingIdentityPreparer,
        BillingIdentityStore,
    )

    return BillingIdentityPreparer(
        routing=_Routing(),
        store=BillingIdentityStore(tmp_path / "multitenancy.db"),
        credentials=credentials or _FakeCredentials(),
    )


def test_legacy_payer_remains_shared_until_selected(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_LITELLM_BILLING_ENABLED", raising=False)
    credentials = _FakeCredentials()
    prepared = _identity_preparer(tmp_path, credentials).prepare(_request())

    assert credentials.calls == []
    assert "litellm_billing_enforced" not in prepared.metadata


def test_selected_dm_is_enforced_and_group_uses_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "actor,owner")
    monkeypatch.setenv("HERMES_LITELLM_BILLING_BASE_URL", "https://litellm.example/v1")
    credentials = _FakeCredentials()
    preparer = _identity_preparer(tmp_path, credentials)

    dm = preparer.prepare(_request())
    group = preparer.prepare(
        _request(chat_type="group", sender="ou_member", chat_id="oc_group")
    )

    assert dm.metadata["litellm_billing_employee_user_id"] == "actor"
    assert group.metadata["litellm_billing_employee_user_id"] == "owner"
    assert "api_key" not in json.dumps(dm.metadata)
    assert [call[0].employee_user_id for call in credentials.calls] == ["actor", "owner"]


def test_enforced_state_never_reverts_when_global_switch_turns_off(tmp_path, monkeypatch):
    from hermes_multitenancy.billing_identity import BillingIdentity, BillingIdentityStore

    db_path = tmp_path / "multitenancy.db"
    store = BillingIdentityStore(db_path)
    store.put(BillingIdentity("actor", "actor", "actor@keep.com", "llm-actor"))
    store.put(BillingIdentity(
        "actor", "actor", "actor@keep.com", "llm-actor", "team-fd", "FD",
        "key-1", 1, FIXTURE["ensure_issued_response"]["expires_at"], "enforced",
    ))
    store.put(BillingIdentity("actor", "actor", "actor@keep.com", "llm-other"))
    monkeypatch.delenv("HERMES_LITELLM_BILLING_ENABLED", raising=False)
    credentials = _FakeCredentials()
    preparer = __import__(
        "hermes_multitenancy.billing_identity", fromlist=["BillingIdentityPreparer"]
    ).BillingIdentityPreparer(routing=_Routing(), store=store, credentials=credentials)

    prepared = preparer.prepare(_request())

    assert prepared.metadata["litellm_billing_enforced"] is True
    assert store.get("actor").migration_state == "enforced"


class _FakeGateway:
    def __init__(self, ensure_responses, ack_response=None):
        self.ensure_responses = list(ensure_responses)
        self.ack_response = ack_response
        self.ensure_calls = []
        self.ack_calls = []

    def ensure(self, **kwargs):
        self.ensure_calls.append(kwargs)
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


def _manager(tmp_path, gateway, *, probe=None):
    from hermes_multitenancy.billing_identity import BillingCredentialManager
    from hermes_multitenancy.credentials import CredentialStore

    return BillingCredentialManager(
        vault=CredentialStore(tmp_path / "vault.db", encryption_key=VAULT_KEY),
        gateway=gateway,
        model_base_url="https://litellm.example/v1",
        now_ms=lambda: NOW_MS,
        probe=probe or (lambda _key: None),
    )


def _payer():
    from hermes_multitenancy.billing_identity import _ResolvedPayer

    return _ResolvedPayer("alice", "alice", "alice@keep.com", "FD")


def _metadata(binding):
    from hermes_multitenancy.billing_identity import _metadata_for_binding

    return _metadata_for_binding(binding, "https://litellm.example/v1")


def test_first_issue_is_probed_saved_and_acked(tmp_path):
    gateway = _FakeGateway([FIXTURE["ensure_issued_response"]])
    probes = []
    manager = _manager(tmp_path, gateway, probe=probes.append)

    binding = manager.ensure_available(_payer(), None)

    assert probes == ["sk-test-hermes-v1-alice"]
    assert binding.key_id == "tok_fixture_alice_g1"
    assert gateway.ensure_calls[0]["reason"] == "missing"
    assert gateway.ack_calls[0]["api_key"] == "sk-test-hermes-v1-alice"
    assert manager.runtime_api_key(_metadata(binding)) == "sk-test-hermes-v1-alice"


def test_valid_vault_hit_never_calls_gateway(tmp_path):
    gateway = _FakeGateway([FIXTURE["ensure_issued_response"]])
    manager = _manager(tmp_path, gateway)
    binding = manager.ensure_available(_payer(), None)
    gateway.ensure_calls.clear()
    gateway.ack_calls.clear()

    reused = manager.ensure_available(_payer(), binding)

    assert reused == binding
    assert gateway.ensure_calls == []
    assert gateway.ack_calls == []


def test_renewal_gateway_outage_keeps_unexpired_key(tmp_path):
    from hermes_multitenancy.billing_identity import _GatewayError

    issued = dict(FIXTURE["ensure_issued_response"])
    issued["expires_at"] = NOW_MS + 20 * 24 * 60 * 60 * 1000
    gateway = _FakeGateway([issued, _GatewayError(503, "broker_disabled", True)])
    manager = _manager(tmp_path, gateway)
    binding = manager.ensure_available(_payer(), None)

    reused = manager.ensure_available(_payer(), binding)

    assert reused.key_id == binding.key_id
    assert gateway.ensure_calls[-1]["reason"] == "renewal"


def test_missing_gateway_outage_fails_only_that_payer(tmp_path):
    from hermes_multitenancy.billing_identity import _GatewayError
    from hermes_multitenancy.run_broker import RunRejected

    manager = _manager(
        tmp_path,
        _FakeGateway([_GatewayError(503, "broker_disabled", True)]),
    )

    with pytest.raises(RunRejected, match="temporarily unavailable"):
        manager.ensure_available(_payer(), None)


def test_unchanged_requires_exact_local_pair(tmp_path):
    from hermes_multitenancy.run_broker import RunRejected

    manager = _manager(
        tmp_path,
        _FakeGateway([FIXTURE["ensure_unchanged_response"]]),
    )

    with pytest.raises(RunRejected, match="invalid credential state"):
        manager.ensure_available(_payer(), None)


def test_ack_credential_gone_discards_generation_and_reissues(tmp_path):
    from hermes_multitenancy.billing_identity import _GatewayError

    gateway = _FakeGateway(
        [FIXTURE["ensure_issued_response"], FIXTURE["ensure_rotated_response"]],
        ack_response=_GatewayError(410, "credential_gone", False),
    )
    manager = _manager(tmp_path, gateway)
    ack_count = 0

    def ack_then_ok(payload):
        nonlocal ack_count
        gateway.ack_calls.append(dict(payload))
        ack_count += 1
        if ack_count == 1:
            raise _GatewayError(410, "credential_gone", False)
        return {
            **FIXTURE["ack_activated_response"],
            "key_id": "tok_fixture_alice_g2",
            "credential_version": 2,
        }

    gateway.ack = ack_then_ok
    binding = manager.ensure_available(_payer(), None)

    assert binding.key_id == "tok_fixture_alice_g2"
    assert [call["reason"] for call in gateway.ensure_calls] == ["missing", "missing"]


def test_invalid_credential_is_rotated_and_not_served_from_cache(tmp_path):
    gateway = _FakeGateway([
        FIXTURE["ensure_issued_response"], FIXTURE["ensure_rotated_response"],
    ])
    manager = _manager(tmp_path, gateway)
    binding = manager.ensure_available(_payer(), None)
    manager.mark_invalid(_metadata(binding))

    rotated = manager.ensure_available(_payer(), binding)

    assert rotated.credential_version == 2
    assert gateway.ensure_calls[-1]["reason"] == "invalid_401"


def test_per_payer_single_flight_creates_one_generation(tmp_path):
    response = FIXTURE["ensure_issued_response"]
    entered = threading.Event()
    release = threading.Event()

    class SlowGateway(_FakeGateway):
        def ensure(self, **kwargs):
            self.ensure_calls.append(kwargs)
            entered.set()
            assert release.wait(2)
            return dict(response)

    gateway = SlowGateway([])
    manager = _manager(tmp_path, gateway)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(manager.ensure_available(_payer(), None)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    assert entered.wait(2)
    release.set()
    for thread in threads:
        thread.join(2)

    assert len(gateway.ensure_calls) == 1
    assert [item.key_id for item in results] == [
        "tok_fixture_alice_g1", "tok_fixture_alice_g1",
    ]


class _Response:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode()
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.raw


def test_gateway_client_matches_shared_fixture_and_ack_hash():
    from hermes_multitenancy.billing_identity import BillingGatewayClient

    requests = []

    def opener(request, *, timeout):
        requests.append((request, timeout))
        if request.full_url.endswith("/ensure"):
            assert json.loads(request.data) == FIXTURE["ensure_request"]
            return _Response(FIXTURE["ensure_issued_response"])
        assert json.loads(request.data) == FIXTURE["ack_request"]
        return _Response(FIXTURE["ack_activated_response"])

    client = BillingGatewayClient(
        "https://gateway.example",
        "fixture-service-token",
        opener=opener,
    )
    issued = client.ensure(**{
        "employee_id": "alice",
        "enterprise_email": "alice@keep.com",
        "department_alias": "FD",
        "reason": "missing",
    })
    client.ack({
        **issued,
        "profile_name": "alice",
    })

    assert all(request.headers["Authorization"] == "Bearer fixture-service-token" for request, _ in requests)
    assert all(request.headers["Idempotency-key"] for request, _ in requests)
    assert FIXTURE["ack_request"]["key_sha256"] == (
        "da436b21a9e13a68408292fb08b6a8887b06c619de274b961f32960239ba9ed8"
    )


def test_gateway_rejects_unknown_major_and_error_envelope():
    from hermes_multitenancy.billing_identity import BillingGatewayClient, _GatewayError

    def bad_major(_request, *, timeout):
        return _Response({"contract_version": "2.0"})

    client = BillingGatewayClient("https://gateway.example", "token", opener=bad_major)
    with pytest.raises(_GatewayError, match="unsupported_contract_version"):
        client.ensure(
            employee_id="alice",
            enterprise_email="alice@keep.com",
            department_alias="FD",
            reason="missing",
        )

    conflict = FIXTURE["identity_conflict"]

    def http_conflict(request, *, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            conflict["status"],
            "conflict",
            {},
            io.BytesIO(json.dumps(conflict["body"]).encode()),
        )

    client = BillingGatewayClient("https://gateway.example", "token", opener=http_conflict)
    with pytest.raises(_GatewayError) as caught:
        client.ensure(
            employee_id="alice",
            enterprise_email="alice@keep.com",
            department_alias="FD",
            reason="missing",
        )
    assert caught.value.code == "identity_conflict"
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("AuthenticationError Error code: 401", "invalid_credential"),
        ("HTTP 429 budget_exceeded", "budget_exceeded"),
        ("status_code=429 rate limit", "rate_limit"),
        ("HTTP 500 upstream", ""),
    ],
)
def test_litellm_error_classification(message, expected):
    from hermes_multitenancy.billing_identity import classify_litellm_error

    assert classify_litellm_error(message) == expected


def test_disabled_path_strips_every_spoofable_field(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_LITELLM_BILLING_ENABLED", raising=False)
    request = _request()
    request = request.__class__(**{
        **request.__dict__,
        "metadata": {
            **request.metadata,
            "litellm_billing_enforced": True,
            "litellm_billing_user_id": "spoofed",
            "litellm_billing_key_id": "spoofed",
        },
    })

    prepared = _identity_preparer(tmp_path).prepare(request)

    assert "litellm_billing_enforced" not in prepared.metadata
    assert "litellm_billing_key_id" not in prepared.metadata
