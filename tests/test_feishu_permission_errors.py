from __future__ import annotations

import json

from hermes_multitenancy.feishu_permission_errors import (
    annotate_permission_error,
    build_permission_url,
    classify_lark_error,
)


def _hint_url(stderr_redacted: str) -> str:
    """The admin URL is the last token after the fullwidth colon in the hint."""
    return stderr_redacted.rsplit("：", 1)[1]


def test_success_result_with_coincidental_code_is_never_touched():
    """Gate 1 (round-2's worst regression): a successful command (ok is True)
    with 99991672 sitting in a non-code field is returned byte-for-byte."""
    success = {
        "ok": True,
        "json": {"code": 0, "data": {"chat_id": "99991672", "elapsed_ms": 99991679}},
        "stdout": "",
        "stderr_redacted": "same bytes",
    }

    assert annotate_permission_error(success, app_id="cli_x") is success
    assert success["stderr_redacted"] == "same bytes"


def test_failure_app_scope_missing_appends_admin_hint_generic_link():
    """ok False + structured code 99991672 → admin hint + app link, NO scope name."""
    result = {"ok": False, "json": {"code": 99991672}, "stdout": "", "stderr_redacted": "orig"}

    annotated = annotate_permission_error(result, app_id="cli_demo123")

    assert annotated is not result
    assert result["stderr_redacted"] == "orig"  # input untouched
    assert annotated["stderr_redacted"].startswith("orig\n")
    assert "只有管理员" in annotated["stderr_redacted"]
    assert "/app/cli_demo123/auth?token_type=user" in annotated["stderr_redacted"]
    assert "q=" not in annotated["stderr_redacted"]  # no scope name anywhere


def test_failure_user_scope_insufficient_appends_auth_hint_no_admin_link():
    """ok False + structured code 99991679 → /auth hint, no admin link."""
    result = {"ok": False, "json": {"code": 99991679}, "stdout": "", "stderr_redacted": "stderr"}

    annotated = annotate_permission_error(result, app_id="cli_demo123")

    assert "需要重新授权" in annotated["stderr_redacted"]
    assert "/auth" in annotated["stderr_redacted"]
    assert "/app" not in annotated["stderr_redacted"]


def test_top_level_code_beats_nested():
    """Top-level 99991679 wins over nested data.code 99991672; nested only
    applies when the top level is absent or not a permission code."""
    assert classify_lark_error({"code": 99991679, "data": {"code": 99991672}}) == "user_scope_insufficient"
    assert classify_lark_error({"code": 0, "data": {"code": 99991672}}) == "app_scope_missing"
    assert classify_lark_error({"error": {"code": "99991679"}}) == "user_scope_insufficient"
    # depth limit: 2 levels max
    assert classify_lark_error({"data": {"data": {"data": {"code": 99991672}}}}) is None


def test_no_text_scanning_prose_only_codes_do_not_classify():
    """STRUCTURED FIELDS ONLY (sunke decision 2026-07-10 after 4 non-converging
    review rounds): a permission code living only in prose — stdout text, stderr
    text, a json string field, or a non-dict payload — gets NO hint. Status quo
    behaviour, never a wrong hint."""
    # code only in a json string field
    prose_dict = {
        "ok": False,
        "json": {"ok": False, "error": "permission denied (99991672)"},
        "stdout": "",
        "stderr_redacted": "keep",
    }
    assert annotate_permission_error(prose_dict, app_id="cli_x") is prose_dict

    # code only in stderr text
    stderr_only = {"ok": False, "json": None, "stdout": "", "stderr_redacted": "错误 99991672 请联系管理员"}
    assert annotate_permission_error(stderr_only, app_id="cli_x") is stderr_only

    # code only in retained stdout text (parse failure)
    stdout_only = {"ok": False, "json": None, "stdout": "权限不足99991672", "stderr_redacted": ""}
    assert annotate_permission_error(stdout_only, app_id="cli_x") is stdout_only

    # non-dict payloads never classify
    assert classify_lark_error([{"code": 99991672}]) is None
    assert classify_lark_error("99991672") is None
    assert classify_lark_error(None) is None


def test_legitimate_non_permission_code_is_definitive():
    """A structured non-permission code (e.g. rate-limit 230020) is a definitive
    answer — nothing may reclassify it: not surrounding text, and not a nested
    error/data code either (the final-review counterexample)."""
    result = {
        "ok": False,
        "json": {"code": 230020, "msg": "rate limited"},
        "stdout": "",
        "stderr_redacted": "request_id: 99991672",  # coincidental digits nearby
    }
    assert annotate_permission_error(result, app_id="cli_x") is result
    assert classify_lark_error({"code": 230020}) is None

    # nested permission code must NOT override a definitive top-level code
    assert classify_lark_error({"code": 230020, "data": {"code": 99991672}}) is None
    assert classify_lark_error({"code": 230020, "error": {"code": 99991679}}) is None
    # ...but a code:0 success-shell wrapping a nested error still descends
    assert classify_lark_error({"code": 0, "data": {"code": 99991672}}) == "app_scope_missing"


def test_numeric_edge_cases_never_crash_and_floats_never_match():
    """Type allowlist: int/str only. Floats (inf → OverflowError under bare
    int(); 99991672.9 → truncation false match) are rejected by construction;
    oversized digit strings and Unicode lookalikes return None."""
    crashers = {
        "ok": False,
        "json": {"code": float("inf"), "error": {"code": "9" * 5000}, "data": {"code": "9999167²"}},
        "stdout": "",
        "stderr_redacted": "z",
    }
    assert annotate_permission_error(crashers) is crashers  # no crash, unchanged

    assert classify_lark_error({"code": float("-inf")}) is None
    assert classify_lark_error({"code": float("nan")}) is None
    assert classify_lark_error({"code": 99991672.0}) is None  # even exact-value floats
    assert classify_lark_error({"code": 99991672.9}) is None
    assert classify_lark_error({"code": True}) is None  # bool is not a code

    # ASCII-digit contract is exact: int()-parseable lookalike shapes rejected
    assert classify_lark_error({"code": "+99991672"}) is None
    assert classify_lark_error({"code": "99_991_672"}) is None
    assert classify_lark_error({"code": "９９９９１６７２"}) is None  # full-width digits


def test_missing_or_dirty_app_id_degrades_to_generic_link():
    """app_id lands in a URL path: absent or dirty values degrade to the generic
    console URL; a well-formed id builds the per-app auth page."""
    result = {"ok": False, "json": {"code": 99991672}, "stdout": "", "stderr_redacted": ""}

    text = annotate_permission_error(result, app_id=None)["stderr_redacted"]
    assert "只有管理员" in text
    url = _hint_url(text)
    assert url.endswith("/app")
    assert url.isascii()

    for dirty in ("cli_x?redirect=evil", "../app", "cli x", "cli_x#frag", "cli/../x", "应用"):
        assert build_permission_url(dirty).endswith("/app"), dirty

    assert build_permission_url("cli_a1B2-c3_d").endswith("/app/cli_a1B2-c3_d/auth?token_type=user")

    # oversized app_id (codex concern): bounded, degrades to generic link
    # instead of bloating the user-visible hint.
    assert build_permission_url("c" * 100_000).endswith("/app")
    assert build_permission_url("c" * 64).endswith("/app/" + "c" * 64 + "/auth?token_type=user")
    assert build_permission_url("c" * 65).endswith("/app")


def test_open_domain_follows_feishu_open_base_url_env(monkeypatch):
    """The domain follows the FEISHU_OPEN_BASE_URL convention (not a hardcode);
    a larksuite tenant gets its own host, and a trailing slash is normalized."""
    monkeypatch.setenv("FEISHU_OPEN_BASE_URL", "https://open.larksuite.com/")

    assert build_permission_url("cli_x") == "https://open.larksuite.com/app/cli_x/auth?token_type=user"

    result = {"ok": False, "json": {"code": 99991672}, "stdout": "", "stderr_redacted": ""}
    url = _hint_url(annotate_permission_error(result, app_id="cli_x")["stderr_redacted"])
    assert url == "https://open.larksuite.com/app/cli_x/auth?token_type=user"


def test_dirty_open_base_url_falls_back_to_default(monkeypatch):
    """The env-derived domain lands verbatim in user-visible output AFTER the
    redaction pipeline, so anything that is not a bare https://host[:port]
    (query, userinfo, fragment, http://) falls back to the default."""
    for dirty in (
        "https://evil.example.com?proxy_key=SECRET",
        "https://user:pass@open.feishu.cn",
        "http://open.feishu.cn",
        "https://open.feishu.cn/#frag",
        "not a url",
    ):
        monkeypatch.setenv("FEISHU_OPEN_BASE_URL", dirty)
        assert build_permission_url("cli_x") == "https://open.feishu.cn/app/cli_x/auth?token_type=user", dirty
    # bare host with port is allowed
    monkeypatch.setenv("FEISHU_OPEN_BASE_URL", "https://feishu.example.com:8443")
    assert build_permission_url("cli_x").startswith("https://feishu.example.com:8443/app/")


def _run_handler_with(monkeypatch, tmp_path, *, returncode: int, stdout: str, stderr: str = ""):
    """Drive the REAL _handle_lark_cli_execute with a mocked subprocess, mirroring
    tests/test_lark_cli_tool_registration.py's fixture pattern."""
    from hermes_multitenancy import lark_cli_tool

    binary = tmp_path / "lark-cli-authsidecar"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    profile = tmp_path / "profile"
    workspace = profile / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HERMES_LARK_CLI_BIN", str(binary))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("LARKSUITE_CLI_DEFAULT_AS", "user")
    monkeypatch.setenv("LARKSUITE_CLI_APP_ID", "cli_test123")

    class Completed:
        pass

    Completed.returncode = returncode
    Completed.stdout = stdout
    Completed.stderr = stderr
    monkeypatch.setattr(lark_cli_tool.subprocess, "run", lambda *_a, **_k: Completed())

    raw = lark_cli_tool._handle_lark_cli_execute(
        {
            "mode": "shortcut",
            "argv": ["base", "+base-create", "--name", "permission-annotation-smoke"],
            "identity": "auto",
            "risk": "write",
            "reason": "integration test",
        }
    )
    return raw if isinstance(raw, dict) else json.loads(raw)


def test_handler_integration_structured_code_gets_admin_hint(monkeypatch, tmp_path):
    """END-TO-END through the real handler: failed subprocess + structured 99991672
    → tool result carries the admin hint with the env-derived app deep-link."""
    result = _run_handler_with(
        monkeypatch,
        tmp_path,
        returncode=1,
        stdout=json.dumps({"code": 99991672, "msg": "forbidden"}),
    )

    assert result["ok"] is False
    assert "只有管理员" in result["stderr_redacted"]
    assert "/app/cli_test123/auth?token_type=user" in result["stderr_redacted"]


def test_handler_integration_prose_failure_untouched_and_stdout_display_preserved(monkeypatch, tmp_path):
    """END-TO-END: failed subprocess with non-JSON prose stdout → NO hint (by
    decision), and the original parsed-is-None stdout display path is preserved
    byte-for-byte (retention condition is unchanged from main)."""
    prose = "权限不足99991672请重新授权"
    result = _run_handler_with(monkeypatch, tmp_path, returncode=1, stdout=prose)

    assert result["ok"] is False
    assert result["stdout"] == prose  # parsed is None → stdout retained for display
    assert "只有管理员" not in result["stderr_redacted"]
    assert "/auth" not in result["stderr_redacted"]


def test_handler_integration_success_passthrough(monkeypatch, tmp_path):
    """END-TO-END: successful subprocess → result shape identical to pre-change
    behaviour (no annotation, stdout blanked for dict payloads as before)."""
    result = _run_handler_with(
        monkeypatch,
        tmp_path,
        returncode=0,
        stdout=json.dumps({"ok": True, "data": {"id": "99991672"}}),
    )

    assert result["ok"] is True
    assert result["stdout"] == ""  # dict payload → blanked, as on main
    assert "只有管理员" not in (result["stderr_redacted"] or "")
