from __future__ import annotations

import json
from pathlib import Path


def _audit_events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_redacted(path: Path, *, secret: str = "secret-token") -> None:
    raw = path.read_text(encoding="utf-8")
    assert "/Users" not in raw
    assert "/home" not in raw
    assert secret not in raw
    for event in _audit_events(path):
        assert "path" not in event
        if event.get("event_type") == "media.denied":
            assert "path_hash" in event
            assert isinstance(event["path_hash"], str)
            assert len(event["path_hash"]) == 12
            assert event["path_kind"] in {".png", ".txt"}


def test_symlink_to_sibling_profile_media_is_blocked_and_audited(tmp_path: Path, monkeypatch):
    from hermes_multitenancy import router

    audit_path = tmp_path / "audit.jsonl"
    profile = tmp_path / "profiles" / "alice"
    sibling = tmp_path / "profiles" / "bob"
    profile_workspace = profile / "workspace"
    sibling_workspace = sibling / "workspace"
    profile_workspace.mkdir(parents=True)
    sibling_workspace.mkdir(parents=True)
    secret_file = sibling_workspace / "secret-token-image.png"
    secret_file.write_bytes(b"png")
    symlink = profile_workspace / "linked.png"
    symlink.symlink_to(secret_file)
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    monkeypatch.setattr(router, "_current_sender_open_id", lambda: "ou_secret-token_sender")

    result = router._profile_scoped_media_response(f"created\nMEDIA:{symlink}", profile)

    assert "MEDIA:" not in result
    events = _audit_events(audit_path)
    assert events[-1]["event_type"] == "media.denied"
    assert events[-1]["reason"] == "outside_profile_home"
    assert "open_id_hash" in events[-1]
    _assert_redacted(audit_path)


def test_absolute_outside_profile_media_is_blocked_and_audited(tmp_path: Path, monkeypatch):
    from hermes_multitenancy import router

    audit_path = tmp_path / "audit.jsonl"
    profile = tmp_path / "profiles" / "alice"
    outside = tmp_path / "outside" / "secret-token-file.png"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"png")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))
    monkeypatch.setattr(router, "_current_sender_open_id", lambda: None)

    result = router._profile_scoped_media_response(f"created\nMEDIA:{outside}", profile)

    assert "MEDIA:" not in result
    events = _audit_events(audit_path)
    assert events[-1]["event_type"] == "media.denied"
    assert events[-1]["reason"] == "outside_profile_home"
    _assert_redacted(audit_path)


def test_legitimate_profile_workspace_media_is_allowed_without_denied_audit(
    tmp_path: Path, monkeypatch
):
    from hermes_multitenancy import router

    audit_path = tmp_path / "audit.jsonl"
    profile = tmp_path / "profiles" / "alice"
    source = profile / "workspace" / "report.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"png")
    monkeypatch.setenv("HERMES_MT_SECURITY_AUDIT_PATH", str(audit_path))

    result = router._profile_scoped_media_response(f"created\nMEDIA:{source}", profile)

    assert "MEDIA:" in result
    assert str(source.resolve()) in result
    assert [event for event in _audit_events(audit_path) if event.get("event_type") == "media.denied"] == []
