"""Session-scoped tool-use trace capture."""
from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .secret_redaction import is_sensitive_name, redact_inline_secrets

TRACE_TTL_MS = 30 * 60 * 1000
MAX_SESSION_TRACES = 128
MAX_STEPS_PER_SESSION = 256
STEP_RUNNING_TIMEOUT_MS = 5 * 60 * 1000
GENERIC_STRING_LIMIT = 512
RESULT_STRING_LIMIT = 1024
COMMAND_STRING_LIMIT = 4096
PATH_STRING_LIMIT = 2048

_URL_SECRET_QUERY_RE = re.compile(
    r"(?P<prefix>[?&])(?P<key>[A-Za-z0-9_.-]*(?:api_)?(?:secret|token|password|key|credential|bearer|auth)[A-Za-z0-9_.-]*)=[^&#]*",
    re.IGNORECASE,
)
_SENSITIVE_KEY_RE = re.compile(
    r"(session_id|client_secret|database_url|connection_string|private_key|signing_key|encryption_key|access_key)",
    re.IGNORECASE,
)

_session_traces: dict[str, dict[str, Any]] = {}


def _time_ms() -> int:
    return int(time.time() * 1000)


def truncate(value: str, maxlen: int) -> str:
    if len(value) <= maxlen:
        return value
    return value[: maxlen - 3] + "..."


def _redact_url_query_secrets(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}{match.group('key')}=[redacted]"

    return _URL_SECRET_QUERY_RE.sub(_replace, value)


def _string_limit_for(*, key: str | None, source: str | None) -> int:
    key_name = (key or "").lower()
    source_name = (source or "").lower()
    if key_name in {"command", "script", "description", "prompt", "task"}:
        return COMMAND_STRING_LIMIT
    if key_name in {"path", "file", "file_path", "url", "uri", "cwd", "folder", "dir"}:
        return PATH_STRING_LIMIT
    if source_name == "result":
        return RESULT_STRING_LIMIT
    return GENERIC_STRING_LIMIT


def sanitize_trace_value(
    value: Any,
    depth: int = 0,
    *,
    source: str | None = None,
    key: str | None = None,
) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = _redact_url_query_secrets(value)
        if (key or "").lower() in {"command", "script", "description", "prompt", "task"}:
            text = redact_inline_secrets(text)
        return truncate(text, _string_limit_for(key=key, source=source))
    if depth >= 2:
        return "[truncated]"
    if isinstance(value, list):
        return [
            sanitize_trace_value(item, depth + 1, source=source, key=key)
            for item in value[:8]
        ]
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for item_key, item_value in list(value.items())[:12]:
            key_text = str(item_key)
            if is_sensitive_name(key_text) or _SENSITIVE_KEY_RE.search(key_text):
                sanitized[key_text] = "[redacted]"
                continue
            sanitized[key_text] = sanitize_trace_value(
                item_value,
                depth + 1,
                source=source,
                key=key_text,
            )
        return sanitized
    text = _redact_url_query_secrets(redact_inline_secrets(str(value)))
    return truncate(text, _string_limit_for(key=key, source=source))


def _normalize_tool_name(tool_name: str) -> str:
    return tool_name.strip().lower()


def _fingerprint_params(tool_params: Any) -> str:
    sanitized = sanitize_trace_value(tool_params, source="params")
    return json.dumps(sanitized, sort_keys=True, separators=(",", ":"))


def _prune_session_traces(now_ms: int | None = None) -> None:
    now = now_ms if now_ms is not None else _time_ms()
    expired = [
        session_key
        for session_key, state in _session_traces.items()
        if now - int(state.get("updated_at", 0)) > TRACE_TTL_MS
    ]
    for session_key in expired:
        _session_traces.pop(session_key, None)

    if len(_session_traces) <= MAX_SESSION_TRACES:
        return

    ordered = sorted(
        _session_traces.items(),
        key=lambda item: int(item[1].get("updated_at", 0)),
    )
    for session_key, _ in ordered[: len(_session_traces) - MAX_SESSION_TRACES]:
        _session_traces.pop(session_key, None)


def start_tool_use_trace_run(session_key: str | None) -> None:
    if not session_key:
        return
    now = _time_ms()
    _prune_session_traces(now)
    _session_traces[session_key] = {
        "next_seq": 1,
        "updated_at": now,
        "steps": [],
        "current_run_id": None,
    }


def clear_tool_use_trace_run(session_key: str | None) -> None:
    if not session_key:
        return
    _session_traces.pop(session_key, None)


def has_tool_use_trace_run(session_key: str | None) -> bool:
    return bool(session_key) and session_key in _session_traces


def record_tool_use_start(
    session_key: str | None,
    tool_name: str | None,
    tool_params: Any = None,
    tool_call_id: str | None = None,
    run_id: str | None = None,
) -> None:
    if not session_key or not tool_name:
        return
    state = _session_traces.get(session_key)
    if state is None:
        return
    if run_id:
        if state["current_run_id"] is None:
            state["current_run_id"] = run_id
        elif state["current_run_id"] != run_id:
            return

    now = _time_ms()
    steps = state["steps"]
    while len(steps) >= MAX_STEPS_PER_SESSION:
        steps.pop(0)

    seq = int(state["next_seq"])
    steps.append(
        {
            "id": str(seq),
            "seq": seq,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "run_id": run_id,
            "params": sanitize_trace_value(tool_params, source="params"),
            "status": "running",
            "started_at": now,
        }
    )
    state["next_seq"] = seq + 1
    state["updated_at"] = now


def _find_pending_step(
    steps: list[dict[str, Any]],
    *,
    tool_name: str,
    tool_params: Any,
    tool_call_id: str | None,
) -> dict[str, Any] | None:
    running_steps = [step for step in steps if step.get("status") == "running"]
    if tool_call_id:
        for step in reversed(running_steps):
            if step.get("tool_call_id") == tool_call_id:
                return step

    if tool_params is not None:
        target_fingerprint = _fingerprint_params(tool_params)
        for step in reversed(running_steps):
            if _normalize_tool_name(str(step.get("tool_name", ""))) != tool_name:
                continue
            if _fingerprint_params(step.get("params")) == target_fingerprint:
                return step

    for step in reversed(running_steps):
        if _normalize_tool_name(str(step.get("tool_name", ""))) == tool_name:
            return step
    return None


def record_tool_use_end(
    session_key: str | None,
    tool_name: str | None,
    *,
    tool_params: Any = None,
    tool_call_id: str | None = None,
    run_id: str | None = None,
    result: Any = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    if not session_key or not tool_name:
        return
    state = _session_traces.get(session_key)
    if state is None:
        return
    if run_id and state["current_run_id"] is not None and state["current_run_id"] != run_id:
        return

    now = _time_ms()
    normalized_name = _normalize_tool_name(tool_name)
    pending = _find_pending_step(
        state["steps"],
        tool_name=normalized_name,
        tool_params=tool_params,
        tool_call_id=tool_call_id,
    )

    sanitized_params = sanitize_trace_value(tool_params, source="params")
    sanitized_result = sanitize_trace_value(result, source="result")
    truncated_error = truncate(error, 160) if error else None
    if pending is not None:
        pending["status"] = "error" if error else "success"
        pending["result"] = sanitized_result
        pending["error"] = truncated_error
        pending["duration_ms"] = duration_ms
        pending["finished_at"] = now
        if pending.get("params") is None and sanitized_params is not None:
            pending["params"] = sanitized_params
        state["updated_at"] = now
        return

    steps = state["steps"]
    while len(steps) >= MAX_STEPS_PER_SESSION:
        steps.pop(0)
    seq = int(state["next_seq"])
    steps.append(
        {
            "id": str(seq),
            "seq": seq,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "run_id": run_id,
            "params": sanitized_params,
            "status": "error" if error else "success",
            "started_at": now,
            "finished_at": now,
            "duration_ms": duration_ms,
            "result": sanitized_result,
            "error": truncated_error,
        }
    )
    state["next_seq"] = seq + 1
    state["updated_at"] = now


def get_tool_use_trace_steps(session_key: str | None) -> list[dict[str, Any]]:
    if not session_key:
        return []
    state = _session_traces.get(session_key)
    if state is None:
        return []
    now = _time_ms()
    if now - int(state.get("updated_at", 0)) > TRACE_TTL_MS:
        _session_traces.pop(session_key, None)
        return []

    steps: list[dict[str, Any]] = []
    for step in state["steps"]:
        snapshot = copy.deepcopy(step)
        if (
            snapshot.get("status") == "running"
            and now - int(snapshot.get("started_at", now)) > STEP_RUNNING_TIMEOUT_MS
        ):
            snapshot["status"] = "error"
            snapshot["error"] = "timed out"
            snapshot["finished_at"] = now
        steps.append(snapshot)
    return steps


def _reset_for_testing() -> None:
    _session_traces.clear()
