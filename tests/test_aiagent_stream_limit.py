"""MED-2 regression (audit 2026-07-03): the AIAgent subprocess NDJSON stream is
read line-by-line via asyncio readline(), but create_subprocess_exec was called
WITHOUT limit=, so the StreamReader's default 64 KiB line cap makes a single
large event line (big final answer / large tool arg / thinking block) raise
LimitOverrun — failing the whole run and poisoning a warm worker.

FAILS on pre-fix code (`_AIAGENT_STREAM_LIMIT` absent; exec calls lack limit=).
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from hermes_multitenancy import agent_real


def test_stream_limit_is_generous_and_env_overridable(monkeypatch):
    assert agent_real._AIAGENT_STREAM_LIMIT > 64 * 1024  # bigger than asyncio default


def test_resolve_stream_limit_survives_bad_env(monkeypatch):
    # a non-numeric env value must NOT crash (raw int() at import time would)
    monkeypatch.setenv("HERMES_AIAGENT_STREAM_LIMIT_BYTES", "not-a-number")
    assert agent_real._resolve_aiagent_stream_limit() >= 64 * 1024
    monkeypatch.setenv("HERMES_AIAGENT_STREAM_LIMIT_BYTES", "1048576")
    assert agent_real._resolve_aiagent_stream_limit() == 1048576
    monkeypatch.setenv("HERMES_AIAGENT_STREAM_LIMIT_BYTES", "100")  # below 64 KiB floor
    assert agent_real._resolve_aiagent_stream_limit() == 64 * 1024


def test_reader_with_limit_reads_line_over_64kib():
    async def run():
        reader = asyncio.StreamReader(limit=agent_real._AIAGENT_STREAM_LIMIT)
        line = b"x" * (200 * 1024) + b"\n"
        reader.feed_data(line)
        reader.feed_eof()
        return await reader.readline()

    got = asyncio.run(run())
    assert len(got) == 200 * 1024 + 1  # no LimitOverrun


def test_default_reader_overflows_on_same_line():
    """Baseline: the DEFAULT (64 KiB) reader — pre-fix behavior — raises on the
    same oversized line, proving the fix is load-bearing."""
    async def run():
        reader = asyncio.StreamReader()  # asyncio default 64 KiB
        reader.feed_data(b"x" * (200 * 1024) + b"\n")
        reader.feed_eof()
        try:
            await reader.readline()
        except ValueError:  # LimitOverrun is a ValueError subclass
            return "overflow"
        return "ok"

    assert asyncio.run(run()) == "overflow"


def test_every_subprocess_exec_call_passes_the_limit():
    # agent_real is now a package (god-node split); its create_subprocess_exec
    # calls live in submodules, so scan every submodule's source — the same
    # invariant, spread across the package instead of one file.
    import importlib
    import pkgutil

    src = "".join(
        inspect.getsource(importlib.import_module(f"{agent_real.__name__}.{m.name}"))
        for m in pkgutil.iter_modules(agent_real.__path__)
    )
    calls = src.count("asyncio.create_subprocess_exec(")
    limited = src.count("limit=_AIAGENT_STREAM_LIMIT")
    assert calls > 0
    assert limited >= calls, f"{calls} create_subprocess_exec calls but only {limited} limit= kwargs"
