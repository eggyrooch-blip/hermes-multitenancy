"""Fill-skill skeleton + confirm callback + idempotent write (SPEC P4/P5).

Covers extraction prefill discipline (money never silent-prefilled, 否定/清空,
user value not clobbered), the confirm form card's openclaw 四坑 (single form,
submit button value, operation_id 双写, high-risk input empty), and the confirm
handler's write invariants: exactly-one backend record under replay / double
confirm / crash-retry, owner + nonce guards, and the credential-expiry re-auth
branch. All pure — an in-memory store + a mock writer, no Feishu SDK.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_multitenancy import push_card_confirm as confirm
from hermes_multitenancy import push_fill_form as fill
from hermes_multitenancy import push_registry as reg
from hermes_multitenancy import push_scenes as scenes

SCENE = scenes.get_scene("dev-acceptance-claim")


@pytest.fixture
def store():
    s = reg.PushRegistryStore(":memory:")
    yield s
    s.close()


def _clarifying(store, *, open_id="ou_alice"):
    """Create a row and walk it to ``clarifying`` (as after the fill skill ran)."""
    res = store.create(
        scene="dev-acceptance-claim", skill="push-fill-form",
        target_open_id=open_id, profile_name="alice-profile",
        business_key=f"dev-acceptance-claim:{open_id}:2026-07-20",
    )
    rid = res.row["registry_id"]
    store.mark_sent(rid, message_id="om_card_1")
    assert store.advance_status(rid, expect=reg.STATUS_PENDING, to=reg.STATUS_CLARIFYING)
    return rid, store.get(rid)["nonce"]


def _form_value(**over):
    fv = {"amount": 68, "date": "上周五", "category": "打车", "reason": "去客户现场"}
    fv.update(over)
    return fv


# ===================== P4: extraction discipline =========================

def test_money_prefilled_only_when_confident():
    # 代填 discipline (sunke's product call, overriding the P0-4 default): a
    # HIGH-confidence money extraction IS pre-filled into the input (marked
    # ai=True → renders "AI 提取，请核对") so the user reviews-and-confirms in one
    # click, while still echoed for二次回显 and no longer counted as missing.
    sub = fill.merge(SCENE, None, "今天打车去客户现场花了58")
    amt = sub.get("amount")
    assert amt is not None and amt.has_value() and amt.value == 58
    assert amt.ai is True  # still flagged as an AI suggestion to core
    assert fill._echo_of(sub, "amount") == 58
    assert sub["category"].value == "打车"
    assert "58" in (fill.money_echo(SCENE, sub) or "")
    assert not any(f.key == "amount" for f in fill.missing_fields(SCENE, sub))

    # A LOW-confidence number (no money context) is NOT ridden in — it stays
    # empty and is asked, so a weak guess never pre-fills money.
    low = fill.merge(SCENE, None, "编号 58")
    lo_amt = low.get("amount")
    assert lo_amt is None or not lo_amt.has_value()
    assert any(f.key == "amount" for f in fill.missing_fields(SCENE, low))


def test_replace_updates_money_echo():
    sub = fill.merge(SCENE, None, "今天打车去客户现场花了58")
    sub = fill.merge(SCENE, sub, "金额改成68")
    assert fill._echo_of(sub, "amount") == 68
    assert "68" in fill.money_echo(SCENE, sub)


def test_negation_clears_field():
    sub = fill.merge(SCENE, None, "类目是打车")
    assert sub["category"].value == "打车"
    sub = fill.merge(SCENE, sub, "不是打车")
    assert "category" not in sub or not sub["category"].has_value()


def test_reset_all_clears_everything():
    sub = fill.merge(SCENE, None, "打车 上周五 去客户现场")
    assert sub  # something extracted
    sub = fill.merge(SCENE, sub, "重新来")
    assert not fill.submission_values(SCENE, sub)


def test_user_typed_value_not_clobbered_by_ai():
    # user typed category in the form (ai=False, full confidence)
    sub = {"category": fill.FieldState(value="餐饮", confidence=1.0, ai=False)}
    sub = fill.merge(SCENE, sub, "打车")  # AI now says 打车
    assert sub["category"].value == "餐饮"  # user wins


# ===================== P4: confirm form card (四坑) =======================

def test_confirm_card_single_form_and_submit_value():
    sub = fill.merge(SCENE, None, "打车 上周五 去客户现场 花了58")
    card = fill.build_confirm_card(
        SCENE, sub, registry_id="pcr_x", nonce="nnn", operation_id="op123",
    )
    forms = [e for e in card["body"]["elements"] if e.get("tag") == "form"]
    assert len(forms) == 1  # ONE form container只回传 form_value
    buttons = [e for e in forms[0]["elements"] if e.get("tag") == "button"]
    assert len(buttons) == 1
    btn = buttons[0]
    # submit button MUST carry a value (else Feishu 200340 silent drop)
    assert btn["form_action_type"] == "submit"
    assert btn["value"]["hermes_action"] == "push_confirm"
    assert btn["value"]["registry_id"] == "pcr_x"
    assert btn["value"]["nonce"] == "nnn"
    # operation_id 双写: in the value AND encoded in the control name
    assert btn["value"]["operation_id"] == "op123"
    assert "op123" in btn["name"]


def test_confirm_card_high_risk_amount_prefilled_and_flagged():
    sub = fill.merge(SCENE, None, "打车 上周五 去客户现场 花了58")
    card = fill.build_confirm_card(SCENE, sub, registry_id="r", nonce="n", operation_id="o")
    form = next(e for e in card["body"]["elements"] if e.get("tag") == "form")
    controls = {e.get("name"): e for e in form["elements"] if e.get("name") in {"amount", "category", "date"}}
    # 代填: high-confidence money IS pre-filled into the input (review-and-confirm).
    assert controls["amount"].get("default_value") == "58"
    # and the input is annotated "AI 提取，请核对" so the user knows to double-check.
    labels = [e.get("content", "") for e in form["elements"] if e.get("tag") == "markdown"]
    assert any("金额" in t and "AI 提取，请核对" in t for t in labels)
    # low-risk enum is prefilled as an initial_option.
    assert controls["category"].get("initial_option") == "打车"


def test_delivery_mode_update_vs_new():
    assert fill.delivery_mode({"message_id": "om_1"}) == fill.DELIVER_UPDATE
    assert fill.delivery_mode({"message_id": None}) == fill.DELIVER_NEW
    assert fill.delivery_mode({}) == fill.DELIVER_NEW


# ===================== P5: happy path + write invariants ==================

def _confirm(store, rid, nonce, writer, *, form=None, ops=None):
    return confirm.handle_confirm(
        registry_id=rid, nonce=nonce,
        operator_open_ids=ops if ops is not None else {"ou_alice"},
        form_value=form if form is not None else _form_value(),
        store=store, writer_lookup=lambda name: writer,
    )


def test_confirm_commits_and_writes_exactly_one_record(store):
    rid, nonce = _clarifying(store)
    writer = confirm.MockKepPreClaimWriter()
    res = _confirm(store, rid, nonce, writer)
    assert res.kind == "committed"
    assert res.written is True
    assert store.get(rid)["status"] == reg.STATUS_COMMITTED
    # backend has exactly one record, keyed by write_idempotency_key = registry_id
    assert writer.write_calls == 1
    assert len(writer.records) == 1
    assert rid in writer.records
    # reason carries the deterministic marker [PAI-ACC-{registry_id}]
    marker = SCENE.deterministic_marker.format(registry_id=rid)
    assert marker in writer.records[rid]["reason"]
    assert len(writer.find_by_marker(marker)) == 1
    # committed card says 已录入✅ with no submit/retry button
    assert res.card["header"]["template"] == "green"
    assert not any(e.get("tag") == "button" for e in res.card["body"]["elements"])


def test_replay_after_commit_is_noop_still_one_record(store):
    rid, nonce = _clarifying(store)
    writer = confirm.MockKepPreClaimWriter()
    _confirm(store, rid, nonce, writer)
    # nonce is cleared on commit; a replay of the old callback no-ops.
    res2 = _confirm(store, rid, nonce, writer)
    assert res2.kind == "noop"
    assert writer.write_calls == 1
    assert len(writer.records) == 1


def test_double_confirm_same_key_dedupes_to_one(store):
    # Simulate a lost-CAS second click by pre-advancing to confirmed with the
    # nonce kept, then firing two confirms — the idempotency key collapses them.
    rid, nonce = _clarifying(store)
    writer = confirm.MockKepPreClaimWriter()
    # first click commits
    _confirm(store, rid, nonce, writer)
    # a stale in-flight duplicate that still carries the (now-cleared) nonce
    res = _confirm(store, rid, nonce, writer)
    assert res.kind == "noop"
    assert len(writer.records) == 1


def test_mock_writer_is_idempotent_on_key():
    writer = confirm.MockKepPreClaimWriter()
    a = writer.write(scene=SCENE, values={"amount": 68, "reason": "x"},
                     registry_id="pcr_1", write_idempotency_key="pcr_1", profile_name="p")
    b = writer.write(scene=SCENE, values={"amount": 999, "reason": "y"},
                     registry_id="pcr_1", write_idempotency_key="pcr_1", profile_name="p")
    assert a.backend_id == b.backend_id
    assert len(writer.records) == 1  # second write did not create a record


# ===================== P5: owner + nonce guards ==========================

def test_non_owner_rejected_no_write(store):
    rid, nonce = _clarifying(store)
    writer = confirm.MockKepPreClaimWriter()
    res = confirm.handle_confirm(
        registry_id=rid, nonce=nonce, operator_open_ids={"ou_mallory"},
        form_value=_form_value(), store=store, writer_lookup=lambda n: writer,
    )
    assert res.kind == "not_owner"
    assert writer.write_calls == 0
    assert store.get(rid)["status"] == reg.STATUS_CLARIFYING  # untouched


def test_nonce_replay_rejected_no_write(store):
    rid, _nonce = _clarifying(store)
    writer = confirm.MockKepPreClaimWriter()
    res = _confirm(store, rid, "forged-nonce", writer)
    assert res.kind == "reject"
    assert writer.write_calls == 0


def test_missing_required_field_rejected(store):
    rid, nonce = _clarifying(store)
    writer = confirm.MockKepPreClaimWriter()
    res = _confirm(store, rid, nonce, writer, form=_form_value(amount=""))
    assert res.kind == "reject"
    assert writer.write_calls == 0


# ===================== P5: credential-expiry branch ======================

def test_expired_credential_guides_reauth_then_retry_succeeds(store):
    rid, nonce = _clarifying(store)
    writer = confirm.MockKepPreClaimWriter()
    writer.credential_expired = True
    res = _confirm(store, rid, nonce, writer)
    assert res.kind == "reauth"
    assert writer.records == {}  # kep零写入
    # a re-auth button (cred_auth) + a retry button are on the card
    actions = [e.get("value", {}).get("hermes_action") for e in res.card["body"]["elements"]
               if e.get("tag") == "button"]
    assert "cred_auth" in actions and "push_confirm" in actions
    # row stays writable (confirmed, nonce kept) so retry resumes录入
    assert store.get(rid)["status"] == reg.STATUS_CONFIRMED

    # user re-authenticates → retry with the SAME nonce commits
    writer.credential_expired = False
    res2 = _confirm(store, rid, nonce, writer)
    assert res2.kind == "committed"
    assert len(writer.records) == 1


def test_reauth_retry_without_form_value_still_commits(store):
    # The reauth/retry cards are PLAIN buttons (not inside a form), so a real
    # retry click carries NO form_value. The handler must recover the confirmed
    # payload from submission_json and commit — not reject as "请填写完整"
    # (finding retry-card-has-no-form).
    rid, nonce = _clarifying(store)
    writer = confirm.MockKepPreClaimWriter()
    writer.credential_expired = True
    res = _confirm(store, rid, nonce, writer)  # first click: full form, expired
    assert res.kind == "reauth"
    assert store.get(rid)["status"] == reg.STATUS_CONFIRMED
    assert store.get(rid)["submission_json"] is not None  # payload persisted at CAS

    # re-auth done, click 重试 — that button submits NO form_value
    writer.credential_expired = False
    res2 = confirm.handle_confirm(
        registry_id=rid, nonce=nonce, operator_open_ids={"ou_alice"},
        form_value=None, store=store, writer_lookup=lambda n: writer,
    )
    assert res2.kind == "committed" and res2.written is True
    assert store.get(rid)["status"] == reg.STATUS_COMMITTED
    assert len(writer.records) == 1
    assert writer.records[rid]["amount"] == 68  # recovered the confirmed value


def test_write_failure_retry_without_form_value_commits(store):
    # A transient write failure leaves the row `confirmed` + a 重试 button (a
    # plain button, no form). Retrying with NO form_value must recover the
    # confirmed payload and commit (finding retry-card-has-no-form).
    rid, nonce = _clarifying(store)

    class _FlakyWriter(confirm.MockKepPreClaimWriter):
        def __init__(self):
            super().__init__()
            self.fail_next = True

        def write(self, **kw):
            if self.fail_next:
                self.fail_next = False
                self.write_calls += 1
                return confirm.WriteResult(ok=False, error="transient")
            return super().write(**kw)

    writer = _FlakyWriter()
    res = _confirm(store, rid, nonce, writer)  # full form, write fails
    assert res.kind == "failed"
    assert store.get(rid)["status"] == reg.STATUS_CONFIRMED

    res2 = confirm.handle_confirm(
        registry_id=rid, nonce=nonce, operator_open_ids={"ou_alice"},
        form_value=None, store=store, writer_lookup=lambda n: writer,
    )
    assert res2.kind == "committed"
    assert len(writer.records) == 1


# ===================== P5: concurrency / failure invariants ==============

def test_lost_clarify_cas_never_writes(store):
    # A concurrent second click that loses the clarifying→confirmed CAS must NOT
    # call the writer (finding lost-cas-falls-through-to-write — otherwise a
    # double-click double-writes). Simulate the race: the CAS returns False while
    # a "winner" has already moved the row to confirmed.
    rid, nonce = _clarifying(store)
    writer = confirm.MockKepPreClaimWriter()
    real_advance = store.advance_status

    def racing_advance(registry_id, *, expect, to, **kw):
        if expect == reg.STATUS_CLARIFYING and to == reg.STATUS_CONFIRMED:
            real_advance(registry_id, expect=expect, to=to, **kw)  # the winner claims it
            return False  # ...but THIS caller lost the CAS
        return real_advance(registry_id, expect=expect, to=to, **kw)

    store.advance_status = racing_advance
    try:
        res = _confirm(store, rid, nonce, writer)
    finally:
        store.advance_status = real_advance

    assert res.kind == "retry"
    assert res.written is False
    assert writer.write_calls == 0  # the loser NEVER wrote
    assert store.get(rid)["status"] == reg.STATUS_CONFIRMED  # winner's claim stands


def test_writer_exception_becomes_visible_failed_card(store):
    # writer.write raising must not be swallowed leaving the row stuck confirmed
    # with no failure card (finding writer-exception-leaves-confirmed-silent).
    rid, nonce = _clarifying(store)

    class _BoomWriter(confirm.MockKepPreClaimWriter):
        def write(self, **kw):
            raise RuntimeError("kep exploded")

    res = _confirm(store, rid, nonce, _BoomWriter())
    assert res.kind == "failed"
    assert res.written is False
    # a retry button is offered and the failure is recorded — never silent
    assert any(e.get("tag") == "button" for e in res.card["body"]["elements"])
    row = store.get(rid)
    assert row["status"] == reg.STATUS_CONFIRMED
    assert row["last_error"]


def test_commit_cas_loss_reports_noop_not_false_green(store):
    # If the confirmed→committed CAS loses (reconcile already committed), we must
    # NOT report a green written=True card (finding commit-cas-result-ignored).
    rid, nonce = _clarifying(store)
    writer = confirm.MockKepPreClaimWriter()
    real_advance = store.advance_status

    def racing_advance(registry_id, *, expect, to, **kw):
        if expect == reg.STATUS_CONFIRMED and to == reg.STATUS_COMMITTED:
            passthrough = {k: v for k, v in kw.items() if k in ("clear_nonce", "submission")}
            real_advance(registry_id, expect=expect, to=to, **passthrough)  # winner commits
            return False  # ...but THIS caller's commit CAS lost
        return real_advance(registry_id, expect=expect, to=to, **kw)

    store.advance_status = racing_advance
    try:
        res = _confirm(store, rid, nonce, writer)
    finally:
        store.advance_status = real_advance

    assert res.kind == "noop"  # committed by another actor — not a false green
    assert res.written is False
    assert store.get(rid)["status"] == reg.STATUS_COMMITTED
    assert len(writer.records) == 1  # backend still恰好一条


# ===================== P5: kep-cli argv discipline =======================

def test_kep_cli_args_are_a_list_shell_injection_inert():
    hostile_reason = "去客户现场; rm -rf / && curl evil"
    args = confirm.build_kep_cli_args(
        SCENE, {"amount": 68, "date": "上周五", "category": "打车", "reason": "去客户现场"},
        registry_id="pcr_1", profile_name="alice",
        reason_with_marker=f"{hostile_reason} [PAI-ACC-pcr_1]",
    )
    assert isinstance(args, list)
    assert all(isinstance(a, str) for a in args)
    # idempotency key threaded as its own argv pair
    assert "--idempotency-key" in args
    assert args[args.index("--idempotency-key") + 1] == "pcr_1"
    # the hostile reason is ONE inert argv element — never split into shell tokens
    assert any(hostile_reason in a for a in args)
    assert "68" in args  # amount passed positionally, as a string element


# ===================== P5: writer registry default =======================

def test_default_writer_is_http_shell_endpoint_bound_later():
    # No override → an endpoint-LESS HTTP shell; the real 回调地址 is resolved and
    # bound per push at confirm time (push > scene > env). Unknown writer → None.
    w = confirm.get_writer("kep-pre-claim-writer")
    assert isinstance(w, confirm.HttpKepPreClaimWriter)
    assert w.url == ""  # shell — bound in _bind_endpoint
    assert confirm.get_writer("no-such-writer") is None


# ===================== P5: env-gated HTTP writer =========================

import json as _json  # noqa: E402
import urllib.error as _uerr  # noqa: E402
import urllib.request as _ureq  # noqa: E402


class _FakeResp:
    """Minimal stand-in for the urlopen() context manager (a 2xx response)."""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_writer_success_posts_json_with_idempotency_key(monkeypatch):
    # HERMES_PUSH_CARD_WRITER_URL set → get_writer hands back the HTTP writer,
    # which POSTs a JSON body (submitted fields + write_idempotency_key) and
    # treats a 2xx parseable response as ok. No real network — urlopen faked.
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["ctype"] = req.headers.get("Content-type")
        captured["body"] = _json.loads(req.data.decode("utf-8"))
        return _FakeResp(_json.dumps({"ok": True, "deduped": False, "record": {"seq": 1}}))

    monkeypatch.setattr(confirm, "_urlopen", fake_urlopen)

    # The writer is constructed with its resolved endpoint (get_writer now hands
    # back an endpoint-less shell that _bind_endpoint fills in; this asserts the
    # POST shape once a url is bound).
    writer = confirm.HttpKepPreClaimWriter("http://127.0.0.1:8971/api/claims")
    res = writer.write(
        scene=SCENE,
        values={"amount": 68, "date": "2026-07-20", "category": "打车", "reason": "去客户现场"},
        registry_id="pcr_1", write_idempotency_key="pcr_1", profile_name="alice",
    )
    assert res.ok is True
    assert res.backend_id == "1"
    assert captured["url"] == "http://127.0.0.1:8971/api/claims"
    assert captured["timeout"] == 10.0
    assert captured["ctype"] == "application/json"
    assert captured["body"]["write_idempotency_key"] == "pcr_1"
    # deterministic marker threaded into reason so the backend can verify 恰好一条
    assert "[PAI-ACC-pcr_1]" in captured["body"]["reason"]


def test_http_writer_failure_is_visible_via_retry_card(monkeypatch, store):
    # A non-2xx from the backend → WriteResult(ok=False) → the SAME failed-retry
    # card path (never swallowed). Routed through handle_confirm to prove 可见.
    def fake_urlopen(req, timeout=None):
        raise _uerr.HTTPError(req.full_url, 500, "Internal Server Error", {}, None)

    monkeypatch.setattr(confirm, "_urlopen", fake_urlopen)
    writer = confirm.HttpKepPreClaimWriter("http://127.0.0.1:8971/api/claims")

    rid, nonce = _clarifying(store)
    res = confirm.handle_confirm(
        registry_id=rid, nonce=nonce, operator_open_ids={"ou_alice"},
        form_value=_form_value(), store=store, writer_lookup=lambda n: writer,
    )
    assert res.kind == "failed"
    assert res.written is False
    assert any(e.get("tag") == "button" for e in res.card["body"]["elements"])  # 重试按钮
    assert "500" in store.get(rid)["last_error"]


def test_http_writer_deduped_response_is_ok(monkeypatch):
    # deduped: true (idempotent replay hit on the backend) is still ok — the
    # write succeeded, the backend just held the first record.
    def fake_urlopen(req, timeout=None):
        return _FakeResp(_json.dumps(
            {"ok": True, "deduped": True, "record": {"seq": 7, "write_idempotency_key": "pcr_9"}}))

    monkeypatch.setattr(confirm, "_urlopen", fake_urlopen)
    writer = confirm.HttpKepPreClaimWriter("http://127.0.0.1:8971/api/claims")
    res = writer.write(
        scene=SCENE, values={"amount": 1, "reason": "x"},
        registry_id="pcr_9", write_idempotency_key="pcr_9", profile_name="p",
    )
    assert res.ok is True
    assert res.backend_id == "7"


# ===================== P5: credential expiry mapping (401/403) ============

def test_http_writer_401_maps_to_credential_expired_reauth(monkeypatch, store):
    # A dead kep token → 401. The writer must flag credential_expired so
    # handle_confirm shows the RE-AUTH card (/auth guidance), not a generic retry
    # loop on a dead credential (finding http-writer-401-not-mapped).
    def fake_urlopen(req, timeout=None):
        raise _uerr.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(confirm, "_urlopen", fake_urlopen)
    writer = confirm.HttpKepPreClaimWriter("http://127.0.0.1:8971/api/claims")
    rid, nonce = _clarifying(store)
    res = confirm.handle_confirm(
        registry_id=rid, nonce=nonce, operator_open_ids={"ou_alice"},
        form_value=_form_value(), store=store, writer_lookup=lambda n: writer)
    assert res.kind == "reauth"
    actions = [e.get("value", {}).get("hermes_action") for e in res.card["body"]["elements"]
               if e.get("tag") == "button"]
    assert "cred_auth" in actions  # /auth guidance on the card
    assert store.get(rid)["status"] == reg.STATUS_CONFIRMED  # writable → retry after auth


def test_http_writer_403_direct_write_flags_credential_expired(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _uerr.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(confirm, "_urlopen", fake_urlopen)
    writer = confirm.HttpKepPreClaimWriter("http://127.0.0.1:8971/api/claims")
    res = writer.write(scene=SCENE, values={"amount": 1, "reason": "x"},
                       registry_id="pcr_1", write_idempotency_key="pcr_1", profile_name="p")
    assert res.ok is False and res.credential_expired is True


# ===================== P5: no-redirect SSRF guard ========================

def test_no_redirect_handler_blocks_ssrf():
    # An allow-listed host that 302s to 169.254.169.254 must NOT be followed —
    # redirect_request returns None so urllib aborts the redirect and the 3xx
    # surfaces as an error instead of fetching the internal metadata address.
    handler = confirm._NoRedirectHandler()
    assert handler.redirect_request(
        SimpleNamespace(full_url="https://acme.example/api"), None, 302,
        "Found", {}, "http://169.254.169.254/latest/meta-data/") is None


def test_http_writer_blocked_redirect_is_visible_failure(monkeypatch):
    # A 3xx (redirect blocked) is a clean ok=False — NOT credential_expired and
    # NOT a silent success.
    def fake_urlopen(req, timeout=None):
        raise _uerr.HTTPError(req.full_url, 302, "Found", {}, None)

    monkeypatch.setattr(confirm, "_urlopen", fake_urlopen)
    writer = confirm.HttpKepPreClaimWriter("https://acme.example/api")
    res = writer.write(scene=SCENE, values={"amount": 1, "reason": "x"},
                       registry_id="pcr_r", write_idempotency_key="pcr_r", profile_name="p")
    assert res.ok is False and res.credential_expired is False
    assert "302" in (res.error or "")


def test_callback_from_payload_requires_https():
    # employee data must not ride plaintext http (except loopback for dev/fake-kep).
    with pytest.raises(ValueError):
        scenes.callback_from_payload({"url": "http://evil.test/save"})
    assert scenes.callback_from_payload({"url": "http://127.0.0.1:9/api"}).url == "http://127.0.0.1:9/api"
    assert scenes.callback_from_payload({"url": "https://acme.example/api"}).url == "https://acme.example/api"


# ===== router card.action.trigger path (form submit drops button value) =====
#
# The live bug: under the multitenancy router the confirm click reached
# _on_card_action_trigger, but a Feishu FORM submit delivers the submit button's
# value stripped (action.value empty) — only action.form_value (inputs) and
# action.name (push_confirm_submit_<op>) survive. The old wrapper keyed detection
# purely on action.value.hermes_action, so it delegated to the core handler,
# which synthesized a "/card ..." message → the "命令调度器" reply and zero write.
# These drive the real wrapper (_patch_card_action) to prove the form submit is
# claimed, routed to handle_confirm, written exactly once, and never falls
# through to the core "/card" path — while non-push_confirm actions still delegate.


def _make_form_submit(*, open_id="ou_alice", op="op1", form=None, value=None,
                      message_id="om_confirm_new", name=None):
    action = SimpleNamespace(
        tag="button",
        name=name if name is not None else f"push_confirm_submit_{op}",
        value=value,  # None → Feishu stripped the button value on a form submit
        form_value=form if form is not None else _form_value(),
    )
    event = SimpleNamespace(
        action=action,
        operator=SimpleNamespace(open_id=open_id, union_id=None, user_id=None),
        context=SimpleNamespace(open_message_id=message_id),
    )
    return SimpleNamespace(event=event)


def _install_confirm_wrapper():
    """Patch a fresh throwaway adapter class with the real confirm card-action
    wrapper and return (class, calls) where calls['original'] counts delegations
    to the core handler (the "/card" path)."""
    calls = {"original": 0}

    class FakeFeishuAdapter:
        def _on_card_action_trigger(self, data):  # stands in for the core chain
            calls["original"] += 1
            return {"kind": "delegated"}

    assert confirm._patch_card_action(FakeFeishuAdapter) is True
    return FakeFeishuAdapter, calls


def test_router_form_submit_confirm_writes_once_and_no_card_command(store, monkeypatch):
    rid, _nonce = _clarifying(store, open_id="ou_alice")
    monkeypatch.setattr(reg, "get_registry_store", lambda: store)
    writer = confirm.MockKepPreClaimWriter()
    confirm.override_writer(SCENE.writer, writer)
    try:
        Adapter, calls = _install_confirm_wrapper()
        adapter = Adapter()

        resp = adapter._on_card_action_trigger(
            _make_form_submit(open_id="ou_alice", form=_form_value(amount=58)))

        # claimed here (handle_confirm), never delegated to the core "/card" path
        assert calls["original"] == 0
        assert resp is not None
        # exactly one backend record, row committed, recovered value written
        assert writer.write_calls == 1
        assert store.get(rid)["status"] == reg.STATUS_COMMITTED
        assert rid in writer.records
        assert writer.records[rid]["amount"] == 58

        # second click is idempotent — no double write, still no "/card" delegate
        adapter._on_card_action_trigger(
            _make_form_submit(open_id="ou_alice", form=_form_value(amount=58)))
        assert writer.write_calls == 1
        assert calls["original"] == 0
    finally:
        confirm.override_writer(SCENE.writer, None)


def test_router_plain_button_uses_value_routing(store, monkeypatch):
    # A plain (non-form) button — retry / reauth — carries its routing value
    # intact; the wrapper routes on it directly, no open-row recovery needed.
    rid, nonce = _clarifying(store, open_id="ou_alice")
    monkeypatch.setattr(reg, "get_registry_store", lambda: store)
    writer = confirm.MockKepPreClaimWriter()
    confirm.override_writer(SCENE.writer, writer)
    try:
        Adapter, calls = _install_confirm_wrapper()
        adapter = Adapter()
        action = SimpleNamespace(
            tag="button", name="push_confirm_retry", form_value=_form_value(amount=58),
            value={"hermes_action": "push_confirm", "registry_id": rid,
                   "nonce": nonce, "operation_id": "op9"},
        )
        event = SimpleNamespace(
            action=action,
            operator=SimpleNamespace(open_id="ou_alice", union_id=None, user_id=None),
            context=SimpleNamespace(open_message_id="om_card_1"),
        )
        adapter._on_card_action_trigger(SimpleNamespace(event=event))
        assert calls["original"] == 0
        assert writer.write_calls == 1
        assert store.get(rid)["status"] == reg.STATUS_COMMITTED
    finally:
        confirm.override_writer(SCENE.writer, None)


def test_router_non_push_confirm_still_delegates(store, monkeypatch):
    # Regression guard: a sibling card action (cred_auth) must pass through
    # unchanged — the confirm wrapper must never吞 the other four hooks.
    monkeypatch.setattr(reg, "get_registry_store", lambda: store)
    Adapter, calls = _install_confirm_wrapper()
    adapter = Adapter()
    action = SimpleNamespace(
        tag="button", name="cred_auth_btn", form_value=None,
        value={"hermes_action": "cred_auth", "cred": "kep-cli-pre"},
    )
    event = SimpleNamespace(
        action=action,
        operator=SimpleNamespace(open_id="ou_bob", union_id=None, user_id=None),
        context=SimpleNamespace(open_message_id="om_x"),
    )
    resp = adapter._on_card_action_trigger(SimpleNamespace(event=event))
    assert calls["original"] == 1
    assert resp == {"kind": "delegated"}


# ===== LIVE gateway: card button routed as a synthetic /card COMMAND event =====
#
# The REAL live root cause (main-agent live gateway evidence): the multitenancy
# Feishu adapter routes a card BUTTON click as a synthetic "/card ..." COMMAND
# event (_handle_card_action_event → handle_message → pre_gateway_dispatch →
# router handle_async). It NEVER re-enters _on_card_action_trigger, so the 5th-
# hook wrapper (tested above) can't fire on this gateway — the confirm fell to
# parse_command → the "命令但无调度器" reply (zero write, re-clickable button).
# The original card-action `data` survives on the synthetic event's raw_message;
# these prove try_route_push_confirm_synthetic detects it there and drives
# handle_confirm before parse_command, exactly once, and consumes the event.

import asyncio as _asyncio  # noqa: E402


class _FakeAdapter:
    """Captures both delivery channels so a test can assert the confirm result
    goes out as a CARD (via _feishu_send_with_retry) and NOT as a plain command
    reply (via .send — what the "/card 命令但无调度器" fallback would use)."""

    def __init__(self):
        self.card_sends = []  # (chat_id, msg_type, payload) — confirm result cards
        self.sends = []       # (chat_id, text) — plain command replies (must be empty)

    async def _feishu_send_with_retry(self, *, chat_id, msg_type, payload,
                                      reply_to=None, metadata=None):
        self.card_sends.append((chat_id, msg_type, payload))
        return SimpleNamespace(data=SimpleNamespace(message_id="om_confirm_result"))

    async def send(self, chat_id, text, *a, **k):
        self.sends.append((chat_id, text))
        return SimpleNamespace(data=SimpleNamespace(message_id="om_sent"))


def _make_synthetic_command(*, open_id="ou_alice", op="op1", form=None, value=None,
                            text="/card button", name=None, chat_type="dm"):
    """Mirror the synthetic COMMAND event the core adapter builds for a card click:
    text="/card <tag> [value]", message_type COMMAND, and raw_message = the raw
    card-action `data` (event.action/operator/context)."""
    action = SimpleNamespace(
        tag="button",
        name=name if name is not None else f"push_confirm_submit_{op}",
        value=value,  # None → Feishu stripped the button value on a form submit
        form_value=form if form is not None else _form_value(),
    )
    card_data = SimpleNamespace(
        event=SimpleNamespace(
            action=action,
            operator=SimpleNamespace(open_id=open_id, union_id=None, user_id=None),
            context=SimpleNamespace(open_message_id="om_card_1", open_chat_id="oc_dm"),
        )
    )
    return SimpleNamespace(
        text=text,
        message_type=SimpleNamespace(name="COMMAND"),
        message_id="om_synth",
        source=SimpleNamespace(
            chat_id="oc_dm", user_id=open_id, user_id_alt=None,
            chat_type=chat_type, platform=SimpleNamespace(value="feishu"),
            message_id="om_synth",
        ),
        media_urls=None, media_types=None, raw_event=None,
        raw_message=card_data,
    )


def test_synthetic_command_confirm_writes_once_and_consumes(store, monkeypatch):
    rid, _nonce = _clarifying(store, open_id="ou_alice")
    monkeypatch.setattr(reg, "get_registry_store", lambda: store)
    writer = confirm.MockKepPreClaimWriter()
    confirm.override_writer(SCENE.writer, writer)
    try:
        fake = _FakeAdapter()
        monkeypatch.setattr("hermes_multitenancy.router._get_feishu_adapter", lambda gw: fake)

        ev = _make_synthetic_command(open_id="ou_alice", form=_form_value(amount=58))
        consumed = _asyncio.run(
            confirm.try_route_push_confirm_synthetic(SimpleNamespace(), ev))

        # CONSUMED (caller returns → never reaches parse_command dispatch)
        assert consumed is True
        # form submit dropped the button value → registry_id/nonce recovered from
        # the clicker's open clarifying row, then written exactly once + committed
        assert writer.write_calls == 1
        assert store.get(rid)["status"] == reg.STATUS_COMMITTED
        assert writer.records[rid]["amount"] == 58
        # confirm result delivered as a CARD, not a plain "/card" command reply
        assert len(fake.card_sends) == 1
        assert fake.card_sends[0][1] == "interactive"
        assert fake.sends == []

        # second click is idempotent — no double write, still consumed
        consumed2 = _asyncio.run(confirm.try_route_push_confirm_synthetic(
            SimpleNamespace(),
            _make_synthetic_command(open_id="ou_alice", form=_form_value(amount=58))))
        assert consumed2 is True
        assert writer.write_calls == 1
    finally:
        confirm.override_writer(SCENE.writer, None)


def test_synthetic_non_confirm_passes_through_without_adapter(store, monkeypatch):
    # A normal text message and a sibling card action (cred_auth) must BOTH
    # passthrough (False) — and resolve NO adapter, so normal-chat dispatch is
    # untouched (the confirm probe adds zero adapter cost on the common path).
    monkeypatch.setattr(reg, "get_registry_store", lambda: store)
    adapter_calls = {"n": 0}

    def _adapter(gw):
        adapter_calls["n"] += 1
        return _FakeAdapter()

    monkeypatch.setattr("hermes_multitenancy.router._get_feishu_adapter", _adapter)

    plain = SimpleNamespace(text="hello bot", raw_message=None)
    assert _asyncio.run(
        confirm.try_route_push_confirm_synthetic(SimpleNamespace(), plain)) is False

    sibling = _make_synthetic_command(
        value={"hermes_action": "cred_auth", "cred": "kep-cli-pre"},
        form=None, name="cred_auth_btn")
    assert _asyncio.run(
        confirm.try_route_push_confirm_synthetic(SimpleNamespace(), sibling)) is False

    assert adapter_calls["n"] == 0


def test_handle_async_routes_synthetic_confirm_before_parse_command(store, monkeypatch):
    # Full router integration: a synthetic "/card button" confirm must reach
    # handle_confirm and short-circuit BEFORE parse_command's command dispatch —
    # otherwise "/card" falls to the "命令但无调度器" reply (zero write).
    from hermes_multitenancy import router as router_mod

    rid, _nonce = _clarifying(store, open_id="ou_alice")
    monkeypatch.setattr(reg, "get_registry_store", lambda: store)
    writer = confirm.MockKepPreClaimWriter()
    confirm.override_writer(SCENE.writer, writer)
    try:
        fake = _FakeAdapter()
        monkeypatch.setattr(router_mod, "_get_feishu_adapter", lambda gw: fake)

        def _boom_cmd(*a, **k):
            raise AssertionError("push-confirm must not reach command dispatch (/card)")

        monkeypatch.setattr(router_mod, "_handle_command", _boom_cmd, raising=False)
        monkeypatch.setattr(router_mod, "_dispatch_gateway_command", _boom_cmd, raising=False)

        ev = _make_synthetic_command(open_id="ou_alice", form=_form_value(amount=58))
        _asyncio.run(router_mod.handle_async(event=ev, gateway=SimpleNamespace()))

        # handle_confirm ran (exactly one write + committed), the /card command
        # path never ran (no plain reply), and the result card was delivered.
        assert writer.write_calls == 1
        assert store.get(rid)["status"] == reg.STATUS_COMMITTED
        assert fake.sends == []
        assert len(fake.card_sends) == 1
    finally:
        confirm.override_writer(SCENE.writer, None)


# ===== callback endpoint: 3-level priority (push > scene > env) + fail-loud =====

import dataclasses as _dc  # noqa: E402

_PUSH_CB = scenes.callback_to_json(scenes.CallbackConfig(url="http://127.0.0.1:9/push/api"))


def _clarifying_row(store, *, callback_json=None, behaviors_json=None,
                    open_id="ou_alice", biz="x"):
    res = store.create(
        scene="dev-acceptance-claim", skill="push-fill-form",
        target_open_id=open_id, profile_name="alice-profile",
        business_key=f"dev-acceptance-claim:{open_id}:{biz}",
        callback_json=callback_json, behaviors_json=behaviors_json,
    )
    rid = res.row["registry_id"]
    store.mark_sent(rid, message_id=f"om_{biz}")
    store.advance_status(rid, expect=reg.STATUS_PENDING, to=reg.STATUS_CLARIFYING)
    return rid, store.get(rid)["nonce"]


def test_resolve_callback_push_override_wins(monkeypatch):
    monkeypatch.setenv("HERMES_PUSH_CARD_WRITER_URL", "http://env/api")
    scene_cb = _dc.replace(SCENE, callback=scenes.CallbackConfig(url="http://scene/api"))
    row = {"callback_json": _PUSH_CB}
    cb = confirm.resolve_callback(scene_cb, row)
    assert cb.url == "http://127.0.0.1:9/push/api"  # push override beats scene + env


def test_resolve_callback_scene_beats_env(monkeypatch):
    monkeypatch.setenv("HERMES_PUSH_CARD_WRITER_URL", "http://env/api")
    scene_cb = _dc.replace(SCENE, callback=scenes.CallbackConfig(url="http://scene/api"))
    assert confirm.resolve_callback(scene_cb, {"callback_json": None}).url == "http://scene/api"


def test_resolve_callback_env_dev_fallback(monkeypatch):
    monkeypatch.setenv("HERMES_PUSH_CARD_WRITER_URL", "http://env/api")
    # SCENE.callback is None, no push override → dev env fallback.
    assert confirm.resolve_callback(SCENE, {"callback_json": None}).url == "http://env/api"


def test_resolve_callback_none_when_all_empty(monkeypatch):
    monkeypatch.delenv("HERMES_PUSH_CARD_WRITER_URL", raising=False)
    assert confirm.resolve_callback(SCENE, {"callback_json": None}) is None


def test_confirm_fail_loud_when_no_callback_configured(store, monkeypatch):
    # default writer_lookup=get_writer → HTTP shell; scene.callback=None, env unset,
    # no push override → fail-loud改卡, ZERO write, row untouched (bind precedes CAS).
    monkeypatch.delenv("HERMES_PUSH_CARD_WRITER_URL", raising=False)
    rid, nonce = _clarifying(store)
    res = confirm.handle_confirm(
        registry_id=rid, nonce=nonce, operator_open_ids={"ou_alice"},
        form_value=_form_value(), store=store)
    assert res.kind == "failed" and res.written is False
    assert "未配置回调地址" in _json.dumps(res.card, ensure_ascii=False)
    assert store.get(rid)["status"] == reg.STATUS_CLARIFYING


def test_push_callback_override_writes_to_that_endpoint(store, monkeypatch):
    monkeypatch.delenv("HERMES_PUSH_CARD_WRITER_URL", raising=False)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = _json.loads(req.data.decode("utf-8"))
        return _FakeResp(_json.dumps({"ok": True, "record": {"seq": 5}}))

    monkeypatch.setattr(confirm, "_urlopen", fake_urlopen)
    rid, nonce = _clarifying_row(store, callback_json=_PUSH_CB, biz="cb")
    res = confirm.handle_confirm(
        registry_id=rid, nonce=nonce, operator_open_ids={"ou_alice"},
        form_value=_form_value(), store=store)  # default writer_lookup → HTTP shell → bound
    assert res.kind == "committed"
    assert captured["url"] == "http://127.0.0.1:9/push/api"  # wrote to the PUSH endpoint
    assert captured["body"]["write_idempotency_key"] == rid
    assert store.get(rid)["status"] == reg.STATUS_COMMITTED


def test_scene_callback_used_when_no_push_override(store, monkeypatch):
    monkeypatch.delenv("HERMES_PUSH_CARD_WRITER_URL", raising=False)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeResp(_json.dumps({"ok": True, "record": {"seq": 1}}))

    monkeypatch.setattr(confirm, "_urlopen", fake_urlopen)
    scene_cb = _dc.replace(SCENE, callback=scenes.CallbackConfig(url="http://127.0.0.1:9/scene/api"))
    rid, nonce = _clarifying(store)
    res = confirm.handle_confirm(
        registry_id=rid, nonce=nonce, operator_open_ids={"ou_alice"},
        form_value=_form_value(), store=store, scene_lookup=lambda row: scene_cb)
    assert res.kind == "committed"
    assert captured["url"] == "http://127.0.0.1:9/scene/api"


def test_http_writer_auth_header_from_env_never_plaintext(monkeypatch):
    monkeypatch.setenv("MY_KEP_TOKEN", "secret-abc")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp(_json.dumps({"ok": True, "record": {"seq": 1}}))

    monkeypatch.setattr(confirm, "_urlopen", fake_urlopen)
    writer = confirm.HttpKepPreClaimWriter(
        "http://x/api", auth_header="Authorization", auth_token_env="MY_KEP_TOKEN")
    writer.write(scene=SCENE, values={"amount": 1, "reason": "x"},
                 registry_id="pcr_1", write_idempotency_key="pcr_1", profile_name="p")
    assert captured["auth"] == "secret-abc"  # resolved from env at request time


# ===== submit behaviors: submit_once / max_submits / allow_resubmit_before_commit =====

def test_submit_once_default_committed_reclick_noops(store):
    rid, nonce = _clarifying(store)
    writer = confirm.MockKepPreClaimWriter()
    first = _confirm(store, rid, nonce, writer)
    assert first.kind == "committed"
    # default submit_once=True: terminal card has no button, re-click no-ops.
    assert not any(e.get("tag") == "button" for e in first.card["body"]["elements"])
    res = _confirm(store, rid, nonce, writer)
    assert res.kind == "noop"
    assert writer.write_calls == 1


def test_submit_once_false_allows_resubmit_amend(store):
    b = scenes.SubmitBehaviors(submit_once=False)
    rid, nonce = _clarifying_row(store, behaviors_json=scenes.behaviors_to_json(b), biz="beh")
    writer = confirm.MockKepPreClaimWriter()
    res = _confirm(store, rid, nonce, writer)
    assert res.kind == "committed"
    assert writer.write_calls == 1
    # nonce kept (not cleared) → the committed card keeps a 重新提交 button.
    assert store.get(rid)["nonce"] == nonce
    btns = [e for e in res.card["body"]["elements"] if e.get("tag") == "button"]
    assert btns and btns[0]["value"]["hermes_action"] == "push_confirm"
    # a re-click re-drives the idempotent write (改单): committed again, one record.
    res2 = _confirm(store, rid, nonce, writer)
    assert res2.kind == "committed"
    assert writer.write_calls == 2
    assert len(writer.records) == 1


def test_max_submits_caps_resubmits(store):
    b = scenes.SubmitBehaviors(submit_once=False, max_submits=2)
    rid, nonce = _clarifying_row(store, behaviors_json=scenes.behaviors_to_json(b), biz="cap")
    writer = confirm.MockKepPreClaimWriter()
    assert _confirm(store, rid, nonce, writer).kind == "committed"  # write_attempts=1
    assert _confirm(store, rid, nonce, writer).kind == "committed"  # write_attempts=2
    res = _confirm(store, rid, nonce, writer)                       # 2>=2 → capped
    assert res.kind == "reject"
    assert "最大提交次数" in _json.dumps(res.toast, ensure_ascii=False)
    assert writer.write_calls == 2  # third refused before writing


def test_allow_resubmit_before_commit_false_locks_confirmed_edit(store):
    b = scenes.SubmitBehaviors(allow_resubmit_before_commit=False)
    rid, nonce = _clarifying_row(store, behaviors_json=scenes.behaviors_to_json(b), biz="lock")
    # move to confirmed (write in-flight) WITHOUT committing
    store.advance_status(rid, expect=reg.STATUS_CLARIFYING, to=reg.STATUS_CONFIRMED,
                         expect_nonce=nonce, submission=_form_value())
    writer = confirm.MockKepPreClaimWriter()
    res = _confirm(store, rid, nonce, writer, form=_form_value(amount=99))
    assert res.kind == "reject"
    assert "锁定" in _json.dumps(res.toast, ensure_ascii=False)
    assert writer.write_calls == 0


# ===== old/stale card UX =====

def test_stale_card_click_clear_message_no_write(store):
    # a click whose registry row is gone → clear "已失效或已录入" text, no write/500.
    writer = confirm.MockKepPreClaimWriter()
    res = confirm.handle_confirm(
        registry_id="pcr_gone", nonce="whatever", operator_open_ids={"ou_alice"},
        form_value=_form_value(), store=store, writer_lookup=lambda n: writer)
    assert res.kind == "invalid"
    assert "已失效或已录入" in _json.dumps(res.toast, ensure_ascii=False)
    assert writer.write_calls == 0
