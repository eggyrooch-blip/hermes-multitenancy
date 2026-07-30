# DEBT — 已知残留债(评审裁决 ship-with-debt 的遗留项)

## 2026-07-29 skillhub-plugin-skill-takeover · PT-001 残留(codex round3 concerns,#p1 收窄)

**场景(窄)**:profile 带既有 personal skills 时的**首次**插件同名接管中,`install_shared_skill_for_profile` 已替换目标后、`.hermes-personal-installs.json` 非原子写入截断/失败——单品链会被 `_restore_standalone_skill` 还原,但该 profile **无关的 personal 所有权元数据可能损坏**(首装无 `_active_plugin_state_transaction_locked` 快照兜底)。

**根治方向(评审建议)**:让 `_active_plugin_state_transaction_locked` 对首装也做快照(插件 manifest + 目标 + 两个 profile manifest),并加 failure-after-real-manifest-mutation 的逐字节回滚测试。

**现状缓解**:takeover 本体已可逆(displace→install 失败还原→成功才摘所有权);PT-002 全局锁已闭;此债只在"首装 × personal-installs 写入中途死"交叠时触发。

## 2026-07-30 mt-transient-replay-retry-silence · 部署重启伤亡三条残留(SPEC 明确 out of scope,待 sunke 拍板)

背景:2026-07-30 17:04:24 / 17:05:47 两次 gateway 重启(另一会话部署 mt,prod reflog 17:05:41 pull 到 2603f40),
in-flight run 随进程组一起吃 SIGTERM。qiaojunlong 那轮 `saw_done=False`、elapsed 34.245s 被打断。
本 slug 只修了"错误文案撒谎"(信号死不再拼陈旧 stderr 尾巴),下列三条**没修**:

**D1 — 无 graceful drain / 在途 run 不会自动重发(需产品拍板)**
部署或重启时,所有在途 run 直接死,用户只拿到一句"请重发"。根治方向:gateway 收到 SIGTERM 后
停止 admit 新 run + 等在途 run 收尾(有上限),或把被打断的 run 标记 `interrupted_by_restart`
并自动重投该轮输入。涉及用户可见语义(自动重发会重复扣费/重复副作用),不能由实施侧单方决定。

**D2 — gateway 收 SIGTERM 后非干净退出(`status=1/FAILURE`)**
journal 两次都是 `Stopping → exited status=1/FAILURE → Started`,不是干净停。退出码 1 说明
shutdown 路径本身有异常未处理。没查,单独立项。与 D1 相关但可独立修。

**D3 — 非流式路径(`agent_real/_core.py:3826/3845`)仍会拼 stderr 尾巴**
本次只改了流式 `agent_real/streaming.py` 的抛错点(SPEC 限定最小面)。`_run_aiagent_subprocess`
(飞书/cron 走的非流式路径)同样把 stderr 尾巴拼进用户可见错误;且信号死时 stdout 为空,
会先落到 `invalid JSON` 分支,文案更难懂。同一类"文案撒谎",修法可照抄本次的 returncode<0 分类。
