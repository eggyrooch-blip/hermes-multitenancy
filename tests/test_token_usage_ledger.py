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
        cache_read_tokens=26988, cache_write_tokens=311, api_calls=19,
    )
    rows = _read_lines(ledger)
    assert len(rows) == 1
    # 全等断言：新增字段必须出现，既有字段名/取值必须逐字不变（uploader 依赖该契约）。
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
        "cache_read_tokens": 26988,
        "cache_write_tokens": 311,
        "api_calls": 19,
    }


def test_observability_fields_default_to_zero_when_caller_omits_them(monkeypatch, tmp_path: Path) -> None:
    """老调用方（不传新参数）仍要写出这三个 key，值为 0 —— 消费侧才能无条件读。"""
    from hermes_multitenancy.token_usage_ledger import append_token_usage

    ledger = tmp_path / "t.jsonl"
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_ENABLED", "1")
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_PATH", str(ledger))

    append_token_usage(
        sender_open_id="ou_a", profile="owner", platform="feishu", chat_type="p2p",
        model="sonnet-4-6", input_tokens=10, output_tokens=5, total_tokens=15,
    )
    row = _read_lines(ledger)[0]
    assert row["cache_read_tokens"] == 0
    assert row["cache_write_tokens"] == 0
    assert row["api_calls"] == 0


def test_read_agent_session_tokens_reads_cache_and_api_calls() -> None:
    from hermes_multitenancy.token_usage_ledger import read_agent_session_tokens

    agent = SimpleNamespace(
        session_input_tokens=100, session_output_tokens=20, session_total_tokens=2120,
        session_cache_read_tokens=2000, session_cache_write_tokens=7,
        _api_call_count=19,
    )
    assert read_agent_session_tokens(agent) == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 2120,
        "cache_read_tokens": 2000,
        "cache_write_tokens": 7,
        "api_calls": 19,
    }


def test_read_agent_session_tokens_tolerates_core_without_new_counters() -> None:
    """MT 测试环境与生产跑的不是同一条 core 线；属性缺失必须兜底 0，绝不抛。"""
    from hermes_multitenancy.token_usage_ledger import read_agent_session_tokens

    old_core_agent = SimpleNamespace(
        session_input_tokens=100, session_output_tokens=20, session_total_tokens=120,
    )
    got = read_agent_session_tokens(old_core_agent)
    assert got["cache_read_tokens"] == 0
    assert got["cache_write_tokens"] == 0
    assert got["api_calls"] == 0
    assert got["input_tokens"] == 100


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
        "input_tokens": 100, "output_tokens": 40, "total_tokens": 140,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "api_calls": 0,
    }
    # Missing attributes (defensive against upstream core renames) -> 0, no crash.
    assert read_agent_session_tokens(SimpleNamespace()) == {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "api_calls": 0,
    }


# ── cron 平台标记 ──────────────────────────────────────────────────────────
# 回归背景：cron 的合成 event 没有 platform，被 _resolve_platform_value 的 "feishu"
# 默认值吞掉，台账里 cron 回合全部戴飞书帽子（生产近 7 天 3632 行 chat_type 为空的
# "飞书"行其实是 cron，真实飞书 DM 只有 486 行），成本按平台归因完全失真。

def test_cron_process_marks_platform_cron(monkeypatch) -> None:
    from hermes_multitenancy.agent_real._core import _resolve_token_ledger_platform

    monkeypatch.setenv("HERMES_CRON_JOB", "1")
    # cron 的合成 source 没有 platform —— 正是被默认值吞掉的那一种。
    assert _resolve_token_ledger_platform(SimpleNamespace()) == "cron"
    assert _resolve_token_ledger_platform(None) == "cron"


def test_non_cron_platforms_do_not_regress(monkeypatch) -> None:
    """负控制：不在 cron 进程里时，飞书/群聊/WebUI 的归属必须逐字不变。"""
    from hermes_multitenancy.agent_real._core import _resolve_token_ledger_platform

    monkeypatch.delenv("HERMES_CRON_JOB", raising=False)
    assert _resolve_token_ledger_platform(SimpleNamespace()) == "feishu"
    assert _resolve_token_ledger_platform(None) == "feishu"
    assert _resolve_token_ledger_platform(SimpleNamespace(platform="webui")) == "webui"
    assert _resolve_token_ledger_platform(
        SimpleNamespace(platform=SimpleNamespace(value="webui"))
    ) == "webui"


def test_cron_marker_is_falsey_safe(monkeypatch) -> None:
    """标记为 "0"/"" 不算 cron —— 否则一个手滑的 env 会把全公司回合都记成 cron。"""
    from hermes_multitenancy.agent_real._core import _resolve_token_ledger_platform

    for raw in ("0", "", "false", "no"):
        monkeypatch.setenv("HERMES_CRON_JOB", raw)
        assert _resolve_token_ledger_platform(SimpleNamespace(platform="webui")) == "webui"


def test_cron_subprocess_env_carries_the_marker(monkeypatch, tmp_path: Path) -> None:
    """契约的另一半：cron 子进程真的会被打上标记（只断言 env 组装，不真跑 job）。"""
    import subprocess as _sp

    from hermes_multitenancy.cron import execution

    seen: dict = {}

    class _FakeProc:
        pid = 4242
        returncode = 0

        def communicate(self, input=None, timeout=None):  # noqa: A002
            return json.dumps({"success": True, "output": "", "final_response": ""}), ""

        def poll(self):
            return 0

    def _fake_popen(argv, **kwargs):
        seen.update(kwargs.get("env") or {})
        return _FakeProc()

    monkeypatch.setattr(_sp, "Popen", _fake_popen)
    monkeypatch.setattr(execution.subprocess, "Popen", _fake_popen, raising=False)
    execution._run_job_for_profile_subprocess(tmp_path, {"id": "j1", "name": "n"})

    assert seen.get("HERMES_CRON_JOB") == "1"
    assert seen.get("HERMES_HOME") == str(tmp_path.resolve())


# ── 只读聚合 CLI ───────────────────────────────────────────────────────────
# 埋点写下来的数要能被算出来，Done 线的后半句就是这个。

def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def test_summarize_groups_three_platforms_separately() -> None:
    from hermes_multitenancy.token_usage_ledger import summarize_rows

    rows = [
        # cron：两轮，共 30 次调用，缓存 900/(900+100)=90%
        {"platform": "cron", "api_calls": 10, "cache_read_tokens": 400, "input_tokens": 50},
        {"platform": "cron", "api_calls": 20, "cache_read_tokens": 500, "input_tokens": 50},
        # feishu：一轮 2 次调用，缓存 0
        {"platform": "feishu", "api_calls": 2, "cache_read_tokens": 0, "input_tokens": 1000},
        {"platform": "webui", "api_calls": 6, "cache_read_tokens": 300, "input_tokens": 100},
    ]
    got = summarize_rows(rows)

    assert set(got) == {"cron", "feishu", "webui"}
    assert got["cron"]["turns"] == 2
    assert got["cron"]["calls_per_turn"] == 15.0
    assert got["cron"]["cache_hit_rate"] == 0.9
    assert got["feishu"]["calls_per_turn"] == 2.0
    assert got["feishu"]["cache_hit_rate"] == 0.0
    assert got["webui"]["cache_hit_rate"] == 0.75


def test_summarize_handles_zero_denominator_and_legacy_rows() -> None:
    """老行没有新字段；全 0 的行不能除零，必须记 0.0。"""
    from hermes_multitenancy.token_usage_ledger import summarize_rows

    got = summarize_rows([
        {"platform": "feishu"},                       # 老行：三字段全缺
        {"platform": "feishu", "input_tokens": 0},     # 分母为 0
    ])
    assert got["feishu"]["turns"] == 2
    assert got["feishu"]["calls_per_turn"] == 0.0
    assert got["feishu"]["cache_hit_rate"] == 0.0


def test_iter_ledger_rows_skips_malformed_lines(tmp_path: Path) -> None:
    from hermes_multitenancy.token_usage_ledger import iter_ledger_rows

    ledger = tmp_path / "t.jsonl"
    ledger.write_text(
        '{"ts":"2026-08-13T10:00:00+08:00","platform":"cron","api_calls":3}\n'
        "\n"
        "{ this is not json\n"          # 半行/脏行：跳过，不能让统计整个失败
        '"a bare string"\n'             # 合法 JSON 但不是 dict
        '{"ts":"2026-08-12T10:00:00+08:00","platform":"feishu","api_calls":1}\n',
        encoding="utf-8",
    )
    assert [r["platform"] for r in iter_ledger_rows(ledger)] == ["cron", "feishu"]
    # --date 过滤只看那一天
    assert [r["platform"] for r in iter_ledger_rows(ledger, "2026-08-13")] == ["cron"]


def test_cli_prints_both_ratios_per_platform(tmp_path: Path, capsys) -> None:
    from hermes_multitenancy.token_usage_ledger import _main

    ledger = tmp_path / "t.jsonl"
    _write_ledger(ledger, [
        {"ts": "2026-08-13T10:00:00+08:00", "platform": "cron",
         "api_calls": 12, "cache_read_tokens": 900, "input_tokens": 100},
        {"ts": "2026-08-13T10:01:00+08:00", "platform": "feishu",
         "api_calls": 2, "cache_read_tokens": 0, "input_tokens": 500},
    ])
    assert _main(["--path", str(ledger), "--date", "2026-08-13"]) == 0

    out = capsys.readouterr().out
    assert "cron" in out and "feishu" in out
    assert "12.0" in out          # cron calls/turn
    assert "90.0%" in out         # cron cache hit
    assert "2.0" in out           # feishu calls/turn


def test_cli_reports_missing_or_empty_ledger(tmp_path: Path, capsys) -> None:
    from hermes_multitenancy.token_usage_ledger import _main

    assert _main(["--path", str(tmp_path / "nope.jsonl")]) == 1
    assert "台账不存在" in capsys.readouterr().out

    empty = tmp_path / "e.jsonl"
    empty.write_text("", encoding="utf-8")
    assert _main(["--path", str(empty)]) == 1
    assert "没有数据" in capsys.readouterr().out


def test_budget_exhausted_row_carries_the_flag_and_omits_it_otherwise(
    monkeypatch, tmp_path: Path
) -> None:
    """预算耗尽必须落盘可 grep；正常回合不给每行加一列。"""
    from hermes_multitenancy.token_usage_ledger import append_token_usage

    ledger = tmp_path / "token-usage.jsonl"
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_ENABLED", "1")
    monkeypatch.setenv("HERMES_TOKEN_USAGE_LEDGER_PATH", str(ledger))

    append_token_usage(
        sender_open_id="ou_a", profile="owner", platform="webui", chat_type="p2p",
        model="gpt-5", input_tokens=1, output_tokens=1, total_tokens=260000,
        api_calls=64, budget_exhausted=True,
    )
    append_token_usage(
        sender_open_id="ou_a", profile="owner", platform="webui", chat_type="p2p",
        model="gpt-5", input_tokens=1, output_tokens=1, total_tokens=2,
        api_calls=3,
    )
    rows = _read_lines(ledger)
    assert rows[0]["budget_exhausted"] is True
    assert "budget_exhausted" not in rows[1]
