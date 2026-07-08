"""Console M1a read-only endpoints — SPEC console-m1a-endpoints acceptance."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_multitenancy import console_api


def _seed_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE multitenancy_routing (
                user_id TEXT PRIMARY KEY,
                profile_name TEXT NOT NULL,
                open_id TEXT,
                union_id TEXT,
                kind TEXT NOT NULL DEFAULT 'user',
                active INTEGER NOT NULL DEFAULT 1,
                chat_id TEXT,
                owner_open_id TEXT,
                display_label TEXT,
                last_active_at INTEGER,
                synced_at INTEGER NOT NULL DEFAULT 0,
                provenance TEXT NOT NULL DEFAULT 'sync'
            )
            """
        )
        rows = [
            ("u-1", "feishu_aaa111", "ou_aaa111", "on_aaa111", "user", 1, None, "ou_aaa111", "张三", 200, 100, "sync"),
            ("u-2", "feishu_bbb222", "ou_bbb222", "on_bbb222", "user", 1, None, "ou_bbb222", "李四", 100, 100, "sync"),
            ("g-1", "feishu_group_ccc", None, None, "group", 1, "oc_ccc333", "ou_aaa111", "数据组日常", None, 100, "auto"),
            ("u-3", "feishu_gone", "ou_gone", None, "user", 0, None, None, "离职者", 50, 100, "sync"),
        ]
        conn.executemany(
            "INSERT INTO multitenancy_routing VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        conn.execute(
            """
            CREATE TABLE skillhub_events (
                event_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                received_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.executemany(
            "INSERT INTO skillhub_events VALUES (?,?,?)",
            [
                ("evt-1", "queued", 1),
                ("evt-2", "queued_unknown_type", 2),
                ("evt-3", "failed", 3),
                ("evt-4", "installed", 4),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _seed_audit(audit_path: Path) -> None:
    lines = [
        {"@timestamp": "2026-07-07T01:00:00+00:00", "profile": "feishu_aaa111",
         "platform": "feishu", "finish_reason": "stop", "content": "NORMAL_BODY"},
        {"@timestamp": "2026-07-07T02:00:00+00:00", "profile": "feishu_aaa111",
         "platform": "feishu", "finish_reason": "error", "content": "SECRET_BODY_MUST_NOT_LEAK"},
        {"@timestamp": "2026-07-07T03:00:00+00:00", "profile": "feishu_bbb222",
         "platform": "webui", "finish_reason": "error", "content": "OTHER_PROFILE"},
        "not-json-garbage",
    ]
    with audit_path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line if isinstance(line, str) else json.dumps(line, ensure_ascii=False))
            fh.write("\n")


@pytest.fixture()
def console_env(tmp_path, monkeypatch):
    """Isolated db/shared-home/audit + master key; resets the module cache."""
    db_path = tmp_path / "multitenancy.db"
    _seed_db(db_path)
    shared_home = tmp_path / "hermes-home"
    marker_dir = shared_home / "profiles" / "feishu_aaa111" / "feishu_uat"
    marker_dir.mkdir(parents=True)
    (marker_dir / "ou_aaa111.needs_reauth").write_text(
        json.dumps({"reason": "refresh_token_expired", "ts": 1751000000}), encoding="utf-8"
    )
    audit_path = tmp_path / "conversation-audit.jsonl"
    _seed_audit(audit_path)

    monkeypatch.setenv("HERMES_MULTITENANCY_DB", str(db_path))
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    monkeypatch.setenv("HERMES_CONVERSATION_AUDIT_PATH", str(audit_path))
    monkeypatch.setenv("HERMES_MULTITENANCY_RUN_BROKER_KEY", "console-test-key")
    console_api._reauth_cache["ts"] = 0.0
    console_api._reauth_cache["items"] = []
    return {"db": db_path, "shared_home": shared_home, "audit": audit_path,
            "headers": {"Authorization": "Bearer console-test-key"}}


def _run_with_client(fn):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    async def runner():
        app = create_run_broker_app(
            mark_seen=lambda _request: True, sandbox_available=lambda: True
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await fn(client)
        finally:
            await client.close()

    return asyncio.run(runner())


# ---------------------------------------------------------------- overview --


def test_overview_shape_and_counts(console_env):
    async def scenario(client):
        resp = await client.get("/api/run-broker/console/overview", headers=console_env["headers"])
        assert resp.status == 200
        return await resp.json()

    body = _run_with_client(scenario)
    assert body["broker"]["alive"] is True
    assert body["broker"]["uptime_s"] >= 0
    assert body["gateway_alive"] is True
    assert body["active"] == {"group": 1, "user": 2}  # active=1 rows only
    assert body["skillhub"] == {"queued": 2, "failed": 1, "installed": 1, "total": 4}
    assert body["reauth_pending_count"] == 1
    assert body["cache_age_s"] >= 0.0  # SPEC-pinned key name
    assert body["generated_at"]


def test_overview_unauthorized_without_key(console_env):
    async def scenario(client):
        resp = await client.get("/api/run-broker/console/overview")
        return resp.status

    assert _run_with_client(scenario) == 401


def test_console_endpoints_reject_post(console_env):
    paths = [
        "/api/run-broker/console/overview",
        "/api/run-broker/console/profiles",
        "/api/run-broker/console/profiles/feishu_aaa111",
        "/api/run-broker/console/reauth-pending",
    ]

    async def scenario(client):
        statuses = []
        for path in paths:
            resp = await client.post(path, headers=console_env["headers"])
            statuses.append(resp.status)
        return statuses

    assert _run_with_client(scenario) == [405, 405, 405, 405]


def test_reauth_ttl_env_garbage_does_not_crash(monkeypatch):
    import importlib

    monkeypatch.setenv("HERMES_CONSOLE_REAUTH_TTL_S", "not-a-number")
    try:
        importlib.reload(console_api)
        assert console_api._REAUTH_TTL_S == 600  # fallback + floor
    finally:
        monkeypatch.delenv("HERMES_CONSOLE_REAUTH_TTL_S", raising=False)
        importlib.reload(console_api)


def test_dirty_markers_are_bounded(console_env):
    marker_dir = console_env["shared_home"] / "profiles" / "feishu_bbb222" / "feishu_uat"
    marker_dir.mkdir(parents=True)
    (marker_dir / "ou_huge.needs_reauth").write_text(
        json.dumps({"reason": "x" * 100_000}), encoding="utf-8"
    )
    (marker_dir / "ou_garbage.needs_reauth").write_text("{not json", encoding="utf-8")

    console_api._reauth_cache["ts"] = 0.0
    snap = console_api.reauth_pending_snapshot()
    by_id = {item["open_id"]: item for item in snap["items"]}
    assert by_id["ou_huge"]["reason"] == "unreadable_marker"  # oversized rejected pre-read
    assert by_id["ou_garbage"]["reason"] == "unreadable_marker"
    assert all(len(item["reason"]) <= 200 for item in snap["items"])


# ---------------------------------------------------------------- profiles --


def test_profile_search_matches_and_pages(console_env):
    async def scenario(client):
        by_name = await client.get(
            "/api/run-broker/console/profiles", params={"q": "张三"},
            headers=console_env["headers"],
        )
        by_ou = await client.get(
            "/api/run-broker/console/profiles", params={"q": "ou_bbb"},
            headers=console_env["headers"],
        )
        paged = await client.get(
            "/api/run-broker/console/profiles", params={"limit": 2, "offset": 0},
            headers=console_env["headers"],
        )
        return await by_name.json(), await by_ou.json(), await paged.json()

    by_name, by_ou, paged = _run_with_client(scenario)
    assert by_name["total"] == 1
    assert by_name["items"][0]["profile"] == "feishu_aaa111"  # SPEC-pinned key name
    assert "profile_name" not in by_name["items"][0]
    assert by_name["items"][0]["display_label"] == "张三"
    assert by_ou["total"] == 1 and by_ou["items"][0]["open_id"] == "ou_bbb222"
    assert paged["total"] == 4 and len(paged["items"]) == 2
    # newest activity first
    assert paged["items"][0]["profile"] == "feishu_aaa111"


def test_profile_search_clamps_and_validates_limit(console_env):
    async def scenario(client):
        clamped = await client.get(
            "/api/run-broker/console/profiles", params={"limit": 5000},
            headers=console_env["headers"],
        )
        bad = await client.get(
            "/api/run-broker/console/profiles", params={"limit": "abc"},
            headers=console_env["headers"],
        )
        return await clamped.json(), bad.status

    clamped, bad_status = _run_with_client(scenario)
    assert clamped["limit"] == 100
    assert bad_status == 400


def test_profile_search_like_wildcards_are_literal(console_env):
    async def scenario(client):
        resp = await client.get(
            "/api/run-broker/console/profiles", params={"q": "%"},
            headers=console_env["headers"],
        )
        return await resp.json()

    body = _run_with_client(scenario)
    assert body["total"] == 0  # '%' is matched literally, not as a wildcard


# ------------------------------------------------------------- agents-by-owner --


def test_agents_for_owner_returns_full_owned_active_fleet(tmp_path):
    """The developer self-view query: every active route owned by a given open_id,
    and ONLY those — not another owner's rows, and text search of the open_id
    (search_profiles) must not be relied on."""
    import sqlite3
    from hermes_multitenancy import console_api

    db = tmp_path / "mt.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE multitenancy_routing (user_id TEXT PRIMARY KEY, profile_name TEXT,"
        " open_id TEXT, union_id TEXT, kind TEXT, active INTEGER, chat_id TEXT,"
        " owner_open_id TEXT, display_label TEXT, last_active_at INTEGER, synced_at INTEGER, provenance TEXT)"
    )
    conn.executemany(
        "INSERT INTO multitenancy_routing VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # the developer's own user/profile row — MUST be returned
            ("u-self", "feishu_self", "ou_self", "on_self", "user", 1, None, "ou_self", "我", 200, 100, "sync"),
            # two ACTIVE agents the developer owns — MUST be returned
            ("ag-1", "webui_ag1", None, None, "agent", 1, None, "ou_self", "我的Agent1", 150, 100, "auto"),
            ("ag-2", "webui_ag2", None, None, "agent", 1, None, "ou_self", "我的Agent2", 120, 100, "auto"),
            # a SOFT-DELETED agent the developer owns — must NOT leak (active=0)
            ("ag-del", "webui_agdel", None, None, "agent", 0, None, "ou_self", "已删Agent", 110, 100, "auto"),
            # an owned GROUP row (group rows carry the puller's owner_open_id) — MUST be returned
            ("grp-self", "feishu_group_x", None, None, "group", 1, "oc_x", "ou_self", "我拉的群", 130, 100, "auto"),
            # another owner's agent — must NEVER leak
            ("ag-x", "webui_agx", None, None, "agent", 1, None, "ou_other", "别人的", 100, 100, "auto"),
        ],
    )
    conn.commit()
    conn.close()

    r = console_api.agents_for_owner("ou_self", db_path=db)
    assert r["available"] is True
    names = sorted(a["display_label"] for a in r["items"])
    assert names == ["我", "我拉的群", "我的Agent1", "我的Agent2"]  # active own fleet only
    assert r["total"] == 4  # pagination contract: total present, excludes soft-deleted
    assert all(a.get("owner_open_id") == "ou_self" for a in r["items"])
    assert {a.get("kind") for a in r["items"]} == {"user", "agent", "group"}
    # not the soft-deleted agent, not another owner's
    for excluded in ("已删Agent", "别人的"):
        assert excluded not in names

    # empty owner is a valid empty result, never a wildcard
    empty = console_api.agents_for_owner("", db_path=db)
    assert empty["available"] is True and empty["items"] == []
    # unknown owner → empty
    assert console_api.agents_for_owner("ou_nobody", db_path=db)["items"] == []


def test_agents_endpoint_http(console_env):
    async def scenario(client):
        ok = await client.get(
            "/api/run-broker/console/agents", params={"owner": "ou_aaa111"},
            headers=console_env["headers"],
        )
        noauth = await client.get("/api/run-broker/console/agents", params={"owner": "ou_aaa111"})
        return ok.status, await ok.json(), noauth.status

    status, body, noauth_status = _run_with_client(scenario)
    assert status == 200 and body["available"] is True
    assert body["total"] == 2
    assert {row["kind"] for row in body["items"]} == {"user", "group"}
    assert noauth_status == 401  # master-key gated like the rest


# ------------------------------------------------------------------ detail --


def test_profile_detail_aggregates_and_redacts(console_env, monkeypatch):
    from hermes_multitenancy.connectors import registry

    class _FakeStatus:
        def to_dict(self):
            return {"id": "lark-cli", "status": "authenticated", "installed": True}

    monkeypatch.setattr(
        registry, "collect_connector_statuses", lambda **kwargs: [_FakeStatus()]
    )
    from hermes_multitenancy import cron_api

    monkeypatch.setattr(
        cron_api, "list_jobs", lambda name, include_disabled=True: [{"id": "job-1", "name": "日报"}]
    )

    async def scenario(client):
        ok = await client.get(
            "/api/run-broker/console/profiles/feishu_aaa111", headers=console_env["headers"]
        )
        missing = await client.get(
            "/api/run-broker/console/profiles/no_such_profile", headers=console_env["headers"]
        )
        return await ok.json(), ok.status, missing.status

    body, ok_status, missing_status = _run_with_client(scenario)
    assert ok_status == 200 and missing_status == 404
    assert body["routing"]["open_id"] == "ou_aaa111"
    assert body["connectors"] == [{"id": "lark-cli", "status": "authenticated", "installed": True}]
    assert body["cron_jobs"] == [{"id": "job-1", "name": "日报"}]
    # error rows: category+ts only, own profile only, no message content anywhere
    assert body["recent_errors"] == [
        {"ts": "2026-07-07T02:00:00+00:00", "category": "error", "platform": "feishu"}
    ]
    assert "SECRET_BODY_MUST_NOT_LEAK" not in json.dumps(body)


def test_profile_name_canonical_guard(console_env, monkeypatch):
    """A7: dirty profile names never reach the DB / filesystem reader."""
    # if a dirty name somehow reached sqlite/registry the test would see this flag
    from hermes_multitenancy.connectors import registry
    touched = {"fs": False}
    monkeypatch.setattr(registry, "collect_connector_statuses",
                        lambda **k: touched.__setitem__("fs", True) or [])
    for bad in ["../../etc/passwd", "feishu_g41a5b5g/../other", "a" * 300,
                "feishu\x00null", "has space", "..", "/abs/path", "slash/name"]:
        assert console_api.profile_detail(bad) is None, f"expected None for {bad!r}"
    assert touched["fs"] is False  # no FS-touching reader was ever called

    # canonical real-world samples must all pass the guard (not be false-rejected)
    for good in ["feishu_group_1a10fb26ac51_f1", "codex_harden_175624",
                 "test-group", "123", "multitenancy_router", "feishu_g41a5b5g"]:
        assert console_api._is_canonical_profile_name(good) is True, good


def test_profile_name_traversal_returns_404_over_http(console_env):
    async def scenario(client):
        # %2f-encoded traversal — aiohttp routes it to the detail handler
        resp = await client.get(
            "/api/run-broker/console/profiles/..%2f..%2fetc%2fpasswd",
            headers=console_env["headers"],
        )
        return resp.status

    # 404 (not found / rejected), never 500
    assert _run_with_client(scenario) in (404, 400)


def test_recent_errors_tail_is_bounded(console_env, monkeypatch):
    # A file larger than the tail window must still be read only partially.
    audit = console_env["audit"]
    filler = json.dumps(
        {"@timestamp": "x", "profile": "other", "finish_reason": "stop", "content": "pad" * 40}
    )
    with audit.open("a", encoding="utf-8") as fh:
        for _ in range(20000):
            fh.write(filler + "\n")
        fh.write(
            json.dumps(
                {"@timestamp": "2026-07-07T04:00:00+00:00", "profile": "feishu_aaa111",
                 "platform": "feishu", "finish_reason": "error", "content": "late"}
            )
            + "\n"
        )
    rows = console_api.recent_errors_for_profile("feishu_aaa111")
    # the early 02:00 row fell outside the bounded window; the late row is present
    assert rows == [
        {"ts": "2026-07-07T04:00:00+00:00", "category": "error", "platform": "feishu"}
    ]


# ---------------------------------------------------------- reauth-pending --


def test_reauth_pending_lists_and_caches(console_env):
    async def scenario(client):
        first = await client.get(
            "/api/run-broker/console/reauth-pending", headers=console_env["headers"]
        )
        first_body = await first.json()
        # delete the marker: a second call must still serve the cached item,
        # proving the TTL cache prevents a rescan (mechanical-disk red line)
        marker = (
            console_env["shared_home"] / "profiles" / "feishu_aaa111" / "feishu_uat"
            / "ou_aaa111.needs_reauth"
        )
        marker.unlink()
        second = await client.get(
            "/api/run-broker/console/reauth-pending", headers=console_env["headers"]
        )
        return first_body, await second.json()

    first, second = _run_with_client(scenario)
    assert first["count"] == 1 and first["cache_age_s"] == 0.0
    assert first["items"][0] == {
        "profile": "feishu_aaa111", "open_id": "ou_aaa111",
        "reason": "refresh_token_expired", "ts": 1751000000,
    }
    assert second["count"] == 1  # cache hit — marker deletion not observed
    assert second["cache_age_s"] >= 0.0


def test_reauth_ttl_floor_is_600s():
    assert console_api._REAUTH_TTL_S >= 600


def test_reauth_expiry_burst_scans_exactly_once(console_env, monkeypatch):
    """Concurrent requests at TTL expiry must trigger ONE marker scan (miss-lock)."""
    import threading

    calls = {"n": 0}
    started = threading.Barrier(8)

    def fake_scan(home):
        calls["n"] += 1
        return [{"profile": "p", "open_id": "ou", "reason": "r", "ts": 1}]

    monkeypatch.setattr(console_api, "_scan_reauth_markers", fake_scan)
    console_api._reauth_cache["ts"] = 0.0
    console_api._reauth_cache["items"] = []

    results: list[dict] = []

    def worker():
        started.wait(timeout=10)
        results.append(console_api.reauth_pending_snapshot())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert calls["n"] == 1  # exactly one directory walk for the whole burst
    assert len(results) == 8
    assert all(r["items"][0]["profile"] == "p" for r in results)


# ---------------------------------------------------------------- degraded --


def test_missing_db_degrades_cleanly(console_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_MULTITENANCY_DB", str(tmp_path / "absent.db"))

    async def scenario(client):
        overview = await client.get(
            "/api/run-broker/console/overview", headers=console_env["headers"]
        )
        search = await client.get(
            "/api/run-broker/console/profiles", headers=console_env["headers"]
        )
        detail = await client.get(
            "/api/run-broker/console/profiles/feishu_aaa111", headers=console_env["headers"]
        )
        return overview.status, await search.json(), detail.status

    overview_status, search_body, detail_status = _run_with_client(scenario)
    assert overview_status == 200  # degraded but alive
    assert search_body["available"] is False and search_body["items"] == []
    assert detail_status == 404
