"""gitlab credential reader."""
from __future__ import annotations
from hermes_multitenancy import credential_hub as _hub  # route patchable helpers via package namespace

from pathlib import Path

from .._io import _read_small_text
from ..model import (
    GITLAB,
    S_CONFIGURED,
    S_MISSING,
    S_NEEDS_AUTH,
    CredentialRow,
    _TITLES,
)


def gitlab_status(*, profile_dir: Path, installed: bool = False) -> CredentialRow:
    """gitlab — readable profile token file ⇒ configured (no interactive flow)."""
    token_path = Path(profile_dir) / "workspace" / "credentials" / "gitlab.token"
    can_read = bool(_hub._read_small_text(token_path).strip())
    is_installed = installed or can_read
    row = CredentialRow(
        id=GITLAB, title=_TITLES[GITLAB], provider="gitlab",
        installed=is_installed, status=S_MISSING,
        action={"kind": "manual", "label": "配置"},
    )
    if can_read:
        row.status = S_CONFIGURED
        row.detail = "当前 profile 可读取 GitLab token（不展示内容）。"
        row.action["label"] = "刷新"
    elif is_installed:
        row.status = S_NEEDS_AUTH
        row.detail = "需要为当前 profile 提供可读的 GitLab token。"
    else:
        row.detail = "该 profile 没有 GitLab 相关 skill 或 token。"
    return row
