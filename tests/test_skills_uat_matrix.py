from __future__ import annotations

import importlib.util
import time
import urllib.error
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "scripts" / "skills_uat_matrix.py"


def _load_matrix_module():
    spec = importlib.util.spec_from_file_location("skills_uat_matrix", MATRIX_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_distribution_uat_proves_symlink_version_rollback(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_distribution_and_versions(tmp_path)

    assert result["manifest_version"] == "v2"
    assert result["rollback_manifest_version"] == "v1"
    assert result["stable_profile_skill_path"] == "weather/shared"
    assert result["rollback_weather_target"].endswith("skill-releases/weather/v1")


def test_distribution_uat_proves_brokered_lark_skill_is_create_ready(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_distribution_and_versions(tmp_path)

    assert result["lark_calendar_install_mode"] == "symlink"
    assert result["lark_calendar_token_policy"] == "brokered"
    assert result["lark_calendar_share_with_children"] is True


def test_profile_user_audience_distribution_uat(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_profile_user_audience_distribution(tmp_path)

    assert result["profile_audience_profile"] == "alice"
    assert result["user_audience_user"] == "bob"
    assert result["carol_received_targeted_skill"] is False


def test_hermes_loader_uat_discovers_symlinked_weather_and_lark_skills(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_hermes_loader_discovers_symlinked_skills(tmp_path)

    assert result["loader_checked"] is True
    assert result["discovered_count"] == 2
    assert result["weather_skill_discovered"] is True
    assert result["lark_skill_discovered"] is True
    assert result["discovered_relative_paths"] == [
        "skills/lark-calendar/SKILL.md",
        "skills/weather/shared/SKILL.md",
    ]


def test_new_hire_sync_auto_installs_managed_skills_and_preserves_personal_installs(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_new_hire_sync_auto_installs_managed_skills(tmp_path)

    assert result["initial_stats"]["created"] == 1
    assert result["new_hire_stats"]["created"] == 1
    assert result["new_hire_profile_created"] is True
    assert result["new_hire_weather_install_mode"] == "symlink"
    assert result["new_hire_weather_version"] == "v2"
    assert result["new_hire_lark_calendar_token_policy"] == "brokered"
    assert result["new_hire_lark_calendar_share_with_children"] is True
    assert result["new_hire_finance_skill"] is True
    assert result["new_hire_personal_install_preserved_after_resync"] is True


def test_webui_child_agent_inherits_shareable_skills_not_tokens(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_webui_child_agent_inherits_skills_not_tokens(tmp_path)

    assert result["webui_child_profile"] == "webui_child_research"
    assert result["inherited_from"] == "alice"
    assert result["weather_skill"] is True
    assert result["lark_calendar_skill"] is True
    assert result["personal_oauth_skill"] is False
    assert result["token_files"] == 0
    assert result["uat_files"] == 0


def test_group_child_inherits_upstream_skill_version_updates_one_way(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_group_child_inherits_upstream_skill_version_updates_one_way(tmp_path)

    assert result["group_profile"] == "feishu_group_weather_versions"
    assert result["initial_weather_version"] == "v1"
    assert result["updated_weather_version"] == "v2"
    assert result["stable_group_skill_path"] == "weather/shared"
    assert result["group_weather_target_after_update"].endswith("skill-releases/weather/v2")
    assert result["owner_received_group_install"] is False
    assert result["group_token_files"] == 0


def test_registry_audit_uat_collects_all_profile_skill_sources(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_registry_audit_and_loop_guard(tmp_path)

    assert result["profile_count"] >= 2
    assert result["audited_profiles"] >= 2
    assert result["source_counts"]["managed"] >= 1
    assert result["source_counts"]["personal"] >= 1
    assert result["source_counts"]["unknown"] >= 1


def test_continue_turn_reconstructs_interrupted_request(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_continue_turn_reconstructs_interrupted_request(tmp_path)

    assert result["continue_used_previous_request"] is True
    assert result["continue_response"] == "continued-weather-report-from-interrupted-request"
    assert any("中断或取消" in content for content in result["continue_history_before_response"])


def test_real_home_skill_inventory_is_secret_free(tmp_path: Path):
    matrix_mod = _load_matrix_module()
    real_home = tmp_path / "real-home"
    shared_skill = real_home / "skills" / "org" / "weather"
    shared_skill.mkdir(parents=True)
    (shared_skill / "SKILL.md").write_text("# Weather\n", encoding="utf-8")
    profile = real_home / "profiles" / "alice"
    managed = profile / "skills" / "org" / "weather"
    managed.parent.mkdir(parents=True)
    managed.symlink_to(shared_skill, target_is_directory=True)
    personal = profile / "skills" / "personal" / "scratch"
    personal.mkdir(parents=True)
    (personal / "SKILL.md").write_text("# Scratch\n", encoding="utf-8")
    unknown = profile / "skills" / "local-tool"
    unknown.mkdir(parents=True)
    (unknown / "SKILL.md").write_text("# Local\n", encoding="utf-8")
    token_secret = "super-secret-token-value"
    token_file = profile / "tokens" / "scratch.token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text(token_secret, encoding="utf-8")
    (profile / "skills" / ".hermes-managed.json").write_text(
        '{"version":1,"skills":{"org/weather":{"source":"managed","target":"/shared/weather","version":"v1"}}}',
        encoding="utf-8",
    )
    (profile / "skills" / ".hermes-personal-installs.json").write_text(
        '{"version":1,"skills":{"personal/scratch":{"source":"personal","target":"/hub/scratch"}}}',
        encoding="utf-8",
    )

    result = matrix_mod.case_real_home_skill_inventory(real_home)

    assert result["checked"] is True
    assert result["secret_free"] is True
    assert result["profile_count"] == 1
    assert result["audited_profiles"] == 1
    assert result["total_skills"] == 3
    assert result["token_file_marker_count"] == 1
    assert result["source_counts"]["managed"] == 1
    assert result["source_counts"]["personal"] == 1
    assert result["source_counts"]["unknown"] == 1
    assert token_secret not in str(result)


def test_interruption_resume_uat_accepts_arbitrary_followup_not_magic_continue(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_interruption_arbitrary_followup_context(tmp_path)

    assert result["followup_text"] == "刚才那个报告还在吗？接着跑"
    assert result["magic_continue_required"] is False
    assert result["interruption_marker"] is True
    assert result["interrupted_request_visible_to_followup"] is True
    assert result["interruption_marker_visible_to_followup"] is True


def test_production_feedback_interruption_quote_maps_to_executable_uat(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_production_feedback_interruption_quote_resume(tmp_path)

    assert result["first_problem_exact_feedback_covered"] is True
    assert result["followup_text"] == "我得说点啥，才能让他继续"
    assert result["magic_continue_required"] is False
    assert result["continue_used_previous_request"] is True
    assert result["interrupted_request_visible_to_followup"] is True
    assert result["interruption_marker_visible_to_followup"] is True
    assert result["feedback_phrase_coverage"]["执行一半突然就没了"] is True
    assert result["feedback_phrase_coverage"]["我得说点啥"] is True
    assert result["feedback_phrase_coverage"]["才能让他继续"] is True


def test_midrun_exception_keeps_recoverable_context_for_followup(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_midrun_exception_preserves_recovery_context(tmp_path)

    assert result["followup_text"] == "刚刚那个执行到一半没了，接着来"
    assert result["failed_request_visible_to_followup"] is True
    assert result["failure_marker_visible_to_followup"] is True
    assert result["followup_used_failed_request"] is True
    assert result["followup_response"] == "resumed-after-midrun-failure"


def test_persistent_event_dedupe_skips_feishu_redelivery(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_persistent_event_dedupe_skips_redelivery(tmp_path)

    assert result["same_message_id_dispatch_count"] == 1
    assert result["same_message_id_duplicate_suppressed"] is True
    assert result["long_content_dispatch_count"] == 1
    assert result["long_content_duplicate_suppressed"] is True
    assert result["processed_event_rows"] >= 2
    assert result["duplicate_processing_completed"] is True


def test_skillhub_clean_personal_install_uses_symlink_and_audit_source(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_personal_skillhub_clean_install_symlink(tmp_path)

    assert result["install_mode"] == "symlink"
    assert result["target_is_symlink"] is True
    assert result["personal_manifest_source"] == "personal"
    assert result["listed_source"] == "personal"
    assert result["audit_source"] == "personal"
    assert result["audit_token_files_present"] is False


def test_wildcard_shared_token_materialization_skips_group_profiles(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_wildcard_shared_token_skips_group_profiles(tmp_path)

    assert result["profiles_targeted"] == 2
    assert result["written"] == 2
    assert result["alice_has_token"] is True
    assert result["bob_has_token"] is True
    assert result["group_has_token"] is False
    assert result["inactive_has_token"] is False


def test_webui_skillhub_owner_scoped_install_and_audit_uat(tmp_path: Path):
    matrix_mod = _load_matrix_module()

    result = matrix_mod.case_webui_skillhub_owner_scoped_install_and_audit(tmp_path)

    assert result["status"] == 200
    assert result["profile_name"] == "owner_sync_profile"
    assert result["install_mode"] == "symlink"
    assert result["target_is_symlink"] is True
    assert result["spoofed_profile_created"] is False
    assert result["audit_status"] == 200
    assert result["audit_profiles"] == ["owner_sync_profile"]


def test_real_uat_scope_inventory_is_secret_free(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.credentials import CredentialStore
    from hermes_multitenancy.routing import RoutingTable

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-scope-inventory-key")
    matrix_mod = _load_matrix_module()
    real_home = tmp_path / "real-home"
    table = RoutingTable(real_home / "multitenancy.db")
    try:
        table.upsert(
            user_id="user-valid",
            profile_name="feishu_valid",
            open_id="ou_valid",
            provenance="sync",
        )
        table.upsert(
            user_id="user-expired",
            profile_name="feishu_expired",
            open_id="ou_expired",
            provenance="sync",
        )
    finally:
        table.close()
    now_ms = int(time.time() * 1000)
    store = CredentialStore(real_home / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="feishu_valid",
            subject_id="ou_valid",
            provider="feishu",
            secret_kind="uat",
            payload={"access_token": "secret-access", "refresh_token": "secret-refresh"},
            scopes=[
                "auth:user.id:read",
                "offline_access",
                "im:message.send_as_user",
                "docx:document:create",
                "docs:document.content:read",
                "drive:file:upload",
            ],
            expires_at=now_ms + 3600_000,
        )
        store.put_credential(
            profile_name="feishu_expired",
            subject_id="ou_expired",
            provider="feishu",
            secret_kind="uat",
            payload={"access_token": "expired-secret", "refresh_token": "expired-refresh"},
            scopes=["auth:user.id:read"],
            expires_at=now_ms - 1_000,
        )
    finally:
        store.close()

    result = matrix_mod.case_real_uat_scope_inventory_secret_free(real_home)

    assert result["checked"] is True
    assert result["secret_free"] is True
    assert result["valid_core_identity_count"] == 1
    assert "im:message.send_as_user" in result["required_core_scopes"]
    assert "secret-access" not in str(result)
    assert "secret-refresh" not in str(result)


def test_real_tat_bot_token_retries_transient_network_errors(monkeypatch, tmp_path: Path):
    from hermes_multitenancy.credentials import CredentialStore

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-tat-retry-key")
    matrix_mod = _load_matrix_module()
    monkeypatch.setattr(matrix_mod.time, "sleep", lambda _seconds: None)
    real_home = tmp_path / "real-home"
    store = CredentialStore(real_home / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="__global__",
            subject_id="feishu_app",
            provider="feishu",
            secret_kind="app",
            payload={"app_id": "cli_app", "app_secret": "app-secret", "domain": "feishu"},
        )
    finally:
        store.close()
    calls: list[float] = []

    def fake_mint(payload, *, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise urllib.error.URLError(TimeoutError("handshake timed out"))
        return "tat-secret"

    monkeypatch.setattr("hermes_multitenancy.lark_cli_auth_broker._mint_tenant_access_token", fake_mint)

    result = matrix_mod.case_real_tat_bot_token(real_home)

    assert result["tat_minted"] is True
    assert result["token_length"] == len("tat-secret")
    assert result["tat_attempts"] == 2
    assert len(calls) == 2
