# Hermes token 用量进排行榜 — 部署 RUNBOOK（工件 1）

让每个人用 Hermes 的 token 消耗进入企业 AI 消耗排行榜，一个人的多个智能体（含群聊里
@bot 触发的）累加到本人。配套工件 2 = `enterprise-token-leaderboard` 仓给收集端
`dev_collector.py` 新增的 `/v1/usage/report` 端点（先部署它，再开本侧）。

## 两个部件

1. **回合台账写入**（`hermes_multitenancy/token_usage_ledger.py`，已挂进 `agent_real.py`）
   每个回合把 `谁(open_id)/profile/平台/群或单聊/模型/in·out·total token` 追加一行到
   `/var/log/hermes/token-usage.jsonl`。**默认关闭**。

   **开关在哪设（重要，别设错）**：token 计数器只在被沙箱化的 AIAgent 子进程里，但子进程
   沙箱不允许写 `/var/log/hermes`，所以子进程只读出 usage 透传给**父进程**，由
   **gateway 父进程（非沙箱）写台账**（`_write_token_ledger_from_child`，与
   `conversation_audit` 同模式）。开关由父进程读自身环境，因此只需设进
   **gateway 进程自己的环境**（`hermes-gateway.service` 的 `Environment=` 或其
   EnvironmentFile，与 `HERMES_RUN_BROKER_KEY` 等同一处）：
   ```
   HERMES_TOKEN_USAGE_LEDGER_ENABLED=1
   ```
   **不要**逐个写 1279 份 profile `.env`——一处 gateway env 即覆盖全员。
   （写发生在父进程，无需进子进程 env 白名单。）

2. **每小时 uploader**（`hermes_multitenancy/token_usage_uploader.py` + 本目录 systemd 单元）
   读台账 → **按 owner 归属**（见下）→ 聚合当天 → 经 **feishu-sync**（`sync.fetch_contact_directory`，
   复用租户 token）解析成企业邮箱/部门 → POST 到收集端，`source=hermes`、`client=Hermes`。

## 归属模型（sunke 2026-06-12）：agent 属于谁，消耗就是谁的

不按「每条消息谁发的」记，按 **owner** 记账。uploader 用 multitenancy 路由表
（`~/.hermes/multitenancy.db`，可 `HERMES_MULTITENANCY_DB` 覆盖）解析每行的归属人：

- **群聊**（`chat_type=group`）→ `lookup_by_chat_id(chat_id).owner_open_id`（**拉群的人**）。
  群里别人 @ 你拉进去的 bot，也算你的消耗。路由表查不到该群 → 丢弃该行，**绝不把全群量
  硬安给某人**（防误记护栏）。
- **个人/DM** → 发送人本人；sender 为空（如 webui ingest 服务身份）→ 退用 profile 的 owner
  （profile 名即 user_id，路由表有归属）。都拿不到 → 丢弃。

结果：你拉进 N 个群的 bot + 你的所有 agent 的消耗，全部归你一人。**漏记不误记**：唯一不记的
是路由表都查不到归属的行（极少），绝不会算到错误的人头上。

## 安装 uploader 到 hermes-1（hermes 用户）

1. 替换 `hermes-token-uploader.service` 里的占位符：
   - `@PYTHON@` → 跑 multitenancy 包的 python（gateway venv 的解释器绝对路径）。
   - `<sync-root-profile-home>` → feishu-sync 的 sync-root profile home（持有租户 app 凭据的那个）。
   - `<tokscale-report-key>` → 上报 Bearer（= 员工 Mac MDM 同一把，收集端 COLLECTOR_API_TOKENS 之一）。
2. 拷进用户级 systemd 并启用：
   ```
   cp deploy/hermes-token-uploader.{service,timer} ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now hermes-token-uploader.timer
   ```
3. 先手动干跑一次确认归属正确（不写网）：
   ```
   systemctl --user start hermes-token-uploader.service   # 真跑（默认只发今天）
   # 或本地干跑：
   HERMES_HOME=<sync-root> python3 -m hermes_multitenancy.token_usage_uploader --dry-run
   ```

### 首次初始化：回写台账里所有历史日期

台账是新建的，**部署前没有 Hermes 逐回合 token 历史**（之前没人按发送人记过 token）。
所以「能采集的所有历史」= 台账启用后已积累的所有日期。首次接入时跑一次 `--backfill`，
把台账里**全部日期**逐日上报（端点会从所有 day 行重算 month/lifetime，回写完总量自动正确）：
```
HERMES_HOME=<sync-root> python3 -m hermes_multitenancy.token_usage_uploader --backfill --dry-run  # 先看
HERMES_HOME=<sync-root> HERMES_TOKSCALE_REPORT_KEY=<key> \
  python3 -m hermes_multitenancy.token_usage_uploader --backfill                                   # 真回写
```
之后交给每小时 timer（默认只发当天，增量幂等）。`--backfill` 可随时重跑，不会翻倍。

## 上线前必查（命门，别跳）

- **feishu-sync 通讯录里有没有 email**：`fetch_contact_directory` 取 `enterprise_email`
  或 `email`。若 multitenancy 飞书 app 没有 `contact:user.email:readonly` scope，
  目录里就没邮箱 → 全员解析不到 → 不上报。**部署时先 `--dry-run` 看 `records` 是否非空、
  邮箱是否 = 排行榜其它源（飞连/MDM）用的同一企业邮箱**（否则同一人会裂成两行，靠
  leaderboard 的 `email_merge.py` 兜，但应尽量对齐）。
- **HERMES_HOME 指对**：指错 profile → 取不到租户凭据 → 目录拉取失败（会退本地缓存或全跳过）。
- 工件 2 的 `/v1/usage/report` 端点要**先**在收集端宿主上线，否则 POST 404。

## 已知限制（漏记不误记 — 都不会让任何人拿到错误数字）

- **路由表查不到归属的行**：群 chat_id 不在路由表、或空 sender 且 profile 也查不到 owner →
  丢弃该行（极少）。绝不误记。
- **失败回合不计**：报错中断的回合即使消耗了 token 也不落账（轻微少计）。

## 验证（端到端）

`--dry-run` 看某人聚合正确 → 真跑一次 → 排行榜 `/v1/leaderboard` 该 email 总 token 增加、
`/v1/breakdown?by=client` 出现 `Hermes` → 隔小时再看不翻倍。
