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

**D2 — gateway 收 SIGTERM 后非干净退出(`status=1/FAILURE`)** — 2026-07-30 已查清,**结论:不是 bug,不改**
journal 两次都是 `Stopping → exited status=1/FAILURE → Started`。原猜测"shutdown 路径有未处理异常"**被推翻**:
退出码 1 是上游**刻意**的,证据链(核心仓 `hermes-agent`,非本仓):
- `gateway/run.py:11166` `_signal_initiated_shutdown = False` + `:11190` 信号处理器置 True(仅当不是 `--replace` 计划接管)
- `gateway/run.py:11326-11331` `if _signal_initiated_shutdown and not runner._restart_requested: return False`
  —— 注释原话:"exit non-zero so systemd's Restart=on-failure revives the process";覆盖 `hermes update` 杀网关 / 外部 kill / 容器运行时乱发信号
- `hermes_cli/gateway.py:2353-2355`(以及 `gateway/run.py:11355-11357`)`success = asyncio.run(start_gateway(...)); if not success: sys.exit(1)`
- 设计注释 `gateway/run.py:11161-11166` 明确解释为什么 `systemctl stop` 安全:systemd 独立跟踪 stop-requested,`Restart=` 不会为主动 stop 触发
复现推理:部署脚本 `systemctl restart` → systemd 发 SIGTERM → 处理器置 `_signal_initiated_shutdown=True`(无 takeover marker)
→ `runner.stop()` 正常 drain(`run.py:2680-2703` 会 interrupt 在途 agent 并等 5s)→ 返回 False → `sys.exit(1)` → journal 记 FAILURE → systemd 拉起。
**为什么不改**:①代码在 SPEC 禁改的核心仓;②把它改成 exit 0 就等于删掉"意外 SIGTERM 后 systemd 复活网关"的语义,
是拿可用性换日志好看;③systemd 无法在带内区分"systemctl restart 发的 SIGTERM"和"外人 kill 发的 SIGTERM",
想干净退出必须先解决这个区分问题(可行方向:部署脚本改用 `SIGUSR1`(`run.py:11229` 已注册 restart handler,走 `_restart_requested` 分支 → exit 0),
或部署前写 takeover marker 走 `--replace` 计划接管路径 `run.py:11178-11188`)。
**实际代价**:仅日志/告警噪音(`OnFailure=` 会误报),重启本身正常。要消噪就走上面两个方向之一,别动退出码。
> 注:本条依据本机 `~/code/hermes-agent` 工作树读码(HEAD `e045d2809`)+ 上一 slug 记录的 journal 证据;未 ssh 生产核对已安装版本。

**D4 — 上游 flake:`test_hook_dispatch.py::test_concurrent_uploaded_files_keep_profile_and_prompt_isolated`**
全量套件里间歇红(`AttributeError: 'FullFeishuAdapter' object has no attribute 'send'`,
`router/commands.py:423`),单跑该文件 5/5 绿,只在全量上下文出现 → 跨测试污染。
**已核实与本 slug 无关**:在干净 `origin/main`(32db743,零本地改动)上全量跑 3 次,第 2 次同样红。
会间歇卡住 ftask TEST 闸,建议单独立项查 monkeypatch/adapter 的 teardown 顺序。

> **2026-07-30 已关闭(slug `test-timing-flakes-fix` 复核)**:不是跨测试污染,是真竞态——
> `RoutingTable` 的 sqlite 连接以 `check_same_thread=False` 无锁共享,billing 工作线程
> (`prepare_billing_request` → `asyncio.to_thread`)与事件循环线程同时 step 同一份缓存
> prepared statement → `sqlite3.InterfaceError`,被 `except Exception` 吞掉后才在 423 行
> 触到桩 adapter 没有的 `send`。已由 `2a64a7f`(routing.py `@_serialized` RLock)根治。
> 复核证据:16 路 CPU 负载下 `tests/test_hook_dispatch.py` ×20 全绿(该用例 0 失败)。

**D3 — 非流式路径(`agent_real/_core.py:3826/3845`)仍会拼 stderr 尾巴**
本次只改了流式 `agent_real/streaming.py` 的抛错点(SPEC 限定最小面)。`_run_aiagent_subprocess`
(飞书/cron 走的非流式路径)同样把 stderr 尾巴拼进用户可见错误;且信号死时 stdout 为空,
会先落到 `invalid JSON` 分支,文案更难懂。同一类"文案撒谎",修法可照抄本次的 returncode<0 分类。
## 2026-07-30 gateway-shutdown-drain-fix · register 期类补丁落在克隆类上(范围外,仅记债)

**现象(prod v0190 boot 2026-07-30 18:04:19 实证)**:核心 plugin loader
`hermes_cli/plugins.py:_load_directory_module` 会把 feishu 平台插件源码在合成名
`hermes_plugins.feishu_platform.adapter` 下**重新 exec**,与
`plugins.platforms.feishu.adapter` 是两份独立类对象。multitenancy `register()`
执行时合成模块**尚不存在**,于是 `load_feishu_module()` 只能返回 fallback 克隆类
——所有 register 期类补丁都打在**运行时不用的那份类**上。日志实证:
`installed cred_auth card-action hook on plugins.platforms.feishu.adapter.FeishuAdapter`
而运行时 adapter 的 logger 名是 `hermes_plugins.feishu_platform.adapter`。

**受影响面(本 slug 未处理)**:`_patch_feishu_open_id_send`、
`_patch_feishu_outbound_link_render`、`install_feishu_inbound_richtext_patch`、
merge_forward / reply_quote / reaction_lifecycle / group_valve / auth_hub_actions、
`group_inviter_hook`、`feishu_group_topic_session` —— 凡在 register 期按类打的补丁同理。
`feishu_adapter_compat` 里的 sys.modules 优先只在**调用时合成模块已存在**才救得回来。

**本 slug 只修了自己那一个**:`_patch_feishu_send_retry_shutdown_fatal` 在
`_note_live_gateway()`(启动期、平台构建后拿到 gateway 实例时)重跑一次,重新解析到
synthetic 类补上;register 期安装保留兜其它装载顺序。

**根治方向**:把"启动期重跑全部类补丁"做成一个统一入口(或等核心给 gateway-started
钩子),而不是每个补丁各自补一次。修前必须逐个确认幂等 marker 挂在方法对象上、
且重跑不会双重包裹。
