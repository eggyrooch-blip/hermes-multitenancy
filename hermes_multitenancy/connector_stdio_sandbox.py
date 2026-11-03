"""Run a hash-verified Python MCP wheel in a secretless macOS sandbox."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Awaitable, Callable


_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_WORKER = """import os, resource, sys
def cap(kind, value):
    soft, hard = resource.getrlimit(kind)
    target = min(value, hard) if hard >= 0 else value
    resource.setrlimit(kind, (min(soft, target) if soft >= 0 else target, hard))
cap(resource.RLIMIT_CPU, 10)
cap(resource.RLIMIT_NOFILE, 128)
cap(resource.RLIMIT_NPROC, 32)
cap(resource.RLIMIT_FSIZE, 16 * 1024 * 1024)
os.chdir(os.environ["HOME"])
os.execve(sys.argv[1], sys.argv[1:], os.environ)
"""


def _scheme(value: Path | str) -> str:
    return json.dumps(str(value))


def _verify_wheel(path: Path, expected_sha256: str) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected_sha256.casefold():
        raise ValueError(f"wheel sha256 mismatch: {path.name}")
    with zipfile.ZipFile(path) as archive:
        total = 0
        for item in archive.infolist():
            parts = Path(item.filename).parts
            if item.filename.startswith(("/", "\\")) or ".." in parts:
                raise ValueError(f"unsafe wheel member: {path.name}")
            total += item.file_size
        if len(archive.infolist()) > 5000 or total > 128 * 1024 * 1024:
            raise ValueError(f"wheel exceeds sandbox admission limits: {path.name}")
    return digest


async def _mcp_runner(command: list[str], env: dict[str, str]) -> list[str]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=command[0], args=command[1:], env=env)

    async def handshake() -> list[str]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return [tool.name for tool in (await session.list_tools()).tools]

    return await asyncio.wait_for(handshake(), timeout=20)


async def run_sandboxed_stdio_handshake(
    *,
    python: Path,
    module: str,
    wheels: list[tuple[Path, str]],
    sandbox_home: Path,
    runner: Callable[[list[str], dict[str, str]], Awaitable[list[str]]] = _mcp_runner,
) -> dict[str, object]:
    if not python.is_absolute() or not python.exists() or not _MODULE.fullmatch(module):
        raise ValueError("an absolute Python executable and safe module name are required")
    if sys.platform != "darwin" or not os.access("/usr/bin/sandbox-exec", os.X_OK):
        raise RuntimeError("macOS sandbox-exec is required for this local probe")
    verified = [(path.resolve(), _verify_wheel(path.resolve(), digest)) for path, digest in wheels]
    sandbox_home.mkdir(parents=True, exist_ok=True)
    temp = sandbox_home / "tmp"
    temp.mkdir(exist_ok=True)
    worker = sandbox_home / "resource_limited_exec.py"
    worker.write_text(_WORKER, encoding="utf-8")

    python_path = python.absolute()
    venv = python.parent.parent.resolve()
    runtime = python.resolve().parent.parent
    read_paths = [
        Path("/System"),
        Path("/usr"),
        Path("/Library"),
        Path("/private/etc"),
        Path("/private/var/db"),
        venv,
        runtime,
        *[path for path, _digest in verified],
    ]
    policy = sandbox_home / "stdio.sb"
    policy.write_text(
        "(version 1)\n"
        "(deny default)\n"
        '(import "system.sb")\n'
        "(allow process-fork)\n"
        f"(allow process-exec* (literal {_scheme(python_path)}) (literal {_scheme(python.resolve())}))\n"
        "(allow file-read*\n"
        + "".join(f"  (subpath {_scheme(path)})\n" for path in read_paths)
        + f"  (subpath {_scheme(sandbox_home.resolve())})\n"
        + "  (literal \"/dev/null\") (literal \"/dev/urandom\"))\n"
        + f"(allow file-write* (subpath {_scheme(sandbox_home.resolve())}))\n"
        + "(deny network*)\n",
        encoding="utf-8",
    )
    env = {
        "HOME": str(sandbox_home.resolve()),
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": os.pathsep.join(str(path) for path, _digest in verified),
        "TMPDIR": str(temp.resolve()),
        "TZ": "UTC",
    }
    command = [
        "/usr/bin/sandbox-exec",
        "-f",
        str(policy),
        str(python_path),
        str(worker),
        str(python_path),
        "-m",
        module,
    ]
    tools = await runner(command, env)
    return {
        "verdict": "pass",
        "complete": True,
        "reason_code": "sandbox_tools_list_ok",
        "tool_count": len(tools),
        "tool_names": tools,
        "evidence": {
            "sandbox": "macos_sandbox_exec",
            "network": "denied",
            "ambient_environment": "not_forwarded",
            "resource_limits": ["cpu", "nofile", "nproc", "fsize"],
            "wheels": [{"filename": path.name, "sha256": digest} for path, digest in verified],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("--wheel", action="append", required=True, metavar="PATH=SHA256")
    parser.add_argument("--sandbox-home", type=Path, required=True)
    parser.add_argument("--row-key")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    wheels = []
    for value in args.wheel:
        path, separator, digest = value.rpartition("=")
        if not separator:
            parser.error("--wheel must be PATH=SHA256")
        wheels.append((Path(path), digest))
    result = asyncio.run(
        run_sandboxed_stdio_handshake(
            python=args.python,
            module=args.module,
            wheels=wheels,
            sandbox_home=args.sandbox_home,
        )
    )
    if args.row_key:
        result["row_key"] = args.row_key
    args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
