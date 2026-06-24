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

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default db path — independent from ~/.hermes/state.db so router writes don't
# contend with gateway sessions/pairing/cron writes for the WAL.
DEFAULT_DB_PATH = Path.home() / ".hermes" / "multitenancy.db"

KIND_USER = "user"
KIND_GROUP = "group"
KIND_AGENT = "agent"
_DEFAULT_REPLY_MODE = "mention"
_VALID_REPLY_MODES = frozenset({"mention", "all"})
AGENT_SHARE_VIEWER = "viewer"
AGENT_SHARE_EDITOR = "editor"
AGENT_SHARE_MANAGER = "manager"
_VALID_AGENT_SHARE_ROLES = frozenset(
    {AGENT_SHARE_VIEWER, AGENT_SHARE_EDITOR, AGENT_SHARE_MANAGER}
)
AGENT_SHARE_STATUS_ACTIVE = "active"
AGENT_SHARE_STATUS_REVOKED = "revoked"

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

-- Pending inviter capture closes the restart / multi-worker hazard between
-- Feishu bot.added and the first @mention. It lives on the same SQLite
-- connection as the main routing table so WAL, busy_timeout, and :memory:
-- tests all observe one shared durable hand-off surface.
CREATE TABLE IF NOT EXISTS multitenancy_pending_group_inviter (
    chat_id          TEXT PRIMARY KEY NOT NULL,
    inviter_open_id  TEXT NOT NULL,
    inviter_union_id TEXT,
    created_at       INTEGER NOT NULL
);

-- Per-group reply-mode valve, keyed by chat_id and DECOUPLED from the
-- routing row. The owner can flip the mode from the welcome card the instant
-- the bot is added — long before any group message triggers
-- _provision_group_route() and creates the kind='group' routing row. Storing
-- the mode on its own table means get/set work regardless of provisioning
-- order (the bug a row-column design hit: set was a silent no-op pre-provision).
CREATE TABLE IF NOT EXISTS multitenancy_group_reply_mode (
    chat_id     TEXT PRIMARY KEY NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'mention',
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS multitenancy_agent_shares (
    agent_id           TEXT NOT NULL,
    grantee_open_id    TEXT NOT NULL,
    role               TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active',
    created_by_open_id TEXT NOT NULL,
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL,
    revoked_at         INTEGER,
    PRIMARY KEY (agent_id, grantee_open_id)
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

_PENDING_INVITER_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("inviter_union_id", "TEXT"),
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
    (
        "idx_agent_shares_grantee_active",
        "CREATE INDEX IF NOT EXISTS idx_agent_shares_grantee_active "
        "ON multitenancy_agent_shares(grantee_open_id, agent_id) "
        "WHERE status = 'active'",
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
    # Multi-user/multi-agent columns (US-03/US-04). Defaulted + Optional so
    # callers and column-list SELECTs that predate them keep working; SELECT *
    # lookups (e.g. lookup_agent) populate them from the row.
    agent_id: Optional[str] = None
    provenance: Optional[str] = None
    upstream_profile: Optional[str] = None


@dataclass(frozen=True)
class TenantContext:
    """Strongly-typed, resolved tenant identity for a single dispatch (P0-2).

    Built only from an ACTIVE routing row (deleted/inactive → no context →
    caller denies). ``user_key`` is the stable session/memory key (open_id
    first; the alias is never the primary key). ``route_version`` lets session
    scopes invalidate when the routing row changes.
    """

    profile_name: str
    open_id: Optional[str]
    union_id: Optional[str]
    user_key: str
    kind: str
    chat_id: Optional[str]
    owner_open_id: Optional[str]
    route_version: int

    @property
    def is_group(self) -> bool:
        return self.kind == KIND_GROUP


@dataclass(frozen=True)
class AgentShareRow:
    agent_id: str
    grantee_open_id: str
    role: str
    status: str
    created_by_open_id: str
    created_at: int
    updated_at: int
    revoked_at: Optional[int] = None


@dataclass(frozen=True)
class SharedAgentRow:
    route: RoutingRow
    share: AgentShareRow


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

        cur = self._conn.execute(
            "PRAGMA table_info(multitenancy_pending_group_inviter)"
        )
        existing_pending_cols = {row["name"] for row in cur.fetchall()}
        for col_name, col_def in _PENDING_INVITER_NEW_COLUMNS:
            if col_name not in existing_pending_cols:
                self._conn.execute(
                    "ALTER TABLE multitenancy_pending_group_inviter "
                    f"ADD COLUMN {col_name} {col_def}"
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
            self._conn.execute("BEGIN")
            try:
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
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

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

    def resolve_context(
        self,
        sender: str,
        *,
        alt_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> Optional[TenantContext]:
        """Resolve a dispatch's sender into a typed, ACTIVE TenantContext, or
        None (caller must deny / auto-provision). Single fail-closed entry: a
        deleted/inactive/unknown sender yields None because every lookup filters
        ``active = 1``. The session ``user_key`` is stable (open_id first; the
        alias is only a last-resort fallback, never the primary key).
        """
        sender_key = str(sender or "").strip()
        row: Optional[RoutingRow] = None
        # Group dispatch: bind by chat_id (stable owner/profile per chat).
        if chat_id:
            row = self.lookup_by_chat_id(str(chat_id))
        if row is None and sender_key and sender_key != "unknown":
            row = self.lookup_by_open_id(sender_key) or self.lookup_by_union_id(sender_key)
        alt_key = str(alt_id or "").strip()
        if row is None and alt_key:  # legacy alias is a LOOKUP helper only
            row = self.lookup_by_user_id(alt_key)
        if row is None or not row.active:
            return None
        # Stable user key: open_id first, never the alias as primary.
        user_key = (
            (sender_key if sender_key and sender_key != "unknown" else "")
            or str(row.open_id or "")
            or str(row.user_id or "")
            or (alt_key or "unknown")
        )
        return TenantContext(
            profile_name=row.profile_name,
            open_id=row.open_id,
            union_id=row.union_id,
            user_key=user_key,
            kind=row.kind,
            chat_id=row.chat_id or (str(chat_id) if chat_id else None),
            owner_open_id=row.owner_open_id,
            route_version=int(row.version),
        )

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

    def lookup_by_profile_name(self, profile_name: str) -> Optional[RoutingRow]:
        """Return the active row that owns ``profile_name``, if any."""
        cur = self._conn.execute(
            "SELECT * FROM multitenancy_routing "
            "WHERE profile_name = ? AND active = 1 "
            "ORDER BY (provenance = 'sync') DESC, updated_at DESC LIMIT 1",
            (profile_name,),
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

    def put_pending_inviter(
        self,
        chat_id: str,
        inviter_open_id: str,
        *,
        inviter_union_id: Optional[str] = None,
    ) -> None:
        """Upsert the latest pending inviter hand-off for a group chat.

        Empty ``chat_id`` / ``inviter_open_id`` are ignored so the Feishu
        ``bot.added`` hook can stay best-effort and never crash callers.
        """
        if not chat_id or not inviter_open_id:
            return
        now = _now()
        self._conn.execute(
            """
            INSERT INTO multitenancy_pending_group_inviter
                (chat_id, inviter_open_id, inviter_union_id, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                inviter_open_id = excluded.inviter_open_id,
                inviter_union_id = excluded.inviter_union_id,
                created_at = excluded.created_at
            """,
            (chat_id, inviter_open_id, inviter_union_id, now),
        )
        self._conn.commit()

    def get_pending_inviter(self, chat_id: str) -> Optional[str]:
        """Return the raw stored inviter_open_id for ``chat_id``, if any.

        This getter intentionally applies no TTL logic: callers must prune
        explicitly via ``prune_pending_inviters`` at their natural boundaries
        so expiry stays visible, testable, and deterministic.
        """
        cur = self._conn.execute(
            "SELECT inviter_open_id FROM multitenancy_pending_group_inviter "
            "WHERE chat_id = ? LIMIT 1",
            (chat_id,),
        )
        row = cur.fetchone()
        return str(row["inviter_open_id"]) if row is not None else None

    def get_pending_inviter_union_id(self, chat_id: str) -> Optional[str]:
        """Return the raw stored inviter_union_id for ``chat_id``, if any."""
        cur = self._conn.execute(
            "SELECT inviter_union_id FROM multitenancy_pending_group_inviter "
            "WHERE chat_id = ? LIMIT 1",
            (chat_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        value = row["inviter_union_id"]
        return str(value) if value else None

    def clear_pending_inviter(self, chat_id: str) -> None:
        """Delete a pending inviter hand-off row for ``chat_id`` if present."""
        self._conn.execute(
            "DELETE FROM multitenancy_pending_group_inviter WHERE chat_id = ?",
            (chat_id,),
        )
        self._conn.commit()

    def prune_pending_inviters(self, now: int, ttl_seconds: int) -> int:
        """Delete expired pending inviter rows and return how many were removed."""
        cur = self._conn.execute(
            "DELETE FROM multitenancy_pending_group_inviter "
            "WHERE created_at < ?",
            (now - ttl_seconds,),
        )
        self._conn.commit()
        return cur.rowcount

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
        upstream_profile: Optional[str] = None,
    ) -> str:
        """Insert or refresh a GROUP route keyed by chat_id.

        Returns the synthetic ``user_id`` (``group:<chat_id>``) used as the
        PRIMARY KEY for the row, so callers can soft-delete by it later.

        ``owner_open_id`` is immutable on a resurrected row — if a row with
        the same chat_id already exists, owner_open_id is preserved.
        ``display_label`` is mutable (group renames refresh it).
        ``upstream_profile`` records the owner's root profile when known; if
        omitted, it is derived from the active sync-root row for owner_open_id.
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
                 kind, chat_id, owner_open_id, display_label,
                 agent_id, provenance, upstream_profile)
            VALUES (?, ?, '', NULL, 1, ?, 1, ?, ?,
                    'group', ?, ?, ?, ?, 'group',
                    COALESCE(
                        ?,
                        (
                            SELECT profile_name
                            FROM multitenancy_routing
                            WHERE open_id = ?
                              AND active = 1
                              AND kind = 'user'
                              AND provenance = 'sync'
                            LIMIT 1
                        )
                    ))
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
                agent_id      = COALESCE(agent_id, excluded.agent_id),
                provenance    = 'group',
                upstream_profile = COALESCE(
                    upstream_profile, excluded.upstream_profile
                ),
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
                synthetic_user_id,
                upstream_profile,
                owner_open_id,
            ),
        )
        self._conn.commit()
        return synthetic_user_id

    def upsert_owned_agent(
        self,
        *,
        agent_id: str,
        profile_name: str,
        owner_open_id: str,
        display_label: Optional[str] = None,
        upstream_profile: Optional[str] = None,
    ) -> str:
        """Insert or refresh a WebUI-owned agent route keyed by agent_id."""
        if not agent_id:
            raise ValueError("agent_id is required for agent routing rows")
        if not owner_open_id:
            raise ValueError("owner_open_id is required for agent routing rows")
        existing = self.lookup_agent(agent_id)
        if existing is not None and existing.owner_open_id != owner_open_id:
            raise ValueError("agent_id does not belong to asserted owner")

        now = _now()
        self._conn.execute(
            """
            INSERT INTO multitenancy_routing
                (user_id, profile_name, open_id, union_id, active,
                 synced_at, version, created_at, updated_at,
                 kind, chat_id, owner_open_id, display_label,
                 agent_id, provenance, upstream_profile)
            VALUES (?, ?, ?, NULL, 1, ?, 1, ?, ?,
                    'agent', NULL, ?, ?, ?, 'webui-agent',
                    COALESCE(
                        ?,
                        (
                            SELECT profile_name
                            FROM multitenancy_routing
                            WHERE open_id = ?
                              AND active = 1
                              AND kind = 'user'
                              AND provenance = 'sync'
                            LIMIT 1
                        )
                    ))
            ON CONFLICT(user_id) DO UPDATE SET
                profile_name  = excluded.profile_name,
                open_id       = excluded.open_id,
                union_id      = NULL,
                active        = 1,
                deleted_at    = NULL,
                synced_at     = excluded.synced_at,
                version       = version + 1,
                updated_at    = excluded.updated_at,
                kind          = 'agent',
                chat_id       = NULL,
                owner_open_id = COALESCE(
                    NULLIF(owner_open_id, ''), excluded.owner_open_id
                ),
                display_label = COALESCE(
                    excluded.display_label, display_label
                ),
                agent_id      = excluded.agent_id,
                provenance    = 'webui-agent',
                upstream_profile = COALESCE(
                    excluded.upstream_profile, upstream_profile
                )
            """,
            (
                agent_id,
                profile_name,
                agent_id,
                now,
                now,
                now,
                owner_open_id,
                display_label,
                agent_id,
                upstream_profile,
                owner_open_id,
            ),
        )
        self._conn.commit()
        return agent_id

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

    def get_group_reply_mode(self, chat_id: str) -> str:
        """Return the per-group reply mode ('mention' | 'all'), default 'mention'.

        Reads the dedicated multitenancy_group_reply_mode table — independent
        of whether the group's routing row has been provisioned yet.
        """
        if not chat_id:
            return _DEFAULT_REPLY_MODE
        cur = self._conn.execute(
            "SELECT mode FROM multitenancy_group_reply_mode WHERE chat_id = ? LIMIT 1",
            (chat_id,),
        )
        row = cur.fetchone()
        mode = str(row["mode"] or "").strip() if row else ""
        if mode in _VALID_REPLY_MODES:
            return mode
        return _DEFAULT_REPLY_MODE

    def set_group_reply_mode(self, chat_id: str, mode: str) -> None:
        """Upsert the per-group reply mode. Works before the group routing row
        exists (owner can flip it from the welcome card right after bot-added).
        """
        if not chat_id:
            raise ValueError("chat_id required")
        if mode not in _VALID_REPLY_MODES:
            raise ValueError(f"invalid reply mode: {mode}")
        self._conn.execute(
            "INSERT INTO multitenancy_group_reply_mode (chat_id, mode, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET mode = excluded.mode, "
            "updated_at = excluded.updated_at",
            (chat_id, mode, _now()),
        )
        self._conn.commit()

    # -- agent sharing ----------------------------------------------------

    def grant_agent_share(
        self,
        *,
        agent_id: str,
        grantee_open_id: str,
        role: str,
        created_by_open_id: str,
    ) -> AgentShareRow:
        """Grant or update non-owner access to a WebUI-owned agent."""
        agent_id = (agent_id or "").strip()
        grantee_open_id = (grantee_open_id or "").strip()
        role = (role or "").strip()
        created_by_open_id = (created_by_open_id or "").strip()
        if not agent_id:
            raise ValueError("agent_id is required")
        if not grantee_open_id:
            raise ValueError("grantee_open_id is required")
        if role not in _VALID_AGENT_SHARE_ROLES:
            raise ValueError(f"invalid agent share role: {role}")
        if not created_by_open_id:
            raise ValueError("created_by_open_id is required")
        agent = self.lookup_agent(agent_id)
        if agent is None or not agent.active:
            raise ValueError("agent_id is not an active agent")
        if grantee_open_id == agent.owner_open_id:
            raise ValueError("owner is already an implicit manager")

        now = _now()
        self._conn.execute(
            """
            INSERT INTO multitenancy_agent_shares
                (agent_id, grantee_open_id, role, status,
                 created_by_open_id, created_at, updated_at, revoked_at)
            VALUES (?, ?, ?, 'active', ?, ?, ?, NULL)
            ON CONFLICT(agent_id, grantee_open_id) DO UPDATE SET
                role = excluded.role,
                status = 'active',
                updated_at = excluded.updated_at,
                revoked_at = NULL
            """,
            (
                agent_id,
                grantee_open_id,
                role,
                created_by_open_id,
                now,
                now,
            ),
        )
        self._conn.commit()
        share = self.lookup_agent_share(agent_id, grantee_open_id)
        if share is None:
            raise RuntimeError("agent share grant did not persist")
        return share

    def lookup_agent_share(
        self, agent_id: str, grantee_open_id: str
    ) -> Optional[AgentShareRow]:
        agent_id = (agent_id or "").strip()
        grantee_open_id = (grantee_open_id or "").strip()
        if not agent_id or not grantee_open_id:
            return None
        cur = self._conn.execute(
            "SELECT * FROM multitenancy_agent_shares "
            "WHERE agent_id = ? AND grantee_open_id = ? LIMIT 1",
            (agent_id, grantee_open_id),
        )
        row = cur.fetchone()
        return _agent_share_row_to_dataclass(row) if row else None

    def get_agent_share_role(
        self, agent_id: str, grantee_open_id: str
    ) -> Optional[str]:
        share = self.lookup_agent_share(agent_id, grantee_open_id)
        if share is None or share.status != AGENT_SHARE_STATUS_ACTIVE:
            return None
        return share.role

    def list_agent_shares(
        self, agent_id: str, *, active_only: bool = True
    ) -> list[AgentShareRow]:
        agent_id = (agent_id or "").strip()
        if not agent_id:
            return []
        sql = "SELECT * FROM multitenancy_agent_shares WHERE agent_id = ?"
        params: list[str] = [agent_id]
        if active_only:
            sql += " AND status = ?"
            params.append(AGENT_SHARE_STATUS_ACTIVE)
        sql += " ORDER BY created_at ASC, grantee_open_id ASC"
        cur = self._conn.execute(sql, params)
        return [_agent_share_row_to_dataclass(row) for row in cur.fetchall()]

    def list_shared_agents_for_actor(self, open_id: str) -> list[SharedAgentRow]:
        open_id = (open_id or "").strip()
        if not open_id:
            return []
        cur = self._conn.execute(
            """
            SELECT
                r.user_id AS r_user_id,
                r.profile_name AS r_profile_name,
                r.open_id AS r_open_id,
                r.union_id AS r_union_id,
                r.active AS r_active,
                r.deleted_at AS r_deleted_at,
                r.synced_at AS r_synced_at,
                r.version AS r_version,
                r.last_active_at AS r_last_active_at,
                r.created_at AS r_created_at,
                r.updated_at AS r_updated_at,
                r.kind AS r_kind,
                r.chat_id AS r_chat_id,
                r.owner_open_id AS r_owner_open_id,
                r.display_label AS r_display_label,
                r.agent_id AS r_agent_id,
                r.upstream_profile AS r_upstream_profile,
                r.provenance AS r_provenance,
                s.agent_id AS s_agent_id,
                s.grantee_open_id AS s_grantee_open_id,
                s.role AS s_role,
                s.status AS s_status,
                s.created_by_open_id AS s_created_by_open_id,
                s.created_at AS s_created_at,
                s.updated_at AS s_updated_at,
                s.revoked_at AS s_revoked_at
            FROM multitenancy_agent_shares s
            JOIN multitenancy_routing r
              ON r.agent_id = s.agent_id
             AND r.active = 1
             AND r.kind = 'agent'
            WHERE s.grantee_open_id = ?
              AND s.status = 'active'
            ORDER BY r.display_label ASC, r.agent_id ASC
            """,
            (open_id,),
        )
        rows: list[SharedAgentRow] = []
        for row in cur.fetchall():
            route = RoutingRow(
                user_id=row["r_user_id"],
                profile_name=row["r_profile_name"],
                open_id=row["r_open_id"],
                union_id=row["r_union_id"],
                active=bool(row["r_active"]),
                last_active_at=row["r_last_active_at"],
                synced_at=row["r_synced_at"],
                version=row["r_version"],
                kind=row["r_kind"] or KIND_AGENT,
                chat_id=row["r_chat_id"],
                owner_open_id=row["r_owner_open_id"],
                display_label=row["r_display_label"],
                agent_id=row["r_agent_id"],
                provenance=row["r_provenance"],
                upstream_profile=row["r_upstream_profile"],
            )
            share = AgentShareRow(
                agent_id=row["s_agent_id"],
                grantee_open_id=row["s_grantee_open_id"],
                role=row["s_role"],
                status=row["s_status"],
                created_by_open_id=row["s_created_by_open_id"],
                created_at=row["s_created_at"],
                updated_at=row["s_updated_at"],
                revoked_at=row["s_revoked_at"],
            )
            rows.append(SharedAgentRow(route=route, share=share))
        return rows

    def revoke_agent_share(self, agent_id: str, grantee_open_id: str) -> bool:
        agent_id = (agent_id or "").strip()
        grantee_open_id = (grantee_open_id or "").strip()
        if not agent_id or not grantee_open_id:
            return False
        now = _now()
        cur = self._conn.execute(
            """
            UPDATE multitenancy_agent_shares
            SET status = 'revoked', revoked_at = ?, updated_at = ?
            WHERE agent_id = ? AND grantee_open_id = ? AND status = 'active'
            """,
            (now, now, agent_id, grantee_open_id),
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

    def count_active(
        self, *, kind: Optional[str] = None, provenance: Optional[str] = None
    ) -> int:
        clauses = ["active = 1"]
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if provenance is not None:
            clauses.append("provenance = ?")
            params.append(provenance)
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM multitenancy_routing WHERE " + " AND ".join(clauses),
            tuple(params),
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
        agent_id=row["agent_id"] if "agent_id" in row.keys() else None,
        provenance=row["provenance"] if "provenance" in row.keys() else None,
        upstream_profile=row["upstream_profile"] if "upstream_profile" in row.keys() else None,
    )


def _agent_share_row_to_dataclass(row: sqlite3.Row) -> AgentShareRow:
    return AgentShareRow(
        agent_id=row["agent_id"],
        grantee_open_id=row["grantee_open_id"],
        role=row["role"],
        status=row["status"],
        created_by_open_id=row["created_by_open_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        revoked_at=row["revoked_at"],
    )


def _now() -> int:
    return int(time.time())
