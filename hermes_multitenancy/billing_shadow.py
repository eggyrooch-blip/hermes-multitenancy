"""Operator-only billing shadow runner: planner-boundary admission replay.

Canary hard-gate tooling (see ``.ftask/billing-shadow-runner/SPEC.md`` and the
canary SPEC's "真实用户 shadow replay 合同").  Exports 100% of the active
sync-owned roots from the routing SQLite (read-only URI, pinned read
transaction), replays a planner-only admission decision for every root across
the five production shapes (DM / group / WebUI / cron / kanban) using the real
per-channel request builders, and prints aggregate counts plus a
machine-assertion block.

Planner boundary (hard interception, not log narration):

* the routing DB is opened ``mode=ro`` — SQLite itself rejects any write —
  and the whole round runs inside one explicit ``BEGIN`` read transaction so
  the universe/replay view is a single pinned snapshot (WAL);
* end-of-round drift detection uses a SECOND fresh read-only connection plus
  an org-snapshot re-digest — any change voids the round with a nonzero exit;
* the identity store handed to the preparer raises on ``put`` and counts the
  attempt;
* the credentials object raises ``_BoundaryStop`` inside ``ensure_available``
  before doing anything, and any other attribute access raises — no Gateway
  client, token, or URL exists anywhere in the preparer object graph;
* ``_boundary_traps`` additionally rebinds ``RunBroker.run`` and every
  ``BillingGatewayClient`` transport method to counting traps for the whole
  replay window, so the zeros in the assertion block are measurements taken
  on the real CLI path, not constants;
* replay content is a fixed constant — no message body is ever read.

Privacy: stdout carries aggregate counts and digests only.  Reason codes come
from a fixed allowlist; an admission rejection that cannot be mapped fails the
whole round rather than ever echoing exception text.  The per-case detail
report keeps nothing but an HMAC opaque case ID (fresh random salt per round,
held only in memory) and allowlisted reason codes, written to a ``0600`` file
the operator must destroy within 24h.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Optional

from .billing_credentials import BillingIdentity
from .billing_identity import BillingIdentityPreparer, _TRUE
from .billing_readiness import BillingReadinessError, cohort_hash
from .routing import DEFAULT_DB_PATH
from .run_broker import RunRejected
from .run_models import RunRequest


# Admission-replay statuses.  DRIFT_OR_CONFLICT deliberately reuses the exact
# spelling of billing_readiness._ALLOWED_STATUSES so downstream artifact
# tooling never sees two spellings of the same concept; the remaining four are
# admission-plane outcomes that the readiness (LiteLLM join) enum does not
# model.  tests/test_billing_shadow_runner.py pins this alignment.
STATUS_NONCOHORT_LEGACY = "NONCOHORT_LEGACY"
STATUS_COHORT_WOULD_ENFORCE = "COHORT_WOULD_ENFORCE"
STATUS_ENFORCED_EXISTING = "ENFORCED_EXISTING"
STATUS_IDENTITY_INVALID = "IDENTITY_INVALID"
STATUS_DRIFT_OR_CONFLICT = "DRIFT_OR_CONFLICT"

# Fail-closed precedence: worst shape outcome classifies the root, and
# identity-invalid can never be laundered into the "noncohort untouched" bucket.
ADMISSION_STATUSES = (
    STATUS_IDENTITY_INVALID,
    STATUS_DRIFT_OR_CONFLICT,
    STATUS_ENFORCED_EXISTING,
    STATUS_COHORT_WOULD_ENFORCE,
    STATUS_NONCOHORT_LEGACY,
)

SHAPES = ("dm", "group", "webui", "cron", "kanban")

_REPLAY_CONTENT = "[billing-shadow-replay]"  # constant; never a message body

# Reason allowlist: admission rejections map to (status, fixed reason code).
# Anything not on this list fails the WHOLE round — exception text is never
# echoed into stdout or the detail report (it can carry real identifiers).
_REASON_BY_REJECTION = {
    "employee billing identity could not be resolved": (
        STATUS_IDENTITY_INVALID,
        "identity_unresolved",
    ),
    "billing payer profile drift detected": (
        STATUS_DRIFT_OR_CONFLICT,
        "profile_drift",
    ),
    "billing payer email drift detected": (
        STATUS_DRIFT_OR_CONFLICT,
        "email_drift",
    ),
    "billing payer profile is ambiguous": (
        STATUS_DRIFT_OR_CONFLICT,
        "profile_binding_ambiguous",
    ),
    "employee email domain is invalid": (
        STATUS_DRIFT_OR_CONFLICT,
        "email_domain_invalid",
    ),
}

EXIT_CONFIG = 2
EXIT_DRIFT = 3
EXIT_INVARIANT = 4


class ShadowError(RuntimeError):
    exit_code = 1


class ShadowConfigError(ShadowError):
    exit_code = EXIT_CONFIG


class ShadowDriftError(ShadowError):
    exit_code = EXIT_DRIFT


class ShadowInvariantError(ShadowError):
    exit_code = EXIT_INVARIANT


class _BoundaryStop(Exception):
    """A live run would now cross into Gateway ensure — planner stops here."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


@dataclass
class _Counters:
    gateway_ensure_calls: int = 0
    billing_db_write_attempts: int = 0
    run_broker_dispatch_calls: int = 0
    planner_boundary_stops: int = 0


class _SentinelCredentials:
    """Hard interception layer at the planner boundary.

    ``ensure_available`` records the boundary crossing that a live run would
    make and raises before doing anything.  Every other attribute access is a
    contract violation and fails the whole round.
    """

    def __init__(self, counters: _Counters) -> None:
        object.__setattr__(self, "_counters", counters)

    def ensure_available(
        self,
        payer: Any,
        existing: Any,
        *,
        force_reason: str = "",
        allow_mint: bool = True,
    ) -> Any:
        # `allow_mint` is accepted and ignored on purpose: this sentinel raises
        # before doing anything, so it cannot mint either way. It has to match
        # the real signature or the request path's `allow_mint=False` would make
        # every shadow round die on a TypeError instead of recording a boundary.
        self._counters.planner_boundary_stops += 1
        if existing is not None and getattr(existing, "migration_state", "") == "enforced":
            raise _BoundaryStop(STATUS_ENFORCED_EXISTING)
        raise _BoundaryStop(STATUS_COHORT_WOULD_ENFORCE)

    def __getattr__(self, name: str) -> Any:
        raise ShadowError(f"planner_boundary_violation:credentials.{name}")


class _ReadOnlyIdentityStore:
    """BillingIdentityStore read semantics over the shared mode=ro connection."""

    _SELECT = (
        "SELECT employee_user_id, profile_name, email, litellm_user_id, "
        "team_id, team_alias, key_id, credential_version, expires_at, "
        "migration_state FROM multitenancy_billing_identities "
    )

    def __init__(self, conn: sqlite3.Connection, counters: _Counters) -> None:
        self._conn = conn
        self._counters = counters

    def _rows(self, where: str, params: tuple) -> list[BillingIdentity]:
        try:
            rows = self._conn.execute(self._SELECT + where, params).fetchall()
        except sqlite3.OperationalError as exc:
            # A source we cannot read is a broken round, never "no binding".
            raise ShadowError("billing_identity_store_unreadable") from exc
        return [BillingIdentity(**dict(row)) for row in rows]

    def get(self, employee_user_id: str) -> Optional[BillingIdentity]:
        rows = self._rows("WHERE employee_user_id = ?", (employee_user_id,))
        return rows[0] if rows else None

    def get_by_profile(self, profile_name: str) -> Optional[BillingIdentity]:
        rows = self._rows(
            "WHERE profile_name = ? AND migration_state = 'enforced'",
            (profile_name,),
        )
        if len(rows) > 1:
            raise RunRejected("billing payer profile is ambiguous")
        return rows[0] if rows else None

    def put(self, identity: Any) -> None:
        self._counters.billing_db_write_attempts += 1
        raise ShadowError("planner_boundary_violation:store.put")


class _ReadOnlyRouting:
    """Read-path lookups mirroring routing.RoutingTable over the ro connection.

    RoutingTable itself runs schema/index writes in ``__init__`` so it cannot
    open a mode=ro database; the SELECTs below are copied verbatim from its
    read path (lookup_by_user_id / lookup_by_profile_name / lookup_by_chat_id /
    resolve_owner_root) so admission replay sees identical routing semantics.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _one(self, sql: str, params: tuple) -> Any:
        row = self._conn.execute(sql, params).fetchone()
        return SimpleNamespace(**dict(row)) if row else None

    def lookup_by_user_id(self, user_id: str) -> Any:
        return self._one(
            "SELECT * FROM multitenancy_routing WHERE user_id = ? AND active = 1 LIMIT 1",
            (user_id,),
        )

    def lookup_by_profile_name(self, profile_name: str) -> Any:
        return self._one(
            "SELECT * FROM multitenancy_routing "
            "WHERE profile_name = ? AND active = 1 "
            "ORDER BY (provenance = 'sync') DESC, updated_at DESC LIMIT 1",
            (profile_name,),
        )

    def lookup_by_chat_id(self, chat_id: str) -> Any:
        return self._one(
            "SELECT * FROM multitenancy_routing "
            "WHERE chat_id = ? AND active = 1 AND kind = 'group' LIMIT 1",
            (chat_id,),
        )

    def resolve_owner_root(self, open_id: str) -> Any:
        return self._one(
            "SELECT * FROM multitenancy_routing "
            "WHERE open_id = ? AND active = 1 "
            "AND kind = 'user' AND provenance = 'sync' LIMIT 1",
            (open_id,),
        )


def _validate_config() -> tuple[bool, str]:
    """Return (billing_enabled, stable cohort hash — memory only, never output)."""
    enabled = os.environ.get("HERMES_LITELLM_BILLING_ENABLED", "").strip().lower() in _TRUE
    if not enabled:
        return False, ""
    raw = os.environ.get("HERMES_LITELLM_BILLING_PAYER_IDS", "")
    try:
        # Same canonical rejection startup_guard enforces: empty / '*' /
        # duplicate / malformed cohorts must never start a shadow round.
        return True, cohort_hash(raw)
    except BillingReadinessError as exc:
        raise ShadowConfigError(str(exc)) from exc


def _open_ro(db_path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        raise ShadowError(f"routing_db_unreadable:{type(exc).__name__}") from exc
    conn.row_factory = sqlite3.Row
    return conn


def _require_tables(conn: sqlite3.Connection) -> None:
    present = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for table in ("multitenancy_routing", "multitenancy_billing_identities"):
        if table not in present:
            raise ShadowError(f"source_table_missing:{table}")


def _db_digest(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table, order in (
        ("multitenancy_routing", "user_id"),
        ("multitenancy_billing_identities", "employee_user_id"),
    ):
        digest.update(table.encode())
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            digest.update(
                json.dumps(list(row), default=str, separators=(",", ":")).encode()
            )
    return digest.hexdigest()


def _load_org_snapshot(db_path: Path) -> tuple[str, dict[str, Any]]:
    """Return (digest, employees).  A missing/unreadable snapshot fails closed."""
    directory = Path(
        os.environ.get("HERMES_ORG_SNAPSHOT_DIR", "").strip()
        or db_path.expanduser().parent / "org-snapshots"
    ).expanduser()
    try:
        newest = max(directory.glob("org-*.json"), key=lambda path: path.stat().st_mtime)
        raw = newest.read_bytes()
    except (OSError, ValueError) as exc:
        raise ShadowError("org_snapshot_missing") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ShadowError("org_snapshot_unreadable") from exc
    employees = payload.get("employees") if isinstance(payload, dict) else None
    # An empty employees table is as disqualifying as a missing file: there is
    # no org universe to reconcile against, so the round must not start
    # (codex round 2: empty {} previously passed with roots_missing_in_org=N).
    if not isinstance(employees, dict) or not employees:
        raise ShadowError("org_snapshot_missing_employees")
    return hashlib.sha256(raw).hexdigest(), employees


@contextlib.contextmanager
def _boundary_traps(counters: _Counters, routing: Any) -> Iterator[None]:
    """Install process-wide traps for the replay window (restored on exit).

    This is what makes the assertion-block zeros measurements on the real CLI
    path: RunBroker dispatch and every Gateway transport method are rebound to
    counting traps, and the router module's lazy routing global is pinned to
    the read-only table so no code path can construct a writable RoutingTable.
    """
    from . import router as router_module
    from .billing_credentials import BillingGatewayClient
    from .run_broker import RunBroker

    def _dispatch_trap(*_args: Any, **_kwargs: Any) -> Any:
        counters.run_broker_dispatch_calls += 1
        raise ShadowError("planner_boundary_violation:run_broker.dispatch")

    def _gateway_trap(*_args: Any, **_kwargs: Any) -> Any:
        counters.gateway_ensure_calls += 1
        raise ShadowError("planner_boundary_violation:gateway.transport")

    saved_routing = router_module._routing_table
    saved_run = RunBroker.run
    saved_gateway = {
        name: getattr(BillingGatewayClient, name) for name in ("ensure", "ack", "_post")
    }
    router_module._routing_table = routing
    RunBroker.run = _dispatch_trap  # type: ignore[method-assign]
    for name in saved_gateway:
        setattr(BillingGatewayClient, name, _gateway_trap)
    try:
        yield
    finally:
        router_module._routing_table = saved_routing
        RunBroker.run = saved_run  # type: ignore[method-assign]
        for name, value in saved_gateway.items():
            setattr(BillingGatewayClient, name, value)


def _shape_builders(root: Any, owned_groups: list[Any]) -> list[tuple[str, Callable[[], RunRequest]]]:
    """Per-shape builders reusing the REAL production request constructors.

    dm/group go through router._run_request_for_routed_event, cron through
    cron.run_broker_bridge._build_cron_run_request, kanban through
    kanban_sidecar.build_run_request_for_task — so an entrance-normalization
    regression cannot leave shadow green.  webui has no reusable builder (it
    is an aiohttp closure); its mirrored fields are pinned in the SPEC Dead
    ends entry and below.
    """
    from . import router as router_module
    from .cron.run_broker_bridge import _build_cron_run_request
    from .kanban_sidecar import KanbanSidecarConfig, build_run_request_for_task

    profile = str(root.profile_name or "") or str(root.user_id)
    open_id = str(root.open_id or "")

    def _dm() -> RunRequest:
        # Incident-shaped DM: the routed sender is the tenant user_id alias,
        # not ou_*; the real builder canonicalizes it through routing.
        return router_module._run_request_for_routed_event(
            event=SimpleNamespace(chat_type="p2p"),
            profile_name=profile,
            sender=str(root.user_id),
            sender_alt=None,
            chat_id="oc-shadow-dm",
            text=_REPLAY_CONTENT,
        )

    def _webui() -> RunRequest:
        # Real production field contract, extracted from the ingest closure
        # for reuse (codex round 2): _prepare_ingest_run_request routes
        # through this same function, so the shapes cannot drift apart.
        from .webui_broker_server import build_ingest_run_request

        return build_ingest_run_request(
            bound_profile=profile,
            content=_REPLAY_CONTENT,
            delivery_mode="sync",  # production sync ingest lane
        )

    def _cron() -> RunRequest:
        return _build_cron_run_request(
            {"id": "shadow", "owner_open_id": open_id, "owner_profile": profile},
            profile_home=Path("/nonexistent/billing-shadow"),
            prompt=_REPLAY_CONTENT,
        )

    def _kanban() -> RunRequest:
        return build_run_request_for_task(
            SimpleNamespace(
                id="shadow",
                assignee=profile,
                title="shadow",
                body="",
                tenant=None,
                created_by=open_id or str(root.user_id),
                current_run_id="",
            ),
            config=KanbanSidecarConfig(),
            workspace="shadow",
        )

    builders: list[tuple[str, Callable[[], RunRequest]]] = [
        ("dm", _dm),
        ("webui", _webui),
        ("cron", _cron),
        ("kanban", _kanban),
    ]
    for group in owned_groups:
        def _group(g: Any = group) -> RunRequest:
            # Unknown member posting in an owned group: billing must resolve
            # the group owner via chat_id, never the sender.
            return router_module._run_request_for_routed_event(
                event=SimpleNamespace(chat_type="group"),
                profile_name=str(g.profile_name),
                sender="",
                sender_alt=None,
                chat_id=str(g.chat_id),
                text=_REPLAY_CONTENT,
            )

        builders.append(("group", _group))
    return builders


def _replay_one(
    preparer: BillingIdentityPreparer, build: Callable[[], RunRequest]
) -> tuple[str, str]:
    try:
        request = build()
    except ValueError:
        # The real channel builder refused to construct the request (missing
        # owner open_id, no user_key, ...): production fails closed at the
        # entrance, so the root lands in the fail-closed bucket.  Fixed reason
        # code only — builder messages can carry identifiers.
        return STATUS_IDENTITY_INVALID, "builder_rejected_identity"
    try:
        prepared = preparer.prepare(request)
    except _BoundaryStop as stop:
        return stop.status, "planner_boundary_stop"
    except RunRejected as exc:
        mapped = _REASON_BY_REJECTION.get(str(exc))
        if mapped is None:
            # Never echo unknown rejection text — fail the whole round.
            raise ShadowError("unmapped_admission_rejection") from None
        return mapped
    if prepared.metadata.get("litellm_billing_enforced"):
        # Cannot happen while the sentinel raises, but never trust it silently.
        raise ShadowError("planner_boundary_violation:enforced_metadata_emitted")
    return STATUS_NONCOHORT_LEGACY, "legacy_admission"


def run_shadow(
    *,
    db_path: str | os.PathLike[str] | None = None,
    report_dir: str | os.PathLike[str] | None = None,
    _mid_run_hook: Optional[Callable[[], None]] = None,  # test seam: drift/trap probes
) -> dict[str, Any]:
    enabled, stable_cohort_hash = _validate_config()
    path = Path(
        str(db_path or os.environ.get("HERMES_MULTITENANCY_DB", "").strip() or DEFAULT_DB_PATH)
    ).expanduser()
    conn = _open_ro(path)
    try:
        _require_tables(conn)
        counters = _Counters()
        # Pin the whole round on one read snapshot: explicit read transaction,
        # acquired by the first SELECT below (routing DB is WAL).
        conn.execute("BEGIN")
        pre_digest = _db_digest(conn)
        pre_org_digest, employees = _load_org_snapshot(path)

        roots = [
            SimpleNamespace(**dict(row))
            for row in conn.execute(
                "SELECT * FROM multitenancy_routing "
                "WHERE active = 1 AND kind = 'user' AND provenance = 'sync' "
                "ORDER BY user_id"
            ).fetchall()
        ]
        groups_by_owner: dict[str, list[Any]] = {}
        for row in conn.execute(
            "SELECT * FROM multitenancy_routing WHERE active = 1 AND kind = 'group'"
        ).fetchall():
            group = SimpleNamespace(**dict(row))
            groups_by_owner.setdefault(str(group.owner_open_id or ""), []).append(group)

        salt = secrets.token_bytes(32)  # in-memory only; discarded with the round
        precedence = {status: rank for rank, status in enumerate(ADMISSION_STATUSES)}
        counts = {status: 0 for status in ADMISSION_STATUSES}
        shape_counts = {shape: {status: 0 for status in ADMISSION_STATUSES} for shape in SHAPES}
        cases: list[dict[str, Any]] = []
        with _boundary_traps(counters, _ReadOnlyRouting(conn)):
            if _mid_run_hook is not None:
                _mid_run_hook()
            preparer = BillingIdentityPreparer(
                routing=_ReadOnlyRouting(conn),
                store=_ReadOnlyIdentityStore(conn, counters),
                credentials=_SentinelCredentials(counters),
            )
            for root in roots:
                owned = groups_by_owner.get(str(root.open_id or ""), [])
                shapes: dict[str, dict[str, str]] = {}
                worst = STATUS_NONCOHORT_LEGACY
                for index, (shape, build) in enumerate(_shape_builders(root, owned)):
                    status, reason = _replay_one(preparer, build)
                    shape_counts[shape][status] += 1
                    key = shape if shape != "group" else f"group:{index}"
                    shapes[key] = {"status": status, "reason": reason}
                    if precedence[status] < precedence[worst]:
                        worst = status
                counts[worst] += 1
                cases.append(
                    {
                        "case_id": hmac.new(
                            salt, str(root.user_id).encode(), hashlib.sha256
                        ).hexdigest()[:32],
                        "status": worst,
                        "shapes": shapes,
                    }
                )
        conn.execute("COMMIT")
    finally:
        conn.close()

    # Drift detection must NOT reuse the pinned transaction: a second fresh
    # read-only connection sees the current committed state.
    conn2 = _open_ro(path)
    try:
        _require_tables(conn2)
        post_digest = _db_digest(conn2)
    finally:
        conn2.close()
    post_org_digest, _post_employees = _load_org_snapshot(path)

    if pre_digest != post_digest or pre_org_digest != post_org_digest:
        raise ShadowDriftError("source_drift_detected_round_voided")
    if (
        counters.gateway_ensure_calls
        or counters.run_broker_dispatch_calls
        or counters.billing_db_write_attempts
    ):
        raise ShadowInvariantError("boundary_counter_nonzero")
    if sum(counts.values()) != len(roots):
        raise ShadowInvariantError("classification_sum_mismatch")

    # Org universe cross-check (counts only, no identifiers): every root should
    # exist in the org snapshot and vice versa; imbalances are surfaced, and
    # coverage is judged against the org universe rather than only against the
    # roots list itself.
    root_ids = {str(root.user_id) for root in roots}
    org_ids: set[str] = set()
    matched_org_entries = 0
    for key, value in employees.items():
        entry_ids = {str(key)}
        if isinstance(value, dict) and str(value.get("user_id") or "").strip():
            entry_ids.add(str(value["user_id"]))
        org_ids |= entry_ids
        if entry_ids & root_ids:
            matched_org_entries += 1
    roots_in_org = sum(1 for root_id in root_ids if root_id in org_ids)

    fd, report_path = tempfile.mkstemp(
        prefix="billing-shadow-", suffix=".json", dir=report_dir and str(report_dir)
    )
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"generated_at": int(time.time()), "cases": cases}, handle, indent=2)

    return {
        "universe_total": len(roots),
        "counts": counts,
        "shape_counts": shape_counts,
        "assertions": {
            "gateway_ensure_calls": counters.gateway_ensure_calls,
            "billing_db_write_attempts": counters.billing_db_write_attempts,
            "run_broker_dispatch_calls": counters.run_broker_dispatch_calls,
            "planner_boundary_stops": counters.planner_boundary_stops,
        },
        "sources": {
            "routing_db_digest": pre_digest,
            "org_snapshot_digest": pre_org_digest,
            "routing_roots": len(roots),
        },
        "org_reconciliation": {
            "org_employees": len(employees),
            "roots_total": len(roots),
            "roots_in_org": roots_in_org,
            "roots_missing_in_org": len(roots) - roots_in_org,
            "org_missing_in_roots": len(employees) - matched_org_entries,
        },
        "config": {
            "billing_enabled": enabled,
            # Salted per round with the case-ID salt: never a stable identity
            # fingerprint of a low-entropy cohort set.
            "cohort_fingerprint": (
                hmac.new(salt, f"cohort:{stable_cohort_hash}".encode(), hashlib.sha256)
                .hexdigest()[:32]
                if enabled
                else ""
            ),
        },
        "detail_report": {
            "path": report_path,
            "mode": "0600",
            "destroy_within_hours": 24,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes-multitenancy-billing-shadow",
        description="Operator-only planner-boundary billing shadow replay (read-only).",
    )
    parser.add_argument("--db", default=None, help="routing SQLite path (default env/production)")
    parser.add_argument("--report-dir", default=None, help="directory for the 0600 detail report")
    args = parser.parse_args(argv)
    try:
        summary = run_shadow(db_path=args.db, report_dir=args.report_dir)
    except ShadowError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return exc.exit_code
    print(json.dumps({"ok": True, **summary}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
