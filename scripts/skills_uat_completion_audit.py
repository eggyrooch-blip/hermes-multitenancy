#!/usr/bin/env python3
"""Audit evidence for the skills unified-management UAT objective.

This script deliberately separates "evidence is internally consistent" from
"the whole user objective is complete". It exits zero when the evidence files
can be parsed and their covered claims are proven; the JSON output still marks
known unproven items as blocked.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


REQUIRED_MATRIX_CASES = {
    "offline_keep_four_skill_policy_model",
    "offline_distribution_audience_symlink_version_self_install",
    "offline_hermes_loader_discovers_symlinked_skills",
    "offline_new_hire_sync_auto_installs_managed_skills",
    "offline_child_agent_inherits_skills_not_tokens",
    "offline_webui_child_agent_inherits_skills_not_tokens",
    "offline_child_install_does_not_sync_back_to_parent",
    "offline_shared_token_materialization_is_scoped",
    "offline_personal_token_stays_profile_local",
    "offline_registry_audit_personal_managed_loop_guard",
    "offline_interruption_resume_context",
    "offline_continue_turn_reconstructs_interrupted_request",
    "offline_interruption_arbitrary_followup_resume_context",
    "offline_production_feedback_interruption_quote_resume",
    "offline_midrun_exception_preserves_recovery_context",
    "offline_persistent_event_dedupe_skips_redelivery",
    "offline_slow_model_idle_feedback_heartbeat",
    "offline_vision_failure_surfaces_recovery_context",
    "offline_context_continuity_private_and_group",
    "offline_inflight_replacement_scoped_private_group",
    "offline_session_guard_replacement_no_duplicate_dispatch",
    "offline_personal_skillhub_install_secret_guard",
    "offline_personal_skillhub_clean_install_symlink",
    "real_home_secret_free_routes_uat_readiness",
    "real_home_skill_inventory_secret_free",
    "real_group_replacement_race_replay",
    "real_feishu_uat_user_info",
    "real_feishu_uat_scope_inventory_secret_free",
    "real_feishu_tat_bot_token",
}
KNOWN_BLOCKED_MATRIX_FAILURES = {
    "real_feishu_uat_user_info": (
        "credential encryption key is required",
        "no valid user UAT canary succeeded",
    ),
    "real_feishu_uat_scope_inventory_secret_free": (
        "credential encryption key is required",
        "no valid real user UAT has the required core lark-cli scopes",
    ),
    "real_feishu_tat_bot_token": ("credential encryption key is required",),
}
IMAGE_ARTIFACT_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _load_optional_json(path: Path) -> dict[str, Any] | None:
    try:
        return _load_json(path)
    except FileNotFoundError:
        return None


def _item(requirement: str, status: str, evidence: str, note: str = "") -> dict[str, str]:
    return {
        "requirement": requirement,
        "status": status,
        "evidence": evidence,
        "note": note,
    }


def _all_pass(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and all(row.get("verdict") == "pass" and row.get("status") == "completed" for row in rows)


def _has_lark_cli_tool(row: dict[str, Any]) -> bool:
    return any(str(tool.get("name") or "") == "lark_cli" for tool in row.get("tools") or [])


def _has_docs_create_result(row: dict[str, Any]) -> bool:
    prompt = str(row.get("prompt") or "")
    output = str(row.get("output") or "")
    has_create_command = "docs +create" in prompt or "docs +create" in output
    has_doc_id = "document_id" in output and re.search(r"\b[A-Za-z0-9]{20,}\b", output) is not None
    return has_create_command and has_doc_id


def _group_permission_grant_skipped(evidence: dict[str, Any]) -> bool:
    raw = evidence.get("stdout_excerpt")
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict):
        return False
    data = parsed.get("data")
    if not isinstance(data, dict):
        return False
    permission_grant = data.get("permission_grant")
    if not isinstance(permission_grant, dict):
        return False
    return permission_grant.get("status") == "skipped"


def _coverage_by_label(
    rows: list[dict[str, Any]],
    required_labels: set[str],
    *,
    require_docs_create: bool = False,
) -> tuple[bool, list[str], dict[str, int]]:
    counts = {label: 0 for label in required_labels}
    for row in rows:
        label = str(row.get("scenario_label") or "")
        if (
            label in counts
            and row.get("verdict") == "pass"
            and row.get("status") == "completed"
            and _has_lark_cli_tool(row)
            and (not require_docs_create or _has_docs_create_result(row))
        ):
            counts[label] += 1
    missing = sorted(label for label, count in counts.items() if count == 0)
    return not missing and _all_pass(rows), missing, counts


def _resolve_link_target(path: Path) -> str:
    if path.is_symlink():
        raw_target = os.readlink(path)
        target = Path(raw_target)
        if not target.is_absolute():
            target = path.parent / target
        return str(target.resolve(strict=False))
    if path.exists():
        return str(path.resolve(strict=False))
    return ""


def _path_mtime(path: Path, *, follow_symlinks: bool = True) -> float:
    try:
        stat_result = path.stat() if follow_symlinks else path.lstat()
    except OSError:
        return 0.0
    return float(stat_result.st_mtime)


def _known_blocked_matrix_failures(cases: dict[Any, dict[str, Any]], failed_cases: list[str]) -> list[str]:
    blocked: list[str] = []
    for name in failed_cases:
        expected_reasons = KNOWN_BLOCKED_MATRIX_FAILURES.get(str(name), ())
        case = cases.get(name) or {}
        reason = str(case.get("reason") or "")
        if any(expected_reason in reason for expected_reason in expected_reasons):
            blocked.append(str(name))
    return sorted(blocked)


def _missing_referenced_artifacts(trace: dict[str, Any]) -> list[str]:
    return [
        str(artifact.get("label"))
        for artifact in trace.get("referenced_artifacts") or []
        if artifact.get("label") and artifact.get("content_available") is not True
    ]


def _green_mapped_scenarios(values: Any, cases: dict[Any, dict[str, Any]]) -> list[str]:
    if not isinstance(values, list):
        return []
    return sorted(
        str(value)
        for value in values
        if isinstance(value, str) and (cases.get(value) or {}).get("ok") is True
    )


def _feedback_artifact_mapping(trace: dict[str, Any], cases: dict[Any, dict[str, Any]]) -> tuple[list[str], list[str]]:
    mapped: list[str] = []
    unmapped: list[str] = []
    for artifact in trace.get("referenced_artifacts") or []:
        if artifact.get("content_available") is not True:
            continue
        label = str(artifact.get("label") or "")
        if not label:
            continue
        if _green_mapped_scenarios(artifact.get("mapped_uat_scenarios"), cases):
            mapped.append(label)
        else:
            unmapped.append(label)
    return sorted(mapped), sorted(unmapped)


def _feedback_artifact_kinds(trace: dict[str, Any]) -> dict[str, str]:
    rows: dict[str, str] = {}
    for artifact in trace.get("referenced_artifacts") or []:
        if artifact.get("content_available") is not True:
            continue
        label = str(artifact.get("label") or "")
        if not label:
            continue
        rows[label] = str(artifact.get("content_kind") or "unknown")
    return {label: rows[label] for label in sorted(rows)}


def _available_referenced_artifacts(trace: dict[str, Any]) -> list[str]:
    return sorted(
        str(artifact.get("label"))
        for artifact in trace.get("referenced_artifacts") or []
        if artifact.get("label") and artifact.get("content_available") is True
    )


def _non_image_referenced_artifacts(trace: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for artifact in trace.get("referenced_artifacts") or []:
        if artifact.get("content_available") is not True:
            continue
        label = str(artifact.get("label") or "")
        if not label:
            continue
        if str(artifact.get("content_kind") or "unknown") != "image":
            rows.append(label)
    return sorted(rows)


def _raw_image_artifact_candidates(trace: dict[str, Any]) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    artifact_roots = [Path(root) for root in trace.get("artifact_roots") or [] if root]
    for artifact in trace.get("referenced_artifacts") or []:
        if artifact.get("content_available") is not True:
            continue
        label = str(artifact.get("label") or "")
        if not label or str(artifact.get("content_kind") or "unknown") == "image":
            continue
        candidates: set[str] = set()
        evidence_path = Path(str(artifact.get("evidence_path") or ""))
        search_dirs: list[Path] = []
        if evidence_path.name and evidence_path.parent != Path("."):
            search_dirs.append(evidence_path.parent)
        search_dirs.extend(artifact_roots)
        stems = {evidence_path.stem, label}
        for directory in search_dirs:
            for stem in stems:
                for suffix in IMAGE_ARTIFACT_SUFFIXES:
                    candidate = directory / f"{stem}{suffix}"
                    if candidate.exists():
                        candidates.add(str(candidate))
        rows[label] = sorted(candidates)

    raw = trace.get("raw_image_artifact_candidates")
    if isinstance(raw, dict):
        for label, values in raw.items():
            if not isinstance(label, str):
                continue
            if not isinstance(values, list):
                continue
            existing = set(rows.get(label, []))
            existing.update(str(value) for value in values if isinstance(value, str))
            rows[label] = sorted(existing)
    return {label: rows[label] for label in sorted(rows)}


def _raw_image_search_roots(trace: dict[str, Any]) -> list[str]:
    return sorted(str(value) for value in trace.get("raw_image_search_roots") or [] if value)


def _historical_image_candidates(trace: dict[str, Any]) -> dict[str, list[str]]:
    rows: dict[str, set[str]] = {}
    for reference in trace.get("historical_image_references") or []:
        if not isinstance(reference, dict):
            continue
        source = str(reference.get("source") or "")
        if not source:
            continue
        for label in reference.get("labels") or []:
            if not isinstance(label, str) or not label:
                continue
            rows.setdefault(label, set()).add(source)
    return {label: sorted(values) for label, values in sorted(rows.items())}


def _historical_image_review_rejections(path: Path) -> list[str]:
    payload = _load_optional_json(path)
    if payload is None:
        return []
    reviews = payload.get("reviews")
    if not isinstance(reviews, list):
        return []
    rows: list[str] = []
    for review in reviews:
        if not isinstance(review, dict) or str(review.get("verdict") or "") != "rejected":
            continue
        labels = ",".join(str(label) for label in review.get("labels") or [] if isinstance(label, str))
        reason = str(review.get("reason") or "")
        source = str(review.get("source") or "")
        md5 = str(review.get("md5") or "")
        width = int(review.get("pixel_width") or 0)
        height = int(review.get("pixel_height") or 0)
        dimensions = f"{width}x{height}" if width and height else ""
        rows.append(
            f"source={source},labels={labels},reason={reason},md5={md5},dimensions={dimensions}"
        )
    return sorted(rows)


def _historical_image_unreviewed_candidates(candidates: dict[str, list[str]], path: Path) -> dict[str, list[str]]:
    payload = _load_optional_json(path)
    reviewed: set[tuple[str, str]] = set()
    if isinstance(payload, dict):
        reviews = payload.get("reviews")
        if isinstance(reviews, list):
            for review in reviews:
                if not isinstance(review, dict):
                    continue
                source = str(review.get("source") or "")
                if not source:
                    continue
                for label in review.get("labels") or []:
                    if isinstance(label, str) and label:
                        reviewed.add((label, source))
    rows: dict[str, list[str]] = {}
    for label, sources in candidates.items():
        unreviewed = sorted(source for source in sources if (label, source) not in reviewed)
        if unreviewed:
            rows[label] = unreviewed
    return {label: rows[label] for label in sorted(rows)}


def _unlinked_clipboard_image_candidates(trace: dict[str, Any]) -> list[dict[str, Any]]:
    rows = trace.get("current_feedback_unlinked_clipboard_image_candidates")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _mapped_exact_match_count(trace: dict[str, Any], cases: dict[Any, dict[str, Any]]) -> int:
    count = 0
    for match in trace.get("exact_matches") or []:
        if _green_mapped_scenarios(match.get("mapped_uat_scenarios"), cases):
            count += 1
    return count


def _feedback_artifacts_item(trace_path: Path, cases: dict[Any, dict[str, Any]]) -> dict[str, str]:
    trace = _load_optional_json(trace_path)
    requirement = "Verify referenced production feedback screenshots are available or explicitly blocked."
    if trace is None:
        return _item(
            requirement,
            "failed",
            str(trace_path),
            "second-problem-trace.json missing; cannot verify referenced feedback screenshots.",
        )
    if trace.get("ok") is not True:
        return _item(
            requirement,
            "failed",
            str(trace_path),
            f"second-problem trace did not complete: reason={trace.get('reason', '')}",
        )

    missing_artifacts = _missing_referenced_artifacts(trace)
    mapped_artifacts, unmapped_artifacts = _feedback_artifact_mapping(trace, cases)
    artifact_kinds = _feedback_artifact_kinds(trace)
    non_image_artifacts = _non_image_referenced_artifacts(trace)
    raw_image_candidates = _raw_image_artifact_candidates(trace)
    raw_image_search_roots = _raw_image_search_roots(trace)
    searched_raw_image_files = int(trace.get("searched_raw_image_files") or 0)
    structured_feedback_message_count = int(trace.get("structured_feedback_message_count") or 0)
    structured_feedback_image_payload_count = int(trace.get("structured_feedback_image_payload_count") or 0)
    structured_feedback_local_image_payload_count = int(
        trace.get("structured_feedback_local_image_payload_count") or 0
    )
    current_feedback_structured_message_count = int(
        trace.get("current_feedback_structured_message_count") or 0
    )
    current_feedback_structured_image_payload_count = int(
        trace.get("current_feedback_structured_image_payload_count") or 0
    )
    current_feedback_structured_local_image_payload_count = int(
        trace.get("current_feedback_structured_local_image_payload_count") or 0
    )
    current_feedback_goal_context_match_count = int(
        trace.get("current_feedback_goal_context_match_count") or 0
    )
    current_feedback_goal_context_snapshot_count = int(
        trace.get("current_feedback_goal_context_snapshot_count") or 0
    )
    current_feedback_goal_context_unique_count = int(
        trace.get("current_feedback_goal_context_unique_count") or 0
    )
    current_feedback_goal_context_image_placeholder_count = int(
        trace.get("current_feedback_goal_context_image_placeholder_count") or 0
    )
    historical_image_candidates = _historical_image_candidates(trace)
    historical_image_reference_count = int(trace.get("historical_image_reference_count") or 0)
    historical_image_candidate_policy = "diagnostic_only_not_current_feedback_artifact"
    historical_image_review_path = trace_path.parent / "historical-image-reviews.json"
    historical_image_review_rejections = _historical_image_review_rejections(
        historical_image_review_path
    )
    historical_image_unreviewed_candidates = _historical_image_unreviewed_candidates(
        historical_image_candidates,
        historical_image_review_path,
    )
    unlinked_clipboard_image_candidates = _unlinked_clipboard_image_candidates(trace)
    unlinked_clipboard_image_candidate_count = int(
        trace.get("current_feedback_unlinked_clipboard_image_candidate_count")
        or len(unlinked_clipboard_image_candidates)
    )
    if unmapped_artifacts:
        return _item(
            requirement,
            "failed",
            str(trace_path),
            f"unmapped_artifacts={unmapped_artifacts}; missing_artifacts={missing_artifacts}; "
            f"artifact_kinds={artifact_kinds}",
        )
    if missing_artifacts:
        return _item(
            requirement,
            "blocked",
            str(trace_path),
            f"mapped_artifacts={mapped_artifacts}; missing_artifacts={missing_artifacts}; "
            f"historical_image_candidates={historical_image_candidates}; "
            f"historical_image_reference_count={historical_image_reference_count}; "
            f"historical_image_candidate_policy={historical_image_candidate_policy}; "
            f"historical_image_review_rejections={historical_image_review_rejections}; "
            f"historical_image_unreviewed_candidates={historical_image_unreviewed_candidates}; "
            f"current_feedback_unlinked_clipboard_image_candidate_count="
            f"{unlinked_clipboard_image_candidate_count}; "
            f"current_feedback_unlinked_clipboard_image_candidates={unlinked_clipboard_image_candidates[:5]}; "
            f"structured_feedback_message_count={structured_feedback_message_count}; "
            f"structured_feedback_image_payload_count={structured_feedback_image_payload_count}; "
            f"structured_feedback_local_image_payload_count={structured_feedback_local_image_payload_count}; "
            f"current_feedback_structured_message_count={current_feedback_structured_message_count}; "
            f"current_feedback_structured_image_payload_count={current_feedback_structured_image_payload_count}; "
            f"current_feedback_structured_local_image_payload_count="
            f"{current_feedback_structured_local_image_payload_count}; "
            f"current_feedback_goal_context_match_count={current_feedback_goal_context_match_count}; "
            f"current_feedback_goal_context_snapshot_count={current_feedback_goal_context_snapshot_count}; "
            f"current_feedback_goal_context_unique_count={current_feedback_goal_context_unique_count}; "
            f"current_feedback_goal_context_image_placeholder_count="
            f"{current_feedback_goal_context_image_placeholder_count}; "
            f"artifact_kinds={artifact_kinds}",
        )
    if non_image_artifacts:
        return _item(
            requirement,
            "blocked",
            str(trace_path),
            f"mapped_artifacts={mapped_artifacts}; non_image_artifacts={non_image_artifacts}; "
            f"raw_image_candidates={raw_image_candidates}; "
            f"historical_image_candidates={historical_image_candidates}; "
            f"historical_image_reference_count={historical_image_reference_count}; "
            f"historical_image_candidate_policy={historical_image_candidate_policy}; "
            f"historical_image_review_rejections={historical_image_review_rejections}; "
            f"historical_image_unreviewed_candidates={historical_image_unreviewed_candidates}; "
            f"current_feedback_unlinked_clipboard_image_candidate_count="
            f"{unlinked_clipboard_image_candidate_count}; "
            f"current_feedback_unlinked_clipboard_image_candidates={unlinked_clipboard_image_candidates[:5]}; "
            f"structured_feedback_message_count={structured_feedback_message_count}; "
            f"structured_feedback_image_payload_count={structured_feedback_image_payload_count}; "
            f"structured_feedback_local_image_payload_count={structured_feedback_local_image_payload_count}; "
            f"current_feedback_structured_message_count={current_feedback_structured_message_count}; "
            f"current_feedback_structured_image_payload_count={current_feedback_structured_image_payload_count}; "
            f"current_feedback_structured_local_image_payload_count="
            f"{current_feedback_structured_local_image_payload_count}; "
            f"current_feedback_goal_context_match_count={current_feedback_goal_context_match_count}; "
            f"current_feedback_goal_context_snapshot_count={current_feedback_goal_context_snapshot_count}; "
            f"current_feedback_goal_context_unique_count={current_feedback_goal_context_unique_count}; "
            f"current_feedback_goal_context_image_placeholder_count="
            f"{current_feedback_goal_context_image_placeholder_count}; "
            f"searched_raw_image_files={searched_raw_image_files}; "
            f"raw_image_search_roots={raw_image_search_roots}; artifact_kinds={artifact_kinds}",
        )
    return _item(
        requirement,
        "covered",
        str(trace_path),
        "all referenced feedback screenshots have available content; "
        f"mapped_artifacts={mapped_artifacts}; artifact_kinds={artifact_kinds}.",
    )


def _second_problem_trace_item(trace_path: Path, cases: dict[Any, dict[str, Any]]) -> dict[str, str]:
    trace = _load_optional_json(trace_path)
    requirement = "Prove coverage for the user's omitted 'second problem' exact text."
    if trace is None:
        return _item(
            requirement,
            "failed",
            str(trace_path),
            "second-problem-trace.json missing; rerun scripts/skills_second_problem_trace.py before audit.",
        )
    if trace.get("ok") is not True:
        return _item(
            requirement,
            "failed",
            str(trace_path),
            f"second-problem trace did not complete: reason={trace.get('reason', '')}",
        )

    candidate_classes = [
        str(candidate.get("name"))
        for candidate in trace.get("candidate_classes") or []
        if candidate.get("match_count")
    ]
    missing_artifacts = _missing_referenced_artifacts(trace)
    available_artifacts = _available_referenced_artifacts(trace)
    artifact_kinds = _feedback_artifact_kinds(trace)
    note = (
        f"searched_files={trace.get('searched_files', 0)}; "
        f"exact_match_count={trace.get('exact_match_count', 0)}; "
        f"exact_phrase_match_count={trace.get('exact_phrase_match_count', 0)}; "
        f"placeholder_match_count={trace.get('placeholder_match_count', 0)}; "
        f"exact_phrase_source_counts={trace.get('exact_phrase_source_counts', {})}; "
        f"placeholder_source_counts={trace.get('placeholder_source_counts', {})}; "
        f"exact_issue_source_counts={trace.get('exact_issue_source_counts', {})}; "
        f"absent_reason={trace.get('exact_issue_text_absent_reason', '')}; "
        f"missing_artifacts={missing_artifacts}; "
        f"available_artifacts={available_artifacts}; "
        f"artifact_kinds={artifact_kinds}; "
        f"candidate_classes={candidate_classes}"
    )
    if trace.get("exact_text_found") is True:
        mapped_exact_matches = _mapped_exact_match_count(trace, cases)
        if mapped_exact_matches:
            return _item(
                requirement,
                "covered",
                str(trace_path),
                f"mapped_exact_matches={mapped_exact_matches}; {note}",
            )
        return _item(
            requirement,
            "failed",
            str(trace_path),
            f"exact second-problem text was found; add a mapped UAT before claiming coverage. {note}",
        )
    return _item(
        requirement,
        "blocked",
        str(trace_path),
        "The exact second problem text is absent in searched local evidence; "
        "candidate classes are covered defensively. "
        + note,
    )


def _gateway_process_evidence_ok(
    process_path: Path,
    *,
    current_repo: str,
    live_plugin_target: str,
    live_plugin_mtime: float,
    current_group_mtime: float,
) -> tuple[bool, str]:
    process = _load_optional_json(process_path)
    if process is None:
        return False, f"gateway_process_evidence=missing:{process_path}"
    process_start = float(process.get("process_start_epoch") or 0)
    command = str(process.get("command") or "")
    evidence_repo = str(process.get("expected_worktree") or "")
    evidence_target = str(process.get("live_plugin_target") or "")
    process_after_link = bool(process_start and (not live_plugin_mtime or process_start >= live_plugin_mtime))
    group_after_process = bool(current_group_mtime and process_start and current_group_mtime >= process_start)
    command_matches = "multitenancy_router" in command and "gateway" in command
    repo_matches = evidence_repo == current_repo and evidence_target == live_plugin_target == current_repo
    ok = bool(process.get("ok") is True and process_after_link and group_after_process and command_matches and repo_matches)
    note = (
        f"gateway_process_pid={process.get('pid', '')}; "
        f"process_start_epoch={int(process_start)}; "
        f"process_after_link={process_after_link}; "
        f"group_write_after_process={group_after_process}; "
        f"command_matches={command_matches}; repo_matches={repo_matches}"
    )
    return ok, note


def _symlink_version_rollback_ok(case: dict[str, Any]) -> tuple[bool, str]:
    expected = {
        "manifest_version": "v2",
        "rollback_manifest_version": "v1",
        "stable_profile_skill_path": "weather/shared",
        "lark_calendar_install_mode": "symlink",
        "lark_calendar_token_policy": "brokered",
        "lark_calendar_share_with_children": True,
    }
    mismatches = {
        key: {"expected": value, "actual": case.get(key)}
        for key, value in expected.items()
        if case.get(key) != value
    }
    weather_target = str(case.get("weather_target") or "")
    rollback_target = str(case.get("rollback_weather_target") or "")
    if not weather_target.endswith("skill-releases/weather/v2"):
        mismatches["weather_target"] = {"expected_suffix": "skill-releases/weather/v2", "actual": weather_target}
    if not rollback_target.endswith("skill-releases/weather/v1"):
        mismatches["rollback_weather_target"] = {"expected_suffix": "skill-releases/weather/v1", "actual": rollback_target}
    note = (
        f"manifest_version={case.get('manifest_version', '')}; "
        f"rollback_manifest_version={case.get('rollback_manifest_version', '')}; "
        f"stable_profile_skill_path={case.get('stable_profile_skill_path', '')}; "
        f"lark_calendar_install_mode={case.get('lark_calendar_install_mode', '')}; "
        f"lark_calendar_token_policy={case.get('lark_calendar_token_policy', '')}; "
        f"lark_calendar_share_with_children={case.get('lark_calendar_share_with_children', '')}; "
        f"weather_target={weather_target}; rollback_weather_target={rollback_target}; "
        f"mismatches={mismatches}"
    )
    return not mismatches, note


def _hermes_loader_symlink_ok(case: dict[str, Any]) -> tuple[bool, str]:
    relative_paths = case.get("discovered_relative_paths")
    if not isinstance(relative_paths, list):
        relative_paths = []
    weather_discovered = case.get("weather_skill_discovered") is True
    lark_discovered = case.get("lark_skill_discovered") is True
    loader_checked = case.get("loader_checked") is True
    discovered_count = int(case.get("discovered_count") or 0)
    required = {"skills/weather/shared/SKILL.md", "skills/lark-calendar/SKILL.md"}
    missing_paths = sorted(path for path in required if path not in {str(item) for item in relative_paths})
    ok = (
        case.get("ok") is True
        and loader_checked
        and weather_discovered
        and lark_discovered
        and discovered_count >= 2
        and not missing_paths
    )
    note = (
        f"ok={case.get('ok') is True}; loader_checked={loader_checked}; "
        f"weather_skill_discovered={weather_discovered}; lark_skill_discovered={lark_discovered}; "
        f"discovered_count={discovered_count}; missing_paths={missing_paths}"
    )
    return ok, note


def _skill_inventory_ok(case: dict[str, Any]) -> tuple[bool, str]:
    source_counts = case.get("source_counts") if isinstance(case.get("source_counts"), dict) else {}
    required_sources = ("managed", "personal", "unknown")
    missing_sources = [source for source in required_sources if int(source_counts.get(source) or 0) < 1]
    profile_count = int(case.get("profile_count") or 0)
    audited_profiles = int(case.get("audited_profiles") or 0)
    ok = case.get("ok") is True and profile_count >= 2 and audited_profiles >= 2 and not missing_sources
    note = (
        f"profile_count={profile_count}; audited_profiles={audited_profiles}; "
        f"source_counts={source_counts}; missing_sources={missing_sources}"
    )
    return ok, note


def _real_home_skill_inventory_ok(case: dict[str, Any]) -> tuple[bool, str]:
    source_counts = case.get("source_counts") if isinstance(case.get("source_counts"), dict) else {}
    matrix_ok = case.get("ok") is True
    checked = case.get("checked") is True
    secret_free = case.get("secret_free") is True
    profile_count = int(case.get("profile_count") or 0)
    audited_profiles = int(case.get("audited_profiles") or 0)
    total_skills = int(case.get("total_skills") or 0)
    missing = []
    if not matrix_ok:
        missing.append("ok")
    if not checked:
        missing.append("checked")
    if not secret_free:
        missing.append("secret_free")
    if profile_count < 1:
        missing.append("profile_count")
    if audited_profiles < 1:
        missing.append("audited_profiles")
    if total_skills < 1:
        missing.append("total_skills")
    note = (
        f"ok={matrix_ok}; checked={checked}; secret_free={secret_free}; profile_count={profile_count}; "
        f"audited_profiles={audited_profiles}; total_skills={total_skills}; "
        f"token_file_marker_count={int(case.get('token_file_marker_count') or 0)}; "
        f"source_counts={source_counts}; missing={missing}"
    )
    return not missing, note


def _continue_reconstruction_ok(case: dict[str, Any]) -> tuple[bool, str]:
    used_previous = case.get("continue_used_previous_request") is True
    response = str(case.get("continue_response") or "")
    history_before = case.get("continue_history_before_response")
    if not isinstance(history_before, list):
        history_before = []
    saw_interrupted_request = any("帮我生成天气 skill 共享报告" in str(content) for content in history_before)
    saw_marker = any("中断或取消" in str(content) for content in history_before)
    expected_response = response == "continued-weather-report-from-interrupted-request"
    ok = case.get("ok") is True and used_previous and expected_response and saw_interrupted_request and saw_marker
    note = (
        f"ok={case.get('ok') is True}; continue_used_previous_request={used_previous}; "
        f"continue_response={response}; saw_interrupted_request={saw_interrupted_request}; "
        f"saw_marker={saw_marker}; history_before_count={len(history_before)}"
    )
    return ok, note


def _arbitrary_followup_resume_ok(case: dict[str, Any]) -> tuple[bool, str]:
    continue_ok, continue_note = _continue_reconstruction_ok(case)
    followup_text = str(case.get("followup_text") or "")
    magic_continue_required = case.get("magic_continue_required") is True
    interrupted_visible = case.get("interrupted_request_visible_to_followup") is True
    marker_visible = case.get("interruption_marker_visible_to_followup") is True
    ok = (
        continue_ok
        and followup_text == "刚才那个报告还在吗？接着跑"
        and not magic_continue_required
        and interrupted_visible
        and marker_visible
    )
    note = (
        f"{continue_note}; followup_text={followup_text}; "
        f"magic_continue_required={magic_continue_required}; "
        f"interrupted_request_visible_to_followup={interrupted_visible}; "
        f"interruption_marker_visible_to_followup={marker_visible}"
    )
    return ok, note


def _production_feedback_interruption_quote_ok(case: dict[str, Any]) -> tuple[bool, str]:
    continue_ok, continue_note = _continue_reconstruction_ok(case)
    quote = str(case.get("production_feedback_quote") or "")
    phrases = case.get("feedback_phrases")
    if not isinstance(phrases, list):
        phrases = []
    coverage = case.get("feedback_phrase_coverage")
    if not isinstance(coverage, dict):
        coverage = {}
    exact_feedback_covered = case.get("first_problem_exact_feedback_covered") is True
    followup_text = str(case.get("followup_text") or "")
    magic_continue_required = case.get("magic_continue_required") is True
    interrupted_visible = case.get("interrupted_request_visible_to_followup") is True
    marker_visible = case.get("interruption_marker_visible_to_followup") is True
    required_phrases = ["会中断", "执行一半突然就没了", "我得说点啥", "才能让他继续"]
    missing_phrases = [
        phrase
        for phrase in required_phrases
        if phrase not in quote or coverage.get(phrase) is not True
    ]
    ok = (
        continue_ok
        and exact_feedback_covered
        and followup_text == "我得说点啥，才能让他继续"
        and not magic_continue_required
        and interrupted_visible
        and marker_visible
        and not missing_phrases
    )
    note = (
        f"{continue_note}; exact_feedback_covered={exact_feedback_covered}; "
        f"followup_text={followup_text}; magic_continue_required={magic_continue_required}; "
        f"interrupted_request_visible_to_followup={interrupted_visible}; "
        f"interruption_marker_visible_to_followup={marker_visible}; "
        f"phrase_count={len(phrases)}; missing_phrases={missing_phrases}"
    )
    return ok, note


def _midrun_exception_recovery_ok(case: dict[str, Any]) -> tuple[bool, str]:
    history_before = case.get("followup_history_before_response")
    if not isinstance(history_before, list):
        history_before = []
    failed_request_visible = case.get("failed_request_visible_to_followup") is True
    failure_marker_visible = case.get("failure_marker_visible_to_followup") is True
    used_failed_request = case.get("followup_used_failed_request") is True
    followup_text = str(case.get("followup_text") or "")
    response = str(case.get("followup_response") or "")
    saw_failed_request = any("天气 skill 半路失败报告" in str(content) for content in history_before)
    saw_failure_marker = any("执行失败或中断" in str(content) for content in history_before)
    ok = (
        case.get("ok") is True
        and failed_request_visible
        and failure_marker_visible
        and used_failed_request
        and followup_text == "刚刚那个执行到一半没了，接着来"
        and response == "resumed-after-midrun-failure"
        and saw_failed_request
        and saw_failure_marker
    )
    note = (
        f"ok={case.get('ok') is True}; followup_text={followup_text}; "
        f"failed_request_visible_to_followup={failed_request_visible}; "
        f"failure_marker_visible_to_followup={failure_marker_visible}; "
        f"followup_used_failed_request={used_failed_request}; "
        f"followup_response={response}; saw_failed_request={saw_failed_request}; "
        f"saw_failure_marker={saw_failure_marker}; history_before_count={len(history_before)}"
    )
    return ok, note


def _persistent_event_dedupe_ok(case: dict[str, Any]) -> tuple[bool, str]:
    same_dispatch_count = int(case.get("same_message_id_dispatch_count") or 0)
    same_suppressed = case.get("same_message_id_duplicate_suppressed") is True
    long_dispatch_count = int(case.get("long_content_dispatch_count") or 0)
    long_suppressed = case.get("long_content_duplicate_suppressed") is True
    processed_event_rows = int(case.get("processed_event_rows") or 0)
    duplicate_processing_completed = case.get("duplicate_processing_completed") is True
    ok = (
        case.get("ok") is True
        and same_dispatch_count == 1
        and same_suppressed
        and long_dispatch_count == 1
        and long_suppressed
        and processed_event_rows >= 2
        and duplicate_processing_completed
    )
    note = (
        f"ok={case.get('ok') is True}; "
        f"same_message_id_dispatch_count={same_dispatch_count}; "
        f"same_message_id_duplicate_suppressed={same_suppressed}; "
        f"long_content_dispatch_count={long_dispatch_count}; "
        f"long_content_duplicate_suppressed={long_suppressed}; "
        f"processed_event_rows={processed_event_rows}; "
        f"duplicate_processing_completed={duplicate_processing_completed}"
    )
    return ok, note


def _clean_skillhub_install_ok(case: dict[str, Any]) -> tuple[bool, str]:
    install_mode = str(case.get("install_mode") or "")
    target_is_symlink = case.get("target_is_symlink") is True
    personal_manifest_source = str(case.get("personal_manifest_source") or "")
    listed_source = str(case.get("listed_source") or "")
    audit_source = str(case.get("audit_source") or "")
    audit_install_mode = str(case.get("audit_install_mode") or "")
    audit_token_files_present = case.get("audit_token_files_present") is True
    ok = (
        case.get("ok") is True
        and install_mode == "symlink"
        and target_is_symlink
        and personal_manifest_source == "personal"
        and listed_source == "personal"
        and audit_source == "personal"
        and audit_install_mode == "symlink"
        and not audit_token_files_present
    )
    note = (
        f"ok={case.get('ok') is True}; install_mode={install_mode}; "
        f"target_is_symlink={target_is_symlink}; "
        f"personal_manifest_source={personal_manifest_source}; listed_source={listed_source}; "
        f"audit_source={audit_source}; audit_install_mode={audit_install_mode}; "
        f"audit_token_files_present={audit_token_files_present}"
    )
    return ok, note


def _new_hire_sync_ok(case: dict[str, Any]) -> tuple[bool, str]:
    missing = []
    initial_stats = case.get("initial_stats") if isinstance(case.get("initial_stats"), dict) else {}
    new_hire_stats = case.get("new_hire_stats") if isinstance(case.get("new_hire_stats"), dict) else {}
    if case.get("ok") is not True:
        missing.append("ok")
    if int(initial_stats.get("created") or 0) < 1:
        missing.append("initial_created")
    if int(new_hire_stats.get("created") or 0) < 1:
        missing.append("new_hire_created")
    if case.get("new_hire_profile_created") is not True:
        missing.append("new_hire_profile_created")
    if case.get("new_hire_weather_install_mode") != "symlink":
        missing.append("weather_symlink")
    if case.get("new_hire_weather_version") != "v2":
        missing.append("weather_v2")
    if case.get("new_hire_lark_calendar_token_policy") != "brokered":
        missing.append("lark_brokered")
    if case.get("new_hire_lark_calendar_share_with_children") is not True:
        missing.append("lark_child_share")
    if case.get("new_hire_finance_skill") is not True:
        missing.append("department_skill")
    if case.get("new_hire_personal_install_preserved_after_resync") is not True:
        missing.append("personal_install_preserved")
    note = (
        f"initial_created={initial_stats.get('created')}; "
        f"new_hire_created={new_hire_stats.get('created')}; "
        f"profile_created={case.get('new_hire_profile_created') is True}; "
        f"weather_mode={case.get('new_hire_weather_install_mode')}; "
        f"weather_version={case.get('new_hire_weather_version')}; "
        f"lark_token_policy={case.get('new_hire_lark_calendar_token_policy')}; "
        f"department_skill={case.get('new_hire_finance_skill') is True}; "
        f"personal_install_preserved={case.get('new_hire_personal_install_preserved_after_resync') is True}; "
        f"missing={missing}"
    )
    return not missing, note


def _real_uat_scope_inventory_ok(case: dict[str, Any]) -> tuple[bool, str]:
    checked = case.get("checked") is True
    secret_free = case.get("secret_free") is True
    valid_count = int(case.get("valid_core_identity_count") or 0)
    results = case.get("results")
    if not isinstance(results, list):
        results = []
    required_scopes = case.get("required_core_scopes")
    if not isinstance(required_scopes, list):
        required_scopes = []
    missing = []
    if case.get("ok") is not True:
        missing.append("ok")
    if not checked:
        missing.append("checked")
    if not secret_free:
        missing.append("secret_free")
    if valid_count < 1:
        missing.append("valid_core_identity")
    if len(required_scopes) < 3:
        missing.append("required_core_scopes")
    note = (
        f"ok={case.get('ok') is True}; checked={checked}; secret_free={secret_free}; "
        f"valid_core_identity_count={valid_count}; inspected={len(results)}; "
        f"required_core_scope_count={len(required_scopes)}; missing={missing}"
    )
    return not missing, note


def audit(evidence_dir: Path, worktree: Path | None = None, gateway_plugin_link: Path | None = None) -> dict[str, Any]:
    matrix_path = evidence_dir / "skills-uat-latest.json"
    dialogue_path = evidence_dir / "lark-dialogue-smoke.jsonl"
    write_path = evidence_dir / "lark-write-smoke.jsonl"
    current_gateway_group_path = evidence_dir / "lark-group-write-rerun-current-gateway.jsonl"
    direct_group_path = evidence_dir / "direct-group-lark-cli-write.json"
    router_group_path = evidence_dir / "router-group-lark-cli-write.json"
    second_problem_trace_path = evidence_dir / "second-problem-trace.json"
    gateway_process_path = evidence_dir / "gateway-process-evidence.json"

    matrix = _load_json(matrix_path)
    cases = {case.get("name"): case for case in matrix.get("cases", [])}
    missing_cases = sorted(REQUIRED_MATRIX_CASES.difference(cases))
    failed_cases = sorted(name for name, case in cases.items() if not case.get("ok"))
    blocked_matrix_failures = _known_blocked_matrix_failures(cases, failed_cases)
    hard_failed_cases = sorted(set(failed_cases) - set(blocked_matrix_failures))

    dialogue_rows = _load_jsonl(dialogue_path)
    write_rows = _load_jsonl(write_path)
    current_group_rows = _load_jsonl(current_gateway_group_path)
    direct_group = _load_json(direct_group_path)
    router_group = _load_json(router_group_path)
    live_plugin_path = gateway_plugin_link or Path.home() / ".hermes" / "profiles" / "multitenancy_router" / "plugins" / "multitenancy"
    live_plugin_target = _resolve_link_target(live_plugin_path)
    current_repo = str((worktree or Path(__file__).resolve().parents[1]).resolve(strict=False))
    live_plugin_mtime = _path_mtime(live_plugin_path, follow_symlinks=False)
    current_group_mtime = _path_mtime(current_gateway_group_path)
    current_group_evidence_after_link = bool(
        current_group_mtime
        and (not live_plugin_mtime or current_group_mtime >= live_plugin_mtime)
    )
    gateway_process_ok, gateway_process_note = _gateway_process_evidence_ok(
        gateway_process_path,
        current_repo=current_repo,
        live_plugin_target=live_plugin_target,
        live_plugin_mtime=live_plugin_mtime,
        current_group_mtime=current_group_mtime,
    )
    live_gateway_exact_branch = live_plugin_target == current_repo and current_group_evidence_after_link and gateway_process_ok

    items: list[dict[str, str]] = []
    matrix_ok = not missing_cases and not hard_failed_cases
    items.append(_item(
        "Construct non-pytest UAT cases for skill distribution, token isolation, interruption, Feishu UAT, and group routing.",
        "covered" if matrix_ok else "failed",
        str(matrix_path),
        f"{len(cases)} matrix cases; missing={missing_cases}; failed={hard_failed_cases}; blocked_failures={blocked_matrix_failures}",
    ))
    distribution_case = cases.get("offline_distribution_audience_symlink_version_self_install") or {}
    version_rollback_ok, version_rollback_note = _symlink_version_rollback_ok(distribution_case)
    items.append(_item(
        "Validate managed symlink version switch and rollback keeps the profile skill path stable.",
        "covered" if distribution_case.get("ok") is True and version_rollback_ok else "failed",
        str(matrix_path),
        version_rollback_note,
    ))
    loader_case = cases.get("offline_hermes_loader_discovers_symlinked_skills") or {}
    loader_ok, loader_note = _hermes_loader_symlink_ok(loader_case)
    items.append(_item(
        "Validate Hermes skill loader discovers symlinked profile skills.",
        "covered" if loader_ok else "failed",
        str(matrix_path),
        loader_note,
    ))
    new_hire_case = cases.get("offline_new_hire_sync_auto_installs_managed_skills") or {}
    new_hire_ok, new_hire_note = _new_hire_sync_ok(new_hire_case)
    items.append(_item(
        "Validate Feishu sync provisions managed skills for new hires and preserves their later personal installs.",
        "covered" if new_hire_ok else "failed",
        str(matrix_path),
        new_hire_note,
    ))
    inventory_case = cases.get("offline_registry_audit_personal_managed_loop_guard") or {}
    inventory_ok, inventory_note = _skill_inventory_ok(inventory_case)
    items.append(_item(
        "Validate cross-profile skill inventory collects managed, personal, and unknown installs.",
        "covered" if inventory_ok else "failed",
        str(matrix_path),
        inventory_note,
    ))
    real_inventory_case = cases.get("real_home_skill_inventory_secret_free") or {}
    real_inventory_ok, real_inventory_note = _real_home_skill_inventory_ok(real_inventory_case)
    items.append(_item(
        "Validate real Hermes skill inventory can collect installed skills across actual profiles without exposing token material.",
        "covered" if real_inventory_ok else "failed",
        str(matrix_path),
        real_inventory_note,
    ))
    webui_child_case = cases.get("offline_webui_child_agent_inherits_skills_not_tokens") or {}
    webui_child_ok = (
        webui_child_case.get("ok") is True
        and webui_child_case.get("weather_skill") is True
        and webui_child_case.get("lark_calendar_skill") is True
        and webui_child_case.get("personal_oauth_skill") is False
        and webui_child_case.get("token_files") == 0
        and webui_child_case.get("uat_files") == 0
        and bool(webui_child_case.get("inherited_from"))
    )
    items.append(_item(
        "Validate WebUI child profile inheritance uses the same skills-not-tokens rule as group child profiles.",
        "covered" if webui_child_ok else "failed",
        str(matrix_path),
        "webui_child_profile="
        f"{webui_child_case.get('webui_child_profile', '')}; "
        f"inherited_from={webui_child_case.get('inherited_from', '')}; "
        f"weather_skill={webui_child_case.get('weather_skill')}; "
        f"lark_calendar_skill={webui_child_case.get('lark_calendar_skill')}; "
        f"personal_oauth_skill={webui_child_case.get('personal_oauth_skill')}; "
        f"token_files={webui_child_case.get('token_files')}; "
        f"uat_files={webui_child_case.get('uat_files')}",
    ))
    context_case = cases.get("offline_context_continuity_private_and_group") or {}
    items.append(_item(
        "Cover the candidate 'second problem' class found in local Hermes notes: private/group context fragmentation.",
        "covered" if context_case.get("ok") is True else "failed",
        str(matrix_path),
        "offline_context_continuity_private_and_group verifies no-tool private and group follow-up messages include prior user+assistant turns.",
    ))
    inflight_scope_case = cases.get("offline_inflight_replacement_scoped_private_group") or {}
    items.append(_item(
        "Cover the production interruption class: same-owner private and group turns must not cancel each other.",
        "covered" if inflight_scope_case.get("ok") is True else "failed",
        str(matrix_path),
        "offline_inflight_replacement_scoped_private_group runs router.handle_async with concurrent private/group long-running turns and verifies both complete.",
    ))
    continue_case = cases.get("offline_continue_turn_reconstructs_interrupted_request") or {}
    continue_ok, continue_note = _continue_reconstruction_ok(continue_case)
    items.append(_item(
        "Validate a continue turn can reconstruct the interrupted request before answering.",
        "covered" if continue_ok else "failed",
        str(matrix_path),
        continue_note,
    ))
    arbitrary_followup_case = cases.get("offline_interruption_arbitrary_followup_resume_context") or {}
    arbitrary_followup_ok, arbitrary_followup_note = _arbitrary_followup_resume_ok(arbitrary_followup_case)
    items.append(_item(
        "Cover arbitrary follow-up resume after an interrupted production-style run; users should not need a magic continue command.",
        "covered" if arbitrary_followup_ok else "failed",
        str(matrix_path),
        arbitrary_followup_note,
    ))
    production_feedback_case = cases.get("offline_production_feedback_interruption_quote_resume") or {}
    production_feedback_ok, production_feedback_note = _production_feedback_interruption_quote_ok(production_feedback_case)
    items.append(_item(
        "Map the production feedback first-problem quote to an executable interruption-resume UAT.",
        "covered" if production_feedback_ok else "failed",
        str(matrix_path),
        production_feedback_note,
    ))
    midrun_case = cases.get("offline_midrun_exception_preserves_recovery_context") or {}
    midrun_ok, midrun_note = _midrun_exception_recovery_ok(midrun_case)
    items.append(_item(
        "Validate mid-run agent exceptions persist a recovery marker so arbitrary follow-up can resume context.",
        "covered" if midrun_ok else "failed",
        str(matrix_path),
        midrun_note,
    ))
    persistent_dedupe_case = cases.get("offline_persistent_event_dedupe_skips_redelivery") or {}
    persistent_dedupe_ok, persistent_dedupe_note = _persistent_event_dedupe_ok(persistent_dedupe_case)
    items.append(_item(
        "Validate persistent inbound event dedupe prevents Feishu redelivery from starting duplicate work.",
        "covered" if persistent_dedupe_ok else "failed",
        str(matrix_path),
        persistent_dedupe_note,
    ))
    slow_idle_case = cases.get("offline_slow_model_idle_feedback_heartbeat") or {}
    items.append(_item(
        "Cover the candidate 'second problem' class from OpenClaw notes: slow model idle waits must keep visible progress instead of going silent.",
        "covered" if slow_idle_case.get("ok") is True else "failed",
        str(matrix_path),
        "offline_slow_model_idle_feedback_heartbeat verifies streaming-card status heartbeats while the agent has not emitted its first content/tool event.",
    ))
    guard_case = cases.get("offline_session_guard_replacement_no_duplicate_dispatch") or {}
    items.append(_item(
        "Cover the production duplicate-dispatch/card class: old replacement cleanup must not remove the newer Feishu session guard.",
        "covered" if guard_case.get("ok") is True else "failed",
        str(matrix_path),
        "offline_session_guard_replacement_no_duplicate_dispatch verifies one guard after replacement, old cleanup preserves the new guard, and replacement cleanup removes it.",
    ))
    skillhub_case = cases.get("offline_personal_skillhub_install_secret_guard") or {}
    items.append(_item(
        "Validate personal SkillHub installs do not expose token-like files through symlinks.",
        "covered" if skillhub_case.get("ok") is True else "failed",
        str(matrix_path),
        "offline_personal_skillhub_install_secret_guard verifies user-initiated symlink installs fall back to filtered copies when the source contains secret-like files.",
    ))
    clean_skillhub_case = cases.get("offline_personal_skillhub_clean_install_symlink") or {}
    clean_skillhub_ok, clean_skillhub_note = _clean_skillhub_install_ok(clean_skillhub_case)
    items.append(_item(
        "Validate clean personal SkillHub installs use symlink mode and are audited as personal installs.",
        "covered" if clean_skillhub_ok else "failed",
        str(matrix_path),
        clean_skillhub_note,
    ))
    user_uat_case = cases.get("real_feishu_uat_user_info") or {}
    user_uat_status = "covered" if user_uat_case.get("ok") is True else (
        "blocked" if "real_feishu_uat_user_info" in blocked_matrix_failures else "failed"
    )
    items.append(_item(
        "Validate real Feishu personal UAT user_info with a valid user token.",
        user_uat_status,
        str(matrix_path),
        "real_feishu_uat_user_info records secret-free status for active user routes and calls /open-apis/authen/v1/user_info when a valid UAT exists."
        + (f" reason={user_uat_case.get('reason')}" if user_uat_case.get("reason") else ""),
    ))
    uat_scope_case = cases.get("real_feishu_uat_scope_inventory_secret_free") or {}
    uat_scope_ok, uat_scope_note = _real_uat_scope_inventory_ok(uat_scope_case)
    uat_scope_status = "covered" if uat_scope_ok else (
        "blocked" if "real_feishu_uat_scope_inventory_secret_free" in blocked_matrix_failures else "failed"
    )
    items.append(_item(
        "Validate real Feishu personal UAT has core lark-cli scopes without exposing token material.",
        uat_scope_status,
        str(matrix_path),
        uat_scope_note,
    ))

    dialogue_ok, missing_dialogue_labels, dialogue_counts = _coverage_by_label(
        dialogue_rows,
        {"group-feishu", "owner-feishu", "owner-webui"},
    )
    items.append(_item(
        "Validate real dialogue-level lark-cli access for group Feishu, personal Feishu, and WebUI personal routes.",
        "covered" if dialogue_ok and len(dialogue_rows) >= 6 else "failed",
        str(dialogue_path),
        f"{len(dialogue_rows)} rows; label_counts={dialogue_counts}; missing_labels={missing_dialogue_labels}",
    ))
    write_ok, missing_write_labels, write_counts = _coverage_by_label(
        write_rows,
        {"owner-webui", "owner-feishu"},
        require_docs_create=True,
    )
    items.append(_item(
        "Validate real personal write paths through WebUI and Feishu private chat.",
        "covered" if write_ok and len(write_rows) >= 2 else "failed",
        str(write_path),
        f"{len(write_rows)} rows; label_counts={write_counts}; missing_labels={missing_write_labels}",
    ))
    current_group_ok, missing_group_labels, current_group_counts = _coverage_by_label(
        current_group_rows,
        {"group-feishu"},
        require_docs_create=True,
    )
    items.append(_item(
        "Validate real current-gateway Feishu group write path after the timeout investigation.",
        "covered" if current_group_ok else "failed",
        str(current_gateway_group_path),
        f"{len(current_group_rows)} rows; label_counts={current_group_counts}; missing_labels={missing_group_labels}",
    ))
    direct_group_permission_skipped = _group_permission_grant_skipped(direct_group)
    items.append(_item(
        "Validate current branch group profile lark-cli bot write path without switching the running gateway symlink.",
        "covered" if (
            direct_group.get("ok") is True
            and direct_group.get("identity_bot") is True
            and direct_group_permission_skipped
            and direct_group.get("document_id")
        ) else "failed",
        str(direct_group_path),
        f"document_id={direct_group.get('document_id', '')}; permission_grant_skipped={direct_group_permission_skipped}",
    ))
    router_group_permission_skipped = _group_permission_grant_skipped(router_group)
    items.append(_item(
        "Validate current branch group router message path can route to the real group profile and create a Feishu doc as bot.",
        "covered" if (
            router_group.get("ok") is True
            and router_group.get("identity_bot") is True
            and router_group_permission_skipped
            and router_group.get("document_id")
            and router_group.get("router_profile") == ((router_group.get("route") or {}).get("group_profile"))
            and router_group.get("router_chat_id") == ((router_group.get("route") or {}).get("chat_id"))
        ) else "failed",
        str(router_group_path),
        f"document_id={router_group.get('document_id', '')}; router_profile={router_group.get('router_profile', '')}; permission_grant_skipped={router_group_permission_skipped}",
    ))

    items.append(_item(
        "Prove the running Feishu gateway process is using this exact branch for live group-message write.",
        "covered" if live_gateway_exact_branch else "blocked",
        "readlink ~/.hermes/profiles/multitenancy_router/plugins/multitenancy + gateway-process-evidence.json + current-gateway group write evidence mtime",
        "current_repo="
        f"{current_repo}; live_plugin_target={live_plugin_target}; "
        f"group_write_evidence_after_link={current_group_evidence_after_link}; "
        f"group_write_mtime={int(current_group_mtime)}; plugin_link_mtime={int(live_plugin_mtime)}; "
        f"{gateway_process_note}",
    ))
    items.append(_feedback_artifacts_item(second_problem_trace_path, cases))
    items.append(_second_problem_trace_item(second_problem_trace_path, cases))

    covered = sum(1 for item in items if item["status"] == "covered")
    failed = sum(1 for item in items if item["status"] == "failed")
    blocked = sum(1 for item in items if item["status"] == "blocked")
    return {
        "evidence_dir": str(evidence_dir),
        "gateway_plugin_link": str(live_plugin_path),
        "expected_worktree": current_repo,
        "evidence_ok": failed == 0,
        "completion_state": "complete" if failed == 0 and blocked == 0 else "incomplete",
        "covered": covered,
        "failed": failed,
        "blocked": blocked,
        "blocked_matrix_failures": blocked_matrix_failures,
        "items": items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skills-uat-completion-audit")
    parser.add_argument("--evidence-dir", type=Path, default=Path("/tmp/hermes-skills-uat"))
    parser.add_argument("--output", type=Path, default=Path("/tmp/hermes-skills-uat/completion-audit-latest.json"))
    parser.add_argument("--worktree", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--gateway-plugin-link",
        type=Path,
        default=Path.home() / ".hermes" / "profiles" / "multitenancy_router" / "plugins" / "multitenancy",
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="Exit non-zero when evidence is parseable but the objective still has blocked items.",
    )
    args = parser.parse_args(argv)

    report = audit(args.evidence_dir, worktree=args.worktree, gateway_plugin_link=args.gateway_plugin_link)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.require_complete and report["completion_state"] != "complete":
        return 2
    return 0 if report["evidence_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
