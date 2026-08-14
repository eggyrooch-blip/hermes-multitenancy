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
import re
import shutil
import sqlite3
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
        # mt 仓才带 relay 源码：relay 是唯一不走软链的组件，发布器要从这里拷。
        m = path / "hermes_multitenancy"
        m.mkdir(exist_ok=True)
        for f in ("agent_relay.py", "agent_relay_feishu.py", "agent_relay_store.py", "credentials.py"):
            (m / f).write_text(f"# NEW {f}\n")
        d = path / "deploy"
        d.mkdir(exist_ok=True)
        probe = d / "hermes-release-probes.sh"
        probe.write_text(f"#!/usr/bin/env bash\necho 'PROBES stub'\nexit {probe_exit}\n")
        probe.chmod(0o755)
        installer = d / "install-gateway-dropins.sh"
        installer.write_text("#!/usr/bin/env bash\nexit 0\n")
        installer.chmod(0o755)
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
    sqlite3.connect(home / ".hermes" / "multitenancy.db").close()

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
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"$@\" >> \"$SYSTEMCTL_LOG\"\n"
        'if [ "$1" = "start" ] && [ -f "$SYSTEMCTL_FAIL_NEXT_START" ]; then\n'
        '  rm -f "$SYSTEMCTL_FAIL_NEXT_START"\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
    )
    stub.chmod(0o755)

    # 桩 uv + venv python：editable 重装是每次切换的硬步骤（release-editable-reinstall）。
    # uv 桩记录最后一个参数（editable 目标目录）；venv 桩模拟「读回真实 import 路径」——
    # 与 uv 桩落盘的最后安装目标比对，一致才 0。UV_FAIL_FLAG 文件存在时 uv 装败。
    uv_stub = tmp_path / "uv-stub.sh"
    # 只有 `install -e <target>` 才更新 UV_STATE。reinstall_editable 里还会跑
    # `uv pip check -p <venv-python>`（依赖体检自愈，vod SDK 缺 19 天那次加的），
    # 它的最后一个参数是 venv python 而不是目标目录 —— 一起记就把 UV_STATE 冲成
    # python 路径，读回核对必然失败。这个桩没跟上，让整份文件红了 25 条。
    uv_stub.write_text(
        "#!/usr/bin/env bash\n"
        '[ -f "$UV_FAIL_FLAG" ] && exit 1\n'
        'case " $* " in *" -e "*)\n'
        '  echo "uv-install ${@: -1}" >> "$SYSTEMCTL_LOG"\n'
        '  echo "${@: -1}" > "$UV_STATE"\n'
        ";; esac\n"
        "exit 0\n"
    )
    uv_stub.chmod(0o755)
    venv_stub = tmp_path / "venv-python-stub.sh"
    venv_stub.write_text(
        "#!/usr/bin/env bash\n"
        "cat >/dev/null\n"  # 吞掉 heredoc stdin
        'if [ "$1" = "-" ]; then\n'
        '  [ "$(cat "$UV_STATE" 2>/dev/null)" = "$2" ] && exit 0 || exit 1\n'
        "fi\nexit 0\n"
    )
    venv_stub.chmod(0o755)

    # relay：假的 /opt 包目录 + 桩重启 + 桩探针。
    # 生产上这一步要 sudo，测试里全部换成可注入的桩，所以这套测试不需要任何权限。
    relay_pkg = tmp_path / "relay-pkg"
    relay_pkg.mkdir()
    for f in ("agent_relay.py", "agent_relay_feishu.py", "agent_relay_store.py", "credentials.py"):
        (relay_pkg / f).write_text(f"# OLD {f}\n")
    relay_restart = tmp_path / "relay-restart-stub.sh"
    relay_restart.write_text(
        "#!/usr/bin/env bash\n"
        'echo restart >> "$RELAY_LOG"\n'
        '[ -f "$RELAY_RESTART_FAIL" ] && exit 1\n'
        "exit 0\n"
    )
    relay_restart.chmod(0o755)
    relay_probe = tmp_path / "relay-probe-stub.sh"
    relay_probe.write_text(
        "#!/usr/bin/env bash\n"
        'cat "$RELAY_PROBE_CODE" 2>/dev/null || echo 401\n'
    )
    relay_probe.chmod(0o755)

    return {
        "RELAY_PKG_DIR": str(relay_pkg),
        "RELAY_RESTART": str(relay_restart),
        "RELAY_IS_ACTIVE": "true",
        "RELAY_PROBE_CMD": str(relay_probe),
        "RELAY_LOG": str(tmp_path / "relay.log"),
        "RELAY_RESTART_FAIL": str(tmp_path / "relay-restart-fail"),
        "RELAY_PROBE_CODE": str(tmp_path / "relay-probe-code"),
        "_relay_pkg": relay_pkg,
        "HOME": str(home), "RELEASES": str(releases), "CODE": str(code),
        "STATE_FILE": str(home / ".hermes" / "deployed-release"),
        "BACKUP_ROOT": str(home / "backups" / "pre-release"),
        "SESSION_DB": str(home / ".hermes" / "multitenancy.db"),
        "LOCK": str(home / ".hermes" / ".release.lock"),
        "SYSTEMCTL": str(stub), "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
        "BACKUP_SH": str(bstub),           # 备份本体另有测试覆盖，这里只要它存在
        "PROBES": str(DEPLOY / "hermes-release-probes.sh"),
        "UV_BIN": str(uv_stub), "VENV_PY": str(venv_stub),
        "UV_STATE": str(tmp_path / "uv-state"),
        "UV_FAIL_FLAG": str(tmp_path / "uv-fail-flag"),
        "SYSTEMCTL_FAIL_NEXT_START": str(tmp_path / "fail-next-start"),
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
    # 先走一次**真实发布**，让软链是发布器自己生成的那对，再验「已是最新」noop。
    # 原来这条直接手写 STATE_FILE、软链还停在 mt-current，等于在一个探针眼里
    # 「无法证明一致」的状态上断言 noop —— 探针上线后这个前提不再成立。
    _deploy_once(env, "release-x")
    before = _links(env)
    r = _run(env)
    assert r.returncode == 0
    assert "已是最新" in r.stdout
    assert _links(env) == before


# ── 1b. 漂移探针：软链被带外改过，必须有人知道 ────────────────────────
#
# 执行器认「状态文件里的标签名」，不认活的软链，所以带外部署（ssh 上去
# build + ln -sfn + restart）在构造上不可察觉。2026-08-04 生产 webui 就是
# 这么漂的。探针只报不改，fail-closed，且挡在所有分支之前。
#
# 这批测试的铁律：**基准状态必须由真实发布流程生成**，不许手搓目录名。
# 第一版手搓 mt-<8位>，而发布器实际生成的是 mt-<7位>（webui 才是 8 位），
# 于是探针在生产上永远判读不出 mt、整条空转，测试却全绿 —— codex 评审实测抓到。


ACK = ".drift-ack"


def _deploy_once(env, tag: str = "release-x") -> None:
    """跑一次真实发布，得到发布器自己生成的软链命名与状态文件。"""
    _tag(env, tag, f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run(env)
    assert r.returncode == 0, f"基准发布必须成功才能谈漂移：{r.stdout}\n{r.stderr}"
    assert Path(env["STATE_FILE"]).read_text().strip() == tag
    Path(env["SYSTEMCTL_LOG"]).unlink(missing_ok=True)


def _repoint(env, which: str, target: str) -> None:
    """把一条软链指到别的目录（目录会被建出来，模拟带外部署真的放了东西）。"""
    releases, code = Path(env["RELEASES"]), Path(env["CODE"])
    (releases / target).mkdir(parents=True, exist_ok=True)
    (code / which).unlink()
    (code / which).symlink_to(f"../releases/{target}")


def _fingerprint(r) -> str:
    """从脚本输出里抠出它让人写进 ack 文件的那一行指纹。

    顺带验证「照着提示复制粘贴」这条路真的走得通——提示词本身也是接口。"""
    m = re.search(r"printf '%s' '([^']*)'", r.stdout)
    assert m, f"输出里没有可复制的 ack 指纹：{r.stdout}"
    return m.group(1)


def test_links_from_a_real_deploy_are_a_clean_noop(env):
    """对得上就安静退出，不许因为多了探针就开始吵。

    同时把发布器真实的命名宽度钉住：mt 取 7 位、webui 取 8 位。这条不变量
    一旦改了，探针的解析必须跟着改，否则又变成空转。"""
    _deploy_once(env)
    mt_link, webui_link = _links(env)
    assert mt_link.endswith(f"mt-{env['_mt_sha'][:7]}"), mt_link
    assert webui_link.endswith(f"webui-{env['_webui_sha'][:8]}"), webui_link
    r = _run(env)
    assert r.returncode == 0
    assert "已是最新" in r.stdout
    assert "漂移" not in r.stdout


def test_webui_only_drift_is_caught(env):
    """只有一个仓被带外换掉也必须抓到（第一版会因为 mt 判读不出而整条跳过）。"""
    _deploy_once(env)
    _repoint(env, "hermes-web-ui", "webui-deadbeef")
    before = _links(env)
    r = _run(env)
    assert r.returncode == 1, r.stdout
    assert "发布漂移" in r.stdout
    assert "webui-deadbeef" in r.stdout
    assert _links(env) == before, "探针只报不改"
    drift_log = Path(env["HOME"]) / ".hermes" / "release-drift.log"
    assert drift_log.exists() and "发布漂移" in drift_log.read_text()


def test_mt_only_drift_is_caught(env):
    _deploy_once(env)
    _repoint(env, "hermes-multitenancy", "mt-dead123")
    r = _run(env)
    assert r.returncode == 1, r.stdout
    assert "mt-dead123" in r.stdout


def test_drift_blocks_a_new_release_too(env):
    """有新标签时也必须先挡住：带着未确认的漂移继续发布，会把别人手工部上去的
    止血补丁静默盖掉。"""
    _deploy_once(env, "release-x")
    _repoint(env, "hermes-web-ui", "webui-deadbeef")
    _tag(env, "release-y", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    before = _links(env)
    r = _run(env)
    assert r.returncode == 1, r.stdout
    assert "发布漂移" in r.stdout
    assert "发现新发布" not in r.stdout, "挡住了就不该再往下走发布流程"
    assert _links(env) == before
    assert Path(env["STATE_FILE"]).read_text().strip() == "release-x"


def test_dangling_link_is_not_mistaken_for_consistent(env):
    """readlink 对悬空软链照样回显目标名。只比名字会把「目标已被删」判成一致。"""
    _deploy_once(env)
    live = Path(env["CODE"], "hermes-web-ui").resolve()
    shutil.rmtree(live)
    r = _run(env)
    assert r.returncode == 1, r.stdout
    assert "悬空" in r.stdout


def test_missing_current_tag_is_unverifiable_not_clean(env):
    """CURRENT 指向的标签被删了 → 取不到期望值 → 无法证明一致，不许当没事。"""
    _deploy_once(env)
    # 注意 `git fetch --tags --prune` **不会**删本地已有的标签（那要 --prune-tags），
    # 所以源仓删掉还不够，克隆里也得删——否则脚本照样看得见它。
    _git(env["_mt_src"], "tag", "-d", "release-x")
    _git(Path(env["RELEASES"]) / ".repo-mt", "tag", "-d", "release-x")
    r = _run(env)
    assert r.returncode == 1, r.stdout
    assert "无法证明一致" in r.stdout


def test_unparseable_link_names_are_unverifiable_not_clean(env):
    """自定义目录名不能成为绕过探针的方法。想放行就显式 ack。

    代价写明：刚迁移完、软链还叫 mt-current 的新机器，第一次跑会告警一次，
    由迁移方 ack 一下。用「静默放行」换这点便利，等于给探针留一个后门。"""
    _deploy_once(env)
    _repoint(env, "hermes-multitenancy", "mt-current")
    r = _run(env)
    assert r.returncode == 1, r.stdout
    assert "无法证明一致" in r.stdout
    assert "mt-current" in r.stdout


def test_ack_silences_the_alert(env):
    """确认过的不再天天告警——否则终点是有人把定时器关了。"""
    _deploy_once(env)
    _repoint(env, "hermes-web-ui", "webui-deadbeef")
    first = _run(env)
    assert first.returncode == 1
    Path(env["STATE_FILE"] + ACK).write_text(_fingerprint(first))
    r = _run(env)
    assert r.returncode == 0, r.stdout
    assert "不再告警" in r.stdout


def test_ack_does_not_survive_drifting_somewhere_else(env):
    _deploy_once(env)
    _repoint(env, "hermes-web-ui", "webui-deadbeef")
    Path(env["STATE_FILE"] + ACK).write_text(_fingerprint(_run(env)))
    _repoint(env, "hermes-web-ui", "webui-cafebabe")
    r = _run(env)
    assert r.returncode == 1, r.stdout
    assert "webui-cafebabe" in r.stdout


def test_ack_does_not_survive_a_new_baseline_tag(env):
    """ack 绑死基准标签。发布到新标签后又漂回曾确认过的那一对，旧 ack 必须失效
    ——只绑 live 那一对的第一版会跨发布永久静默。"""
    _deploy_once(env, "release-x")
    _repoint(env, "hermes-web-ui", "webui-deadbeef")
    Path(env["STATE_FILE"] + ACK).write_text(_fingerprint(_run(env)))
    # 换个基准：状态文件改记 release-y（annotation 相同，只是标签名变了）
    _tag(env, "release-y", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    Path(env["STATE_FILE"]).write_text("release-y\n")
    r = _run(env)
    assert r.returncode == 1, "换了基准标签，旧 ack 不该再生效"


def test_ack_of_an_unreadable_tag_does_not_blind_the_probe(env):
    """标签读不到时，ack 只能确认「这一对软链」，不能变成一张空白通行证。

    评审 round-2 实测的洞：早期版本在标签读不到时根本不去读软链，指纹退化成
    `release-x ? ? <无> <无>`，ack 掉之后只要标签仍读不到，软链随便怎么换都
    静默放行。指纹里必须永远带真实的那一对。"""
    _deploy_once(env, "release-x")
    _git(env["_mt_src"], "tag", "-d", "release-x")
    _git(Path(env["RELEASES"]) / ".repo-mt", "tag", "-d", "release-x")

    first = _run(env)
    assert first.returncode == 1
    fp = _fingerprint(first)
    assert "<无>" not in fp, f"标签读不到也必须记下实际在跑的那一对：{fp}"
    Path(env["STATE_FILE"] + ACK).write_text(fp)
    assert _run(env).returncode == 0, "确认过的那一条应当放行"

    # 同一个「标签读不到」状态下换掉 webui 软链 —— 旧 ack 必须失效
    _repoint(env, "hermes-web-ui", "webui-deadbeef")
    r = _run(env)
    assert r.returncode == 1, f"ack 不该变成空白通行证：{r.stdout}"
    assert "webui-deadbeef" in r.stdout


def test_drift_probe_is_silent_before_the_first_deploy(env):
    """状态文件还不存在时无从比较，别对着空状态喊。

    必须真的有标签存在，否则脚本在「没有任何 release-* 标签」处就退了，
    根本走不到 CURRENT 为空那条分支——第一版就是这么空跑的。"""
    _tag(env, "release-x", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    assert not Path(env["STATE_FILE"]).exists()
    _repoint(env, "hermes-web-ui", "webui-deadbeef")
    r = _run(env)
    assert "漂移" not in r.stdout
    assert "无法证明一致" not in r.stdout
    assert "发现新发布" in r.stdout, "没有基准就不该挡住首次发布"


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


def test_release_reinstalls_editable_before_start(env):
    """翻软链不等于换 import（release-20260803-02/-03 两次实锤）：成功路径必须在
    stop 之后、start 之前把 editable 重装到新 mt 目录并读回核对。"""
    src = env["_mt_src"]
    (src / "extra.txt").write_text("new\n")
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "green")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-edi", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")

    r = _run(env)
    assert r.returncode == 0, r.stdout + r.stderr
    installed = Path(env["UV_STATE"]).read_text().strip()
    assert installed == str(Path(env["RELEASES"]) / f"mt-{sha[:7]}")
    lines = Path(env["SYSTEMCTL_LOG"]).read_text().splitlines()
    i_stop = max(i for i, l in enumerate(lines) if l.startswith("stop"))
    i_uv = next(i for i, l in enumerate(lines) if l.startswith("uv-install"))
    i_start = min(i for i, l in enumerate(lines) if l.startswith("start"))
    assert i_stop < i_uv < i_start, f"顺序必须 stop→重装→start：{lines}"


def test_release_installs_gateway_dropins_before_start(env):
    src = env["_mt_src"]
    installer = src / "deploy/install-gateway-dropins.sh"
    installer.write_text(
        '#!/usr/bin/env bash\n'
        '[ "$HERMES_MEEGLE_PREPARED" = "1" ] || exit 9\n'
        'echo dropin-install >> "$SYSTEMCTL_LOG"\n'
    )
    installer.chmod(0o755)
    (src / "extra-dropin.txt").write_text("new\n")
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "dropin")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-dropin", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    lines = Path(env["SYSTEMCTL_LOG"]).read_text().splitlines()
    assert lines.index("dropin-install") < min(
        i for i, line in enumerate(lines) if line.startswith("start")
    )


def test_probe_rollback_restores_previous_gateway_dropins(env):
    current = Path(env["RELEASES"]) / "mt-current/deploy"
    current.mkdir(parents=True, exist_ok=True)
    old_installer = current / "install-gateway-dropins.sh"
    old_installer.write_text('#!/usr/bin/env bash\necho old-dropin-install >> "$SYSTEMCTL_LOG"\n')
    old_installer.chmod(0o755)
    src = env["_mt_src"]
    new_installer = src / "deploy/install-gateway-dropins.sh"
    new_installer.write_text('#!/usr/bin/env bash\necho new-dropin-install >> "$SYSTEMCTL_LOG"\n')
    new_installer.chmod(0o755)
    (src / "deploy/hermes-release-probes.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
    (src / "deploy/hermes-release-probes.sh").chmod(0o755)
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "bad probe with dropin")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-dropinrb", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")

    r = _run(env)

    assert r.returncode != 0
    lines = Path(env["SYSTEMCTL_LOG"]).read_text().splitlines()
    assert lines.index("new-dropin-install") < lines.index("old-dropin-install")


def test_probe_rollback_missing_previous_dropin_installer_needs_human(env):
    old_installer = Path(env["RELEASES"]) / "mt-current/deploy/install-gateway-dropins.sh"
    old_installer.unlink()
    src = env["_mt_src"]
    (src / "deploy/hermes-release-probes.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
    (src / "deploy/hermes-release-probes.sh").chmod(0o755)
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "bad probe")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-missingoldinstaller", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")

    r = _run(env)

    assert r.returncode != 0
    rb = (Path(env["BACKUP_ROOT"]) / "release-missingoldinstaller" / "ROLLBACK.txt").read_text()
    assert "outcome=NEEDS_HUMAN" in rb
    assert "outcome=ROLLED_BACK" not in rb


def test_editable_reinstall_failure_rolls_back(env):
    """重装/读回失败 = 新版本没真生效，必须按切换失败处理：翻回、重启、记 outcome。"""
    src = env["_mt_src"]
    (src / "extra.txt").write_text("new\n")
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "green")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-edifail", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")
    Path(env["UV_FAIL_FLAG"]).write_text("boom\n")

    before = _links(env)
    r = _run(env)
    assert r.returncode != 0
    assert _links(env) == before, "editable 失败必须原样翻回上一版"
    assert not Path(env["STATE_FILE"]).exists()
    rb = (Path(env["BACKUP_ROOT"]) / "release-edifail" / "ROLLBACK.txt").read_text()
    assert "outcome=EDITABLE_FAILED" in rb
    assert "start" in Path(env["SYSTEMCTL_LOG"]).read_text(), "回滚后必须把服务拉起来"


def test_probe_rollback_reinstalls_editable_to_prev(env):
    """探针失败自动回滚时，editable 也必须跟着回到上一版目录 —— 否则软链回去了、
    import 还钉在坏版本上，回滚是假的。"""
    src = env["_mt_src"]
    (src / "deploy" / "hermes-release-probes.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
    (src / "deploy" / "hermes-release-probes.sh").chmod(0o755)
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "red probe")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-edirb", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")

    r = _run(env)
    assert r.returncode != 0
    installed = Path(env["UV_STATE"]).read_text().strip()
    prev = str((Path(env["RELEASES"]) / "mt-current").resolve())
    assert installed == prev, (
        f"回滚后 editable 最终必须指向上一版：installed={installed}"
    )


def test_gateway_start_failure_rolls_back_instead_of_running_probes_as_green(env):
    src = env["_mt_src"]
    (src / "extra.txt").write_text("new\n")
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "green probe")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-startfail", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")
    Path(env["SYSTEMCTL_FAIL_NEXT_START"]).write_text("fail once\n")

    before = _links(env)
    r = _run(env)

    assert r.returncode != 0
    assert _links(env) == before
    assert not Path(env["STATE_FILE"]).exists()
    rb = (Path(env["BACKUP_ROOT"]) / "release-startfail" / "ROLLBACK.txt").read_text()
    assert "outcome=ROLLED_BACK" in rb


def test_connector_prepare_failure_happens_before_service_stop(env):
    src = env["_mt_src"]
    prepare = src / "deploy/ensure-meegle.sh"
    prepare.write_text("#!/usr/bin/env bash\nexit 1\n")
    prepare.chmod(0o755)
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "bad connector")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-connectorfail", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")

    before = _links(env)
    r = _run(env)

    assert r.returncode != 0
    assert "未停止服务" in r.stderr
    assert _links(env) == before
    log = Path(env["SYSTEMCTL_LOG"])
    assert not log.exists() or "stop" not in log.read_text()


def test_session_write_lock_probe_failure_happens_before_service_stop(env, tmp_path):
    src = env["_mt_src"]
    (src / "extra-db-lock.txt").write_text("new\n")
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", "db lock")
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, "release-dblock", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")
    fake_bin = tmp_path / "fake-sqlite"
    fake_bin.mkdir()
    sqlite = fake_bin / "sqlite3"
    sqlite.write_text("#!/usr/bin/env bash\nexit 1\n")
    sqlite.chmod(0o755)

    before = _links(env)
    r = _run({**env, "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}"})

    assert r.returncode != 0
    assert "session DB 写锁探针失败" in r.stderr
    assert _links(env) == before
    log = Path(env["SYSTEMCTL_LOG"])
    assert not log.exists() or "stop" not in log.read_text()


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


def test_deploy_scripts_are_executable_in_git(): 
    """发布脚本在 git 里必须是 100755。

    2026-08-01 实弹踩到：它们被以 644 提交，worktree 检出后不可执行，
    执行器的「新版本必须自带可执行探针」守卫直接拒绝切换 —— 守卫是对的，
    但根因是打包缺陷。用测试守住，别再靠实弹发现。
    """
    import subprocess as sp
    repo = Path(__file__).resolve().parents[1]
    out = sp.run(["git", "-C", str(repo), "ls-files", "-s", "deploy/"],
                 capture_output=True, text=True).stdout
    modes = {line.split()[3]: line.split()[0] for line in out.splitlines() if line.strip()}
    for f in ("deploy/hermes-release.sh", "deploy/hermes-release-probes.sh",
              "deploy/hermes_patch_probe.py", "deploy/install-hermes-release.sh",
              "deploy/hermes-backup.sh", "deploy/hermes-restore-drill.sh"):
        assert modes.get(f) == "100755", f"{f} 在 git 里是 {modes.get(f)}，必须是 100755"


# ── 6. relay：唯一不走软链的组件，必须跟着发布一起动 ──────────────────
#
# 2026-08-13 合进 main 的四个 relay 提交在生产上整整两天没生效 —— 因为
# hermes-release.sh 里根本没有 relay。这几条守住它不再掉队。


def _relay_text(env, name: str) -> str:
    return (env["_relay_pkg"] / name).read_text()


def test_relay_files_synced_and_restarted_on_success(env):
    _deploy_once(env, "release-relay-1")
    for f in ("agent_relay.py", "agent_relay_feishu.py", "agent_relay_store.py", "credentials.py"):
        assert _relay_text(env, f) == f"# NEW {f}\n", f"{f} 没被同步到新版本"
    assert "restart" in Path(env["RELAY_LOG"]).read_text(), "relay 没被重启 = 换了文件也没生效"


def test_relay_not_touched_when_probes_fail(env):
    """探针失败要回滚 mt/webui —— relay 此时抢先升级就是反向漂移。"""
    mt_src = env["_mt_src"]
    (mt_src / "deploy" / "hermes-release-probes.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
    (mt_src / "deploy" / "hermes-release-probes.sh").chmod(0o755)
    _git(mt_src, "add", "-A")
    _git(mt_src, "commit", "-q", "-m", "bad probes")
    env["_mt_sha"] = _git(mt_src, "rev-parse", "HEAD")
    _tag(env, "release-relay-bad", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run(env)
    assert r.returncode != 0
    assert _relay_text(env, "agent_relay.py") == "# OLD agent_relay.py\n", "回滚的发布不该动 relay"
    assert not Path(env["RELAY_LOG"]).exists(), "回滚的发布不该重启 relay"


def test_relay_restart_failure_restores_and_reports(env):
    """缺 sudoers（sudo -n 当场失败）→ 还原 relay、记 RELAY_FAILED、非零退出，
    但 mt/webui 保持在新版本：不为一个独立进程让 1259 人再吃一次重启。"""
    Path(env["RELAY_RESTART_FAIL"]).write_text("x")
    _tag(env, "release-relay-2", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run(env)
    assert r.returncode != 0
    assert "RELAY FAILED" in r.stdout
    assert _relay_text(env, "agent_relay.py") == "# OLD agent_relay.py\n", "失败必须还原到发布前的字节"
    mt_link, webui_link = _links(env)
    assert env["_mt_sha"][:7] in mt_link, "relay 失败不该把 mt 连坐回滚"
    assert env["_webui_sha"][:8] in webui_link, "relay 失败不该把 webui 连坐回滚"
    snap = Path(env["BACKUP_ROOT"]) / "release-relay-2" / "ROLLBACK.txt"
    assert "outcome=RELAY_FAILED" in snap.read_text()


def test_relay_probe_failure_restores(env):
    """文件拷对了、进程也起来了，但路由没注册（404）—— 照样算没跟上。"""
    Path(env["RELAY_PROBE_CODE"]).write_text("404\n")
    _tag(env, "release-relay-3", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run(env)
    assert r.returncode != 0
    assert "存活探针未过" in r.stdout and "实得 404" in r.stdout
    assert _relay_text(env, "agent_relay.py") == "# OLD agent_relay.py\n"


def test_relay_file_list_is_globbed_not_hardcoded(env):
    """新增一个 relay 模块，不改发布器也要被拷过去。

    写死清单会静默漏拷单个模块 —— 比漏拷全部更难发现，正是本次要根治的类型。
    """
    mt_src = env["_mt_src"]
    (mt_src / "hermes_multitenancy" / "agent_relay_newthing.py").write_text("# NEW agent_relay_newthing.py\n")
    _git(mt_src, "add", "-A")
    _git(mt_src, "commit", "-q", "-m", "new relay module")
    env["_mt_sha"] = _git(mt_src, "rev-parse", "HEAD")
    _deploy_once(env, "release-relay-4")
    assert _relay_text(env, "agent_relay_newthing.py") == "# NEW agent_relay_newthing.py\n"
