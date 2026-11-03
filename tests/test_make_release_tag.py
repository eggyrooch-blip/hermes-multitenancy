"""deploy/make-release-tag.sh: release-* 标签只能从 rev-parse 生成，且拒绝空发布 / 回退方向 / 残缺清单。

发布器 (deploy/hermes-release.sh) 只认标签正文的 `multitenancy:` / `webui:` 两行全 SHA，
写错就是静默回滚；这里用临时 bare origin + clone 把脚本每条拒绝路径都跑一遍。
"""
from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# 被测脚本是本地操作工具，git 是它的本体依赖；CI 的 python-slim 镜像没有 git，
# 在那里跑没有意义 —— 本地 targeted TEST 门仍强制执行本文件。
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="requires git binary")

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "make-release-tag.sh"
WEBUI_A = "a" * 40
WEBUI_B = "b" * 40

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}
_clock = [1_700_000_000]


def _git(cwd: Path, *args: str, **kw) -> str:
    # 每次调用换一个 committer 时间，保证 --sort=-creatordate 排序确定
    _clock[0] += 60
    env = {**_ENV, "GIT_COMMITTER_DATE": f"@{_clock[0]} +0000", "GIT_AUTHOR_DATE": f"@{_clock[0]} +0000"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, text=True,
                          capture_output=True, **kw).stdout.strip()


def _commit(work: Path, name: str) -> str:
    (work / name).write_text(name)
    _git(work, "add", name)
    _git(work, "commit", "-q", "-m", name)
    return _git(work, "rev-parse", "HEAD")


def _tag(work: Path, name: str, mt: str, webui: str | None, msg: str = "x") -> None:
    body = f"multitenancy: {mt}\n" + (f"webui: {webui}\n" if webui else "") + f"\n{msg}\n"
    _git(work, "tag", "-a", name, mt, "-m", body)
    _git(work, "push", "-q", "origin", f"refs/tags/{name}")


def _run(work: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=work, env=_ENV, text=True, capture_output=True)


def _tag_body(repo: Path, tag: str) -> str:
    return _git(repo, "tag", "-l", "--format=%(contents)", tag)


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "-c", "init.defaultBranch=main", "init", "-q", "--bare")
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "main")
    a = _commit(work, "a")
    _git(work, "push", "-q", "-u", "origin", "main")
    _tag(work, "release-20260101-01", a, WEBUI_A, "first")
    b = _commit(work, "b: 修了个东西")
    _git(work, "push", "-q", "origin", "main")
    return origin, work, a, b


def _today_tag(n: int) -> str:
    return f"release-{_dt.date.today():%Y%m%d}-{n:02d}"


def test_dry_run_prints_full_body_without_creating_tag(repos):
    origin, work, a, b = repos
    r = _run(work, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0] == _today_tag(1)
    assert f"multitenancy: {b}" in r.stdout
    assert f"webui: {WEBUI_A}" in r.stdout
    assert "b: 修了个东西" in r.stdout  # 默认说明 = 两版之间的 commit 标题
    assert "dry-run" in r.stdout
    assert _git(origin, "tag", "-l", "release-*").splitlines() == ["release-20260101-01"]


def test_creates_and_pushes_tag_pinning_main_and_inherited_webui(repos):
    origin, work, a, b = repos
    r = _run(work, "-m", "上线说明")
    assert r.returncode == 0, r.stderr
    tag = _today_tag(1)
    body = _tag_body(origin, tag).splitlines()
    assert body[0] == f"multitenancy: {b}"
    assert body[1] == f"webui: {WEBUI_A}"
    assert "上线说明" in body
    assert _git(origin, "rev-parse", f"{tag}^{{commit}}") == b
    assert "18:00" in r.stdout


def test_same_shas_is_rejected_as_empty_release(repos):
    origin, work, a, b = repos
    assert _run(work).returncode == 0
    r = _run(work)
    assert r.returncode != 0
    assert "无需新发布" in r.stderr
    assert _today_tag(2) not in _git(origin, "tag", "-l", "release-*")


def test_explicit_webui_bumps_sequence_and_rejects_short_sha(repos):
    origin, work, a, b = repos
    assert _run(work).returncode == 0
    r = _run(work, "--webui", WEBUI_B)
    assert r.returncode == 0, r.stderr
    body = _tag_body(origin, _today_tag(2)).splitlines()
    assert body[:2] == [f"multitenancy: {b}", f"webui: {WEBUI_B}"]
    r = _run(work, "--webui", WEBUI_B[:8])
    assert r.returncode != 0 and "40 位" in r.stderr


def test_rollback_direction_is_rejected(repos):
    origin, work, a, b = repos
    assert _run(work).returncode == 0  # 最新标签现在钉 b
    r = _run(work, "--mt", a)
    assert r.returncode != 0
    assert "回退" in r.stderr
    assert _today_tag(2) not in _git(origin, "tag", "-l", "release-*")


def test_last_tag_missing_webui_line_fails_closed(repos):
    origin, work, a, b = repos
    _tag(work, "release-20260102-01", b, None, "残缺")
    c = _commit(work, "c")
    _git(work, "push", "-q", "origin", "main")
    r = _run(work)
    assert r.returncode != 0
    assert "webui" in r.stderr and "残缺" in r.stderr
    assert _today_tag(1) not in _git(origin, "tag", "-l", "release-*")
