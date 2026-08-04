"""gitlab credential reader."""
from __future__ import annotations
from hermes_multitenancy import credential_hub as _hub  # route patchable helpers via package namespace

from pathlib import Path
from typing import Any, Optional

from .._io import _read_small_text
from ..model import (
    GITLAB,
    S_CONFIGURED,
    S_MISSING,
    S_NEEDS_AUTH,
    CredentialRow,
    _TITLES,
    human_expiry,
)


def _personal_record(profile_dir: Path) -> Optional[dict[str, Any]]:
    """The employee's own vaulted gitlab token row, if they submitted one.

    A personal token is deliberately env-only — it never lands in
    ``workspace/credentials/gitlab.token`` — so a file-only reader would report
    "missing" to the very users who just configured themselves. The vault is the
    source of truth here; the file is only the shared-credential fallback.
    """
    try:
        from ...credentials import CredentialStore
        from ...gitlab_token_intake import gitlab_subject_id

        profile_dir = Path(profile_dir)
        shared_home = profile_dir.parent.parent
        subject_id = gitlab_subject_id(shared_home)
        if not subject_id:
            return None
        db_path = shared_home / "multitenancy.db"
        if not db_path.exists():
            return None
        store = CredentialStore(db_path)
        try:
            status = store.get_status(
                profile_name=profile_dir.name,
                subject_id=subject_id,
                provider="gitlab",
                secret_kind="token",
            )
        finally:
            store.close()
        return status if status.get("status") != "missing" else None
    except Exception:
        return None


def gitlab_status(*, profile_dir: Path, installed: bool = False) -> CredentialRow:
    """gitlab — personal vaulted token wins; shared token file is the fallback."""
    token_path = Path(profile_dir) / "workspace" / "credentials" / "gitlab.token"
    can_read = bool(_hub._read_small_text(token_path).strip())
    personal = _personal_record(profile_dir)
    is_installed = installed or can_read or personal is not None
    row = CredentialRow(
        id=GITLAB, title=_TITLES[GITLAB], provider="gitlab",
        installed=is_installed, status=S_MISSING,
        action={"kind": "manual", "label": "配置"},
    )
    if personal is not None:
        expires_at = personal.get("expires_at")
        row.status = S_CONFIGURED
        row.expires_at = expires_at
        row.default_identity = "user"
        window = human_expiry(expires_at)
        row.detail = "使用你本人提供的 GitLab token（不展示内容）。"
        if window:
            row.detail += f"{window}。"
        if personal.get("status") == "expired":
            # Still "configured" in the WebUI's vocabulary — there IS a token —
            # but it will fail every call until replaced, so say so plainly.
            row.status = S_NEEDS_AUTH
            row.detail = "你的 GitLab token 已过期，请重新提交一个。"
        row.action["label"] = "更换"
    elif can_read:
        row.status = S_CONFIGURED
        row.detail = "当前使用全局 GitLab token（不展示内容）。可提交你自己的 token 来替代它。"
        row.action["label"] = "改用我自己的"
    elif is_installed:
        row.status = S_NEEDS_AUTH
        row.detail = "需要为当前 profile 提供可读的 GitLab token。"
    else:
        row.detail = "该 profile 没有 GitLab 相关 skill 或 token。"
    return row
