from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .ingest_keys import (
    IngestKeyError,
    add_binding,
    generate_token,
    load_key_file,
    mask_token,
    public_entry,
    revoke_binding,
    rotate_binding,
    save_key_file,
)


DEFAULT_KEYS_FILE = Path(os.getenv("HERMES_INGEST_KEYS_FILE", "~/.hermes/ingest-keys.json")).expanduser()


def _add_keys_file_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--keys-file", type=Path, default=DEFAULT_KEYS_FILE, help="HERMES_INGEST_KEYS_FILE path")


def _add_selector_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--index", type=int, default=None, help="binding index from list output")
    parser.add_argument("--owner", default="", help="exact owner open_id selector")
    parser.add_argument("--profile", default="", help="exact bound profile selector")
    parser.add_argument("--agent", default="", help="exact bound agent id selector")
    parser.add_argument("--name", default="", help="exact display name selector")


def _selector_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "index": args.index,
        "owner": args.owner,
        "profile": args.profile,
        "agent": args.agent,
        "name": args.name,
    }


def _write_grant_result(entry: dict[str, str], *, show_token: bool) -> None:
    shown = entry["token"] if show_token else mask_token(entry["token"])
    print(
        "granted "
        f"token={shown} "
        f"owner={entry.get('owner', '')} "
        f"profile={entry.get('profile', '')} "
        f"agent={entry.get('agent', '')} "
        f"name={entry.get('name', '')}"
    )


def _command_grant(args: argparse.Namespace) -> int:
    keys = load_key_file(args.keys_file)
    entry = add_binding(
        keys,
        token=args.token or generate_token(),
        owner=args.owner,
        profile=args.profile,
        agent=args.agent,
        name=args.name,
    )
    save_key_file(args.keys_file, keys)
    _write_grant_result(entry, show_token=args.show_token)
    return 0


def _command_list(args: argparse.Namespace) -> int:
    keys = load_key_file(args.keys_file)
    rows = [public_entry(entry, index=idx) for idx, entry in enumerate(keys)]
    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        print(
            f"[{row['index']}] token={row['token']} owner={row['owner']} "
            f"profile={row['profile']} agent={row['agent']} name={row['name']} source={row['source']}"
        )
    return 0


def _command_rotate(args: argparse.Namespace) -> int:
    keys = load_key_file(args.keys_file)
    entry = rotate_binding(keys, token=args.token or generate_token(), **_selector_kwargs(args))
    save_key_file(args.keys_file, keys)
    shown = entry["token"] if args.show_token else mask_token(entry["token"])
    print(
        "rotated "
        f"token={shown} "
        f"owner={entry.get('owner', '')} "
        f"profile={entry.get('profile', '')} "
        f"agent={entry.get('agent', '')}"
    )
    return 0


def _command_revoke(args: argparse.Namespace) -> int:
    keys = load_key_file(args.keys_file)
    entry = revoke_binding(keys, **_selector_kwargs(args))
    save_key_file(args.keys_file, keys)
    print(
        "revoked "
        f"token={mask_token(entry.get('token', ''))} "
        f"owner={entry.get('owner', '')} "
        f"profile={entry.get('profile', '')} "
        f"agent={entry.get('agent', '')}"
    )
    return 0


def _redact_for_token(text: str, token: str) -> str:
    if token:
        text = text.replace(token, mask_token(token))
    return text


def _smoke_body_excerpt(body: str, token: str) -> str:
    return _redact_for_token(body, token)[:500]


def _command_smoke(args: argparse.Namespace) -> int:
    url = args.base_url.rstrip("/") + "/api/run-broker/ingest/agents"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {args.token}"})
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            status = int(status)
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(
            f"smoke failed status={exc.code} token={mask_token(args.token)} body={_smoke_body_excerpt(body, args.token)}",
            file=sys.stderr,
        )
        return 1
    except urllib.error.URLError as exc:
        print(f"smoke failed token={mask_token(args.token)} reason={exc.reason}", file=sys.stderr)
        return 1
    if status < 200 or status >= 300:
        print(
            f"smoke failed status={status} token={mask_token(args.token)} body={_smoke_body_excerpt(body, args.token)}",
            file=sys.stderr,
        )
        return 1
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        print(f"smoke failed status={status} token={mask_token(args.token)} invalid_json", file=sys.stderr)
        return 1
    if payload.get("ok") is False:
        print(
            f"smoke failed status={status} token={mask_token(args.token)} body={_smoke_body_excerpt(body, args.token)}",
            file=sys.stderr,
        )
        return 1
    agents = payload.get("agents") if isinstance(payload.get("agents"), list) else []
    print(f"smoke ok status={status} token={mask_token(args.token)} owner={payload.get('owner', '')} agents={len(agents)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-multitenancy-ingest")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_grant = sub.add_parser("grant", help="Create an ingest key binding")
    _add_keys_file_arg(p_grant)
    p_grant.add_argument("--owner", default="", help="owner open_id for discovery/audit")
    p_grant.add_argument("--profile", default="", help="fixed profile to run")
    p_grant.add_argument("--agent", default="", help="fixed external agent id")
    p_grant.add_argument("--name", default="", help="display name for list/discovery")
    p_grant.add_argument("--token", default="", help="explicit token; generated when omitted")
    p_grant.add_argument("--show-token", action="store_true", help="print the full new token once")
    p_grant.set_defaults(func=_command_grant)

    p_list = sub.add_parser("list", help="List ingest key bindings with masked tokens")
    _add_keys_file_arg(p_list)
    p_list.add_argument("--format", choices=["table", "json"], default="table")
    p_list.set_defaults(func=_command_list)

    p_rotate = sub.add_parser("rotate", help="Replace one binding's token")
    _add_keys_file_arg(p_rotate)
    _add_selector_args(p_rotate)
    p_rotate.add_argument("--token", default="", help="explicit replacement token; generated when omitted")
    p_rotate.add_argument("--show-token", action="store_true", help="print the full replacement token once")
    p_rotate.set_defaults(func=_command_rotate)

    p_revoke = sub.add_parser("revoke", help="Remove one ingest key binding")
    _add_keys_file_arg(p_revoke)
    _add_selector_args(p_revoke)
    p_revoke.set_defaults(func=_command_revoke)

    p_smoke = sub.add_parser("smoke", help="Call /api/run-broker/ingest/agents with a bearer token")
    p_smoke.add_argument("--base-url", required=True, help="RunBroker public base URL")
    p_smoke.add_argument("--token", required=True, help="Bearer token to smoke-test")
    p_smoke.add_argument("--timeout", type=float, default=15.0)
    p_smoke.set_defaults(func=_command_smoke)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (IngestKeyError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
