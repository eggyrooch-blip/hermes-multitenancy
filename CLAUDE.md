# CLAUDE.md — hermes-multitenancy

## TL;DR

这是 **hermes-agent 的飞书多租户路由插件**：`One Feishu bot, N users, N profiles`。

- **物理位置**：`/Users/kite/code/hermes-multitenancy`
- **加载方式**：`~/.hermes/plugins/multitenancy` → symlink → 本仓
- **入口契约**：注册 `pre_gateway_dispatch` hook（`__init__.py:18-38`），sync 回调 `create_task(handle_async(...))` 后 `return {"action": "skip"}` 让 gateway 主流程不再处理这条消息
- **完整内部架构**：见 **[ARCHITECTURE-GUIDE.md](./ARCHITECTURE-GUIDE.md)**（§1–§14 + 附录 A/B/C）

> 改之前请先把 ARCHITECTURE-GUIDE.md 读一遍。它讲了：hook 流程、sender 解析层叠、SQLite routing schema、open_id 列的过载语义、ContextVar 切 profile_home、RuntimePool LRU、AIAgent subprocess NDJSON 协议、UAT/streaming card 接口契约。

---

## 改动这个仓时

1. **改 `routing.py` 必看 `~/.hermes/multitenancy.db` schema**
    - `sqlite3 ~/.hermes/multitenancy.db ".schema"` 实查一遍
    - 物理 DB 是 `multitenancy.db`（**不是** `multitenancy_routing.db`——那个是 0 字节空壳）
    - 同一 `.db` 里有 `multitenancy_routing` + `multitenancy_sessions` 两表，共享 WAL
    - 改 schema 必须同时改 `routing.py:_SCHEMA` + 给现有数据库写迁移
    - 5/11 `RoutingTable.lookup_by_union_id` / `lookup_by_user_id` 是 dirty 未提交（见 ARCHITECTURE-GUIDE §5.6, §14）

2. **改 hook 流程，先分清 `plugin.py` / `router.py` / `runtime.py` 的分工**
    - 顶层 `__init__.py`：**只**做 `register(ctx)`——`override_pool` + 注册 hook，两步顺序不能颠倒（见 GUIDE §2.4）
    - `router.py`：所有 sync hook + async dispatch + slash 命令 + auto-provision 都在这。文件大（2500+ 行），改之前先 grep 验证锚点（GUIDE 附录 A）
    - `runtime.py`：**ContextVar 是真相，os.environ 是 legacy**。env-lock 按 loop 分（pytest 每 test 新 loop）。改这里要同时跑 `tests/test_concurrent_dispatch.py`

3. **改 `agent_real.py` 子进程协议要同时改 `aiagent_subprocess.py`**
    - 父进程（`agent_real.py`）和子进程（`aiagent_subprocess.py`）通过 stdin JSON + stdout NDJSON 通信
    - 加事件类型：父进程的 `_stream_aiagent_subprocess` 要识别；子进程的 `event_sink` callback 要 emit
    - exit code **永远 0**，错误通过 `out["error"]` 传——别改这一约定

4. **改 sync（`sync/feishu_org.py` / `sync/feishu_hr.py`）必跑 dry-run**
    - `python -m hermes_multitenancy.sync pull-feishu --dry-run --snapshot-out /tmp/snap.json`
    - `apply_users` 必须**幂等**：再跑一次同样的 list = 0 upsert + 0 soft_delete + N kept
    - `--soft-delete-missing` 默认行为不一样：full sync 默认开；`--dept` 子树同步默认关

5. **改 streaming/节流要同时验 card 路径 + edit fallback 路径**
    - `_stream_into_feishu_shared_consumer`（用 hermes 主线 `GatewayStreamConsumer`）和 legacy `_stream_into_feishu` 两条路径都要 tests/test_streaming_card_transport.py 跑过
    - 节流参数对齐 hermes 主线（`_PROGRESS_EDIT_INTERVAL=1.0s`），别擅自调小

---

## 红线

1. **hook 必须 sync 返回，不能 await**
    - `on_pre_gateway_dispatch` 是 sync def。所有 async 工作都通过 `loop.create_task(handle_async(...))` 放到后台
    - hook 立即返回 `{"action": "skip"}`——阻塞 hook 会卡住整个 gateway 主循环

2. **不要直接读 `~/.hermes/multitenancy.db`**
    - 一律走 `RoutingTable` / `SessionStore` 类。它们各开独立 `sqlite3.connect(..., check_same_thread=False)`，共享 WAL
    - 直接 sqlite3 命令行只用来诊断（read-only），不写

3. **不能跨 profile 共用 `os.environ['HERMES_HOME']`**
    - 必须经 `ProfileRuntime.dispatch`，它会先 set ContextVar 再拿 env-lock 切 env，dispatch 结束后还原
    - 单测如果绕过 ProfileRuntime 直接调 `_run_agent_fn`，会污染下一个 test 的 env

4. **UAT token 物理只一份在 shared `~/.hermes/feishu_uat/`，不要 per-profile 复制**
    - `_configure_feishu_uat_home` 把 `feishu_oapi.FEISHU_UAT_PATH / FEISHU_UAT_DIR` 改到 shared_home，**不要**改成 `profile_home/feishu_uat`
    - 同一物理用户的 token 不应该跟 profile 同步 N 份；profile 是会话/记忆隔离，**不是**身份隔离

5. **`open_id` 列被过载为"广义稳定键"，不是协议字段**
    - 它可以是真 `ou_*`、真 `on_*`、或 sync 自选的占位 token
    - 加 lookup 通道时**不要假设 open_id 列只装 `ou_*`**（5/11 dirty 修就是为了这种情况）

6. **跨仓 import 必须 `try/except`**
    - `from hermes_cli.commands import ...` / `from gateway.stream_consumer import ...` / `from agent.skill_commands import ...` 全用 try/except 兜底
    - 不是为了"防万一"，是为了让本仓 tests 在纯插件环境（无 hermes-agent checkout）能跑

7. **改 hook / 子进程协议后必须重启 gateway**
    - plugin import 是 startup-once 的
    - 改 .py 文件不需要 reinstall（symlink 直接生效），但要 `launchctl kickstart -k gui/$(id -u)/com.hermes.multitenant_router`

---

## 常用排障命令

```bash
# 1. 验证 plugin symlink 在位
ls -la ~/.hermes/plugins/multitenancy
# 期望: lrwxr-xr-x ... -> /Users/kite/code/hermes-multitenancy

# 2. dump routing/sessions schema
sqlite3 ~/.hermes/multitenancy.db ".schema"

# 3. 看当前 active 路由
sqlite3 ~/.hermes/multitenancy.db \
  "SELECT user_id, profile_name, open_id, union_id, last_active_at, version
   FROM multitenancy_routing WHERE active=1;"

# 4. 看软删历史（占位行 / 迁移残留）
sqlite3 ~/.hermes/multitenancy.db \
  "SELECT user_id, profile_name, open_id, union_id, deleted_at
   FROM multitenancy_routing WHERE active=0
   ORDER BY deleted_at DESC LIMIT 10;"

# 5. 看 per-(profile, user) 历史条数
sqlite3 ~/.hermes/multitenancy.db \
  "SELECT profile_name, user_key, COUNT(*)
   FROM multitenancy_sessions GROUP BY profile_name, user_key;"

# 6. dry-run pull Feishu Contact（不写 DB,看 plan 差异）
python -m hermes_multitenancy.sync pull-feishu --dry-run \
  --snapshot-out /tmp/feishu_snap.json

# 7. apply 一份 JSON UserSpec
python -m hermes_multitenancy.sync apply users.json

# 8. 看 active profile 目录
ls -la ~/.hermes/profiles/

# 9. 看子进程残留 approval 文件
ls -la /tmp/hermes-mt-approval-*/ 2>/dev/null

# 10. 跑测试
cd /Users/kite/code/hermes-multitenancy && pytest -x

# 11. 看 plugin 自身 git dirty
git -C /Users/kite/code/hermes-multitenancy status
git -C /Users/kite/code/hermes-multitenancy diff hermes_multitenancy/router.py hermes_multitenancy/routing.py
```

---

## 参考

- 上游主仓 `hermes-feishu-uat` 的 `ARCHITECTURE-GUIDE.md`：飞书 inbound / FeishuAdapter / UAT OAuth Device Flow / StreamingCardController 实现都在那
- Master GUIDE：`/Users/kite/Library/Mobile Documents/iCloud~md~obsidian/Documents/My-Second-Brain/hermes/ARCHITECTURE-GUIDE.md`（仓间关系 + 生产链路概览）
- Research 笔记：`hermes/.omc/research/02-multitenancy-plugin.md`（本仓最详细的原始调研，1050 行）

<!-- ftask:managed v1 — auto-generated; edit OUTSIDE this block -->
# Agent rules — hermes-multitenancy (managed by ftask)

- This repo is part of sunke's agent-OS. Agents NEVER run git directly here — use `bun ~/.claude/PAI/TOOLS/ftask.ts`.
- Base branch: `main`. Feature work happens in a `ftask new <slug>` worktree, never on `main` directly.
- Test gate: `ftask ship` runs `pytest -q` (auto-detected) in the rebased worktree and BLOCKS the merge if it fails.
- When you fix a bug found while troubleshooting (a 排障), add a regression test that FAILS without the fix BEFORE `ftask ship`, and record the root cause as one line under "Known gotchas" below.
- Global protocol: `~/.claude/CLAUDE.md` and `~/AGENTS.md` ("AGENT-OS" section). User cheatsheet: `~/code/AGENT-OS.md`.

## Known gotchas
- (root causes from 排障 sessions accrue here so the same bug is never debugged twice)
<!-- /ftask:managed -->
