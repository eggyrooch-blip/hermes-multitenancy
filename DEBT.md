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

## 2026-08-01 v0190-compat-commit-triage · `44f6d71` 多问题澄清卡能力未搬入 main(需产品拍板)

**背景**:`feat/local-hermes-agent-v0190-compat` 分支(分叉点 `c5e4110` @ 2026-07-10)上的
`44f6d71 feat: improve Feishu card feedback` 是一整套澄清卡能力增强,281 行实质代码 +
约 1700 行测试,横跨 10 个源文件。main 在分叉后独立走了 161 个 commit,澄清卡是另一套实现。
本 slug 的 triage 判定它为**真缺口**,但**不在 triage 里搬运** —— 它是 `feat:` 不是 `fix:`,
整体搬运是独立项目,需要产品决策。其余 5 个 commit 的裁定见
`.ftask/v0190-compat-commit-triage/TRIAGE.md`。

**能力差(一句话)**:main 的澄清卡是「单问题 + 提交即完」,compat 的是
「多问题 + 处理中/过期状态卡 + 结构化答案」。
规模对照 `hermes_multitenancy/feishu_clarify_cards.py`:compat **520 行** vs main **251 行**。

**main 缺失的 9 个符号及归属**:
- `feishu_clarify_cards.py`:`_normalize_questions`(多问题归一化)、
  `handle_feishu_clarify_resolved`(完成回收)、`_structured_answers`(结构化多答案)、
  `_clarify_status_card`、`_clarify_processing_card`
- `agent_real/_core.py`:`_claim_clarify_timeout`、`_clarify_response_expired`、
  `_request_clarify_response`
- `router/streaming.py`:`_tool_display_title`

**要拍的板**:多问题澄清是不是我们要的产品形态?是 → 单独立 slug,按功能块分批搬
(建议顺序:`_normalize_questions` + `build_clarify_card` 多问题签名 → 状态卡 →
`handle_feishu_clarify_resolved` 回收链 → agent_real 超时/过期)。否 → 关闭本条,
compat 分支可直接删除。

**注**:本次 triage 顺带实证了一件与上一条 DEBT(v0190 类补丁装错模块)相关的事 ——
在 main + 核心 v0.19.1 上,活 gateway 启动日志里 **全部 13 类飞书补丁的安装目标都是
`hermes_plugins.feishu_platform.adapter.FeishuAdapter`**(合成模块,即运行时真用的那份),
不再是 `plugins.platforms.feishu.adapter`。疑似 `d0433e5`(materialize deferred platform)
已比那条 DEBT 描述的范围修得更广。**未逐个复核,仅作线索**,建议下次碰这块时用
`__code__.co_filename` 逐个复验后再决定关不关那条债(注意:`__module__` 会被
`functools.wraps` 伪造,不能用作判据)。

## 2026-08-01 — CI 门禁上线时排除的项（都要放回去）

CI（`.gitlab-ci.yml`）首次真跑全量测试时暴露的，逐条记账。排除的都是「这台机器
没有的外部依赖」或「CI 抓出来的真缺陷」，不是放宽断言。

### D1 — 脚本里硬编码开发机绝对路径（**CI 抓出来的真缺陷**，优先级最高）

- `scripts/lark_cli_matrix_runner.mjs:18`
  `import { io } from '/Users/kite/code/hermes-web-ui/node_modules/socket.io-client/build/esm/index.js'`
  另有 4 处 `/Users/kite/.hermes/...`（第 138、158、388、393 行）
- `scripts/feishu_file_media_matrix_runner.py:27`
  `SHARED_HOME = Path("/Users/kite/.hermes")`，第 387 行 `"/Users/kite/.hermes/profiles" in text`

后果：这 17 条测试**只在 sunke 那台 Mac 上能过**，生产机和任何新同事的机器都会失败
（容器里直接 `PermissionError: '/Users'`）。这违反 CLAUDE.md 的「Never hardcode paths」。
修法：改成相对路径 / 走仓内依赖，然后从 `.gitlab-ci.yml` 的 `--ignore` 里拿掉。

### D2 — `tests/test_billing_readiness.py` 6 条权限不变量测试（**安全测试，不该长期排除**）

容器里报 `readiness_replay_store_permissions_invalid`。已排除 root 身份这个因素
（job 改成非 root 的 `ci` 用户跑仍失败）。需要定位是容器的 umask/挂载语义不同，
还是 `billing_readiness.py:568` 的权限假设本身太紧。**这是安全面，别拖。**

### D3 — 需要外部业务 CLI 的 2 条

- `tests/test_plugin_ingest.py::test_install_clis_skips_when_present` — 要 `kep-cli`，CI 里没有也不该有
- `tests/test_aiagent_subprocess.py::test_session_search_proxy_covers_real_agent_tool_dispatch`

修法：让它们在依赖缺失时自行 skip，而不是 fail。

### D4 — GitLab 双推被自己的分支保护挡住

`main` 设了 `push=No one` 之后，ftask 的 `postship --finalize` 往 GitLab 推 main 会被
pre-receive 拒（GitHub 正常）。这是**设计预期**——一切必须走 MR——但在
`ship_backend=pr` 打通之前，GitLab 镜像会滞后。当前靠临时放开保护同步基线，
不可持续。修法：把 `ship_backend` 翻成 `pr`。
