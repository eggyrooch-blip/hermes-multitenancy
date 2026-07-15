from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


class _Store:
    def __init__(self):
        self.values = {}

    def get(self, employee_user_id):
        return self.values.get(employee_user_id)

    def put(self, identity):
        self.values[identity.employee_user_id] = identity


class _Routing:
    def __init__(self):
        self.group_owner = "ou_owner"
        self.profile_owner = "ou_actor"
        self.users = {
            "ou_actor": "actor",
            "ou_owner": "owner",
            "ou_member": "member",
        }

    def lookup_by_chat_id(self, chat_id):
        return SimpleNamespace(owner_open_id=self.group_owner) if chat_id == "oc_group" else None

    def lookup_by_profile_name(self, profile_name):
        return SimpleNamespace(owner_open_id=self.profile_owner, open_id=self.profile_owner)

    def resolve_owner_root(self, open_id):
        user_id = self.users.get(open_id)
        return SimpleNamespace(user_id=user_id) if user_id else None

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


def _preparer(routing=None):
    from hermes_multitenancy.billing_identity import BillingIdentityPreparer

    calls = []

    def ensure(employee_user_id):
        calls.append(employee_user_id)
        return {
            "user_id": employee_user_id,
            "email": f"{employee_user_id}@keep.com",
            "litellm_user_id": f"llm-{employee_user_id}",
            "created": True,
        }

    return BillingIdentityPreparer(
        routing=routing or _Routing(),
        store=_Store(),
        ensure_user=ensure,
        billing_base_url="https://litellm.example/v1",
    ), calls


def test_dm_identity_is_created_once_then_reused():
    preparer, calls = _preparer()

    first = preparer.prepare(_request())
    second = preparer.prepare(_request())

    assert calls == ["actor"]
    assert first.metadata["litellm_billing_user_id"] == "llm-actor"
    assert second.metadata["litellm_billing_email"] == "actor@keep.com"


def test_existing_mapping_survives_identity_service_outage_and_new_user_fails():
    from hermes_multitenancy.billing_identity import (
        BillingIdentity,
        BillingIdentityPreparer,
    )
    from hermes_multitenancy.run_broker import RunRejected

    store = _Store()
    store.put(BillingIdentity("actor", "actor@keep.com", "llm-actor"))

    def unavailable(_employee_user_id):
        raise RunRejected("employee billing identity service is unavailable")

    preparer = BillingIdentityPreparer(
        routing=_Routing(),
        store=store,
        ensure_user=unavailable,
        billing_base_url="https://litellm.example/v1",
    )

    existing = preparer.prepare(_request())
    assert existing.metadata["litellm_billing_user_id"] == "llm-actor"

    routing = _Routing()
    routing.users["ou_actor"] = "new-actor"
    new_preparer = BillingIdentityPreparer(
        routing=routing,
        store=store,
        ensure_user=unavailable,
        billing_base_url="https://litellm.example/v1",
    )
    with pytest.raises(RunRejected, match="identity service is unavailable"):
        new_preparer.prepare(_request())


def test_group_is_billed_to_group_owner_not_sender():
    preparer, calls = _preparer()

    prepared = preparer.prepare(
        _request(chat_type="group", sender="ou_member", chat_id="oc_group")
    )

    assert calls == ["owner"]
    assert prepared.metadata["litellm_billing_employee_user_id"] == "owner"


def test_unresolved_employee_fails_before_model_dispatch():
    routing = _Routing()
    routing.profile_owner = "ou_unknown"
    preparer, _calls = _preparer(routing)

    from hermes_multitenancy.run_broker import RunRejected

    with pytest.raises(RunRejected, match="could not be resolved"):
        preparer.prepare(_request(sender="ou_unknown"))


def test_disabled_billing_strips_spoofed_reserved_metadata(monkeypatch):
    from hermes_multitenancy.billing_identity import prepare_billing_request

    monkeypatch.delenv("HERMES_LITELLM_BILLING_ENABLED", raising=False)
    request = _request()
    request = request.__class__(
        **{
            **request.__dict__,
            "metadata": {
                **request.metadata,
                "litellm_billing_user_id": "spoofed",
                "litellm_billing_base_url": "https://evil.example/v1",
            },
        }
    )

    prepared = asyncio.run(prepare_billing_request(request))

    assert "litellm_billing_user_id" not in prepared.metadata
    assert "litellm_billing_base_url" not in prepared.metadata


def test_headers_require_allowed_path_on_exact_billing_origin(monkeypatch):
    from hermes_multitenancy.billing_identity import request_overrides_for_endpoint

    metadata = {
        "litellm_billing_user_id": "llm-actor",
        "litellm_billing_base_url": "https://litellm.example/v1",
    }

    same = request_overrides_for_endpoint(metadata, "https://litellm.example/v1/")
    anthropic = request_overrides_for_endpoint(
        metadata, "https://litellm.example/anthropic"
    )
    other = request_overrides_for_endpoint(metadata, "https://api.external.example/v1")
    unapproved = request_overrides_for_endpoint(
        metadata, "https://litellm.example/admin"
    )

    assert same["extra_headers"]["X-Hermes-User-Id"] == "llm-actor"
    assert anthropic["extra_headers"]["X-Hermes-User-Id"] == "llm-actor"
    assert other == {}
    assert unapproved == {}
