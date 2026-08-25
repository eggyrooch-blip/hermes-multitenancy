"""Credential delegation leases (connector-GitLab first lane).

A NON-personal profile (``feishu_group_*``) that lacks a connector credential
may borrow the INITIATOR's own personal credential — after the initiator
explicitly approves a delegation card in their DM. The grant is recorded as a
*lease* row in ``multitenancy.db``; the secret itself is injected only into the
run-level subprocess env (``GITLAB_TOKEN``) and never touches the shared
profile's config/, vault, or workspace/credentials/.

``resource_type`` is modelled as {connector, skill, expert} so later slugs can
reuse the same table/card seam, but ONLY connector-GitLab is implemented here —
deliberately no skill/expert code paths.

Recovery/crash story: leases carry a TTL and are judged AT TAKE TIME — a lease
that outlived its window is flipped to ``expired`` on the next lookup, and an
``once`` lease is flipped to ``consumed`` the moment it is injected into a run.
The env injection itself dies with the run's subprocess, so there is nothing to
clean up when a run crashes.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PROVIDER_GITLAB = "gitlab"
RESOURCE_CONNECTOR = "connector"  # schema also admits skill|expert (later slugs)
SCOPE_ONCE = "once"
SCOPE_CHAT = "chat"

# 允许一次: enough for card click + synthetic replay; dead afterwards even if
# the replay never ran (gateway restart). 允许本群: revocable standing grant
# with a hard ceiling so a forgotten grant does not live forever.
ONCE_TTL_SECONDS = 15 * 60
CHAT_TTL_SECONDS = 30 * 24 * 3600

# Mirrors router/provisioning `_GROUP_PROFILE_PREFIX`. Duplicated as a literal
# so the child-process side (credential_tool) never imports the router package.
GROUP_PROFILE_PREFIX = "feishu_group_"

_MARKER_STEM = "cred-delegation-auth-required"
_MARKER_NAME = f"{_MARKER_STEM}.json"

# Parent→child: the run nonce that names this run's auth-required marker.
DELEGATION_NONCE_ENV = "HERMES_MULTITENANCY_DELEGATION_NONCE"
# Parent→child: the delegation grant this run is the replay of (once-lease bind).
DELEGATION_ID_ENV = "HERMES_MULTITENANCY_DELEGATION_ID"

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS multitenancy_credential_leases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_profile     TEXT NOT NULL,
        owner_open_id     TEXT NOT NULL,
        borrower_profile  TEXT NOT NULL,
        resource_type     TEXT NOT NULL DEFAULT 'connector',
        provider          TEXT NOT NULL,
        scope             TEXT NOT NULL,
        chat_id           TEXT NOT NULL DEFAULT '',
        delegation_id     TEXT NOT NULL DEFAULT '',
        status            TEXT NOT NULL DEFAULT 'active',
        created_at        INTEGER NOT NULL,
        expires_at        INTEGER,
        consumed_at       INTEGER,
        last_used_at      INTEGER,
        use_count         INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_cred_leases_lookup
    ON multitenancy_credential_leases(borrower_profile, owner_open_id, provider, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS multitenancy_credential_lease_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lease_id          INTEGER,
        action            TEXT NOT NULL,
        owner_profile     TEXT NOT NULL DEFAULT '',
        owner_open_id     TEXT NOT NULL DEFAULT '',
        borrower_profile  TEXT NOT NULL DEFAULT '',
        provider          TEXT NOT NULL DEFAULT '',
        chat_id           TEXT NOT NULL DEFAULT '',
        detail            TEXT NOT NULL DEFAULT '',
        created_at        INTEGER NOT NULL
    )
    """,
)


def is_delegatable_profile(profile_name: Optional[str]) -> bool:
    return bool(profile_name) and str(profile_name).startswith(GROUP_PROFILE_PREFIX)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    for stmt in _DDL:
        conn.execute(stmt)
    try:
        # Tables created before the run-binding fix predate this column.
        conn.execute(
            "ALTER TABLE multitenancy_credential_leases "
            "ADD COLUMN delegation_id TEXT NOT NULL DEFAULT ''"
        )
    except sqlite3.OperationalError:
        pass  # already present
    return conn


def _audit(
    conn: sqlite3.Connection,
    *,
    action: str,
    lease_id: Optional[int] = None,
    owner_profile: str = "",
    owner_open_id: str = "",
    borrower_profile: str = "",
    provider: str = "",
    chat_id: str = "",
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO multitenancy_credential_lease_audit "
        "(lease_id, action, owner_profile, owner_open_id, borrower_profile,"
        " provider, chat_id, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            lease_id,
            action,
            owner_profile,
            owner_open_id,
            borrower_profile,
            provider,
            chat_id,
            detail,
            _now_ms(),
        ),
    )


def create_lease(
    db_path: Path,
    *,
    owner_profile: str,
    owner_open_id: str,
    borrower_profile: str,
    provider: str = PROVIDER_GITLAB,
    scope: str = SCOPE_ONCE,
    chat_id: str = "",
    resource_type: str = RESOURCE_CONNECTOR,
    delegation_id: str = "",
) -> int:
    """Record one approved delegation. Returns the lease id.

    A ``once`` lease MUST carry the ``delegation_id`` of the request it answers:
    "允许一次" means *this* run, not "the next run of this person in this group
    that happens to reach the injection point first".
    """
    if scope not in {SCOPE_ONCE, SCOPE_CHAT}:
        raise ValueError(f"unsupported lease scope {scope!r}")
    delegation_id = str(delegation_id or "")
    if scope == SCOPE_ONCE and not delegation_id:
        raise ValueError("a once-scope lease requires the originating delegation_id")
    now = _now_ms()
    ttl = ONCE_TTL_SECONDS if scope == SCOPE_ONCE else CHAT_TTL_SECONDS
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO multitenancy_credential_leases "
            "(owner_profile, owner_open_id, borrower_profile, resource_type,"
            " provider, scope, chat_id, delegation_id, status, created_at,"
            " expires_at, use_count)"
            " VALUES (?,?,?,?,?,?,?,?, 'active', ?, ?, 0)",
            (
                owner_profile,
                owner_open_id,
                borrower_profile,
                resource_type,
                provider,
                scope,
                chat_id,
                delegation_id,
                now,
                now + ttl * 1000,
            ),
        )
        lease_id = int(cursor.lastrowid)
        _audit(
            conn,
            action="granted",
            lease_id=lease_id,
            owner_profile=owner_profile,
            owner_open_id=owner_open_id,
            borrower_profile=borrower_profile,
            provider=provider,
            chat_id=chat_id,
            detail=f"scope={scope}",
        )
    return lease_id


def find_active_lease(
    db_path: Path,
    *,
    owner_open_id: str,
    borrower_profile: str,
    provider: str = PROVIDER_GITLAB,
    delegation_id: Optional[str] = None,
    scope: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Return the freshest ELIGIBLE lease, flipping stale ones to ``expired``.

    Expiry is judged HERE (take time), never trusted from the writer — a lease
    that outlived its TTL is dead even if no cleaner ever ran.

    Eligibility filters walk the rest of the list instead of stopping: a fresh
    ``once`` grant belonging to a different run must not shadow the standing
    ``chat`` grant behind it.

      * ``delegation_id`` (pass ``None`` to disable) — a ``once`` lease is
        eligible only for the run it was granted to; ``chat`` leases are
        run-agnostic and always pass.
      * ``scope`` — restrict to one scope (the standing-grant fast path wants
        ``chat`` only; a live ``once`` lease is a pending replay, not a
        standing grant).
    """
    if not db_path.exists() or not owner_open_id or not borrower_profile:
        return None
    now = _now_ms()
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM multitenancy_credential_leases "
            "WHERE borrower_profile = ? AND owner_open_id = ? AND provider = ? "
            "AND status = 'active' ORDER BY created_at DESC",
            (borrower_profile, owner_open_id, provider),
        ).fetchall()
        for row in rows:
            expires_at = row["expires_at"]
            if expires_at is not None and int(expires_at) <= now:
                # `AND status='active'`: this row is a snapshot — a revoker may
                # have committed between the SELECT and here, and an
                # unconditional UPDATE would overwrite `revoked` with `expired`.
                cursor = conn.execute(
                    "UPDATE multitenancy_credential_leases SET status='expired' "
                    "WHERE id=? AND status='active'",
                    (row["id"],),
                )
                if cursor.rowcount:
                    _audit(
                        conn,
                        action="expired",
                        lease_id=int(row["id"]),
                        owner_profile=row["owner_profile"],
                        owner_open_id=row["owner_open_id"],
                        borrower_profile=row["borrower_profile"],
                        provider=row["provider"],
                        chat_id=row["chat_id"],
                    )
                continue
            row_scope = str(row["scope"] or "")
            if scope is not None and row_scope != scope:
                continue
            if delegation_id is not None and row_scope == SCOPE_ONCE:
                want = str(delegation_id or "")
                have = str(row["delegation_id"] or "")
                # Both sides must be NON-EMPTY and equal. An empty id on either
                # side is "no run identity", not a match: an ordinary run (no
                # DELEGATION_ID_ENV) must never consume a once grant.
                if not want or not have or want != have:
                    continue  # another run's grant — look past it, don't stop
            return dict(row)
    return None


def record_lease_use(db_path: Path, lease: dict[str, Any], *, detail: str = "") -> bool:
    """Atomically CLAIM the lease for one borrow. Returns False if it lost.

    The row read by :func:`find_active_lease` is a snapshot: between the read
    and here the lease may have been consumed by a racing run, revoked by the
    owner, or aged past its TTL. The claim is therefore a conditional UPDATE —
    only ``rowcount == 1`` means "this run owns the borrow", and only then is a
    ``used`` audit row written. Losing the race leaves the row exactly as the
    winner (or the revoker) left it.
    """
    now = _now_ms()
    with _connect(db_path) as conn:
        if str(lease.get("scope")) == SCOPE_ONCE:
            cursor = conn.execute(
                "UPDATE multitenancy_credential_leases "
                "SET status='consumed', consumed_at=?, last_used_at=?, use_count=use_count+1 "
                "WHERE id=? AND status='active' "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (now, now, lease["id"], now),
            )
        else:
            cursor = conn.execute(
                "UPDATE multitenancy_credential_leases "
                "SET last_used_at=?, use_count=use_count+1 "
                "WHERE id=? AND status='active' "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (now, lease["id"], now),
            )
        if not cursor.rowcount:
            return False
        _audit(
            conn,
            action="used",
            lease_id=int(lease["id"]),
            owner_profile=str(lease.get("owner_profile") or ""),
            owner_open_id=str(lease.get("owner_open_id") or ""),
            borrower_profile=str(lease.get("borrower_profile") or ""),
            provider=str(lease.get("provider") or ""),
            chat_id=str(lease.get("chat_id") or ""),
            detail=detail,
        )
    return True


def record_denial(
    db_path: Path,
    *,
    owner_open_id: str,
    borrower_profile: str,
    provider: str = PROVIDER_GITLAB,
    chat_id: str = "",
    detail: str = "",
) -> None:
    with _connect(db_path) as conn:
        _audit(
            conn,
            action="denied",
            owner_open_id=owner_open_id,
            borrower_profile=borrower_profile,
            provider=provider,
            chat_id=chat_id,
            detail=detail,
        )


def revoke_chat_leases(
    db_path: Path,
    *,
    owner_open_id: str,
    borrower_profile: str,
    provider: str = PROVIDER_GITLAB,
) -> int:
    """Flip this owner's standing (chat-scope) leases for one group to revoked."""
    if not db_path.exists():
        return 0
    now = _now_ms()
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE multitenancy_credential_leases SET status='revoked', consumed_at=? "
            "WHERE borrower_profile=? AND owner_open_id=? AND provider=? AND status='active'",
            (now, borrower_profile, owner_open_id, provider),
        )
        if cursor.rowcount:
            _audit(
                conn,
                action="revoked",
                owner_open_id=owner_open_id,
                borrower_profile=borrower_profile,
                provider=provider,
            )
        return int(cursor.rowcount)


# --- owner-side resolution ------------------------------------------------------

def owner_profile_for_open_id(db_path: Path, open_id: str) -> Optional[str]:
    """The initiator's PERSONAL profile via authoritative routing (kind=user)."""
    if not db_path.exists() or not str(open_id or "").startswith("ou_"):
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            row = conn.execute(
                "SELECT profile_name FROM multitenancy_routing "
                "WHERE open_id = ? AND active = 1 AND kind = 'user' "
                "ORDER BY updated_at DESC LIMIT 1",
                (str(open_id),),
            ).fetchone()
    except Exception as exc:
        logger.debug("[cred_delegation] owner profile lookup failed: %s", exc)
        return None
    return str(row[0]) if row and row[0] else None


def profile_is_solely_owned_by(db_path: Path, profile_name: str, open_id: str) -> bool:
    """True when ``profile_name``'s ONLY active user routing row is ``open_id``.

    ``owner_profile_for_open_id`` answers "which profile is this person routed
    to", which is NOT the question a borrow must ask. There is no unique index
    on active ``profile_name`` (routing.py only constrains open_id / chat_id /
    agent_id), and the HR sync upserts the incoming user BEFORE soft-deleting
    the outgoing one, so two active rows can legitimately point at one profile
    for the duration of a sync. During that window the forward check still
    passes for the departing owner while the profile's vault already holds the
    arriving person's token — i.e. their credential is injected into someone
    else's run. Answering the reverse question closes it: fail closed unless
    the profile maps back to exactly this one person.
    """
    wanted = str(open_id or "")
    if not db_path.exists() or not str(profile_name or "") or not wanted:
        return False

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            # Selection happens in PYTHON, not SQL. Three rounds of leaks came
            # from trying to express the write path's normalisation in SQL and
            # never matching it: SQLite's TRIM() strips only U+0020 while the
            # credential writer and the vault use Python str.strip(), which
            # strips every Unicode space — so `profile_name='alice '` was
            # invisible here yet stored under `alice`. The only way these agree
            # is to run the SAME function on both sides, so fetch the active
            # rows and filter with str.strip() exactly like the writer does.
            rows = conn.execute(
                "SELECT open_id, kind, profile_name FROM multitenancy_routing "
                "WHERE active = 1"
            ).fetchall()
    except Exception as exc:
        logger.debug("[cred_delegation] profile ownership lookup failed: %s", exc)
        return False

    # `kind` is deliberately NOT filtered. The resolver that reaches the
    # credential write path — routing.lookup_by_user_id — matches on user_id and
    # active alone and never looks at kind, so a row of ANY kind sitting on this
    # profile can put its own token in this vault. Filtering kind here would hide
    # exactly those rows. (Prod today: no personal profile carries a non-user
    # active row, so this costs no false refusals.)
    target = str(profile_name).strip()
    rows = [row for row in rows if str(row[2] or "").strip() == target]
    # ONE load-bearing invariant, deliberately: the RAW open_ids on this profile
    # must be exactly {wanted}. Compared raw and never normalised — normalising
    # is what broke the previous attempt (strip() folded ' ou_alice ' into
    # 'ou_alice', letting a padded impostor pass as the owner). NULL, blank,
    # padded, or anybody else all make the set differ, so each is refused by the
    # same comparison. Per-row guards were tried and removed: no mutation could
    # kill them, i.e. they carried no protection while looking like they did.
    return {str(row[0]) for row in rows} == {wanted}


def owner_gitlab_env(shared_home: Path, owner_profile: str) -> dict[str, str]:
    """The owner's PERSONAL GitLab token as env, or {} when they have none.

    Goes through ``resolve_runtime_secret`` (the single precedence seam) and
    then REQUIRES the payload to have come from the owner's own vault record:
    the shared fallback token must never be laundered into a non-allowlisted
    group through delegation.
    """
    try:
        from .credential_materializer import (
            _payload_content,
            resolve_runtime_secret,
        )
        from .credentials import CredentialStore
        from .gitlab_token_intake import gitlab_config_entry

        entry = gitlab_config_entry(shared_home)
        if not entry:
            return {}
        store = CredentialStore(shared_home / "multitenancy.db")
        try:
            payload, vault_profile = resolve_runtime_secret(
                store, entry, profile_name=owner_profile
            )
        finally:
            store.close()
        if payload is None or vault_profile != owner_profile:
            return {}
        env_name = str(entry.get("env") or entry.get("env_name") or "GITLAB_TOKEN").strip()
        token = _payload_content(payload, entry.get("payload_key")).rstrip("\n")
        if not env_name or not token:
            return {}
        env = {env_name: token}
        extra = entry.get("env_extra")
        if isinstance(extra, dict):
            for key, value in extra.items():
                key = str(key).strip()
                if key:
                    env.setdefault(key, str(value))
        env.setdefault("GITLAB_HOST", "gitlab.example.com")
        return env
    except Exception:
        logger.debug("[cred_delegation] owner gitlab env resolution failed", exc_info=True)
        return {}


def owner_has_personal_gitlab_token(shared_home: Path, owner_profile: str) -> bool:
    return bool(owner_gitlab_env(shared_home, owner_profile))


# --- run-level env injection ----------------------------------------------------

def revoke_lease(db_path: Path, lease_id: int, *, detail: str = "") -> None:
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE multitenancy_credential_leases SET status='revoked', consumed_at=? "
            "WHERE id=? AND status='active'",
            (_now_ms(), lease_id),
        )
        if cursor.rowcount:
            _audit(conn, action="revoked", lease_id=lease_id, detail=detail)


def _env_already_carries(existing_env: Any, env_name: str) -> bool:
    """True only when the profile env carries a NON-EMPTY value for ``env_name``.

    A group ``.env`` holding a bare ``GITLAB_TOKEN=`` used to count as "this
    group has its own token": delegation early-returned forever, the child kept
    reporting the credential as missing, and the once-lease was never consumed —
    an endless card loop. A mapping is judged by value; a bare name collection
    (older callers/tests) still counts as present.
    """
    if not env_name:
        return False
    if hasattr(existing_env, "get"):
        return bool(str(existing_env.get(env_name) or "").strip())
    return env_name in set(existing_env or ())


def _owner_route_matches(
    db_path: Path,
    *,
    sender_open_id: str,
    lease: dict[str, Any],
) -> bool:
    """FAIL-CLOSED owner check: routing must resolve AND agree with the lease.

    Three outcomes, none of which may fall through to a borrow:

      * resolves to the lease's owner_profile  -> True (borrow allowed);
      * resolves to a DIFFERENT profile        -> drift: the profile was handed
        to somebody else, so the lease is revoked and the borrow refused;
      * does not resolve at all (``None``)     -> routing row deleted/inactive,
        or the DB is momentarily unreadable. Refuse THIS borrow and audit it,
        but do NOT revoke: an unavailable router must not destroy a valid grant.

    The old code only refused on drift, so a ``None`` resolution kept reading
    the token of whoever now owns the profile recorded on the stale lease.
    """
    owner_profile = str(lease.get("owner_profile") or "")
    current = owner_profile_for_open_id(db_path, sender_open_id)
    if owner_profile and current == owner_profile:
        # Forward agreement is necessary but NOT sufficient: the profile must
        # also map back to this person alone. See profile_is_solely_owned_by.
        if profile_is_solely_owned_by(db_path, owner_profile, sender_open_id):
            return True
        with _connect(db_path) as conn:
            _audit(
                conn,
                action="denied",
                lease_id=int(lease.get("id") or 0) or None,
                owner_profile=owner_profile,
                owner_open_id=str(sender_open_id),
                borrower_profile=str(lease.get("borrower_profile") or ""),
                provider=str(lease.get("provider") or ""),
                chat_id=str(lease.get("chat_id") or ""),
                detail="owner profile shared/contested; borrow refused (lease kept)",
            )
        return False
    if current is None:
        with _connect(db_path) as conn:
            _audit(
                conn,
                action="denied",
                lease_id=int(lease.get("id") or 0) or None,
                owner_profile=owner_profile,
                owner_open_id=str(sender_open_id),
                borrower_profile=str(lease.get("borrower_profile") or ""),
                provider=str(lease.get("provider") or ""),
                chat_id=str(lease.get("chat_id") or ""),
                detail="owner profile unresolved; borrow refused (lease kept)",
            )
        return False
    revoke_lease(
        db_path,
        int(lease["id"]),
        detail=f"owner profile drift {owner_profile}->{current}",
    )
    return False


def gitlab_env_name(shared_home: Path) -> str:
    """The env var this deployment injects GitLab tokens as (config-driven)."""
    try:
        from .gitlab_token_intake import gitlab_config_entry

        entry = gitlab_config_entry(shared_home) or {}
        return str(entry.get("env") or entry.get("env_name") or "GITLAB_TOKEN").strip()
    except Exception:
        return "GITLAB_TOKEN"


def delegation_env_for_run(
    profile_home: Path,
    *,
    sender_open_id: str,
    delegation_id: str = "",
    existing_env_names: Any = (),  # env mapping (preferred) or bare name set
) -> dict[str, str]:
    """Env to inject into ONE run of a borrower profile, keyed to its sender.

    Empty dict unless ALL of:

      * the borrower is a group profile and the run carries an EXPLICIT sender
        (never an ambient identity — see ``subprocess_env``);
      * that group does not already carry its own GitLab token (an explicit
        allowlist group never touches this lane, so it can neither consume a
        grant nor emit a borrow audit row);
      * the sender holds an unexpired active lease for the group, and — for a
        ``once`` lease — this run IS the run the grant was issued for
        (``delegation_id`` match);
      * the sender's CURRENT personal profile still equals the one recorded on
        the lease (routing may have moved them and handed the old profile to
        somebody else; a drifted lease is revoked, never borrowed);
      * that profile's own vault holds a personal GitLab token.

    The borrow itself is an atomic claim, so a ``once`` grant yields exactly one
    run's env even under concurrency.
    """
    profile_home = Path(profile_home)
    borrower = profile_home.name
    sender = str(sender_open_id or "").strip()
    if not is_delegatable_profile(borrower) or not sender.startswith("ou_"):
        return {}
    shared_home = (
        profile_home.parent.parent
        if profile_home.parent.name == "profiles"
        else profile_home
    )
    db_path = shared_home / "multitenancy.db"
    try:
        # P2-1: bail BEFORE any lease lookup so an explicit-token group cannot
        # burn a once-lease (and audit a borrow) that it then discards.
        if _env_already_carries(existing_env_names, gitlab_env_name(shared_home)):
            return {}
        lease = find_active_lease(
            db_path,
            owner_open_id=sender,
            borrower_profile=borrower,
            delegation_id=str(delegation_id or ""),
        )
        if lease is None:
            return {}
        owner_profile = str(lease.get("owner_profile") or "")
        if not _owner_route_matches(db_path, sender_open_id=sender, lease=lease):
            return {}
        env = owner_gitlab_env(shared_home, owner_profile)
        if not env:
            return {}
        # Re-confirm AFTER the read: the routing check and the token read are two
        # separate snapshots, and a profile handover landing between them would
        # otherwise hand the new owner's token to the old owner's run.
        if not _owner_route_matches(db_path, sender_open_id=sender, lease=lease):
            return {}
        if not record_lease_use(db_path, lease, detail="run env injection"):
            return {}  # lost the claim race / revoked since the lookup
        return env
    except Exception:
        logger.debug("[cred_delegation] delegation env lookup failed", exc_info=True)
        return {}


# --- child→parent auth-required marker ------------------------------------------
# The AIAgent subprocess (possibly inside bwrap, where the parent's mkdtemp
# approval dir is NOT visible) signals "GitLab credential missing" through the
# profile's own tmp/ — the one directory both sides always share rw.

def marker_path(profile_home: Path, nonce: str = "") -> Path:
    """One marker file PER RUN.

    A single profile-wide filename was wrong: two concurrent runs of the same
    group share the profile, so run B would unlink and consume A's marker —
    B gets a delegation card for a request it never made, A's real request is
    dropped, and B's replay re-runs the wrong prompt. The parent only ever
    looks for the nonce it minted for its own spawn.
    """
    nonce = _safe_nonce(nonce)
    name = _MARKER_NAME if not nonce else f"{_MARKER_STEM}-{nonce}.json"
    return Path(profile_home) / "tmp" / name


def _safe_nonce(nonce: str) -> str:
    return "".join(
        ch for ch in str(nonce or "") if ch.isalnum() or ch in {"-", "_"}
    )[:64]


def write_auth_required_marker(
    profile_home: Path, payload: dict[str, Any], *, nonce: str = ""
) -> None:
    path = marker_path(profile_home, nonce)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = dict(payload)
        body.setdefault("provider", PROVIDER_GITLAB)
        body["ts"] = time.time()
        # tmp+rename: the parent polls this path, so it must never observe a
        # half-written file.
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        logger.debug("[cred_delegation] marker write failed", exc_info=True)


def take_auth_required_marker(
    profile_home: Path, *, since: float, nonce: str = ""
) -> Optional[dict[str, Any]]:
    """Read-and-unlink THIS run's marker; only markers after ``since`` count."""
    path = marker_path(profile_home, nonce)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if float(data.get("ts") or 0) < float(since):
        return None
    return data
