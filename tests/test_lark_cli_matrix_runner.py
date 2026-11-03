import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_matrix_runner_self_test_handles_locked_sqlite():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "sqlite-lock"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "sqlite-lock ok" in proc.stdout


def test_matrix_runner_self_test_calendar_prompt_uses_official_shortcut():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "calendar-prompt"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "calendar-prompt ok" in proc.stdout


def test_matrix_runner_self_test_calendar_unavailable_is_not_pass():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "calendar-unavailable-verdict"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "calendar-unavailable-verdict ok" in proc.stdout


def test_matrix_runner_self_test_prompt_canaries_use_official_shortcuts():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "prompt-canaries"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "prompt-canaries ok" in proc.stdout


def test_matrix_runner_self_test_success_text_with_no_error_is_pass():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "no-error-success-verdict"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "no-error-success-verdict ok" in proc.stdout


def test_matrix_runner_self_test_truncated_output_is_fail():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "truncated-output-verdict"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "truncated-output-verdict ok" in proc.stdout


def test_matrix_runner_self_test_mail_user_not_found_is_blocked():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "mail-user-not-found-blocked"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "mail-user-not-found-blocked ok" in proc.stdout


def test_matrix_runner_self_test_external_auth_management_is_blocked():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "external-auth-management-blocked"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "external-auth-management-blocked ok" in proc.stdout


def test_matrix_runner_self_test_base_url_artifact_is_pass():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "base-url-artifact"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "base-url-artifact ok" in proc.stdout


def test_matrix_runner_self_test_markdown_link_artifact_is_pass():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "markdown-link-artifact"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "markdown-link-artifact ok" in proc.stdout


def test_matrix_runner_self_test_openapi_schema_prompt_uses_raw_api_form():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "openapi-schema-prompt"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "openapi-schema-prompt ok" in proc.stdout


def test_matrix_runner_self_test_render_prompt_forbids_cached_answers():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "render-prompt-no-cache"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "render-prompt-no-cache ok" in proc.stdout


def test_matrix_runner_self_test_feishu_url_artifacts_are_pass():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "feishu-url-artifacts"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "feishu-url-artifacts ok" in proc.stdout


def test_matrix_runner_self_test_user_not_found_is_blocked():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "user-not-found-blocked"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "user-not-found-blocked ok" in proc.stdout


def test_matrix_runner_self_test_external_credentials_auth_is_blocked():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "external-credentials-blocked"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "external-credentials-blocked ok" in proc.stdout


def test_matrix_runner_self_test_interim_tool_intent_is_fail():
    proc = subprocess.run(
        ["node", str(ROOT / "scripts/lark_cli_matrix_runner.mjs"), "--self-test", "interim-tool-intent"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert proc.returncode == 0, proc.stderr
    assert "interim-tool-intent ok" in proc.stdout
