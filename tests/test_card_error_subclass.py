"""Phase 4 (card-error-subclass) — CardKitApiError hierarchy tests.

Verifies that ``_raise_on_lark_error`` raises the most-specific subclass
for known Lark response codes and the base ``CardKitApiError`` for any
other non-zero code. Subclasses must remain ``RuntimeError`` so existing
``except Exception`` / ``except RuntimeError`` blocks keep working.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_multitenancy.card.card_error import (
    CardKitApiError,
    RateLimitError,
    StreamingClosedError,
    TableLimitError,
    UnavailableError,
    _raise_on_lark_error,
)


def _response(code, msg="boom", sub_code=None):
    return SimpleNamespace(code=code, msg=msg, sub_code=sub_code)


def test_zero_code_does_not_raise():
    _raise_on_lark_error(_response(0), "card.create")
    _raise_on_lark_error(_response(None), "card.create")


def test_rate_limit_error_on_230020():
    with pytest.raises(RateLimitError) as info:
        _raise_on_lark_error(_response(230020, "rate"), "card.create")
    exc = info.value
    assert exc.code == 230020
    assert exc.msg == "rate"
    assert exc.api == "card.create"
    assert isinstance(exc, CardKitApiError)
    assert isinstance(exc, RuntimeError)  # backward compat


@pytest.mark.parametrize("code", [200850, 300309])
def test_streaming_closed_error_on_official_codes(code):
    with pytest.raises(StreamingClosedError) as info:
        _raise_on_lark_error(_response(code, "streaming closed"), "cardElement.content")
    assert info.value.code == code
    assert isinstance(info.value, CardKitApiError)


def test_table_limit_error_on_230099_with_sub_code_11310():
    with pytest.raises(TableLimitError) as info:
        _raise_on_lark_error(_response(230099, "table", sub_code=11310), "cardElement.content")
    exc = info.value
    assert exc.code == 230099
    assert exc.sub_code == 11310
    assert isinstance(exc, CardKitApiError)


def test_table_limit_falls_back_to_base_when_sub_code_differs():
    """230099 alone (without sub_code 11310) is NOT the table-limit case —
    must raise the generic CardKitApiError base instead of TableLimitError."""
    with pytest.raises(CardKitApiError) as info:
        _raise_on_lark_error(_response(230099, "other 230099", sub_code=9999), "card.update")
    exc = info.value
    assert not isinstance(exc, TableLimitError)
    assert exc.code == 230099


def test_unavailable_error_on_recalled_99991663():
    with pytest.raises(UnavailableError) as info:
        _raise_on_lark_error(_response(99991663, "recalled"), "card.update")
    exc = info.value
    assert exc.code == 99991663
    assert isinstance(exc, CardKitApiError)


def test_unavailable_error_on_deleted_230006():
    with pytest.raises(UnavailableError) as info:
        _raise_on_lark_error(_response(230006, "deleted"), "card.update")
    exc = info.value
    assert exc.code == 230006


def test_generic_card_kit_api_error_on_unknown_non_zero_code():
    with pytest.raises(CardKitApiError) as info:
        _raise_on_lark_error(_response(500, "internal"), "card.settings")
    exc = info.value
    assert type(exc) is CardKitApiError  # exact type, not subclass
    assert exc.code == 500


def test_card_kit_api_error_str_carries_legacy_format():
    """Existing log filters / dashboards may grep for the legacy string."""
    err = CardKitApiError("api.x", 42, "boom")
    assert "api.x failed: code=42" in str(err)
    assert "msg=boom" in str(err)


def test_card_kit_api_error_with_sub_code_renders_it():
    err = TableLimitError("cardElement.content", 230099, "limit", sub_code=11310)
    assert "sub_code=11310" in str(err)
