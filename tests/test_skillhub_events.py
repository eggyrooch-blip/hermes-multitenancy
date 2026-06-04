"""Tests for the AiDock SkillHub webhook receiver.

Covers payload normalization (王克杰's flat shape + PRD §9.5 full envelope),
event_id synthesis + idempotency, signature verification, and the HTTP endpoint
``POST /api/run-broker/skillhub/events`` end to end.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from hermes_multitenancy import skillhub_events


# 王克杰's actual shipped payload (2026-05-27 飞书).
WANGKEJIE_PAYLOAD = {
    "event_type": "skill.install_approved",
    "skill_code": "daily-breaking",
    "release_id": "rel_001",
    "version": "1.0.3",
    "download_url": "https://example.invalid/daily-breaking/1.0.3.zip",
    "skill_status": "active",
    "audience": {
        "auth_type": "auth",
        "users": [{"profile_id": "profile-owner"}],
    },
}

# PRD §9.5 full envelope (nested skill / release.package / audience.type).
PRD_ENVELOPE = {
    "event_id": "evt_202605250001",
    "event_type": "skill.install_approved",
    "schema_version": "2026-05-25",
    "skill": {"skill_code": "daily-breaking", "name": "每日简报"},
    "release": {
        "release_id": "rel_202605250001",
        "version": "1.0.3",
        "package": {
            "download_url": "https://example.invalid/1.0.3.zip",
            "checksum_sha256": "abc123",
        },
    },
    "audience": {
        "type": "users",
        "users": [{"employee_id": "E123", "open_id": "ou_x", "profile_id": "profile-xxx"}],
    },
}


TEST_KEY = "test-skillhub-key"


@pytest.fixture(autouse=True)
def _fresh_event_store(monkeypatch):
    """Each test gets an isolated in-memory store + a configured webhook key.

    The endpoint is fail-closed, so a key MUST be configured for it to serve at
    all; tests send TEST_KEY by default via ``_post``.
    """
    monkeypatch.setenv("HERMES_SKILLHUB_WEBHOOK_KEY", TEST_KEY)
    skillhub_events.override_event_store(":memory:")
    yield
    skillhub_events.override_event_store(":memory:")


# --- normalization ---

def test_normalize_wangkejie_flat_payload():
    raw = json.dumps(WANGKEJIE_PAYLOAD)
    event = skillhub_events.normalize_event(WANGKEJIE_PAYLOAD, raw_body=raw)
    assert event["event_type"] == "skill.install_approved"
    assert event["skill_code"] == "daily-breaking"
    assert event["release_id"] == "rel_001"
    assert event["version"] == "1.0.3"
    assert event["download_url"].endswith("1.0.3.zip")
    assert event["skill_status"] == "active"
    assert event["auth_type"] == "auth"
    assert event["audience"]["users"] == [{"profile_id": "profile-owner"}]
    assert event["known_type"] is True
    # No event_id provided → synthesized deterministically.
    assert event["event_id"].startswith("daily-breaking:rel_001:")


def test_normalize_prd_full_envelope():
    raw = json.dumps(PRD_ENVELOPE)
    event = skillhub_events.normalize_event(PRD_ENVELOPE, raw_body=raw)
    assert event["event_id"] == "evt_202605250001"
    assert event["skill_code"] == "daily-breaking"
    assert event["release_id"] == "rel_202605250001"
    assert event["download_url"].endswith("1.0.3.zip")
    assert event["checksum_sha256"] == "abc123"
    assert event["auth_type"] == "users"
    assert event["audience"]["users"][0]["employee_id"] == "E123"
    assert event["audience"]["users"][0]["profile_id"] == "profile-xxx"


def test_normalize_missing_skill_code_raises():
    bad = {"event_type": "skill.install_approved"}
    with pytest.raises(skillhub_events.SkillhubEventError) as exc:
        skillhub_events.normalize_event(bad, raw_body=json.dumps(bad))
    assert exc.value.error_code == "INVALID_PAYLOAD"


def test_normalize_missing_event_type_raises():
    bad = {"skill_code": "x"}
    with pytest.raises(skillhub_events.SkillhubEventError):
        skillhub_events.normalize_event(bad, raw_body=json.dumps(bad))


def test_synthesized_event_id_is_stable_for_same_body():
    raw = json.dumps(WANGKEJIE_PAYLOAD)
    a = skillhub_events.normalize_event(WANGKEJIE_PAYLOAD, raw_body=raw)["event_id"]
    b = skillhub_events.normalize_event(WANGKEJIE_PAYLOAD, raw_body=raw)["event_id"]
    assert a == b


# --- store idempotency ---

def test_store_record_then_duplicate():
    store = skillhub_events.SkillhubEventStore(":memory:")
    raw = json.dumps(WANGKEJIE_PAYLOAD)
    event = skillhub_events.normalize_event(WANGKEJIE_PAYLOAD, raw_body=raw)
    status, dup = store.record(event, raw_payload=raw, signature_verified=False)
    assert status == "queued"
    assert dup is False
    assert store.count() == 1
    status2, dup2 = store.record(event, raw_payload=raw, signature_verified=False)
    assert dup2 is True
    assert store.count() == 1  # no second row


def test_store_unknown_event_type_flagged():
    store = skillhub_events.SkillhubEventStore(":memory:")
    payload = {"event_type": "skill.mystery", "skill_code": "x"}
    raw = json.dumps(payload)
    event = skillhub_events.normalize_event(payload, raw_body=raw)
    status, _ = store.record(event, raw_payload=raw, signature_verified=False)
    assert status == "queued_unknown_type"


# --- signature ---

def test_signature_roundtrip():
    secret = "shared-secret"
    raw = json.dumps(WANGKEJIE_PAYLOAD)
    event_id = "evt_1"
    sig = skillhub_events.signature_for(secret, "1700000000", event_id, raw)
    assert sig.startswith("sha256=")
    assert skillhub_events.verify_signature(secret, "1700000000", event_id, raw, sig)
    assert not skillhub_events.verify_signature(secret, "1700000000", event_id, raw, "sha256=bad")


# --- HTTP endpoint ---

def _make_app():
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    return create_run_broker_app()


def _post(path, *, json_body=None, raw=None, headers=None, token=TEST_KEY):
    from aiohttp.test_utils import TestClient, TestServer

    final_headers = dict(headers or {})
    if token is not None and "Authorization" not in final_headers:
        final_headers["Authorization"] = f"Bearer {token}"

    async def runner():
        client = TestClient(TestServer(_make_app()))
        await client.start_server()
        try:
            if raw is not None:
                resp = await client.post(path, data=raw, headers=final_headers)
            else:
                resp = await client.post(path, json=json_body, headers=final_headers)
            body = await resp.json()
            return resp.status, body
        finally:
            await client.close()

    return asyncio.run(runner())


def test_endpoint_accepts_wangkejie_payload():
    status, body = _post("/api/run-broker/skillhub/events", json_body=WANGKEJIE_PAYLOAD)
    assert status == 200
    assert body["ok"] is True
    assert body["accepted"] is True
    assert body["duplicate"] is False
    assert body["status"] == "queued"
    assert body["skill_code"] == "daily-breaking"
    # persisted in the shared singleton store
    assert skillhub_events.get_event_store().count() == 1


def test_endpoint_idempotent_on_repost():
    first_status, first = _post("/api/run-broker/skillhub/events", json_body=WANGKEJIE_PAYLOAD)
    second_status, second = _post("/api/run-broker/skillhub/events", json_body=WANGKEJIE_PAYLOAD)
    assert first_status == 200 and second_status == 200
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["event_id"] == first["event_id"]
    assert skillhub_events.get_event_store().count() == 1


def test_endpoint_rejects_missing_skill_code():
    status, body = _post(
        "/api/run-broker/skillhub/events", json_body={"event_type": "skill.install_approved"}
    )
    assert status == 400
    assert body["ok"] is False
    assert body["error_code"] == "INVALID_PAYLOAD"
    assert skillhub_events.get_event_store().count() == 0


def test_endpoint_rejects_invalid_json():
    status, body = _post(
        "/api/run-broker/skillhub/events",
        raw="not json",
        headers={"Content-Type": "application/json"},
    )
    assert status == 400
    assert body["error_code"] == "INVALID_JSON"


def test_endpoint_signature_required_when_secret_set(monkeypatch):
    monkeypatch.setenv("HERMES_SKILLHUB_WEBHOOK_SECRET", "topsecret")
    # No signature header → rejected.
    status, body = _post("/api/run-broker/skillhub/events", json_body=WANGKEJIE_PAYLOAD)
    assert status == 401
    assert body["error_code"] == "INVALID_SIGNATURE"


def test_endpoint_dedicated_key_accepts_and_rejects(monkeypatch):
    # Both a dedicated skillhub key AND a master broker key are set, so the
    # "no key → open" fallback cannot mask a bad token.
    monkeypatch.setenv("HERMES_SKILLHUB_WEBHOOK_KEY", "wkj-fixed-key")
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "master-key")

    ok_status, ok_body = _post(
        "/api/run-broker/skillhub/events",
        json_body=WANGKEJIE_PAYLOAD,
        token="wkj-fixed-key",
    )
    assert ok_status == 200
    assert ok_body["status"] == "queued"

    bad_status, _ = _post(
        "/api/run-broker/skillhub/events",
        json_body=WANGKEJIE_PAYLOAD,
        token="wrong-key",
    )
    assert bad_status == 401

    missing_status, _ = _post(
        "/api/run-broker/skillhub/events", json_body=WANGKEJIE_PAYLOAD, token=None
    )
    assert missing_status == 401


def test_endpoint_master_key_still_works_for_skillhub(monkeypatch):
    # A caller holding the master broker key is also accepted (single-key setups).
    monkeypatch.setenv("HERMES_SKILLHUB_WEBHOOK_KEY", "wkj-fixed-key")
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "master-key")
    status, _ = _post(
        "/api/run-broker/skillhub/events",
        json_body=WANGKEJIE_PAYLOAD,
        headers={"Authorization": "Bearer master-key"},
    )
    assert status == 200


def test_endpoint_fail_closed_when_no_key_configured(monkeypatch):
    # Public route must NOT be open when neither key is configured.
    monkeypatch.delenv("HERMES_SKILLHUB_WEBHOOK_KEY", raising=False)
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    # Even with a bearer token, no configured key → refuse.
    status, _ = _post(
        "/api/run-broker/skillhub/events", json_body=WANGKEJIE_PAYLOAD, token="anything"
    )
    assert status == 401
    no_token_status, _ = _post(
        "/api/run-broker/skillhub/events", json_body=WANGKEJIE_PAYLOAD, token=None
    )
    assert no_token_status == 401


def test_endpoint_rejects_oversize_body():
    big = {
        "event_type": "skill.install_approved",
        "skill_code": "x",
        "blob": "A" * (300 * 1024),
    }
    status, body = _post("/api/run-broker/skillhub/events", json_body=big)
    assert status == 413
    assert body["error_code"] == "PAYLOAD_TOO_LARGE"
    assert skillhub_events.get_event_store().count() == 0


def test_endpoint_accepts_valid_signature(monkeypatch):
    monkeypatch.setenv("HERMES_SKILLHUB_WEBHOOK_SECRET", "topsecret")
    raw = json.dumps(WANGKEJIE_PAYLOAD)
    event = skillhub_events.normalize_event(WANGKEJIE_PAYLOAD, raw_body=raw)
    ts = "1700000000"
    sig = skillhub_events.signature_for("topsecret", ts, event["event_id"], raw)
    status, body = _post(
        "/api/run-broker/skillhub/events",
        raw=raw,
        headers={
            "Content-Type": "application/json",
            "X-AiDock-Timestamp": ts,
            "X-AiDock-Signature": sig,
        },
    )
    assert status == 200
    assert body["ok"] is True
    assert body["status"] == "queued"
