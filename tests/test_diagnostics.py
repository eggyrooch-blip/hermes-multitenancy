from __future__ import annotations

from pathlib import Path


def _sample_credential(*, status: str = "valid") -> dict[str, object]:
    return {
        "profile": "owner",
        "provider": "feishu",
        "subject_id": "ou_owner",
        "credential_kind": "uat",
        "status": status,
        "storage": "multitenancy_db",
        "expires_at": 1_900_000_000_000,
        "refresh_expires_at": 1_900_000_100_000,
        "scopes": ["contact:user.base:readonly"],
        "missing_scopes": [],
        "has_credential": True,
        "sandbox_note": ".env/auth.json are masked by design",
    }


def _sample_health(*, ok: bool = True) -> dict[str, object]:
    return {
        "ready": ok,
        "checks": [
            {
                "name": "lark_cli_registration",
                "ok": ok,
                "status": "registered" if ok else "unavailable",
                "owner": "multitenancy_runtime",
            }
        ],
        "attention": [] if ok else ["lark_cli_registration"],
        "secret_free": True,
    }


def _sample_env() -> dict[str, str]:
    return {
        "python_version": "3.12.0",
        "platform": "darwin-arm64",
    }


def test_build_doctor_markdown_is_non_empty_in_both_locales():
    from hermes_multitenancy.diagnostics import build_doctor_markdown

    zh = build_doctor_markdown(
        version="0.1.0",
        profile_name="owner",
        credential_status=_sample_credential(),
        health=_sample_health(),
        env=_sample_env(),
        locale="zh_cn",
    )
    en = build_doctor_markdown(
        version="0.1.0",
        profile_name="owner",
        credential_status=_sample_credential(),
        health=_sample_health(),
        env=_sample_env(),
        locale="en_us",
    )

    assert zh.strip()
    assert "插件版本" in zh
    assert "凭证状态" in zh
    assert en.strip()
    assert "Plugin Version" in en
    assert "Credential Status" in en


def test_mask_secret_never_leaks_full_value():
    from hermes_multitenancy.diagnostics import _mask_secret

    assert _mask_secret("", locale="en_us") == "(not set)"
    assert _mask_secret(None, locale="zh_cn") == "(未设置)"
    assert _mask_secret("abcd", locale="en_us") == "****"
    masked = _mask_secret("secret-value", locale="en_us")
    assert masked == "secr****"
    assert "secret-value" not in masked


def test_build_diagnose_report_contains_required_keys():
    from hermes_multitenancy.diagnostics import build_diagnose_report

    report = build_diagnose_report(
        version="0.1.0",
        profile_name="owner",
        credential_status=_sample_credential(),
        health=_sample_health(),
        env=_sample_env(),
    )

    assert report["overall"] == "healthy"
    assert report["environment"]["python_version"] == "3.12.0"
    assert report["environment"]["platform"] == "darwin-arm64"
    assert report["credential"]["status"] == "valid"
    assert report["capability"]["ready"] is True
    assert "recent_errors" in report["notes"]
    assert "message_trace" in report["notes"]


def test_render_diagnose_markdown_is_non_empty_in_both_locales():
    from hermes_multitenancy.diagnostics import (
        build_diagnose_report,
        render_diagnose_markdown,
    )

    report = build_diagnose_report(
        version="0.1.0",
        profile_name="owner",
        credential_status=_sample_credential(),
        health=_sample_health(),
        env=_sample_env(),
    )

    zh = render_diagnose_markdown(report, "zh_cn")
    en = render_diagnose_markdown(report, "en_us")

    assert zh.strip()
    assert "总体结论" in zh
    assert en.strip()
    assert "Overall Verdict" in en


def test_overall_verdict_is_deterministic():
    from hermes_multitenancy.diagnostics import build_diagnose_report

    healthy = build_diagnose_report(
        version="0.1.0",
        profile_name="owner",
        credential_status=_sample_credential(status="valid"),
        health=_sample_health(ok=True),
        env=_sample_env(),
    )
    degraded = build_diagnose_report(
        version="0.1.0",
        profile_name="owner",
        credential_status={**_sample_credential(status="scope_missing"), "missing_scopes": ["scope:a"]},
        health=_sample_health(ok=True),
        env=_sample_env(),
    )
    unhealthy = build_diagnose_report(
        version="0.1.0",
        profile_name="owner",
        credential_status=_sample_credential(status="expired"),
        health={"status": "unavailable", "error": "boom", "checks": [], "attention": []},
        env=_sample_env(),
    )

    assert healthy["overall"] == "healthy"
    assert degraded["overall"] == "degraded"
    assert unhealthy["overall"] == "unhealthy"


def test_plugin_version_reads_manifest():
    from hermes_multitenancy.diagnostics import plugin_version

    assert plugin_version() == "0.1.0"


def test_collect_runtime_inputs_degrades_gracefully_without_env(monkeypatch):
    from hermes_multitenancy.diagnostics import collect_runtime_inputs

    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_SHARED_HOME", raising=False)
    monkeypatch.delenv("HERMES_FEISHU_USER_OPEN_ID", raising=False)

    inputs = collect_runtime_inputs()

    assert inputs["version"]
    assert "credential_status" in inputs
    assert "health" in inputs
    assert inputs["env"]["python_version"]
    assert inputs["env"]["platform"]


def test_parse_command_supports_doctor_and_diagnose():
    from hermes_multitenancy.commands import parse_command

    assert parse_command("/doctor") == ("doctor", "")
    assert parse_command("/diagnose") == ("diagnose", "")
