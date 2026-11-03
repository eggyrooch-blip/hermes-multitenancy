"""Owner-isolated runtime for pinned WorkBuddy npm CLI connectors."""
from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .connector_stdio_runtime import (
    _cached_archive,
    _freeze_tree,
    build_linux_stdio_command,
    prepare_npm_runtime,
    verify_npm_runtime_tree,
    write_runtime_environment,
    write_runtime_resolver,
)


_URL = re.compile(r"https://[^\s'\"<>]+")
_ARCHIVE_HOSTS = {"oss-openclaw.77ircloud.com", "personal.wpscdn.cn"}


def catalog_cli_spec(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate one frozen CLI lifecycle and return its executable contract."""
    state = manifest.get("state")
    if state not in {"npm_resolvable", "embedded_npm", "pinned_archive"} or manifest.get("row_key") == "workbuddy:feishu":
        raise ValueError("catalog CLI requires a dedicated adapter")
    if not re.fullmatch(r"[a-f0-9]{64}", str(manifest.get("source_sha256") or "")):
        raise ValueError("catalog CLI source is not immutable")
    resolution = manifest.get("package_resolution") or {}
    if state == "npm_resolvable":
        lock = resolution.get("dependency_lock") or {}
        package_lock = str(lock.get("package_lock") or "")
        if (
            resolution.get("state") != "resolved"
            or lock.get("state") != "resolved"
            or hashlib.sha256(package_lock.encode()).hexdigest() != lock.get("package_lock_sha256")
        ):
            raise ValueError("catalog CLI package is not resolved")
    elif state == "embedded_npm":
        try:
            tarball = base64.b64decode(str(resolution.get("tarball_base64") or ""), validate=True)
        except ValueError as exc:
            raise ValueError("embedded catalog CLI package is invalid") from exc
        if (
            resolution.get("state") != "embedded"
            or not re.fullmatch(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", str(resolution.get("package") or ""))
            or not re.fullmatch(r"[a-f0-9]{64}", str(resolution.get("resolution_fingerprint") or ""))
            or hashlib.sha256(tarball).hexdigest() != resolution.get("tarball_sha256")
        ):
            raise ValueError("embedded catalog CLI package is not resolved")
    else:
        archive_url = str(resolution.get("archive_url") or "")
        host = str(urlparse(archive_url).hostname or "")
        if (
            resolution.get("state") != "pinned_archive"
            or host not in _ARCHIVE_HOSTS
            or not re.fullmatch(r"[a-f0-9]{64}", str(resolution.get("archive_sha256") or ""))
            or not re.fullmatch(r"[a-f0-9]{64}", str(resolution.get("resolution_fingerprint") or ""))
        ):
            raise ValueError("pinned catalog CLI archive is not resolved")
    bins = {str(key): str(value) for key, value in (resolution.get("bin") or {}).items()}
    commands = [
        *([manifest["setup_args"]] if manifest.get("setup_args") else []),
        *(manifest.get("auth_steps") or []),
        manifest.get("status_args") or [],
        manifest.get("logout_args") or [],
    ]
    if not manifest.get("auth_steps") or not manifest.get("status_args"):
        raise ValueError("catalog CLI lifecycle is incomplete")
    if any(
        not isinstance(command, list) or not command or command[0] not in bins
        or len(command) > 32 or any(not isinstance(arg, str) or len(arg) > 2048 for arg in command)
        for command in commands if command
    ):
        raise ValueError("catalog CLI lifecycle command is not admitted")
    domains = [str(value).lower() for value in manifest.get("auth_domains") or []]
    if not domains or any(not re.fullmatch(r"[a-z0-9.-]{1,253}", value) for value in domains):
        raise ValueError("catalog CLI authorization domain is unavailable")
    if manifest.get("status_match"):
        re.compile(str(manifest["status_match"]))
    return {**manifest, "resolution": resolution, "bins": bins, "auth_domains": domains}


def _executable(spec: dict[str, Any], command: list[str]) -> str:
    if spec["resolution"].get("state") == "pinned_archive":
        relative = Path(str(spec["bins"][command[0]]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("catalog CLI executable path is unsafe")
        return str(relative)
    package = str(spec["resolution"]["package"])
    relative = Path(str(spec["bins"][command[0]]))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("catalog CLI executable path is unsafe")
    return str(Path("node_modules") / Path(*package.split("/")) / relative)


def _paths(runtime_base: Path, connector_id: str) -> tuple[Path, Path, Path]:
    owner_key = hashlib.sha256(connector_id.encode()).hexdigest()[:24]
    return (
        runtime_base,
        runtime_base.parent / "connector-cli-homes" / owner_key,
        runtime_base.parent / "connector-runtime-resolv.conf",
    )


async def prepare_cli_runtime(runtime_base: Path | str, manifest: dict[str, Any]) -> Path:
    spec = catalog_cli_spec(manifest)
    if spec["resolution"].get("state") == "pinned_archive":
        return await _prepare_pinned_runtime(Path(runtime_base), spec["resolution"])
    if spec["resolution"].get("state") == "embedded":
        return await _prepare_embedded_runtime(Path(runtime_base), spec["resolution"])
    return await prepare_npm_runtime(runtime_base, spec["resolution"])


def _verify_embedded_runtime(root: Path, resolution: dict[str, Any]) -> None:
    package = Path(*str(resolution["package"]).split("/"))
    package_root = root / "node_modules" / package
    document = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    if document.get("name") != resolution["package"] or document.get("version") != resolution["version"]:
        raise ValueError("embedded catalog CLI identity mismatch")
    for executable in (resolution.get("bin") or {}).values():
        path = package_root / str(executable)
        if not path.is_file() or package_root.resolve() not in path.resolve().parents:
            raise ValueError("embedded catalog CLI executable is unavailable")


def _extract_embedded_runtime(temp: Path, resolution: dict[str, Any]) -> None:
    payload = base64.b64decode(str(resolution["tarball_base64"]), validate=True)
    package_root = temp / "node_modules" / Path(*str(resolution["package"]).split("/"))
    package_root.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            relative = Path(member.name)
            if not relative.parts or relative.parts[0] != "package" or ".." in relative.parts:
                raise ValueError("embedded catalog CLI archive path is unsafe")
            target = package_root.joinpath(*relative.parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("embedded catalog CLI file is unavailable")
                target.write_bytes(source.read())
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                raise ValueError("embedded catalog CLI archive member is unsupported")


async def _prepare_embedded_runtime(runtime_base: Path, resolution: dict[str, Any]) -> Path:
    base = runtime_base.resolve()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    fingerprint = str(resolution["resolution_fingerprint"])
    target = base / fingerprint
    lock_fd = os.open(base / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX)
    temp: Path | None = None
    try:
        if target.is_dir():
            _verify_embedded_runtime(target, resolution)
            return target
        temp = Path(tempfile.mkdtemp(prefix=".cli-embedded-", dir=base))
        _extract_embedded_runtime(temp, resolution)
        _verify_embedded_runtime(temp, resolution)
        (temp / "installed.json").write_text(
            json.dumps({"resolution_fingerprint": fingerprint}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _freeze_tree(temp)
        temp.rename(target)
        temp = None
        return target
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _verify_pinned_runtime(root: Path, resolution: dict[str, Any]) -> None:
    for executable in (resolution.get("bin") or {}).values():
        relative = Path(str(executable))
        path = (root / relative).resolve()
        if relative.is_absolute() or ".." in relative.parts or root.resolve() not in path.parents or not path.is_file():
            raise ValueError("pinned catalog CLI executable is unavailable")


def _extract_pinned_runtime(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts or member.isdev() or member.isfifo():
                raise ValueError("pinned catalog CLI archive path is unsafe")
            if member.isdir():
                (destination / relative).mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("pinned catalog CLI file is unavailable")
                target.write_bytes(source.read())
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
            else:
                raise ValueError("pinned catalog CLI archive member is unsupported")


async def _prepare_pinned_runtime(runtime_base: Path, resolution: dict[str, Any]) -> Path:
    base = runtime_base.resolve()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    fingerprint = str(resolution["resolution_fingerprint"])
    target = base / fingerprint
    lock_fd = os.open(base / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX)
    temp: Path | None = None
    try:
        if target.is_dir():
            _verify_pinned_runtime(target, resolution)
            return target
        archive_url = str(resolution["archive_url"])
        archive = await asyncio.to_thread(
            _cached_archive,
            base,
            archive_url,
            str(resolution["archive_sha256"]),
            {str(urlparse(archive_url).hostname)},
        )
        temp = Path(tempfile.mkdtemp(prefix=".cli-pinned-", dir=base))
        _extract_pinned_runtime(archive, temp)
        _verify_pinned_runtime(temp, resolution)
        (temp / "installed.json").write_text(
            json.dumps({"resolution_fingerprint": fingerprint}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _freeze_tree(temp)
        temp.rename(target)
        temp = None
        return target
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        await asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _command(
    runtime_base: Path,
    connector_id: str,
    spec: dict[str, Any],
    argv: list[str],
) -> tuple[list[str], Path, str]:
    runtime_root = runtime_base / str(spec["resolution"]["resolution_fingerprint"])
    if spec["resolution"].get("state") == "embedded":
        _verify_embedded_runtime(runtime_root, spec["resolution"])
    elif spec["resolution"].get("state") == "pinned_archive":
        _verify_pinned_runtime(runtime_root, spec["resolution"])
    else:
        verify_npm_runtime_tree(runtime_root, spec["resolution"])
    _base, home, resolver = _paths(runtime_base, connector_id)
    env_dir = runtime_base.parent / "connector-cli-env"
    env_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix="cli-", suffix=".env", dir=env_dir)
    os.close(fd)
    env_path = Path(raw)
    values = {**(spec.get("static_env") or {}), "BROWSER": "/bin/true"}
    write_runtime_environment(env_path, values, allowed_fields=list(values))
    command = build_linux_stdio_command(
        runtime_root=runtime_root,
        executable=_executable(spec, argv),
        runtime_args=argv[1:],
        sandbox_home=home,
        env_file=env_path,
        resolver_file=write_runtime_resolver(resolver),
    )
    unit = "hermes-cli-" + secrets.token_hex(8)
    command[1:1] = [f"--unit={unit}", "--property", "RuntimeMaxSec=600"]
    return command, env_path, unit


async def _run(runtime_base: Path, connector_id: str, spec: dict[str, Any], argv: list[str]) -> tuple[int, str]:
    command, env_path, _unit = _command(runtime_base, connector_id, spec, argv)
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=45)
        return int(process.returncode or 0), stdout.decode(errors="replace")[-64 * 1024:]
    finally:
        env_path.unlink(missing_ok=True)


async def cli_status(
    runtime_base: Path | str, connector_id: str, manifest: dict[str, Any]
) -> bool:
    spec = catalog_cli_spec(manifest)
    code, output = await _run(Path(runtime_base), connector_id, spec, spec["status_args"])
    if code:
        return False
    expected = spec.get("status_match_json") or {}
    if expected:
        try:
            document = json.loads(output)
        except json.JSONDecodeError:
            return False
        if any(str(document.get(key)).casefold() != str(value).casefold() for key, value in expected.items()):
            return False
    pattern = str(spec.get("status_match") or "")
    return bool(expected or (pattern and re.search(pattern, output)))


async def start_cli_auth(
    runtime_base: Path | str, connector_id: str, manifest: dict[str, Any]
) -> tuple[str, asyncio.subprocess.Process, Path, str]:
    spec = catalog_cli_spec(manifest)
    base = Path(runtime_base)
    if spec.get("setup_args"):
        code, _output = await _run(base, connector_id, spec, spec["setup_args"])
        if code:
            raise RuntimeError("catalog CLI setup failed")
    for step in spec["auth_steps"][:-1]:
        code, _output = await _run(base, connector_id, spec, step)
        if code:
            raise RuntimeError("catalog CLI authorization preparation failed")
    command, env_path, unit = _command(base, connector_id, spec, spec["auth_steps"][-1])
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None
    output = ""
    try:
        async with asyncio.timeout(15):
            while line := await process.stdout.readline():
                output += line.decode(errors="replace")
                if match := _URL.search(output):
                    url = match.group(0).rstrip(".,;)")
                    host = str(urlparse(url).hostname or "").lower()
                    if not any(host == domain or host.endswith("." + domain) for domain in spec["auth_domains"]):
                        raise PermissionError("catalog CLI returned an untrusted authorization URL")
                    return url, process, env_path, unit
                if len(output) > 64 * 1024:
                    break
    except Exception:
        process.kill()
        await process.wait()
        env_path.unlink(missing_ok=True)
        raise
    process.kill()
    await process.wait()
    env_path.unlink(missing_ok=True)
    raise RuntimeError("catalog CLI did not return an authorization URL")


async def stop_cli_auth(process: asyncio.subprocess.Process, env_path: Path, unit: str) -> None:
    if process.returncode is None:
        process.kill()
        await process.wait()
    stop = await asyncio.create_subprocess_exec(
        "/usr/bin/systemctl", "stop", unit,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.wait_for(stop.wait(), timeout=10)
    env_path.unlink(missing_ok=True)


async def logout_cli(runtime_base: Path | str, connector_id: str, manifest: dict[str, Any]) -> None:
    spec = catalog_cli_spec(manifest)
    if spec.get("logout_args"):
        await _run(Path(runtime_base), connector_id, spec, spec["logout_args"])
