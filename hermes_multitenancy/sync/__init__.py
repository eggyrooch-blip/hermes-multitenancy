"""feishu-sync — keep multitenancy_routing in sync with an external user list.

Reference implementation. Real deployments wire this into a Feishu HR webhook
(``feishu_hr.subscribe_events``); the spike layer here just defines the
``apply_users`` core so a CLI / cron / webhook can all share it.

Contract:
  - ``apply_users(table, users)`` is idempotent. Re-running with the same
    list is a no-op (modulo synced_at + version bumps in the row).
  - Users present in the table but absent from ``users`` get **soft-deleted**.
    The plugin treats soft-deleted rows as routing-miss (per US-007 schema).
"""
from __future__ import annotations

from .feishu_hr import UserSpec, apply_users, plan_users
from .feishu_org import (
    Department,
    DepartmentUser,
    Employee,
    FeishuContactClient,
    FeishuOrgSyncError,
    OrgSnapshot,
    build_org_snapshot,
    build_user_specs,
    fetch_contact_directory,
    profile_name_for_user_id,
    pull_feishu_org,
    save_snapshot,
    sync_feishu_org,
    sync_profiles,
)

__all__ = [
    "Department",
    "DepartmentUser",
    "Employee",
    "FeishuContactClient",
    "FeishuOrgSyncError",
    "OrgSnapshot",
    "UserSpec",
    "apply_users",
    "build_org_snapshot",
    "build_user_specs",
    "fetch_contact_directory",
    "plan_users",
    "profile_name_for_user_id",
    "pull_feishu_org",
    "save_snapshot",
    "sync_feishu_org",
    "sync_profiles",
]
