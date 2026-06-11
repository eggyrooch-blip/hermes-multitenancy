from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_disabled_by_default_writes_nothing(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy.token_usage_ledger import append_token_usage

    ledger = tmp_path / "token-usage.jsonl"
    monkeypatch.delenv("HERMES_TOKEN_USAGE_LEDGER_ENABLED", raising=False)
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_PATH", str(ledger))

    append_token_usage(
        sender_open_id="ou_a", profile="owner", platform="feishu", chat_type="p2p",
        model="sonnet-4-6", input_tokens=10, output_tokens=5, total_tokens=15,
    )
    assert not ledger.exists()


def test_enabled_writes_full_contract_shape(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy.token_usage_ledger import append_token_usage

    ledger = tmp_path / "token-usage.jsonl"
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_ENABLED", "1")
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_PATH", str(ledger))

    append_token_usage(
        sender_open_id="ou_a", profile="owner", platform="feishu", chat_type="p2p",
        chat_id="oc_x", model="sonnet-4-6", input_tokens=1234, output_tokens=567, total_tokens=1801,
        timestamp="2026-06-11T17:40:12+08:00",
    )
    rows = _read_lines(ledger)
    assert len(rows) == 1
    assert rows[0] == {
        "ts": "2026-06-11T17:40:12+08:00",
        "sender_open_id": "ou_a",
        "profile": "owner",
        "platform": "feishu",
        "chat_type": "p2p",
        "chat_id": "oc_x",
        "model": "sonnet-4-6",
        "input_tokens": 1234,
        "output_tokens": 567,
        "total_tokens": 1801,
    }


def test_total_falls_back_to_input_plus_output(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy.token_usage_ledger import append_token_usage

    ledger = tmp_path / "t.jsonl"
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_ENABLED", "1")
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_PATH", str(ledger))

    append_token_usage(
        sender_open_id="ou_a", profile="p", platform="feishu", chat_type="p2p",
        model="m", input_tokens=10, output_tokens=5, total_tokens=0,
    )
    assert _read_lines(ledger)[0]["total_tokens"] == 15


def test_zero_usage_turn_is_not_logged(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy.token_usage_ledger import append_token_usage

    ledger = tmp_path / "t.jsonl"
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_ENABLED", "1")
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_PATH", str(ledger))

    append_token_usage(
        sender_open_id="ou_a", profile="p", platform="feishu", chat_type="p2p",
        model="m", input_tokens=0, output_tokens=0, total_tokens=0,
    )
    assert not ledger.exists()


def test_group_chat_two_senders_attribute_to_each_person(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy.token_usage_ledger import append_token_usage

    ledger = tmp_path / "t.jsonl"
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_ENABLED", "1")
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_PATH", str(ledger))

    # Same group profile, two different humans @-ing the bot.
    for sender in ("ou_alice", "ou_bob"):
        append_token_usage(
            sender_open_id=sender, profile="group_profile", platform="feishu",
            chat_type="group", model="m", input_tokens=10, output_tokens=5, total_tokens=15,
        )
    rows = _read_lines(ledger)
    assert [r["sender_open_id"] for r in rows] == ["ou_alice", "ou_bob"]
    # Attribution keys on the human sender, never collapses to the group profile.
    assert all(r["profile"] == "group_profile" for r in rows)


def test_append_never_raises_on_unwritable_path(monkeypatch, tmp_path: Path) -> None:
    from hermes_multitenancy.token_usage_ledger import append_token_usage

    # Point at a path whose parent is a file -> mkdir/open will fail; must swallow.
    blocker = tmp_path / "iam_a_file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_ENABLED", "1")
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_PATH", str(blocker / "nested" / "t.jsonl"))

    # Should not raise.
    append_token_usage(
        sender_open_id="ou_a", profile="p", platform="feishu", chat_type="p2p",
        model="m", input_tokens=10, output_tokens=5, total_tokens=15,
    )


def test_read_agent_session_tokens_reads_counters_and_falls_back(monkeypatch) -> None:
    from hermes_multitenancy.token_usage_ledger import read_agent_session_tokens

    agent = SimpleNamespace(
        session_input_tokens=100, session_output_tokens=40, session_total_tokens=140
    )
    assert read_agent_session_tokens(agent) == {
        "input_tokens": 100, "output_tokens": 40, "total_tokens": 140
    }
    # Missing attributes (defensive against upstream core renames) -> 0, no crash.
    assert read_agent_session_tokens(SimpleNamespace()) == {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0
    }
