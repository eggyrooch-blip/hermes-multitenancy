"""notify-card input hardening (SPEC mt-notify-card-input-hardening, audit MT #17/#18/#31).

Three holes in the ingest-key-authenticated ``POST /api/run-broker/notify-card``
entry and the confirm path it feeds:

1. ``callback.timeout_s`` / ``behaviors.max_submits`` went through bare ``int()``:
   ``json.loads`` accepts ``Infinity`` → ``int(inf)`` raises OverflowError (not a
   ValueError) → 500; a huge finite ``timeout_s`` was stored and later blocked
   ``urlopen`` for decades. Now: finite number, truncated, bounded, else 400.
2. ``deterministic_marker`` was stored raw and later ``str.format``-ed:
   ``{registry_id:2000000000}`` allocated ~2GB per click, ``{x}`` KeyError'd the
   card forever. Now: only literal text + ``{registry_id}`` admitted (≤200 chars),
   and the writers do a literal ``replace`` so historical rows are inert too.
3. ``try_route_push_confirm_synthetic`` (async, on the router event loop) ran the
   blocking confirm + HTTP write inline. Now it runs in a worker thread, and the
   HTTP writer timeout is clamped to [1, 60]s.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_multitenancy
from hermes_multitenancy import push_card_confirm as confirm
from hermes_multitenancy import push_card_routes as routes
from hermes_multitenancy import push_registry as reg
from hermes_multitenancy import push_scenes as scenes

_KEY = "harden-key"
_URL = "/api/run-broker/notify-card"
SCENE = scenes.get_scene("dev-acceptance-claim")


def test_imports_resolve_to_this_worktree():
    # Evidence guard: the suite must exercise the task worktree's code, not the
    # main checkout's (the venv is borrowed from the main checkout).
    print("hermes_multitenancy.__file__ =", hermes_multitenancy.__file__)
    assert Path(hermes_multitenancy.__file__).resolve().parents[1] == Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    reg.override_registry_store(":memory:")
    routes.override_send_dispatch(lambda registry_id: None)
    routes.override_target_resolver(
        lambda *, open_id, union_id, user_id="": {
            "open_id": open_id or "ou_from_union",
            "union_id": union_id,
            "profile_name": "alice-profile",
            "chat_id": None,
        }
    )
    key_file = tmp_path / "keys.json"
    key_file.write_text(json.dumps({"keys": [{
        "token": _KEY, "owner": "sunke", "profile": "alice-profile",
        "allowed_scenes": ["dev-acceptance-claim"],
        "allowed_skills": ["harden-skill"],
        "allowed_callback_domains": ["acme.example"],
    }]}), encoding="utf-8")
    monkeypatch.setenv("HERMES_INGEST_KEYS_FILE", str(key_file))
    monkeypatch.delenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", raising=False)
    monkeypatch.delenv("HERMES_INGEST_KEY", raising=False)
    monkeypatch.delenv("HERMES_PUSH_CARD_WRITER_URL", raising=False)
    yield
    reg.override_registry_store(None)
    routes.override_send_dispatch(None)
    routes.override_target_resolver(None)


def _post(body):
    """POST with stdlib json encoding — ``float('inf')`` goes on the wire as the
    bare ``Infinity`` token, exactly what a hostile caller can send."""
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(
        dispatch_agent=lambda request: "noop",
        mark_seen=lambda _r: True,
        sandbox_available=lambda: True,
    )

    async def runner():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                _URL, data=json.dumps(body),
                headers={"Authorization": f"Bearer {_KEY}", "Content-Type": "application/json"},
            )
            text = await resp.text()
            try:
                data = json.loads(text)
            except ValueError:
                data = {"_raw": text}
            return resp.status, data
        finally:
            await client.close()

    return asyncio.run(runner())


_NAMED = {"open_id": "ou_alice", "scene": "dev-acceptance-claim", "payload": {"amount": None}}


def _row(registry_id):
    return reg.get_registry_store().get(registry_id)


# ---------------- 1. callback.timeout_s -----------------------------------

@pytest.mark.parametrize("bad", [
    float("inf"), float("-inf"), float("nan"), 10**9, 10**30, 0, -5, 61, True, "30", None,
], ids=["inf", "-inf", "nan", "1e9", "1e30", "zero", "neg", "61", "bool", "str", "null"])
def test_callback_timeout_out_of_range_is_400(bad):
    body = {**_NAMED, "callback": {"url": "https://acme.example/api", "timeout_s": bad}}
    status, data = _post(body)
    assert status == 400, (status, data)
    assert "invalid callback" in data.get("error", "")


@pytest.mark.parametrize("good,stored", [(30, 30), (1, 1), (60, 60), (30.7, 30)])
def test_callback_timeout_in_range_is_stored(good, stored):
    body = {**_NAMED, "callback": {"url": "https://acme.example/api", "timeout_s": good}}
    status, data = _post(body)
    assert status == 202, (status, data)
    assert json.loads(_row(data["registry_id"])["callback_json"])["timeout_s"] == stored


def test_callback_timeout_default_unchanged():
    status, data = _post({**_NAMED, "callback": {"url": "https://acme.example/api"}})
    assert status == 202
    assert json.loads(_row(data["registry_id"])["callback_json"])["timeout_s"] == 10
    # key ABSENT → default; explicit null is a malformed value (review R2)
    assert scenes.callback_from_payload({"url": "https://acme.example/a"}).timeout_s == 10
    with pytest.raises(ValueError):
        scenes.callback_from_payload({"url": "https://acme.example/a", "timeout_s": None})
    # the read side (stored rows) stays tolerant
    assert scenes.callback_from_json({"url": "https://acme.example/a", "timeout_s": None}).timeout_s == 10


# ---------------- 2. behaviors.max_submits --------------------------------

@pytest.mark.parametrize("bad", [
    float("inf"), float("nan"), 10**30, 0, -1, 1001, True, "3", None,
], ids=["inf", "nan", "1e30", "zero", "neg", "1001", "bool", "str", "null"])
def test_behaviors_max_submits_out_of_range_is_400(bad):
    status, data = _post({**_NAMED, "behaviors": {"max_submits": bad}})
    assert status == 400, (status, data)
    assert "invalid behaviors" in data.get("error", "")


def test_behaviors_max_submits_in_range_and_default():
    status, data = _post({**_NAMED, "behaviors": {"max_submits": 3}})
    assert status == 202
    assert json.loads(_row(data["registry_id"])["behaviors_json"])["max_submits"] == 3
    status, data = _post({**_NAMED, "open_id": "ou_bob", "behaviors": {"submit_once": False}})
    assert status == 202
    assert json.loads(_row(data["registry_id"])["behaviors_json"])["max_submits"] is None
    assert scenes.behaviors_from_payload({}).max_submits is None
    with pytest.raises(ValueError):
        scenes.behaviors_from_payload({"max_submits": None})
    # stored rows serialize max_submits=None → the read side must accept it
    assert scenes.behaviors_from_json({"max_submits": None}).max_submits is None
    assert scenes.behaviors_from_payload({"max_submits": 1000}).max_submits == 1000


# ---------------- 3. deterministic_marker ---------------------------------

def _inline(marker):
    return {
        "open_id": "ou_alice", "skill": "harden-skill", "mode": "card",
        "fields": [{"key": "reason", "label": "事由", "type": "text"}],
        "deterministic_marker": marker,
    }


@pytest.mark.parametrize("bad", [
    "{registry_id:2000000000}", "{x}", "{unknown_key}", "KEP-{registry_id!r}",
    "{registry_id.__class__}", "{0}", "{}", "A" * 201,
    "{{registry_id}}", "{registry_id:{registry_id}}", "a}b", "{registry_id",
    "\ud800", "KEP-\udfff-{registry_id}", 123,
], ids=["width", "x", "unknown", "conversion", "attr", "positional", "empty", "too-long",
        "double-brace", "nested", "lone-close", "lone-open",
        "surrogate", "surrogate-mixed", "non-str"])
def test_inline_marker_with_format_fields_is_400_and_not_stored(bad):
    status, data = _post(_inline(bad))
    assert status == 400, (status, data)
    assert "invalid scene spec" in data.get("error", "")
    store = reg.get_registry_store()
    assert store._conn.execute("SELECT COUNT(*) FROM push_registry").fetchone()[0] == 0


def test_inline_marker_literal_placeholder_stored_and_confirm_substitutes():
    status, data = _post(_inline("KEP-{registry_id}"))
    assert status == 202, (status, data)
    rid = data["registry_id"]
    store = reg.get_registry_store()
    assert json.loads(_row(rid)["scene_spec_json"])["deterministic_marker"] == "KEP-{registry_id}"
    store.mark_sent(rid, message_id="om_card_1")
    assert store.advance_status(rid, expect=reg.STATUS_PENDING, to=reg.STATUS_CLARIFYING)
    writer = confirm.MockKepPreClaimWriter()
    confirm.override_writer("kep-pre-claim-writer", writer)
    try:
        result = confirm.handle_confirm(
            registry_id=rid, nonce=_row(rid)["nonce"], operator_open_ids={"ou_alice"},
            form_value={"reason": "出差"}, store=store,
        )
    finally:
        confirm.override_writer("kep-pre-claim-writer", None)
    assert result.kind == "committed", result
    assert writer.records[rid]["reason"] == f"出差 KEP-{rid}"


# ------ 4. historical rows: writers treat the marker as literal text ------

@pytest.mark.parametrize("marker", [
    "{registry_id:5000}", "{registry_id:2000000000}", "{unknown_key}",
], ids=["width-5000", "width-2e9", "unknown"])
def test_writers_never_format_a_historical_marker(marker, monkeypatch):
    scene = dataclasses.replace(SCENE, deterministic_marker=marker)
    mock = confirm.MockKepPreClaimWriter()
    res = mock.write(scene=scene, values={"reason": "r"}, registry_id="pcr_h",
                     write_idempotency_key="pcr_h", profile_name="p")
    assert res.ok
    reason = mock.records["pcr_h"]["reason"]
    assert len(reason) < 1024 and reason == f"r {marker}"

    captured = {}

    class _Resp:
        def read(self):
            return json.dumps({"ok": True, "record": {"seq": 1}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(confirm, "_urlopen", fake_urlopen)
    http = confirm.HttpKepPreClaimWriter("http://127.0.0.1:1/api")
    assert http.write(scene=scene, values={"reason": "r"}, registry_id="pcr_h",
                      write_idempotency_key="pcr_h", profile_name="p").ok
    assert len(captured["body"]["reason"]) < 1024
    assert captured["body"]["reason"] == f"r {marker}"


def test_writers_substitute_literal_registry_id_placeholder():
    scene = dataclasses.replace(SCENE, deterministic_marker="[A-{registry_id}]-{registry_id}")
    mock = confirm.MockKepPreClaimWriter()
    mock.write(scene=scene, values={"reason": "x"}, registry_id="pcr_9",
               write_idempotency_key="k", profile_name="p")
    assert mock.records["k"]["reason"] == "x [A-pcr_9]-pcr_9"


# ---------------- 5. HTTP writer timeout clamp ----------------------------

@pytest.mark.parametrize("raw,clamped", [(1e9, 60.0), (61, 60.0), (0, 1.0), (0.2, 1.0), (10, 10.0)])
def test_http_writer_timeout_is_clamped(raw, clamped):
    assert confirm.HttpKepPreClaimWriter("https://acme.example/a", timeout=raw).timeout == clamped


@pytest.mark.parametrize("raw", [10**4000, float("inf"), float("nan"), "30", True, None],
                         ids=["1e4000", "inf", "nan", "str", "bool", "none"])
def test_http_writer_timeout_never_raises_on_hostile_values(raw):
    t = confirm.HttpKepPreClaimWriter("https://acme.example/a", timeout=raw).timeout
    assert isinstance(t, float) and 1.0 <= t <= 60.0


def test_historical_4000_digit_timeout_row_binds_without_overflow():
    # review R1: a stored callback_json may carry any int Python's json parses
    # (10**4000); reading it back + binding the writer must not OverflowError.
    row = {"callback_json": json.dumps({"url": "https://acme.example/claims", "timeout_s": 10**4000})}
    writer, err = confirm._bind_endpoint(confirm.HttpKepPreClaimWriter(""), None, row)
    assert err is None
    assert writer.url == "https://acme.example/claims" and writer.timeout == 60.0


def test_historical_huge_timeout_row_still_binds_with_clamped_timeout():
    # A row stored before this fix with timeout_s=10**9 must still resolve to its
    # per-push callback (not silently fall back to scene/env) — with ≤60s timeout.
    row = {"callback_json": json.dumps({"url": "https://acme.example/claims", "timeout_s": 10**9})}
    cb = confirm.resolve_callback(None, row)
    assert cb is not None and cb.url == "https://acme.example/claims"
    writer, err = confirm._bind_endpoint(confirm.HttpKepPreClaimWriter(""), None, row)
    assert err is None
    assert writer.url == "https://acme.example/claims" and writer.timeout == 60.0


# ---------------- 6. confirm off the event loop ---------------------------

def _synthetic_event():
    action = SimpleNamespace(tag="button", name="push_confirm_submit_op1", value=None,
                             form_value={"reason": "x"})
    card_event = SimpleNamespace(
        action=action,
        operator=SimpleNamespace(open_id="ou_alice", union_id=None, user_id=None),
        context=SimpleNamespace(open_message_id="om_card_1", open_chat_id="oc_dm"),
    )
    return SimpleNamespace(text="/card button", raw_message=SimpleNamespace(event=card_event))


def test_synthetic_confirm_does_not_block_event_loop(monkeypatch):
    def slow_compute(card_event, action, value, form_value=None):
        time.sleep(0.3)  # stands in for a slow KEP urlopen inside handle_confirm
        return confirm.ConfirmResult(kind="noop", toast=confirm._toast("ok"))

    monkeypatch.setattr(confirm, "_compute_confirm_result", slow_compute)
    monkeypatch.setattr("hermes_multitenancy.router._get_feishu_adapter", lambda gw: None)
    assert confirm._synthetic_confirm_parts(_synthetic_event()) is not None

    async def scenario():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        consumed = await confirm.try_route_push_confirm_synthetic(SimpleNamespace(), _synthetic_event())
        seen = ticks  # snapshot BEFORE yielding again
        stop = True
        await task
        return consumed, seen

    consumed, seen = asyncio.run(scenario())
    print("ticks during 0.3s confirm =", seen)
    assert consumed is True
    assert seen > 0
