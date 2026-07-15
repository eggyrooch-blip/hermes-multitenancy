from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
from types import SimpleNamespace

import pytest


@pytest.fixture()
def callback_module(monkeypatch):
    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code = status_code
            self.detail = detail
            super().__init__(detail)

    fastapi = types.ModuleType("fastapi")
    fastapi.HTTPException = HTTPException
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")
    custom_logger.CustomLogger = type("CustomLogger", (), {})
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "litellm", types.ModuleType("litellm"))
    monkeypatch.setitem(
        sys.modules, "litellm.integrations", types.ModuleType("litellm.integrations")
    )
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", custom_logger)

    path = (
        Path(__file__).parents[1]
        / "deploy"
        / "litellm"
        / "hermes_employee_budget.py"
    )
    spec = importlib.util.spec_from_file_location("_test_hermes_employee_budget", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _auth(*, token="personal-hash", user_id="employee-uuid", blocked=False):
    return SimpleNamespace(
        token=token,
        user_id=user_id,
        request_route="/v1/chat/completions",
        blocked=blocked,
    )


def _data(headers=None):
    return {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": {"headers": headers or {}},
        "litellm_call_id": "call-1",
    }


def test_shared_key_requires_trusted_identity_and_personal_key_cannot_spoof(
    callback_module, monkeypatch
):
    guard = callback_module.HermesEmployeeBudgetGuard()
    monkeypatch.setenv("HERMES_LITELLM_SHARED_KEY_HASH", "shared-hash")

    employee, shared = asyncio.run(
        guard._employee_for_request(
            _data({"X-Hermes-User-Id": "employee-uuid", "X-Hermes-Source": "hermes"}),
            _auth(token="shared-hash", user_id="service-user"),
        )
    )
    assert (employee, shared) == ("employee-uuid", True)

    with pytest.raises(callback_module.HTTPException) as missing_source:
        asyncio.run(
            guard._employee_for_request(
                _data({"X-Hermes-User-Id": "employee-uuid"}),
                _auth(token="shared-hash", user_id="service-user"),
            )
        )
    assert missing_source.value.status_code == 403

    with pytest.raises(callback_module.HTTPException) as spoofed:
        asyncio.run(
            guard._employee_for_request(
                _data({"X-Hermes-User-Id": "victim"}),
                _auth(),
            )
        )
    assert spoofed.value.status_code == 403


def test_calendar_month_is_utc(callback_module):
    from datetime import datetime, timezone

    period, start, ttl = callback_module._month(
        datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc)
    )

    assert period == "2026-07"
    assert start == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert ttl >= 86460


class _Harness:
    def __init__(self, module, *, employee=True, blocked=False, estimate=1.0):
        self.guard = module.HermesEmployeeBudgetGuard()
        self.employee = employee
        self.blocked = blocked
        self.estimate = estimate
        self.actual = 0.0
        self.pending = {}
        self.lock = asyncio.Lock()
        self.settled = []
        self.released = []

        async def load_employee(_user_id):
            if not self.employee:
                return None
            return SimpleNamespace(user_email="employee@keep.com", blocked=self.blocked)

        async def seed(*_args):
            return None

        async def reserve(*, keys, args):
            async with self.lock:
                _now, budget, amount, request_id, _expires, _ttl = args
                pending = sum(self.pending.values())
                if self.actual + pending + float(amount) > float(budget):
                    return [0, str(self.actual), str(pending)]
                self.pending[str(request_id)] = float(amount)
                return [1, str(self.actual), str(pending + float(amount))]

        self.guard._load_employee = load_employee
        self.guard._estimate_cost = lambda *_args: self.estimate
        self.guard._redis_cache = lambda: object()
        self.guard._redis_keys = lambda *_args: ("actual", "pending", "expires")
        self.guard._seed_actual_spend = seed
        self.guard._ensure_scripts = lambda _redis: setattr(
            self.guard, "_reserve_script", reserve
        )


def test_only_one_concurrent_request_can_reserve_last_budget(
    callback_module, monkeypatch
):
    monkeypatch.setenv("HERMES_LITELLM_EMPLOYEE_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_SHARED_KEY_HASH", "shared-hash")
    monkeypatch.setenv("HERMES_LITELLM_MONTHLY_BUDGET_USD", "1.5")
    harness = _Harness(callback_module, estimate=1.0)

    async def run_both():
        first = _data()
        second = _data()
        first["litellm_call_id"] = "first"
        second["litellm_call_id"] = "second"
        return await asyncio.gather(
            harness.guard.async_pre_call_hook(_auth(), None, first, "acompletion"),
            harness.guard.async_pre_call_hook(_auth(), None, second, "acompletion"),
            return_exceptions=True,
        )

    results = asyncio.run(run_both())

    assert sum(isinstance(result, dict) for result in results) == 1
    rejected = next(result for result in results if isinstance(result, Exception))
    assert rejected.status_code == 429
    assert sum(harness.pending.values()) == 1.0


def test_non_employee_personal_key_is_unchanged(callback_module, monkeypatch):
    monkeypatch.setenv("HERMES_LITELLM_EMPLOYEE_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_SHARED_KEY_HASH", "shared-hash")
    harness = _Harness(callback_module, employee=False)

    result = asyncio.run(
        harness.guard.async_pre_call_hook(_auth(), None, _data(), "acompletion")
    )

    assert result is None
    assert harness.pending == {}


def test_blocked_employee_is_rejected_before_reservation(callback_module, monkeypatch):
    monkeypatch.setenv("HERMES_LITELLM_EMPLOYEE_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_SHARED_KEY_HASH", "shared-hash")
    harness = _Harness(callback_module, blocked=True)

    with pytest.raises(callback_module.HTTPException) as blocked:
        asyncio.run(
            harness.guard.async_pre_call_hook(_auth(), None, _data(), "acompletion")
        )

    assert blocked.value.status_code == 403
    assert harness.pending == {}


def test_success_settles_actual_cost_and_failure_releases(callback_module):
    guard = callback_module.HermesEmployeeBudgetGuard()
    reservation = {
        "employee_user_id": "employee-uuid",
        "period": "2026-07",
        "request_id": "call-1",
        "reserved_cost": 1.0,
        "period_ttl": 3600,
    }
    data = {
        "litellm_params": {
            "metadata": {
                "hermes_employee_budget_reservation": reservation,
            }
        }
    }
    settled = []
    released = []

    async def settle(value, cost):
        settled.append((value, cost))

    async def release(value):
        released.append(value)

    guard._settle = settle
    guard._release = release

    asyncio.run(
        guard.async_log_success_event(
            data,
            SimpleNamespace(_hidden_params={"response_cost": 0.4}),
            None,
            None,
        )
    )
    asyncio.run(guard.async_log_failure_event(data, None, None, None))

    assert settled == [(reservation, 0.4)]
    assert released == [data]


def test_missing_response_cost_charges_reserved_amount(callback_module, caplog):
    guard = callback_module.HermesEmployeeBudgetGuard()
    reservation = {
        "employee_user_id": "employee-uuid",
        "period": "2026-07",
        "request_id": "call-1",
        "reserved_cost": 0.75,
        "period_ttl": 3600,
    }
    data = {
        "metadata": {
            "hermes_employee_budget_reservation": reservation,
        }
    }
    settled = []

    async def settle(value, cost):
        settled.append((value, cost))

    guard._settle = settle

    asyncio.run(guard.async_log_success_event(data, object(), None, None))

    assert settled == [(reservation, 0.75)]
    assert "response cost missing" in caplog.text
