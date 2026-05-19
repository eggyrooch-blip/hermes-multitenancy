from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRACE_PATH = ROOT / "scripts" / "skills_second_problem_trace.py"


def _load_trace_module():
    spec = importlib.util.spec_from_file_location("skills_second_problem_trace", TRACE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_second_problem_trace_records_absent_exact_text_and_candidate_classes(tmp_path: Path):
    trace_mod = _load_trace_module()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "hermes.md").write_text("上下文割裂 导致第二轮问题丢历史\n重复 dispatch 产生两张 CardKit\n", encoding="utf-8")
    (docs / "openclaw.md").write_text("慢模型等待时用户感觉中断，需要继续追问\n", encoding="utf-8")

    report = trace_mod.build_trace([docs], exact_phrases=["然后第二个问题"])

    assert report["ok"] is True
    assert report["searched_files"] == 2
    assert report["exact_text_found"] is False
    classes = {item["name"]: item for item in report["candidate_classes"]}
    assert classes["context_fragmentation"]["match_count"] == 1
    assert classes["duplicate_dispatch_card"]["match_count"] == 1
    assert classes["slow_model_idle_wait"]["match_count"] == 1


def test_second_problem_trace_marks_exact_text_found(tmp_path: Path):
    trace_mod = _load_trace_module()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "feedback.md").write_text("然后第二个问题 是卡片重复出现\n", encoding="utf-8")

    report = trace_mod.build_trace([docs], exact_phrases=["然后第二个问题"])

    assert report["ok"] is True
    assert report["exact_text_found"] is True
    assert report["exact_match_count"] == 1
    assert report["exact_matches"][0]["path"].endswith("feedback.md")


def test_second_problem_trace_distinguishes_placeholder_phrase_without_issue_text(tmp_path: Path):
    trace_mod = _load_trace_module()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "placeholder.md").write_text("然后第二个问题\n\n\n这个是用户在生产环境中的反馈\n", encoding="utf-8")

    report = trace_mod.build_trace([docs], exact_phrases=["然后第二个问题"])

    assert report["ok"] is True
    assert report["exact_phrase_match_count"] == 1
    assert report["exact_issue_text_found"] is False
    assert report["exact_text_found"] is False
    assert report["placeholder_match_count"] == 1
    assert report["exact_issue_text_absent_reason"] == "phrase_present_but_only_placeholder_followup"


def test_second_problem_trace_treats_documented_absence_as_placeholder_not_issue_text(tmp_path: Path):
    trace_mod = _load_trace_module()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "state.md").write_text(
        "当前反馈中“然后第二个问题”后只有说明占位、没有问题正文。\n",
        encoding="utf-8",
    )

    report = trace_mod.build_trace([docs], exact_phrases=["然后第二个问题"])

    assert report["exact_text_found"] is False
    assert report["exact_match_count"] == 0
    assert report["placeholder_match_count"] == 1
    assert report["exact_issue_text_absent_reason"] == "phrase_present_but_only_placeholder_followup"


def test_second_problem_trace_treats_state_journal_missing_body_as_placeholder(tmp_path: Path):
    trace_mod = _load_trace_module()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "STATE.md").write_text(
        "唯一 blocker 是用户未给出“然后第二个问题”的实际正文。验证：make test 566 passed。\n",
        encoding="utf-8",
    )

    report = trace_mod.build_trace([docs], exact_phrases=["然后第二个问题"])

    assert report["exact_text_found"] is False
    assert report["exact_match_count"] == 0
    assert report["placeholder_match_count"] == 1
    assert report["exact_issue_text_absent_reason"] == "phrase_present_but_only_placeholder_followup"


def test_second_problem_trace_treats_state_journal_missing_exact_body_as_placeholder(tmp_path: Path):
    trace_mod = _load_trace_module()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "STATE.md").write_text(
        "唯一 blocker 是‘然后第二个问题’缺精确正文，不能标 complete。\n",
        encoding="utf-8",
    )

    report = trace_mod.build_trace([docs], exact_phrases=["然后第二个问题"])

    assert report["exact_text_found"] is False
    assert report["exact_match_count"] == 0
    assert report["placeholder_match_count"] == 1
    assert report["exact_issue_text_absent_reason"] == "phrase_present_but_only_placeholder_followup"


def test_second_problem_trace_records_referenced_feedback_artifacts_as_unavailable(tmp_path: Path):
    trace_mod = _load_trace_module()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "feedback.md").write_text("先报个问题，会中断，需要继续\n", encoding="utf-8")

    report = trace_mod.build_trace([docs], referenced_artifacts=["Image #1", "Image #2"])

    assert report["ok"] is True
    assert report["referenced_artifacts"] == [
        {"label": "Image #1", "content_available": False, "evidence_path": ""},
        {"label": "Image #2", "content_available": False, "evidence_path": ""},
    ]


def test_second_problem_trace_marks_referenced_artifacts_available_from_artifact_roots(tmp_path: Path):
    trace_mod = _load_trace_module()
    docs = tmp_path / "docs"
    artifacts = tmp_path / "artifacts"
    docs.mkdir()
    artifacts.mkdir()
    (docs / "feedback.md").write_text("先报个问题，会中断，需要继续\n", encoding="utf-8")
    image_one = artifacts / "Image #1.png"
    image_two = artifacts / "image-2.jpg"
    image_one.write_bytes(b"\x89PNG\r\n\x1a\n")
    image_two.write_bytes(b"\xff\xd8\xff")

    report = trace_mod.build_trace(
        [docs],
        referenced_artifacts=["Image #1", "Image #2"],
        artifact_roots=[artifacts],
    )

    assert report["ok"] is True
    assert report["referenced_artifacts"] == [
        {
            "label": "Image #1",
            "content_available": True,
            "evidence_path": str(image_one),
            "content_kind": "image",
            "extracted_text": "",
            "mapped_uat_scenarios": [],
        },
        {
            "label": "Image #2",
            "content_available": True,
            "evidence_path": str(image_two),
            "content_kind": "image",
            "extracted_text": "",
            "mapped_uat_scenarios": [],
        },
    ]
    assert report["raw_image_artifact_candidates"] == {
        "Image #1": [str(image_one)],
        "Image #2": [str(image_two)],
    }


def test_second_problem_trace_searches_roots_for_raw_image_candidates(tmp_path: Path):
    trace_mod = _load_trace_module()
    docs = tmp_path / "docs"
    docs.mkdir()
    image_one = docs / "Image #1.png"
    image_two = docs / "image-2.jpg"
    image_one.write_bytes(b"\x89PNG\r\n\x1a\n")
    image_two.write_bytes(b"\xff\xd8\xff")

    report = trace_mod.build_trace(
        [docs],
        referenced_artifacts=["Image #1", "Image #2"],
    )

    assert report["raw_image_search_roots"] == [str(docs)]
    assert report["searched_raw_image_files"] == 2
    assert report["raw_image_artifact_candidates"] == {
        "Image #1": [str(image_one)],
        "Image #2": [str(image_two)],
    }


def test_second_problem_trace_ingests_text_feedback_artifacts_for_exact_second_problem(tmp_path: Path):
    trace_mod = _load_trace_module()
    docs = tmp_path / "docs"
    artifacts = tmp_path / "artifacts"
    docs.mkdir()
    artifacts.mkdir()
    (artifacts / "production-feedback.md").write_text("然后第二个问题 是卡片重复出现\n", encoding="utf-8")

    report = trace_mod.build_trace(
        [docs],
        exact_phrases=["然后第二个问题"],
        artifact_roots=[artifacts],
    )

    assert report["ok"] is True
    assert report["exact_text_found"] is True
    assert report["exact_matches"][0]["path"].endswith("production-feedback.md")


def test_second_problem_trace_uses_feedback_artifact_manifest_for_paths_and_uat_mapping(tmp_path: Path):
    trace_mod = _load_trace_module()
    docs = tmp_path / "docs"
    artifacts = tmp_path / "artifacts"
    docs.mkdir()
    artifacts.mkdir()
    transcript = artifacts / "second-problem-transcript.txt"
    transcript.write_text("然后第二个问题 是卡片重复出现\n", encoding="utf-8")
    (artifacts / "feedback-artifacts.json").write_text(
        """
        {
          "artifacts": [
            {
              "label": "Image #1",
              "path": "second-problem-transcript.txt",
              "content_kind": "text",
              "mapped_uat_scenarios": ["offline_session_guard_replacement_no_duplicate_dispatch"]
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    report = trace_mod.build_trace(
        [docs],
        exact_phrases=["然后第二个问题"],
        referenced_artifacts=["Image #1", "Image #2"],
        artifact_roots=[artifacts],
    )

    assert report["ok"] is True
    assert report["referenced_artifacts"][0] == {
        "label": "Image #1",
        "content_available": True,
        "evidence_path": str(transcript),
        "content_kind": "text",
        "extracted_text": "然后第二个问题 是卡片重复出现\n",
        "mapped_uat_scenarios": ["offline_session_guard_replacement_no_duplicate_dispatch"],
    }
    assert report["referenced_artifacts"][1] == {"label": "Image #2", "content_available": False, "evidence_path": ""}
    assert report["exact_text_found"] is True
    assert report["exact_matches"][0]["mapped_uat_scenarios"] == [
        "offline_session_guard_replacement_no_duplicate_dispatch"
    ]
    assert report["raw_image_artifact_candidates"] == {"Image #1": [], "Image #2": []}


def test_second_problem_trace_records_historical_image_references_without_accepting_them(tmp_path: Path):
    trace_mod = _load_trace_module()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    stale_image = tmp_path / "clipboard-old.png"
    stale_image.write_bytes(b"\x89PNG\r\n\x1a\n")
    (sessions / "rollout.jsonl").write_text(
        f"USER: 参照大纲，[Image #2] goods 部门。\n"
        f"USER: [Image: source: {stale_image}]\n",
        encoding="utf-8",
    )

    report = trace_mod.build_trace(
        [sessions],
        referenced_artifacts=["Image #1", "Image #2"],
    )

    assert report["referenced_artifacts"] == [
        {"label": "Image #1", "content_available": False, "evidence_path": ""},
        {"label": "Image #2", "content_available": False, "evidence_path": ""},
    ]
    assert report["raw_image_artifact_candidates"] == {"Image #1": [], "Image #2": []}
    assert report["historical_image_reference_count"] == 1
    assert report["historical_image_references"] == [
        {
            "path": str(sessions / "rollout.jsonl"),
            "source": str(stale_image),
            "labels": ["Image #2"],
        }
    ]


def test_second_problem_trace_ignores_self_reference_historical_image_references(tmp_path: Path):
    trace_mod = _load_trace_module()
    repo_like = tmp_path / "repo"
    tests = repo_like / "tests"
    tests.mkdir(parents=True)
    (tests / "test_second_problem_trace.py").write_text(
        "USER: 参照大纲，[Image #2] goods 部门。\n"
        "USER: [Image: source: /tmp/stale-self-reference.png]\n",
        encoding="utf-8",
    )

    report = trace_mod.build_trace(
        [repo_like],
        referenced_artifacts=["Image #1", "Image #2"],
    )

    assert report["historical_image_reference_count"] == 0
    assert report["historical_image_references"] == []


def test_second_problem_trace_ignores_missing_historical_image_sources(tmp_path: Path):
    trace_mod = _load_trace_module()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "rollout.jsonl").write_text(
        "USER: 参照大纲，[Image #2] goods 部门。\n"
        "USER: [Image: source: /tmp/stale-self-reference.png]\n"
        "USER: [Image: source: ...]\n",
        encoding="utf-8",
    )

    report = trace_mod.build_trace(
        [sessions],
        referenced_artifacts=["Image #1", "Image #2"],
    )

    assert report["historical_image_reference_count"] == 0
    assert report["historical_image_references"] == []


def test_second_problem_trace_cli_materializes_feedback_transcript_manifest(tmp_path: Path):
    trace_mod = _load_trace_module()
    docs = tmp_path / "docs"
    output = tmp_path / "evidence" / "second-problem-trace.json"
    source_transcript = tmp_path / "production-feedback.txt"
    docs.mkdir()
    source_transcript.write_text("然后第二个问题\n这个是用户在生产环境中的反馈\n", encoding="utf-8")

    exit_code = trace_mod.main([
        "--root",
        str(docs),
        "--output",
        str(output),
        "--feedback-transcript-file",
        str(source_transcript),
        "--feedback-artifact-label",
        "Image #1",
        "--feedback-artifact-label",
        "Image #2",
        "--feedback-artifact-scenario",
        "offline_session_guard_replacement_no_duplicate_dispatch",
    ])

    materialized = output.parent / "current-production-feedback.txt"
    manifest = output.parent / "feedback-artifacts.json"
    assert exit_code == 0
    assert materialized.read_text(encoding="utf-8") == source_transcript.read_text(encoding="utf-8")
    assert manifest.exists()
    report = trace_mod.json.loads(output.read_text(encoding="utf-8"))
    assert report["referenced_artifacts"][0]["content_available"] is True
    assert report["referenced_artifacts"][0]["evidence_path"] == str(materialized)
    assert report["referenced_artifacts"][0]["mapped_uat_scenarios"] == [
        "offline_session_guard_replacement_no_duplicate_dispatch"
    ]
    assert report["referenced_artifacts"][1]["content_available"] is True
    assert report["referenced_artifacts"][1]["evidence_path"] == str(materialized)
    assert report["referenced_artifacts"][1]["mapped_uat_scenarios"] == [
        "offline_session_guard_replacement_no_duplicate_dispatch"
    ]
    assert report["exact_text_found"] is False
    assert report["placeholder_match_count"] == 1


def test_second_problem_trace_ignores_self_reference_exact_phrase_matches(tmp_path: Path):
    trace_mod = _load_trace_module()
    repo_like = tmp_path / "repo"
    tests = repo_like / "tests"
    tmp_evidence = tmp_path / "evidence"
    session_root = tmp_path / "sessions"
    tests.mkdir(parents=True)
    tmp_evidence.mkdir()
    session_root.mkdir()
    (tests / "test_skills_uat_completion_audit.py").write_text(
        '"phrase": "然后第二个问题",\n',
        encoding="utf-8",
    )
    (tmp_evidence / "second-problem-trace-with-agent-history.json").write_text(
        '"phrase": "然后第二个问题",\n',
        encoding="utf-8",
    )
    (tmp_evidence / "second-problem-trace.stdout.json").write_text(
        '"phrase": "然后第二个问题",\n',
        encoding="utf-8",
    )
    (session_root / "rollout.jsonl").write_text(
        '{"type":"response_item","payload":{"output":"然后第二个问题 without the actual problem text. Search the repo"}}\n',
        encoding="utf-8",
    )

    report = trace_mod.build_trace(
        [repo_like, session_root],
        exact_phrases=["然后第二个问题"],
        artifact_roots=[tmp_evidence],
    )

    assert report["exact_text_found"] is False
    assert report["exact_match_count"] == 0


def test_second_problem_trace_marks_phrase_absent_separately_from_blank_body(tmp_path: Path):
    trace_mod = _load_trace_module()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "feedback.md").write_text("先报个问题，执行一半突然就没了\n", encoding="utf-8")

    report = trace_mod.build_trace([docs], exact_phrases=["然后第二个问题"])

    assert report["exact_text_found"] is False
    assert report["exact_phrase_match_count"] == 0
    assert report["exact_issue_text_absent_reason"] == "exact_phrase_not_found"


def test_second_problem_trace_counts_structured_user_image_payloads_for_feedback_message(tmp_path: Path):
    trace_mod = _load_trace_module()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "rollout.jsonl").write_text(
        trace_mod.json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "[Image #1] [Image #2]\n然后第二个问题 是卡片重复出现",
                    "images": [
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                        {"type": "input_image", "image_url": "data:image/png;base64,BBBB"},
                    ],
                    "local_images": [],
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    report = trace_mod.build_trace(
        [sessions],
        exact_phrases=["然后第二个问题"],
        referenced_artifacts=["Image #1", "Image #2"],
    )

    assert report["structured_feedback_message_count"] == 1
    assert report["structured_feedback_image_payload_count"] == 2
    assert report["structured_feedback_local_image_payload_count"] == 0
    assert report["structured_feedback_payload_matches"] == [
        {
            "path": str(sessions / "rollout.jsonl"),
            "line": 1,
            "image_payload_count": 2,
            "local_image_payload_count": 0,
            "sample": "[Image #1] [Image #2]\n然后第二个问题 是卡片重复出现",
        }
    ]


def test_second_problem_trace_records_structured_feedback_text_when_no_image_payloads(tmp_path: Path):
    trace_mod = _load_trace_module()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "rollout.jsonl").write_text(
        trace_mod.json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "[Image #1] [Image #2]\n然后第二个问题\n\n这个是用户在生产环境中的反馈",
                    "images": [],
                    "local_images": [],
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    report = trace_mod.build_trace(
        [sessions],
        exact_phrases=["然后第二个问题"],
        referenced_artifacts=["Image #1", "Image #2"],
    )

    assert report["exact_text_found"] is False
    assert report["structured_feedback_message_count"] == 1
    assert report["structured_feedback_image_payload_count"] == 0
    assert report["structured_feedback_local_image_payload_count"] == 0


def test_second_problem_trace_counts_current_feedback_structured_payloads_separately(tmp_path: Path):
    trace_mod = _load_trace_module()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    historical_line = trace_mod.json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "[Image #1] 旧会话里的无关截图",
                "images": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}],
                "local_images": [],
            },
        },
        ensure_ascii=False,
    )
    current_line = trace_mod.json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "先报个问题，我遇到两次了，就是会中断，执行一半突然就没了。",
                "images": [],
                "local_images": [],
            },
        },
        ensure_ascii=False,
    )
    (sessions / "rollout.jsonl").write_text(f"{historical_line}\n{current_line}\n", encoding="utf-8")

    report = trace_mod.build_trace(
        [sessions],
        referenced_artifacts=["Image #1", "Image #2"],
    )

    assert report["structured_feedback_message_count"] == 1
    assert report["structured_feedback_image_payload_count"] == 1
    assert report["current_feedback_structured_message_count"] == 1
    assert report["current_feedback_structured_image_payload_count"] == 0
    assert report["current_feedback_structured_local_image_payload_count"] == 0
    assert report["current_feedback_structured_payload_matches"] == [
        {
            "path": str(sessions / "rollout.jsonl"),
            "line": 2,
            "image_payload_count": 0,
            "local_image_payload_count": 0,
            "sample": "先报个问题，我遇到两次了，就是会中断，执行一半突然就没了。",
        }
    ]
