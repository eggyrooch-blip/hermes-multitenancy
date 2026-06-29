"""Built-in connector definitions — the first-party connectors.

These mirror exactly the ids / providers / display order that
``credential_hub`` already uses, so the new ``/connectors`` endpoint and the
legacy ``/credentials/hub`` endpoint describe the same capabilities.

Phase 1 adds NO new connectors and changes NO runtime behavior; this is purely
the static metadata that enriches each existing credential row with scope and
policy fields.
"""
from __future__ import annotations

from .models import (
    AuthFlowSpec,
    ConnectorDefinition,
    ConnectorPolicy,
    ConnectorUiSpec,
    InvocationSpec,
)

# Ids — identical display order to credential_hub.CREDENTIAL_ORDER.
LARK_CLI = "lark-cli"
FEISHU_PROJECT = "feishu-project"
KEEP_RECORD = "keep-record"
KEP_CLI = "kep-cli"
KEP_CLI_ONLINE = "kep-cli-online"
KEP_CLI_PRE = "kep-cli-pre"
GITLAB = "gitlab"

CONNECTOR_ORDER = (LARK_CLI, FEISHU_PROJECT, KEEP_RECORD, KEP_CLI_ONLINE, KEP_CLI_PRE, GITLAB)


BUILTIN_CONNECTORS: dict[str, ConnectorDefinition] = {
    LARK_CLI: ConnectorDefinition(
        id=LARK_CLI,
        title="Lark-cli",
        provider="lark",
        kind="internal",
        scope="profile",
        invocation=InvocationSpec(type="native_cli", detail="lark-cli"),
        auth_flow=AuthFlowSpec(
            type="existing_profile_secret",
            status_probe="feishu_uat_auth.credential_status",
            start="feishu_uat_auth.start_session",
        ),
        # HIGH-RISK: the registry only REPORTS status and STARTS auth. It never
        # proxies Feishu IM send, never replaces lark_cli_tool schema/shortcut/api
        # policy, never bypasses lark_cli_auth_broker HMAC/run-token/allowlist, and
        # never alters the personal-bot owner-mapped group allowlist. The runtime
        # policy is owned by the authsidecar broker, NOT this control plane.
        policy=ConnectorPolicy(
            supported_identities=("user", "bot"),
            default_identity="user",
            audit=True,
            secrets_owner="profile_home",
            runtime_policy_owner="authsidecar_broker",
        ),
        ui=ConnectorUiSpec(group="internal-systems", action="feishu_device_flow"),
    ),
    FEISHU_PROJECT: ConnectorDefinition(
        id=FEISHU_PROJECT,
        title="飞书项目",
        provider="feishu-project",
        kind="internal",
        scope="profile",
        invocation=InvocationSpec(type="native_cli", detail="meegle"),
        auth_flow=AuthFlowSpec(
            type="existing_profile_secret",
            status_probe="credential_hub.feishu_project_status",
        ),
        policy=ConnectorPolicy(
            supported_identities=("user",),
            default_identity="user",
            audit=True,
            secrets_owner="profile_home",
            runtime_policy_owner="connector_driver",
        ),
        ui=ConnectorUiSpec(group="internal-systems", action="oauth_url"),
    ),
    KEEP_RECORD: ConnectorDefinition(
        id=KEEP_RECORD,
        title="Keep-record",
        provider="keep",
        kind="internal",
        scope="profile",
        invocation=InvocationSpec(type="skill_script", detail="Keep/keep-record"),
        auth_flow=AuthFlowSpec(
            type="qr_poll",
            status_probe="credential_hub.keep_record_status",
            start="credential_hub_auth.start_keep_record_qr",
            poll="credential_hub_auth.poll_keep_record_once",
        ),
        policy=ConnectorPolicy(
            supported_identities=("user",),
            default_identity="user",
            audit=True,
            secrets_owner="profile_home",
            runtime_policy_owner="connector_driver",
        ),
        ui=ConnectorUiSpec(group="internal-systems", action="qr_flow"),
    ),
    KEP_CLI_ONLINE: ConnectorDefinition(
        id=KEP_CLI_ONLINE,
        title="kep-cli online",
        provider="keep",
        kind="internal",
        scope="profile",
        invocation=InvocationSpec(type="cli_command", detail="kep-auth"),
        auth_flow=AuthFlowSpec(
            type="oauth_callback",
            status_probe="credential_hub.kep_cli_status",
            start="credential_hub_auth.start_kep_cli_login",
            poll="credential_hub_auth.kep_cli_logged_in",
            complete="credential_hub_auth.complete_kep_callback",
        ),
        policy=ConnectorPolicy(
            supported_identities=("user",),
            default_identity="user",
            audit=True,
            secrets_owner="profile_home",
            runtime_policy_owner="connector_driver",
        ),
        ui=ConnectorUiSpec(group="internal-systems", action="oauth_url"),
    ),
    KEP_CLI_PRE: ConnectorDefinition(
        id=KEP_CLI_PRE,
        title="kep-cli pre",
        provider="keep",
        kind="internal",
        scope="profile",
        invocation=InvocationSpec(type="cli_command", detail="kep-auth"),
        auth_flow=AuthFlowSpec(
            type="oauth_callback",
            status_probe="credential_hub.kep_cli_status",
            start="credential_hub_auth.start_kep_cli_login",
            poll="credential_hub_auth.kep_cli_logged_in",
            complete="credential_hub_auth.complete_kep_callback",
        ),
        policy=ConnectorPolicy(
            supported_identities=("user",),
            default_identity="user",
            audit=True,
            secrets_owner="profile_home",
            runtime_policy_owner="connector_driver",
        ),
        ui=ConnectorUiSpec(group="internal-systems", action="oauth_url"),
    ),
    KEP_CLI: ConnectorDefinition(
        id=KEP_CLI,
        title="kep-cli",
        provider="keep",
        kind="internal",
        scope="profile",
        invocation=InvocationSpec(type="cli_command", detail="kep-auth"),
        auth_flow=AuthFlowSpec(type="oauth_callback", status_probe="credential_hub.kep_cli_status"),
        policy=ConnectorPolicy(
            supported_identities=("user",),
            default_identity="user",
            audit=True,
            secrets_owner="profile_home",
            runtime_policy_owner="connector_driver",
        ),
        ui=ConnectorUiSpec(group="internal-systems", action="oauth_url"),
        compat_ids=("keep-cli",),
    ),
    GITLAB: ConnectorDefinition(
        id=GITLAB,
        title="GitLab",
        provider="gitlab",
        kind="manual",
        scope="profile",
        invocation=InvocationSpec(type="token", detail="gitlab.token"),
        auth_flow=AuthFlowSpec(type="manual_token", status_probe="credential_hub.gitlab_status"),
        policy=ConnectorPolicy(
            supported_identities=("user",),
            default_identity="user",
            audit=True,
            secrets_owner="profile_home",
            runtime_policy_owner="connector_driver",
        ),
        ui=ConnectorUiSpec(group="internal-systems", action="manual"),
    ),
}


def get_definition(connector_id: str) -> ConnectorDefinition | None:
    """Resolve a connector definition by id or compat id."""
    direct = BUILTIN_CONNECTORS.get(connector_id)
    if direct is not None:
        return direct
    for definition in BUILTIN_CONNECTORS.values():
        if connector_id in definition.compat_ids:
            return definition
    return None
