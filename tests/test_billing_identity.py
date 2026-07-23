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
        return (
            SimpleNamespace(
                user_id=value[0],
                profile_name=value[1],
                open_id=open_id,
                active=True,
                kind="user",
                provenance="sync",
            )
            if value
            else None
        )

    def lookup_by_user_id(self, user_id):
        return next(
            (
                self.resolve_owner_root(open_id)
                for open_id, (employee_id, _profile) in self.users.items()
                if employee_id == user_id
            ),
            None,
        )

    def lookup_by_open_id(self, open_id):
        return self.resolve_owner_root(open_id)


class _ProductionShapeRouting(_Routing):
    def lookup_by_user_id(self, user_id):
        value = {"employee-a": "ou_actor"}.get(user_id)
        return (
            SimpleNamespace(
                user_id=user_id,
                profile_name="actor",
                open_id=value,
                kind="user",
                provenance="sync",
                active=True,
            )
            if value
            else None
        )


def _request(
    *,
    chat_type="p2p",
    sender="ou_actor",
    chat_id="oc_dm",
    profile_name="actor",
):
    from hermes_multitenancy.run_models import RunRequest

    return RunRequest(
        channel="feishu",
        profile_name=profile_name,
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
        _request(
            chat_type="group",
            sender="ou_member",
            chat_id="oc_group",
            profile_name="owner",
        )
    )

    assert dm.metadata["litellm_billing_employee_user_id"] == "actor"
    assert group.metadata["litellm_billing_employee_user_id"] == "owner"
    assert "api_key" not in json.dumps(dm.metadata)
    assert [call[0].employee_user_id for call in credentials.calls] == ["actor", "owner"]


def test_production_user_id_is_resolved_to_sync_open_id_before_billing(
    tmp_path, monkeypatch
):
    from hermes_multitenancy.billing_identity import (
        BillingIdentityPreparer,
        BillingIdentityStore,
    )

    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "someone-else")
    credentials = _FakeCredentials()
    preparer = BillingIdentityPreparer(
        routing=_ProductionShapeRouting(),
        store=BillingIdentityStore(tmp_path / "multitenancy.db"),
        credentials=credentials,
    )

    prepared = preparer.prepare(
        _request(sender="employee-a")
    )

    assert prepared.metadata.get("litellm_billing_enforced") is None
    assert credentials.calls == []


def test_incident_replay_keeps_twelve_noncohort_requests_legacy(
    tmp_path, monkeypatch
):
    """Known-gotcha: production Feishu can route with employee user_id, not ou_*.

    The 2026-07-22 incident was twelve requests from six non-canary profiles;
    replay that exact cardinality without retaining employee data.
    """
    from hermes_multitenancy.billing_identity import (
        BillingIdentityPreparer,
        BillingIdentityStore,
    )
    from hermes_multitenancy.run_broker import RunBroker
    from hermes_multitenancy.router import _run_request_for_routed_event
    import asyncio

    class ProductionRouting(_Routing):
        def __init__(self):
            super().__init__()
            self.users = {
                f"ou_fixture_{index}": (f"employee_{index}", f"profile_{index}")
                for index in range(6)
            }

        def lookup_by_profile_name(self, profile_name):
            return next(
                (
                    self.resolve_owner_root(open_id)
                    for open_id, (_employee_id, profile) in self.users.items()
                    if profile == profile_name
                ),
                None,
            )

    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "canary_employee")
    credentials = _FakeCredentials()
    routing = ProductionRouting()
    import hermes_multitenancy.router as router

    monkeypatch.setattr(router, "_get_routing_table", lambda: routing)
    preparer = BillingIdentityPreparer(
        routing=routing,
        store=BillingIdentityStore(tmp_path / "multitenancy.db"),
        credentials=credentials,
    )

    dispatched = []
    emitted = []
    broker = RunBroker(
        dispatch_agent=lambda request: dispatched.append(request) or "ok",
        emit_event=lambda event: emitted.append(event),
        prepare_request=preparer.prepare,
        sandbox_available=lambda: True,
    )
    for index in range(6):
        employee_id = f"employee_{index}"
        for request_index in range(2):
            request = _run_request_for_routed_event(
                event=SimpleNamespace(),
                profile_name=f"profile_{index}",
                sender=employee_id,
                sender_alt=None,
                chat_id=f"oc_fixture_{index}",
                text=f"fixture-{request_index}",
            )
            result = asyncio.run(broker.run(request))
            assert result.content == "ok"

    assert len(dispatched) == 12
    assert credentials.calls == []
    assert all(
        "litellm_billing_enforced" not in item.metadata for item in dispatched
    )
    assert all(event.kind in {"content", "done"} for event in emitted)


def test_noncohort_returns_before_billing_org_initialization(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "owner")
    monkeypatch.setenv("HERMES_LITELLM_EMPLOYEE_EMAIL_DOMAIN", "invalid@domain")
    credentials = _FakeCredentials()

    prepared = _identity_preparer(tmp_path, credentials).prepare(_request())

    assert credentials.calls == []
    assert "litellm_billing_enforced" not in prepared.metadata


def test_routed_request_metadata_never_labels_user_id_as_open_id(monkeypatch):
    import hermes_multitenancy.router as router
    from hermes_multitenancy.router import _run_request_for_routed_event

    row = SimpleNamespace(
        user_id="employee-a",
        open_id="ou_actor",
        kind="user",
        provenance="sync",
        active=True,
    )
    monkeypatch.setattr(
        router,
        "_get_routing_table",
        lambda: SimpleNamespace(lookup_by_user_id=lambda value: row if value == "employee-a" else None),
    )

    request = _run_request_for_routed_event(
        event=SimpleNamespace(),
        profile_name="actor",
        sender="employee-a",
        sender_alt=None,
        chat_id="oc_fixture",
        text="fixture",
    )

    assert request.metadata["sender_open_id"] == "ou_actor"
    assert request.user_key == "ou_actor"


def test_routed_request_rejects_non_open_id_from_sync_lookup(monkeypatch):
    import hermes_multitenancy.router as router
    from hermes_multitenancy.router import _run_request_for_routed_event

    row = SimpleNamespace(
        open_id="employee-b",
        kind="user",
        provenance="sync",
        active=True,
    )
    monkeypatch.setattr(
        router,
        "_get_routing_table",
        lambda: SimpleNamespace(lookup_by_user_id=lambda _value: row),
    )

    request = _run_request_for_routed_event(
        event=SimpleNamespace(),
        profile_name="actor",
        sender="employee-a",
        sender_alt=None,
        chat_id="oc_fixture",
        text="fixture",
    )

    assert request.metadata["sender_open_id"] == ""
    assert request.user_key == "employee-a"


def test_selected_production_user_id_resolves_to_sync_root(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "actor")
    monkeypatch.setenv(
        "HERMES_LITELLM_BILLING_BASE_URL", "https://litellm.example/v1"
    )
    credentials = _FakeCredentials()
    request = _request(sender="actor")

    prepared = _identity_preparer(tmp_path, credentials).prepare(request)

    assert prepared.metadata["litellm_billing_employee_user_id"] == "actor"
    assert [call[0].employee_user_id for call in credentials.calls] == ["actor"]


def test_feishu_sender_and_routed_profile_mismatch_fails_closed(
    tmp_path, monkeypatch
):
    from hermes_multitenancy.run_broker import RunRejected

    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "actor")
    credentials = _FakeCredentials()
    request = _request(sender="ou_owner")

    with pytest.raises(RunRejected, match="could not be resolved"):
        _identity_preparer(tmp_path, credentials).prepare(request)
    assert credentials.calls == []


@pytest.mark.parametrize("cohort", ["", "*", "actor,*"])
def test_empty_or_wildcard_cohort_never_selects_a_new_payer(
    tmp_path, monkeypatch, cohort
):
    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", cohort)
    credentials = _FakeCredentials()

    prepared = _identity_preparer(tmp_path, credentials).prepare(_request())

    assert credentials.calls == []
    assert "litellm_billing_enforced" not in prepared.metadata


@pytest.mark.parametrize("channel", ["webui", "cron", "kanban"])
def test_non_feishu_entrypoints_bill_profile_owner(
    tmp_path, monkeypatch, channel
):
    from hermes_multitenancy.run_models import RunRequest

    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "actor")
    monkeypatch.setenv(
        "HERMES_LITELLM_BILLING_BASE_URL", "https://litellm.example/v1"
    )
    request = RunRequest(
        channel=channel,
        profile_name="actor",
        user_key="untrusted-caller",
        content="hello",
        # Non-Feishu metadata is caller-controlled and must not spoof the
        # canonical owner of an existing Feishu group.
        chat_id="oc_group",
        metadata={"chat_type": "group", "sender_open_id": "ou_member"},
    )

    prepared = _identity_preparer(tmp_path).prepare(request)

    assert prepared.metadata["litellm_billing_employee_user_id"] == "actor"


def test_auto_provisioned_non_sync_route_cannot_become_a_billing_payer(
    tmp_path, monkeypatch
):
    from hermes_multitenancy.billing_identity import (
        BillingIdentityPreparer,
        BillingIdentityStore,
    )
    from hermes_multitenancy.run_broker import RunRejected

    class UntrustedRouting(_Routing):
        def resolve_owner_root(self, _open_id):
            return None

        def lookup_by_open_id(self, _open_id):
            return SimpleNamespace(user_id="looks-real", profile_name="actor")

    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    credentials = _FakeCredentials()
    preparer = BillingIdentityPreparer(
        routing=UntrustedRouting(),
        store=BillingIdentityStore(tmp_path / "multitenancy.db"),
        credentials=credentials,
    )

    with pytest.raises(RunRejected, match="could not be resolved"):
        preparer.prepare(_request())
    assert credentials.calls == []


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


def test_enforced_profile_identity_mismatch_stops_at_run_broker_when_billing_off(
    tmp_path, monkeypatch
):
    import asyncio

    from hermes_multitenancy.billing_identity import (
        BillingIdentity,
        BillingIdentityPreparer,
        BillingIdentityStore,
    )
    from hermes_multitenancy.run_broker import RunBroker, RunRejected

    monkeypatch.delenv("HERMES_LITELLM_BILLING_ENABLED", raising=False)
    store = BillingIdentityStore(tmp_path / "multitenancy.db")
    store.put(BillingIdentity(
        "actor",
        "actor",
        "actor@keep.com",
        "llm-actor",
        "team-fd",
        "FD",
        "key-actor",
        1,
        FIXTURE["ensure_issued_response"]["expires_at"],
        "enforced",
    ))
    preparer = BillingIdentityPreparer(
        routing=_Routing(),
        store=store,
        credentials=_FakeCredentials(),
    )
    dispatched = []
    emitted = []
    broker = RunBroker(
        dispatch_agent=lambda request: dispatched.append(request) or "unexpected",
        emit_event=lambda event: emitted.append(event),
        prepare_request=preparer.prepare,
        sandbox_available=lambda: True,
    )

    with pytest.raises(RunRejected, match="could not be resolved"):
        asyncio.run(broker.run(_request(sender="ou_owner")))

    assert dispatched == []
    assert emitted == []


def test_enforced_group_missing_owner_route_stops_when_billing_off(
    tmp_path, monkeypatch
):
    from hermes_multitenancy.billing_identity import (
        BillingIdentity,
        BillingIdentityPreparer,
        BillingIdentityStore,
    )
    from hermes_multitenancy.run_broker import RunRejected

    class MissingGroupOwnerRouting(_Routing):
        def lookup_by_chat_id(self, _chat_id):
            return None

        def lookup_by_profile_name(self, _profile_name):
            return SimpleNamespace(owner_open_id="ou_owner", open_id="ou_owner")

    monkeypatch.delenv("HERMES_LITELLM_BILLING_ENABLED", raising=False)
    store = BillingIdentityStore(tmp_path / "multitenancy.db")
    store.put(BillingIdentity(
        "owner",
        "owner",
        "owner@keep.com",
        "llm-owner",
        "team-fd",
        "FD",
        "key-owner",
        1,
        FIXTURE["ensure_issued_response"]["expires_at"],
        "enforced",
    ))
    preparer = BillingIdentityPreparer(
        routing=MissingGroupOwnerRouting(),
        store=store,
        credentials=_FakeCredentials(),
    )

    with pytest.raises(RunRejected, match="could not be resolved"):
        preparer.prepare(_request(
            chat_type="group",
            sender="ou_member",
            chat_id="oc_missing",
            profile_name="group_oc_missing",
        ))


def test_multiple_group_profiles_bill_the_same_trusted_owner(
    tmp_path, monkeypatch
):
    class MultipleGroupsRouting(_Routing):
        def lookup_by_chat_id(self, chat_id):
            if chat_id in {"oc_one", "oc_two"}:
                return SimpleNamespace(owner_open_id="ou_owner")
            return None

        def lookup_by_profile_name(self, _profile_name):
            return None

    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "owner")
    credentials = _FakeCredentials()
    from hermes_multitenancy.billing_identity import (
        BillingIdentityPreparer,
        BillingIdentityStore,
    )

    preparer = BillingIdentityPreparer(
        routing=MultipleGroupsRouting(),
        store=BillingIdentityStore(tmp_path / "multitenancy.db"),
        credentials=credentials,
    )
    first = preparer.prepare(_request(
        chat_type="group",
        sender="ou_member",
        chat_id="oc_one",
        profile_name="group_oc_one",
    ))
    second = preparer.prepare(_request(
        chat_type="group",
        sender="ou_member",
        chat_id="oc_two",
        profile_name="group_oc_two",
    ))

    assert first.metadata["litellm_billing_employee_user_id"] == "owner"
    assert second.metadata["litellm_billing_employee_user_id"] == "owner"
    assert [call[0].employee_user_id for call in credentials.calls] == [
        "owner",
        "owner",
    ]


def test_noncohort_group_ignores_stale_billing_profile_binding(
    tmp_path, monkeypatch
):
    from hermes_multitenancy.billing_identity import (
        BillingIdentity,
        BillingIdentityPreparer,
        BillingIdentityStore,
    )

    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "someone-else")
    store = BillingIdentityStore(tmp_path / "multitenancy.db")
    store.put(BillingIdentity(
        "stale-employee",
        "group_oc_group",
        "stale@keep.com",
        "llm-stale",
        "team-stale",
        "FD",
        "key-stale",
        1,
        FIXTURE["ensure_issued_response"]["expires_at"],
        "legacy",
    ))
    credentials = _FakeCredentials()
    preparer = BillingIdentityPreparer(
        routing=_Routing(),
        store=store,
        credentials=credentials,
    )
    request = _request(
        chat_type="group",
        sender="ou_member",
        chat_id="oc_group",
        profile_name="group_oc_group",
    )

    assert preparer.prepare(request) == request
    assert credentials.calls == []


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
    reused_again = manager.ensure_available(_payer(), binding)

    assert reused.key_id == binding.key_id
    assert reused_again.key_id == binding.key_id
    assert gateway.ensure_calls[-1]["reason"] == "renewal"
    assert len(gateway.ensure_calls) == 2


def test_renewal_probe_failure_restores_unexpired_previous_key(tmp_path):
    issued = dict(FIXTURE["ensure_issued_response"])
    issued["expires_at"] = NOW_MS + 20 * 24 * 60 * 60 * 1000
    rotated = dict(FIXTURE["ensure_rotated_response"])
    probes = []

    def probe(key):
        probes.append(key)
        if key == rotated["api_key"]:
            raise RuntimeError("probe unavailable")

    gateway = _FakeGateway([issued, rotated])
    manager = _manager(tmp_path, gateway, probe=probe)
    previous = manager.ensure_available(_payer(), None)

    reused = manager.ensure_available(_payer(), previous)
    reused_again = manager.ensure_available(_payer(), previous)

    assert reused.key_id == previous.key_id
    assert reused_again.key_id == previous.key_id
    assert len(gateway.ensure_calls) == 2
    assert manager.runtime_api_key(_metadata(reused)) == issued["api_key"]


def test_ack_retryable_failure_uses_key_and_backs_off(tmp_path):
    from hermes_multitenancy.billing_identity import _GatewayError

    gateway = _FakeGateway(
        [FIXTURE["ensure_issued_response"]],
        ack_response=_GatewayError(503, "broker_unavailable", True),
    )
    manager = _manager(tmp_path, gateway)

    binding = manager.ensure_available(_payer(), None)
    reused = manager.ensure_available(_payer(), binding)

    assert reused == binding
    assert len(gateway.ack_calls) == 1


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


def test_gateway_rejects_credential_or_query_in_control_plane_url():
    from hermes_multitenancy.billing_identity import BillingGatewayClient, _GatewayError

    for base_url in (
        "https://user:password@gateway.example",
        "https://gateway.example?target=other",
        "http://gateway.example",
    ):
        client = BillingGatewayClient(
            base_url,
            "token",
            opener=lambda *_args, **_kwargs: pytest.fail("invalid URL must not open"),
        )
        with pytest.raises(_GatewayError, match="broker_not_configured"):
            client.ensure(
                employee_id="alice",
                enterprise_email="alice@keep.com",
                department_alias="FD",
                reason="missing",
            )


def test_gateway_rejects_error_envelope_without_message():
    from hermes_multitenancy.billing_identity import BillingGatewayClient, _GatewayError

    def bad_error(request, *, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            503,
            "unavailable",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "contract_version": "1.0",
                        "error": {"code": "broker_unavailable", "retryable": True},
                    }
                ).encode()
            ),
        )

    client = BillingGatewayClient("https://gateway.example", "token", opener=bad_error)
    with pytest.raises(_GatewayError, match="invalid_error_envelope"):
        client.ensure(
            employee_id="alice",
            enterprise_email="alice@keep.com",
            department_alias="FD",
            reason="missing",
        )


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
