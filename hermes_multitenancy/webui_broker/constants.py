"""Constants and type aliases for the WebUI run-broker seam (pure move)."""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, Optional

from ..run_models import RunEvent, RunRequest

DispatchAgent = Callable[[RunRequest], Awaitable[str] | str]


EmitRunEvent = Callable[[RunEvent], Awaitable[None] | None]


MarkSeen = Callable[[RunRequest], bool]


IsSeen = Callable[[RunRequest], bool]


SandboxAvailable = Callable[[], bool]


_OWNER_OPEN_ID_HEADER = "X-Hermes-Owner-Open-Id"


_EXPERT_ID_HEADER = "X-Hermes-Expert-Id"


_AGENT_ID_HEADER = "X-Hermes-Agent-Id"


_ACTOR_PRINCIPAL_ID_HEADER = "X-Hermes-Actor-Principal-Id"


_ACTOR_PROVIDER_HEADER = "X-Hermes-Actor-Provider"


_ACTOR_TENANT_KEY_HEADER = "X-Hermes-Actor-Tenant-Key"


_ACTOR_APP_ID_HEADER = "X-Hermes-Actor-App-Id"


_ACTOR_USER_ID_HEADER = "X-Hermes-Actor-User-Id"


_ACTOR_DISPLAY_NAME_HEADER = "X-Hermes-Actor-Display-Name"


_ACTOR_DISPLAY_NAME_ENCODED_HEADER = "X-Hermes-Actor-Display-Name-Encoded"


_ACTOR_AVATAR_URL_HEADER = "X-Hermes-Actor-Avatar-Url"


_ACTOR_EMAIL_HEADER = "X-Hermes-Actor-Email"


_AGENT_SHARE_CONTEXT_METADATA_KEY = "agent_share_context"


_AGENT_SHARED_ROLES = frozenset({"viewer", "editor", "manager"})


_RUN_BROKER_DEFAULT_CLIENT_MAX_SIZE = 32 * 1024 * 1024


# Public SkillHub webhook bodies are small JSON envelopes; cap hard to avoid
# disk-fill / DoS on an internet-exposed route (the 32MB broker default is for
# WebUI run submissions, not webhooks).
_SKILLHUB_MAX_BODY_BYTES = 256 * 1024


_INGEST_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


_INGEST_AUTH_BEARER_RE = re.compile(
    r"\b(authorization\s*[:=]\s*bearer\s+)([A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)


_INGEST_JWT_LIKE_RE = re.compile(r"\beyJ[A-Za-z0-9._-]{16,}\b")


_INGEST_SECRET_TYPES = frozenset({"bearer_token", "api_key", "cookie", "basic", "opaque"})


_INGEST_SECRET_MAX_VALUE_BYTES = 16 * 1024


_INGEST_SECRET_MAX_TOTAL_BYTES = 64 * 1024


_INGEST_SECRET_USAGE = {
    "bearer_token": "Authorization Bearer",
    "api_key": "API key/header/query credential",
    "cookie": "Cookie header",
    "basic": "HTTP Basic authorization",
    "opaque": "caller-defined opaque secret",
}


_PLUGIN_ASSET_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,180}$")


_PLUGIN_ASSET_MANAGED_DIR = ".hermes-plugin-managed"


_PLUGIN_ASSET_STORAGE_DIR = ".hermes-plugin-assets"


_PLUGIN_ASSET_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


_RUN_BROKER_SHARED_ENV_KEYS = frozenset(
    {
        "HERMES_MULTITENANCY_CREDENTIAL_KEY",
        "HERMES_CREDENTIAL_KEY",
        "HERMES_LARK_CLI_APP_ID",
        "HERMES_LARK_CLI_BRAND",
        "HERMES_LARK_CLI_DEFAULT_AS",
        "HERMES_LARK_CLI_STRICT_MODE",
    }
)


_SESSION_COMMAND_RE = re.compile(r"^/([A-Za-z][\w-]*)(?:\s+([\s\S]*))?$")


_SESSION_HISTORY_COMMANDS = frozenset({"new", "reset", "status"})


_GOAL_COMMANDS = frozenset({"goal", "subgoal"})


# In-process TTL stash for webui credential-expiry replay. When a run emits an
# `auth_required` frame, its original inbound request is parked here keyed by a
# server-minted `signal_run_id`; after the user re-authenticates, the bearer-
# protected replay endpoint re-dispatches it. Bounded (TTL + cap), single
# process — never crosses restarts, mirroring the broker's own dedup posture.
# NOTE: this is the WebUI seam only; the Feishu-path replay lives independently
# in router._pending_auth_replay and must not be touched here.
_AUTH_SIGNAL_TTL_SECONDS = 600.0


_AUTH_SIGNAL_CAP = 512


_INGEST_RESERVED_METADATA_KEYS = {
    _AGENT_SHARE_CONTEXT_METADATA_KEY,
    "ingest_secret_dir",
    "ingest_secret_fingerprint",
    "ingest_secrets",
}

__all__ = [
    'DispatchAgent',
    'EmitRunEvent',
    'MarkSeen',
    'IsSeen',
    'SandboxAvailable',
    '_OWNER_OPEN_ID_HEADER',
    '_EXPERT_ID_HEADER',
    '_AGENT_ID_HEADER',
    '_ACTOR_PRINCIPAL_ID_HEADER',
    '_ACTOR_PROVIDER_HEADER',
    '_ACTOR_TENANT_KEY_HEADER',
    '_ACTOR_APP_ID_HEADER',
    '_ACTOR_USER_ID_HEADER',
    '_ACTOR_DISPLAY_NAME_HEADER',
    '_ACTOR_DISPLAY_NAME_ENCODED_HEADER',
    '_ACTOR_AVATAR_URL_HEADER',
    '_ACTOR_EMAIL_HEADER',
    '_AGENT_SHARE_CONTEXT_METADATA_KEY',
    '_AGENT_SHARED_ROLES',
    '_RUN_BROKER_DEFAULT_CLIENT_MAX_SIZE',
    '_SKILLHUB_MAX_BODY_BYTES',
    '_INGEST_SECRET_NAME_RE',
    '_INGEST_AUTH_BEARER_RE',
    '_INGEST_JWT_LIKE_RE',
    '_INGEST_SECRET_TYPES',
    '_INGEST_SECRET_MAX_VALUE_BYTES',
    '_INGEST_SECRET_MAX_TOTAL_BYTES',
    '_INGEST_SECRET_USAGE',
    '_PLUGIN_ASSET_COMPONENT_RE',
    '_PLUGIN_ASSET_MANAGED_DIR',
    '_PLUGIN_ASSET_STORAGE_DIR',
    '_PLUGIN_ASSET_MIME_BY_SUFFIX',
    '_RUN_BROKER_SHARED_ENV_KEYS',
    '_SESSION_COMMAND_RE',
    '_SESSION_HISTORY_COMMANDS',
    '_GOAL_COMMANDS',
    '_AUTH_SIGNAL_TTL_SECONDS',
    '_AUTH_SIGNAL_CAP',
    '_INGEST_RESERVED_METADATA_KEYS',
]
