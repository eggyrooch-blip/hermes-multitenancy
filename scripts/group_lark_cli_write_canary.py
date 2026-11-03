#!/usr/bin/env python3
"""Current-branch group profile lark-cli write canary.

This intentionally bypasses the running gateway process. It imports the
current worktree's auth broker, starts a short-lived broker for one group
profile, and runs lark-cli with bot identity. Use it to prove the branch's
group-profile lark-cli write path without switching the live gateway symlink.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_multitenancy.agent_real import _lark_cli_auth_broker_scope  # noqa: E402


def _latest_group_profile(real_home: Path) -> str:
    db_path = real_home / "multitenancy.db"
    if not db_path.exists():
        raise SystemExit(f"missing routing db: {db_path}")
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            """
            SELECT profile_name
            FROM multitenancy_routing
            WHERE active = 1 AND kind = 'group' AND upstream_profile IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise SystemExit("no active group route with upstream_profile")
    return str(row[0])


def _load_dotenv(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'\"")


def _extract_doc_id(stdout: str) -> str:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        document = (((payload.get("data") or {}).get("document")) or {})
        doc_id = str(document.get("document_id") or "").strip()
        if doc_id:
            return doc_id
    candidates = re.findall(r"\b[A-Za-z0-9]{20,}\b", stdout)
    return candidates[0] if candidates else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="group-lark-cli-write-canary")
    parser.add_argument("--real-home", type=Path, default=Path("~/.hermes"))
    parser.add_argument("--profile", default="")
    parser.add_argument("--output", type=Path, default=Path("/tmp/hermes-skills-uat/direct-group-lark-cli-write.json"))
    parser.add_argument("--execute-write", action="store_true", help="Actually create a Feishu doc; without this the command is dry-run only")
    args = parser.parse_args(argv)

    real_home = args.real_home.expanduser()
    _load_dotenv(real_home / ".env")
    profile_name = args.profile.strip() or _latest_group_profile(real_home)
    profile_home = real_home / "profiles" / profile_name
    if not profile_home.exists():
        raise SystemExit(f"group profile does not exist: {profile_home}")

    mark = f"DIRECT_GROUP_DOC_{int(time.time())}"
    content = (
        f"<title>HERMES_DIRECT_GROUP_DOC_{mark}</title>"
        f"<p>current branch direct group bot canary {mark}</p>"
    )
    started = time.time()
    with _lark_cli_auth_broker_scope(profile_home, "") as broker_env:
        if not broker_env:
            raise SystemExit("lark-cli auth broker env is empty")
        env = os.environ.copy()
        env.update(broker_env)
        cmd = [
            broker_env["HERMES_LARK_CLI_BIN"],
            "docs",
            "+create",
            "--api-version",
            "v2",
            "--content",
            content,
            "--as",
            "bot",
        ]
        if not args.execute_write:
            cmd.append("--dry-run")
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=60,
            env=env,
            check=False,
        )

    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    doc_id = _extract_doc_id(stdout) if args.execute_write else ""
    identity_bot = '"identity": "bot"' in stdout or '"identity":"bot"' in stdout
    ok = proc.returncode == 0 and (not args.execute_write or bool(doc_id)) and (not args.execute_write or identity_bot)
    report: dict[str, Any] = {
        "scenario": "direct_group_profile_lark_cli_docs_create",
        "profile": profile_name,
        "mark": mark,
        "execute_write": bool(args.execute_write),
        "ok": ok,
        "returncode": proc.returncode,
        "elapsed_ms": round((time.time() - started) * 1000),
        "document_id": doc_id,
        "identity_bot": identity_bot,
        "stdout_excerpt": stdout[:1200],
        "stderr_excerpt": stderr[:800],
        "secret_free": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
