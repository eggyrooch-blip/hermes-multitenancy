"""release-cut.sh 的验收测试(SPEC S1/S2/S3)。

在临时 fixture 双仓(各带本地 bare origin)上跑真脚本,绝不触碰真实仓库/标签。
"""

import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "release-cut.sh"


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )
    return r.stdout.strip()


def _commit(repo: Path, msg: str) -> str:
    (repo / "f.txt").write_text(msg)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    _git(repo, "push", "-q", "-u", "origin", "main")
    return _git(repo, "rev-parse", "HEAD")


def _make_repo(root: Path, name: str) -> Path:
    origin = root / f"{name}-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = root / name
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "config", "user.email", "t@test")
    _git(work, "config", "user.name", "t")
    _git(work, "remote", "add", "origin", str(origin))
    _commit(work, "c1")
    return work


@pytest.fixture()
def repos(tmp_path):
    return _make_repo(tmp_path, "mt"), _make_repo(tmp_path, "webui")


def _cut(mt: Path, webui: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), "--repo-mt", str(mt), "--repo-webui", str(webui), *extra],
        capture_output=True, text=True,
    )


def _latest_tag(mt: Path) -> str:
    # 与 release-cut.sh 同款双键:同秒多标签 creatordate 平局时以版本名决胜
    return _git(
        mt, "tag", "-l", "release-*", "--sort=-v:refname", "--sort=-creatordate"
    ).splitlines()[0]


def _manifest(mt: Path, tag: str) -> dict:
    body = _git(mt, "tag", "-l", "--format=%(contents)", tag)
    out = {}
    for key in ("multitenancy", "webui"):
        m = re.search(rf"^{key}: ([0-9a-f]{{40}})$", body, re.M)
        assert m, f"{tag} 清单里解析不出 {key} 的 40 位 SHA:\n{body}"
        out[key] = m.group(1)
    return out


def test_s3_cut_produces_consumer_compatible_tag(repos):
    mt, webui = repos
    r = _cut(mt, webui)
    assert r.returncode == 0, r.stdout + r.stderr
    tag = _latest_tag(mt)
    assert re.fullmatch(r"release-\d{8}-\d{2}", tag)
    manifest = _manifest(mt, tag)
    assert manifest["multitenancy"] == _git(mt, "rev-parse", "origin/main")
    assert manifest["webui"] == _git(webui, "rev-parse", "origin/main")


def test_s1_no_new_commits_refused(repos):
    mt, webui = repos
    assert _cut(mt, webui).returncode == 0
    r = _cut(mt, webui)
    assert r.returncode != 0
    assert "无可发布内容" in r.stdout + r.stderr
    assert len(_git(mt, "tag", "-l", "release-*").splitlines()) == 1


def test_s2_rollback_refused_then_allowed(repos):
    mt, webui = repos
    webui_c1 = _git(webui, "rev-parse", "HEAD")
    assert _cut(mt, webui).returncode == 0          # tag-01 钉 webui c1
    _commit(webui, "c2")
    assert _cut(mt, webui).returncode == 0          # tag-02 钉 webui c2

    r = _cut(mt, webui, "--webui-ref", webui_c1)    # 要求回退到 c1
    assert r.returncode != 0
    assert "回退" in r.stdout + r.stderr
    assert len(_git(mt, "tag", "-l", "release-*").splitlines()) == 2

    r = _cut(mt, webui, "--webui-ref", webui_c1, "--allow-rollback")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _manifest(mt, _latest_tag(mt))["webui"] == webui_c1


@pytest.mark.parametrize(("current", "expected"), [("08", "09"), ("09", "10")])
def test_same_day_sequence_is_decimal(repos, current: str, expected: str):
    mt, webui = repos
    day = datetime.now().strftime("%Y%m%d")
    body = (
        f"multitenancy: {_git(mt, 'rev-parse', 'HEAD')}\n"
        f"webui: {_git(webui, 'rev-parse', 'HEAD')}"
    )
    _git(mt, "tag", "-a", f"release-{day}-{current}", "-m", body)
    _commit(webui, "c2")

    result = _cut(mt, webui, "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"release-{day}-{expected}" in result.stdout
