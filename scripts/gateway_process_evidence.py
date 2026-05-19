#!/usr/bin/env python3
"""Capture process-level evidence for exact-branch gateway UAT.

The completion audit already checks the live plugin symlink and the group-write
evidence mtime. This script adds the missing runtime fact: the gateway process
must have started after the symlink pointed at the expected worktree, and the
group-write evidence must be newer than that process start.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any


PS_LINE = re.compile(
    r"^\s*(?P<pid>\d+)\s+"
    r"(?P<lstart>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d\d:\d\d:\d\d\s+\d{4})\s+"
    r"(?P<command>.*)$"
)


def _resolve_link_target(path: Path) -> str:
    if path.is_symlink():
        raw_target = os.readlink(path)
        target = Path(raw_target)
        if not target.is_absolute():
            target = path.parent / target
        return str(target.resolve(strict=False))
    if path.exists():
        return str(path.resolve(strict=False))
    return ""


def _mtime(path: Path, *, follow_symlinks: bool = True) -> int:
    try:
        stat_result = path.stat() if follow_symlinks else path.lstat()
    except OSError:
        return 0
    return int(stat_result.st_mtime)


def _process_start_epoch(lstart: str) -> int:
    parsed = datetime.strptime(lstart, "%a %b %d %H:%M:%S %Y")
    return int(time.mktime(parsed.timetuple()))


def _iter_processes() -> list[dict[str, Any]]:
    output = subprocess.check_output(["ps", "-axo", "pid=,lstart=,command="], text=True)
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = PS_LINE.match(line)
        if not match:
            continue
        rows.append({
            "pid": int(match.group("pid")),
            "lstart": match.group("lstart"),
            "process_start_epoch": _process_start_epoch(match.group("lstart")),
            "command": match.group("command"),
        })
    return rows


def _find_gateway_process(profile: str, pid: int | None) -> dict[str, Any] | None:
    candidates = _iter_processes()
    if pid is not None:
        return next((row for row in candidates if row["pid"] == pid), None)
    matches = [
        row
        for row in candidates
        if profile in row["command"]
        and "gateway" in row["command"]
        and "hermes_cli.main" in row["command"]
        and "gateway_process_evidence.py" not in row["command"]
    ]
    if not matches:
        return None
    return max(matches, key=lambda row: row["process_start_epoch"])


def build_evidence(
    *,
    profile: str,
    worktree: Path,
    gateway_plugin_link: Path,
    group_write_evidence: Path,
    pid: int | None = None,
) -> dict[str, Any]:
    process = _find_gateway_process(profile, pid)
    live_plugin_target = _resolve_link_target(gateway_plugin_link)
    expected_worktree = str(worktree.resolve(strict=False))
    plugin_link_mtime = _mtime(gateway_plugin_link, follow_symlinks=False)
    group_write_mtime = _mtime(group_write_evidence)
    if process is None:
        return {
            "ok": False,
            "reason": "gateway process not found",
            "profile": profile,
            "expected_worktree": expected_worktree,
            "live_plugin_target": live_plugin_target,
            "plugin_link_mtime": plugin_link_mtime,
            "group_write_mtime": group_write_mtime,
        }
    process_start = int(process["process_start_epoch"])
    return {
        "ok": (
            live_plugin_target == expected_worktree
            and process_start >= plugin_link_mtime
            and group_write_mtime >= process_start
        ),
        "profile": profile,
        "pid": process["pid"],
        "process_lstart": process["lstart"],
        "process_start_epoch": process_start,
        "command": process["command"],
        "expected_worktree": expected_worktree,
        "live_plugin_target": live_plugin_target,
        "plugin_link_mtime": plugin_link_mtime,
        "group_write_mtime": group_write_mtime,
        "process_after_link": process_start >= plugin_link_mtime,
        "group_write_after_process": group_write_mtime >= process_start,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway-process-evidence")
    parser.add_argument("--profile", default="multitenancy_router")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--worktree", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--gateway-plugin-link",
        type=Path,
        default=Path.home() / ".hermes" / "profiles" / "multitenancy_router" / "plugins" / "multitenancy",
    )
    parser.add_argument(
        "--group-write-evidence",
        type=Path,
        default=Path("/tmp/hermes-skills-uat/lark-group-write-rerun-current-gateway.jsonl"),
    )
    parser.add_argument("--output", type=Path, default=Path("/tmp/hermes-skills-uat/gateway-process-evidence.json"))
    args = parser.parse_args(argv)

    evidence = build_evidence(
        profile=args.profile,
        worktree=args.worktree,
        gateway_plugin_link=args.gateway_plugin_link,
        group_write_evidence=args.group_write_evidence,
        pid=args.pid,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if evidence.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
