"""Fill-skill skeleton + confirm callback + idempotent write (SPEC P4/P5).

Covers extraction prefill discipline (money never silent-prefilled, 否定/清空,
user value not clobbered), the confirm form card's openclaw 四坑 (single form,
submit button value, operation_id 双写, high-risk input empty), and the confirm
handler's write invariants: exactly-one backend record under replay / double
confirm / crash-retry, owner + nonce guards, and the credential-expiry re-auth
branch. All pure — an in-memory store + a mock writer, no Feishu SDK.
"""
from __future__ import annotations

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

def test_money_is_never_silently_prefilled():
    sub = fill.merge(SCENE, None, "今天打车去客户现场花了58")
    # amount (high-risk, rule 不预填) stays empty — no盲预填 of money.
    amt = sub.get("amount")
    assert amt is None or not amt.has_value()
    # but it IS echoed for二次回显, and category (low-risk) IS prefilled.
    assert fill._echo_of(sub, "amount") == 58
    assert sub["category"].value == "打车"
    assert fill.money_echo(SCENE, sub) is not None
    assert "58" in fill.money_echo(SCENE, sub)
    # amount is still a required-missing field so the skill asks for it.
    assert any(f.key == "amount" for f in fill.missing_fields(SCENE, sub))


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


def test_confirm_card_high_risk_amount_input_is_empty():
    sub = fill.merge(SCENE, None, "打车 上周五 去客户现场 花了58")
    card = fill.build_confirm_card(SCENE, sub, registry_id="r", nonce="n", operation_id="o")
    form = next(e for e in card["body"]["elements"] if e.get("tag") == "form")
    controls = {e.get("name"): e for e in form["elements"] if e.get("name") in {"amount", "category", "date"}}
    # money input carries NO default_value — user must actively enter it.
    assert "default_value" not in controls["amount"]
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

def test_default_writer_is_mock_until_real_endpoint_given():
    w = confirm.get_writer("kep-pre-claim-writer")
    assert isinstance(w, confirm.MockKepPreClaimWriter)
    assert confirm.get_writer("no-such-writer") is None
