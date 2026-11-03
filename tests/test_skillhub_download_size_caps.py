"""MED-3 regression (audit 2026-07-03): skill/plugin package download + zip
extraction had no size caps, so a compromised/malicious artifact host could OOM
the shared broker (unbounded response.read()) or fill the shared disk (zip bomb
via shutil.copyfileobj). The download is capped, and extraction is bounded by
entry count + declared uncompressed total + actual written bytes.

FAILS on pre-fix code (no caps → no raise on oversized inputs).
"""
from __future__ import annotations

import io
import zipfile

import pytest

from hermes_multitenancy import skillhub_installer as shi
from hermes_multitenancy.skillhub_installer import SkillhubInstallError


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_download_cap_rejects_oversized_body(monkeypatch):
    monkeypatch.setattr(shi, "_MAX_DOWNLOAD_BYTES", 1024)

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b"x" * (n if n and n > 0 else 4096)  # over the cap

    monkeypatch.setattr(shi.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    with pytest.raises(SkillhubInstallError):
        shi._default_downloader("https://cdn.example/pkg.zip")


def test_extract_rejects_too_many_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(shi, "_MAX_ZIP_ENTRIES", 3)
    pkg = _make_zip({f"f{i}.txt": b"x" for i in range(5)})
    with pytest.raises(SkillhubInstallError):
        shi._extract_zip_safely(pkg, tmp_path)


def test_extract_rejects_oversized_uncompressed(monkeypatch, tmp_path):
    monkeypatch.setattr(shi, "_MAX_UNCOMPRESSED_BYTES", 1024)
    pkg = _make_zip({"big.txt": b"x" * 4096})
    with pytest.raises(SkillhubInstallError):
        shi._extract_zip_safely(pkg, tmp_path)


def test_copy_capped_rejects_actual_over_budget():
    # simulates a lying file_size header: actual bytes exceed the budget even
    # though the declared-total up-front check may have passed.
    with pytest.raises(SkillhubInstallError):
        shi._copy_capped(io.BytesIO(b"x" * 5000), io.BytesIO(), max_bytes=1000)


def test_copy_capped_allows_within_budget():
    sink = io.BytesIO()
    n = shi._copy_capped(io.BytesIO(b"hello"), sink, max_bytes=1000)
    assert n == 5
    assert sink.getvalue() == b"hello"


def test_extract_benign_zip_ok(tmp_path):
    pkg = _make_zip({"SKILL.md": b"# ok", "sub/a.txt": b"hi"})
    shi._extract_zip_safely(pkg, tmp_path)
    assert (tmp_path / "SKILL.md").read_bytes() == b"# ok"
    assert (tmp_path / "sub" / "a.txt").read_bytes() == b"hi"
