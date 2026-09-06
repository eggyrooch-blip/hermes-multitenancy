from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


NOW_MS = 1_800_000_000_000
ACTOR = "ou_actor_secret"
PROFILE = "profile-secret"
API_KEY = "sk-actor-runtime-secret"
SPEND_KEY = hashlib.sha256(API_KEY.encode()).hexdigest()
RUN_ID = "run-123"
AUDIENCE = "hermes-local-t05"


@pytest.fixture(autouse=True)
def _zero_observation_window_by_default(monkeypatch):
    import hermes_multitenancy.single_actor_spend_receipt as receipt_module

    monkeypatch.setattr(receipt_module, "_SPEND_POLL_TIMEOUT_S", 0)


class _Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Routing:
    mode = "ok"

    def lookup_by_open_id(self, actor):
        if self.mode == "missing":
            return None
        return SimpleNamespace(
            user_id="employee-a",
            profile_name=PROFILE if self.mode != "profile" else "other-profile",
            open_id=actor,
            active=True,
            kind="user",
            provenance="sync",
        )


class _Store:
    mode = "ok"

    def get(self, employee_user_id):
        from hermes_multitenancy.billing_credentials import BillingIdentity

        if self.mode == "missing":
            return None
        return BillingIdentity(
            employee_user_id=employee_user_id,
            profile_name=PROFILE,
            email="employee-a@example.invalid",
            litellm_user_id="litellm-a",
            team_id="team-a",
            team_alias="team-a",
            key_id="key-a",
            credential_version=1,
            expires_at=NOW_MS + (1 if self.mode == "expired" else 3_600_000),
            migration_state="legacy" if self.mode == "legacy" else "enforced",
        )


class _Credentials:
    api_key = API_KEY

    def runtime_api_key(self, metadata):
        assert metadata["litellm_billing_employee_user_id"] == "employee-a"
        assert metadata["litellm_billing_profile_name"] == PROFILE
        return self.api_key


class _SpendClient:
    def __init__(self, mode="ok", request_id="new-request"):
        self.mode = mode
        self.request_id = request_id
        self.snapshot = 0
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        assert path == "/spend/logs/v2"
        assert params["api_key"] == SPEND_KEY
        page = params["page"]
        if page == 1:
            rows = [
                {"request_id": "old-request", "api_key": SPEND_KEY, "spend": "0.001"},
            ]
            if self.snapshot and self.mode != "zero":
                rows.append(
                    {"request_id": self.request_id, "api_key": SPEND_KEY, "spend": "0.002"}
                )
            if self.mode == "leak" and self.snapshot:
                rows[-1]["api_key"] = "another-key"
            if self.mode == "duplicate" and self.snapshot:
                rows[-1]["request_id"] = "old-request"
            payload = {
                "data": rows,
                "page": 1,
                "page_size": 100,
                "total": len(rows),
                "total_pages": 1,
                "total_is_capped": self.mode == "capped",
            }
            self.snapshot += 1
            return payload
        raise AssertionError("unexpected page")


def _issue(*, client=None, routing=None, store=None, model_call=None, principal=None):
    from hermes_multitenancy.single_actor_spend_receipt import (
        issue_single_actor_spend_receipt,
    )
    from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal

    private_key = Ed25519PrivateKey.generate()
    principal = principal or issue_webui_principal(
            profile_name=PROFILE,
            actor_subject=ACTOR,
            credential_subject=ACTOR,
        )
    fingerprint_key = b"fingerprint-secret-that-is-at-least-32-bytes"
    receipt = issue_single_actor_spend_receipt(
        principal=principal,
        routing=routing or _Routing(),
        store=store or _Store(),
        credentials=_Credentials(),
        client=client or _SpendClient(),
        model_call=model_call or (lambda _key: "new-request"),
        profile_is_solely_owned=lambda profile, actor: (
            profile == PROFILE and actor == ACTOR
        ),
        run_id=RUN_ID,
        audience=AUDIENCE,
        billing_base_url="https://litellm.sre.example.com",
        signing_key=private_key,
        fingerprint_key=fingerprint_key,
        now_ms=NOW_MS,
    )
    return receipt, private_key.public_key(), principal, fingerprint_key


def test_exact_actor_key_is_measured_once_and_receipt_verifies():
    from hermes_multitenancy.single_actor_spend_receipt import (
        verify_single_actor_spend_receipt,
    )

    calls = []
    client = _SpendClient()
    def model_call(key):
        calls.append(key)
        return "new-request"

    receipt, public_key, principal, fingerprint_key = _issue(
        client=client, model_call=model_call
    )
    verified = verify_single_actor_spend_receipt(
        receipt,
        public_key=public_key,
        expected_run_id=RUN_ID,
        expected_audience=AUDIENCE,
        expected_principal=principal,
        expected_api_key=API_KEY,
        fingerprint_key=fingerprint_key,
        now_ms=NOW_MS,
    )

    assert calls == [API_KEY]
    assert [call[1]["page"] for call in client.calls] == [1, 1]
    assert verified["spend_delta"] == "0.002"
    assert verified["request_count"] == 1
    serialized = json.dumps(receipt)
    assert all(secret not in serialized for secret in (ACTOR, PROFILE, API_KEY))
    assert API_KEY not in json.dumps(client.calls)


def test_both_snapshots_read_every_page():
    class PagedClient:
        def __init__(self):
            self.snapshot = 0
            self.pages = []

        def get(self, path, params):
            assert path == "/spend/logs/v2"
            page = params["page"]
            current_snapshot = self.snapshot
            self.pages.append(page)
            if page == 1:
                rows = [
                    {"request_id": f"old-{index}", "api_key": SPEND_KEY, "spend": "0"}
                    for index in range(100)
                ]
            else:
                rows = [{"request_id": "old-last", "api_key": SPEND_KEY, "spend": "0"}]
                if current_snapshot:
                    rows.append(
                        {"request_id": "new-last", "api_key": SPEND_KEY, "spend": "0.003"}
                    )
                self.snapshot += 1
            total = 101 + int(bool(current_snapshot))
            return {
                "data": rows,
                "page": page,
                "page_size": 100,
                "total": total,
                "total_pages": 2,
                "total_is_capped": False,
            }

    client = PagedClient()
    receipt, *_ = _issue(client=client, model_call=lambda _key: "new-last")

    assert client.pages == [1, 2, 1, 2]
    assert receipt["spend_delta"] == "0.003"


@pytest.mark.parametrize("mode", ["capped", "leak", "duplicate"])
def test_spend_evidence_fail_closed(mode):
    from hermes_multitenancy.single_actor_spend_receipt import SpendReceiptRejected

    client = _SpendClient(mode)
    with pytest.raises(SpendReceiptRejected):
        _issue(client=client)


def test_finish_polls_until_the_exact_spend_row_appears(monkeypatch):
    import hermes_multitenancy.single_actor_spend_receipt as receipt_module

    class DelayedSpendClient(_SpendClient):
        def get(self, path, params):
            self.calls.append((path, dict(params)))
            assert path == "/spend/logs/v2"
            assert params["api_key"] == SPEND_KEY
            rows = [
                {"request_id": "old-request", "api_key": SPEND_KEY, "spend": "0.001"}
            ]
            if len(self.calls) >= 4:
                rows.append(
                    {"request_id": "new-request", "api_key": SPEND_KEY, "spend": "0.002"}
                )
            return {
                "data": rows,
                "page": 1,
                "page_size": 100,
                "total": len(rows),
                "total_pages": 1,
                "total_is_capped": False,
            }

    clock = _Clock()
    monkeypatch.setattr(receipt_module, "_SPEND_POLL_TIMEOUT_S", 1.5)
    monkeypatch.setattr(receipt_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(receipt_module.time, "sleep", clock.sleep)
    client = DelayedSpendClient()
    receipt, *_ = _issue(client=client)

    assert receipt["request_count"] == 1
    assert len(client.calls) == 7


def test_late_second_row_rejects_before_observation_window_closes(monkeypatch):
    import hermes_multitenancy.single_actor_spend_receipt as receipt_module
    from hermes_multitenancy.single_actor_spend_receipt import SpendReceiptRejected

    class LateSecondSpendClient(_SpendClient):
        def get(self, path, params):
            self.calls.append((path, dict(params)))
            assert path == "/spend/logs/v2"
            assert params["api_key"] == SPEND_KEY
            rows = [
                {"request_id": "old-request", "api_key": SPEND_KEY, "spend": "0.001"}
            ]
            if len(self.calls) >= 2:
                rows.append(
                    {"request_id": "new-request", "api_key": SPEND_KEY, "spend": "0.002"}
                )
            if len(self.calls) >= 4:
                rows.append(
                    {"request_id": "late-request", "api_key": SPEND_KEY, "spend": "0.003"}
                )
            return {
                "data": rows,
                "page": 1,
                "page_size": 100,
                "total": len(rows),
                "total_pages": 1,
                "total_is_capped": False,
            }

    clock = _Clock()
    monkeypatch.setattr(receipt_module, "_SPEND_POLL_TIMEOUT_S", 1.5)
    monkeypatch.setattr(receipt_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(receipt_module.time, "sleep", clock.sleep)
    client = LateSecondSpendClient()

    with pytest.raises(SpendReceiptRejected, match="spend_delta_missing"):
        _issue(client=client)
    assert len(client.calls) == 4


def test_last_moment_first_row_gets_a_full_settle_window(monkeypatch):
    import hermes_multitenancy.single_actor_spend_receipt as receipt_module
    from hermes_multitenancy.single_actor_spend_receipt import SpendReceiptRejected

    class LastMomentSpendClient(_SpendClient):
        def get(self, path, params):
            self.calls.append((path, dict(params)))
            assert path == "/spend/logs/v2"
            assert params["api_key"] == SPEND_KEY
            rows = [
                {"request_id": "old-request", "api_key": SPEND_KEY, "spend": "0.001"}
            ]
            if len(self.calls) >= 4:
                rows.append(
                    {"request_id": "new-request", "api_key": SPEND_KEY, "spend": "0.002"}
                )
            if len(self.calls) >= 5:
                rows.append(
                    {"request_id": "late-request", "api_key": SPEND_KEY, "spend": "0.003"}
                )
            return {
                "data": rows,
                "page": 1,
                "page_size": 100,
                "total": len(rows),
                "total_pages": 1,
                "total_is_capped": False,
            }

    clock = _Clock()
    monkeypatch.setattr(receipt_module, "_SPEND_POLL_TIMEOUT_S", 1)
    monkeypatch.setattr(receipt_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(receipt_module.time, "sleep", clock.sleep)
    client = LastMomentSpendClient()

    with pytest.raises(SpendReceiptRejected, match="spend_delta_missing"):
        _issue(client=client)
    assert len(client.calls) == 5


def test_finish_times_out_when_spend_row_never_appears(monkeypatch):
    import hermes_multitenancy.single_actor_spend_receipt as receipt_module
    from hermes_multitenancy.single_actor_spend_receipt import SpendReceiptRejected

    monkeypatch.setattr(receipt_module, "_SPEND_POLL_TIMEOUT_S", 0)
    with pytest.raises(SpendReceiptRejected, match="spend_delta_missing"):
        _issue(client=_SpendClient("zero"))


@pytest.mark.parametrize("route_mode,store_mode", [
    ("missing", "ok"),
    ("profile", "ok"),
    ("ok", "missing"),
    ("ok", "legacy"),
    ("ok", "expired"),
])
def test_principal_route_and_billing_binding_fail_closed(route_mode, store_mode):
    from hermes_multitenancy.single_actor_spend_receipt import SpendReceiptRejected

    routing = _Routing()
    routing.mode = route_mode
    store = _Store()
    store.mode = store_mode
    with pytest.raises(SpendReceiptRejected):
        _issue(routing=routing, store=store)


def test_tampered_or_stale_receipt_fails_closed():
    from hermes_multitenancy.single_actor_spend_receipt import (
        SpendReceiptRejected,
        verify_single_actor_spend_receipt,
    )

    receipt, public_key, principal, fingerprint_key = _issue()
    tampered = dict(receipt)
    tampered["spend_delta"] = "9"
    with pytest.raises(SpendReceiptRejected):
        verify_single_actor_spend_receipt(
            tampered,
            public_key=public_key,
            expected_run_id=RUN_ID,
            expected_audience=AUDIENCE,
            expected_principal=principal,
            expected_api_key=API_KEY,
            fingerprint_key=fingerprint_key,
            now_ms=NOW_MS,
        )
    with pytest.raises(SpendReceiptRejected):
        verify_single_actor_spend_receipt(
            receipt,
            public_key=public_key,
            expected_run_id=RUN_ID,
            expected_audience=AUDIENCE,
            expected_principal=principal,
            expected_api_key=API_KEY,
            fingerprint_key=fingerprint_key,
            now_ms=receipt["expires_at_ms"],
        )


def test_unsealed_principal_fails_before_spend_or_model_call():
    from hermes_multitenancy.single_actor_spend_receipt import SpendReceiptRejected
    from hermes_multitenancy.trusted_runtime_principal import TrustedRuntimePrincipal

    client = _SpendClient()
    calls = []
    with pytest.raises(SpendReceiptRejected, match="principal_invalid"):
        _issue(
            client=client,
            model_call=lambda key: calls.append(key),
            principal=TrustedRuntimePrincipal(
                channel="webui",
                profile_name=PROFILE,
                actor_subject=ACTOR,
                credential_subject=ACTOR,
            ),
        )
    assert client.calls == []
    assert calls == []


def test_begin_and_finish_span_the_real_model_run_and_consume_state_once():
    from hermes_multitenancy.single_actor_spend_receipt import (
        SpendReceiptRejected,
        begin_single_actor_spend_receipt,
        finish_single_actor_spend_receipt,
        verify_single_actor_spend_receipt,
    )
    from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal

    principal = issue_webui_principal(
        profile_name=PROFILE, actor_subject=ACTOR, credential_subject=ACTOR,
    )
    private_key = Ed25519PrivateKey.generate()
    fingerprint_key = b"fingerprint-secret-that-is-at-least-32-bytes"
    client = _SpendClient(request_id="resp_" + "A" * 463 + "=")
    state = begin_single_actor_spend_receipt(
        principal=principal,
        routing=_Routing(),
        store=_Store(),
        credentials=_Credentials(),
        client=client,
        profile_is_solely_owned=lambda profile, actor: (
            profile == PROFILE and actor == ACTOR
        ),
        run_id=RUN_ID,
        audience=AUDIENCE,
        billing_base_url="https://litellm.sre.example.com",
        signing_key=private_key,
        fingerprint_key=fingerprint_key,
        now_ms=NOW_MS,
    )
    assert len(client.calls) == 1
    assert API_KEY not in repr(state)

    receipt = finish_single_actor_spend_receipt(
        state,
        run_id=RUN_ID,
        audience=AUDIENCE,
        now_ms=NOW_MS + 1_000,
    )
    assert len(client.calls) == 2
    assert verify_single_actor_spend_receipt(
        receipt,
        public_key=private_key.public_key(),
        expected_run_id=RUN_ID,
        expected_audience=AUDIENCE,
        expected_principal=principal,
        expected_api_key=API_KEY,
        fingerprint_key=fingerprint_key,
        now_ms=NOW_MS + 1_000,
    )["request_count"] == 1
    with pytest.raises(SpendReceiptRejected, match="state_invalid"):
        finish_single_actor_spend_receipt(
            state,
            run_id=RUN_ID,
            audience=AUDIENCE,
            model_request_id="new-request",
            now_ms=NOW_MS + 2_000,
        )


def test_finish_rejects_forged_state_wrong_run_or_rotated_key_before_after_read():
    from hermes_multitenancy.single_actor_spend_receipt import (
        SpendReceiptRejected,
        begin_single_actor_spend_receipt,
        finish_single_actor_spend_receipt,
    )
    from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal

    with pytest.raises(SpendReceiptRejected, match="state_invalid"):
        finish_single_actor_spend_receipt(
            object(), run_id=RUN_ID, audience=AUDIENCE,
            model_request_id="new-request", now_ms=NOW_MS,
        )

    def begin(credentials):
        return begin_single_actor_spend_receipt(
            principal=issue_webui_principal(
                profile_name=PROFILE, actor_subject=ACTOR, credential_subject=ACTOR,
            ),
            routing=_Routing(),
            store=_Store(),
            credentials=credentials,
            client=_SpendClient(),
            profile_is_solely_owned=lambda profile, actor: (
                profile == PROFILE and actor == ACTOR
            ),
            run_id=RUN_ID,
            audience=AUDIENCE,
            billing_base_url="https://litellm.sre.example.com",
            signing_key=Ed25519PrivateKey.generate(),
            fingerprint_key=b"fingerprint-secret-that-is-at-least-32-bytes",
            now_ms=NOW_MS,
        )

    with pytest.raises(SpendReceiptRejected, match="state_run_mismatch"):
        finish_single_actor_spend_receipt(
            begin(_Credentials()),
            run_id="another-run",
            audience=AUDIENCE,
            model_request_id="new-request",
            now_ms=NOW_MS + 1_000,
        )

    with pytest.raises(SpendReceiptRejected, match="state_audience_mismatch"):
        finish_single_actor_spend_receipt(
            begin(_Credentials()),
            run_id=RUN_ID,
            audience="wrong-audience",
            model_request_id="new-request",
            now_ms=NOW_MS + 1_000,
        )

    with pytest.raises(SpendReceiptRejected, match="state_stale"):
        finish_single_actor_spend_receipt(
            begin(_Credentials()),
            run_id=RUN_ID,
            audience=AUDIENCE,
            model_request_id="new-request",
            now_ms=NOW_MS + 10 * 60 * 1000,
        )

    credentials = _Credentials()
    state = begin(credentials)
    credentials.api_key = "sk-rotated-runtime-secret"
    with pytest.raises(SpendReceiptRejected, match="state_key_mismatch"):
        finish_single_actor_spend_receipt(
            state,
            run_id=RUN_ID,
            audience=AUDIENCE,
            model_request_id="new-request",
            now_ms=NOW_MS + 1_000,
        )


def test_reverse_profile_ambiguity_fails_before_spend_or_model_call():
    from hermes_multitenancy.single_actor_spend_receipt import (
        SpendReceiptRejected,
        issue_single_actor_spend_receipt,
    )
    from hermes_multitenancy.trusted_runtime_principal import issue_webui_principal

    client = _SpendClient()
    calls = []
    with pytest.raises(SpendReceiptRejected, match="actor_route_invalid"):
        issue_single_actor_spend_receipt(
            principal=issue_webui_principal(
                profile_name=PROFILE,
                actor_subject=ACTOR,
                credential_subject=ACTOR,
            ),
            routing=_Routing(),
            store=_Store(),
            credentials=_Credentials(),
            client=client,
            model_call=lambda key: calls.append(key),
            profile_is_solely_owned=lambda _profile, _actor: False,
            run_id=RUN_ID,
            audience=AUDIENCE,
            billing_base_url="https://litellm.sre.example.com",
            signing_key=Ed25519PrivateKey.generate(),
            fingerprint_key=b"fingerprint-secret-that-is-at-least-32-bytes",
            now_ms=NOW_MS,
        )
    assert client.calls == []
    assert calls == []
