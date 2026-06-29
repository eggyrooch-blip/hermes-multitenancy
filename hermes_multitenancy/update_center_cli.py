"""CLI for Multitenancy Update Center MVP."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .update_center import (
    KepCliSystem,
    UpdateLedger,
    apply_additive_plugin_update,
    check_lark_cli_candidate,
    default_shared_home,
    scan_lark_cli_notice,
    sync_kep_cli_systems,
)


def _ledger(args: argparse.Namespace) -> UpdateLedger:
    return UpdateLedger(args.ledger, shared_home=args.shared_home)


def _command_scan_lark_notice(args: argparse.Namespace) -> int:
    text = args.text
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    report = scan_lark_cli_notice(text, ledger=_ledger(args), current_version=args.current_version)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _command_lark_preflight(args: argparse.Namespace) -> int:
    report = check_lark_cli_candidate(
        shared_home=args.shared_home,
        profile_name=args.profile,
        open_id=args.open_id,
        target_version=args.target_version,
        ledger=_ledger(args),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["preflight"].get("ready") else 2


def _load_systems(path: Path) -> list[KepCliSystem]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("systems") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("systems file must be a list or {'systems': [...]}")
    allowed = {"system", "binary", "target_version", "installed_version"}
    out: list[KepCliSystem] = []
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("each system entry must be an object")
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise ValueError(f"unknown key(s) in system entry: {unknown}")
        out.append(KepCliSystem(**dict(item)))
    return out


def _profile_targets(args: argparse.Namespace) -> list[str]:
    if args.no_profile_sync:
        return []
    explicit: list[str] = []
    for item in args.profile or []:
        explicit.extend(part.strip() for part in str(item).split(",") if part.strip())
    if explicit:
        return sorted(dict.fromkeys(explicit))
    profiles_root = args.shared_home / "profiles"
    if not profiles_root.is_dir():
        return []
    return sorted(path.name for path in profiles_root.iterdir() if path.is_dir())


def _command_kep_sync(args: argparse.Namespace) -> int:
    systems = _load_systems(args.systems_file)
    report = sync_kep_cli_systems(
        systems=systems,
        shared_home=args.shared_home,
        ledger=_ledger(args),
        profiles=_profile_targets(args),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    quarantined = [item for item in report["systems"] if item.get("action") == "quarantined"]
    return 2 if quarantined else 0


def _command_apply_plugin(args: argparse.Namespace) -> int:
    report = apply_additive_plugin_update(
        args.repo,
        audience=args.audience,
        shared_home=args.shared_home,
        ledger=_ledger(args),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("applied") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-multitenancy-update-center")
    parser.add_argument("--shared-home", type=Path, default=default_shared_home())
    parser.add_argument("--ledger", type=Path, default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan-lark-notice", help="Record a lark-cli update notice as an internal candidate")
    p_scan.add_argument("--text", default="")
    p_scan.add_argument("--file", type=Path)
    p_scan.add_argument("--current-version", default="")
    p_scan.set_defaults(func=_command_scan_lark_notice)

    p_lark = sub.add_parser("lark-preflight", help="Run lark-cli candidate preflight without replacing binaries")
    p_lark.add_argument("--profile", required=True)
    p_lark.add_argument("--open-id", required=True)
    p_lark.add_argument("--target-version", required=True)
    p_lark.set_defaults(func=_command_lark_preflight)

    p_kep = sub.add_parser("kep-sync", help="Install/update kep-cli systems and sync shared sandbox bin")
    p_kep.add_argument("--systems-file", type=Path, required=True)
    p_kep.add_argument("--profile", action="append", default=[], help="Profile to sync skills for; defaults to all profiles")
    p_kep.add_argument("--no-profile-sync", action="store_true", help="Only sync shared binaries and shared skill sources")
    p_kep.set_defaults(func=_command_kep_sync)

    p_plugin = sub.add_parser("apply-plugin", help="Auto-apply a skill-only additive plugin update")
    p_plugin.add_argument("repo", type=Path)
    p_plugin.add_argument("--audience", required=True)
    p_plugin.set_defaults(func=_command_apply_plugin)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
