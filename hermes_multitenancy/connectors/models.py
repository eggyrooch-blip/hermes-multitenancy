"""Connector Registry data models.

Two orthogonal axes per the plan: *how to invoke* (``InvocationSpec``) and
*how to authorize* (``AuthFlowSpec``). ``ConnectorStatus`` is a strict superset
of the legacy ``credential_hub.CredentialRow`` fields plus the 防串号 scope
fields, so it can round-trip back to the old redacted dict shape with zero loss
(see ``compat.py``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

# --- enums (kept as Literal for typing; values are the wire strings) ---------

InvocationType = Literal["native_cli", "skill_script", "cli_command", "token", "mcp", "manual"]
AuthFlowType = Literal[
    "existing_profile_secret", "qr_poll", "oauth_callback", "manual_token", "mcp_oauth", "none"
]
ConnectorKind = Literal["internal", "external", "manual"]
ConnectorScope = Literal["profile", "user", "tenant", "global"]
ActingIdentity = Literal["user", "bot", "auto", "none"]
# Status vocab is EXACTLY the WebUI SkillCredentialState set (no 'expired').
ConnectorState = Literal["authenticated", "configured", "needs_auth", "unknown", "missing", "error"]


@dataclass(frozen=True)
class InvocationSpec:
    """How a connector is invoked at runtime."""

    type: InvocationType
    # Optional hint for drivers (e.g. the skill dir name, cli binary name). The
    # registry never executes from this in Phase 1 — runtime execution stays in
    # the existing hermes-agent / lark-cli paths.
    detail: Optional[str] = None


@dataclass(frozen=True)
class AuthFlowSpec:
    """How a connector's credential is authorized."""

    type: AuthFlowType
    # Names of the credential_hub_auth functions this flow delegates to (Phase 1
    # keeps the real implementation in credential_hub_auth; auth_flows.py only
    # wraps them). Informational / for audit + diagnostics.
    status_probe: Optional[str] = None
    start: Optional[str] = None
    poll: Optional[str] = None
    complete: Optional[str] = None


@dataclass(frozen=True)
class ConnectorPolicy:
    """Multi-tenant safety policy for a connector."""

    supported_identities: tuple[ActingIdentity, ...] = ("user",)
    default_identity: ActingIdentity = "user"
    audit: bool = True
    # Where the secret physically lives — used for credential_owner attribution.
    secrets_owner: str = "profile_home"
    # Who owns the RUNTIME policy. For high-risk connectors this is NOT the
    # registry: lark-cli is 'authsidecar_broker' to make explicit that the
    # registry only reports status / starts auth and NEVER proxies IM send.
    runtime_policy_owner: str = "connector_driver"


@dataclass(frozen=True)
class ConnectorUiSpec:
    """How the connector renders in the WebUI / Feishu card."""

    group: str = "internal-systems"
    action: str = "manual"


@dataclass(frozen=True)
class ConnectorDefinition:
    """Static definition of a connector (the 'what', not the live status)."""

    id: str
    title: str
    provider: str
    kind: ConnectorKind
    scope: ConnectorScope
    invocation: InvocationSpec
    auth_flow: AuthFlowSpec
    policy: ConnectorPolicy
    ui: ConnectorUiSpec
    compat_ids: tuple[str, ...] = ()


@dataclass
class AuthAction:
    """A redacted auth action descriptor (mirrors the legacy ``action`` dict).

    Kept dict-compatible: ``kind`` is one of
    ``feishu_device_flow | skill_flow | qr_flow | oauth_url | manual`` and the
    free-form extra keys (``label``, ``command``) survive round-trip.
    """

    kind: str
    label: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Optional[dict[str, Any]]) -> Optional["AuthAction"]:
        if not raw:
            return None
        kind = str(raw.get("kind") or "manual")
        label = str(raw.get("label") or "")
        extra = {k: v for k, v in raw.items() if k not in ("kind", "label")}
        return cls(kind=kind, label=label, extra=extra)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "label": self.label}
        out.update(self.extra)
        return out


@dataclass
class ConnectorStatus:
    """One connector's live, redacted status for a (profile, user) context.

    Superset of ``CredentialRow``: the first block mirrors the legacy fields
    1:1 (so ``compat.to_credential_row`` is lossless), the second block is the
    additive 防串号 / control-plane metadata. NEVER serialize a secret here —
    no token, raw env, keyring path, or real profile-home secret path.
    """

    # --- legacy CredentialRow-compatible fields (1:1) ---
    id: str
    title: str
    provider: str
    installed: bool
    status: ConnectorState
    expires_at: Optional[int] = None  # epoch ms (additive; lark/keep/kep)
    account_hint: Optional[str] = None
    default_identity: Optional[str] = None
    detail: Optional[str] = None
    required_by: list[str] = field(default_factory=list)
    action: Optional[AuthAction] = None
    environments: dict[str, dict[str, Any]] = field(default_factory=dict)

    # --- additive control-plane fields (防串号 + policy) ---
    profile: Optional[str] = None
    scope: str = "profile"
    acting_identity: ActingIdentity = "user"
    credential_owner: Optional[str] = None
    runtime_policy_owner: str = "connector_driver"
    kind: str = "internal"
    stale: bool = False

    @property
    def authenticated(self) -> bool:
        return self.status == "authenticated"

    def to_dict(self) -> dict[str, Any]:
        """Full ConnectorStatus wire shape for the NEW /connectors endpoint.

        Includes every additive field. The LEGACY shape is produced separately
        by ``compat.to_credential_row_dict`` to keep the old endpoint byte-stable.
        """
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "provider": self.provider,
            "installed": self.installed,
            "status": self.status,
            "expires_at": self.expires_at,
            "profile": self.profile,
            "scope": self.scope,
            "acting_identity": self.acting_identity,
            "credential_owner": self.credential_owner,
            "runtime_policy_owner": self.runtime_policy_owner,
            "kind": self.kind,
            "stale": self.stale,
            # None，不是伪造 {"kind":"manual","label":""}。这一层才是 WebUI 真正读的
            # /connectors 产物；伪造在这里等于把「这张卡没有员工可执行的操作」的语义
            # 抹掉，逼客户端拿空 label 当哨兵（codex 评审 2026-08-04）。
            "action": self.action.to_dict() if self.action else None,
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
