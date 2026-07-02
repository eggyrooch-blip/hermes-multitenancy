from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .report import (
    DEFAULT_AUDIT_PATH,
    DEFAULT_ROUTING_DB,
    build_skillhub_audit,
    build_summary,
    dumps_json,
    render_markdown,
    render_skillhub_markdown,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-multitenancy-analytics")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_summary = sub.add_parser("summary", help="Summarize conversation audit usage and completion proxy metrics")
    p_summary.add_argument("--audit", type=Path, default=DEFAULT_AUDIT_PATH, help="conversation-audit.jsonl path")
    p_summary.add_argument("--routing-db", type=Path, default=DEFAULT_ROUTING_DB, help="multitenancy.db path")
    p_summary.add_argument("--no-routing-db", action="store_true", help="Do not read routing DB")
    p_summary.add_argument("--days", type=int, default=7, help="Selected report window in days")
    p_summary.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p_summary.add_argument("--include-profiles", action="store_true", help="Include top active profile names")
    p_summary.add_argument("--include-samples", action="store_true", help="Include short redacted demand samples")
    p_summary.add_argument("--sample-limit", type=int, default=10)
    p_summary.add_argument("--output", type=Path, default=None, help="Write report to a file instead of stdout")

    p_skillhub = sub.add_parser("skillhub", help="Audit SkillHub events: received/installed/failed/queued + failure reasons")
    p_skillhub.add_argument("--routing-db", type=Path, default=DEFAULT_ROUTING_DB, help="multitenancy.db path")
    p_skillhub.add_argument("--days", type=int, default=7, help="Window for the last-N-days tally")
    p_skillhub.add_argument("--all", dest="all_time_only", action="store_true", help="Only the all-time totals (skip the window)")
    p_skillhub.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p_skillhub.add_argument("--output", type=Path, default=None, help="Write report to a file instead of stdout")

    args = parser.parse_args(argv)

    if args.cmd == "skillhub":
        try:
            audit = build_skillhub_audit(
                args.routing_db, days=max(1, args.days), all_time_only=args.all_time_only
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        rendered = dumps_json(audit) if args.format == "json" else render_skillhub_markdown(audit)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0

    if args.cmd == "summary":
        summary = build_summary(
            audit_path=args.audit,
            routing_db=None if args.no_routing_db else args.routing_db,
            days=max(1, args.days),
            include_profiles=args.include_profiles,
            include_samples=args.include_samples,
            sample_limit=max(0, args.sample_limit),
        )
        rendered = dumps_json(summary) if args.format == "json" else render_markdown(summary)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
