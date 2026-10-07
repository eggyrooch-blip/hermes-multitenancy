from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_multitenancy.analytics.kep_telemetry_export import (
    BOUNDARY_TURN_CLOSED,
    BOUNDARY_UNRESOLVED,
    CLIENT,
    LIFECYCLE_V9_KEYS,
    SURFACE,
    RunTerminal,
    SkillCall,
    UploadSafetyError,
    assert_upload_safe,
    build_record,
    parse_audit_line,
    read_new_audit_lines,
    run_export,
    run_id_for,
    settle_batch,
    status_report,
    InvalidSkillName,
    make_batch_id,
    normalize_skill_name,
    rfc3339,
    parse_ts,
)
import re

SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=SHANGHAI)


def ts(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def ts_ms(minutes_ago: float) -> str:
    """What the exporter emits for an audit timestamp: millisecond precision, +08:00."""
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="milliseconds")


def skill_row(
    when: str,
    *,
    profile: str = "profile_a",
    platform: str = "webui",
    session: str = "s1",
    message_id: int = 1,
    skill: str | None = "lark-base",
) -> dict:
    args = None if skill is None else {"name": skill}
    preview = "generating arguments" if skill is None else f"skill_view({skill})"
    return {
        "@timestamp": when,
        "event_type": "conversation_message",
        "profile": profile,
        "platform": platform,
        "chat_type": "dm",
        "session_id": session,
        "message_id": message_id,
        "role": "assistant",
        "content": "",
        "tool_name": "skill_view",
        "tool_calls": json.dumps({"name": "skill_view", "args": args, "preview": preview}, ensure_ascii=False),
        "finish_reason": None,
        "source": "state_db_mirror",
    }


def terminal_row(
    when: str,
    *,
    profile: str = "profile_a",
    platform: str = "webui",
    expert_id: str | None = "resource-delivery",
    status: str = "completed",
) -> dict:
    return {
        "@timestamp": when,
        "event_type": "run_terminal",
        "schema_version": 1,
        "terminal_event_id": f"run-{when}",
        "profile": profile,
        "platform": platform,
        "chat_type": "dm",
        "source": "run_broker",
        "expert_requested": expert_id is not None,
        "expert_id": expert_id,
        "expert_resolution": "resolved",
        "terminal_status": status,
        "error_code": None,
        "failure_subsystem": None,
        "retryable": False,
        "retried": False,
        "answer_completed": status == "completed",
        "duration_ms": 12,
    }


def write_audit(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


class FakeHub:
    """Records every posted body and replays a scripted sequence of responses."""

    def __init__(self, responses: list[tuple[int | None, object]] | None = None) -> None:
        self.responses = list(responses or [])
        self.bodies: list[dict] = []
        self.headers: list[dict] = []
        self.urls: list[str] = []

    def __call__(self, url: str, headers: dict, body: bytes) -> tuple[int | None, object]:
        self.urls.append(url)
        self.headers.append(headers)
        payload = json.loads(body.decode("utf-8"))
        self.bodies.append(payload)
        if self.responses:
            return self.responses.pop(0)
        runs = payload["runs"]
        return 200, {"accepted": len(runs), "rejected": 0, "errors": []}


# ── parse_audit_line ──────────────────────────────────────────────────────


def test_parse_audit_line_reads_a_resolved_skill_view() -> None:
    fact = parse_audit_line(json.dumps(skill_row(ts(10), skill="kippieswork-ops", message_id=7)))

    assert isinstance(fact, SkillCall)
    assert fact.skill == "kippieswork-ops"
    assert fact.profile == "profile_a"
    assert fact.platform == "webui"
    assert fact.session_id == "s1"
    assert fact.message_id == "7"
    assert fact.observed_at == ts(10)


def test_parse_audit_line_skips_generating_arguments_rows() -> None:
    assert parse_audit_line(json.dumps(skill_row(ts(10), skill=None))) is None


def test_parse_audit_line_reads_run_terminal() -> None:
    fact = parse_audit_line(json.dumps(terminal_row(ts(5), expert_id="lark-base-expert")))

    assert isinstance(fact, RunTerminal)
    assert fact.expert_id == "lark-base-expert"
    assert fact.terminal_status == "completed"
    assert fact.at == ts(5)


def test_parse_audit_line_ignores_garbage_and_foreign_rows() -> None:
    assert parse_audit_line("not json at all") is None
    assert parse_audit_line("") is None
    assert parse_audit_line("[1, 2, 3]") is None
    assert parse_audit_line(json.dumps({"event_type": "conversation_message"})) is None
    row = skill_row(ts(1))
    row["@timestamp"] = "yesterday afternoon"
    assert parse_audit_line(json.dumps(row)) is None
    other = skill_row(ts(1))
    other["tool_name"] = "terminal"
    assert parse_audit_line(json.dumps(other)) is None
    missing_id = skill_row(ts(1))
    missing_id["session_id"] = ""
    assert parse_audit_line(json.dumps(missing_id)) is None


# ── build_record ──────────────────────────────────────────────────────────


def _call(when: str, **kw) -> SkillCall:
    fact = parse_audit_line(json.dumps(skill_row(when, **kw)))
    assert isinstance(fact, SkillCall)
    return fact


def _terminal(when: str, **kw) -> RunTerminal:
    fact = parse_audit_line(json.dumps(terminal_row(when, **kw)))
    assert isinstance(fact, RunTerminal)
    return fact


def test_build_record_unmatched_call_is_unresolved_and_content_free() -> None:
    record = build_record(_call(ts(40)), None)

    assert set(record) <= LIFECYCLE_V9_KEYS
    assert record["client"] == CLIENT == "hermes"
    assert record["surface"] == SURFACE == "cloud"
    assert record["status"] == "closed"
    assert record["record_kind"] == "lifecycle"
    assert record["skill"] == "lark-base"
    assert record["ended_at"] == record["opened_observed_at"] == ts_ms(40)
    assert (record["boundary_status"], record["boundary_source"], record["boundary_confidence"]) == BOUNDARY_UNRESOLVED
    assert "expert" not in record
    assert record["model_name"] is None and record["project_id"] is None
    assert "profile" not in record and "session_id" not in record


def test_build_record_matched_terminal_carries_the_turn_closed_triple_and_expert() -> None:
    record = build_record(_call(ts(40)), _terminal(ts(38), expert_id="resource-delivery"))

    assert (record["boundary_status"], record["boundary_source"], record["boundary_confidence"]) == BOUNDARY_TURN_CLOSED
    assert BOUNDARY_TURN_CLOSED == ("candidate", "prompt_stop", "medium")
    assert record["expert"] == "resource-delivery"
    assert record["ended_at"] == ts_ms(38)


def test_build_record_omits_expert_when_the_terminal_has_none() -> None:
    record = build_record(_call(ts(40)), _terminal(ts(38), expert_id=None))

    assert "expert" not in record
    assert record["boundary_status"] == "candidate"


def test_assert_upload_safe_rejects_unknown_keys_and_leaky_strings() -> None:
    good = build_record(_call(ts(40)), None)

    with pytest.raises(UploadSafetyError):
        assert_upload_safe({**good, "profile": "feishu_ou_x"})
    with pytest.raises(UploadSafetyError):
        assert_upload_safe({**good, "skill": "/home/x"})
    with pytest.raises(UploadSafetyError):
        assert_upload_safe({**good, "skill": "/Users/dev/skills/a"})
    assert assert_upload_safe(good) is good


# ── run_id determinism ────────────────────────────────────────────────────


def test_run_id_is_deterministic_32_hex_over_profile_session_message() -> None:
    a = run_id_for("profile_a", "s1", "7")
    b = run_id_for("profile_a", "s1", "7")

    assert a == b
    assert len(a) == 32 and all(c in "0123456789abcdef" for c in a)
    assert a != run_id_for("profile_b", "s1", "7")
    assert a != run_id_for("profile_a", "s2", "7")
    assert a != run_id_for("profile_a", "s1", "8")


def test_run_export_twice_over_the_same_file_settles_each_record_once(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    hub = FakeHub()

    first = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)
    second = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    assert first["upload"]["accepted"] == 1
    assert second["upload"]["accepted"] == 0
    assert second["upload"]["batches"] == 0
    ledger = json.loads((state / "ledger.json").read_text(encoding="utf-8"))["confirmed"]
    assert len(ledger) == 1
    assert list(ledger) == [run_id_for("profile_a", "s1", "1")]
    assert json.loads((state / "outbox.json").read_text(encoding="utf-8")) == []
    assert len(hub.bodies) == 1


def test_run_export_after_rotation_replays_the_file_without_resending(tmp_path: Path) -> None:
    """Rotation makes the exporter re-read from byte 0; the ledger is what stops a re-send."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    rows = [skill_row(ts(60), message_id=1), terminal_row(ts(59))]
    write_audit(audit, rows)
    hub = FakeHub()

    first = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)
    assert first["upload"]["accepted"] == 1

    # same content, new inode — the pass restarts at 0 and sees the call again
    audit.unlink()
    write_audit(audit, rows)
    second = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    assert second["read"]["restarted"] is True
    assert second["read"]["skill_calls"] == 1
    assert second["built"]["already_confirmed"] == 1
    assert second["built"]["records"] == 0
    assert len(hub.bodies) == 1  # nothing posted twice
    assert len(json.loads((state / "ledger.json").read_text(encoding="utf-8"))["confirmed"]) == 1


# ── read_new_audit_lines ──────────────────────────────────────────────────


def test_read_new_audit_lines_reads_only_the_appended_tail(tmp_path: Path) -> None:
    audit = tmp_path / "a.jsonl"
    audit.write_text("one\ntwo\n", encoding="utf-8")

    lines, cursor, restarted, _ = read_new_audit_lines(audit, {})
    assert lines == ["one", "two"] and restarted is False

    with audit.open("a", encoding="utf-8") as fh:
        fh.write("three\n")
    lines2, cursor2, restarted2, _ = read_new_audit_lines(audit, cursor)

    assert lines2 == ["three"]
    assert restarted2 is False
    assert cursor2["offset"] == audit.stat().st_size


def test_read_new_audit_lines_defers_a_partial_trailing_line(tmp_path: Path) -> None:
    audit = tmp_path / "a.jsonl"
    audit.write_text("one\npart", encoding="utf-8")

    lines, cursor, _, _ = read_new_audit_lines(audit, {})
    assert lines == ["one"]
    assert cursor["offset"] == len("one\n")

    with audit.open("a", encoding="utf-8") as fh:
        fh.write("ial\n")
    lines2, cursor2, _, _ = read_new_audit_lines(audit, cursor)

    assert lines2 == ["partial"]
    assert cursor2["offset"] == audit.stat().st_size


def test_read_new_audit_lines_restarts_on_rotation_and_on_truncation(tmp_path: Path) -> None:
    audit = tmp_path / "a.jsonl"
    audit.write_text("one\ntwo\n", encoding="utf-8")
    _, cursor, _, _ = read_new_audit_lines(audit, {})

    # rotation: same path, new inode
    audit.unlink()
    audit.write_text("fresh\n", encoding="utf-8")
    lines, cursor_after, restarted, _ = read_new_audit_lines(audit, cursor)
    assert restarted is True
    assert lines == ["fresh"]
    assert cursor_after["inode"] != cursor["inode"]

    # truncation in place: same inode, cursor beyond EOF
    stale = {"inode": cursor_after["inode"], "offset": 10_000}
    lines2, _, restarted2, _ = read_new_audit_lines(audit, stale)
    assert restarted2 is True
    assert lines2 == ["fresh"]


def test_read_new_audit_lines_caps_one_pass_and_resumes_next_pass(tmp_path: Path) -> None:
    audit = tmp_path / "a.jsonl"
    audit.write_text("aaaa\nbbbb\ncccc\n", encoding="utf-8")

    lines, cursor, _, _ = read_new_audit_lines(audit, {}, max_bytes=7)
    assert lines == ["aaaa"]

    lines2, cursor2, _, _ = read_new_audit_lines(audit, cursor, max_bytes=7)
    assert lines2 == ["bbbb"]

    lines3, cursor3, _, _ = read_new_audit_lines(audit, cursor2, max_bytes=7)
    assert lines3 == ["cccc"]
    assert cursor3["offset"] == audit.stat().st_size


def test_read_new_audit_lines_steps_over_a_line_longer_than_the_budget(tmp_path: Path) -> None:
    audit = tmp_path / "a.jsonl"
    audit.write_text("x" * 50 + "\nok\n", encoding="utf-8")

    lines, cursor, _, _ = read_new_audit_lines(audit, {}, max_bytes=8)
    assert lines == []
    assert cursor["offset"] == 8  # advanced, never stuck on the oversized line

    seen: list[str] = []
    for _ in range(20):
        lines, cursor, _, _ = read_new_audit_lines(audit, cursor, max_bytes=8)
        seen.extend(lines)
    assert "ok" in seen


# ── settle_batch (parity with kep-telemetry lib/outbox.mjs) ───────────────


def _batch(n: int) -> list[dict]:
    return [build_record(_call(ts(60), message_id=i), None) for i in range(1, n + 1)]


def test_settle_batch_network_error_retries_everything() -> None:
    batch = _batch(2)
    out = settle_batch(batch, None, None)

    assert out["action"] == "retry"
    assert out["reason"] == "network_error"
    assert out["retry"] == batch
    assert out["confirmed"] == [] and out["dead_letter"] == []


@pytest.mark.parametrize("status", [401, 403])
def test_settle_batch_auth_failure_pauses(status: int) -> None:
    batch = _batch(2)
    out = settle_batch(batch, status, None)

    assert out["action"] == "pause_auth"
    assert out["reason"] == "auth_required"
    assert out["retry"] == batch


def test_settle_batch_rate_limit_and_server_error_retry_later() -> None:
    batch = _batch(1)

    rate = settle_batch(batch, 429, None)
    assert rate["action"] == "retry_later" and rate["reason"] == "rate_limited"

    boom = settle_batch(batch, 503, None)
    assert boom["action"] == "retry_later" and boom["reason"] == "http_503"
    assert boom["retry"] == batch


def test_settle_batch_400_quarantines_the_whole_batch() -> None:
    batch = _batch(3)
    out = settle_batch(batch, 400, {"message": "bad"})

    assert out["action"] == "quarantine_batch"
    assert out["retry"] == []
    assert [reason for _, reason in out["dead_letter"]] == ["invalid_batch"] * 3


def test_settle_batch_unexpected_status_retries() -> None:
    batch = _batch(1)
    out = settle_batch(batch, 302, None)

    assert out["action"] == "retry"
    assert out["reason"] == "unexpected_http_302"


def test_settle_batch_200_confirms_all_but_the_indexed_error() -> None:
    batch = _batch(3)
    body = {"accepted": 2, "rejected": 1, "errors": [{"index": 1, "reason": "client_not_allowed"}]}

    out = settle_batch(batch, 200, body)

    assert out["action"] == "settled" and out["reason"] is None
    assert out["confirmed"] == [batch[0], batch[2]]
    assert out["dead_letter"] == [(batch[1], "client_not_allowed")]
    assert out["retry"] == []


def test_settle_batch_rejects_a_response_that_does_not_add_up() -> None:
    batch = _batch(2)

    assert settle_batch(batch, 200, None)["reason"] == "invalid_response_shape"
    assert settle_batch(batch, 200, {"accepted": 2})["reason"] == "invalid_response_shape"
    assert settle_batch(batch, 200, {"accepted": 1, "rejected": 1, "errors": []})["reason"] == "invalid_response_shape"
    counts_off = {"accepted": 0, "rejected": 1, "errors": [{"index": 0, "reason": "x"}]}
    assert settle_batch(batch, 200, counts_off)["reason"] == "invalid_response_shape"
    for out in (
        settle_batch(batch, 200, {"accepted": 1, "rejected": 1, "errors": [{"index": 9, "reason": "x"}]}),
        settle_batch(batch, 200, {"accepted": 1, "rejected": 1, "errors": [{"index": 0, "reason": ""}]}),
        settle_batch(batch, 200, {"accepted": 1, "rejected": 1, "errors": [{"reason": "x"}]}),
    ):
        assert out["action"] == "retry" and out["reason"] == "invalid_response_indices"
    dup = {"accepted": 0, "rejected": 2, "errors": [{"index": 0, "reason": "x"}, {"index": 0, "reason": "y"}]}
    assert settle_batch(batch, 200, dup)["reason"] == "invalid_response_indices"


def test_settle_batch_refuses_a_mismatched_run_id() -> None:
    batch = _batch(2)
    body = {"accepted": 1, "rejected": 1, "errors": [{"index": 0, "reason": "x", "run_id": "deadbeef"}]}

    out = settle_batch(batch, 200, body)

    assert out["action"] == "retry"
    assert out["reason"] == "invalid_response_run_id"
    assert out["retry"] == batch


def test_settle_batch_accepts_the_servers_matching_run_id() -> None:
    batch = _batch(2)
    body = {"accepted": 1, "rejected": 1, "errors": [{"index": 0, "reason": "x", "run_id": batch[0]["run_id"]}]}

    out = settle_batch(batch, 200, body)

    assert out["action"] == "settled"
    assert out["confirmed"] == [batch[1]]


# ── run_export end to end ─────────────────────────────────────────────────


def test_run_export_holds_a_fresh_call_and_emits_an_aged_one(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(
        audit,
        [
            skill_row(ts(5), session="fresh", message_id=1, skill="lark-doc"),
            skill_row(ts(40), session="aged", message_id=2, skill="lark-base"),
        ],
    )
    hub = FakeHub()

    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    assert report["read"]["skill_calls"] == 2
    assert report["built"]["records"] == 1
    assert report["built"]["unresolved"] == 1
    assert report["built"]["still_pending"] == 1
    runs = hub.bodies[0]["runs"]
    assert [r["skill"] for r in runs] == ["lark-base"]
    assert runs[0]["boundary_status"] == "unresolved"
    # the fresh call is kept for the next pass, not dropped
    assert [c["skill"] for c in json.loads((state / "pending.json").read_text(encoding="utf-8"))] == ["lark-doc"]


def test_run_export_matches_a_terminal_two_minutes_later(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(
        audit,
        [
            skill_row(ts(40), message_id=3, skill="kippieswork-ops"),
            terminal_row(ts(38), expert_id="resource-delivery"),
        ],
    )
    hub = FakeHub()

    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    assert report["built"]["with_terminal"] == 1
    assert report["upload"]["accepted"] == 1
    run = hub.bodies[0]["runs"][0]
    assert (run["boundary_status"], run["boundary_source"], run["boundary_confidence"]) == BOUNDARY_TURN_CLOSED
    assert run["expert"] == "resource-delivery"
    assert run["ended_at"] == ts_ms(38)
    assert run["opened_observed_at"] == ts_ms(40)


def test_run_export_dry_run_writes_nothing_and_sends_nothing(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(40), message_id=4), terminal_row(ts(39))])
    hub = FakeHub()

    report = run_export(
        audit_path=audit, state_dir=state, now=NOW, dry_run=True, credentials=("tok", "sunke"), http_post=hub
    )

    assert report["dry_run"] is True
    assert report["upload"]["would_send"] == 1
    assert report["upload"]["sample"][0]["skill"] == "lark-base"
    assert hub.bodies == []
    assert not state.exists()


def test_run_export_dead_letters_a_rejected_record_and_status_counts_it(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(50), message_id=5), skill_row(ts(49), message_id=6, skill="lark-im")])
    hub = FakeHub([(200, {"accepted": 1, "rejected": 1, "errors": [{"index": 1, "reason": "client_not_allowed"}]})])

    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    assert report["upload"]["accepted"] == 1
    assert report["upload"]["rejected_by_reason"] == {"client_not_allowed": 1}
    dead = [json.loads(line) for line in (state / "dead-letter.ndjson").read_text(encoding="utf-8").splitlines()]
    assert [row["skill"] for row in dead] == ["lark-im"]
    assert dead[0]["reason"] == "client_not_allowed"

    status = status_report(state)
    assert status["confirmed"] == 1
    assert status["dead_letter"] == 1
    assert status["dead_letter_by_reason"] == {"client_not_allowed": 1}
    assert status["outbox"] == 0
    assert status["last_run"]["upload"]["accepted"] == 1


def test_run_export_keeps_the_profile_out_of_the_body_and_carries_it_as_operator(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(45), profile="feishu_ou_REDACTED", message_id=8)])
    hub = FakeHub()

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    rid = run_id_for("feishu_ou_REDACTED", "s1", "8")
    assert json.loads((state / "profile-map.json").read_text(encoding="utf-8")) == {rid: "feishu_ou_REDACTED"}
    raw = json.dumps(hub.bodies, ensure_ascii=False)
    for leak in ("feishu_ou_REDACTED", "profile", "session_id", "/home/", "/Users/"):
        assert leak not in raw
    assert hub.headers[0]["Authorization"] == "Bearer tok"
    # actor dimension rides in the header, never in the record body (sunke 2026-09-20)
    assert hub.headers[0]["X-Operator"] == "feishu_ou_REDACTED"


def test_run_export_stops_the_round_on_auth_failure_and_keeps_the_queue(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(50), message_id=i) for i in range(1, 4)])
    hub = FakeHub([(401, {"message": "token expired"})])

    report = run_export(
        audit_path=audit, state_dir=state, now=NOW, batch_size=1, credentials=("tok", "sunke"), http_post=hub
    )

    assert report["upload"]["stopped"] == "auth_required"
    assert report["upload"]["batches"] == 1  # stopped after the first attempt
    assert report["upload"]["accepted"] == 0
    assert len(json.loads((state / "outbox.json").read_text(encoding="utf-8"))) == 3
    assert status_report(state)["outbox"] == 3


def test_run_export_backfill_window_drops_calls_older_than_the_cutoff(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(
        audit,
        [
            skill_row(ts(60 * 24 * 9), session="old", message_id=1, skill="ancient"),
            skill_row(ts(60), session="recent", message_id=2, skill="lark-base"),
        ],
    )
    hub = FakeHub()

    report = run_export(
        audit_path=audit, state_dir=state, now=NOW, backfill_days=7, credentials=("tok", "sunke"), http_post=hub
    )

    assert report["read"]["skill_calls"] == 2
    assert report["built"]["records"] == 1
    assert [r["skill"] for r in hub.bodies[0]["runs"]] == ["lark-base"]


def test_run_export_batches_at_the_requested_size(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(50), message_id=i) for i in range(1, 6)])
    hub = FakeHub()

    report = run_export(
        audit_path=audit, state_dir=state, now=NOW, batch_size=2, credentials=("tok", "sunke"), http_post=hub
    )

    assert [len(body["runs"]) for body in hub.bodies] == [2, 2, 1]
    assert report["upload"]["accepted"] == 5
    assert len({body["batch_id"] for body in hub.bodies}) == 3


def test_run_export_missing_audit_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_export(audit_path=tmp_path / "nope.jsonl", state_dir=tmp_path / "state", now=NOW,
                   credentials=("tok", "sunke"), http_post=FakeHub())


# ── CLI wiring ────────────────────────────────────────────────────────────


def test_cli_status_prints_the_state_dir_report(tmp_path: Path, capsys) -> None:
    from hermes_multitenancy.analytics.cli import main

    code = main(["kep-telemetry-export", "--status", "--state-dir", str(tmp_path / "state")])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["confirmed"] == 0 and payload["dead_letter"] == 0


def test_cli_dry_run_reports_without_touching_state(tmp_path: Path, capsys) -> None:
    from hermes_multitenancy.analytics.cli import main

    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(40), message_id=1)])

    code = main([
        "kep-telemetry-export", "--dry-run", "--audit", str(audit), "--state-dir", str(state), "--env", "pre",
        # fixture rows are pinned to NOW (2026-09-20); keep them inside the wall-clock lookback
        "--backfill-days", "36500",
    ])

    out = capsys.readouterr().out
    assert code == 0
    assert "dry-run" in out and "would_send=1" in out
    assert not state.exists()


def test_cli_missing_audit_exits_1(tmp_path: Path, capsys) -> None:
    from hermes_multitenancy.analytics.cli import main

    code = main(["kep-telemetry-export", "--audit", str(tmp_path / "nope.jsonl"), "--state-dir", str(tmp_path / "s")])

    assert code == 1
    assert "audit not readable" in capsys.readouterr().err


def test_cli_exits_2_when_the_upload_stops_for_auth(tmp_path: Path, capsys, monkeypatch) -> None:
    from hermes_multitenancy.analytics import cli

    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(50), message_id=1)])
    monkeypatch.setattr(
        cli,
        "run_export",
        lambda **kw: {
            "at": "2026-09-20T12:00:00+08:00", "env": "online", "dry_run": False, "audit": str(audit),
            "read": {"lines": 1, "skill_calls": 1, "terminals": 0, "restarted": False, "backfill_cutoff": None},
            "built": {"records": 1, "with_terminal": 0, "unresolved": 1, "still_pending": 0, "already_confirmed": 0},
            "upload": {"batches": 1, "sent": 1, "accepted": 0, "rejected_by_reason": {}, "retry": 1,
                       "queued": 1, "stopped": "auth_required"},
        },
    )

    code = cli.main(["kep-telemetry-export", "--audit", str(audit), "--state-dir", str(state)])

    assert code == 2
    assert "stopped auth_required" in capsys.readouterr().out


def test_cli_json_flag_emits_the_raw_report(tmp_path: Path, capsys, monkeypatch) -> None:
    from hermes_multitenancy.analytics import cli
    from hermes_multitenancy.analytics import kep_telemetry_export as export_mod

    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(50), message_id=1)])
    hub = FakeHub()
    # run_export binds its http_post default at def time, so the fake hub has to be
    # injected at the call site — never leave a CLI test able to reach the real Hub.
    real = export_mod.run_export
    monkeypatch.setattr(
        cli, "run_export", lambda **kw: real(**kw, credentials=("tok", "sunke"), http_post=hub)
    )

    code = cli.main([
        "kep-telemetry-export", "--json", "--audit", str(audit), "--state-dir", str(state), "--batch-size", "10",
        "--backfill-days", "36500",
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["upload"]["accepted"] == 1
    assert payload["env"] == "online"


# ── contract follow-ups: timestamps, batch id, charsets ─────────────────────


def test_rfc3339_emits_millisecond_precision_with_numeric_offset() -> None:
    out = rfc3339(NOW)
    assert out == "2026-09-20T12:00:00.000+08:00"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+08:00$", out)
    assert not out.endswith("Z")


def test_batch_id_matches_flush_mjs_shape() -> None:
    bid = make_batch_id(NOW)
    assert re.match(r"^v1-[0-9a-z]+-[0-9a-f]{6}$", bid)
    assert make_batch_id(NOW) != bid  # random suffix


def test_skill_slash_is_namespaced_with_colon() -> None:
    call = parse_audit_line(json.dumps(skill_row(ts(40), skill="creative/internal-tool-html-prototype")))
    record = build_record(call, None)
    assert record["skill"] == "creative:internal-tool-html-prototype"
    assert normalize_skill_name("lark-base") == "lark-base"


def test_skill_outside_charset_is_skipped_and_dead_lettered_locally(tmp_path: Path) -> None:
    with pytest.raises(InvalidSkillName):
        normalize_skill_name("x" * 70)
    with pytest.raises(InvalidSkillName):
        normalize_skill_name("bad name with spaces")
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(50), message_id=1, skill="y" * 70), skill_row(ts(50), message_id=2)])
    hub = FakeHub()
    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"),
                        http_post=hub)
    assert report["built"]["skipped_invalid_skill"] == 1
    assert report["built"]["records"] == 1
    dead = [json.loads(l) for l in (state / "dead-letter.ndjson").read_text().splitlines()]
    assert [d["reason"] for d in dead] == ["local:invalid_skill_charset"]
    assert all("y" * 70 not in json.dumps(body) for body in hub.bodies)


def test_expert_outside_charset_is_omitted() -> None:
    call = parse_audit_line(json.dumps(skill_row(ts(40))))
    ok = RunTerminal(profile="profile_a", platform="webui", at=ts(38), terminal_status="completed",
                     expert_id="kep-trevi_resource-delivery")
    bad_dot = RunTerminal(profile="profile_a", platform="webui", at=ts(38), terminal_status="completed",
                          expert_id="kep.trevi")
    bad_long = RunTerminal(profile="profile_a", platform="webui", at=ts(38), terminal_status="completed",
                           expert_id="e" * 70)
    assert build_record(call, ok)["expert"] == "kep-trevi_resource-delivery"
    assert "expert" not in build_record(call, bad_dot)
    assert "expert" not in build_record(call, bad_long)


def test_first_run_on_a_large_file_starts_at_the_backfill_cutoff_not_a_byte_budget(tmp_path: Path) -> None:
    """Finding 4: the start offset is chosen by time; a window larger than one read budget is covered."""
    audit = tmp_path / "audit.jsonl"
    rows = [json.dumps(skill_row(ts(1000 - i), message_id=i, skill="pad-" + "x" * 30)) for i in range(200)]
    audit.write_text("\n".join(rows) + "\n", encoding="utf-8")
    size = audit.stat().st_size
    start = NOW - timedelta(minutes=850)  # rows 150..199 are inside the window
    lines, cursor, restarted, caught_up = read_new_audit_lines(audit, {}, max_bytes=size // 10, backfill_start=start)
    assert not restarted
    first = json.loads(lines[0])
    assert parse_ts(first["@timestamp"]) <= start          # started at or before the cutoff ...
    assert json.loads(lines[-1])["message_id"] < 199        # ... and did not jump to the tail
    assert not caught_up
    seen = [json.loads(l)["message_id"] for l in lines]
    while not caught_up:
        more, cursor, _, caught_up = read_new_audit_lines(audit, cursor, max_bytes=size // 10, backfill_start=start)
        seen += [json.loads(l)["message_id"] for l in more]
    assert [m for m in seen if m >= 150] == list(range(150, 200))  # the whole window, in order, once
    assert cursor["offset"] == size


# ── review round 1 (codex, 2026-09-20) regression guards ─────────────────


def _export(tmp_path: Path, audit: Path, hub: FakeHub, **kw):
    return run_export(audit_path=audit, state_dir=tmp_path / "state", now=NOW, credentials=("t", "sunke"),
                      http_post=hub, **kw)


def test_finding1_cursor_commits_after_the_facts_so_a_crash_loses_nothing(tmp_path: Path, monkeypatch) -> None:
    from hermes_multitenancy.analytics import kep_telemetry_export as mod

    audit = tmp_path / "audit.jsonl"
    write_audit(audit, [skill_row(ts(90), message_id=1), skill_row(ts(80), message_id=2)])
    hub = FakeHub()

    def boom(self, cursor):  # crash right when the cursor would be committed
        raise OSError("disk full")

    monkeypatch.setattr(mod.ExportState, "save_cursor", boom)
    with pytest.raises(OSError):
        _export(tmp_path, audit, hub)
    monkeypatch.undo()
    assert hub.bodies == []  # crashed before the network leg
    state_outbox = json.loads((tmp_path / "state" / "outbox.json").read_text())
    assert len(state_outbox) == 2  # facts were already durable
    assert not (tmp_path / "state" / "cursor.json").exists()

    report = _export(tmp_path, audit, hub)  # re-reads the same lines: dedup, no duplicates, nothing lost
    assert report["upload"]["accepted"] == 2
    sent = [r["run_id"] for b in hub.bodies for r in b["runs"]]
    assert len(sent) == len(set(sent)) == 2


def test_finding2_second_exporter_on_the_same_state_dir_exits_without_touching_it(tmp_path: Path) -> None:
    import fcntl

    audit = tmp_path / "audit.jsonl"
    write_audit(audit, [skill_row(ts(90), message_id=1)])
    state = tmp_path / "state"
    state.mkdir()
    holder = (state / "lock").open("a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        hub = FakeHub()
        report = _export(tmp_path, audit, hub)
        assert report["upload"]["stopped"].startswith("locked:")
        assert report["upload"]["sent"] == 0 and hub.bodies == []
        assert sorted(p.name for p in state.iterdir()) == ["lock"]
    finally:
        holder.close()
    report = _export(tmp_path, audit, FakeHub())  # lock released → normal pass
    assert report["upload"]["accepted"] == 1


@pytest.mark.parametrize("leaky", [
    "/home/hermes/private-skill", "/Users/dev/skills/a", "~/skills/a", "../secret", "a/../b",
    "C:\\Users\\kite\\s", "ns//x", "\\\\share\\s",
])
def test_finding3_path_like_skill_names_are_refused_before_normalisation(leaky: str) -> None:
    with pytest.raises(InvalidSkillName):
        normalize_skill_name(leaky)


def test_finding3_path_like_skill_never_reaches_the_hub_and_is_quarantined_locally(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    write_audit(audit, [skill_row(ts(90), message_id=1, skill="/home/hermes/private-skill"),
                        skill_row(ts(89), message_id=2, skill="ns/skill")])
    hub = FakeHub()
    report = _export(tmp_path, audit, hub)
    assert report["built"]["skipped_invalid_skill"] == 1
    posted = json.dumps(hub.bodies, ensure_ascii=False)
    assert "home" not in posted and "private-skill" not in posted
    assert [r["skill"] for b in hub.bodies for r in b["runs"]] == ["ns:skill"]
    dead = status_report(tmp_path / "state")["dead_letter_by_reason"]
    assert dead == {"local": 1}


def test_finding4_window_larger_than_one_read_budget_settles_completely(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    rows = [skill_row(ts(600 - i), message_id=i, skill="pad-" + "x" * 30) for i in range(100)]
    write_audit(audit, rows)
    budget = audit.stat().st_size // 5
    hub = FakeHub()
    total = 0
    for _ in range(12):
        report = _export(tmp_path, audit, hub, max_read_bytes=budget)
        total += report["upload"]["accepted"]
        if report["read"]["caught_up"] and report["built"]["still_pending"] == 0:
            break
    assert total == 100
    assert status_report(tmp_path / "state")["confirmed"] == 100


def test_finding5_terminal_in_the_next_chunk_still_resolves_the_call(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    filler = [terminal_row(ts(500 - i), profile="other", expert_id=None) for i in range(40)]
    rows = filler + [skill_row(ts(100), message_id=1)] + [terminal_row(ts(98), expert_id="resource-delivery")] \
        + [terminal_row(ts(60 - i), profile="other", expert_id=None) for i in range(40)]
    write_audit(audit, rows)
    # budget: enough to read the call but not the terminal two lines later
    text = audit.read_text().splitlines()
    budget = sum(len(l) + 1 for l in text[:41]) + 5
    hub = FakeHub()
    for _ in range(20):
        report = _export(tmp_path, audit, hub, max_read_bytes=budget)
        if report["read"]["caught_up"]:
            break
    runs = [r for b in hub.bodies for r in b["runs"]]
    assert len(runs) == 1
    assert runs[0]["expert"] == "resource-delivery"
    assert (runs[0]["boundary_status"], runs[0]["boundary_source"], runs[0]["boundary_confidence"]) == BOUNDARY_TURN_CLOSED


def test_finding6_a_rejected_run_id_is_never_resent_or_dead_lettered_twice(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    row = skill_row(ts(90), message_id=1)
    write_audit(audit, [row])
    hub = FakeHub([(200, {"accepted": 0, "rejected": 1, "errors": [{"index": 0, "reason": "invalid_enum:client"}]})])
    _export(tmp_path, audit, hub)
    # audit replays the same row (duplicate mirror write) and the file rotates
    write_audit(audit, [row, row])
    import os
    os.utime(audit)
    report = _export(tmp_path, audit, FakeHub())
    assert report["built"]["already_rejected"] >= 1
    status = status_report(tmp_path / "state")
    assert status["dead_letter"] == 1 and status["confirmed"] == 0 and status["outbox"] == 0


def test_finding7_state_dir_is_bound_to_one_env(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    write_audit(audit, [skill_row(ts(90), message_id=1)])
    pre = FakeHub()
    report = _export(tmp_path, audit, pre, env="pre")
    assert report["upload"]["accepted"] == 1 and "pre.example.com" in pre.urls[0]
    online = FakeHub()
    report = _export(tmp_path, audit, online, env="online")
    assert report["upload"]["stopped"].startswith("env_mismatch")
    assert online.bodies == [] and report["read"]["lines"] == 0
    assert status_report(tmp_path / "state")["env"] == "pre"
    # a separate state dir for online processes production normally
    report = run_export(audit_path=audit, state_dir=tmp_path / "state-online", now=NOW, credentials=("t", "sunke"),
                        http_post=online, env="online")
    assert report["upload"]["accepted"] == 1


def test_finding8_an_out_of_range_timestamp_is_skipped_and_the_cursor_moves_on(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    poison = skill_row("9999-12-31T23:59:00+08:00", message_id=1)
    poison_term = terminal_row("9999-12-31T23:59:00+08:00")
    write_audit(audit, [poison, poison_term, skill_row(ts(90), message_id=2)])
    hub = FakeHub()
    report = _export(tmp_path, audit, hub)
    assert report["upload"]["accepted"] == 1
    assert report["read"]["skill_calls"] == 1 and report["read"]["terminals"] == 0
    assert status_report(tmp_path / "state")["cursor"]["offset"] == audit.stat().st_size
    assert parse_audit_line(json.dumps(poison)) is None


def test_gateway_envelope_401_pauses_for_auth_and_other_codes_retry_later() -> None:
    batch = _batch(2)
    out = settle_batch(batch, 200, {"ok": False, "data": "用户身份验证失败，请重新登录", "errorCode": 401, "env": "online"})
    assert out["action"] == "pause_auth" and out["reason"] == "auth_required" and len(out["retry"]) == 2
    out = settle_batch(batch, 200, {"ok": False, "errorCode": 50000, "data": "内部错误"})
    assert out["action"] == "retry_later" and out["reason"] == "gateway_error_50000"
    wrapped = settle_batch(batch, 200, {"ok": True, "data": {"accepted": 2, "rejected": 0, "errors": []}})
    assert len(wrapped["confirmed"]) == 2


def test_read_credentials_refuses_an_expired_jwt_before_any_post(tmp_path: Path) -> None:
    import base64
    from hermes_multitenancy.analytics.kep_telemetry_export import AuthUnavailable, jwt_expiry, read_credentials

    def jwt(exp: int) -> str:
        seg = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()  # noqa: E731
        return f"{seg({'alg': 'HS256'})}.{seg({'sub': 'sunke', 'exp': exp})}.sig"

    expired = jwt(int(NOW.timestamp()) - 60)
    fresh = jwt(int(NOW.timestamp()) + 3600)
    assert jwt_expiry(expired) < NOW < jwt_expiry(fresh)
    assert jwt_expiry("opaque-token") is None
    fake = tmp_path / "kep-auth"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\nsunke\\n' '{expired}'\n")
    fake.chmod(0o755)
    with pytest.raises(AuthUnavailable, match="expired"):
        read_credentials("online", str(fake), now=NOW)
    fake.write_text(f"#!/bin/sh\nprintf '%s\\nsunke\\n' '{fresh}'\n")
    assert read_credentials("online", str(fake), now=NOW) == (fresh, "sunke")


# ── actor dimension: X-Operator = Hermes profile (sunke 2026-09-20 「打log 就打 profile 的即可」) ──


def test_upload_batches_are_split_per_profile_with_x_operator_set_to_the_profile(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    write_audit(audit, [
        skill_row(ts(90), message_id=1, profile="sunqi"),
        skill_row(ts(89), message_id=2, profile="zhouba", session="s2"),
        skill_row(ts(88), message_id=3, profile="sunqi", session="s3"),
    ])
    hub = FakeHub()
    report = run_export(audit_path=audit, state_dir=tmp_path / "state", now=NOW, credentials=("t", "sunke"),
                        http_post=hub)
    assert report["upload"]["batches"] == 2 and report["upload"]["operators"] == 2
    assert report["upload"]["accepted"] == 3
    by_operator = {h["X-Operator"]: len(b["runs"]) for h, b in zip(hub.headers, hub.bodies)}
    assert by_operator == {"sunqi": 2, "zhouba": 1}
    assert all(h["Authorization"] == "Bearer t" for h in hub.headers)
    # the record body itself still carries no profile
    assert "sunqi" not in json.dumps([b["runs"] for b in hub.bodies])


def test_upload_falls_back_to_the_service_operator_without_a_profile_mapping(tmp_path: Path) -> None:
    from hermes_multitenancy.analytics.kep_telemetry_export import plan_batches

    recs = [{"run_id": "a"}, {"run_id": "b"}, {"run_id": "c"}]
    plan, fallbacks = plan_batches(recs, {"a": "wujiu", "c": "  "}, "sunke", batch_size=100)
    assert plan == [("sunke", [{"run_id": "b"}, {"run_id": "c"}]), ("wujiu", [{"run_id": "a"}])]
    assert fallbacks == 2
    # batch_size still applies inside one operator
    many = [{"run_id": str(i)} for i in range(5)]
    plan, _ = plan_batches(many, {str(i): "hudi" for i in range(5)}, "sunke", batch_size=2)
    assert [len(b) for _, b in plan] == [2, 2, 1]


def test_upload_stop_keeps_later_operator_batches_queued(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    write_audit(audit, [skill_row(ts(90), message_id=1, profile="aaa"), skill_row(ts(89), message_id=2, profile="bbb", session="s2")])
    hub = FakeHub([(503, None)])
    report = run_export(audit_path=audit, state_dir=tmp_path / "state", now=NOW, credentials=("t", "sunke"),
                        http_post=hub)
    assert report["upload"]["stopped"] == "http_503" and report["upload"]["queued"] == 2
    assert len(hub.bodies) == 1 and hub.headers[0]["X-Operator"] == "aaa"
    report = run_export(audit_path=audit, state_dir=tmp_path / "state", now=NOW, credentials=("t", "sunke"),
                        http_post=FakeHub())
    assert report["upload"]["accepted"] == 2 and report["upload"]["operators"] == 2


def test_review_finding1_non_header_safe_profile_rides_under_the_service_operator_and_blocks_nothing(tmp_path: Path) -> None:
    """A Chinese / CRLF profile cannot be an HTTP header; the record still uploads, attributed to the service operator."""
    from urllib import request as urlrequest

    audit = tmp_path / "audit.jsonl"
    write_audit(audit, [
        skill_row(ts(90), message_id=1, profile="张三"),
        skill_row(ts(89), message_id=2, profile="evil\r\nX-Injected: 1", session="s2"),
        skill_row(ts(88), message_id=3, profile="wujiu", session="s3"),
    ])
    hub = FakeHub()
    report = run_export(audit_path=audit, state_dir=tmp_path / "state", now=NOW, credentials=("t", "sunke"),
                        http_post=hub)
    assert report["upload"]["accepted"] == 3 and report["upload"]["stopped"] is None
    assert report["upload"]["operator_fallback"] == 2 and report["upload"]["operators"] == 2
    assert sorted(h["X-Operator"] for h in hub.headers) == ["sunke", "wujiu"]
    for h, b in zip(hub.headers, hub.bodies):  # every header set really serialises through urllib
        urlrequest.Request("https://example.invalid/x", data=json.dumps(b).encode(), headers=h, method="POST")


def test_review_finding2_crash_between_profile_map_and_outbox_keeps_attribution(tmp_path: Path, monkeypatch) -> None:
    from hermes_multitenancy.analytics import kep_telemetry_export as mod

    audit = tmp_path / "audit.jsonl"
    write_audit(audit, [skill_row(ts(90), message_id=1, profile="sunqi"),
                        skill_row(ts(89), message_id=2, profile="zhouba", session="s2")])
    hub = FakeHub()

    def boom(self, records):  # crash right when the outbox would be committed (profile map already durable)
        raise OSError("disk full")

    monkeypatch.setattr(mod.ExportState, "save_outbox", boom)
    with pytest.raises(OSError):
        run_export(audit_path=audit, state_dir=tmp_path / "state", now=NOW, credentials=("t", "sunke"), http_post=hub)
    monkeypatch.undo()
    assert (tmp_path / "state" / "profile-map.json").exists() and not (tmp_path / "state" / "outbox.json").exists()

    report = run_export(audit_path=audit, state_dir=tmp_path / "state", now=NOW, credentials=("t", "sunke"), http_post=hub)
    assert report["upload"]["accepted"] == 2 and report["upload"]["operators"] == 2
    assert sorted(h["X-Operator"] for h in hub.headers) == ["sunqi", "zhouba"]
    sent = [r["run_id"] for b in hub.bodies for r in b["runs"]]
    assert len(sent) == len(set(sent)) == 2


# ── local diagnostic log ──────────────────────────────────────────────────
#
# What this section pins: a settled record has to leave local evidence a telemetry
# developer can read — payload as sent, batch id, the Hub's own answer, the verdict,
# and where the audit row it came from lives. And the log must never be able to
# change what the exporter uploads or settles.


from hermes_multitenancy.analytics import kep_telemetry_export as mod  # noqa: E402


def diag_rows(state: Path, day: str = "20260920") -> list[dict]:
    path = state / "diagnostics" / f"export-{day}.ndjson"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_diagnostics_logs_request_response_and_verdict_for_an_accepted_record(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    hub = FakeHub()

    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    assert report["upload"]["accepted"] == 1
    rows = diag_rows(state)
    attempts = [r for r in rows if r["kind"] == "upload_attempt"]
    assert len(attempts) == 1
    assert report["diagnostics"]["rows"] == 2  # the attempt + the pass summary
    row = attempts[0]
    # request: the payload exactly as posted, not a hash of it
    assert row["request"] == hub.bodies[0]["runs"][0]
    assert row["request"]["run_id"] == row["run_id"]
    assert row["batch_id"] == hub.bodies[0]["batch_id"]
    assert row["sent_at"] == hub.bodies[0]["sent_at"]
    assert row["content_hash"] == mod.content_hash(row["request"])
    assert row["attempt"] == 1 and row["at_epoch_ms"] == int(NOW.timestamp() * 1000)
    # response: the Hub's own answer
    assert row["response"]["status"] == 200
    assert json.loads(row["response"]["body"]) == {"accepted": 1, "rejected": 0, "errors": []}
    assert row["response"]["truncated"] is False
    # verdict + attribution + where the audit row is
    assert row["verdict"] == {"outcome": "confirmed", "reason": None}
    assert row["profile"] == "profile_a"
    assert row["operator"] == "profile_a"
    assert row["url"].endswith("/api/v1/skill-runs")
    assert row["source_inode"] == audit.stat().st_ino and row["source_offset"] == 0
    assert row["source_available"] is True and row["source_reason"] is None
    assert row["observed_at"] == ts(60)
    assert row["at"].endswith("+08:00")
    # the pass itself is recorded too, so a round that sends nothing still leaves a trace
    assert [r["kind"] for r in rows].count("pass_summary") == 1


def test_diagnostics_source_offset_points_at_the_exact_audit_line(tmp_path: Path) -> None:
    """The one link that used to be missing: run_id is a one-way hash, so without a
    byte offset you had to rehash a 400 MB audit to find the row again."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [terminal_row(ts(70)), skill_row(ts(60), message_id=7), terminal_row(ts(59))])
    raw = audit.read_bytes()
    expected = [line for line in raw.split(b"\n") if b'"message_id": 7' in line][0]

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    offset = row["source_offset"]
    assert raw[offset:offset + len(expected)] == expected
    assert json.loads(expected.decode("utf-8"))["message_id"] == 7


def test_diagnostics_dead_letter_row_carries_the_servers_own_reason(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    rejection = {"accepted": 0, "rejected": 1,
                 "errors": [{"index": 0, "reason": 'invalid_enum:client: "hermes"'}]}
    hub = FakeHub([(200, rejection)])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert row["verdict"] == {"outcome": "dead_letter", "reason": 'invalid_enum:client: "hermes"'}
    # the four-field dead-letter line stays the settlement record; the log adds the evidence
    dead = [json.loads(line) for line in (state / "dead-letter.ndjson").read_text(encoding="utf-8").splitlines()]
    assert dead[0]["reason"] == row["verdict"]["reason"] and dead[0]["run_id"] == row["run_id"]
    assert json.loads(row["response"]["body"]) == rejection
    assert row["response"]["error"] == rejection["errors"][0], "this record's own error entry"
    assert row["request"]["client"] == CLIENT and row["request"]["skill"] == "lark-base"
    assert row["class"] == "repairable" and row["reason"] == 'invalid_enum:client: "hermes"' 


def test_diagnostics_records_a_network_retry_with_a_null_status(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    hub = FakeHub([(None, None)])

    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert row["verdict"] == {"outcome": "retry", "reason": "network_error"}
    assert row["response"] == {"status": None, "body": "", "truncated": False}
    # the record is still queued — the log did not touch the settlement
    assert report["upload"]["queued"] == 1
    assert len(json.loads((state / "outbox.json").read_text(encoding="utf-8"))) == 1


def test_diagnostics_local_reject_is_logged_even_though_the_hub_never_sees_it(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1, skill="/home/hermes/secret-skill"), terminal_row(ts(59))])
    hub = FakeHub()

    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    assert report["built"]["skipped_invalid_skill"] == 1 and hub.bodies == []
    row = [r for r in diag_rows(state) if r["kind"] == "local_reject"][0]
    assert row["verdict"] == {"outcome": "dead_letter", "reason": "local:invalid_skill_charset"}
    assert row["request"] is None and row["response"] is None
    assert row["source_offset"] == 0 and row["source_available"] is True
    # the refused name came from the audit and is path-shaped — it is flagged, not echoed
    assert row["skill"] is None and row["skill_withheld"] is True
    assert "/home/" not in (state / "diagnostics" / "export-20260920.ndjson").read_text(encoding="utf-8")


def test_diagnostics_truncates_a_long_response_body_and_says_so(tmp_path: Path, monkeypatch) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    monkeypatch.setattr(mod, "DIAGNOSTICS_BODY_CAP", 64)
    hub = FakeHub([(200, {"accepted": 1, "rejected": 0, "errors": [], "note": "x" * 500})])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert row["response"]["truncated"] is True
    assert len(row["response"]["body"].encode("utf-8")) <= 64
    assert row["verdict"]["outcome"] == "confirmed"


def test_diagnostics_dry_run_writes_no_log_at_all(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    hub = FakeHub()

    report = run_export(audit_path=audit, state_dir=state, now=NOW, dry_run=True,
                        credentials=("tok", "sunke"), http_post=hub)

    assert report["upload"]["would_send"] == 1
    assert not (state / "diagnostics").exists()
    assert report["diagnostics"] == {"path": None, "rows": 0, "dropped": 0, "errors": 0,
                                     "pruned": [], "stopped": None}


def test_diagnostics_enforces_the_per_day_size_cap_and_keeps_uploading(tmp_path: Path, monkeypatch) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    day_file = state / "diagnostics" / "export-20260920.ndjson"
    day_file.parent.mkdir(parents=True)
    day_file.write_text("x" * 900 + "\n", encoding="utf-8")
    monkeypatch.setattr(mod, "DIAGNOSTICS_MAX_DAY_BYTES", 1000)

    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    # the upload is untouched by the cap
    assert report["upload"]["accepted"] == 1
    assert report["diagnostics"]["stopped"] == "size_cap" and report["diagnostics"]["dropped"] >= 1
    assert report["diagnostics"]["rows"] == 0
    # the file itself admits it stopped — exactly one marker, not one per dropped row
    written = day_file.read_text(encoding="utf-8").splitlines()
    markers = [json.loads(line) for line in written if line.startswith("{")]
    assert len(markers) == 1 and markers[0]["kind"] == "truncated"
    assert markers[0]["reason"] == "size_cap" and markers[0]["max_day_bytes"] == 1000


def test_diagnostics_prunes_day_files_past_the_retention_window(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    diag_dir = state / "diagnostics"
    diag_dir.mkdir(parents=True)
    stale = diag_dir / "export-20260905.ndjson"     # 15 days before NOW
    edge = diag_dir / "export-20260907.ndjson"      # 13 days before NOW
    junk = diag_dir / "export-notadate.ndjson"
    for f in (stale, edge, junk):
        f.write_text('{"kind":"old"}\n', encoding="utf-8")

    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    assert not stale.exists(), "a day file older than 14 days must be deleted by the pass itself"
    assert edge.exists() and junk.exists(), "inside the window, or unparsable, is left alone"
    assert report["diagnostics"]["pruned"] == ["export-20260905.ndjson"]


def test_diagnostics_write_failure_never_breaks_the_upload(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    state.mkdir(parents=True)
    (state / "diagnostics").write_text("not a directory", encoding="utf-8")  # every open() will fail

    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    assert report["upload"]["accepted"] == 1
    assert report["diagnostics"]["errors"] >= 1 and report["diagnostics"]["rows"] == 0
    assert len(json.loads((state / "ledger.json").read_text(encoding="utf-8"))["confirmed"]) == 1


def test_diagnostics_logs_a_pass_that_sent_nothing_because_auth_failed(tmp_path: Path, monkeypatch) -> None:
    """last-run.json is overwritten every ten minutes; without this row an auth stop
    leaves nothing behind to look back at."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])

    def no_auth(env, kep_auth_bin="kep-auth", *, now=None):
        raise mod.AuthUnavailable("kep-auth token expired at 2026-09-20T11:00:00+08:00")

    monkeypatch.setattr(mod, "read_credentials", no_auth)
    hub = FakeHub()
    report = run_export(audit_path=audit, state_dir=state, now=NOW, http_post=hub)

    assert hub.bodies == [] and report["upload"]["stopped"].startswith("auth_unavailable")
    rows = diag_rows(state)
    assert [r["kind"] for r in rows] == ["pass_summary"]
    assert rows[0]["report"]["upload"]["stopped"].startswith("auth_unavailable")
    assert rows[0]["report"]["built"]["records"] == 1
    # the queue and its locator survive for the next pass
    assert len(json.loads((state / "outbox.json").read_text(encoding="utf-8"))) == 1
    assert len(json.loads((state / "locator-map.json").read_text(encoding="utf-8"))) == 1


def test_diagnostics_never_writes_the_bearer_token_or_a_home_path(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])

    run_export(audit_path=audit, state_dir=state, now=NOW,
               credentials=("super-secret-jwt", "sunke"), http_post=FakeHub())

    text = (state / "diagnostics" / "export-20260920.ndjson").read_text(encoding="utf-8")
    assert "super-secret-jwt" not in text and "Bearer" not in text
    assert "Authorization" not in text
    assert "/home/" not in text and "/Users/" not in text
    payloads = [r["request"] for r in diag_rows(state) if r["kind"] == "upload_attempt"]
    assert "s1" not in json.dumps(payloads), "no session id in the payload"
    assert "\"1\"" not in json.dumps(payloads), "no message id in the payload"


def test_diagnostics_locator_sidecar_is_pruned_to_the_queue(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    # settled ⇒ the locator is not kept forever (unlike profile-map, which is attribution)
    assert json.loads((state / "locator-map.json").read_text(encoding="utf-8")) == {}


def test_diagnostics_keeps_the_locator_while_a_record_is_still_queued(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"),
               http_post=FakeHub([(None, None)]))

    locators = json.loads((state / "locator-map.json").read_text(encoding="utf-8"))
    rid = run_id_for("profile_a", "s1", "1")
    assert list(locators) == [rid]
    assert locators[rid]["offset"] == 0 and locators[rid]["inode"] == audit.stat().st_ino


# ── review round 1 (codex, gpt-6-astra): one regression test per finding ──────


def test_review1_locator_sidecar_io_failure_never_blocks_the_upload(tmp_path: Path, monkeypatch) -> None:
    """finding locator-io-blocks-upload: the sidecar is diagnostic; it must not be able
    to abort a pass before the POST."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])

    def boom(self, mapping):
        raise PermissionError("locator sidecar not writable")

    monkeypatch.setattr(mod.ExportState, "save_locator_map", boom)
    hub = FakeHub()
    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    assert report["upload"]["accepted"] == 1 and len(hub.bodies) == 1
    assert len(json.loads((state / "ledger.json").read_text(encoding="utf-8"))["confirmed"]) == 1
    assert report["diagnostics"]["errors"] >= 1, "the failure is disclosed, not swallowed"
    # the row is still written, just without a usable locator
    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert row["source_available"] is True  # still known in memory for this pass


def test_review2_unencodable_response_body_does_not_abort_the_pass(tmp_path: Path) -> None:
    """finding response-formatting-escapes-error-boundary: a lone surrogate in the Hub's
    JSON must not throw between the POST and save_ledger — that would re-upload."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    poisoned = json.loads('{"accepted": 1, "rejected": 0, "errors": [], "note": "\\ud800"}')
    hub = FakeHub([(200, poisoned)])

    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    assert report["upload"]["accepted"] == 1
    assert len(json.loads((state / "ledger.json").read_text(encoding="utf-8"))["confirmed"]) == 1
    assert json.loads((state / "outbox.json").read_text(encoding="utf-8")) == []
    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert row["verdict"]["outcome"] == "confirmed" and row["response"]["status"] == 200


def test_review3_pass_summary_carries_no_session_content_and_no_token(tmp_path: Path) -> None:
    """finding forbidden-content-written-verbatim, narrowed: the host's own paths are
    allowed in a 0600 local log (they are already in last-run.json); credentials,
    conversation ids and audit-supplied names that failed the charset are not."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1, skill="../../etc/passwd"), terminal_row(ts(59))])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok-abc", "sunke"), http_post=FakeHub())

    text = (state / "diagnostics" / "export-20260920.ndjson").read_text(encoding="utf-8")
    assert "tok-abc" not in text and "passwd" not in text and "etc" not in text
    rows = diag_rows(state)
    assert [r for r in rows if r["kind"] == "local_reject"][0]["skill_withheld"] is True
    summary = [r for r in rows if r["kind"] == "pass_summary"][0]
    assert "diagnostics" not in summary["report"], "the report's own counters cannot be logged inside themselves"


def test_review4_long_non_json_response_is_capped_in_bytes_and_flagged(tmp_path: Path, monkeypatch) -> None:
    """finding response-evidence-lost-before-logging: truncation happens once, where the
    real length is known, and the flag tells the truth."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    monkeypatch.setattr(mod, "DIAGNOSTICS_BODY_CAP", 32)
    # a gateway HTML error page: not JSON, far longer than the cap, multi-byte
    hub = FakeHub([(502, "网关炸了 " * 50)])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert row["response"]["status"] == 502 and row["response"]["truncated"] is True
    assert len(row["response"]["body"].encode("utf-8")) <= 32
    assert row["response"]["body"].startswith("网关")  # cut on a character boundary, not mid-rune
    assert row["verdict"]["outcome"] == "retry"


def test_review5_cap_marker_is_written_once_per_day_not_once_per_pass(tmp_path: Path, monkeypatch) -> None:
    """finding cap-marker-resets-each-pass: past the cap the day file must stop growing,
    including across passes and restarts."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    day_file = state / "diagnostics" / "export-20260920.ndjson"
    day_file.parent.mkdir(parents=True)
    day_file.write_text("x" * 900 + "\n", encoding="utf-8")
    monkeypatch.setattr(mod, "DIAGNOSTICS_MAX_DAY_BYTES", 1000)

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())
    after_first = day_file.read_bytes()
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59)),
                        skill_row(ts(50), message_id=2), terminal_row(ts(49))])
    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    assert report["upload"]["accepted"] == 1  # the second call still uploads
    assert day_file.read_bytes() == after_first, "a capped day file must not grow again"
    markers = [json.loads(l) for l in day_file.read_text(encoding="utf-8").splitlines() if l.startswith("{")]
    assert len(markers) == 1 and markers[0]["kind"] == "truncated"
    assert report["diagnostics"]["stopped"] == "size_cap" and report["diagnostics"]["rows"] == 0


def test_review6_close_failure_is_reported_in_the_pass_diagnostics(tmp_path: Path, monkeypatch) -> None:
    """finding diagnostic-report-finalized-too-early: buffered-and-lost must not read as
    written, so the report is taken after close()."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    real_close = mod.DiagnosticsLog.close

    def close_boom(self):
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            finally:
                self.errors += 1  # stand in for an ENOSPC surfacing at close()

    monkeypatch.setattr(mod.DiagnosticsLog, "close", close_boom)
    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())
    monkeypatch.setattr(mod.DiagnosticsLog, "close", real_close)

    assert report["upload"]["accepted"] == 1
    assert report["diagnostics"]["errors"] == 1, "a close failure has to reach last-run.json"


def test_review7_a_record_queued_before_the_locator_existed_is_flagged_not_hunted(tmp_path: Path) -> None:
    """finding legacy-queue-loses-source-locator: no rehash-the-whole-audit recovery
    (that is the scan this field exists to avoid) — an unknown source says so."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    state.mkdir(parents=True)
    # an outbox left behind by the previous version: payload only, no locator sidecar
    legacy = build_record(SkillCall("profile_a", "webui", "s9", "9", "lark-base", ts(200)), None)
    (state / "outbox.json").write_text(json.dumps([legacy]), encoding="utf-8")
    (state / "env.json").write_text(json.dumps({"env": "online"}), encoding="utf-8")

    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    assert report["upload"]["accepted"] == 2
    rows = {r["run_id"]: r for r in diag_rows(state) if r["kind"] == "upload_attempt"}
    old = rows[legacy["run_id"]]
    assert old["source_available"] is False and old["source_reason"] == "queued_before_locator"
    assert old["source_offset"] is None and old["source_inode"] is None
    fresh = rows[run_id_for("profile_a", "s1", "1")]
    assert fresh["source_available"] is True and fresh["source_offset"] == 0


def test_review8_request_envelope_exposes_payload_batch_id_and_sent_at(tmp_path: Path) -> None:
    """finding request-schema-misses-payload: the SPEC's acceptance line reads
    request.payload.run_id, so that is the shape on disk."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    hub = FakeHub()

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    # the pinned hand-off schema: these keys are read by jq without knowing our code
    for key in ("at", "run_id", "skill", "operator", "batch_id", "request", "response",
                "verdict", "source_offset", "source_inode", "reason", "class",
                "content_hash", "attempt", "first_at" if False else "at_epoch_ms"):
        assert key in row, key
    assert row["request"] == hub.bodies[0]["runs"][0]
    assert row["batch_id"] == hub.bodies[0]["batch_id"]
    assert row["sent_at"] == hub.bodies[0]["sent_at"]
    assert "Authorization" not in json.dumps(row) and row["operator"] == "profile_a"


# ── graded rejections and silent automatic retry ─────────────────────────────
#
# The real defect behind the 500 records that sat in dead-letter for two days: every
# server rejection landed in one terminal state, so a contract problem the Hub later
# fixed could never be resent without someone deleting a file by hand.


def dead_rows(state: Path) -> list[dict]:
    return [json.loads(line) for line in
            (state / "dead-letter.ndjson").read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("reason,expected", [
    ('invalid_enum:client: "hermes"', "repairable"),   # the one that actually happened
    ("internal_error", "retryable"),
    ("rate_limited:slow down", "retryable"),
    ("retention_exceeded", "permanent"),
    ("operator_forbidden", "permanent"),
    ("revision_conflict:rev 2", "conflict"),
    ("local:invalid_skill_charset", "permanent"),
    ("something_nobody_has_seen", "repairable"),       # unknown is never silently permanent
])
def test_reject_reasons_are_graded_like_kep_telemetry(reason: str, expected: str) -> None:
    assert mod.classify_reject_reason(reason) == expected


def test_retryable_rejection_is_resent_on_the_next_pass_without_human_help(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    reject = FakeHub([(200, {"accepted": 0, "rejected": 1,
                             "errors": [{"index": 0, "reason": 'invalid_enum:client: "hermes"'}]})])

    first = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=reject)
    assert first["upload"]["accepted"] == 0
    row = dead_rows(state)[0]
    # the dead-letter row now carries kep's grading fields plus the payload to resend
    assert row["class"] == "repairable" and row["attempt"] == 1
    assert row["first_at"] == row["at"] and row["payload"]["run_id"] == row["run_id"]
    assert row["content_hash"] == mod.content_hash(row["payload"])

    # one backoff later (10 min for attempt 1) the exporter resends it by itself
    accept = FakeHub()
    later = run_export(audit_path=audit, state_dir=state, now=NOW + timedelta(minutes=11),
                       credentials=("tok", "sunke"), http_post=accept)

    assert later["retry"]["requeued"] == 1
    assert later["upload"]["accepted"] == 1
    assert accept.bodies[0]["runs"][0]["run_id"] == row["run_id"]
    assert list(json.loads((state / "ledger.json").read_text(encoding="utf-8"))["confirmed"]) == [row["run_id"]]


def test_retry_waits_for_the_backoff_instead_of_hammering_every_tick(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    reject = FakeHub([(200, {"accepted": 0, "rejected": 1,
                             "errors": [{"index": 0, "reason": "internal_error"}]})])
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=reject)

    hub = FakeHub()
    soon = run_export(audit_path=audit, state_dir=state, now=NOW + timedelta(minutes=3),
                      credentials=("tok", "sunke"), http_post=hub)

    assert soon["retry"] == {"requeued": 0, "rescheduled": 0, "waiting": 1, "exhausted": 0,
                             "permanent": 0, "legacy": 0, "corrupt": 0,
                             "by_class": {"retryable": 1}}
    assert hub.bodies == [], "nothing is resent before its backoff elapses"


def test_a_permanent_rejection_is_never_resent(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    reject = FakeHub([(200, {"accepted": 0, "rejected": 1,
                             "errors": [{"index": 0, "reason": "retention_exceeded"}]})])
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=reject)

    hub = FakeHub()
    later = run_export(audit_path=audit, state_dir=state, now=NOW + timedelta(days=2),
                       credentials=("tok", "sunke"), http_post=hub)

    assert dead_rows(state)[0]["class"] == "permanent"
    assert later["retry"]["permanent"] == 1 and later["retry"]["requeued"] == 0
    assert hub.bodies == []


def test_retry_stops_after_the_attempt_ceiling(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    state.mkdir(parents=True)
    (state / "env.json").write_text(json.dumps({"env": "online"}), encoding="utf-8")
    payload = build_record(SkillCall("profile_a", "webui", "s1", "1", "lark-base", ts(60)), None)
    (state / "dead-letter.ndjson").write_text(json.dumps({
        "run_id": payload["run_id"], "reason": "internal_error", "class": "retryable",
        "at": rfc3339(NOW - timedelta(days=1)), "first_at": rfc3339(NOW - timedelta(days=3)),
        "attempt": mod.DEAD_LETTER_MAX_ATTEMPTS, "payload": payload}) + "\n", encoding="utf-8")

    hub = FakeHub()
    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    assert report["retry"]["exhausted"] == 1 and report["retry"]["requeued"] == 0
    assert hub.bodies == [], "a record that spent its attempts rests instead of looping forever"


def test_a_pre_grading_dead_letter_row_is_counted_legacy_not_silently_retried(tmp_path: Path) -> None:
    """The 500 rows already on the production host carry no payload. Rebuilding them would
    mean rehashing a 400 MB audit, so they are disclosed as legacy, never faked as retried."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    state.mkdir(parents=True)
    (state / "env.json").write_text(json.dumps({"env": "online"}), encoding="utf-8")
    old_id = run_id_for("profile_a", "s1", "1")
    (state / "dead-letter.ndjson").write_text(json.dumps({
        "run_id": old_id, "reason": 'invalid_enum:client: "hermes"',
        "at": rfc3339(NOW - timedelta(days=2)), "skill": "lark-base"}) + "\n", encoding="utf-8")

    hub = FakeHub()
    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    assert report["retry"]["legacy"] == 1 and report["retry"]["requeued"] == 0
    assert hub.bodies == []


def test_rejected_by_class_is_reported_per_pass(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), skill_row(ts(60), message_id=2),
                        terminal_row(ts(59))])
    hub = FakeHub([(200, {"accepted": 0, "rejected": 2, "errors": [
        {"index": 0, "reason": "internal_error"},
        {"index": 1, "reason": "retention_exceeded"}]})])

    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    assert report["upload"]["rejected_by_class"] == {"retryable": 1, "permanent": 1}


# ── review round 2 (codex gpt-5.6-sol): one regression test per finding ───────


def test_review9_a_long_legal_skill_name_is_not_validated_after_truncation(tmp_path: Path) -> None:
    """finding truncated-skill-revalidated: validating call.skill[:64] would let a
    70-character name — or one whose 65th character is illegal — through as 'safe'."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    long_legal = "a" * 64 + "bbbbbb"          # 70 legal characters: too long for the gateway
    sneaky = "b" * 64 + "/etc/passwd"          # legal for 64 chars, then a path
    write_audit(audit, [skill_row(ts(60), message_id=1, skill=long_legal),
                        skill_row(ts(60), message_id=2, skill=sneaky), terminal_row(ts(59))])

    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    assert report["built"]["skipped_invalid_skill"] == 2
    rows = [r for r in diag_rows(state) if r["kind"] == "local_reject"]
    assert len(rows) == 2
    assert all(r["skill"] is None and r["skill_withheld"] is True for r in rows)
    text = (state / "diagnostics" / "export-20260920.ndjson").read_text(encoding="utf-8")
    assert "passwd" not in text and "a" * 64 not in text


def test_review10_a_full_day_file_without_a_marker_still_gets_one(tmp_path: Path, monkeypatch) -> None:
    """finding full-file-marker-suppressed: a file at the cap but carrying no marker (the
    marker write failed, or it was pre-filled) must still say why it stopped."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    day_file = state / "diagnostics" / "export-20260920.ndjson"
    day_file.parent.mkdir(parents=True)
    day_file.write_text("y" * 1200 + "\n", encoding="utf-8")   # already over the cap, no marker
    monkeypatch.setattr(mod, "DIAGNOSTICS_MAX_DAY_BYTES", 1000)

    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())
    first = day_file.read_bytes()
    run_export(audit_path=audit, state_dir=state, now=NOW + timedelta(minutes=20),
               credentials=("tok", "sunke"), http_post=FakeHub())

    markers = [json.loads(l) for l in day_file.read_text(encoding="utf-8").splitlines() if l.startswith("{")]
    assert len(markers) == 1 and markers[0]["kind"] == "truncated"
    assert report["upload"]["accepted"] == 1 and report["diagnostics"]["stopped"] == "size_cap"
    assert day_file.read_bytes() == first, "and the second pass adds nothing more"


def test_review11_response_values_survive_a_lone_surrogate_unchanged(tmp_path: Path) -> None:
    """finding unicode-evidence-replaced: escaping (ensure_ascii) preserves the value;
    encoding with errors='replace' would have rewritten it to '?' and changed the evidence."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    body = json.loads('{"accepted": 1, "rejected": 0, "errors": [], "note": "\\ud800x"}')
    hub = FakeHub([(200, body)])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert row["response"]["truncated"] is False
    assert json.loads(row["response"]["body"])["note"] == body["note"]


def test_review12_an_invalid_locator_sidecar_is_flagged_not_trusted(tmp_path: Path) -> None:
    """finding invalid-locator-trusted: dd at a bogus offset hands the reader someone
    else's audit row, which is worse than admitting we do not know."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    state.mkdir(parents=True)
    (state / "env.json").write_text(json.dumps({"env": "online"}), encoding="utf-8")
    payload = build_record(SkillCall("profile_a", "webui", "s1", "1", "lark-base", ts(60)), None)
    (state / "outbox.json").write_text(json.dumps([payload]), encoding="utf-8")
    (state / "locator-map.json").write_text(json.dumps({
        payload["run_id"]: {"inode": "not-an-inode", "offset": -5, "observed_at": "nonsense"}}), encoding="utf-8")

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    row = [r for r in diag_rows(state) if r.get("run_id") == payload["run_id"]][0]
    assert row["source_available"] is False and row["source_reason"] == "invalid_locator"
    assert row["source_offset"] is None and row["source_inode"] is None


def test_review13_an_echoed_bearer_token_is_redacted_and_a_path_profile_withheld(tmp_path: Path) -> None:
    """finding unsafe-inputs-persisted: a proxy that mirrors our headers into its error
    body is the one way the live token could reach this file."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1, profile="/home/hermes/private"),
                        terminal_row(ts(59), profile="/home/hermes/private")])
    token = "eyJhbGciOiJIUzI1NiJ9.super-secret-token-value.sig"
    hub = FakeHub([(401, f"upstream rejected: Authorization: Bearer {token}")])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=(token, "sunke"), http_post=hub)

    text = (state / "diagnostics" / "export-20260920.ndjson").read_text(encoding="utf-8")
    assert token not in text and "super-secret-token-value" not in text
    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert row["response"]["redacted"] is True and "«redacted»" in row["response"]["body"]
    # the profile is path-shaped, so it is withheld rather than echoed
    assert row["profile"] is None and row["profile_withheld"] is True
    assert "/home/" not in text


def test_review14_this_records_own_error_survives_a_truncated_batch_response(tmp_path: Path, monkeypatch) -> None:
    """CONFUSION from round 2: with 100 rejections the errors array runs past the 2 KiB
    cut, so each row also carries its own errors[] entry, projected by index."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), skill_row(ts(60), message_id=2),
                        terminal_row(ts(59))])
    monkeypatch.setattr(mod, "DIAGNOSTICS_BODY_CAP", 48)  # the body is cut well before errors[1]
    hub = FakeHub([(200, {"accepted": 0, "rejected": 2, "errors": [
        {"index": 0, "reason": "internal_error", "detail": "x" * 200},
        {"index": 1, "reason": "retention_exceeded", "detail": "y" * 200}]})])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    rows = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"]
    assert len(rows) == 2 and all(r["response"]["truncated"] is True for r in rows)
    by_reason = {r["response"]["error"]["reason"]: r for r in rows}
    assert set(by_reason) == {"internal_error", "retention_exceeded"}
    assert by_reason["internal_error"]["class"] == "retryable"
    assert by_reason["retention_exceeded"]["class"] == "permanent"


# ── --diagnose: one command, one file to hand over ───────────────────────────


def test_diagnose_writes_one_self_describing_file(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())
    out = tmp_path / "handover.ndjson"

    report = mod.diagnose(state_dir=state, since="3d", out=out, now=NOW)

    lines = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    header, rows = lines[0], lines[1:]
    assert header["kind"] == "diagnose_header" and header["client"] == "hermes"
    assert header["rows"] == len(rows) == report["rows"] == 2   # the attempt + the pass summary
    assert header["env"] == "online" and header["since"] == "3d"
    # self-describing: every field a reader will meet is documented in the header itself
    for key in ("run_id", "batch_id", "request", "response", "verdict", "source_offset", "class"):
        assert key in header["schema"]
    assert "jq" in header["notes"]
    attempt = [r for r in rows if r["kind"] == "upload_attempt"][0]
    assert attempt["request"]["run_id"] == attempt["run_id"] and attempt["response"]["status"] == 200
    assert report["gzipped"] is False and out.stat().st_mode & 0o777 == 0o600


def test_diagnose_window_and_read_only_guarantee(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())
    # a day file from outside the window
    (state / "diagnostics" / "export-20260910.ndjson").write_text(
        json.dumps({"kind": "upload_attempt", "at": rfc3339(NOW - timedelta(days=10)), "run_id": "old"}) + "\n",
        encoding="utf-8")
    before = {p.name: p.read_bytes() for p in state.iterdir() if p.is_file()}

    narrow = mod.diagnose(state_dir=state, since="3d", out=tmp_path / "a.ndjson", now=NOW)
    wide = mod.diagnose(state_dir=state, since="all", out=tmp_path / "b.ndjson", now=NOW)

    assert narrow["rows"] == 2 and narrow["skipped_outside_window"] == 1
    assert wide["rows"] == 3 and wide["skipped_outside_window"] == 0
    after = {p.name: p.read_bytes() for p in state.iterdir() if p.is_file()}
    assert before == after, "diagnose must not touch cursor, ledger, outbox or dead-letter"


def test_diagnose_gzips_a_large_bundle_and_rejects_a_bad_window(tmp_path: Path) -> None:
    import gzip as gz
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    report = mod.diagnose(state_dir=state, since="all", out=tmp_path / "big.ndjson", now=NOW, gzip_over=10)

    assert report["gzipped"] is True and report["out"].endswith(".ndjson.gz")
    with gz.open(report["out"], "rt", encoding="utf-8") as fh:
        assert json.loads(fh.readline())["kind"] == "diagnose_header"
    with pytest.raises(ValueError):
        mod.diagnose(state_dir=state, since="3 fortnights", out=tmp_path / "x.ndjson", now=NOW)


def test_cli_diagnose_is_read_only_and_exits_zero(tmp_path: Path, capsys) -> None:
    from hermes_multitenancy.analytics import cli
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())
    out = tmp_path / "bundle.ndjson"

    # the rows carry the test clock, so the window is 'all' rather than a wall-clock one
    rc = cli.main(["kep-telemetry-export", "--diagnose", "--since", "all",
                   "--state-dir", str(state), "--out", str(out)])

    assert rc == 0
    printed = capsys.readouterr().out
    assert str(out) in printed and "rows=2" in printed
    assert json.loads(out.read_text(encoding="utf-8").splitlines()[0])["kind"] == "diagnose_header"


# ── red line: no conversation text may reach the diagnostic file ─────────────
#
# The file is about to be exposed through an admin endpoint, so the guarantee has to
# hold in the file itself — an interface layer must not be what keeps user text out.


def test_redline_no_conversation_text_reaches_the_diagnostic_file(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    secret_text = "SECRET_USER_UTTERANCE_请把这段话导出来"
    rows = [skill_row(ts(60), message_id=1), terminal_row(ts(59))]
    rows[0]["content"] = secret_text                       # the user's own words
    rows[0]["preview"] = f"skill_view({secret_text})"
    write_audit(audit, rows)

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    text = (state / "diagnostics" / "export-20260920.ndjson").read_text(encoding="utf-8")
    assert secret_text not in text and "SECRET_USER" not in text
    # the locator points at the line that holds it; the line itself is never copied
    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert row["source_offset"] == 0 and row["source_available"] is True
    raw = audit.read_bytes()
    assert secret_text.encode("utf-8") in raw[row["source_offset"]:row["source_offset"] + 2000]


def test_redline_every_row_is_free_of_content_and_identity_keys(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59)),
                        skill_row(ts(50), message_id=2, skill="/bad/skill"), terminal_row(ts(49))])
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    def walk(value, path="$"):
        if isinstance(value, dict):
            for key, item in value.items():
                assert key.lower() not in mod.FORBIDDEN_CONTENT_KEYS, f"{path}.{key} is a content key"
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, f"{path}[{i}]")

    rows = diag_rows(state)
    assert len(rows) >= 3
    for row in rows:
        walk(row)


def test_redline_request_is_whitelisted_to_the_upload_contract(tmp_path: Path, monkeypatch) -> None:
    """A dump-then-drop projection would leak the day build_record grows a field; this
    one can only ever emit keys the wire contract already allows."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    real_build = mod.build_record

    def leaky(call, terminal):
        record = dict(real_build(call, terminal))
        record["content"] = "用户原话不该出现在这里"   # a future field that must not ride along
        return record

    monkeypatch.setattr(mod, "build_record", leaky)
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert set(row["request"]) <= set(LIFECYCLE_V9_KEYS)
    assert "content" not in row["request"]
    assert "用户原话" not in (state / "diagnostics" / "export-20260920.ndjson").read_text(encoding="utf-8")


def test_redline_a_hub_body_that_echoes_content_is_withheld(tmp_path: Path) -> None:
    """We author every other field; the Hub's body is the one thing we do not. If it ever
    echoes something content-shaped, the counters stay and the body goes."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    echo = {"accepted": 1, "rejected": 0, "errors": [],
            "echo": {"messages": [{"role": "user", "content": "用户原话被回显了"}]}}
    hub = FakeHub([(200, echo)])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert row["response"]["body_withheld"] == "content_shaped"
    assert json.loads(row["response"]["body"]) == {"accepted": 1, "rejected": 0}
    assert "用户原话" not in (state / "diagnostics" / "export-20260920.ndjson").read_text(encoding="utf-8")
    assert row["verdict"]["outcome"] == "confirmed", "the settlement itself is unaffected"


def test_redline_pass_summary_report_is_whitelisted(tmp_path: Path, monkeypatch) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])

    original = mod.project_report
    monkeypatch.setattr(mod, "project_report", lambda report: original(dict(report, sample=["用户原话"])))
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    summary = [r for r in diag_rows(state) if r["kind"] == "pass_summary"][0]
    assert set(summary["report"]) <= set(mod.REPORT_KEYS)
    assert "sample" not in summary["report"]


# ── row ids: the downstream cursor needs stable + monotonic ──────────────────


def test_row_ids_are_unique_monotonic_and_survive_across_passes(tmp_path: Path) -> None:
    """since=<at>&after_id=<id> paging stalls on rows sharing a millisecond unless every
    row carries its own ordered id."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), skill_row(ts(60), message_id=2),
                        skill_row(ts(60), message_id=3), terminal_row(ts(59))])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())
    first = diag_rows(state)
    before = [r["id"] for r in first]
    write_audit(audit, [skill_row(ts(60), message_id=1), skill_row(ts(60), message_id=2),
                        skill_row(ts(60), message_id=3), terminal_row(ts(59)),
                        skill_row(ts(40), message_id=4), terminal_row(ts(39))])
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())
    rows = diag_rows(state)

    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "ids are unique"
    assert ids == sorted(ids), "ids sort in append order"
    assert ids[: len(before)] == before, "already-written ids never change"
    # rows that share a timestamp are still distinguishable
    same_ms = [r for r in rows if r["at"] == rows[0]["at"]]
    assert len(same_ms) > 1 and len({r["id"] for r in same_ms}) == len(same_ms)
    assert all(r["id"] == f"20260920-{r['seq']:08d}" for r in rows)


def test_row_ids_keep_climbing_when_the_tail_is_unparsable(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())
    day = state / "diagnostics" / "export-20260920.ndjson"
    highest = max(json.loads(l)["seq"] for l in day.read_text(encoding="utf-8").splitlines())
    with day.open("a", encoding="utf-8") as fh:
        fh.write("{partial-write-that-never-finished\n")   # a torn line, as a crash would leave

    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59)),
                        skill_row(ts(40), message_id=9), terminal_row(ts(39))])
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    seqs = [json.loads(l)["seq"] for l in day.read_text(encoding="utf-8").splitlines() if l.startswith("{\"")]
    assert min(s for s in seqs if s > highest) == highest + 1, "the sequence resumes, it does not restart"


# ── the red line covers dead-letter.ndjson too ───────────────────────────────
#
# The admin endpoint reads this file as well, so the same guarantee has to hold in it —
# an interface layer is not where user text gets kept out.


def test_redline_dead_letter_row_withholds_the_audit_supplied_skill_name(tmp_path: Path) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1, skill="/home/hermes/private-skill-名字"),
                        terminal_row(ts(59))])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    text = (state / "dead-letter.ndjson").read_text(encoding="utf-8")
    assert "private-skill" not in text and "/home/" not in text
    row = dead_rows(state)[0]
    assert row["skill"] is None and row["skill_withheld"] is True
    assert row["class"] == "permanent" and row["reason"] == "local:invalid_skill_charset"
    # and the two files agree — this used to be the one place they diverged
    diag = [r for r in diag_rows(state) if r["kind"] == "local_reject"][0]
    assert diag["skill"] is None and diag["skill_withheld"] is True


def test_redline_dead_letter_payload_is_whitelisted_to_the_contract(tmp_path: Path, monkeypatch) -> None:
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    real_build = mod.build_record

    def leaky(call, terminal):
        return dict(real_build(call, terminal), content="用户原话不该进 dead-letter")

    monkeypatch.setattr(mod, "build_record", leaky)
    hub = FakeHub([(200, {"accepted": 0, "rejected": 1,
                          "errors": [{"index": 0, "reason": 'invalid_enum:client: "hermes"'}]})])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    row = dead_rows(state)[0]
    assert set(row["payload"]) <= set(LIFECYCLE_V9_KEYS) and "content" not in row["payload"]
    assert "用户原话" not in (state / "dead-letter.ndjson").read_text(encoding="utf-8")


def test_redline_a_rejection_reason_is_scrubbed_and_capped(tmp_path: Path) -> None:
    """The reason is the server's free text and the row exists to carry it — so it is kept
    verbatim, minus anything credential- or conversation-shaped."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), skill_row(ts(60), message_id=2),
                        skill_row(ts(60), message_id=3), terminal_row(ts(59))])
    token = "eyJhbGciOiJIUzI1NiJ9.leaked-through-the-reason.sig"
    hub = FakeHub([(200, {"accepted": 0, "rejected": 3, "errors": [
        {"index": 0, "reason": f"rejected_by_upstream: Authorization: Bearer {token}"},
        {"index": 1, "reason": 'echo_of_request: "content": "用户原话"'},
        {"index": 2, "reason": "x" * 900}]})])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    text = (state / "dead-letter.ndjson").read_text(encoding="utf-8")
    assert token not in text and "leaked-through-the-reason" not in text
    assert "用户原话" not in text
    reasons = sorted(r["reason"] for r in dead_rows(state))
    assert any("«redacted»" in r for r in reasons)
    assert any(r == "«withheld: content_shaped»" for r in reasons)
    assert any(r.endswith("…«truncated»") and len(r) <= mod.REASON_MAX_CHARS + 16 for r in reasons)


def test_redline_conversation_text_never_reaches_either_file(tmp_path: Path) -> None:
    """One check over both files at once: the audit carries user text, a legal skill call,
    an illegal one and a rejection — none of it may show up in what we hand over."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    utterance = "USER_SAID_把这段原文导出去"
    rows = [skill_row(ts(60), message_id=1), terminal_row(ts(59)),
            skill_row(ts(58), message_id=2, skill="../../etc/passwd")]
    for row in rows:
        row["content"] = utterance
        row["preview"] = utterance
    write_audit(audit, rows)
    hub = FakeHub([(200, {"accepted": 0, "rejected": 1,
                          "errors": [{"index": 0, "reason": 'invalid_enum:client: "hermes"'}]})])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok-xyz", "sunke"), http_post=hub)
    bundle = tmp_path / "handover.ndjson"
    mod.diagnose(state_dir=state, since="all", out=bundle, now=NOW)

    for path in (state / "dead-letter.ndjson", state / "diagnostics" / "export-20260920.ndjson", bundle):
        text = path.read_text(encoding="utf-8")
        for probe in (utterance, "USER_SAID", "passwd", "tok-xyz", "Bearer", "\"content\"", "s1"):
            assert probe not in text, f"{probe!r} leaked into {path.name}"


# ── terminal review round (codex gpt-6-astra): one regression test per finding ────


def all_files_text(state: Path, extra: Path | None = None) -> str:
    parts = [(state / "dead-letter.ndjson").read_text(encoding="utf-8")]
    for f in sorted((state / "diagnostics").glob("export-*.ndjson")):
        parts.append(f.read_text(encoding="utf-8"))
    if extra is not None:
        parts.append(extra.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_t1_every_copy_of_a_malicious_reason_is_sanitized(tmp_path: Path) -> None:
    """finding forbidden-content-written-verbatim: verdict.reason, response.error.reason
    and the report's own counter KEY are all copies of the server's string."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    nasty = 'content: "PRIVATE_UTTERANCE" Authorization: Bearer eyJleWes.tokenvalue.sig'
    hub = FakeHub([(200, {"accepted": 0, "rejected": 1,
                          "errors": [{"index": 0, "reason": nasty}]})])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)
    bundle = tmp_path / "out.ndjson"
    mod.diagnose(state_dir=state, since="all", out=bundle, now=NOW)

    text = all_files_text(state, bundle)
    assert "PRIVATE_UTTERANCE" not in text and "tokenvalue" not in text
    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert row["reason"] == row["verdict"]["reason"] == "«withheld: content_shaped»"
    assert row["response"].get("error") in (None, {"index": 0, "reason": "«withheld: content_shaped»"})
    summary = [r for r in diag_rows(state) if r["kind"] == "pass_summary"][0]
    assert "content" not in summary["report"]["upload"]["rejected_by_reason"]
    assert summary["report"]["upload"]["rejected_by_reason"] == {"unclassified": 1}


def test_t2_the_content_guard_fails_closed(tmp_path: Path) -> None:
    """finding content-guard-fails-open: depth/array budget used to answer 'clean' and the
    withheld branch copied unvalidated values."""
    assert mod.contains_forbidden_content({"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}) is True
    deep_list = [{"ok": i} for i in range(60)] + [{"content": "PRIVATE"}]
    assert mod.contains_forbidden_content({"errors": deep_list}) is True
    assert mod.contains_forbidden_content({"note": 'has "content": inside a string'}) is True

    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    hub = FakeHub([(200, {"accepted": 1, "rejected": 0, "errors": [],
                          "errorCode": {"content": "PRIVATE_UTTERANCE"}})])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert row["response"]["body_withheld"] == "content_shaped"
    assert "PRIVATE_UTTERANCE" not in json.dumps(row, ensure_ascii=False)
    assert json.loads(row["response"]["body"]) == {"accepted": 1, "rejected": 0}


def test_t3_a_legal_prefix_with_an_illegal_suffix_is_withheld_in_both_files(tmp_path: Path) -> None:
    """finding truncated-skill-revalidated: the call sites still truncated to 64 before
    validation, so dead-letter kept the legal-looking prefix."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    sneaky = "b" * 64 + "/home/hermes/secret"
    write_audit(audit, [skill_row(ts(60), message_id=1, skill=sneaky), terminal_row(ts(59))])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    dead = dead_rows(state)[0]
    diag = [r for r in diag_rows(state) if r["kind"] == "local_reject"][0]
    assert dead["skill"] is None and dead["skill_withheld"] is True
    assert diag["skill"] is None and diag["skill_withheld"] is True
    assert dead["skill"] == diag["skill"], "the two files must not disagree"
    text = all_files_text(state)
    assert "b" * 64 not in text and "/home/" not in text


def test_t4_an_unsettled_retry_advances_its_attempt_instead_of_looping(tmp_path: Path) -> None:
    """finding retry-attempt-not-persisted: once requeued, the outbox resent the record
    every tick — past the backoff and past the eight-attempt ceiling."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"),
               http_post=FakeHub([(200, {"accepted": 0, "rejected": 1,
                                         "errors": [{"index": 0, "reason": "internal_error"}]})]))

    # the retry goes out and the Hub is down: the record must leave the queue with its
    # attempt advanced, not sit in the outbox to be resent on the next tick
    down = FakeHub([(503, None)])
    second = run_export(audit_path=audit, state_dir=state, now=NOW + timedelta(minutes=11),
                        credentials=("tok", "sunke"), http_post=down)

    assert second["retry"]["requeued"] == 1 and second["retry"]["rescheduled"] == 1
    assert json.loads((state / "outbox.json").read_text(encoding="utf-8")) == []
    assert max(r["attempt"] for r in dead_rows(state)) == 2

    # three minutes later it is inside the (doubled) backoff, so nothing is sent
    quiet = FakeHub()
    third = run_export(audit_path=audit, state_dir=state, now=NOW + timedelta(minutes=14),
                       credentials=("tok", "sunke"), http_post=quiet)
    assert quiet.bodies == [] and third["retry"]["waiting"] == 1


def test_t5_a_retried_record_keeps_its_audit_locator(tmp_path: Path) -> None:
    """finding retry-metadata-discarded: the locator was pruned when the record left the
    outbox, so the resend — the row you actually want to debug — lost its offset."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"),
               http_post=FakeHub([(200, {"accepted": 0, "rejected": 1,
                                         "errors": [{"index": 0, "reason": 'invalid_enum:client: "hermes"'}]})]))
    assert dead_rows(state)[0]["locator"]["offset"] == 0

    run_export(audit_path=audit, state_dir=state, now=NOW + timedelta(minutes=11),
               credentials=("tok", "sunke"), http_post=FakeHub())

    resend = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][-1]
    assert resend["verdict"]["outcome"] == "confirmed" and resend["attempt"] == 2
    assert resend["source_available"] is True and resend["source_offset"] == 0
    assert resend["first_at"] == dead_rows(state)[0]["first_at"]


def test_t6_a_corrupt_retry_payload_is_isolated_not_fatal(tmp_path: Path) -> None:
    """finding retry-payload-not-validated: one bad row raised KeyError before any POST,
    taking every healthy record down with it — every ten minutes, forever."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    state.mkdir(parents=True)
    (state / "env.json").write_text(json.dumps({"env": "online"}), encoding="utf-8")
    (state / "dead-letter.ndjson").write_text("\n".join([
        json.dumps({"run_id": "deadbeef" * 4, "reason": "internal_error", "class": "retryable",
                    "at": rfc3339(NOW - timedelta(hours=2)), "attempt": 1, "payload": {}}),
        json.dumps({"run_id": "cafebabe" * 4, "reason": "internal_error", "class": "retryable",
                    "at": rfc3339(NOW - timedelta(hours=2)), "attempt": 1,
                    "payload": {"run_id": "mismatched", "client": "hermes"}}),
    ]) + "\n", encoding="utf-8")

    hub = FakeHub()
    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    assert report["retry"]["corrupt"] == 2 and report["retry"]["requeued"] == 0
    assert report["upload"]["accepted"] == 1, "the healthy record still goes out"


def test_t7_an_unknown_reason_starting_with_local_is_not_permanent() -> None:
    """finding unknown-local-prefix-permanent: startswith('local') swallowed a real
    server reason into the never-retry bucket."""
    assert mod.classify_reject_reason("localization_unavailable") == "repairable"
    assert mod.classify_reject_reason("local:invalid_skill_charset") == "permanent"
    assert mod.classify_reject_reason("local:processing_error") == "permanent"


def test_t8_a_torn_tail_does_not_swallow_the_next_row(tmp_path: Path) -> None:
    """finding torn-tail-not-recovered: appending onto an unterminated line glued two
    records into one unparsable row, and a scalar tail broke recovery outright."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())
    day = state / "diagnostics" / "export-20260920.ndjson"
    before = len(diag_rows(state))
    with day.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "upload_attempt", "seq": 99, "at": "2026')   # torn, no newline

    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59)),
                        skill_row(ts(40), message_id=2), terminal_row(ts(39))])
    report = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())

    lines = day.read_text(encoding="utf-8").splitlines()
    parsed = [l for l in lines if l.startswith("{") and _is_json(l)]
    assert len(parsed) > before, "new evidence still lands"
    assert report["upload"]["accepted"] == 1
    # a bare scalar tail must not break the next pass either
    with day.open("a", encoding="utf-8") as fh:
        fh.write("null\n")
    write_audit(audit, [skill_row(ts(30), message_id=3), terminal_row(ts(29))])
    again = run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())
    assert again["diagnostics"]["rows"] >= 1


def _is_json(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except ValueError:
        return False


def test_t9_diagnose_refuses_to_write_into_the_state_directory(tmp_path: Path) -> None:
    """finding output-can-overwrite-state: --out pointing at ledger.json overwrote the
    settlement record, which is the opposite of a read-only export."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=FakeHub())
    ledger_before = (state / "ledger.json").read_bytes()
    snapshot = {p.name: p.read_bytes() for p in state.rglob("*") if p.is_file()}

    for bad in (state / "ledger.json", state / "diagnostics" / "export-20260920.ndjson", state):
        with pytest.raises(ValueError):
            mod.diagnose(state_dir=state, since="all", out=bad, now=NOW)

    assert (state / "ledger.json").read_bytes() == ledger_before
    assert {p.name: p.read_bytes() for p in state.rglob("*") if p.is_file()} == snapshot


def test_t10_a_wrapped_gateway_response_still_yields_this_records_error(tmp_path: Path) -> None:
    """finding wrapped-error-evidence-missing: settle_batch unwraps {ok,data}; the log
    read only the top level and lost the per-record reason."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    hub = FakeHub([(200, {"ok": True, "data": {"accepted": 0, "rejected": 1, "errors": [
        {"index": 0, "reason": "retention_exceeded"}]}})])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert row["verdict"]["outcome"] == "dead_letter"
    assert row["response"]["error"] == {"index": 0, "reason": "retention_exceeded"}
    assert row["class"] == "permanent"


def test_t11_a_surrogate_reads_back_identically_in_body_and_error(tmp_path: Path) -> None:
    """finding unicode-evidence-replaced: the line was written with ensure_ascii=False and
    errors='replace', so the structured error said '?' while the body said \\ud800."""
    audit = tmp_path / "conversation-audit.jsonl"
    state = tmp_path / "state"
    write_audit(audit, [skill_row(ts(60), message_id=1), terminal_row(ts(59))])
    detail = json.loads('"\\ud800x"')
    hub = FakeHub([(200, {"accepted": 0, "rejected": 1,
                          "errors": [{"index": 0, "reason": "internal_error", "detail": detail}]})])

    run_export(audit_path=audit, state_dir=state, now=NOW, credentials=("tok", "sunke"), http_post=hub)

    row = [r for r in diag_rows(state) if r["kind"] == "upload_attempt"][0]
    assert row["response"]["error"]["detail"] == detail
    assert json.loads(row["response"]["body"])["errors"][0]["detail"] == detail
