"""deploy/hermes-backup.sh 与 deploy/hermes-restore-drill.sh 的真逻辑测试。

不碰任何运行时代码，也不碰生产：全部在 tmp_path 里造假库跑真脚本。
覆盖三处会真出事的地方：
  1. 磁盘前置检查 —— 空间不够必须拒绝，且不能留下半份备份
  2. 保留策略 —— 安全机制不能自己把盘写满
  3. 演练判据 —— 数据被改坏时必须报失败（负控制；没有它，演练就只是个橡皮图章）
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
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
        # 必须一起钉死：默认 FIRST_FULL_GB=25 会把首次全量的门槛抬到 1+25=26G，
        # 于是测试变成「取决于这台机器还剩多少盘」—— 26G 以下就全红。
        # 2026-08-01 在只剩 25G 的 Mac 上真的踩到了。磁盘门槛另有专门的测试覆盖。
        "FIRST_FULL_GB": "0",
        "RESTORE_HEADROOM_GB": "0",
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


def test_missing_critical_credential_blocks_complete_backup(env):
    (Path(env["HERMES_HOME_DIR"]) / "auth.json").unlink()

    result = _run(BACKUP_SH, env)

    assert result.returncode != 0
    assert "关键配置缺失" in result.stderr
    assert _state_snapshots(env) == []


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


def test_drill_fails_when_backup_exceeds_rpo(env):
    """超过 24 小时的快照即使数据完整也不能算恢复演练通过。"""
    assert _run(BACKUP_SH, env).returncode == 0
    manifest = _state_snapshots(env)[-1] / "MANIFEST.txt"
    stale = (datetime.now() - timedelta(hours=25)).strftime("%Y%m%dT%H%M%S")
    manifest.write_text(
        manifest.read_text().replace(
            next(line for line in manifest.read_text().splitlines() if line.startswith("backup_ts=")),
            f"backup_ts={stale}",
        )
    )

    result = _run(DRILL_SH, env)

    assert result.returncode != 0
    assert "RESULT status=FAIL" in result.stdout
    assert "rpo_seconds=" in result.stdout


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
    victim = Path(env["HERMES_HOME_DIR"]) / "profiles" / "u1" / "secret.txt"
    victim.write_text("x\n")
    victim.chmod(0o000)
    try:
        result = _run(BACKUP_SH, env)
        assert result.returncode != 0
        assert "静默漏数据" in result.stderr
        assert "safe_locator=unreadable:" in result.stdout
        assert env["HERMES_HOME_DIR"] not in result.stdout
        assert "chown -R" not in result.stdout
        locator_map = next(Path(env["BACKUP_ROOT"]).glob("unreadable-items-*"))
        assert locator_map.stat().st_mode & 0o777 == 0o600
        assert json.loads(locator_map.read_text().splitlines()[0])["relative_path"] == "u1/secret.txt"
    finally:
        victim.chmod(0o644)


def test_unreadable_profile_directory_blocks_with_safe_error(env):
    victim = Path(env["HERMES_HOME_DIR"]) / "profiles" / "u1"
    victim.chmod(0o000)
    try:
        result = _run(BACKUP_SH, env)
        assert result.returncode != 0
        assert "静默漏数据" in result.stderr
        assert env["HERMES_HOME_DIR"] not in result.stdout + result.stderr
    finally:
        victim.chmod(0o755)


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


def test_drill_restores_profiles_into_the_isolated_directory(env):
    """恢复演练必须真的还原 profiles，不能只在原快照上抽样检查。"""
    assert _run(BACKUP_SH, env).returncode == 0

    result = _run(DRILL_SH, env, KEEP_RESTORE="1")

    assert result.returncode == 0, result.stdout + result.stderr
    work = next(Path(env["DRILL_ROOT"]).glob("hermes-drill-*"))
    assert (work / "profiles" / "u1" / "f.txt").read_text() == "hello\n"


def test_drill_fails_when_profiles_snapshot_is_missing(env):
    """有 state 备份却没有 profiles 快照 = 半份备份，演练必须判失败。"""
    assert _run(BACKUP_SH, env).returncode == 0
    shutil.rmtree(Path(env["BACKUP_ROOT"]) / "profiles")

    result = _run(DRILL_SH, env)
    assert result.returncode != 0
    report = sorted((Path(env["BACKUP_ROOT"]) / "drill-reports").glob("*.md"))[-1].read_text()
    assert "缺失" in report


def test_drill_fails_when_profile_snapshot_was_corrupted_after_backup(env):
    assert _run(BACKUP_SH, env).returncode == 0
    profile = next((Path(env["BACKUP_ROOT"]) / "profiles").glob("*/u1/f.txt"))
    profile.write_text("corrupted after backup\n")

    result = _run(DRILL_SH, env)

    assert result.returncode != 0
    report = sorted((Path(env["BACKUP_ROOT"]) / "drill-reports").glob("*.md"))[-1].read_text()
    assert "固化 manifest 失败" in report


def test_drill_fails_when_profile_symlink_target_changed_after_backup(env):
    live = Path(env["HERMES_HOME_DIR"]) / "profiles" / "u1"
    (live / "link").symlink_to("f.txt")
    assert _run(BACKUP_SH, env).returncode == 0
    link = next((Path(env["BACKUP_ROOT"]) / "profiles").glob("*/u1/link"))
    link.unlink()
    link.symlink_to("missing.txt")

    result = _run(DRILL_SH, env)

    assert result.returncode != 0
    report = sorted((Path(env["BACKUP_ROOT"]) / "drill-reports").glob("*.md"))[-1].read_text()
    assert "固化 manifest 失败" in report


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


def test_late_profiles_failure_leaves_no_complete_backup(env):
    """哨兵必须等两层都成功才落。否则 profiles 失败后，演练会挑中这份"完整"的
    状态核心、再自己配一份更老的 profiles，对一份从未同时完成的备份报"通过"。"""
    # 先做一份正常的，确认哨兵里记了配套的 profiles 快照
    assert _run(BACKUP_SH, env).returncode == 0
    sentinel = (_state_snapshots(env)[-1] / "COMPLETE").read_text()
    assert "profiles_snapshot=" in sentinel

    # 再让 profiles 那层必失败：把源目录换成一个不可进入的目录
    before = len(_state_snapshots(env))
    victim = Path(env["HERMES_HOME_DIR"]) / "profiles"
    victim.chmod(0o000)
    try:
        result = _run(BACKUP_SH, env)
        assert result.returncode != 0
        # 半份 state 目录不能留下来冒充"最新一份"
        assert len(_state_snapshots(env)) == before
    finally:
        victim.chmod(0o755)


def test_drill_reports_all_six_known_dbs_by_name(env):
    """报告要按固定六库清单点名，而不是按备份目录里恰好有什么。"""
    assert _run(BACKUP_SH, env).returncode == 0
    assert _run(DRILL_SH, env).returncode == 0

    report = sorted((Path(env["BACKUP_ROOT"]) / "drill-reports").glob("*.md"))[-1].read_text()
    assert "## 六库清单对账" in report
    for known in [
        "multitenancy.db", "state.db", "kanban.db",
        "multitenancy_routing.db", "hermes-web-ui.db", "web-ui.db",
    ]:
        assert known in report, f"{known} 未在报告里点名"
    assert "0 字节，按设计跳过" in report


def test_overlapping_db_names_are_matched_exactly(env):
    """`web-ui.db` 是 `hermes-web-ui.db` 的子串。子串匹配会把「真缺失」误报成
    「按设计跳过的空库」，一个真缺的库就这么混过去。必须按逗号分词精确匹配。"""
    # 让 web-ui.db 真缺失（删掉），hermes-web-ui.db 正常存在
    (Path(env["HERMES_WEBUI_DIR"]) / "web-ui.db").unlink()
    assert _run(BACKUP_SH, env).returncode == 0

    manifest = (_state_snapshots(env)[-1] / "MANIFEST.txt").read_text()
    assert "db_missing=web-ui.db," in manifest

    result = _run(DRILL_SH, env)
    report = sorted((Path(env["BACKUP_ROOT"]) / "drill-reports").glob("*.md"))[-1].read_text()
    assert "| `web-ui.db` | **缺失**" in report, "真缺失被误判成空库了"
    assert result.returncode != 0, "已知库缺失必须判失败"


def test_drill_uses_the_profiles_snapshot_pinned_in_sentinel(env):
    """两层必须是同一次备份的产物。一个更新的孤儿 profiles 目录不能被
    配到更老的 state 上，凑出一个从未同时产出的组合。"""
    assert _run(BACKUP_SH, env).returncode == 0
    snap = _state_snapshots(env)[-1]
    pinned = Path(
        [l for l in (snap / "COMPLETE").read_text().splitlines()
         if l.startswith("profiles_snapshot=")][0].split("=", 1)[1]
    )
    assert pinned.is_dir()

    # 伪造一个"更新的"孤儿 profiles 目录
    orphan = pinned.parent / "29991231T235959"
    orphan.mkdir()
    (orphan / "junk.txt").write_text("not part of any backup\n")

    assert _run(DRILL_SH, env).returncode == 0
    report = sorted((Path(env["BACKUP_ROOT"]) / "drill-reports").glob("*.md"))[-1].read_text()
    assert pinned.name in report, "演练应当用哨兵钉住的那一份"
    assert "29991231" not in report, "孤儿快照不能被选中"


def test_drill_rejects_profiles_snapshot_outside_backup_root(env, tmp_path):
    assert _run(BACKUP_SH, env).returncode == 0
    sentinel = _state_snapshots(env)[-1] / "COMPLETE"
    sentinel.write_text(f"completed_at=x\nprofiles_snapshot={tmp_path}\n")

    result = _run(DRILL_SH, env)

    assert result.returncode != 0
    assert "配套快照路径越界" in result.stderr
    assert not list(Path(env["DRILL_ROOT"]).glob("hermes-drill-*"))


def test_drill_refuses_when_scratch_space_cannot_hold_restore(env):
    assert _run(BACKUP_SH, env).returncode == 0

    result = _run(DRILL_SH, env, RESTORE_HEADROOM_GB="999999999")

    assert result.returncode != 0
    assert "恢复空间不足" in result.stderr
    assert not list(Path(env["DRILL_ROOT"]).glob("hermes-drill-*"))
