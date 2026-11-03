"""Sanitize a transient unit environment before entering the stdio bwrap."""
from __future__ import annotations

import argparse
import os

from .connector_stdio_runtime import _safe_env_name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or command[0] != "/usr/bin/bwrap":
        raise PermissionError("connector sandbox command is unavailable")
    allowed = {
        _safe_env_name(name)
        for name in str(os.environ.get("HERMES_CONNECTOR_ALLOWED_ENV") or "").split(",")
        if name
    }
    environment = {
        "HOME": "/home/connector",
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "TZ": "UTC",
        **{name: os.environ[name] for name in allowed if name in os.environ},
    }
    os.unlink(args.env_file)
    os.execve(command[0], command, environment)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
