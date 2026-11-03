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
    # The dev-speak "no cheap data source, omitted" notes were removed.
    assert "notes" not in report
    # New structured keys for identity + multitenancy.
    assert "identity" in report
    assert "multitenancy" in report


def test_diagnose_identifies_user_and_real_credential_validity():
    """When the invoking user is known, the report names them and shows real
    credential validity instead of the no-subject skip."""
    from hermes_multitenancy.diagnostics import (
        build_diagnose_report,
        render_diagnose_markdown,
    )

    report = build_diagnose_report(
        version="0.1.0",
        profile_name="feishu_sunke",
        credential_status={"status": "expired", "has_credential": True},
        health=_sample_health(),
        env=_sample_env(),
        identity={"open_id": "ou_abc", "name": "孙可", "profile": "feishu_sunke"},
        multitenancy={"kind": "user", "profile": "feishu_sunke", "owner_open_id": None, "agent_id": None},
    )
    md = render_diagnose_markdown(report, "zh_cn")
    assert report["identity"]["name"] == "孙可"
    assert "孙可" in md                              # names the user
    assert "ou_abc" not in md                        # never show raw open_id
    assert "已过期" in md                            # real validity word, not "已跳过"
    assert "多租户状态" in md                         # multitenancy section present
    assert "无廉价数据源" not in md                   # dev-speak gone
    assert "subject_id is required" not in md        # no raw internal error
    assert report["overall"] == "unhealthy"          # expired cred → unhealthy (real)


def test_diagnose_lists_owned_agents_by_kind_and_owner_name():
    from hermes_multitenancy.diagnostics import build_diagnose_report, render_diagnose_markdown

    report = build_diagnose_report(
        version="0.1.0",
        profile_name="feishu_sunke",
        credential_status={"status": "valid", "has_credential": True},
        health=_sample_health(),
        env=_sample_env(),
        identity={"open_id": "ou_x", "name": "孙可", "profile": "feishu_sunke", "hide_open_id": True},
        multitenancy={
            "kind": "user",
            "profile": "feishu_sunke",
            "owner_open_id": "ou_owner",
            "owner_name": "李雷",
            "agents": [
                {"kind": "group", "profile": "feishu_group_abc", "label": "IT 组"},
                {"kind": "agent", "profile": "feishu_sunke_bot2", "label": "助手2"},
            ],
        },
    )
    md = render_diagnose_markdown(report, "zh_cn")
    assert "我的 Agent 数量**: 2" in md
    assert "群聊 Agent" in md and "智能体" in md       # grouped by kind
    assert "李雷" in md and "ou_owner" not in md       # owner by name, no open_id


def test_diagnose_renders_agent_friendly_names():
    """Each agent renders its readable name (group chat name / 智能体 display
    name), not the raw chat_id / profile id."""
    from hermes_multitenancy.diagnostics import build_diagnose_report, render_diagnose_markdown

    report = build_diagnose_report(
        version="0.1.0",
        profile_name="feishu_sunke",
        credential_status={"status": "valid", "has_credential": True},
        health=_sample_health(),
        env=_sample_env(),
        identity={"open_id": "ou_x", "name": "孙可", "profile": "feishu_sunke", "hide_open_id": True},
        multitenancy={
            "kind": "user",
            "profile": "feishu_sunke",
            "agents": [
                {"kind": "group", "profile": "feishu_group_dfe", "chat_id": "oc_dfe", "name": "IT 运维组"},
                {"kind": "agent", "profile": "webui_abc_codex", "chat_id": None, "name": "codex_verify"},
            ],
        },
    )
    md = render_diagnose_markdown(report, "zh_cn")
    assert "IT 运维组" in md                 # group chat name, not oc_dfe
    assert "oc_dfe" not in md                 # raw chat_id hidden
    assert "codex_verify" in md              # agent friendly name


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


def test_no_subject_credential_error_is_treated_as_skipped():
    from hermes_multitenancy.diagnostics import (
        build_diagnose_report,
        build_doctor_markdown,
        render_diagnose_markdown,
    )

    credential_status = {
        "error": "subject_id is required when HERMES_FEISHU_USER_OPEN_ID is unset",
    }
    report = build_diagnose_report(
        version="0.1.0",
        profile_name="owner",
        credential_status=credential_status,
        health=_sample_health(ok=True),
        env=_sample_env(),
    )
    doctor_zh = build_doctor_markdown(
        version="0.1.0",
        profile_name="owner",
        credential_status=credential_status,
        health=_sample_health(ok=True),
        env=_sample_env(),
        locale="zh_cn",
    )
    doctor_en = build_doctor_markdown(
        version="0.1.0",
        profile_name="owner",
        credential_status=credential_status,
        health=_sample_health(ok=True),
        env=_sample_env(),
        locale="en_us",
    )
    diagnose_zh = render_diagnose_markdown(report, "zh_cn")
    diagnose_en = render_diagnose_markdown(report, "en_us")

    assert report["overall"] == "healthy"
    assert report["credential"]["error"] is None
    assert "未指定用户，已跳过个人凭证检查" in doctor_zh
    assert "No user context" in doctor_en
    assert "未指定用户，已跳过个人凭证检查" in diagnose_zh
    assert "No user context" in diagnose_en
    assert "subject_id is required" not in doctor_zh
    assert "subject_id is required" not in doctor_en
    assert "subject_id is required" not in diagnose_zh
    assert "subject_id is required" not in diagnose_en


def test_genuine_credential_error_remains_unhealthy_and_visible():
    from hermes_multitenancy.diagnostics import (
        build_diagnose_report,
        build_doctor_markdown,
        render_diagnose_markdown,
    )

    credential_status = {"error": "CredentialStoreError"}
    report = build_diagnose_report(
        version="0.1.0",
        profile_name="owner",
        credential_status=credential_status,
        health=_sample_health(ok=True),
        env=_sample_env(),
    )
    doctor = build_doctor_markdown(
        version="0.1.0",
        profile_name="owner",
        credential_status=credential_status,
        health=_sample_health(ok=True),
        env=_sample_env(),
        locale="en_us",
    )
    diagnose = render_diagnose_markdown(report, "en_us")

    assert report["overall"] == "unhealthy"
    assert report["credential"]["error"] == "CredentialStoreError"
    assert "CredentialStoreError" in doctor
    assert "CredentialStoreError" in diagnose


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
