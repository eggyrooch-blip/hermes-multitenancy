#!/usr/bin/env python3
"""Create a reproducible trace for the omitted "second problem" evidence.

The production feedback available in this thread says only "然后第二个问题"
without the actual problem text. This script records where we searched for the
exact phrase and which nearby candidate classes exist in local notes.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable


DEFAULT_EXACT_PHRASES = ["然后第二个问题"]
DEFAULT_REFERENCED_ARTIFACTS = ["Image #1", "Image #2"]
DEFAULT_ARTIFACT_MANIFEST_NAMES = {"feedback-artifacts.json", "feedback-artifacts.jsonl"}
DEFAULT_FEEDBACK_TRANSCRIPT_NAME = "current-production-feedback.txt"
DEFAULT_ROOTS = [
    Path(__file__).resolve().parents[1],
    Path("/Users/kite/Library/Mobile Documents/iCloud~md~obsidian/Documents/My-Second-Brain/hermes"),
    Path("/Users/kite/Library/Mobile Documents/iCloud~md~obsidian/Documents/My-Second-Brain/OpenClaw"),
]
AGENT_HISTORY_ROOTS = [
    Path.home() / ".codex" / "sessions",
    Path.home() / ".claude" / "projects",
]
EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
    "venv",
}
EXCLUDED_TEXT_FILENAMES = {
    "completion-audit-latest.json",
    "second-problem-trace.json",
    "second-problem-trace.stdout.json",
    "second-problem-trace-with-agent-history.json",
    "skills-uat-latest.json",
}
TEXT_SUFFIXES = {
    "",
    ".html",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".txt",
    ".yaml",
    ".yml",
}
ARTIFACT_SUFFIXES = {
    ".gif",
    ".jpeg",
    ".jpg",
    ".md",
    ".pdf",
    ".png",
    ".txt",
    ".webp",
}
IMAGE_ARTIFACT_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
CANDIDATE_CLASSES = [
    {
        "name": "vision_or_screenshot_failure",
        "patterns": [r"截图", r"vision", r"图片.*失败", r"视觉"],
    },
    {
        "name": "context_fragmentation",
        "patterns": [r"上下文割裂", r"丢(?:失)?历史", r"context fragmentation", r"follow-up"],
    },
    {
        "name": "duplicate_dispatch_card",
        "patterns": [r"重复\s*dispatch", r"两张\s*Card", r"重复.*Card", r"duplicate.*card"],
    },
    {
        "name": "slow_model_idle_wait",
        "patterns": [r"沉默", r"慢模型", r"得.*追问", r"继续追问", r"突然就没", r"执行一半"],
    },
]
SELF_REFERENCE_EXACT_SUFFIXES = {
    "docs/plans/2026-05-19-skills-unified-management-uat-matrix.md",
    "scripts/skills_second_problem_trace.py",
    "tests/test_second_problem_trace.py",
    "tests/test_skills_uat_completion_audit.py",
}
PLACEHOLDER_FOLLOWUP_PATTERNS = [
    re.compile(r"这个是用户在生产环境中的反馈"),
    re.compile(r"你也需要枚举一些测试场景"),
    re.compile(r"</?objective>"),
    re.compile(r"Continuation behavior"),
    re.compile(r"without the actual problem text", re.IGNORECASE),
    re.compile(r"Search the repo", re.IGNORECASE),
    re.compile(r"说明占位"),
    re.compile(r"没有问题正文"),
    re.compile(r"没有第二问题正文"),
    re.compile(r"no issue text", re.IGNORECASE),
]


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        normalized = str(path.expanduser().resolve(strict=False))
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(path)
    return unique


def _iter_text_files(roots: Iterable[Path]) -> Iterable[Path]:
    yielded: set[str] = set()
    for root in roots:
        root = root.expanduser()
        if not root.exists():
            continue
        if root.is_file():
            if root.name in EXCLUDED_TEXT_FILENAMES:
                continue
            if root.suffix in TEXT_SUFFIXES:
                key = str(root.resolve(strict=False))
                if key not in yielded:
                    yielded.add(key)
                    yield root
            continue
        for path in root.rglob("*"):
            if any(part in EXCLUDED_DIRS for part in path.parts):
                continue
            if path.name in EXCLUDED_TEXT_FILENAMES:
                continue
            if path.is_file() and path.suffix in TEXT_SUFFIXES:
                key = str(path.resolve(strict=False))
                if key not in yielded:
                    yielded.add(key)
                    yield path


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _sample_line(text: str, pattern: str) -> str:
    for line in text.splitlines():
        if re.search(pattern, line, flags=re.IGNORECASE):
            return line.strip()[:240]
    return ""


def _line_after_phrase(text: str, phrase: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if phrase not in line:
            continue
        after = line.split(phrase, 1)[1].strip(" ：:，,。")
        if after:
            return after[:240]
        for followup in lines[index + 1 : index + 6]:
            candidate = followup.strip()
            if candidate:
                return candidate[:240]
    return ""


def _looks_like_placeholder_followup(text: str) -> bool:
    if not text:
        return True
    return any(pattern.search(text) for pattern in PLACEHOLDER_FOLLOWUP_PATTERNS)


def _is_self_reference_exact_path(path: Path) -> bool:
    normalized = str(path).replace("\\", "/")
    return any(normalized.endswith(suffix) for suffix in SELF_REFERENCE_EXACT_SUFFIXES)


def _exact_issue_text_absent_reason(
    exact_phrase_matches: list[dict[str, Any]],
    placeholder_matches: list[dict[str, Any]],
) -> str:
    if not exact_phrase_matches:
        return "exact_phrase_not_found"
    if placeholder_matches and len(placeholder_matches) == len(exact_phrase_matches):
        return "phrase_present_but_only_placeholder_followup"
    return ""


def _artifact_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _artifact_label_variants(label: str) -> set[str]:
    normalized = _artifact_key(label)
    variants = {normalized}
    number_match = re.search(r"(\d+)", label)
    if number_match:
        number = number_match.group(1)
        variants.update({f"image-{number}", f"image-{number.zfill(2)}", f"image-{number_match.group(1)}"})
    return variants


def _load_artifact_manifest(artifact_roots: Iterable[Path]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for root in artifact_roots:
        root = root.expanduser()
        candidates: list[Path]
        if root.is_file() and root.name in DEFAULT_ARTIFACT_MANIFEST_NAMES:
            candidates = [root]
        elif root.is_dir():
            candidates = [path for name in DEFAULT_ARTIFACT_MANIFEST_NAMES for path in root.glob(name)]
        else:
            candidates = []
        for manifest in candidates:
            try:
                if manifest.suffix == ".jsonl":
                    payloads = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
                else:
                    raw = json.loads(manifest.read_text(encoding="utf-8"))
                    payloads = raw.get("artifacts", raw) if isinstance(raw, dict) else raw
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(payloads, dict):
                payloads = [payloads]
            if not isinstance(payloads, list):
                continue
            for item in payloads:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "")
                artifact_path = str(item.get("path") or item.get("evidence_path") or "")
                if not label or not artifact_path:
                    continue
                path = Path(artifact_path)
                if not path.is_absolute():
                    path = manifest.parent / path
                row = {
                    "path": path.resolve(strict=False),
                    "content_kind": str(item.get("content_kind") or ""),
                    "mapped_uat_scenarios": [
                        str(value)
                        for value in item.get("mapped_uat_scenarios") or []
                        if isinstance(value, str)
                    ],
                }
                rows[label] = row
    return rows


def _find_artifact(label: str, artifact_roots: Iterable[Path], manifest: dict[str, dict[str, Any]]) -> Path | None:
    manifest_row = manifest.get(label)
    if manifest_row:
        return manifest_row["path"]
    variants = _artifact_label_variants(label)
    candidates: list[Path] = []
    for root in artifact_roots:
        root = root.expanduser()
        if not root.exists():
            continue
        if root.is_file():
            candidates = [root]
        else:
            candidates = sorted(path for path in root.rglob("*") if path.is_file())
        for path in candidates:
            if path.suffix.lower() not in ARTIFACT_SUFFIXES:
                continue
            stem_key = _artifact_key(path.stem)
            name_key = _artifact_key(path.name)
            if stem_key in variants or name_key in variants:
                return path
    return None


def _artifact_rows(
    labels: list[str],
    artifact_roots: Iterable[Path] = (),
    manifest: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    manifest = manifest or {}
    rows: list[dict[str, Any]] = []
    for label in labels:
        artifact = _find_artifact(label, artifact_roots, manifest)
        content_kind = ""
        extracted_text = ""
        mapped_uat_scenarios: list[str] = []
        if artifact is not None:
            manifest_row = manifest.get(label) or {}
            suffix = artifact.suffix.lower()
            content_kind = str(manifest_row.get("content_kind") or "")
            if not content_kind:
                content_kind = "image" if suffix in IMAGE_ARTIFACT_SUFFIXES else "text"
            if content_kind == "text":
                extracted_text = _read_text(artifact)
            mapped_uat_scenarios = list(manifest_row.get("mapped_uat_scenarios") or [])
        row = {
            "label": label,
            "content_available": artifact is not None,
            "evidence_path": str(artifact) if artifact is not None else "",
        }
        if artifact is not None:
            row.update({
                "content_kind": content_kind,
                "extracted_text": extracted_text,
                "mapped_uat_scenarios": mapped_uat_scenarios,
            })
        rows.append(row)
    return rows


def _artifact_mapping_by_path(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for row in rows:
        evidence_path = str(row.get("evidence_path") or "")
        scenarios = row.get("mapped_uat_scenarios")
        if evidence_path and isinstance(scenarios, list):
            mapping[str(Path(evidence_path).resolve(strict=False))] = [
                str(value) for value in scenarios if isinstance(value, str)
            ]
    return mapping


def _read_manifest_items(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    payloads = raw.get("artifacts", raw) if isinstance(raw, dict) else raw
    if isinstance(payloads, dict):
        payloads = [payloads]
    if not isinstance(payloads, list):
        return []
    return [item for item in payloads if isinstance(item, dict)]


def materialize_feedback_transcript(
    *,
    source: Path,
    output_dir: Path,
    labels: list[str],
    mapped_uat_scenarios: list[str],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    transcript = output_dir / DEFAULT_FEEDBACK_TRANSCRIPT_NAME
    transcript.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    manifest = output_dir / "feedback-artifacts.json"
    existing_items = _read_manifest_items(manifest)
    materialized_labels = set(labels)
    retained_items = [item for item in existing_items if str(item.get("label") or "") not in materialized_labels]
    for label in labels:
        retained_items.append({
            "label": label,
            "path": transcript.name,
            "content_kind": "text",
            "mapped_uat_scenarios": mapped_uat_scenarios,
        })
    manifest.write_text(
        json.dumps({"artifacts": retained_items}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return transcript


def build_trace(
    roots: list[Path],
    *,
    exact_phrases: list[str] | None = None,
    referenced_artifacts: list[str] | None = None,
    artifact_roots: list[Path] | None = None,
) -> dict[str, Any]:
    exact_phrases = exact_phrases or DEFAULT_EXACT_PHRASES
    referenced_artifacts = referenced_artifacts or DEFAULT_REFERENCED_ARTIFACTS
    artifact_roots = artifact_roots or []
    artifact_manifest = _load_artifact_manifest(artifact_roots)
    artifact_rows = _artifact_rows(referenced_artifacts, artifact_roots, artifact_manifest)
    artifact_mapping = _artifact_mapping_by_path(artifact_rows)
    search_roots = _unique_paths([*roots, *artifact_roots])
    searched_files = 0
    exact_phrase_matches: list[dict[str, Any]] = []
    exact_issue_matches: list[dict[str, Any]] = []
    placeholder_matches: list[dict[str, Any]] = []
    candidate_classes: list[dict[str, Any]] = [
        {"name": item["name"], "match_count": 0, "evidence_files": []}
        for item in CANDIDATE_CLASSES
    ]

    for path in _iter_text_files(search_roots):
        searched_files += 1
        text = _read_text(path)
        if not text:
            continue
        for phrase in exact_phrases:
            if phrase in text and not _is_self_reference_exact_path(path):
                followup_text = _line_after_phrase(text, phrase)
                match = {
                    "path": str(path),
                    "phrase": phrase,
                    "sample": _sample_line(text, re.escape(phrase)),
                    "followup_text": followup_text,
                }
                mapped_scenarios = artifact_mapping.get(str(path.resolve(strict=False)))
                if mapped_scenarios:
                    match["mapped_uat_scenarios"] = mapped_scenarios
                exact_phrase_matches.append(match)
                if _looks_like_placeholder_followup(followup_text):
                    placeholder_matches.append(match)
                else:
                    exact_issue_matches.append(match)
        for index, candidate in enumerate(CANDIDATE_CLASSES):
            matched_pattern = ""
            for pattern in candidate["patterns"]:
                if re.search(pattern, text, flags=re.IGNORECASE):
                    matched_pattern = pattern
                    break
            if matched_pattern:
                entry = candidate_classes[index]
                entry["match_count"] += 1
                if len(entry["evidence_files"]) < 8:
                    entry["evidence_files"].append({
                        "path": str(path),
                        "pattern": matched_pattern,
                        "sample": _sample_line(text, matched_pattern),
                    })

    return {
        "ok": True,
        "searched_roots": [str(path) for path in search_roots],
        "artifact_roots": [str(path) for path in artifact_roots],
        "searched_files": searched_files,
        "exact_phrases": exact_phrases,
        "exact_text_found": bool(exact_issue_matches),
        "exact_issue_text_found": bool(exact_issue_matches),
        "exact_match_count": len(exact_issue_matches),
        "exact_phrase_match_count": len(exact_phrase_matches),
        "placeholder_match_count": len(placeholder_matches),
        "exact_issue_text_absent_reason": _exact_issue_text_absent_reason(
            exact_phrase_matches,
            placeholder_matches,
        ) if not exact_issue_matches else "",
        "exact_matches": exact_issue_matches[:20],
        "exact_phrase_matches": exact_phrase_matches[:20],
        "placeholder_matches": placeholder_matches[:20],
        "referenced_artifacts": artifact_rows,
        "candidate_classes": candidate_classes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skills-second-problem-trace")
    parser.add_argument("--root", action="append", type=Path, dest="roots")
    parser.add_argument("--include-agent-history", action="store_true")
    parser.add_argument("--exact-phrase", action="append", dest="exact_phrases")
    parser.add_argument("--referenced-artifact", action="append", dest="referenced_artifacts")
    parser.add_argument("--artifact-root", action="append", type=Path, dest="artifact_roots")
    parser.add_argument("--feedback-transcript-file", type=Path)
    parser.add_argument("--feedback-artifact-label", action="append", dest="feedback_artifact_labels")
    parser.add_argument("--feedback-artifact-scenario", action="append", dest="feedback_artifact_scenarios")
    parser.add_argument("--output", type=Path, default=Path("/tmp/hermes-skills-uat/second-problem-trace.json"))
    args = parser.parse_args(argv)

    roots = args.roots or DEFAULT_ROOTS
    artifact_roots = args.artifact_roots or [args.output.parent]
    if args.feedback_transcript_file:
        materialize_feedback_transcript(
            source=args.feedback_transcript_file,
            output_dir=args.output.parent,
            labels=args.feedback_artifact_labels or ["Image #1"],
            mapped_uat_scenarios=args.feedback_artifact_scenarios or [],
        )
        artifact_roots = _unique_paths([*artifact_roots, args.output.parent])
    if args.include_agent_history:
        roots = [*roots, *AGENT_HISTORY_ROOTS]
    report = build_trace(
        roots,
        exact_phrases=args.exact_phrases or DEFAULT_EXACT_PHRASES,
        referenced_artifacts=args.referenced_artifacts or DEFAULT_REFERENCED_ARTIFACTS,
        artifact_roots=artifact_roots,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
