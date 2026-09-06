"""Non-executing supply-chain admission for catalog stdio rows."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
from pathlib import Path
from typing import Any

from .connector_catalog_conformance import read_catalog


SHELL_OR_INDIRECT = {"sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh", "env"}


def _review(row: dict[str, Any]) -> dict[str, Any]:
    product = str(row["product"]).strip()
    catalog_id = str(row["catalog_id"]).strip()
    command = str(row.get("command") or "").strip()
    base = {
        "row_key": f"{product.casefold()}:{catalog_id.casefold()}",
        "product": product,
        "catalog_id": catalog_id,
        "stage": "stdio_admission",
        "complete": True,
        "credential_field_count": len(row.get("credential_key_names") or []),
        "evidence": {
            "catalog_version_present": bool(str(row.get("version") or "").strip()),
            "market_certified": row.get("certified") is True,
            "package_version_pinned": False,
            "source_digest_present": False,
            "license_verified": False,
        },
    }
    if not command:
        return {
            **base,
            "verdict": "rejected",
            "reason_code": "missing_stdio_command",
            "risks": ["invalid_manifest"],
            "next_action": "recover a complete public command manifest before review",
        }
    try:
        launcher = Path(shlex.split(command, posix=True)[0]).name.casefold()
    except (ValueError, IndexError):
        launcher = ""
    command_fingerprint = hashlib.sha256(command.encode()).hexdigest()[:16]
    if launcher in SHELL_OR_INDIRECT or not launcher:
        return {
            **base,
            "command_fingerprint": command_fingerprint,
            "verdict": "rejected",
            "reason_code": "indirect_or_shell_launcher",
            "risks": ["arbitrary_shell"],
            "next_action": "replace the shell wrapper with a pinned direct executable manifest",
        }
    risks = ["unverified_package"]
    if any(token in command for token in ("&&", "||", ";", "`", "$(")):
        risks.append("shell_metacharacters")
    if launcher == "docker":
        risks.append("container_control")
    return {
        **base,
        "command_fingerprint": command_fingerprint,
        "launcher": launcher,
        "verdict": "needs_sandbox",
        "reason_code": "package_identity_missing",
        "risks": risks,
        "next_action": "supply an immutable package version, source digest and license before sandbox launch",
    }


def admit_stdio_catalog(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
) -> list[dict[str, Any]]:
    return [
        _review(row)
        for row in read_catalog(path, expected_sha256=expected_sha256)
        if str(row.get("transport") or "").casefold() == "stdio"
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--expect-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    results = admit_stdio_catalog(args.catalog, expected_sha256=args.expect_sha256)
    args.output.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in results),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
