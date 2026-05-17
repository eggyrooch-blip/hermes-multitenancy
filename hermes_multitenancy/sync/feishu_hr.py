"""Idempotent user-list → routing-table sync.

This is the *core* of feishu-sync — the integration with Feishu's HR APIs is
left to the deployment (cron / webhook / manual export). All this module
asserts is:

  Given a desired ``users`` list, mutate ``RoutingTable`` so the active set
  matches. Specifically:
    * Insert / refresh routes present in the list.
    * Soft-delete routes present in the table but missing from the list.
    * Do nothing for matching rows (no version bumps, no last_active_at
      churn — those are router-only fields).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from ..routing import KIND_USER


@dataclass(frozen=True)
class UserSpec:
    """One user → one profile route. Mirrors the columns feishu-sync writes."""
    user_id: str
    profile_name: str
    open_id: str
    union_id: Optional[str] = None


def apply_users(
    table,
    users: Iterable[UserSpec],
    *,
    soft_delete_missing: bool = True,
) -> dict[str, int]:
    """Reconcile ``table`` to match ``users``.

    Returns a counters dict::

        {"upserted": int, "soft_deleted": int, "kept": int}

    The numbers are per-row-action, useful for ops dashboards and tests.
    """
    desired, current = _desired_and_current(table, users)
    current_by_open_id = {row["open_id"]: row for row in current.values()}

    upserted = 0
    soft_deleted = 0
    kept = 0

    # 1. Upsert / refresh
    for u in desired.values():
        existing = current.get(u.user_id)
        if existing and (
            existing["profile_name"] == u.profile_name
            and existing["open_id"] == u.open_id
            and (existing["union_id"] or None) == u.union_id
        ):
            kept += 1
            continue
        conflicting = current_by_open_id.get(u.open_id)
        if conflicting and conflicting["user_id"] != u.user_id:
            # Common migration path: an unseen sender was auto-provisioned as
            # user_id=open_id, then the org sync later learns the real
            # Feishu user_id for the same open_id. Deactivate the temporary
            # row before inserting the canonical user_id route.
            if table.soft_delete(conflicting["user_id"]):
                soft_deleted += 1
            current.pop(conflicting["user_id"], None)
        table.upsert(
            user_id=u.user_id,
            profile_name=u.profile_name,
            open_id=u.open_id,
            union_id=u.union_id,
            provenance="sync",
        )
        upserted += 1

    # 2. Soft-delete rows in table but missing from desired
    if soft_delete_missing:
        for user_id in current:
            if user_id not in desired:
                if table.soft_delete(user_id):
                    soft_deleted += 1

    return {"upserted": upserted, "soft_deleted": soft_deleted, "kept": kept}


def plan_users(
    table,
    users: Iterable[UserSpec],
    *,
    soft_delete_missing: bool = True,
) -> dict[str, int]:
    """Return the same counters as ``apply_users`` without writing rows."""
    desired, current = _desired_and_current(table, users)
    current_by_open_id = {row["open_id"]: row for row in current.values()}

    upserted = 0
    soft_deleted = 0
    kept = 0
    for u in desired.values():
        existing = current.get(u.user_id)
        if existing and (
            existing["profile_name"] == u.profile_name
            and existing["open_id"] == u.open_id
            and (existing["union_id"] or None) == u.union_id
        ):
            kept += 1
        else:
            conflicting = current_by_open_id.get(u.open_id)
            if conflicting and conflicting["user_id"] != u.user_id:
                soft_deleted += 1
                current.pop(conflicting["user_id"], None)
            upserted += 1

    if soft_delete_missing:
        soft_deleted += sum(1 for user_id in current if user_id not in desired)
    return {"upserted": upserted, "soft_deleted": soft_deleted, "kept": kept}


def _desired_and_current(table, users: Iterable[UserSpec]) -> tuple[dict[str, UserSpec], dict[str, dict]]:
    desired = {u.user_id: u for u in users}

    # Snapshot current active rows so we can detect deletes.
    cur = table._conn.execute(
        "SELECT user_id, profile_name, open_id, union_id"
        " FROM multitenancy_routing WHERE active = 1 AND kind = ? AND provenance = ?",
        (KIND_USER, "sync"),
    )
    current = {r["user_id"]: dict(r) for r in cur.fetchall()}
    return desired, current
