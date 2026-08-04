"""Golden byte-identical regression: registry path == legacy reader path.

``credential_hub.collect_credential_statuses`` now routes through the Connector
Registry (enrich → ConnectorStatus → compat.to_credential_row). This file is the
hard invariant: the PUBLIC path must produce ``CredentialRow.to_dict()`` output
EXACTLY equal to the pre-registry low-level reader
``credential_hub._collect_credential_rows`` — field for field, including the
``action`` dict (with extra keys like keep-record's ``command``) and
``required_by``. If the registry ever drops/reorders/renames a legacy field this
test fails.

It also pins the action-dict round-trip per ``kind`` and the kep-cli expired-JWT
→ needs_auth collapse through the registry.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time


# --- helpers (mirrors test_credential_hub conventions) ----------------------


def _make_jwt(exp: int) -> str:
    def _seg(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{_seg({'alg': 'HS256'})}.{_seg({'exp': exp})}.sig"


def _kep_run_stub(*, status_out: str, token_out: str, token_rc: int = 0):
    class _StatusProc:
        returncode = 0
        stdout = status_out
        stderr = ""

    class _TokenProc:
        returncode = token_rc
        stdout = token_out
        stderr = ""

    def _run(cmd, *a, **k):
        return _TokenProc() if "token" in cmd else _StatusProc()

    return _run


def _kep_bin(monkeypatch, tmp_path):
    bin_path = tmp_path / "bin" / "kep-auth"
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_KEP_AUTH_BIN", str(bin_path))
    return bin_path


def _mock_kep_identity(monkeypatch, *, profile_name: str, expires_at: int):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps({
                "errorCode": 0,
                "ok": True,
                "data": {"payload": {"name": profile_name, "exp": expires_at}},
            }).encode()

    from hermes_multitenancy import kep_live_identity
    monkeypatch.setattr(kep_live_identity, "_urlopen", lambda *_a, **_k: _Response())


def _install_keep_record(home, *, token="tok123", username="owner"):
    """keep-record skill + authenticated keepai env/marker under the profile."""
    skill = home.parent / "skills" / "Keep" / "keep-record"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "name: keep-record\nget_qrcode keep_auth_token\n", encoding="utf-8"
    )
    keepai = home / ".keepai"
    keepai.mkdir(parents=True)
    future = int(time.time() * 1000) + 7 * 24 * 3600 * 1000
    (keepai / ".env").write_text(
        f"keep_auth_token={token}\nkeep_auth_token_expired={future}\nkeep_username={username}\n",
        encoding="utf-8",
    )
    (keepai / "webui-auth-verified.json").write_text(
        json.dumps(
            {"token_sha256": hashlib.sha256(token.encode()).hexdigest(), "account_hint": username}
        ),
        encoding="utf-8",
    )


# --- the golden equality ----------------------------------------------------


def test_public_path_to_dict_equals_low_level_reader(monkeypatch, tmp_path):
    """collect_credential_statuses == _collect_credential_rows, dict-for-dict."""
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    home = tmp_path / "profiles" / "owner" / "home"
    home.mkdir(parents=True)
    _install_keep_record(home, token="golden-tok", username="owner")
    # FROZEN expiry: the reader is invoked once per collect_* call (public path +
    # low-level path), so a time.time()-derived value would differ by ~1ms between
    # the two calls and make the byte-identical comparison flaky. A constant proves
    # the contract without that timing artifact.
    frozen_exp = int(time.time() * 1000) + 3600_000
    monkeypatch.setattr(
        feishu_uat_auth,
        "credential_status",
        lambda **kw: {"status": "valid", "expires_at": frozen_exp},
    )
    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda **k: None)

    public_rows = credential_hub.collect_credential_statuses(
        profile_name="owner", open_id="ou_owner", shared_home=tmp_path
    )
    low_rows = credential_hub._collect_credential_rows(
        profile_name="owner", open_id="ou_owner", shared_home=tmp_path
    )

    public_dicts = [r.to_dict() for r in public_rows]
    low_dicts = [r.to_dict() for r in low_rows]
    assert public_dicts == low_dicts
    # And the order/ids are the canonical connector set.
    assert [d["id"] for d in public_dicts] == list(credential_hub.CREDENTIAL_ORDER)


def test_action_dict_round_trip_is_lossless_per_kind(monkeypatch, tmp_path):
    """Each connector's legacy action kind survives the registry round-trip.

    keep-record's action carries an EXTRA ``command`` key — the most fragile case
    for AuthAction.from_dict→to_dict — so it is asserted explicitly.
    """
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    home = tmp_path / "profiles" / "owner" / "home"
    home.mkdir(parents=True)
    _install_keep_record(home, token="rt-tok", username="owner")
    frozen_exp = int(time.time() * 1000) + 3600_000  # constant: see golden test note
    monkeypatch.setattr(
        feishu_uat_auth,
        "credential_status",
        lambda **kw: {"status": "valid", "expires_at": frozen_exp},
    )
    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda **k: None)

    public = {
        r.id: r.to_dict()
        for r in credential_hub.collect_credential_statuses(
            profile_name="owner", open_id="ou_owner", shared_home=tmp_path
        )
    }
    low = {
        r.id: r.to_dict()
        for r in credential_hub._collect_credential_rows(
            profile_name="owner", open_id="ou_owner", shared_home=tmp_path
        )
    }

    # Every kind seen must match the legacy action exactly.
    assert public["lark-cli"]["action"]["kind"] == "feishu_device_flow"
    assert public["keep-record"]["action"]["kind"] == "skill_flow"
    # The extra command key must survive (this is the lossless-extra assertion).
    assert public["keep-record"]["action"].get("command") == "/keep-record auth"
    assert public["kep-cli-online"]["action"]["kind"] == "oauth_url"
    assert public["kep-cli-online"]["action"]["env"] == "online"
    assert public["kep-cli-pre"]["action"]["kind"] == "oauth_url"
    assert public["kep-cli-pre"]["action"]["env"] == "pre"
    assert public["feishu-project"]["action"]["kind"] == "oauth_url"
    # gitlab 拆成两行后，全局那行是管理员运维的纯陈述 —— 它 *没有* 员工可执行的操作，
    # 所以 action 必须是 None 而不是伪造一个 manual。空 action 被伪造成
    # {"kind":"manual","label":""} 正是下游被迫拿空 label 当哨兵的根源。
    assert public["gitlab"]["action"] is None
    assert public["gitlab-personal"]["action"]["kind"] == "manual"
    assert public["gitlab-personal"]["action"]["label"]  # 个人行必须带可点的标签

    for cid in ("lark-cli", "feishu-project", "keep-record", "kep-cli-online", "kep-cli-pre",
                "gitlab", "gitlab-personal"):
        assert public[cid]["action"] == low[cid]["action"], cid


def test_connector_status_serializer_does_not_fabricate_an_action():
    """`/connectors` must报出「没有操作」，而不是伪造一个空 manual。

    这一层是 WebUI 真正读的产物。伪造成 {"kind":"manual","label":""} 会把「这张卡
    由管理员运维、员工无可执行操作」的语义抹掉，逼客户端拿空 label 当哨兵 —— 该脆性
    已实际咬过一次（webui 降级态全员空 label，去掉客户端兜底后一颗按钮都不剩）。
    """
    from hermes_multitenancy.connectors.models import AuthAction, ConnectorStatus

    def _status(action):
        return ConnectorStatus(
            id="x", title="X", provider="p", installed=True, status="configured",
            action=action,
        ).to_dict()

    assert _status(None)["action"] is None
    assert _status(AuthAction(kind="manual", label="绑定"))["action"] == {
        "kind": "manual", "label": "绑定",
    }


def test_kep_cli_expired_jwt_collapses_to_needs_auth_through_registry(monkeypatch, tmp_path):
    """The kep-cli stale-token bug stays fixed on the PUBLIC registry path."""
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    home = tmp_path / "profiles" / "owner" / "home"
    home.mkdir(parents=True)
    # Install a kep-cli-requiring skill so kep is reported installed.
    skill = home.parent / "skills" / "kep-hades-cli"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "name: kep-hades-cli\ntags: [kep-cli]\nkep-auth --env online\n", encoding="utf-8"
    )
    monkeypatch.setattr(feishu_uat_auth, "credential_status", lambda **kw: {"status": "missing"})
    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda **k: None)
    _kep_bin(monkeypatch, tmp_path)
    past = int(credential_hub._now_ms() / 1000) - 3600  # expired 1h ago
    monkeypatch.setattr(
        credential_hub,
        "_run",
        _kep_run_stub(
            status_out="state: valid\noperator: owner <owner@keep.com>\n",
            token_out=_make_jwt(past) + "\n",
        ),
    )
    _mock_kep_identity(monkeypatch, profile_name="owner", expires_at=past)

    rows = credential_hub.collect_credential_statuses(
        profile_name="owner", open_id="ou_owner", shared_home=tmp_path
    )
    kep = next(r for r in rows if r.id == "kep-cli-online")
    assert kep.status == "needs_auth"
    assert kep.expires_at == past * 1000


def test_kep_cli_environment_statuses_round_trip_through_registry(monkeypatch, tmp_path):
    """pre/online child statuses must survive registry enrichment and compat."""
    from hermes_multitenancy import credential_hub, feishu_uat_auth

    home = tmp_path / "profiles" / "owner" / "home"
    home.mkdir(parents=True)
    skill = home.parent / "skills" / "kep-trevi-delivery-orchestrate"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "name: kep-trevi-delivery-orchestrate\ntags: [kep-cli]\n默认全程 `--env pre` 演练\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(feishu_uat_auth, "credential_status", lambda **kw: {"status": "missing"})
    monkeypatch.setattr(credential_hub, "_meegle_invocation", lambda **k: None)
    _kep_bin(monkeypatch, tmp_path)
    future = int(credential_hub._now_ms() / 1000) + 3600

    class _Proc:
        def __init__(self, stdout: str, returncode: int = 0):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, *a, **k):
        if "token" in cmd:
            return _Proc(_make_jwt(future) + "\n")
        env = cmd[cmd.index("--env") + 1]
        if env == "online":
            return _Proc("state: valid\noperator: owner <owner@keep.com>\n")
        if env == "pre":
            return _Proc("state: not logged in\n", returncode=3)
        raise AssertionError(f"unexpected env in {cmd!r}")

    monkeypatch.setattr(credential_hub, "_run", fake_run)
    _mock_kep_identity(monkeypatch, profile_name="owner", expires_at=future)
    rows = credential_hub.collect_credential_statuses(
        profile_name="owner", open_id="ou_owner", shared_home=tmp_path
    )
    by_id = {r.id: r.to_dict() for r in rows}
    kep = by_id["kep-cli-pre"]
    online = by_id["kep-cli-online"]

    assert kep["status"] == "needs_auth"
    assert kep["action"]["env"] == "pre"
    assert online["status"] == "authenticated"
    assert online["action"]["env"] == "online"
