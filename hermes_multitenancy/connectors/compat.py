"""Compatibility bridge between the registry and the legacy credential shapes.

Two directions:

* ``enrich`` — legacy ``CredentialRow`` + ``ConnectorDefinition`` →
  ``ConnectorStatus`` (adds the 防串号 scope/policy fields).
* ``to_credential_row`` / ``to_credential_row_dict`` — ``ConnectorStatus`` back
  to the EXACT legacy ``CredentialRow`` (object / redacted dict). This is what
  keeps the old ``/credentials/hub`` endpoint and the Feishu ``/auth`` card
  byte-stable: the additive fields are dropped, the legacy fields pass through
  1:1 because ``ConnectorStatus`` is a strict superset.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from .models import ActingIdentity, AuthAction, ConnectorDefinition, ConnectorStatus

if TYPE_CHECKING:  # avoid import cycle at module load
    from ..credential_hub import CredentialRow


def _acting_identity(row_default_identity: Optional[str], definition: ConnectorDefinition) -> ActingIdentity:
    """Pick the live acting identity: the row's own default if valid, else policy."""
    candidate = str(row_default_identity or "").strip()
    if candidate in definition.policy.supported_identities:
        return candidate  # type: ignore[return-value]
    return definition.policy.default_identity


def enrich(row: "CredentialRow", definition: ConnectorDefinition, *, profile: Optional[str]) -> ConnectorStatus:
    """Wrap a legacy ``CredentialRow`` into a ``ConnectorStatus``.

    The legacy fields are copied 1:1; the additive fields come from the static
    ``ConnectorDefinition`` plus the live ``profile``. ``credential_owner`` is a
    redacted attribution string (NOT a path): for profile-scoped secrets it is
    the profile name, never the real profile-home secret path.
    """
    secrets_owner = definition.policy.secrets_owner
    if secrets_owner == "profile_home":
        credential_owner = profile  # redacted: profile name, not a filesystem path
    else:
        credential_owner = secrets_owner

    return ConnectorStatus(
        # legacy 1:1
        id=row.id,
        title=row.title,
        provider=row.provider,
        installed=row.installed,
        status=row.status,  # type: ignore[arg-type]
        expires_at=row.expires_at,
        account_hint=row.account_hint,
        default_identity=row.default_identity,
        detail=row.detail,
        required_by=list(row.required_by),
        action=AuthAction.from_dict(row.action),
        # additive control-plane
        profile=profile,
        scope=definition.scope,
        acting_identity=_acting_identity(row.default_identity, definition),
        credential_owner=credential_owner,
        runtime_policy_owner=definition.policy.runtime_policy_owner,
        kind=definition.kind,
        stale=False,
    )


def to_credential_row(status: ConnectorStatus) -> "CredentialRow":
    """Reconstruct the legacy ``CredentialRow`` (lossless for legacy fields)."""
    from ..credential_hub import CredentialRow

    return CredentialRow(
        id=status.id,
        title=status.title,
        provider=status.provider,
        installed=status.installed,
        status=status.status,
        expires_at=status.expires_at,
        account_hint=status.account_hint,
        default_identity=status.default_identity,
        detail=status.detail,
        required_by=list(status.required_by),
        action=status.action.to_dict() if status.action else {},
    )


def to_credential_row_dict(status: ConnectorStatus) -> dict[str, Any]:
    """Byte-stable legacy redacted dict — identical to ``CredentialRow.to_dict``."""
    return to_credential_row(status).to_dict()
