"""Lark CardKit / IM API error + response helpers.

Phase 4 — typed exception hierarchy mirroring openclaw-lark's
``CardKitApiError`` ancestry. ``_raise_on_lark_error`` now classifies the
response code and raises the most-specific subclass so callers can switch on
type instead of regex-grepping the error string. All subclasses remain
``RuntimeError`` so legacy ``except Exception`` / ``except RuntimeError``
blocks keep catching them — backward compatible.

Class hierarchy::

    RuntimeError
    └── CardKitApiError              (base — any non-zero Lark code)
        ├── RateLimitError           (code 230020 — CardKit rate limit)
        ├── TableLimitError          (code 230099 sub_code 11310 — table limit)
        └── UnavailableError         (code 99991663 recalled, 230006 deleted)
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

try:  # pragma: no cover - import shape depends on the installed Hermes agent.
    from gateway.platforms.base import SendResult  # type: ignore
except Exception:  # pragma: no cover
    SendResult = None  # type: ignore


_RATE_LIMIT_CODE = 230020
_TABLE_LIMIT_CODE = 230099
_TABLE_LIMIT_SUB_CODE = 11310
_RECALLED_CODE = 99991663
_DELETED_CODE = 230006


class CardKitApiError(RuntimeError):
    """Base — any non-zero response code from the Lark CardKit / IM API.

    Carries the structured ``code`` / ``sub_code`` / ``msg`` / ``api`` fields
    callers may want to dispatch on, alongside the legacy formatted string
    in ``str(exc)`` for log compatibility.
    """

    def __init__(self, api: str, code: int, msg: str = "", sub_code: Optional[int] = None) -> None:
        self.api = api
        self.code = int(code)
        self.sub_code = int(sub_code) if sub_code is not None else None
        self.msg = str(msg or "")
        super().__init__(self._render())

    def _render(self) -> str:
        if self.sub_code is not None:
            return f"{self.api} failed: code={self.code}, sub_code={self.sub_code}, msg={self.msg}"
        return f"{self.api} failed: code={self.code}, msg={self.msg}"


class RateLimitError(CardKitApiError):
    """Lark CardKit rate limit (code 230020)."""


class TableLimitError(CardKitApiError):
    """Card hit a table-element shape limit (code 230099, sub_code 11310)."""


class UnavailableError(CardKitApiError):
    """Source message no longer reachable — recalled (99991663) or deleted (230006).

    Phase 3's ``UnavailableGuard.mark_unavailable`` is the appropriate handler
    in the streaming-controller exception path: a single mark causes all
    subsequent ``should_proceed`` checks for that message_id to return False,
    silencing the retry storm Lark used to produce for recalled cards.
    """


def _classify_lark_error(api: str, code: int, msg: str, sub_code: Optional[int]) -> CardKitApiError:
    """Return the most-specific ``CardKitApiError`` subclass for the response.

    Routing order is deliberate — table-limit takes precedence over the
    generic 230099 base, and unavailable takes precedence over arbitrary
    non-zero codes.
    """
    if code in {_RECALLED_CODE, _DELETED_CODE}:
        return UnavailableError(api, code, msg, sub_code)
    if code == _RATE_LIMIT_CODE:
        return RateLimitError(api, code, msg, sub_code)
    if code == _TABLE_LIMIT_CODE and sub_code == _TABLE_LIMIT_SUB_CODE:
        return TableLimitError(api, code, msg, sub_code)
    return CardKitApiError(api, code, msg, sub_code)


def _raise_on_lark_error(response: Any, api: str) -> None:
    code = getattr(response, "code", None)
    if code is None or code == 0:
        return
    msg = getattr(response, "msg", "")
    sub_code = getattr(response, "sub_code", None)
    raise _classify_lark_error(api, int(code), str(msg or ""), sub_code)


def _response_succeeded(adapter: Any, response: Any, default_message: str) -> bool:
    finalizer = getattr(adapter, "_finalize_send_result", None)
    if callable(finalizer):
        try:
            return bool(getattr(finalizer(response, default_message), "success", False))
        except Exception:
            pass
    succeeded = getattr(adapter, "_response_succeeded", None)
    if callable(succeeded):
        return bool(succeeded(response))
    code = getattr(response, "code", None)
    return code is None or code == 0


def _finalize(adapter: Any, response: Any, default_message: str) -> Any:
    finalizer = getattr(adapter, "_finalize_send_result", None)
    if callable(finalizer):
        return finalizer(response, default_message)
    message_id = _extract_response_field(response, "message_id")
    if message_id:
        return _result(True, message_id=message_id, raw_response=response)
    return _result(False, error=default_message, raw_response=response)


def _extract_response_field(response: Any, name: str) -> Optional[str]:
    for source in (
        response,
        getattr(response, "data", None),
        getattr(response, "message", None),
    ):
        if isinstance(source, dict):
            value = source.get(name)
        else:
            value = getattr(source, name, None)
        if value:
            return str(value)
    return None


def _result(
    success: bool,
    *,
    message_id: Optional[str] = None,
    error: Optional[str] = None,
    raw_response: Any = None,
) -> Any:
    if SendResult is not None:
        return SendResult(success=success, message_id=message_id, error=error, raw_response=raw_response)
    return SimpleNamespace(success=success, message_id=message_id, error=error, raw_response=raw_response)
