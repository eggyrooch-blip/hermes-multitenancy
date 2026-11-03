"""M-0 executor identity: tasks carry who-does-it (agent_id + expert_id).

Covers the four SPEC segments (m0-task-executor-identity):
  * create — persist agent_id (server-derived) + expert_id (fail-closed audience
    gate at creation), executor immutable via update;
  * store  — fields ride the owner_updates bus into the profile cron store;
  * wake   — _expert_id_for_cron_job trusts the stored field only for
    non-source_app jobs; RunRequest metadata carries agent_id;
  * shadow — plan_cron_bridge_run exposes agent_id/expert_id (the Done probe)
    and stays byte-compatible for legacy jobs without executor fields.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_multitenancy import cron_api
from hermes_multitenancy import cron_worker
from hermes_multitenancy import expert_overlay
from hermes_multitenancy.cron import run_broker_bridge

from tests.test_webui_broker_server import _install_fake_cron


class _Overlay:
    """Minimal stand-in for expert_overlay.ExpertOverlay."""


def _stub_visible_catalog(monkeypatch, expert_id: str = "hr-expert") -> None:
    """Make the wake-time catalog revalidation see the stored expert as visible.

    The revalidation itself (revoked → reject) has its own assertions below and
    in test_expert_bot_scheduled_tasks; the delivery/mirror preflight has a
    dedicated test there too, so it is stubbed out where irrelevant.
    """
    overlay = SimpleNamespace(expert_id=expert_id)
    monkeypatch.setattr(expert_overlay, "resolve_expert", lambda *a, **k: overlay)
    monkeypatch.setattr(expert_overlay, "resolve_caller_departments", lambda *a, **k: None)
    monkeypatch.setattr(
        cron_worker, "_assert_webui_expert_cron_dependencies", lambda *a, **k: None
    )


def _patch_profile_home(monkeypatch, tmp_path: Path) -> Path:
    profiles_root = tmp_path / "profiles"

    def fake_home(profile_name: str) -> Path:
        return profiles_root / cron_api.validate_profile_name(profile_name)

    monkeypatch.setattr(cron_api, "profile_home_for", fake_home)
    _install_fake_cron(monkeypatch)
    return profiles_root


def _create_body(**extra):
    body = {"name": "m0 task", "schedule": "*/5 * * * *", "prompt": "do the thing"}
    body.update(extra)
    return body


# ---------------------------------------------------------------- create/store


def test_create_job_persists_executor_identity(monkeypatch, tmp_path):
    _patch_profile_home(monkeypatch, tmp_path)
    monkeypatch.setattr(expert_overlay, "resolve_expert", lambda *a, **k: _Overlay())
    monkeypatch.setattr(expert_overlay, "resolve_caller_departments", lambda *a, **k: None)

    job = cron_api.create_job(
        "agent_prof",
        "ou_creator",
        _create_body(expert_id="hr-expert"),
        agent_id="agent-1",
    )

    assert job["agent_id"] == "agent-1"
    assert job["expert_id"] == "hr-expert"
    assert job["owner_open_id"] == "ou_creator"
    assert job["owner_profile"] == "agent_prof"

    # Persisted, not just returned: read back through the same profile scope.
    stored = cron_api.get_job("agent_prof", job["id"])
    assert stored["agent_id"] == "agent-1"
    assert stored["expert_id"] == "hr-expert"


def test_create_job_without_executor_keeps_legacy_shape(monkeypatch, tmp_path):
    _patch_profile_home(monkeypatch, tmp_path)

    job = cron_api.create_job("ownerprof", "ou_creator", _create_body())

    assert "agent_id" not in job
    assert "expert_id" not in job
    assert job["owner_open_id"] == "ou_creator"


def test_create_job_rejects_unavailable_expert(monkeypatch, tmp_path):
    _patch_profile_home(monkeypatch, tmp_path)
    monkeypatch.setattr(expert_overlay, "resolve_expert", lambda *a, **k: None)
    monkeypatch.setattr(expert_overlay, "resolve_caller_departments", lambda *a, **k: None)

    with pytest.raises(cron_api.CronApiError) as exc:
        cron_api.create_job(
            "agent_prof",
            "ou_creator",
            _create_body(expert_id="forbidden-expert"),
            agent_id="agent-1",
        )
    assert exc.value.status == 403
    assert cron_api.list_jobs("agent_prof") == []


def test_create_job_rejects_malformed_expert_id(monkeypatch, tmp_path):
    _patch_profile_home(monkeypatch, tmp_path)

    with pytest.raises(cron_api.CronApiError) as exc:
        cron_api.create_job(
            "agent_prof",
            "ou_creator",
            _create_body(expert_id="bad expert!"),
        )
    assert exc.value.status == 400


def test_update_cannot_change_executor(monkeypatch, tmp_path):
    _patch_profile_home(monkeypatch, tmp_path)
    monkeypatch.setattr(expert_overlay, "resolve_expert", lambda *a, **k: _Overlay())
    monkeypatch.setattr(expert_overlay, "resolve_caller_departments", lambda *a, **k: None)

    job = cron_api.create_job(
        "agent_prof",
        "ou_creator",
        _create_body(expert_id="hr-expert"),
        agent_id="agent-1",
    )

    # Executor-only update: nothing valid to change.
    with pytest.raises(cron_api.CronApiError) as exc:
        cron_api.update_job(
            "agent_prof", job["id"], {"agent_id": "agent-2", "expert_id": "other"}
        )
    assert exc.value.status == 400

    # Mixed update: allowed field applies, executor fields are dropped.
    updated = cron_api.update_job(
        "agent_prof",
        job["id"],
        {"name": "renamed", "agent_id": "agent-2", "expert_id": "other"},
    )
    assert updated["name"] == "renamed"
    assert updated["agent_id"] == "agent-1"
    assert updated["expert_id"] == "hr-expert"


# ----------------------------------------------------------------------- wake


def test_expert_id_for_cron_job_priority(monkeypatch, tmp_path):
    from hermes_multitenancy import expert_bot_route

    monkeypatch.setattr(expert_bot_route, "fixed_expert_id_from_env", lambda: "env-expert")

    # source_app jobs keep the gateway-env derivation, ignoring any stored field.
    assert (
        run_broker_bridge._expert_id_for_cron_job(
            {"source_app": "expertapp", "expert_id": "stored-expert"}
        )
        == "env-expert"
    )
    # Non-source_app jobs use the stored (creation-gated) field, accepted only
    # after re-resolving it against the current owner/profile catalog.
    _stub_visible_catalog(monkeypatch, expert_id="stored-expert")
    assert (
        run_broker_bridge._expert_id_for_cron_job(
            {"expert_id": "stored-expert", "owner_open_id": "ou_creator"},
            profile_home=tmp_path,
        )
        == "stored-expert"
    )
    # Legacy jobs stay expert-free.
    assert run_broker_bridge._expert_id_for_cron_job({}, profile_home=tmp_path) == ""


def test_expert_id_for_cron_job_rejects_revoked_catalog(monkeypatch, tmp_path):
    """A stored WebUI expert no longer visible to the owner fails before RunRequest."""
    monkeypatch.setattr(expert_overlay, "resolve_expert", lambda *a, **k: None)
    monkeypatch.setattr(expert_overlay, "resolve_caller_departments", lambda *a, **k: None)

    with pytest.raises(ValueError):
        run_broker_bridge._expert_id_for_cron_job(
            {"expert_id": "revoked-expert", "owner_open_id": "ou_creator"},
            profile_home=tmp_path,
        )


def _executor_job(**extra):
    job = {
        "id": "abcdefabcdef",
        "name": "m0 task",
        "prompt": "do the thing",
        "deliver": "feishu",
        "enabled": True,
        "owner_open_id": "ou_creator",
        "owner_profile": "agent_prof",
    }
    job.update(extra)
    return job


def test_build_cron_run_request_metadata_carries_executor(monkeypatch, tmp_path):
    _stub_visible_catalog(monkeypatch)
    profile_home = tmp_path / "profiles" / "agent_prof"
    profile_home.mkdir(parents=True)
    request = run_broker_bridge._build_cron_run_request(
        _executor_job(agent_id="agent-1", expert_id="hr-expert"),
        profile_home=profile_home,
        prompt="p",
    )
    assert request.profile_name == "agent_prof"
    assert request.metadata["agent_id"] == "agent-1"
    assert request.metadata["expert_id"] == "hr-expert"


# --------------------------------------------------------------------- shadow

# The exact key set legacy plans have always had — plans for jobs WITHOUT an
# executor must keep it byte-identical (codex review: legacy-plan-shape-drift).
LEGACY_PLAN_KEYS = {
    "mode",
    "job_id",
    "job_name",
    "profile_name",
    "profile_home",
    "user_key",
    "credential_subject",
    "channel",
    "session_id",
    "idempotency_key",
    "deliver",
    "deliver_target",
    "next_run_at",
    "enabled",
    "due",
    "would_execute",
    "will_execute",
    "problems",
    "secret_free",
}


def test_plan_shadow_exposes_executor_identity(monkeypatch, tmp_path):
    """The SPEC Done probe: shadow plan shows agent_id/expert_id + agent profile."""
    _stub_visible_catalog(monkeypatch)
    profile_home = tmp_path / "profiles" / "agent_prof"
    profile_home.mkdir(parents=True)
    plan = run_broker_bridge.plan_cron_bridge_run(
        _executor_job(agent_id="agent-1", expert_id="hr-expert"),
        profile_home=profile_home,
    )
    assert plan["mode"] == "shadow"
    assert plan["agent_id"] == "agent-1"
    assert plan["expert_id"] == "hr-expert"
    assert plan["profile_name"] == "agent_prof"
    assert plan["would_execute"] is True
    assert plan["secret_free"] is True
    assert set(plan) == LEGACY_PLAN_KEYS | {"agent_id", "expert_id"}


def test_plan_shadow_legacy_job_unchanged(tmp_path):
    profile_home = tmp_path / "profiles" / "ownerprof"
    profile_home.mkdir(parents=True)
    plan = run_broker_bridge.plan_cron_bridge_run(
        _executor_job(owner_profile="ownerprof"),
        profile_home=profile_home,
    )
    assert set(plan) == LEGACY_PLAN_KEYS
    assert plan["profile_name"] == "ownerprof"
    assert plan["user_key"] == "ou_creator"
    assert plan["deliver"] == "feishu"
    assert plan["would_execute"] is True


def test_plan_shadow_source_app_ignores_dirty_stored_expert(monkeypatch, tmp_path):
    """A dirty stored expert_id on a source_app job must not appear in the plan.

    Execution derives the expert from the gateway env for source_app jobs; the
    plan must mirror execution, not storage (codex review:
    stored-expert-overrides-source-app-resolution).
    """
    from hermes_multitenancy import expert_bot_route

    profile_home = tmp_path / "profiles" / "expertprof"
    profile_home.mkdir(parents=True)
    job = _executor_job(
        owner_profile="expertprof", source_app="expertapp", expert_id="dirty-expert"
    )

    monkeypatch.setattr(expert_bot_route, "fixed_expert_id_from_env", lambda: "")
    plan = run_broker_bridge.plan_cron_bridge_run(job, profile_home=profile_home)
    assert "expert_id" not in plan

    monkeypatch.setattr(expert_bot_route, "fixed_expert_id_from_env", lambda: "env-expert")
    plan = run_broker_bridge.plan_cron_bridge_run(job, profile_home=profile_home)
    assert plan["expert_id"] == "env-expert"


def test_plan_shadow_construction_failure_still_execution_faithful(monkeypatch, tmp_path):
    """A FAILED plan must not report an expert execution would never use.

    Invalid owner_open_id → RunRequest construction fails → the fallback still
    derives the expert the execution way: env for source_app (dirty stored id
    suppressed), stored value for normal jobs.
    """
    from hermes_multitenancy import expert_bot_route

    profile_home = tmp_path / "profiles" / "expertprof"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(expert_bot_route, "fixed_expert_id_from_env", lambda: "")
    _stub_visible_catalog(monkeypatch)

    broken = _executor_job(
        owner_profile="expertprof",
        owner_open_id="not-a-feishu-id",
        source_app="expertapp",
        expert_id="dirty-expert",
    )
    plan = run_broker_bridge.plan_cron_bridge_run(broken, profile_home=profile_home)
    assert plan["problems"]
    assert plan["would_execute"] is False
    assert "expert_id" not in plan

    normal_broken = _executor_job(
        owner_profile="expertprof",
        owner_open_id="not-a-feishu-id",
        agent_id="agent-1",
        expert_id="hr-expert",
    )
    plan = run_broker_bridge.plan_cron_bridge_run(normal_broken, profile_home=profile_home)
    assert plan["problems"]
    assert plan["agent_id"] == "agent-1"
    assert plan["expert_id"] == "hr-expert"


# -------------------------------------------------- departments resolution


def test_resolve_caller_departments_explicit_open_id_is_exclusive(tmp_path):
    """A trusted open_id must never be shadowed by a profile_name collision.

    On a shared Agent the profile home derives the AGENT's profile name; an
    unrelated employee record matching that name must not supply the caller's
    departments (codex review: explicit-open-id-is-not-authoritative).
    """
    import json

    shared = tmp_path / "root"
    profile_home = shared / "profiles" / "agent_prof"
    profile_home.mkdir(parents=True)
    snap_dir = shared / "org-snapshots"
    snap_dir.mkdir(parents=True)
    (snap_dir / "org-1.json").write_text(
        json.dumps(
            {
                "employees": {
                    "impostor": {
                        "profile_name": "agent_prof",
                        "open_id": "ou_other",
                        "dept_id": "DX",
                        "dept_name": "X部",
                    },
                    "creator": {
                        "profile_name": "creator_prof",
                        "open_id": "ou_creator",
                        "dept_id": "DY",
                        "dept_name": "Y部",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    # Explicit open_id: only the creator's record may match.
    depts = expert_overlay.resolve_caller_departments(profile_home, open_id="ou_creator")
    assert depts == ["DY", "Y部"]
    # Unknown open_id fails closed even though the profile_name would match.
    assert expert_overlay.resolve_caller_departments(profile_home, open_id="ou_ghost") is None
    # No open_id: profile_name fallback keeps working.
    depts = expert_overlay.resolve_caller_departments(profile_home)
    assert depts == ["DX", "X部"]


# ------------------------------------------- executor agent_id derivation


class _FakeRequest:
    """Header/query duck for _owner_scoped_tenant_resolved unit tests."""

    def __init__(self, headers=None, query=None):
        self.headers = dict(headers or {})
        self.query = dict(query or {})
        self.path = "/api/run-broker/jobs"
        self.method = "POST"


def test_owner_scoped_tenant_resolved_single_snapshot(monkeypatch):
    """Authorization and executor identity come from ONE routing snapshot."""
    from types import SimpleNamespace

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker import periphery

    agent_row = SimpleNamespace(kind="agent", profile_name="agent_prof", agent_id="agent-1")
    agent_dup = SimpleNamespace(kind="agent", profile_name="agent_prof", agent_id="agent-2")
    broken_row = SimpleNamespace(kind="agent", profile_name="agent_prof", agent_id="")
    root_row = SimpleNamespace(kind="user", profile_name="rootprof", agent_id=None)

    class _Table:
        def __init__(self, *, root=root_row, owned=(), lookup=None, share_role=None):
            self._root = root
            self._owned = list(owned)
            self._lookup = lookup
            self._share_role = share_role

        def resolve_owner_root(self, open_id):
            return self._root

        def list_agents_for_owner(self, open_id):
            return list(self._owned)

        def lookup_by_profile_name(self, profile_name):
            return self._lookup

        def get_agent_share_role(self, agent_id, open_id):
            return self._share_role

        def get_agent_share_role_for_principal(self, agent_id, principal_id):
            return None

    def resolved(table, profile="agent_prof", require_write=True):
        monkeypatch.setattr(router_mod, "_get_routing_table", lambda: table)
        return periphery._owner_scoped_tenant_resolved(
            _FakeRequest(
                headers={
                    "X-Hermes-User-Key": "ou_owner",
                    "X-Hermes-Profile": profile,
                }
            ),
            require_write=require_write,
        )

    # Owned agent: agent_id from the enumerated owned row.
    assert resolved(_Table(owned=[agent_row])) == ("agent_prof", "ou_owner", "agent-1")
    # Shared agent: agent_id from the exact row the share was authorized on,
    # even when a different-looking owned enumeration is empty.
    assert resolved(_Table(lookup=agent_row, share_role="editor")) == (
        "agent_prof",
        "ou_owner",
        "agent-1",
    )
    # Owner root default (no requested profile): personal, no executor.
    monkeypatch.setattr(
        router_mod, "_get_routing_table", lambda: _Table(owned=[agent_row])
    )
    from hermes_multitenancy.webui_broker import periphery as p

    assert p._owner_scoped_tenant_resolved(
        _FakeRequest(headers={"X-Hermes-User-Key": "ou_owner"})
    ) == ("rootprof", "ou_owner", "")

    # Ambiguity and broken rows fail closed.
    with pytest.raises(PermissionError):
        resolved(_Table(owned=[agent_row, agent_dup]))
    with pytest.raises(PermissionError):
        resolved(_Table(owned=[broken_row]))
    with pytest.raises(PermissionError):
        resolved(
            _Table(
                root=SimpleNamespace(kind="user", profile_name="agent_prof", agent_id=None),
                owned=[agent_row],
            )
        )
    # A share-granted row that is not an agent (e.g. a dirty group row carrying
    # an agent_id) must never be stamped as the executor.
    group_row = SimpleNamespace(kind="group", profile_name="agent_prof", agent_id="group-1")
    with pytest.raises(PermissionError):
        resolved(_Table(lookup=group_row, share_role="editor"))
    shared_broken = SimpleNamespace(kind="agent", profile_name="agent_prof", agent_id="  ")
    with pytest.raises(PermissionError):
        resolved(_Table(lookup=shared_broken, share_role="editor"))


# ------------------------------------------------------- broker write gates


def _seed_agent_routing(db_path: Path):
    from hermes_multitenancy.routing import RoutingTable

    table = RoutingTable(db_path)
    table.upsert(
        user_id="owner-sync",
        profile_name="owner_sync_profile",
        open_id="ou_owner",
        provenance="sync",
    )
    table.upsert(
        user_id="viewer-sync",
        profile_name="viewer_sync_profile",
        open_id="ou_viewer",
        provenance="sync",
    )
    table.upsert(
        user_id="editor-sync",
        profile_name="editor_sync_profile",
        open_id="ou_editor",
        provenance="sync",
    )
    table.upsert_owned_agent(
        agent_id="agent-1",
        profile_name="agent_prof",
        owner_open_id="ou_owner",
    )
    table.grant_agent_share(
        agent_id="agent-1",
        grantee_open_id="ou_viewer",
        role="viewer",
        created_by_open_id="ou_owner",
    )
    table.grant_agent_share(
        agent_id="agent-1",
        grantee_open_id="ou_editor",
        role="editor",
        created_by_open_id="ou_owner",
    )
    principal_ids = {}
    for key, role in (("pviewer", "viewer"), ("peditor", "editor"), ("pconflict", "viewer")):
        principal = table.upsert_principal(
            provider="feishu",
            tenant_key="tenant-1",
            canonical_id_type="user_id",
            canonical_id=f"user-{key}",
            display_name=key,
            avatar_url=None,
            email=None,
            aliases=[],
        )
        table.grant_agent_share_principal(
            agent_id="agent-1",
            grantee_principal_id=principal.principal_id,
            role=role,
            created_by_open_id="ou_owner",
        )
        principal_ids[key] = principal.principal_id
    # Conflicting grants: principal says viewer, a stale open-id grant says
    # editor. The principal is canonical and must win (deny write).
    table.upsert(
        user_id="conflict-sync",
        profile_name="conflict_sync_profile",
        open_id="ou_pconflict",
        provenance="sync",
    )
    table.grant_agent_share(
        agent_id="agent-1",
        grantee_open_id="ou_pconflict",
        role="editor",
        created_by_open_id="ou_owner",
    )
    table.close()
    return principal_ids


def test_jobs_write_gate_and_agent_id_derivation(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.webui_broker_server import create_run_broker_app

    db_path = tmp_path / "routing.db"
    principal_ids = _seed_agent_routing(db_path)

    create_calls: list[tuple[str, str, str]] = []

    def sentinel_create(profile_name, user_key, body, *, agent_id=""):
        create_calls.append((profile_name, user_key, agent_id))
        return {"id": "job-1", "agent_id": agent_id}

    monkeypatch.setattr(cron_api, "create_job", sentinel_create)
    monkeypatch.setattr(cron_api, "list_jobs", lambda *a, **k: [])

    body = {"name": "x", "schedule": "*/5 * * * *", "prompt": "p"}

    async def runner():
        router_mod.override_routing_table(db_path)
        try:
            app = create_run_broker_app(
                mark_seen=lambda _request: True,
                sandbox_available=lambda: True,
            )
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                # viewer share: read allowed, write blocked on the BFF/master path.
                viewer_read = await client.get(
                    "/api/run-broker/jobs",
                    headers={
                        "X-Hermes-User-Key": "ou_viewer",
                        "X-Hermes-Profile": "agent_prof",
                    },
                )
                viewer_write = await client.post(
                    "/api/run-broker/jobs",
                    json=body,
                    headers={
                        "X-Hermes-User-Key": "ou_viewer",
                        "X-Hermes-Profile": "agent_prof",
                    },
                )
                # editor share may create; agent_id is derived server-side.
                editor_write = await client.post(
                    "/api/run-broker/jobs",
                    json=body,
                    headers={
                        "X-Hermes-User-Key": "ou_editor",
                        "X-Hermes-Profile": "agent_prof",
                    },
                )
                # owner on the agent profile also derives agent_id.
                owner_write = await client.post(
                    "/api/run-broker/jobs",
                    json=body,
                    headers={
                        "X-Hermes-User-Key": "ou_owner",
                        "X-Hermes-Profile": "agent_prof",
                    },
                )
                # owner's own sync profile: no agent row → empty agent_id.
                root_write = await client.post(
                    "/api/run-broker/jobs",
                    json=body,
                    headers={
                        "X-Hermes-User-Key": "ou_owner",
                        "X-Hermes-Profile": "owner_sync_profile",
                    },
                )
                # Principal-granted shares are as authoritative as open-id
                # shares (codex review: principal-shares-ignored).
                pviewer_read = await client.get(
                    "/api/run-broker/jobs",
                    headers={
                        "X-Hermes-User-Key": "ou_pviewer",
                        "X-Hermes-Profile": "agent_prof",
                        "X-Hermes-Actor-Principal-Id": principal_ids["pviewer"],
                    },
                )
                pviewer_write = await client.post(
                    "/api/run-broker/jobs",
                    json=body,
                    headers={
                        "X-Hermes-User-Key": "ou_pviewer",
                        "X-Hermes-Profile": "agent_prof",
                        "X-Hermes-Actor-Principal-Id": principal_ids["pviewer"],
                    },
                )
                peditor_write = await client.post(
                    "/api/run-broker/jobs",
                    json=body,
                    headers={
                        "X-Hermes-User-Key": "ou_peditor",
                        "X-Hermes-Profile": "agent_prof",
                        "X-Hermes-Actor-Principal-Id": principal_ids["peditor"],
                    },
                )
                # Conflicting grants: canonical principal=viewer beats a stale
                # open-id editor grant → write denied.
                pconflict_write = await client.post(
                    "/api/run-broker/jobs",
                    json=body,
                    headers={
                        "X-Hermes-User-Key": "ou_pconflict",
                        "X-Hermes-Profile": "agent_prof",
                        "X-Hermes-Actor-Principal-Id": principal_ids["pconflict"],
                    },
                )
                return (
                    viewer_read.status,
                    viewer_write.status,
                    editor_write.status,
                    owner_write.status,
                    root_write.status,
                    pviewer_read.status,
                    pviewer_write.status,
                    peditor_write.status,
                    pconflict_write.status,
                )
            finally:
                await client.close()
        finally:
            router_mod.override_routing_table(None)

    statuses = asyncio.run(runner())
    assert statuses == (200, 403, 200, 200, 200, 200, 403, 200, 403)
    assert create_calls == [
        ("agent_prof", "ou_editor", "agent-1"),
        ("agent_prof", "ou_owner", "agent-1"),
        ("owner_sync_profile", "ou_owner", ""),
        ("agent_prof", "ou_peditor", "agent-1"),
    ]

# ------------------------------------------------- per-job model updates


def test_update_can_move_an_existing_job_to_another_model(monkeypatch, tmp_path):
    """The point of per-job models: an EXISTING expensive job can be made cheap."""
    _patch_profile_home(monkeypatch, tmp_path)
    job = cron_api.create_job("ownerprof", "ou_creator", _create_body())

    updated = cron_api.update_job("ownerprof", job["id"], {"model": "cheap-model"})
    assert updated["model"] == "cheap-model"
    assert cron_api.get_job("ownerprof", job["id"])["model"] == "cheap-model"

    # …and the wake path carries it into the RunRequest — fed by the PERSISTED
    # job, so a shape/key drift between update and wake cannot hide here.
    _stub_visible_catalog(monkeypatch)
    profile_home = tmp_path / "profiles" / "ownerprof"
    profile_home.mkdir(parents=True, exist_ok=True)
    persisted = dict(cron_api.get_job("ownerprof", job["id"]))
    persisted.update(owner_open_id="ou_creator", owner_profile="ownerprof")
    request = run_broker_bridge._build_cron_run_request(
        persisted, profile_home=profile_home, prompt="p",
    )
    assert request.metadata["model"] == "cheap-model"


def test_update_with_empty_model_clears_it_back_to_the_profile_default(monkeypatch, tmp_path):
    _patch_profile_home(monkeypatch, tmp_path)
    job = cron_api.create_job("ownerprof", "ou_creator", _create_body())
    cron_api.update_job("ownerprof", job["id"], {"model": "pricey"})
    assert cron_api.get_job("ownerprof", job["id"])["model"] == "pricey"

    cron_api.update_job("ownerprof", job["id"], {"model": ""})
    assert not (cron_api.get_job("ownerprof", job["id"]).get("model") or "")

    # An empty model must not reach RunRequest metadata at all — and the paired
    # provider must be gone too, or the next model-only set re-poisons routing.
    _stub_visible_catalog(monkeypatch)
    profile_home = tmp_path / "profiles" / "ownerprof"
    profile_home.mkdir(parents=True, exist_ok=True)
    persisted = dict(cron_api.get_job("ownerprof", job["id"]))
    persisted.update(owner_open_id="ou_creator", owner_profile="ownerprof")
    request = run_broker_bridge._build_cron_run_request(
        persisted, profile_home=profile_home, prompt="p",
    )
    assert "model" not in request.metadata
    assert "provider" not in request.metadata


def test_model_only_update_drops_a_stale_provider(monkeypatch, tmp_path):
    """model+provider are ONE routing decision: a model-only PATCH (the WebUI
    path) must not leave the old provider prefixing the new model."""
    _patch_profile_home(monkeypatch, tmp_path)
    job = cron_api.create_job("ownerprof", "ou_creator", _create_body())
    cron_api.update_job("ownerprof", job["id"], {"model": "old-model", "provider": "openai"})
    assert cron_api.get_job("ownerprof", job["id"])["provider"] == "openai"

    cron_api.update_job("ownerprof", job["id"], {"model": "cheap-model"})
    stored = cron_api.get_job("ownerprof", job["id"])
    assert stored["model"] == "cheap-model"
    assert not (stored.get("provider") or "")

    # Both together still stick.
    cron_api.update_job("ownerprof", job["id"], {"model": "m2", "provider": "custom:litellm"})
    stored = cron_api.get_job("ownerprof", job["id"])
    assert (stored["model"], stored["provider"]) == ("m2", "custom:litellm")


def test_update_without_model_leaves_it_untouched(monkeypatch, tmp_path):
    _patch_profile_home(monkeypatch, tmp_path)
    job = cron_api.create_job("ownerprof", "ou_creator", _create_body())
    cron_api.update_job("ownerprof", job["id"], {"model": "keep-me"})

    updated = cron_api.update_job("ownerprof", job["id"], {"name": "renamed"})
    assert updated["name"] == "renamed"
    assert updated["model"] == "keep-me"


def test_update_still_cannot_change_the_executor_even_with_model_allowed(monkeypatch, tmp_path):
    """Widening the whitelist must not weaken the executor-immutability ruling."""
    _patch_profile_home(monkeypatch, tmp_path)
    monkeypatch.setattr(expert_overlay, "resolve_expert", lambda *a, **k: _Overlay())
    monkeypatch.setattr(expert_overlay, "resolve_caller_departments", lambda *a, **k: None)
    job = cron_api.create_job(
        "agent_prof", "ou_creator", _create_body(expert_id="hr-expert"), agent_id="agent-1"
    )

    updated = cron_api.update_job(
        "agent_prof", job["id"], {"model": "cheap", "agent_id": "agent-2", "expert_id": "other"}
    )
    assert updated["model"] == "cheap"
    assert updated["agent_id"] == "agent-1"
    assert updated["expert_id"] == "hr-expert"

