"""Relay stdio MCP JSON-RPC while dropping legacy stdout log lines."""
from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import threading


_MAX_LINE = 8 * 1024 * 1024


def _jsonrpc_line(line: bytes) -> bool:
    if len(line) > _MAX_LINE:
        return False
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(value, dict) and value.get("jsonrpc") == "2.0"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or command[0] != "/usr/bin/systemd-run":
        raise PermissionError("connector systemd command is unavailable")
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    def stop(_signal, _frame):
        process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def input_pump():
        assert process.stdin is not None
        try:
            while chunk := sys.stdin.buffer.read(64 * 1024):
                process.stdin.write(chunk)
                process.stdin.flush()
            process.stdin.close()
        except BrokenPipeError:
            pass

    threading.Thread(target=input_pump, daemon=True).start()
    assert process.stdout is not None
    while line := process.stdout.readline(_MAX_LINE + 1):
        if _jsonrpc_line(line):
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
