"""US-016 — _edit_with_retry behaves correctly on 429 / non-429 / final give-up."""
from __future__ import annotations

import asyncio
import pytest


def test_is_rate_limit_error():
    from hermes_multitenancy.router import _is_rate_limit_error
    assert _is_rate_limit_error(Exception("HTTP 429 too many requests"))
    assert _is_rate_limit_error(Exception("RateLimit exceeded"))
    assert _is_rate_limit_error(Exception("ratelimit"))
    assert _is_rate_limit_error(Exception("Too Many Requests"))
    assert not _is_rate_limit_error(Exception("connection refused"))
    assert not _is_rate_limit_error(Exception("404 not found"))


@pytest.mark.asyncio
async def test_retry_succeeds_after_429(monkeypatch):
    """Returns successfully after a transient 429 + backoff retry."""
    from hermes_multitenancy import router as router_mod

    sleeps: list[float] = []
    async def fake_sleep(s): sleeps.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    calls = []
    class A:
        async def edit_message(self, c, mid, content, *, finalize=False):
            calls.append(content)
            if len(calls) < 3:
                raise Exception("HTTP 429 Too Many Requests")
            return "ok"

    res = await router_mod._edit_with_retry(A(), "c", "m", "hello")
    assert res == "ok"
    assert len(calls) == 3
    # First 2 attempts hit 429 → backoff 0.5 + 1.0
    assert sleeps == [0.5, 1.0]


@pytest.mark.asyncio
async def test_retry_gives_up_after_4_consecutive_429(monkeypatch, caplog):
    """4 consecutive 429s → returns None and logs a warning."""
    import logging
    from hermes_multitenancy import router as router_mod

    sleeps: list[float] = []
    async def fake_sleep(s): sleeps.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class A:
        async def edit_message(self, c, mid, content, *, finalize=False):
            raise Exception("HTTP 429 too many requests")

    with caplog.at_level(logging.WARNING, logger="hermes_multitenancy.router"):
        result = await router_mod._edit_with_retry(A(), "c", "m", "x")
    assert result is None
    assert sleeps == [0.5, 1.0, 2.0]  # 3 backoffs, 4th attempt fails outright
    assert any("rate-limited 4x" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_non_429_retried_once_then_raised(monkeypatch):
    """Non-429 exception: 1 retry after 0.2s, then propagate."""
    from hermes_multitenancy import router as router_mod

    sleeps: list[float] = []
    async def fake_sleep(s): sleeps.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    calls = []
    class A:
        async def edit_message(self, c, mid, content, *, finalize=False):
            calls.append(content)
            raise Exception("connection refused")

    with pytest.raises(Exception, match="connection refused"):
        await router_mod._edit_with_retry(A(), "c", "m", "x")
    assert len(calls) == 2
    assert sleeps == [0.2]


@pytest.mark.asyncio
async def test_finalize_kwarg_falls_back_when_unsupported(monkeypatch):
    """Adapters without finalize= param still get the call (without it)."""
    from hermes_multitenancy import router as router_mod

    monkeypatch.setattr(asyncio, "sleep", lambda s: asyncio.sleep(0))

    seen_finalize = []
    class A:
        async def edit_message(self, c, mid, content):
            seen_finalize.append("no-kwarg")
            return "ok"

    res = await router_mod._edit_with_retry(A(), "c", "m", "x", finalize=True)
    assert res == "ok"
    assert seen_finalize == ["no-kwarg"]
