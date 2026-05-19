#!/usr/bin/env python3
"""Create a reproducible trace for the omitted "second problem" evidence.

The production feedback available in this thread says only "然后第二个问题"
without the actual problem text. This script records where we searched for the
exact phrase and which nearby candidate classes exist in local notes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from collections import deque
from pathlib import Path
from typing import Any, Iterable


DEFAULT_EXACT_PHRASES = ["然后第二个问题"]
DEFAULT_REFERENCED_ARTIFACTS = ["Image #1", "Image #2"]
DEFAULT_CURRENT_FEEDBACK_PHRASES = [
    "可以可以，我正在体验 Hermes",
    "先报个问题，我遇到两次了",
    "我得说点啥，才能让他继续",
    "这个是用户在生产环境中的反馈",
]
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
EXCLUDED_TEXT_NAME_PREFIXES = (
    "completion-audit",
    "review-completion-audit",
    "skills-uat.stdout",
)
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
LARGE_TEXT_FILE_BYTES = 2_000_000
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
PREFILTER_LITERAL_NEEDLES = [
    "[Image:",
    "截图",
    "图片",
    "视觉",
    "上下文割裂",
    "丢失历史",
    "丢历史",
    "context fragmentation",
    "重复 dispatch",
    "两张 Card",
    "重复 Card",
    "duplicate card",
    "沉默",
    "慢模型",
    "继续追问",
    "突然就没",
    "执行一半",
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
    re.compile(r"</?goal_context>"),
    re.compile(r"</?objective>"),
    re.compile(r"Continuation behavior"),
    re.compile(r"without the actual problem text", re.IGNORECASE),
    re.compile(r"Search the repo", re.IGNORECASE),
    re.compile(r"说明占位"),
    re.compile(r"没有问题正文"),
    re.compile(r"没有第二问题正文"),
    re.compile(r"实际正文"),
    re.compile(r"缺.*精确正文"),
    re.compile(r"未给出.*实际正文"),
    re.compile(r"唯一\s*blocker.*实际正文", re.IGNORECASE),
    re.compile(r"唯一\s*blocker.*精确正文", re.IGNORECASE),
    re.compile(r"不能标\s*complete", re.IGNORECASE),
    re.compile(r"no issue text", re.IGNORECASE),
    re.compile(r"\bis still absent\b", re.IGNORECASE),
    re.compile(r"\bbody absent\b", re.IGNORECASE),
]
IMAGE_SOURCE_PATTERN = re.compile(r"\[Image:\s*source:\s*([^\]\n]+)\]")
INTERNAL_CONTEXT_PREFIXES = (
    "<goal_context>",
    "<subagent_notification>",
)


def _valid_historical_image_source(raw_source: str) -> str:
    source = raw_source.strip().replace("\\/", "/")
    if not source or source == "..." or any(marker in source for marker in ('"', "{", "}", "\n", "…")):
        return ""
    path = Path(source)
    if not path.is_absolute() or path.suffix.lower() not in IMAGE_ARTIFACT_SUFFIXES:
        return ""
    if not path.exists() or not path.is_file():
        return ""
    return str(path)


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
            if root.name.startswith(EXCLUDED_TEXT_NAME_PREFIXES):
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
            if path.name.startswith(EXCLUDED_TEXT_NAME_PREFIXES):
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


def _read_matching_text_window(path: Path, needles: list[str], needle_pattern: re.Pattern[str] | None) -> str:
    try:
        if path.stat().st_size <= LARGE_TEXT_FILE_BYTES:
            return _read_text(path)
    except OSError:
        return ""
    if not needles or needle_pattern is None:
        return _read_text(path)

    selected: list[str] = []
    seen_lines: set[int] = set()
    previous: deque[tuple[int, str]] = deque(maxlen=5)
    carry = 0
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line_number, line in enumerate(handle, start=1):
                matched = needle_pattern.search(line) is not None
                if matched:
                    for previous_line_number, previous_line in previous:
                        if previous_line_number not in seen_lines:
                            selected.append(previous_line)
                            seen_lines.add(previous_line_number)
                    if line_number not in seen_lines:
                        selected.append(line)
                        seen_lines.add(line_number)
                    carry = 5
                elif carry > 0:
                    if line_number not in seen_lines:
                        selected.append(line)
                        seen_lines.add(line_number)
                    carry -= 1
                previous.append((line_number, line))
    except OSError:
        return ""
    return "".join(selected)


def _prefilter_needles(
    exact_phrases: list[str],
    referenced_artifacts: list[str],
    current_feedback_phrases: list[str],
) -> list[str]:
    seen: set[str] = set()
    needles: list[str] = []
    for value in [
        *exact_phrases,
        *referenced_artifacts,
        *current_feedback_phrases,
        *PREFILTER_LITERAL_NEEDLES,
    ]:
        if value and value not in seen:
            seen.add(value)
            needles.append(value)
    return needles


def _rg_matching_text_files(
    roots: list[Path],
    all_text_files: list[Path],
    *,
    needles: list[str],
) -> list[Path]:
    """Use ripgrep as a fast native prefilter, falling back to Python scanning."""
    if not needles or shutil.which("rg") is None:
        return all_text_files

    path_by_key = {str(path.resolve(strict=False)): path for path in all_text_files}
    cmd = [
        "rg",
        "--fixed-strings",
        "--files-with-matches",
        "--no-messages",
        "--color",
        "never",
        "--hidden",
    ]
    for dirname in sorted(EXCLUDED_DIRS):
        cmd.extend(["--glob", f"!**/{dirname}/**"])
    for filename in sorted(EXCLUDED_TEXT_FILENAMES):
        cmd.extend(["--glob", f"!**/{filename}"])
    for prefix in EXCLUDED_TEXT_NAME_PREFIXES:
        cmd.extend(["--glob", f"!**/{prefix}*"])
    for needle in needles:
        cmd.extend(["-e", needle])
    cmd.extend(str(path) for path in roots if path.exists())

    try:
        completed = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired):
        return all_text_files

    if completed.returncode not in {0, 1}:
        return all_text_files
    if completed.returncode == 1:
        return []

    matches: list[Path] = []
    seen: set[str] = set()
    for line in completed.stdout.splitlines():
        path = Path(line).expanduser()
        key = str(path.resolve(strict=False))
        original = path_by_key.get(key)
        if original is None or key in seen:
            continue
        seen.add(key)
        matches.append(original)
    return matches


def _structured_user_message(line: str) -> dict[str, Any] | None:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(row, dict):
        return None
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None

    role = str(payload.get("role") or "")
    payload_type = str(payload.get("type") or "")
    row_type = str(row.get("type") or "")
    is_user_message = (
        (row_type == "event_msg" and payload_type == "user_message")
        or (payload_type == "message" and role == "user")
    )
    if not is_user_message:
        return None

    parts = [str(payload.get("message") or payload.get("text") or "")]
    images = list(payload.get("images") or [])
    local_images = list(payload.get("local_images") or [])
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            parts.append(str(item.get("text") or item.get("input_text") or ""))
            if item.get("type") in {"input_image", "image"} or item.get("image_url"):
                images.append(item)
    message = "\n".join(part for part in parts if part)
    if message.lstrip().startswith(INTERNAL_CONTEXT_PREFIXES):
        return None
    return {
        "message": message,
        "image_payload_count": len(images),
        "local_image_payload_count": len(local_images),
    }


def _structured_feedback_payload_matches(
    path: Path,
    text: str,
    *,
    text_phrases: list[str],
    referenced_artifacts: list[str] | None = None,
) -> list[dict[str, Any]]:
    referenced_artifacts = referenced_artifacts or []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        structured = _structured_user_message(line)
        if structured is None:
            continue
        message = str(structured.get("message") or "")
        if not message:
            continue
        if not any(phrase in message for phrase in text_phrases) and not any(
            label in message for label in referenced_artifacts
        ):
            continue
        rows.append({
            "path": str(path),
            "line": line_number,
            "image_payload_count": int(structured.get("image_payload_count") or 0),
            "local_image_payload_count": int(structured.get("local_image_payload_count") or 0),
            "sample": message[:240],
        })
    return rows


def _thread_goal_feedback_matches(
    path: Path,
    text: str,
    *,
    text_phrases: list[str],
    referenced_artifacts: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_objectives: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "thread_goal_updated":
            continue
        goal = payload.get("goal")
        if not isinstance(goal, dict):
            continue
        objective = str(goal.get("objective") or "")
        if not objective or objective in seen_objectives:
            continue
        if not any(phrase in objective for phrase in text_phrases):
            continue
        seen_objectives.add(objective)
        rows.append({
            "path": str(path),
            "line": line_number,
            "objective_hash": hashlib.sha256(objective.encode("utf-8")).hexdigest(),
            "image_placeholder_count": sum(objective.count(label) for label in referenced_artifacts),
            "sample": objective[:240],
        })
    return rows


def _unique_goal_context_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for match in matches:
        objective_hash = str(match.get("objective_hash") or "")
        if not objective_hash:
            continue
        unique.setdefault(objective_hash, match)
    return list(unique.values())


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


def _historical_image_references(
    path: Path,
    text: str,
    referenced_artifacts: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in IMAGE_SOURCE_PATTERN.finditer(text):
        source = _valid_historical_image_source(match.group(1))
        if not source:
            continue
        context = text[max(0, match.start() - 500) : match.end() + 200]
        labels = sorted(label for label in referenced_artifacts if label in context)
        rows.append({
            "path": str(path),
            "source": source,
            "labels": labels,
        })
    return rows


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


def _exact_phrase_source(path: Path, sample: str, followup_text: str) -> str:
    normalized_path = str(path).replace("\\", "/")
    combined = f"{sample}\n{followup_text}"
    if path.name == DEFAULT_FEEDBACK_TRANSCRIPT_NAME or "这个是用户在生产环境中的反馈" in combined:
        return "current_feedback_transcript_placeholder"
    if ".codex/sessions" in normalized_path or ".claude/projects" in normalized_path:
        if "<goal_context>" in combined or "Continuation behavior" in combined:
            return "agent_history_goal_context"
        if "Search the repo" in combined or "without the actual problem text" in combined:
            return "agent_history_investigation_instruction"
        return "agent_history_other"
    if "/AgentOS/" in normalized_path or path.name.upper() == "STATE.MD":
        return "state_journal_documented_absence"
    if "ARCHITECTURE-GUIDE.md" in normalized_path or "/docs/plans/" in normalized_path:
        return "documentation_documented_absence"
    if "没有问题正文" in combined or "缺精确正文" in combined or "is still absent" in combined:
        return "documented_absence_note"
    return "unclassified_phrase_match"


def _source_counts(matches: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in matches:
        source = str(match.get("source") or "unknown")
        counts[source] = counts.get(source, 0) + 1
    return {source: counts[source] for source in sorted(counts)}


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


def _find_label_artifact_candidates(
    label: str,
    roots: Iterable[Path],
    *,
    suffixes: set[str],
) -> list[Path]:
    variants = _artifact_label_variants(label)
    matches: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        root = root.expanduser()
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and not any(part in EXCLUDED_DIRS for part in path.parts)
        )
        for path in candidates:
            if path.suffix.lower() not in suffixes:
                continue
            stem_key = _artifact_key(path.stem)
            name_key = _artifact_key(path.name)
            if stem_key not in variants and name_key not in variants:
                continue
            key = str(path.resolve(strict=False))
            if key in seen:
                continue
            seen.add(key)
            matches.append(path)
    return matches


def _count_raw_image_files(roots: Iterable[Path]) -> int:
    seen: set[str] = set()
    for root in roots:
        root = root.expanduser()
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else (
            path
            for path in root.rglob("*")
            if path.is_file() and not any(part in EXCLUDED_DIRS for part in path.parts)
        )
        for path in candidates:
            if path.suffix.lower() not in IMAGE_ARTIFACT_SUFFIXES:
                continue
            seen.add(str(path.resolve(strict=False)))
    return len(seen)


def _raw_image_artifact_candidates(labels: list[str], roots: Iterable[Path]) -> dict[str, list[str]]:
    return {
        label: [
            str(path)
            for path in _find_label_artifact_candidates(
                label,
                roots,
                suffixes=IMAGE_ARTIFACT_SUFFIXES,
            )
        ]
        for label in labels
    }


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
    current_feedback_phrases: list[str] | None = None,
) -> dict[str, Any]:
    exact_phrases = exact_phrases or DEFAULT_EXACT_PHRASES
    referenced_artifacts = referenced_artifacts or DEFAULT_REFERENCED_ARTIFACTS
    current_feedback_phrases = current_feedback_phrases or DEFAULT_CURRENT_FEEDBACK_PHRASES
    artifact_roots = artifact_roots or []
    artifact_manifest = _load_artifact_manifest(artifact_roots)
    artifact_rows = _artifact_rows(referenced_artifacts, artifact_roots, artifact_manifest)
    artifact_mapping = _artifact_mapping_by_path(artifact_rows)
    search_roots = _unique_paths([*roots, *artifact_roots])
    raw_image_candidates = _raw_image_artifact_candidates(referenced_artifacts, search_roots)
    searched_raw_image_files = _count_raw_image_files(search_roots)
    prefilter_needles = _prefilter_needles(exact_phrases, referenced_artifacts, current_feedback_phrases)
    prefilter_pattern = (
        re.compile("|".join(re.escape(needle) for needle in prefilter_needles))
        if prefilter_needles
        else None
    )
    all_text_files = list(_iter_text_files(search_roots))
    searched_files = len(all_text_files)
    matching_text_files = _rg_matching_text_files(
        search_roots,
        all_text_files,
        needles=prefilter_needles,
    )
    exact_phrase_matches: list[dict[str, Any]] = []
    exact_issue_matches: list[dict[str, Any]] = []
    placeholder_matches: list[dict[str, Any]] = []
    historical_image_references: list[dict[str, Any]] = []
    historical_image_keys: set[tuple[str, str]] = set()
    structured_feedback_payload_matches: list[dict[str, Any]] = []
    current_feedback_structured_payload_matches: list[dict[str, Any]] = []
    current_feedback_goal_context_snapshots: list[dict[str, Any]] = []
    candidate_classes: list[dict[str, Any]] = [
        {"name": item["name"], "match_count": 0, "evidence_files": []}
        for item in CANDIDATE_CLASSES
    ]

    for path in matching_text_files:
        text = _read_matching_text_window(path, prefilter_needles, prefilter_pattern)
        if not text:
            continue
        structured_feedback_payload_matches.extend(
            _structured_feedback_payload_matches(
                path,
                text,
                text_phrases=exact_phrases,
                referenced_artifacts=referenced_artifacts,
            )
        )
        current_feedback_structured_payload_matches.extend(
            _structured_feedback_payload_matches(
                path,
                text,
                text_phrases=current_feedback_phrases,
            )
        )
        current_feedback_goal_context_snapshots.extend(
            _thread_goal_feedback_matches(
                path,
                text,
                text_phrases=current_feedback_phrases,
                referenced_artifacts=referenced_artifacts,
            )
        )
        if not _is_self_reference_exact_path(path):
            for reference in _historical_image_references(path, text, referenced_artifacts):
                key = (reference["path"], reference["source"])
                if key in historical_image_keys:
                    continue
                historical_image_keys.add(key)
                historical_image_references.append(reference)
        for phrase in exact_phrases:
            if phrase in text and not _is_self_reference_exact_path(path):
                followup_text = _line_after_phrase(text, phrase)
                sample = _sample_line(text, re.escape(phrase))
                match = {
                    "path": str(path),
                    "phrase": phrase,
                    "sample": sample,
                    "followup_text": followup_text,
                    "source": _exact_phrase_source(path, sample, followup_text),
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

    current_feedback_goal_context_matches = _unique_goal_context_matches(
        current_feedback_goal_context_snapshots
    )

    return {
        "ok": True,
        "searched_roots": [str(path) for path in search_roots],
        "artifact_roots": [str(path) for path in artifact_roots],
        "searched_files": searched_files,
        "raw_image_search_roots": [str(path) for path in search_roots],
        "searched_raw_image_files": searched_raw_image_files,
        "exact_phrases": exact_phrases,
        "exact_text_found": bool(exact_issue_matches),
        "exact_issue_text_found": bool(exact_issue_matches),
        "exact_match_count": len(exact_issue_matches),
        "exact_phrase_match_count": len(exact_phrase_matches),
        "placeholder_match_count": len(placeholder_matches),
        "exact_phrase_source_counts": _source_counts(exact_phrase_matches),
        "placeholder_source_counts": _source_counts(placeholder_matches),
        "exact_issue_source_counts": _source_counts(exact_issue_matches),
        "exact_issue_text_absent_reason": _exact_issue_text_absent_reason(
            exact_phrase_matches,
            placeholder_matches,
        ) if not exact_issue_matches else "",
        "exact_matches": exact_issue_matches[:20],
        "exact_phrase_matches": exact_phrase_matches[:20],
        "placeholder_matches": placeholder_matches[:20],
        "referenced_artifacts": artifact_rows,
        "raw_image_artifact_candidates": raw_image_candidates,
        "structured_feedback_message_count": len(structured_feedback_payload_matches),
        "structured_feedback_image_payload_count": sum(
            int(match.get("image_payload_count") or 0) for match in structured_feedback_payload_matches
        ),
        "structured_feedback_local_image_payload_count": sum(
            int(match.get("local_image_payload_count") or 0) for match in structured_feedback_payload_matches
        ),
        "structured_feedback_payload_matches": structured_feedback_payload_matches[:20],
        "current_feedback_phrases": current_feedback_phrases,
        "current_feedback_structured_message_count": len(current_feedback_structured_payload_matches),
        "current_feedback_structured_image_payload_count": sum(
            int(match.get("image_payload_count") or 0) for match in current_feedback_structured_payload_matches
        ),
        "current_feedback_structured_local_image_payload_count": sum(
            int(match.get("local_image_payload_count") or 0)
            for match in current_feedback_structured_payload_matches
        ),
        "current_feedback_structured_payload_matches": current_feedback_structured_payload_matches[:20],
        "current_feedback_goal_context_snapshot_count": len(current_feedback_goal_context_snapshots),
        "current_feedback_goal_context_unique_count": len(current_feedback_goal_context_matches),
        "current_feedback_goal_context_match_count": len(current_feedback_goal_context_matches),
        "current_feedback_goal_context_image_placeholder_count": sum(
            int(match.get("image_placeholder_count") or 0) for match in current_feedback_goal_context_matches
        ),
        "current_feedback_goal_context_matches": current_feedback_goal_context_matches[:20],
        "historical_image_reference_count": len(historical_image_references),
        "historical_image_references": historical_image_references[:20],
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
