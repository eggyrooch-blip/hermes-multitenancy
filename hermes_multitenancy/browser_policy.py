"""Profile-scoped browser policy for Hermes native browser tools.

This module deliberately does not implement browser automation. Hermes upstream
continues to own ``browser_*`` tools. Multitenancy only decides whether a
profile may use those tools, where browser runtime state lives, and whether a
URL is allowed for a tenant-scoped local browser.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import socket
from typing import Any
from urllib.parse import urlparse


BROWSER_TOOLSET = "browser"
ROUTER_PROFILE = "multitenancy_router"

_TRUTHY = {"1", "true", "yes", "on", "enabled"}
_METADATA_HOSTS = {
    "metadata.google.internal",
}
_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.169.253"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("100.100.100.200"),
}
_BENCHMARK_FAKE_IP_NET = ipaddress.ip_network("192.0.2.0/15")


@dataclass(frozen=True)
class BrowserDecision:
    enabled: bool
    reason: str
    backend: str = "local"
    allow_private_urls: bool = False
    profile_home: Path | None = None

    @property
    def runtime_dir(self) -> Path | None:
        return self.profile_home / "browser" if self.profile_home is not None else None

    @property
    def socket_base_dir(self) -> Path | None:
        root = self.runtime_dir
        return root / "run" if root is not None else None

    @property
    def downloads_dir(self) -> Path | None:
        root = self.runtime_dir
        return root / "downloads" if root is not None else None

    @property
    def screenshots_dir(self) -> Path | None:
        root = self.runtime_dir
        return root / "screenshots" if root is not None else None

    @property
    def tmp_dir(self) -> Path | None:
        root = self.runtime_dir
        return root / "tmp" if root is not None else None

    @property
    def playwright_browsers_path(self) -> Path | None:
        root = self.runtime_dir
        return root / "ms-playwright" if root is not None else None

    @property
    def executable_path(self) -> Path | None:
        root = self.runtime_dir
        return _find_profile_browser_executable(root) if root is not None else None


@dataclass(frozen=True)
class UrlDecision:
    allowed: bool
    reason: str
    url: str


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in _TRUTHY


def _browser_config(config: dict[str, Any]) -> dict[str, Any]:
    mt_cfg = config.get("multitenancy") or {}
    if not isinstance(mt_cfg, dict):
        return {}
    browser_cfg = mt_cfg.get("browser") or {}
    return browser_cfg if isinstance(browser_cfg, dict) else {}


def browser_decision(
    config: dict[str, Any],
    profile_home: Path,
    *,
    profile_name: str | None = None,
) -> BrowserDecision:
    """Return the browser capability decision for a routed profile."""
    profile_home = Path(profile_home).expanduser()
    profile_name = (profile_name or profile_home.name or "").strip()
    if profile_name == ROUTER_PROFILE:
        return BrowserDecision(
            enabled=False,
            reason="router profile does not run browser tasks",
            profile_home=profile_home,
        )

    cfg = _browser_config(config)
    if not _truthy(cfg.get("enabled")):
        return BrowserDecision(
            enabled=False,
            reason="profile browser capability is disabled",
            profile_home=profile_home,
        )

    backend = str(cfg.get("backend") or "local").strip().lower() or "local"
    allow_private_urls = _truthy(cfg.get("allow_private_urls"))
    return BrowserDecision(
        enabled=True,
        reason="profile browser capability enabled",
        backend=backend,
        allow_private_urls=allow_private_urls,
        profile_home=profile_home,
    )


def ensure_browser_runtime_dirs(decision: BrowserDecision) -> None:
    """Create profile-local browser directories for an enabled decision."""
    if not decision.enabled:
        return
    for path in (
        decision.socket_base_dir,
        decision.downloads_dir,
        decision.screenshots_dir,
        decision.tmp_dir,
        decision.playwright_browsers_path,
    ):
        if path is not None:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)


def _find_profile_browser_executable(runtime_dir: Path) -> Path | None:
    """Return a browser binary installed inside the profile browser runtime."""
    candidates = (
        runtime_dir
        / "chrome-for-testing"
        / "chrome-mac-arm64"
        / "Google Chrome for Testing.app"
        / "Contents"
        / "MacOS"
        / "Google Chrome for Testing",
        runtime_dir
        / "chrome-for-testing"
        / "chrome-mac-x64"
        / "Google Chrome for Testing.app"
        / "Contents"
        / "MacOS"
        / "Google Chrome for Testing",
        runtime_dir / "chrome-for-testing" / "chrome-linux64" / "chrome",
        runtime_dir / "chrome-for-testing" / "chrome-win64" / "chrome.exe",
    )
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    for pattern in (
        "chrome-for-testing/**/Google Chrome for Testing",
        "chrome-for-testing/**/chrome",
        "chrome-for-testing/**/chrome.exe",
        "ms-playwright/**/chrome",
        "ms-playwright/**/Chromium",
    ):
        for path in runtime_dir.glob(pattern):
            if path.is_file() and os.access(path, os.X_OK):
                return path
    return None


def browser_env(decision: BrowserDecision) -> dict[str, str]:
    """Return secret-free env vars for Hermes native browser runtime."""
    if not decision.enabled:
        return {}
    ensure_browser_runtime_dirs(decision)
    env: dict[str, str] = {
        "HERMES_MULTITENANCY_BROWSER_ENABLED": "1",
        "HERMES_MULTITENANCY_BROWSER_BACKEND": decision.backend,
    }
    if decision.socket_base_dir is not None:
        env["AGENT_BROWSER_SOCKET_DIR"] = str(decision.socket_base_dir)
        env["HERMES_BROWSER_SOCKET_BASE_DIR"] = str(decision.socket_base_dir)
    if decision.downloads_dir is not None:
        env["HERMES_BROWSER_DOWNLOADS_DIR"] = str(decision.downloads_dir)
    if decision.screenshots_dir is not None:
        env["HERMES_BROWSER_SCREENSHOTS_DIR"] = str(decision.screenshots_dir)
    if decision.tmp_dir is not None:
        env["HERMES_BROWSER_TMP_DIR"] = str(decision.tmp_dir)
    if decision.playwright_browsers_path is not None:
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(decision.playwright_browsers_path))
    if decision.executable_path is not None:
        env["AGENT_BROWSER_EXECUTABLE_PATH"] = str(decision.executable_path)
    env.setdefault("AGENT_BROWSER_IDLE_TIMEOUT_MS", "300000")
    return env


def _host_from_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    return (parsed.hostname or "").strip().lower().rstrip(".")


def _ip_for_host(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _resolved_ips_for_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    ip = _ip_for_host(host)
    if ip is not None:
        return [ip]
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []

    resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for result in results:
        try:
            sockaddr = result[4]
            resolved.append(ipaddress.ip_address(str(sockaddr[0]).strip("[]")))
        except (IndexError, ValueError, TypeError):
            continue
    return resolved


def _is_private_or_internal_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_benchmark_fake_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.version == 4 and ip in _BENCHMARK_FAKE_IP_NET


def _is_metadata_host(host: str) -> bool:
    if host in _METADATA_HOSTS:
        return True
    return any(ip in _METADATA_IPS for ip in _resolved_ips_for_host(host))


def _is_private_or_internal_host(host: str) -> bool:
    if not host:
        return True
    literal_ip = _ip_for_host(host)
    if literal_ip is not None:
        return _is_private_or_internal_ip(literal_ip)
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return True
    if host.endswith(".local"):
        return True
    return any(
        _is_private_or_internal_ip(ip) and not _is_benchmark_fake_ip(ip)
        for ip in _resolved_ips_for_host(host)
    )


def decide_url(url: str, decision: BrowserDecision) -> UrlDecision:
    """Apply multitenancy URL policy before Hermes native browser navigation."""
    raw = str(url or "").strip()
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return UrlDecision(False, "browser navigation only allows http/https URLs", raw)

    host = _host_from_url(raw)
    if _is_metadata_host(host):
        return UrlDecision(False, "browser navigation to cloud metadata endpoints is denied", raw)
    if _is_private_or_internal_host(host) and not decision.allow_private_urls:
        return UrlDecision(False, "browser navigation to private/internal URLs is denied", raw)
    return UrlDecision(True, "allowed", raw)


def browser_toolsets_for_policy(
    toolsets: list[str] | None,
    decision: BrowserDecision,
) -> list[str] | None:
    """Add or remove Hermes' native browser toolset according to policy."""
    if toolsets is None:
        return [BROWSER_TOOLSET] if decision.enabled else None
    items = {str(item).strip() for item in toolsets if str(item).strip()}
    if decision.enabled:
        items.add(BROWSER_TOOLSET)
    else:
        items.discard(BROWSER_TOOLSET)
    return sorted(items)


def install_browser_guard(config: dict[str, Any], profile_home: Path) -> bool:
    """Patch Hermes upstream browser tool with a multitenancy URL/path guard.

    Returns True when the guard was installed. Returns False if upstream browser
    modules are unavailable in the current test/runtime environment.
    """
    decision = browser_decision(config, profile_home)
    try:
        from tools import browser_tool  # type: ignore
    except Exception:
        return False

    original = getattr(browser_tool, "_mt_original_browser_navigate", None)
    if original is None:
        original = getattr(browser_tool, "browser_navigate", None)
        if original is None:
            return False
        setattr(browser_tool, "_mt_original_browser_navigate", original)

    original_socket_tmpdir = getattr(browser_tool, "_mt_original_socket_safe_tmpdir", None)
    if original_socket_tmpdir is None and hasattr(browser_tool, "_socket_safe_tmpdir"):
        setattr(browser_tool, "_mt_original_socket_safe_tmpdir", browser_tool._socket_safe_tmpdir)

    if decision.enabled:
        ensure_browser_runtime_dirs(decision)

        def _socket_safe_tmpdir() -> str:
            if decision.socket_base_dir is None:
                return tempfile_fallback()
            decision.socket_base_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            return str(decision.socket_base_dir)

        def tempfile_fallback() -> str:
            fallback = getattr(browser_tool, "_mt_original_socket_safe_tmpdir", None)
            return fallback() if callable(fallback) else os.environ.get("TMPDIR", "/tmp")

        browser_tool._socket_safe_tmpdir = _socket_safe_tmpdir

    def guarded_browser_navigate(url: str, task_id: str | None = None) -> str:
        if not decision.enabled:
            return json.dumps(
                {
                    "success": False,
                    "error": decision.reason,
                    "source": "multitenancy_browser_policy",
                },
                ensure_ascii=False,
            )
        pre = decide_url(url, decision)
        if not pre.allowed:
            return json.dumps(
                {
                    "success": False,
                    "error": pre.reason,
                    "source": "multitenancy_browser_policy",
                },
                ensure_ascii=False,
            )
        result_text = original(url, task_id=task_id)
        try:
            payload = json.loads(result_text)
            final_url = str((payload.get("data") or {}).get("url") or "")
            if final_url:
                post = decide_url(final_url, decision)
                if not post.allowed:
                    return json.dumps(
                        {
                            "success": False,
                            "error": f"browser redirect denied: {post.reason}",
                            "source": "multitenancy_browser_policy",
                        },
                        ensure_ascii=False,
                    )
        except Exception:
            pass
        return result_text

    browser_tool.browser_navigate = guarded_browser_navigate
    return True
