#!/usr/bin/env python3
"""Record reviewed historical feedback-image candidates.

Historical `[Image: source: ...]` references are diagnostic only. This script
records an explicit human review when a historical candidate is rejected, so
the completion audit can explain why it did not satisfy the current feedback
screenshot requirement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path
from typing import Any


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return 0, 0
    if len(data) < 24 or data[12:16] != b"IHDR":
        return 0, 0
    return struct.unpack(">II", data[16:24])


def _image_dimensions(path: Path, data: bytes) -> tuple[int, int]:
    if path.suffix.lower() == ".png":
        return _png_dimensions(data)
    return 0, 0


def build_review(
    *,
    source: Path,
    labels: list[str],
    verdict: str,
    reason: str,
) -> dict[str, Any]:
    data = source.read_bytes()
    width, height = _image_dimensions(source, data)
    return {
        "source": str(source),
        "labels": labels,
        "verdict": verdict,
        "reason": reason,
        "md5": hashlib.md5(data).hexdigest(),
        "pixel_width": width,
        "pixel_height": height,
    }


def _load_reviews(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    reviews = payload.get("reviews") if isinstance(payload, dict) else None
    if not isinstance(reviews, list):
        return []
    return [review for review in reviews if isinstance(review, dict)]


def write_review(output: Path, review: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    source = str(review.get("source") or "")
    reviews = [
        existing
        for existing in _load_reviews(output)
        if str(existing.get("source") or "") != source
    ]
    reviews.append(review)
    output.write_text(
        json.dumps({"reviews": reviews}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--label", action="append", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--verdict", default="rejected", choices=["rejected"])
    args = parser.parse_args()

    review = build_review(
        source=args.source,
        labels=[str(label) for label in args.label],
        verdict=args.verdict,
        reason=args.reason,
    )
    write_review(args.output, review)
    print(json.dumps(review, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
