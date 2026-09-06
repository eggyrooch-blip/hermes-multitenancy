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


def _invoke(argv, mode="shortcut", **extra):
    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": mode,
            "argv": argv,
            "identity": "user",
            "risk": "read",
            "reason": "test",
            **extra,
        }
    )
    return raw if isinstance(raw, dict) else json.loads(raw)


def _setup_sequence(monkeypatch, tmp_path, payloads):
    captured = _setup(monkeypatch, tmp_path, stdout="")
    responses = iter(payloads)

    def fake_run(command, **kwargs):
        captured.setdefault("commands", []).append(command)
        captured.setdefault("kwargs_sequence", []).append(kwargs)

        class Completed:
            returncode = 0
            stdout = json.dumps(next(responses))
            stderr = ""

        return Completed()

    monkeypatch.setattr(lark_cli_tool.subprocess, "run", fake_run)
    return captured


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

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_OUTPUT_UNPARSEABLE"
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


def test_unstructured_document_read_cannot_claim_complete(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, stdout="# Doc title\n\nplain markdown body\n")

    result = _invoke(["docs", "get", "doccnFake", "--format", "md"])

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_OUTPUT_UNPARSEABLE"
    assert result["read_incomplete_reason"] == "output_unparseable"
    assert "plain markdown body" in result["stdout"]


@pytest.mark.parametrize(
    ("format_args", "stdout"),
    [
        (["--format", "table"], "NODE_TOKEN  TITLE\nnode-1  First page\n"),
        (["--format", "csv"], "node_token,title\nnode-1,First page\n"),
        (["--format", "ndjson"], '{"node_token":"node-1"}\n'),
        (["--jq", ".data.nodes[].title"], '"First page"\n'),
        (["-q", ".data.nodes"], '[{"node_token":"node-1"}]\n'),
        (["-q=.data.nodes"], '[{"node_token":"node-1"}]\n'),
        (["-q.data.nodes[0]"], '{"node_token":"node-1"}\n'),
        (["-q.data"], '{"nodes":[{"node_token":"node-1"}]}\n'),
    ],
)
def test_read_terminal_fails_closed_when_shortcut_output_is_not_structured_json(
    monkeypatch,
    tmp_path,
    format_args,
    stdout,
):
    _setup(monkeypatch, tmp_path, stdout=stdout)

    result = _invoke(["wiki", "+node-list", "--space-id", "space-1", *format_args])

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_OUTPUT_UNPARSEABLE"
    assert result["read_incomplete_reason"] == "output_unparseable"


def test_read_terminal_fails_closed_when_api_output_uses_projection(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, stdout="user_id,name\nou_1,Example\n")

    result = _invoke(
        ["GET", "/open-apis/contact/v3/users", "--format", "csv"],
        mode="api",
    )

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_OUTPUT_UNPARSEABLE"
    assert result["read_incomplete_reason"] == "output_unparseable"


@pytest.mark.parametrize("stdout", ['[{"has_more": true}]', "true"])
def test_read_terminal_rejects_non_object_json(monkeypatch, tmp_path, stdout):
    _setup(monkeypatch, tmp_path, stdout=stdout)

    result = _invoke(["wiki", "+node-list", "--space-id", "space-1"])

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_OUTPUT_UNPARSEABLE"
    assert result["read_incomplete_reason"] == "output_unparseable"


def test_read_terminal_cannot_be_bypassed_by_schema_mode_label(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, stdout="NODE_TOKEN  TITLE\nnode-1  First page\n")

    result = _invoke(
        ["wiki", "+node-list", "--space-id", "space-1"],
        mode="schema",
    )

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_OUTPUT_UNPARSEABLE"
    assert result["read_incomplete_reason"] == "output_unparseable"


@pytest.mark.parametrize("argument_value", ["help", "-h", "--help"])
def test_read_terminal_does_not_treat_help_argument_value_as_control(
    monkeypatch,
    tmp_path,
    argument_value,
):
    captured = _setup(monkeypatch, tmp_path, stdout="NODE_TOKEN  TITLE\nnode-1  First page\n")

    result = _invoke(
        ["wiki", "+node-list", "--space-id", argument_value],
        mode="schema",
    )

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_OUTPUT_UNPARSEABLE"
    assert captured["command"][-2:] == ["--as", "user"]


@pytest.mark.parametrize(
    ("mode", "argv"),
    [
        ("shortcut", ["doctor"]),
        ("shortcut", ["schema", "wiki"]),
        ("schema", ["schema", "wiki"]),
        ("shortcut", ["help", "wiki"]),
        ("shortcut", ["wiki", "+node-list", "--help"]),
    ],
)
def test_control_and_diagnostic_commands_remain_textual(monkeypatch, tmp_path, mode, argv):
    _setup(monkeypatch, tmp_path, stdout="diagnostic output\n")

    result = _invoke(argv, mode=mode)

    assert result["ok"] is True
    assert result["read_complete"] is True
    assert result["error_code"] is None
    assert result["stdout"] == "diagnostic output"


@pytest.mark.parametrize(
    ("argv", "expected_command"),
    [
        (["auth", "status"], ["auth", "status"]),
        (["auth", "list"], ["auth", "list"]),
        (["auth", "check", "--scope", "x"], ["auth", "check", "--scope", "x"]),
        (["skills", "list"], ["skills", "list"]),
        (["skills", "read", "lark-wiki"], ["skills", "read", "lark-wiki"]),
        (["whoami"], ["whoami", "--as", "user"]),
        (["config", "show"], ["config", "show"]),
        (["config", "default-as"], ["config", "default-as"]),
        (["profile", "list"], ["profile", "list"]),
    ],
)
def test_formatless_control_inventory_remains_textual(
    monkeypatch,
    tmp_path,
    argv,
    expected_command,
):
    captured = _setup(monkeypatch, tmp_path, stdout="diagnostic output\n")

    result = _invoke(argv)

    assert result["ok"] is True
    assert result["read_complete"] is True
    assert result["error_code"] is None
    assert result["stdout"] == "diagnostic output"
    assert captured["command"][1:] == expected_command


@pytest.mark.parametrize("output_format", ["pretty", "table"])
def test_auth_scopes_non_json_format_cannot_claim_read_complete(
    monkeypatch,
    tmp_path,
    output_format,
):
    captured = _setup(monkeypatch, tmp_path, stdout="scope diagnostic output\n")

    result = _invoke(["auth", "scopes", "--format", output_format])

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_OUTPUT_UNPARSEABLE"
    assert captured["command"][1:] == ["auth", "scopes", "--format", output_format]


def test_unknown_config_list_is_not_a_control_exemption(monkeypatch, tmp_path):
    captured = _setup(monkeypatch, tmp_path, stdout="diagnostic output\n")

    result = _invoke(["config", "list"])

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_OUTPUT_UNPARSEABLE"
    assert captured["command"][1:] == ["config", "list", "--format", "json", "--as", "user"]


def test_non_status_auth_command_is_blocked_before_identity_or_spawn(monkeypatch, tmp_path):
    captured = _setup(monkeypatch, tmp_path, stdout='{"ok": true}')

    result = _invoke(["auth", "login"])

    assert result["ok"] is False
    assert result["error_code"] == "FEISHU_AUTH_INTERACTIVE_BLOCKED"
    assert result["auth_required"] is True
    assert "command" not in captured


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


def test_read_terminal_marks_pending_page_incomplete(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        stdout=json.dumps(
            {"ok": True, "data": {"items": [{"id": "first"}], "has_more": True, "page_token": "next"}}
        ),
    )

    result = _invoke(APPROVAL_ARGV)

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_READ_INCOMPLETE"
    assert result["read_incomplete_reason"] == "pagination_remaining"


def test_read_terminal_marks_finite_page_all_cap_incomplete(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        stdout=json.dumps(
            {"ok": True, "data": {"items": [{"id": "tenth-page"}], "has_more": True, "page_token": "page-11"}}
        ),
    )

    result = _invoke(["wiki", "+node-list", "--page-all", "--page-limit", "10"])

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_READ_INCOMPLETE"
    assert result["read_incomplete_reason"] == "page_limit_reached"


def test_read_terminal_fails_closed_when_cursor_is_missing(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        stdout=json.dumps(
            {
                "ok": True,
                "data": {
                    "items": [{"id": "missing-cursor"}],
                    "has_more": True,
                },
            }
        ),
    )

    result = _invoke(APPROVAL_ARGV)

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_READ_CURSOR_MISSING"
    assert result["read_incomplete_reason"] == "cursor_missing"


def test_read_terminal_fails_closed_when_cursor_loops(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        stdout=json.dumps(
            {"ok": True, "data": {"items": [{"id": "same-page"}], "has_more": True, "page_token": "repeat"}}
        ),
    )

    result = _invoke(["wiki", "+node-list", "--page-token", "repeat"])

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_READ_CURSOR_LOOP"
    assert result["read_incomplete_reason"] == "cursor_loop"


def test_read_terminal_ignores_pagination_named_fields_in_business_rows(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        stdout=json.dumps(
            {
                "ok": True,
                "data": {
                    "items": [
                        {
                            "id": "record-1",
                            "fields": {
                                "has_more": True,
                                "page_token": "requested-page",
                            },
                        }
                    ],
                    "has_more": False,
                    "page_token": "",
                },
            }
        ),
    )

    result = _invoke(["bitable", "+records-list", "--page-token", "requested-page"])

    assert result["ok"] is True
    assert result["read_complete"] is True
    assert result["error_code"] is None


def test_read_terminal_inspects_root_protocol_envelope(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        stdout=json.dumps(
            {
                "ok": True,
                "has_more": True,
                "next_page_token": "root-page-2",
                "data": {
                    "roots": [
                        {"node": "complete", "has_more": False, "page_token": ""},
                        {"node": "partial", "children": {"has_more": True, "next_page_token": "business-value"}},
                    ]
                },
            }
        ),
    )

    result = _invoke(["wiki", "+node-list", "--page-all", "--page-limit", "0"])

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_READ_INCOMPLETE"
    assert result["read_incomplete_reason"] == "pagination_remaining"


def test_read_terminal_is_complete_only_when_all_nested_pages_are_exhausted(monkeypatch, tmp_path):
    _setup(
        monkeypatch,
        tmp_path,
        stdout=json.dumps(
            {
                "ok": True,
                "data": {
                    "has_more": False,
                    "page_token": "",
                    "pages": [
                        {"items": [{"id": "one"}], "has_more": False, "page_token": ""},
                        {"items": [{"id": "two"}], "children": {"has_more": False, "next_page_token": ""}},
                    ]
                },
            }
        ),
    )

    result = _invoke(["wiki", "+node-list", "--page-all", "--page-limit", "0"])

    assert result["ok"] is True
    assert result["read_complete"] is True
    assert result["error_code"] is None


def test_recursive_read_ignores_pagination_named_fields_in_node_content(monkeypatch, tmp_path):
    _setup_sequence(
        monkeypatch,
        tmp_path,
        [
            {
                "ok": True,
                "data": {
                    "nodes": [
                        {
                            "node_token": "node-1",
                            "has_child": False,
                            "metadata": {"has_more": True},
                        }
                    ],
                    "has_more": False,
                    "page_token": "",
                },
            }
        ],
    )

    result = _invoke(
        ["wiki", "+node-list", "--space-id", "space-1"],
        recursive_read=True,
    )

    assert result["ok"] is True
    assert result["read_complete"] is True
    assert result["read_requests"] == 1
    assert result["read_pending_count"] == 0


def test_recursive_read_drains_pages_and_children_discovered_on_later_pages(monkeypatch, tmp_path):
    captured = _setup_sequence(
        monkeypatch,
        tmp_path,
        [
            {
                "ok": True,
                "data": {
                    "nodes": [{"node_token": "node-first", "has_child": False}],
                    "has_more": True,
                    "page_token": "root-page-2",
                },
            },
            {
                "ok": True,
                "data": {
                    "nodes": [{"node_token": "node-parent", "has_child": True}],
                    "has_more": False,
                    "page_token": "",
                },
            },
            {
                "ok": True,
                "data": {
                    "nodes": [{"node_token": "node-leaf", "has_child": False}],
                    "has_more": False,
                    "page_token": "",
                },
            },
        ],
    )

    result = _invoke(
        ["wiki", "+node-list", "--space-id", "space-1"],
        recursive_read=True,
        recursive_read_limit=10,
    )

    assert result["ok"] is True
    assert result["read_complete"] is True
    assert result["read_requests"] == 3
    assert result["read_pending_count"] == 0
    assert [node["node_token"] for node in result["json"]["data"]["nodes"]] == [
        "node-first",
        "node-parent",
        "node-leaf",
    ]
    assert any("--page-token" in command and "root-page-2" in command for command in captured["commands"])
    assert any(
        "--parent-node-token" in command and "node-parent" in command
        for command in captured["commands"]
    )


def test_recursive_read_fails_closed_when_cursor_is_missing(monkeypatch, tmp_path):
    _setup_sequence(
        monkeypatch,
        tmp_path,
        [
            {
                "ok": True,
                "data": {
                    "nodes": [{"node_token": "node-first", "has_child": False}],
                    "has_more": True,
                },
            }
        ],
    )

    result = _invoke(
        ["wiki", "+node-list", "--space-id", "space-1"],
        recursive_read=True,
    )

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_READ_CURSOR_MISSING"
    assert result["read_incomplete_reason"] == "cursor_missing"
    assert result["read_pending_count"] == 1


def test_recursive_read_fails_closed_when_cursor_repeats(monkeypatch, tmp_path):
    _setup_sequence(
        monkeypatch,
        tmp_path,
        [
            {
                "ok": True,
                "data": {"nodes": [], "has_more": True, "page_token": "page-2"},
            },
            {
                "ok": True,
                "data": {"nodes": [], "has_more": True, "page_token": "page-2"},
            },
        ],
    )

    result = _invoke(
        ["wiki", "+node-list", "--space-id", "space-1"],
        recursive_read=True,
    )

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_READ_CURSOR_LOOP"
    assert result["read_incomplete_reason"] == "cursor_loop"
    assert result["read_pending_count"] == 1


def test_recursive_read_fails_closed_when_node_cycle_is_discovered(monkeypatch, tmp_path):
    _setup_sequence(
        monkeypatch,
        tmp_path,
        [
            {
                "ok": True,
                "data": {
                    "nodes": [{"node_token": "node-loop", "has_child": True}],
                    "has_more": False,
                    "page_token": "",
                },
            },
            {
                "ok": True,
                "data": {
                    "nodes": [{"node_token": "node-loop", "has_child": True}],
                    "has_more": False,
                    "page_token": "",
                },
            },
        ],
    )

    result = _invoke(
        ["wiki", "+node-list", "--space-id", "space-1"],
        recursive_read=True,
    )

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_READ_NODE_LOOP"
    assert result["read_incomplete_reason"] == "node_loop"
    assert result["read_pending_count"] == 1


def test_recursive_read_fails_closed_with_pending_queue_at_request_limit(monkeypatch, tmp_path):
    captured = _setup_sequence(
        monkeypatch,
        tmp_path,
        [
            {
                "ok": True,
                "data": {"nodes": [], "has_more": True, "page_token": "page-2"},
            }
        ],
    )

    result = _invoke(
        ["wiki", "+node-list", "--space-id", "space-1"],
        recursive_read=True,
        recursive_read_limit=1,
    )

    assert result["ok"] is False
    assert result["read_complete"] is False
    assert result["error_code"] == "FEISHU_READ_LIMIT_REACHED"
    assert result["read_incomplete_reason"] == "read_limit_reached"
    assert result["read_requests"] == 1
    assert result["read_pending_count"] == 1
    assert len(captured["commands"]) == 1
