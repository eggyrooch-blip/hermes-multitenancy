from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _message(
    ts: str,
    *,
    profile: str,
    platform: str,
    session: str,
    message_id: int,
    role: str,
    content: str = "",
    tool_name: str | None = None,
    tool_calls: dict | None = None,
    finish_reason: str | None = None,
) -> dict:
    return {
        "@timestamp": ts,
        "event_type": "conversation_message",
        "profile": profile,
        "platform": platform,
        "chat_type": "dm",
        "session_id": session,
        "message_id": message_id,
        "role": role,
        "content": content,
        "tool_name": tool_name,
        "tool_calls": json.dumps(tool_calls, ensure_ascii=False) if tool_calls is not None else None,
        "finish_reason": finish_reason,
        "source": "state_db_mirror",
    }


def _routing_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "create table multitenancy_routing ("
            "profile_name text, kind text, active integer, open_id text, chat_id text)"
        )
        conn.executemany(
            "insert into multitenancy_routing values (?, ?, ?, ?, ?)",
            [
                ("alice", "user", 1, "ou_alice_should_not_print", ""),
                ("bob", "user", 1, "ou_bob_should_not_print", ""),
                ("group_one", "group", 1, "", "oc_group_should_not_print"),
                ("old_user", "user", 0, "ou_inactive", ""),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_summary_reconstructs_turns_and_reports_safe_metrics(tmp_path: Path) -> None:
    from hermes_multitenancy.analytics.report import build_summary, render_markdown

    audit = tmp_path / "conversation-audit.jsonl"
    routing = tmp_path / "multitenancy.db"
    _routing_db(routing)
    _write_jsonl(
        audit,
        [
            _message(
                "2026-06-10T09:00:00+08:00",
                profile="alice",
                platform="feishu",
                session="s1",
                message_id=1,
                role="user",
                content="帮我更新飞书表格 https://keep.feishu.cn/sheets/secret",
            ),
            _message(
                "2026-06-10T09:00:01+08:00",
                profile="alice",
                platform="feishu",
                session="s1",
                message_id=2,
                role="assistant",
                tool_name="lark_cli",
                tool_calls={
                    "name": "lark_cli",
                    "args": {"mode": "shortcut", "argv": ["sheets", "+read", "--url", "secret"]},
                },
            ),
            _message(
                "2026-06-10T09:00:05+08:00",
                profile="alice",
                platform="feishu",
                session="s1",
                message_id=3,
                role="assistant",
                tool_name="skill_view",
                tool_calls={"name": "skill_view", "args": {"name": "lark-im"}},
            ),
            _message(
                "2026-06-10T09:00:05+08:00",
                profile="alice",
                platform="feishu",
                session="s1",
                message_id=4,
                role="assistant",
                content="✅ 已完成表格更新",
                finish_reason="stop",
            ),
            _message(
                "2026-06-10T10:00:00+08:00",
                profile="bob",
                platform="webui",
                session="s2",
                message_id=1,
                role="user",
                content="生成一张图片，Bearer sk-secret ou_1234567890abcdef oc_abcdef https://example.com/a?token=x",
            ),
            _message(
                "2026-06-10T10:00:01+08:00",
                profile="bob",
                platform="webui",
                session="s2",
                message_id=2,
                role="assistant",
                tool_name="image_generate",
                tool_calls={"name": "image_generate", "args": {"prompt": "secret"}},
            ),
            _message(
                "2026-06-10T10:00:05+08:00",
                profile="bob",
                platform="webui",
                session="s2",
                message_id=3,
                role="assistant",
                content="失败：model provider rejected the request",
                finish_reason="stop",
            ),
            _message(
                "2026-06-11T11:00:00+08:00",
                profile="alice",
                platform="feishu",
                session="s1",
                message_id=5,
                role="user",
                content="跑个 python 脚本处理 csv",
            ),
            _message(
                "2026-06-11T11:00:01+08:00",
                profile="alice",
                platform="feishu",
                session="s1",
                message_id=6,
                role="assistant",
                tool_name="terminal",
                tool_calls={"name": "terminal", "args": {"command": "python clean.py input.csv"}},
            ),
            _message(
                "2026-06-11T12:00:00+08:00",
                profile="alice",
                platform="feishu",
                session="s1",
                message_id=7,
                role="user",
                content="查一下资料",
            ),
            _message(
                "2026-06-11T12:00:01+08:00",
                profile="alice",
                platform="feishu",
                session="s1",
                message_id=8,
                role="assistant",
                tool_name="web_search",
                tool_calls={"name": "web_search", "args": {"query": "market"}},
            ),
            _message(
                "2026-06-11T12:00:04+08:00",
                profile="alice",
                platform="feishu",
                session="s1",
                message_id=9,
                role="assistant",
                content="这里是整理后的资料",
                finish_reason="stop",
            ),
        ],
    )

    summary = build_summary(
        audit_path=audit,
        routing_db=routing,
        days=7,
        include_profiles=False,
        include_samples=False,
    )

    assert summary["audit"]["rows"] == 12
    assert summary["routing"]["active_by_kind"] == {"group": 1, "user": 2}
    assert summary["windows"]["7d"]["turns"] == 4
    assert summary["windows"]["7d"]["active_profiles"] == 2
    assert summary["windows"]["7d"]["final_stop_rate"] == 75.0
    assert summary["windows"]["7d"]["explicit_failure_rate"] == 25.0
    assert summary["windows"]["7d"]["completion_proxy_rate"] == 50.0
    assert summary["scenarios"]["7d"]["primary"]["Feishu/Lark office automation"]["turns"] == 1
    assert summary["scenarios"]["7d"]["primary"]["Image/multimodal generation/analysis"]["failures"] == 1
    assert summary["failure_categories"]["7d"]["model/provider"] == 1
    assert summary["top"]["7d"]["tools"][0] == ["image_generate", 1]
    assert summary["top"]["7d"]["skills"] == [["lark-im", 1]]
    assert ["sheets +read", 1] in summary["top"]["7d"]["lark_commands"]
    assert "top_active_profiles" not in summary["top"]["7d"]
    assert "completion_proxy" in summary["methodology"]
    assert "proxy" in summary["methodology"]["completion_proxy"]
    assert "tool-call-only" in summary["methodology"]["completion_proxy_blind_spot"]
    assert "active profiles" in summary["methodology"]["active_user_proxy"]

    markdown = render_markdown(summary)
    assert "| 1d |" in markdown
    assert "| 7d |" in markdown
    assert "| 30d |" in markdown
    assert "| all |" in markdown
    assert "## Top Skills" in markdown
    assert "| lark-im | 1 |" in markdown
    assert "Completion is a proxy metric" in markdown
    assert "tool-call-only" in markdown
    assert "Completion Proxy Rate" in markdown
    assert "DAU Proxy" in markdown
    assert "active profiles" in markdown
    assert "## Concentration" in markdown
    assert "Top 10 Turn Share" in markdown
    assert "Proxy Completion" in markdown
    assert "Feishu/Lark office automation" in markdown
    assert "https://keep.feishu.cn" not in markdown
    assert "ou_alice" not in markdown


def test_summary_library_api_accepts_in_memory_records() -> None:
    from hermes_multitenancy.analytics import build_summary_from_records

    rows = [
        _message(
            "2026-06-11T12:00:00+08:00",
            profile="profile_a",
            platform="feishu",
            session="s",
            message_id=1,
            role="user",
            content="帮我查一下资料",
        ),
        _message(
            "2026-06-11T12:00:02+08:00",
            profile="profile_a",
            platform="feishu",
            session="s",
            message_id=2,
            role="assistant",
            content="整理完成",
            finish_reason="stop",
        ),
    ]
    routing_rows = [
        {"profile_name": "profile_a", "kind": "user", "active": 1},
        {"profile_name": "inactive_group", "kind": "group", "active": 0},
    ]

    summary = build_summary_from_records(rows, routing_rows, days=7)

    assert summary["audit"]["path"] == "<in-memory>"
    assert summary["routing"]["available"] is True
    assert summary["routing"]["active_by_kind"] == {"user": 1}
    assert summary["windows"]["1d"]["turns"] == 1
    assert summary["windows"]["7d"]["turns"] == 1
    assert summary["windows"]["30d"]["turns"] == 1
    assert summary["windows"]["all"]["turns"] == 1
    assert summary["scenarios"]["7d"]["primary"]["Knowledge/search/research"]["turns"] == 1


def test_missing_session_rows_do_not_merge_profiles() -> None:
    from hermes_multitenancy.analytics import build_summary_from_records

    rows = [
        _message(
            "2026-06-11T12:00:00+08:00",
            profile="alice",
            platform="feishu",
            session="",
            message_id=1,
            role="user",
            content="hello",
        ),
        _message(
            "2026-06-11T12:00:01+08:00",
            profile="alice",
            platform="feishu",
            session="",
            message_id=2,
            role="assistant",
            content="done",
            finish_reason="stop",
        ),
        _message(
            "2026-06-11T12:01:00+08:00",
            profile="bob",
            platform="feishu",
            session="",
            message_id=1,
            role="user",
            content="hello",
        ),
        _message(
            "2026-06-11T12:01:01+08:00",
            profile="bob",
            platform="feishu",
            session="",
            message_id=2,
            role="assistant",
            content="done",
            finish_reason="stop",
        ),
    ]

    summary = build_summary_from_records(rows, days=7)

    assert summary["windows"]["7d"]["turns"] == 2
    assert summary["windows"]["7d"]["sessions"] == 2
    assert summary["windows"]["7d"]["completion_proxy_rate"] == 100.0


def test_window_metrics_count_group_chat_and_agent_profiles() -> None:
    from hermes_multitenancy.analytics import build_summary_from_records

    rows = [
        _message(
            "2026-06-11T12:00:00+08:00",
            profile="unmapped_room",
            platform="feishu",
            session="group-session",
            message_id=1,
            role="user",
            content="群里查一下资料",
        )
        | {"chat_type": "group"},
        _message(
            "2026-06-11T12:00:01+08:00",
            profile="unmapped_room",
            platform="feishu",
            session="group-session",
            message_id=2,
            role="assistant",
            content="整理完成",
            finish_reason="stop",
        )
        | {"chat_type": "group"},
        _message(
            "2026-06-11T12:01:00+08:00",
            profile="agent_ops",
            platform="webui",
            session="agent-session",
            message_id=1,
            role="user",
            content="agent 帮我处理",
        ),
        _message(
            "2026-06-11T12:01:01+08:00",
            profile="agent_ops",
            platform="webui",
            session="agent-session",
            message_id=2,
            role="assistant",
            content="done",
            finish_reason="stop",
        ),
    ]
    routing_rows = [{"profile_name": "agent_ops", "kind": "agent", "active": 1}]

    summary = build_summary_from_records(rows, routing_rows, days=7)
    metrics = summary["windows"]["7d"]

    assert metrics["active_profiles"] == 2
    assert metrics["active_user_like_profiles"] == 0
    assert metrics["active_group_like_profiles"] == 1
    assert metrics["active_agent_like_profiles"] == 1
    assert metrics["active_profiles_by_kind_proxy"] == {"agent": 1, "group": 1, "user": 0}


def test_summary_can_include_profile_names_and_redacted_samples(tmp_path: Path) -> None:
    from hermes_multitenancy.analytics.report import build_summary

    audit = tmp_path / "conversation-audit.jsonl"
    _write_jsonl(
        audit,
        [
            _message(
                "2026-06-11T12:00:00+08:00",
                profile="alice",
                platform="feishu",
                session="s",
                message_id=1,
                role="user",
                content=(
                    "请读取 https://example.com/a?token=secret "
                    "Bearer abcdefghijklmnopqrstuvwxyz123456 ou_abcdef123456 oc_abcd1234"
                ),
            ),
            _message(
                "2026-06-11T12:00:02+08:00",
                profile="alice",
                platform="feishu",
                session="s",
                message_id=2,
                role="assistant",
                content="需要授权，无法读取",
                finish_reason="stop",
            ),
        ],
    )

    summary = build_summary(
        audit_path=audit,
        routing_db=None,
        days=7,
        include_profiles=True,
        include_samples=True,
        sample_limit=1,
    )

    assert summary["top"]["7d"]["top_active_profiles"] == [["alice", 1]]
    sample = summary["samples"][0]["text"]
    assert "<url>" in sample
    assert "<bearer>" in sample
    assert "<open_id>" in sample
    assert "<chat_id>" in sample
    assert "abcdefghijklmnopqrstuvwxyz" not in sample

    zero_sample_summary = build_summary(
        audit_path=audit,
        routing_db=None,
        days=7,
        include_profiles=True,
        include_samples=True,
        sample_limit=0,
    )
    assert zero_sample_summary["samples"] == []


def test_include_profiles_redacts_identifier_like_profile_names(tmp_path: Path) -> None:
    from hermes_multitenancy.analytics.report import build_summary, render_markdown

    audit = tmp_path / "conversation-audit.jsonl"
    _write_jsonl(
        audit,
        [
            _message(
                "2026-06-11T12:00:00+08:00",
                profile="feishu_group_oc_secret_chat_id",
                platform="feishu",
                session="s",
                message_id=1,
                role="user",
                content="hello",
            ),
            _message(
                "2026-06-11T12:00:02+08:00",
                profile="feishu_group_oc_secret_chat_id",
                platform="feishu",
                session="s",
                message_id=2,
                role="assistant",
                content="done",
                finish_reason="stop",
            ),
        ],
    )

    summary = build_summary(
        audit_path=audit,
        routing_db=None,
        days=7,
        include_profiles=True,
        include_samples=True,
        sample_limit=1,
    )
    payload = json.dumps(summary, ensure_ascii=False)
    markdown = render_markdown(summary)

    assert "oc_secret_chat_id" not in payload
    assert "oc_secret_chat_id" not in markdown
    assert summary["top"]["7d"]["top_active_profiles"] == [["feishu_group_<chat_id>", 1]]
    assert summary["samples"][0]["profile"] == "feishu_group_<chat_id>"


def test_cli_outputs_json_and_markdown(tmp_path: Path, capsys) -> None:
    from hermes_multitenancy.analytics.cli import main

    audit = tmp_path / "conversation-audit.jsonl"
    _write_jsonl(
        audit,
        [
            _message(
                "2026-06-11T12:00:00+08:00",
                profile="alice",
                platform="feishu",
                session="s",
                message_id=1,
                role="user",
                content="hello",
            ),
            _message(
                "2026-06-11T12:00:02+08:00",
                profile="alice",
                platform="feishu",
                session="s",
                message_id=2,
                role="assistant",
                content="done",
                finish_reason="stop",
            ),
        ],
    )

    assert main(["summary", "--audit", str(audit), "--days", "7", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["windows"]["7d"]["turns"] == 1

    assert main(["summary", "--audit", str(audit), "--days", "7", "--format", "markdown"]) == 0
    out = capsys.readouterr().out
    assert "# Hermes Conversation Analytics" in out
    assert "DAU" in out


def test_lark_command_family_omits_sensitive_argv_by_default(tmp_path: Path) -> None:
    from hermes_multitenancy.analytics.report import build_summary, render_markdown

    audit = tmp_path / "conversation-audit.jsonl"
    _write_jsonl(
        audit,
        [
            _message(
                "2026-06-11T12:00:00+08:00",
                profile="alice",
                platform="feishu",
                session="s",
                message_id=1,
                role="user",
                content="发群消息",
            ),
            _message(
                "2026-06-11T12:00:01+08:00",
                profile="alice",
                platform="feishu",
                session="s",
                message_id=2,
                role="assistant",
                tool_name="lark_cli",
                tool_calls={
                    "name": "lark_cli",
                    "args": {"mode": "shortcut", "argv": ["im", "oc_secret_chat_id", "--text", "hello"]},
                },
            ),
            _message(
                "2026-06-11T12:00:02+08:00",
                profile="alice",
                platform="feishu",
                session="s",
                message_id=3,
                role="assistant",
                content="发送成功",
                finish_reason="stop",
            ),
            _message(
                "2026-06-11T12:01:00+08:00",
                profile="alice",
                platform="feishu",
                session="s2",
                message_id=1,
                role="user",
                content="读取文档",
            ),
            _message(
                "2026-06-11T12:01:01+08:00",
                profile="alice",
                platform="feishu",
                session="s2",
                message_id=2,
                role="assistant",
                tool_name="lark_cli",
                tool_calls={
                    "name": "lark_cli",
                    "args": {"mode": "shortcut", "argv": ["docs", "Nq5JdFabcdef123456", "--format", "md"]},
                },
            ),
            _message(
                "2026-06-11T12:01:02+08:00",
                profile="alice",
                platform="feishu",
                session="s2",
                message_id=3,
                role="assistant",
                content="读取成功",
                finish_reason="stop",
            ),
        ],
    )

    summary = build_summary(audit_path=audit, routing_db=None, days=7)
    markdown = render_markdown(summary)
    payload = json.dumps(summary, ensure_ascii=False)

    assert ["docs", 1] in summary["top"]["7d"]["lark_commands"]
    assert ["im", 1] in summary["top"]["7d"]["lark_commands"]
    assert "oc_secret_chat_id" not in markdown
    assert "oc_secret_chat_id" not in payload
    assert "Nq5JdFabcdef123456" not in markdown
    assert "Nq5JdFabcdef123456" not in payload


def test_summary_tolerates_bad_jsonl_and_missing_routing_db(tmp_path: Path) -> None:
    from hermes_multitenancy.analytics.report import build_summary, render_markdown

    audit = tmp_path / "conversation-audit.jsonl"
    audit.write_text(
        '{"event_type":"conversation_message","@timestamp":"2026-06-11T12:00:00+08:00","role":"user"}\n'
        "{not-json}\n"
        "[]\n"
        "null\n"
        '"not an object"\n',
        encoding="utf-8",
    )

    summary = build_summary(audit_path=audit, routing_db=tmp_path / "missing.db", days=7)

    assert summary["audit"]["bad_lines"] == 4
    assert summary["routing"]["available"] is False
    assert summary["windows"]["7d"]["turns"] == 1
    assert "Hermes Conversation Analytics" in render_markdown(summary)


def test_audit_path_is_redacted_in_outputs(tmp_path: Path) -> None:
    from hermes_multitenancy.analytics.report import build_summary, render_markdown

    sensitive_dir = tmp_path / "feishu_group_oc_secret_chat_id"
    sensitive_dir.mkdir()
    audit = sensitive_dir / "conversation-audit.jsonl"
    _write_jsonl(
        audit,
        [
            _message(
                "2026-06-11T12:00:00+08:00",
                profile="alice",
                platform="feishu",
                session="s",
                message_id=1,
                role="user",
                content="hello",
            ),
        ],
    )

    summary = build_summary(audit_path=audit, routing_db=None, days=7)
    markdown = render_markdown(summary)
    payload = json.dumps(summary, ensure_ascii=False)

    assert "oc_secret_chat_id" not in payload
    assert "oc_secret_chat_id" not in markdown
    assert "<chat_id>" in summary["audit"]["path"]


def test_file_path_failure_phrases_are_counted_as_explicit_failures() -> None:
    from hermes_multitenancy.analytics.classify import classify_failure

    for text in [
        "file not found",
        "no such file or directory",
        "read-only file system",
        "文件不存在",
        "cannot open file",
    ]:
        assert classify_failure(text) == "file/path"


def test_negative_final_statuses_do_not_count_as_completion_proxy(tmp_path: Path) -> None:
    from hermes_multitenancy.analytics.report import build_summary

    audit = tmp_path / "conversation-audit.jsonl"
    rows = []
    for index, final_text in enumerate(["未完成", "token expired", "not ok"], start=1):
        rows.extend(
            [
                _message(
                    f"2026-06-11T12:0{index}:00+08:00",
                    profile=f"user_{index}",
                    platform="feishu",
                    session=f"s{index}",
                    message_id=1,
                    role="user",
                    content="处理一下",
                ),
                _message(
                    f"2026-06-11T12:0{index}:01+08:00",
                    profile=f"user_{index}",
                    platform="feishu",
                    session=f"s{index}",
                    message_id=2,
                    role="assistant",
                    content=final_text,
                    finish_reason="stop",
                ),
            ]
        )
    _write_jsonl(audit, rows)

    summary = build_summary(audit_path=audit, routing_db=None, days=7)

    assert summary["windows"]["7d"]["final_stop_rate"] == 100.0
    assert summary["windows"]["7d"]["explicit_failure_rate"] == 100.0
    assert summary["windows"]["7d"]["completion_proxy_rate"] == 0.0
    assert summary["windows"]["7d"]["success_signal_rate"] == 0.0


def test_scenario_uses_user_request_not_final_status_text(tmp_path: Path) -> None:
    from hermes_multitenancy.analytics.report import build_summary

    audit = tmp_path / "conversation-audit.jsonl"
    _write_jsonl(
        audit,
        [
            _message(
                "2026-06-11T12:00:00+08:00",
                profile="alice",
                platform="feishu",
                session="s",
                message_id=1,
                role="user",
                content="帮我查一下资料",
            ),
            _message(
                "2026-06-11T12:00:02+08:00",
                profile="alice",
                platform="feishu",
                session="s",
                message_id=2,
                role="assistant",
                content="已完成，顺带附上图片上传状态说明",
                finish_reason="stop",
            ),
        ],
    )

    summary = build_summary(audit_path=audit, routing_db=None, days=7)

    assert summary["scenarios"]["7d"]["primary"]["Knowledge/search/research"]["turns"] == 1
    assert "Image/multimodal generation/analysis" not in summary["scenarios"]["7d"]["primary"]


def test_scenario_prioritizes_reminder_automation_over_research_keywords(tmp_path: Path) -> None:
    from hermes_multitenancy.analytics.report import build_summary

    audit = tmp_path / "conversation-audit.jsonl"
    _write_jsonl(
        audit,
        [
            _message(
                "2026-06-11T12:00:00+08:00",
                profile="alice",
                platform="feishu",
                session="s",
                message_id=1,
                role="user",
                content="每天提醒我查一下资料",
            ),
        ],
    )

    summary = build_summary(audit_path=audit, routing_db=None, days=7)

    assert summary["scenarios"]["7d"]["primary"]["Automation/reminder/cron"]["turns"] == 1


def test_scenario_classifies_internal_api_text_as_code_data(tmp_path: Path) -> None:
    from hermes_multitenancy.analytics.report import build_summary

    audit = tmp_path / "conversation-audit.jsonl"
    _write_jsonl(
        audit,
        [
            _message(
                "2026-06-11T12:00:00+08:00",
                profile="alice",
                platform="feishu",
                session="s",
                message_id=1,
                role="user",
                content="调用 internal API 查询库存",
            ),
        ],
    )

    summary = build_summary(audit_path=audit, routing_db=None, days=7)

    assert summary["scenarios"]["7d"]["primary"]["Code/data/file operations"]["turns"] == 1
