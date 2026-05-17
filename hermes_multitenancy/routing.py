"""SQLite-backed routing table — replaces the _SPIKE_ROUTING dict.

Schema (per architect-verification.md follow-up + review v2 笔记 D4 修订版):
  - user_id PRIMARY KEY (business key for ops/feishu-sync)
  - profile_name (NOT UNIQUE — guest profile can serve multiple users)
  - open_id (queryable hot path, UNIQUE among active user rows only)
  - union_id (cross-app stable id, optional)
  - active flag + deleted_at + synced_at + version (soft-delete + sync-staleness)
  - kind ('user' | 'group') — group rows are per-chat-id profiles
  - chat_id (UNIQUE among active group rows; NULL for user rows)
  - owner_open_id (group inviter; NULL for user rows)
  - display_label (Web UI label like "owner-IT组"; NULL for user rows)

Read/write contract (per design D6 in review v2 + group-profile extension):
  - feishu-sync writes user_id / profile_name / open_id / union_id (and version++)
  - router writes group rows via upsert_group()
  - router only updates last_active_at via touch_active(open_id) for user rows
    and touch_active_group(chat_id) for group rows.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Default db path — independent from ~/.hermes/state.db so router writes don't
# contend with gateway sessions/pairing/cron writes for the WAL.
DEFAULT_DB_PATH = Path.home() / ".hermes" / "multitenancy.db"

KIND_USER = "user"
KIND_GROUP = "group"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS multitenancy_routing (
    user_id        TEXT PRIMARY KEY NOT NULL,
    profile_name   TEXT NOT NULL,
    open_id        TEXT,
    union_id       TEXT,
    active         INTEGER NOT NULL DEFAULT 1,
    deleted_at     INTEGER,
    synced_at      INTEGER NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    last_active_at INTEGER,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'user',
    chat_id        TEXT,
    owner_open_id  TEXT,
    display_label  TEXT
);
"""

# Idempotent migrations for databases created by an earlier schema. SQLite
# does not support `IF NOT EXISTS` on ADD COLUMN, so probe pragma_table_info.
_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("kind", "TEXT NOT NULL DEFAULT 'user'"),
    ("chat_id", "TEXT"),
    ("owner_open_id", "TEXT"),
    ("display_label", "TEXT"),
    ("agent_id", "TEXT"),
    ("upstream_profile", "TEXT"),
    ("provenance", "TEXT NOT NULL DEFAULT 'auto'"),
)

# Old (pre-group) index name to drop before recreating a kind-aware version.
_LEGACY_OPEN_ID_INDEX = "idx_routing_open_id_active"

_NEW_INDEXES: tuple[tuple[str, str], ...] = (
    # Sync-root user rows: at most one active row per open_id. Auto/group rows
    # are exempt so one owner can accumulate multiple owned agents.
    (
        "idx_routing_open_id_active_user",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_routing_open_id_active_user "
        "ON multitenancy_routing(open_id) "
        "WHERE active = 1 AND kind = 'user' AND provenance = 'sync' "
        "AND open_id IS NOT NULL",
    ),
    # Group rows: at most one active row per chat_id. User rows are exempt.
    (
        "idx_routing_chat_id_active_group",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_routing_chat_id_active_group "
        "ON multitenancy_routing(chat_id) "
        "WHERE active = 1 AND kind = 'group' AND chat_id IS NOT NULL",
    ),
    (
        "idx_routing_active_user",
        "CREATE INDEX IF NOT EXISTS idx_routing_active_user "
        "ON multitenancy_routing(active, user_id)",
    ),
    (
        "idx_routing_owner_open_id",
        "CREATE INDEX IF NOT EXISTS idx_routing_owner_open_id "
        "ON multitenancy_routing(owner_open_id, kind) "
        "WHERE active = 1 AND owner_open_id IS NOT NULL",
    ),
    # Agent selection uses agent_id as the stable handle, so active rows must
    # stay globally unique when agent_id is populated by migration/backfill.
    (
        "idx_routing_agent_id_active",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_routing_agent_id_active "
        "ON multitenancy_routing(agent_id) "
        "WHERE active = 1 AND agent_id IS NOT NULL",
    ),
    # Group/user agent listings derive the immediate upstream profile through
    # this column, so keep active non-NULL values cheaply queryable.
    (
        "idx_routing_upstream",
        "CREATE INDEX IF NOT EXISTS idx_routing_upstream "
        "ON multitenancy_routing(upstream_profile) "
        "WHERE active = 1 AND upstream_profile IS NOT NULL",
    ),
)


@dataclass(frozen=True)
class RoutingRow:
    user_id: str
    profile_name: str
    open_id: Optional[str]
    union_id: Optional[str]
    active: bool
    last_active_at: Optional[int]
    synced_at: int
    version: int
    kind: str = KIND_USER
    chat_id: Optional[str] = None
    owner_open_id: Optional[str] = None
    display_label: Optional[str] = None


class RoutingTable:
    """SQLite-backed routing table.

    Use ``RoutingTable(":memory:")`` in tests for isolation.
    Use ``RoutingTable()`` (default) in production — points to
    ``~/.hermes/multitenancy.db``.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = str(db_path) if db_path is not None else str(DEFAULT_DB_PATH)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so the same connection survives across the
        # asyncio task switches that the plugin does. SQLite operations are
        # short and serial within a single dispatch.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("PRAGMA journal_mode=WAL;")
        # lark-bridge fans one Feishu WS out to multiple gateway processes,
        # so several RoutingTable connections can write this DB concurrently
        # (touch_active_group fires on every group message). Default
        # busy_timeout is 0 → an instant SQLITE_BUSY under contention. Block
        # up to 5s for the writer lock instead of erroring out.
        self._conn.executescript("PRAGMA busy_timeout=5000;")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Bring an older DB up to the current schema. Safe to call repeatedly."""
        cur = self._conn.execute("PRAGMA table_info(multitenancy_routing)")
        existing_cols = {row["name"] for row in cur.fetchall()}
        for col_name, col_def in _NEW_COLUMNS:
            if col_name not in existing_cols:
                self._conn.execute(
                    f"ALTER TABLE multitenancy_routing ADD COLUMN {col_name} {col_def}"
                )

        # Drop the legacy open_id unique index — its predicate doesn't include
        # `kind`, so it would block group rows that store chat_id in open_id.
        # The replacement (idx_routing_open_id_active_user) keeps user-row
        # uniqueness while leaving group rows free.
        self._conn.execute(f"DROP INDEX IF EXISTS {_LEGACY_OPEN_ID_INDEX}")
        self._conn.execute("DROP INDEX IF EXISTS idx_routing_open_id_active_user")
        for _name, sql in _NEW_INDEXES:
            self._conn.execute(sql)

        cur = self._conn.execute(
            "SELECT COUNT(*) FROM multitenancy_routing "
            "WHERE active = 1 AND agent_id IS NULL"
        )
        needs_backfill = int(cur.fetchone()[0]) > 0
        if needs_backfill:
            # Provenance must be derived from stable columns, not profile-name
            # shape: router auto-provision writes user_id == open_id, while
            # Feishu sync writes distinct user_id/open_id pairs; sync profile
            # names only sometimes keep a `feishu_` prefix, so that string
            # shape is a weak signal and not deterministic enough for migration.
            self._conn.execute(
                """
                UPDATE multitenancy_routing
                SET agent_id = user_id,
                    owner_open_id = CASE
                        WHEN kind = 'user' THEN COALESCE(owner_open_id, open_id)
                        ELSE owner_open_id
                    END,
                    provenance = CASE
                        WHEN kind = 'group' THEN 'group'
                        WHEN kind = 'user' AND user_id = open_id THEN 'auto'
                        ELSE 'sync'
                    END
                WHERE active = 1 AND agent_id IS NULL
                """
            )
            self._conn.execute(
                """
                UPDATE multitenancy_routing
                SET upstream_profile = (
                    SELECT u.profile_name
                    FROM multitenancy_routing AS u
                    WHERE u.open_id = multitenancy_routing.owner_open_id
                      AND u.active = 1
                      AND u.kind = 'user'
                    LIMIT 1
                )
                WHERE active = 1
                  AND kind = 'group'
                  AND upstream_profile IS NULL
                  AND agent_id IS NOT NULL
                """
            )

    # -- read path (router) -----------------------------------------------

    def lookup_by_open_id(self, open_id: str) -> Optional[RoutingRow]:
        """Return the active user row for open_id, or None if missing.

        Restricted to ``kind = 'user'`` so group rows (which may carry an
        owner's open_id for ACL purposes but are not the lookup key) do not
        shadow real user routes.
        """
        cur = self._conn.execute(
            "SELECT * FROM multitenancy_routing "
            "WHERE open_id = ? AND active = 1 AND kind = 'user'",
            (open_id,),
        )
        row = cur.fetchone()
        return _row_to_dataclass(row) if row else None

    def lookup_by_union_id(self, union_id: str) -> Optional[RoutingRow]:
        """Return the active user row whose union_id column matches."""
        cur = self._conn.execute(
            "SELECT * FROM multitenancy_routing "
            "WHERE union_id = ? AND active = 1 AND kind = 'user' LIMIT 1",
            (union_id,),
        )
        row = cur.fetchone()
        return _row_to_dataclass(row) if row else None

    def lookup_by_user_id(self, user_id: str) -> Optional[RoutingRow]:
        """Return the active row by tenant user_id (PRIMARY KEY).

        Closes the third lookup channel: open_id / union_id / user_id. Used
        when sync upstream (e.g. Feishu Contact v3) hands router a stable
        user_id but neither open_id nor union_id, or when callers prefer the
        canonical PK.
        """
        cur = self._conn.execute(
            "SELECT * FROM multitenancy_routing WHERE user_id = ? AND active = 1 LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
        return _row_to_dataclass(row) if row else None

    def lookup_agent(self, agent_id: str) -> Optional[RoutingRow]:
        """Return the active row for a stable agent_id, or None if missing.

        Agent selection is owner-scoped at higher layers, but the lookup key
        itself must resolve to exactly one active row across all route kinds.
        """
        cur = self._conn.execute(
            "SELECT * FROM multitenancy_routing "
            "WHERE agent_id = ? AND active = 1 LIMIT 1",
            (agent_id,),
        )
        row = cur.fetchone()
        return _row_to_dataclass(row) if row else None

    def lookup_by_chat_id(self, chat_id: str) -> Optional[RoutingRow]:
        """Return the active group row for chat_id, or None if missing.

        Group profiles are keyed by Feishu ``chat_id`` (``oc_*``). User rows
        never carry chat_id, so this is restricted to ``kind = 'group'``.
        """
        cur = self._conn.execute(
            "SELECT * FROM multitenancy_routing "
            "WHERE chat_id = ? AND active = 1 AND kind = 'group' LIMIT 1",
            (chat_id,),
        )
        row = cur.fetchone()
        return _row_to_dataclass(row) if row else None

    def resolve_owner_root(self, open_id: str) -> Optional[RoutingRow]:
        """Return the deterministic sync-root user row for a login open_id.

        Login resolution must target the single sync-owned root, not any
        auto-provisioned sibling rows that may share the same open_id.
        """
        cur = self._conn.execute(
            "SELECT * FROM multitenancy_routing "
            "WHERE open_id = ? AND active = 1 "
            "AND kind = 'user' AND provenance = 'sync' LIMIT 1",
            (open_id,),
        )
        row = cur.fetchone()
        return _row_to_dataclass(row) if row else None

    def list_by_owner(
        self, owner_open_id: str, *, kind: str | None = None
    ) -> list[RoutingRow]:
        """Return all active rows belonging to ``owner_open_id``.

        Owner-scoped surfaces need the full active route set for a user, with
        the sync root first and optional kind narrowing for legacy callers.
        """
        sql = (
            "SELECT * FROM multitenancy_routing "
            "WHERE owner_open_id = ? AND active = 1"
        )
        params: list[str] = [owner_open_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY (provenance = 'sync') DESC, created_at ASC"
        cur = self._conn.execute(sql, params)
        return [_row_to_dataclass(row) for row in cur.fetchall()]

    def list_agents_for_owner(self, open_id: str) -> list[RoutingRow]:
        """Return every active agent owned by ``open_id``.

        This named wrapper makes owner-wide agent enumeration explicit for
        login-resolution and Web UI call sites that should not imply groups-only.
        """
        return self.list_by_owner(open_id, kind=None)

    def touch_active(self, open_id: str) -> None:
        """Update last_active_at — router-only, does NOT bump version."""
        self._conn.execute(
            "UPDATE multitenancy_routing SET last_active_at = ? "
            "WHERE open_id = ? AND active = 1 AND kind = 'user'",
            (_now(), open_id),
        )
        self._conn.commit()

    def touch_active_group(self, chat_id: str) -> None:
        """Update last_active_at for a group row — does NOT bump version."""
        self._conn.execute(
            "UPDATE multitenancy_routing SET last_active_at = ? "
            "WHERE chat_id = ? AND active = 1 AND kind = 'group'",
            (_now(), chat_id),
        )
        self._conn.commit()

    # -- write path (feishu-sync) ----------------------------------------

    def upsert(
        self,
        *,
        user_id: str,
        profile_name: str,
        open_id: str,
        union_id: Optional[str] = None,
        provenance: str = "auto",
    ) -> None:
        """Insert or refresh a USER route. Bumps version, sets synced_at to now."""
        now = _now()
        self._conn.execute(
            """
            INSERT INTO multitenancy_routing
                (user_id, profile_name, open_id, union_id, active,
                 synced_at, version, created_at, updated_at, kind, provenance)
            VALUES (?, ?, ?, ?, 1, ?, 1, ?, ?, 'user', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                profile_name = excluded.profile_name,
                open_id      = excluded.open_id,
                union_id     = excluded.union_id,
                active       = 1,
                deleted_at   = NULL,
                synced_at    = excluded.synced_at,
                version      = version + 1,
                updated_at   = excluded.updated_at,
                kind         = 'user',
                provenance   = excluded.provenance
            """,
            (user_id, profile_name, open_id, union_id, now, now, now, provenance),
        )
        self._conn.commit()

    def upsert_group(
        self,
        *,
        chat_id: str,
        profile_name: str,
        owner_open_id: str,
        display_label: Optional[str] = None,
    ) -> str:
        """Insert or refresh a GROUP route keyed by chat_id.

        Returns the synthetic ``user_id`` (``group:<chat_id>``) used as the
        PRIMARY KEY for the row, so callers can soft-delete by it later.

        ``owner_open_id`` is immutable on a resurrected row — if a row with
        the same chat_id already exists, owner_open_id is preserved.
        ``display_label`` is mutable (group renames refresh it).
        """
        if not chat_id:
            raise ValueError("chat_id is required for group routing rows")
        if not owner_open_id:
            raise ValueError("owner_open_id is required for group routing rows")
        synthetic_user_id = f"group:{chat_id}"
        now = _now()
        # Atomic upsert mirroring the user `upsert` above. The two-step
        # SELECT-then-(INSERT|UPDATE) it replaced raced two concurrent
        # first-provisions into a lost-message DoS (one INSERT won the
        # chat_id unique index, the other raised IntegrityError that the
        # caller swallowed into a "no profile" reply). owner_open_id stays
        # immutable: ON CONFLICT keeps the stored owner whenever it is
        # already a non-empty value, only adopting the proposed owner when
        # the row was never owned. display_label keeps its old value when
        # the caller passes None (excluded.display_label IS NULL).
        self._conn.execute(
            """
            INSERT INTO multitenancy_routing
                (user_id, profile_name, open_id, union_id, active,
                 synced_at, version, created_at, updated_at,
                 kind, chat_id, owner_open_id, display_label)
            VALUES (?, ?, '', NULL, 1, ?, 1, ?, ?,
                    'group', ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                profile_name  = excluded.profile_name,
                chat_id       = excluded.chat_id,
                owner_open_id = COALESCE(
                    NULLIF(owner_open_id, ''), excluded.owner_open_id
                ),
                display_label = COALESCE(
                    excluded.display_label, display_label
                ),
                active        = 1,
                deleted_at    = NULL,
                synced_at     = excluded.synced_at,
                version       = version + 1,
                updated_at    = excluded.updated_at,
                kind          = 'group',
                open_id       = '',
                union_id      = NULL
            """,
            (
                synthetic_user_id,
                profile_name,
                now,
                now,
                now,
                chat_id,
                owner_open_id,
                display_label,
            ),
        )
        self._conn.commit()
        return synthetic_user_id

    def update_display_label(self, chat_id: str, display_label: str) -> bool:
        """Refresh a group row's display_label (e.g. after group rename).

        Does NOT bump version — display label is a presentation-only field.
        Returns True if a row was updated.
        """
        cur = self._conn.execute(
            "UPDATE multitenancy_routing SET display_label = ?, updated_at = ? "
            "WHERE chat_id = ? AND active = 1 AND kind = 'group'",
            (display_label, _now(), chat_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def soft_delete(self, user_id: str) -> bool:
        """Mark a route as inactive (kind-agnostic). Returns True on update."""
        now = _now()
        cur = self._conn.execute(
            """
            UPDATE multitenancy_routing
            SET active = 0, deleted_at = ?, updated_at = ?, version = version + 1
            WHERE user_id = ? AND active = 1
            """,
            (now, now, user_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def soft_delete_group(self, chat_id: str) -> bool:
        """Soft-delete a group row by chat_id. Returns True if a row was updated."""
        return self.soft_delete(f"group:{chat_id}")

    # -- diagnostics -------------------------------------------------------

    def count_active(self, *, kind: Optional[str] = None) -> int:
        if kind is None:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM multitenancy_routing WHERE active = 1"
            )
        else:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM multitenancy_routing "
                "WHERE active = 1 AND kind = ?",
                (kind,),
            )
        return int(cur.fetchone()[0])

    def close(self) -> None:
        self._conn.close()


def _row_to_dataclass(row: sqlite3.Row) -> RoutingRow:
    return RoutingRow(
        user_id=row["user_id"],
        profile_name=row["profile_name"],
        open_id=row["open_id"],
        union_id=row["union_id"],
        active=bool(row["active"]),
        last_active_at=row["last_active_at"],
        synced_at=row["synced_at"],
        version=row["version"],
        kind=(row["kind"] if "kind" in row.keys() else KIND_USER) or KIND_USER,
        chat_id=row["chat_id"] if "chat_id" in row.keys() else None,
        owner_open_id=row["owner_open_id"] if "owner_open_id" in row.keys() else None,
        display_label=row["display_label"] if "display_label" in row.keys() else None,
    )


def _now() -> int:
    return int(time.time())
