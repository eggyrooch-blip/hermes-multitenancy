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

## 2026-08-01 v0190-compat-commit-triage · `44f6d71` 多问题澄清卡:**弃**,唯一真缺口已修(残留 4 符号未复核)

**裁定(2026-08-01)**:`44f6d71 feat: improve Feishu card feedback` 的多问题机制**弃**,不搬。
理由是无驱动而非无价值:核心 clarify schema 收的是**单数** `question` string,
`_ClarifyEntry.signature()` 也只发单个问题,多问题分支在 main 上永远走不到,搬过去是不可达代码。
随之作废:`_normalize_questions`、`_structured_answers`、多问题版 `build_clarify_card`、
`_clarify_status_card`、`_clarify_processing_card`(处理中态也不要 —— 提交 toast 已经说了
「已提交,正在继续」,同一张卡再多一次往返没收益)。

**唯一真缺口 = 卡片终态,已修**:main 收到 `clarify_resolved` 只 `continue` 吞事件(防 payload
dict 泄进回复正文),那张表单卡就永远停在「等待你的选择」。slug `clarify-card-final-state` 修掉了:
发卡 handle 存进有界 map,resolved 时 pop 出来走 `update_auth_card` 落终态(pop 即 write-once 闸),
`_stream_into_feishu` 与 `_stream_into_feishu_shared_consumer` 两条分支都覆盖,各有一条改坏即红的测试。

**顺带搁置、未复核**:`agent_real/_core.py` 的 `_claim_clarify_timeout` /
`_clarify_response_expired` / `_request_clarify_response` 与 `router/streaming.py` 的
`_tool_display_title` —— 与多问题一并搁置,**没有逐条核过 main 是否已有等价路径**,
真要用时重新 triage,别把本条当"已确认无缺口"。其余 5 个 commit 的裁定见
`.ftask/v0190-compat-commit-triage/TRIAGE.md`。

**注**:那次 triage 顺带实证了一件与上一条 DEBT(v0190 类补丁装错模块)相关的事 ——
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

## 2026-08-01 — 原子发布执行器上线时的已知缺口

### D5 — 发布前备份只做状态核心，不含 profiles

`hermes-release.sh` 调 `hermes-backup.sh` 时传了 `SKIP_PROFILES=1`：发布回滚包只需要
6 个库 + 配置，40G profiles 每次发布都拷一遍不现实。代价：如果一次发布同时改坏了
profiles 里的东西，回滚包救不了 —— 得靠每日备份（保留 7 份硬链快照）。
目前发布不碰 profiles，风险可接受；哪天发布开始动 profiles 结构，这条要重新评估。

### D6 — webui 构建期若需要 .env，当前没喂

`.env` 已挪到 `~/.hermes-web-ui/.env` 并在 release 目录里做软链，运行期没问题。
但如果将来构建期（vite/next 的 define 注入）也要读它，现在的流程没有显式传入。
届时要从稳定路径 source 进构建环境，且不能把密钥写进 release 树。

### D7 — 首次部署的鸡生蛋

`~/code/hermes-*` 必须先是软链，执行器才肯工作（否则直接 die）。这次是手工迁移的，
迁移步骤只在 SPEC 的 Plan 里，没有脚本化。换机器或重建时要照着 Plan 手工再来一遍。

### D8 — 保留策略只按目录数，不看磁盘

`KEEP_RELEASES=3` 是固定份数。webui 一个 release 目录 834M（含 node_modules），
3 份约 2.5G，当前 275G 可用没问题。但没有「磁盘低于 X 就多裁」的逻辑。

### D9 — 原子发布执行器:终局评审 BLOCK 的开放项（2026-08-01）

grok 终局裁决为 BLOCK，开放项全是**测试覆盖完整性**，不是已实现行为的缺陷：
dangling PREV 拒绝、STABLE_BIN 首次 bootstrap 的 live 路径、expert 单元启动断言、
六库快照布局在 release 层的显式记录、EnvironmentFiles 预检。
已补三条（裁剪必须保住回滚目标、非软链拒绝、STABLE_BIN 只在成功后同步），18/18 绿。
剩余项需要注入 live systemctl 或属于 ops checklist。**未 ship，等 sunke 放行。**

同轮降级为债的还有：`hermes-release.sh:backup:missing-script-skips-silently` ——
`BACKUP_SH` 不可执行时静默跳过发布前备份。当前生产上它存在且可执行，
但换机器/路径变动时会静默失去"不带备份不发布"这条保护。修法：缺失即 die。

### D10 — 发布执行器：评审 round 1（重开后）留作债的四条

已修：首次安装鸡生蛋（新增 `deploy/install-hermes-release.sh`）、回滚 flip 不检查返回值
（半坏状态会打印假的 ROLLED_BACK）、悬空回滚目标（动手前就拒）。

**留作债的：**
- `release-tag-fetch:deploys-uncertified-cached-tags-after-fetch-failure` —— 两个远端都拉不到时
  会用本地缓存的标签继续。若一个坏标签在远端被撤销、而 hermes-1 恰好断网，定时器仍可能部署它。
  修法：拉取失败就 log + exit 0，不碰服务。
- `build_worktree:abbreviated-sha-directory-reuses-wrong-release` —— 目录名只用 7/8 位 SHA，
  已存在就直接复用而不校验 HEAD。两个提交前缀相同时会静默部署成前一个。
  修法：目录名用完整 SHA，或复用前 `git rev-parse HEAD` 核对。
- `pre-release-backup:accepts-incomplete-database-backup` —— 只要备份退出 0 就放行，
  没有解析 MANIFEST 确认六个库都在。修法：按 db_count/db_missing 卡。
- SIM_VERDICT=trace_inadequate、QA_VERDICT=absent —— 生产实弹证据在 STATE.md 与本方案稿里，
  但没进 SIM_TRACE 的 capture 块。

**一条误报已核实并驳回：** `hermes_multitenancy/cron/orchestrator.py` 被指删了 run_claim 逻辑
及其回归测试。实测本分支 vs main 是 **892 行纯新增、0 删除、只碰 6 个新文件**，
`orchestrator.py` 一行未动。

## 2026-08-03 · release editable-finder 钉死解析路径（发布假生效陷阱）
- 现象: hermes-release.sh 翻 `~/code/hermes-multitenancy` 软链后，venv 的
  `__editable___hermes_multitenancy_finder.py` 仍钉着上一个 `releases/mt-<旧sha>` 解析路径
  → gateway 重启后照跑旧代码，RELEASE OK + 12 探针全绿也发现不了（release-20260803-02 实锤）。
- 根因: 带外部署用 `pip/uv install -e <解析路径>` 会把 MAPPING 写死；uv 对软链路径也会 canonicalize，
  软链间接层形同虚设。
- 临时解法(已执行): `uv pip install --no-deps -e ~/code/hermes-multitenancy` + 重启双 gateway。
- 根治方向: hermes-release.sh 在 flip 后无条件重装 editable 并加「进程实际 import 路径 == 本次 release 目录」探针
  （probe 判据用 `hermes_multitenancy.__file__`，别信 deployed-release 文件）。

## 2026-08-03 · 个人 token 档位闸只能防误操作，不能防故意规避（sunke 显式接受为债）

> 三轮跨模型评审（codex gpt-5.6-sol）round-3 终裁为 **block**，sunke 2026-08-03 显式接受为债后放行。
> 记在此以免只活在评审回执里。

- **缺陷**：档位校验靠"员工把 token 命名为 `hermes`，我们列出他的 token 找到那一行读 scopes"。
  这回答的是「**有个**叫 hermes 的 token 权限如何」，不是「**我这个** token 权限如何」。
  员工可以建一个只读的 `hermes` token，提交时却粘贴另一个带 `api` 的 token：
  匹配到只读那行 → 档位闸通过 → 而入库的是大权限的那个。
- **为什么不修**：能回答"我是哪一行"的只有 `GET /personal_access_tokens/self`，
  **CE 14.10 没有这个端点**。`last_used_at` 官方文档明说 24 小时才更新一次，`created_at` 同样
  无法把 token **值**绑定到元数据行。这个版本上没有任何 API 能做这个绑定。
  （前两版方案也都栽在同一处：先是"拒收 api"让 glab 完全不可用，再是"行为探针"——
  14.10 对所有 GET 同放 `api`/`read_api`，非 GET 端点实测连 `api` 都被拒。）
- **为什么可以作为债**：绕过**不产生提权**——hermes 拿到的是员工本来就持有的权限，
  差别只是标签不准，不是越权；且绕过必须**故意**（没人会不小心先建只读 token 再粘贴强 token）。
  定位因此是**防误操作的提示**，不是安全边界。
- **已落地的诚实措施**：
  - vault 行的 scopes 追加 `gitlab:scope-binding-unverified` 标记；
    **存储的 scope 列表永不可作为审计依据**（本文件下一条就是这个教训的来源）。
  - 飞书卡片明写"能帮你发现填错档位，但没法严格保证你粘贴的就是那个 token，请自行确认"。
- **将来根治**：GitLab 升到有 `/personal_access_tokens/self` 的版本（16.x+）后，
  改为直接问"我是谁"，即可把档位闸变成真边界。

## 2026-08-03 · 全局 GitLab token 实为管理员凭据、明文分发全员（P0，sunke 已知，排期止损）

> 2026-08-03 gitlab-user-token 调研时实测发现。sunke 拍板：先做个人 token 功能，本条另排期止损。
> **不是本 slug 的交付范围**，记在此以免只活在聊天里。

- **现象**: vault 里 `__shared__ / kep-prd-skills / gitlab` 这条凭据，scope 标签写的是
  `["gitlab:read"]`，实测权限与标签完全不符。用 prod profile 里的明文 token 打 API：
  - `GET /api/v4/user` → 200，返回 `is_admin: True`（**GitLab 管理员账号**，非服务账号）
  - `GET /api/v4/projects` → 200
  - `GET /api/v4/personal_access_tokens` → 200 ← 该端点需要完整 `api` scope，`read_api` 不够
- **爆炸半径**: 该 token 以明文落在 **1426 个员工 profile** 的
  `workspace/credentials/gitlab.token`（共 2041 个 profile），任一员工会话或一次 prompt
  injection 都能 `cat` 到，拿到的是管理员 API 权限。经它列出的同账号活跃 token 有 12 条、
  **全部 `expires_at: None`**（jenkins / bjtx-jenkins-01 / murphysec / zion / tianfeng /
  glib_cli 等），其中 5 条带 `write_repository` —— 即泄漏面还包含整条 CI 凭据链。
- **未证实的部分（别当护身符）**: 试过 `POST /api/v4/projects` 返回 403，但那次请求参数为空，
  403 不能证明它不能写。按 `api` scope 定义应为完整读写；**没有在生产上实证写权限，也不该试**。
- **止损方向**（按优先级）:
  1. 换成最小权限的**专用服务账号** token（非管理员），scope 只给 `read_repository`；
     并让 vault 里的 scope 标签与实际一致（现在是假的，不能作为审计依据）。
  2. 收回明文分发：改为只注 `GITLAB_TOKEN` env、不写 profile 文件，并清理既有 1426 份文件。
     （注：本 slug 已让**新增的个人 token** 走"只注 env 不落文件"，但不动存量文件。）
  3. 给该 admin 账号下 12 条永不过期 token 定期轮换/设过期——需先确认各自归属与用途，
     不能直接 revoke（会打断 Jenkins / murphysec 等 CI）。

## 2026-08-04 · run-broker 47 个 handler 里 16 个不做 owner 收口（主密钥持有者可达，独立于 owner-spoof 那条）

> 来源：slug `run-broker-owner-spoof-failclosed` 的类扫（Algorithm 规则 9），SPEC 完成项第 65 行写了
> 「属独立债，不在本 slug 范围」但**没落到本文件**。2026-08-04 在 main `d3843a8` 上独立复跑类扫复现：
> 47 条路由 / 23 条过 owner 收口 / **16 条只过 `_authorized`**，与 SPEC 记的数字逐字一致。
> 记在此以免只活在 SPEC 和评审回执里。

- **已经关掉的部分**：沙箱里那枚 run-scoped token 按路由钉死在 `/api/run-broker/jobs` 前缀，
  对这 16 个兄弟一次性 401。**agent 侧不再是入口**。
- **仍然开着的部分**：`_authorized` 认的另一把是 **run-broker 主密钥**。任何持主密钥的调用方
  （WebUI 服务端、内部脚本、以及任何能读到主密钥的人）打这 16 个端点时，**broker 不校验它代表谁**，
  owner 完全来自请求自报或干脆不看。修 owner-spoof 那条**没有改变**这一点。
- **清单**（`hermes_multitenancy/webui_broker_server.py` @ `d3843a8`，行号为 handler 定义行）：

  | 端点 | handler | 行 | 影响面 |
  |---|---|---|---|
  | `POST /profiles` | `handle_provision_profile` | 1972 | 任意开户 |
  | `GET /agents/shared` | `handle_list_shared_agents` | 2056 | 跨租户读共享关系 |
  | `GET /agents/{id}/shares` | `handle_list_agent_shares` | 2083 | 同上 |
  | `POST /agents/{id}/shares` | `handle_grant_agent_share` | 2105 | **代他人授权** |
  | `DELETE /agents/{id}/shares/{key}` | `handle_revoke_agent_share` | 2150 | **代他人撤权** |
  | `GET /plugin-assets/{plugin}/{asset}` | `handle_plugin_asset` | 2527 | 跨租户读插件资产 |
  | `GET /kanban/boards` | `handle_kanban_boards` | 2813 | 跨租户读看板 |
  | `GET /kanban/capabilities` | `handle_kanban_capabilities` | 2776 | 同上 |
  | `GET /kanban/assignees` | `handle_kanban_assignees` | 2759 | 同上（含人员名单） |
  | `GET /kanban/stats` | `handle_kanban_stats` | 2796 | 同上 |
  | `GET /kanban/tasks` | `handle_kanban_tasks` | 2831 | 同上 |
  | `POST /kanban/tasks` | `handle_kanban_create_task` | 2853 | **代他人建任务** |
  | `POST /kanban/dispatch` | `handle_kanban_dispatch` | 2872 | **代他人派活（会真跑 agent）** |
  | `GET /skills/audit` | `handle_skill_audit` | 2965 | 跨租户读技能审计 |

  另两条计入 16、但**不算债**，写出来是免得下次类扫又当新发现：
  `GET /health`（2 个字段，无租户数据）、`POST /feishu/helpdesk/events`
  （ws-adapter 内部扇入口，owner 从事件体推出，本来就不是按调用方分租户的）。
  → **真正的债面是 14 条**，其中 4 条是写操作。

- **为什么现在不修**：这 16 条的调用方目前只有 WebUI 服务端（它自己已在 chat-plane 做过用户鉴权，
  再盖 owner 头下来），改成 owner 收口要逐个确认「WebUI 盖的头是不是够权威」，
  不是一次机械替换；而且 `handle_provision_profile` / `handle_kanban_dispatch` 这类本来就是
  **管理面**语义，收口方式应该是「区分管理面密钥和租户面密钥」，不是给每个 handler 补 `_owner_scoped_tenant`。
- **根治方向**：给 `_authorized` 分层——主密钥只放行显式标注为管理面的路由，
  其余一律要求可解析到租户（run-scoped token 或服务端签名的 owner 头，而非自报头）；
  新增 handler 默认落在「要租户」那一侧，fail-closed。类扫脚本应固化成测试，
  防止 47/23/16 这三个数字无声漂移。

## 2026-08-04 · 生产发布漂移在构造上不可察觉（软链被手工翻过，执行器只比标签名）

> 来源：2026-08-04 安全评审附带发现——生产 webui 软链指着 `6aca93cc`，而当时最新标签 `release-20260804-02`
> 的 annotation 记的是 `290c1152`。当天 `-03` 标签把它钉回一致了，**但成因没查**。本条是成因。

- **机制（不是操作事故，是设计缺口）**：`deploy/hermes-release.sh:21,73-80` 里，执行器认定的
  「当前已部署版本」是 `~/.hermes/deployed-release` 里的**一个标签名**，不是 `readlink` 出来的实际软链。
  最新标签 == 状态文件里的标签 → `log "已是最新"; exit 0`，**全程不看 `~/code/hermes-web-ui` 指向哪**。
  于是：任何带外的 `ln -sfn` 都不会被发现，而且会一直挂着，直到下一个新标签把两个仓一起翻掉。
  脚本对标签 annotation 的校验很硬（40 位 hex、缺仓即拒、SHA 必须在仓里），
  但**从来没有「活的软链 == 当前标签钉的 SHA」这条不变量**。
- **为什么会有人绕过**（诱因，非借口）：
  - `ftask ship` 只负责合进 main，**不负责部署**；部署要另外手打一枚 annotated `release-*` 标签。
  - 打标签**没有任何工具**：全仓 + `LIFEOS/TOOLS/ftask.ts` 里没有一处创建 `release-*`，
    只有 `hermes-release.sh` 读它。这一步纯靠人记得。
  - 触发是 `hermes-release.timer` 每天 18:00 一次。急修等窗口 → ssh 上去 build + `ln -sfn` + restart
    是**更短更确定**的路径，而且成功了没有任何东西会说它不对。
  - 参见本文件 2026-08-03「release editable-finder 钉死解析路径」——那条也是带外部署留下的坑，
    同一个诱因的第二次发作。
- **最小修法**：在 `hermes-release.sh` 早退分支（`$TAG == $CURRENT`）**之前**加漂移探针：
  读 `$CURRENT` 标签的 annotation → 两个 SHA → 与 `readlink` 出来的 `mt-<sha8>` / `webui-<sha8>` 比对，
  不等就大声报（日志 + 告警），**但不要自动翻回去**（带外部署可能正是当时唯一止血手段，
  静默改回等于二次事故）。这条探针在「无新标签」路径上也必须跑——那正是漂移唯一存活的地方。
- **配套（可选，但成因就在这）**：给打标签这一步一个动词（从 main 的两仓 HEAD 生成 annotation、
  校验 SHA 确在 main、推受保护标签），并允许手动立刻触发一次执行器，
  让「正规路径」比 ssh 手翻更省事——否则诱因还在。

## 2026-08-06 · `expires_at` 一个字段兼任「展示事实」与「本地失效门」（codex 复评，deferred）

**现状**：vault 行的 `expires_at` 既是「这枚 token 什么时候到期」的事实记录，又被
`credentials.py:158` 拿来做本地失效判定（到点即拒绝注入）。两种用途的正确来源不同：
前者应如实反映 GitLab，后者本应由 GitLab 说了算。

**本单（gitlab-drop-expiry-gate）做了什么**：sunke 拍板「有效期归上游 GitLab 管，不是
hermes 该判的」，于是 intake 不再因到期日拒收；并且**过去的日期不写进 vault（存 None）**——
否则 intake 报「已保存」，运行时却按我们的时钟判过期拒注入，之后每次调用静默回落到共享
凭据，是最坏的一种失败形态。

**残留（本单不修）**：这只压住了最坏形态，没有拆掉两种用途的耦合。时钟/时区分歧仍可能在
未来某个边界重新表现出来（比如 GitLab 认为还有效、我们已判过期）。真正的解是把字段拆成
「展示用的 expires_at（如实抄）」和「本地失效用的 enforce_until（可为空＝不本地失效）」，
涉及 vault schema 与所有 provider 的消费方，独立重构。

**为什么不夹带进本单**：本单是 credential intake 的准入语义变更，sunke 正在等着绑 token；
把 schema 重构塞进来会把一个可当场验证的小改动变成跨 provider 的大改。

**触发条件**：再出现一次「凭据显示已绑定但实际走了共享凭据」的报障，就该排这条。

## 2026-08-06 · `billing_readiness.verify_enabled_environment` 生产代码已无调用方（billing-drop-release-gate）

**现状**：`startup_guard._validate_billing_cohort` 摘除对 `verify_enabled_environment` 的调用后
（同批 sunke 拍板：`ExecStartPre` 无前导 `-`，任何 raise 都是全员网关停服，ceremony 这类校验
不该挂在启动路径上），核查发现 `billing_shadow.py` 只 `import` 了同模块的 `BillingReadinessError`
与 `cohort_hash`，**不调用** `verify_enabled_environment` 本体。也就是说这个函数在生产代码里
**已经没有调用方**——不是"仍被 shadow/CLI 用"（早期 release note 曾这样写，已改正），而是死代码。

**为什么不删**：签名 artifact 验证机制本身（8 env + 双 artifact 验签 + nonce live recheck）
可能还会被 operator 手工核查复用，且删除属于另一个决策面（`billing_readiness.py` 本体不在
本单改动范围内，见 SPEC Out of Scope）。留着不碍事，删了省不了什么。

**触发条件**：下次碰 `billing_readiness.py` 时确认是否还有任何调用方（含手工 CLI 脚本/文档
指引的调用方式）；如果确认彻底无人用，直接删掉 `verify_enabled_environment` 连带
`tests/test_billing_readiness.py` 里只测它的用例，不必等一个专门 slug。

## 2026-08-06 · codex 终审 ship-with-debt 残留两条 #p1（billing-degrade-not-refuse）

**背景**：codex 终局评审判 ship-with-debt（无 P0）。回执两次被 ftask 拒收——台账里的 open
finding ID 长 123/131 字符，超出 `FINDING_ID` 正则的 120 上限（写入侧没做同样校验），叠加
「终局裁决禁开新 ID」+「concerns 必须带 ID」形成记账死结。债按 ftask 自己的约定
（"Review findings shipped as debt (concerns never block)"）落在这里，不落回执。

**#1 `BillingGatewayClient._post` 畸形 DNS label 分类成可降级**（原 ID:
`billing_credentials.py:BillingGatewayClient._post/_gateway_rejection:broker-not-configured-degrades#p1`）
- 症状：label 含下划线/空组件/首尾连字符的 broker URL 过了 ASCII 正则 + IDNA 兜底，到 opener
  才炸 DNS，被归成 `broker_unavailable`（可降级）→ **永久配置错**的 cohort 请求会静默走共享
  key，账一直记不上而无人知晓。codex 实测 `https://foo_bar.example` 复现。
- 修法：`_post` 统一 IDNA 规范化 + 逐 label 校验（1-63 字节、非空、字母数字边界、主体仅
  字母数字连字符、总长合法）；bearer 拒控制字符；请求构造期 `ValueError` 归
  `broker_not_configured`（不可降级）；补 `ensure()` 全链测试断言 opener 从未被调用。

**#2 `_is_vault_unavailable` 漏 SQLite 扩展错误码**（原 ID:
`billing_credentials.py:_load_payload/_save_payload/_delete_payload:operational-error-classification-too-broad#p1`）
- 症状：精确比对原生码，`SQLITE_LOCKED_SHAREDCACHE`(262) 等扩展码不被认作"暂态不可用"，
  仅仅锁表的瞬间 cohort 成员被 `RunRejected` 拒服务——与本单「锁表应降级」的既定策略相反。
  codex 用真实共享缓存锁复现。
- 修法：要求整数码并以 `(code & 0xFF)` 比对 BUSY/LOCKED/FULL；补一条经 manager 路径的
  共享缓存锁回归测试。

**触发条件**：两条都在降级边界上，下一个碰 `billing_credentials.py` 的 slug 顺手清；或计费
全员稳定运行一周后专门开一个小 slug 一起修。

## 2026-08-06 · `repair_metadata` / `_repair_employee_key` 已成生产不可达（billing-runtime-never-mints）

**现状**：401 自愈的唯一生产调用方 `agent_real/_core.py` 改为降级（标 invalid + 剥计费
metadata + 重试一次），不再调 `repair_billing_metadata`。于是 `BillingIdentityPreparer.repair_metadata`
与 `_repair_employee_key` 在生产代码里**没有调用方**，只有测试还在调。

**为什么不这轮删**：本单是全员开关前的阻塞项，删公开函数要连带改 3 个测试文件，
midnight 前扩大 diff 不值得。两个分支都已改成**不能签发**（一个抛 `BillingUnavailable`，
一个传 `allow_mint=False`），所以即使有人把调用方接回来，也不会重新引入
「员工请求路径上签发」这条本单要消掉的东西。

**触发条件**：下次碰 `billing_identity.py` 时，确认无人调用后直接删这两个函数 +
`repair_billing_metadata` 包装 + 只测它们的用例；同时把 `CREDENTIAL_SOURCE` 分支判断
一起清掉。
