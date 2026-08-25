"""Channel-neutral run broker contracts."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Optional

RunChannel = Literal["feishu", "webui", "cron", "kanban"]
RunEventKind = Literal[
    "content",
    "thinking",
    "tool_started",
    "tool_completed",
    "approval_required",
    "approval_resolved",
    "auth_required",
    "done",
    "error",
]

_VALID_CHANNELS = {"feishu", "webui", "cron", "kanban"}


def _clean(value: str) -> str:
    return str(value or "").strip()


def _content_key(content: str) -> str:
    normalized = re.sub(r"\s+", " ", content).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def resolve_profile_workspace(
    profile_home: Path,
    workspace: Optional[str],
) -> tuple[Optional[str], Path]:
    """Resolve one untrusted relative workspace inside a routed profile."""
    root = profile_home / "workspace"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ValueError("invalid workspace")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("invalid workspace")

    if workspace is None or workspace == "":
        return None, root.resolve(strict=True)
    if not isinstance(workspace, str):
        raise ValueError("invalid workspace")
    raw = workspace.strip()
    path = PurePosixPath(raw)
    if (
        not raw
        or "\\" in raw
        or "\0" in raw
        or path.is_absolute()
        or any(part in {".", ".."} for part in raw.split("/"))
    ):
        raise ValueError("invalid workspace")
    try:
        root_real = root.resolve(strict=True)
        target = (root / path).resolve(strict=True)
        relative = target.relative_to(root_real)
    except (OSError, ValueError):
        raise ValueError("invalid workspace") from None
    if not target.is_dir():
        raise ValueError("invalid workspace")
    normalized = relative.as_posix()
    return (normalized if normalized != "." else None), target


@dataclass(frozen=True)
class RunRequest:
    """A tenant-scoped executable agent run request from any channel."""

    channel: RunChannel
    profile_name: str
    user_key: str
    content: str
    chat_id: Optional[str] = None
    session_id: Optional[str] = None
    message_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    delivery_mode: str = "stream"
    credential_subject: Optional[str] = None
    requires_host_tools: bool = False
    workspace: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        channel = _clean(self.channel)
        profile_name = _clean(self.profile_name)
        user_key = _clean(self.user_key)
        content = str(self.content or "").strip()
        if channel not in _VALID_CHANNELS:
            raise ValueError(f"channel must be one of {sorted(_VALID_CHANNELS)}")
        if not profile_name:
            raise ValueError("profile_name is required")
        if not user_key:
            raise ValueError("user_key is required")
        if not content:
            raise ValueError("content is required")
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "profile_name", profile_name)
        object.__setattr__(self, "user_key", user_key)
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "delivery_mode", _clean(self.delivery_mode) or "stream")
        if self.workspace is not None:
            object.__setattr__(self, "workspace", _clean(self.workspace) or None)
        object.__setattr__(
            self,
            "messages",
            [dict(message) for message in self.messages if isinstance(message, dict)],
        )
        if self.credential_subject is None:
            object.__setattr__(self, "credential_subject", user_key)

    @property
    def effective_idempotency_key(self) -> str:
        """Return the stable key used to dedupe inbound run submissions."""
        explicit = _clean(self.idempotency_key or "")
        if explicit:
            return explicit
        message_id = _clean(self.message_id or "")
        if message_id:
            return f"{self.channel}:{self.profile_name}:{self.user_key}:{message_id}"
        return (
            f"{self.channel}:{self.profile_name}:{self.user_key}:"
            f"content:{_content_key(self.content)}"
        )


@dataclass(frozen=True)
class RunEvent:
    """Channel-neutral stream event emitted by the broker."""

    kind: RunEventKind
    text: str = ""
    name: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    source_refs: Optional[list[dict[str, str]]] = None


@dataclass(frozen=True)
class RunResult:
    """Final broker outcome for a run request."""

    content: str
    duplicate: bool = False
    run_id: Optional[str] = None
