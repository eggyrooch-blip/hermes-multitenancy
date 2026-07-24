from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .audit import AuditLoadResult, Turn, build_turns, load_audit_rows, parse_timestamp
from .classify import SCENARIOS, annotate_turn

DEFAULT_AUDIT_PATH = Path("/var/log/hermes/conversation-audit.jsonl")
DEFAULT_ROUTING_DB = Path.home() / ".hermes" / "multitenancy.db"
COMPLETION_PROXY_NOTE = (
    "Completion is a proxy metric based on assistant final stop replies and explicit failure text; "
    "it is not a real user satisfaction or business success measurement."
)
COMPLETION_PROXY_BLIND_SPOT = (
    "tool-call-only turns without a later finish_reason=stop assistant row are counted as unfinished, "
    "even when the underlying tool action may have succeeded."
)
ACTIVE_USER_PROXY_NOTE = (
    "DAU is an active profiles proxy: it counts unique Hermes profiles seen in the audit window, "
    "not de-duplicated natural people."
)


def _rate(part: int, total: int) -> float:
    return round((part / total) * 100, 1) if total else 0.0


def _top(counter: Counter[str], limit: int = 20) -> list[list[Any]]:
    return [[key, count] for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def load_routing_summary(db_path: Path | None) -> dict[str, Any]:
    if db_path is None or not db_path.exists():
        return {"active_by_kind": {}, "profile_kind": {}, "available": False}
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return {"active_by_kind": {}, "profile_kind": {}, "available": False}
    try:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
        if "multitenancy_routing" not in tables:
            return {"active_by_kind": {}, "profile_kind": {}, "available": False}
        columns = [row[1] for row in conn.execute("pragma table_info(multitenancy_routing)")]
        profile_col = "profile_name" if "profile_name" in columns else "profile"
        if profile_col not in columns or "kind" not in columns or "active" not in columns:
            return {"active_by_kind": {}, "profile_kind": {}, "available": False}
        active_by_kind: Counter[str] = Counter()
        profile_kind: dict[str, str] = {}
        for row in conn.execute(f"select {profile_col} as profile, kind from multitenancy_routing where active=1"):
            profile = str(row["profile"] or "")
            kind = str(row["kind"] or "unknown")
            active_by_kind[kind] += 1
            if profile:
                profile_kind[profile] = kind
        return {
            "active_by_kind": dict(sorted(active_by_kind.items())),
            "profile_kind": profile_kind,
            "available": True,
        }
    except sqlite3.Error:
        return {"active_by_kind": {}, "profile_kind": {}, "available": False}
    finally:
        conn.close()


def routing_summary_from_records(records: list[dict[str, Any]] | None) -> dict[str, Any]:
    if not records:
        return {"active_by_kind": {}, "profile_kind": {}, "available": False}
    active_by_kind: Counter[str] = Counter()
    profile_kind: dict[str, str] = {}
    for record in records:
        active = record.get("active", 1)
        if str(active).lower() in {"0", "false", "no", "off"}:
            continue
        profile = str(record.get("profile_name") or record.get("profile") or "")
        kind = str(record.get("kind") or "unknown")
        active_by_kind[kind] += 1
        if profile:
            profile_kind[profile] = kind
    return {
        "active_by_kind": dict(sorted(active_by_kind.items())),
        "profile_kind": profile_kind,
        "available": True,
    }


def _audit_load_from_records(records: list[dict[str, Any]]) -> AuditLoadResult:
    rows: list[dict[str, Any]] = []
    bad = 0
    first: datetime | None = None
    last: datetime | None = None
    for record in records:
        if not isinstance(record, dict):
            bad += 1
            continue
        if record.get("event_type", "conversation_message") != "conversation_message":
            continue
        timestamp = parse_timestamp(record.get("@timestamp"))
        if timestamp is None:
            bad += 1
            continue
        row = dict(record)
        row["_dt"] = timestamp
        rows.append(row)
        first = timestamp if first is None or timestamp < first else first
        last = timestamp if last is None or timestamp > last else last
    rows.sort(key=lambda row: (row["_dt"], str(row.get("session_id") or ""), str(row.get("message_id") or "")))
    return AuditLoadResult(rows=rows, total_lines=len(records), bad_lines=bad, first_timestamp=first, last_timestamp=last)


def _window_turns(turns: list[Turn], last: datetime | None, days: int | None) -> list[Turn]:
    if last is None or days is None:
        return list(turns)
    cutoff = last - timedelta(days=days)
    return [turn for turn in turns if turn.timestamp >= cutoff]


def _window_key(days: int | None) -> str:
    return "all" if days is None else f"{days}d"


def _window_metrics(turns: list[Turn], profile_kind: dict[str, str]) -> dict[str, Any]:
    profiles = {turn.profile for turn in turns}
    sessions = {turn.session_id for turn in turns}
    final_stop = sum(1 for turn in turns if turn.has_final_stop)
    explicit_failure = sum(1 for turn in turns if turn.explicit_failure)
    completion_proxy = sum(1 for turn in turns if turn.has_final_stop and not turn.explicit_failure)
    success_signal = sum(1 for turn in turns if turn.success_signal)
    by_platform: dict[str, set[str]] = defaultdict(set)
    turns_by_platform: Counter[str] = Counter()
    chat_types_by_profile: dict[str, set[str]] = defaultdict(set)
    for turn in turns:
        by_platform[turn.platform or "unknown"].add(turn.profile)
        turns_by_platform[turn.platform or "unknown"] += 1
        chat_types_by_profile[turn.profile].add((turn.chat_type or "").lower())
    agent_like = {profile for profile in profiles if profile_kind.get(profile) == "agent"}
    group_like = {
        profile
        for profile in profiles
        if profile not in agent_like
        and (
            profile_kind.get(profile) == "group"
            or str(profile).startswith("feishu_group_")
            or any("group" in chat_type for chat_type in chat_types_by_profile.get(profile, set()))
        )
    }
    user_like = {
        profile
        for profile in profiles
        if profile not in group_like
        and profile not in agent_like
        and profile_kind.get(profile, "user") in {"user", "unknown", ""}
    }
    counts = Counter(turn.profile for turn in turns)
    total_turns = sum(counts.values())
    top10 = sum(count for _profile, count in counts.most_common(10))
    top20 = sum(count for _profile, count in counts.most_common(20))
    return {
        "turns": len(turns),
        "sessions": len(sessions),
        "active_profiles": len(profiles),
        "active_user_like_profiles": len(user_like),
        "active_group_like_profiles": len(group_like),
        "active_agent_like_profiles": len(agent_like),
        "active_profiles_by_kind_proxy": {
            "agent": len(agent_like),
            "group": len(group_like),
            "user": len(user_like),
        },
        "active_profiles_by_platform": {platform: len(items) for platform, items in sorted(by_platform.items())},
        "turns_by_platform": dict(sorted(turns_by_platform.items())),
        "final_stop": final_stop,
        "final_stop_rate": _rate(final_stop, len(turns)),
        "explicit_failures": explicit_failure,
        "explicit_failure_rate": _rate(explicit_failure, len(turns)),
        "completion_proxy": completion_proxy,
        "completion_proxy_rate": _rate(completion_proxy, len(turns)),
        "success_signals": success_signal,
        "success_signal_rate": _rate(success_signal, len(turns)),
        "top10_turn_share": _rate(top10, total_turns),
        "top20_turn_share": _rate(top20, total_turns),
        "profiles_with_3plus_turns": sum(1 for count in counts.values() if count >= 3),
        "profiles_with_10plus_turns": sum(1 for count in counts.values() if count >= 10),
    }


def _scenario_metrics(turns: list[Turn]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    by_scenario: dict[str, list[Turn]] = defaultdict(list)
    for turn in turns:
        by_scenario[turn.scenario].append(turn)
    for scenario in SCENARIOS:
        items = by_scenario.get(scenario, [])
        if not items:
            continue
        final_stop = sum(1 for item in items if item.has_final_stop)
        failures = sum(1 for item in items if item.explicit_failure)
        completion_proxy = sum(1 for item in items if item.has_final_stop and not item.explicit_failure)
        result[scenario] = {
            "turns": len(items),
            "final_stop_rate": _rate(final_stop, len(items)),
            "failures": failures,
            "failure_rate": _rate(failures, len(items)),
            "completion_proxy_rate": _rate(completion_proxy, len(items)),
        }
    return result


def _counter_metrics(turns: list[Turn], *, include_profiles: bool) -> dict[str, Any]:
    tools: Counter[str] = Counter()
    skills: Counter[str] = Counter()
    lark_commands: Counter[str] = Counter()
    lark_modes: Counter[str] = Counter()
    terminal_themes: Counter[str] = Counter()
    profiles: Counter[str] = Counter()
    for turn in turns:
        profiles[redact_text(turn.profile)] += 1
        tools.update(turn.tools)
        skills.update(turn.skills)
        lark_commands.update(turn.lark_commands)
        lark_modes.update(turn.lark_modes)
        terminal_themes.update(turn.terminal_themes)
    result = {
        "tools": _top(tools),
        "skills": _top(skills),
        "lark_commands": _top(lark_commands),
        "lark_modes": _top(lark_modes),
        "terminal_themes": _top(terminal_themes),
    }
    if include_profiles:
        result["top_active_profiles"] = _top(profiles)
    return result


def _failure_categories(turns: list[Turn]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for turn in turns:
        if turn.failure_category:
            counter[turn.failure_category] += 1
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _dau(turns: list[Turn], last: datetime | None, days: int) -> list[dict[str, Any]]:
    if last is None:
        return []
    start_date = (last - timedelta(days=days - 1)).date()
    by_date: dict[str, list[Turn]] = defaultdict(list)
    for turn in turns:
        if turn.timestamp.date() >= start_date:
            by_date[turn.date].append(turn)
    rows: list[dict[str, Any]] = []
    for date in sorted(by_date):
        items = by_date[date]
        by_platform: dict[str, set[str]] = defaultdict(set)
        for turn in items:
            by_platform[turn.platform or "unknown"].add(turn.profile)
        rows.append(
            {
                "date": date,
                "active_profiles": len({turn.profile for turn in items}),
                "turns": len(items),
                "sessions": len({turn.session_id for turn in items}),
                "feishu_profiles": len(by_platform.get("feishu", set())),
                "webui_profiles": len(by_platform.get("webui", set())),
            }
        )
    return rows


_URL_RE = re.compile(r"https?://\S+")
_JWT_RE = re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.I)
_OPEN_ID_RE = re.compile(r"(?<![A-Za-z0-9])ou_[A-Za-z0-9_-]+")
_CHAT_ID_RE = re.compile(r"(?<![A-Za-z0-9])oc_[A-Za-z0-9_-]+")
_MESSAGE_ID_RE = re.compile(r"(?<![A-Za-z0-9])om_[A-Za-z0-9_-]+")
_IMAGE_KEY_RE = re.compile(r"(?<![A-Za-z0-9])img_[A-Za-z0-9_-]+")
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")


def redact_text(text: str) -> str:
    redacted = _URL_RE.sub("<url>", text)
    redacted = _JWT_RE.sub("<jwt>", redacted)
    redacted = _BEARER_RE.sub("<bearer>", redacted)
    redacted = _OPEN_ID_RE.sub("<open_id>", redacted)
    redacted = _CHAT_ID_RE.sub("<chat_id>", redacted)
    redacted = _MESSAGE_ID_RE.sub("<message_id>", redacted)
    redacted = _IMAGE_KEY_RE.sub("<image_key>", redacted)
    return _LONG_TOKEN_RE.sub("<token>", redacted)


def _samples(turns: list[Turn], *, include_profiles: bool, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    result: list[dict[str, Any]] = []
    for turn in turns:
        if not turn.text.strip():
            continue
        if len(result) >= limit:
            break
        item: dict[str, Any] = {
            "timestamp": turn.timestamp.isoformat(),
            "platform": turn.platform,
            "scenario": turn.scenario,
            "text": redact_text(turn.text).replace("\n", " ")[:240],
        }
        if include_profiles:
            item["profile"] = redact_text(turn.profile)
        result.append(item)
    return result


def _insights(turns: list[Turn], scenario_counts: dict[str, Any], failure_counts: dict[str, int]) -> list[str]:
    insights: list[str] = []
    if scenario_counts:
        top_scenario, top_stats = max(scenario_counts.items(), key=lambda item: item[1]["turns"])
        insights.append(f"Top demand area is {top_scenario} ({top_stats['turns']} turns in the selected window).")
    if len(scenario_counts) >= 2:
        second, stats = sorted(scenario_counts.items(), key=lambda item: item[1]["turns"], reverse=True)[1]
        insights.append(f"Second demand area is {second} ({stats['turns']} turns), so analytics should track both workflow depth and breadth.")
    if failure_counts:
        top_failure, count = max(failure_counts.items(), key=lambda item: item[1])
        insights.append(f"Most common explicit failure category is {top_failure} ({count} turns); prioritize this before tuning prompts.")
    image_turns = scenario_counts.get("Image/multimodal generation/analysis", {}).get("turns", 0)
    image_failures = scenario_counts.get("Image/multimodal generation/analysis", {}).get("failures", 0)
    if image_turns and _rate(image_failures, image_turns) >= 40:
        insights.append("Image/multimodal demand is visible but has a high explicit-failure rate; keep tracing file path, upload, and provider boundaries.")
    if any("lark_cli" in turn.tools for turn in turns):
        insights.append("Lark/Feishu tool usage is a core product surface; command-family metrics should remain first-class.")
    return insights


def _build_summary(
    *,
    load: AuditLoadResult,
    audit_path: str,
    routing: dict[str, Any],
    days: int,
    include_profiles: bool,
    include_samples: bool,
    sample_limit: int,
) -> dict[str, Any]:
    profile_kind = routing.get("profile_kind", {})
    turns = [annotate_turn(turn) for turn in build_turns(load.rows)]
    last = load.last_timestamp
    window_days: list[int | None] = sorted({1, 7, 30, max(1, int(days))})
    window_days.append(None)

    summary: dict[str, Any] = {
        "audit": {
            "path": audit_path,
            "rows": len(load.rows),
            "total_lines": load.total_lines,
            "bad_lines": load.bad_lines,
            "first_timestamp": _iso(load.first_timestamp),
            "last_timestamp": _iso(load.last_timestamp),
        },
        "routing": {
            "available": routing.get("available", False),
            "active_by_kind": routing.get("active_by_kind", {}),
        },
        "selected_days": days,
        "methodology": {
            "completion_proxy": COMPLETION_PROXY_NOTE,
            "completion_proxy_blind_spot": COMPLETION_PROXY_BLIND_SPOT,
            "active_user_proxy": ACTIVE_USER_PROXY_NOTE,
        },
        "dau": _dau(turns, last, max(1, int(days))),
        "windows": {},
        "scenarios": {},
        "failure_categories": {},
        "top": {},
    }

    for item in window_days:
        key = _window_key(item)
        items = _window_turns(turns, last, item)
        summary["windows"][key] = _window_metrics(items, profile_kind)
        summary["scenarios"][key] = {"primary": _scenario_metrics(items)}
        summary["failure_categories"][key] = _failure_categories(items)
        summary["top"][key] = _counter_metrics(items, include_profiles=include_profiles)

    selected_key = _window_key(max(1, int(days)))
    selected_turns = _window_turns(turns, last, max(1, int(days)))
    summary["insights"] = _insights(
        selected_turns,
        summary["scenarios"].get(selected_key, {}).get("primary", {}),
        summary["failure_categories"].get(selected_key, {}),
    )
    if include_samples:
        summary["samples"] = _samples(selected_turns, include_profiles=include_profiles, limit=max(0, sample_limit))
    return summary


def build_summary(
    *,
    audit_path: Path = DEFAULT_AUDIT_PATH,
    routing_db: Path | None = DEFAULT_ROUTING_DB,
    days: int = 7,
    include_profiles: bool = False,
    include_samples: bool = False,
    sample_limit: int = 10,
) -> dict[str, Any]:
    load = load_audit_rows(audit_path)
    routing = load_routing_summary(routing_db)
    return _build_summary(
        load=load,
        audit_path=redact_text(str(audit_path)),
        routing=routing,
        days=days,
        include_profiles=include_profiles,
        include_samples=include_samples,
        sample_limit=sample_limit,
    )


def build_summary_from_records(
    conversation_rows: list[dict[str, Any]],
    routing_rows: list[dict[str, Any]] | None = None,
    *,
    days: int = 7,
    include_profiles: bool = False,
    include_samples: bool = False,
    sample_limit: int = 10,
) -> dict[str, Any]:
    return _build_summary(
        load=_audit_load_from_records(conversation_rows),
        audit_path="<in-memory>",
        routing=routing_summary_from_records(routing_rows),
        days=days,
        include_profiles=include_profiles,
        include_samples=include_samples,
        sample_limit=sample_limit,
    )


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_No data._\n"
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(item) for item in row) + " |")
    return "\n".join(lines) + "\n"


def render_markdown(summary: dict[str, Any]) -> str:
    selected_key = f"{summary.get('selected_days', 7)}d"
    selected = summary.get("windows", {}).get(selected_key, {})
    routing = summary.get("routing", {})
    lines = [
        "# Hermes Conversation Analytics",
        "",
        f"- Audit window: `{summary.get('audit', {}).get('first_timestamp')}` to `{summary.get('audit', {}).get('last_timestamp')}`",
        f"- Audit rows: `{summary.get('audit', {}).get('rows', 0)}` (bad lines: `{summary.get('audit', {}).get('bad_lines', 0)}`)",
        f"- Routing active by kind: `{json.dumps(routing.get('active_by_kind', {}), ensure_ascii=False, sort_keys=True)}`",
        f"- Selected window: `{selected_key}`",
        "",
        "## Activity",
        "",
        _table(
            [
                "Window",
                "Turns",
                "Active Profiles",
                "User-like",
                "Group-like",
                "Agent-like",
                "Sessions",
                "Feishu Profiles",
                "WebUI Profiles",
            ],
            [
                [
                    key,
                    metrics.get("turns", 0),
                    metrics.get("active_profiles", 0),
                    metrics.get("active_user_like_profiles", 0),
                    metrics.get("active_group_like_profiles", 0),
                    metrics.get("active_agent_like_profiles", 0),
                    metrics.get("sessions", 0),
                    metrics.get("active_profiles_by_platform", {}).get("feishu", 0),
                    metrics.get("active_profiles_by_platform", {}).get("webui", 0),
                ]
                for key, metrics in (
                    (key, summary.get("windows", {}).get(key, {}))
                    for key in ("1d", "7d", "30d", "all")
                    if key in summary.get("windows", {})
                )
            ],
        ),
        "## DAU Proxy (Active Profiles)",
        "",
        summary.get("methodology", {}).get("active_user_proxy", ACTIVE_USER_PROXY_NOTE),
        "",
        _table(
            ["Date", "Active Profiles", "Turns", "Sessions", "Feishu", "WebUI"],
            [
                [
                    row["date"],
                    row["active_profiles"],
                    row["turns"],
                    row["sessions"],
                    row["feishu_profiles"],
                    row["webui_profiles"],
                ]
                for row in summary.get("dau", [])
            ],
        ),
        "## Proxy Completion",
        "",
        summary.get("methodology", {}).get("completion_proxy", COMPLETION_PROXY_NOTE),
        summary.get("methodology", {}).get("completion_proxy_blind_spot", COMPLETION_PROXY_BLIND_SPOT),
        "",
        _table(
            ["Window", "Completion Proxy Rate", "Final Stop Rate", "Explicit Failure Rate", "Success Signal Rate"],
            [
                [
                    selected_key,
                    f"{selected.get('completion_proxy_rate', 0)}%",
                    f"{selected.get('final_stop_rate', 0)}%",
                    f"{selected.get('explicit_failure_rate', 0)}%",
                    f"{selected.get('success_signal_rate', 0)}%",
                ]
            ],
        ),
        "## Concentration",
        "",
        _table(
            ["Window", "Top 10 Turn Share", "Top 20 Turn Share", "Profiles >=3 Turns", "Profiles >=10 Turns"],
            [
                [
                    selected_key,
                    f"{selected.get('top10_turn_share', 0)}%",
                    f"{selected.get('top20_turn_share', 0)}%",
                    selected.get("profiles_with_3plus_turns", 0),
                    selected.get("profiles_with_10plus_turns", 0),
                ]
            ],
        ),
        "## Scenarios",
        "",
        _table(
            ["Scenario", "Turns", "Completion Proxy Rate", "Final Stop Rate", "Failure Rate"],
            [
                [
                    name,
                    stats["turns"],
                    f"{stats['completion_proxy_rate']}%",
                    f"{stats['final_stop_rate']}%",
                    f"{stats['failure_rate']}%",
                ]
                for name, stats in summary.get("scenarios", {}).get(selected_key, {}).get("primary", {}).items()
            ],
        ),
        "## Failure Categories",
        "",
        _table(
            ["Category", "Turns"],
            [[name, count] for name, count in summary.get("failure_categories", {}).get(selected_key, {}).items()],
        ),
        "## Top Tools",
        "",
        _table(["Tool", "Turns"], summary.get("top", {}).get(selected_key, {}).get("tools", [])[:15]),
        "## Top Skills",
        "",
        _table(["Skill", "Turns"], summary.get("top", {}).get(selected_key, {}).get("skills", [])[:15]),
        "## Top Lark Commands",
        "",
        _table(["Command", "Turns"], summary.get("top", {}).get(selected_key, {}).get("lark_commands", [])[:15]),
    ]
    top_profiles = summary.get("top", {}).get(selected_key, {}).get("top_active_profiles")
    if top_profiles is not None:
        lines.extend(["## Top Profiles", "", _table(["Profile", "Turns"], top_profiles[:20])])
    insights = summary.get("insights") or []
    if insights:
        lines.extend(["## Demand Insights", ""])
        lines.extend(f"- {insight}" for insight in insights)
        lines.append("")
    samples = summary.get("samples") or []
    if samples:
        lines.extend(["## Redacted Samples", ""])
        for sample in samples:
            label = sample.get("profile", sample.get("platform", "sample"))
            lines.append(f"- `{label}` {sample.get('scenario')}: {sample.get('text')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def dumps_json(summary: dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# ─────────────────────────── SkillHub events audit ───────────────────────────
# The writer persists status='installed' for benign no-ops too (mark_installed is
# called for skipped_pending / plugin_no_audience / …), so a status='installed' row
# whose results_json.action is one of these did NOT actually install/uninstall
# anything — count it as skipped, not as processed.
_SKILLHUB_SKIP_ACTIONS = frozenset({
    "skipped_pending", "skipped_inactive", "plugin_deferred",
    "plugin_no_audience", "plugin_noop_already_all", "plugin_no_package",
})


def _skillhub_json_field(blob: Any, key: str) -> Any:
    try:
        return json.loads(blob).get(key) if blob else None
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None


def _skillhub_json_object(blob: Any) -> dict[str, Any]:
    try:
        value = json.loads(blob) if isinstance(blob, str) else blob
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _skillhub_first(*values: Any) -> str:
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text:
            return text
    return ""


def _skillhub_targets(payload: dict[str, Any]) -> list[str]:
    audience = payload.get("audience") if isinstance(payload.get("audience"), dict) else {}
    users = audience.get("users") if isinstance(audience.get("users"), list) else []
    targets: list[str] = []
    for user in users:
        if not isinstance(user, dict):
            continue
        identity = _skillhub_first(
            user.get("profile_id"), user.get("employee_id"), user.get("open_id")
        )
        if identity:
            targets.append(identity)
    return targets or ["audience_all"]


def _skillhub_anonymous_profile(identity: str, hmac_key: bytes | None) -> str | None:
    if identity == "audience_all":
        return identity
    if not hmac_key:
        return None
    digest = hmac.new(hmac_key, identity.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return f"profile_{digest}"


def _skillhub_private_values(payload: dict[str, Any]) -> list[str]:
    audience = payload.get("audience") if isinstance(payload.get("audience"), dict) else {}
    users = audience.get("users") if isinstance(audience.get("users"), list) else []
    return sorted(
        {
            str(user[key])
            for user in users
            if isinstance(user, dict)
            for key in ("profile_id", "employee_id", "open_id", "name", "display_name")
            if user.get(key)
        },
        key=len,
        reverse=True,
    )


def _skillhub_public_message(message: Any, payload: dict[str, Any]) -> str | None:
    if message is None:
        return None
    text = redact_text(str(message))
    for private in _skillhub_private_values(payload):
        text = text.replace(private, "<profile>")
    return text


def _skillhub_terminal_status(status: str) -> str:
    if status == "failed":
        return "failed"
    if status in {"queued", "queued_unknown_type"}:
        return "pending"
    return "completed"


def _skillhub_aggregate(
    rows: list[sqlite3.Row], *, profile_hmac_key: bytes | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in rows:
        payload = _skillhub_json_object(row["raw_payload"])
        result = _skillhub_json_object(row["results_json"])
        skill = payload.get("skill") if isinstance(payload.get("skill"), dict) else {}
        release = payload.get("release") if isinstance(payload.get("release"), dict) else {}
        item = _skillhub_first(row["skill_code"], payload.get("skill_code"), skill.get("skill_code"), "(none)")
        version = _skillhub_first(row["version"], payload.get("version"), release.get("version"), "(none)")
        desired = _skillhub_first(
            payload.get("desired_state"), payload.get("skill_status"), skill.get("status"), "active"
        ).lower()
        event_type = _skillhub_first(row["event_type"], payload.get("event_type"), "unknown")
        trusted_id = _skillhub_first(
            payload.get("fanout_id"),
            payload.get("batch_id"),
            release.get("fanout_id"),
            release.get("batch_id"),
            row["release_id"],
            payload.get("release_id"),
            release.get("release_id"),
        )
        entries.append({
            "event_id": str(row["event_id"]),
            "at": int(row["updated_at"] or row["received_at"] or 0),
            "item": item,
            "version": version,
            "desired_state": desired,
            "event_type": event_type,
            "status": _skillhub_terminal_status(str(row["status"] or "")),
            "targets": _skillhub_targets(payload),
            "trusted_id": trusted_id,
            "batched_events": result.get("batched_events"),
        })

    fanout_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fallback_clusters: dict[tuple[str, str, str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for entry in sorted(entries, key=lambda item: (item["at"], item["event_id"])):
        intent = (entry["event_type"], entry["item"], entry["version"], entry["desired_state"])
        if entry["trusted_id"]:
            key = "trusted:" + "|".join((*intent, entry["trusted_id"]))
        elif isinstance(entry["batched_events"], int) and entry["batched_events"] > 1:
            cluster_key = (*intent, entry["batched_events"])
            clusters = fallback_clusters[cluster_key]
            if not clusters or entry["at"] - clusters[-1][-1]["at"] > 600:
                clusters.append([])
            clusters[-1].append(entry)
            key = f"inferred:{cluster_key!r}:{len(clusters) - 1}"
        else:
            key = f"event:{entry['event_id']}"
        fanout_groups[key].append(entry)

    fanout_counts: Counter[str] = Counter()
    affected_targets = 0
    raw_failures_in_failed_fanouts = 0
    fanout_rows: list[dict[str, Any]] = []
    for fanout_index, (_, grouped) in enumerate(sorted(fanout_groups.items()), start=1):
        final: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for entry in grouped:
            for target in entry["targets"]:
                target_key = (entry["item"], entry["version"], target, entry["desired_state"])
                previous = final.get(target_key)
                if previous is None or (entry["at"], entry["event_id"]) > (
                    previous["at"], previous["event_id"]
                ):
                    final[target_key] = entry
        statuses = {entry["status"] for entry in final.values()}
        status = "failed" if "failed" in statuses else "pending" if "pending" in statuses else "completed"
        fanout_counts[status] += 1
        if status == "failed":
            affected_targets += sum(entry["status"] == "failed" for entry in final.values())
            raw_failures_in_failed_fanouts += sum(entry["status"] == "failed" for entry in grouped)
        fanout_rows.append({
            "fanout_key": f"fanout_{fanout_index:04d}",
            "status": status,
            "affected_targets": sum(entry["status"] == "failed" for entry in final.values()),
        })

    target_final: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for entry in entries:
        for target in entry["targets"]:
            key = (entry["item"], entry["version"], target, entry["desired_state"])
            previous = target_final.get(key)
            if previous is None or (entry["at"], entry["event_id"]) > (
                previous["at"], previous["event_id"]
            ):
                target_final[key] = entry
    target_counts = Counter(entry["status"] for entry in target_final.values())
    target_rows: list[dict[str, Any]] = []
    if profile_hmac_key:
        for key, entry in sorted(target_final.items()):
            anonymous = _skillhub_anonymous_profile(key[2], profile_hmac_key)
            if anonymous is None:
                continue
            target_rows.append({
                "item": key[0],
                "version": key[1],
                "anonymous_profile": anonymous,
                "desired_state": key[3],
                "status": entry["status"],
            })
    failed_fanouts = fanout_counts["failed"]
    return (
        {
            "total": len(fanout_groups),
            "completed": fanout_counts["completed"],
            "failed": failed_fanouts,
            "pending": fanout_counts["pending"],
            "affected_targets": affected_targets,
            "collapsed_raw_failures": max(0, raw_failures_in_failed_fanouts - failed_fanouts),
            "trusted": sum(key.startswith("trusted:") for key in fanout_groups),
            "inferred": sum(key.startswith("inferred:") for key in fanout_groups),
            "single_event": sum(key.startswith("event:") for key in fanout_groups),
            "aggregation_window_seconds": 600,
            "items": sorted(fanout_rows, key=lambda item: item["fanout_key"]),
        },
        {
            "total": len(target_final),
            "completed": target_counts["completed"],
            "failed": target_counts["failed"],
            "pending": target_counts["pending"],
            "items": target_rows,
            "profile_details_available": bool(profile_hmac_key),
            "profile_details_reason": None if profile_hmac_key else "missing_or_invalid_hmac_key",
        },
    )


def _skillhub_item_type(raw_payload: Any) -> str:
    """item_type as the writer's normalize_event resolves it: top-level ``item_type``
    OR nested ``skill.item_type`` (PRD shape), normalized to exactly "plugin" (only when
    the value lower-cases to "plugin") else "skill". Mirrors normalize_event so the audit
    reflects how the event was actually routed (not arbitrary raw casing)."""
    try:
        payload = json.loads(raw_payload) if raw_payload else {}
    except (json.JSONDecodeError, TypeError):
        return "skill"
    if not isinstance(payload, dict):
        return "skill"
    skill_block = payload.get("skill") if isinstance(payload.get("skill"), dict) else {}
    for candidate in (payload.get("item_type"), skill_block.get("item_type")):
        text = str(candidate).strip() if candidate is not None else ""
        if text:
            return "plugin" if text.lower() == "plugin" else "skill"
    return "skill"


def _skillhub_bucket(status: str, action: str | None) -> str:
    if status == "failed":
        return "failed"
    if status == "queued":
        return "queued"
    if status == "queued_unknown_type":
        return "queued_unknown"
    if status == "installed":
        return "skipped" if (action or "") in _SKILLHUB_SKIP_ACTIONS else "processed"
    return "skipped"


def _md_cell(value: Any) -> str:
    """Sanitize a value for a markdown table cell (no pipes / newlines break the table)."""
    text = "" if value is None else str(value)
    return text.replace("|", "/").replace("\n", " ").replace("\r", " ").strip()


def build_skillhub_audit(
    db_path: Path,
    *,
    days: int = 7,
    all_time_only: bool = False,
    sample_limit: int = 5,
    top_n: int = 10,
    profile_hmac_key: str | bytes | None = None,
) -> dict[str, Any]:
    """Aggregate the skillhub_events ledger: received / processed / failed / queued
    counts (all-time + last N days), failures grouped by error_code with newest-K
    samples, and distribution by item_type and skill_code. Read-only.

    '成功'(processed) = a status='installed' row that actually did work (install /
    uninstall / shrink); benign no-ops (pending / no-audience …) are counted as
    'skipped' even though the DB status is 'installed'."""
    db_path = Path(db_path).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(f"multitenancy.db not found at {db_path}")
    # as_uri() percent-encodes the path (a literal '?' in it would otherwise be parsed
    # as the URI query, yielding a false missing-table error).
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='skillhub_events'"
        ).fetchone()
        if not exists:
            raise ValueError("table skillhub_events does not exist in this DB")
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(skillhub_events)")
        }
        optional = {
            "event_type": "'unknown'",
            "release_id": "NULL",
            "version": "NULL",
            "updated_at": "received_at",
        }
        optional_select = ", ".join(
            name if name in columns else f"{fallback} AS {name}"
            for name, fallback in optional.items()
        )
        cutoff = int(datetime.now().timestamp()) - max(1, days) * 86400
        # newest first so failure samples are the most recent K
        rows = conn.execute(
            "SELECT event_id, skill_code, status, received_at, raw_payload, results_json, "
            + optional_select
            + " FROM skillhub_events ORDER BY received_at DESC, event_id DESC"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # bad/incompatible skillhub_events schema (missing columns etc.) → same friendly
        # nonzero failure path as a missing table, not an uncaught crash.
        raise ValueError(f"cannot read skillhub_events (bad schema?): {exc}") from exc
    finally:
        conn.close()

    def _tally(subset: list[sqlite3.Row]) -> dict[str, int]:
        c: Counter[str] = Counter()
        for r in subset:
            action = _skillhub_json_field(r["results_json"], "action")
            c[_skillhub_bucket(r["status"] or "", action)] += 1
        return {
            "received": len(subset),
            "processed": c.get("processed", 0),       # 成功：真装/卸/收缩
            "failed": c.get("failed", 0),
            "queued": c.get("queued", 0),
            "queued_unknown": c.get("queued_unknown", 0),  # 未识别类型（另列）
            "skipped": c.get("skipped", 0),           # 无操作：pending / no-audience 等
        }

    windowed = [r for r in rows if (r["received_at"] or 0) >= cutoff]

    # failures grouped by error_code (all-time; rows already sorted newest-first)
    err_counts: Counter[str] = Counter()
    err_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if (r["status"] or "") != "failed":
            continue
        code = _skillhub_json_field(r["results_json"], "error_code") or "UNKNOWN"
        err_counts[code] += 1
        if len(err_samples[code]) < sample_limit:
            payload = _skillhub_json_object(r["raw_payload"])
            err_samples[code].append({
                "skill_code": r["skill_code"],
                "message": _skillhub_public_message(
                    _skillhub_json_field(r["results_json"], "message"), payload
                ),
                "time": datetime.fromtimestamp(r["received_at"]).strftime("%Y-%m-%d %H:%M")
                if r["received_at"] else None,
            })

    by_item_type: Counter[str] = Counter()
    by_skill: Counter[str] = Counter()
    for r in rows:
        by_item_type[_skillhub_item_type(r["raw_payload"])] += 1
        by_skill[r["skill_code"] or "(none)"] += 1

    configured_hmac_key = (
        profile_hmac_key
        if profile_hmac_key is not None
        else os.environ.get("HERMES_ANALYTICS_PROFILE_HMAC_KEY")
    )
    candidate_hmac_key = (
        configured_hmac_key.encode("utf-8")
        if isinstance(configured_hmac_key, str) and configured_hmac_key
        else configured_hmac_key if isinstance(configured_hmac_key, bytes) and configured_hmac_key else None
    )
    hmac_key_bytes = (
        candidate_hmac_key
        if candidate_hmac_key is not None and len(candidate_hmac_key) >= 32
        else None
    )
    fanouts, target_final_states = _skillhub_aggregate(
        rows, profile_hmac_key=hmac_key_bytes
    )
    raw_events = _tally(rows)
    audit: dict[str, Any] = {
        "db_path": str(db_path),
        "generated_days_window": days,
        "all_time": raw_events,
        "all_time_semantics": "raw_events",
        "raw_events": raw_events,
        "fanouts": fanouts,
        "target_final_states": target_final_states,
        "failures": {
            "by_error_code": dict(err_counts.most_common()),
            "samples": {k: err_samples[k] for k in err_counts},
        },
        "by_item_type": dict(by_item_type.most_common()),
        "by_skill_top": dict(by_skill.most_common(top_n)),
    }
    if not all_time_only:
        audit["last_n_days"] = _tally(windowed)
    return audit


def _skillhub_line(label: str, t: dict[str, int]) -> str:
    return (f"**{label}**：收到 {t['received']} · 成功 {t['processed']} · 失败 {t['failed']} · "
            f"排队 {t['queued']} · 未识别 {t['queued_unknown']} · 无操作 {t['skipped']}")


def render_skillhub_markdown(audit: dict[str, Any]) -> str:
    out: list[str] = ["# SkillHub 事件审计", ""]
    out.append("## 原始事件")
    out.append(_skillhub_line("全量（兼容字段 all_time）", audit["all_time"]))
    if "last_n_days" in audit:
        out.append(_skillhub_line(f"近 {audit['generated_days_window']} 天", audit["last_n_days"]))
    out.append("")
    fanouts = audit["fanouts"]
    out.append("## 批量结果")
    out.append(
        f"批次 {fanouts['total']} · 完成 {fanouts['completed']} · 失败 {fanouts['failed']} · "
        f"待处理 {fanouts['pending']} · 受影响目标 {fanouts['affected_targets']}"
    )
    out.append(
        f"聚合依据：可信标识 {fanouts['trusted']} · 10 分钟保守推导 {fanouts['inferred']} · "
        f"单事件回退 {fanouts['single_event']}"
    )
    final = audit["target_final_states"]
    out.append(
        f"目标最终状态：共 {final['total']} · 完成 {final['completed']} · "
        f"失败 {final['failed']} · 待处理 {final['pending']}"
    )
    if not final["profile_details_available"]:
        out.append("Profile 明细：未输出（缺少或无效的 HMAC 匿名化密钥）")
    out.append("")
    errs = audit["failures"]["by_error_code"]
    if errs:
        out.append("## 失败原因（按 error_code）")
        out.append("| error_code | 次数 | 近样本 (skill · message · 时间) |")
        out.append("|---|---|---|")
        for code, cnt in errs.items():
            samples = audit["failures"]["samples"].get(code, [])
            sample_txt = "; ".join(
                f"{_md_cell(s['skill_code'])} · {_md_cell(s['message'])} · {_md_cell(s['time'])}"
                for s in samples
            ) or "-"
            out.append(f"| {_md_cell(code)} | {cnt} | {sample_txt} |")
    else:
        out.append("## 失败原因\n无失败 ✓")
    out.append("")
    out.append("## 分布")
    out.append("- 按类型: " + ", ".join(f"{k}={v}" for k, v in audit["by_item_type"].items()))
    out.append("- Top skill/plugin: " + ", ".join(f"{k}={v}" for k, v in audit["by_skill_top"].items()))
    return "\n".join(out) + "\n"
