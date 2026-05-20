import json
import sys
import types
from pathlib import Path

from hermes_multitenancy import browser_policy


def test_browser_policy_disabled_by_default(tmp_path: Path):
    decision = browser_policy.browser_decision({}, tmp_path / "profiles" / "alice")

    assert decision.enabled is False
    assert decision.reason == "profile browser capability is disabled"
    assert browser_policy.browser_toolsets_for_policy(["web", "browser"], decision) == ["web"]


def test_browser_policy_router_is_always_denied(tmp_path: Path):
    decision = browser_policy.browser_decision(
        {"multitenancy": {"browser": {"enabled": True}}},
        tmp_path / "profiles" / "multitenancy_router",
    )

    assert decision.enabled is False
    assert "router profile" in decision.reason


def test_browser_policy_enabled_adds_native_browser_toolset(tmp_path: Path):
    profile = tmp_path / "profiles" / "alice"
    decision = browser_policy.browser_decision(
        {"multitenancy": {"browser": {"enabled": True}}},
        profile,
    )

    assert decision.enabled is True
    assert browser_policy.browser_toolsets_for_policy(["web"], decision) == ["browser", "web"]
    assert browser_policy.browser_toolsets_for_policy(None, decision) == ["browser"]


def test_browser_env_is_profile_scoped(tmp_path: Path):
    profile = tmp_path / "profiles" / "alice"
    decision = browser_policy.browser_decision(
        {"multitenancy": {"browser": {"enabled": True}}},
        profile,
    )

    env = browser_policy.browser_env(decision)

    assert env["HERMES_MULTITENANCY_BROWSER_ENABLED"] == "1"
    assert env["HERMES_BROWSER_SOCKET_BASE_DIR"] == str(profile / "browser" / "run")
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(profile / "browser" / "ms-playwright")
    assert (profile / "browser" / "run").is_dir()
    assert (profile / "browser" / "downloads").is_dir()


def test_browser_env_uses_profile_local_chrome_for_testing(tmp_path: Path):
    profile = tmp_path / "profiles" / "alice"
    chrome = (
        profile
        / "browser"
        / "chrome-for-testing"
        / "chrome-linux64"
        / "chrome"
    )
    chrome.parent.mkdir(parents=True)
    chrome.write_text("#!/bin/sh\n", encoding="utf-8")
    chrome.chmod(0o755)
    decision = browser_policy.browser_decision(
        {"multitenancy": {"browser": {"enabled": True}}},
        profile,
    )

    env = browser_policy.browser_env(decision)

    assert env["AGENT_BROWSER_EXECUTABLE_PATH"] == str(chrome)
    assert "/Applications/Google Chrome.app" not in env["AGENT_BROWSER_EXECUTABLE_PATH"]


def test_url_guard_blocks_private_and_metadata_urls(tmp_path: Path):
    decision = browser_policy.browser_decision(
        {"multitenancy": {"browser": {"enabled": True}}},
        tmp_path / "profiles" / "alice",
    )

    for url in (
        "http://127.0.0.1:8876/health",
        "http://192.168.1.1/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
    ):
        assert browser_policy.decide_url(url, decision).allowed is False, url

    assert browser_policy.decide_url("https://example.com/", decision).allowed is True


def test_url_guard_can_allow_private_but_never_metadata(tmp_path: Path):
    decision = browser_policy.browser_decision(
        {"multitenancy": {"browser": {"enabled": True, "allow_private_urls": True}}},
        tmp_path / "profiles" / "alice",
    )

    assert browser_policy.decide_url("http://127.0.0.1:8876/health", decision).allowed is True
    assert browser_policy.decide_url("http://169.254.169.254/latest/meta-data/", decision).allowed is False


def test_url_guard_blocks_hostname_that_resolves_private(monkeypatch, tmp_path: Path):
    decision = browser_policy.browser_decision(
        {"multitenancy": {"browser": {"enabled": True}}},
        tmp_path / "profiles" / "alice",
    )

    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "internal.example"
        return [
            (
                browser_policy.socket.AF_INET,
                browser_policy.socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1", 0),
            )
        ]

    monkeypatch.setattr(browser_policy.socket, "getaddrinfo", fake_getaddrinfo)

    result = browser_policy.decide_url("https://internal.example/path", decision)

    assert result.allowed is False
    assert "private/internal" in result.reason


def test_url_guard_allows_private_dns_when_enabled_but_never_metadata(
    monkeypatch,
    tmp_path: Path,
):
    decision = browser_policy.browser_decision(
        {"multitenancy": {"browser": {"enabled": True, "allow_private_urls": True}}},
        tmp_path / "profiles" / "alice",
    )

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "internal.example":
            return [
                (
                    browser_policy.socket.AF_INET,
                    browser_policy.socket.SOCK_STREAM,
                    6,
                    "",
                    ("10.0.0.5", 0),
                )
            ]
        if host == "metadata.example":
            return [
                (
                    browser_policy.socket.AF_INET,
                    browser_policy.socket.SOCK_STREAM,
                    6,
                    "",
                    ("169.254.169.254", 0),
                )
            ]
        raise AssertionError(host)

    monkeypatch.setattr(browser_policy.socket, "getaddrinfo", fake_getaddrinfo)

    assert browser_policy.decide_url("https://internal.example/", decision).allowed is True
    metadata = browser_policy.decide_url("https://metadata.example/", decision)
    assert metadata.allowed is False
    assert "metadata" in metadata.reason


def test_install_browser_guard_wraps_upstream_navigate(monkeypatch, tmp_path: Path):
    profile = tmp_path / "profiles" / "alice"
    calls = []

    fake_browser_tool = types.SimpleNamespace()

    def fake_navigate(url, task_id=None):
        calls.append((url, task_id))
        return json.dumps({"success": True, "data": {"url": url, "title": "OK"}})

    fake_browser_tool.browser_navigate = fake_navigate
    fake_browser_tool._socket_safe_tmpdir = lambda: "/tmp"
    tools_mod = sys.modules.get("tools") or types.ModuleType("tools")
    tools_mod.browser_tool = fake_browser_tool
    monkeypatch.setitem(sys.modules, "tools", tools_mod)
    monkeypatch.setitem(sys.modules, "tools.browser_tool", fake_browser_tool)

    installed = browser_policy.install_browser_guard(
        {"multitenancy": {"browser": {"enabled": True}}},
        profile,
    )

    assert installed is True
    ok = json.loads(fake_browser_tool.browser_navigate("https://example.com", task_id="t1"))
    denied = json.loads(fake_browser_tool.browser_navigate("http://127.0.0.1:8876", task_id="t1"))

    assert ok["success"] is True
    assert calls == [("https://example.com", "t1")]
    assert denied["success"] is False
    assert denied["source"] == "multitenancy_browser_policy"
    assert fake_browser_tool._socket_safe_tmpdir() == str(profile / "browser" / "run")
