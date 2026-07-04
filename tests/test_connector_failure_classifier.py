"""Terminal-vs-transient classification, incl. the exit-code / false-valid traps."""
from __future__ import annotations

import time

from hermes_multitenancy.connector_failure_classifier import (
    NEEDS_REAUTH,
    TRANSIENT,
    classify_connector_failure,
)


def cls(cid, **kw):
    return classify_connector_failure(cid, **kw)["class"]


# --- terminal HTTP -----------------------------------------------------------
def test_http_401_403_422_are_needs_reauth():
    for status in (401, 403, 422):
        assert cls("lark-cli", http_status=status) == NEEDS_REAUTH
        assert cls("gitlab", http_status=status) == NEEDS_REAUTH


# --- trap 1: hades 401 but exit_code 0 (must NOT trust exit_code) -------------
def test_hades_401_with_exit_code_zero_is_needs_reauth():
    # http_status wins over the lying exit_code=0
    assert cls("kep-cli-online", http_status=401, exit_code=0) == NEEDS_REAUTH
    # even if only surfaced in stderr body with exit 0
    assert cls("kep-cli-online", exit_code=0, stderr="HTTP 401 unauthorized from hades") == NEEDS_REAUTH


# --- trap 2: kep locally "valid" but backend 401 -----------------------------
def test_kep_false_valid_backend_401():
    # a local "valid" status means nothing; the downstream 401 is authoritative
    assert cls("kep-cli-pre", http_status=401) == NEEDS_REAUTH


# --- per-connector business codes --------------------------------------------
def test_feishu_business_invalid_code_in_body():
    assert cls("lark-cli", stderr='{"code":99991668,"msg":"token invalid"}') == NEEDS_REAUTH
    assert cls("kep-cli", stderr="code: 10101 unauthorized") == NEEDS_REAUTH


def test_transient_business_code_not_terminal():
    # a random non-terminal business code is transient, not re-auth
    assert cls("lark-cli", stderr='{"code":20050,"msg":"server busy"}') == TRANSIENT


# --- stderr terminal markers -------------------------------------------------
def test_meegle_token_expired_stderr_is_needs_reauth():
    assert cls("feishu-project", exit_code=1, stderr="token expired, please re-login") == NEEDS_REAUTH


def test_meegle_network_stderr_is_transient():
    assert cls("feishu-project", exit_code=1, stderr="npx: network error ETIMEDOUT") == TRANSIENT


# --- payload direct ----------------------------------------------------------
def test_payload_refresh_expired_is_needs_reauth():
    past = int(time.time() * 1000) - 1000
    assert cls("lark-cli", payload={"refresh_expires_at": past}) == NEEDS_REAUTH


def test_payload_access_expired_but_refresh_alive_is_transient_refreshable():
    now = int(time.time() * 1000)
    assert cls("lark-cli", payload={"expires_at": now - 1000, "refresh_expires_at": now + 10_000_000}) == TRANSIENT


# --- negatives: no false-positive needs_reauth -------------------------------
def test_negatives_zero_false_positive():
    now = int(time.time() * 1000)
    negatives = [
        cls("lark-cli", http_status=200),
        cls("gitlab", http_status=500),                          # server error = transient
        cls("gitlab", http_status=503),
        cls("kep-cli-online", exit_code=0),                      # clean success
        cls("feishu-project", exit_code=1, stderr="rate limited, try again"),
        cls("keep-record", exit_code=1, stderr="UPSTREAM_ERROR timeout after 60s"),
        cls("lark-cli", payload={"expires_at": now + 10_000_000, "refresh_expires_at": now + 20_000_000}),
    ]
    assert all(c == TRANSIENT for c in negatives), negatives
