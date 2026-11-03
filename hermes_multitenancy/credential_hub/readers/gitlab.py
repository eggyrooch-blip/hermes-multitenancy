"""gitlab credential reader."""
from __future__ import annotations
from hermes_multitenancy import credential_hub as _hub  # route patchable helpers via package namespace

from pathlib import Path
from typing import Any, Optional

from .._io import _read_small_text
from ..model import (
    GITLAB,
    GITLAB_PERSONAL,
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


def _is_group_profile(profile_dir: Path) -> bool:
    """True when this profile is a routed GROUP profile (kind='group').

    The panel texts must speak in group terms there — the token is the group
    owner's and every session in the chat uses it — or the card reads like a
    personal bind that never takes effect (the 2026-08-14 zhaozhiguang bug).
    """
    try:
        from ...routing import RoutingTable

        profile_dir = Path(profile_dir)
        db_path = profile_dir.parent.parent / "multitenancy.db"
        if not db_path.exists():
            return False
        table = RoutingTable(db_path)
        try:
            row = table.lookup_by_profile_name(profile_dir.name)
        finally:
            table.close()
        return bool(row is not None and row.kind == "group")
    except Exception:
        return False


def gitlab_status(*, profile_dir: Path, installed: bool = False) -> list[CredentialRow]:
    """gitlab — 两行：管理员放的全局 token，和员工自己绑的个人 token。

    以前这是一行，两种来源互斥地抢同一张卡（有个人的就把全局的顶掉）。结果员工既看不出
    "公司给了我一个共用的"和"我可以绑自己的"是两件事，也找不到绑定入口 —— 那张卡在只有
    全局 token 时按钮叫「改用我自己的」，在页面上被当成刷新按钮从没人点过。

    拆成两行后各自独立：左卡只陈述全局 token 的存在（管理员运维，员工改不了，所以
    ``action={}`` —— 客户端据此不渲染按钮），右卡才是员工的操作面。
    """
    token_path = Path(profile_dir) / "workspace" / "credentials" / "gitlab.token"
    can_read = bool(_hub._read_small_text(token_path).strip())
    personal = _personal_record(profile_dir)
    is_group = _is_group_profile(profile_dir)
    # 任一来源可用即算"装了"，两行共享这个判断：个人卡在没有全局 token 时也要能显示成
    # "未绑定"而不是"未安装"，否则员工连绑定按钮都看不到。
    is_installed = installed or can_read or personal is not None

    # --- 左卡：全局 token（只读陈述，无按钮）---
    global_row = CredentialRow(
        id=GITLAB, title=_TITLES[GITLAB], provider="gitlab",
        installed=is_installed,
        status=S_CONFIGURED if can_read else (S_NEEDS_AUTH if is_installed else S_MISSING),
        action={},  # 管理员运维，员工无操作 —— 给按钮等于骗人
    )
    if can_read:
        global_row.default_identity = "shared"
        global_row.detail = "管理员配置的共用 GitLab token（不展示内容），全员共享；你不能改它。"
    elif is_installed:
        global_row.detail = "当前 profile 读不到全局 GitLab token，需要管理员配置。"
    else:
        global_row.detail = "该 profile 没有 GitLab 相关 skill 或 token。"

    # --- 右卡：员工自己的 token（唯一的操作面）---
    personal_row = CredentialRow(
        id=GITLAB_PERSONAL, title=_TITLES[GITLAB_PERSONAL], provider="gitlab",
        installed=is_installed, status=S_NEEDS_AUTH,
        action={"kind": "manual", "label": "群主绑定 GitLab" if is_group else "绑定我的 GitLab"},
    )
    if personal is not None:
        expires_at = personal.get("expires_at")
        personal_row.status = S_CONFIGURED
        personal_row.expires_at = expires_at
        personal_row.default_identity = "user"
        window = human_expiry(expires_at)
        personal_row.detail = (
            "使用群主提供的 GitLab token（不展示内容），本群所有会话共用。"
            if is_group
            else "使用你本人提供的 GitLab token（不展示内容）。"
        )
        if window:
            personal_row.detail += f"{window}。"
        if personal.get("status") == "expired":
            # Still "configured" in the WebUI's vocabulary — there IS a token —
            # but it will fail every call until replaced, so say so plainly.
            personal_row.status = S_NEEDS_AUTH
            personal_row.detail = (
                "本群绑定的 GitLab token 已过期，请群主重新提交一个。"
                if is_group
                else "你的 GitLab token 已过期，请重新提交一个。"
            )
        personal_row.action["label"] = "更换"
    else:
        personal_row.detail = (
            "群主绑定后，本群所有会话都会用群主的权限操作仓库；不绑就一直用全局那个。"
            if is_group
            else "绑定后 hermes 用你本人的权限操作仓库；不绑就一直用全局那个。"
        )

    return [global_row, personal_row]
