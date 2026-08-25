"""GitLab credential delegation — three segments per SPEC credential-delegation-jit.

  1. TRIGGER   — group profile + missing GitLab credential → auth_required
                 delegation branch (marker from the child tool, router branch).
  2. GRANT     — card click by the initiator → lease row → run-level env
                 injection → synthetic replay of the original request.
  3. ISOLATION — B never reuses A's lease; the shared profile keeps zero token
                 residue on disk/vault; every borrow is audited; `once` dies on
                 first use; expired leases are dead at take time.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from hermes_multitenancy import credential_delegation as leases


GROUP = "feishu_group_abc123"
OWNER = "alice"
OWNER_OPEN_ID = "ou_alice"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture()
def shared_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A shared hermes home with a personal GitLab token for alice in the vault
    and a __self__-lane materialization entry (the production shape)."""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / "hermes"
    (shared / "profiles" / GROUP / "tmp").mkdir(parents=True)
    (shared / "profiles" / OWNER).mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        yaml.safe_dump(
            {
                "credentials": [
                    {
                        "provider": "gitlab",
                        "subject_id": "kep-prd-skills",
                        "secret_kind": "token",
                        "vault_profile": "__self__",
                        "env": "GITLAB_TOKEN",
                        "env_extra": {"GITLAB_HOST": "gitlab.example.com"},
                        "target": "workspace/credentials/gitlab.token",
                        "profiles": [OWNER],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(shared / "multitenancy.db")
    try:
        store.put_credential(
            profile_name=OWNER,
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "glpat-alice-personal"},
            scopes=["api"],
            expires_at=None,
        )
    finally:
        store.close()
    # Authoritative routing for the initiator. Production ALWAYS has this row
    # (the delegation card is only built after it resolves), and the borrow path
    # is fail-closed on it — an unresolvable owner refuses the borrow.
    with sqlite3.connect(str(shared / "multitenancy.db")) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS multitenancy_routing "
            "(open_id TEXT, active INT, kind TEXT, profile_name TEXT, updated_at INT)"
        )
        conn.execute(
            "INSERT INTO multitenancy_routing VALUES (?,1,'user',?,0)",
            (OWNER_OPEN_ID, OWNER),
        )
    return shared


def _db(shared: Path) -> Path:
    return shared / "multitenancy.db"


def _audit_rows(shared: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(str(_db(shared))) as conn:
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM multitenancy_credential_lease_audit ORDER BY id"
            ).fetchall()
        except sqlite3.OperationalError:
            return []


# --------------------------------------------------------------------------- #
# Segment 1 — TRIGGER
# --------------------------------------------------------------------------- #

def _tool_status(monkeypatch, *, profile: str, home: Path, has_credential: bool) -> dict:
    from hermes_multitenancy import credential_tool

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_SHARED_HOME", raising=False)
    monkeypatch.setattr(
        credential_tool,
        "_provider_adapter_status",
        lambda provider: {
            "subject_id": "kep-prd-skills",
            "status": "valid" if has_credential else "missing",
            "storage": "env",
            "expires_at": None,
            "scopes": ["api"],
            "missing_scopes": [],
            "has_credential": has_credential,
            "selected_source": "profile_env" if has_credential else None,
            "source_profile": profile if has_credential else None,
            "env": "GITLAB_TOKEN",
        },
    )
    return json.loads(
        credential_tool.credential_status(
            {"provider": "gitlab", "credential_kind": "token"}
        )
    )


def test_trigger_group_profile_missing_gitlab_writes_marker(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / GROUP
    home.mkdir(parents=True)
    payload = _tool_status(monkeypatch, profile=GROUP, home=home, has_credential=False)
    assert payload["profile"] == GROUP
    assert payload["has_credential"] is False
    assert "委托授权卡" in payload["delegation"]
    marker = leases.take_auth_required_marker(home, since=time.time() - 60)
    assert marker is not None
    assert marker["provider"] == "gitlab"
    assert marker["reason"] == "missing_credential"
    assert marker["profile"] == GROUP
    # take is destructive — a second read finds nothing (no stale re-trigger).
    assert leases.take_auth_required_marker(home, since=0) is None


def test_trigger_personal_profile_missing_gitlab_is_unchanged(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / OWNER
    home.mkdir(parents=True)
    payload = _tool_status(monkeypatch, profile=OWNER, home=home, has_credential=False)
    assert "delegation" not in payload
    assert leases.take_auth_required_marker(home, since=0) is None


def test_trigger_group_profile_with_credential_stays_silent(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / GROUP
    home.mkdir(parents=True)
    payload = _tool_status(monkeypatch, profile=GROUP, home=home, has_credential=True)
    assert "delegation" not in payload
    assert leases.take_auth_required_marker(home, since=0) is None


def test_marker_freshness_gate_rejects_stale_marker(tmp_path):
    home = tmp_path / GROUP
    leases.write_auth_required_marker(home, {"provider": "gitlab"})
    assert leases.take_auth_required_marker(home, since=time.time() + 60) is None


@pytest.mark.asyncio
async def test_router_gitlab_payload_routes_group_to_delegation_not_uat(monkeypatch):
    from hermes_multitenancy import feishu_cred_delegation, feishu_uat_auth, router

    def _boom(**_kw):
        raise AssertionError("gitlab payload must never start a Feishu UAT session")

    monkeypatch.setattr(feishu_uat_auth, "start_session", _boom)
    monkeypatch.setattr(feishu_uat_auth, "find_active_session", _boom)

    called: dict[str, Any] = {}

    async def fake_delegation(**kwargs):
        called.update(kwargs)

    monkeypatch.setattr(
        feishu_cred_delegation, "handle_gitlab_delegation_required", fake_delegation
    )

    event = SimpleNamespace(
        sender_open_id=OWNER_OPEN_ID, source=SimpleNamespace(user_id=OWNER_OPEN_ID)
    )
    adapter = SimpleNamespace(send=lambda *a, **k: None)

    await router._handle_jit_auth_required(
        gateway=SimpleNamespace(), adapter=adapter, chat_id="oc_group",
        profile_name=GROUP, profile_home=None, event=event,
        payload={"provider": "gitlab", "reason": "missing_credential"},
    )
    assert called["profile_name"] == GROUP
    assert called["chat_id"] == "oc_group"

    # Personal profile + gitlab payload: NO delegation, NO UAT — zero change.
    called.clear()
    await router._handle_jit_auth_required(
        gateway=SimpleNamespace(), adapter=adapter, chat_id="oc_dm",
        profile_name=OWNER, profile_home=None, event=event,
        payload={"provider": "gitlab"},
    )
    assert called == {}


@pytest.mark.asyncio
async def test_delegation_card_dm_to_initiator(monkeypatch, shared_home):
    from hermes_multitenancy import feishu_auth_cards, feishu_cred_delegation as fcd

    fcd._reset_pending_for_tests()
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    monkeypatch.setattr(
        leases, "owner_profile_for_open_id", lambda _db, _oid: OWNER
    )

    sent: dict[str, Any] = {}

    async def fake_send_auth_card(*, adapter, chat_id, card, metadata=None):
        sent["chat_id"] = chat_id
        sent["card"] = card
        return {"transport": "interactive", "message_id": "om_card", "sequence": 0}

    monkeypatch.setattr(feishu_auth_cards, "send_auth_card", fake_send_auth_card)

    event = SimpleNamespace(
        sender_open_id=OWNER_OPEN_ID,
        source=SimpleNamespace(user_id=OWNER_OPEN_ID),
        text="帮我看下 gitlab 上这个 MR",
    )
    adapter = SimpleNamespace(send=lambda *a, **k: None, _loop=None)

    await fcd.handle_gitlab_delegation_required(
        gateway=SimpleNamespace(), adapter=adapter, chat_id="oc_group",
        profile_name=GROUP, event=event, payload={"provider": "gitlab"},
    )

    # Card went to the INITIATOR's DM (open_id target), not the group.
    assert sent["chat_id"] == OWNER_OPEN_ID
    values = _button_values(sent["card"])
    assert {v["choice"] for v in values} == {"allow_once", "allow_chat", "deny"}
    assert all(v["action"] == "cred_delegation" for v in values)
    # Identity never rides in the button payload.
    assert all(
        set(v) == {"action", "choice", "delegation_id"} for v in values
    )
    assert fcd._has_pending_for(OWNER_OPEN_ID, GROUP)
    fcd._reset_pending_for_tests()


@pytest.mark.asyncio
async def test_no_personal_token_sends_bind_first_notice_not_card(
    monkeypatch, shared_home
):
    from hermes_multitenancy import feishu_auth_cards, feishu_cred_delegation as fcd

    fcd._reset_pending_for_tests()
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    monkeypatch.setattr(leases, "owner_profile_for_open_id", lambda _db, _oid: "bob")

    def _no_card(**_kw):
        raise AssertionError("no delegation card when the owner has no token")

    monkeypatch.setattr(feishu_auth_cards, "send_auth_card", _no_card)

    dm: list[tuple[str, str]] = []

    async def send(chat_id, text):
        dm.append((chat_id, text))

    adapter = SimpleNamespace(send=send, _loop=None)
    event = SimpleNamespace(
        sender_open_id="ou_bob", source=SimpleNamespace(user_id="ou_bob"), text="x"
    )
    await fcd.handle_gitlab_delegation_required(
        gateway=SimpleNamespace(), adapter=adapter, chat_id="oc_group",
        profile_name=GROUP, event=event,
    )
    assert dm and dm[0][0] == "ou_bob" and "/auth" in dm[0][1]
    assert not fcd._has_pending_for("ou_bob", GROUP)


def _button_values(card: dict) -> list[dict]:
    values = []
    for element in card["body"]["elements"]:
        if element.get("tag") == "column_set":
            for column in element["columns"]:
                for child in column["elements"]:
                    if child.get("tag") == "button":
                        values.append(child["value"])
    return values


# --------------------------------------------------------------------------- #
# Segment 2 — GRANT: click → lease → env injection → replay
# --------------------------------------------------------------------------- #

def _cb(value: Any, operator: str = OWNER_OPEN_ID) -> Any:
    from hermes_multitenancy.feishu_card_action_dispatcher import parse_card_callback

    event = SimpleNamespace(
        action=SimpleNamespace(tag="button", name="btn", value=value, form_value=None),
        operator=SimpleNamespace(open_id=operator, union_id=None, user_id=None),
        context=SimpleNamespace(open_chat_id="oc_dm", open_message_id="om_1"),
    )
    return parse_card_callback(SimpleNamespace(event=event))


def _pending(fcd, adapter, *, replay_text="继续 gitlab 操作") -> Any:
    entry = fcd._Pending(
        delegation_id="dg-test-1",
        owner_open_id=OWNER_OPEN_ID,
        owner_profile=OWNER,
        borrower_profile=GROUP,
        group_chat_id="oc_group",
        replay_text=replay_text,
        gateway=SimpleNamespace(),
        event=SimpleNamespace(sender_open_id=OWNER_OPEN_ID, source=None, text=replay_text),
        adapter=adapter,
        card_state={"transport": "interactive", "message_id": "om_card", "sequence": 0},
    )
    fcd._register_pending(entry)
    return entry


def test_allow_once_creates_lease_and_schedules_replay(monkeypatch, shared_home):
    from hermes_multitenancy import feishu_cred_delegation as fcd

    fcd._reset_pending_for_tests()
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    adapter = SimpleNamespace(send=lambda *a, **k: None, _loop=None)
    _pending(fcd, adapter)

    scheduled: list[Any] = []
    monkeypatch.setattr(fcd, "_schedule", lambda _a, coro: scheduled.append(coro))

    replayed: dict[str, Any] = {}

    async def fake_replay(**kwargs):
        replayed.update(kwargs)

    monkeypatch.setattr(fcd, "_dispatch_replay", fake_replay)

    response = fcd.handle_delegation_card_action(
        adapter,
        _cb({"action": "cred_delegation", "choice": "allow_once", "delegation_id": "dg-test-1"}),
    )
    # Lease row landed with every field filled.
    lease = leases.find_active_lease(
        _db(shared_home), owner_open_id=OWNER_OPEN_ID, borrower_profile=GROUP
    )
    assert lease is not None
    assert {
        "owner_profile": lease["owner_profile"],
        "owner_open_id": lease["owner_open_id"],
        "borrower_profile": lease["borrower_profile"],
        "resource_type": lease["resource_type"],
        "provider": lease["provider"],
        "scope": lease["scope"],
        "chat_id": lease["chat_id"],
        "status": lease["status"],
        "use_count": lease["use_count"],
    } == {
        "owner_profile": OWNER,
        "owner_open_id": OWNER_OPEN_ID,
        "borrower_profile": GROUP,
        "resource_type": "connector",
        "provider": "gitlab",
        "scope": "once",
        "chat_id": "oc_group",
        "status": "active",
        "use_count": 0,
    }
    assert lease["expires_at"] is not None
    # Replay was scheduled with the ORIGINAL request bound to the group chat.
    assert len(scheduled) == 1
    asyncio.run(scheduled[0])
    assert replayed["chat_id"] == "oc_group"
    assert replayed["profile_name"] == GROUP
    assert replayed["open_id"] == OWNER_OPEN_ID
    assert replayed["text"] == "继续 gitlab 操作"
    # The card flipped to a granted state, and pending is gone (click-once).
    assert response is not None
    assert not fcd._has_pending_for(OWNER_OPEN_ID, GROUP)
    granted = [row for row in _audit_rows(shared_home) if row["action"] == "granted"]
    assert len(granted) == 1 and granted[0]["borrower_profile"] == GROUP


def test_deny_leaves_no_lease_and_notifies_group(monkeypatch, shared_home):
    from hermes_multitenancy import feishu_cred_delegation as fcd

    fcd._reset_pending_for_tests()
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    adapter = SimpleNamespace(send=lambda *a, **k: None, _loop=None)
    _pending(fcd, adapter)
    scheduled: list[Any] = []
    monkeypatch.setattr(fcd, "_schedule", lambda _a, coro: scheduled.append(coro))

    fcd.handle_delegation_card_action(
        adapter,
        _cb({"action": "cred_delegation", "choice": "deny", "delegation_id": "dg-test-1"}),
    )
    assert (
        leases.find_active_lease(
            _db(shared_home), owner_open_id=OWNER_OPEN_ID, borrower_profile=GROUP
        )
        is None
    )
    denied = [row for row in _audit_rows(shared_home) if row["action"] == "denied"]
    assert len(denied) == 1
    # Group notice coroutine was scheduled (friendly termination).
    assert len(scheduled) == 1
    for coro in scheduled:
        coro.close()


def test_operator_mismatch_is_rejected_without_lease(monkeypatch, shared_home):
    """Anti-spoof: a leaked card clicked by a stranger must not grant."""
    from hermes_multitenancy import feishu_cred_delegation as fcd

    fcd._reset_pending_for_tests()
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    adapter = SimpleNamespace(send=lambda *a, **k: None, _loop=None)
    _pending(fcd, adapter)

    response = fcd.handle_delegation_card_action(
        adapter,
        _cb(
            {"action": "cred_delegation", "choice": "allow_chat", "delegation_id": "dg-test-1"},
            operator="ou_mallory",
        ),
    )
    assert (
        leases.find_active_lease(
            _db(shared_home), owner_open_id=OWNER_OPEN_ID, borrower_profile=GROUP
        )
        is None
    )
    assert (
        leases.find_active_lease(
            _db(shared_home), owner_open_id="ou_mallory", borrower_profile=GROUP
        )
        is None
    )
    # Pending survives — the real initiator can still answer.
    assert fcd._has_pending_for(OWNER_OPEN_ID, GROUP)
    mismatch = [row for row in _audit_rows(shared_home) if row["action"] == "denied"]
    assert len(mismatch) == 1 and "ou_mallory" in mismatch[0]["detail"]
    assert response is not None
    fcd._reset_pending_for_tests()


def test_unknown_or_expired_delegation_click_fails_closed(monkeypatch, shared_home):
    from hermes_multitenancy import feishu_cred_delegation as fcd

    fcd._reset_pending_for_tests()
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    adapter = SimpleNamespace(send=lambda *a, **k: None, _loop=None)
    fcd.handle_delegation_card_action(
        adapter,
        _cb({"action": "cred_delegation", "choice": "allow_once", "delegation_id": "dg-nope"}),
    )
    assert (
        leases.find_active_lease(
            _db(shared_home), owner_open_id=OWNER_OPEN_ID, borrower_profile=GROUP
        )
        is None
    )


def test_env_injection_from_lease_and_once_consumption(shared_home):
    profile_home = shared_home / "profiles" / GROUP
    leases.create_lease(
        _db(shared_home),
        owner_profile=OWNER,
        owner_open_id=OWNER_OPEN_ID,
        borrower_profile=GROUP,
        scope="once",
        chat_id="oc_group",
        delegation_id="dg-inject",
    )
    env = leases.delegation_env_for_run(
        profile_home, sender_open_id=OWNER_OPEN_ID, delegation_id="dg-inject"
    )
    assert env == {
        "GITLAB_TOKEN": "glpat-alice-personal",
        "GITLAB_HOST": "gitlab.example.com",
    }
    used = [row for row in _audit_rows(shared_home) if row["action"] == "used"]
    assert len(used) == 1
    # `once` died on first use — the next run of the same sender gets nothing.
    assert leases.delegation_env_for_run(
        profile_home, sender_open_id=OWNER_OPEN_ID, delegation_id="dg-inject"
    ) == {}


def test_chat_scope_lease_survives_multiple_uses(shared_home):
    profile_home = shared_home / "profiles" / GROUP
    leases.create_lease(
        _db(shared_home),
        owner_profile=OWNER,
        owner_open_id=OWNER_OPEN_ID,
        borrower_profile=GROUP,
        scope="chat",
        chat_id="oc_group",
    )
    for _ in range(2):
        env = leases.delegation_env_for_run(profile_home, sender_open_id=OWNER_OPEN_ID)
        assert env["GITLAB_TOKEN"] == "glpat-alice-personal"
    assert len([r for r in _audit_rows(shared_home) if r["action"] == "used"]) == 2
    # Revocation kills it.
    assert leases.revoke_chat_leases(
        _db(shared_home), owner_open_id=OWNER_OPEN_ID, borrower_profile=GROUP
    ) == 1
    assert leases.delegation_env_for_run(profile_home, sender_open_id=OWNER_OPEN_ID) == {}


def test_build_subprocess_env_wires_delegation_and_forced_mirror(
    monkeypatch, shared_home
):
    """The run env builder injects the leased token + its terminal force-mirror,
    keyed to the run's own sender; no lease → no key at all."""
    from hermes_multitenancy import agent_real, credential_delegation as cd

    profile_home = shared_home / "profiles" / GROUP
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))

    calls: list[str] = []

    def fake_delegation(ph, *, sender_open_id, delegation_id="", existing_env_names=()):
        calls.append(sender_open_id)
        if sender_open_id == OWNER_OPEN_ID:
            return {"GITLAB_TOKEN": "glpat-alice-personal", "GITLAB_HOST": "gitlab.example.com"}
        return {}

    monkeypatch.setattr(cd, "delegation_env_for_run", fake_delegation)

    env = agent_real._build_subprocess_env(
        profile_home,
        approval_dir=shared_home / "approvals",
        extra={"HERMES_FEISHU_USER_OPEN_ID": OWNER_OPEN_ID},
    )
    assert env["GITLAB_TOKEN"] == "glpat-alice-personal"
    assert env["_HERMES_FORCE_GITLAB_TOKEN"] == "glpat-alice-personal"
    assert env["GITLAB_HOST"] == "gitlab.example.com"
    assert calls == [OWNER_OPEN_ID]

    env_b = agent_real._build_subprocess_env(
        profile_home,
        approval_dir=shared_home / "approvals",
        extra={"HERMES_FEISHU_USER_OPEN_ID": "ou_bob"},
    )
    assert "GITLAB_TOKEN" not in env_b
    assert "_HERMES_FORCE_GITLAB_TOKEN" not in env_b


# --------------------------------------------------------------------------- #
# Segment 3 — ISOLATION & AUDIT
# --------------------------------------------------------------------------- #

def test_b_never_reuses_a_lease(shared_home):
    profile_home = shared_home / "profiles" / GROUP
    leases.create_lease(
        _db(shared_home),
        owner_profile=OWNER,
        owner_open_id=OWNER_OPEN_ID,
        borrower_profile=GROUP,
        scope="chat",
        chat_id="oc_group",
    )
    assert leases.delegation_env_for_run(profile_home, sender_open_id="ou_bob") == {}
    # And A's lease stays untouched by B's attempt.
    assert (
        leases.find_active_lease(
            _db(shared_home), owner_open_id=OWNER_OPEN_ID, borrower_profile=GROUP
        )
        is not None
    )


def test_lease_is_group_profile_scoped(shared_home):
    """A lease for group X must not leak into group Y (or a personal profile)."""
    leases.create_lease(
        _db(shared_home),
        owner_profile=OWNER,
        owner_open_id=OWNER_OPEN_ID,
        borrower_profile=GROUP,
        scope="chat",
        chat_id="oc_group",
    )
    other = shared_home / "profiles" / "feishu_group_other"
    (other / "tmp").mkdir(parents=True)
    assert leases.delegation_env_for_run(other, sender_open_id=OWNER_OPEN_ID) == {}
    personal = shared_home / "profiles" / OWNER
    assert leases.delegation_env_for_run(personal, sender_open_id=OWNER_OPEN_ID) == {}


def test_expired_lease_is_dead_at_take_time(shared_home):
    profile_home = shared_home / "profiles" / GROUP
    lease_id = leases.create_lease(
        _db(shared_home),
        owner_profile=OWNER,
        owner_open_id=OWNER_OPEN_ID,
        borrower_profile=GROUP,
        scope="once",
        chat_id="oc_group",
        delegation_id="dg-expired",
    )
    with sqlite3.connect(str(_db(shared_home))) as conn:
        conn.execute(
            "UPDATE multitenancy_credential_leases SET expires_at=? WHERE id=?",
            (int(time.time() * 1000) - 1000, lease_id),
        )
    assert leases.delegation_env_for_run(
        profile_home, sender_open_id=OWNER_OPEN_ID, delegation_id="dg-expired"
    ) == {}
    with sqlite3.connect(str(_db(shared_home))) as conn:
        status = conn.execute(
            "SELECT status FROM multitenancy_credential_leases WHERE id=?", (lease_id,)
        ).fetchone()[0]
    assert status == "expired"
    assert [r["action"] for r in _audit_rows(shared_home)][-1] == "expired"


def test_shared_fallback_token_is_never_laundered(shared_home, monkeypatch):
    """Owner whose vault holds ONLY the shared record gets no delegation env."""
    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(_db(shared_home))
    try:
        store.put_credential(
            profile_name="__shared__",
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "glpat-SHARED"},
            scopes=["api"],
            expires_at=None,
        )
        store.delete_credential(
            profile_name=OWNER,
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
        )
    finally:
        store.close()
    assert leases.owner_gitlab_env(shared_home, OWNER) == {}
    profile_home = shared_home / "profiles" / GROUP
    leases.create_lease(
        _db(shared_home),
        owner_profile=OWNER,
        owner_open_id=OWNER_OPEN_ID,
        borrower_profile=GROUP,
        scope="chat",
        chat_id="oc_group",
    )
    assert leases.delegation_env_for_run(profile_home, sender_open_id=OWNER_OPEN_ID) == {}


def test_no_token_residue_in_shared_profile_after_injection(shared_home):
    """Read-back per the SPEC Done line: after a delegated injection the GROUP
    profile carries no trace of the owner's token — not in config/, not in
    workspace/credentials/, not as a vault row."""
    profile_home = shared_home / "profiles" / GROUP
    (profile_home / "config").mkdir(parents=True, exist_ok=True)
    (profile_home / "workspace" / "credentials").mkdir(parents=True, exist_ok=True)
    leases.create_lease(
        _db(shared_home),
        owner_profile=OWNER,
        owner_open_id=OWNER_OPEN_ID,
        borrower_profile=GROUP,
        scope="once",
        chat_id="oc_group",
        delegation_id="dg-residue",
    )
    env = leases.delegation_env_for_run(
        profile_home, sender_open_id=OWNER_OPEN_ID, delegation_id="dg-residue"
    )
    assert env["GITLAB_TOKEN"] == "glpat-alice-personal"

    for path in profile_home.rglob("*"):
        if path.is_file():
            assert "glpat-alice-personal" not in path.read_text(
                encoding="utf-8", errors="ignore"
            ), f"token leaked into {path}"

    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(_db(shared_home))
    try:
        status = store.get_status(
            profile_name=GROUP,
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
        )
    finally:
        store.close()
    assert status["status"] == "missing"


def test_owner_profile_lookup_uses_active_user_routing(tmp_path):
    db = tmp_path / "multitenancy.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE multitenancy_routing "
            "(open_id TEXT, active INT, kind TEXT, profile_name TEXT, updated_at INT)"
        )
        conn.executemany(
            "INSERT INTO multitenancy_routing VALUES (?,?,?,?,?)",
            [
                (OWNER_OPEN_ID, 1, "user", OWNER, 2),
                (OWNER_OPEN_ID, 0, "user", "alice-old", 1),
                ("ou_bob", 1, "group", "not-a-user-row", 3),
            ],
        )
    assert leases.owner_profile_for_open_id(db, OWNER_OPEN_ID) == OWNER
    assert leases.owner_profile_for_open_id(db, "ou_bob") is None
    assert leases.owner_profile_for_open_id(db, "oc_chat") is None


# --------------------------------------------------------------------------- #
# Dispatcher wiring — two-sided negative controls (fake adapter both ways)
# --------------------------------------------------------------------------- #

def _dispatcher_adapter():
    calls = {"original": 0}

    class FakeFeishuAdapter:
        _app_id = "cli_test"
        _loop = None

        def _on_card_action_trigger(self, data):
            calls["original"] += 1
            return {"kind": "delegated"}

    from hermes_multitenancy import feishu_card_action_dispatcher as dispatcher

    assert dispatcher.install_feishu_card_action_dispatcher(FakeFeishuAdapter) is True
    return FakeFeishuAdapter(), calls


def _dispatch_data(value):
    event = SimpleNamespace(
        action=SimpleNamespace(tag="button", name="btn", value=value, form_value=None),
        operator=SimpleNamespace(open_id=OWNER_OPEN_ID, union_id=None, user_id=None),
        context=SimpleNamespace(open_chat_id="oc_dm", open_message_id="om_x"),
        token=f"tok-{time.time_ns()}",
    )
    return SimpleNamespace(event=event)


def test_dispatcher_routes_cred_delegation_to_handler_never_core(monkeypatch):
    """Positive: dict-valued callback reaches OUR handler. Negative: core's
    original handler (the `/card` model path) is never touched."""
    from hermes_multitenancy import feishu_cred_delegation as fcd

    handled: list[Any] = []
    monkeypatch.setattr(
        fcd,
        "handle_delegation_card_action",
        lambda adapter, cb: handled.append(cb.kind) or {"kind": "ok"},
    )
    adapter, calls = _dispatcher_adapter()
    response = adapter._on_card_action_trigger(
        _dispatch_data({"action": "cred_delegation", "choice": "deny", "delegation_id": "d1"})
    )
    assert handled == ["cred_delegation"]
    assert calls["original"] == 0
    assert response == {"kind": "ok"}


def test_dispatcher_consumes_string_valued_cred_delegation_without_core(monkeypatch):
    """A JSON-STRING value parses to the same kind for MT but is NOT a dict for
    core — it must still be consumed here, never delegated (the WP02 P0)."""
    from hermes_multitenancy import feishu_cred_delegation as fcd

    handled: list[Any] = []
    monkeypatch.setattr(
        fcd,
        "handle_delegation_card_action",
        lambda adapter, cb: handled.append(cb.kind) or {"kind": "ok"},
    )
    adapter, calls = _dispatcher_adapter()
    adapter._on_card_action_trigger(
        _dispatch_data(json.dumps({"action": "cred_delegation", "choice": "deny", "delegation_id": "d1"}))
    )
    assert handled == ["cred_delegation"]
    assert calls["original"] == 0


def test_dispatcher_negative_control_handler_exception_is_consumed(monkeypatch):
    """The reverse direction: our handler blowing up must NOT fall through to
    core — the failure is consumed as a data-free error."""
    from hermes_multitenancy import feishu_cred_delegation as fcd

    def _boom(adapter, cb):
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(fcd, "handle_delegation_card_action", _boom)
    adapter, calls = _dispatcher_adapter()
    response = adapter._on_card_action_trigger(
        _dispatch_data({"action": "cred_delegation", "choice": "deny", "delegation_id": "d1"})
    )
    assert calls["original"] == 0
    toast = response["toast"] if isinstance(response, dict) else response.toast
    assert "暂不支持" in str(toast)


def test_cred_delegation_namespace_cannot_be_claimed_by_business_action():
    from hermes_multitenancy import feishu_card_action_dispatcher as dispatcher

    with pytest.raises(ValueError):
        dispatcher.register_business_action("cred_delegation", lambda a, cb: None)


# --------------------------------------------------------------------------- #
# streaming marker pickup (parent side)
# --------------------------------------------------------------------------- #

def test_marker_survives_roundtrip_shape():
    """The payload the parent yields matches what the router branch expects."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / GROUP
        leases.write_auth_required_marker(
            home, {"reason": "missing_credential", "profile": GROUP}
        )
        payload = leases.take_auth_required_marker(home, since=time.time() - 5)
    assert payload is not None
    assert payload["provider"] == "gitlab"
    assert payload["profile"] == GROUP


def test_trigger_fires_even_when_credential_kind_omitted(tmp_path, monkeypatch):
    """The tool's schema default is uat; gitlab coerces to token so the
    delegation trigger cannot be dodged by an omitted credential_kind."""
    from hermes_multitenancy import credential_tool

    home = tmp_path / "profiles" / GROUP
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_SHARED_HOME", raising=False)
    monkeypatch.setattr(
        credential_tool,
        "_provider_adapter_status",
        lambda provider: {"has_credential": False, "status": "missing"},
    )
    payload = json.loads(credential_tool.credential_status({"provider": "gitlab"}))
    assert payload["credential_kind"] == "token"
    assert "delegation" in payload
    assert leases.take_auth_required_marker(home, since=time.time() - 60) is not None


# --------------------------------------------------------------------------- #
# Segment 4 — cross-family review fixes (P0 / P1 / P2 regressions)
# --------------------------------------------------------------------------- #

def _fake_ambient_sender(monkeypatch, open_id: str) -> None:
    """Install a fake ``tools.feishu_oapi_client`` whose ContextVar is already
    set — the ambient identity leak the P0 finding rides on."""
    import contextvars
    import tools

    var = contextvars.ContextVar("current_sender_open_id", default="")
    var.set(open_id)
    monkeypatch.setattr(
        tools, "feishu_oapi_client", SimpleNamespace(current_sender_open_id=var),
        raising=False,
    )


def _lease_row(shared: Path, lease_id: int) -> sqlite3.Row:
    with sqlite3.connect(str(_db(shared))) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM multitenancy_credential_leases WHERE id=?", (lease_id,)
        ).fetchone()


def _chat_lease(shared: Path) -> int:
    """A standing grant — usable with no nonce, so a leak test isolates the
    finding under test instead of being masked by the once-lease run binding."""
    return leases.create_lease(
        _db(shared),
        owner_profile=OWNER,
        owner_open_id=OWNER_OPEN_ID,
        borrower_profile=GROUP,
        scope="chat",
        chat_id="oc_group",
    )


def _once_lease(shared: Path, *, delegation_id: str = "dg-run-1") -> int:
    return leases.create_lease(
        _db(shared),
        owner_profile=OWNER,
        owner_open_id=OWNER_OPEN_ID,
        borrower_profile=GROUP,
        scope="once",
        chat_id="oc_group",
        delegation_id=delegation_id,
    )


# --- P0: warm worker must never bake a delegated token into its base env ------

def test_warm_worker_base_env_never_carries_delegated_token(monkeypatch, shared_home):
    """P0: the long-lived warm worker's BASE env is built with no `extra`. With
    an ambient sender contextvar set, the old code resolved A's leased token
    into that base env (readable by B via /proc/<pid>/environ) AND burned A's
    once-lease. Both must be false."""
    from hermes_multitenancy import agent_real

    profile_home = shared_home / "profiles" / GROUP
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    _fake_ambient_sender(monkeypatch, OWNER_OPEN_ID)
    lease_id = _chat_lease(shared_home)

    env = agent_real._build_aiagent_warm_worker_base_env(profile_home)

    assert "GITLAB_TOKEN" not in env
    assert "_HERMES_FORCE_GITLAB_TOKEN" not in env
    assert not any("glpat-alice-personal" in str(v) for v in env.values())
    row = _lease_row(shared_home, lease_id)
    assert row["status"] == "active" and row["use_count"] == 0
    assert [r["action"] for r in _audit_rows(shared_home)] == ["granted"]


def test_delegation_ignores_ambient_contextvar_sender(monkeypatch, shared_home):
    """P0 (root cause): delegation injection requires an EXPLICIT run sender.
    An ambient contextvar alone must never unlock a lease."""
    from hermes_multitenancy import agent_real

    profile_home = shared_home / "profiles" / GROUP
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    _fake_ambient_sender(monkeypatch, OWNER_OPEN_ID)
    lease_id = _chat_lease(shared_home)

    env = agent_real._build_subprocess_env(
        profile_home, approval_dir=shared_home / "approvals"
    )
    assert "GITLAB_TOKEN" not in env
    assert _lease_row(shared_home, lease_id)["use_count"] == 0


# --- P1-1: once lease is bound to the originating run -------------------------

def test_once_lease_requires_matching_delegation_nonce(shared_home):
    profile_home = shared_home / "profiles" / GROUP
    lease_id = _once_lease(shared_home, delegation_id="dg-A")

    # Another concurrent run of the same person in the same group: no nonce.
    assert leases.delegation_env_for_run(profile_home, sender_open_id=OWNER_OPEN_ID) == {}
    # And a run carrying somebody else's delegation nonce.
    assert leases.delegation_env_for_run(
        profile_home, sender_open_id=OWNER_OPEN_ID, delegation_id="dg-B"
    ) == {}
    # Neither attempt consumed the grant.
    row = _lease_row(shared_home, lease_id)
    assert row["status"] == "active" and row["use_count"] == 0
    assert not [r for r in _audit_rows(shared_home) if r["action"] == "used"]

    # The originating replay run gets it.
    env = leases.delegation_env_for_run(
        profile_home, sender_open_id=OWNER_OPEN_ID, delegation_id="dg-A"
    )
    assert env["GITLAB_TOKEN"] == "glpat-alice-personal"
    assert _lease_row(shared_home, lease_id)["status"] == "consumed"


def test_once_lease_without_delegation_id_is_rejected_at_creation(shared_home):
    with pytest.raises(ValueError):
        leases.create_lease(
            _db(shared_home),
            owner_profile=OWNER,
            owner_open_id=OWNER_OPEN_ID,
            borrower_profile=GROUP,
            scope="once",
        )


def test_chat_lease_needs_no_nonce(shared_home):
    profile_home = shared_home / "profiles" / GROUP
    leases.create_lease(
        _db(shared_home),
        owner_profile=OWNER,
        owner_open_id=OWNER_OPEN_ID,
        borrower_profile=GROUP,
        scope="chat",
        chat_id="oc_group",
    )
    env = leases.delegation_env_for_run(profile_home, sender_open_id=OWNER_OPEN_ID)
    assert env["GITLAB_TOKEN"] == "glpat-alice-personal"


# --- P1-2: claiming a once lease is atomic ------------------------------------

def test_once_lease_claim_is_atomic_under_concurrency(shared_home):
    """Two runs racing the same grant: exactly one gets the token, use_count==1,
    one `used` audit row."""
    import threading

    profile_home = shared_home / "profiles" / GROUP
    lease_id = _once_lease(shared_home, delegation_id="dg-race")

    results: list[dict[str, str]] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        results.append(
            leases.delegation_env_for_run(
                profile_home, sender_open_id=OWNER_OPEN_ID, delegation_id="dg-race"
            )
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len([r for r in results if r]) == 1
    row = _lease_row(shared_home, lease_id)
    assert row["use_count"] == 1 and row["status"] == "consumed"
    assert len([r for r in _audit_rows(shared_home) if r["action"] == "used"]) == 1


def test_revoked_lease_is_not_claimable_after_lookup(shared_home):
    """SELECT-then-UPDATE window: a lease revoked after the read must not be
    claimed, must not be rewritten to `consumed`, and must not audit a use."""
    lease_id = _once_lease(shared_home, delegation_id="dg-stale")
    stale = dict(
        leases.find_active_lease(
            _db(shared_home), owner_open_id=OWNER_OPEN_ID, borrower_profile=GROUP
        )
    )
    assert leases.revoke_chat_leases(
        _db(shared_home), owner_open_id=OWNER_OPEN_ID, borrower_profile=GROUP
    ) == 1

    assert leases.record_lease_use(_db(shared_home), stale) is False
    row = _lease_row(shared_home, lease_id)
    assert row["status"] == "revoked" and row["use_count"] == 0
    assert not [r for r in _audit_rows(shared_home) if r["action"] == "used"]


# --- P1-3: the auth-required marker is per-run, not per-profile ---------------

def test_marker_is_run_nonce_scoped(tmp_path):
    """Two concurrent runs of the same group profile each own their own marker;
    neither can unlink or read the other's."""
    home = tmp_path / GROUP
    leases.write_auth_required_marker(home, {"profile": GROUP, "run": "A"}, nonce="n-A")
    leases.write_auth_required_marker(home, {"profile": GROUP, "run": "B"}, nonce="n-B")

    assert leases.take_auth_required_marker(home, since=0, nonce="n-A")["run"] == "A"
    # B's marker survived A's take.
    assert leases.take_auth_required_marker(home, since=0, nonce="n-B")["run"] == "B"
    assert leases.take_auth_required_marker(home, since=0, nonce="n-A") is None


def test_marker_write_is_atomic_no_partial_file(tmp_path):
    home = tmp_path / GROUP
    leases.write_auth_required_marker(home, {"profile": GROUP}, nonce="n-1")
    path = leases.marker_path(home, nonce="n-1")
    assert json.loads(path.read_text(encoding="utf-8"))["provider"] == "gitlab"
    # tmp+rename leaves no scratch file behind.
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_credential_tool_marker_uses_run_nonce(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / GROUP
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_MULTITENANCY_DELEGATION_NONCE", "n-run-7")
    _tool_status(monkeypatch, profile=GROUP, home=home, has_credential=False)
    assert leases.take_auth_required_marker(home, since=0, nonce="n-other") is None
    assert leases.take_auth_required_marker(home, since=0, nonce="n-run-7") is not None


# --- P1-4: owner profile is re-resolved at take time --------------------------

def _routing(shared: Path, open_id: str, profile: str) -> None:
    with sqlite3.connect(str(_db(shared))) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS multitenancy_routing "
            "(open_id TEXT, active INT, kind TEXT, profile_name TEXT, updated_at INT)"
        )
        conn.execute(
            "INSERT INTO multitenancy_routing VALUES (?,1,'user',?,1)",
            (open_id, profile),
        )


def test_owner_profile_drift_revokes_lease_instead_of_borrowing(shared_home):
    """Routing moved ou_alice to a NEW profile and someone else now owns the old
    one. The stale lease must be revoked, never used to read the new holder's
    token out of the old profile."""
    profile_home = shared_home / "profiles" / GROUP
    lease_id = leases.create_lease(
        _db(shared_home),
        owner_profile=OWNER,
        owner_open_id=OWNER_OPEN_ID,
        borrower_profile=GROUP,
        scope="chat",
        chat_id="oc_group",
    )
    _routing(shared_home, OWNER_OPEN_ID, "alice_new")

    assert leases.delegation_env_for_run(profile_home, sender_open_id=OWNER_OPEN_ID) == {}
    row = _lease_row(shared_home, lease_id)
    assert row["status"] == "revoked" and row["use_count"] == 0
    assert [r["action"] for r in _audit_rows(shared_home)][-1] == "revoked"


def test_owner_profile_match_still_borrows(shared_home):
    profile_home = shared_home / "profiles" / GROUP
    leases.create_lease(
        _db(shared_home),
        owner_profile=OWNER,
        owner_open_id=OWNER_OPEN_ID,
        borrower_profile=GROUP,
        scope="chat",
        chat_id="oc_group",
    )
    _routing(shared_home, OWNER_OPEN_ID, OWNER)
    env = leases.delegation_env_for_run(profile_home, sender_open_id=OWNER_OPEN_ID)
    assert env["GITLAB_TOKEN"] == "glpat-alice-personal"


# --- P2-1: an explicit group token skips the whole delegation lane ------------

def test_existing_group_token_skips_delegation_entirely(shared_home):
    """Allowlist group already carries GITLAB_TOKEN: no lease lookup, no
    consumption, no borrow audit."""
    profile_home = shared_home / "profiles" / GROUP
    lease_id = _once_lease(shared_home, delegation_id="dg-skip")

    assert leases.delegation_env_for_run(
        profile_home,
        sender_open_id=OWNER_OPEN_ID,
        delegation_id="dg-skip",
        existing_env_names={"GITLAB_TOKEN"},
    ) == {}
    row = _lease_row(shared_home, lease_id)
    assert row["status"] == "active" and row["use_count"] == 0
    assert not [r for r in _audit_rows(shared_home) if r["action"] == "used"]


def test_build_subprocess_env_skips_delegation_when_profile_has_token(
    monkeypatch, shared_home
):
    from hermes_multitenancy import agent_real, credential_delegation as cd

    profile_home = shared_home / "profiles" / GROUP
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    seen: list[Any] = []

    def fake_delegation(ph, *, sender_open_id, delegation_id="", existing_env_names=()):
        seen.append(set(existing_env_names))
        return {}

    monkeypatch.setattr(cd, "delegation_env_for_run", fake_delegation)
    agent_real._build_subprocess_env(
        profile_home,
        approval_dir=shared_home / "approvals",
        extra={
            "HERMES_FEISHU_USER_OPEN_ID": OWNER_OPEN_ID,
            "GITLAB_TOKEN": "glpat-group-explicit",
        },
    )
    assert seen and "GITLAB_TOKEN" in seen[0]


# --- P2-2: pending reservation is atomic --------------------------------------

def test_pending_reservation_is_atomic_one_card_per_owner_group():
    from hermes_multitenancy import feishu_cred_delegation as fcd

    fcd._reset_pending_for_tests()
    first = fcd._reserve_pending(_bare_pending(fcd, "dg-1"))
    second = fcd._reserve_pending(_bare_pending(fcd, "dg-2"))
    assert first is True and second is False
    assert fcd._peek_pending("dg-2") is None
    fcd._reset_pending_for_tests()


def test_pending_reservation_released_when_card_send_fails(monkeypatch, shared_home):
    """A failed DM must not leave a phantom reservation that blocks every retry."""
    from hermes_multitenancy import feishu_auth_cards, feishu_cred_delegation as fcd

    fcd._reset_pending_for_tests()
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    monkeypatch.setattr(leases, "owner_profile_for_open_id", lambda _db, _oid: OWNER)

    async def failing_send(*, adapter, chat_id, card, metadata=None):
        return None

    monkeypatch.setattr(feishu_auth_cards, "send_auth_card", failing_send)
    event = SimpleNamespace(
        sender_open_id=OWNER_OPEN_ID,
        source=SimpleNamespace(user_id=OWNER_OPEN_ID),
        text="gitlab 一下",
    )
    adapter = SimpleNamespace(send=lambda *a, **k: None, _loop=None)
    asyncio.run(
        fcd.handle_gitlab_delegation_required(
            gateway=SimpleNamespace(), adapter=adapter, chat_id="oc_group",
            profile_name=GROUP, event=event,
        )
    )
    assert not fcd._has_pending_for(OWNER_OPEN_ID, GROUP)
    fcd._reset_pending_for_tests()


def _bare_pending(fcd, delegation_id: str):
    return fcd._Pending(
        delegation_id=delegation_id,
        owner_open_id=OWNER_OPEN_ID,
        owner_profile=OWNER,
        borrower_profile=GROUP,
        group_chat_id="oc_group",
        replay_text="x",
        gateway=SimpleNamespace(),
        event=SimpleNamespace(sender_open_id=OWNER_OPEN_ID, source=None, text="x"),
        adapter=SimpleNamespace(send=lambda *a, **k: None, _loop=None),
    )


# --- nonce plumbing: card grant → replay → run env ----------------------------

def test_allow_once_binds_lease_to_delegation_id_and_replays_it(
    monkeypatch, shared_home
):
    from hermes_multitenancy import feishu_cred_delegation as fcd

    fcd._reset_pending_for_tests()
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    adapter = SimpleNamespace(send=lambda *a, **k: None, _loop=None)
    _pending(fcd, adapter)
    monkeypatch.setattr(fcd, "_schedule", lambda _a, coro: coro.close())
    replayed: dict[str, Any] = {}

    async def fake_replay(**kwargs):
        replayed.update(kwargs)

    monkeypatch.setattr(fcd, "_dispatch_replay", fake_replay)
    monkeypatch.setattr(
        fcd, "_schedule", lambda _a, coro: asyncio.run(coro)
    )
    fcd.handle_delegation_card_action(
        adapter,
        _cb({"action": "cred_delegation", "choice": "allow_once", "delegation_id": "dg-test-1"}),
    )
    lease = leases.find_active_lease(
        _db(shared_home), owner_open_id=OWNER_OPEN_ID, borrower_profile=GROUP
    )
    assert lease["delegation_id"] == "dg-test-1"
    assert replayed["delegation_id"] == "dg-test-1"


def test_synthetic_replay_event_carries_delegation_id(monkeypatch):
    from hermes_multitenancy import router
    from hermes_multitenancy.router import commands

    captured: dict[str, Any] = {}

    async def fake_handle_async(*, event, gateway):
        captured["event"] = event

    monkeypatch.setattr(router, "handle_async", fake_handle_async, raising=False)
    event = SimpleNamespace(
        text="原始请求", source=None, sender_open_id=OWNER_OPEN_ID, raw_event={}
    )
    assert asyncio.run(
        commands._dispatch_synthetic_auth_complete(
            event=event, gateway=SimpleNamespace(), chat_id="oc_group",
            profile_name=GROUP, open_id=OWNER_OPEN_ID, text="原始请求",
            delegation_id="dg-xyz",
        )
    )
    synthetic = captured["event"]
    from hermes_multitenancy.agent_real import _core

    assert _core._event_delegation_id(synthetic) == "dg-xyz"


def test_event_delegation_id_defaults_empty():
    from hermes_multitenancy.agent_real import _core

    assert _core._event_delegation_id(SimpleNamespace(raw_event={})) == ""
    assert _core._event_delegation_id(object()) == ""


# --- lease SELECTION: one active lease must not shadow another ----------------

def test_stale_once_lease_does_not_shadow_a_standing_chat_lease(shared_home):
    """A newer un-replayed `once` grant must not blind an ordinary run to the
    standing chat grant sitting behind it."""
    profile_home = shared_home / "profiles" / GROUP
    chat_id = _chat_lease(shared_home)
    once_id = _once_lease(shared_home, delegation_id="dg-orphan")

    env = leases.delegation_env_for_run(profile_home, sender_open_id=OWNER_OPEN_ID)
    assert env["GITLAB_TOKEN"] == "glpat-alice-personal"
    # The chat lease was the one used; the once grant is untouched.
    assert _lease_row(shared_home, chat_id)["use_count"] == 1
    once_row = _lease_row(shared_home, once_id)
    assert once_row["status"] == "active" and once_row["use_count"] == 0


def test_once_lease_still_wins_for_its_own_run(shared_home):
    profile_home = shared_home / "profiles" / GROUP
    chat_id = _chat_lease(shared_home)
    once_id = _once_lease(shared_home, delegation_id="dg-mine")

    assert leases.delegation_env_for_run(
        profile_home, sender_open_id=OWNER_OPEN_ID, delegation_id="dg-mine"
    )["GITLAB_TOKEN"] == "glpat-alice-personal"
    assert _lease_row(shared_home, once_id)["status"] == "consumed"
    assert _lease_row(shared_home, chat_id)["use_count"] == 0


@pytest.mark.asyncio
async def test_live_once_lease_does_not_short_circuit_into_a_bare_replay(
    monkeypatch, shared_home
):
    """The standing-grant fast path is for `chat` leases only. A once lease that
    was granted but never replayed (gateway restart) must NOT trigger a replay
    with no delegation id — that run cannot claim the lease, so the model would
    loop asking for authorization forever."""
    from hermes_multitenancy import feishu_auth_cards, feishu_cred_delegation as fcd

    fcd._reset_pending_for_tests()
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    monkeypatch.setattr(leases, "owner_profile_for_open_id", lambda _db, _oid: OWNER)
    _once_lease(shared_home, delegation_id="dg-orphaned-run")

    replays: list[dict[str, Any]] = []

    async def fake_replay(**kwargs):
        replays.append(kwargs)

    monkeypatch.setattr(fcd, "_dispatch_replay", fake_replay)
    sent: dict[str, Any] = {}

    async def fake_send_auth_card(*, adapter, chat_id, card, metadata=None):
        sent["card"] = card
        return {"transport": "interactive", "message_id": "om_card", "sequence": 0}

    monkeypatch.setattr(feishu_auth_cards, "send_auth_card", fake_send_auth_card)
    event = SimpleNamespace(
        sender_open_id=OWNER_OPEN_ID,
        source=SimpleNamespace(user_id=OWNER_OPEN_ID),
        text="gitlab 再来一次",
    )
    adapter = SimpleNamespace(send=lambda *a, **k: None, _loop=None)
    await fcd.handle_gitlab_delegation_required(
        gateway=SimpleNamespace(), adapter=adapter, chat_id="oc_group",
        profile_name=GROUP, event=event,
    )
    assert replays == []
    assert sent.get("card") is not None
    fcd._reset_pending_for_tests()


@pytest.mark.asyncio
async def test_standing_chat_lease_still_short_circuits(monkeypatch, shared_home):
    from hermes_multitenancy import feishu_auth_cards, feishu_cred_delegation as fcd

    fcd._reset_pending_for_tests()
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    monkeypatch.setattr(leases, "owner_profile_for_open_id", lambda _db, _oid: OWNER)
    _chat_lease(shared_home)

    replays: list[dict[str, Any]] = []

    async def fake_replay(**kwargs):
        replays.append(kwargs)

    monkeypatch.setattr(fcd, "_dispatch_replay", fake_replay)

    def _no_card(**_kw):
        raise AssertionError("a standing grant must not re-ask")

    monkeypatch.setattr(feishu_auth_cards, "send_auth_card", _no_card)
    event = SimpleNamespace(
        sender_open_id=OWNER_OPEN_ID,
        source=SimpleNamespace(user_id=OWNER_OPEN_ID),
        text="gitlab 继续",
    )
    adapter = SimpleNamespace(send=lambda *a, **k: None, _loop=None)
    await fcd.handle_gitlab_delegation_required(
        gateway=SimpleNamespace(), adapter=adapter, chat_id="oc_group",
        profile_name=GROUP, event=event,
    )
    assert len(replays) == 1 and replays[0]["profile_name"] == GROUP
    fcd._reset_pending_for_tests()


# --------------------------------------------------------------------------- #
# Round-2 cross-family review fixes (A–F)
# --------------------------------------------------------------------------- #

def _clear_routing(shared: Path) -> None:
    with sqlite3.connect(str(_db(shared))) as conn:
        conn.execute("DELETE FROM multitenancy_routing")


# --- A: owner routing is FAIL-CLOSED ------------------------------------------

def test_unresolvable_owner_routing_refuses_borrow_and_keeps_lease(shared_home):
    """Alice's routing row is gone and profile `alice` now belongs to Bob (his
    token sits in that vault record). Resolution returns None — the old code
    read the lease's recorded profile anyway and injected BOB's token into
    ALICE's run. Fail-closed: no env, lease NOT revoked (routing may just be
    unavailable), and the refusal is audited."""
    profile_home = shared_home / "profiles" / GROUP
    lease_id = _chat_lease(shared_home)
    _clear_routing(shared_home)

    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(_db(shared_home))
    try:  # profile `alice` inherited by Bob, token swapped
        store.put_credential(
            profile_name=OWNER,
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "glpat-BOB-personal"},
            scopes=["api"],
            expires_at=None,
        )
    finally:
        store.close()

    assert leases.delegation_env_for_run(profile_home, sender_open_id=OWNER_OPEN_ID) == {}
    row = _lease_row(shared_home, lease_id)
    assert row["status"] == "active" and row["use_count"] == 0
    denials = [r for r in _audit_rows(shared_home) if r["action"] == "denied"]
    assert denials and "unresolved" in denials[-1]["detail"]


def test_owner_handover_between_route_check_and_token_read_is_blocked(shared_home):
    """The routing check and the token read are two snapshots. A handover that
    lands between them must still be caught by the post-read re-confirmation."""
    profile_home = shared_home / "profiles" / GROUP
    lease_id = _chat_lease(shared_home)

    real_env = leases.owner_gitlab_env

    def flipping_env(shared, owner_profile):
        _routing(shared_home, OWNER_OPEN_ID, "alice_new")  # handover mid-flight
        return real_env(shared, owner_profile)

    import unittest.mock as _mock

    with _mock.patch.object(leases, "owner_gitlab_env", flipping_env):
        assert (
            leases.delegation_env_for_run(profile_home, sender_open_id=OWNER_OPEN_ID)
            == {}
        )
    row = _lease_row(shared_home, lease_id)
    assert row["use_count"] == 0 and row["status"] == "revoked"


# --- B: empty delegation_id must never match ----------------------------------

def test_legacy_empty_delegation_id_once_lease_is_not_consumed_by_plain_run(
    shared_home,
):
    """A migrated once row with delegation_id='' must not be matched by an
    ordinary run (which also carries ''): empty is 'no run identity'."""
    profile_home = shared_home / "profiles" / GROUP
    lease_id = _once_lease(shared_home, delegation_id="dg-legacy")
    with sqlite3.connect(str(_db(shared_home))) as conn:
        conn.execute(
            "UPDATE multitenancy_credential_leases SET delegation_id='' WHERE id=?",
            (lease_id,),
        )

    assert leases.delegation_env_for_run(profile_home, sender_open_id=OWNER_OPEN_ID) == {}
    row = _lease_row(shared_home, lease_id)
    assert row["status"] == "active" and row["use_count"] == 0
    assert not [r for r in _audit_rows(shared_home) if r["action"] == "used"]


def test_run_with_delegation_id_cannot_match_empty_id_lease(shared_home):
    lease_id = _once_lease(shared_home, delegation_id="dg-x")
    with sqlite3.connect(str(_db(shared_home))) as conn:
        conn.execute(
            "UPDATE multitenancy_credential_leases SET delegation_id='' WHERE id=?",
            (lease_id,),
        )
    assert (
        leases.find_active_lease(
            _db(shared_home),
            owner_open_id=OWNER_OPEN_ID,
            borrower_profile=GROUP,
            delegation_id="dg-x",
        )
        is None
    )


# --- C: an EMPTY GITLAB_TOKEN= is not a token ---------------------------------

def test_empty_group_token_does_not_disable_delegation(shared_home):
    """group .env with a bare `GITLAB_TOKEN=`: the name is present but there is
    no token. Early-returning here made the child re-report 'missing' forever
    (endless cards, once-lease never consumed)."""
    profile_home = shared_home / "profiles" / GROUP
    _chat_lease(shared_home)
    env = leases.delegation_env_for_run(
        profile_home,
        sender_open_id=OWNER_OPEN_ID,
        existing_env_names={"GITLAB_TOKEN": ""},
    )
    assert env["GITLAB_TOKEN"] == "glpat-alice-personal"

    # ...while a real value still skips the lane entirely.
    assert (
        leases.delegation_env_for_run(
            profile_home,
            sender_open_id=OWNER_OPEN_ID,
            existing_env_names={"GITLAB_TOKEN": "glpat-group-explicit"},
        )
        == {}
    )


def test_build_subprocess_env_delegates_when_profile_token_is_empty(
    monkeypatch, shared_home
):
    from hermes_multitenancy import agent_real

    profile_home = shared_home / "profiles" / GROUP
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    _chat_lease(shared_home)
    env = agent_real._build_subprocess_env(
        profile_home,
        approval_dir=shared_home / "approvals",
        extra={"HERMES_FEISHU_USER_OPEN_ID": OWNER_OPEN_ID, "GITLAB_TOKEN": ""},
    )
    assert env["GITLAB_TOKEN"] == "glpat-alice-personal"


# --- D: cancellation must release the pending reservation ---------------------

@pytest.mark.asyncio
async def test_cancelled_card_send_releases_pending_reservation(
    monkeypatch, shared_home
):
    """CancelledError is a BaseException: the old `except Exception` let it
    escape with the slot still held and no expiry task created — 10 minutes of
    silently swallowed retries."""
    from hermes_multitenancy import feishu_auth_cards, feishu_cred_delegation as fcd

    fcd._reset_pending_for_tests()
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    monkeypatch.setattr(leases, "owner_profile_for_open_id", lambda _db, _oid: OWNER)

    async def cancelled_send(*, adapter, chat_id, card, metadata=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(feishu_auth_cards, "send_auth_card", cancelled_send)
    event = SimpleNamespace(
        sender_open_id=OWNER_OPEN_ID,
        source=SimpleNamespace(user_id=OWNER_OPEN_ID),
        text="gitlab 看下",
    )
    adapter = SimpleNamespace(send=lambda *a, **k: None, _loop=None)
    with pytest.raises(asyncio.CancelledError):
        await fcd.handle_gitlab_delegation_required(
            gateway=SimpleNamespace(), adapter=adapter, chat_id="oc_group",
            profile_name=GROUP, event=event,
        )
    assert not fcd._has_pending_for(OWNER_OPEN_ID, GROUP)
    fcd._reset_pending_for_tests()


# --- E: expiry must not overwrite a concurrent revoke -------------------------

def test_expiry_update_does_not_overwrite_revoked(shared_home, monkeypatch):
    """finder reads an expired-but-active row, the owner's revoke commits, and
    the finder then wrote status='expired' over it — the audit trail claimed the
    lease timed out when it was actually revoked.

    The revoke is injected exactly between the SELECT and the expiry UPDATE by
    intercepting that UPDATE on the finder's own connection.
    """
    profile_home = shared_home / "profiles" / GROUP
    lease_id = _chat_lease(shared_home)
    with sqlite3.connect(str(_db(shared_home))) as conn:
        conn.execute(
            "UPDATE multitenancy_credential_leases SET expires_at=? WHERE id=?",
            (int(time.time() * 1000) - 1000, lease_id),
        )

    real_connect = leases._connect

    class _RacingConn:
        """Delegates everything; the first expiry UPDATE is preceded by a
        concurrent revoke committing from another connection."""

        def __init__(self, conn, db_path):
            self._conn = conn
            self._db_path = db_path
            self._raced = False

        def execute(self, sql, *args, **kwargs):
            if not self._raced and "status='expired'" in sql:
                self._raced = True
                with sqlite3.connect(str(self._db_path)) as other:
                    other.execute(
                        "UPDATE multitenancy_credential_leases "
                        "SET status='revoked' WHERE id=?",
                        (lease_id,),
                    )
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, *exc):
            return self._conn.__exit__(*exc)

    def racing_connect(db_path):
        return _RacingConn(real_connect(db_path), db_path)

    monkeypatch.setattr(leases, "_connect", racing_connect)
    assert (
        leases.find_active_lease(
            _db(shared_home), owner_open_id=OWNER_OPEN_ID, borrower_profile=GROUP
        )
        is None
    )
    monkeypatch.undo()
    assert _lease_row(shared_home, lease_id)["status"] == "revoked"
    assert "expired" not in [r["action"] for r in _audit_rows(shared_home)]
    del profile_home


# --- F: warm worker base env must not carry an identity -----------------------

def test_warm_worker_base_env_drops_feishu_sender_identity(monkeypatch, shared_home):
    """Pre-existing leak: the ambient sender contextvar baked the FIRST user's
    open_id into the long-lived base env, readable by every later user."""
    from hermes_multitenancy import agent_real

    profile_home = shared_home / "profiles" / GROUP
    monkeypatch.setenv("HERMES_SHARED_HOME", str(shared_home))
    _fake_ambient_sender(monkeypatch, OWNER_OPEN_ID)
    env = agent_real._build_aiagent_warm_worker_base_env(profile_home)
    assert "HERMES_FEISHU_USER_OPEN_ID" not in env
    assert not any(OWNER_OPEN_ID in str(v) for v in env.values())


# --- A (round 3): forward routing agreement is NOT sufficient ------------------

def test_contested_owner_profile_refuses_borrow(shared_home):
    """Alice's open_id still routes to profile `alice`, so the FORWARD check
    passes — but during an HR sync (upsert-new before soft-delete-old, and no
    unique index on active profile_name) Bob is ALSO active on `alice` and his
    token now sits in that vault record. The forward-only check let Bob's token
    into Alice's run. The reverse check refuses: the profile must map back to
    Alice alone. Lease is kept (contention is transient), refusal is audited."""
    profile_home = shared_home / "profiles" / GROUP
    lease_id = _chat_lease(shared_home)

    with sqlite3.connect(str(_db(shared_home))) as conn:  # Bob joins the profile
        conn.execute(
            "INSERT INTO multitenancy_routing VALUES (?,1,'user',?,1)",
            ("ou_bob", OWNER),
        )

    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(_db(shared_home))
    try:  # the vault record for `alice` now holds BOB's token
        store.put_credential(
            profile_name=OWNER,
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "glpat-BOB-personal"},
            scopes=["api"],
            expires_at=None,
        )
    finally:
        store.close()

    # forward check still agrees — that is exactly the trap
    assert leases.owner_profile_for_open_id(_db(shared_home), OWNER_OPEN_ID) == OWNER

    assert leases.delegation_env_for_run(profile_home, sender_open_id=OWNER_OPEN_ID) == {}
    row = _lease_row(shared_home, lease_id)
    assert row["status"] == "active" and row["use_count"] == 0
    assert any(
        r["action"] == "denied" and "contested" in (r["detail"] or "")
        for r in _audit_rows(shared_home)
    )


def test_sole_ownership_helper_accepts_and_rejects(shared_home):
    db = _db(shared_home)
    assert leases.profile_is_solely_owned_by(db, OWNER, OWNER_OPEN_ID) is True
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO multitenancy_routing VALUES (?,1,'user',?,1)",
            ("ou_bob", OWNER),
        )
    assert leases.profile_is_solely_owned_by(db, OWNER, OWNER_OPEN_ID) is False


def test_blank_open_id_cooccupant_refuses_borrow(shared_home):
    """Round-4: the schema permits an active user row with a blank open_id and
    RoutingTable.upsert does not reject it. The first sole-ownership query
    EXCLUDED such rows, so an unidentifiable co-occupant made the check report
    'solely owned' while that person's token already sat in the profile vault
    (reviewer reproduced sole_check=True / injected=bob-token). Any blank
    open_id on the profile must fail closed instead of dropping out of the set."""
    profile_home = shared_home / "profiles" / GROUP
    lease_id = _chat_lease(shared_home)

    with sqlite3.connect(str(_db(shared_home))) as conn:
        conn.execute(
            "INSERT INTO multitenancy_routing VALUES (?,1,'user',?,1)",
            ("", OWNER),  # Bob, unidentifiable on this seam
        )

    from hermes_multitenancy.credentials import CredentialStore

    store = CredentialStore(_db(shared_home))
    try:
        store.put_credential(
            profile_name=OWNER,
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
            payload={"token": "glpat-BOB-personal"},
            scopes=["api"],
            expires_at=None,
        )
    finally:
        store.close()

    assert leases.profile_is_solely_owned_by(_db(shared_home), OWNER, OWNER_OPEN_ID) is False
    assert leases.delegation_env_for_run(profile_home, sender_open_id=OWNER_OPEN_ID) == {}
    assert _lease_row(shared_home, lease_id)["use_count"] == 0


def test_null_open_id_cooccupant_refuses_borrow(shared_home):
    """Same seam, NULL rather than empty string."""
    with sqlite3.connect(str(_db(shared_home))) as conn:
        conn.execute(
            "INSERT INTO multitenancy_routing VALUES (?,1,'user',?,1)", (None, OWNER)
        )
    assert leases.profile_is_solely_owned_by(_db(shared_home), OWNER, OWNER_OPEN_ID) is False


# --- Round-5: canonical-or-refuse (my own strip() introduced a collision) ------

def test_whitespace_padded_duplicate_open_id_refuses(shared_home):
    """Round-5 regression. Bob's row carries ' ou_alice ' — the credential and
    user_id seams resolve it, and my strip()-then-dedupe folded it into Alice's
    own identity, reporting sole ownership (the reviewer's probe returned
    new_sole_check=True). Non-canonical identities must REFUSE, not normalise."""
    with sqlite3.connect(str(_db(shared_home))) as conn:
        conn.execute(
            "INSERT INTO multitenancy_routing VALUES (?,1,'user',?,1)",
            (f" {OWNER_OPEN_ID} ", OWNER),
        )
    assert leases.profile_is_solely_owned_by(_db(shared_home), OWNER, OWNER_OPEN_ID) is False


def test_padded_profile_name_cooccupant_is_seen(shared_home):
    """A row on 'alice ' is excluded by exact SQL matching, but the credential
    writer strips the profile name, so that person's token lands in the SAME
    vault record. The query must match on TRIM(profile_name) to see them."""
    with sqlite3.connect(str(_db(shared_home))) as conn:
        conn.execute(
            "INSERT INTO multitenancy_routing VALUES (?,1,'user',?,1)",
            ("ou_bob", f"{OWNER} "),
        )
    assert leases.profile_is_solely_owned_by(_db(shared_home), OWNER, OWNER_OPEN_ID) is False


def test_mixed_case_kind_cooccupant_is_seen(shared_home):
    """kind='User' is excluded by an exact 'user' match, yet user_id lookup is
    kind-agnostic and still reaches the credential write path."""
    with sqlite3.connect(str(_db(shared_home))) as conn:
        conn.execute(
            "INSERT INTO multitenancy_routing VALUES (?,1,'User',?,1)",
            ("ou_bob", OWNER),
        )
    assert leases.profile_is_solely_owned_by(_db(shared_home), OWNER, OWNER_OPEN_ID) is False


def test_clean_single_owner_still_passes(shared_home):
    """No false refusal for the normal case (prod today: every active user row
    is canonical and every profile has exactly one owner)."""
    assert leases.profile_is_solely_owned_by(_db(shared_home), OWNER, OWNER_OPEN_ID) is True


def test_unicode_space_padded_profile_cooccupant_is_seen(shared_home):
    """Round-6: SQLite TRIM() strips only U+0020, Python str.strip() strips all
    Unicode spaces. A row on 'alice\\u00a0' was invisible to the SQL filter yet
    the credential writer stored it under 'alice' — same vault, hidden owner.
    Selection must use the writer's own str.strip() semantics."""
    with sqlite3.connect(str(_db(shared_home))) as conn:
        conn.execute(
            "INSERT INTO multitenancy_routing VALUES (?,1,'user',?,1)",
            ("ou_bob", f"{OWNER} "),
        )
    assert leases.profile_is_solely_owned_by(_db(shared_home), OWNER, OWNER_OPEN_ID) is False


def test_unicode_space_padded_kind_cooccupant_is_seen(shared_home):
    """Same mismatch on `kind`: '\\tuser\\t' hid from the SQL filter while
    user_id lookup ignores kind entirely."""
    with sqlite3.connect(str(_db(shared_home))) as conn:
        conn.execute(
            "INSERT INTO multitenancy_routing VALUES (?,1,?,?,1)",
            ("ou_bob", "\tuser\t", OWNER),
        )
    assert leases.profile_is_solely_owned_by(_db(shared_home), OWNER, OWNER_OPEN_ID) is False


def test_non_user_kind_cooccupant_is_seen(shared_home):
    """Round-7: routing.lookup_by_user_id matches on user_id + active and never
    looks at `kind`, so a row of any kind on this profile still reaches the
    credential write path. Filtering kind here hid exactly those rows
    (reviewer: resolved_profile='alice', resolved_open_id='bob_uid',
    sole_check=True)."""
    for kind in ("group", "agent", "", "anything"):
        home = shared_home
        with sqlite3.connect(str(_db(home))) as conn:
            conn.execute(
                "INSERT INTO multitenancy_routing VALUES (?,1,?,?,1)",
                (f"ou_bob_{kind}", kind, OWNER),
            )
        assert leases.profile_is_solely_owned_by(_db(home), OWNER, OWNER_OPEN_ID) is False, kind
        with sqlite3.connect(str(_db(home))) as conn:
            conn.execute("DELETE FROM multitenancy_routing WHERE open_id = ?", (f"ou_bob_{kind}",))
    # and the clean profile still passes afterwards
    assert leases.profile_is_solely_owned_by(_db(shared_home), OWNER, OWNER_OPEN_ID) is True
