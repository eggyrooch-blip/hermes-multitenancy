"""larkcli-offload-parse-fabrication — the notice-strip regexes must never
corrupt lark-cli's JSON stdout, and a requested-JSON parse failure must be
loud instead of a silent ok:true (the 2026-08-20 fabrication incident)."""

import json

import pytest

from hermes_multitenancy import lark_cli_tool


@pytest.fixture(autouse=True)
def _broker_env(monkeypatch):
    monkeypatch.setenv("LARKSUITE_CLI_AUTH_PROXY", "http://127.0.0.1:16384")
    monkeypatch.setenv("LARKSUITE_CLI_PROXY_KEY", "per-run-proxy-key")
    monkeypatch.setenv("LARKSUITE_CLI_APP_ID", "cli_public")


def _setup(monkeypatch, tmp_path, stdout, stderr="", returncode=0):
    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "profile"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))

    captured = {}

    class Completed:
        pass

    Completed.returncode = returncode
    Completed.stdout = stdout
    Completed.stderr = stderr

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)
    return captured


def _invoke(argv, mode="shortcut"):
    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": mode,
            "argv": argv,
            "identity": "user",
            "risk": "read",
            "reason": "test",
        }
    )
    return raw if isinstance(raw, dict) else json.loads(raw)


def _notice_payload() -> str:
    """Real lark-cli 1.0.86 shape: pretty-printed JSON whose `_notice` tail
    carries "run: lark-cli update" lines, `message` last (no trailing comma)."""
    payload = {
        "ok": True,
        "identity": "user",
        "data": {
            "count": 2,
            "has_more": False,
            "tasks": [
                {
                    "definition_name": "AI 工具开通审批",
                    "initiator_name": "测试甲",
                    "summaries": [{"key": "申请账号", "value": "fake-a@example.com"}],
                },
                {
                    "definition_name": "服务台审批",
                    "initiator_name": "测试乙",
                    "summaries": [],
                },
            ],
        },
        "_notice": {
            "skills": {
                "command": "lark-cli update",
                "current": "1.0.63",
                "target": "1.0.86",
                "message": "lark-cli skills 1.0.63 out of sync with binary 1.0.86, run: lark-cli update",
            },
            "update": {
                "command": "lark-cli update",
                "current": "1.0.86",
                "latest": "1.0.88",
                "message": "lark-cli 1.0.88 available, current 1.0.86, run: lark-cli update",
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


APPROVAL_ARGV = [
    "approval",
    "tasks",
    "query",
    "--params",
    '{"topic":"1","page_size":100}',
    "--format",
    "json",
]


def test_json_with_notice_tail_parses_and_notice_is_dropped(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, stdout=_notice_payload())

    result = _invoke(APPROVAL_ARGV)

    assert result["ok"] is True
    assert result["json"] is not None, "notice-strip corrupted valid JSON"
    assert result["json"]["data"]["count"] == 2
    assert len(result["json"]["data"]["tasks"]) == 2
    assert "_notice" not in result["json"]
    assert result["stdout"] == ""


def test_plain_text_update_banner_still_stripped(monkeypatch, tmp_path):
    text = (
        "queried 3 events\n"
        "lark-cli 1.0.88 available, current 1.0.86, run: lark-cli update\n"
        "done\n"
    )
    _setup(monkeypatch, tmp_path, stdout=text)

    result = _invoke(["calendar", "+agenda"])

    assert result["ok"] is True
    assert "lark-cli update" not in result["stdout"]
    assert "queried 3 events" in result["stdout"]
    assert "done" in result["stdout"]


def test_requested_json_parse_failure_is_loud(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, stdout='{"ok": true, "data": {"count": 99')

    result = _invoke(APPROVAL_ARGV)

    assert result["json_parse_failed"] is True
    assert result["ok"] is False
    assert result["error_code"] == "FEISHU_OUTPUT_UNPARSEABLE"


def test_subprocess_env_suppresses_notice_at_source(monkeypatch, tmp_path):
    captured = _setup(monkeypatch, tmp_path, stdout='{"ok": true}')

    _invoke(APPROVAL_ARGV)

    env = captured["kwargs"]["env"]
    assert env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] == "1"
    assert env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] == "1"


def test_non_json_format_flag_is_not_flagged_as_parse_failure(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, stdout="# Doc title\n\nplain markdown body\n")

    result = _invoke(["docs", "get", "doccnFake", "--format", "md"])

    assert result["ok"] is True
    assert "json_parse_failed" not in result
    assert "plain markdown body" in result["stdout"]


def test_notice_dropped_when_json_parses_only_after_banner_strip(monkeypatch, tmp_path):
    banner = "lark-cli update available, please upgrade\n"
    payload = json.dumps(
        {
            "ok": True,
            "data": {"count": 1, "tasks": [{"definition_name": "服务台审批"}]},
            "_notice": {"update": {"latest": "1.0.88", "current": "1.0.86"}},
        },
        ensure_ascii=False,
        indent=2,
    )
    _setup(monkeypatch, tmp_path, stdout=banner + payload)

    result = _invoke(APPROVAL_ARGV)

    assert result["ok"] is True
    assert result["json"]["data"]["count"] == 1
    assert "_notice" not in result["json"]


def test_redact_inside_json_string_keeps_json_valid(monkeypatch, tmp_path):
    payload = json.dumps(
        {"ok": True, "data": {"access_token": "u-secretvalue123", "name": "x"}},
        indent=2,
    )
    _setup(monkeypatch, tmp_path, stdout=payload)

    result = _invoke(APPROVAL_ARGV)

    assert result["ok"] is True
    assert result["json"]["data"]["access_token"] == "***REDACTED***"
    assert result["json"]["data"]["name"] == "x"
