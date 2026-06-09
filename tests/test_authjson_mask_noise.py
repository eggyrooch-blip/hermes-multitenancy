"""auth.json sandbox mask: silence the misleading 'failed to parse' noise.

The sandbox hides the host credential pool from tool-visible files. It used to
mask ``auth.json`` with ``/dev/null``; hermes_cli.auth._load_auth_store then read
empty bytes, failed json.loads, and logged a scary
``failed to parse ... Corrupt file preserved`` WARNING on every request.

Fix: mask with a VALID-but-EMPTY auth store so the core loader parses it
silently while still exposing zero credentials.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from hermes_multitenancy import agent_real

_EMPTY_AUTH = agent_real._BWRAP_ARGS_FILE.parent / "empty-auth.json"

_SUBS = {
    "PROFILE_HOME": "/probe/shared/profiles/alice",
    "SHARED_HOME": "/probe/shared",
    "USER_HOME": "/probe/user",
    "HERMES_VENV": "/probe/venv",
    "HERMES_AGENT_INSTALL": "/probe/install",
    "HERMES_AGENT_REPO": "/probe/agent-repo",
    "HERMES_MT_REPO": "/probe/mt-repo",
}


def test_empty_auth_json_is_valid_empty_store():
    assert _EMPTY_AUTH.is_file(), f"{_EMPTY_AUTH} must ship with the plugin"
    data = json.loads(_EMPTY_AUTH.read_text())
    assert data == {"version": 1, "providers": {}, "credential_pool": {}}


def test_core_loader_is_silent_on_empty_store_but_warns_on_devnull(caplog, tmp_path):
    """Failing-first contrast: empty-bytes (old /dev/null behaviour) WARNS;
    the shipped valid-empty store loads SILENTLY with zero credentials."""
    from hermes_cli.auth import _load_auth_store

    # (a) old behaviour: a /dev/null-equivalent empty file -> noisy warning
    devnull_like = tmp_path / "auth.json"
    devnull_like.write_text("")
    with caplog.at_level(logging.WARNING):
        store = _load_auth_store(devnull_like)
    assert any("failed to parse" in r.message for r in caplog.records), (
        "empty-bytes mask should reproduce the noisy warning (failing-first)"
    )
    assert store.get("providers") == {}

    # (b) fixed behaviour: shipped valid-empty store -> silent, still empty
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        store = _load_auth_store(_EMPTY_AUTH)
    assert not any("failed to parse" in r.message for r in caplog.records), (
        "valid-empty auth store must load without the parse warning"
    )
    assert store.get("providers") == {}
    assert store.get("credential_pool", {}) == {}


def test_bwrap_masks_authjson_with_empty_store_not_devnull():
    tokens = agent_real._render_bwrap_args(
        agent_real._BWRAP_ARGS_FILE.read_text(), _SUBS
    )
    triples = list(zip(tokens, tokens[1:], tokens[2:]))

    # auth.json is masked with the empty-store file, NOT /dev/null.
    assert (
        "--ro-bind-try",
        "/probe/mt-repo/hermes_multitenancy/sandbox/empty-auth.json",
        "/probe/shared/auth.json",
    ) in triples
    assert (
        "--ro-bind-try",
        "/dev/null",
        "/probe/shared/auth.json",
    ) not in triples

    # .env and auth.lock stay masked with /dev/null (unchanged).
    assert ("--ro-bind-try", "/dev/null", "/probe/shared/.env") in triples
    assert ("--ro-bind-try", "/dev/null", "/probe/shared/auth.lock") in triples
