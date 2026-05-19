from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "scripts" / "skills_uat_completion_audit.py"


def _load_audit_module():
    spec = importlib.util.spec_from_file_location("skills_uat_completion_audit", AUDIT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _write_second_problem_trace(path: Path, *, exact_found: bool = False) -> None:
    _write_json(
        path / "second-problem-trace.json",
        {
            "ok": True,
            "exact_text_found": exact_found,
            "exact_match_count": 1 if exact_found else 0,
            "exact_issue_text_absent_reason": "" if exact_found else "exact_phrase_not_found",
            "searched_files": 42,
            "searched_roots": [str(ROOT), "/Users/kite/Library/Mobile Documents/iCloud~md~obsidian/Documents/My-Second-Brain/hermes"],
            "referenced_artifacts": [
                {"label": "Image #1", "content_available": False, "evidence_path": ""},
                {"label": "Image #2", "content_available": False, "evidence_path": ""},
            ],
            "candidate_classes": [
                {"name": "vision_or_screenshot_failure", "match_count": 2, "evidence_files": ["hermes/vision.md"]},
                {"name": "context_fragmentation", "match_count": 1, "evidence_files": ["hermes/context.md"]},
                {"name": "duplicate_dispatch_card", "match_count": 1, "evidence_files": ["hermes/card.md"]},
                {"name": "slow_model_idle_wait", "match_count": 1, "evidence_files": ["OpenClaw/slow.md"]},
            ],
        },
    )


def _write_second_problem_trace_with_available_artifact(
    path: Path,
    *,
    mapped_uat_scenarios: list[str] | None = None,
    exact_found: bool = False,
) -> None:
    mapped_uat_scenarios = mapped_uat_scenarios or []
    _write_json(
        path / "second-problem-trace.json",
        {
            "ok": True,
            "exact_text_found": exact_found,
            "exact_match_count": 1 if exact_found else 0,
            "exact_issue_text_absent_reason": "" if exact_found else "phrase_present_but_only_placeholder_followup",
            "searched_files": 42,
            "searched_roots": [str(ROOT), "/Users/kite/Library/Mobile Documents/iCloud~md~obsidian/Documents/My-Second-Brain/hermes"],
            "exact_matches": [
                {
                    "path": str(path / "Image #1.txt"),
                    "phrase": "然后第二个问题",
                    "sample": "然后第二个问题 是卡片重复出现",
                    "followup_text": "是卡片重复出现",
                    "mapped_uat_scenarios": mapped_uat_scenarios,
                }
            ] if exact_found else [],
            "referenced_artifacts": [
                {
                    "label": "Image #1",
                    "content_available": True,
                    "evidence_path": str(path / "Image #1.txt"),
                    "content_kind": "text",
                    "extracted_text": "然后第二个问题 是卡片重复出现" if exact_found else "截图内容已落盘",
                    "mapped_uat_scenarios": mapped_uat_scenarios,
                },
                {"label": "Image #2", "content_available": False, "evidence_path": ""},
            ],
            "candidate_classes": [
                {"name": "vision_or_screenshot_failure", "match_count": 2, "evidence_files": ["hermes/vision.md"]},
                {"name": "context_fragmentation", "match_count": 1, "evidence_files": ["hermes/context.md"]},
                {"name": "duplicate_dispatch_card", "match_count": 1, "evidence_files": ["hermes/card.md"]},
                {"name": "slow_model_idle_wait", "match_count": 1, "evidence_files": ["OpenClaw/slow.md"]},
            ],
        },
    )


def _write_gateway_process_evidence(
    path: Path,
    *,
    expected_worktree: Path,
    live_plugin_target: Path,
    process_start_epoch: int = 2_005,
) -> None:
    _write_json(
        path / "gateway-process-evidence.json",
        {
            "ok": True,
            "pid": 12345,
            "command": "python -m hermes_cli.main --profile multitenancy_router gateway run --replace",
            "expected_worktree": str(expected_worktree.resolve(strict=False)),
            "live_plugin_target": str(live_plugin_target.resolve(strict=False)),
            "process_start_epoch": process_start_epoch,
        },
    )


def _base_evidence(tmp_path: Path) -> object:
    audit_mod = _load_audit_module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    cases = []
    for name in sorted(audit_mod.REQUIRED_MATRIX_CASES):
        case = {"name": name, "ok": True}
        if name == "offline_distribution_audience_symlink_version_self_install":
            case.update({
                "manifest_version": "v2",
                "weather_target": "/tmp/shared/skill-releases/weather/v2",
                "stable_profile_skill_path": "weather/shared",
                "rollback_manifest_version": "v1",
                "rollback_weather_target": "/tmp/shared/skill-releases/weather/v1",
                "lark_calendar_install_mode": "symlink",
                "lark_calendar_token_policy": "brokered",
                "lark_calendar_share_with_children": True,
            })
        if name == "offline_registry_audit_personal_managed_loop_guard":
            case.update({
                "profile_count": 2,
                "audited_profiles": 2,
                "source_counts": {"managed": 2, "personal": 1, "unknown": 1},
            })
        if name == "real_home_skill_inventory_secret_free":
            case.update({
                "checked": True,
                "secret_free": True,
                "profile_count": 3,
                "audited_profiles": 3,
                "total_skills": 8,
                "token_file_marker_count": 2,
                "source_counts": {"managed": 5, "personal": 2, "unknown": 1},
            })
        if name == "offline_webui_child_agent_inherits_skills_not_tokens":
            case.update({
                "webui_child_profile": "webui_child_research",
                "inherited_from": "alice",
                "weather_skill": True,
                "lark_calendar_skill": True,
                "personal_oauth_skill": False,
                "token_files": 0,
                "uat_files": 0,
            })
        if name == "offline_continue_turn_reconstructs_interrupted_request":
            case.update({
                "continue_used_previous_request": True,
                "continue_response": "continued-weather-report-from-interrupted-request",
                "continue_history_before_response": [
                    "帮我生成天气 skill 共享报告，执行时间长一点",
                    "上一个任务在完成前被中断或取消；如果用户要求继续，请根据上一条用户请求继续推进，不要丢失上下文。",
                    "继续",
                ],
            })
        if name == "offline_interruption_arbitrary_followup_resume_context":
            case.update({
                "followup_text": "刚才那个报告还在吗？接着跑",
                "magic_continue_required": False,
                "continue_used_previous_request": True,
                "continue_response": "continued-weather-report-from-interrupted-request",
                "interrupted_request_visible_to_followup": True,
                "interruption_marker_visible_to_followup": True,
                "continue_history_before_response": [
                    "帮我生成天气 skill 共享报告，执行时间长一点",
                    "上一个任务在完成前被中断或取消；如果用户要求继续，请根据上一条用户请求继续推进，不要丢失上下文。",
                    "刚才那个报告还在吗？接着跑",
                ],
            })
        if name == "offline_production_feedback_interruption_quote_resume":
            case.update({
                "first_problem_exact_feedback_covered": True,
                "production_feedback_quote": "先报个问题，我遇到两次了，就是会中断，执行一半突然就没了。我得说点啥，才能让他继续。",
                "feedback_phrases": ["会中断", "执行一半突然就没了", "我得说点啥", "才能让他继续"],
                "feedback_phrase_coverage": {
                    "会中断": True,
                    "执行一半突然就没了": True,
                    "我得说点啥": True,
                    "才能让他继续": True,
                },
                "followup_text": "我得说点啥，才能让他继续",
                "magic_continue_required": False,
                "continue_used_previous_request": True,
                "continue_response": "continued-weather-report-from-interrupted-request",
                "interrupted_request_visible_to_followup": True,
                "interruption_marker_visible_to_followup": True,
                "continue_history_before_response": [
                    "帮我生成天气 skill 共享报告，执行时间长一点",
                    "上一个任务在完成前被中断或取消；如果用户要求继续，请根据上一条用户请求继续推进，不要丢失上下文。",
                    "我得说点啥，才能让他继续",
                ],
            })
        if name == "offline_hermes_loader_discovers_symlinked_skills":
            case.update({
                "loader_checked": True,
                "discovered_count": 2,
                "weather_skill_discovered": True,
                "lark_skill_discovered": True,
                "discovered_relative_paths": [
                    "skills/lark-calendar/SKILL.md",
                    "skills/weather/shared/SKILL.md",
                ],
            })
        if name == "offline_persistent_event_dedupe_skips_redelivery":
            case.update({
                "same_message_id_dispatch_count": 1,
                "same_message_id_duplicate_suppressed": True,
                "long_content_dispatch_count": 1,
                "long_content_duplicate_suppressed": True,
                "processed_event_rows": 2,
                "duplicate_processing_completed": True,
            })
        if name == "offline_personal_skillhub_clean_install_symlink":
            case.update({
                "install_mode": "symlink",
                "target_is_symlink": True,
                "personal_manifest_source": "personal",
                "listed_source": "personal",
                "audit_source": "personal",
                "audit_install_mode": "symlink",
                "audit_token_files_present": False,
            })
        if name == "offline_new_hire_sync_auto_installs_managed_skills":
            case.update({
                "initial_stats": {"created": 1, "updated": 0, "kept": 0, "skipped": 0},
                "new_hire_stats": {"created": 1, "updated": 1, "kept": 0, "skipped": 0},
                "new_hire_profile_created": True,
                "new_hire_weather_install_mode": "symlink",
                "new_hire_weather_version": "v2",
                "new_hire_lark_calendar_token_policy": "brokered",
                "new_hire_lark_calendar_share_with_children": True,
                "new_hire_finance_skill": True,
                "new_hire_personal_install_preserved_after_resync": True,
            })
        if name == "real_feishu_uat_scope_inventory_secret_free":
            case.update({
                "checked": True,
                "secret_free": True,
                "valid_core_identity_count": 1,
                "required_core_scopes": [
                    "auth:user.id:read",
                    "docx:document:create",
                    "docs:document.content:read",
                    "drive:file:upload",
                    "im:message.send_as_user",
                    "offline_access",
                ],
                "results": [
                    {
                        "profile_name": "feishu_g41a5b5g",
                        "open_id": "ou_valid",
                        "status": "valid",
                        "scope_count": 181,
                        "has_payload": True,
                        "missing_core_scopes": [],
                        "secret_free": True,
                    }
                ],
            })
        cases.append(case)
    _write_json(
        tmp_path / "skills-uat-latest.json",
        {
            "ok": True,
            "repo": str(ROOT),
            "cases": cases,
        },
    )
    _write_jsonl(
        tmp_path / "lark-write-smoke.jsonl",
        [
            {"scenario_label": "owner-webui", "status": "completed", "verdict": "pass", "tools": [{"name": "lark_cli"}]},
            {"scenario_label": "owner-feishu", "status": "completed", "verdict": "pass", "tools": [{"name": "lark_cli"}]},
        ],
    )
    _write_jsonl(
        tmp_path / "lark-group-write-rerun-current-gateway.jsonl",
        [{"scenario_label": "group-feishu", "status": "completed", "verdict": "pass", "tools": [{"name": "lark_cli"}]}],
    )
    _write_json(
        tmp_path / "direct-group-lark-cli-write.json",
        {
            "ok": True,
            "identity_bot": True,
            "document_id": "doc_123",
            "stdout_excerpt": json.dumps({"ok": True, "data": {"permission_grant": {"status": "skipped"}}}),
        },
    )
    _write_json(
        tmp_path / "router-group-lark-cli-write.json",
        {
            "ok": True,
            "identity_bot": True,
            "document_id": "doc_456",
            "stdout_excerpt": json.dumps({"ok": True, "data": {"permission_grant": {"status": "skipped"}}}),
            "router_profile": "feishu_group_ctx",
            "router_chat_id": "oc_ctx",
            "route": {"group_profile": "feishu_group_ctx", "chat_id": "oc_ctx"},
        },
    )
    _write_second_problem_trace(tmp_path)
    return audit_mod


def _write_passing_dialogue_evidence(path: Path) -> None:
    _write_jsonl(
        path / "lark-dialogue-smoke.jsonl",
        [
            {"scenario_label": "group-feishu", "status": "completed", "verdict": "pass", "tools": [{"name": "lark_cli"}]},
            {"scenario_label": "group-feishu", "status": "completed", "verdict": "pass", "tools": [{"name": "lark_cli"}]},
            {"scenario_label": "owner-feishu", "status": "completed", "verdict": "pass", "tools": [{"name": "lark_cli"}]},
            {"scenario_label": "owner-feishu", "status": "completed", "verdict": "pass", "tools": [{"name": "lark_cli"}]},
            {"scenario_label": "owner-webui", "status": "completed", "verdict": "pass", "tools": [{"name": "lark_cli"}]},
            {"scenario_label": "owner-webui", "status": "completed", "verdict": "pass", "tools": [{"name": "lark_cli"}]},
        ],
    )


def _write_passing_write_evidence(path: Path) -> None:
    _write_jsonl(
        path / "lark-write-smoke.jsonl",
        [
            {
                "scenario_label": "owner-webui",
                "status": "completed",
                "verdict": "pass",
                "prompt": "请执行 `docs +create --api-version v2 --content x`",
                "output": "document_id: AbCdEfGhIjKlMnOpQrStUvWxYz1 https://example.feishu.cn/docx/AbCdEfGhIjKlMnOpQrStUvWxYz1",
                "tools": [{"name": "lark_cli", "event": "completed", "error": False}],
            },
            {
                "scenario_label": "owner-feishu",
                "status": "completed",
                "verdict": "pass",
                "prompt": "请执行 `docs +create --api-version v2 --content y`",
                "output": "document_id: ZyXwVuTsRqPoNmLkJiHgFeDcBa9 https://example.feishu.cn/docx/ZyXwVuTsRqPoNmLkJiHgFeDcBa9",
                "tools": [{"name": "lark_cli", "event": "completed", "error": False}],
            },
        ],
    )


def _write_passing_group_write_evidence(path: Path) -> None:
    _write_jsonl(
        path / "lark-group-write-rerun-current-gateway.jsonl",
        [
            {
                "scenario_label": "group-feishu",
                "status": "completed",
                "verdict": "pass",
                "prompt": "请执行 `docs +create --api-version v2 --content z`",
                "output": "document_id: GroupDocAbCdEfGhIjKlMnOpQrSt1 https://example.feishu.cn/docx/GroupDocAbCdEfGhIjKlMnOpQrSt1",
                "tools": [{"name": "lark_cli", "event": "completed", "error": False}],
            }
        ],
    )


def _write_complete_second_problem_trace(path: Path, *, artifact_kind: str = "image") -> None:
    suffix = "png" if artifact_kind == "image" else "txt"
    transcript = path / f"Image #1.{suffix}"
    transcript.write_text("然后第二个问题 是卡片重复出现\n", encoding="utf-8")
    _write_json(
        path / "second-problem-trace.json",
        {
            "ok": True,
            "exact_text_found": True,
            "exact_match_count": 1,
            "exact_issue_text_absent_reason": "",
            "searched_files": 42,
            "searched_roots": [str(ROOT)],
            "exact_matches": [
                {
                    "path": str(transcript),
                    "phrase": "然后第二个问题",
                    "sample": "然后第二个问题 是卡片重复出现",
                    "followup_text": "是卡片重复出现",
                    "mapped_uat_scenarios": ["offline_session_guard_replacement_no_duplicate_dispatch"],
                }
            ],
            "referenced_artifacts": [
                {
                    "label": "Image #1",
                    "content_available": True,
                    "evidence_path": str(transcript),
                    "content_kind": artifact_kind,
                    "extracted_text": "然后第二个问题 是卡片重复出现\n",
                    "mapped_uat_scenarios": ["offline_session_guard_replacement_no_duplicate_dispatch"],
                },
                {
                    "label": "Image #2",
                    "content_available": True,
                    "evidence_path": str(transcript),
                    "content_kind": artifact_kind,
                    "extracted_text": "然后第二个问题 是卡片重复出现\n",
                    "mapped_uat_scenarios": ["offline_session_guard_replacement_no_duplicate_dispatch"],
                },
            ],
            "raw_image_artifact_candidates": {
                "Image #1": [] if artifact_kind != "image" else [str(transcript)],
                "Image #2": [] if artifact_kind != "image" else [str(transcript)],
            },
            "raw_image_search_roots": [str(path)],
            "searched_raw_image_files": 0 if artifact_kind != "image" else 1,
            "candidate_classes": [],
        },
    )


def _write_exact_branch_gateway_evidence(path: Path, *, worktree: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(worktree, target_is_directory=True)
    group_evidence = path / "lark-group-write-rerun-current-gateway.jsonl"
    os.utime(link, (2_000, 2_000), follow_symlinks=False)
    os.utime(group_evidence, (2_020, 2_020))
    _write_gateway_process_evidence(
        path,
        expected_worktree=worktree,
        live_plugin_target=worktree,
        process_start_epoch=2_010,
    )


def test_completion_audit_rejects_dialogue_rows_without_required_identity_coverage(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_jsonl(
        tmp_path / "lark-dialogue-smoke.jsonl",
        [
            {"scenario_label": "owner-feishu", "status": "completed", "verdict": "pass"},
            {"scenario_label": "owner-feishu", "status": "completed", "verdict": "pass"},
            {"scenario_label": "owner-feishu", "status": "completed", "verdict": "pass"},
            {"scenario_label": "owner-feishu", "status": "completed", "verdict": "pass"},
            {"scenario_label": "owner-feishu", "status": "completed", "verdict": "pass"},
            {"scenario_label": "owner-feishu", "status": "completed", "verdict": "pass"},
        ],
    )

    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    dialogue = next(item for item in report["items"] if item["requirement"].startswith("Validate real dialogue-level"))
    assert dialogue["status"] == "failed"
    assert "missing_labels" in dialogue["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_rejects_write_rows_without_docs_create_and_document_id(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_jsonl(
        tmp_path / "lark-write-smoke.jsonl",
        [
            {"scenario_label": "owner-webui", "status": "completed", "verdict": "pass", "prompt": "GET /user_info", "output": "ok", "tools": [{"name": "lark_cli"}]},
            {"scenario_label": "owner-feishu", "status": "completed", "verdict": "pass", "prompt": "GET /user_info", "output": "ok", "tools": [{"name": "lark_cli"}]},
        ],
    )
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    write = next(item for item in report["items"] if item["requirement"].startswith("Validate real personal write paths"))
    assert write["status"] == "failed"
    assert report["evidence_ok"] is False


def test_completion_audit_requires_session_guard_replacement_uat_case(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix["cases"] = [
        case
        for case in matrix["cases"]
        if case["name"] != "offline_session_guard_replacement_no_duplicate_dispatch"
    ]
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    matrix_item = next(item for item in report["items"] if item["requirement"].startswith("Construct non-pytest"))
    assert matrix_item["status"] == "failed"
    assert "offline_session_guard_replacement_no_duplicate_dispatch" in matrix_item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_inflight_scope_uat_case(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix["cases"] = [
        case
        for case in matrix["cases"]
        if case["name"] != "offline_inflight_replacement_scoped_private_group"
    ]
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    matrix_item = next(item for item in report["items"] if item["requirement"].startswith("Construct non-pytest"))
    assert matrix_item["status"] == "failed"
    assert "offline_inflight_replacement_scoped_private_group" in matrix_item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_slow_model_idle_feedback_uat_case(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix["cases"] = [
        case
        for case in matrix["cases"]
        if case["name"] != "offline_slow_model_idle_feedback_heartbeat"
    ]
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    matrix_item = next(item for item in report["items"] if item["requirement"].startswith("Construct non-pytest"))
    assert matrix_item["status"] == "failed"
    assert "offline_slow_model_idle_feedback_heartbeat" in matrix_item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_symlink_version_rollback_evidence(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for case in matrix["cases"]:
        if case["name"] == "offline_distribution_audience_symlink_version_self_install":
            case.pop("rollback_manifest_version", None)
            case.pop("rollback_weather_target", None)
            break
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Validate managed symlink version switch and rollback"))
    assert item["status"] == "failed"
    assert "rollback_manifest_version" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_brokered_lark_skill_create_ready_metadata(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for case in matrix["cases"]:
        if case["name"] == "offline_distribution_audience_symlink_version_self_install":
            case.pop("lark_calendar_token_policy", None)
            case["lark_calendar_share_with_children"] = False
            break
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Validate managed symlink version switch and rollback"))
    assert item["status"] == "failed"
    assert "lark_calendar_token_policy" in item["note"]
    assert "lark_calendar_share_with_children" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_cross_profile_skill_source_inventory(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for case in matrix["cases"]:
        if case["name"] == "offline_registry_audit_personal_managed_loop_guard":
            case["source_counts"] = {"managed": 2, "personal": 1}
            break
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Validate cross-profile skill inventory"))
    assert item["status"] == "failed"
    assert "unknown" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_real_home_secret_free_skill_inventory(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix["cases"] = [
        case
        for case in matrix["cases"]
        if case["name"] != "real_home_skill_inventory_secret_free"
    ]
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Validate real Hermes skill inventory"))
    assert item["status"] == "failed"
    assert "missing" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_second_problem_trace_evidence(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    (tmp_path / "second-problem-trace.json").unlink()
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    second_problem = next(item for item in report["items"] if item["requirement"].startswith("Prove coverage for the user's omitted"))
    assert second_problem["status"] == "failed"
    assert "second-problem-trace.json missing" in second_problem["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_blocks_when_second_problem_trace_has_no_exact_text(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    second_problem = next(item for item in report["items"] if item["requirement"].startswith("Prove coverage for the user's omitted"))
    assert second_problem["status"] == "blocked"
    assert "searched_files=42" in second_problem["note"]
    assert "slow_model_idle_wait" in second_problem["note"]
    assert "exact_match_count=0" in second_problem["note"]
    assert "exact_phrase_match_count=0" in second_problem["note"]
    assert "placeholder_match_count=0" in second_problem["note"]
    assert "exact_phrase_source_counts={}" in second_problem["note"]
    assert "placeholder_source_counts={}" in second_problem["note"]
    assert "exact_issue_source_counts={}" in second_problem["note"]
    assert "absent_reason=exact_phrase_not_found" in second_problem["note"]
    assert "missing_artifacts=['Image #1', 'Image #2']" in second_problem["note"]
    assert report["evidence_ok"] is True


def test_completion_audit_fails_when_webui_child_inheritance_uat_is_missing(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    skills_path = tmp_path / "skills-uat-latest.json"
    payload = json.loads(skills_path.read_text(encoding="utf-8"))
    payload["cases"] = [
        case
        for case in payload["cases"]
        if case.get("name") != "offline_webui_child_agent_inherits_skills_not_tokens"
    ]
    skills_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(
        item
        for item in report["items"]
        if item["requirement"].startswith("Validate WebUI child profile")
    )
    assert item["status"] == "failed"
    assert report["evidence_ok"] is False


def test_completion_audit_blocks_when_referenced_feedback_artifacts_are_unavailable(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    artifact_item = next(
        item
        for item in report["items"]
        if item["requirement"].startswith("Verify referenced production feedback screenshots")
    )
    assert artifact_item["status"] == "blocked"
    assert "Image #1" in artifact_item["note"]
    assert "Image #2" in artifact_item["note"]
    assert report["evidence_ok"] is True


def test_completion_audit_fails_if_second_problem_exact_text_is_found_without_mapped_uat(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_second_problem_trace(tmp_path, exact_found=True)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    second_problem = next(item for item in report["items"] if item["requirement"].startswith("Prove coverage for the user's omitted"))
    assert second_problem["status"] == "failed"
    assert "exact second-problem text was found" in second_problem["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_note_distinguishes_blank_second_problem_body(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_second_problem_trace_with_available_artifact(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    second_problem = next(item for item in report["items"] if item["requirement"].startswith("Prove coverage for the user's omitted"))
    assert "absent_reason=phrase_present_but_only_placeholder_followup" in second_problem["note"]


def test_completion_audit_fails_when_feedback_artifact_is_available_without_mapped_uat(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_second_problem_trace_with_available_artifact(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    artifact_item = next(
        item
        for item in report["items"]
        if item["requirement"].startswith("Verify referenced production feedback screenshots")
    )
    assert artifact_item["status"] == "failed"
    assert "unmapped_artifacts=['Image #1']" in artifact_item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_accepts_available_feedback_artifact_mapped_to_green_uat(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_second_problem_trace_with_available_artifact(
        tmp_path,
        mapped_uat_scenarios=["offline_session_guard_replacement_no_duplicate_dispatch"],
    )
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    artifact_item = next(
        item
        for item in report["items"]
        if item["requirement"].startswith("Verify referenced production feedback screenshots")
    )
    assert artifact_item["status"] == "blocked"
    assert "mapped_artifacts=['Image #1']" in artifact_item["note"]
    assert "missing_artifacts=['Image #2']" in artifact_item["note"]
    assert "artifact_kinds={'Image #1': 'text'}" in artifact_item["note"]
    assert report["evidence_ok"] is True


def test_completion_audit_blocks_when_feedback_screenshots_are_only_text_transcripts(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_complete_second_problem_trace(tmp_path, artifact_kind="text")
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    artifact_item = next(
        item
        for item in report["items"]
        if item["requirement"].startswith("Verify referenced production feedback screenshots")
    )
    assert artifact_item["status"] == "blocked"
    assert "non_image_artifacts=['Image #1', 'Image #2']" in artifact_item["note"]
    assert "raw_image_candidates={'Image #1': [], 'Image #2': []}" in artifact_item["note"]
    assert "structured_feedback_image_payload_count=0" in artifact_item["note"]
    assert "structured_feedback_local_image_payload_count=0" in artifact_item["note"]
    assert "current_feedback_structured_image_payload_count=0" in artifact_item["note"]
    assert "current_feedback_structured_local_image_payload_count=0" in artifact_item["note"]
    assert "current_feedback_goal_context_match_count=0" in artifact_item["note"]
    assert "current_feedback_goal_context_snapshot_count=0" in artifact_item["note"]
    assert "current_feedback_goal_context_unique_count=0" in artifact_item["note"]
    assert "current_feedback_goal_context_image_placeholder_count=0" in artifact_item["note"]
    assert "searched_raw_image_files=0" in artifact_item["note"]
    assert "artifact_kinds={'Image #1': 'text', 'Image #2': 'text'}" in artifact_item["note"]
    second_problem = next(
        item
        for item in report["items"]
        if item["requirement"].startswith("Prove coverage for the user's omitted")
    )
    assert "available_artifacts=['Image #1', 'Image #2']" in second_problem["note"]
    assert "artifact_kinds={'Image #1': 'text', 'Image #2': 'text'}" in second_problem["note"]
    assert report["evidence_ok"] is True


def test_completion_audit_surfaces_text_only_goal_context_feedback_without_accepting_screenshots(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_complete_second_problem_trace(tmp_path, artifact_kind="text")
    trace_path = tmp_path / "second-problem-trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    trace["current_feedback_goal_context_match_count"] = 1
    trace["current_feedback_goal_context_snapshot_count"] = 3
    trace["current_feedback_goal_context_unique_count"] = 1
    trace["current_feedback_goal_context_image_placeholder_count"] = 2
    trace["current_feedback_goal_context_matches"] = [
        {
            "path": str(tmp_path / "rollout.jsonl"),
            "line": 7,
            "image_placeholder_count": 2,
            "sample": "[Image #1] [Image #2] 可以可以，我正在体验 Hermes",
        }
    ]
    _write_json(trace_path, trace)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    artifact_item = next(
        item
        for item in report["items"]
        if item["requirement"].startswith("Verify referenced production feedback screenshots")
    )
    assert artifact_item["status"] == "blocked"
    assert "current_feedback_goal_context_match_count=1" in artifact_item["note"]
    assert "current_feedback_goal_context_snapshot_count=3" in artifact_item["note"]
    assert "current_feedback_goal_context_unique_count=1" in artifact_item["note"]
    assert "current_feedback_goal_context_image_placeholder_count=2" in artifact_item["note"]
    assert "non_image_artifacts=['Image #1', 'Image #2']" in artifact_item["note"]


def test_completion_audit_surfaces_historical_image_candidates_for_text_only_screenshots(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_complete_second_problem_trace(tmp_path, artifact_kind="text")
    trace_path = tmp_path / "second-problem-trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    historical_source = tmp_path / "clipboard-image-one.png"
    historical_source.write_bytes(b"\x89PNG\r\n\x1a\n")
    trace["historical_image_references"] = [
        {
            "path": str(tmp_path / "agent-session.jsonl"),
            "source": str(historical_source),
            "labels": ["Image #1"],
        },
    ]
    trace["historical_image_reference_count"] = 1
    _write_json(trace_path, trace)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    artifact_item = next(
        item
        for item in report["items"]
        if item["requirement"].startswith("Verify referenced production feedback screenshots")
    )
    assert artifact_item["status"] == "blocked"
    assert f"historical_image_candidates={{'Image #1': ['{historical_source}']}}" in artifact_item["note"]
    assert "historical_image_reference_count=1" in artifact_item["note"]
    assert "historical_image_candidate_policy=diagnostic_only_not_current_feedback_artifact" in artifact_item["note"]


def test_completion_audit_surfaces_historical_image_review_rejections(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_complete_second_problem_trace(tmp_path, artifact_kind="text")
    trace_path = tmp_path / "second-problem-trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    historical_source = tmp_path / "qr-invite.png"
    historical_source.write_bytes(b"\x89PNG\r\n\x1a\n")
    trace["historical_image_references"] = [
        {
            "path": str(tmp_path / "agent-session.jsonl"),
            "source": str(historical_source),
            "labels": ["Image #1"],
        },
    ]
    trace["historical_image_reference_count"] = 1
    _write_json(trace_path, trace)
    _write_json(
        tmp_path / "historical-image-reviews.json",
        {
            "reviews": [
                {
                    "source": str(historical_source),
                    "labels": ["Image #1"],
                    "verdict": "rejected",
                    "reason": "lark_group_invite_qr_not_feedback_screenshot",
                    "md5": "abc123",
                    "pixel_width": 1372,
                    "pixel_height": 1488,
                }
            ]
        },
    )
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    artifact_item = next(
        item
        for item in report["items"]
        if item["requirement"].startswith("Verify referenced production feedback screenshots")
    )
    assert artifact_item["status"] == "blocked"
    assert "historical_image_review_rejections=" in artifact_item["note"]
    assert "lark_group_invite_qr_not_feedback_screenshot" in artifact_item["note"]
    assert "abc123" in artifact_item["note"]
    assert "1372x1488" in artifact_item["note"]


def test_completion_audit_surfaces_unreviewed_historical_image_candidates(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_complete_second_problem_trace(tmp_path, artifact_kind="text")
    trace_path = tmp_path / "second-problem-trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    reviewed_source = tmp_path / "reviewed-qr.png"
    unreviewed_source = tmp_path / "unreviewed-candidate.png"
    reviewed_source.write_bytes(b"\x89PNG\r\n\x1a\n")
    unreviewed_source.write_bytes(b"\x89PNG\r\n\x1a\n")
    trace["historical_image_references"] = [
        {
            "path": str(tmp_path / "agent-session.jsonl"),
            "source": str(reviewed_source),
            "labels": ["Image #1"],
        },
        {
            "path": str(tmp_path / "agent-session.jsonl"),
            "source": str(unreviewed_source),
            "labels": ["Image #1"],
        },
    ]
    trace["historical_image_reference_count"] = 2
    _write_json(trace_path, trace)
    _write_json(
        tmp_path / "historical-image-reviews.json",
        {
            "reviews": [
                {
                    "source": str(reviewed_source),
                    "labels": ["Image #1"],
                    "verdict": "rejected",
                    "reason": "lark_group_invite_qr_not_feedback_screenshot",
                    "md5": "abc123",
                    "pixel_width": 1372,
                    "pixel_height": 1488,
                }
            ]
        },
    )
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    artifact_item = next(
        item
        for item in report["items"]
        if item["requirement"].startswith("Verify referenced production feedback screenshots")
    )
    assert artifact_item["status"] == "blocked"
    assert "historical_image_unreviewed_candidates=" in artifact_item["note"]
    assert str(unreviewed_source) in artifact_item["note"]
    assert str(reviewed_source) not in artifact_item["note"].split("historical_image_unreviewed_candidates=")[1]


def test_completion_audit_accepts_exact_second_problem_text_mapped_to_green_uat(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_second_problem_trace_with_available_artifact(
        tmp_path,
        mapped_uat_scenarios=["offline_session_guard_replacement_no_duplicate_dispatch"],
        exact_found=True,
    )
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    second_problem = next(item for item in report["items"] if item["requirement"].startswith("Prove coverage for the user's omitted"))
    assert second_problem["status"] == "covered"
    assert "mapped_exact_matches=1" in second_problem["note"]
    assert report["evidence_ok"] is True


def test_completion_audit_fails_direct_group_write_when_permission_grant_is_not_skipped(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    direct_path = tmp_path / "direct-group-lark-cli-write.json"
    direct = json.loads(direct_path.read_text(encoding="utf-8"))
    direct["stdout_excerpt"] = json.dumps({"ok": True, "data": {"permission_grant": {"status": "granted"}}})
    direct_path.write_text(json.dumps(direct, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Validate current branch group profile"))
    assert item["status"] == "failed"
    assert "permission_grant_skipped=False" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_fails_router_group_write_when_permission_grant_is_not_skipped(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    router_path = tmp_path / "router-group-lark-cli-write.json"
    router = json.loads(router_path.read_text(encoding="utf-8"))
    router["stdout_excerpt"] = json.dumps({"ok": True, "data": {"permission_grant": {"status": "granted"}}})
    router_path.write_text(json.dumps(router, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Validate current branch group router"))
    assert item["status"] == "failed"
    assert "permission_grant_skipped=False" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_fails_explicit_inflight_scope_item_when_case_is_red(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for case in matrix["cases"]:
        if case["name"] == "offline_inflight_replacement_scoped_private_group":
            case["ok"] = False
            case["reason"] = "AssertionError: group turn cancelled the owner's private turn"
            break
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Cover the production interruption class"))
    assert item["status"] == "failed"
    assert report["evidence_ok"] is False


def test_completion_audit_requires_arbitrary_followup_resume_uat(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for case in matrix["cases"]:
        if case["name"] == "offline_interruption_arbitrary_followup_resume_context":
            case["ok"] = False
            case["interrupted_request_visible_to_followup"] = False
            case["interruption_marker_visible_to_followup"] = False
            break
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Cover arbitrary follow-up resume"))
    assert item["status"] == "failed"
    assert "interrupted_request_visible_to_followup=False" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_production_feedback_interruption_quote_mapping(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for case in matrix["cases"]:
        if case["name"] == "offline_production_feedback_interruption_quote_resume":
            case["first_problem_exact_feedback_covered"] = False
            case["feedback_phrase_coverage"]["执行一半突然就没了"] = False
            break
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Map the production feedback first-problem quote"))
    assert item["status"] == "failed"
    assert "exact_feedback_covered=False" in item["note"]
    assert "执行一半突然就没了" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_persistent_event_dedupe_uat(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for case in matrix["cases"]:
        if case["name"] == "offline_persistent_event_dedupe_skips_redelivery":
            case["same_message_id_dispatch_count"] = 2
            case["same_message_id_duplicate_suppressed"] = False
            break
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Validate persistent inbound event dedupe"))
    assert item["status"] == "failed"
    assert "same_message_id_dispatch_count=2" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_continue_turn_reconstruction_uat_case(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for case in matrix["cases"]:
        if case["name"] == "offline_continue_turn_reconstructs_interrupted_request":
            case["continue_used_previous_request"] = False
            case["continue_response"] = "missing-context"
            break
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Validate a continue turn"))
    assert item["status"] == "failed"
    assert "continue_used_previous_request=False" in item["note"]
    assert "missing-context" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_personal_skillhub_secret_guard_uat_case(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix["cases"] = [
        case
        for case in matrix["cases"]
        if case["name"] != "offline_personal_skillhub_install_secret_guard"
    ]
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    matrix_item = next(item for item in report["items"] if item["requirement"].startswith("Construct non-pytest"))
    assert matrix_item["status"] == "failed"
    assert "offline_personal_skillhub_install_secret_guard" in matrix_item["note"]


def test_completion_audit_requires_clean_skillhub_personal_install_uat_case(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for case in matrix["cases"]:
        if case["name"] == "offline_personal_skillhub_clean_install_symlink":
            case["install_mode"] = "copy"
            case["target_is_symlink"] = False
            case["audit_source"] = "unknown"
            break
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Validate clean personal SkillHub installs"))
    assert item["status"] == "failed"
    assert "install_mode=copy" in item["note"]
    assert "audit_source=unknown" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_real_uat_scope_inventory_case(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for case in matrix["cases"]:
        if case["name"] == "real_feishu_uat_scope_inventory_secret_free":
            case["valid_core_identity_count"] = 0
            case["results"][0]["missing_core_scopes"] = ["im:message.send_as_user"]
            break
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Validate real Feishu personal UAT has core"))
    assert item["status"] == "failed"
    assert "valid_core_identity_count=0" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_new_hire_sync_case(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for case in matrix["cases"]:
        if case["name"] == "offline_new_hire_sync_auto_installs_managed_skills":
            case["new_hire_profile_created"] = False
            case["new_hire_personal_install_preserved_after_resync"] = False
            break
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Validate Feishu sync provisions managed skills"))
    assert item["status"] == "failed"
    assert "new_hire_profile_created" in item["note"]
    assert "personal_install_preserved" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_hermes_loader_symlink_discovery_case(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for case in matrix["cases"]:
        if case["name"] == "offline_hermes_loader_discovers_symlinked_skills":
            case["weather_skill_discovered"] = False
            case["discovered_relative_paths"] = ["skills/lark-calendar/SKILL.md"]
            break
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Validate Hermes skill loader discovers"))
    assert item["status"] == "failed"
    assert "weather_skill_discovered=False" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_all_profile_skill_inventory_sources(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for case in matrix["cases"]:
        if case["name"] == "offline_registry_audit_personal_managed_loop_guard":
            case.pop("profile_count", None)
            case.pop("source_counts", None)
            break
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Validate cross-profile skill inventory"))
    assert item["status"] == "failed"
    assert "source_counts" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_requires_real_home_skill_inventory_secret_free(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for case in matrix["cases"]:
        if case["name"] == "real_home_skill_inventory_secret_free":
            case["secret_free"] = False
            case["total_skills"] = 0
            break
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    item = next(item for item in report["items"] if item["requirement"].startswith("Validate real Hermes skill inventory"))
    assert item["status"] == "failed"
    assert "secret_free=False" in item["note"]
    assert "total_skills=0" in item["note"]
    assert report["evidence_ok"] is False


def test_completion_audit_accepts_required_dialogue_and_write_identity_coverage(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    assert report["failed"] == 0
    assert report["blocked"] == 3
    assert report["covered"] == 24
    assert report["evidence_ok"] is True
    assert report["completion_state"] == "incomplete"


def test_completion_audit_treats_missing_credential_key_real_cases_as_blocked_not_failed(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix["ok"] = False
    for case in matrix["cases"]:
        if case["name"] in {"real_feishu_uat_user_info", "real_feishu_tat_bot_token"}:
            case["ok"] = False
            case["reason"] = "RuntimeError: credential encryption key is required"
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    matrix_item = next(item for item in report["items"] if item["requirement"].startswith("Construct non-pytest"))
    assert matrix_item["status"] == "covered"
    assert "blocked_failures" in matrix_item["note"]
    assert report["failed"] == 0
    assert report["blocked"] == 4
    assert report["covered"] == 23
    assert report["evidence_ok"] is True


def test_completion_audit_treats_expired_real_user_uat_as_blocked_not_failed(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix["ok"] = False
    for case in matrix["cases"]:
        if case["name"] == "real_feishu_uat_user_info":
            case["ok"] = False
            case["reason"] = "AssertionError: no valid user UAT canary succeeded"
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    matrix_item = next(item for item in report["items"] if item["requirement"].startswith("Construct non-pytest"))
    assert matrix_item["status"] == "covered"
    assert "real_feishu_uat_user_info" in matrix_item["note"]
    assert report["failed"] == 0
    assert report["blocked"] == 4
    assert report["covered"] == 23
    assert report["evidence_ok"] is True


def test_completion_audit_treats_scope_inventory_without_valid_user_uat_as_blocked_not_failed(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    matrix_path = tmp_path / "skills-uat-latest.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix["ok"] = False
    for case in matrix["cases"]:
        if case["name"] == "real_feishu_uat_scope_inventory_secret_free":
            case.clear()
            case.update({
                "name": "real_feishu_uat_scope_inventory_secret_free",
                "ok": False,
                "reason": "AssertionError: no valid real user UAT has the required core lark-cli scopes",
            })
    matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
    link = tmp_path / "gateway-plugin"
    link.symlink_to(tmp_path / "other-worktree", target_is_directory=True)

    report = audit_mod.audit(tmp_path, worktree=ROOT, gateway_plugin_link=link)

    matrix_item = next(item for item in report["items"] if item["requirement"].startswith("Construct non-pytest"))
    scope_item = next(item for item in report["items"] if item["requirement"].startswith("Validate real Feishu personal UAT has core"))
    assert matrix_item["status"] == "covered"
    assert "real_feishu_uat_scope_inventory_secret_free" in matrix_item["note"]
    assert scope_item["status"] == "blocked"
    assert report["failed"] == 0
    assert report["blocked"] == 4
    assert report["covered"] == 23
    assert report["evidence_ok"] is True


def test_completion_audit_marks_exact_live_branch_covered_when_gateway_link_matches(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    expected_worktree = tmp_path / "worktree"
    expected_worktree.mkdir()
    link = tmp_path / "gateway-plugin"
    link.symlink_to(expected_worktree, target_is_directory=True)
    group_evidence = tmp_path / "lark-group-write-rerun-current-gateway.jsonl"
    os.utime(link, (2_000, 2_000), follow_symlinks=False)
    os.utime(group_evidence, (2_010, 2_010))
    _write_gateway_process_evidence(tmp_path, expected_worktree=expected_worktree, live_plugin_target=expected_worktree)

    report = audit_mod.audit(tmp_path, worktree=expected_worktree, gateway_plugin_link=link)

    live_branch = next(item for item in report["items"] if item["requirement"].startswith("Prove the running Feishu gateway"))
    assert live_branch["status"] == "covered"
    assert report["blocked"] == 2
    assert report["completion_state"] == "incomplete"


def test_completion_audit_blocks_exact_live_branch_when_gateway_process_evidence_missing(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    expected_worktree = tmp_path / "worktree"
    expected_worktree.mkdir()
    link = tmp_path / "gateway-plugin"
    link.symlink_to(expected_worktree, target_is_directory=True)
    group_evidence = tmp_path / "lark-group-write-rerun-current-gateway.jsonl"
    os.utime(link, (2_000, 2_000), follow_symlinks=False)
    os.utime(group_evidence, (2_010, 2_010))

    report = audit_mod.audit(tmp_path, worktree=expected_worktree, gateway_plugin_link=link)

    live_branch = next(item for item in report["items"] if item["requirement"].startswith("Prove the running Feishu gateway"))
    assert live_branch["status"] == "blocked"
    assert "gateway_process_evidence=missing" in live_branch["note"]


def test_completion_audit_blocks_exact_live_branch_when_process_predates_link(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    expected_worktree = tmp_path / "worktree"
    expected_worktree.mkdir()
    link = tmp_path / "gateway-plugin"
    link.symlink_to(expected_worktree, target_is_directory=True)
    group_evidence = tmp_path / "lark-group-write-rerun-current-gateway.jsonl"
    os.utime(link, (2_000, 2_000), follow_symlinks=False)
    os.utime(group_evidence, (2_010, 2_010))
    _write_gateway_process_evidence(
        tmp_path,
        expected_worktree=expected_worktree,
        live_plugin_target=expected_worktree,
        process_start_epoch=1_990,
    )

    report = audit_mod.audit(tmp_path, worktree=expected_worktree, gateway_plugin_link=link)

    live_branch = next(item for item in report["items"] if item["requirement"].startswith("Prove the running Feishu gateway"))
    assert live_branch["status"] == "blocked"
    assert "process_after_link=False" in live_branch["note"]


def test_completion_audit_blocks_exact_live_branch_when_group_write_evidence_predates_link(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    expected_worktree = tmp_path / "worktree"
    expected_worktree.mkdir()
    link = tmp_path / "gateway-plugin"
    link.symlink_to(expected_worktree, target_is_directory=True)
    group_evidence = tmp_path / "lark-group-write-rerun-current-gateway.jsonl"
    os.utime(group_evidence, (2_000, 2_000))
    os.utime(link, (2_010, 2_010), follow_symlinks=False)
    _write_gateway_process_evidence(
        tmp_path,
        expected_worktree=expected_worktree,
        live_plugin_target=expected_worktree,
        process_start_epoch=2_020,
    )

    report = audit_mod.audit(tmp_path, worktree=expected_worktree, gateway_plugin_link=link)

    live_branch = next(item for item in report["items"] if item["requirement"].startswith("Prove the running Feishu gateway"))
    assert live_branch["status"] == "blocked"
    assert "group_write_evidence_after_link=False" in live_branch["note"]
    assert report["blocked"] == 3
    assert report["completion_state"] == "incomplete"


def test_completion_audit_resolves_relative_gateway_symlink(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    link_dir = tmp_path / "plugins"
    link_dir.mkdir()
    expected_worktree = tmp_path / "relative-target"
    expected_worktree.mkdir()
    link = link_dir / "multitenancy"
    os.symlink("../relative-target", link)
    group_evidence = tmp_path / "lark-group-write-rerun-current-gateway.jsonl"
    os.utime(link, (2_000, 2_000), follow_symlinks=False)
    os.utime(group_evidence, (2_010, 2_010))
    _write_gateway_process_evidence(tmp_path, expected_worktree=expected_worktree, live_plugin_target=expected_worktree)

    report = audit_mod.audit(tmp_path, worktree=expected_worktree, gateway_plugin_link=link)

    live_branch = next(item for item in report["items"] if item["requirement"].startswith("Prove the running Feishu gateway"))
    assert live_branch["status"] == "covered"


def test_completion_audit_require_complete_returns_nonzero_for_incomplete_evidence(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    link = tmp_path / "plugins" / "multitenancy"
    _write_exact_branch_gateway_evidence(tmp_path, worktree=worktree, link=link)
    output = tmp_path / "audit.json"

    exit_code = audit_mod.main([
        "--evidence-dir",
        str(tmp_path),
        "--worktree",
        str(worktree),
        "--gateway-plugin-link",
        str(link),
        "--output",
        str(output),
        "--require-complete",
    ])

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert report["evidence_ok"] is True
    assert report["completion_state"] == "incomplete"
    assert report["blocked"] > 0


def test_completion_audit_require_complete_returns_zero_for_complete_evidence(tmp_path: Path):
    audit_mod = _base_evidence(tmp_path)
    _write_passing_dialogue_evidence(tmp_path)
    _write_passing_write_evidence(tmp_path)
    _write_passing_group_write_evidence(tmp_path)
    _write_complete_second_problem_trace(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    link = tmp_path / "plugins" / "multitenancy"
    _write_exact_branch_gateway_evidence(tmp_path, worktree=worktree, link=link)
    output = tmp_path / "audit.json"

    exit_code = audit_mod.main([
        "--evidence-dir",
        str(tmp_path),
        "--worktree",
        str(worktree),
        "--gateway-plugin-link",
        str(link),
        "--output",
        str(output),
        "--require-complete",
    ])

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["evidence_ok"] is True
    assert report["completion_state"] == "complete"
    assert report["blocked"] == 0
