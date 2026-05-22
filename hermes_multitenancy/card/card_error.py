"""Lark CardKit / IM API error + response helpers.

Future home for openclaw-lark style ``CardKitApiError`` sub-class hierarchy
(``RateLimitError``, ``TableLimitError``…). Currently a single
``_raise_on_lark_error`` plus result builders.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

try:  # pragma: no cover - import shape depends on the installed Hermes agent.
    from gateway.platforms.base import SendResult  # type: ignore
except Exception:  # pragma: no cover
    SendResult = None  # type: ignore


def _raise_on_lark_error(response: Any, api: str) -> None:
    code = getattr(response, "code", None)
    if code is not None and code != 0:
        msg = getattr(response, "msg", "")
        raise RuntimeError(f"{api} failed: code={code}, msg={msg}")


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
