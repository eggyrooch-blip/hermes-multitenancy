"""Feishu group-webhook alert for update-center timer failures.

Wired via systemd ``OnFailure=hermes-update-center-alert@%n.service``. The
webhook URL is deployment secret material: it lives ONLY in the prod
EnvironmentFile (``~/.hermes/update-center/alert.env``), never in this repo.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

from .update_center import redact

WEBHOOK_ENV = "HERMES_UPDATE_CENTER_WEBHOOK"
_JOURNAL_TAIL_LINES = 15
_TEXT_CAP = 1800


def _journal_tail(unit: str) -> str:
    try:
        result = subprocess.run(
            ["journalctl", "--user", "-u", unit, "-n", str(_JOURNAL_TAIL_LINES), "--no-pager", "-o", "cat"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        return (result.stdout or result.stderr or "").strip()[-_TEXT_CAP:]
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(journal unavailable: {exc})"


def build_alert_text(unit: str, *, journal: str | None = None) -> str:
    host = os.uname().nodename
    tail = journal if journal is not None else _journal_tail(unit)
    return redact(
        f"⚠️ update-center 同步失败\nunit: {unit}\nhost: {host}\n--- journal tail ---\n{tail}"
    )


def send_failure_alert(unit: str, *, webhook: str | None = None, opener=None) -> int:
    url = (webhook or os.environ.get(WEBHOOK_ENV, "")).strip()
    if not url:
        print(f"error: {WEBHOOK_ENV} not set (expected via EnvironmentFile)", file=sys.stderr)
        return 1
    payload = json.dumps(
        {"msg_type": "text", "content": {"text": build_alert_text(unit)}}, ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=10) as response:
            body = response.read().decode("utf-8", "replace")
    except (OSError, ValueError) as exc:
        print(f"error: webhook post failed: {exc}", file=sys.stderr)
        return 1
    print(body[:200])
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or not args[0].strip():
        print("usage: python -m hermes_multitenancy.update_center_alert <failed-unit-name>", file=sys.stderr)
        return 2
    return send_failure_alert(args[0].strip())


if __name__ == "__main__":
    raise SystemExit(main())
