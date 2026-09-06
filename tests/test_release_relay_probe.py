"""relay 存活探针的重启竞态（relay-probe-restart-race）。

被测的是 deploy/relay-probe-lib.sh 里真正发货的 `relay_probe_wait`：它是纯函数文件，
可以直接 source（hermes-release.sh 本身是顶层直接执行的，source 它等于跑一次真发布，
这正是这段判定过去零覆盖的原因）。

2026-08-15 release-20260815-01 现场：探针在 relay 重启后立刻打一次，撞上「systemd 说
started、aiohttp 还没 bind」的一两秒拿到 000 → 还原 relay → 整个发布 exit 1 + 告警。
事后同一条探针连打三次全 401，同步的 4 个文件与生产逐字节相同：纯假失败。
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
LIB = DEPLOY / "relay-probe-lib.sh"
RELEASE_SH = DEPLOY / "hermes-release.sh"


def _wait(codes: list[str], timeout: int = 3, *, tmp_path: Path | None = None) -> tuple[int, int, str]:
    """Drive relay_probe_wait with a scripted probe sequence.

    Returns (rc, waited_seconds, last_code). `sleep` is stubbed to a no-op so the
    retry path costs no wall clock. The cursor lives in a FILE on purpose: the
    function calls the probe through `$(...)`, i.e. in a subshell, so a shell
    variable cursor would silently reset and every call would replay code #1.
    """
    base = tmp_path or Path(subprocess.run(["mktemp", "-d"], capture_output=True, text=True).stdout.strip())
    codes_file = base / "codes"
    codes_file.write_text("\n".join(codes) + "\n", encoding="utf-8")
    script = textwrap.dedent(f"""
        set -uo pipefail
        . {LIB}
        probe() {{
          local f={codes_file} c
          c=$(head -1 "$f" 2>/dev/null)
          [ -n "$c" ] || c=401
          tail -n +2 "$f" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f"
          printf '%s' "$c"
        }}
        noop_sleep() {{ :; }}
        relay_probe_wait probe {timeout} noop_sleep
        printf 'RC=%s WAITED=%s LAST=%s\\n' "$?" "$RELAY_PROBE_WAITED" "$RELAY_PROBE_LAST_CODE"
    """)
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    out = (proc.stdout + proc.stderr).strip()
    line = [ln for ln in out.splitlines() if ln.startswith("RC=")]
    assert line, out
    rc, waited, last = (part.split("=", 1)[1] for part in line[-1].split())
    return int(rc), int(waited), last


@pytest.mark.parametrize("down_code", ["000000", "000"])
def test_waits_through_the_restart_window_then_passes(tmp_path, down_code):
    """连不上 → 连不上 → 401：通过，并报出等了几秒。

    `000000` 是**生产实测**的形状（release-20260815-01 日志逐字：「实得 000000」）：
    curl 连接失败时 -w 先打 000，curl 非零退出又触发 `|| echo 000`，两段拼在一起。
    第一版修复只认字面 "000"，在生产上永远不会重试 —— 评审 P0 抓到的正是这条，
    所以生产形状排在参数化的第一个。
    """
    rc, waited, last = _wait([down_code, down_code, "401"], tmp_path=tmp_path)

    assert rc == 0
    assert waited == 2
    assert last == "401"


def test_immediate_401_does_not_wait(tmp_path):
    """立刻 401：零等待，行为与改动前一致。"""
    rc, waited, last = _wait(["401"], tmp_path=tmp_path)

    assert (rc, waited, last) == (0, 0, "401")


def test_never_binding_relay_still_fails_after_timeout(tmp_path):
    """一直连不上（生产形状 000000）：超时后失败（重试不能吃掉拦截力）。"""
    rc, waited, last = _wait(["000000"] * 20, timeout=3, tmp_path=tmp_path)

    assert rc == 1
    assert waited == 3, "必须在 timeout 秒后停手，不能无限等"
    assert last == "000000", "最后实得码必须原样保留，日志才有排障价值"


def test_route_not_registered_fails_immediately(tmp_path):
    """404 = 服务起来了但路由没注册（代码真坏）：立刻失败，不烧超时。"""
    rc, waited, last = _wait(["404", "401", "401"], timeout=30, tmp_path=tmp_path)

    assert rc == 1
    assert waited == 0, "确定性坏不该等"
    assert last == "404"


@pytest.mark.parametrize("code", ["405", "500", "200"])
def test_only_401_counts_as_alive(code, tmp_path):
    """401 是唯一通过条件；405/500/200 都不算。"""
    rc, _waited, last = _wait([code], tmp_path=tmp_path)

    assert rc == 1
    assert last == code


@pytest.mark.parametrize("code,is_down", [
    ("000000", True),   # 生产实测形状
    ("000", True),      # 单段形状（探针实现变了也认）
    ("0", True),
    ("401", False),
    ("404", False),
    ("500", False),
    ("", False),        # 空 ≠ 连不上：分不清就不该当成可重试
])
def test_connection_failure_classifier(code, is_down):
    """全零串 = 连不上（可重试）；其它一律不是。"""
    script = f'. {LIB}\nrelay_probe_connect_failed "{code}"\nprintf "RC=%s" "$?"'
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    rc = int((proc.stdout + proc.stderr).strip().split("RC=")[-1])

    assert (rc == 0) is is_down, proc.stdout + proc.stderr


def test_release_script_logs_both_failure_shapes_distinctly():
    """回归护栏：超时失败与确定性坏必须写成两句不同的日志，且都带实得码。"""
    text = RELEASE_SH.read_text(encoding="utf-8")

    assert "relay_probe_connect_failed" in text, "主脚本必须用同一个判据分流两种失败"
    assert text.count("RELAY_PROBE_LAST_CODE") >= 2, "两种失败日志都要带最后实得码"
    assert "RELAY_PROBE_WAITED" in text


def test_simulation_driver_ships_with_the_repo():
    """仿真驱动必须住在仓库里、可执行、且用与生产同款的 curl 探针。

    round-2 评审在 worktree 里找不到它（当时放在 .ftask/ 下）——藏在任务目录里的
    复现脚本，等于让下一个人重犯同一个 P0（生产的连不上码是 000000 而非 000）。
    """
    sim = DEPLOY / "relay-probe-sim.sh"

    assert sim.exists(), "仿真驱动必须随仓库发货"
    assert sim.stat().st_mode & 0o111, "必须可执行"
    text = sim.read_text(encoding="utf-8")
    assert "%{http_code}" in text and "|| echo 000" in text, "必须复刻生产探针的形状（P0 就出在这两段的拼接）"
    assert subprocess.run(["bash", "-n", str(sim)]).returncode == 0


def test_release_script_uses_the_lib_and_stays_syntactically_valid():
    """回归护栏：主脚本必须 source 这个 lib 并调用它，且两个文件语法都可解析。"""
    text = RELEASE_SH.read_text(encoding="utf-8")

    assert "relay-probe-lib.sh" in text, "主脚本必须 source 探针 lib"
    assert "relay_probe_wait" in text, "主脚本必须走 lib 的等待逻辑"
    assert subprocess.run(["bash", "-n", str(RELEASE_SH)]).returncode == 0
    assert subprocess.run(["bash", "-n", str(LIB)]).returncode == 0


# ── 探针上限 vs relay 自己的 pre-bind 耗时 ───────────────────────────────
# 2026-08-27 release-20260827-01 第二次假失败后加的守卫。前面那些测试证明
# 「等待逻辑」是对的，但没人证明【等多久】是对的 —— 15s 这个数字从来没有依据，
# 而 relay 的 bind 时间有一个可算的下界，两者的错配才是复发的真原因。
RELAY_FEISHU = Path(__file__).resolve().parents[1] / "hermes_multitenancy" / "agent_relay_feishu.py"

#: relay 放弃飞书 ws 后还要 join 工作线程（agent_relay_feishu.py 里的
#: `thread.join(timeout=5)`）。没抽成常量是有意的：动它要改 relay 运行时代码，
#: 而那份代码走 /opt 下独立 venv 的同步链，为一个字面量不值当。
RELAY_WS_JOIN_SECONDS = 5
#: 下界之上的余量倍数：冷启动要 import lark_oapi / aiohttp / cryptography，
#: 飞书握手本身也会抖。真死的 relay 只是晚报红，代价远小于一次假失败。
PROBE_HEADROOM_FACTOR = 4


def _shell_default(text: str, name: str) -> int:
    """从 `NAME="${NAME:-<n>}"` 里取出发货默认值。"""
    import re

    match = re.search(rf'^{name}="\$\{{{name}:-(\d+)\}}"', text, re.MULTILINE)
    assert match, f"{name} 的默认值形状变了（必须仍可被环境变量覆盖）"
    return int(match.group(1))


def _py_constant(text: str, name: str) -> int:
    import re

    match = re.search(rf"^{name}\s*=\s*(\d+)", text, re.MULTILINE)
    assert match, f"读不到 {name}"
    return int(match.group(1))


def test_probe_timeout_covers_relay_pre_bind_worst_case():
    """探针上限必须数倍于 relay 放弃飞书 ws 所需的时间。

    aiohttp 跑完 on_startup 才 bind socket，而 on_startup 里同步等 ws：
    relay 最坏要 EVENT_STREAM_START_TIMEOUT_SECONDS + join 才可能 bind。
    探针上限 ≤ 这个数，就是结构性地保证超时（15s vs 10+5=15s，两次事故）。

    relay 侧把 ws 上限调大而这里没跟上 → 本测试红，逼一次有意识的决定。
    """
    ws = _py_constant(RELAY_FEISHU.read_text(encoding="utf-8"), "EVENT_STREAM_START_TIMEOUT_SECONDS")
    probe = _shell_default(RELEASE_SH.read_text(encoding="utf-8"), "RELAY_PROBE_TIMEOUT")
    floor = (ws + RELAY_WS_JOIN_SECONDS) * PROBE_HEADROOM_FACTOR

    assert probe >= floor, (
        f"RELAY_PROBE_TIMEOUT={probe}s 盖不住 relay 的 pre-bind 最坏耗时："
        f"ws {ws}s + join {RELAY_WS_JOIN_SECONDS}s，需 ≥{floor}s"
    )


def test_slow_bind_passes_at_shipped_timeout_but_failed_at_the_old_15(tmp_path):
    """一次真实形状的慢 bind：新默认下通过，旧的 15s 下失败。

    红绿双向 —— 只证"现在绿了"无法证明这次改动就是原因。
    """
    slow = ["000000"] * 16 + ["401"]
    shipped = _shell_default(RELEASE_SH.read_text(encoding="utf-8"), "RELAY_PROBE_TIMEOUT")
    # 两轮各用自己的目录：_wait 的游标是会被吃掉的文件，共用目录会串味。
    for sub in ("new", "old"):
        (tmp_path / sub).mkdir()

    rc, waited, last = _wait(list(slow), timeout=shipped, tmp_path=tmp_path / "new")
    assert (rc, waited, last) == (0, 16, "401"), "发货默认值必须等得过 16s 的慢 bind"

    rc_old, waited_old, last_old = _wait(list(slow), timeout=15, tmp_path=tmp_path / "old")
    assert rc_old == 1, "旧的 15s 必须在这条序列上失败，否则这次改动没有解释力"
    assert waited_old == 15
    assert last_old == "000000"
