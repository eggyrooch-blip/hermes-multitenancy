"""LiteLLM callback for one employee budget shared by personal and Hermes keys.

Mount this file into the LiteLLM proxy and load
``hermes_employee_budget.hermes_employee_budget_guard`` as a callback.  The
callback deliberately uses LiteLLM's own spend logs as the cold-start ledger
and its configured Redis as the cross-pod reservation store.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hmac
import logging
import math
import os
import re
import uuid
from typing import Any, Optional

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger


logger = logging.getLogger(__name__)

_TRUE = frozenset({"1", "true", "yes", "on"})
_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_RESERVATION_METADATA_KEY = "hermes_employee_budget_reservation"

_SYNC_ACTUAL_SCRIPT = """
local ledger_spend = tonumber(ARGV[1])
local period_ttl = tonumber(ARGV[2])
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if ledger_spend > current then
  current = ledger_spend
  redis.call('SET', KEYS[1], tostring(current))
end
for index = 3, #ARGV do
  redis.call('HDEL', KEYS[2], ARGV[index])
  redis.call('ZREM', KEYS[3], ARGV[index])
end
redis.call('EXPIRE', KEYS[1], period_ttl)
redis.call('EXPIRE', KEYS[2], period_ttl)
redis.call('EXPIRE', KEYS[3], period_ttl)
return tostring(current)
"""

_PENDING_IDS_SCRIPT = """
return redis.call('HKEYS', KEYS[1])
"""

_RESERVE_SCRIPT = """
local now = tonumber(ARGV[1])
local budget = tonumber(ARGV[2])
local amount = tonumber(ARGV[3])
local request_id = ARGV[4]
local reservation_expires_at = tonumber(ARGV[5])
local period_ttl = tonumber(ARGV[6])

local expired = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', now)
for _, expired_id in ipairs(expired) do
  redis.call('HDEL', KEYS[2], expired_id)
end
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now)

local actual = tonumber(redis.call('GET', KEYS[1]) or '0')
local pending = 0
for _, value in ipairs(redis.call('HVALS', KEYS[2])) do
  pending = pending + tonumber(value)
end

if redis.call('HEXISTS', KEYS[2], request_id) == 1 then
  return {2, tostring(actual), tostring(pending)}
end
if actual + pending + amount > budget + 0.000000000001 then
  return {0, tostring(actual), tostring(pending)}
end

redis.call('HSET', KEYS[2], request_id, tostring(amount))
redis.call('ZADD', KEYS[3], reservation_expires_at, request_id)
redis.call('EXPIRE', KEYS[1], period_ttl)
redis.call('EXPIRE', KEYS[2], period_ttl)
redis.call('EXPIRE', KEYS[3], period_ttl)
return {1, tostring(actual), tostring(pending + amount)}
"""

_SETTLE_SCRIPT = """
local request_id = ARGV[1]
local actual_cost = tonumber(ARGV[2])
local period_ttl = tonumber(ARGV[3])
local reserved = redis.call('HGET', KEYS[2], request_id)
if not reserved then
  return {0, redis.call('GET', KEYS[1]) or '0'}
end
redis.call('HDEL', KEYS[2], request_id)
redis.call('ZREM', KEYS[3], request_id)
local total = redis.call('INCRBYFLOAT', KEYS[1], actual_cost)
redis.call('EXPIRE', KEYS[1], period_ttl)
return {1, tostring(total)}
"""

_DEFER_SCRIPT = """
local request_id = ARGV[1]
local expires_at = tonumber(ARGV[2])
local period_ttl = tonumber(ARGV[3])
if redis.call('HEXISTS', KEYS[2], request_id) == 0 then
  return 0
end
redis.call('ZADD', KEYS[3], expires_at, request_id)
redis.call('EXPIRE', KEYS[2], period_ttl)
redis.call('EXPIRE', KEYS[3], period_ttl)
return 1
"""


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in _TRUE


def _month(now: Optional[datetime] = None) -> tuple[str, datetime, int]:
    """Return UTC calendar-month key, start, and Redis TTL."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        next_start = start.replace(year=start.year + 1, month=1)
    else:
        next_start = start.replace(month=start.month + 1)
    # Keep the just-finished period briefly for late stream settlement.
    ttl = max(60, int((next_start - current).total_seconds()) + 86400)
    return start.strftime("%Y-%m"), start, ttl


def _metadata(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("metadata", "litellm_metadata"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    data["metadata"] = {}
    return data["metadata"]


def _headers(data: dict[str, Any]) -> dict[str, str]:
    raw = _metadata(data).get("headers")
    if not isinstance(raw, dict):
        return {}
    return {str(key).lower(): str(value) for key, value in raw.items()}


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _response_cost(kwargs: dict[str, Any], response_obj: Any) -> Optional[float]:
    for candidate in (
        kwargs.get("response_cost"),
        _value(_value(response_obj, "_hidden_params", {}), "response_cost"),
        _value(_value(response_obj, "hidden_params", {}), "response_cost"),
    ):
        try:
            if candidate is not None and math.isfinite(float(candidate)):
                return max(0.0, float(candidate))
        except (TypeError, ValueError):
            continue
    return None


class HermesEmployeeBudgetGuard(CustomLogger):
    """Enforce a strict monthly employee budget at the LiteLLM model boundary."""

    def __init__(self) -> None:
        self._scripts_for_redis: Any = None
        self._sync_actual_script: Any = None
        self._pending_ids_script: Any = None
        self._reserve_script: Any = None
        self._settle_script: Any = None
        self._defer_script: Any = None

    @property
    def enabled(self) -> bool:
        return _env_bool("HERMES_LITELLM_EMPLOYEE_BILLING_ENABLED")

    @property
    def budget(self) -> float:
        try:
            value = float(os.environ.get("HERMES_LITELLM_MONTHLY_BUDGET_USD", "120"))
        except ValueError as exc:
            raise RuntimeError("invalid HERMES_LITELLM_MONTHLY_BUDGET_USD") from exc
        if not math.isfinite(value) or value <= 0:
            raise RuntimeError("employee monthly budget must be positive")
        return value

    @property
    def shared_key_hash(self) -> str:
        return os.environ.get("HERMES_LITELLM_SHARED_KEY_HASH", "").strip()

    @property
    def non_employee_key_hashes(self) -> tuple[str, ...]:
        """Explicit service-key escape hatch; employee keys must carry user_id."""
        return tuple(
            value.strip()
            for value in os.environ.get(
                "HERMES_LITELLM_NON_EMPLOYEE_KEY_HASHES", ""
            ).split(",")
            if value.strip()
        )

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict[str, Any],
        call_type: str,
    ) -> Optional[dict[str, Any]]:
        if not self.enabled:
            return None

        metadata = _metadata(data)
        metadata.pop(_RESERVATION_METADATA_KEY, None)
        employee_user_id, shared_request = await self._employee_for_request(
            data, user_api_key_dict
        )
        if employee_user_id is None:
            return None

        user = await self._load_employee(employee_user_id)
        if user is None:
            if not shared_request and self._is_non_employee_key(user_api_key_dict):
                return None
            raise HTTPException(status_code=403, detail="Unknown employee billing user")
        user_metadata = _value(user, "metadata", {})
        if not isinstance(user_metadata, dict):
            user_metadata = {}
        if user_metadata.get("scim_active") is False:
            raise HTTPException(status_code=403, detail="LiteLLM employee account is inactive")
        if shared_request and user_metadata.get("hermes_billing_active") is not True:
            raise HTTPException(status_code=403, detail="Hermes employee billing is inactive")

        spend_metadata = metadata.get("spend_logs_metadata")
        if not isinstance(spend_metadata, dict):
            spend_metadata = {}
        metadata["spend_logs_metadata"] = {
            **spend_metadata,
            # Overwrite client input so the log source is authoritative.
            "source": "hermes" if shared_request else "personal_key",
        }

        reservation_cost = self._estimate_cost(data, user_api_key_dict)
        period, period_start, period_ttl = _month()
        redis_cache = self._redis_cache()
        keys = self._redis_keys(redis_cache, employee_user_id, period)
        await self._sync_actual_spend(
            redis_cache, keys, employee_user_id, period_start, period_ttl
        )
        self._ensure_scripts(redis_cache)

        # Never use litellm_call_id here: request JSON can carry arbitrary
        # extra fields, so a personal-key caller could reuse it concurrently
        # and make multiple calls share one reservation.
        request_id = str(uuid.uuid4())
        reservation_ttl = self._reservation_ttl()
        now = int(datetime.now(timezone.utc).timestamp())
        result = await self._reserve_script(
            keys=list(keys),
            args=[
                now,
                self.budget,
                reservation_cost,
                request_id,
                now + reservation_ttl,
                period_ttl,
            ],
        )
        status = int(result[0])
        if status == 2:
            raise HTTPException(
                status_code=503,
                detail="Employee budget reservation id collision",
            )
        if status == 0:
            actual = float(result[1])
            pending = float(result[2])
            raise HTTPException(
                status_code=429,
                detail=(
                    "HERMES_EMPLOYEE_BUDGET_EXCEEDED: Employee monthly budget reached "
                    f"(spent=${actual:.4f}, pending=${pending:.4f}, "
                    f"limit=${self.budget:.2f})"
                ),
            )
        # SpendLogs uses litellm_call_id as its request_id. Replace any
        # client-provided value with the same server UUID used by the Redis
        # reservation, so an unknown-cost row can release exactly its own
        # pending reservation once the durable ledger appears.
        data["litellm_call_id"] = request_id
        metadata["spend_logs_metadata"]["hermes_reservation_id"] = request_id
        metadata[_RESERVATION_METADATA_KEY] = {
            "employee_user_id": employee_user_id,
            "period": period,
            "request_id": request_id,
            "reserved_cost": reservation_cost,
            "period_ttl": period_ttl,
        }
        return data

    async def async_post_call_failure_hook(
        self,
        request_data: dict[str, Any],
        original_exception: Exception,
        user_api_key_dict: Any,
        traceback_str: Optional[str] = None,
    ) -> None:
        await self._settle_failure(request_data)

    async def async_log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        await self._settle_failure(kwargs)

    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        reservation = self._reservation(kwargs)
        if reservation is None:
            return
        actual_cost = _response_cost(kwargs, response_obj)
        if actual_cost is None:
            # Fail closed while SpendLogs catches up. The reservation stays
            # pending for the billing period and is released only when the
            # request-correlated durable ledger row is observed.
            await self._defer_unknown_cost(reservation)
            logger.warning(
                "LiteLLM response cost missing; awaiting ledger user_id=%s",
                reservation["employee_user_id"],
            )
            return
        await self._settle(reservation, actual_cost)

    async def _employee_for_request(
        self, data: dict[str, Any], user_api_key_dict: Any
    ) -> tuple[Optional[str], bool]:
        headers = _headers(data)
        header_user = headers.get("x-hermes-user-id", "").strip()
        source = headers.get("x-hermes-source", "").strip().lower()
        token_hash = str(_value(user_api_key_dict, "token", "") or "")
        configured_hash = self.shared_key_hash
        if not configured_hash:
            raise HTTPException(status_code=503, detail="Hermes billing key is not configured")
        is_shared = hmac.compare_digest(token_hash, configured_hash)

        if header_user and not is_shared:
            raise HTTPException(status_code=403, detail="Hermes billing header is reserved")
        if is_shared:
            if not header_user or source != "hermes":
                raise HTTPException(status_code=403, detail="Trusted Hermes billing identity required")
            if not _USER_ID_RE.fullmatch(header_user):
                raise HTTPException(status_code=400, detail="Invalid Hermes billing identity")
            return header_user, True

        personal_user = str(_value(user_api_key_dict, "user_id", "") or "").strip()
        if _USER_ID_RE.fullmatch(personal_user):
            return personal_user, False
        if self._is_non_employee_key(user_api_key_dict):
            return None, False
        raise HTTPException(
            status_code=403,
            detail="Personal model key must be assigned to an employee user",
        )

    def _is_non_employee_key(self, user_api_key_dict: Any) -> bool:
        token_hash = str(_value(user_api_key_dict, "token", "") or "")
        return bool(token_hash) and any(
            hmac.compare_digest(token_hash, allowed)
            for allowed in self.non_employee_key_hashes
        )

    async def _load_employee(self, user_id: str) -> Any:
        from litellm.proxy.proxy_server import prisma_client

        if prisma_client is None:
            raise HTTPException(status_code=503, detail="LiteLLM billing database unavailable")
        user = await prisma_client.db.litellm_usertable.find_unique(
            where={"user_id": user_id}
        )
        email = str(_value(user, "user_email", "") or "").strip().lower()
        domain = os.environ.get("HERMES_LITELLM_EMPLOYEE_EMAIL_DOMAIN", "").strip()
        if not domain or "@" in domain or "/" in domain:
            raise HTTPException(
                status_code=503,
                detail="Employee billing email domain is not configured",
            )
        return user if email.endswith("@" + domain.lower()) else None

    def _estimate_cost(self, data: dict[str, Any], user_api_key_dict: Any) -> float:
        from litellm.proxy.proxy_server import llm_router
        from litellm.proxy.spend_tracking.budget_reservation import (
            estimate_request_max_cost,
        )

        route = str(_value(user_api_key_dict, "request_route", "") or "")
        estimate = estimate_request_max_cost(
            request_body=data,
            route=route,
            llm_router=llm_router,
        )
        if estimate is None or not math.isfinite(float(estimate)) or estimate <= 0:
            raise HTTPException(
                status_code=400,
                detail="Model cost cannot be reserved for employee billing",
            )
        return float(estimate)

    def _redis_cache(self) -> Any:
        from litellm.proxy.proxy_server import spend_counter_cache

        redis_cache = spend_counter_cache.redis_cache
        if redis_cache is None:
            raise HTTPException(
                status_code=503,
                detail="Redis is required for atomic employee billing",
            )
        return redis_cache

    def _ensure_scripts(self, redis_cache: Any) -> None:
        if self._scripts_for_redis is redis_cache:
            return
        self._sync_actual_script = redis_cache.async_register_script(_SYNC_ACTUAL_SCRIPT)
        self._pending_ids_script = redis_cache.async_register_script(_PENDING_IDS_SCRIPT)
        self._reserve_script = redis_cache.async_register_script(_RESERVE_SCRIPT)
        self._settle_script = redis_cache.async_register_script(_SETTLE_SCRIPT)
        self._defer_script = redis_cache.async_register_script(_DEFER_SCRIPT)
        self._scripts_for_redis = redis_cache

    def _redis_keys(self, redis_cache: Any, user_id: str, period: str) -> tuple[str, str, str]:
        # Redis Cluster requires every key touched by one Lua script to share
        # a hash slot.  The braces make period + employee the common slot key.
        prefix = f"hermes:employee-budget:{{{period}:{user_id}}}"
        return tuple(
            redis_cache.check_and_fix_namespace(key=f"{prefix}:{suffix}")
            for suffix in ("actual", "reservations", "expires")
        )  # type: ignore[return-value]

    async def _sync_actual_spend(
        self,
        redis_cache: Any,
        redis_keys: tuple[str, str, str],
        user_id: str,
        period_start: datetime,
        period_ttl: int,
    ) -> None:
        from litellm.proxy.proxy_server import prisma_client

        if prisma_client is None:
            raise HTTPException(status_code=503, detail="LiteLLM billing database unavailable")
        rows = await prisma_client.db.litellm_spendlogs.group_by(
            by=["user"],
            where={"user": user_id, "startTime": {"gte": period_start}},
            sum={"spend": True},
        )
        spend = 0.0
        if rows:
            total = _value(rows[0], "_sum", {})
            spend = float(_value(total, "spend", 0.0) or 0.0)
        self._ensure_scripts(redis_cache)
        raw_pending_ids = await self._pending_ids_script(
            keys=[redis_keys[1]],
            args=[],
        )
        pending_ids = [
            value.decode("utf-8") if isinstance(value, bytes) else str(value)
            for value in (raw_pending_ids or [])
            if value
        ]
        reconciled_ids: list[str] = []
        if pending_ids:
            correlated_rows = await prisma_client.db.litellm_spendlogs.find_many(
                where={
                    "request_id": {"in": pending_ids},
                    "user": user_id,
                    "startTime": {"gte": period_start},
                }
            )
            reconciled_ids = [
                str(_value(row, "request_id", "") or "")
                for row in correlated_rows
                if _value(row, "request_id", None)
            ]
        await self._sync_actual_script(
            keys=list(redis_keys),
            args=[max(0.0, spend), period_ttl, *reconciled_ids],
        )

    def _reservation_ttl(self) -> int:
        try:
            value = int(os.environ.get("HERMES_LITELLM_RESERVATION_TTL_SECONDS", "7200"))
        except ValueError as exc:
            raise RuntimeError("invalid HERMES_LITELLM_RESERVATION_TTL_SECONDS") from exc
        return max(60, min(value, 86400))

    def _reservation(self, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        candidates = [data]
        litellm_params = data.get("litellm_params")
        if isinstance(litellm_params, dict):
            candidates.append(litellm_params)
        for candidate in candidates:
            reservation = _metadata(candidate).get(_RESERVATION_METADATA_KEY)
            if isinstance(reservation, dict) and reservation.get("request_id"):
                return reservation
        return None

    async def _settle(self, reservation: dict[str, Any], actual_cost: float) -> None:
        redis_cache = self._redis_cache()
        self._ensure_scripts(redis_cache)
        keys = self._redis_keys(
            redis_cache,
            str(reservation["employee_user_id"]),
            str(reservation["period"]),
        )
        await self._settle_script(
            keys=list(keys),
            args=[
                str(reservation["request_id"]),
                actual_cost,
                int(reservation["period_ttl"]),
            ],
        )

    async def _settle_failure(self, data: dict[str, Any]) -> None:
        reservation = self._reservation(data)
        if reservation is None:
            return
        actual_cost = _response_cost(data, None)
        if actual_cost is None:
            await self._defer_unknown_cost(reservation)
            return
        await self._settle(reservation, actual_cost)

    async def _defer_unknown_cost(self, reservation: dict[str, Any]) -> None:
        """Retain an estimate until its correlated SpendLog or period expiry."""
        redis_cache = self._redis_cache()
        self._ensure_scripts(redis_cache)
        keys = self._redis_keys(
            redis_cache,
            str(reservation["employee_user_id"]),
            str(reservation["period"]),
        )
        now = int(datetime.now(timezone.utc).timestamp())
        await self._defer_script(
            keys=list(keys),
            args=[
                str(reservation["request_id"]),
                now + int(reservation["period_ttl"]),
                int(reservation["period_ttl"]),
            ],
        )


hermes_employee_budget_guard = HermesEmployeeBudgetGuard()
