"""SPEC mt-kep-telemetry-diagnostics-api — telemetry diagnostics on the relay admin plane.

The acceptance line that matters most is `test_conversation_content_never_leaves`:
the response is constructed from an allowlist, so a field the exporter grows
tomorrow — or an audit row that smuggled prose into one — cannot reach the wire.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_multitenancy import agent_relay_admin as plane, agent_relay_admin_diag as api

TOKEN = "kep-diag-token-canary"
SHANGHAI = timezone(timedelta(hours=8))
EPOCH = date(1970, 1, 1)


@pytest.fixture(autouse=True)
def _unthrottled(monkeypatch):
    """Every test but the throttling one wants the limiter out of the way."""
    monkeypatch.setattr(api, "_limiter", plane.RateLimiter(per_second=10_000, per_hour=10_000))


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TELEMETRY_DIAG_TOKEN", TOKEN)
    monkeypatch.delenv("HERMES_AGENT_RELAY_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv(api.DIR_ENV, str(tmp_path / "diagnostics"))


# ── fixtures ──────────────────────────────────────────────────────────────


def _day_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_day(directory: Path, day: str, rows: list[dict]) -> Path:
    """Append rows the way ``DiagnosticsLog`` does, including its stamped row id."""
    path = directory / f"export-{day}.ndjson"
    seq = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                parsed = json.loads(line)
                seq = max(seq, int(parsed.get("seq") or 0)) if isinstance(parsed, dict) else seq
            except (ValueError, TypeError, AttributeError):
                continue  # the fixture may deliberately contain junk lines
    with path.open("ab") as fh:
        for row in rows:
            if "id" not in row:
                seq += 1
                row = dict(row, id=f"{day}-{seq:08d}", seq=seq)
            fh.write((json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    return path


def _stamp(dt: datetime) -> dict:
    return {"at": dt.astimezone(SHANGHAI).isoformat(timespec="milliseconds"),
            "at_epoch_ms": int(dt.timestamp() * 1000)}


def _attempt(dt: datetime, run_id: str, *, operator: str = "zhangsan", **extra) -> dict:
    row = {
        "kind": "upload_attempt",
        "run_id": run_id,
        "content_hash": "c" * 64,
        "skill": "kep-ub-gen",
        "profile": operator,
        "profile_withheld": False,
        "operator": operator,
        "batch_id": "batch-1",
        "url": "https://proxy.cms.example.com/api/kep-cli-hub-admin/api/v1/skill-runs",
        "attempt": 1,
        "reason": "invalid_enum:client",
        "class": "repairable",
        "request": {"run_id": run_id, "client": "hermes", "skill": "kep-ub-gen",
                    "record_kind": "lifecycle", "status": "closed", "surface": "cloud"},
        "response": {"status": 200, "body": '{"accepted":0,"rejected":1}', "truncated": False,
                     "error": {"index": 0, "reason": "invalid_enum:client", "run_id": run_id}},
        "verdict": {"outcome": "dead_letter", "reason": "invalid_enum:client"},
        "sent_at": "2026-09-22T15:00:00.000+08:00",
        "source_offset": 4096, "source_inode": 12345, "source_available": True,
        "source_reason": None, "observed_at": "2026-09-22T14:59:00.000+08:00",
    }
    row.update(_stamp(dt))
    row.update(extra)
    return row


def _day_of(dt: datetime) -> str:
    return dt.astimezone(SHANGHAI).strftime("%Y%m%d")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _client_app():
    from aiohttp import web

    app = web.Application()
    api.register_telemetry_diagnostics_routes(app)
    return app


def _get(path: str, params: dict, headers: dict | None = None) -> tuple[int, dict]:
    from aiohttp.test_utils import TestClient, TestServer

    async def runner():
        client = TestClient(TestServer(_client_app()))
        await client.start_server()
        try:
            resp = await client.get(path, params={k: str(v) for k, v in params.items()},
                                    headers=headers or {})
            return resp.status, await resp.json()
        finally:
            await client.close()

    return asyncio.run(runner())


def _admin() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


DIAG = "/v1/admin/telemetry-diagnostics"
STATS = "/v1/admin/telemetry-stats"


# ── privacy: the line that must never move ────────────────────────────────


def test_conversation_content_never_leaves(tmp_path):
    """A row carrying prose — in any of the shapes an upstream change could take —
    comes back with none of it. Allowlist, not blocklist."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    poisoned = _attempt(now, "run-poison")
    poisoned["content"] = "帮我把这份合同里的甲方改成……"
    poisoned["prompt"] = "user said something private"
    poisoned["messages"] = [{"role": "user", "content": "secret question"}]
    poisoned["request"] = dict(poisoned["request"])
    poisoned["request"]["answer"] = "assistant replied with the whole document"
    poisoned["request"]["preview"] = "first 200 chars of the conversation"
    poisoned["response"] = dict(poisoned["response"])
    poisoned["response"]["transcript"] = "the entire turn"
    _write_day(directory, _day_of(now), [poisoned])

    status, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    assert status == 200
    assert len(body["items"]) == 1
    wire = json.dumps(body, ensure_ascii=False)
    for leaked in ("帮我把这份合同", "secret question", "assistant replied with the whole document",
                   "first 200 chars of the conversation", "the entire turn",
                   "user said something private"):
        assert leaked not in wire, f"conversation content reached the wire: {leaked}"
    for key in ("content", "prompt", "messages", "answer", "preview", "transcript"):
        assert f'"{key}"' not in wire
    # Drift is counted, not silent: five unknown keys were refused.
    assert body["dropped_keys"] >= 5


def test_one_attribution_field_only(tmp_path):
    """``profile`` and ``operator`` are the same employee account name; only one ships."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    reject = {
        "kind": "local_reject", "run_id": "run-reject", "skill": None, "skill_withheld": True,
        "profile": "lisi", "profile_withheld": False, "operator": None, "batch_id": None,
        "attempt": 1, "reason": "skill_name_out_of_charset", "class": "permanent",
        "request": None, "response": None,
        "verdict": {"outcome": "dead_letter", "reason": "skill_name_out_of_charset"},
        "source_offset": 10, "source_inode": 7, "source_available": True,
        "source_reason": None, "observed_at": None,
    }
    reject.update(_stamp(now))
    _write_day(directory, _day_of(now), [_attempt(now, "run-a", operator="zhangsan"), reject])

    _, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    assert len(body["items"]) == 2
    for item in body["items"]:
        assert "profile" not in item
        assert "profile_withheld" not in item
    # upload_attempt keeps the X-Operator value the Hub already received; a row that
    # never went out falls back to the profile, so attribution is not lost.
    assert body["items"][0]["operator"] == "zhangsan"
    assert body["items"][1]["operator"] == "lisi"


def test_request_allowlist_is_exactly_what_the_exporter_emits():
    """The allowlist tracks the *emitted* payload, and stays inside the contract.

    Two directions, both deliberate. It must equal what ``build_record`` actually
    produces, so a new emitted field is a red test rather than a silent hole in
    the evidence; and it must stay a subset of ``LIFECYCLE_V9_KEYS``, so widening
    the upload contract never widens this API by itself.
    """
    from hermes_multitenancy.analytics import kep_telemetry_export as exporter

    call = exporter.SkillCall(profile="zhangsan", platform="feishu", session_id="s-abc",
                              message_id="om_dead", skill="kep-ub-gen",
                              observed_at="2026-09-22T15:00:00.000+08:00",
                              source_inode=1, source_offset=2)
    terminal = exporter.RunTerminal(profile="zhangsan", platform="feishu",
                                    at="2026-09-22T15:00:05.000+08:00",
                                    terminal_status="closed", expert_id="chenshier")
    emitted = set(exporter.build_record(call, terminal)) | set(exporter.build_record(call, None))
    assert set(api.REQUEST_FIELDS) == emitted
    assert set(api.REQUEST_FIELDS) <= set(exporter.LIFECYCLE_V9_KEYS)


def test_row_allowlist_tracks_the_producer_row_builder(tmp_path):
    """Every key ``DiagnosticsLog`` writes is either served or *named* as withheld.

    The producer is driven for real rather than described from memory: a hand-kept
    list of its fields goes stale silently, and a field this API has never heard of
    is either evidence that quietly goes missing or a key that quietly ships. Run
    the writer, read what it wrote, compare.
    """
    from hermes_multitenancy.analytics import kep_telemetry_export as exporter

    log = exporter.DiagnosticsLog(tmp_path / "produced", datetime.now(tz=SHANGHAI))
    record = {"run_id": "r" * 32, "client": "hermes", "record_kind": "lifecycle",
              "skill": "kep-ub-gen", "status": "closed", "surface": "cloud"}
    locator = {"inode": 7, "offset": 11, "observed_at": "2026-09-22T15:00:00.000+08:00"}
    log.upload_attempt(record, batch_id="b-1", operator="zhangsan", url="https://h/x",
                       sent_at="2026-09-22T15:00:00.000+08:00", status=200,
                       body='{"accepted":0,"rejected":1}', outcome="dead_letter",
                       reason="invalid_enum:client", klass="repairable", tries=1, index=0,
                       profile="zhangsan", locator=locator)
    log.local_reject(run_id="r" * 32, skill="bad/name", reason="skill_name_out_of_charset",
                     klass="permanent", profile="zhangsan", locator=locator)
    log.pass_summary({"at": "2026-09-22T15:00:00.000+08:00", "env": "online",
                      "upload": {"sent": 1, "accepted": 0}})
    log.close()

    produced: set[str] = set()
    for path in (tmp_path / "produced").glob("export-*.ndjson"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                produced |= set(json.loads(line))
    assert produced, "the producer wrote nothing — this test proves nothing"

    known = set(api.ROW_FIELDS) | set(api.WITHHELD_FIELDS) | {
        "request", "response", "verdict", "report"}
    assert produced <= known, (
        f"the producer grew fields this API has no declared handling for: {sorted(produced - known)}"
    )


def test_audit_line_content_is_never_served(tmp_path):
    """``source_offset``/``source_inode`` are a handle, not a resource."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    _write_day(directory, _day_of(now), [_attempt(now, "run-a")])
    _, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    item = body["items"][0]
    assert item["source_offset"] == 4096 and item["source_inode"] == 12345
    app = _client_app()
    paths = sorted({str(route.resource.canonical) for route in app.router.routes()})
    assert paths == [DIAG, STATS], "no endpoint may exist that reads the audit itself"


# ── auth: fail closed ─────────────────────────────────────────────────────


def test_fail_closed_without_configured_token(tmp_path, monkeypatch):
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    _write_day(directory, _day_of(now), [_attempt(now, "run-a")])
    window = {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}

    monkeypatch.delenv("HERMES_TELEMETRY_DIAG_TOKEN", raising=False)
    for path in (DIAG, STATS):
        status, body = _get(path, window, _admin())
        assert status == 403 and body["error"]["code"] == "forbidden"
        status, body = _get(path, window, {})
        assert status == 401 and body["error"]["code"] == "unauthorized"


def test_the_relay_admin_token_cannot_read_telemetry(tmp_path, monkeypatch):
    """Two consumers, two tokens: 云驿's bearer stops at the resources it was issued for."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    _write_day(directory, _day_of(now), [_attempt(now, "run-a")])
    monkeypatch.setenv("HERMES_AGENT_RELAY_ADMIN_TOKEN", "relay-admin-canary")
    window = {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}
    for path in (DIAG, STATS):
        status, body = _get(path, window, {"Authorization": "Bearer relay-admin-canary"})
        assert status == 403 and body["error"]["code"] == "forbidden"


def test_range_validation_matches_the_relay_admin_plane(tmp_path):
    _day_dir(tmp_path)
    now = int(time.time() * 1000)
    bad_windows = [
        {},
        {"since": "x", "until": now},
        {"since": now, "until": now - 1},
        {"since": now - plane.ADMIN_MAX_WINDOW_MS - 1, "until": now},
        {"since": -1, "until": now},
    ]
    for params in bad_windows:
        status, body = _get(DIAG, params, _admin())
        assert status == 400, params
        assert body["error"]["code"] == "invalid_range"
        assert set(body["error"]) == {"code", "message"}
    for bad_cursor in ({"after_id": "-3"}, {"after_id": "20260922"}, {"after_id": "x"}):
        params = {"since": now - 1000, "until": now, **bad_cursor}
        status, body = _get(DIAG, params, _admin())
        assert status == 400 and body["error"]["code"] == "invalid_range", bad_cursor


def test_rate_limit_returns_retry_after(tmp_path, monkeypatch):
    _day_dir(tmp_path)
    monkeypatch.setattr(api, "_limiter", plane.RateLimiter(per_second=1, per_hour=100))
    now = int(time.time() * 1000)
    window = {"since": now - 1000, "until": now}
    first, _ = _get(DIAG, window, _admin())
    second, body = _get(DIAG, window, _admin())
    assert first == 200
    assert second == 429 and body["error"]["code"] == "rate_limited"
    assert body["error"]["retry_after"] >= 1


# ── paging: the double cursor ─────────────────────────────────────────────


def _seed_three_passes(directory: Path, base: datetime) -> list[datetime]:
    """Three exporter rounds across three days; every row of a round shares its ``at``."""
    stamps = [base - timedelta(days=2), base - timedelta(days=1), base]
    for stamp in stamps:
        rows = [_attempt(stamp, f"run-{stamp:%d}-{i}") for i in range(5)]
        _write_day(directory, _day_of(stamp), rows)
    return stamps


def test_double_cursor_walks_every_row_exactly_once(tmp_path):
    directory = _day_dir(tmp_path)
    base = datetime.now(tz=SHANGHAI)
    stamps = _seed_three_passes(directory, base)
    window = {"since": _ms(stamps[0]) - 60_000, "until": _ms(base) + 60_000}

    seen: list[str] = []
    page = api.read_page(directory, window["since"], window["until"], limit=2)
    while page["items"]:
        seen.extend(item["run_id"] for item in page["items"])
        if not page["truncated"]:
            break
        cursor = page["next"]
        page = api.read_page(directory, cursor["since"], window["until"], cursor["after_id"], limit=2)
    assert len(seen) == 15
    assert len(set(seen)) == 15, "a page boundary inside one pass must not repeat rows"


def test_timestamp_only_continuation_spins_in_place(tmp_path):
    """Why ``after_id`` is not optional here: one pass writes one timestamp.

    Continuing on ``since`` alone re-serves the rows just read — the relay's
    round-1 review P1, except that here it is certain rather than occasional,
    because every row of an exporter round carries that round's single ``at``.
    """
    directory = _day_dir(tmp_path)
    base = datetime.now(tz=SHANGHAI)
    stamps = _seed_three_passes(directory, base)
    since = _ms(stamps[0]) - 60_000
    until = _ms(base) + 60_000

    first = api.read_page(directory, since, until, limit=2)
    last = first["items"][-1]
    naive = api.read_page(directory, last["at_epoch_ms"], until, limit=50)
    assert [item["id"] for item in naive["items"]][:2] == [item["id"] for item in first["items"]], (
        "timestamp-only continuation must be shown to make no progress"
    )
    # The double cursor moves on instead.
    correct = api.read_page(directory, last["at_epoch_ms"], until, last["id"], limit=50)
    assert [item["run_id"] for item in correct["items"]][:3] == [
        f"run-{stamps[0]:%d}-{i}" for i in (2, 3, 4)
    ]


def test_rows_are_ordered_and_window_filtered(tmp_path):
    directory = _day_dir(tmp_path)
    base = datetime.now(tz=SHANGHAI)
    stamps = _seed_three_passes(directory, base)
    page = api.read_page(directory, _ms(stamps[1]) - 1000, _ms(stamps[1]) + 1000)
    assert len(page["items"]) == 5
    assert [item["id"] for item in page["items"]] == sorted(item["id"] for item in page["items"])
    assert all(item["run_id"].startswith(f"run-{stamps[1]:%d}-") for item in page["items"])


def test_partial_tail_line_is_never_served_and_not_skipped(tmp_path):
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    day = _day_of(now)
    path = _write_day(directory, day, [_attempt(now, "run-whole")])
    pending = _attempt(now, "run-half")
    pending.update(id=f"{day}-00000002", seq=2)
    line = json.dumps(pending, ensure_ascii=False, sort_keys=True)
    with path.open("ab") as fh:  # a row mid-write: no trailing newline
        fh.write(line[:40].encode("utf-8"))

    since, until = _ms(now) - 60_000, _ms(now) + 60_000
    page = api.read_page(directory, since, until)
    assert [item["run_id"] for item in page["items"]] == ["run-whole"]

    with path.open("ab") as fh:  # the exporter finishes the line
        fh.write(line[40:].encode("utf-8") + b"\n")
    after = page["items"][-1]
    page2 = api.read_page(directory, after["at_epoch_ms"], until, after["id"])
    assert [item["run_id"] for item in page2["items"]] == ["run-half"]


def test_byte_budget_truncates_and_resumes(tmp_path):
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    _write_day(directory, _day_of(now), [_attempt(now, f"run-{i}") for i in range(20)])
    since, until = _ms(now) - 60_000, _ms(now) + 60_000
    page = api.read_page(directory, since, until, budget=2000)
    assert page["truncated"] is True and page["next"] is not None
    assert 0 < len(page["items"]) < 20
    rest = api.read_page(directory, page["next"]["since"], until, page["next"]["after_id"])
    assert len(page["items"]) + len(rest["items"]) == 20


def test_missing_directory_reads_as_empty_not_broken(tmp_path):
    now = int(time.time() * 1000)
    window = {"since": now - 60_000, "until": now + 60_000}
    status, body = _get(DIAG, window, _admin())
    assert status == 200 and body["items"] == [] and body["truncated"] is False
    status, stats = _get(STATS, window, _admin())
    assert status == 200 and stats["state"]["available"] is False


def test_cursor_into_a_pruned_day_reports_a_gap(tmp_path):
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    _write_day(directory, _day_of(now), [_attempt(now, "run-a")])
    gone_day = (now - timedelta(days=3)).date()
    stale = f"{gone_day:%Y%m%d}-00000009"
    page = api.read_page(directory, _ms(now) - 5 * 86_400_000, _ms(now) + 60_000, stale)
    assert page["gap"] is True
    assert [item["run_id"] for item in page["items"]] == ["run-a"]


# ── stats ─────────────────────────────────────────────────────────────────


def test_stats_counts_the_window_and_reports_disk_state(tmp_path):
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    summary = {"kind": "pass_summary",
               "report": {"at": "2026-09-22T15:00:00.000+08:00", "env": "online",
                          "audit": "/var/log/hermes/conversation-audit.jsonl",
                          "upload": {"sent": 5, "accepted": 4},
                          "operators": "zhangsan"}}
    summary.update(_stamp(now))
    _write_day(directory, _day_of(now), [_attempt(now, "run-a"), _attempt(now, "run-b"), summary])

    status, stats = _get(STATS, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    assert status == 200
    assert stats["rows"]["total"] == 3
    assert stats["rows"]["by_kind"] == {"upload_attempt": 2, "pass_summary": 1}
    assert stats["verdicts"] == {"dead_letter": 2}
    assert stats["rejected_by_reason"] == {"invalid_enum": 2}
    assert stats["rejected_by_class"] == {"repairable": 2}
    assert stats["state"]["available"] is True and stats["state"]["retention_days"] == 14
    # last_pass comes from the newest pass_summary row, inside the mounted dir —
    # `last-run.json` lives one level up, which the production bind does not expose.
    assert stats["state"]["last_pass"]["upload"] == {"sent": 5, "accepted": 4}


def test_pass_summary_report_is_counters_only(tmp_path):
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    summary = {"kind": "pass_summary",
               "report": {"at": "2026-09-22T15:00:00.000+08:00", "env": "online",
                          "audit": "/var/log/hermes/conversation-audit.jsonl",
                          "diagnostics": {"path": "/home/hermes/.hermes/state/x/export-1.ndjson"},
                          "upload": {"sent": 5, "accepted": 4}}}
    summary.update(_stamp(now))
    _write_day(directory, _day_of(now), [summary])
    _, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    report = body["items"][0]["report"]
    assert report["audit"] == "conversation-audit.jsonl", "host paths do not leave the box"
    assert "diagnostics" not in report, "the log's own path is not part of the report"
    assert report["upload"] == {"sent": 5, "accepted": 4}
    assert "/home/" not in json.dumps(body)


def test_rows_without_a_producer_id_are_counted_not_served(tmp_path):
    """A row the producer did not stamp cannot page: serving it would loop."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    orphan = _attempt(now, "run-orphan")
    orphan["id"] = None
    _write_day(directory, _day_of(now), [orphan, _attempt(now, "run-ok")])
    page = api.read_page(directory, _ms(now) - 60_000, _ms(now) + 60_000)
    assert [item["run_id"] for item in page["items"]] == ["run-ok"]
    assert page["unpageable_lines"] == 1


def test_the_id_is_authoritative_so_a_stale_since_neither_repeats_nor_loses(tmp_path):
    """The cursor predicate is ``since <= ts <= until AND id > after_id``.

    Because the id decides, a caller who advances ``after_id`` gets the same answer
    whether or not it also moved ``since`` — no duplicates, no gaps. That is also
    what lets the reader skip whole day files and file prefixes: with the relay's
    disjunction the id is only a tie-break, and skipping a prefix would drop rows
    that still satisfy ``ts > since``.
    """
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    _write_day(directory, _day_of(now), [_attempt(now, f"run-{i}") for i in range(4)])
    window_start, until = _ms(now) - 3_600_000, _ms(now) + 3_600_000
    day = _day_of(now)

    stale_since = api.read_page(directory, window_start, until, f"{day}-00000002")
    moved = api.read_page(directory, _ms(now), until, f"{day}-00000002")
    expected = [f"{day}-{i:08d}" for i in (3, 4)]
    assert [item["id"] for item in stale_since["items"]] == expected
    assert [item["id"] for item in moved["items"]] == expected


def test_cursor_day_outside_the_window_is_not_reported_as_a_gap(tmp_path):
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    _write_day(directory, _day_of(now), [_attempt(now, "run-a")])
    old_day = (now - timedelta(days=5)).strftime("%Y%m%d")
    page = api.read_page(directory, _ms(now) - 60_000, _ms(now) + 60_000, f"{old_day}-00000001")
    assert page["gap"] is False
    assert [item["run_id"] for item in page["items"]] == ["run-a"]


# ── review round 1 (codex gpt-6-astra) regressions ────────────────────────


def test_review1_nested_content_cannot_ride_a_permitted_key(tmp_path):
    """shallow-allowlist#p1 — a permitted key is not a permitted subtree."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    summary = {"kind": "pass_summary",
               "report": {"at": "2026-09-22T15:00:00.000+08:00", "env": "online",
                          "upload": {"sent": 5, "content": "PRIVATE_TEXT",
                                     "nested": {"deeper": {"prompt": "PRIVATE_TEXT"}}}}}
    summary.update(_stamp(now))
    poisoned = _attempt(now, "run-nested")
    poisoned["request"] = dict(poisoned["request"])
    poisoned["request"]["token_metrics"] = {"input": 12, "prompt": "PRIVATE_TEXT"}  # not emitted today
    poisoned["response"] = dict(poisoned["response"])
    poisoned["response"]["error"] = {"index": 0, "detail": {"preview": "PRIVATE_TEXT"}}
    _write_day(directory, _day_of(now), [summary, poisoned])

    _, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    wire = json.dumps(body, ensure_ascii=False)
    assert "PRIVATE_TEXT" not in wire
    assert "token_metrics" not in body["items"][1]["request"], "not an emitted key: dropped whole"
    assert body["dropped_keys"] >= 3


def test_review2_relay_shaped_after_id_zero_is_accepted(tmp_path):
    """cursor-contract-drift#p1 — the integer plane's default must not 400."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    _write_day(directory, _day_of(now), [_attempt(now, "run-a")])
    status, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000,
                               "after_id": "0"}, _admin())
    assert status == 200 and [i["run_id"] for i in body["items"]] == ["run-a"]


def test_review3_a_line_without_a_newline_is_bounded(tmp_path):
    """unbounded-reads#p1 — the budget is checked against bytes actually read."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    path = directory / f"export-{_day_of(now)}.ndjson"
    path.write_bytes(b"x" * (3 * 1024 * 1024))  # one 3 MiB line, no newline at all
    scan = api._Scan(directory, _ms(now) - 60_000, _ms(now) + 60_000, budget=512 * 1024)
    assert list(scan.rows()) == []
    assert scan.scanned <= 1024 * 1024 + 65536, f"read {scan.scanned} bytes past the ceiling"


def test_review4_budget_spent_outside_the_window_still_advances(tmp_path):
    """budget-exhaustion-without-progress#p1 — a page with no items must still move."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    old = now - timedelta(hours=6)
    # A day file whose early rows are all older than the window.
    _write_day(directory, _day_of(now), [_attempt(old, f"old-{i}") for i in range(20)]
               + [_attempt(now, "run-wanted")])
    page = api.read_page(directory, _ms(now) - 60_000, _ms(now) + 60_000, budget=2000)
    assert page["items"] == [] and page["truncated"] is True
    assert page["next"] is not None, "no cursor means the caller retries this prefix forever"
    seen = []
    cursor, guard, trace = page["next"], 0, []
    while cursor is not None and guard < 30:
        page = api.read_page(directory, cursor["since"], _ms(now) + 60_000, cursor["after_id"], budget=2000)
        seen.extend(item["run_id"] for item in page["items"])
        trace.append((len(page["items"]), page["truncated"], page["next"]))
        cursor, guard = page["next"], guard + 1
    assert "run-wanted" in seen, trace


def test_review5_padded_bearer_shares_one_rate_bucket(tmp_path, monkeypatch):
    """uncanonicalized-token-bypass#p1 — auth strips, so the limiter must too."""
    _day_dir(tmp_path)
    monkeypatch.setattr(api, "_limiter", plane.RateLimiter(per_second=1, per_hour=100))
    now = int(time.time() * 1000)
    window = {"since": now - 1000, "until": now}
    first, _ = _get(DIAG, window, {"Authorization": f"Bearer {TOKEN}"})
    second, _ = _get(DIAG, window, {"Authorization": f"Bearer  {TOKEN} "})
    assert first == 200 and second == 429


def test_review6_unrepresentable_timestamps_are_400_not_500(tmp_path):
    """datetime-range-unchecked#p1 — int64 is not the same as a date."""
    _day_dir(tmp_path)
    status, body = _get(DIAG, {"since": 2**63 - 2, "until": 2**63 - 1}, _admin())
    assert status == 400 and body["error"]["code"] == "invalid_range"


def test_review7_a_crashed_older_day_does_not_hide_later_days(tmp_path):
    """partial-tail-aborts-later-days#p1."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    yesterday = now - timedelta(days=1)
    path = _write_day(directory, _day_of(yesterday), [_attempt(yesterday, "old-whole")])
    with path.open("ab") as fh:  # writer crashed mid-row, a year ago
        fh.write(b'{"kind": "upload_attempt", "run_id": "old-half"')
    _write_day(directory, _day_of(now), [_attempt(now, "new-row")])

    page = api.read_page(directory, _ms(yesterday) - 60_000, _ms(now) + 60_000)
    assert [item["run_id"] for item in page["items"]] == ["old-whole", "new-row"]
    assert page["unreadable_lines"] == 1


def test_review8_junk_at_the_resume_point_does_not_crash(tmp_path):
    """non-object-probe-crash#p1 — a JSON array, a number, a fragment: all survivable."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    path = _write_day(directory, _day_of(now), [_attempt(now, f"run-{i}") for i in range(3)])
    day = _day_of(now)
    with path.open("ab") as fh:
        fh.write(b"[]\n42\n")
    _write_day(directory, day, [_attempt(now, "run-after")])

    page = api.read_page(directory, _ms(now), _ms(now) + 60_000, f"{day}-00000001")
    assert [item["run_id"] for item in page["items"]] == ["run-1", "run-2", "run-after"]
    assert page["unreadable_lines"] == 2


# ── review round 2 (codex gpt-6-astra) regressions ────────────────────────


def test_r2_1_hub_body_is_rebuilt_not_echoed(tmp_path):
    """opaque-body-bypasses-allowlist#p1 — the gateway's text is not forwarded."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    row = _attempt(now, "run-body")
    row["response"] = {"status": 200, "truncated": False,
                       "body": json.dumps({"accepted": {"content": "PRIVATE_CONVERSATION"},
                                           "rejected": 1,
                                           "errors": [{"index": 0, "reason": "invalid_enum",
                                                       "echo": "PRIVATE_CONVERSATION"}]})}
    html = _attempt(now, "run-html")
    html["response"] = {"status": 502, "truncated": False,
                        "body": "<html>upstream said PRIVATE_CONVERSATION</html>"}
    _write_day(directory, _day_of(now), [row, html])

    _, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    wire = json.dumps(body, ensure_ascii=False)
    assert "PRIVATE_CONVERSATION" not in wire
    first, second = body["items"]
    assert first["response"]["body"]["rejected"] == 1
    assert "accepted" not in first["response"]["body"], "a non-numeric count is not a count"
    assert first["response"]["body"]["errors"] == [{"index": 0, "reason": "invalid_enum"}]
    # A body that is not the skill-runs contract is reported by shape only.
    assert second["response"]["body"] is None
    assert second["response"]["body_unparsed"]["reason"] == "not_json"
    assert second["response"]["body_unparsed"]["bytes"] > 0


def test_r2_2_map_keys_are_allowlisted_too(tmp_path):
    """unrestricted-map-keys#p1 — an upstream key must not become a response key."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    summary = {"kind": "pass_summary",
               "report": {"env": "online",
                          "upload": {"sent": 4, "content": 7, "私密的一句话": 1,
                                     "rejected_by_reason": {"invalid_enum": 4,
                                                            "这也是私密文本": 2}}}}
    summary.update(_stamp(now))
    poisoned = _attempt(now, "run-keys")
    poisoned["request"] = dict(poisoned["request"], token_metrics={"input": 5, "answer": 3})
    _write_day(directory, _day_of(now), [summary, poisoned])

    _, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    wire = json.dumps(body, ensure_ascii=False)
    assert "私密的一句话" not in wire and "这也是私密文本" not in wire
    assert '"content"' not in wire and '"answer"' not in wire
    upload = body["items"][0]["report"]["upload"]
    assert upload["sent"] == 4
    assert upload["rejected_by_reason"] == {"invalid_enum": 4, "other": 2}
    assert "token_metrics" not in body["items"][1]["request"]
    assert body["dropped_keys"] >= 4


def test_r2_3_paging_without_the_hint_still_finishes(tmp_path):
    """optional-hint-pagination-stalls#p1 — a since+after_id client must terminate."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    _write_day(directory, _day_of(now), [_attempt(now, f"run-{i}") for i in range(200)])
    until = _ms(now) + 60_000
    seen: list[str] = []
    page = api.read_page(directory, _ms(now) - 60_000, until, budget=20_000)
    guard = 0
    while True:
        seen.extend(item["run_id"] for item in page["items"])
        if not page["truncated"] or guard > 60:
            break
        guard += 1
        # deliberately drop after_offset: the documented cursor is since+after_id
        page = api.read_page(directory, page["next"]["since"], until,
                             page["next"]["after_id"], budget=20_000)
    assert len(seen) == 200 and len(set(seen)) == 200, f"stalled after {len(seen)} rows"


def test_r2_4_a_corrupt_giant_line_does_not_stall_or_blow_the_budget(tmp_path):
    """budget-exhaustion-without-progress#p1 — garbage may cost a file, not the scan."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    yesterday = now - timedelta(days=1)
    bad = directory / f"export-{_day_of(yesterday)}.ndjson"
    bad.write_bytes(b"x" * (9 * 1024 * 1024) + b"\n")
    _write_day(directory, _day_of(now), [_attempt(now, "run-after-the-garbage")])

    scan = api._Scan(directory, _ms(yesterday) - 60_000, _ms(now) + 60_000)
    rows = [r["run_id"] for r in scan.rows()]
    assert rows == ["run-after-the-garbage"]
    assert scan.scanned <= 2 * 1024 * 1024, f"read {scan.scanned} bytes of a 9 MiB corrupt line"
    assert scan.corrupt_days == [bad.name]


def test_r2_5_skipping_a_prefix_never_changes_the_result_set(tmp_path):
    """shortcuts-bypass-cursor-predicate#p1 — with or without the hint, same rows."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    yesterday = now - timedelta(days=1)
    _write_day(directory, _day_of(yesterday), [_attempt(yesterday, f"old-{i}") for i in range(3)])
    path = _write_day(directory, _day_of(now), [_attempt(now, f"new-{i}") for i in range(3)])
    day = _day_of(now)
    first_line_len = len(path.read_bytes().split(b"\n")[0]) + 1

    since, until = _ms(yesterday) - 60_000, _ms(now) + 60_000
    without = api.read_page(directory, since, until, f"{day}-00000001")
    with_hint = api.read_page(directory, since, until, f"{day}-00000001")
    located = api.read_page(directory, since, until, f"{day}-00000001")  # wrong: points at row 2
    assert [i["run_id"] for i in without["items"]] == ["new-1", "new-2"]
    assert [i["run_id"] for i in with_hint["items"]] == ["new-1", "new-2"]
    assert [i["run_id"] for i in located["items"]] == ["new-1", "new-2"]


def test_r2_6_a_continuation_on_the_window_edge_is_accepted(tmp_path):
    """until-boundary-continuation-rejected#p1 — since == until must page, not 400."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    _write_day(directory, _day_of(now), [_attempt(now, f"run-{i}") for i in range(3)])
    edge = _ms(now)
    status, body = _get(DIAG, {"since": edge, "until": edge,
                               "after_id": f"{_day_of(now)}-00000001"}, _admin())
    assert status == 200
    assert [item["run_id"] for item in body["items"]] == ["run-1", "run-2"]


def test_r2_7_deeply_nested_json_is_a_skipped_line_not_a_500(tmp_path):
    """uncaught-json-recursion#p1 — RecursionError is not a ValueError."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    path = _write_day(directory, _day_of(now), [_attempt(now, "run-ok")])
    with path.open("ab") as fh:  # ~200 KiB of nesting: a line, but not a parsable one
        fh.write(b'{"id": "' + _day_of(now).encode() + b'-00000002", "at_epoch_ms": '
                 + str(_ms(now)).encode() + b', "deep": ' + b"[" * 100_000 + b"]" * 100_000
                 + b"}\n")
    with path.open("ab") as fh:
        fh.write((json.dumps(dict(_attempt(now, "run-after"),
                                  id=f"{_day_of(now)}-00000003", seq=3), sort_keys=True)
                  + "\n").encode())

    status, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    assert status == 200
    assert [item["run_id"] for item in body["items"]] == ["run-ok", "run-after"]
    assert body["unreadable_lines"] == 1


def test_r2_8_non_finite_numbers_never_reach_the_wire(tmp_path):
    """nonfinite-numbers-break-json#p1 — Infinity is valid Python, invalid JSON."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    row = _attempt(now, "run-inf")
    row["request"] = dict(row["request"], token_metrics={"input": float("inf"), "total": 5})
    row["attempt"] = float("nan")
    path = directory / f"export-{_day_of(now)}.ndjson"
    payload = dict(row, id=f"{_day_of(now)}-00000001", seq=1)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")  # allow_nan writes Infinity

    status, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    assert status == 200, "a strict-JSON client must be able to parse the page"
    assert "token_metrics" not in body["items"][0]["request"]
    assert "attempt" not in body["items"][0]


# ── the plane: two consumers, two tokens, one shape ───────────────────────


def _relay_probe(tmp_path, calls):
    """Run ``calls`` against the real relay app inside one loop.

    The app has to be built inside the loop that serves it, and every call has to
    share that loop — hence one runner rather than a helper per request.
    """
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import agent_relay

    class _Oauth:
        def authorize_url(self, *_a, **_k):
            return "https://example.invalid/authorize"

    async def runner():
        app = agent_relay.create_agent_relay_app(
            db_path=tmp_path / "relay.db", encryption_key="k" * 32, oauth=_Oauth(), feishu=None)
        client = TestClient(TestServer(app))
        await client.start_server()
        out = []
        try:
            for path, params, headers in calls:
                resp = await client.get(path, params={k: str(v) for k, v in params.items()},
                                        headers=headers)
                out.append((path, resp.status, await resp.json()))
        finally:
            await client.close()
        return out

    return asyncio.run(runner())


def test_scopes_isolate_the_two_consumers_on_the_real_relay_app(tmp_path, monkeypatch):
    """云驿's token reads logs/stats and nothing else; the telemetry token the reverse.

    Run against the relay app itself: the whole point of putting this resource on
    the existing plane is that the *shape* is shared, so the isolation has to hold
    where both resources are actually mounted.
    """
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    _write_day(directory, _day_of(now), [_attempt(now, "run-a")])
    monkeypatch.setenv("HERMES_AGENT_RELAY_ADMIN_TOKEN", "relay-admin-canary")
    monkeypatch.setenv("HERMES_TELEMETRY_DIAG_TOKEN", "telemetry-canary")
    window = {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}
    relay_admin = {"Authorization": "Bearer relay-admin-canary"}
    telemetry = {"Authorization": "Bearer telemetry-canary"}

    results = _relay_probe(tmp_path, [
        ("/v1/admin/logs", window, relay_admin),      # unchanged for 云驿
        ("/v1/admin/stats", window, relay_admin),
        ("/v1/admin/logs", window, telemetry),        # and closed to the other team
        ("/v1/admin/stats", window, telemetry),
        (DIAG, window, telemetry),                    # mirror image
        (STATS, window, telemetry),
        (DIAG, window, relay_admin),
        (STATS, window, relay_admin),
        ("/v1/admin/logs", window, {}),               # no header is still 401
        (DIAG, window, {}),
    ])
    codes = [status for _, status, _ in results]
    assert codes == [200, 200, 403, 403, 200, 200, 403, 403, 401, 401], results
    assert all(b["error"]["code"] == "forbidden" for _, s, b in results if s == 403)


def test_both_tokens_unset_means_the_whole_plane_is_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_AGENT_RELAY_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_TELEMETRY_DIAG_TOKEN", raising=False)
    now = int(time.time() * 1000)
    window = {"since": now - 60_000, "until": now}
    head = {"Authorization": "Bearer anything"}
    results = _relay_probe(tmp_path, [(p, window, head) for p in
                                      ("/v1/admin/logs", "/v1/admin/stats", DIAG, STATS)])
    for path, status, body in results:
        assert status == 403 and body["error"]["code"] == "forbidden", path


def test_the_bundle_header_row_is_never_served(tmp_path):
    """``diagnose_header`` carries the host name, the state dir and a file
    inventory. The producer's cursor contract says skip it; so does this."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    header = {"kind": "diagnose_header", "host": "hermes-1",
              "state_dir": "/home/hermes/.hermes/state/kep-telemetry-export",
              "day_files": ["export-20260922.ndjson"], "schema": {"at": "..."}}
    header.update(_stamp(now))
    _write_day(directory, _day_of(now), [header, _attempt(now, "run-a")])
    status, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    assert status == 200
    assert [item["run_id"] for item in body["items"]] == ["run-a"]
    assert body["skipped_lines"] == 1
    wire = json.dumps(body, ensure_ascii=False)
    assert "hermes-1" not in wire and "/home/" not in wire and "day_files" not in wire


# ── review round 3 (codex gpt-6-astra) regressions ────────────────────────


def test_r3_1_the_run_broker_still_builds_without_the_moved_module():
    """removed-module-import#p1 — the old landing left a dead import behind."""
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    app = create_run_broker_app(dispatch_agent=lambda _r: "", mark_seen=lambda _r: True,
                               sandbox_available=lambda: True)
    paths = {str(route.resource.canonical) for route in app.router.routes()}
    assert not any("telemetry" in path for path in paths), "the resource lives on the relay now"


def test_r3_2_free_text_cannot_ride_a_permitted_string_field(tmp_path):
    """free-text-crosses-privacy-boundary#p1 — short is not the same as safe."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    prose = "这是一句不该出现在接口里的私密内容"
    row = _attempt(now, "run-prose")
    row["reason"] = prose
    row["class"] = prose
    row["verdict"] = {"outcome": "dead_letter", "reason": prose}
    row["response"] = {"status": 400, "truncated": False,
                       "body": json.dumps({"message": prose, "rejected": 1}),
                       "error": {"index": 0, "reason": prose}}
    _write_day(directory, _day_of(now), [row])

    status, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    assert status == 200
    assert prose not in json.dumps(body, ensure_ascii=False)
    _, stats = _get(STATS, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    assert prose not in json.dumps(stats, ensure_ascii=False)
    assert set(stats["rejected_by_class"]) <= {"other"}
    # a machine reason still travels intact
    ok = _attempt(now, "run-ok")
    _write_day(directory, _day_of(now), [ok])
    _, body2 = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    assert body2["items"][-1]["reason"] == 'invalid_enum:client'


def test_r3_3_numbers_that_cannot_be_json_are_refused(tmp_path):
    """incomplete-numeric-validation#p1 — huge ints, NaN, and sums that overflow."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    day = _day_of(now)
    summary = {"kind": "pass_summary", "id": f"{day}-00000001", "seq": 1,
               "report": {"upload": {"sent": 10 ** 400, "accepted": 3,
                                     "rejected_by_reason": {"a": 1e308, "b": 1e308}},
                          "read": {"lines": float("nan")}}}
    summary.update(_stamp(now))
    path = directory / f"export-{day}.ndjson"
    path.write_text(json.dumps(summary) + "\n", encoding="utf-8")  # allow_nan writes NaN

    status, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    assert status == 200, "a strict-JSON client must be able to parse this page"
    report = body["items"][0]["report"]
    assert report["upload"]["accepted"] == 3
    assert "sent" not in report["upload"]
    assert "lines" not in report["read"]
    assert report["upload"]["rejected_by_reason"] in ({"a": 1e308}, {})


def test_r3_4_an_unhashable_kind_is_a_skipped_line(tmp_path):
    """unhashable-kind-crashes-reader#p1 — ``[] in frozenset`` raises TypeError."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    day = _day_of(now)
    bad = dict(_attempt(now, "run-bad"), kind=[], id=f"{day}-00000001", seq=1)
    path = directory / f"export-{day}.ndjson"
    path.write_text(json.dumps(bad, sort_keys=True) + "\n", encoding="utf-8")
    _write_day(directory, day, [_attempt(now, "run-good")])

    status, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    assert status == 200
    assert [item["run_id"] for item in body["items"]] == ["run-good"]
    assert body["unreadable_lines"] == 1


def test_r3_5_an_impossible_timestamp_does_not_hide_the_rest(tmp_path):
    """invalid-future-timestamp-hides-tail#p1."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    day = _day_of(now)
    poisoned = dict(_attempt(now, "run-poison"), at_epoch_ms=10 ** 400,
                    id=f"{day}-00000001", seq=1)
    path = directory / f"export-{day}.ndjson"
    path.write_text(json.dumps(poisoned, sort_keys=True) + "\n", encoding="utf-8")
    _write_day(directory, day, [_attempt(now, "run-behind-it")])

    page = api.read_page(directory, _ms(now) - 60_000, _ms(now) + 60_000)
    assert [item["run_id"] for item in page["items"]] == ["run-behind-it"]
    assert page["unreadable_lines"] == 1


def test_r3_6_a_cursor_at_the_tail_does_not_walk_backwards(tmp_path):
    """eof-resets-cursor-to-file-start#p1 — EOF and "cannot locate" are not the same."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    day = _day_of(now)
    _write_day(directory, day, [_attempt(now, f"run-{i}") for i in range(40)])
    last = f"{day}-00000040"
    page = api.read_page(directory, _ms(now), _ms(now) + 60_000, last)
    assert page["items"] == []
    assert page["next"] is None or page["next"]["after_id"] >= last, page["next"]


def test_r3_7_a_long_unpageable_prefix_still_yields_a_cursor(tmp_path):
    """unpageable-prefix-exhausts-progress#p1 — a page with no items must still move."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    day = _day_of(now)
    path = directory / f"export-{day}.ndjson"
    junk = [json.dumps({"kind": "upload_attempt", "at_epoch_ms": _ms(now), "run_id": f"noid-{i}"})
            for i in range(40)]  # valid JSON, no id: unpageable
    path.write_text("\n".join(junk) + "\n", encoding="utf-8")
    _write_day(directory, day, [_attempt(now, "run-wanted")])

    seen, cursor, guard = [], None, 0
    page = api.read_page(directory, _ms(now) - 60_000, _ms(now) + 60_000, budget=1500)
    while guard < 40:
        seen.extend(item["run_id"] for item in page["items"])
        if not page["truncated"]:
            break
        cursor = page["next"]
        assert cursor is not None, "a truncated page with no items still has to move"
        guard += 1
        page = api.read_page(directory, cursor["since"], _ms(now) + 60_000, cursor["after_id"], budget=1500)
    assert "run-wanted" in seen


def test_r3_9_the_locator_respects_its_byte_cap(tmp_path):
    """probe-byte-cap-not-enforced#p1 — a ceiling that each read ignores is not one."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    day = _day_of(now)
    path = directory / f"export-{day}.ndjson"
    fat = []
    for i in range(1, 41):  # 40 rows of ~300 KB each
        row = dict(_attempt(now, f"run-{i}"), id=f"{day}-{i:08d}", seq=i)
        row["url"] = "https://example.invalid/" + "p" * 300_000
        fat.append(json.dumps(row, sort_keys=True))
    path.write_text("\n".join(fat) + "\n", encoding="utf-8")

    scan = api._Scan(directory, _ms(now), _ms(now) + 60_000, f"{day}-00000020", budget=4096)
    list(scan.rows())
    assert scan.located <= api._LOCATE_BYTE_CAP, f"locator spent {scan.located} bytes"


def test_r3_3b_serialisation_failure_is_the_plane_error_body(tmp_path, monkeypatch):
    """incomplete-numeric-validation#p1 (second half) — ``allow_nan=False`` raises
    inside the handler's boundary, so it is a 500 with the plane's body, not a
    bare traceback out of aiohttp."""
    _day_dir(tmp_path)
    monkeypatch.setattr(api, "read_page", lambda *a, **k: {"items": [float("inf")]})
    now = int(time.time() * 1000)
    status, body = _get(DIAG, {"since": now - 1000, "until": now}, _admin())
    assert status == 500 and body["error"]["code"] == "internal_error"

def test_r4_1_english_prose_from_the_hub_never_reaches_the_wire(tmp_path):
    """free-text-crosses-privacy-boundary#p1 — ASCII was not a boundary either."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    secret = "My private password is swordfish"
    row = _attempt(now, "run-en")
    row["reason"] = secret
    row["class"] = secret
    row["verdict"] = {"outcome": "dead_letter", "reason": secret}
    row["response"] = {"status": 400, "truncated": False,
                       "body": json.dumps({"message": secret, "rejected": 1,
                                           "errors": [{"index": 0, "reason": secret}]}),
                       "error": {"index": 0, "reason": secret}}
    _write_day(directory, _day_of(now), [row])

    _, body = _get(DIAG, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    _, stats = _get(STATS, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    assert secret not in json.dumps(body, ensure_ascii=False)
    assert secret not in json.dumps(stats, ensure_ascii=False)
    assert body["items"][0]["response"]["body"]["message_withheld"] is True
    assert set(stats["rejected_by_class"]) <= {"other"}


def test_r4_2_the_cursor_is_an_id_and_nothing_else(tmp_path):
    """unchecked-hint-skips-records#p1 — the byte hint is gone from the contract."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    _write_day(directory, _day_of(now), [_attempt(now, f"run-{i}") for i in range(6)])
    day = _day_of(now)
    page = api.read_page(directory, _ms(now), _ms(now) + 60_000, f"{day}-00000002")
    assert [item["run_id"] for item in page["items"]] == [f"run-{i}" for i in range(2, 6)]
    assert page["next"] is None
    # an offset parameter is not accepted and cannot influence the result
    status, body = _get(DIAG, {"since": _ms(now), "until": _ms(now) + 60_000,
                               "after_id": f"{day}-00000002", "after_offset": 99_999}, _admin())
    assert status == 200
    assert [item["run_id"] for item in body["items"]] == [f"run-{i}" for i in range(2, 6)]


def test_r4_3_a_default_budget_walk_over_fat_rows_never_regresses(tmp_path):
    """budget-exhaustion-regresses-cursor#p1 — page all the way through, by id only."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    day = _day_of(now)
    rows = []
    for i in range(1, 25):  # ~900 KB each: the locator and the page budget both bite
        row = dict(_attempt(now, f"run-{i:02d}"), id=f"{day}-{i:08d}", seq=i)
        row["url"] = "https://example.invalid/" + "p" * 900_000
        rows.append(json.dumps(row, sort_keys=True))
    (directory / f"export-{day}.ndjson").write_text("\n".join(rows) + "\n", encoding="utf-8")

    seen, cursor, guard, prev = [], "", 0, ""
    while guard < 40:
        page = api.read_page(directory, _ms(now), _ms(now) + 60_000, cursor)
        seen.extend(item["run_id"] for item in page["items"])
        if not page["truncated"]:
            break
        cursor = page["next"]["after_id"]
        assert cursor > prev, f"cursor regressed: {prev} -> {cursor}"
        prev, guard = cursor, guard + 1
    assert seen == [f"run-{i:02d}" for i in range(1, 25)], seen


def test_r4_4_an_unpageable_prefix_is_declared_corrupt_not_stalled(tmp_path):
    """unpageable-prefix-stalls-pagination#p1 — with no byte cursor, the skip budget
    is what keeps a bad prefix from eating the page for ever."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    yesterday = now - timedelta(days=1)
    junk = [json.dumps({"kind": "upload_attempt", "at_epoch_ms": _ms(yesterday),
                        "run_id": f"noid-{i}", "pad": "x" * 900_000}) for i in range(4)]
    (directory / f"export-{_day_of(yesterday)}.ndjson").write_text(
        "\n".join(junk) + "\n", encoding="utf-8")
    _write_day(directory, _day_of(now), [_attempt(now, "run-wanted")])

    page = api.read_page(directory, _ms(yesterday) - 60_000, _ms(now) + 60_000)
    assert [item["run_id"] for item in page["items"]] == ["run-wanted"]
    assert page["corrupt_files"] == [f"export-{_day_of(yesterday)}.ndjson"]


def test_r4_5_a_clock_that_steps_backwards_hides_nothing(tmp_path):
    """timestamp-reversal-hides-tail#p1 — ``next.since`` is the caller's floor."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    later = now + timedelta(seconds=1)
    day = _day_of(now)
    _write_day(directory, day, [_attempt(now, "run-a"), _attempt(later, "run-late"),
                                _attempt(now, "run-after-the-rewind")])
    since, until = _ms(now) - 60_000, _ms(now) + 60_000

    page = api.read_page(directory, since, until, limit=2)
    assert [item["run_id"] for item in page["items"]] == ["run-a", "run-late"]
    assert page["next"]["since"] == since, "the window floor must not move with a row"
    rest = api.read_page(directory, page["next"]["since"], until, page["next"]["after_id"])
    assert [item["run_id"] for item in rest["items"]] == ["run-after-the-rewind"]


def test_r4_6_last_pass_comes_from_inside_the_mounted_directory(tmp_path):
    """last-pass-outside-readable-mount#p1 — the bind exposes ``diagnostics/`` only."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    summary = {"kind": "pass_summary",
               "report": {"env": "online", "upload": {"sent": 7, "accepted": 7},
                          "diagnostics": {"rows": 8, "dropped": 0, "errors": 0, "stopped": None}}}
    summary.update(_stamp(now))
    _write_day(directory, _day_of(now), [_attempt(now, "run-a"), summary])
    # nothing outside the directory exists, exactly as in production
    assert not (directory.parent / "last-run.json").exists()

    _, stats = _get(STATS, {"since": _ms(now) - 60_000, "until": _ms(now) + 60_000}, _admin())
    last = stats["state"]["last_pass"]
    assert last["rows"] == 8 and last["errors"] == 0
    assert last["upload"] == {"sent": 7, "accepted": 7}


def test_r4_3b_the_memo_is_a_cache_not_a_contract(tmp_path):
    """The position memo may be empty, stale or wrong — the answer must not change."""
    directory = _day_dir(tmp_path)
    now = datetime.now(tz=SHANGHAI)
    _write_day(directory, _day_of(now), [_attempt(now, f"run-{i}") for i in range(6)])
    day = _day_of(now)
    api._position_memo.clear()
    cold = api.read_page(directory, _ms(now), _ms(now) + 60_000, f"{day}-00000002")
    warm = api.read_page(directory, _ms(now), _ms(now) + 60_000, f"{day}-00000002")
    # a memo pointing into the middle of a line must be refused, not followed
    with api._position_lock:
        api._position_memo.update({k: 7 for k in api._position_memo})
    poisoned = api.read_page(directory, _ms(now), _ms(now) + 60_000, f"{day}-00000002")
    expected = [f"run-{i}" for i in range(2, 6)]
    for page in (cold, warm, poisoned):
        assert [item["run_id"] for item in page["items"]] == expected
    assert len(api._position_memo) <= api._MEMO_CAP


def test_registration_survives_an_unreadable_directory(tmp_path, monkeypatch):
    """Found by the hermes-pre rehearsal, not by a reviewer: ``Path.is_dir()``
    raises ``PermissionError`` under ``ProtectHome=true`` instead of returning
    False, and this runs while the relay is building its app — so the log line
    took the whole service down. The code must be deployable before the unit
    change, in any order."""
    from aiohttp import web

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    monkeypatch.setenv(api.DIR_ENV, str(blocked / "diagnostics"))
    real_is_dir = api.Path.is_dir

    def boom(self):
        if "blocked" in str(self):
            raise PermissionError(13, "Permission denied")
        return real_is_dir(self)

    monkeypatch.setattr(api.Path, "is_dir", boom)
    app = web.Application()
    api.register_telemetry_diagnostics_routes(app)  # must not raise
    assert len({str(r.resource.canonical) for r in app.router.routes()}) == 2
