"""Relay hardening: enroll rate limit, enrollment prune, replies query bounds."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import types
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

WORKTREE = Path(__file__).resolve().parents[1]


class FakeOAuth:
    def authorize_url(self, *, state: str) -> str:
        return f"https://oauth.example/authorize?state={state}"

    async def exchange(self, code: str) -> dict[str, str]:
        return {"actor_id": "ou_alice", "display_name": "Alice"}


class FakeFeishu:
    async def send_message(self, **request) -> dict[str, str]:
        return {"message_id": "om_1", "conversation_id": "oc_self"}


def test_imports_resolve_to_this_worktree():
    import hermes_multitenancy

    resolved = Path(hermes_multitenancy.__file__).resolve()
    print("hermes_multitenancy.__file__ =", resolved)
    assert resolved.is_relative_to(WORKTREE), resolved


def _app(tmp_path):
    from hermes_multitenancy.agent_relay import create_agent_relay_app

    return create_agent_relay_app(
        db_path=tmp_path / "relay.db",
        encryption_key="test-encryption-key",
        oauth=FakeOAuth(),
        feishu=FakeFeishu(),
    )


def _run_client(tmp_path, body, *, with_app=False):
    from aiohttp.test_utils import TestClient, TestServer

    async def runner() -> None:
        app = _app(tmp_path)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await (body(client, app) if with_app else body(client))
        finally:
            await client.close()

    asyncio.run(runner())


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    from hermes_multitenancy import agent_relay

    fake = FakeClock()
    monkeypatch.setattr(agent_relay, "_enroll_clock", fake, raising=False)
    return fake


def _post(client, ip: str):
    return client.post("/v1/enroll/sessions", headers={"X-Forwarded-For": f"{ip}, 10.9.9.9"})


def test_enroll_limits_are_module_constants():
    from hermes_multitenancy import agent_relay

    assert agent_relay.ENROLL_PER_IP_PER_MIN == 10
    assert agent_relay.ENROLL_GLOBAL_PER_MIN == 120


def test_enroll_per_ip_limit_is_ten_per_minute_and_slides(tmp_path, clock, caplog):
    async def body(client) -> None:
        statuses = [(await _post(client, "203.0.113.7")).status for _ in range(11)]
        assert statuses == [201] * 10 + [429], statuses
        blocked = await _post(client, "203.0.113.7")
        assert blocked.status == 429
        assert (await blocked.json())["error"]["code"] == "rate_limited"
        # Another source IP is unaffected.
        assert (await _post(client, "198.51.100.1")).status == 201
        # Window slides: 61 seconds later the first IP may enroll again.
        clock.now += 61
        assert (await _post(client, "203.0.113.7")).status == 201

    with caplog.at_level(logging.WARNING, logger="hermes_multitenancy.agent_relay"):
        _run_client(tmp_path, body)
    audit = [r.getMessage() for r in caplog.records if "event=enroll status=rate_limited" in r.getMessage()]
    # At most one summary per 60s window (the second flushes the trailing rejection
    # once the window has passed), never the raw source address.
    assert len(audit) == 2, audit
    assert audit[0].startswith("relay_audit event=enroll status=rate_limited rejected=1 keys=1 ")
    assert audit[1].startswith("relay_audit event=enroll status=rate_limited rejected=1 keys=1 ")
    assert all("203.0.113.7" not in line for line in audit)


def test_enroll_global_limit_is_120_per_minute(tmp_path, clock):
    async def body(client) -> None:
        for i in range(12):
            for _ in range(10):
                resp = await _post(client, f"192.0.2.{i + 1}")
                assert resp.status == 201
        over = await _post(client, "192.0.2.200")
        assert over.status == 429
        assert (await over.json())["error"]["code"] == "rate_limited"
        clock.now += 61
        assert (await _post(client, "192.0.2.200")).status == 201

    _run_client(tmp_path, body)


def test_enroll_client_ip_prefers_non_loopback_remote():
    from hermes_multitenancy.agent_relay import _enroll_client_ip

    def req(remote, xff=None):
        headers = {} if xff is None else {"X-Forwarded-For": xff}
        return types.SimpleNamespace(remote=remote, headers=headers)

    assert _enroll_client_ip(req("10.0.3.4", "1.1.1.1")) == "10.0.3.4"
    assert _enroll_client_ip(req("127.0.0.1", " 1.1.1.1 , 2.2.2.2")) == "1.1.1.1"
    assert _enroll_client_ip(req("::1", "3.3.3.3")) == "3.3.3.3"
    assert _enroll_client_ip(req("127.0.0.1")) == "unknown"
    assert _enroll_client_ip(req(None)) == "unknown"
    # XFF is validated and normalized; anything that is not a bare IP is 'unknown'.
    assert _enroll_client_ip(req("127.0.0.1", "203.0.113.7 status=completed actor=fake")) == "unknown"
    assert _enroll_client_ip(req("127.0.0.1", "fe80::1%eth0")) == "unknown"
    assert _enroll_client_ip(req("127.0.0.1", "1.2.3.4:5678")) == "unknown"
    assert _enroll_client_ip(req("127.0.0.1", "2001:DB8:0:0::1")) == "2001:db8::1"


def _log_rows(db: Path, where: str = "1=1") -> list[sqlite3.Row]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f"SELECT * FROM relay_logs WHERE {where} ORDER BY id").fetchall()


def test_rejected_enrollments_keep_enrollments_and_persisted_logs_bounded(tmp_path, clock):
    from hermes_multitenancy.agent_relay import RELAY_STORE_KEY, install_relay_log_handler

    db = tmp_path / "relay.db"
    injected = "203.0.113.7 status=completed actor=fake"

    async def body(client, app) -> None:
        handler = install_relay_log_handler(app[RELAY_STORE_KEY])
        try:
            for i in range(130):
                await _post(client, f"192.0.2.{i % 13 + 1}")
            # 500 more over-limit requests from forged / injected forwarded addresses.
            for i in range(250):
                resp = await _post(client, f"10.{i // 250}.{i // 256 % 256}.{i % 256}")
                assert resp.status == 429
            for _ in range(250):
                resp = await client.post("/v1/enroll/sessions", headers={"X-Forwarded-For": injected})
                assert resp.status == 429
            assert _count(db) == 120
            first_window = _log_rows(db)
            assert len(first_window) == 1, [dict(r) for r in first_window]

            clock.now += 61
            for _ in range(200):
                await client.post("/v1/enroll/sessions", headers={"X-Forwarded-For": injected})
            assert _count(db) == 120 + 10  # new window: 'unknown' key gets its 10, then 429
        finally:
            import logging as _logging

            _logging.getLogger().removeHandler(handler)

    _run_client(tmp_path, body, with_app=True)
    rows = _log_rows(db, "event='enroll'")
    assert 1 <= len(rows) <= 2, [dict(r) for r in rows]
    assert len(_log_rows(db)) == len(rows)
    # First line at the first rejection; the second carries the other 509 of window one.
    assert [r["raw"].split("rejected=")[1].split()[0] for r in rows] == ["1", "509"]
    for row in rows:
        assert row["status"] == "rate_limited"
        assert row["actor"] == ""
        assert "status=completed" not in row["raw"]
        assert "actor=fake" not in row["raw"]
        assert "203.0.113.7" not in row["raw"]


def _count(db: Path, where: str = "1=1") -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM relay_enrollments WHERE {where}").fetchone()[0]


def test_prune_removes_stale_enrollments(tmp_path, monkeypatch):
    from hermes_multitenancy import agent_relay_store
    from hermes_multitenancy.agent_relay_store import RelayStore

    base = 1_800_000_000_000
    monkeypatch.setattr(agent_relay_store, "_now_ms", lambda: base)
    db = tmp_path / "relay.db"
    store = RelayStore(db, "test-encryption-key")
    oauth = FakeOAuth()
    try:
        for _ in range(1000):
            store.create_enrollment(oauth)
        states = []
        for _ in range(10):
            started = store.create_enrollment(oauth)
            state = parse_qs(urlparse(started["authorize_url"]).query)["state"][0]
            assert store.begin_enrollment(state)
            assert store.complete_enrollment(state, "ou_alice", "Alice")
            states.append(state)
        assert _count(db) == 1010
        assert _count(db, "status='completed'") == 10

        hour = 3_600_000
        # 25 hours later: every non-completed row expired >24h ago; completed rows are kept
        # (claim_enrollment still hands their token_payload to a polling client).
        store.prune(now_ms=base + 25 * hour)
        assert _count(db, "status!='completed'") == 0
        assert _count(db, "status='completed'") == 10

        # A fresh pending row survives a prune that runs before its 24h grace ends.
        monkeypatch.setattr(agent_relay_store, "_now_ms", lambda: base + 25 * hour)
        store.create_enrollment(oauth)
        store.prune(now_ms=base + 26 * hour)
        assert _count(db, "status='pending'") == 1

        # 31 days after completion the completed rows are gone too.
        store.prune(now_ms=base + 31 * 24 * hour)
        assert _count(db) == 0
    finally:
        store.close()


def _enroll(client):
    async def go():
        started = await client.post("/v1/enroll/sessions")
        body = await started.json()
        state = parse_qs(urlparse(body["authorize_url"]).query)["state"][0]
        cb = await client.get("/v1/enroll/callback", params={"state": state, "code": "x"})
        assert cb.status == 200
        claimed = await (await client.get(f"/v1/enroll/sessions/{body['enroll_id']}")).json()
        return claimed["token"]

    return go()


@pytest.mark.parametrize(
    "query",
    [
        {"since_ts": "9" * 30},
        {"since_ts": "-1"},
        {"since_ts": str(2**63)},
        {"since_ts": "abc"},
        {"limit": "9" * 30, "since_ts": "9" * 30},
        {"limit": "abc"},
        {"limit": "9" * 30},
        {"limit": str(2**63)},
        {"limit": "-1"},
    ],
)
def test_replies_rejects_out_of_range_query_with_400(tmp_path, query):
    async def body(client) -> None:
        token = await _enroll(client)
        headers = {"Authorization": f"Bearer {token}"}
        sent = await client.post("/v1/messages", json={"type": "text", "content": {"text": "hi"}, "idempotency_key": "k1"}, headers=headers)
        assert sent.status in (200, 201), await sent.text()
        message_id = (await sent.json())["message_id"]
        resp = await client.get(f"/v1/messages/{message_id}/replies", params=query, headers=headers)
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["error"]["code"] == "invalid_query"
        ok = await client.get(
            f"/v1/messages/{message_id}/replies",
            params={"since_ts": str(2**63 - 1), "limit": str(2**63 - 1)},
            headers=headers,
        )
        assert ok.status == 200
        assert await ok.json() == {"replies": []}

    _run_client(tmp_path, body)
