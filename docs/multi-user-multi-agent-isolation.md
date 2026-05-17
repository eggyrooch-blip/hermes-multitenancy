# Hermes 多用户 · 多 Agent · 用户层级隔离 — 权威设计

> 状态：设计（实现进行中，分支 `feature/lark-cli-multitenant`）。
> 本文档**取代** `docs/webui-multi-agent-plan.md` 中关于 P1 的过时表述（该文档基于"只看了 python 后端"的不完整调查，误判 webui 为"前端自报 profile、后端裸信任"）。本文档基于对 `hermes-multitenancy` + `hermes-web-ui` 双仓库的完整代码取证。
> 关联代码：`hermes_multitenancy/{routing,router,webui_broker_server}.py`、`sync/feishu_{org,hr}.py`；BFF `hermes-web-ui/packages/server/src/{services/request-context.ts,routes/hermes/group-chat.ts,controllers/hermes/kanban.ts,services/hermes/group-chat/index.ts,services/hermes/hermes-kanban.ts}`。

## 0. 目标场景（用户原话具象化）

一个公司 5 名员工 a,b,c,d,e：

- a 拥有 ai助理1/2/3 —— a 可与 1/2/3 拉群、看 1/2/3 的看板
- b 拥有 ai助理4/5/6
- c 拥有 ai助理7/8/9

铁律：**用户只能看到/操作自己创建的 Agent，看不到别人的**。owner = 飞书 openid，**不可变**（视图切换语义，不是归属转移）。每个 Agent 默认一级上游 = 通过 sync 创建的 user-profile。skills/CLI 经 multitenancy 统一分发（`merge_default` 修复已提交 `126c4a9`）。

把 hermes 当前的"**单用户、多 agent**"模型，改造成"**多用户、多 agent、按 owner 隔离**"。核心能力在 multitenancy 层完善；群聊与看板是必须遵守该隔离的 surface。

## 1. 当前事实（取证，file:line）

### 1.1 已经是对的（不要改）

- **身份建立是可信的**：`hermes-web-ui/.../services/request-context.ts:41-69` `verifyTrustedFeishuHeaders` 用 HMAC-SHA256 + `timingSafeEqual` + 时间戳窗口验证飞书 openid。webui 全部受保护路由前挂 `requireAuth`（`routes/index.ts:48`）。**webui 确实已强制飞书登录，openid 是密码学验证的**——这点之前的 plan 文档说错了。
- **routing 表的 user/group 隔离查询**：`routing.py:161-174 lookup_by_open_id`（`kind='user'`）、`:201-213 lookup_by_chat_id`（`kind='group'`）都带 kind 作用域，group 不会影子化 user 路由。
- **群行不可变 owner + 一群一 profile**：`routing.py:281-351 upsert_group` 用 `COALESCE(NULLIF(owner_open_id,''),excluded)` 保证 owner 写入即不可变；`idx_routing_chat_id_active_group` UNIQUE 保证一群一 profile。这是 user_agent owner 不可变规则的模板。
- **server-stamped-owner 参考模式**：`hermes-web-ui/.../controllers/hermes/jobs.ts:73-99 normalizeChatPlaneJobBody` 已经"剥离客户端传的 owner，重注服务端派生 openid"。这是要泛化复用的正确范式，不要另造鉴权。

### 1.2 三个 P0 跨 owner 泄漏（必须封堵）

| # | 位置 | 问题 |
|---|------|------|
| P0-1 | `webui_broker_server.py:307-376 handle_run` + `:133-147 _tenant_from_request` | 直接信任 `payload['profile_name']`/`X-Hermes-Profile`，唯一鉴权是一个共享静态 bearer（`:117-126 _authorized`），**不是 per-user 身份**。任何拿到 broker key 的调用方可以 run 成任意 profile。 |
| P0-2 | `request-context.ts:81-101 resolveProfileForOpenId` | `WHERE open_id=? AND active=1 LIMIT 1`。今天安全只因 `idx_routing_open_id_active_user` 保证一 openid≤1 user 行。**一旦一个 owner 有 N 个 agent，这个查询返回任意一个**，非确定性。 |
| P0-3 | `hermes-web-ui/.../routes/hermes/group-chat.ts:139-178` POST `/rooms/:roomId/agents` | `profile` 取自请求体，**零 owner/openid 校验**。任何已登录 webui 用户可把任意 profile 挂到任意房间。 |

### 1.3 一个 P1 数据丢失前置（必须先于迁移修）

`sync/feishu_hr.py:120-129 _desired_and_current` 是 **kind-blind** 的 `SELECT ... WHERE active=1`；`apply_users` 在 `soft_delete_missing=True`（`feishu_org.py:397` 全量 org sync 默认 True）时，会**软删每一个不在飞书员工列表里的 active 行——包括所有 group 行和未来的 self-agent 行**。这是真实的生产数据丢失向量（1259 行规模）。**必须在任何 backfill 迁移之前先收口**（US-02），否则一次全量 sync 会清掉所有多 agent/群数据。

### 1.4 kind='user' 语义重载

`router.py:2145-2150 _auto_provision_route`（未见过的飞书发送者自动建行）也写 `kind='user'` 且 `synced_at` 已设——**与 sync 建的 root 在 schema 上不可区分**，只有 `profile_name` 形状（`feishu_<userid>` vs `feishu_<openid>`）这个弱信号。因此"链顶必须 kind='user' 且 synced_at 非空"**不足以**唯一确定 root。需引入 `provenance` 列作为列级判定，而非字符串启发。

### 1.5 群聊 / 看板 surface 现状

- **群聊**：`hermes-web-ui` 的 `gc_rooms` 是 **BFF 独立 SQLite 表**，与 multitenancy **不连通**。无 owner 列、无过滤——`GET /rooms` 返回所有房间给所有已登录用户（`group-chat.ts:90-98 getAllRooms()`）。Socket.IO `/group-chat` 的 `join` 接受任意 roomId 不校验 owner。5 个 seam（建表/POST rooms/GET rooms/单房间操作/socket join）。group-chat 在 chat-plane 被 `enforcePlaneAccess` 整体 403，仅 admin-plane 可用。
- **看板**：单一全局 `~/.hermes/kanban.db`，由 **hermes CLI** 管理（不在 BFF、不在 python broker）。`controllers/hermes/kanban.ts` 从不读 `ctx.state.user`，零 owner 隔离。`assign`(body.profile) / `searchSessions`(?profile) 是 client 可控的。**关键边界**：`tasks` 表 schema 在 hermes CLI 的迁移系统内，**不在本仓库**——BFF 只能按 CLI 的 `--json` 输出过滤。需实跑确认 `hermes kanban list --json` 是否含 `created_by`、`kanban create` 是否接受 owner 参数。残留 DB 级隔离若需 CLI 改 schema，属本仓库越界项，必须书面标注。

## 2. Schema Delta（加列，不新建表；走 `routing.py:57-157` 既有幂等机制）

| 变更 | 内容 | 理由 |
|------|------|------|
| `_NEW_COLUMNS +=` | `agent_id TEXT`、`upstream_profile TEXT`、`provenance TEXT NOT NULL DEFAULT 'auto'` | agent_id=前端选 agent 的稳定键；upstream_profile=一级上游；provenance∈{sync,auto,self,group} 列级判定 root（解决 §1.4） |
| `_NEW_INDEXES +=` | `UNIQUE idx_routing_agent_id_active ON(agent_id) WHERE active=1 AND agent_id IS NOT NULL`；`idx_routing_upstream ON(upstream_profile) WHERE active=1 AND upstream_profile IS NOT NULL` | agent_id 必须 active 内全局唯一，否则 `lookup_agent` 歧义 |
| 收窄既有唯一索引 | `idx_routing_open_id_active_user` → `... AND provenance='sync'` | 释放"一 owner N 个 user_agent 共享空 open_id"，同时**保住"一 openid 一个 sync root"**这个 `resolveProfileForOpenId` 依赖的不变量。**此改动与登录解析改 `AND provenance='sync'` 必须同提交**（耦合，否则登录解析非确定性）。 |
| backfill（`_migrate` 内，幂等卫语 `agent_id IS NULL`） | 每 active 行：`agent_id=user_id`；`kind='user'` 行 `owner_open_id=COALESCE(owner_open_id,open_id)`；`provenance` 派生（synced_at 且 `feishu_<userid>` 形→`sync`；`feishu_<openid>` 形→`auto`；`kind='group'`→`group`）；group 行 `upstream_profile`=该 owner 的 root profile | backfill 后**所有 active 行 owner_open_id 非 NULL** → 可对每个查询加统一 `AND owner_open_id=:verified` 守卫；复用既有 `idx_routing_owner_open_id`（已 `(owner_open_id,kind) WHERE active=1`），无需新 owner 索引 |

不新建表。不破坏性迁移。1259 行规模 DB 副本上幂等跑两次结果必须完全一致（US-03 测试断言）。

## 3. owner 作用域路由 API（US-04）

- `lookup_agent(agent_id) -> RoutingRow`：`WHERE agent_id=? AND active=1 LIMIT 1`。
- `list_by_owner` 泛化：去掉 `kind=KIND_GROUP` 硬过滤 → `(owner_open_id, kind=None)`，`WHERE owner_open_id=? AND active=1 [AND kind=?]`，`ORDER BY (provenance='sync') DESC, created_at ASC`（root 排首）。
- `resolve_owner_root(open_id)`：`WHERE open_id=? AND active=1 AND kind='user' AND provenance='sync' LIMIT 1`（登录解析用，确定性返回 sync root）。**与索引收窄同提交**。
- `list_agents_for_owner(open_id)`：返回该 owner 全部 agent（sync root + group + 未来 self），供 webui 列举与越权校验。

路由解析算法（请求→profile）：`owner_open_id` 取自服务端已验证会话；`row=lookup_agent(agent_id)`，None→404；`row.owner_open_id != owner` 且 row 非该 owner sync root→403；不带 `agent_id`→回退该 owner 的 sync root（**存量单用户零回归**）；profile 由 multitenancy 填，前端不可覆盖。owner 永不被该流程修改。

## 4. 分阶段计划（映射 PRD）

| 阶段 | Story | 产物 | 关键验收 |
|------|-------|------|----------|
| 设计 | US-01 | 本文档 | 含 schema delta/3 P0/sync 前置/kanban 边界/分阶段 |
| 前置 | US-02 | sync 软删收口到 `kind='user'` | 全量 sync 后 group 行仍 active（fail-without 实证） |
| 地基 | US-03 | schema delta + 幂等迁移 + backfill | 二次迁移 no-op；owner_open_id 全非空；agent_id 全唯一 |
| 路由 | US-04 | lookup_agent / 泛化 list_by_owner / 收窄 open_id 唯一 + resolve_owner_root（耦合同提交） | 跨 owner 隔离；多 agent 下登录解析确定性 |
| 收口 | US-05 | sync 软删二次收窄到 `provenance='sync'` | 自建 agent 在全量 sync 后存活 |
| P0-1 | US-06 | webui_broker handle_run owner 校验 agent 解析 | 伪造他人 agent_id→403；不传→本人 sync root，零回归 |
| 群聊 | US-07 | gc_rooms owner 列 + 列表/单房间/socket join 校验 + agents 接口 owner 校验 | A 看不到 B 房间；A 用 B roomId/profile→拒绝 |
| 看板 | US-08 | kanban BFF 层 owner 隔离 + CLI 边界确认与记录 | A 不见 B 任务；client profile 越权→403；残留缺口书面化 |
| P1.5 | US-09 | 拉群人 owner 落库 | router 重启后首次 @ owner 仍正确；TTL 清理 |
| 收尾 | US-10 | 零回归总验证 + 跨模型 review + progress | 全量测试绿；reviewer verdict 记录；无 doc drift |

## 5. 硬约束

- **存量单用户零回归**：不带 agent_id / 单 profile openid 的端到端路径行为与改造前完全一致，有针对性回归测试佐证。
- **TDD**：每个行为变更先写一个**没有修复就失败**的回归测试，实证 fail-without 再 pass-with。
- **生产安全**：迁移幂等；US-02 必须先于 US-03 的 backfill；不碰远端 `10.2.14.249`。
- **仓库边界诚实**：kanban tasks 表 schema 属 hermes CLI，BFF 层尽力 + 残留缺口明确标注，不过度承诺。
