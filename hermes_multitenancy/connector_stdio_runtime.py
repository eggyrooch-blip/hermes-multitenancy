"""Immutable npm admission and Linux launch boundary for catalog stdio MCPs."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse


_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_PACKAGE_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
_DENIED_ENV = {
    "HOME", "PATH", "PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS", "SSLKEYLOGFILE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
}
_PRIVATE_NETWORKS = (
    "localhost", "link-local", "multicast",
    "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7", "fe80::/10",
)
_ENV_ARG = re.compile(r"^@env:([A-Za-z_][A-Za-z0-9_]{0,127})$")
_NODE_ENV_ARG_LOADER = (
    "import{pathToFileURL as u}from'node:url';"
    "let[e,...a]=process.argv.slice(1);"
    "process.argv=[process.execPath,e,...a.map(x=>x.startsWith('@env:')?process.env[x.slice(5)]:x)];"
    "await import(u(e).href)"
)
_PYTHON_ENV_ARG_LOADER = (
    "import os,runpy,sys;"
    "e,*a=sys.argv[1:];"
    "sys.argv=[e]+[os.environ[x[5:]] if x.startswith('@env:') else x for x in a];"
    "runpy.run_path(e,run_name='__main__')"
)
_PYTHON_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _python_module_wrapper(module: str) -> str:
    if not _PYTHON_MODULE.fullmatch(str(module or "")):
        raise ValueError("unsafe Python module entry point")
    return f"import runpy\nrunpy.run_module({module!r}, run_name='__main__')\n"


async def _run_install(command: list[str], cwd: Path, env: dict[str, str]) -> None:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
    if process.returncode != 0:
        raise RuntimeError(f"runtime install failed ({process.returncode}): {stderr.decode(errors='replace')[-500:]}")


def _freeze_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            resolved = path.resolve()
            if root not in resolved.parents:
                raise ValueError("runtime tree contains an escaping symlink")
            continue
        mode = path.stat().st_mode
        path.chmod(0o555 if path.is_dir() else (0o555 if mode & 0o111 else 0o444))
    root.chmod(0o555)


async def prepare_npm_runtime(
    runtime_base: Path | str,
    resolution: dict[str, Any],
    *,
    runner: Callable[[list[str], Path, dict[str, str]], Awaitable[None]] = _run_install,
) -> Path:
    fingerprint = str(resolution.get("resolution_fingerprint") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise ValueError("npm resolution fingerprint is invalid")
    base = Path(runtime_base).resolve()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    base.chmod(0o700)
    target = base / fingerprint
    lock_fd = os.open(base / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX)
    temp: Path | None = None
    try:
        if target.is_dir():
            verify_npm_runtime_tree(target, resolution)
            return target
        temp = Path(tempfile.mkdtemp(prefix=".npm-", dir=base))
        home = temp / ".home"
        home.mkdir(mode=0o700)
        package = str(resolution["package"])
        version = str(resolution["version"])
        (temp / "package.json").write_text(
            json.dumps({"private": True, "dependencies": {package: version}}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        dependency_lock = resolution.get("dependency_lock") or {}
        package_lock = str(dependency_lock.get("package_lock") or "")
        if (
            dependency_lock.get("state") != "resolved"
            or hashlib.sha256(package_lock.encode()).hexdigest()
            != dependency_lock.get("package_lock_sha256")
        ):
            raise ValueError("npm dependency lock integrity mismatch")
        (temp / "package-lock.json").write_text(package_lock, encoding="utf-8")
        command = [
            "/usr/bin/npm", "ci", "--ignore-scripts", "--omit=dev", "--no-audit",
            "--no-fund", "--registry=https://registry.npmjs.org",
        ]
        await runner(command, temp, {
            "HOME": str(home), "LANG": "C.UTF-8", "PATH": "/usr/bin:/bin", "TZ": "UTC",
        })
        installed = verify_npm_runtime_tree(temp, resolution)
        (temp / "installed.json").write_text(
            json.dumps({
                "resolution_fingerprint": fingerprint,
                **installed,
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if target.exists():
            verify_npm_runtime_tree(target, resolution)
            return target
        temp.rename(target)
        temp = None
        _freeze_tree(target)
        return target
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _package_path(root: Path, package: str) -> Path:
    parts = package.split("/")
    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise ValueError("invalid npm package path")
    return root / "node_modules" / Path(*parts)


def _safe_relative(value: str) -> Path:
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("unsafe npm executable path")
    return path


def verify_npm_runtime_tree(runtime_root: Path | str, resolution: dict[str, Any]) -> dict[str, str]:
    root = Path(runtime_root).resolve()
    if resolution.get("state") != "resolved":
        raise ValueError("npm resolution is unavailable")
    package = str(resolution.get("package") or "")
    version = str(resolution.get("version") or "")
    integrity = str(resolution.get("integrity") or "")
    if not version or not integrity.startswith("sha512-"):
        raise ValueError("npm resolution is not immutable")

    lock_path = root / "package-lock.json"
    dependency_lock = resolution.get("dependency_lock") or {}
    if (
        dependency_lock.get("state") != "resolved"
        or hashlib.sha256(lock_path.read_bytes()).hexdigest()
        != dependency_lock.get("package_lock_sha256")
    ):
        raise ValueError("npm dependency lock drift")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("lockfileVersion") != 3 or not isinstance(lock.get("packages"), dict):
        raise ValueError("npm lockfile v3 is required")
    lock_key = "node_modules/" + package
    locked = lock["packages"].get(lock_key) or {}
    if locked.get("version") != version or locked.get("integrity") != integrity:
        raise ValueError("npm package version or integrity drift")
    for key, item in lock["packages"].items():
        if not key or not key.startswith("node_modules/"):
            continue
        parsed = urlparse(str((item or {}).get("resolved") or ""))
        if (
            (item or {}).get("link") is True
            or parsed.scheme != "https"
            or parsed.hostname != "registry.npmjs.org"
            or not str((item or {}).get("integrity") or "").startswith("sha512-")
        ):
            raise ValueError("npm dependency is not registry-integrity pinned")

    package_root = _package_path(root, package).resolve()
    metadata = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    if metadata.get("name") != package or metadata.get("version") != version:
        raise ValueError("installed npm package identity drift")
    declared = metadata.get("bin")
    if isinstance(declared, str):
        bins = {package.rsplit("/", 1)[-1]: declared}
    elif isinstance(declared, dict):
        bins = {str(key): str(value) for key, value in declared.items()}
    else:
        bins = {}
    expected = {str(key): str(value) for key, value in (resolution.get("bin") or {}).items()}
    if bins != expected or not bins:
        raise ValueError("installed npm executable drift")
    bin_name = package.rsplit("/", 1)[-1] if package.rsplit("/", 1)[-1] in bins else sorted(bins)[0]
    executable = (package_root / _safe_relative(bins[bin_name])).resolve()
    if package_root not in executable.parents or not executable.is_file():
        raise ValueError("npm executable is unavailable")
    return {
        "executable": str(executable.relative_to(root)),
        "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
    }


def _safe_env_name(value: str) -> str:
    name = str(value or "").strip()
    upper = name.upper()
    if (
        not _ENV.fullmatch(name)
        or upper in _DENIED_ENV
        or upper.startswith(("LD_", "DYLD_", "NPM_CONFIG_", "HERMES_"))
    ):
        raise ValueError("unsafe connector environment field")
    return name


def _catalog_launch_spec(manifest: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any]:
    configs = manifest.get("configs") or []
    if not isinstance(configs, list) or any(item.get("type") not in {"env", "file"} for item in configs):
        raise ValueError("catalog npm arguments require an adapter")
    fields = [_safe_env_name(str(item.get("key") or "")) for item in configs]
    if len(fields) != len(set(fields)):
        raise ValueError("catalog npm environment schema is ambiguous")
    runtime_args = [str(value) for value in resolution.get("runtime_args") or []]
    placeholder = re.compile(r"(?i)(your-|path/to|/users/|<[^>]+>|\$\{|actor1|workspace_id|localhost|127\.0\.0\.1)")
    env_args = [match.group(1) for value in runtime_args if (match := _ENV_ARG.fullmatch(value))]
    if any(name not in fields for name in env_args):
        raise ValueError("catalog runtime argument references an undeclared field")
    literal_args = [value for value in runtime_args if not _ENV_ARG.fullmatch(value)]
    def safe_literal(value: str) -> bool:
        path = PurePosixPath(value)
        if path.is_absolute():
            return path == PurePosixPath("/home/connector") or (
                path.parts[:3] == ("/", "home", "connector") and ".." not in path.parts
            )
        return not placeholder.search(value)
    if any(not safe_literal(value) for value in literal_args):
        raise ValueError("catalog npm arguments contain a placeholder")
    static_env = {str(key): str(value) for key, value in (manifest.get("static_env") or {}).items()}
    env_fields = [str(item["key"]) for item in configs if item.get("type") == "env"]
    if any(_safe_env_name(key) in env_fields for key in static_env):
        raise ValueError("catalog npm static environment shadows owner input")
    files = {
        str(item["key"]): str(_safe_relative(str(item.get("path") or "")))
        for item in configs if item.get("type") == "file"
    }
    return {
        "fields": fields,
        "files": files,
        "runtime_args": runtime_args,
        "static_env": static_env,
        "resolution": resolution,
    }


def catalog_npm_spec(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the executable owner-input contract for a safely pinned npm row."""
    resolution = manifest.get("package_resolution") or {}
    lock = resolution.get("dependency_lock") or {}
    package_lock = str(lock.get("package_lock") or "")
    if (
        manifest.get("state") != "direct"
        or lock.get("state") != "resolved"
        or hashlib.sha256(package_lock.encode()).hexdigest() != lock.get("package_lock_sha256")
    ):
        raise ValueError("catalog npm package is not resolved")
    if resolution.get("state") == "git_resolved":
        if (
            not re.fullmatch(r"[a-f0-9]{64}", str(resolution.get("source_archive_sha256") or ""))
            or not re.fullmatch(r"[a-f0-9]{64}", str(resolution.get("package_json_sha256") or ""))
        ):
            raise ValueError("catalog npm Git source is not immutable")
        return {**_catalog_launch_spec(manifest, resolution), "runtime_kind": "node_git"}
    if resolution.get("state") != "resolved":
        raise ValueError("catalog npm package is not resolved")
    return {**_catalog_launch_spec(manifest, resolution), "runtime_kind": "npm"}


def catalog_python_spec(manifest: dict[str, Any]) -> dict[str, Any]:
    resolution = manifest.get("package_resolution") or {}
    lock = resolution.get("dependency_lock") or {}
    requirements_ok = (
        lock.get("state") == "resolved"
        and hashlib.sha256(str(lock.get("requirements") or "").encode()).hexdigest()
        == lock.get("requirements_sha256")
    )
    source_ok = resolution.get("state") == "pypi_resolved" or (
        resolution.get("state") == "git_resolved"
        and lock.get("pinned_source") == resolution.get("pinned_source")
        and re.fullmatch(r"[a-f0-9]{64}", str(lock.get("source_archive_sha256") or ""))
    )
    patch = lock.get("source_patch")
    if patch and (
        not re.fullmatch(r"[a-f0-9]{64}", str(patch.get("before_sha256") or ""))
        or hashlib.sha256(str(patch.get("content") or "").encode()).hexdigest()
        != patch.get("content_sha256")
    ):
        source_ok = False
    if manifest.get("state") != "direct" or not requirements_ok or not source_ok:
        raise ValueError("catalog Python package is not wheel-locked")
    return {**_catalog_launch_spec(manifest, resolution), "runtime_kind": "python"}


def catalog_stdio_spec(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("command") == "npx":
        return catalog_npm_spec(manifest)
    if manifest.get("command") == "uvx":
        return catalog_python_spec(manifest)
    raise ValueError("catalog stdio runtime requires an adapter")


_REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)")


def _locked_requirements(lock: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lock.splitlines():
        match = _REQUIREMENT.match(line)
        if match:
            result[re.sub(r"[-_.]+", "-", match.group(1)).casefold()] = match.group(2)
    if not result:
        raise ValueError("Python dependency lock is empty")
    return result


def verify_python_runtime_tree(runtime_root: Path | str, resolution: dict[str, Any]) -> dict[str, str]:
    root = Path(runtime_root).resolve()
    lock = resolution.get("dependency_lock") or {}
    requirements = str(lock.get("requirements") or "")
    if (
        resolution.get("state") not in {"pypi_resolved", "git_resolved"}
        or lock.get("state") != "resolved"
        or hashlib.sha256(requirements.encode()).hexdigest() != lock.get("requirements_sha256")
    ):
        raise ValueError("Python runtime lock is invalid")
    lock_path = root / "requirements.txt"
    if lock_path.read_text(encoding="utf-8") != requirements:
        raise ValueError("Python runtime lock drift")
    expected = _locked_requirements(requirements)
    installed: dict[str, str] = {}
    for metadata in (root / "venv" / "lib").glob("python*/site-packages/*.dist-info/METADATA"):
        values = {}
        for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(("Name: ", "Version: ")):
                key, value = line.split(": ", 1)
                values[key] = value
        if values.get("Name") and values.get("Version"):
            installed[re.sub(r"[-_.]+", "-", values["Name"]).casefold()] = values["Version"]
    if any(installed.get(name) != version for name, version in expected.items()):
        raise ValueError("Python installed dependency drift")
    executable = root / "venv" / "bin" / str(resolution.get("executable") or "")
    python = root / "venv" / "bin" / "python"
    if not executable.is_file() or not python.is_file():
        raise ValueError("Python MCP executable is unavailable")
    if resolution.get("module") and executable.read_text(encoding="utf-8") != _python_module_wrapper(
        str(resolution["module"])
    ):
        raise ValueError("Python MCP module wrapper drift")
    installed_path = root / "installed.json"
    installed_record = json.loads(installed_path.read_text(encoding="utf-8")) if installed_path.is_file() else {}
    if resolution.get("state") == "git_resolved":
        if (
            installed_record.get("source_archive_sha256") != lock.get("source_archive_sha256")
            or installed_record.get("pinned_source") != resolution.get("pinned_source")
        ):
            raise ValueError("Python Git source installation drift")
    python_runtime = _python_runtime(lock)
    if installed_record.get("python_runtime") != python_runtime:
        raise ValueError("Python system runtime binding drift")
    result = {
        "executable": str(executable.relative_to(root)),
        "python": str(python.relative_to(root)),
        "requirements_sha256": str(lock["requirements_sha256"]),
    }
    result.update(python_base="/usr", python_version=python_runtime["version"])
    return result


def verify_node_git_runtime_tree(runtime_root: Path | str, resolution: dict[str, Any]) -> dict[str, str]:
    root = Path(runtime_root).resolve()
    lock = resolution.get("dependency_lock") or {}
    package_lock = str(lock.get("package_lock") or "")
    if (
        resolution.get("state") != "git_resolved"
        or lock.get("state") != "resolved"
        or hashlib.sha256(package_lock.encode()).hexdigest() != lock.get("package_lock_sha256")
    ):
        raise ValueError("npm Git runtime lock is invalid")
    document = json.loads(package_lock)
    if document.get("lockfileVersion") != 3 or not isinstance(document.get("packages"), dict):
        raise ValueError("npm Git lockfile v3 is required")
    for path, item in document["packages"].items():
        if not path or (item or {}).get("link"):
            continue
        if (
            not str((item or {}).get("resolved") or "").startswith("https://registry.npmjs.org/")
            or not str((item or {}).get("integrity") or "").startswith("sha512-")
        ):
            raise ValueError("npm Git dependency is not registry-integrity pinned")
    project = root / "project"
    package_path = project / "package.json"
    lock_path = project / "package-lock.json"
    if hashlib.sha256(package_path.read_bytes()).hexdigest() != resolution.get("package_json_sha256"):
        raise ValueError("npm Git package metadata drift")
    metadata = json.loads(package_path.read_text(encoding="utf-8"))
    if metadata.get("name") != resolution.get("package") or metadata.get("version") != resolution.get("version"):
        raise ValueError("npm Git package identity drift")
    if lock_path.read_text(encoding="utf-8") != package_lock:
        raise ValueError("npm Git dependency lock drift")
    executable = (project / _safe_relative(str(resolution.get("entry") or ""))).resolve()
    if project not in executable.parents or not executable.is_file():
        raise ValueError("npm Git executable is unavailable")
    installed = json.loads((root / "installed.json").read_text(encoding="utf-8"))
    if (
        installed.get("resolution_fingerprint") != resolution.get("resolution_fingerprint")
        or installed.get("source_archive_sha256") != resolution.get("source_archive_sha256")
    ):
        raise ValueError("npm Git source installation drift")
    return {
        "executable": str(executable.relative_to(root)),
        "lock_sha256": str(lock["package_lock_sha256"]),
    }


def _cached_archive(base: Path, url: str, digest: str, allowed_hosts: set[str] | None = None) -> Path:
    parsed = urlparse(url)
    allowed = allowed_hosts or {"github.com", "codeload.github.com"}
    if parsed.scheme != "https" or parsed.hostname not in allowed:
        raise ValueError("runtime archive host is not allowed")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("runtime archive digest is invalid")
    cache = base / ".downloads"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = cache / f"{digest}.tar.gz"
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
        return target
    temp = cache / f".{digest}.{os.getpid()}.tmp"
    try:
        completed = subprocess.run([
            "/usr/bin/curl", "--silent", "--show-error", "--fail", "--location",
            "--proto", "=https", "--tlsv1.2", "--max-time", "300",
            "--max-filesize", str(512 * 1024 * 1024), "--output", str(temp),
            "--write-out", "%{url_effective}", url,
        ], check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=310)
        redirect_hosts = allowed | ({
            "objects.githubusercontent.com", "release-assets.githubusercontent.com",
        } if "github.com" in allowed or "codeload.github.com" in allowed else set())
        if urlparse(completed.stdout).hostname not in redirect_hosts:
            raise ValueError("runtime archive redirect host is not allowed")
        if temp.stat().st_size > 512 * 1024 * 1024:
            raise ValueError("runtime archive is too large")
        if hashlib.sha256(temp.read_bytes()).hexdigest() != digest:
            raise ValueError("runtime archive integrity mismatch")
        temp.chmod(0o444)
        temp.replace(target)
        return target
    finally:
        temp.unlink(missing_ok=True)


def _extract_locked_archive(archive_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True)
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or member.isdev() or member.isfifo():
                raise ValueError("runtime archive contains an unsafe path")
            if member.issym() or member.islnk():
                link = PurePosixPath(member.linkname)
                combined = (path.parent / link) if member.issym() else link
                if link.is_absolute() or ".." in combined.parts:
                    raise ValueError("runtime archive contains an unsafe link")
        archive.extractall(destination)
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise ValueError("runtime archive root is ambiguous")
    return roots[0]


def _apply_source_patch(project: Path, patch: dict[str, Any]) -> None:
    target = (project / _safe_relative(str(patch.get("path") or ""))).resolve()
    if project != target and project not in target.parents:
        raise ValueError("source patch path escapes its project")
    original = target.read_bytes()
    if hashlib.sha256(original).hexdigest() != patch.get("before_sha256"):
        raise ValueError("source patch input drift")
    content = str(patch.get("content") or "")
    if hashlib.sha256(content.encode()).hexdigest() != patch.get("content_sha256"):
        raise ValueError("source patch integrity mismatch")
    target.write_text(content, encoding="utf-8")


def _system_python(_base: Path, runtime: dict[str, Any]) -> Path:
    executable = Path(str(runtime.get("executable") or ""))
    requested = str(runtime.get("version") or "")
    if (
        runtime.get("kind") != "system"
        or requested not in {"3.11", "3.12"}
        or executable != Path(f"/usr/bin/python{requested}")
        or not executable.is_file()
    ):
        raise ValueError("required system Python runtime is unavailable")
    version = subprocess.run(
        [str(executable), "--version"], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10,
    ).stdout.strip()
    if not version.startswith(f"Python {requested}."):
        raise ValueError("system Python runtime version mismatch")
    return executable


def _python_runtime(lock: dict[str, Any]) -> dict[str, str]:
    if isinstance(lock.get("python_runtime"), dict):
        return {str(key): str(value) for key, value in lock["python_runtime"].items()}
    version = str(lock.get("python_version") or "")
    return {"kind": "system", "version": version, "executable": f"/usr/bin/python{version}"}


async def _run_source_install(command: list[str], cwd: Path, env: dict[str, str]) -> None:
    properties = [
        "NoNewPrivileges=yes", "PrivateDevices=yes", "PrivateNetwork=yes",
        "ProtectSystem=strict", "ProtectKernelTunables=yes", "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes", "LockPersonality=yes", "MemoryMax=1G", "TasksMax=128",
        f"ReadWritePaths={cwd}",
    ]
    wrapped = [
        "/usr/bin/systemd-run", "--quiet", "--pipe", "--wait", "--collect",
        "--service-type=exec", f"--working-directory={cwd}",
        *(value for prop in properties for value in ("--property", prop)),
        *(f"--setenv={key}={value}" for key, value in env.items()),
        *command,
    ]
    await _run_install(wrapped, cwd, {"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin", "TZ": "UTC"})


async def prepare_python_runtime(
    runtime_base: Path | str,
    resolution: dict[str, Any],
    *,
    runner: Callable[[list[str], Path, dict[str, str]], Awaitable[None]] = _run_install,
    source_runner: Callable[[list[str], Path, dict[str, str]], Awaitable[None]] = _run_source_install,
    python_resolver: Callable[[Path, dict[str, Any]], Path] = _system_python,
) -> Path:
    lock = resolution.get("dependency_lock") or {}
    python_runtime = _python_runtime(lock)
    if resolution.get("state") == "git_resolved":
        fingerprint = hashlib.sha256(
            f"{lock.get('source_archive_sha256')}\0{resolution.get('subdirectory')}\0"
            f"{lock.get('requirements_sha256')}\0{(lock.get('source_patch') or {}).get('content_sha256', '')}\0"
            f"{json.dumps(python_runtime, sort_keys=True)}".encode()
        ).hexdigest()
    else:
        fingerprint = hashlib.sha256(
            f"{resolution.get('resolution_fingerprint')}\0{lock.get('requirements_sha256')}\0"
            f"{json.dumps(python_runtime, sort_keys=True)}".encode()
        ).hexdigest()
    if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise ValueError("Python runtime fingerprint is invalid")
    base = Path(runtime_base).resolve()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    base.chmod(0o700)
    target = base / fingerprint
    lock_fd = os.open(base / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX)
    temp: Path | None = None
    try:
        if target.is_dir():
            verify_python_runtime_tree(target, resolution)
            return target
        temp = Path(tempfile.mkdtemp(prefix=".python-", dir=base))
        requirements = str(lock.get("requirements") or "")
        if hashlib.sha256(requirements.encode()).hexdigest() != lock.get("requirements_sha256"):
            raise ValueError("Python dependency lock integrity mismatch")
        (temp / "requirements.txt").write_text(requirements, encoding="utf-8")
        env = {"HOME": str(temp), "LANG": "C.UTF-8", "PATH": "/usr/bin:/bin", "TZ": "UTC"}
        venv = temp / "venv"
        python = str(await asyncio.to_thread(python_resolver, base, python_runtime))
        await runner([python, "-m", "venv", "--copies", str(venv)], temp, env)
        pip = [str(venv / "bin" / "python"), "-m", "pip"]
        if lock.get("source_build"):
            downloads = temp / "downloads"
            downloads.mkdir()
            await runner([
                *pip, "download", "--disable-pip-version-check", "--no-input",
                "--require-hashes", "--dest", str(downloads), "-r", str(temp / "requirements.txt"),
            ], temp, env)
            artifacts = sorted(path for path in downloads.iterdir() if path.is_file())
            if not artifacts:
                raise ValueError("Python dependency download is empty")
            wheels = [path for path in artifacts if path.suffix == ".whl"]
            sources = [path for path in artifacts if path.suffix != ".whl"]
            for wheel in wheels:
                await runner([
                    *pip, "install", "--disable-pip-version-check", "--no-input",
                    "--no-compile", "--no-deps", "--no-index", str(wheel),
                ], temp, env)
            for index, source in enumerate(sources):
                build_root = temp / f".source-{index}"
                build_root.mkdir()
                locked_source = build_root / source.name
                shutil.copyfile(source, locked_source)
                built = build_root / "built"
                built.mkdir()
                await source_runner([
                    *pip, "wheel", "--disable-pip-version-check", "--no-input",
                    "--no-deps", "--no-build-isolation", "--no-index",
                    "--find-links", str(downloads), "--wheel-dir", str(built), str(locked_source),
                ], build_root, {**env, "TMPDIR": str(build_root)})
                built_wheels = list(built.glob("*.whl"))
                if len(built_wheels) != 1:
                    raise ValueError("Python source dependency did not produce exactly one wheel")
                await runner([
                    *pip, "install", "--disable-pip-version-check", "--no-input",
                    "--no-compile", "--no-deps", "--no-index", str(built_wheels[0]),
                ], temp, env)
        else:
            await runner([
                *pip, "install", "--disable-pip-version-check", "--no-input", "--no-compile",
                "--require-hashes", "--only-binary=:all:", "-r", str(temp / "requirements.txt"),
            ], temp, env)
        if resolution.get("state") == "git_resolved":
            archive = await asyncio.to_thread(
                _cached_archive,
                base,
                str(lock.get("source_archive_url") or ""),
                str(lock.get("source_archive_sha256") or ""),
            )
            source_root = await asyncio.to_thread(_extract_locked_archive, archive, temp / "source")
            project = (source_root / str(resolution.get("subdirectory") or "")).resolve()
            if source_root not in project.parents and project != source_root:
                raise ValueError("Python Git project path escapes its source archive")
            if not (project / "pyproject.toml").is_file():
                raise ValueError("Python Git project is unavailable")
            if lock.get("source_patch"):
                _apply_source_patch(project, lock["source_patch"])
            wheels = temp / "wheels"
            wheels.mkdir()
            await source_runner([
                python, "-m", "pip", "wheel", "--disable-pip-version-check", "--no-input",
                "--no-deps", "--no-build-isolation", "--no-cache-dir",
                "--wheel-dir", str(wheels), str(project),
            ], temp, {
                **env,
                "PYTHONPATH": str(
                    venv / "lib" / f"python{python_runtime['version']}" / "site-packages"
                ),
            })
            built = list(wheels.glob("*.whl"))
            if len(built) != 1:
                raise ValueError("Python Git source did not produce exactly one wheel")
            await runner([
                str(venv / "bin" / "python"), "-m", "pip", "install",
                "--disable-pip-version-check", "--no-input", "--no-compile",
                "--no-deps", "--no-index", str(built[0]),
            ], temp, env)
        if resolution.get("module"):
            wrapper = venv / "bin" / str(resolution.get("executable") or "")
            wrapper.write_text(_python_module_wrapper(str(resolution["module"])), encoding="utf-8")
            wrapper.chmod(0o555)
        record = {
            "runtime_fingerprint": fingerprint,
            "python_runtime": python_runtime,
            **({
                "source_archive_sha256": lock.get("source_archive_sha256"),
                "pinned_source": resolution.get("pinned_source"),
            } if resolution.get("state") == "git_resolved" else {}),
        }
        (temp / "installed.json").write_text(
            json.dumps(record, sort_keys=True) + "\n", encoding="utf-8"
        )
        installed = verify_python_runtime_tree(temp, resolution)
        (temp / "installed.json").write_text(
            json.dumps({**record, **installed}, sort_keys=True) + "\n", encoding="utf-8"
        )
        temp.rename(target)
        temp = None
        _freeze_tree(target)
        return target
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


async def prepare_node_git_runtime(
    runtime_base: Path | str,
    resolution: dict[str, Any],
    *,
    runner: Callable[[list[str], Path, dict[str, str]], Awaitable[None]] = _run_install,
    source_runner: Callable[[list[str], Path, dict[str, str]], Awaitable[None]] = _run_source_install,
) -> Path:
    fingerprint = str(resolution.get("resolution_fingerprint") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise ValueError("npm Git resolution fingerprint is invalid")
    base = Path(runtime_base).resolve()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    base.chmod(0o700)
    target = base / fingerprint
    lock_fd = os.open(base / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX)
    temp: Path | None = None
    try:
        if target.is_dir():
            verify_node_git_runtime_tree(target, resolution)
            return target
        temp = Path(tempfile.mkdtemp(prefix=".node-git-", dir=base))
        archive = await asyncio.to_thread(
            _cached_archive,
            base,
            str(resolution.get("source_archive_url") or ""),
            str(resolution.get("source_archive_sha256") or ""),
        )
        source_root = await asyncio.to_thread(_extract_locked_archive, archive, temp / "source")
        project = temp / "project"
        source_root.rename(project)
        shutil.rmtree(temp / "source")
        package_path = project / "package.json"
        source_lock_path = project / "package-lock.json"
        if (
            hashlib.sha256(package_path.read_bytes()).hexdigest() != resolution.get("package_json_sha256")
            or hashlib.sha256(source_lock_path.read_bytes()).hexdigest()
            != (resolution.get("dependency_lock") or {}).get("source_package_lock_sha256")
        ):
            raise ValueError("npm Git source metadata integrity mismatch")
        package_lock = str((resolution.get("dependency_lock") or {}).get("package_lock") or "")
        source_lock_path.write_text(package_lock, encoding="utf-8")
        home = temp / ".home"
        home.mkdir(mode=0o700)
        env = {"HOME": str(home), "LANG": "C.UTF-8", "PATH": "/usr/bin:/bin", "TZ": "UTC"}
        await runner([
            "/usr/bin/npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund",
            "--registry=https://registry.npmjs.org",
        ], project, env)
        for package in resolution.get("rebuild") or []:
            if not _PACKAGE_NAME.fullmatch(str(package)):
                raise ValueError("unsafe npm Git rebuild package")
            await source_runner([
                "/usr/bin/npm", "rebuild", str(package), "--build-from-source",
            ], project, env)
        await source_runner(["/usr/bin/npm", "run", "build"], project, env)
        (temp / "installed.json").write_text(json.dumps({
            "resolution_fingerprint": fingerprint,
            "source_archive_sha256": resolution.get("source_archive_sha256"),
        }, sort_keys=True) + "\n", encoding="utf-8")
        verify_node_git_runtime_tree(temp, resolution)
        temp.rename(target)
        temp = None
        _freeze_tree(target)
        return target
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def stdio_runtime_fingerprint(spec: dict[str, Any]) -> str:
    resolution = spec["resolution"]
    if spec["runtime_kind"] in {"npm", "node_git"}:
        return str(resolution["resolution_fingerprint"])
    lock = resolution["dependency_lock"]
    if resolution.get("state") == "git_resolved":
        return hashlib.sha256(
            f"{lock['source_archive_sha256']}\0{resolution.get('subdirectory')}\0"
            f"{lock['requirements_sha256']}\0{(lock.get('source_patch') or {}).get('content_sha256', '')}\0"
            f"{json.dumps(lock['python_runtime'], sort_keys=True)}".encode()
        ).hexdigest()
    return hashlib.sha256(
        f"{resolution['resolution_fingerprint']}\0{lock['requirements_sha256']}\0"
        f"{json.dumps(_python_runtime(lock), sort_keys=True)}".encode()
    ).hexdigest()


async def prepare_stdio_runtime(runtime_base: Path | str, manifest: dict[str, Any]) -> Path:
    spec = catalog_stdio_spec(manifest)
    if spec["runtime_kind"] == "npm":
        return await prepare_npm_runtime(runtime_base, spec["resolution"])
    if spec["runtime_kind"] == "node_git":
        return await prepare_node_git_runtime(runtime_base, spec["resolution"])
    return await prepare_python_runtime(runtime_base, spec["resolution"])


def _environment_line(name: str, value: str) -> str:
    if "\0" in value or "\n" in value or "\r" in value or len(value) > 8192:
        raise ValueError("unsafe connector environment value")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{name}="{escaped}"'


def write_runtime_environment(
    path: Path | str,
    values: dict[str, str],
    *,
    allowed_fields: list[str],
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    allowed = [_safe_env_name(value) for value in allowed_fields]
    if set(values) - set(allowed):
        raise ValueError("connector environment does not match its schema")
    lines = [_environment_line("HERMES_CONNECTOR_ALLOWED_ENV", ",".join(sorted(allowed)))]
    lines.extend(_environment_line(_safe_env_name(name), str(value)) for name, value in sorted(values.items()))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(0o600)
    return target


def materialize_runtime_values(
    sandbox_home: Path | str,
    values: dict[str, str],
    files: dict[str, str],
) -> dict[str, str]:
    home = Path(sandbox_home).resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    environment = dict(values)
    for field, relative in files.items():
        if field not in environment:
            raise ValueError("connector secret file field is missing")
        content = str(environment.pop(field))
        if "\0" in content or len(content.encode()) > 1024 * 1024:
            raise ValueError("connector secret file is unsafe")
        target = (home / _safe_relative(relative)).resolve()
        if home not in target.parents:
            raise ValueError("connector secret file escapes its sandbox home")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.parent.chmod(0o700)
        target.write_text(content, encoding="utf-8")
        target.chmod(0o600)
    return environment


def write_runtime_resolver(path: Path | str) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        if target.read_text(encoding="ascii") != "nameserver 8.8.8.8\noptions edns0\n":
            raise ValueError("connector resolver configuration drift")
        target.chmod(0o444)
        return target
    target.write_text("nameserver 8.8.8.8\noptions edns0\n", encoding="ascii")
    target.chmod(0o444)
    return target


def build_linux_stdio_command(
    *,
    runtime_root: Path | str,
    executable: str,
    runtime_args: list[str],
    sandbox_home: Path | str,
    env_file: Path | str,
    resolver_file: Path | str,
    python_executable: str | None = None,
    python_base: Path | str | None = None,
    python_version: str | None = None,
) -> list[str]:
    root = Path(runtime_root).resolve()
    target = (root / _safe_relative(executable)).resolve()
    if root not in target.parents or not target.is_file():
        raise ValueError("stdio executable is outside the immutable runtime")
    home = Path(sandbox_home).resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    environment = Path(env_file).resolve()
    if not environment.is_file() or environment.stat().st_mode & 0o777 != 0o600:
        raise ValueError("private connector environment file is required")
    resolver = Path(resolver_file).resolve()
    if (
        not resolver.is_file()
        or resolver.stat().st_mode & 0o777 != 0o444
        or resolver.read_text(encoding="ascii") != "nameserver 8.8.8.8\noptions edns0\n"
    ):
        raise ValueError("fixed public connector resolver is required")
    runtime_target = "/runtime/" + str(target.relative_to(root))
    runtime_command = ["/usr/bin/node", runtime_target]
    python_mount: list[str] = []
    python_environment: list[str] = []
    if python_executable is not None:
        python_target = (root / _safe_relative(python_executable)).resolve()
        base_python = Path(python_base or "").resolve()
        if root not in python_target.parents or not python_target.is_file() or not base_python.is_dir():
            raise ValueError("Python stdio runtime is incomplete")
        if python_version not in {"3.11", "3.12"}:
            raise ValueError("Python stdio runtime version is unavailable")
        runtime_command = [f"/usr/bin/python{python_version}", runtime_target]
        python_environment = [
            "--setenv", "PYTHONPATH", f"/runtime/venv/lib/python{python_version}/site-packages",
        ]
        python_mount = [] if base_python == Path("/usr") else [
            "--ro-bind", str(base_python), str(base_python)
        ]
    if any(_ENV_ARG.fullmatch(str(arg)) for arg in runtime_args):
        runtime_command = (
            [runtime_command[0], "-c", _PYTHON_ENV_ARG_LOADER, runtime_target]
            if python_executable is not None else
            ["/usr/bin/node", "--input-type=module", "-e", _NODE_ENV_ARG_LOADER, runtime_target]
        )
    bwrap = [
        "/usr/bin/bwrap", "--die-with-parent", "--new-session", "--unshare-user-try",
        "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try",
        "--share-net", "--cap-drop", "ALL", "--dev", "/dev", "--proc", "/proc",
        "--tmpfs", "/tmp", "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/sbin", "/sbin", "--ro-bind", "/etc", "/etc",
        "--ro-bind", str(resolver), "/etc/resolv.conf",
        "--ro-bind", str(root), "/runtime", *python_mount,
        "--bind", str(home), "/home/connector",
        "--chdir", "/home/connector", *python_environment, "--", *runtime_command,
        *(str(arg) for arg in runtime_args),
    ]
    properties = [
        "NoNewPrivileges=yes", "PrivateDevices=yes", "ProtectKernelTunables=yes",
        "ProtectControlGroups=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes",
        "MemoryMax=512M", "TasksMax=64", "CPUQuota=100%",
        f"EnvironmentFile={environment}",
        *[f"IPAddressDeny={network}" for network in _PRIVATE_NETWORKS],
    ]
    return [
        "/usr/bin/systemd-run", "--quiet", "--pipe", "--wait", "--collect",
        "--service-type=exec", f"--working-directory={Path(__file__).parents[1]}",
        *(value for prop in properties for value in ("--property", prop)),
        sys.executable, "-m", "hermes_multitenancy.connector_stdio_exec",
        "--env-file", str(environment), "--", *bwrap,
    ]
