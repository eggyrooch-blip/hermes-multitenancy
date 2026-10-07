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

import contextlib
import hashlib
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


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, env=env,
    ).stdout.strip()



# relay 桩文件正文。agent_relay.py 必须真的 import 其它模块：生产上 agent_relay.py:17
# 就是这样，预检 import 目标只有沿着这条链走下去才会撞到漏拷的依赖。
_RELAY_CORE = ("agent_relay.py", "agent_relay_feishu.py", "agent_relay_store.py", "credentials.py")


def _relay_body(tag: str, name: str) -> str:
    if name == "agent_relay.py":
        return f"# {tag} {name}\nfrom . import agent_relay_feishu, agent_relay_store, credentials\n"
    return f"# {tag} {name}\n"

WEBUI_LOCK = '{"name": "hermes-web-ui", "lockfileVersion": 3}\n'


def _make_repo(path: Path, with_probes: bool, probe_exit: int = 0, prebuilt: bool = False,
               manifests: bool = False) -> str:
    """造一个带 deploy/ 的假仓，返回 HEAD 的完整 sha。

    prebuilt=True 时预置 dist/server/index.js —— 脚本只在 dist 缺失时才跑
    npm ci + build，假仓里没有 package.json，预置它才能测到构建之后的分支。
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "README").write_text("x\n")
    if manifests:
        # 发布器要拿 checkout 里的 package-lock 去比对产物，webui 假仓必须有它
        (path / "package.json").write_text('{"name": "hermes-web-ui"}\n')
        (path / "package-lock.json").write_text(WEBUI_LOCK)
    if prebuilt:
        (path / "dist" / "server").mkdir(parents=True, exist_ok=True)
        (path / "dist" / "server" / "index.js").write_text("//\n")
    if with_probes:
        # mt 仓才带 relay 源码：relay 是唯一不走软链的组件，发布器要从这里拷。
        m = path / "hermes_multitenancy"
        m.mkdir(exist_ok=True)
        for f in _RELAY_CORE:
            (m / f).write_text(_relay_body("NEW", f))
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
def env(tmp_path: Path, request):
    """一整套假环境：两个 canonical 仓 + releases 目录 + code 软链 + 桩 systemctl。

    间接参数 `webui_prebuilt=False` 让 webui 假仓【不】预置 dist —— 产物那几条
    测试必须走到「dist 缺失」的那个分支上，否则测的是一条永远不会执行的路。
    """
    opts = getattr(request, "param", None) or {}
    home = tmp_path / "home"
    releases = home / "releases"
    code = home / "code"
    for d in (releases, code, home / ".hermes"):
        d.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(home / ".hermes" / "multitenancy.db").close()

    mt_src = tmp_path / "src-mt"
    webui_src = tmp_path / "src-webui"
    mt_sha = _make_repo(mt_src, with_probes=True)
    webui_sha = _make_repo(webui_src, with_probes=False, manifests=True,
                           prebuilt=opts.get("webui_prebuilt", True))

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
    # 生产上 gateway venv 此刻 import 的就是正在跑的那一版 MT（上一次发布装进去的），
    # core 也还是它配对的那个。配对判据读的就是这两条。
    (tmp_path / "uv-state").write_text(str(cur_mt.resolve()) + "\n")
    (tmp_path / "core-id").write_text(f"0.19.1 {tmp_path / 'core-v0191'}\n")
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
        # is-active 读回：默认都已停（输出 inactive、exit 3）；SYSTEMCTL_STILL_ACTIVE 存在 = stop 没停下来。
        # SYSTEMCTL_IS_ACTIVE 文件按行 `<unit> <rc> [输出…]` 覆盖单个 unit 的读回（D-Bus/权限故障等）。
        'if [ "$1" = "is-active" ]; then\n'
        '  u="${@: -1}"\n'
        '  if [ -f "$SYSTEMCTL_IS_ACTIVE" ]; then\n'
        '    while read -r name rc out; do\n'
        '      if [ "$name" = "$u" ]; then [ -n "$out" ] && echo "$out"; exit "$rc"; fi\n'
        '    done < "$SYSTEMCTL_IS_ACTIVE"\n'
        '  fi\n'
        '  if [ -f "$SYSTEMCTL_STILL_ACTIVE" ]; then echo active; exit 0; fi\n'
        '  echo inactive; exit 3\n'
        'fi\n'
        'if [ "$1" = "stop" ] && [ -f "$SYSTEMCTL_STILL_ACTIVE" ]; then exit 1; fi\n'
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
        '  [ -n "${CORE_SWAP_TO:-}" ] && [ -f "$CORE_SWAP_TO" ] && cp "$CORE_SWAP_TO" "$CORE_ID"\n'
        ";; esac\n"
        # pip install 不带 --no-deps = 依赖自愈重装（DEP_HEAL=0 时绝不能出现）
        'case " $* " in *" pip install "*) case " $* " in *" --no-deps "*) ;; *) echo "uv-heal" >> "$SYSTEMCTL_LOG"; rm -f "$UV_CHECK_FAIL" ;; esac ;; esac\n'
        'case " $* " in *" pip check "*) [ -f "$UV_CHECK_FAIL" ] && { echo "missing dep x" ; exit 1; } ;; esac\n'
        "exit 0\n"
    )
    uv_stub.chmod(0o755)
    venv_stub = tmp_path / "venv-python-stub.sh"
    venv_stub.write_text(
        "#!/usr/bin/env bash\n"
        # core_identity：打印「hermes-agent 版本 + core 真实路径」
        'if [ "$1" = "-c" ]; then cat "$CORE_ID" 2>/dev/null; exit 0; fi\n'
        "cat >/dev/null\n"  # 吞掉 heredoc stdin
        'if [ "$1" = "-" ]; then\n'
        '  [ "$(cat "$UV_STATE" 2>/dev/null)" = "$2" ] && exit 0 || exit 1\n'
        "fi\nexit 0\n"
    )
    venv_stub.chmod(0o755)

    # relay：假的 /opt 包目录 + 桩重启 + 桩探针。
    # 生产上这一步要 sudo，测试里全部换成可注入的桩，所以这套测试不需要任何权限。
    # 真实包布局：site/hermes_agent_relay_runtime/，让 RELAY_PY 能真的 import 它。
    relay_site = tmp_path / "relay-site"
    relay_pkg = relay_site / "hermes_agent_relay_runtime"
    relay_pkg.mkdir(parents=True)
    for f in _RELAY_CORE:
        (relay_pkg / f).write_text(_relay_body("OLD", f))
    relay_restart = tmp_path / "relay-restart-stub.sh"
    relay_restart.write_text(
        "#!/usr/bin/env bash\n"
        'echo restart >> "$RELAY_LOG"\n'
        '[ -f "$RELAY_RESTART_FAIL" ] && exit 1\n'
        "exit 0\n"
    )
    relay_restart.chmod(0o755)
    # 预检解释器：记一笔日志，然后把 -c 原样交给真 python3，PYTHONPATH 指向假 site。
    # 不模拟 import —— 生产上 2026-09-17 崩掉 relay 的就是真实 ModuleNotFoundError。
    relay_py = tmp_path / "relay-python.sh"
    relay_py.write_text(
        "#!/usr/bin/env bash\n"
        'echo preflight >> "$RELAY_LOG"\n'
        f'PYTHONPATH="{relay_site}" exec python3 "$@"\n'
    )
    relay_py.chmod(0o755)
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
        "RELAY_PY": str(relay_py),
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
        "UV_CHECK_FAIL": str(tmp_path / "uv-check-fail"),
        "CORE_ID": str(tmp_path / "core-id"),
        "SYSTEMCTL_FAIL_NEXT_START": str(tmp_path / "fail-next-start"),
        "SYSTEMCTL_STILL_ACTIVE": str(tmp_path / "still-active"),
        "SYSTEMCTL_IS_ACTIVE": str(tmp_path / "is-active-override"),
        "PATH": os.environ["PATH"],
        "_mt_sha": mt_sha, "_webui_sha": webui_sha,
        "_releases": releases, "_code": code, "_mt_src": mt_src,
    }


def _tag(env, name: str, body: str) -> None:
    src = env["_mt_src"]
    tag_env = {**os.environ, "GIT_COMMITTER_DATE": env["_tagger_date"]} if "_tagger_date" in env else None
    _git(src, "tag", "-a", name, "-m", body, env=tag_env)
    _git(Path(env["RELEASES"]) / ".repo-mt", "fetch", "-q", "--tags", "origin")


def _run(env) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in env.items() if not k.startswith("_")}
    return subprocess.run(["bash", str(RELEASE_SH)], env=clean, capture_output=True, text=True)


def _links(env):
    c = Path(env["CODE"])
    return os.readlink(c / "hermes-multitenancy"), os.readlink(c / "hermes-web-ui")


@pytest.mark.parametrize(("ss_mode", "returncode", "message"), [
    ("delayed", 0, "apiserver :8652 在听"),
    ("delayed_line_end", 0, "apiserver :8652 在听"),
    ("wrong_port", 1, "apiserver :8652 没在听（等了 10s）"),
    ("absent", 1, "apiserver :8652 没在听（等了 10s）"),
])
def test_apiserver_probe_polls_the_real_port_check(tmp_path: Path, ss_mode: str, returncode: int, message: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "ss-count"

    stubs = {
        "curl": '#!/usr/bin/env bash\ncase "$*" in *api/health*) printf 401;; *run-broker*) printf 401;; *) printf 200;; esac\n',
        "sleep": "#!/usr/bin/env bash\nexit 0\n",
        "ss": """#!/usr/bin/env bash
n=$(cat "$SS_STATE" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$SS_STATE"
printf 'State Recv-Q Send-Q Local Address:Port Peer Address:Port\\n'
[ "$SS_MODE" = delayed ] && [ "$n" -ge 2 ] && printf 'LISTEN 0 128 127.0.0.1:8652 0.0.0.0:*\\n'
[ "$SS_MODE" = delayed_line_end ] && [ "$n" -ge 2 ] && printf 'LISTEN 0 128 127.0.0.1:8652\\n'
[ "$SS_MODE" = wrong_port ] && printf 'LISTEN 0 128 127.0.0.1:86520 0.0.0.0:*\\n'
""",
    }
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)

    systemctl = tmp_path / "systemctl"
    systemctl.write_text('#!/usr/bin/env bash\n[ "$1" = is-active ] && printf active\nexit 0\n')
    systemctl.chmod(0o755)
    env = {
        **os.environ,
        "LC_ALL": "C",
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "HERMES_HOME_DIR": str(tmp_path / "hermes"),
        "HERMES_WEBUI_DIR": str(tmp_path / "webui"),
        "PROBE_PY": str(tmp_path / "missing-probe.py"),
        "SYSTEMCTL": str(systemctl),
        "USER_UNITS": "hermes-gateway.service",
        "BOOT_WAIT": "10",
        "SS_MODE": ss_mode,
        "SS_STATE": str(state),
    }

    result = subprocess.run(["bash", str(DEPLOY / "hermes-release-probes.sh")], env=env, capture_output=True, text=True)

    assert result.returncode == returncode
    assert message in result.stdout
    assert state.read_text().strip() == "2"


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
    env["_tagger_date"] = "2026-08-28T00:00:00+00:00"
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


def test_prune_keeps_pinned_and_config_referenced_rollback_deps(env):
    """unit 同款环境（KEEP_RELEASES=3、不设 KEEP_PRERELEASE_BACKUPS）连发多次：
    钉住清单里的目录、只被活配置引用的目录、core 目录和全部回滚包都不许删；
    既没钉住也没被引用的旧 mt-*/webui-* 照常裁掉。

    2026-09-24 core 0.21.4 切换：整体回退目标 mt-a8ea70e 不在「当前 + 上一版」里，
    router config.yaml 的 hermes-studio MCP 还写死指着 webui-0e3ad1b3，
    而回滚包默认上限 5 会把生产 134 份删到 5 份 —— 三处都违反「不删备份」。
    """
    env["_tagger_date"] = "2026-09-24T00:00:00+00:00"
    env.pop("KEEP_PRERELEASE_BACKUPS", None)
    env["KEEP_RELEASES"] = "3"
    home = Path(env["HOME"])
    releases = Path(env["RELEASES"])
    ancient = 1_600_000_000.0

    pinned = ["mt-a8ea70e", "mt-0c5c1d9", "mt-7f5847c", "webui-0e3ad1b3"]
    referenced = ["webui-5dd1e02", "webui-9ab0c11", "mt-4b2d9bb"]
    core = ["hermes-agent-v0191-20260801"]
    stale = ["mt-dead001", "webui-dead001"]
    # 未钉住的旧目录排最老：它们一定落在 KEEP_RELEASES 之外，必须被裁
    for i, name in enumerate(stale + pinned + referenced + core):
        d = releases / name
        (d / "bin").mkdir(parents=True)
        (d / "bin" / "hermes-web-ui-mcp.mjs").write_text("//\n")
        os.utime(d, (ancient + i, ancient + i))

    (releases / ".keep-pins").write_text(
        "# 整体回退链（2026-09-24 §5）\n"
        "mt-a8ea70e\n"
        "  mt-0c5c1d9  \n"
        "\n"
        "mt-7f5847c # 上一版生产\n"
        "webui-0e3ad1b3\n"
    )
    # router config.yaml：hermes-studio MCP 写死旧 webui 目录（风险表第 44 条）
    hermes_home = home / ".hermes"
    (hermes_home / "config.yaml").write_text(
        "mcp_servers:\n"
        "  hermes-studio:\n"
        "    command: node\n"
        f"    args: [\"{releases}/webui-5dd1e02/bin/hermes-web-ui-mcp.mjs\"]\n"
    )
    prof = hermes_home / "profiles" / "alice"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text(
        f"mcp_servers:\n  hermes-studio:\n    args: [{releases}/webui-9ab0c11/bin/hermes-web-ui-mcp.mjs]\n"
    )
    # gateway drop-in 里写死的 mt 目录（install-gateway-dropins 渲染的 45-meegle-bin.conf 形态）
    dropin = home / ".config" / "systemd" / "user" / "hermes-gateway@expert.service.d"
    dropin.mkdir(parents=True)
    (dropin / "45-meegle-bin.conf").write_text(
        f"[Service]\nEnvironment=MEEGLE_BIN={releases}/mt-4b2d9bb/node_modules/.bin/meegle\n"
    )
    # 一个读不了的配置不能让发布中止
    unreadable = hermes_home / "profiles" / "bob"
    unreadable.mkdir(parents=True)
    (unreadable / "config.yaml").write_text("x: 1\n")
    (unreadable / "config.yaml").chmod(0o000)

    backup_root = Path(env["BACKUP_ROOT"])
    _seed_backup_dirs(backup_root, [f"release-old{i:02d}" for i in range(10)],
                      base_mtime=ancient)

    src = env["_mt_src"]
    outs = []
    try:
        for i in range(4):
            (src / f"k{i}").write_text("x\n")
            _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", f"k{i}")
            sha = _git(src, "rev-parse", "HEAD")
            _tag(env, f"release-k{i}", f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")
            r = _run(env)
            assert r.returncode == 0, f"第 {i} 次发布应成功：{r.stdout}\n{r.stderr}"
            outs.append(r.stdout)
    finally:
        (unreadable / "config.yaml").chmod(0o644)

    log = "\n".join(outs)
    left = sorted(d.name for d in backup_root.iterdir() if d.name.startswith("release-"))
    for i in range(10):
        assert f"release-old{i:02d}" in left, f"回滚包被删了：{left}\n{log}"
    assert len(left) == 14, left  # 10 份历史 + 4 次发布各一份

    for name in pinned + referenced + core:
        assert (releases / name).is_dir(), f"{name} 被裁掉了 —— 回退/活配置依赖的目录不能删\n{log}"
    cur = os.readlink(Path(env["CODE"]) / "hermes-multitenancy")
    assert (releases / Path(cur).name).is_dir(), "当前版本目录必须还在"
    for name in stale:
        assert not (releases / name).exists(), f"{name} 既没钉住也没被引用，应当被裁\n{log}"
    assert "裁剪回滚包" not in log
    assert "未设 KEEP_PRERELEASE_BACKUPS，不裁回滚包" in log
    assert "保留 mt-a8ea70e（pinned）" in log
    assert "保留 webui-0e3ad1b3（pinned）" in log
    assert f"保留 webui-5dd1e02（referenced-by {hermes_home / 'config.yaml'}）" in log
    assert f"保留 mt-4b2d9bb（referenced-by {dropin / '45-meegle-bin.conf'}）" in log
    assert "裁剪旧版本 mt-dead001" in log
    assert "裁剪旧版本 webui-dead001" in log


# ── 5b. 发布前回滚包的保留规则 ───────────────────────────────────────
#
# hermes-backup.sh 自己的 prune() 相对 BACKUP_ROOT，而这条路径每个 tag 都把
# BACKUP_ROOT 顶成 $SNAP —— 于是它只在单个 tag 内裁剪，从不删旧 tag。
# 生产上因此堆到 133 份、22G（2026-09-21）。保留规则必须在发布器这一层。


def _seed_backup_dirs(root: Path, names: list[str], base_mtime: float) -> None:
    """按给定顺序造一批备份目录，mtime 递增（列表末尾最新）。"""
    root.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(names):
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "ROLLBACK.txt").write_text(f"tag={name}\n")
        os.utime(d, (base_mtime + i, base_mtime + i))


def test_prerelease_backups_capped_and_both_anchors_protected(env):
    """跑完一次发布后只剩 KEEP_PRERELEASE_BACKUPS 份，
    且当前 tag 与回滚锚点（此刻在跑的那个 tag）一定在里面。"""
    _deploy_once(env, "release-pr1")          # 这份成了回滚锚点（STATE_FILE 里的 tag）
    root = Path(env["BACKUP_ROOT"])
    assert (root / "release-pr1").is_dir()

    # 6 份历史包，全部比 release-pr1 旧
    old = [f"release-f{i}" for i in range(1, 7)]
    _seed_backup_dirs(root, old, base_mtime=(root / "release-pr1").stat().st_mtime - 10_000)

    _tag(env, "release-pr2", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run({**env, "KEEP_PRERELEASE_BACKUPS": "5"})
    assert r.returncode == 0, r.stdout + r.stderr

    left = sorted(d.name for d in root.iterdir() if d.is_dir())
    assert len(left) == 5, f"应当只剩 5 份：{left}"
    assert "release-pr2" in left, "当前 tag 的回滚包不能被自己裁掉"
    assert "release-pr1" in left, "回滚锚点被裁掉 = 出事时无路可退"
    # 删掉的必须是最老的那几份，留下的是最新的
    assert left == sorted(["release-pr1", "release-pr2", "release-f6", "release-f5", "release-f4"])
    assert "pre-release 备份保留 5 份，删除 3 份" in r.stdout


def test_prune_keeps_the_rollback_anchor_even_when_it_is_the_oldest(env):
    """回滚锚点就算是全目录里最老，也绝不能删；但它必须【占掉一个名额】，
    而不是在 keep_n 之外额外多留一份 —— 那样上限就形同虚设。"""
    _deploy_once(env, "release-pr1")
    root = Path(env["BACKUP_ROOT"])
    anchor = root / "release-pr1"
    ancient = 1_600_000_000.0
    os.utime(anchor, (ancient, ancient))          # 锚点变成全场最老的一份
    _seed_backup_dirs(root, [f"release-f{i}" for i in range(1, 7)], base_mtime=ancient + 1_000)

    _tag(env, "release-pr2", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run({**env, "KEEP_PRERELEASE_BACKUPS": "5"})
    assert r.returncode == 0, r.stdout + r.stderr

    left = sorted(d.name for d in root.iterdir() if d.is_dir())
    assert "release-pr1" in left, f"回滚锚点被 mtime 裁掉了：{left}"
    assert len(left) == 5, f"受保护的那份必须占名额，不能让总数涨到 6：{left}"
    # 两份受保护的先占 2 个名额，剩下 3 个按 mtime 给最新的三份普通目录
    assert left == sorted(["release-pr1", "release-pr2",
                           "release-f6", "release-f5", "release-f4"]), left
    assert "pre-release 备份保留 5 份，删除 3 份" in r.stdout


def test_prune_holds_the_cap_when_stale_dirs_carry_future_mtimes(env):
    """有目录的 mtime 在未来（时钟跳变、被 touch 过）时，当前 tag 的包不再排第一。
    上限和两个锚点都不许因此失守。"""
    import time

    _deploy_once(env, "release-pr1")
    root = Path(env["BACKUP_ROOT"])
    # 六份"来自未来"的历史包，全部排在当前 tag 和锚点前面
    _seed_backup_dirs(root, [f"release-f{i}" for i in range(1, 7)],
                      base_mtime=time.time() + 86_400)

    _tag(env, "release-pr2", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run({**env, "KEEP_PRERELEASE_BACKUPS": "5"})
    assert r.returncode == 0, r.stdout + r.stderr

    left = sorted(d.name for d in root.iterdir() if d.is_dir())
    assert len(left) == 5, f"未来时间戳把上限顶破了：{left}"
    assert left == sorted(["release-pr1", "release-pr2",
                           "release-f6", "release-f5", "release-f4"]), left
    assert "pre-release 备份保留 5 份，删除 3 份" in r.stdout


def test_prune_with_cap_below_the_protected_count_keeps_only_the_anchors(env):
    """名额比受保护的还少时保护优先，但必须把这个例外打在日志里 ——
    否则下次有人盯着"保留 1 份"却看到 2 份，又得去翻代码。"""
    _deploy_once(env, "release-pr1")
    root = Path(env["BACKUP_ROOT"])
    _seed_backup_dirs(root, ["release-f1", "release-f2", "release-f3"],
                      base_mtime=1_600_000_000.0)

    _tag(env, "release-pr2", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run({**env, "KEEP_PRERELEASE_BACKUPS": "1"})
    assert r.returncode == 0, r.stdout + r.stderr

    left = sorted(d.name for d in root.iterdir() if d.is_dir())
    assert left == ["release-pr1", "release-pr2"], left
    assert ("KEEP_PRERELEASE_BACKUPS=1 小于受保护的 2 份（当前 tag + 回滚锚点）"
            "—— 只留受保护的这 2 份") in r.stdout
    assert "pre-release 备份保留 2 份，删除 3 份" in r.stdout


def test_prune_never_touches_a_non_release_directory(env):
    """备份根目录下不叫 release-* 的东西一律不动 —— 误删一次就是删掉唯一的退路。"""
    root = Path(env["BACKUP_ROOT"])
    _seed_backup_dirs(root, ["not-a-release", "manual-copy-20260921", "release-ancient"],
                      base_mtime=1_700_000_000.0)

    _tag(env, "release-pr9", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run({**env, "KEEP_PRERELEASE_BACKUPS": "1"})
    assert r.returncode == 0, r.stdout + r.stderr

    left = sorted(d.name for d in root.iterdir() if d.is_dir())
    assert left == ["manual-copy-20260921", "not-a-release", "release-pr9"], left
    assert "保留 not-a-release（不是 release-* 目录，不裁）" in r.stdout


def test_release_unit_does_not_cap_prerelease_backups():
    """「不删备份」是红线：单元不能替 sunke 默认打开回滚包上限，要裁必须显式设。"""
    unit = (DEPLOY / "hermes-release.service").read_text()
    assert not re.search(r"^Environment=KEEP_PRERELEASE_BACKUPS=", unit, re.M), unit
    assert "Environment=KEEP_RELEASES=3" in unit


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
    env["_tagger_date"] = "2026-08-28T00:00:00+00:00"
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
    for f in _RELAY_CORE:
        assert _relay_text(env, f) == _relay_body("NEW", f), f"{f} 没被同步到新版本"
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
    assert _relay_text(env, "agent_relay.py") == _relay_body("OLD", "agent_relay.py"), "回滚的发布不该动 relay"
    assert not Path(env["RELAY_LOG"]).exists(), "回滚的发布不该重启 relay"


def test_relay_restart_failure_restores_and_reports(env):
    """缺 sudoers（sudo -n 当场失败）→ 还原 relay、记 RELAY_FAILED、非零退出，
    但 mt/webui 保持在新版本：不为一个独立进程让 1259 人再吃一次重启。"""
    Path(env["RELAY_RESTART_FAIL"]).write_text("x")
    _tag(env, "release-relay-2", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run(env)
    assert r.returncode != 0
    assert "RELAY FAILED" in r.stdout
    assert _relay_text(env, "agent_relay.py") == _relay_body("OLD", "agent_relay.py"), "失败必须还原到发布前的字节"
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
    assert _relay_text(env, "agent_relay.py") == _relay_body("OLD", "agent_relay.py")


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


def _commit_relay_src(env, files: dict, msg: str) -> None:
    mt_src = env["_mt_src"]
    m = mt_src / "hermes_multitenancy"
    for name, body in files.items():
        (m / name).write_text(body)
    _git(mt_src, "add", "-A")
    _git(mt_src, "commit", "-q", "-m", msg)
    env["_mt_sha"] = _git(mt_src, "rev-parse", "HEAD")


def _relay_pkg_names(env) -> set[str]:
    return {p.name for p in env["_relay_pkg"].iterdir() if p.suffix == ".py"}


def test_relay_import_preflight_fails_closed_without_interpreter(env):
    """解释器路径失配 ≠ 免检。放行会把本次要拦的启动崩溃原样放回去。"""
    env["RELAY_PY"] = str(Path(env["RELAY_PY"]).parent / "does-not-exist")
    _tag(env, "release-relay-7", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run(env)
    assert r.returncode != 0
    assert "relay 解释器不存在或不可执行" in r.stdout
    assert not Path(env["RELAY_LOG"]).exists(), "解释器都没有，绝不能 restart"
    assert _relay_text(env, "agent_relay.py") == _relay_body("OLD", "agent_relay.py")


def test_relay_file_list_follows_indented_lazy_imports(env):
    """函数体里的延迟 import 也是依赖：顶层 import 过了，第一次走到那个函数才炸。"""
    _commit_relay_src(env, {
        "agent_relay_feishu.py": "# NEW agent_relay_feishu.py\ndef handler():\n    from .lazy_dep import value\n    return value\n",
        "lazy_dep.py": "# NEW lazy_dep.py\nvalue = 1\n",
    }, "lazy import")
    _deploy_once(env, "release-relay-8")
    assert _relay_text(env, "lazy_dep.py") == "# NEW lazy_dep.py\nvalue = 1\n", "缩进的相对 import 没被收进清单"


def test_relay_preflight_failure_removes_newly_created_files(env):
    """首次带进来的模块，在预检失败时必须删掉，不能留一个失败版本的残件。"""
    before = _relay_pkg_names(env)
    _commit_relay_src(env, {
        "agent_relay_store.py": "# NEW agent_relay_store.py\nfrom .shared_db import connect_shared\nfrom .ghost_module import x\n",
        "shared_db.py": "# NEW shared_db.py\nconnect_shared = None\n",
    }, "new dep + ghost")
    _tag(env, "release-relay-9", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run(env)
    assert r.returncode != 0 and "relay import 预检失败" in r.stdout
    assert _relay_pkg_names(env) == before, f"还原后包里多了文件: {_relay_pkg_names(env) - before}"
    assert _relay_text(env, "agent_relay_store.py") == _relay_body("OLD", "agent_relay_store.py")


def test_relay_preflight_failure_never_restarts_even_when_service_looks_down(env):
    """RELAY_IS_ACTIVE 说不活跃（activating / 查询失败）也不能借还原之名重启。"""
    env["RELAY_IS_ACTIVE"] = "false"
    _commit_relay_src(env, {
        "agent_relay_store.py": "# NEW agent_relay_store.py\nfrom .ghost_module import x\n",
    }, "ghost")
    _tag(env, "release-relay-10", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run(env)
    assert r.returncode != 0
    log = Path(env["RELAY_LOG"]).read_text() if Path(env["RELAY_LOG"]).exists() else ""
    assert "restart" not in log, "预检失败 + 服务看着不活跃，也不能 restart"


def test_relay_file_list_follows_relative_imports(env):
    """agent_relay_store 新 import 一个不叫 agent_relay_* 的模块，它也得被拷过去。

    2026-09-17 release-20260917-02：shared_db.py 正是这样漏掉的 —— 通配只看文件名，
    看不到 import。清单必须沿 `from .xxx import` 收敛。
    """
    mt_src = env["_mt_src"]
    m = mt_src / "hermes_multitenancy"
    (m / "agent_relay_store.py").write_text("# NEW agent_relay_store.py\nfrom .shared_db import connect_shared\n")
    (m / "shared_db.py").write_text("# NEW shared_db.py\nconnect_shared = None\n")
    _git(mt_src, "add", "-A")
    _git(mt_src, "commit", "-q", "-m", "store imports shared_db")
    env["_mt_sha"] = _git(mt_src, "rev-parse", "HEAD")
    _deploy_once(env, "release-relay-5")
    assert _relay_text(env, "shared_db.py") == "# NEW shared_db.py\nconnect_shared = None\n", "被 import 的模块没被同步"
    log = Path(env["RELAY_LOG"]).read_text()
    assert log.index("preflight") < log.index("restart"), "预检必须发生在重启之前"


def test_relay_import_preflight_blocks_restart_and_restores(env):
    """新代码 import 一个清单里根本没有的模块 → 老进程不能被重启去撞这个坑。"""
    mt_src = env["_mt_src"]
    m = mt_src / "hermes_multitenancy"
    # 引用一个源目录里不存在的模块：清单收敛不到它，预检里必然 ModuleNotFoundError
    (m / "agent_relay_store.py").write_text("# NEW agent_relay_store.py\nfrom .ghost_module import x\n")
    _git(mt_src, "add", "-A")
    _git(mt_src, "commit", "-q", "-m", "store imports a ghost")
    env["_mt_sha"] = _git(mt_src, "rev-parse", "HEAD")
    _tag(env, "release-relay-6", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")
    r = _run(env)
    assert r.returncode != 0
    assert "relay import 预检失败" in r.stdout and "ghost_module" in r.stdout
    assert "RELAY FAILED" in r.stdout
    log = Path(env["RELAY_LOG"]).read_text()
    assert "restart" not in log, "预检失败后绝不能重启 relay —— 老进程还活着就是最好的状态"
    assert _relay_text(env, "agent_relay_store.py") == _relay_body("OLD", "agent_relay_store.py"), "预检失败必须还原到发布前的字节"
    mt_link, _ = _links(env)
    assert env["_mt_sha"][:7] in mt_link, "relay 预检失败不该把 mt 连坐回滚"


def test_harness_pilot_check_requires_pinned_clean_source_and_codex(tmp_path: Path):
    source = tmp_path / "source"
    rev = _make_repo(source, with_probes=False)
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\necho codex-cli 0.150.1\n")
    codex.chmod(0o755)
    systemctl = tmp_path / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)
    ready = tmp_path / "harness.ready"
    ready.write_text(f"{rev}\n")
    env_file = tmp_path / "harness.env"
    env_file.write_text(
        "\n".join([
            "HERMES_WEBUI_HARNESS_ENABLED=1",
            "HERMES_WEBUI_HARNESS_PROFILES=sunke",
            f"HERMES_WEBUI_HARNESS_SOURCE_REV={rev}",
        ]) + "\n"
    )
    run_env = {
        **os.environ,
        "HARNESS_PLATFORM": "Linux",
        "HARNESS_SOURCE_DIR": str(source),
        "HARNESS_CODEX_BIN": str(codex),
        "HARNESS_ENV_FILE": str(env_file),
        "HARNESS_READY_FILE": str(ready),
        "SYSTEMCTL": str(systemctl),
    }

    ok = subprocess.run(
        [str(DEPLOY / "prepare-harness-pilot.sh"), "--check"],
        capture_output=True, text=True, env=run_env,
    )
    assert ok.returncode == 0, ok.stderr

    (source / "drift").write_text("x\n")
    drift = subprocess.run(
        [str(DEPLOY / "prepare-harness-pilot.sh"), "--check"],
        capture_output=True, text=True, env=run_env,
    )
    assert drift.returncode != 0
    assert "source repository is dirty" in drift.stderr


def test_harness_pilot_default_codex_root_is_inside_shared_runtime(tmp_path: Path):
    source = tmp_path / "source"
    rev = _make_repo(source, with_probes=False)
    home = tmp_path / "home"
    codex = (
        home
        / ".hermes/bin/hermes-codex-0.150.1/node_modules/.bin/codex"
    )
    codex.parent.mkdir(parents=True)
    codex.write_text("#!/bin/sh\necho codex-cli 0.150.1\n")
    codex.chmod(0o755)
    systemctl = tmp_path / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)
    ready = tmp_path / "harness.ready"
    ready.write_text(f"{rev}\n")
    env_file = tmp_path / "harness.env"
    env_file.write_text(
        "\n".join([
            "HERMES_WEBUI_HARNESS_ENABLED=1",
            "HERMES_WEBUI_HARNESS_PROFILES=sunke",
            f"HERMES_WEBUI_HARNESS_SOURCE_REV={rev}",
        ]) + "\n"
    )

    result = subprocess.run(
        [str(DEPLOY / "prepare-harness-pilot.sh"), "--check"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "HARNESS_PLATFORM": "Linux",
            "HARNESS_SOURCE_DIR": str(source),
            "HARNESS_ENV_FILE": str(env_file),
            "HARNESS_READY_FILE": str(ready),
            "SYSTEMCTL": str(systemctl),
        },
    )

    assert result.returncode == 0, result.stderr


# ── 7. webui 产物：CI 构建好，生产只解压 ──────────────────────────────
#
# 2026-09-21 事故：生产机上 npm ci + build 跑了 9.5 分钟，机械盘 IO 被拖死，
# 1259 个人干什么都卡。构建是纯函数，没理由发生在这台机器上。这批测试守的是
# 「有产物就绝不构建」和「产物有任何不对就必须退回构建、而不是发布半个 dist」。

ARTIFACT_TOKEN = "deploy-token-stub"  # 假 token，只是为了让脚本走进下载分支


def _make_artifact(tmp_path: Path, sha: str, *, with_node_pty: bool = True,
                   digest_override: str | None = None, lock_body: str = WEBUI_LOCK,
                   extra_members: dict[str, str] | None = None,
                   escaping_symlink: bool = False,
                   sidecar_body: str | None = None) -> Path:
    """造一份 CI 侧产物（tar 根含 dist/ node_modules/ package.json package-lock.json ARTIFACT.json）。

    gzip 不是选择题：生产 hermes-1 上没有 zstd 二进制，只有 GNU tar + gzip，
    所以这里压的、脚本解的、CI 打的必须是同一种 —— 真压真解，没有桩。

    默认带一条 node_modules/.bin/tsc -> ../typescript/bin/tsc：npm ci 出来的
    node_modules 全是这种带 `..` 的合法软链，校验不能把它们当越界给拒了。
    """
    pkg = tmp_path / "pkgreg"
    pkg.mkdir(exist_ok=True)
    stage = tmp_path / f"stage-{sha[:8]}"
    (stage / "dist" / "server").mkdir(parents=True, exist_ok=True)
    (stage / "dist" / "server" / "index.js").write_text("// from artifact\n")
    (stage / "package.json").write_text('{"name": "hermes-web-ui"}\n')
    (stage / "package-lock.json").write_text(lock_body)
    (stage / "ARTIFACT.json").write_text('{"sha": "%s"}\n' % sha)
    if with_node_pty:
        (stage / "node_modules" / "node-pty").mkdir(parents=True, exist_ok=True)
        (stage / "node_modules" / "node-pty" / "package.json").write_text("{}\n")
        tsc = stage / "node_modules" / "typescript" / "bin"
        tsc.mkdir(parents=True, exist_ok=True)
        (tsc / "tsc").write_text("#!/usr/bin/env node\n")
        binv = stage / "node_modules" / ".bin"
        binv.mkdir(parents=True, exist_ok=True)
        link = binv / "tsc"
        if not link.is_symlink():
            link.symlink_to("../typescript/bin/tsc")
    if escaping_symlink:
        evil = stage / "node_modules" / "evil"
        if not evil.is_symlink():
            evil.symlink_to("../../../../etc")
    for rel, body in (extra_members or {}).items():
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)

    tarball = pkg / f"webui-{sha}.tar.gz"
    subprocess.run(["tar", "-czf", str(tarball), "-C", str(stage), "."], check=True)
    digest = digest_override or hashlib.sha256(tarball.read_bytes()).hexdigest()
    (pkg / f"webui-{sha}.sha256").write_text(
        sidecar_body if sidecar_body is not None else f"{digest}  webui-{sha}.tar.gz\n"
    )
    return pkg


def _artifact_stubs(env, tmp_path: Path, *, token: str | None = ARTIFACT_TOKEN,
                    env_mode: int = 0o600) -> None:
    """桩 curl（从本地包目录取文件）+ 桩 npm（记调用并造 dist）+ release.env。"""
    bin_dir = tmp_path / "artifact-bin"
    bin_dir.mkdir(exist_ok=True)

    # 桩 curl 要同时演两个角色：
    #   第一跳 = 带 token（从 stdin 的 config 里读）问包仓库，必须回 http_code；
    #   第二跳 = 不带任何 auth 去取预签名 URL（只有 REDIRECT_TO 非空时才发生）。
    # 每次调用的完整 argv 都落盘，测试据此断言 token 一次都没进过命令行。
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'argv="$*"\n'
        'out=""; url=""; writefmt=""; used_config=0\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    -o) out="$2"; shift 2;;\n'
        '    -w) writefmt="$2"; shift 2;;\n'
        "    --config|-K) used_config=1; shift 2;;\n"
        "    --header|-H|--retry|--max-time) shift 2;;\n"
        "    -*) shift;;\n"
        '    *) url="$1"; shift;;\n'
        "  esac\n"
        "done\n"
        'printf "ARGV %s\\n" "$argv" >> "$CURL_ARGV_LOG"\n'
        # config 只在第一跳出现。必须把 stdin 读干净，否则喂它的 printf 吃 SIGPIPE。
        'if [ "$used_config" = 1 ]; then\n'
        '  cfg=$(cat)\n'
        '  printf "%s\\n" "$cfg" >> "$CURL_CONFIG_LOG"\n'
        '  case "$cfg" in *"DEPLOY-TOKEN: "?*) echo deploy-token-in-config >> "$CURL_LOG";; esac\n'
        "fi\n"
        'echo "GET $url" >> "$CURL_LOG"\n'
        # 预签名 URL 带查询串，取文件名时要把它切掉
        'name="${url##*/}"; name="${name%%\\?*}"\n'
        'already_redirected=0\n'
        'case "$url" in "${REDIRECT_TO:-__none__}"*) already_redirected=1;; esac\n'
        'if [ "$already_redirected" = 0 ] && [ -n "${REDIRECT_TO:-}" ]; then\n'
        '  printf "<html>moved</html>" > "$out"\n'   # 3xx 的响应体必须被丢掉
        '  printf "302 %s/%s?sig=presigned-not-a-token\\n" "$REDIRECT_TO" "$name"\n'
        "  exit 0\n"
        "fi\n"
        'if [ ! -f "$PKG_DIR/$name" ]; then\n'
        '  if [ -n "$writefmt" ]; then printf "404 \\n"; exit 0; fi\n'   # 第一跳没有 --fail
        '  echo "curl: (22) The requested URL returned error: 404" >&2\n'
        "  exit 22\n"
        "fi\n"
        'cp "$PKG_DIR/$name" "$out"\n'
        '[ -n "$writefmt" ] && printf "200 \\n"\n'
        "exit 0\n"
    )
    curl.chmod(0o755)

    npm = bin_dir / "npm"
    npm.write_text(
        "#!/usr/bin/env bash\n"
        # 退回原地构建时，npm 读到的 package-lock 必须还是 checkout 里的那份
        "lock=none\n"
        "if [ -f package-lock.json ]; then\n"
        "  if command -v sha256sum >/dev/null 2>&1; then lock=$(sha256sum package-lock.json | cut -d' ' -f1)\n"
        "  else lock=$(shasum -a 256 package-lock.json | cut -d' ' -f1); fi\n"
        "fi\n"
        'echo "npm $1 lock=$lock" >> "$NPM_LOG"\n'
        'if [ "$1" = run ]; then mkdir -p dist/server; printf "// built on host\\n" > dist/server/index.js; fi\n'
        "exit 0\n"
    )
    npm.chmod(0o755)

    if token is not None:
        release_env = Path(env["HOME"]) / ".hermes" / "release.env"
        release_env.write_text(f"HERMES_RELEASE_PKG_TOKEN={token}\n")
        release_env.chmod(env_mode)

    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["PKG_DIR"] = str(tmp_path / "pkgreg")
    env["CURL_LOG"] = str(tmp_path / "curl.log")
    env["CURL_ARGV_LOG"] = str(tmp_path / "curl-argv.log")
    env["CURL_CONFIG_LOG"] = str(tmp_path / "curl-config.log")
    env["NPM_LOG"] = str(tmp_path / "npm.log")
    env["WEBUI_PKG_BASE"] = "https://pkg.invalid/api/v4/projects/2829/packages/generic/webui-release"


def _artifact_leftovers(env) -> list[Path]:
    return sorted((Path(env["RELEASES"]) / ".artifacts").glob("webui-*"))


def _npm_calls(env) -> str:
    log = Path(env["NPM_LOG"])
    return log.read_text() if log.exists() else ""


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_artifact_present_skips_the_on_host_build(env, tmp_path):
    """有产物就绝不在生产机上构建 —— 这是整张单的完成线。"""
    sha = env["_webui_sha"]
    _make_artifact(tmp_path, sha)
    _artifact_stubs(env, tmp_path)
    _tag(env, "release-artifact", f"multitenancy: {env['_mt_sha']}\nwebui: {sha}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert f"webui 产物 {sha[:8]} 下载校验通过，跳过 npm ci/build" in r.stdout
    assert "含 npm ci + build" not in r.stdout
    assert _npm_calls(env) == "", "产物就位还去构建 = 事故没修"

    webui_dir = Path(env["RELEASES"]) / f"webui-{sha[:8]}"
    assert (webui_dir / "dist" / "server" / "index.js").read_text() == "// from artifact\n"
    assert (webui_dir / "node_modules" / "node-pty").exists(), "生产依赖也得跟着产物来"
    # token 只能出现在 header 里，不许落进任何日志
    curl_log = Path(env["CURL_LOG"]).read_text()
    assert "deploy-token-in-config" in curl_log
    assert ARTIFACT_TOKEN not in curl_log
    assert ARTIFACT_TOKEN not in Path(env["CURL_ARGV_LOG"]).read_text()
    assert ARTIFACT_TOKEN not in r.stdout and ARTIFACT_TOKEN not in r.stderr
    # 几百兆的包不留在盘上
    assert _artifact_leftovers(env) == []


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_missing_artifact_falls_back_to_the_on_host_build(env, tmp_path):
    """包仓库里没有（404）只准让发布变慢，不准让发布失败。"""
    (tmp_path / "pkgreg").mkdir()  # 空包仓库
    _artifact_stubs(env, tmp_path)
    _tag(env, "release-nopkg", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "ARTIFACT MISSING" in r.stdout
    assert "退回原地构建（慢）" in r.stdout
    assert "原地构建 webui（含 npm ci + build，要几分钟）" in r.stdout
    assert "npm ci" in _npm_calls(env) and "npm run" in _npm_calls(env)
    assert _artifact_leftovers(env) == [], "取失败不留半截文件"


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_checksum_mismatch_deletes_the_download_and_builds(env, tmp_path):
    """校验不过 = 当没拿到。半个产物比没有产物更危险，删干净再退回构建。"""
    sha = env["_webui_sha"]
    _make_artifact(tmp_path, sha, digest_override="0" * 64)
    _artifact_stubs(env, tmp_path)
    _tag(env, "release-badsum", f"multitenancy: {env['_mt_sha']}\nwebui: {sha}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert f"ARTIFACT CHECKSUM MISMATCH: {sha[:8]}" in r.stdout
    assert _artifact_leftovers(env) == [], "校验不过的文件必须删掉"
    assert "npm ci" in _npm_calls(env)
    webui_dir = Path(env["RELEASES"]) / f"webui-{sha[:8]}"
    assert (webui_dir / "dist" / "server" / "index.js").read_text() == "// built on host\n"


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_loose_release_env_permissions_refuse_the_artifact(env, tmp_path):
    """token 文件权限松了就拒用产物 —— 否则「chmod 644」就是把 token 借给同机其他用户的办法。"""
    sha = env["_webui_sha"]
    _make_artifact(tmp_path, sha)
    _artifact_stubs(env, tmp_path, env_mode=0o644)
    _tag(env, "release-openenv-pkg", f"multitenancy: {env['_mt_sha']}\nwebui: {sha}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "ARTIFACT SKIPPED" in r.stdout and "必须是 600" in r.stdout
    assert not Path(env["CURL_LOG"]).exists(), "权限没过就不该发起下载"
    assert "npm ci" in _npm_calls(env)


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_no_token_is_a_loud_skip_not_a_failure(env, tmp_path):
    """生产上 token 还没配好时（本单 ship 的那一刻就是这个状态）发布必须照常成功。"""
    _make_artifact(tmp_path, env["_webui_sha"])
    _artifact_stubs(env, tmp_path, token=None)
    _tag(env, "release-notoken", f"multitenancy: {env['_mt_sha']}\nwebui: {env['_webui_sha']}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "ARTIFACT SKIPPED: no HERMES_RELEASE_PKG_TOKEN" in r.stdout
    assert not Path(env["CURL_LOG"]).exists()
    assert "npm ci" in _npm_calls(env)


def test_unit_loads_the_release_env_file():
    """产物 token 靠 unit 的 EnvironmentFile 进环境；`-` 前缀保证没有它也能启动。"""
    unit = (DEPLOY / "hermes-release.service").read_text()
    assert "EnvironmentFile=-%h/.hermes/release.env" in unit
    assert "read_package_registry" in unit, "怎么造 token 要写在用到它的地方"


def _curl_argv(env) -> list[str]:
    log = Path(env["CURL_ARGV_LOG"])
    return [l for l in log.read_text().splitlines() if l.strip()] if log.exists() else []


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_token_never_reaches_the_curl_command_line(env, tmp_path):
    """token 进 argv = 同机任何用户一条 ps 就读走它。只许从 stdin 的 config 进去。"""
    sha = env["_webui_sha"]
    _make_artifact(tmp_path, sha)
    _artifact_stubs(env, tmp_path)
    _tag(env, "release-argv", f"multitenancy: {env['_mt_sha']}\nwebui: {sha}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    argv = _curl_argv(env)
    assert argv, "下载根本没发生，这条测试就没在测东西"
    for line in argv:
        assert ARTIFACT_TOKEN not in line, f"token 进了命令行：{line}"
        assert "DEPLOY-TOKEN" not in line, f"header 进了命令行：{line}"
        assert "--config -" in line, f"token 没走 stdin 的 config：{line}"
    # 真的送到了：config 内容里有完整的 header 行
    assert f'header = "DEPLOY-TOKEN: {ARTIFACT_TOKEN}"' in Path(env["CURL_CONFIG_LOG"]).read_text()


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_redirect_to_object_storage_drops_the_token(env, tmp_path):
    """GitLab 把下载 302 到对象存储时，第二跳绝不能带着 deploy token 去。

    这就是不用 `-L` 的全部理由：curl 跟随重定向会把 --config 里的 header
    原样带到新域名，等于把 token 交给 S3。两跳分开走才拿得住这条。
    """
    sha = env["_webui_sha"]
    _make_artifact(tmp_path, sha)
    _artifact_stubs(env, tmp_path)
    env["REDIRECT_TO"] = "https://objstore.invalid/presigned"
    _tag(env, "release-redirect", f"multitenancy: {env['_mt_sha']}\nwebui: {sha}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    # 产物照样拿到了：302 的响应体被丢掉，第二跳取回的才是真包
    assert f"webui 产物 {sha[:8]} 下载校验通过，跳过 npm ci/build" in r.stdout
    assert _npm_calls(env) == ""
    webui_dir = Path(env["RELEASES"]) / f"webui-{sha[:8]}"
    assert (webui_dir / "dist" / "server" / "index.js").read_text() == "// from artifact\n"

    argv = _curl_argv(env)
    first = [l for l in argv if "--config -" in l]
    second = [l for l in argv if "--config -" not in l]
    assert len(first) == 2, f"两个文件各问一次包仓库，实际 {len(first)} 次：{argv}"
    assert len(second) == 2, f"两个文件各取一次预签名 URL，实际 {len(second)} 次：{argv}"
    for line in first:
        assert "objstore.invalid" not in line, "带 token 的那一跳只该问 GitLab"
        assert ARTIFACT_TOKEN not in line
    for line in second:
        assert "objstore.invalid" in line
        assert "DEPLOY-TOKEN" not in line, f"第二跳带了 token：{line}"
        assert "--config" not in line and "-K" not in line, f"第二跳还在喂 config：{line}"
        assert ARTIFACT_TOKEN not in line
    # 预签名 URL 自带凭据，不许进发布日志
    assert "presigned" not in r.stdout and "objstore.invalid" not in r.stdout


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_redirect_that_cannot_be_followed_falls_back_loudly(env, tmp_path):
    """第二跳取不到（预签名过期之类）不许静默，也不许把发布搞挂 —— 退回原地构建。"""
    sha = env["_webui_sha"]
    _make_artifact(tmp_path, sha)
    _artifact_stubs(env, tmp_path)
    env["REDIRECT_TO"] = "https://objstore.invalid/gone"
    # 第一跳照常 302（包仓库认这个 sha），第二跳的预签名目标已经没东西了
    for f in Path(env["PKG_DIR"]).glob("webui-*"):
        f.unlink()
    _tag(env, "release-redirect-gone", f"multitenancy: {env['_mt_sha']}\nwebui: {sha}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "ARTIFACT MISSING" in r.stdout
    assert "objstore.invalid" in r.stdout, "只记 host，出事要看得出是哪一跳断的"
    assert "presigned" not in r.stdout and "sig=" not in r.stdout, "预签名凭据不许进日志"
    assert "npm ci" in _npm_calls(env)
    assert _artifact_leftovers(env) == []


# ── 8. 产物是不可信输入：校验必须绑在包身上，被拒的包不许碰 checkout ──
#
# codex review 抓到的三条 P1：边车能指向任意路径、被拒的包已经落进源码树、
# 装到一半的树被下一次当成装好了。下面每条对应一个真实的攻击/事故场景。

MARKER = ".artifact-installing"


def _lock_hash() -> str:
    return hashlib.sha256(WEBUI_LOCK.encode()).hexdigest()


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_sidecar_pointing_at_another_file_is_rejected(env, tmp_path):
    """边车里写 `/dev/null` 就能让没校验过的包过关 —— 只信我们自己对受控路径算的 hex。"""
    sha = env["_webui_sha"]
    empty = hashlib.sha256(b"").hexdigest()
    _make_artifact(tmp_path, sha, sidecar_body=f"{empty}  /dev/null\n")
    _artifact_stubs(env, tmp_path)
    _tag(env, "release-sidecar-path", f"multitenancy: {env['_mt_sha']}\nwebui: {sha}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "ARTIFACT CHECKSUM MISMATCH" in r.stdout
    assert "边车清单不合法" in r.stdout
    assert "npm ci" in _npm_calls(env)
    assert _artifact_leftovers(env) == []


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_multi_line_sidecar_is_rejected(env, tmp_path):
    """多行边车 = 多个校验目标，`sha256sum -c` 会挨个校验，我们只认一行一包。"""
    sha = env["_webui_sha"]
    pkg = _make_artifact(tmp_path, sha)
    good = (pkg / f"webui-{sha}.sha256").read_text().strip()
    (pkg / f"webui-{sha}.sha256").write_text(good + "\n" + hashlib.sha256(b"x").hexdigest() + "  /dev/null\n")
    _artifact_stubs(env, tmp_path)
    _tag(env, "release-sidecar-lines", f"multitenancy: {env['_mt_sha']}\nwebui: {sha}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "ARTIFACT CHECKSUM MISMATCH" in r.stdout
    assert "边车清单不合法" in r.stdout
    assert "npm ci" in _npm_calls(env)


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_rejected_artifact_never_touches_the_checkout(env, tmp_path):
    """被拒的包不许在源码树里留下任何东西 —— 否则退回去跑的 npm ci 读的是它的 package-lock。"""
    sha = env["_webui_sha"]
    _make_artifact(tmp_path, sha, lock_body='{"name": "evil", "lockfileVersion": 3}\n')
    _artifact_stubs(env, tmp_path)
    _tag(env, "release-badlock", f"multitenancy: {env['_mt_sha']}\nwebui: {sha}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "ARTIFACT REJECTED" in r.stdout and "package-lock.json 与 checkout 不一致" in r.stdout
    webui_dir = Path(env["RELEASES"]) / f"webui-{sha[:8]}"
    assert webui_dir.joinpath("package-lock.json").read_text() == WEBUI_LOCK, "checkout 的清单被包改写了"
    assert not webui_dir.joinpath("node_modules").exists(), "被拒的包把依赖留下了"
    assert webui_dir.joinpath("dist", "server", "index.js").read_text() == "// built on host\n"
    assert f"lock={_lock_hash()}" in _npm_calls(env), "npm 读到的不是 checkout 那份 package-lock"


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_archive_with_an_unexpected_member_is_rejected(env, tmp_path):
    """白名单之外的成员一律拒：产物只该有 dist/node_modules/三个清单文件。"""
    sha = env["_webui_sha"]
    _make_artifact(tmp_path, sha, extra_members={"scripts/evil.sh": "#!/bin/sh\necho pwned\n"})
    _artifact_stubs(env, tmp_path)
    _tag(env, "release-extra-member", f"multitenancy: {env['_mt_sha']}\nwebui: {sha}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "ARTIFACT REJECTED" in r.stdout and "越界成员" in r.stdout
    webui_dir = Path(env["RELEASES"]) / f"webui-{sha[:8]}"
    assert not webui_dir.joinpath("scripts").exists(), "越界成员落进了源码树"
    assert "npm ci" in _npm_calls(env)
    assert _artifact_leftovers(env) == []


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_archive_with_an_escaping_symlink_is_rejected(env, tmp_path):
    """跑出包的软链是经典的解包穿越：先放 x -> /etc，再往 x/ 里写文件。

    同时守住反面：npm 的 node_modules/.bin/* -> ../pkg/bin/* 带 `..` 但没跑出去，
    正常产物里全是这种，一刀切拒绝等于永远用不上产物（见下一条）。
    """
    sha = env["_webui_sha"]
    _make_artifact(tmp_path, sha, escaping_symlink=True)
    _artifact_stubs(env, tmp_path)
    _tag(env, "release-symlink", f"multitenancy: {env['_mt_sha']}\nwebui: {sha}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "ARTIFACT REJECTED" in r.stdout
    assert "npm ci" in _npm_calls(env)
    assert _artifact_leftovers(env) == []


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_legit_npm_bin_symlinks_are_accepted(env, tmp_path):
    """反面守卫：合法的 .bin 软链（带 `..` 但不越界）必须照常装进去。"""
    sha = env["_webui_sha"]
    _make_artifact(tmp_path, sha)
    _artifact_stubs(env, tmp_path)
    _tag(env, "release-binlinks", f"multitenancy: {env['_mt_sha']}\nwebui: {sha}")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert f"webui 产物 {sha[:8]} 下载校验通过" in r.stdout
    assert _npm_calls(env) == ""
    webui_dir = Path(env["RELEASES"]) / f"webui-{sha[:8]}"
    assert webui_dir.joinpath("node_modules", ".bin", "tsc").is_symlink()


@pytest.mark.parametrize("env", [{"webui_prebuilt": False}], indirect=True)
def test_interrupted_install_is_never_reused(env, tmp_path):
    """装到一半被杀（dist 已就位、node_modules 还缺）留下的树，下一次必须整棵重来。

    没有这条，下一次发布会走「dist 在就不构建」的早退，带着半套依赖上线。
    """
    sha = env["_webui_sha"]
    _make_artifact(tmp_path, sha)
    _artifact_stubs(env, tmp_path)

    # 上一次的残局：dist 有了、node_modules 没有、标记还在
    webui_dir = Path(env["RELEASES"]) / f"webui-{sha[:8]}"
    (webui_dir / "dist" / "server").mkdir(parents=True)
    (webui_dir / "dist" / "server" / "index.js").write_text("// half-installed\n")
    (webui_dir / "package-lock.json").write_text(WEBUI_LOCK)
    (webui_dir / MARKER).write_text("")

    _tag(env, "release-interrupted", f"multitenancy: {env['_mt_sha']}\nwebui: {sha}")
    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "上次产物安装没走完" in r.stdout
    assert f"webui 产物 {sha[:8]} 下载校验通过" in r.stdout
    assert (webui_dir / "dist" / "server" / "index.js").read_text() == "// from artifact\n", "半截 dist 被当成装好了"
    assert (webui_dir / "node_modules" / "node-pty").exists()
    assert not (webui_dir / MARKER).exists(), "装完必须把标记摘掉"
    assert _npm_calls(env) == ""


# ── 与 core 配对切换：AUTO_ROLLBACK / DEP_HEAL / 回滚配对判据（2026-09-24）──────
# core 0.21.4 与新 MT 同一次停服切换。自动回滚会把旧 MT 装进新 core 的 venv 再起服务，
# 旧版探针可能判 ROLLED_BACK 假绿；依赖自愈会带依赖重装、改写 core 环境。


def _new_release(env, name: str, *, probe_exit: int = 0, dropin_exit: int = 0) -> str:
    """在假 mt 仓上提交一个新版本并打标签，返回新 mt 目录的相对软链目标。"""
    src = env["_mt_src"]
    (src / f"extra-{name}.txt").write_text("new\n")
    probe = src / "deploy" / "hermes-release-probes.sh"
    probe.write_text(f"#!/usr/bin/env bash\nexit {probe_exit}\n")
    probe.chmod(0o755)
    installer = src / "deploy" / "install-gateway-dropins.sh"
    installer.write_text(
        '#!/usr/bin/env bash\necho new-dropin-install >> "$SYSTEMCTL_LOG"\n'
        f"exit {dropin_exit}\n"
    )
    installer.chmod(0o755)
    _git(src, "add", "-A"); _git(src, "commit", "-q", "-m", name)
    sha = _git(src, "rev-parse", "HEAD")
    _tag(env, name, f"multitenancy: {sha}\nwebui: {env['_webui_sha']}")
    return f"../releases/mt-{sha[:7]}"


def _old_dropin_installer_logs(env) -> None:
    old = Path(env["RELEASES"]) / "mt-current/deploy/install-gateway-dropins.sh"
    old.write_text('#!/usr/bin/env bash\necho old-dropin-install >> "$SYSTEMCTL_LOG"\n')
    old.chmod(0o755)


@contextlib.contextmanager
def _code_dir_readonly(env, on: bool):
    """让翻软链这一步真的失败：$CODE 只读 = ln 换不了里面的软链。读回核对不受影响。"""
    code = Path(env["CODE"])
    if on:
        code.chmod(0o555)
    try:
        yield
    finally:
        code.chmod(0o755)


def _rollback_txt(env, tag: str) -> str:
    return (Path(env["BACKUP_ROOT"]) / tag / "ROLLBACK.txt").read_text()


def _assert_held_for_human(env, tag: str, r, new_mt_link: str | None) -> None:
    """不回滚的全部含义：没翻回、没重装旧 editable、没装旧 drop-in、最后一个动作是停服，
    且停服经 is-active 读回确认（exit 1，不是停服失败的 exit 2）。

    new_mt_link=None：失败发生在翻软链这一步本身，软链应保持发布前原样。
    """
    prev = str((Path(env["RELEASES"]) / "mt-current").resolve())
    assert r.returncode == 1, r.stdout + r.stderr
    rb = _rollback_txt(env, tag)
    assert "outcome=NEEDS_HUMAN_CORE_PAIRED" in rb
    assert "outcome=ROLLED_BACK" not in rb
    assert "ROLLED BACK" not in r.stdout
    assert "NEEDS_HUMAN_CORE_PAIRED" in r.stdout
    assert "still_active=<none>" in rb
    mt_link, webui_link = _links(env)
    if new_mt_link is None:
        assert (mt_link, webui_link) == ("../releases/mt-current", "../releases/webui-current")
    else:
        assert mt_link == new_mt_link, "不许把 MT 软链翻回上一版"
        assert webui_link != "../releases/webui-current", "不许把 WebUI 软链翻回上一版"
    lines = Path(env["SYSTEMCTL_LOG"]).read_text().splitlines()
    assert f"uv-install {prev}" not in lines, "不许把旧 MT editable 装进当前 venv"
    assert "old-dropin-install" not in lines, "不许重装旧 drop-in"
    svc = [l for l in lines if l.startswith(("start", "stop"))]
    assert svc and svc[-1].startswith("stop"), f"失败后服务必须停着：{svc}"
    assert not Path(env["STATE_FILE"]).exists()


@pytest.mark.parametrize(("var", "value"), [
    ("AUTO_ROLLBACK", "false"), ("AUTO_ROLLBACK", "2"), ("DEP_HEAL", "no"),
])
def test_switches_reject_values_other_than_0_or_1(env, var, value):
    _new_release(env, "release-badflag")
    before = _links(env)
    r = _run({**env, var: value})
    assert r.returncode != 0
    assert "只能是 0 或 1" in r.stderr
    assert _links(env) == before
    assert not Path(env["SYSTEMCTL_LOG"]).exists(), "配置写错不许动服务"


@pytest.mark.parametrize("failure", ["flip", "editable", "dropin", "start", "probe"])
def test_auto_rollback_off_holds_every_failure_point_for_a_human(env, failure):
    """AUTO_ROLLBACK=0：editable / drop-in / 启动 / 探针四处失败都不翻回，只停服交人。"""
    _old_dropin_installer_logs(env)
    tag = f"release-hold-{failure}"
    link = _new_release(env, tag, probe_exit=1 if failure == "probe" else 0,
                        dropin_exit=1 if failure == "dropin" else 0)
    if failure == "editable":
        Path(env["UV_FAIL_FLAG"]).write_text("boom\n")
    if failure == "start":
        Path(env["SYSTEMCTL_FAIL_NEXT_START"]).write_text("fail once\n")

    with _code_dir_readonly(env, failure == "flip"):
        r = _run({**env, "AUTO_ROLLBACK": "0"})

    _assert_held_for_human(env, tag, r, None if failure == "flip" else link)
    rb = _rollback_txt(env, tag)
    step = {"flip": "软链切换失败", "editable": "editable 重装/读回失败",
            "dropin": "gateway drop-in 安装失败"}.get(failure, "新版本启动或探针失败")
    assert f"failed_step={step}" in rb
    assert "auto_rollback=0" in rb
    assert "hold_reason=AUTO_ROLLBACK=0" in rb


@pytest.mark.parametrize("failure", ["flip", "editable", "dropin", "start", "probe"])
def test_auto_rollback_on_still_rolls_back_every_failure_point(env, failure):
    """默认 AUTO_ROLLBACK=1 且 core 未换：四处失败照旧翻回上一版。"""
    _old_dropin_installer_logs(env)
    tag = f"release-rb-{failure}"
    _new_release(env, tag, probe_exit=1 if failure == "probe" else 0,
                 dropin_exit=1 if failure == "dropin" else 0)
    if failure == "editable":
        Path(env["UV_FAIL_FLAG"]).write_text("boom\n")
    if failure == "start":
        Path(env["SYSTEMCTL_FAIL_NEXT_START"]).write_text("fail once\n")

    before = _links(env)
    with _code_dir_readonly(env, failure == "flip"):
        r = _run(env)

    assert r.returncode != 0
    assert _links(env) == before, "默认模式必须原样翻回上一版"
    if failure == "flip":
        assert "outcome=FLIP_FAILED" in _rollback_txt(env, tag)
    rb = _rollback_txt(env, tag)
    assert "NEEDS_HUMAN_CORE_PAIRED" not in rb
    svc = [l for l in Path(env["SYSTEMCTL_LOG"]).read_text().splitlines()
           if l.startswith(("start", "stop"))]
    assert svc[-1].startswith("start"), f"回滚后必须把服务拉起来：{svc}"


def test_auto_rollback_off_does_not_change_a_green_release(env):
    _new_release(env, "release-pair-green")
    r = _run({**env, "AUTO_ROLLBACK": "0", "DEP_HEAL": "0"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RELEASE OK" in r.stdout
    assert Path(env["STATE_FILE"]).read_text().strip() == "release-pair-green"
    assert "outcome=SUCCESS" in _rollback_txt(env, "release-pair-green")


def test_dep_heal_off_fails_instead_of_reinstalling_with_deps(env):
    tag = "release-noheal"
    link = _new_release(env, tag)
    Path(env["UV_CHECK_FAIL"]).write_text("x\n")

    r = _run({**env, "AUTO_ROLLBACK": "0", "DEP_HEAL": "0"})

    lines = Path(env["SYSTEMCTL_LOG"]).read_text().splitlines()
    assert "uv-heal" not in lines, "DEP_HEAL=0 不许带依赖重装"
    assert "DEP_HEAL=0" in r.stdout
    assert "missing dep x" in r.stdout, "pip check 的原文要进日志，方便现场判断"
    _assert_held_for_human(env, tag, r, link)


def test_dep_heal_on_still_self_heals(env):
    _new_release(env, "release-heal")
    Path(env["UV_CHECK_FAIL"]).write_text("x\n")

    r = _run(env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "uv-heal" in Path(env["SYSTEMCTL_LOG"]).read_text().splitlines()
    assert "依赖自愈完成" in r.stdout


def test_default_rollback_refuses_when_venv_no_longer_runs_the_previous_mt(env):
    """同一次停服先翻了 core：venv 是新 core 的、里面预装的是新 MT。默认模式的回滚
    会把旧 MT 装进新 core —— 必须识别出来，不回滚、停服、不报 ROLLED_BACK。"""
    _old_dropin_installer_logs(env)
    tag = "release-unpaired"
    link = _new_release(env, tag, probe_exit=1)
    Path(env["UV_STATE"]).write_text(str(Path(env["RELEASES"]) / "mt-preinstalled-new") + "\n")
    Path(env["CORE_ID"]).write_text("0.21.4 /home/hermes/releases/hermes-agent-v0214-x\n")

    r = _run(env)

    _assert_held_for_human(env, tag, r, link)
    rb = _rollback_txt(env, tag)
    assert "prev_mt_in_venv=0" in rb
    assert "core_at_start=0.21.4 /home/hermes/releases/hermes-agent-v0214-x" in rb
    assert "不配对" in r.stdout


def test_default_rollback_refuses_when_core_changed_during_the_release(env, tmp_path):
    _old_dropin_installer_logs(env)
    tag = "release-coreswap"
    link = _new_release(env, tag, probe_exit=1)
    swap = tmp_path / "core-id-new"
    swap.write_text("0.21.4 /core-new\n")

    r = _run({**env, "CORE_SWAP_TO": str(swap)})

    _assert_held_for_human(env, tag, r, link)
    assert "core 在发布过程中变了" in r.stdout
    assert "core_now=0.21.4 /core-new" in _rollback_txt(env, tag)


def test_paired_baseline_is_recorded_in_the_rollback_anchor(env, tmp_path):
    _new_release(env, "release-baseline")
    r = _run(env)
    assert r.returncode == 0, r.stdout + r.stderr
    rb = _rollback_txt(env, "release-baseline")
    assert f"core_at_start=0.19.1 {tmp_path / 'core-v0191'}" in rb
    assert "prev_mt_in_venv=1" in rb
    assert "auto_rollback=1" in rb and "dep_heal=1" in rb


def test_hold_reports_a_failed_stop_instead_of_claiming_services_are_down(env):
    """停服失败时不许写「服务已停」：逐个读回 is-active，仍在跑的写进 still_active，exit 2。"""
    tag = "release-stopfail"
    _new_release(env, tag, probe_exit=1)
    Path(env["SYSTEMCTL_STILL_ACTIVE"]).write_text("stop hangs\n")

    r = _run({**env, "AUTO_ROLLBACK": "0", "UNITS": "hermes-gateway.service hermes-web-ui.service"})

    assert r.returncode == 2, r.stdout + r.stderr
    rb = _rollback_txt(env, tag)
    assert "outcome=NEEDS_HUMAN_CORE_PAIRED" in rb
    assert "still_active=hermes-gateway.service hermes-web-ui.service" in rb
    assert "stop_rc=1" in rb
    assert "停服失败" in r.stdout
    assert "服务已停" not in r.stdout
    lines = Path(env["SYSTEMCTL_LOG"]).read_text().splitlines()
    assert "is-active hermes-gateway.service" in lines


@pytest.mark.parametrize(("rc", "out"), [
    ("1", ""), ("4", ""), ("1", "Failed to connect to bus"), ("4", "unknown"),
])
def test_hold_treats_an_unreadable_unit_state_as_not_stopped(env, rc, out):
    """stop 和 is-active 同时失败（D-Bus / 权限故障）：读不出状态不许说「服务已停」，按停服失败 exit 2。"""
    tag = f"release-unknown-{rc}"
    _new_release(env, tag, probe_exit=1)
    units = "hermes-gateway.service hermes-web-ui.service"
    Path(env["SYSTEMCTL_IS_ACTIVE"]).write_text(
        "".join(f"{u} {rc} {out}\n" for u in units.split()))
    stub = Path(env["SYSTEMCTL"])
    stub.write_text(stub.read_text().replace(
        'if [ "$1" = "stop" ] && [ -f "$SYSTEMCTL_STILL_ACTIVE" ]; then exit 1; fi',
        'if [ "$1" = "stop" ]; then exit 1; fi'))

    r = _run({**env, "AUTO_ROLLBACK": "0", "UNITS": units})

    assert r.returncode == 2, r.stdout + r.stderr
    assert "服务已停" not in r.stdout
    assert "状态未知" in r.stdout
    rb = _rollback_txt(env, tag)
    assert "outcome=NEEDS_HUMAN_CORE_PAIRED" in rb
    assert "stop_rc=1" in rb
    assert "still_active=<none>" in rb
    assert f"unknown_state={units}" in rb


def test_hold_lists_only_the_unreadable_unit_when_the_other_is_stopped(env):
    """混合：一个明确 inactive、一个读不出状态 → exit 2，只列读不出的那个。"""
    tag = "release-unknown-mixed"
    _new_release(env, tag, probe_exit=1)
    Path(env["SYSTEMCTL_IS_ACTIVE"]).write_text(
        "hermes-gateway.service 3 inactive\nhermes-web-ui.service 4\n")

    r = _run({**env, "AUTO_ROLLBACK": "0", "UNITS": "hermes-gateway.service hermes-web-ui.service"})

    assert r.returncode == 2, r.stdout + r.stderr
    assert "服务已停" not in r.stdout
    rb = _rollback_txt(env, tag)
    assert "unknown_state=hermes-web-ui.service\n" in rb
    assert "still_active=<none>" in rb


def test_hold_still_reports_stopped_when_every_unit_reads_back_inactive_or_failed(env):
    """全部明确已停（inactive / failed，exit 3）：照旧「服务已停」、exit 1。"""
    tag = "release-stopped-ok"
    _new_release(env, tag, probe_exit=1)
    Path(env["SYSTEMCTL_IS_ACTIVE"]).write_text(
        "hermes-gateway.service 3 inactive\nhermes-web-ui.service 3 failed\n")

    r = _run({**env, "AUTO_ROLLBACK": "0", "UNITS": "hermes-gateway.service hermes-web-ui.service"})

    assert r.returncode == 1, r.stdout + r.stderr
    assert "服务已停" in r.stdout
    rb = _rollback_txt(env, tag)
    assert "still_active=<none>" in rb
    assert "unknown_state=<none>" in rb


def test_default_rollback_refuses_when_core_identity_is_unreadable_before_the_release(env):
    """发布前就取不到 core 身份：证明不了 core 没变，按不配对停服交人，不是只打警告。"""
    _old_dropin_installer_logs(env)
    tag = "release-nocoreid"
    link = _new_release(env, tag, probe_exit=1)
    Path(env["CORE_ID"]).write_text("")

    r = _run(env)

    _assert_held_for_human(env, tag, r, link)
    rb = _rollback_txt(env, tag)
    assert "core_at_start=<unknown>" in rb
    assert "prev_mt_in_venv=1" in rb, "这条用例只让 core 身份缺失，venv 里的 MT 仍是上一版"
    assert "取不到 core 身份" in r.stdout
