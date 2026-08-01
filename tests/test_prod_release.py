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

    # 稳定 .env：脚本会校验它存在且权限 600
    stable = home / ".hermes-web-ui"
    stable.mkdir(parents=True, exist_ok=True)
    (stable / ".env").write_text("SECRET=x\n")
    (stable / ".env").chmod(0o600)

    # 桩备份脚本：脚本现在要求 BACKUP_SH 必须存在且可执行
    #（缺了就静默跳过 = 悄悄失去「不带备份不发布」这条保护）
    bstub = tmp_path / "backup-stub.sh"
    bstub.write_text('#!/usr/bin/env bash\nmkdir -p "$BACKUP_ROOT/state/x/db"\nexit 0\n')
    bstub.chmod(0o755)

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
        "BACKUP_SH": str(bstub),           # 备份本体另有测试覆盖，这里只要它存在
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


# ── 6. 评审 round 2 逼出来的加固项 ───────────────────────────────────


@pytest.mark.parametrize("bad", ["deadbeef", "zz" * 20, ""])
def test_malformed_sha_is_refused(env, bad):
    """手打标签少写几位、或误写成分支名，必须得到明确的拒绝，
    而不是一个难懂的 worktree 报错、更不能检出到别的东西。"""
    _tag(env, f"release-bad-{abs(hash(bad)) % 9999}",
         f"multitenancy: {bad}\nwebui: {env['_webui_sha']}")
    before = _links(env)
    r = _run(env)
    assert r.returncode != 0
    assert ("40 位 hex" in r.stderr or "长度不是 40" in r.stderr
            or "没有这个提交" in r.stderr or "残缺清单" in r.stderr)
    assert _links(env) == before


def test_missing_stable_env_blocks_build(env):
    """webui 的 .env 不在 git 里；稳定副本缺了就起不来，必须在构建前拦住。"""
    (Path(env["HOME"]) / ".hermes-web-ui" / ".env").unlink()
    _tag(env, "release-noenv", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run(env)
    assert r.returncode != 0
    assert "缺少稳定的 webui .env" in r.stderr


def test_loose_env_permissions_block_build(env):
    """22 行密钥不能摊开给同机其他用户。"""
    envfile = Path(env["HOME"]) / ".hermes-web-ui" / ".env"
    envfile.chmod(0o644)
    _tag(env, "release-openenv", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run(env)
    assert r.returncode != 0
    assert "必须是 600" in r.stderr


def test_every_terminal_branch_records_an_outcome(env):
    """只 exit 1 的话运维手上只有一份陈旧锚点，看不出这次是成了、退了、还是退也没退成。"""
    src = env["_mt_src"]
    (src / "extra").write_text("x\n")
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "green")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-outcome", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")

    assert _run(env).returncode == 0
    rb = Path(env["BACKUP_ROOT"]) / "release-outcome" / "ROLLBACK.txt"
    assert "outcome=SUCCESS" in rb.read_text()


def test_rollback_branch_records_outcome(env):
    src = env["_mt_src"]
    (src / "deploy" / "hermes-release-probes.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
    (src / "deploy" / "hermes-release-probes.sh").chmod(0o755)
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "red")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-redout", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")

    assert _run(env).returncode != 0
    rb = (Path(env["BACKUP_ROOT"]) / "release-redout" / "ROLLBACK.txt").read_text()
    assert "outcome=ROLLED_BACK" in rb or "outcome=NEEDS_HUMAN" in rb


def test_env_hash_survives_a_release(env):
    """.env 跨版本必须原样存活 —— 这是迁移时最大的雷。"""
    import hashlib
    envfile = Path(env["HOME"]) / ".hermes-web-ui" / ".env"
    before = hashlib.sha256(envfile.read_bytes()).hexdigest()

    src = env["_mt_src"]
    (src / "e2").write_text("x\n")
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "g2")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-envkeep", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")
    assert _run(env).returncode == 0

    assert hashlib.sha256(envfile.read_bytes()).hexdigest() == before
    link = Path(env["CODE"]) / "hermes-web-ui" / ".env"
    assert link.exists(), "release 目录里应有指向稳定 .env 的软链"


def test_prune_never_removes_the_rollback_target(env):
    """裁剪按绝对路径精确比对：当前版本和上一版(回滚目标)都不许删。"""
    src = env["_mt_src"]
    kept = []
    for i in range(3):
        (src / f"f{i}").write_text("x\n")
        _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", f"c{i}")
        sha = _git(src, "rev-parse", "HEAD")
        _tag(env, f"release-p{i}", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")
        assert _run(env).returncode == 0, f"第 {i} 次发布应成功"
        kept.append(sha[:7])

    cur = os.readlink(Path(env["CODE"]) / "hermes-multitenancy")
    assert (Path(env["RELEASES"]) / Path(cur).name).is_dir(), "当前版本目录必须还在"
    # 关键：跑过 KEEP_RELEASES+1 次之后，**上一版（回滚目标）也必须还在**。
    # 只断言"当前还在"是不够的 —— 把回滚目标裁掉，等于发布出问题时无路可退。
    prev_dir = Path(env["RELEASES"]) / f"mt-{kept[-2]}"
    assert prev_dir.is_dir(), f"上一版 {prev_dir.name} 被裁掉了 —— 回滚目标不能删"


def test_refuses_when_code_paths_are_not_symlinks(env):
    """执行器要求 ~/code/hermes-* 已经是软链。不是的话必须明确拒绝，
    而不是稀里糊涂地在真目录上乱来。"""
    code = Path(env["CODE"])
    (code / "hermes-multitenancy").unlink()
    (code / "hermes-multitenancy").mkdir()
    _tag(env, "release-nolink", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run(env)
    assert r.returncode != 0
    assert "还不是软链" in r.stderr


def test_stable_bin_bootstrap_only_after_success(env, tmp_path):
    """执行器自身只在发布成功之后才同步到稳定路径 —— 否则一个坏版本
    会把下次部署和回滚工具一起弄坏，连退路都没有。"""
    stable = tmp_path / "stable-bin"
    src = env["_mt_src"]

    # 先来一次会失败的发布：稳定路径不该被写
    (src / "deploy" / "hermes-release-probes.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
    (src / "deploy" / "hermes-release-probes.sh").chmod(0o755)
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "red")
    _tag(env, "release-sb1", f"multitenancy: {_git(src, 'rev-parse', 'HEAD')}\nwebui: {env['_webui_sha']}")
    assert _run({**env, "STABLE_BIN": str(stable)}).returncode != 0
    assert not (stable / "hermes-release.sh").exists(), "失败的发布不许更新执行器自身"

    # 再来一次会成功的：这时才允许同步
    (src / "deploy" / "hermes-release-probes.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (src / "deploy" / "hermes-release-probes.sh").chmod(0o755)
    (src / "deploy" / "hermes-release.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (src / "deploy" / "hermes-release.sh").chmod(0o755)
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "green")
    _tag(env, "release-sb2", f"multitenancy: {_git(src, 'rev-parse', 'HEAD')}\nwebui: {env['_webui_sha']}")
    assert _run({**env, "STABLE_BIN": str(stable)}).returncode == 0
    assert (stable / "hermes-release.sh").exists(), "成功后应把执行器同步到稳定路径"


def test_missing_backup_script_blocks_release(env):
    """缺了备份脚本就静默跳过 = 悄悄失去「不带备份不发布」这条保护。
    换机器或路径变动时最容易踩，必须拒绝。"""
    _tag(env, "release-nobk", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run({**env, "BACKUP_SH": "/nonexistent"})
    assert r.returncode != 0
    assert "不带备份不发布" in r.stderr


def test_dangling_rollback_target_is_refused_before_touching_anything(env):
    """回滚目标悬空时必须在动任何东西之前拒绝。否则新版本探针一失败，
    回滚会把两个对外路径指到不存在的目录上 —— 比不回滚还糟。"""
    shutil.rmtree(Path(env["RELEASES"]) / "mt-current")
    _tag(env, "release-dangle", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run(env)
    assert r.returncode != 0
    assert "悬空" in r.stderr
    assert not Path(env["SYSTEMCTL_LOG"]).exists(), "拒绝要发生在碰服务之前"


def test_installer_seeds_stable_bin_on_a_fresh_host(tmp_path):
    """鸡生蛋：单元跑 STABLE_BIN 下的副本，而那份平时只在发布成功后更新。
    全新机器上它不存在，第一次触发必然失败 —— 安装脚本负责把它种下去。"""
    stable = tmp_path / "stable"
    units = tmp_path / "units"
    r = subprocess.run(
        ["bash", str(DEPLOY / "install-hermes-release.sh")],
        env={"STABLE_BIN": str(stable), "UNIT_DIR": str(units),
             "SRC": str(DEPLOY), "HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    for f in ("hermes-release.sh", "hermes-release-probes.sh", "hermes_patch_probe.py"):
        assert (stable / f).exists(), f"{f} 没被种下"
        assert os.access(stable / f, os.X_OK)
    assert (units / "hermes-release.service").exists()
    # 幂等：再跑一次不该炸
    assert subprocess.run(
        ["bash", str(DEPLOY / "install-hermes-release.sh")],
        env={"STABLE_BIN": str(stable), "UNIT_DIR": str(units),
             "SRC": str(DEPLOY), "HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        capture_output=True,
    ).returncode == 0
