#!/usr/bin/env python3
"""Prove CI can obtain the production core line from its private source.

MT's uv.lock resolves ``hermes-agent`` from PyPI (0.14.0), while production runs
the private fork's release line, which is never published to PyPI. This probe
downloads the production core wheel from the MT project's GitLab generic
package registry, checks it against the pinned sha256, installs it into a
throwaway venv (``--no-deps``) and prints the installed version. It does not
touch the test environment: the test jobs keep resolving uv.lock as before.

Auth: ``CI_JOB_TOKEN`` (``JOB-TOKEN`` header) inside a GitLab job; for a local
run set ``HERMES_CORE_PKG_TOKEN`` to a token with read_api / read_package_registry.
Any failure exits non-zero with ``core 来源不可达`` — it never skips silently.
"""
from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

# Production core line: tag prod-core-v0214-2082ff0c17 of the private hermes-agent fork.
CORE_COMMIT = "2082ff0c1717b3ed2dc534a8eb54e00e7e657d1e"
CORE_VERSION = "0.21.4"
WHEEL = f"hermes_agent-{CORE_VERSION}-py3-none-any.whl"
WHEEL_SHA256 = "d76c90ca409d1b7a8eb7ce6c37b39f6c79c9188aeb4a068c9f15fc17cfc96f13"
PACKAGE = "hermes-core"
PROJECT_ID = "2765"  # sunke/hermes-multitenancy
DEFAULT_API = "https://gitlab.example.com/api/v4"


def wheel_url(api: str | None = None) -> str:
    api = (api or os.environ.get("CI_API_V4_URL") or DEFAULT_API).rstrip("/")
    return f"{api}/projects/{PROJECT_ID}/packages/generic/{PACKAGE}/{CORE_COMMIT}/{WHEEL}"


def fail(message: str) -> NoReturn:
    print(f"core-source: FAIL core 来源不可达: {message}", flush=True)
    raise SystemExit(2)


def _get(url: str, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URL
        return response.read()


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """subprocess.run whose timeout / missing binary still ends in the loud fail() line."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        fail(f"{' '.join(cmd[:3])} timed out after {exc.timeout}s")
    except OSError as exc:  # FileNotFoundError when uv is not on PATH
        fail(f"{' '.join(cmd[:3])}: {exc}")


def main() -> int:
    job_token = os.environ.get("CI_JOB_TOKEN", "")
    local_token = os.environ.get("HERMES_CORE_PKG_TOKEN", "")
    if job_token:
        headers = {"JOB-TOKEN": job_token}
    elif local_token:
        headers = {"PRIVATE-TOKEN": local_token}
    else:
        fail("no CI_JOB_TOKEN (GitLab job) or HERMES_CORE_PKG_TOKEN (local run) in the environment")

    url = wheel_url()
    print(f"core-source: GET {url}", flush=True)
    try:
        payload = _get(url, headers)
    except (urllib.error.URLError, OSError) as exc:
        fail(f"{url}: {exc}")
    digest = hashlib.sha256(payload).hexdigest()
    print(f"core-source: downloaded {len(payload)} bytes sha256={digest}", flush=True)
    if digest != WHEEL_SHA256:
        fail(f"sha256 mismatch: got {digest}, pinned {WHEEL_SHA256}")
    print("core-source: sha256 matches the pinned value", flush=True)

    # Informational for the uv.lock switch: uv/pip send credentials as HTTP basic
    # auth (netrc / URL userinfo), not as a JOB-TOKEN header. One byte is enough
    # to tell whether the registry accepts it; never download the wheel twice.
    if job_token:
        basic = base64.b64encode(f"gitlab-ci-token:{job_token}".encode()).decode()
        try:
            _get(url, {"Authorization": f"Basic {basic}", "Range": "bytes=0-0"})
            print("core-source: basic auth gitlab-ci-token -> ok", flush=True)
        except (urllib.error.URLError, OSError) as exc:
            print(f"core-source: basic auth gitlab-ci-token -> {exc}", flush=True)

    with tempfile.TemporaryDirectory(prefix="core-source-") as tmp:
        wheel = Path(tmp) / WHEEL
        wheel.write_bytes(payload)
        venv = Path(tmp) / "venv"
        python = venv / "bin" / "python"
        steps = [
            ["uv", "venv", "--quiet", "--no-cache", "--python", sys.executable, str(venv)],
            ["uv", "pip", "install", "--quiet", "--no-cache", "--no-deps", "--python", str(python), str(wheel)],
        ]
        for step in steps:
            result = _run(step, timeout=120)
            if result.returncode != 0:
                fail(f"{' '.join(step[:3])} exited {result.returncode}: {result.stderr.strip()[-500:]}")
        result = _run(
            [str(python), "-c", "import importlib.metadata as m; print(m.version('hermes-agent'))"],
            timeout=60,
        )
        installed = result.stdout.strip()
        if result.returncode != 0 or installed != CORE_VERSION:
            fail(f"installed version {installed!r} (want {CORE_VERSION}): {result.stderr.strip()[-300:]}")
    print(f"core-source: OK hermes-agent {installed} (core {CORE_COMMIT[:10]}) installed from {PACKAGE}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
