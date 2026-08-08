#!/usr/bin/env python3
"""Validate the MT half of the fixed Feishu capability contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

BASELINE = "dde0be3680d6fd5443cab426c8f4b3216266346a"
DIGEST = "5b24153ed4f0f1eeec410028534fbfd8b35ff5af875aa197f9378311bdeb4c15"
MAP = Path("hermes_multitenancy/feishu_openclaw_capabilities.json")
CAPABILITY_STATES = {"PARITY", "PARTIAL", "GAP", "HERMES_SUPERIOR", "SECURITY_DEVIATION", "NOT_APPLICABLE"}
ENABLED_STATES = {"ENABLED", "DISABLED", "UNVERIFIED", "NOT_APPLICABLE"}
VERIFIED_STATES = {"VERIFIED", "NOT_VERIFIED", "NOT_APPLICABLE"}
REQUIRED = {"contract_entry", "local_owner", "capability_state", "enabled_state", "production_verified_state", "test_id"}
EXPECTED_ENTRIES = {
    "events.core", "events.reaction", "events.vc", "events.drive_comment", "events.card_action",
    "converters.parity", "converters.partial", "actions.partial", "actions.gap", "cards.generic",
    "cards.actions.gap", "cards.actions.auth", "ask_user.schema", "workspace_tools.current_map",
    "footer.flags.partial", "footer.flags.gap", "footer.metrics", "config.identity", "config.policies",
    "config.reply_media_thread", "config.tool_flags", "config.runtime", "special.explicit", "advantages.hermes",
}
EXPECTED_EFFECTIVE = {"drive_comment", "webhook", "business_push_extension", "legacy_clarify", "readme_only_confirm_builder"}


def _validate_row(root: Path, row: dict, name_field: str, errors: list[str]) -> None:
    name = row.get(name_field, f"<missing-{name_field}>")
    required = REQUIRED if name_field == "contract_entry" else REQUIRED - {"contract_entry"} | {"id"}
    missing = required - row.keys()
    if missing:
        errors.append(f"{name}: missing {sorted(missing)}")
        return
    if row["capability_state"] not in CAPABILITY_STATES:
        errors.append(f"{name}: unknown capability state {row['capability_state']}")
    if row["enabled_state"] not in ENABLED_STATES:
        errors.append(f"{name}: unknown enabled state {row['enabled_state']}")
    if row["production_verified_state"] not in VERIFIED_STATES:
        errors.append(f"{name}: unknown production verified state {row['production_verified_state']}")
    if not row["test_id"]:
        errors.append(f"{name}: empty test id")
    for owner in row["local_owner"].split(";"):
        if not (root / owner).exists():
            errors.append(f"{name}: missing local owner {owner}")


def validate_capabilities(root: Path, capability_map: Path | None = None) -> list[str]:
    errors: list[str] = []
    try:
        data = json.loads((capability_map or root / MAP).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"capability map unreadable: {exc}"]
    if data.get("baseline_commit") != BASELINE:
        errors.append("baseline commit mismatch")
    if data.get("contract_digest") != DIGEST:
        errors.append("contract digest mismatch")

    rows = data.get("contract_entries", [])
    names = [row.get("contract_entry") for row in rows]
    if set(names) != EXPECTED_ENTRIES:
        errors.append(f"contract entries mismatch: missing={sorted(EXPECTED_ENTRIES - set(names))} extra={sorted(set(names) - EXPECTED_ENTRIES)}")
    if len(names) != len(set(names)):
        errors.append("duplicate contract entry")
    for row in rows:
        _validate_row(root, row, "contract_entry", errors)

    effective = data.get("effective_states", [])
    effective_names = [row.get("id") for row in effective]
    if set(effective_names) != EXPECTED_EFFECTIVE:
        errors.append("effective-state entries mismatch")
    if len(effective_names) != len(set(effective_names)):
        errors.append("duplicate effective-state entry")
    for row in effective:
        _validate_row(root, row, "id", errors)
    effective_by_id = {row.get("id"): row for row in effective}
    drive = effective_by_id.get("drive_comment", {})
    if drive.get("enabled_state") == "ENABLED" or drive.get("production_verified_state") == "VERIFIED":
        errors.append("drive_comment cannot be effective before trusted bridge verification")
    for feature in ("webhook", "business_push_extension", "legacy_clarify", "readme_only_confirm_builder"):
        if effective_by_id.get(feature, {}).get("enabled_state") != "DISABLED":
            errors.append(f"{feature} must remain disabled")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors = validate_capabilities(args.root)
    if errors:
        print("\n".join(errors))
        return 1
    print(f"ok entries={len(EXPECTED_ENTRIES)} effective_states={len(EXPECTED_EFFECTIVE)} digest={DIGEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
