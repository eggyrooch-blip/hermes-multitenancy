"""Credential ids/status vocabulary + the redacted ``CredentialRow`` model.

Pure data and display — zero IO. ``human_expiry`` is the only behaviour here and
depends only on ``_now_ms`` for the current clock.
"""
from __future__ import annotations
from hermes_multitenancy import credential_hub as _hub  # route patchable helpers via package namespace

from dataclasses import dataclass, field
from typing import Any, Optional

from ._io import _now_ms

# Credential ids in display order (identical set + order to the WebUI).
LARK_CLI = "lark-cli"
FEISHU_PROJECT = "feishu-project"
KEEP_RECORD = "keep-record"
KEP_CLI = "kep-cli"
KEP_CLI_ONLINE = "kep-cli-online"
KEP_CLI_PRE = "kep-cli-pre"
GITLAB = "gitlab"

CREDENTIAL_ORDER = (LARK_CLI, FEISHU_PROJECT, KEEP_RECORD, KEP_CLI_ONLINE, KEP_CLI_PRE, GITLAB)
KEP_CLI_ENV_IDS = {"online": KEP_CLI_ONLINE, "pre": KEP_CLI_PRE}
KEP_CLI_IDS = (KEP_CLI_ONLINE, KEP_CLI_PRE)

_TITLES = {
    LARK_CLI: "Lark-cli",
    FEISHU_PROJECT: "飞书项目",
    KEEP_RECORD: "Keep-record",
    KEP_CLI: "kep-cli",
    KEP_CLI_ONLINE: "kep-cli online",
    KEP_CLI_PRE: "kep-cli pre",
    GITLAB: "GitLab",
}

# Status vocabulary (matches the WebUI SkillCredentialState set).
S_AUTHENTICATED = "authenticated"
S_CONFIGURED = "configured"
S_NEEDS_AUTH = "needs_auth"
S_UNKNOWN = "unknown"
S_MISSING = "missing"
S_ERROR = "error"  # never emitted today, kept for import compatibility with pre-split callers


@dataclass
class CredentialRow:
    """One credential's redacted status (SkillCredentialEntry-compatible)."""

    id: str
    title: str
    provider: str
    installed: bool
    status: str
    expires_at: Optional[int] = None  # epoch ms (additive; lark/keep)
    account_hint: Optional[str] = None
    default_identity: Optional[str] = None
    detail: Optional[str] = None
    required_by: list[str] = field(default_factory=list)
    action: dict[str, Any] = field(default_factory=dict)
    environments: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def authenticated(self) -> bool:
        return self.status == S_AUTHENTICATED

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "provider": self.provider,
            "installed": self.installed,
            "status": self.status,
            "expires_at": self.expires_at,
            "action": self.action or {"kind": "manual", "label": ""},
        }
        if self.account_hint:
            out["account_hint"] = self.account_hint
        if self.default_identity:
            out["default_identity"] = self.default_identity
        if self.detail:
            out["detail"] = self.detail
        if self.required_by:
            out["required_by"] = self.required_by
        if self.environments:
            out["environments"] = self.environments
        return out


def human_expiry(expires_at: Optional[int], *, now_ms: Optional[int] = None) -> str:
    """Render an expiry timestamp (ms) as a short zh phrase, or '' if unknown."""
    if not expires_at:
        return ""
    now = now_ms if now_ms is not None else _hub._now_ms()
    delta_ms = int(expires_at) - now
    if delta_ms <= 0:
        return "已过期"
    days = delta_ms // (24 * 3600 * 1000)
    if days >= 1:
        return f"{days}天后过期"
    hours = delta_ms // (3600 * 1000)
    if hours >= 1:
        return f"{hours}小时后过期"
    minutes = max(1, delta_ms // (60 * 1000))
    return f"{minutes}分钟后过期"
