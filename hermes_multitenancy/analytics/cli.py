from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .kep_telemetry_export import (
    BATCH_SIZE,
    DEFAULT_BACKFILL_DAYS,
    default_state_dir,
    diagnose,
    run_export,
    status_report,
)
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

    p_kep = sub.add_parser(
        "kep-telemetry-export",
        help="Export Hermes skill usage to the kep-telemetry Hub (skill-runs)",
    )
    p_kep.add_argument("--audit", type=Path, default=DEFAULT_AUDIT_PATH, help="conversation-audit.jsonl path")
    p_kep.add_argument("--state-dir", type=Path, default=default_state_dir(), help="cursor/ledger/dead-letter dir")
    p_kep.add_argument("--env", choices=["online", "pre"], default="online", help="Hub environment")
    p_kep.add_argument("--dry-run", action="store_true", help="Report the would-be batch; write nothing, send nothing")
    p_kep.add_argument("--status", action="store_true", help="Print the settlement state and exit")
    p_kep.add_argument("--backfill-days", type=int, default=DEFAULT_BACKFILL_DAYS, help="First-run/rotation lookback")
    p_kep.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Records per POST")
    p_kep.add_argument("--kep-auth-bin", default="kep-auth", help="kep-auth executable used to inject credentials")
    p_kep.add_argument("--json", dest="as_json", action="store_true", help="Print the raw report as JSON")
    p_kep.add_argument("--diagnose", action="store_true",
                       help="Export the local diagnostic rows to ONE self-describing file and exit (read-only)")
    p_kep.add_argument("--since", default="3d", help="--diagnose window: 3d / 12h / 90m / all (default 3d)")
    p_kep.add_argument("--out", type=Path, default=None,
                       help="--diagnose output path (default /tmp/kep-telemetry-diag-<date>.ndjson; gzipped when large)")

    args = parser.parse_args(argv)

    if args.cmd == "kep-telemetry-export":
        return _run_kep_telemetry_export(args)

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


def _run_kep_telemetry_export(args: argparse.Namespace) -> int:
    # --diagnose is a read-only hand-off: no cursor, no ledger, no network. It returns
    # before the export path, so the export exit codes below are untouched by it.
    if getattr(args, "diagnose", False):
        try:
            report = diagnose(state_dir=args.state_dir, since=args.since, out=args.out, env=args.env)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"error: could not write the diagnostic bundle: {exc}", file=sys.stderr)
            return 1
        if args.as_json:
            sys.stdout.write(dumps_json(report))
        else:
            sys.stdout.write(
                f"kep-telemetry diagnose since={report['since']} rows={report['rows']}"
                f" files={report['day_files']}{' gzip' if report['gzipped'] else ''}\n"
                f"  {report['out']} ({report['bytes']} bytes)\n"
            )
        return 0

    if args.status:
        sys.stdout.write(dumps_json(status_report(args.state_dir)))
        return 0

    try:
        report = run_export(
            audit_path=args.audit,
            state_dir=args.state_dir,
            env=args.env,
            dry_run=args.dry_run,
            backfill_days=max(0, args.backfill_days),
            batch_size=max(1, args.batch_size),
            kep_auth_bin=args.kep_auth_bin,
        )
    except FileNotFoundError as exc:
        print(f"error: audit not readable: {exc}", file=sys.stderr)
        return 1

    if args.as_json:
        sys.stdout.write(dumps_json(report))
    else:
        sys.stdout.write(_render_kep_summary(report))

    stopped = report["upload"].get("stopped")
    # An auth stop is operator-actionable (kep-auth login expired), so it gets its
    # own exit code; a network/rate-limit stop is normal and retries next tick.
    if isinstance(stopped, str) and stopped.startswith("auth"):
        return 2
    return 0


def _render_kep_summary(report: dict) -> str:
    read, built, upload = report["read"], report["built"], report["upload"]
    lines = [
        f"kep-telemetry-export {report['at']} env={report['env']}"
        f"{' dry-run' if report['dry_run'] else ''}",
        f"  read    lines={read['lines']} skill_calls={read['skill_calls']} terminals={read['terminals']}"
        f" restarted={read['restarted']}",
        f"  built   records={built['records']} with_terminal={built['with_terminal']}"
        f" unresolved={built['unresolved']} pending={built['still_pending']}"
        f" already_confirmed={built['already_confirmed']}",
    ]
    if report["dry_run"]:
        lines.append(f"  upload  would_send={upload['would_send']} (nothing written, nothing sent)")
    else:
        lines.append(
            f"  upload  batches={upload['batches']} operators={upload.get('operators', 0)} sent={upload['sent']}"
            f" accepted={upload['accepted']} rejected={sum(upload['rejected_by_reason'].values())} queued={upload['queued']}"
        )
        if upload["rejected_by_reason"]:
            reasons = ", ".join(f"{k}={v}" for k, v in sorted(upload["rejected_by_reason"].items()))
            lines.append(f"  rejected {reasons}")
        retry = report.get("retry") or {}
        if retry.get("requeued") or retry.get("waiting") or retry.get("exhausted"):
            lines.append(
                f"  retry   requeued={retry.get('requeued', 0)} waiting={retry.get('waiting', 0)}"
                f" exhausted={retry.get('exhausted', 0)} permanent={retry.get('permanent', 0)}"
                f" legacy={retry.get('legacy', 0)}")
        diag = report.get("diagnostics") or {}
        if diag.get("path"):
            line = f"  log     rows={diag.get('rows', 0)} {diag['path']}"
            if diag.get("pruned"):
                line += f" pruned={len(diag['pruned'])}"
            if diag.get("dropped"):
                line += f" dropped={diag['dropped']}"
            if diag.get("errors"):
                line += f" write_errors={diag['errors']}"
            if diag.get("stopped"):
                line += f" stopped={diag['stopped']}"
            lines.append(line)
    if upload.get("stopped"):
        lines.append(f"  stopped {upload['stopped']}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
