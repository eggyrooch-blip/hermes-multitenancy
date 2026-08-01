"""deploy/hermes-release.sh 的真逻辑测试。

不碰生产：造两个假 git 仓、把 systemctl 和探针换成可注入的桩，在 tmp_path 里跑真脚本。
覆盖会真出事的分支：
  1. 没有新标签 / 已是最新 → 一个字节都不许动
  2. 发布清单残缺 → 拒绝发布
  3. 新版本不带探针 → 拒绝切换（没有存活判据就切，等于蒙眼上线）
  4. 探针失败 → 自动把软链翻回上一版
  5. 探针通过 → 记录已部署标签，并裁剪旧版本
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
RELEASE_SH = DEPLOY / "hermes-release.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _make_repo(path: Path, with_probes: bool, probe_exit: int = 0, prebuilt: bool = False) -> str:
    """造一个带 deploy/ 的假仓，返回 HEAD 的完整 sha。

    prebuilt=True 时预置 dist/server/index.js —— 脚本只在 dist 缺失时才跑
    npm ci + build，假仓里没有 package.json，预置它才能测到构建之后的分支。
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "README").write_text("x\n")
    if prebuilt:
        (path / "dist" / "server").mkdir(parents=True, exist_ok=True)
        (path / "dist" / "server" / "index.js").write_text("//\n")
    if with_probes:
        d = path / "deploy"
        d.mkdir(exist_ok=True)
        probe = d / "hermes-release-probes.sh"
        probe.write_text(f"#!/usr/bin/env bash\necho 'PROBES stub'\nexit {probe_exit}\n")
        probe.chmod(0o755)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture()
def env(tmp_path: Path):
    """一整套假环境：两个 canonical 仓 + releases 目录 + code 软链 + 桩 systemctl。"""
    home = tmp_path / "home"
    releases = home / "releases"
    code = home / "code"
    for d in (releases, code, home / ".hermes"):
        d.mkdir(parents=True, exist_ok=True)

    mt_src = tmp_path / "src-mt"
    webui_src = tmp_path / "src-webui"
    mt_sha = _make_repo(mt_src, with_probes=True)
    webui_sha = _make_repo(webui_src, with_probes=False, prebuilt=True)

    subprocess.run(["git", "clone", "-q", "--no-checkout", str(mt_src), str(releases / ".repo-mt")], check=True)
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(webui_src), str(releases / ".repo-webui")], check=True)

    # 先摆一个「当前在跑的版本」，并让 code 下是软链
    cur_mt = releases / "mt-current"
    cur_webui = releases / "webui-current"
    for d, src in ((cur_mt, mt_src), (cur_webui, webui_src)):
        shutil.copytree(src, d, ignore=shutil.ignore_patterns(".git"))
    (cur_webui / "dist" / "server").mkdir(parents=True, exist_ok=True)
    (cur_webui / "dist" / "server" / "index.js").write_text("//\n")
    (code / "hermes-multitenancy").symlink_to("../releases/mt-current")
    (code / "hermes-web-ui").symlink_to("../releases/webui-current")

    # 桩 systemctl：只记调用，不真动服务
    stub = tmp_path / "systemctl-stub.sh"
    stub.write_text("#!/usr/bin/env bash\necho \"$@\" >> \"$SYSTEMCTL_LOG\"\nexit 0\n")
    stub.chmod(0o755)

    return {
        "HOME": str(home), "RELEASES": str(releases), "CODE": str(code),
        "STATE_FILE": str(home / ".hermes" / "deployed-release"),
        "BACKUP_ROOT": str(home / "backups" / "pre-release"),
        "LOCK": str(home / ".hermes" / ".release.lock"),
        "SYSTEMCTL": str(stub), "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
        "BACKUP_SH": "/nonexistent",       # 备份另有测试覆盖，这里不重复
        "PROBES": str(DEPLOY / "hermes-release-probes.sh"),
        "PATH": os.environ["PATH"],
        "_mt_sha": mt_sha, "_webui_sha": webui_sha,
        "_releases": releases, "_code": code, "_mt_src": mt_src,
    }


def _tag(env, name: str, body: str) -> None:
    src = env["_mt_src"]
    _git(src, "tag", "-a", name, "-m", body)
    _git(Path(env["RELEASES"]) / ".repo-mt", "fetch", "-q", "--tags", "origin")


def _run(env) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in env.items() if not k.startswith("_")}
    return subprocess.run(["bash", str(RELEASE_SH)], env=clean, capture_output=True, text=True)


def _links(env):
    c = Path(env["CODE"])
    return os.readlink(c / "hermes-multitenancy"), os.readlink(c / "hermes-web-ui")


# ── 1. 没有新东西时一个字节都不许动 ──────────────────────────────────


def test_no_tag_is_a_noop(env):
    before = _links(env)
    r = _run(env)
    assert r.returncode == 0
    assert "没有任何 release-* 标签" in r.stdout
    assert _links(env) == before
    assert not Path(env["SYSTEMCTL_LOG"]).exists(), "不该碰任何服务"


def test_already_deployed_tag_is_a_noop(env):
    _tag(env, "release-x", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    Path(env["STATE_FILE"]).write_text("release-x\n")
    before = _links(env)
    r = _run(env)
    assert r.returncode == 0
    assert "已是最新" in r.stdout
    assert _links(env) == before


# ── 2. 清单残缺就别发 ────────────────────────────────────────────────


def test_incomplete_manifest_is_refused(env):
    _tag(env, "release-bad", f"multitenancy: {env['_mt_sha']}")   # 少了 webui
    before = _links(env)
    r = _run(env)
    assert r.returncode != 0
    assert "拒绝发布残缺清单" in r.stderr
    assert _links(env) == before


# ── 3. 新版本不带探针 → 拒绝切换（没有判据不许上）────────────────────


def test_refuses_to_flip_when_new_release_has_no_probes(env, tmp_path):
    # 造一个不带 deploy/ 的新提交
    src = env["_mt_src"]
    shutil.rmtree(src / "deploy")
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "drop probes")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-noprobe", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")

    before = _links(env)
    r = _run(env)
    assert r.returncode != 0
    assert "没有可执行的探针" in r.stderr
    assert _links(env) == before, "没有判据就不该切，更不该切了再回滚"
    assert not Path(env["SYSTEMCTL_LOG"]).exists(), "不该重启服务"


# ── 4. 探针失败 → 自动回滚 ───────────────────────────────────────────


def test_failing_probe_triggers_rollback(env):
    src = env["_mt_src"]
    (src / "deploy" / "hermes-release-probes.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
    (src / "deploy" / "hermes-release-probes.sh").chmod(0o755)
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "red probe")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-red", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")

    before = _links(env)
    r = _run(env)
    assert r.returncode != 0
    assert "自动回滚" in r.stdout
    assert "ROLLED BACK" in r.stdout
    assert _links(env) == before, "必须原样翻回上一版"
    assert not Path(env["STATE_FILE"]).exists(), "失败的发布不许记成已部署"


# ── 5. 探针通过 → 记录并生效 ─────────────────────────────────────────


def test_passing_probe_marks_release_deployed(env):
    src = env["_mt_src"]
    (src / "extra.txt").write_text("new\n")
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "green")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-green", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")

    r = _run(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RELEASE OK" in r.stdout
    assert Path(env["STATE_FILE"]).read_text().strip() == "release-green"
    mt_link, _ = _links(env)
    assert sha[:7] in mt_link, "软链应指向新版本目录"
    assert "start" in Path(env["SYSTEMCTL_LOG"]).read_text()


def test_dry_run_never_touches_anything(env):
    _tag(env, "release-dry", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    before = _links(env)
    env2 = {**env, "DRY_RUN": "1"}
    r = _run(env2)
    assert r.returncode == 0
    assert "DRY_RUN=1" in r.stdout
    assert _links(env) == before
    assert not Path(env["SYSTEMCTL_LOG"]).exists()
