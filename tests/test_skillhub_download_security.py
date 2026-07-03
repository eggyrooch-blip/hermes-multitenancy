"""HIGH-3 regression (audit 2026-07-03): skillhub package downloads must enforce
https + an optional host allowlist (no http:// MITM, no file:// local read, no
SSRF to internal hosts), and support opt-in mandatory-checksum enforcement so
unverified bytes can be refused. The fetched bytes are extracted into every
profile's agent instruction layer, so this is RCE-equivalent supply-chain
surface.

The URL-guard tests and the require-checksum test FAIL on pre-fix code
(`_assert_download_url_allowed` absent; `_verify_checksum` returns on a missing
checksum regardless of env).
"""
from __future__ import annotations

import hashlib

import pytest

from hermes_multitenancy import skillhub_installer as shi
from hermes_multitenancy.skillhub_installer import SkillhubInstallError


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://evil.example/pkg.zip",   # MITM-able
        "file:///etc/passwd",            # local file read
        "ftp://host/pkg.zip",            # non-http scheme
        "https:///pkg.zip",              # no host
    ],
)
def test_download_url_guard_rejects_non_https_and_hostless(bad_url):
    with pytest.raises(SkillhubInstallError) as exc:
        shi._assert_download_url_allowed(bad_url)
    assert exc.value.error_code == "PACKAGE_INVALID"


@pytest.mark.parametrize(
    "internal_url",
    [
        "https://10.0.0.5/pkg.zip",         # RFC1918
        "https://192.168.1.1/pkg.zip",      # RFC1918
        "https://127.0.0.1/pkg.zip",        # loopback
        "https://169.254.169.254/pkg.zip",  # cloud metadata (link-local)
        "https://[::1]/pkg.zip",            # ipv6 loopback
        "https://0.0.0.0/pkg.zip",          # unspecified
        # obfuscated numeric IPv4 forms the resolver accepts → still private
        "https://2130706433/pkg.zip",       # decimal 127.0.0.1
        "https://0x7f000001/pkg.zip",       # hex 127.0.0.1
        "https://017700000001/pkg.zip",     # octal 127.0.0.1
        "https://127.1/pkg.zip",            # short 127.0.0.1
        "https://10.1/pkg.zip",             # short 10.0.0.1
    ],
)
def test_download_url_guard_rejects_private_and_reserved_ip_hosts(internal_url):
    with pytest.raises(SkillhubInstallError) as exc:
        shi._assert_download_url_allowed(internal_url)
    assert exc.value.error_code == "PACKAGE_INVALID"


def test_download_url_guard_allows_https(monkeypatch):
    monkeypatch.delenv("HERMES_SKILLHUB_ALLOWED_HOSTS", raising=False)
    shi._assert_download_url_allowed("https://cdn.example/pkg.zip")  # no raise (public hostname)


def test_host_allowlist_enforced_when_set(monkeypatch):
    monkeypatch.setenv("HERMES_SKILLHUB_ALLOWED_HOSTS", "good.example, cdn.aidock.example")
    shi._assert_download_url_allowed("https://cdn.aidock.example/pkg.zip")  # allowed
    with pytest.raises(SkillhubInstallError):
        shi._assert_download_url_allowed("https://evil.example/pkg.zip")   # not in list


def test_checksum_default_allows_missing_but_always_verifies_present(monkeypatch):
    monkeypatch.delenv("HERMES_SKILLHUB_REQUIRE_CHECKSUM", raising=False)
    data = b"package-bytes"
    shi._verify_checksum(data, None)  # default OFF → missing allowed (byte-identical)
    shi._verify_checksum(data, hashlib.sha256(data).hexdigest())  # matching → ok
    with pytest.raises(SkillhubInstallError):
        shi._verify_checksum(data, "deadbeef")  # mismatch → always rejected


def test_checksum_required_when_opted_in(monkeypatch):
    monkeypatch.setenv("HERMES_SKILLHUB_REQUIRE_CHECKSUM", "1")
    with pytest.raises(SkillhubInstallError) as exc:
        shi._verify_checksum(b"package-bytes", None)  # missing + required → refused
    assert exc.value.error_code == "PACKAGE_INVALID"
