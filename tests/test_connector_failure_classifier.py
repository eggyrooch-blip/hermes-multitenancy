"""Terminal-vs-transient classification, incl. the exit-code / false-valid traps."""
from __future__ import annotations

import time

import pytest

from hermes_multitenancy.connector_failure_classifier import (
    NEEDS_REAUTH,
    TRANSIENT,
    classify_connector_failure,
)


def cls(cid, **kw):
    return classify_connector_failure(cid, **kw)["class"]


def taxonomy(**kw):
    result = classify_connector_failure("lark-cli", **kw)
    return {
        key: result[key]
        for key in ("failure_subsystem", "error_code", "retryable")
    }


def test_lark_cli_structured_failure_taxonomy():
    cases = [
        (
            {"business_payload": {"code": 99991668}},
            ("credential", "FEISHU_AUTH_REAUTH_REQUIRED", False),
        ),
        (
            {"refresh_class": "invalid"},
            ("credential", "FEISHU_AUTH_REAUTH_REQUIRED", False),
        ),
        (
            {"failure_hint": "identity_unbound"},
            ("identity", "FEISHU_IDENTITY_UNBOUND", False),
        ),
        (
            {"failure_hint": "identity_mismatch"},
            ("identity", "FEISHU_IDENTITY_MISMATCH", False),
        ),
        (
            {"business_payload": {"code": 99991672}},
            ("permission", "FEISHU_PERMISSION_DENIED", False),
        ),
        (
            {"http_status": 429},
            ("lark_api", "FEISHU_RATE_LIMITED", True),
        ),
        (
            {"timed_out": True},
            ("transport", "FEISHU_DEPENDENCY_TIMEOUT", True),
        ),
        (
            {"http_status": 503},
            ("transport", "FEISHU_DEPENDENCY_UNAVAILABLE", True),
        ),
        (
            {"http_status": 400},
            ("lark_api", "FEISHU_REQUEST_INVALID", False),
        ),
        (
            {"business_payload": {"code": 123456}},
            ("lark_api", "FEISHU_BUSINESS_ERROR", False),
        ),
        (
            {"exit_code": 1, "stderr": "unrecognized failure"},
            ("lark_api", "FEISHU_UNKNOWN", False),
        ),
    ]
    for signals, expected in cases:
        result = taxonomy(**signals)
        assert tuple(result.values()) == expected


def test_lark_cli_taxonomy_prefers_structured_signal_over_exit_and_text():
    assert taxonomy(
        http_status=429,
        exit_code=1,
        stderr="token expired",
        business_payload={"code": 99991672},
    ) == {
        "failure_subsystem": "lark_api",
        "error_code": "FEISHU_RATE_LIMITED",
        "retryable": True,
    }


def test_lark_cli_success_has_empty_failure_fields():
    assert taxonomy(exit_code=0, business_payload={"code": 0}) == {
        "failure_subsystem": None,
        "error_code": None,
        "retryable": False,
    }
    assert taxonomy(
        exit_code=0,
        business_payload={"code": 0, "data": {"code": 123456}},
    ) == {
        "failure_subsystem": None,
        "error_code": None,
        "retryable": False,
    }


@pytest.mark.parametrize(
    "stderr",
    [
        "request timed out once, auto-retried and succeeded",
        "HTTP 429 rate limited, retry succeeded",
        "dependency temporarily unavailable before successful retry",
        "identity was not bound during the first attempt; succeeded after refresh",
    ],
)
def test_lark_cli_clean_exit_beats_historical_failure_text(stderr):
    assert taxonomy(exit_code=0, stderr=stderr) == {
        "failure_subsystem": None,
        "error_code": None,
        "retryable": False,
    }


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
def test_stderr_bare_number_not_false_positive():
    # review [4]: bare "401"/"403"/"forbidden" in benign business text must NOT trip re-auth
    assert cls("feishu-project", exit_code=1, stderr="工作项 401 不存在") == TRANSIENT
    assert cls("feishu-project", exit_code=1, stderr="work item 403 archived") == TRANSIENT
    assert cls("feishu-project", exit_code=1, stderr="This action is forbidden by policy") == TRANSIENT
    assert cls("gitlab", exit_code=1, stderr="record 1403 not found") == TRANSIENT
    # ...but an auth-context status IS still terminal
    assert cls("feishu-project", exit_code=1, stderr="HTTP 401 unauthorized") == NEEDS_REAUTH
    assert cls("feishu-project", exit_code=1, stderr="status: 403") == NEEDS_REAUTH


def test_body_code_word_boundary():
    # review [1]: must read the real "code", not the value of status_code/retcode
    assert cls("kep-cli", stderr='{"status_code":200,"code":10101}') == NEEDS_REAUTH
    assert cls("kep-cli", stderr='{"retcode":200,"code":10101}') == NEEDS_REAUTH
    # a benign status_code alone (no terminal business code) stays transient
    assert cls("kep-cli", stderr='{"status_code":200,"code":40004}') == TRANSIENT


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
