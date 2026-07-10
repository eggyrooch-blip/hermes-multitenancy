"""Read-only self-diagnostic reports for the multitenancy plugin.

Powers the ``/doctor`` and ``/diagnose`` slash commands. Behaviour is ported
(not transliterated) from the openclaw-lark TS reference
``src/commands/{doctor,diagnose}.ts``: a locale-aware Markdown health report
and a structured health verdict.

Design split, so the report logic stays unit-testable without a live Hermes
runtime:

* **Builders** (``build_doctor_markdown`` / ``build_diagnose_report`` /
  ``render_diagnose_markdown``) are pure functions over already-collected,
  redacted inputs.
* **Collectors** (``collect_runtime_inputs``) gather those inputs from the live
  environment (``HERMES_HOME``, the credential vault, upstream capability
  health) and degrade gracefully when the environment is absent.

Everything here is read-only. No writes, no decrypted secrets. The credential
status reused from :mod:`credential_tool` is already redacted; this module only
ever masks values further, never widens them.
"""
from __future__ import annotations

import json
import os
import platform as _platform
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from .feishu_message_trace import trace_prefix


FeishuLocale = str  # "zh_cn" | "en_us"


# ---------------------------------------------------------------------------
# i18n text map
# ---------------------------------------------------------------------------

_TEXT: dict[str, dict[str, str]] = {
    "zh_cn": {
        "doctor_title": "飞书机器人体检报告",
        "plugin_version": "插件版本",
        "profile": "当前 Profile",
        "profile_unrouted": "(未路由)",
        "credential_status": "凭证状态",
        "cred_state": "状态",
        "cred_provider": "提供方",
        "cred_kind": "类型",
        "cred_expires": "访问令牌到期",
        "cred_refresh": "刷新令牌到期",
        "cred_scopes": "授权范围",
        "cred_has": "已存储凭证",
        "cred_no_subject": "未指定用户，已跳过个人凭证检查（私聊机器人时会针对你的身份检查）",
        "permission_summary": "权限/范围概览",
        "missing_scopes": "缺失范围",
        "no_missing_scopes": "无缺失范围",
        "capability_summary": "能力/边界概览",
        "capability_ready": "整体就绪",
        "capability_attention": "需关注",
        "capability_unavailable": "能力检查不可用",
        "none": "无",
        "yes": "是",
        "no": "否",
        "not_set": "(未设置)",
        "unknown": "未知",
        "environment": "运行环境",
        "python_version": "Python 版本",
        "platform": "平台",
        # diagnose
        "diagnose_title": "飞书机器人诊断",
        "overall": "总体结论",
        "overall_healthy": "健康",
        "overall_degraded": "降级",
        "overall_unhealthy": "异常",
        "current_user": "当前用户",
        "user_unresolved": "未识别用户",
        "cred_valid": "有效",
        "cred_expired": "已过期",
        "cred_scope_missing": "缺少权限",
        "cred_unauth": "未授权（请私聊发 /auth 授权）",
        "multitenancy": "多租户状态",
        "mt_kind": "路由类型",
        "mt_owner": "群主(邀请人)",
        "mt_agent": "Agent ID",
        "mt_unrouted": "尚无路由记录",
        "mt_agents_count": "我的 Agent 数量",
        "agent_kind_group": "群聊 Agent",
        "agent_kind_agent": "智能体",
        "agent_kind_user": "用户",
        "agents_none": "暂无",
    },
    "en_us": {
        "doctor_title": "Feishu Bot Doctor Report",
        "plugin_version": "Plugin Version",
        "profile": "Current Profile",
        "profile_unrouted": "(unrouted)",
        "credential_status": "Credential Status",
        "cred_state": "State",
        "cred_provider": "Provider",
        "cred_kind": "Kind",
        "cred_expires": "Access token expires",
        "cred_refresh": "Refresh token expires",
        "cred_scopes": "Scopes",
        "cred_has": "Credential stored",
        "cred_no_subject": "No user context — personal credential check skipped (it runs when you DM the bot)",
        "permission_summary": "Permission / Scope Summary",
        "missing_scopes": "Missing scopes",
        "no_missing_scopes": "No missing scopes",
        "capability_summary": "Capability / Boundary Summary",
        "capability_ready": "Overall ready",
        "capability_attention": "Needs attention",
        "capability_unavailable": "Capability check unavailable",
        "none": "none",
        "yes": "yes",
        "no": "no",
        "not_set": "(not set)",
        "unknown": "unknown",
        "environment": "Environment",
        "python_version": "Python version",
        "platform": "Platform",
        # diagnose
        "diagnose_title": "Feishu Bot Diagnose",
        "overall": "Overall Verdict",
        "overall_healthy": "healthy",
        "overall_degraded": "degraded",
        "overall_unhealthy": "unhealthy",
        "current_user": "Current user",
        "user_unresolved": "unresolved user",
        "cred_valid": "valid",
        "cred_expired": "expired",
        "cred_scope_missing": "missing scopes",
        "cred_unauth": "not authorized (DM /auth to authorize)",
        "multitenancy": "Multitenancy",
        "mt_kind": "Route kind",
        "mt_owner": "Owner (inviter)",
        "mt_agent": "Agent ID",
        "mt_unrouted": "no routing record yet",
        "mt_agents_count": "My agents",
        "agent_kind_group": "Group agent",
        "agent_kind_agent": "Assistant",
        "agent_kind_user": "User",
        "agents_none": "none",
    },
}


def _normalize_locale(value: Optional[str]) -> str:
    """Map any incoming locale-ish string to a supported locale (default zh_cn)."""
    text = str(value or "").strip().lower().replace("-", "_")
    if text.startswith("en"):
        return "en_us"
    return "zh_cn"


def _t(locale: str, key: str) -> str:
    table = _TEXT.get(locale) or _TEXT["zh_cn"]
    return table.get(key, _TEXT["zh_cn"].get(key, key))


def _is_no_subject_error(credential_status: dict[str, Any]) -> bool:
    """Return whether the credential error only indicates missing user context."""
    error = credential_status.get("error")
    if not error:
        return False
    text = str(error).strip().lower()
    return any(
        needle in text
        for needle in ("subject_id is required", "user_open_id", "no user subject")
    )


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def _mask_secret(value: Optional[str], *, locale: str = "zh_cn") -> str:
    """Mask a secret for display. Never returns more than the first 4 chars."""
    if not value:
        return _t(_normalize_locale(locale), "not_set")
    text = str(value)
    if len(text) <= 4:
        return "****"
    return text[:4] + "****"


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

def plugin_version() -> str:
    """Return the plugin version from the bundled manifest, or "unknown"."""
    manifest = Path(__file__).parent / "plugin.yaml"
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except Exception:
        return "unknown"
    version = data.get("version") if isinstance(data, dict) else None
    return str(version) if version else "unknown"


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def _environment() -> dict[str, str]:
    return {
        "python_version": _platform.python_version() or ".".join(map(str, sys.version_info[:3])),
        "platform": f"{_platform.system().lower() or 'unknown'}-{_platform.machine() or 'unknown'}",
    }


# ---------------------------------------------------------------------------
# /doctor — Markdown builder
# ---------------------------------------------------------------------------

def _format_expiry(value: Any) -> Optional[str]:
    """Render an epoch-ms expiry as an ISO-ish string, else None."""
    if value in (None, "", 0):
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return str(value)
    if ms <= 0:
        return None
    import datetime as _dt

    try:
        return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (OverflowError, OSError, ValueError):
        return str(value)


def _identity_label(identity: Optional[dict[str, Any]], loc: dict[str, str]) -> Optional[str]:
    """One-line 'name · profile' for the invoking user (Feishu name, never raw
    open_id), or None."""
    if not isinstance(identity, dict):
        return None
    open_id = str(identity.get("open_id") or "").strip()
    name = str(identity.get("name") or "").strip()
    profile = str(identity.get("profile") or "").strip()
    hide = bool(identity.get("hide_open_id"))
    if not open_id and not name:
        return None
    who = name or _t(loc, "user_unresolved")
    parts = [who]
    if open_id and not (hide or name):
        # Only fall back to open_id when we have no name AND aren't told to hide.
        parts.append(f"`{open_id}`")
    label = " ".join(parts)
    if profile:
        label += f" · {profile}"
    return label


def _cred_validity(cred: dict[str, Any], loc: dict[str, str]) -> str:
    """Map a credential-status dict to a friendly validity word."""
    state = str(cred.get("status") or "").lower()
    if state in {"missing", "unauthorized", "none"} or not cred.get("has_credential"):
        return _t(loc, "cred_unauth")
    if state == "expired":
        return _t(loc, "cred_expired")
    if state == "scope_missing" or (cred.get("missing_scopes") or []):
        return _t(loc, "cred_scope_missing")
    return _t(loc, "cred_valid")


def build_doctor_markdown(
    *,
    version: str,
    profile_name: Optional[str],
    credential_status: dict[str, Any],
    health: dict[str, Any],
    env: dict[str, str],
    identity: Optional[dict[str, Any]] = None,
    locale: str = "zh_cn",
) -> str:
    """Build a locale-aware, non-empty Markdown doctor report from plain inputs."""
    loc = _normalize_locale(locale)
    cred = credential_status if isinstance(credential_status, dict) else {}
    hc = health if isinstance(health, dict) else {}
    environment = env if isinstance(env, dict) else {}

    lines: list[str] = [f"## {_t(loc, 'doctor_title')}", ""]

    who = _identity_label(identity, loc)
    if who:
        lines.append(f"- **{_t(loc, 'current_user')}**: {who}")
    # Version + profile + environment
    lines.append(f"- **{_t(loc, 'plugin_version')}**: {version or _t(loc, 'unknown')}")
    lines.append(f"- **{_t(loc, 'profile')}**: {profile_name or _t(loc, 'profile_unrouted')}")
    lines.append(
        f"- **{_t(loc, 'python_version')}**: "
        f"{environment.get('python_version', _t(loc, 'unknown'))}"
    )
    lines.append(
        f"- **{_t(loc, 'platform')}**: {environment.get('platform', _t(loc, 'unknown'))}"
    )
    lines.append("")

    # Credential status
    lines.append(f"### {_t(loc, 'credential_status')}")
    if _is_no_subject_error(cred):
        lines.append(f"- {_t(loc, 'cred_no_subject')}")
    elif cred.get("error"):
        lines.append(f"- {_t(loc, 'capability_unavailable')}: {cred.get('error')}")
    else:
        lines.append(f"- **{_t(loc, 'cred_state')}**: {_cred_validity(cred, loc)}")
        lines.append(f"- **{_t(loc, 'cred_provider')}**: {cred.get('provider', _t(loc, 'unknown'))}")
        lines.append(f"- **{_t(loc, 'cred_kind')}**: {cred.get('credential_kind', _t(loc, 'unknown'))}")
        expires = _format_expiry(cred.get("expires_at"))
        if expires:
            lines.append(f"- **{_t(loc, 'cred_expires')}**: {expires}")
        refresh = _format_expiry(cred.get("refresh_expires_at"))
        if refresh:
            lines.append(f"- **{_t(loc, 'cred_refresh')}**: {refresh}")
        has_cred = _t(loc, "yes") if cred.get("has_credential") else _t(loc, "no")
        lines.append(f"- **{_t(loc, 'cred_has')}**: {has_cred}")
    lines.append("")

    # Permission / scope summary (reuse scopes already present in cred status)
    lines.append(f"### {_t(loc, 'permission_summary')}")
    scopes = cred.get("scopes") or []
    if isinstance(scopes, list) and scopes:
        for scope in scopes:
            lines.append(f"- `{scope}`")
    else:
        lines.append(f"- {_t(loc, 'none')}")
    missing = cred.get("missing_scopes") or []
    if isinstance(missing, list) and missing:
        lines.append(f"- **{_t(loc, 'missing_scopes')}**: " + ", ".join(f"`{s}`" for s in missing))
    else:
        lines.append(f"- {_t(loc, 'no_missing_scopes')}")
    lines.append("")

    # Capability / boundary summary (reuse upstream_health)
    lines.append(f"### {_t(loc, 'capability_summary')}")
    if hc.get("status") == "unavailable" or hc.get("error"):
        reason = hc.get("error") or hc.get("status")
        lines.append(f"- {_t(loc, 'capability_unavailable')}: {reason}")
    else:
        ready = _t(loc, "yes") if hc.get("ready") else _t(loc, "no")
        lines.append(f"- **{_t(loc, 'capability_ready')}**: {ready}")
        attention = hc.get("attention") or []
        if isinstance(attention, list) and attention:
            lines.append(
                f"- **{_t(loc, 'capability_attention')}**: "
                + ", ".join(f"`{name}`" for name in attention)
            )
        else:
            lines.append(f"- **{_t(loc, 'capability_attention')}**: {_t(loc, 'none')}")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# /diagnose — structured report + Markdown renderer
# ---------------------------------------------------------------------------

def _capability_errored(health: dict[str, Any]) -> bool:
    return bool(health.get("error")) or health.get("status") == "unavailable"


def _capability_degraded(health: dict[str, Any]) -> bool:
    if _capability_errored(health):
        return False
    if health.get("ready") is False:
        return True
    attention = health.get("attention") or []
    return bool(isinstance(attention, list) and attention)


def _overall_verdict(credential_status: dict[str, Any], health: dict[str, Any]) -> str:
    """Deterministic health verdict from credential + capability inputs."""
    cred_state = str(credential_status.get("status") or "").lower()
    if cred_state in {"missing", "expired"}:
        return "unhealthy"
    if credential_status.get("error") and not _is_no_subject_error(credential_status):
        return "unhealthy"
    if _capability_errored(health):
        return "unhealthy"
    if cred_state in {"scope_missing"} or (credential_status.get("missing_scopes") or []):
        return "degraded"
    if _capability_degraded(health):
        return "degraded"
    return "healthy"


def build_diagnose_report(
    *,
    version: str,
    profile_name: Optional[str],
    credential_status: dict[str, Any],
    health: dict[str, Any],
    env: dict[str, str],
    identity: Optional[dict[str, Any]] = None,
    multitenancy: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build a structured, secret-free health report with an overall verdict."""
    cred = credential_status if isinstance(credential_status, dict) else {}
    hc = health if isinstance(health, dict) else {}
    environment = env if isinstance(env, dict) else {}
    no_subject = _is_no_subject_error(cred)

    return {
        "version": version or "unknown",
        "profile": profile_name,
        "identity": identity if isinstance(identity, dict) else None,
        "multitenancy": multitenancy if isinstance(multitenancy, dict) else None,
        "overall": _overall_verdict(cred, hc),
        "environment": {
            "python_version": environment.get("python_version", "unknown"),
            "platform": environment.get("platform", "unknown"),
        },
        "credential": {
            "status": cred.get("status", "unknown"),
            "provider": cred.get("provider"),
            "credential_kind": cred.get("credential_kind"),
            "has_credential": bool(cred.get("has_credential")),
            "expires_at": cred.get("expires_at"),
            "missing_scopes": cred.get("missing_scopes") or [],
            "error": None if no_subject else cred.get("error"),
            "no_subject": no_subject,
        },
        "capability": {
            "ready": hc.get("ready"),
            "attention": hc.get("attention") or [],
            "status": hc.get("status"),
            "error": hc.get("error"),
        },
    }


def render_diagnose_markdown(report: dict[str, Any], locale: str = "zh_cn") -> str:
    """Render a structured diagnose report into non-empty locale-aware Markdown."""
    loc = _normalize_locale(locale)
    rpt = report if isinstance(report, dict) else {}
    environment = rpt.get("environment") or {}
    cred = rpt.get("credential") or {}
    cap = rpt.get("capability") or {}

    overall = str(rpt.get("overall") or "unhealthy")
    overall_label = _t(loc, f"overall_{overall}") if overall in {
        "healthy",
        "degraded",
        "unhealthy",
    } else overall

    lines: list[str] = [f"## {_t(loc, 'diagnose_title')}", ""]
    lines.append(f"- **{_t(loc, 'overall')}**: {overall_label}")
    who = _identity_label(rpt.get("identity"), loc)
    if who:
        lines.append(f"- **{_t(loc, 'current_user')}**: {who}")
    lines.append(f"- **{_t(loc, 'plugin_version')}**: {rpt.get('version', _t(loc, 'unknown'))}")
    lines.append(f"- **{_t(loc, 'profile')}**: {rpt.get('profile') or _t(loc, 'profile_unrouted')}")
    lines.append("")

    lines.append(f"### {_t(loc, 'environment')}")
    lines.append(f"- **{_t(loc, 'python_version')}**: {environment.get('python_version', _t(loc, 'unknown'))}")
    lines.append(f"- **{_t(loc, 'platform')}**: {environment.get('platform', _t(loc, 'unknown'))}")
    lines.append("")

    lines.append(f"### {_t(loc, 'credential_status')}")
    if cred.get("no_subject"):
        lines.append(f"- {_t(loc, 'cred_no_subject')}")
    elif cred.get("error"):
        lines.append(f"- {_t(loc, 'capability_unavailable')}: {cred.get('error')}")
    else:
        lines.append(f"- **{_t(loc, 'cred_state')}**: {_cred_validity(cred, loc)}")
        has_cred = _t(loc, "yes") if cred.get("has_credential") else _t(loc, "no")
        lines.append(f"- **{_t(loc, 'cred_has')}**: {has_cred}")
        expires = _format_expiry(cred.get("expires_at"))
        if expires:
            lines.append(f"- **{_t(loc, 'cred_expires')}**: {expires}")
        missing = cred.get("missing_scopes") or []
        if isinstance(missing, list) and missing:
            lines.append(
                f"- **{_t(loc, 'missing_scopes')}**: " + ", ".join(f"`{s}`" for s in missing)
            )
    lines.append("")

    lines.append(f"### {_t(loc, 'capability_summary')}")
    if cap.get("error") or cap.get("status") == "unavailable":
        lines.append(f"- {_t(loc, 'capability_unavailable')}: {cap.get('error') or cap.get('status')}")
    else:
        ready = _t(loc, "yes") if cap.get("ready") else _t(loc, "no")
        lines.append(f"- **{_t(loc, 'capability_ready')}**: {ready}")
        attention = cap.get("attention") or []
        if isinstance(attention, list) and attention:
            lines.append(
                f"- **{_t(loc, 'capability_attention')}**: "
                + ", ".join(f"`{n}`" for n in attention)
            )
        else:
            lines.append(f"- **{_t(loc, 'capability_attention')}**: {_t(loc, 'none')}")
    lines.append("")

    mt = rpt.get("multitenancy")
    lines.append("")
    lines.append(f"### {_t(loc, 'multitenancy')}")
    if isinstance(mt, dict) and (mt.get("kind") or mt.get("profile")):
        lines.append(f"- **{_t(loc, 'mt_kind')}**: {mt.get('kind') or _t(loc, 'unknown')}")
        lines.append(f"- **{_t(loc, 'profile')}**: {mt.get('profile') or _t(loc, 'profile_unrouted')}")
        owner_name = str(mt.get("owner_name") or "").strip()
        if owner_name:  # show the Feishu name, never the raw open_id
            lines.append(f"- **{_t(loc, 'mt_owner')}**: {owner_name}")
        agents = mt.get("agents") or []
        if isinstance(agents, list):
            lines.append(f"- **{_t(loc, 'mt_agents_count')}**: {len(agents)}")
            for a in agents[:20]:
                kind = a.get("kind") or "agent"
                kind_label = _t(loc, f"agent_kind_{kind}")
                if kind_label == f"agent_kind_{kind}":  # unknown kind → raw
                    kind_label = kind
                label = a.get("name") or a.get("profile") or a.get("label") or "?"
                lines.append(f"  - {kind_label}: {label}")
            if not agents:
                lines.append(f"  - {_t(loc, 'agents_none')}")
    else:
        lines.append(f"- {_t(loc, 'mt_unrouted')}")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Collectors — live environment, all guarded
# ---------------------------------------------------------------------------

def _collect_credential_status(subject_open_id: Optional[str] = None) -> dict[str, Any]:
    """Pull redacted credential status for a subject (the invoking user when
    known, else the current profile). Never raises."""
    try:
        from .credential_tool import credential_status

        args = {"subject_id": subject_open_id} if subject_open_id else {}
        raw = credential_status(args)
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"error": "unexpected credential status shape"}
    except Exception as exc:
        return {"error": exc.__class__.__name__}


def _shared_home() -> Optional[Path]:
    explicit = os.getenv("HERMES_SHARED_HOME")
    if explicit:
        return Path(explicit).expanduser()
    home = os.getenv("HERMES_HOME")
    if not home:
        return None
    path = Path(home).expanduser()
    if path.parent.name == "profiles":
        return path.parent.parent
    return path


def _collect_health() -> dict[str, Any]:
    """Pull the upstream capability/boundary health report. Never raises."""
    shared = _shared_home()
    if shared is None:
        return {"status": "unavailable", "error": "HERMES_HOME unset"}
    profile_home_env = os.getenv("HERMES_HOME")
    profile_home = Path(profile_home_env).expanduser() if profile_home_env else None
    try:
        from .upstream_health import upstream_capability_health

        return upstream_capability_health(shared_home=shared, profile_home=profile_home)
    except Exception as exc:
        return {"status": "unavailable", "error": exc.__class__.__name__}


def collect_runtime_inputs(subject_open_id: Optional[str] = None) -> dict[str, Any]:
    """Gather all builder inputs from the live environment, degrading gracefully.

    ``subject_open_id`` — the invoking user's Feishu open_id (from the message
    sender). When present we query that user's real credential validity instead
    of failing with "no subject".
    """
    profile_name: Optional[str] = None
    home = os.getenv("HERMES_HOME")
    if home:
        try:
            name = Path(home).expanduser().name
            profile_name = name or None
        except Exception:
            profile_name = None

    return {
        "version": plugin_version(),
        "profile_name": profile_name,
        "credential_status": _collect_credential_status(subject_open_id),
        "health": _collect_health(),
        "env": _environment(),
    }


# ---------------------------------------------------------------------------
# Top-level entry points used by the router
# ---------------------------------------------------------------------------

def render_doctor(
    *,
    locale: str = "zh_cn",
    subject_open_id: Optional[str] = None,
    identity: Optional[dict[str, Any]] = None,
) -> str:
    """Collect live inputs and render the /doctor Markdown report."""
    inputs = collect_runtime_inputs(subject_open_id)
    return build_doctor_markdown(
        version=inputs["version"],
        profile_name=inputs["profile_name"],
        credential_status=inputs["credential_status"],
        health=inputs["health"],
        env=inputs["env"],
        identity=identity,
        locale=locale,
    )


def render_diagnose(
    *,
    locale: str = "zh_cn",
    subject_open_id: Optional[str] = None,
    identity: Optional[dict[str, Any]] = None,
    multitenancy: Optional[dict[str, Any]] = None,
) -> str:
    """Collect live inputs, build the structured report, render its Markdown."""
    inputs = collect_runtime_inputs(subject_open_id)
    report = build_diagnose_report(
        version=inputs["version"],
        profile_name=inputs["profile_name"],
        credential_status=inputs["credential_status"],
        health=inputs["health"],
        env=inputs["env"],
        identity=identity,
        multitenancy=multitenancy,
    )
    return render_diagnose_markdown(report, locale)


# ---------------------------------------------------------------------------
# Per-message log trace (see feishu_message_trace for the writer side)
# ---------------------------------------------------------------------------

# EXTRACTION ONLY — stage classification (replied/duplicate/error keyword
# analysis) was cut by decision (sunke, 2026-07-10) after five review rounds
# each constructed a new in-band forgery against whichever heuristic the
# classifier used; the durable value is "one command greps a message's full
# trail out of agent.log", and that is all this does. The operator reads the
# extracted lines; the machine does not opine on them.
#
# Ownership rules (each survived an adversarial review round):
# * A record is attributed ONLY when its VALIDATED real prefix token sits
#   immediately after the timestamped log header — the factory prepends the
#   token to ``record.msg``, so nothing can appear between header and token.
#   Tokens anywhere later in a line are content (echoed reply text, hostile
#   exception strings) and never attribute.
# * There is NO bare/line-start token format: a multiline hostile payload can
#   fabricate a line-start ``[msg:...]`` via an embedded newline
#   (review-reproduced), so only the timestamped header frame is trusted.
#   The DEPLOYED formatter is ``%(name)s: %(message)s`` (verified against live
#   ~/.hermes/profiles/multitenancy_router/logs/agent.log:
#   ``2026-07-08 14:22:01,415 INFO hermes_multitenancy.credential_renewal_worker: [credential_renewal] tick ...``);
#   colon-less logger names stay accepted.
# * Continuation lines (traceback frames — no timestamped header) belong to the
#   current owner and cannot re-attribute regardless of embedded tokens.
# Residual (documented, accepted): hostile content that byte-mimics a FULL
# timestamped header + token on its own line AND carries a real calendar
# datetime still forges — this is the theoretical limit of in-band text
# markers; structured logging is the real fix and deliberately out of scope
# (diagnostic aid, not a security boundary). Impossible timestamps
# (2026-99-99) no longer frame records (round-8 fix, _real_header_ts).

# Matches the ``[msg:<id>]`` token the factory injects. ``[^\]]*`` (no regex
# ``\b``, so CJK-safe) captures the id.
_MSG_TOKEN_RE = re.compile(r"\[msg:([^\]]*)\]")
_LOG_LEVELS = r"(?:DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL|TRACE|EXCEPTION)"
_TIMESTAMPED_TRACED_RE = re.compile(
    r"^\s*(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})\S*\s+" + _LOG_LEVELS + r"\s+[\w.\-]+:?\s+\[msg:(?P<mid>[^\]]*)\]\s?.*$"
)
# An UNTRACED record boundary must match the COMPLETE header grammar
# (timestamp + level word + dotted logger name) — a bare timestamp-looking
# string inside a traceback/hostile payload must not terminate continuation
# capture and truncate the owner's trail (review round-6, reproduced).
_RECORD_START_RE = re.compile(
    r"^\s*(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})\S*\s+" + _LOG_LEVELS + r"\s+[\w.\-]+:?\s"
)


def _real_header_ts(match: "re.Match[str]") -> bool:
    """A header only frames a record if its timestamp is a real calendar
    datetime (review round-8, codex-reproduced: ``2026-99-99 99:99`` byte-mimics
    the grammar and forged attribution). Forged headers become plain content."""
    try:
        datetime.strptime(match.group("ts").replace("T", " "), "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    return True


@dataclass
class MessageTrace:
    """Structured result of :func:`trace_by_message_id`. All fields default, so
    the empty-token / read-error early returns stay KeyError-free."""

    message_id: str
    matched_lines: list[str] = field(default_factory=list)
    received: bool = False
    read_error: Optional[str] = None


def _owned_lines(lines, target_id: str):
    """Yield the log lines belonging to ``target_id`` (see ownership rules in
    the section comment above)."""
    attributing = False
    for line in lines:
        match = _TIMESTAMPED_TRACED_RE.match(line)
        if match is not None and _real_header_ts(match):
            attributing = match.group("mid") == target_id
            if attributing:
                yield line
            continue
        start = _RECORD_START_RE.match(line)
        if start is not None and _real_header_ts(start):
            attributing = False  # untraced record boundary — stop capture
        elif attributing:
            yield line  # traceback / continuation of the owned record


def trace_by_message_id(msg_id: object, log_path: object) -> MessageTrace:
    """Extract one message's log lines (record + traceback continuations) from
    ``log_path``. ``received`` is True iff any line was found — meaning only
    "the router process holds traces of this id" (a healthy message may log
    little; absence proves nothing about upstream delivery)."""
    try:
        token = trace_prefix(msg_id)
    except Exception:
        token = ""  # a raising msg_id.__str__ must not crash the diagnostic
    target_id = _sanitized_from_prefix(token)
    result = MessageTrace(message_id=target_id)
    if not token:
        return result

    try:
        # Stream — agent.log is unbounded (rotation aside); slurping the whole
        # file into memory on the DIAGNOSTIC path would be its own incident.
        with open(str(log_path), "r", encoding="utf-8", errors="replace") as handle:  # str() inside try: raising __str__ lands in read_error
            result.matched_lines = list(
                _owned_lines((line.rstrip("\n") for line in handle), target_id)
            )
    except Exception as exc:  # OSError + pathological __str__/__fspath__ inputs
        result.read_error = f"{exc.__class__.__name__}: {exc}"
        return result

    result.received = bool(result.matched_lines)
    return result


def _sanitized_from_prefix(token: str) -> str:
    """Recover the sanitized id from a ``[msg:<id>]`` token (``""`` if empty)."""
    if not token:
        return ""
    return token[len("[msg:") : -1]
