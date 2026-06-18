"""Connector Registry — the Run Broker's connector control plane.

A *connector* is an external capability a user/profile/tenant can authorize,
audit, and have a skill or tool invoke. MCP is just one connector tool surface,
not the connector itself.

This package is the source-of-truth control plane the
``2026-06-18-hermes-connector-registry-plan.md`` describes. Phase 1 wraps the
existing ``credential_hub`` / ``credential_hub_auth`` readers WITHOUT changing
any runtime behavior: the registry enriches each credential row with the
防串号 scope fields (profile / scope / acting_identity / credential_owner) and
the high-risk ``runtime_policy_owner`` marker, while the legacy
``credential_hub.collect_credential_statuses`` keeps returning byte-identical
``CredentialRow`` output by delegating here.

Dependency direction (no import-time cycle): this package imports
``credential_hub`` readers at module load; ``credential_hub`` only reaches back
into the registry through a function-local (lazy) import in the deprecated
``collect_credential_statuses`` adapter. The registry is the core; the hub is a
transitional adapter.
"""
from __future__ import annotations

from .models import (
    AuthAction,
    AuthFlowSpec,
    ConnectorDefinition,
    ConnectorPolicy,
    ConnectorStatus,
    ConnectorUiSpec,
    InvocationSpec,
)

__all__ = [
    "AuthAction",
    "AuthFlowSpec",
    "ConnectorDefinition",
    "ConnectorPolicy",
    "ConnectorStatus",
    "ConnectorUiSpec",
    "InvocationSpec",
]
