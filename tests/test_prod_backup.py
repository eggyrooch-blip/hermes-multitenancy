"""deploy/hermes-backup.sh 与 deploy/hermes-restore-drill.sh 的真逻辑测试。

不碰任何运行时代码，也不碰生产：全部在 tmp_path 里造假库跑真脚本。
覆盖三处会真出事的地方：
  1. 磁盘前置检查 —— 空间不够必须拒绝，且不能留下半份备份
  2. 保留策略 —— 安全机制不能自己把盘写满
  3. 演练判据 —— 数据被改坏时必须报失败（负控制；没有它，演练就只是个橡皮图章）
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
BACKUP_SH = DEPLOY / "hermes-backup.sh"
DRILL_SH = DEPLOY / "hermes-restore-drill.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("sqlite3") is None or shutil.which("rsync") is None,
    reason="需要 sqlite3 与 rsync 可执行文件",
)


def _make_db(path: Path, table: str, rows: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute("pragma journal_mode=wal")
    conn.execute(f"create table {table}(id integer primary key, v text)")
    conn.executemany(f"insert into {table}(v) values(?)", [(f"v{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    """造一个和生产同形状的假环境：4 个真库 + 2 个空库 + config + profiles。"""
    home = tmp_path / "home" / ".hermes"
    webui = tmp_path / "home" / ".hermes-web-ui"
    (home / "profiles" / "u1").mkdir(parents=True)
    (home / "feishu_uat").mkdir()
    webui.mkdir(parents=True)

    _make_db(home / "multitenancy.db", "t_mt", 3)
    _make_db(home / "state.db", "t_state", 1)
    _make_db(home / "kanban.db", "t_kanban", 2)
    _make_db(webui / "hermes-web-ui.db", "t_web", 4)
    (home / "multitenancy_routing.db").touch()
    (webui / "web-ui.db").touch()

    (home / "config.yaml").write_text("k: v\n")
    (home / ".env").write_text("SECRET=x\n")
    (home / "auth.json").write_text("{}\n")
    (home / "feishu_uat" / "ou_test.json").write_text("{}\n")
    (home / "profiles" / "u1" / "f.txt").write_text("hello\n")

    return {
        "HERMES_HOME_DIR": str(home),
        "HERMES_WEBUI_DIR": str(webui),
        "BACKUP_ROOT": str(tmp_path / "backups"),
        "DRILL_ROOT": str(tmp_path / "drill"),
        "MIN_FREE_GB": "1",
        "HOME": str(tmp_path / "home"),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }


def _run(script: Path, env: dict[str, str], **overrides: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)],
        env={**env, **overrides},
        capture_output=True,
        text=True,
    )


def _state_snapshots(env: dict[str, str]) -> list[Path]:
    root = Path(env["BACKUP_ROOT"]) / "state"
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []


# ── 1. 磁盘前置检查 ────────────────────────────────────────────────


def test_refuses_when_disk_below_floor_and_leaves_nothing_behind(env):
    """空间不够时必须非零退出，且绝不留下半份备份 —— 半份备份比没有更危险。"""
    result = _run(BACKUP_SH, env, MIN_FREE_GB="999999999")

    assert result.returncode != 0
    assert "拒绝执行" in result.stderr
    assert _state_snapshots(env) == []
    # staging 也不能留
    root = Path(env["BACKUP_ROOT"]) / "state"
    assert not list(root.glob(".staging-*")) if root.exists() else True


# ── 2. 备份产物形状与自足性 ────────────────────────────────────────


def test_backup_produces_selfcontained_dbs_manifest_and_checksums(env):
    assert _run(BACKUP_SH, env).returncode == 0

    snap = _state_snapshots(env)[-1]
    dbs = sorted(p.name for p in (snap / "db").iterdir())
    # 只有 4 个：另外两个是 0 字节的空库，故意不打开、不备份（见 zero_byte 那条测试）
    assert dbs == [
        "hermes-web-ui.db",
        "kanban.db",
        "multitenancy.db",
        "state.db",
    ], "备份目录里应当只剩纯 .db，不留 -wal/-shm 旁文件"

    manifest = (snap / "MANIFEST.txt").read_text()
    assert "rows multitenancy.db.t_mt=3" in manifest
    assert "kind=local-only" in manifest, "必须自带『本机备份非灾备』的标记"
    assert (snap / "SHA256SUMS").exists()
    assert (snap / "config" / ".env").exists(), "凭证文件必须一并备份"
    assert oct(snap.stat().st_mode)[-3:] == "700", "备份目录含密钥，权限必须 700"


def test_backed_up_db_survives_being_moved_alone(env, tmp_path):
    """把 .db 单独拷走、丢掉所有旁文件，数据还必须在 —— 否则恢复时会静默丢数据。"""
    assert _run(BACKUP_SH, env).returncode == 0
    snap = _state_snapshots(env)[-1]

    isolated = tmp_path / "isolated.db"
    shutil.copy(snap / "db" / "multitenancy.db", isolated)

    conn = sqlite3.connect(isolated)
    assert conn.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert conn.execute("select count(*) from t_mt").fetchone()[0] == 3
    conn.close()


# ── 3. 保留策略 ────────────────────────────────────────────────────


def test_retention_prunes_oldest_and_holds_the_cap(env):
    """跑超过上限后，最老的一份被删，总份数稳定 —— 不然备份会把盘撑爆。"""
    for _ in range(4):
        assert _run(BACKUP_SH, env, KEEP_STATE="2", SKIP_PROFILES="1").returncode == 0

    snaps = _state_snapshots(env)
    assert len(snaps) == 2


# ── 4. profiles 硬链增量 ───────────────────────────────────────────


def test_profiles_snapshot_is_hardlinked_not_recopied(env):
    """未变的文件必须与上一份共享 inode，否则每天重拷 40G。"""
    assert _run(BACKUP_SH, env).returncode == 0
    (Path(env["HERMES_HOME_DIR"]) / "profiles" / "u1" / "new.txt").write_text("new\n")
    assert _run(BACKUP_SH, env).returncode == 0

    snaps = sorted(p for p in (Path(env["BACKUP_ROOT"]) / "profiles").iterdir() if p.is_dir())
    assert len(snaps) == 2
    first = (snaps[0] / "u1" / "f.txt").stat()
    second = (snaps[1] / "u1" / "f.txt").stat()
    assert first.st_ino == second.st_ino, "未改动的文件应是硬链接，不该重新拷贝"
    assert (snaps[1] / "u1" / "new.txt").exists(), "新增文件必须出现在新快照里"


# ── 5. 演练：正控制 + 负控制 ───────────────────────────────────────


def test_drill_passes_on_intact_backup_and_reports_live_drift(env):
    assert _run(BACKUP_SH, env).returncode == 0

    # 备份之后生产继续写 —— 演练不能因此判失败，这是正常业务，不是错误
    conn = sqlite3.connect(Path(env["HERMES_HOME_DIR"]) / "multitenancy.db")
    conn.execute("insert into t_mt(v) values('later')")
    conn.commit()
    conn.close()

    result = _run(DRILL_SH, env)
    assert result.returncode == 0, result.stdout + result.stderr

    report = sorted((Path(env["BACKUP_ROOT"]) / "drill-reports").glob("*.md"))[-1].read_text()
    assert "✅ 通过" in report
    assert "恢复花了多久" in report
    assert "| `multitenancy.db.t_mt` | 3 | 4 |" in report, "与生产的漂移应作为信息列出"


def test_drill_fails_when_backup_rows_were_tampered(env):
    """负控制：备份里少了行，演练必须报失败。没有这条，演练就只是橡皮图章。"""
    assert _run(BACKUP_SH, env).returncode == 0
    snap = _state_snapshots(env)[-1]

    conn = sqlite3.connect(snap / "db" / "multitenancy.db")
    conn.execute("delete from t_mt where id=1")
    conn.commit()
    conn.close()

    result = _run(DRILL_SH, env)
    assert result.returncode != 0
    report = sorted((Path(env["BACKUP_ROOT"]) / "drill-reports").glob("*.md"))[-1].read_text()
    assert "❌ 未通过" in report


def test_drill_refuses_to_restore_into_production_dirs(env):
    """还原目标绝不能落在生产目录里 —— 演练把生产覆盖掉是灾难级事故。"""
    assert _run(BACKUP_SH, env).returncode == 0
    result = _run(DRILL_SH, env, DRILL_ROOT=env["HERMES_HOME_DIR"])
    assert result.returncode != 0
    assert "拒绝执行" in result.stderr


# ── 6. 跨模型评审 round 1 提出的加固项 ─────────────────────────────


def test_zero_byte_db_is_skipped_and_never_opened(env):
    """0 字节的库不能交给 sqlite 打开——sqlite 会给它写文件头，那就是写生产了。"""
    empty = Path(env["HERMES_HOME_DIR"]) / "multitenancy_routing.db"
    assert empty.stat().st_size == 0

    assert _run(BACKUP_SH, env, SKIP_PROFILES="1").returncode == 0

    assert empty.stat().st_size == 0, "源文件被写了——违反『备份只读生产』"
    snap = _state_snapshots(env)[-1]
    assert not (snap / "db" / "multitenancy_routing.db").exists()
    assert "db_empty_skipped=multitenancy_routing.db" in (snap / "MANIFEST.txt").read_text()


def test_missing_known_db_is_reported_not_silently_skipped(env):
    """6 个库是写死的已知清单，缺一个就是异常，必须留痕。"""
    (Path(env["HERMES_HOME_DIR"]) / "kanban.db").unlink()

    result = _run(BACKUP_SH, env, SKIP_PROFILES="1")
    assert result.returncode == 0
    assert "kanban.db 不存在" in result.stdout

    manifest = (_state_snapshots(env)[-1] / "MANIFEST.txt").read_text()
    assert "db_missing=kanban.db," in manifest


def test_missing_profiles_dir_is_fatal(env):
    """profiles 是 sunke 要求必须备的那层，目录不在就该拒绝，而不是产出半份备份。"""
    shutil.rmtree(Path(env["HERMES_HOME_DIR"]) / "profiles")

    result = _run(BACKUP_SH, env)
    assert result.returncode != 0
    assert "拒绝产出只有状态核心的半份备份" in result.stderr


def test_unreadable_profile_file_blocks_backup(env):
    """读不到的文件会被 rsync 静默跳过——默认必须硬拦，不能只警告。"""
    if shutil.which("find") is None:
        pytest.skip("需要 find")
    victim = Path(env["HERMES_HOME_DIR"]) / "profiles" / "u1" / "secret.txt"
    victim.write_text("x\n")
    victim.chmod(0o000)
    try:
        result = _run(BACKUP_SH, env)
        # GNU find 才有 -readable；BSD find（macOS）下脚本会跳过这项检查
        probe = subprocess.run(["find", "/dev/null", "-readable"], capture_output=True)
        if probe.returncode != 0:
            pytest.skip("BSD find 不支持 -readable，该检查只在 Linux 生效")
        assert result.returncode != 0
        assert "静默漏数据" in result.stderr
        # 显式放行时应当能继续
        assert _run(BACKUP_SH, env, ALLOW_UNREADABLE="1").returncode == 0
    finally:
        victim.chmod(0o644)


def test_drill_rejects_symlink_into_production(env, tmp_path):
    """字符串前缀挡不住软链：一条指向 ~/.hermes 的链接能骗过前缀比较，
    然后把生产覆盖掉。守卫必须比真实路径。"""
    assert _run(BACKUP_SH, env).returncode == 0

    sneaky = tmp_path / "innocent-looking"
    sneaky.symlink_to(env["HERMES_HOME_DIR"])

    result = _run(DRILL_SH, env, DRILL_ROOT=str(sneaky))
    assert result.returncode != 0
    assert "拒绝执行" in result.stderr
    # 而且不能在生产目录里留下任何演练残留
    assert not list(Path(env["HERMES_HOME_DIR"]).glob("hermes-drill-*"))


def test_drill_report_covers_profiles_layer(env):
    """Done 是两层备份，演练报告必须把 40G 那层也核一遍。"""
    assert _run(BACKUP_SH, env).returncode == 0
    assert _run(DRILL_SH, env).returncode == 0

    report = sorted((Path(env["BACKUP_ROOT"]) / "drill-reports").glob("*.md"))[-1].read_text()
    assert "## 第二层：profiles" in report
    assert "个文件" in report


def test_drill_fails_when_profiles_snapshot_is_missing(env):
    """有 state 备份却没有 profiles 快照 = 半份备份，演练必须判失败。"""
    assert _run(BACKUP_SH, env).returncode == 0
    shutil.rmtree(Path(env["BACKUP_ROOT"]) / "profiles")

    result = _run(DRILL_SH, env)
    assert result.returncode != 0
    report = sorted((Path(env["BACKUP_ROOT"]) / "drill-reports").glob("*.md"))[-1].read_text()
    assert "缺失" in report


def test_first_full_profiles_run_raises_the_disk_floor(env):
    """首次全量要吃掉一整份 profiles，门槛必须相应抬高，否则 30G 门槛挡不住 25G 写入。"""
    result = _run(BACKUP_SH, env, MIN_FREE_GB="1", FIRST_FULL_GB="999999999")
    assert result.returncode != 0
    assert "首次全量" in result.stdout
    assert _state_snapshots(env) == []


def test_drill_ignores_incomplete_backup_without_sentinel(env):
    """半途失败留下的残缺目录不能被当成"最新一份"——拿残缺备份跑出"通过"比没备份更糟。"""
    assert _run(BACKUP_SH, env).returncode == 0
    good = _state_snapshots(env)[-1]
    assert (good / "COMPLETE").exists(), "成功的备份必须落下完成哨兵"

    # 伪造一份"更新但残缺"的备份（时间戳更大、没有哨兵）
    broken = good.parent / "29991231T235959"
    shutil.copytree(good, broken)
    (broken / "COMPLETE").unlink()

    result = _run(DRILL_SH, env)
    assert result.returncode == 0, "应当跳过残缺目录、改用上一份完整备份"
    report = sorted((Path(env["BACKUP_ROOT"]) / "drill-reports").glob("*.md"))[-1].read_text()
    assert good.name in report, "演练应当选中带哨兵的那一份"
    assert "29991231" not in report


def test_drill_refuses_when_every_backup_is_incomplete(env):
    """一份完整的都没有时，必须明确报错，而不是拿残缺的凑合。"""
    assert _run(BACKUP_SH, env).returncode == 0
    (_state_snapshots(env)[-1] / "COMPLETE").unlink()

    result = _run(DRILL_SH, env)
    assert result.returncode != 0
    assert "COMPLETE" in result.stderr
