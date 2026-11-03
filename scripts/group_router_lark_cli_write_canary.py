#!/usr/bin/env python3
"""Current-branch group router lark-cli write canary.

This does not switch the running gateway plugin symlink. It imports this
worktree's router, builds a real group Feishu event from the local routing DB,
lets ``handle_async()`` resolve the group route, and then performs a real
``lark-cli docs +create --as bot`` from the routed group profile.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_multitenancy.agent_real import _lark_cli_auth_broker_scope  # noqa: E402


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


def _latest_group_route(real_home: Path) -> dict[str, str]:
    db_path = real_home / "multitenancy.db"
    if not db_path.exists():
        raise SystemExit(f"missing routing db: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        group = conn.execute(
            """
            SELECT profile_name, chat_id, owner_open_id, upstream_profile, display_label
            FROM multitenancy_routing
            WHERE active = 1 AND kind = 'group'
              AND chat_id IS NOT NULL
              AND owner_open_id IS NOT NULL
              AND upstream_profile IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
        if group is None:
            raise SystemExit("no active group route with upstream_profile")
        owner = conn.execute(
            """
            SELECT user_id, profile_name, open_id
            FROM multitenancy_routing
            WHERE active = 1 AND kind = 'user' AND open_id = ?
            LIMIT 1
            """,
            (group["owner_open_id"],),
        ).fetchone()
        if owner is None:
            raise SystemExit("group owner route missing")
    finally:
        conn.close()
    return {
        "group_profile": str(group["profile_name"]),
        "chat_id": str(group["chat_id"]),
        "owner_open_id": str(group["owner_open_id"]),
        "upstream_profile": str(group["upstream_profile"]),
        "display_label": str(group["display_label"] or group["chat_id"]),
        "owner_user_id": str(owner["user_id"]),
        "owner_profile": str(owner["profile_name"]),
    }


def _run_lark_docs_create(profile_home: Path, *, content: str, execute_write: bool) -> dict[str, Any]:
    with _lark_cli_auth_broker_scope(profile_home, "") as broker_env:
        if not broker_env:
            raise RuntimeError("lark-cli auth broker env is empty")
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
        if not execute_write:
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
    return {
        "returncode": proc.returncode,
        "document_id": _extract_doc_id(stdout) if execute_write else "",
        "identity_bot": '"identity": "bot"' in stdout or '"identity":"bot"' in stdout,
        "stdout_excerpt": stdout[:1200],
        "stderr_excerpt": stderr[:800],
    }


async def _run_router_canary(real_home: Path, *, execute_write: bool) -> dict[str, Any]:
    from hermes_multitenancy import router as router_mod
    from hermes_multitenancy.router import handle_async
    from hermes_multitenancy.routing import RoutingTable

    route = _latest_group_route(real_home)
    mark = f"ROUTER_GROUP_DOC_{int(time.time())}"
    content = (
        f"<title>HERMES_ROUTER_GROUP_DOC_{mark}</title>"
        f"<p>current branch router group bot canary {mark}</p>"
    )
    stream_seen: dict[str, Any] = {}

    async def fake_stream(adapter, chat_id, profile_name, profile_home, event, *, messages):
        stream_seen.update(
            {
                "chat_id": chat_id,
                "profile_name": profile_name,
                "profile_home": str(profile_home),
                "event_text": getattr(event, "text", ""),
                "messages": list(messages),
            }
        )
        lark_result = _run_lark_docs_create(Path(profile_home), content=content, execute_write=execute_write)
        stream_seen["lark_result"] = lark_result
        if lark_result["returncode"] != 0:
            return f"router group lark-cli failed: {lark_result['stderr_excerpt'] or lark_result['stdout_excerpt']}"
        return f"router group lark-cli ok document_id={lark_result['document_id']}"

    class Adapter:
        def __init__(self) -> None:
            self.completions: list[str] = []

        async def send(self, _chat, _msg, *, reply_to=None, metadata=None): pass
        async def edit_message(self, *args, **kwargs): pass
        async def on_processing_start(self, _event): pass
        async def on_processing_complete(self, _event, outcome):
            self.completions.append(str(outcome))

    original_stream = router_mod._stream_into_feishu
    router_mod._stream_into_feishu = fake_stream
    router_mod._session_history.clear()
    router_mod._session_loaded.clear()
    router_mod._user_inflight_tasks.clear()
    router_mod._user_inflight_history_keys.clear()
    router_mod._suppress_interruption_marker_tasks.clear()
    router_mod.override_session_store(":memory:")
    started = time.time()
    adapter = Adapter()
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-router-group-canary-") as tmp:
            tmp_db = Path(tmp) / "multitenancy.db"
            table = RoutingTable(tmp_db)
            try:
                table.upsert(
                    user_id=route["owner_user_id"],
                    profile_name=route["owner_profile"],
                    open_id=route["owner_open_id"],
                    provenance="sync",
                )
                table.upsert_group(
                    chat_id=route["chat_id"],
                    profile_name=route["group_profile"],
                    owner_open_id=route["owner_open_id"],
                    display_label=route["display_label"],
                    upstream_profile=route["upstream_profile"],
                )
            finally:
                table.close()
            router_mod.override_routing_table(tmp_db)

            event = SimpleNamespace(
                text=f"ROUTER_GROUP_CANARY {mark} create a Feishu doc through lark-cli bot",
                message_id=f"router_group_canary_{mark}",
                source=SimpleNamespace(
                    chat_id=route["chat_id"],
                    user_id=route["owner_open_id"],
                    user_id_alt=None,
                    user_name="group-owner",
                    chat_type="group",
                    platform=SimpleNamespace(value="feishu"),
                    message_id=f"router_group_canary_{mark}",
                    thread_id=None,
                ),
            )
            await handle_async(event=event, gateway=SimpleNamespace(adapters={"feishu": adapter}))
    finally:
        router_mod._stream_into_feishu = original_stream
        router_mod.override_routing_table(None)
        router_mod.override_session_store(None)

    lark_result = stream_seen.get("lark_result") or {}
    messages = stream_seen.get("messages") or []
    ok = (
        stream_seen.get("profile_name") == route["group_profile"]
        and stream_seen.get("chat_id") == route["chat_id"]
        and bool(messages)
        and lark_result.get("returncode") == 0
        and (not execute_write or bool(lark_result.get("document_id")))
        and (not execute_write or lark_result.get("identity_bot") is True)
    )
    return {
        "scenario": "current_branch_group_router_lark_cli_docs_create",
        "ok": ok,
        "execute_write": execute_write,
        "mark": mark,
        "route": route,
        "elapsed_ms": round((time.time() - started) * 1000),
        "router_profile": stream_seen.get("profile_name"),
        "router_chat_id": stream_seen.get("chat_id"),
        "message_roles": [item.get("role") for item in messages],
        "message_contents": [item.get("content") for item in messages],
        "completion_count": len(adapter.completions),
        "document_id": lark_result.get("document_id", ""),
        "identity_bot": bool(lark_result.get("identity_bot")),
        "returncode": lark_result.get("returncode"),
        "stdout_excerpt": lark_result.get("stdout_excerpt", ""),
        "stderr_excerpt": lark_result.get("stderr_excerpt", ""),
        "secret_free": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="group-router-lark-cli-write-canary")
    parser.add_argument("--real-home", type=Path, default=Path("~/.hermes"))
    parser.add_argument("--output", type=Path, default=Path("/tmp/hermes-skills-uat/router-group-lark-cli-write.json"))
    parser.add_argument("--execute-write", action="store_true", help="Actually create a Feishu doc; without this the command is dry-run only")
    args = parser.parse_args(argv)

    real_home = args.real_home.expanduser()
    _load_dotenv(real_home / ".env")
    report = asyncio.run(_run_router_canary(real_home, execute_write=bool(args.execute_write)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
