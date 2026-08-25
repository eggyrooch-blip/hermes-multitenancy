"""Employee GitLab token intake: gates, storage, and the Feishu submit guards."""
from datetime import date
from pathlib import Path

import pytest

from hermes_multitenancy.gitlab_token_intake import (
    HERMES_TOKEN_NAME,
    SCOPE_BINDING_UNVERIFIED,
    INVALID_TOKEN,
    PROBE_GIT_ONLY,
    PROBE_NOT_FOUND,
    PROBE_OK,
    TIER_READ,
    TIER_WRITE,
    UNDETERMINED,
    TokenRejected,
    expiry_from_gitlab,
    probe_token,
    submit_personal_token,
)

READ_SCOPES = ["read_api", "read_repository"]
WRITE_SCOPES = ["api", "write_repository"]


def _ok(scopes, expires_at="2099-01-01"):
    return lambda *a, **k: (PROBE_OK, list(scopes), expires_at)


def _home(tmp_path: Path) -> Path:
    """A shared home whose deployed config fully opts alice into the self lane."""
    shared = tmp_path / ".hermes"
    (shared / "profiles" / "alice").mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        "credentials:\n  - subject_id: kep-prd-skills\n    provider: gitlab\n"
        "    secret_kind: token\n    target: workspace/credentials/gitlab.token\n"
        "    env: GITLAB_TOKEN\n    vault_profile: __self__\n    profiles: [alice]\n",
        encoding="utf-8",
    )
    return shared


# -- expiry gate -------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", None, "not-a-date", "2026/01/01", "2000-01-01"])
def test_unreadable_or_absent_expiry_is_None_never_an_error(blank):
    """Transcription, not a gate (sunke 2026-08-06: 有效期归上游 GitLab 管).
    None means "no expiry on the row" and the vault already reads that as
    never-expires. Refusing here would dead-end a submit over something we do
    not even gate on."""
    assert expiry_from_gitlab(blank) is None


def test_expiry_parses_to_utc_midnight_epoch_ms():
    assert expiry_from_gitlab("2099-01-01") == 4070908800000
    # 边界：到期日就是今天也算不再持有（今天午夜早已过去）。
    assert expiry_from_gitlab("2026-08-06", today=date(2026, 8, 6)) is None
    assert expiry_from_gitlab("2026-08-07", today=date(2026, 8, 6)) is not None


# -- scope probe -------------------------------------------------------------


def _rows(payload):
    """Opener returning a token-list JSON body."""
    import json as _json

    body = _json.dumps(payload).encode()

    def _open(request, timeout=None):
        class _R:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body

        return _R()

    return _open


def _http(code):
    import urllib.error

    def _open(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, code, "", {}, None)

    return _open


def test_scopes_are_read_off_gitlab_not_inferred():
    """The authoritative source is the token's own row, found by agreed name.

    Behavioural inference was tried twice and abandoned: 14.10 permits both
    `api` and `read_api` on every GET, so no GET separates them, and non-GET
    probes are refused even for `api` on this instance.
    """
    assert probe_token("t", opener=_rows(
        [{"name": HERMES_TOKEN_NAME, "active": True, "scopes": ["api", "write_repository"]}]
    )) == (PROBE_OK, ["api", "write_repository"], None)


def test_expiry_is_read_off_the_same_row():
    """The row that gives us the scopes also carries expires_at — read it there
    instead of making the employee re-type it (a hand-copied 2031-11-31 once
    400'd the whole form)."""
    assert probe_token("t", opener=_rows(
        [{"name": HERMES_TOKEN_NAME, "active": True, "scopes": ["api"],
          "expires_at": "2027-01-31"}]
    )) == (PROBE_OK, ["api"], "2027-01-31")


def test_only_an_active_row_with_the_agreed_name_counts():
    assert probe_token("t", opener=_rows(
        [{"name": "something-else", "active": True, "scopes": ["api"]}]
    )) == (PROBE_NOT_FOUND, [], None)
    assert probe_token("t", opener=_rows(
        [{"name": HERMES_TOKEN_NAME, "active": False, "scopes": ["api"]}]
    )) == (PROBE_NOT_FOUND, [], None)


def test_duplicate_names_pick_the_newest_created_row():
    """Stale hermes tokens from earlier rounds must not dead-end the submit
    (sunke 2026-08-05: 「能用不就行了」). The real-world flow is create-then-
    paste, so the newest created row is the one being submitted; a wrong pick
    only mislabels (SCOPE_BINDING_UNVERIFIED accepted-debt class), never
    escalates."""
    assert probe_token("t", opener=_rows([
        {"name": HERMES_TOKEN_NAME, "active": True, "scopes": ["api"],
         "created_at": "2026-08-01T00:00:00Z", "expires_at": "2027-01-01"},
        {"name": HERMES_TOKEN_NAME, "active": True, "scopes": ["read_api"],
         "created_at": "2026-08-05T00:00:00Z", "expires_at": "2027-06-01"},
    ])) == (PROBE_OK, ["read_api"], "2027-06-01")
    # Order in the listing must not matter — only created_at does.
    assert probe_token("t", opener=_rows([
        {"name": HERMES_TOKEN_NAME, "active": True, "scopes": ["read_api"],
         "created_at": "2026-08-05T00:00:00Z", "expires_at": "2027-06-01"},
        {"name": HERMES_TOKEN_NAME, "active": True, "scopes": ["api"],
         "created_at": "2026-08-01T00:00:00Z", "expires_at": "2027-01-01"},
    ])) == (PROBE_OK, ["read_api"], "2027-06-01")


def test_garbage_or_missing_created_at_never_beats_a_real_timestamp():
    """codex review: a raw lexicographic sort would let a garbage created_at
    string ("zzz…") outrank every legitimate ISO timestamp. Parse defensively:
    unparseable/missing sorts as oldest-possible, so the real timestamp wins."""
    assert probe_token("t", opener=_rows([
        {"name": HERMES_TOKEN_NAME, "active": True, "scopes": ["api"],
         "created_at": "zzz-not-a-date", "expires_at": "2027-01-01"},
        {"name": HERMES_TOKEN_NAME, "active": True, "scopes": ["read_api"],
         "created_at": "2026-08-05T00:00:00Z", "expires_at": "2027-06-01"},
    ])) == (PROBE_OK, ["read_api"], "2027-06-01")
    assert probe_token("t", opener=_rows([
        {"name": HERMES_TOKEN_NAME, "active": True, "scopes": ["api"],
         "expires_at": "2027-01-01"},
        {"name": HERMES_TOKEN_NAME, "active": True, "scopes": ["read_api"],
         "created_at": "2026-08-05T00:00:00Z", "expires_at": "2027-06-01"},
    ])) == (PROBE_OK, ["read_api"], "2027-06-01")


def test_401_is_an_invalid_token():
    assert probe_token("t", opener=_http(401)) == (INVALID_TOKEN, [], None)


def test_403_means_no_api_scope_at_all_so_glab_cannot_work():
    """Repository-only token: fine for git clone, inert for every glab command."""
    assert probe_token("t", opener=_http(403)) == (PROBE_GIT_ONLY, [], None)


@pytest.mark.parametrize("code", [500, 502, 404])
def test_unexpected_codes_are_undetermined(code):
    assert probe_token("t", opener=_http(code)) == (UNDETERMINED, [], None)


def test_network_failure_is_undetermined_not_a_pass():
    def _boom(*a, **k):
        raise OSError("dns")

    assert probe_token("t", opener=_boom) == (UNDETERMINED, [], None)


# -- storage -----------------------------------------------------------------


@pytest.mark.parametrize(
    "status", [INVALID_TOKEN, UNDETERMINED, PROBE_GIT_ONLY, PROBE_NOT_FOUND]
)
def test_no_lookup_outcome_but_ok_may_store(monkeypatch, tmp_path, status):
    """Fail closed: if we could not read the real scopes, nothing is banked."""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _home(tmp_path)
    with pytest.raises(TokenRejected):
        submit_personal_token(
            profile_name="alice", token="glpat-x",
            tier=TIER_READ, shared_home=shared, prober=lambda *a, **k: (status, [], None),
        )
    assert not (shared / "multitenancy.db").exists() or _vault_empty(shared)


@pytest.mark.parametrize("scopes", [["api", "write_repository"], ["read_api"], []])
def test_read_tier_refuses_wrong_scope_sets(monkeypatch, tmp_path, scopes):
    """An api token submitted as 'read' means the user handed over more than they
    think; a set missing read_repository would fail at git clone."""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _home(tmp_path)
    with pytest.raises(TokenRejected):
        submit_personal_token(
            profile_name="alice", token="glpat-x",
            tier=TIER_READ, shared_home=shared, prober=_ok(scopes),
        )
    assert not (shared / "multitenancy.db").exists() or _vault_empty(shared)


def test_write_claim_with_a_read_only_token_is_refused(monkeypatch, tmp_path):
    """The other direction: the connector would silently fail every write."""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _home(tmp_path)
    with pytest.raises(TokenRejected):
        submit_personal_token(
            profile_name="alice", token="glpat-x",
            tier=TIER_WRITE, shared_home=shared, prober=_ok(READ_SCOPES),
        )


def test_api_scope_satisfies_read_api_requirement_for_the_write_tier(monkeypatch, tmp_path):
    """`api` implies read access, so it must not be reported as missing read_api."""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _home(tmp_path)
    result = submit_personal_token(
        profile_name="alice", token="glpat-x",
        tier=TIER_WRITE, shared_home=shared, prober=_ok(WRITE_SCOPES),
    )
    assert result["stored"] and result["tier"] == TIER_WRITE


def test_submit_retires_the_legacy_file_synchronously(monkeypatch, tmp_path):
    """The submit path itself must retire the file — materialization is not
    invoked on this path, so relying on it would leave the split in place."""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _home(tmp_path)
    legacy = shared / "profiles" / "alice" / "workspace" / "credentials"
    legacy.mkdir(parents=True)
    (legacy / "gitlab.token").write_text("global-token\n", encoding="utf-8")

    result = submit_personal_token(
        profile_name="alice", token="glpat-x",
        tier=TIER_READ, shared_home=shared, prober=_ok(READ_SCOPES),
    )
    assert result["legacy_file_retired"] is True
    assert not (legacy / "gitlab.token").exists()


def test_unremovable_legacy_file_fails_closed_and_stores_nothing(monkeypatch, tmp_path):
    """Half-switched is worse than not switched: refuse rather than split.

    Also the last refusing branch the no-echo sweeps cannot reach (it fires
    after the probe, on the filesystem) — codex delta review named it, so the
    token-absent assertion rides here rather than in a fourth sweep.
    """
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _home(tmp_path)
    legacy = shared / "profiles" / "alice" / "workspace" / "credentials"
    legacy.mkdir(parents=True)
    (legacy / "gitlab.token").write_text("global-token\n", encoding="utf-8")

    def _boom(self):
        raise OSError("read-only fs")

    monkeypatch.setattr(Path, "unlink", _boom)
    with pytest.raises(TokenRejected) as exc:
        submit_personal_token(
            profile_name="alice", token="glpat-SECRETVALUE",
            tier=TIER_READ, shared_home=shared, prober=_ok(READ_SCOPES),
        )
    assert not (shared / "multitenancy.db").exists() or _vault_empty(shared)
    assert "glpat-SECRETVALUE" not in exc.value.reason


@pytest.mark.parametrize("drop", ["vault_profile", "env", "targeting"])
def test_incomplete_runtime_contract_refuses(monkeypatch, tmp_path, drop):
    """Any missing piece means the token is banked but never injected."""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    (shared / "profiles" / "alice").mkdir(parents=True)
    lines = [
        "credentials:", "  - subject_id: kep-prd-skills", "    provider: gitlab",
        "    secret_kind: token", "    target: workspace/credentials/gitlab.token",
    ]
    if drop != "env":
        lines.append("    env: GITLAB_TOKEN")
    if drop != "vault_profile":
        lines.append("    vault_profile: __self__")
    lines.append("    profiles: [bob]" if drop == "targeting" else "    profiles: [alice]")
    (shared / "credential-materialization.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    with pytest.raises(TokenRejected):
        submit_personal_token(
            profile_name="alice", token="glpat-x",
            tier=TIER_READ, shared_home=shared, prober=_ok(READ_SCOPES),
        )
    assert not (shared / "multitenancy.db").exists() or _vault_empty(shared)


def test_refuses_while_the_self_lane_is_not_deployed(monkeypatch, tmp_path):
    """Banking a token the runtime never reads is the worst outcome available:
    the card says configured, and every call still uses the shared credential."""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    shared.mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        "credentials:\n  - subject_id: kep-prd-skills\n    provider: gitlab\n"
        "    secret_kind: token\n    profiles: ['*']\n",
        encoding="utf-8",
    )
    with pytest.raises(TokenRejected, match="尚未开启"):
        submit_personal_token(
            profile_name="alice", token="glpat-x",
            tier=TIER_READ, shared_home=shared, prober=_ok(READ_SCOPES),
        )


@pytest.mark.parametrize(
    "status", [INVALID_TOKEN, UNDETERMINED, PROBE_GIT_ONLY, PROBE_NOT_FOUND]
)
def test_no_rejection_reason_ever_echoes_the_token(monkeypatch, tmp_path, status):
    """Rejection reasons render into Feishu cards that persist in chat history,
    so the submitted token must never appear in one verbatim. Swept across every
    refusing path rather than a single one — a new gate that interpolates the
    token would otherwise slip in unnoticed."""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _home(tmp_path)
    secret = "glpat-SECRETVALUE"
    with pytest.raises(TokenRejected) as exc:
        submit_personal_token(
            profile_name="alice", token=secret,
            tier=TIER_READ, shared_home=shared,
            prober=lambda *a, **k: (status, [], None),
        )
    assert secret not in exc.value.reason


@pytest.mark.parametrize(
    "tier,prober_scopes,why",
    [
        (TIER_READ, WRITE_SCOPES, "只读档收到 api token"),
        (TIER_WRITE, READ_SCOPES, "可写档收到只读 token"),
        (TIER_READ, ["read_api"], "缺 read_repository"),
        ("bogus-tier", READ_SCOPES, "非法档位"),
    ],
)
def test_no_scope_or_tier_rejection_echoes_the_token(
    monkeypatch, tmp_path, tier, prober_scopes, why
):
    """codex review: 原来只扫了 probe 失败一类路径。scope/档位这几条会把档位名和
    scope 名拼进更长的句子，正是最容易顺手把 token 也拼进去的地方。"""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _home(tmp_path)
    secret = "glpat-SECRETVALUE"
    with pytest.raises(TokenRejected) as exc:
        submit_personal_token(
            profile_name="alice", token=secret,
            tier=tier, shared_home=shared, prober=_ok(prober_scopes),
        )
    assert secret not in exc.value.reason, why


def test_runtime_contract_rejection_does_not_echo_the_token(monkeypatch, tmp_path):
    """The runtime-contract path refuses BEFORE the probe runs, so it is the one
    branch the probe-driven sweeps above can never reach."""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    shared.mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        "credentials:\n  - subject_id: kep-prd-skills\n    provider: gitlab\n"
        "    secret_kind: token\n    profiles: ['*']\n",
        encoding="utf-8",
    )
    secret = "glpat-SECRETVALUE"
    with pytest.raises(TokenRejected) as exc:
        submit_personal_token(
            profile_name="alice", token=secret,
            tier=TIER_READ, shared_home=shared, prober=_ok(READ_SCOPES),
        )
    assert secret not in exc.value.reason


def test_never_expiring_token_is_stored_with_a_null_expiry(monkeypatch, tmp_path):
    """sunke 2026-08-06 拍板：有效期归上游 GitLab 管，不是这里该拦的。
    A null row must NOT dead-end the submit — store it with expires_at=None,
    which credentials.py already treats as never-expires (its staleness check
    is guarded on ``expires_at is not None``)."""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _home(tmp_path)
    result = submit_personal_token(
        profile_name="alice", token="glpat-x",
        tier=TIER_READ, shared_home=shared, prober=_ok(READ_SCOPES, expires_at=None),
    )
    assert result["stored"] and result["expires_at"] is None
    assert not _vault_empty(shared), "永久 token 也要真入库"


def test_a_past_expiry_is_accepted_and_stored_as_no_expiry(monkeypatch, tmp_path):
    """codex review 抓到的静默坏状态：过去的日期若原样写进 vault，
    ``credentials.py`` 会按**我们的**时钟判它过期并拒绝注入 —— intake 报「已保存」，
    之后每次调用却悄悄回落到共享凭据。GitLab 会把真过期的 PAT 标为 inactive（那样
    probe 根本拿不到行），所以 active 行带过去日期只意味着我们和 GitLab 的时钟/时区
    不一致。存 None：不收，也不替 GitLab 判。"""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _home(tmp_path)
    result = submit_personal_token(
        profile_name="alice", token="glpat-x",
        tier=TIER_READ, shared_home=shared,
        prober=_ok(READ_SCOPES, expires_at="2000-01-01"),
    )
    assert result["stored"] and result["expires_at"] is None


def test_caller_cannot_supply_an_expiry_at_all(monkeypatch, tmp_path):
    """The deprecated ``expires_on`` shim is gone: the expiry has exactly one
    source, the token's own GitLab row. A caller that still passes a date must
    fail loudly rather than have it silently ignored — silent acceptance is how
    a caller keeps believing it controls the expiry."""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _home(tmp_path)
    with pytest.raises(TypeError):
        submit_personal_token(
            profile_name="alice", token="glpat-x", expires_on="2031-11-31",
            tier=TIER_READ, shared_home=shared, prober=_ok(READ_SCOPES),
        )
    # …and the normal call still stores the probe row's expiry.
    result = submit_personal_token(
        profile_name="alice", token="glpat-x",
        tier=TIER_READ, shared_home=shared, prober=_ok(READ_SCOPES),
    )
    assert result["stored"] and result["expires_at"] == 4070908800000


def _vault_empty(shared: Path) -> bool:
    import sqlite3

    conn = sqlite3.connect(str(shared / "multitenancy.db"))
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM multitenancy_credentials WHERE provider='gitlab'"
        ).fetchone()[0]
    finally:
        conn.close()
    return rows == 0


def test_clean_token_lands_in_the_submitters_own_profile(monkeypatch, tmp_path):
    from hermes_multitenancy.credentials import CredentialStore

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _home(tmp_path)

    result = submit_personal_token(
        profile_name="alice",
        token="glpat-clean",
        tier=TIER_READ,
        shared_home=shared,
        prober=_ok(READ_SCOPES),
    )
    assert result["stored"] and result["tier"] == TIER_READ
    assert result["scopes"] == READ_SCOPES
    assert result["scope_binding_verified"] is False
    # The stored expiry is the probe row's date, converted to UTC-midnight ms.
    assert result["expires_at"] == 4070908800000

    store = CredentialStore(shared / "multitenancy.db")
    try:
        payload = store.get_secret_for_runtime(
            profile_name="alice",
            subject_id="kep-prd-skills",
            provider="gitlab",
            secret_kind="token",
        )
        assert payload["token"] == "glpat-clean"
        # And NOT under the shared profile — that would hand one employee's
        # token to every other user.
        with pytest.raises(PermissionError):
            store.get_secret_for_runtime(
                profile_name="__shared__",
                subject_id="kep-prd-skills",
                provider="gitlab",
                secret_kind="token",
            )
    finally:
        store.close()


def test_subject_id_follows_the_config_so_the_runtime_can_find_it(monkeypatch, tmp_path):
    """Storing under a subject the config doesn't name would strand the token."""
    from hermes_multitenancy.credential_materializer import resolve_runtime_secret
    from hermes_multitenancy.credentials import CredentialStore

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = tmp_path / ".hermes"
    (shared / "profiles" / "alice").mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        "credentials:\n  - subject_id: some-other-subject\n    provider: gitlab\n"
        "    secret_kind: token\n    env: GITLAB_TOKEN\n"
        "    vault_profile: __self__\n    profiles: [alice]\n",
        encoding="utf-8",
    )
    submit_personal_token(
        profile_name="alice", token="glpat-x",
        tier=TIER_READ, shared_home=shared, prober=_ok(READ_SCOPES),
    )

    entry = {"subject_id": "some-other-subject", "provider": "gitlab",
             "secret_kind": "token", "vault_profile": "__self__"}
    store = CredentialStore(shared / "multitenancy.db")
    try:
        payload, vault_profile = resolve_runtime_secret(store, entry, profile_name="alice")
    finally:
        store.close()
    assert payload is not None and vault_profile == "alice"


# -- Feishu submit guards ----------------------------------------------------


@pytest.fixture
def submit_harness(monkeypatch):
    """Everything downstream of the guards is made to SUCCEED.

    That is the whole point: with identity resolution, the form body and the
    vault call all wired to work, the ONLY thing that can stop a submit is a
    guard. A test that instead lets a later step fail would pass even with the
    guards deleted — which is exactly how the first version of these two tests
    was silently vacuous.
    """
    from hermes_multitenancy import feishu_auth_hub_actions as fha

    monkeypatch.setattr(fha, "_signed_chat_id", lambda e: "oc_dm")
    monkeypatch.setattr(fha, "_ctx_message_id", lambda e: "om_known")
    monkeypatch.setattr(fha, "_chat_is_group", lambda e, c: False)
    monkeypatch.setattr(fha, "_is_known_dm_auth_card", lambda m: True)
    monkeypatch.setattr(fha, "_collect_rows", lambda **k: [])
    monkeypatch.setattr(
        fha, "_resolve_operator_profile", lambda e, s: ("alice", "ou_alice", Path("/tmp/p"))
    )
    monkeypatch.setattr(
        fha, "_gitlab_form_value", lambda a: ("glpat-typed", "read")
    )

    class _Uat:
        @staticmethod
        def resolve_shared_home():
            return Path("/tmp/shared")

    monkeypatch.setitem(__import__("sys").modules, "hermes_multitenancy.feishu_uat_auth", _Uat)

    captured: list[dict] = []
    monkeypatch.setattr(
        "hermes_multitenancy.gitlab_token_intake.submit_personal_token",
        lambda **k: (captured.append(k), {"stored": True})[1],
    )
    return fha, captured


def test_harness_reaches_the_vault_when_every_guard_passes(submit_harness):
    """Control for the two guard tests below: this MUST reach the vault.

    Without this, a harness that silently stopped working would make the guard
    tests pass for the wrong reason.
    """
    fha, captured = submit_harness
    fha._handle_gitlab_token_submit(object(), object(), object())
    assert len(captured) == 1
    assert captured[0]["profile_name"] == "alice"
    assert captured[0]["token"] == "glpat-typed"
    # The Feishu path must not resurrect the deprecated expiry argument.
    assert "expires_on" not in captured[0]


def test_group_chat_submit_is_refused(submit_harness, monkeypatch):
    """A credential write must never be reachable from a group chat."""
    fha, captured = submit_harness
    monkeypatch.setattr(fha, "_chat_is_group", lambda e, c: True)

    fha._handle_gitlab_token_submit(object(), object(), object())
    assert captured == [], "group-chat submit must not reach the vault"


def test_submit_from_an_unknown_card_is_refused(submit_harness, monkeypatch):
    """Forwarding the form elsewhere produces a message id we never recorded."""
    fha, captured = submit_harness
    monkeypatch.setattr(fha, "_is_known_dm_auth_card", lambda m: False)

    fha._handle_gitlab_token_submit(object(), object(), object())
    assert captured == [], "submit from an unrecognised card must not reach the vault"


def test_profile_comes_from_the_signed_operator_never_the_payload(submit_harness, monkeypatch):
    """The form body is untrusted input: it supplies the token, never the identity."""
    fha, captured = submit_harness
    # The signed operator is the only identity source; a hostile form body
    # carrying someone else's profile must not change where the token lands.
    monkeypatch.setattr(
        fha, "_gitlab_form_value", lambda a: ("glpat-typed", "read")
    )
    monkeypatch.setattr(
        fha, "_resolve_operator_profile", lambda e, s: ("alice", "ou_alice", Path("/tmp/p"))
    )

    fha._handle_gitlab_token_submit(object(), object(), object())
    assert captured[0]["profile_name"] == "alice"


def test_gitlab_form_card_has_no_expiry_control_and_says_expiry_is_read():
    """Render-level regression guard (codex review finding): all other tests
    exercise parsing and the vault path, so a card edit that restores a
    gitlab_expiry input or the old '把日期抄回来' copy would pass every one of
    them. Walk the actual card payload instead."""
    from hermes_multitenancy.feishu_credential_hub_cards import (
        GITLAB_FORM,
        build_gitlab_token_form_card,
    )

    card = build_gitlab_token_form_card()

    def _walk(node):
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from _walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from _walk(value)

    nodes = list(_walk(card))
    names = {n.get("name") for n in nodes if isinstance(n.get("name"), str)}
    assert GITLAB_FORM in names, "the form container itself must still render"
    assert "gitlab_expiry" not in names
    copy = "".join(str(n.get("content") or "") for n in nodes)
    assert "直接从 GitLab 读" in copy
    # Marker-agnostic guard against the old step-2 wording: the original text
    # was `**填一个到期日**（GitLab 允许不填…` — asserting on a substring that
    # ignores the Markdown ** so a resurrected old line cannot hide behind
    # formatting (codex delta-review catch: the first version of this assert
    # included the parens without ** and matched nothing).
    assert "允许不填" not in copy


def test_form_value_parsing_handles_dict_and_json_string():
    from hermes_multitenancy import feishu_auth_hub_actions as fha

    class _A:
        # A stale client still sending gitlab_expiry must not break parsing.
        form_value = {
            "gitlab_token": " t ", "gitlab_expiry": " 2099-01-01 ", "gitlab_tier": "read",
        }

    assert fha._gitlab_form_value(_A()) == ("t", "read")

    class _B:
        form_value = '{"gitlab_token": "j", "gitlab_tier": "write"}'

    assert fha._gitlab_form_value(_B()) == ("j", "write")

    class _C:
        form_value = None

    assert fha._gitlab_form_value(_C()) == ("", "")


def test_stored_scopes_are_marked_unverified_so_they_are_never_audit_evidence(monkeypatch, tmp_path):
    """The tier check catches mistakes; it is not an enforced boundary.

    No API on CE 14.10 binds a token value to its metadata row, so a user could
    name a weak token `hermes` and submit a stronger one. The row must say so —
    a scope label taken on trust is exactly how an admin credential once sat in
    the vault labelled read-only (DEBT.md 2026-08-03).
    """
    import sqlite3

    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _home(tmp_path)
    submit_personal_token(
        profile_name="alice", token="glpat-x",
        tier=TIER_READ, shared_home=shared, prober=_ok(READ_SCOPES),
    )
    conn = sqlite3.connect(str(shared / "multitenancy.db"))
    try:
        stored = conn.execute(
            "SELECT scopes_json FROM multitenancy_credentials WHERE provider='gitlab'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert SCOPE_BINDING_UNVERIFIED in stored


# -- group-profile intake (group-agent-gitlab-binding) ------------------------


def _group_home(tmp_path, profile="feishu_group_x"):
    """Shared home with a wildcard self-lane entry and one routed group."""
    from hermes_multitenancy.routing import RoutingTable

    shared = tmp_path / ".hermes"
    (shared / "profiles" / profile).mkdir(parents=True)
    (shared / "credential-materialization.yaml").write_text(
        "credentials:\n  - subject_id: kep-prd-skills\n    provider: gitlab\n"
        "    secret_kind: token\n    target: workspace/credentials/gitlab.token\n"
        "    env: GITLAB_TOKEN\n    vault_profile: __self__\n    profiles: ['*']\n",
        encoding="utf-8",
    )
    table = RoutingTable(shared / "multitenancy.db")
    try:
        table.upsert_group(chat_id="oc_g", profile_name=profile, owner_open_id="ou_owner")
    finally:
        table.close()
    return shared


def test_group_profile_passes_the_targeting_gate_before_first_row(monkeypatch, tmp_path):
    """A routed group is targetable the moment its owner binds: the row it
    creates is exactly what makes `_target_profiles` pick the group up, so the
    banked-but-never-injected failure the gate exists for cannot happen."""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _group_home(tmp_path)

    result = submit_personal_token(
        profile_name="feishu_group_x", token="glpat-group-token",
        tier=TIER_READ, shared_home=shared, prober=_ok(READ_SCOPES),
        group_owner_open_id="ou_owner",
    )

    assert result["stored"] is True
    assert result["profile_name"] == "feishu_group_x"


def test_unrouted_group_shaped_profile_is_still_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _group_home(tmp_path)

    with pytest.raises(TokenRejected, match="适用范围"):
        submit_personal_token(
            profile_name="feishu_group_missing", token="glpat-x",
            tier=TIER_READ, shared_home=shared, prober=_ok(READ_SCOPES),
        )


def test_group_profile_without_owner_proof_is_refused(monkeypatch, tmp_path):
    """grok round-1 #3 采纳：intake 层自己也验群主 —— broker 之外的任何直连
    调用方都不能只报 profile 名就把 token 银行进群。"""
    monkeypatch.setenv("HERMES_MULTITENANCY_CREDENTIAL_KEY", "test-key")
    shared = _group_home(tmp_path)

    for claimed in (None, "", "ou_not_the_owner"):
        with pytest.raises(TokenRejected, match="群主"):
            submit_personal_token(
                profile_name="feishu_group_x", token="glpat-x",
                tier=TIER_READ, shared_home=shared, prober=_ok(READ_SCOPES),
                group_owner_open_id=claimed,
            )
