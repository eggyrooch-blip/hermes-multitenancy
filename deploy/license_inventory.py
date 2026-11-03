#!/usr/bin/env python3
"""AGPL / license inventory for Hermes dependencies.

Enumerates all installed Python distributions in the current environment,
extracts license metadata, classifies into permissive / copyleft / unknown,
and writes a structured JSON report.

Usage:
    python deploy/license_inventory.py [--output report.json]

No external dependencies — uses only stdlib (importlib.metadata).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from importlib.metadata import distributions, PackageNotFoundError
except ImportError:
    # Python < 3.8 fallback (unlikely in production, but defensive)
    from importlib_metadata import distributions, PackageNotFoundError


# ---------------------------------------------------------------------------
# License classification
# ---------------------------------------------------------------------------

#: Licenses considered copyleft — require source disclosure / derivative
#: works must be under the same license. These are the ones we must flag
#: for AGPL compliance review.
COPYLEFT_PATTERNS = [
    re.compile(r"\bAGPL\b", re.IGNORECASE),
    re.compile(r"\bGPL\b", re.IGNORECASE),
    re.compile(r"\bLGPL\b", re.IGNORECASE),
    re.compile(r"\bMPL\b", re.IGNORECASE),
    re.compile(r"\bAffero\s+General\s+Public", re.IGNORECASE),
    re.compile(r"\bGeneral\s+Public\s+License", re.IGNORECASE),
    re.compile(r"\bLesser\s+General\s+Public", re.IGNORECASE),
    re.compile(r"\bMozilla\s+Public", re.IGNORECASE),
    re.compile(r"\bCC-BY-SA\b", re.IGNORECASE),
    re.compile(r"\bEclipse\s+Public", re.IGNORECASE),
    re.compile(r"\bCDDL\b", re.IGNORECASE),
    re.compile(r"\bEPL\b", re.IGNORECASE),
]

#: Licenses considered permissive — no copyleft restrictions.
PERMISSIVE_PATTERNS = [
    re.compile(r"\bMIT\b", re.IGNORECASE),
    re.compile(r"\bApache\b", re.IGNORECASE),
    re.compile(r"\bBSD\b", re.IGNORECASE),
    re.compile(r"\bISC\b", re.IGNORECASE),
    re.compile(r"\bPython\s+Software\s+Foundation", re.IGNORECASE),
    re.compile(r"\bPSF\b", re.IGNORECASE),
    re.compile(r"\bUnlicense\b", re.IGNORECASE),
    re.compile(r"\bCC0\b", re.IGNORECASE),
    re.compile(r"\bWTFPL\b", re.IGNORECASE),
    re.compile(r"\bZlib\b", re.IGNORECASE),
    re.compile(r"\bBouncy\s+Castle", re.IGNORECASE),
]

#: Classifier prefixes that indicate copyleft.
COPYLEFT_CLASSIFIER_PREFIX = "License :: OSI Approved :: "

COPYLEFT_CLASSIFIER_KEYWORDS = [
    "GNU General Public License",
    "GNU Lesser General Public License",
    "GNU Affero General Public License",
    "Mozilla Public License",
    "Eclipse Public License",
    "Common Development and Distribution License",
    "CC-BY-SA",
]


def classify_license(license_text: str, classifiers: list[str]) -> str:
    """Classify a license string as 'permissive', 'copyleft', or 'unknown'.

    Checks both the License metadata field and the Trove classifiers.
    Copyleft takes priority — if any signal suggests copyleft, we flag it.
    """
    combined = (license_text or "") + " " + " ".join(classifiers or [])

    # Check copyleft first (priority — false negative is worse than false positive)
    for pattern in COPYLEFT_PATTERNS:
        if pattern.search(combined):
            return "copyleft"

    for kw in COPYLEFT_CLASSIFIER_KEYWORDS:
        for clf in classifiers or []:
            if kw.lower() in clf.lower():
                return "copyleft"

    # Check permissive
    for pattern in PERMISSIVE_PATTERNS:
        if pattern.search(combined):
            return "permissive"

    return "unknown"


def extract_license(dist) -> tuple[str, list[str]]:
    """Extract license text and classifiers from a distribution."""
    metadata = dist.metadata
    license_text = metadata.get("License", "") or ""
    classifiers = [c for c in metadata.get_all("Classifier") or []
                   if c.startswith("License")]

    # If License field is empty or generic, try to extract from classifiers
    if not license_text or license_text.upper() == "UNKNOWN":
        for clf in classifiers:
            # "License :: OSI Approved :: MIT License" → "MIT License"
            parts = clf.split("::")
            if len(parts) >= 3:
                extracted = parts[-1].strip()
                if extracted and extracted.upper() != "OTHER":
                    license_text = extracted
                    break

    return license_text.strip(), classifiers


def build_inventory() -> dict:
    """Build the full license inventory report."""
    packages = []
    copyleft_count = 0
    permissive_count = 0
    unknown_count = 0

    for dist in distributions():
        name = dist.metadata["Name"] or "(unknown)"
        version = dist.version or ""

        license_text, classifiers = extract_license(dist)
        category = classify_license(license_text, classifiers)

        if category == "copyleft":
            copyleft_count += 1
        elif category == "permissive":
            permissive_count += 1
        else:
            unknown_count += 1

        packages.append({
            "name": name,
            "version": version,
            "license": license_text or "(unknown)",
            "category": category,
            "classifiers": classifiers,
        })

    # Sort by name for stable output
    packages.sort(key=lambda p: p["name"].lower())

    # Move copyleft to the top for visibility
    copyleft = [p for p in packages if p["category"] == "copyleft"]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_packages": len(packages),
            "permissive": permissive_count,
            "copyleft": copyleft_count,
            "unknown": unknown_count,
        },
        "copyleft_packages": copyleft,
        "all_packages": packages,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AGPL/license inventory for Hermes dependencies"
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=Path("license_inventory.json"),
        help="Output JSON file (default: license_inventory.json)",
    )
    parser.add_argument(
        "--summary-only", "-s",
        action="store_true",
        help="Print only the summary to stdout",
    )
    args = parser.parse_args(argv)

    report = build_inventory()

    # Write JSON report
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Print summary
    s = report["summary"]
    print(f"License Inventory — {s['total_packages']} packages")
    print(f"  Permissive: {s['permissive']}")
    print(f"  Copyleft:   {s['copyleft']}")
    print(f"  Unknown:    {s['unknown']}")

    if report["copyleft_packages"]:
        print(f"\n⚠️  Copyleft packages requiring review:")
        for p in report["copyleft_packages"]:
            print(f"  {p['name']}=={p['version']} — {p['license']}")

    if not args.summary_only and s["unknown"] > 0:
        print(f"\nℹ️  {s['unknown']} package(s) with unknown license — manual review recommended")

    print(f"\nFull report: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
