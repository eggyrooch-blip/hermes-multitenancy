"""HIGH-1 regression (audit 2026-07-03): the kep-auth browser-suppression shim
dir must be created securely — mkdtemp (random name), mode 0700, owned by this
process — so no other local user can pre-plant a malicious `open` that kep-auth
would execute as the hermes service account (CWE-377 local RCE on the box that
holds every profile's credentials).

FAILS on pre-fix code (fixed `$TMPDIR/hermes-credhub-nobrowser`, mkdir
exist_ok=True, shim written only `if not shim.exists()`).
"""
from __future__ import annotations

import os
import stat

import pytest

from hermes_multitenancy import credential_hub_auth as cha


def _reset_cache():
    cha._nobrowser_dir = None


def test_nobrowser_dir_is_private_unpredictable_and_clean():
    _reset_cache()
    d = cha._ensure_no_browser_dir()
    try:
        # not the old predictable fixed path; mkdtemp random suffix present
        assert d.name != "hermes-credhub-nobrowser"
        assert d.name.startswith("hermes-credhub-nobrowser-")
        # 0700, owned by us — an attacker can neither predict nor write here
        st = d.stat()
        assert stat.S_IMODE(st.st_mode) == 0o700
        assert st.st_uid == os.getuid()
        # shims are our executable no-ops
        for name in ("open", "xdg-open", "www-browser", "x-www-browser"):
            shim = d / name
            assert shim.is_file()
            assert shim.read_text().strip().endswith("exit 0")
            assert stat.S_IMODE(shim.stat().st_mode) & 0o111  # executable
    finally:
        _reset_cache()


def test_fresh_build_yields_new_secure_dir_with_clean_shims():
    """A rebuild (cache cleared) produces a DISTINCT mkdtemp dir with clean
    shims — never reuses a predictable location whose contents could be tampered."""
    _reset_cache()
    d1 = cha._ensure_no_browser_dir()
    _reset_cache()
    d2 = cha._ensure_no_browser_dir()
    try:
        assert d1 != d2
        assert (d2 / "open").read_text().strip().endswith("exit 0")
    finally:
        _reset_cache()


def test_login_env_points_browser_into_secure_dir(tmp_path):
    _reset_cache()
    env = cha._kep_login_env(tmp_path, "p")
    try:
        nobrowser = cha._nobrowser_dir
        assert nobrowser is not None
        assert env["BROWSER"] == str(nobrowser / "open")
        assert env["PATH"].split(os.pathsep)[0] == str(nobrowser)  # prepended
    finally:
        _reset_cache()
