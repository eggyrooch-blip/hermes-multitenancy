"""Unit tests for hermes_multitenancy.skill_storage (档 A token isolation)."""
from __future__ import annotations

import os
import stat as stat_mod
from pathlib import Path

import pytest

from hermes_multitenancy import skill_storage
from hermes_multitenancy.skill_storage import (
    SkillStorageError,
    delete_token,
    get_token_path,
    list_skills_with_tokens,
    read_token,
    write_token,
)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_get_token_path_uses_explicit_profile_home(tmp_path: Path):
    path = get_token_path("google_drive", profile_home=tmp_path)
    assert path == tmp_path / "tokens" / "google_drive.json"
    # Parent dir created mode 0700.
    mode = stat_mod.S_IMODE(path.parent.stat().st_mode)
    assert mode == 0o700


def test_get_token_path_uses_hermes_home_env_when_no_arg(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = get_token_path("notion")
    assert path == tmp_path / "tokens" / "notion.json"


def test_get_token_path_explicit_overrides_env(monkeypatch, tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other))
    path = get_token_path("x_twitter", profile_home=tmp_path)
    assert path.parent == tmp_path / "tokens"  # the explicit arg wins


def test_get_token_path_raises_when_hermes_home_missing(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    with pytest.raises(SkillStorageError, match="HERMES_HOME is not set"):
        get_token_path("notion")


def test_get_token_path_honours_custom_extension(tmp_path: Path):
    path = get_token_path("github", extension="env", profile_home=tmp_path)
    assert path.name == "github.env"


# ---------------------------------------------------------------------------
# Skill name validation — path traversal prevention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "../evil",
        "..\\evil",
        "/etc/passwd",
        "good/sub",
        "good\\sub",
        ".hidden",
        "",
        " ",
        "Bad Name",
        "name with space",
        "UPPER",  # caller must pre-lower
        "x" * 64,  # exceeds 63-char limit
        "!notallowed",
    ],
)
def test_get_token_path_rejects_unsafe_skill_names(bad_name, tmp_path: Path):
    with pytest.raises(SkillStorageError):
        get_token_path(bad_name, profile_home=tmp_path)


@pytest.mark.parametrize(
    "good_name",
    [
        "google_drive",
        "google-drive",
        "x_twitter",
        "notion.refresh",
        "kep-prd-analysis",
        "a",
        "skill1",
    ],
)
def test_get_token_path_accepts_valid_skill_names(good_name, tmp_path: Path):
    path = get_token_path(good_name, profile_home=tmp_path)
    assert path.name.startswith(good_name)


def test_get_token_path_strips_surrounding_whitespace(tmp_path: Path):
    # Strip is applied before validation; case is NOT folded (fail-loud).
    path = get_token_path("  google_drive  ", profile_home=tmp_path)
    assert path.name == "google_drive.json"


def test_get_token_path_does_not_lowercase_caller_input(tmp_path: Path):
    """Mixed-case must be rejected to prevent silent 'Google' vs 'google' collision."""
    with pytest.raises(SkillStorageError):
        get_token_path("Google_Drive", profile_home=tmp_path)


@pytest.mark.parametrize(
    "bad_ext",
    [
        "",
        ".json",
        "json/etc",
        "json\\sub",
        "too-long-extension",
        "JSON",
        "j!",
    ],
)
def test_get_token_path_rejects_bad_extensions(bad_ext, tmp_path: Path):
    with pytest.raises(SkillStorageError):
        get_token_path("good", extension=bad_ext, profile_home=tmp_path)


# ---------------------------------------------------------------------------
# Write / read / delete roundtrip
# ---------------------------------------------------------------------------


def test_write_token_creates_file_chmod_0600(tmp_path: Path):
    target = write_token("google_drive", '{"refresh_token": "abc"}', profile_home=tmp_path)
    assert target.is_file()
    mode = stat_mod.S_IMODE(target.stat().st_mode)
    assert mode == 0o600


def test_read_token_returns_written_content(tmp_path: Path):
    write_token("notion", "secret-payload", profile_home=tmp_path)
    assert read_token("notion", profile_home=tmp_path) == "secret-payload"


def test_read_token_returns_none_when_missing(tmp_path: Path):
    assert read_token("never-written", profile_home=tmp_path) is None


def test_write_token_overwrites_existing(tmp_path: Path):
    write_token("k", "v1", profile_home=tmp_path)
    write_token("k", "v2", profile_home=tmp_path)
    assert read_token("k", profile_home=tmp_path) == "v2"


def test_write_token_rejects_non_string_content(tmp_path: Path):
    with pytest.raises(SkillStorageError):
        write_token("k", 12345, profile_home=tmp_path)  # type: ignore[arg-type]


def test_write_token_atomic_no_partial_files_on_success(tmp_path: Path):
    """After a successful write, no .skill.json.<random> temp file remains."""
    write_token("github", '{"token": "x"}', profile_home=tmp_path)
    tokens_dir = tmp_path / "tokens"
    leftovers = [p for p in tokens_dir.iterdir() if p.name.startswith(".")]
    assert not leftovers, f"temp files leaked: {[p.name for p in leftovers]}"


def test_write_token_atomic_cleanup_on_failure(tmp_path: Path, monkeypatch):
    """If os.replace fails, the temp file must be unlinked, not left behind."""
    def boom(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(skill_storage.os, "replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        write_token("github", '{"token": "x"}', profile_home=tmp_path)

    tokens_dir = tmp_path / "tokens"
    if tokens_dir.exists():
        leftovers = list(tokens_dir.iterdir())
        assert leftovers == [], f"failed write left temp files: {[p.name for p in leftovers]}"


def test_delete_token_removes_and_returns_true(tmp_path: Path):
    write_token("zapier", "x", profile_home=tmp_path)
    assert delete_token("zapier", profile_home=tmp_path) is True
    assert read_token("zapier", profile_home=tmp_path) is None


def test_delete_token_returns_false_when_missing(tmp_path: Path):
    assert delete_token("never", profile_home=tmp_path) is False


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_skills_with_tokens_returns_sorted_basenames(tmp_path: Path):
    write_token("zapier", "x", profile_home=tmp_path)
    write_token("google_drive", "y", profile_home=tmp_path)
    write_token("aaa", "z", profile_home=tmp_path)
    assert list_skills_with_tokens(tmp_path) == ["aaa", "google_drive", "zapier"]


def test_list_skills_with_tokens_empty_when_no_tokens(tmp_path: Path):
    assert list_skills_with_tokens(tmp_path) == []


# ---------------------------------------------------------------------------
# Cross-profile isolation — the headline goal of this module
# ---------------------------------------------------------------------------


def test_two_profiles_have_independent_token_dirs(tmp_path: Path):
    """Writing to profile A's google_drive token must not affect profile B."""
    alice = tmp_path / "alice"
    bob = tmp_path / "bob"
    write_token("google_drive", "alice-token", profile_home=alice)
    write_token("google_drive", "bob-token", profile_home=bob)

    assert read_token("google_drive", profile_home=alice) == "alice-token"
    assert read_token("google_drive", profile_home=bob) == "bob-token"

    # Independent file objects.
    assert (alice / "tokens" / "google_drive.json").read_text() == "alice-token"
    assert (bob / "tokens" / "google_drive.json").read_text() == "bob-token"


def test_env_based_resolution_swaps_when_hermes_home_changes(monkeypatch, tmp_path: Path):
    """Same skill name resolves to different files when HERMES_HOME swaps."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    monkeypatch.setenv("HERMES_HOME", str(a))
    write_token("x", "from-a")

    monkeypatch.setenv("HERMES_HOME", str(b))
    write_token("x", "from-b")

    monkeypatch.setenv("HERMES_HOME", str(a))
    assert read_token("x") == "from-a"
    monkeypatch.setenv("HERMES_HOME", str(b))
    assert read_token("x") == "from-b"
