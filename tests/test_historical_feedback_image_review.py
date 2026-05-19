from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "historical_feedback_image_review.py"


def _load_review_module():
    spec = importlib.util.spec_from_file_location("historical_feedback_image_review", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tiny_png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        b"\x00\x00\x00\x00"
    )


def test_build_rejected_review_records_md5_dimensions_labels_and_reason(tmp_path: Path):
    review_mod = _load_review_module()
    image = tmp_path / "candidate.png"
    image.write_bytes(_tiny_png(1372, 1488))

    review = review_mod.build_review(
        source=image,
        labels=["Image #1"],
        verdict="rejected",
        reason="lark_group_invite_qr_not_feedback_screenshot",
    )

    assert review["source"] == str(image)
    assert review["labels"] == ["Image #1"]
    assert review["verdict"] == "rejected"
    assert review["reason"] == "lark_group_invite_qr_not_feedback_screenshot"
    assert review["md5"] == "96b3daf0d33ca3e076ca0c8f6b46cd9e"
    assert review["pixel_width"] == 1372
    assert review["pixel_height"] == 1488


def test_write_review_file_upserts_same_source_and_labels(tmp_path: Path):
    review_mod = _load_review_module()
    output = tmp_path / "historical-image-reviews.json"
    image = tmp_path / "candidate.png"
    image.write_bytes(_tiny_png(10, 20))

    review = review_mod.build_review(
        source=image,
        labels=["Image #1"],
        verdict="rejected",
        reason="first_reason",
    )
    review_mod.write_review(output, review)
    review_mod.write_review(output, {**review, "reason": "second_reason"})

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload == {
        "reviews": [
            {
                "source": str(image),
                "labels": ["Image #1"],
                "verdict": "rejected",
                "reason": "second_reason",
                "md5": "4eb335d00ca620ce7a3fa5289389a825",
                "pixel_width": 10,
                "pixel_height": 20,
            }
        ]
    }


def test_write_review_file_replaces_stale_review_for_same_source(tmp_path: Path):
    review_mod = _load_review_module()
    output = tmp_path / "historical-image-reviews.json"
    image = tmp_path / "candidate.png"
    image.write_bytes(_tiny_png(10, 20))
    output.write_text(
        json.dumps(
            {
                "reviews": [
                    {
                        "source": str(image),
                        "labels": ["Image "],
                        "verdict": "rejected",
                        "reason": "stale_make_comment_parse",
                        "md5": "bad",
                        "pixel_width": 0,
                        "pixel_height": 0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    review = review_mod.build_review(
        source=image,
        labels=["Image #1"],
        verdict="rejected",
        reason="lark_group_invite_qr_not_feedback_screenshot",
    )
    review_mod.write_review(output, review)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len(payload["reviews"]) == 1
    assert payload["reviews"][0]["labels"] == ["Image #1"]
    assert payload["reviews"][0]["reason"] == "lark_group_invite_qr_not_feedback_screenshot"
