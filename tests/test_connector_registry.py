"""Connector Registry — list / get / status surface + 防串号 redaction.

Pins the registry's public read surface: the built-in definitions and
their order, the high-risk lark-cli runtime-policy marker, and the additive
scope fields every ``ConnectorStatus`` carries. Subprocess-backed readers are
monkeypatched (no real meegle/kep-auth binary) exactly like
``test_credential_hub`` does, so the live status is deterministic.

The redaction asserts are the security spine of this file: ``credential_owner``
must be the redacted profile NAME (never a filesystem path), and ``to_dict()``
must never serialize a planted secret token.
"""
from __future__ import annotations

import json
import time


def _mk_profile_home(tmp_path, profile_name="owner"):
    """Build ``<shared>/profiles/<name>/home`` and return (shared_home, home)."""
    home = tmp_path / "profiles" / profile_name / "home"
    home.mkdir(parents=True)
    return tmp_path, home


def _patch_no_binaries(monkeypatch, *, lark_status):
    """Silence the two subprocess readers (meegle, lark DB) so no binary runs."""
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    monkeypatch.setattr(feishu_uat_auth, "credential_status", lambda **kw: lark_status)
    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda **k: None)


def test_list_definitions_returns_builtins_in_credential_order():
    from hermes_multitenancy import credential_hub
    from hermes_multitenancy.connectors import registry

    defs = registry.list_definitions()
    assert [d.id for d in defs] == [*credential_hub.CREDENTIAL_ORDER, "github-mcp"]


def test_lark_cli_is_high_risk_authsidecar_broker_owned():
    from hermes_multitenancy.connectors import registry

    lark = registry.get_connector("lark-cli")
    assert lark is not None
    assert lark.policy.runtime_policy_owner == "authsidecar_broker"


def test_other_connectors_are_connector_driver_owned():
    from hermes_multitenancy.connectors import registry

    for cid in ("feishu-project", "keep-record", "kep-cli-online", "kep-cli-pre", "gitlab"):
        definition = registry.get_connector(cid)
        assert definition is not None
        assert definition.policy.runtime_policy_owner == "connector_driver", cid


def test_collect_returns_statuses_with_scope_fields(monkeypatch, tmp_path):
    from hermes_multitenancy.connectors import registry

    shared, _ = _mk_profile_home(tmp_path, "owner")
    _patch_no_binaries(
        monkeypatch,
        lark_status={"status": "valid", "expires_at": int(time.time() * 1000) + 1000},
    )

    statuses = registry.collect_connector_statuses(
        profile_name="owner", open_id="ou_owner", shared_home=shared
    )
    # lark-cli / feishu-project / keep-record / kep-cli-online / kep-cli-pre /
    # gitlab（全局）/ gitlab-personal（员工自己绑的）
    assert len(statuses) == 8
    for status in statuses:
        assert status.profile == "owner"
        assert status.scope  # non-empty
        d = status.to_dict()
        for key in (
            "profile",
            "scope",
            "acting_identity",
            "credential_owner",
            "runtime_policy_owner",
        ):
            assert key in d, (status.id, key)


def test_github_status_failure_preserves_all_legacy_connector_rows(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub, github_mcp_connector
    from hermes_multitenancy.connectors import registry

    shared, _ = _mk_profile_home(tmp_path, "owner")
    _patch_no_binaries(monkeypatch, lark_status={"status": "missing"})
    monkeypatch.setattr(
        github_mcp_connector,
        "status",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("vault locked")),
    )

    statuses = registry.collect_connector_statuses(
        profile_name="owner", open_id="ou_owner", shared_home=shared
    )

    assert [row.id for row in statuses[:-1]] == list(credential_hub.CREDENTIAL_ORDER)
    assert statuses[-1].id == "github-mcp"
    assert statuses[-1].status == "error"


def test_registry_lark_cli_uses_profile_json_when_status_reader_keyless_error(monkeypatch, tmp_path):
    from hermes_multitenancy import credential_hub, feishu_uat_auth
    from hermes_multitenancy.connectors import registry

    shared, _ = _mk_profile_home(tmp_path, "owner")
    uat = shared / "profiles" / "owner" / "feishu_uat"
    uat.mkdir(parents=True)
    future = int(time.time() * 1000) + 3600_000
    (uat / "ou_owner.json").write_text(
        json.dumps({"access_token": "registry-profile-json-secret", "expires_at": future}),
        encoding="utf-8",
    )

    def _keyless_error(**kw):
        raise RuntimeError("credential encryption key is required")

    monkeypatch.setattr(feishu_uat_auth, "credential_status", _keyless_error)
    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda **k: None)

    statuses = registry.collect_connector_statuses(
        profile_name="owner",
        open_id="ou_owner",
        shared_home=shared,
    )
    lark = next(status for status in statuses if status.id == "lark-cli")
    blob = json.dumps(lark.to_dict(), ensure_ascii=False)

    assert lark.status == "authenticated"
    assert lark.expires_at == future
    assert "registry-profile-json-secret" not in blob


def test_credential_owner_is_redacted_profile_name_not_a_path(monkeypatch, tmp_path):
    from hermes_multitenancy.connectors import registry

    shared, _ = _mk_profile_home(tmp_path, "owner")
    _patch_no_binaries(
        monkeypatch,
        lark_status={"status": "valid", "expires_at": int(time.time() * 1000) + 1000},
    )

    statuses = registry.collect_connector_statuses(
        profile_name="owner", open_id="ou_owner", shared_home=shared
    )
    for status in statuses:
        # All builtins are secrets_owner="profile_home" → owner == profile name.
        assert status.credential_owner == "owner", status.id
        # A filesystem path would contain a separator; the redacted name must not.
        assert "/" not in (status.credential_owner or ""), status.id


def test_to_dict_never_leaks_a_planted_secret_token(monkeypatch, tmp_path):
    """A real keep-record token + keyring path must never reach the wire shape."""
    from hermes_multitenancy.connectors import registry

    shared, home = _mk_profile_home(tmp_path, "owner")
    # Install a keep-record skill so it is reported installed.
    skill = home.parent / "skills" / "Keep" / "keep-record"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "name: keep-record\nget_qrcode keep_auth_token\n", encoding="utf-8"
    )
    # Plant a secret token + verification marker so keep-record reads authenticated.
    secret = "SECRET-keep-token-DO-NOT-LEAK-9f3a"
    keepai = home / ".keepai"
    keepai.mkdir(parents=True)
    future = int(time.time() * 1000) + 7 * 24 * 3600 * 1000
    (keepai / ".env").write_text(
        f"keep_auth_token={secret}\nkeep_auth_token_expired={future}\nkeep_username=owner\n",
        encoding="utf-8",
    )
    import hashlib

    (keepai / "webui-auth-verified.json").write_text(
        json.dumps(
            {"token_sha256": hashlib.sha256(secret.encode()).hexdigest(), "account_hint": "owner"}
        ),
        encoding="utf-8",
    )
    _patch_no_binaries(monkeypatch, lark_status={"status": "missing"})

    statuses = registry.collect_connector_statuses(
        profile_name="owner", open_id="ou_owner", shared_home=shared
    )
    keep = next(s for s in statuses if s.id == "keep-record")
    assert keep.status == "authenticated"  # proves the secret was actually read

    for status in statuses:
        blob = json.dumps(status.to_dict(), ensure_ascii=False)
        assert secret not in blob, status.id
        assert ".keepai" not in blob, status.id
        assert str(home) not in blob, status.id

def test_use_cache_true_serves_second_call_from_cache(monkeypatch, tmp_path):
    """Repeat panel loads within the TTL skip the (subprocess-heavy) readers."""
    from hermes_multitenancy import credential_hub
    from hermes_multitenancy.connectors import registry

    shared, _ = _mk_profile_home(tmp_path, "owner")
    _patch_no_binaries(monkeypatch, lark_status={"status": "missing"})
    registry._cache.clear()
    calls = {"n": 0}
    orig = credential_hub._collect_credential_rows
    monkeypatch.setattr(
        credential_hub, "_collect_credential_rows",
        lambda **kw: (calls.__setitem__("n", calls["n"] + 1) or orig(**kw)),
    )
    for _ in range(2):
        registry.collect_connector_statuses(
            profile_name="owner", open_id="ou_owner", shared_home=shared, use_cache=True
        )
    assert calls["n"] == 1  # second call hit the cache


def test_use_cache_false_bypasses_cache_every_call(monkeypatch, tmp_path):
    """The post-auth poll / manual refresh path must read live every time."""
    from hermes_multitenancy import credential_hub
    from hermes_multitenancy.connectors import registry

    shared, _ = _mk_profile_home(tmp_path, "owner")
    _patch_no_binaries(monkeypatch, lark_status={"status": "missing"})
    registry._cache.clear()
    calls = {"n": 0}
    orig = credential_hub._collect_credential_rows
    monkeypatch.setattr(
        credential_hub, "_collect_credential_rows",
        lambda **kw: (calls.__setitem__("n", calls["n"] + 1) or orig(**kw)),
    )
    for _ in range(2):
        registry.collect_connector_statuses(
            profile_name="owner", open_id="ou_owner", shared_home=shared, use_cache=False
        )
    assert calls["n"] == 2  # fresh each time — no cache reuse


def test_fresh_read_refreshes_cache_so_next_cached_read_is_not_stale(monkeypatch, tmp_path):
    """A use_cache=False (post-auth poll) read must OVERWRITE the cache, else a
    pre-auth 'needs_auth' survives the TTL and the next cached load regresses."""
    from hermes_multitenancy import credential_hub
    from hermes_multitenancy.connectors import registry

    shared, _ = _mk_profile_home(tmp_path, "owner")
    _patch_no_binaries(monkeypatch, lark_status={"status": "missing"})
    registry._cache.clear()
    live = {"status": "needs_auth"}
    monkeypatch.setattr(
        credential_hub, "_collect_credential_rows",
        lambda **kw: [credential_hub.CredentialRow(
            id="kep-cli-online", title="kep-cli online", provider="keep", installed=True, status=live["status"])],
    )

    # 1. prime cache with needs_auth
    registry.collect_connector_statuses(profile_name="owner", open_id="o", shared_home=shared, use_cache=True)
    # 2. user authenticates → reader now returns authenticated
    live["status"] = "authenticated"
    # 3. fresh poll read: live authenticated AND refreshes cache
    fresh = registry.collect_connector_statuses(profile_name="owner", open_id="o", shared_home=shared, use_cache=False)
    assert fresh[0].to_dict()["status"] == "authenticated"
    # 4. a later CACHED read must reflect the refreshed value, not the stale needs_auth
    live["status"] = "SHOULD_NOT_BE_READ_LIVE"
    cached = registry.collect_connector_statuses(profile_name="owner", open_id="o", shared_home=shared, use_cache=True)
    assert cached[0].to_dict()["status"] == "authenticated"
