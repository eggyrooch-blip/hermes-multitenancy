"""POST/GET /api/run-broker/notify-card seam tests (SPEC push-card-fill-loop P2).

Covers fail-closed Bearer auth, the per-key ``allowed_scenes`` whitelist, the
inline self-serve scene contract (``skill``/``mode``/``fields`` gated by
``allowed_skills`` + ``allowed_callback_domains``), 202 async accept,
idempotency-key replay, in-flight business_key 409, unknown-scene and
unregistered-target 4xx, and the status query.
Delivery is stubbed via the send-dispatch seam so the endpoint stays deterministic
(the queue itself is tested in test_push_card_registry.py).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from hermes_multitenancy import push_card_routes as routes
from hermes_multitenancy import push_registry as reg


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # in-memory registry; deterministic send (record, don't fire)
    reg.override_registry_store(":memory:")
    dispatched: list[str] = []
    routes.override_send_dispatch(lambda registry_id: dispatched.append(registry_id))
    # every open_id resolves to a sending profile unless the test says otherwise
    routes.override_target_resolver(
        lambda *, open_id, union_id, user_id="": {
            "open_id": open_id or "ou_from_union",
            "union_id": union_id,
            "profile_name": "alice-profile",
            "chat_id": None,
        }
    )
    # ingest key file: one key granting exactly dev-acceptance-claim
    key_file = tmp_path / "keys.json"
    key_file.write_text(json.dumps({
        "keys": [{
            "token": "push-key-1",
            "owner": "sunke",
            "profile": "alice-profile",
            "allowed_scenes": ["dev-acceptance-claim"],
            # a per-push callback now REQUIRES a domain allowlist on the key
            # (P0 fail-closed); the callback-override tests below target acme.example.
            "allowed_callback_domains": ["acme.example"],
        }]
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE", str(key_file))
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.delenv("HERMES_INGEST_KEY", raising=False)
    yield {"dispatched": dispatched, "key_file": key_file}
    reg.override_registry_store(None)
    routes.override_send_dispatch(None)
    routes.override_target_resolver(None)


def _app():
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    return create_run_broker_app(
        dispatch_agent=lambda request: "noop",
        mark_seen=lambda _r: True,
        sandbox_available=lambda: True,
    )


def _call(method, path, *, body=None, token="push-key-1"):
    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(_app()))
        await client.start_server()
        try:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            if method == "POST":
                resp = await client.post(path, json=body or {}, headers=headers)
            else:
                resp = await client.get(path, headers=headers)
            return resp.status, json.loads(await resp.text())
        finally:
            await client.close()

    return asyncio.run(runner())


_GOOD = {"open_id": "ou_alice", "scene": "dev-acceptance-claim", "payload": {"amount": None}}


def test_unauthorized_without_key():
    status, data = _call("POST", "/api/run-broker/notify-card", body=_GOOD, token=None)
    assert status == 401
    assert data["ok"] is False


def test_unauthorized_wrong_key():
    status, _ = _call("POST", "/api/run-broker/notify-card", body=_GOOD, token="nope")
    assert status == 401


def test_happy_path_returns_202_and_registry_id(_isolate):
    status, data = _call("POST", "/api/run-broker/notify-card", body=_GOOD)
    assert status == 202
    assert data["ok"] is True
    assert data["registry_id"].startswith("pcr_")
    assert data["status"] == reg.STATUS_SENDING
    # the row exists in 'sending' and a send was dispatched
    row = reg.get_registry_store().get(data["registry_id"])
    assert row is not None and row["status"] == reg.STATUS_SENDING
    assert row["skill"] == "push-fill-form"  # server-derived from scene
    assert _isolate["dispatched"] == [data["registry_id"]]


def test_default_card_built_when_omitted(_isolate):
    # No `card` in the body → the row must carry a non-null default card so the
    # send does not serialize card=None → "null" and fail (finding
    # no-default-card-when-omitted).
    assert "card" not in _GOOD
    status, data = _call("POST", "/api/run-broker/notify-card", body=_GOOD)
    assert status == 202
    row = reg.get_registry_store().get(data["registry_id"])
    assert row["card_json"], "a default card must be stored when card is omitted"
    card = json.loads(row["card_json"])
    assert isinstance(card, dict) and card.get("body")  # a real card, not null
    dumped = json.dumps(card, ensure_ascii=False)
    assert "开发验收·测试报销单" in dumped  # scene name
    assert "直接回复此卡即可填写" in dumped  # reply-to-fill prompt


def test_explicit_card_is_preserved(_isolate):
    body = {**_GOOD, "card": {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "CUSTOM"}]}}}
    status, data = _call("POST", "/api/run-broker/notify-card", body=body)
    assert status == 202
    card = json.loads(reg.get_registry_store().get(data["registry_id"])["card_json"])
    assert "CUSTOM" in json.dumps(card, ensure_ascii=False)


def test_inline_skill_not_in_allowed_skills_403():
    # Route A: a ``skill`` in the body now DESCRIBES an inline scene (self-serve)
    # — it is no longer ignored. A leaked key's blast radius is bounded by the
    # ``allowed_skills`` allowlist, so a skill the key was not granted → 403.
    body = {"open_id": "ou_alice", "skill": "evil-skill", "mode": "yolo"}
    status, data = _call("POST", "/api/run-broker/notify-card", body=body)
    assert status == 403
    assert "not authorized" in data["error"]


def test_unknown_scene_4xx():
    body = {**_GOOD, "scene": "no-such-scene"}
    status, data = _call("POST", "/api/run-broker/notify-card", body=body)
    assert status == 400
    assert "unknown scene" in data["error"]


def test_scene_not_in_key_whitelist_403(monkeypatch, tmp_path):
    # a key that does NOT grant dev-acceptance-claim
    key_file = tmp_path / "keys2.json"
    key_file.write_text(json.dumps({
        "keys": [{"token": "push-key-2", "owner": "sunke", "profile": "p",
                  "allowed_scenes": ["some-other-scene"]}]
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE", str(key_file))
    status, data = _call("POST", "/api/run-broker/notify-card", body=_GOOD, token="push-key-2")
    assert status == 403
    assert "not authorized" in data["error"]


def test_target_not_registered_4xx():
    routes.override_target_resolver(lambda *, open_id, union_id, user_id="": None)
    status, data = _call("POST", "/api/run-broker/notify-card", body=_GOOD)
    assert status == 404
    assert "target not registered" in data["error"]


def test_missing_open_id_and_union_id_400():
    body = {"scene": "dev-acceptance-claim", "payload": {}}
    status, data = _call("POST", "/api/run-broker/notify-card", body=body)
    assert status == 400


def test_idempotency_key_returns_original_no_second_card(_isolate):
    body = {**_GOOD, "idempotency_key": "idem-xyz"}
    s1, d1 = _call("POST", "/api/run-broker/notify-card", body=body)
    s2, d2 = _call("POST", "/api/run-broker/notify-card", body=body)
    assert s1 == 202 and s2 == 200
    assert d2["duplicate"] is True
    assert d2["registry_id"] == d1["registry_id"]
    # only the first POST dispatched a send
    assert _isolate["dispatched"] == [d1["registry_id"]]


def test_business_key_inflight_returns_409():
    body = {**_GOOD, "business_key": "bk-shared"}
    s1, d1 = _call("POST", "/api/run-broker/notify-card", body=body)
    # different idempotency but same in-flight business_key
    body2 = {**body, "idempotency_key": "other"}
    s2, d2 = _call("POST", "/api/run-broker/notify-card", body=body2)
    assert s1 == 202 and s2 == 409
    assert d2["registry_id"] == d1["registry_id"]


def test_expires_at_within_bounds_is_stored(_isolate):
    import time as _t
    good = int(_t.time()) + 3600  # 1h ahead, well within 30d
    status, data = _call("POST", "/api/run-broker/notify-card", body={**_GOOD, "expires_at": good})
    assert status == 202
    assert reg.get_registry_store().get(data["registry_id"])["expires_at"] == good


def test_expires_at_too_far_ahead_is_400(_isolate):
    status, data = _call("POST", "/api/run-broker/notify-card", body={**_GOOD, "expires_at": 2 ** 40})
    assert status == 400
    assert "expires_at" in data["error"]


def test_expires_at_in_the_past_is_400(_isolate):
    status, data = _call("POST", "/api/run-broker/notify-card", body={**_GOOD, "expires_at": 1})
    assert status == 400
    assert "expires_at" in data["error"]


def test_expires_at_non_int_is_400(_isolate):
    status, data = _call("POST", "/api/run-broker/notify-card", body={**_GOOD, "expires_at": "soon"})
    assert status == 400
    assert "expires_at" in data["error"]


def test_status_query_returns_public_row():
    _, created = _call("POST", "/api/run-broker/notify-card", body=_GOOD)
    rid = created["registry_id"]
    status, data = _call("GET", f"/api/run-broker/notify-card/{rid}")
    assert status == 200
    assert data["registry_id"] == rid
    assert data["scene"] == "dev-acceptance-claim"
    assert data["status"] == reg.STATUS_SENDING
    assert "nonce" not in data  # internal fields never leak to the caller


def test_status_query_unknown_id_404():
    status, _ = _call("GET", "/api/run-broker/notify-card/pcr_nope")
    assert status == 404


def test_status_query_hides_scene_outside_key_whitelist(monkeypatch, tmp_path):
    _, created = _call("POST", "/api/run-broker/notify-card", body=_GOOD)
    rid = created["registry_id"]
    # a different key with no grant on this scene cannot read the row
    key_file = tmp_path / "keys3.json"
    key_file.write_text(json.dumps({
        "keys": [{"token": "push-key-3", "owner": "x", "profile": "p",
                  "allowed_scenes": ["unrelated"]}]
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE", str(key_file))
    status, _ = _call("GET", f"/api/run-broker/notify-card/{rid}", token="push-key-3")
    assert status == 404


# --- per-push callback / behaviors overrides -----------------------------

def test_push_callback_override_is_stored_on_row(_isolate):
    body = {**_GOOD, "callback": {
        "url": "https://acme.example/api/claims",
        "auth_header": "Authorization", "auth_token_env": "ACME_TOKEN", "timeout_s": 7,
    }}
    status, data = _call("POST", "/api/run-broker/notify-card", body=body)
    assert status == 202
    stored = json.loads(reg.get_registry_store().get(data["registry_id"])["callback_json"])
    assert stored["url"] == "https://acme.example/api/claims"
    assert stored["auth_header"] == "Authorization"
    assert stored["auth_token_env"] == "ACME_TOKEN"  # env NAME, not a plaintext token
    assert stored["timeout_s"] == 7


def test_push_callback_never_stores_a_plaintext_token(_isolate):
    # A stray plaintext token field is NOT whitelisted → never persisted.
    body = {**_GOOD, "callback": {"url": "https://acme.example/api", "token": "S--secret--"}}
    status, data = _call("POST", "/api/run-broker/notify-card", body=body)
    assert status == 202
    raw = reg.get_registry_store().get(data["registry_id"])["callback_json"]
    assert "S--secret--" not in raw


def test_named_scene_per_push_callback_domain_not_allowlisted_403(monkeypatch, tmp_path):
    # P0: a NAMED scene push carrying a per-push callback whose host is not in the
    # key's allowlist must 403 — the named path previously stored the callback with
    # ZERO domain vetting, so a leaked key could exfiltrate to attacker.evil.
    key_file = tmp_path / "named-cb-keys.json"
    key_file.write_text(json.dumps({"keys": [{
        "token": "named-cb-key", "owner": "sunke", "profile": "alice-profile",
        "allowed_scenes": ["dev-acceptance-claim"],
        "allowed_callback_domains": ["acme.example"],  # attacker.evil NOT in it
    }]}), encoding="utf-8")
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE", str(key_file))
    body = {**_GOOD, "callback": {"url": "https://attacker.evil/steal"}}
    status, data = _call("POST", "/api/run-broker/notify-card", body=body, token="named-cb-key")
    assert status == 403
    assert "callback domain not authorized" in data["error"]
    # and with NO domain allowlist at all, a per-push callback is also refused.
    key_file2 = tmp_path / "named-cb-keys2.json"
    key_file2.write_text(json.dumps({"keys": [{
        "token": "named-cb-key2", "owner": "sunke", "profile": "alice-profile",
        "allowed_scenes": ["dev-acceptance-claim"],
    }]}), encoding="utf-8")
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE", str(key_file2))
    status2, data2 = _call("POST", "/api/run-broker/notify-card",
                           body={**_GOOD, "callback": {"url": "https://acme.example/api"}},
                           token="named-cb-key2")
    assert status2 == 403
    assert "callback" in data2["error"]


def test_push_callback_missing_url_is_400(_isolate):
    body = {**_GOOD, "callback": {"auth_header": "Authorization"}}  # no url
    status, data = _call("POST", "/api/run-broker/notify-card", body=body)
    assert status == 400
    assert "callback" in data["error"]


def test_push_behaviors_override_is_stored_on_row(_isolate):
    body = {**_GOOD, "behaviors": {"submit_once": False, "max_submits": 3}}
    status, data = _call("POST", "/api/run-broker/notify-card", body=body)
    assert status == 202
    stored = json.loads(reg.get_registry_store().get(data["registry_id"])["behaviors_json"])
    assert stored["submit_once"] is False
    assert stored["max_submits"] == 3


def test_no_override_leaves_callback_and_behaviors_null(_isolate):
    status, data = _call("POST", "/api/run-broker/notify-card", body=_GOOD)
    assert status == 202
    row = reg.get_registry_store().get(data["registry_id"])
    assert row["callback_json"] is None and row["behaviors_json"] is None
    # a named-scene push does NOT carry an inline spec on the row.
    assert row["scene_spec_json"] is None


# --- inline self-serve scene (route A) -----------------------------------

def _inline_key_file(tmp_path, *, skills, domains=()):
    kf = tmp_path / "inline-keys.json"
    kf.write_text(json.dumps({"keys": [{
        "token": "inline-key", "owner": "sunke", "profile": "alice-profile",
        "allowed_skills": list(skills),
        "allowed_callback_domains": list(domains),
    }]}), encoding="utf-8")
    return kf


def test_inline_card_push_stores_scene_spec_on_row(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE",
                       str(_inline_key_file(tmp_path, skills=["atom-member-commission"])))
    body = {
        "user_id": "sunke", "skill": "atom-member-commission", "mode": "card",
        "fields": [
            {"key": "amount", "label": "金额", "type": "number", "required": True, "risk": "high"},
            {"key": "note", "label": "备注", "type": "text"},
        ],
    }
    status, data = _call("POST", "/api/run-broker/notify-card", body=body, token="inline-key")
    assert status == 202
    row = reg.get_registry_store().get(data["registry_id"])
    assert row["skill"] == "atom-member-commission"  # the inline skill, on the row
    assert row["scene_spec_json"], "an inline push must carry its full spec on the row"
    spec = json.loads(row["scene_spec_json"])
    assert spec["mode"] == "card" and spec["skill"] == "atom-member-commission"
    assert [f["key"] for f in spec["fields"]] == ["amount", "note"]


def test_inline_yolo_push_needs_no_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE",
                       str(_inline_key_file(tmp_path, skills=["quote-bot"])))
    body = {"user_id": "sunke", "skill": "quote-bot", "mode": "yolo"}
    status, data = _call("POST", "/api/run-broker/notify-card", body=body, token="inline-key")
    assert status == 202
    spec = json.loads(reg.get_registry_store().get(data["registry_id"])["scene_spec_json"])
    assert spec["mode"] == "yolo" and spec["fields"] == []


def test_inline_card_without_fields_is_400(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE",
                       str(_inline_key_file(tmp_path, skills=["x"])))
    body = {"user_id": "sunke", "skill": "x", "mode": "card"}
    status, data = _call("POST", "/api/run-broker/notify-card", body=body, token="inline-key")
    assert status == 400
    assert "invalid scene spec" in data["error"]


def test_inline_callback_domain_not_allowlisted_403(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE", str(_inline_key_file(
        tmp_path, skills=["quote-bot"], domains=["acme.example"])))
    body = {"user_id": "sunke", "skill": "quote-bot", "mode": "yolo",
            "callback": {"url": "https://evil.test/save"}}
    status, data = _call("POST", "/api/run-broker/notify-card", body=body, token="inline-key")
    assert status == 403
    assert "callback domain not authorized" in data["error"]


def test_inline_callback_domain_allowlisted_subdomain_202(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE", str(_inline_key_file(
        tmp_path, skills=["quote-bot"], domains=["acme.example"])))
    body = {"user_id": "sunke", "skill": "quote-bot", "mode": "yolo",
            "callback": {"url": "https://api.acme.example/save"}}  # subdomain allowed
    status, data = _call("POST", "/api/run-broker/notify-card", body=body, token="inline-key")
    assert status == 202
    stored = json.loads(reg.get_registry_store().get(data["registry_id"])["callback_json"])
    assert stored["url"] == "https://api.acme.example/save"


def test_inline_status_query_authorized_by_skill(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE",
                       str(_inline_key_file(tmp_path, skills=["quote-bot"])))
    _, created = _call("POST", "/api/run-broker/notify-card",
                       body={"user_id": "sunke", "skill": "quote-bot", "mode": "yolo"},
                       token="inline-key")
    rid = created["registry_id"]
    status, data = _call("GET", f"/api/run-broker/notify-card/{rid}", token="inline-key")
    assert status == 200 and data["registry_id"] == rid
