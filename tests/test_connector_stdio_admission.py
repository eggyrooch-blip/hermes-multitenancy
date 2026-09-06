import json
from pathlib import Path
import zipfile

import pytest


def test_stdio_proxy_keeps_only_bounded_jsonrpc_frames():
    from hermes_multitenancy.connector_stdio_proxy import _MAX_LINE, _jsonrpc_line

    assert _jsonrpc_line(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
    assert not _jsonrpc_line(b"legacy startup log\n")
    assert not _jsonrpc_line(b'{"id":1}\n')
    assert not _jsonrpc_line(b"{" + b"x" * _MAX_LINE + b"}\n")


def test_stdio_admission_reviews_every_row_without_launch_or_secret_copy(tmp_path: Path):
    from hermes_multitenancy.connector_stdio_admission import admit_stdio_catalog

    marker = tmp_path / "must-not-exist"
    source = tmp_path / "source.jsonl"
    rows = [
        {
            "product": "TRAE",
            "catalog_id": "missing",
            "transport": "stdio",
            "command": None,
        },
        {
            "product": "TRAE",
            "catalog_id": "shell",
            "transport": "stdio",
            "command": "sh",
        },
        {
            "product": "TRAE",
            "catalog_id": "npx",
            "transport": "stdio",
            "command": f"npx && touch {marker}",
            "credential_key_names": ["TOP_SECRET_TOKEN"],
            "version": "2026.08.31_0001",
            "certified": True,
        },
        {"product": "WorkBuddy", "catalog_id": "remote", "transport": "http"},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    results = admit_stdio_catalog(source)

    assert [item["row_key"] for item in results] == [
        "trae:missing",
        "trae:shell",
        "trae:npx",
    ]
    assert [item["verdict"] for item in results] == [
        "rejected",
        "rejected",
        "needs_sandbox",
    ]
    assert [item["reason_code"] for item in results] == [
        "missing_stdio_command",
        "indirect_or_shell_launcher",
        "package_identity_missing",
    ]
    assert all(item["complete"] is True for item in results)
    assert not marker.exists()
    assert "TOP_SECRET_TOKEN" not in json.dumps(results)


def test_stdio_sandbox_verifies_wheels_and_builds_secretless_limited_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from hermes_multitenancy import connector_stdio_sandbox as sandbox

    # The injected runner never executes sandbox-exec; simulate its admission
    # precondition so Linux CI still verifies the generated macOS command/env.
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox.os, "access", lambda path, mode: path == "/usr/bin/sandbox-exec")
    run_sandboxed_stdio_handshake = sandbox.run_sandboxed_stdio_handshake

    wheel = tmp_path / "sample.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("sample/__init__.py", "")
    import hashlib

    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    seen = {}

    async def runner(command, env):
        seen["command"] = command
        seen["env"] = env
        return ["one", "two"]

    result = __import__("asyncio").run(
        run_sandboxed_stdio_handshake(
            python=Path(__import__("sys").executable),
            module="sample",
            wheels=[(wheel, digest)],
            sandbox_home=tmp_path / "home",
            runner=runner,
        )
    )

    assert result["verdict"] == "pass"
    assert result["tool_count"] == 2
    assert seen["command"][0] == "/usr/bin/sandbox-exec"
    assert "TOP_SECRET_TOKEN" not in json.dumps(seen)
    assert set(seen["env"]) == {
        "HOME",
        "LANG",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "PYTHONPATH",
        "TMPDIR",
        "TZ",
    }

    with pytest.raises(ValueError, match="sha256"):
        __import__("asyncio").run(
            run_sandboxed_stdio_handshake(
                python=Path(__import__("sys").executable),
                module="sample",
                wheels=[(wheel, "0" * 64)],
                sandbox_home=tmp_path / "bad-home",
                runner=runner,
            )
        )


def test_npm_runtime_tree_and_linux_launcher_are_pinned_private_and_secretless(tmp_path: Path):
    from hermes_multitenancy.connector_stdio_runtime import (
        build_linux_stdio_command,
        verify_npm_runtime_tree,
        write_runtime_environment,
        write_runtime_resolver,
    )

    runtime = tmp_path / "runtime"
    package = runtime / "node_modules" / "demo-mcp"
    (package / "dist").mkdir(parents=True)
    (package / "dist" / "cli.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    (package / "package.json").write_text(json.dumps({
        "name": "demo-mcp", "version": "1.2.3", "bin": {"demo-mcp": "dist/cli.js"},
    }), encoding="utf-8")
    integrity = "sha512-" + "A" * 88
    lock_text = json.dumps({
        "lockfileVersion": 3,
        "packages": {
            "": {"dependencies": {"demo-mcp": "1.2.3"}},
            "node_modules/demo-mcp": {
                "version": "1.2.3",
                "resolved": "https://registry.npmjs.org/demo-mcp/-/demo-mcp-1.2.3.tgz",
                "integrity": integrity,
                "bin": {"demo-mcp": "dist/cli.js"},
            },
        },
    }, sort_keys=True, separators=(",", ":")) + "\n"
    (runtime / "package-lock.json").write_text(lock_text, encoding="utf-8")
    resolution = {
        "state": "resolved",
        "package": "demo-mcp",
        "version": "1.2.3",
        "integrity": integrity,
        "bin": {"demo-mcp": "dist/cli.js"},
        "resolution_fingerprint": "a" * 64,
        "dependency_lock": {
            "state": "resolved", "package_lock": lock_text,
            "package_lock_sha256": __import__("hashlib").sha256(lock_text.encode()).hexdigest(),
        },
    }

    installed = verify_npm_runtime_tree(runtime, resolution)
    assert installed["executable"] == "node_modules/demo-mcp/dist/cli.js"
    assert len(installed["lock_sha256"]) == 64

    env_file = tmp_path / "run" / "owner.env"
    write_runtime_environment(
        env_file,
        {"API_TOKEN": "owner-secret"},
        allowed_fields=["API_TOKEN"],
    )
    resolver_file = write_runtime_resolver(tmp_path / "run" / "resolv.conf")
    command = build_linux_stdio_command(
        runtime_root=runtime,
        executable=installed["executable"],
        runtime_args=["--safe", "@env:API_TOKEN"],
        sandbox_home=tmp_path / "sandbox-home",
        env_file=env_file,
        resolver_file=resolver_file,
    )
    rendered = " ".join(command)
    assert command[0] == "/usr/bin/systemd-run"
    assert "/usr/bin/bwrap" in command
    assert "IPAddressDeny=10.0.0.0/8" in command
    assert "IPAddressDeny=169.254.0.0/16" in command
    assert "--cap-drop" in command and "ALL" in command
    assert "--working-directory=" in rendered
    assert str(resolver_file) in command and "/etc/resolv.conf" in command
    assert resolver_file.read_text(encoding="ascii") == "nameserver 8.8.8.8\noptions edns0\n"
    assert "owner-secret" not in rendered
    assert "@env:API_TOKEN" in command and "--input-type=module" in command
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert "owner-secret" in env_file.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="environment"):
        write_runtime_environment(
            tmp_path / "bad.env", {"PATH": "/steal"}, allowed_fields=["PATH"]
        )


def test_npm_runtime_install_disables_scripts_and_reuses_verified_tree(tmp_path: Path):
    import asyncio

    from hermes_multitenancy.connector_stdio_runtime import prepare_npm_runtime

    integrity = "sha512-" + "B" * 88
    resolution = {
        "state": "resolved",
        "package": "demo-mcp",
        "version": "1.2.3",
        "integrity": integrity,
        "bin": {"demo-mcp": "dist/cli.js"},
        "resolution_fingerprint": "b" * 64,
    }
    lock_text = json.dumps({
        "lockfileVersion": 3,
        "packages": {
            "": {"dependencies": {"demo-mcp": "1.2.3"}},
            "node_modules/demo-mcp": {
                "version": "1.2.3",
                "resolved": "https://registry.npmjs.org/demo-mcp/-/demo-mcp-1.2.3.tgz",
                "integrity": integrity,
                "bin": {"demo-mcp": "dist/cli.js"},
            },
        },
    }, sort_keys=True, separators=(",", ":")) + "\n"
    resolution["dependency_lock"] = {
        "state": "resolved", "package_lock": lock_text,
        "package_lock_sha256": __import__("hashlib").sha256(lock_text.encode()).hexdigest(),
    }
    calls = []

    async def runner(command, cwd, env):
        calls.append((command, cwd, env))
        package = cwd / "node_modules" / "demo-mcp"
        (package / "dist").mkdir(parents=True)
        (package / "dist" / "cli.js").write_text("", encoding="utf-8")
        (package / "package.json").write_text(json.dumps({
            "name": "demo-mcp", "version": "1.2.3", "bin": {"demo-mcp": "dist/cli.js"},
        }), encoding="utf-8")
        assert (cwd / "package-lock.json").read_text(encoding="utf-8") == lock_text

    async def run():
        first = await prepare_npm_runtime(tmp_path / "runtimes", resolution, runner=runner)
        second = await prepare_npm_runtime(tmp_path / "runtimes", resolution, runner=runner)
        assert first == second
        return first

    installed = asyncio.run(run())
    assert len(calls) == 1
    command, _cwd, env = calls[0]
    assert command[0] == "/usr/bin/npm"
    assert "--ignore-scripts" in command
    assert command[1] == "ci"
    assert set(env) == {"HOME", "LANG", "PATH", "TZ"}
    assert (installed / "installed.json").is_file()


def test_python_runtime_installs_only_hashed_wheels_and_reuses_verified_tree(tmp_path: Path):
    import asyncio
    import hashlib
    import sys

    from hermes_multitenancy.connector_stdio_runtime import (
        build_linux_stdio_command,
        prepare_python_runtime,
        write_runtime_environment,
        write_runtime_resolver,
    )

    requirements = (
        "demo-mcp==1.2.3 \\\n+    --hash=sha256:" + "a" * 64 + "\n"
        "dependency==4.5.6 \\\n+    --hash=sha256:" + "b" * 64 + "\n"
    )
    resolution = {
        "state": "pypi_resolved",
        "package": "demo-mcp",
        "version": "1.2.3",
        "executable": "demo-mcp",
        "runtime_args": [],
        "resolution_fingerprint": "c" * 64,
        "dependency_lock": {
            "state": "resolved",
            "python_version": "3.11",
            "python_platform": "x86_64-unknown-linux-gnu",
            "requirements": requirements,
            "requirements_sha256": hashlib.sha256(requirements.encode()).hexdigest(),
        },
    }
    calls = []

    async def runner(command, cwd, env):
        calls.append(command)
        venv = cwd / "venv"
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "bin" / "python").write_text("", encoding="utf-8")
        if "pip" not in command:
            return
        (venv / "bin" / "demo-mcp").write_text("", encoding="utf-8")
        site = venv / "lib" / "python3.11" / "site-packages"
        for name, version in (("demo-mcp", "1.2.3"), ("dependency", "4.5.6")):
            metadata = site / f"{name.replace('-', '_')}-{version}.dist-info" / "METADATA"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text(f"Name: {name}\nVersion: {version}\n", encoding="utf-8")

    async def run():
        resolver = lambda _base, _runtime: Path(sys.executable)
        first = await prepare_python_runtime(
            tmp_path / "runtimes", resolution, runner=runner, python_resolver=resolver
        )
        second = await prepare_python_runtime(
            tmp_path / "runtimes", resolution, runner=runner, python_resolver=resolver
        )
        assert first == second
        return first

    installed = asyncio.run(run())
    assert len(calls) == 2
    assert calls[0][1:3] == ["-m", "venv"]
    assert "--require-hashes" in calls[1]
    assert "--only-binary=:all:" in calls[1]
    assert (installed / "installed.json").is_file()
    env_file = write_runtime_environment(tmp_path / "run" / "python.env", {}, allowed_fields=[])
    resolver = write_runtime_resolver(tmp_path / "run" / "resolv.conf")
    command = build_linux_stdio_command(
        runtime_root=installed,
        executable="venv/bin/demo-mcp",
        python_executable="venv/bin/python",
        python_base=tmp_path,
        python_version="3.11",
        runtime_args=[],
        sandbox_home=tmp_path / "python-home",
        env_file=env_file,
        resolver_file=resolver,
    )
    assert "/usr/bin/python3.11" in command
    assert "/runtime/venv/lib/python3.11/site-packages" in command
    assert "/runtime/venv/bin/demo-mcp" in command


def test_python_runtime_builds_hash_checked_source_dependencies_without_network(tmp_path: Path):
    import asyncio
    import hashlib
    import sys

    from hermes_multitenancy.connector_stdio_runtime import prepare_python_runtime

    requirements = (
        "demo-mcp==1.2.3 \\\n+    --hash=sha256:" + "a" * 64 + "\n"
        "source-only==4.5.6 \\\n+    --hash=sha256:" + "b" * 64 + "\n"
    )
    resolution = {
        "state": "pypi_resolved",
        "package": "demo-mcp",
        "version": "1.2.3",
        "executable": "demo-mcp",
        "runtime_args": [],
        "resolution_fingerprint": "d" * 64,
        "dependency_lock": {
            "state": "resolved",
            "source_build": True,
            "python_version": "3.11",
            "python_platform": "x86_64-unknown-linux-gnu",
            "requirements": requirements,
            "requirements_sha256": hashlib.sha256(requirements.encode()).hexdigest(),
        },
    }
    calls = []
    source_calls = []

    async def runner(command, cwd, _env):
        calls.append(command)
        venv = cwd / "venv"
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "bin" / "python").write_text("", encoding="utf-8")
        if "download" in command:
            downloads = cwd / "downloads"
            downloads.mkdir(exist_ok=True)
            (downloads / "demo_mcp-1.2.3-py3-none-any.whl").write_bytes(b"wheel")
            (downloads / "source_only-4.5.6.tar.gz").write_bytes(b"source")
        if "install" in command:
            (venv / "bin" / "demo-mcp").write_text("", encoding="utf-8")
            site = venv / "lib" / "python3.11" / "site-packages"
            for name, version in (("demo-mcp", "1.2.3"), ("source-only", "4.5.6")):
                metadata = site / f"{name.replace('-', '_')}-{version}.dist-info" / "METADATA"
                metadata.parent.mkdir(parents=True, exist_ok=True)
                metadata.write_text(f"Name: {name}\nVersion: {version}\n", encoding="utf-8")

    async def source_runner(command, cwd, _env):
        source_calls.append((command, cwd))
        built = cwd / "built"
        built.mkdir(exist_ok=True)
        (built / "source_only-4.5.6-py3-none-any.whl").write_bytes(b"built")

    installed = asyncio.run(prepare_python_runtime(
        tmp_path / "runtimes",
        resolution,
        runner=runner,
        source_runner=source_runner,
        python_resolver=lambda _base, _runtime: Path(sys.executable),
    ))
    assert installed.is_dir()
    assert any("download" in command and "--require-hashes" in command for command in calls)
    assert len(source_calls) == 1
    assert "--no-build-isolation" in source_calls[0][0]
    assert "--no-index" in source_calls[0][0]


def test_python_source_repair_requires_exact_input_and_output_hashes(tmp_path: Path):
    import hashlib

    from hermes_multitenancy.connector_stdio_runtime import _apply_source_patch

    target = tmp_path / "pyproject.toml"
    target.write_text("broken\n", encoding="utf-8")
    fixed = "fixed\n"
    patch = {
        "path": "pyproject.toml",
        "before_sha256": hashlib.sha256(b"broken\n").hexdigest(),
        "content": fixed,
        "content_sha256": hashlib.sha256(fixed.encode()).hexdigest(),
    }
    _apply_source_patch(tmp_path, patch)
    assert target.read_text(encoding="utf-8") == fixed
    with pytest.raises(ValueError, match="input drift"):
        _apply_source_patch(tmp_path, patch)


def test_node_git_runtime_requires_pinned_source_lock_and_built_entry(tmp_path: Path):
    import hashlib

    from hermes_multitenancy.connector_stdio_runtime import (
        catalog_stdio_spec,
        verify_node_git_runtime_tree,
    )

    package = b'{"name":"demo-git-mcp","version":"1.0.0"}\n'
    lock = '{"lockfileVersion":3,"packages":{"":{}}}\n'
    resolution = {
        "state": "git_resolved",
        "package": "demo-git-mcp",
        "version": "1.0.0",
        "entry": "dist/index.js",
        "runtime_args": [],
        "resolution_fingerprint": "e" * 64,
        "source_archive_sha256": "f" * 64,
        "package_json_sha256": hashlib.sha256(package).hexdigest(),
        "dependency_lock": {
            "state": "resolved",
            "package_lock": lock,
            "package_lock_sha256": hashlib.sha256(lock.encode()).hexdigest(),
            "source_package_lock_sha256": "a" * 64,
        },
    }
    manifest = {
        "state": "direct", "command": "npx", "configs": [], "static_env": {},
        "package_resolution": resolution,
    }
    assert catalog_stdio_spec(manifest)["runtime_kind"] == "node_git"
    project = tmp_path / "project"
    (project / "dist").mkdir(parents=True)
    (project / "package.json").write_bytes(package)
    (project / "package-lock.json").write_text(lock, encoding="utf-8")
    (project / "dist" / "index.js").write_text("", encoding="utf-8")
    (tmp_path / "installed.json").write_text(
        json.dumps({
            "resolution_fingerprint": "e" * 64,
            "source_archive_sha256": "f" * 64,
        }),
        encoding="utf-8",
    )
    assert verify_node_git_runtime_tree(tmp_path, resolution)["executable"] == "project/dist/index.js"


def test_owner_secret_files_are_materialized_only_inside_the_connector_home(tmp_path: Path):
    from hermes_multitenancy.connector_stdio_runtime import materialize_runtime_values

    values = materialize_runtime_values(
        tmp_path,
        {"PROJECT": "demo", "SERVICE_ACCOUNT_JSON": '{"type":"service_account"}'},
        {"SERVICE_ACCOUNT_JSON": "credentials/google.json"},
    )
    assert values == {"PROJECT": "demo"}
    secret = tmp_path / "credentials" / "google.json"
    assert secret.read_text(encoding="utf-8") == '{"type":"service_account"}'
    assert secret.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        materialize_runtime_values(tmp_path, {"SECRET": "x"}, {"SECRET": "../escape"})
