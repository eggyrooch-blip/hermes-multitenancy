"""Planner-boundary contract for the operator billing shadow runner.

Covers the four SPEC acceptance scenarios, the review-probe counterexamples
(P0: stable cohort fingerprint, unmapped rejection text leak), the hard
interception layers wired on the real CLI path, and the classification-enum
alignment with billing_readiness.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import stat
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_environ():
    # Billing helpers read os.environ directly; snapshot/restore so a failing
    # test can never leak HERMES_* / billing env into later tests.
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


_NOW = int(time.time() * 1000)

# Fixture identities — none of these strings may ever appear in runner output.
_ROOTS = (
    # (user_id, profile, open_id)
    ("e-alpha", "prof-alpha", "ou_alpha"),   # noncohort, DM ok
    ("e-bravo", "prof-bravo", "ou_bravo"),   # cohort payer
    ("e-charlie", "prof-charlie", "ou_charlie"),  # existing enforced binding
    ("e-delta", "prof-delta", None),         # identity-invalid (no open_id)
    ("e-echo", "prof-echo", "ou_echo"),      # owns a group chat
)
_GROUP_CHAT = "oc-shadow-group"
_SECRET_STRINGS = [item for root in _ROOTS for item in root if item] + [_GROUP_CHAT]


def _build_fixture(tmp_path, monkeypatch, *, billing_table=True):
    from hermes_multitenancy.routing import RoutingTable

    db_path = tmp_path / "multitenancy.db"
    RoutingTable(db_path)  # create the real production schema
    conn = sqlite3.connect(db_path)
    for user_id, profile, open_id in _ROOTS:
        conn.execute(
            "INSERT INTO multitenancy_routing "
            "(user_id, profile_name, open_id, active, synced_at, version, "
            " created_at, updated_at, kind, owner_open_id, provenance) "
            "VALUES (?, ?, ?, 1, ?, 1, ?, ?, 'user', ?, 'sync')",
            (user_id, profile, open_id, _NOW, _NOW, _NOW, open_id),
        )
    conn.execute(
        "INSERT INTO multitenancy_routing "
        "(user_id, profile_name, open_id, active, synced_at, version, "
        " created_at, updated_at, kind, chat_id, owner_open_id) "
        "VALUES ('group-shadow', 'group-shadow', NULL, 1, ?, 1, ?, ?, "
        " 'group', ?, 'ou_echo')",
        (_NOW, _NOW, _NOW, _GROUP_CHAT),
    )
    conn.commit()
    conn.close()

    if billing_table:
        from hermes_multitenancy.billing_credentials import BillingIdentity
        from hermes_multitenancy.billing_identity import BillingIdentityStore

        store = BillingIdentityStore(db_path)
        store.put(
            BillingIdentity(
                employee_user_id="e-charlie",
                profile_name="prof-charlie",
                email="e-charlie@example.com",
                litellm_user_id="llm-charlie",
                team_id="team-1",
                team_alias="T1",
                key_id="key-1",
                credential_version=1,
                expires_at=_NOW + 10_000_000,
                migration_state="enforced",
            )
        )

    org_dir = tmp_path / "org-snapshots"
    org_dir.mkdir()
    # Real employees universe: all five roots plus one org-only employee, so
    # the org cross-check has something真实 to reconcile (review P1: an empty
    # employees snapshot must not silently pass as "covered").
    employees = {user_id: {"user_id": user_id} for user_id, _p, _o in _ROOTS}
    employees["e-zulu"] = {"user_id": "e-zulu"}
    (org_dir / "org-1.json").write_text(
        json.dumps({"employees": employees, "departments": []})
    )

    monkeypatch.setenv("HERMES_MULTITENANCY_DB", str(db_path))
    monkeypatch.setenv("HERMES_ORG_SNAPSHOT_DIR", str(org_dir))
    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", "e-bravo")
    # The real cron builder falls back to session-owner env when the job has
    # no valid owner; keep classification deterministic.
    monkeypatch.delenv("HERMES_FEISHU_USER_OPEN_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    return db_path


@pytest.fixture
def _hard_blocks(monkeypatch):
    """Belt-and-braces below the runner's own traps: network raises."""

    def _no_network(*_args, **_kwargs):
        raise AssertionError("network egress crossed the planner boundary")

    monkeypatch.setattr(socket, "socket", _no_network)


def test_full_round_classifies_all_roots_with_zero_writes(
    tmp_path, monkeypatch, _hard_blocks
):
    from hermes_multitenancy import billing_shadow as shadow

    _build_fixture(tmp_path, monkeypatch)
    summary = shadow.run_shadow(report_dir=tmp_path)

    assert summary["universe_total"] == 5
    counts = summary["counts"]
    assert sum(counts.values()) == 5
    assert counts[shadow.STATUS_IDENTITY_INVALID] == 1  # independent fail-closed bucket
    assert counts[shadow.STATUS_NONCOHORT_LEGACY] == 2
    assert counts[shadow.STATUS_COHORT_WOULD_ENFORCE] == 1
    assert counts[shadow.STATUS_ENFORCED_EXISTING] == 1
    assert counts[shadow.STATUS_DRIFT_OR_CONFLICT] == 0

    # All five production shapes actually replayed.
    shape_totals = {
        shape: sum(statuses.values()) for shape, statuses in summary["shape_counts"].items()
    }
    assert shape_totals["dm"] == 5
    assert shape_totals["webui"] == 5
    assert shape_totals["cron"] == 5
    assert shape_totals["kanban"] == 5
    assert shape_totals["group"] == 1
    # The group replay resolved the owner and stayed legacy.
    assert summary["shape_counts"]["group"][shadow.STATUS_NONCOHORT_LEGACY] == 1

    # Machine-assertion block: zero boundary crossings, measured by the traps.
    assertions = summary["assertions"]
    assert assertions["gateway_ensure_calls"] == 0
    assert assertions["billing_db_write_attempts"] == 0
    assert assertions["run_broker_dispatch_calls"] == 0
    # cohort payer + enforced-existing each stop at the boundary on all four
    # profile-owner shapes (dm/webui/cron/kanban): 2 roots x 4 shapes.
    assert assertions["planner_boundary_stops"] == 8

    # Org universe cross-check: five roots reconciled, one org-only employee
    # surfaced, nothing silently assumed covered.
    assert summary["org_reconciliation"] == {
        "org_employees": 6,
        "roots_total": 5,
        "roots_in_org": 5,
        "roots_missing_in_org": 0,
        "org_missing_in_roots": 1,
    }

    # Privacy: no real identifier in stdout summary nor in the detail report.
    report_path = Path(summary["detail_report"]["path"])
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    detail_text = report_path.read_text()
    summary_text = json.dumps(summary)
    for secret in _SECRET_STRINGS + ["@example.com", "ou_"]:
        assert secret not in summary_text
        assert secret not in detail_text
    cases = json.loads(detail_text)["cases"]
    assert len(cases) == 5
    assert {case["status"] for case in cases} == {
        shadow.STATUS_IDENTITY_INVALID,
        shadow.STATUS_NONCOHORT_LEGACY,
        shadow.STATUS_COHORT_WOULD_ENFORCE,
        shadow.STATUS_ENFORCED_EXISTING,
    }
    # The identity-invalid case proves the REAL builders ran: the cron channel
    # refused to construct the request (fail-closed at the entrance) while the
    # feishu path resolved-and-rejected inside the preparer.
    invalid = next(c for c in cases if c["status"] == shadow.STATUS_IDENTITY_INVALID)
    assert invalid["shapes"]["cron"]["reason"] == "builder_rejected_identity"
    assert invalid["shapes"]["dm"]["reason"] == "identity_unresolved"


def test_second_round_rotates_case_ids_but_counts_match(
    tmp_path, monkeypatch, _hard_blocks
):
    from hermes_multitenancy import billing_shadow as shadow

    _build_fixture(tmp_path, monkeypatch)
    first = shadow.run_shadow(report_dir=tmp_path)
    second = shadow.run_shadow(report_dir=tmp_path)

    assert first["counts"] == second["counts"]
    assert first["shape_counts"] == second["shape_counts"]
    ids_first = {
        case["case_id"]
        for case in json.loads(Path(first["detail_report"]["path"]).read_text())["cases"]
    }
    ids_second = {
        case["case_id"]
        for case in json.loads(Path(second["detail_report"]["path"]).read_text())["cases"]
    }
    assert ids_first and ids_second
    assert ids_first.isdisjoint(ids_second)  # fresh random salt per round


def test_cohort_fingerprint_is_salted_per_round(tmp_path, monkeypatch, _hard_blocks):
    """Review P0 probe: the stable cohort hash of a low-entropy cohort set is
    an enumerable identity fingerprint and must never appear in output."""
    from hermes_multitenancy import billing_shadow as shadow
    from hermes_multitenancy.billing_readiness import cohort_hash

    _build_fixture(tmp_path, monkeypatch)
    stable = cohort_hash("e-bravo")
    first = shadow.run_shadow(report_dir=tmp_path)
    second = shadow.run_shadow(report_dir=tmp_path)

    for summary in (first, second):
        text = json.dumps(summary)
        assert stable not in text
        assert stable[:16] not in text  # no truncated stable form either
    assert first["config"]["cohort_fingerprint"]
    assert first["config"]["cohort_fingerprint"] != second["config"]["cohort_fingerprint"]


def test_unmapped_rejection_fails_round_without_leaking(
    tmp_path, monkeypatch, _hard_blocks
):
    """Review P0 probe: unknown RunRejected text must fail the round, never
    land in the report or summary."""
    from hermes_multitenancy import billing_shadow as shadow
    from hermes_multitenancy.billing_identity import BillingIdentityPreparer
    from hermes_multitenancy.run_broker import RunRejected

    _build_fixture(tmp_path, monkeypatch)
    probe = "probe employee e-alpha with profile prof-alpha leaked"

    def _hostile_prepare(self, request):
        raise RunRejected(probe)

    monkeypatch.setattr(BillingIdentityPreparer, "prepare", _hostile_prepare)
    with pytest.raises(shadow.ShadowError) as excinfo:
        shadow.run_shadow(report_dir=tmp_path)
    assert str(excinfo.value) == "unmapped_admission_rejection"
    assert probe not in str(excinfo.value)
    assert list(tmp_path.glob("billing-shadow-*.json")) == []  # no report written


def test_db_mutation_mid_round_voids_the_round(tmp_path, monkeypatch, _hard_blocks):
    from hermes_multitenancy import billing_shadow as shadow

    db_path = _build_fixture(tmp_path, monkeypatch)

    def _mutate():
        writer = sqlite3.connect(db_path)
        writer.execute(
            "UPDATE multitenancy_routing SET updated_at = updated_at + 1 "
            "WHERE user_id = 'e-alpha'"
        )
        writer.commit()
        writer.close()

    with pytest.raises(shadow.ShadowDriftError):
        shadow.run_shadow(report_dir=tmp_path, _mid_run_hook=_mutate)
    # CLI maps the drift to a dedicated nonzero exit and writes no report.
    monkeypatch.setattr(shadow, "run_shadow", _raise_drift)
    assert shadow.main([]) == shadow.EXIT_DRIFT
    assert list(tmp_path.glob("billing-shadow-*.json")) == []


def _raise_drift(**_kwargs):
    from hermes_multitenancy.billing_shadow import ShadowDriftError

    raise ShadowDriftError("source_drift_detected_round_voided")


def test_enabled_with_wildcard_or_empty_cohort_refuses_start(tmp_path, monkeypatch):
    from hermes_multitenancy import billing_shadow as shadow

    monkeypatch.setenv("HERMES_LITELLM_BILLING_ENABLED", "true")
    for cohort in ("*", "", "a,*", "a,a"):
        monkeypatch.setenv("HERMES_LITELLM_BILLING_PAYER_IDS", cohort)
        with pytest.raises(shadow.ShadowConfigError):
            shadow.run_shadow(db_path=tmp_path / "never-created.db")
        assert shadow.main(["--db", str(tmp_path / "never-created.db")]) == shadow.EXIT_CONFIG
    assert not (tmp_path / "never-created.db").exists()  # refused before touching sources


def test_missing_org_snapshot_fails_closed(tmp_path, monkeypatch, _hard_blocks):
    """Review P1: an absent/unreadable org source voids the round, never
    degrades to an 'absent' digest."""
    from hermes_multitenancy import billing_shadow as shadow

    _build_fixture(tmp_path, monkeypatch)
    org_dir = tmp_path / "org-snapshots"
    (org_dir / "org-1.json").unlink()
    with pytest.raises(shadow.ShadowError) as excinfo:
        shadow.run_shadow(report_dir=tmp_path)
    assert str(excinfo.value) == "org_snapshot_missing"

    (org_dir / "org-1.json").write_text(json.dumps({"departments": []}))
    with pytest.raises(shadow.ShadowError) as excinfo:
        shadow.run_shadow(report_dir=tmp_path)
    assert str(excinfo.value) == "org_snapshot_missing_employees"

    # codex round 2 counterexample: employees == {} used to pass with
    # roots_missing_in_org=N; an empty org universe must refuse to start.
    (org_dir / "org-1.json").write_text(json.dumps({"employees": {}, "departments": []}))
    with pytest.raises(shadow.ShadowError) as excinfo:
        shadow.run_shadow(report_dir=tmp_path)
    assert str(excinfo.value) == "org_snapshot_missing_employees"


def test_missing_billing_table_fails_round(tmp_path, monkeypatch, _hard_blocks):
    """Review P1: an unreadable billing-identity source fails the round; it is
    never downgraded to 'no binding'."""
    from hermes_multitenancy import billing_shadow as shadow

    _build_fixture(tmp_path, monkeypatch, billing_table=False)
    with pytest.raises(shadow.ShadowError) as excinfo:
        shadow.run_shadow(report_dir=tmp_path)
    assert str(excinfo.value) == "source_table_missing:multitenancy_billing_identities"


def test_boundary_traps_are_wired_in_runner_path(tmp_path, monkeypatch, _hard_blocks):
    """Review P1 probe: the gateway/dispatch zeros must be measurements.  Any
    crossing during the round is counted by traps the RUNNER installed (no
    test monkeypatching) and fails the round."""
    from hermes_multitenancy import billing_shadow as shadow

    _build_fixture(tmp_path, monkeypatch)

    def _probe():
        from hermes_multitenancy.billing_credentials import BillingGatewayClient
        from hermes_multitenancy.run_broker import RunBroker

        with pytest.raises(shadow.ShadowError):
            RunBroker.run(None)
        with pytest.raises(shadow.ShadowError):
            BillingGatewayClient.ensure(None)
        with pytest.raises(shadow.ShadowError):
            BillingGatewayClient._post(None)

    with pytest.raises(shadow.ShadowInvariantError) as excinfo:
        shadow.run_shadow(report_dir=tmp_path, _mid_run_hook=_probe)
    assert str(excinfo.value) == "boundary_counter_nonzero"
    assert list(tmp_path.glob("billing-shadow-*.json")) == []


def test_interception_layers_hard_block_not_narrate(tmp_path, monkeypatch):
    from hermes_multitenancy import billing_shadow as shadow

    db_path = _build_fixture(tmp_path, monkeypatch)
    counters = shadow._Counters()

    sentinel = shadow._SentinelCredentials(counters)
    with pytest.raises(shadow._BoundaryStop):
        sentinel.ensure_available(object(), None)
    with pytest.raises(shadow.ShadowError):
        sentinel.runtime_api_key  # any non-boundary attribute is a violation
    assert counters.planner_boundary_stops == 1

    conn = shadow._open_ro(db_path)
    store = shadow._ReadOnlyIdentityStore(conn, counters)
    with pytest.raises(shadow.ShadowError):
        store.put(object())
    assert counters.billing_db_write_attempts == 1
    # The connection itself refuses writes — enforcement below Python code.
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE multitenancy_routing SET active = 0")
    conn.close()


def test_webui_shape_matches_production_ingest_contract():
    """codex round 2: the webui shadow shape must be the SAME field contract
    the production ingest closure builds — both route through the extracted
    build_ingest_run_request, and every field must match."""
    from types import SimpleNamespace

    from hermes_multitenancy import billing_shadow as shadow
    from hermes_multitenancy.webui_broker_server import build_ingest_run_request

    root = SimpleNamespace(user_id="e-x", profile_name="prof-x", open_id="ou_x")
    webui_build = dict(shadow._shape_builders(root, []))["webui"]
    shadow_request = webui_build()
    production_request = build_ingest_run_request(
        bound_profile="prof-x",
        content=shadow._REPLAY_CONTENT,
        delivery_mode="sync",
    )
    assert shadow_request == production_request  # frozen dataclass: all fields
    assert shadow_request.delivery_mode == "sync"  # a real production lane
    assert shadow_request.metadata == {"source": "ingest"}
    assert shadow_request.requires_host_tools is True
    assert shadow_request.credential_subject == "prof-x"


def test_admission_enum_aligns_with_billing_readiness():
    from hermes_multitenancy import billing_shadow as shadow
    from hermes_multitenancy.billing_readiness import _ALLOWED_STATUSES

    # Shared concept keeps the exact readiness spelling; the admission-only
    # statuses must not collide with (or shadow) any readiness status.
    assert shadow.STATUS_DRIFT_OR_CONFLICT in _ALLOWED_STATUSES
    admission_only = set(shadow.ADMISSION_STATUSES) - {shadow.STATUS_DRIFT_OR_CONFLICT}
    assert admission_only.isdisjoint(_ALLOWED_STATUSES)
    assert len(set(shadow.ADMISSION_STATUSES)) == len(shadow.ADMISSION_STATUSES)


def test_dormant_config_replays_everything_legacy(tmp_path, monkeypatch, _hard_blocks):
    from hermes_multitenancy import billing_shadow as shadow

    _build_fixture(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_LITELLM_BILLING_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_LITELLM_BILLING_PAYER_IDS", raising=False)
    summary = shadow.run_shadow(report_dir=tmp_path)

    # Billing off: nobody is newly selected; only the already-enforced payer
    # still reaches the planner boundary (enforced never falls back to shared).
    # The open_id-less root stays fail-closed even dormant: the real cron
    # builder cannot construct a request for it in production either.
    assert summary["universe_total"] == 5
    assert summary["counts"][shadow.STATUS_NONCOHORT_LEGACY] == 3
    assert summary["counts"][shadow.STATUS_ENFORCED_EXISTING] == 1
    assert summary["counts"][shadow.STATUS_IDENTITY_INVALID] == 1
    assert summary["assertions"]["gateway_ensure_calls"] == 0
    assert summary["assertions"]["billing_db_write_attempts"] == 0
    assert summary["config"] == {"billing_enabled": False, "cohort_fingerprint": ""}
