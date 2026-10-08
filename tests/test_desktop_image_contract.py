"""Contract tests for the Hermes cloud-desktop image recipe (docker/desktop/).

The image itself is built on hermes-pre with podman (see ``docker/desktop/build.sh``); these tests
only parse the Dockerfile / build script text so they run anywhere without a container runtime.
They pin the decisions the mt-desktop-image SPEC made, so a later edit that silently drops one of
them (base image no longer ``-desktop``, cua-driver no longer handed to uid 10000, Chromium sandbox
flag gone) fails here instead of in a container on hermes-pre.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_DIR = REPO_ROOT / "docker" / "desktop"
DOCKERFILE = DESKTOP_DIR / "Dockerfile"
BUILD_SH = DESKTOP_DIR / "build.sh"

RUNTIME_UID = "10000"
RUNTIME_GID = "10000"
REQUIRED_APT_PACKAGES = ("at-spi2-core", "fonts-noto-cjk")
REQUIRED_BROWSER_ARGS = ("--no-sandbox", "--disable-dev-shm-usage")

_ARG_RE = re.compile(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?\s*$")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _dockerfile_text() -> str:
    assert DOCKERFILE.is_file(), f"missing {DOCKERFILE}"
    return DOCKERFILE.read_text(encoding="utf-8")


def _instructions(text: str) -> list[tuple[str, str]]:
    """Dockerfile → [(INSTRUCTION, argument-text)], with line continuations joined and
    comment / blank lines dropped (comments may sit between continued lines)."""
    joined: list[str] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if buf and line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        joined.append(buf.strip())
        buf = ""
    assert not buf, "Dockerfile ends inside a line continuation"
    out: list[tuple[str, str]] = []
    for entry in joined:
        head, _, rest = entry.partition(" ")
        out.append((head.upper(), rest.strip()))
    return out


def _arg_defaults(instrs: list[tuple[str, str]]) -> dict[str, str]:
    """Default values of every ``ARG NAME=value`` (first definition wins, as the build would use it
    when no --build-arg overrides it)."""
    defaults: dict[str, str] = {}
    for instr, rest in instrs:
        if instr != "ARG":
            continue
        m = _ARG_RE.match(f"ARG {rest}")
        assert m, f"unparseable ARG: {rest!r}"
        name, value = m.group(1), m.group(2)
        if value is not None and name not in defaults:
            defaults[name] = value.strip().strip('"').strip("'")
    return defaults


def _expand(value: str, defaults: dict[str, str]) -> str:
    """Substitute ``${ARG}`` references with their ARG defaults. Lower-case names are shell
    variables local to a RUN step (``${uv}``, ``${whl}``) and are left untouched; an upper-case
    name without an ARG default is a Dockerfile bug."""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        if name in defaults:
            return defaults[name]
        assert not name.isupper(), f"${{{name}}} used in Dockerfile without an ARG default"
        return m.group(0)

    return _VAR_RE.sub(repl, value)


@pytest.fixture(scope="module")
def dockerfile() -> list[tuple[str, str]]:
    return _instructions(_dockerfile_text())


@pytest.fixture(scope="module")
def arg_defaults(dockerfile: list[tuple[str, str]]) -> dict[str, str]:
    return _arg_defaults(dockerfile)


def _runs(dockerfile: list[tuple[str, str]]) -> list[str]:
    return [rest for instr, rest in dockerfile if instr == "RUN"]


def test_base_image_is_upstream_desktop_variant_pinned_by_digest(dockerfile, arg_defaults):
    froms = [rest for instr, rest in dockerfile if instr == "FROM"]
    assert len(froms) == 1, f"expected a single-stage Dockerfile, got FROM x{len(froms)}"
    ref = _expand(froms[0].split()[0], arg_defaults)
    name_tag, sep, digest = ref.partition("@")
    assert name_tag.startswith("docker.io/nousresearch/hermes-agent:"), ref
    tag = name_tag.rsplit(":", 1)[1]
    assert tag.endswith("-desktop"), f"base tag must be the upstream -desktop variant, got {tag!r}"
    assert sep == "@" and re.fullmatch(r"sha256:[0-9a-f]{64}", digest), f"base image must be pinned by digest: {ref}"


def test_apt_layer_installs_atspi_and_cjk_fonts(dockerfile):
    apt_runs = [r for r in _runs(dockerfile) if "apt-get" in r and "install" in r]
    assert apt_runs, "no apt-get install step"
    for pkg in REQUIRED_APT_PACKAGES:
        assert any(re.search(rf"(^|\s){re.escape(pkg)}(\s|$)", r) for r in apt_runs), f"{pkg} not installed by any apt-get step"
    assert all("--no-install-recommends" in r for r in apt_runs), "every apt-get install must use --no-install-recommends"
    assert any("rm -rf /var/lib/apt/lists" in r for r in apt_runs), "apt lists must be removed in the same layer"


def test_cua_driver_installed_as_root_via_hermes_and_handed_to_runtime_uid(dockerfile, arg_defaults):
    runs = [_expand(r, arg_defaults) for r in _runs(dockerfile)]
    install_runs = [r for r in runs if "hermes computer-use install" in r]
    assert len(install_runs) == 1, f"expected exactly one cua-driver install step, got {len(install_runs)}"
    step = install_runs[0]
    assert "HERMES_DOCKER_EXEC_AS_ROOT=1" in step, "install must opt out of the exec shim's privilege drop"
    assert re.search(r"CUA_DRIVER_RS_VERSION=\"?\d+\.\d+\.\d+\"?", step), "cua-driver version must be pinned"
    assert "CUA_DRIVER_RS_HOME=/opt/hermes/cua-driver" in step, "package home must live outside /opt/data"
    assert "CUA_DRIVER_RS_INSTALL_DIR=/usr/local/bin" in step, "entry symlink must land on a system PATH dir"
    chown = re.search(rf"chown -R {RUNTIME_UID}:{RUNTIME_GID} (\S+)", step)
    assert chown, f"install step must chown the driver to {RUNTIME_UID}:{RUNTIME_GID}"
    assert chown.group(1).startswith("/opt/hermes/cua-driver"), chown.group(0)
    assert "find /opt/data" not in step, "build-time deletion under the inherited VOLUME would not affect the final image"


def test_fork_core_wheel_is_sha_addressed_verified_and_installed_without_dep_resolution(dockerfile, arg_defaults):
    runs = [_expand(r, arg_defaults) for r in _runs(dockerfile)]
    core_runs = [r for r in runs if "hermes_agent-" in r and "pip install" in r]
    assert len(core_runs) == 1, f"expected exactly one core-wheel install step, got {len(core_runs)}"
    step = core_runs[0]
    version = arg_defaults["CORE_VERSION"]
    sha = arg_defaults["CORE_WHEEL_SHA256"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
    assert re.fullmatch(r"[0-9a-f]{64}", sha), sha
    url = arg_defaults["CORE_WHEEL_URL"]
    url = _expand(url, arg_defaults)
    assert url.startswith("https://gitlab.example.com/api/v4/projects/2765/packages/pypi/files/"), url
    assert f"/{sha}/hermes_agent-{version}-py3-none-any.whl" in url, "wheel URL must be the sha256-addressed file URL (anonymous-readable, no index credentials)"
    assert "sha256sum -c" in step, "downloaded wheel must be checksum-verified"
    assert 'whl="/tmp/hermes_agent-' in step and re.search(r'pip install --python "\$\{py\}" --no-deps "\$\{whl\}"', step), (
        "core wheel must be installed with --no-deps (3.14 venv vs <3.14 metadata; deps reconciled separately)")
    assert "pip install --python \"${py}\" -r /tmp/core-unmet.txt" in step and 'test -z "$(unmet)"' in step, (
        "fork pins the upstream venv does not satisfy must be installed natively and re-checked")
    assert "tencentcloud-sdk-python-vod" in arg_defaults["MT_WORKER_PYTHON_DEPS"], "MT worker import deps live in MT_WORKER_PYTHON_DEPS"
    import_check = "import importlib.metadata as m, hermes_cli, yaml, tencentcloud; assert m.version('hermes-agent')=='0.21.5'"
    assert import_check in step, "install step must check the distribution version and importable modules"
    assert "import hermes_agent" not in step, "hermes-agent is a distribution name, not an importable module"
    assert f'Hermes Agent v{version} ' in _expand("Hermes Agent v${CORE_VERSION} ", arg_defaults) and "hermes --version" in step
    assert not re.search(r"UV_INDEX_\w+_(USERNAME|PASSWORD)|--secret|password", step), "no index credentials may touch the build"


def test_agent_browser_args_cover_podman_sandbox_gap(dockerfile):
    envs = [rest for instr, rest in dockerfile if instr == "ENV"]
    matches = [e for e in envs if e.startswith("AGENT_BROWSER_ARGS=")]
    assert len(matches) == 1, f"expected exactly one ENV AGENT_BROWSER_ARGS, got {matches}"
    value = matches[0].split("=", 1)[1].strip().strip('"')
    args = value.split(",")
    for flag in REQUIRED_BROWSER_ARGS:
        assert flag in args, f"{flag} missing from AGENT_BROWSER_ARGS={value!r}"


def test_cua_driver_override_points_at_baked_symlink(dockerfile):
    envs = [rest for instr, rest in dockerfile if instr == "ENV"]
    matches = [e for e in envs if e.startswith("HERMES_CUA_DRIVER_CMD=")]
    assert len(matches) == 1, f"expected exactly one ENV HERMES_CUA_DRIVER_CMD, got {matches}"
    assert matches[0].split("=", 1)[1].strip() == "/usr/local/bin/cua-driver", (
        "override must name the symlink the install step creates (CUA_DRIVER_RS_INSTALL_DIR=/usr/local/bin)")


def test_agent_browser_preinstalled_from_npm_and_pinned_to_system_chromium(dockerfile, arg_defaults):
    runs = [_expand(r, arg_defaults) for r in _runs(dockerfile)]
    ab_runs = [r for r in runs if "npm install -g" in r and "agent-browser@" in r]
    assert len(ab_runs) == 1, f"expected exactly one agent-browser install step, got {len(ab_runs)}"
    version = arg_defaults["AGENT_BROWSER_VERSION"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), "agent-browser version must be pinned"
    assert f'"agent-browser@{version}"' in ab_runs[0]
    assert "agent-browser --version" in ab_runs[0], "install step must prove the CLI runs"
    assert "agent-browser install" not in ab_runs[0].replace("npm install", ""), "never download Playwright browsers; the system Chromium is used"
    envs = dict(e.split("=", 1) for instr, e in dockerfile if instr == "ENV" and "=" in e)
    assert envs.get("AGENT_BROWSER_EXECUTABLE_PATH") == "/usr/bin/chromium"


def test_image_defaults_to_runtime_uid_and_heartbeat(dockerfile):
    users = [rest for instr, rest in dockerfile if instr == "USER"]
    assert users[-1] == f"{RUNTIME_UID}:{RUNTIME_GID}", f"image must end as the runtime uid/gid, got {users}"
    assert [rest for instr, rest in dockerfile if instr == "ENTRYPOINT"] == ["[]"]
    assert [rest for instr, rest in dockerfile if instr == "CMD"] == ['["sleep", "infinity"]']


def test_runtime_uid_owns_xdg_runtime_dir(dockerfile):
    runtime_dir = "/tmp/hermes-runtime"
    runs = _runs(dockerfile)
    assert any(
        f"mkdir -p {runtime_dir}" in run
        and f"chown {RUNTIME_UID}:{RUNTIME_GID} {runtime_dir}" in run
        and f"chmod 700 {runtime_dir}" in run
        for run in runs
    )
    envs = [rest for instr, rest in dockerfile if instr == "ENV"]
    assert f"XDG_RUNTIME_DIR={runtime_dir}" in envs


def test_workdir_leaves_the_upstream_source_tree(dockerfile):
    workdirs = [rest for instr, rest in dockerfile if instr == "WORKDIR"]
    assert workdirs == ["/opt/data"], (
        "WORKDIR must move off /opt/hermes: with the fork wheel in site-packages, a cwd of /opt/hermes "
        f"puts the upstream source tree first on sys.path (got {workdirs})")
    runs = _runs(dockerfile)
    assert any("cd /tmp" in r and '"${py}" -c' in r for r in runs), (
        "the import proof must run from /tmp so cwd cannot mask site-packages or write to /opt/data")


def test_hermes_build_checks_use_disposable_home(dockerfile):
    hermes_runs = [run for run in _runs(dockerfile) if re.search(r"(^|\s)hermes\s", run)]
    assert hermes_runs, "expected at least one hermes invocation"
    assert all("HERMES_HOME=/tmp/" in run for run in hermes_runs), (
        "every RUN invoking hermes must use a disposable /tmp HERMES_HOME")


def test_base_image_labels_can_see_the_pre_from_args(dockerfile):
    """ARGs declared before FROM are out of scope afterwards; the provenance LABEL needs them re-declared."""
    from_idx = next(i for i, (instr, _) in enumerate(dockerfile) if instr == "FROM")
    redeclared = {rest.split("=", 1)[0].strip() for instr, rest in dockerfile[from_idx + 1:] if instr == "ARG"}
    labels = " ".join(rest for instr, rest in dockerfile if instr == "LABEL")
    for name in ("BASE_IMAGE", "BASE_TAG", "BASE_DIGEST"):
        if f"${{{name}}}" in labels:
            assert name in redeclared, f"LABEL uses ${{{name}}} but it is not re-declared after FROM (value would be empty)"
    assert "org.opencontainers.image.base.digest" in labels


def test_no_secrets_or_volume_declared(dockerfile):
    text = _dockerfile_text()
    assert not re.search(r"(?i)(api[_-]?key|token|secret|password)\s*=", text), "no credential-looking assignments in the Dockerfile"
    assert not any(instr in ("VOLUME", "COPY", "ADD") for instr, _ in dockerfile), "the recipe adds nothing from the build context and declares no extra volumes"


def test_build_script_parses_and_only_takes_registry_token_via_stdin():
    assert BUILD_SH.is_file(), f"missing {BUILD_SH}"
    assert BUILD_SH.stat().st_mode & 0o111, "build.sh must be executable"
    text = BUILD_SH.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash"), "build.sh must declare a bash shebang"
    assert "set -euo pipefail" in text
    assert "--password-stdin" in text, "registry login must read the token from stdin"
    assert 'REGISTRY_AUTH_FILE="$(mktemp)"' in text
    assert 'chmod 600 "${REGISTRY_AUTH_FILE}"' in text
    assert 'trap \'rm -f "${REGISTRY_AUTH_FILE}"\' EXIT' in text
    assert re.search(r'login --authfile "\$\{REGISTRY_AUTH_FILE\}"', text)
    assert re.search(r'push --authfile "\$\{REGISTRY_AUTH_FILE\}"', text)
    assert not re.search(r"--password(?!-stdin)", text), "never pass the token on the command line"
    token_lines = [line.strip() for line in text.splitlines() if "${REGISTRY_TOKEN}" in line]
    assert token_lines, "build.sh must read REGISTRY_TOKEN from the environment"
    for line in token_lines:
        if line.startswith("REGISTRY_TOKEN=") or line.startswith("if [[") or line.startswith("[["):
            continue
        assert re.fullmatch(r"printf '%s' \"\$\{REGISTRY_TOKEN\}\" \| .*login .*--password-stdin.*", line), (
            f"the token may only flow through stdin into the registry login, got: {line!r}")
        assert not re.search(r">(?!&2)", line) and "echo" not in line, f"token must not be echoed or redirected: {line!r}"
    assert '"${CORE_TAG}-${DATE_TAG}"' in text, "image tag rule is <core-tag>-<yyyymmdd>"
    assert "podman build --pull" in text.replace('"${PODMAN}" build --pull', "podman build --pull")
    bash = "bash"
    try:
        result = subprocess.run([bash, "-n", str(BUILD_SH)], capture_output=True, text=True, timeout=30, check=False)
    except FileNotFoundError:
        pytest.skip("bash not available")
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_build_script_checks_volume_contents_and_requires_digest_with_overridden_core_tag():
    text = BUILD_SH.read_text(encoding="utf-8")
    assert "find /opt/data" not in _dockerfile_text()
    assert "run --rm --entrypoint sh" in text and "-c 'ls -A /opt/data'" in text
    for allowed in (".bashrc", ".profile", ".bash_logout"):
        assert allowed in text

    env = os.environ.copy()
    env.pop("BASE_DIGEST", None)
    env["CORE_TAG"] = "v2026.10.7"
    result = subprocess.run(["bash", str(BUILD_SH)], env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode != 0
    assert "CORE_TAG" in result.stderr and "BASE_DIGEST" in result.stderr and "成对" in result.stderr


def test_build_script_build_args_match_dockerfile_args(dockerfile, arg_defaults):
    text = BUILD_SH.read_text(encoding="utf-8")
    passed = set(re.findall(r'--build-arg "([A-Z_]+)=', text))
    declared = {name for name, _ in (_ARG_RE.match(f"ARG {rest}").groups() for instr, rest in dockerfile if instr == "ARG")}
    unknown = passed - declared
    assert not unknown, f"build.sh passes --build-arg for ARGs the Dockerfile never declares: {sorted(unknown)}"
    for required in ("BASE_TAG", "BASE_DIGEST", "CORE_TAG", "IMAGE_TAG"):
        assert required in passed, f"build.sh must pass --build-arg {required}"
    default_digest = arg_defaults["BASE_DIGEST"]
    assert default_digest in text, "build.sh default BASE_DIGEST must match the Dockerfile default (one pin, two places kept equal)"
    assert arg_defaults["BASE_TAG"] == f"{arg_defaults['CORE_TAG']}-desktop", "BASE_TAG default must derive from CORE_TAG default"
    assert f'CORE_TAG="${{CORE_TAG:-{arg_defaults["CORE_TAG"]}}}"' in text, "build.sh CORE_TAG default must match the Dockerfile"


if __name__ == "__main__":  # pragma: no cover - convenience for `python tests/test_desktop_image_contract.py`
    sys.exit(pytest.main([__file__, "-q"]))
