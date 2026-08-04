"""Invocation drivers — Phase 1 status/auth dispatch only.

A driver groups the generic lifecycle for one ``invocation.type``. In Phase 1
the drivers do NOT execute the connector's runtime invocation — runtime tool
execution stays in the existing hermes-agent / lark-cli paths and is guarded
there. The drivers only route auth start/poll/complete to ``auth_flows`` based
on the connector's ``auth_flow.type``.

This keeps the high-risk rule explicit and testable: a ``NativeCliConnectorDriver``
for ``lark-cli`` exposes NO send/proxy method, so the registry structurally
cannot proxy IM send — it can only report status and start auth.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from . import auth_flows
from .models import ConnectorDefinition


class AuthFlowUnsupported(Exception):
    """Raised when an auth action is requested for a flow that does not support it."""


class ConnectorDriver:
    """Base driver — auth dispatch by ``auth_flow.type``. No runtime invocation."""

    def __init__(self, definition: ConnectorDefinition) -> None:
        self.definition = definition

    def _kep_env_name(self) -> str:
        return "pre" if self.definition.id.endswith("-pre") else "online"

    # --- auth lifecycle (delegates to auth_flows / credential_hub_auth) ---

    def start_auth(self, *, profile_dir: Path, profile_name: str, shared_home: Path,
                   public_origin: Optional[str] = None) -> dict[str, Any]:
        flow = self.definition.auth_flow.type
        if flow == "qr_poll":
            return auth_flows.start_qr(profile_dir)
        if flow == "oauth_callback":
            return auth_flows.start_oauth(profile_dir, profile_name, shared_home,
                                          public_origin=public_origin,
                                          env_name=self._kep_env_name())
        if flow in ("existing_profile_secret", "manual_token", "none"):
            # No interactive start in this control plane: existing-secret flows
            # (lark-cli/feishu-uat) start via feishu_uat_auth in the router;
            # manual_token (gitlab) is operator-placed.
            raise AuthFlowUnsupported(
                f"connector {self.definition.id!r} flow {flow!r} has no registry-driven start"
            )
        raise AuthFlowUnsupported(f"unknown auth_flow {flow!r}")

    def poll_auth(self, *, profile_dir: Path, profile_name: str, shared_home: Path,
                  qrcode_id: Optional[str] = None, timeout_ms: int = 15000) -> Any:
        flow = self.definition.auth_flow.type
        if flow == "qr_poll":
            if not qrcode_id:
                raise AuthFlowUnsupported("qr_poll requires a qrcode_id to poll")
            return auth_flows.poll_qr(profile_dir, qrcode_id, timeout_ms=timeout_ms)
        if flow == "oauth_callback":
            return auth_flows.poll_oauth(
                profile_dir,
                profile_name,
                shared_home,
                env_name=self._kep_env_name(),
            )
        raise AuthFlowUnsupported(f"connector {self.definition.id!r} flow {flow!r} has no poll")

    def complete_auth(self, *, session_id: str, query: str) -> str:
        if self.definition.auth_flow.type == "oauth_callback":
            return auth_flows.complete_oauth_callback(session_id, query)
        raise AuthFlowUnsupported(
            f"connector {self.definition.id!r} has no callback-complete step"
        )


class NativeCliConnectorDriver(ConnectorDriver):
    """A connector fronted by a real CLI binary in the shared bin dir.

    lark-cli / feishu UAT: runtime owned by authsidecar_broker — NO IM proxy here.
    gitlab: runtime is the ``glab`` binary, driven purely by the GITLAB_TOKEN /
    GITLAB_HOST pair injected per profile; there is no interactive auth start,
    so ``start_auth`` still raises for its manual_token flow.
    """


class SkillScriptConnectorDriver(ConnectorDriver):
    """keep-record — auth via the skill's own node scripts (qr_poll)."""


class CliCommandConnectorDriver(ConnectorDriver):
    """kep-cli — auth via kep-auth login/status (oauth_callback)."""


class TokenConnectorDriver(ConnectorDriver):
    """GitLab — operator-placed token; status only, no interactive flow."""


class McpConnectorDriver(ConnectorDriver):
    """MCP backend — Phase 4 placeholder; not wired in Phase 1."""


_DRIVERS_BY_INVOCATION = {
    "native_cli": NativeCliConnectorDriver,
    "skill_script": SkillScriptConnectorDriver,
    "cli_command": CliCommandConnectorDriver,
    "token": TokenConnectorDriver,
    "mcp": McpConnectorDriver,
    "manual": ConnectorDriver,
}


def driver_for(definition: ConnectorDefinition) -> ConnectorDriver:
    cls = _DRIVERS_BY_INVOCATION.get(definition.invocation.type, ConnectorDriver)
    return cls(definition)
