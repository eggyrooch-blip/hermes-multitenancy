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


async def _append_async(target, value):
    target.append(value)


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
            return SimpleNamespace(
                user_email="employee@keep.com",
                metadata={
                    "hermes_billing_active": True,
                    "scim_active": not self.blocked,
                },
            )

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
        self.guard._sync_actual_spend = seed
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


def test_client_litellm_call_id_cannot_reuse_a_reservation(
    callback_module, monkeypatch
):
    monkeypatch.setenv("HERMES_LITELLM_EMPLOYEE_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_SHARED_KEY_HASH", "shared-hash")
    monkeypatch.setenv("HERMES_LITELLM_MONTHLY_BUDGET_USD", "2.1")
    harness = _Harness(callback_module, estimate=1.0)
    first = _data()
    second = _data()
    first["litellm_call_id"] = "client-controlled"
    second["litellm_call_id"] = "client-controlled"

    asyncio.run(harness.guard.async_pre_call_hook(_auth(), None, first, "acompletion"))
    asyncio.run(harness.guard.async_pre_call_hook(_auth(), None, second, "acompletion"))

    assert len(harness.pending) == 2
    assert "client-controlled" not in harness.pending


def test_one_employee_at_limit_does_not_block_another_on_shared_key(
    callback_module, monkeypatch
):
    monkeypatch.setenv("HERMES_LITELLM_EMPLOYEE_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_SHARED_KEY_HASH", "shared-hash")
    monkeypatch.setenv("HERMES_LITELLM_MONTHLY_BUDGET_USD", "1")
    monkeypatch.setenv("HERMES_LITELLM_EMPLOYEE_EMAIL_DOMAIN", "keep.com")
    guard = callback_module.HermesEmployeeBudgetGuard()
    pending: dict[str, dict[str, float]] = {}

    async def load_employee(user_id):
        return SimpleNamespace(
            user_email=f"{user_id}@keep.com",
            metadata={"hermes_billing_active": True, "scim_active": True},
        )

    async def seed(*_args):
        return None

    async def reserve(*, keys, args):
        _now, budget, amount, request_id, _expires, _ttl = args
        employee_pending = pending.setdefault(keys[0], {})
        total = sum(employee_pending.values())
        if total + float(amount) > float(budget):
            return [0, "0", str(total)]
        employee_pending[str(request_id)] = float(amount)
        return [1, "0", str(total + float(amount))]

    guard._load_employee = load_employee
    guard._estimate_cost = lambda *_args: 1.0
    guard._redis_cache = lambda: object()
    guard._redis_keys = lambda _redis, user_id, _period: (
        f"{user_id}:actual",
        f"{user_id}:pending",
        f"{user_id}:expires",
    )
    guard._sync_actual_spend = seed
    guard._ensure_scripts = lambda _redis: setattr(guard, "_reserve_script", reserve)

    def shared_request(employee: str, call_id: str):
        data = _data({
            "X-Hermes-User-Id": employee,
            "X-Hermes-Source": "hermes",
        })
        data["litellm_call_id"] = call_id
        return data

    shared_auth = _auth(token="shared-hash", user_id="service-user")
    asyncio.run(
        guard.async_pre_call_hook(
            shared_auth, None, shared_request("employee-a", "a-1"), "acompletion"
        )
    )
    with pytest.raises(callback_module.HTTPException) as employee_a_limited:
        asyncio.run(
            guard.async_pre_call_hook(
                shared_auth, None, shared_request("employee-a", "a-2"), "acompletion"
            )
        )
    employee_b = asyncio.run(
        guard.async_pre_call_hook(
            shared_auth, None, shared_request("employee-b", "b-1"), "acompletion"
        )
    )

    assert employee_a_limited.value.status_code == 429
    assert employee_b["metadata"]["hermes_employee_budget_reservation"][
        "employee_user_id"
    ] == "employee-b"
    assert employee_b["metadata"]["spend_logs_metadata"]["source"] == "hermes"
    assert set(pending) == {"employee-a:actual", "employee-b:actual"}


def test_non_employee_personal_key_is_unchanged(callback_module, monkeypatch):
    monkeypatch.setenv("HERMES_LITELLM_EMPLOYEE_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_SHARED_KEY_HASH", "shared-hash")
    harness = _Harness(callback_module, employee=False)

    result = asyncio.run(
        harness.guard.async_pre_call_hook(_auth(), None, _data(), "acompletion")
    )

    assert result is None
    assert harness.pending == {}


def test_employee_personal_key_source_cannot_spoof_hermes(
    callback_module, monkeypatch
):
    monkeypatch.setenv("HERMES_LITELLM_EMPLOYEE_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_SHARED_KEY_HASH", "shared-hash")
    harness = _Harness(callback_module)
    data = _data()
    data["metadata"]["spend_logs_metadata"] = {"source": "hermes"}

    result = asyncio.run(
        harness.guard.async_pre_call_hook(_auth(), None, data, "acompletion")
    )

    assert result["metadata"]["spend_logs_metadata"]["source"] == "personal_key"


def test_inactive_employee_is_rejected_before_reservation(callback_module, monkeypatch):
    monkeypatch.setenv("HERMES_LITELLM_EMPLOYEE_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_SHARED_KEY_HASH", "shared-hash")
    harness = _Harness(callback_module, blocked=True)

    with pytest.raises(callback_module.HTTPException) as blocked:
        asyncio.run(
            harness.guard.async_pre_call_hook(_auth(), None, _data(), "acompletion")
        )

    assert blocked.value.status_code == 403
    assert harness.pending == {}


def test_shared_key_fails_closed_without_hermes_active_marker(
    callback_module, monkeypatch
):
    monkeypatch.setenv("HERMES_LITELLM_EMPLOYEE_BILLING_ENABLED", "true")
    monkeypatch.setenv("HERMES_LITELLM_SHARED_KEY_HASH", "shared-hash")
    monkeypatch.setenv("HERMES_LITELLM_EMPLOYEE_EMAIL_DOMAIN", "keep.com")
    guard = callback_module.HermesEmployeeBudgetGuard()

    async def load_employee(_user_id):
        return SimpleNamespace(
            user_email="employee@keep.com",
            metadata={"scim_active": True},
        )

    guard._load_employee = load_employee
    with pytest.raises(callback_module.HTTPException) as inactive:
        asyncio.run(
            guard.async_pre_call_hook(
                _auth(token="shared-hash", user_id="service-user"),
                None,
                _data({
                    "X-Hermes-User-Id": "employee-uuid",
                    "X-Hermes-Source": "hermes",
                }),
                "acompletion",
            )
        )

    assert inactive.value.status_code == 403


def test_success_and_failure_both_settle_provider_cost(callback_module):
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
    async def settle(value, cost):
        settled.append((value, cost))

    guard._settle = settle

    asyncio.run(
        guard.async_log_success_event(
            data,
            SimpleNamespace(_hidden_params={"response_cost": 0.4}),
            None,
            None,
        )
    )
    failure_data = {
        **data,
        "response_cost": 0.25,
    }
    asyncio.run(guard.async_log_failure_event(failure_data, None, None, None))

    assert settled == [(reservation, 0.4), (reservation, 0.25)]


def test_failure_without_cost_stays_pending_for_ledger(callback_module):
    guard = callback_module.HermesEmployeeBudgetGuard()
    reservation = {
        "employee_user_id": "employee-uuid",
        "period": "2026-07",
        "request_id": "call-1",
        "reserved_cost": 0.75,
        "period_ttl": 3600,
    }
    data = {"metadata": {"hermes_employee_budget_reservation": reservation}}
    settled = []
    deferred = []

    async def settle(value, cost):
        settled.append((value, cost))

    guard._settle = settle
    guard._defer_unknown_cost = lambda value: _append_async(deferred, value)
    asyncio.run(guard.async_log_failure_event(data, None, None, None))

    assert settled == []
    assert deferred == [reservation]


def test_ledger_reconciliation_raises_redis_actual_after_crash(
    callback_module, monkeypatch
):
    proxy_server = types.ModuleType("litellm.proxy.proxy_server")

    class SpendLogs:
        async def group_by(self, **_kwargs):
            return [{"_sum": {"spend": 3.5}}]

    proxy_server.prisma_client = SimpleNamespace(
        db=SimpleNamespace(litellm_spendlogs=SpendLogs())
    )
    monkeypatch.setitem(sys.modules, "litellm.proxy", types.ModuleType("litellm.proxy"))
    monkeypatch.setitem(sys.modules, "litellm.proxy.proxy_server", proxy_server)
    guard = callback_module.HermesEmployeeBudgetGuard()
    calls = []

    async def sync(*, keys, args):
        calls.append((keys, args))

    guard._ensure_scripts = lambda _redis: setattr(guard, "_sync_actual_script", sync)
    from datetime import datetime, timezone

    asyncio.run(
        guard._sync_actual_spend(
            object(), "actual-key", "employee-uuid",
            datetime(2026, 7, 1, tzinfo=timezone.utc), 3600,
        )
    )

    assert calls == [(["actual-key"], [3.5, 3600])]


def test_missing_response_cost_stays_pending_for_ledger(callback_module, caplog):
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
    deferred = []

    async def settle(value, cost):
        settled.append((value, cost))

    guard._settle = settle
    guard._defer_unknown_cost = lambda value: _append_async(deferred, value)

    asyncio.run(guard.async_log_success_event(data, object(), None, None))

    assert settled == []
    assert deferred == [reservation]
    assert "response cost missing" in caplog.text
