# WebUI 多 Profile 智能体 — 架构规划

> 状态：规划（未实现）。本文档是实现前的 SPEC，决策已与 owner 对齐。
> 关联分支：`feature/lark-cli-multitenant`。关联代码：`hermes_multitenancy/{router,routing,webui_broker_server,agent_real}.py`。

## 0. 已锁定的决策（owner 拍板）

| 决策点 | 选择 | 影响 |
|---|---|---|
| 问题1 skills 分发 | feishu toolset 改 `merge_default` | ✅ 已实现并验证（见 §1） |
| 问题2 "webui 中切换" 语义 | **视图切换，不改归属** | owner 保持不可变（现有代码已正确）；**不需要** reassign/鉴权/审计/SOUL 同步那一整套，问题2 大幅简化 |
| 推进方式 | 先修 P1，再出本规划 | 本文档即"再出规划"产物 |

**关键简化**：因为是"视图切换"而非"转移归属"，问题2 与问题3 合并成**同一个模型**——"一个 owner 拥有 N 个 agent，webui 让 owner 在自己有权的 agent 间切换当前操作对象"。owner 一旦由拉群人确定即不可变，这正是现有代码的行为，**无需改动现有 provision 不变量**。

## 1. 问题1：已完成（背景）

`router.py:_apply_lark_cli_profile_defaults` 原本把 feishu 平台 toolset 模式钉死成 `explicit`，导致 `_resolve_enabled_toolsets` 原样返回 `["lark-cli"]`、丢掉 `skills` 工具集——skill 文件软链进了 profile 但 agent 清单里看不到（即 owner 报告的"技能里看不到 lark skills"）。

修复：`feishu` 改为 `merge_default`，与 `api_server`/`webui` 一致，默认工具集（含 `skills`）与 `lark-cli` 取并集。回归测试 `test_feishu_profile_keeps_skills_toolset_after_lark_cli_defaults` 已加，去掉修复即 `AssertionError: assert 'skills' in ['lark-cli']`，确定性复现原症状。全量受影响测试 98 passed / 0 failed。

**遗留（非本次范围，建议后续）**：`hermes-agent/hermes_cli/tools_config.py:683` 用 `PLATFORMS[platform]` 索引未知 key `webui` 会 `KeyError`，当前被 `agent_real.py` 的 `webui→api_server` remap 偶然挡住（非主动触发）。建议改 `.get` 安全兜底，消除"webui 静默拿到零工具集"的潜在路径。

## 2. 问题2 现状盘点（视图切换语义下）

数据面**已建好且线上跑通**，视图切换语义下几乎无缺口：

| 能力 | 状态 | 证据 |
|---|---|---|
| 拉群→自动建专属 profile，一群一 profile | ✅ 完成 | `group_inviter_hook.py:165-192` 抓拉群人；`router.py:2382-2533` provision；线上真实行 `feishu_group_dfe8bc83167b_e18e` |
| 一群一 profile 不变量 | ✅ 硬保证 | DB 部分唯一索引 + chat_id 确定性命名 + 原子 upsert（旧并发竞态已修） |
| 拉群人=owner，持久化 | ✅ 完成 | DB `owner_open_id` 列 + profile json + SOUL；没抓到拉群人则拒绝 provision |
| 多 agent 隔离（记忆/会话） | ✅ 完成 | session key `multitenancy:feishu:<profile>:<chat_id>:<user_key>` |
| webui **读取** owner 的 agent 列表 | ⚠️ 数据 API 存在但零调用方 | `routing.py:215 list_by_owner()` 已写好，无 HTTP 端点暴露 |
| webui **切换**当前操作 agent | ❌ 不存在 | 无 agents 列举/选择端点 |

视图切换语义下**唯一真实缺口**：把休眠的 `list_by_owner()` 通过一个 webui 端点激活 + 前端切换器。**不涉及** owner 可变、鉴权转移、SOUL 同步（这些在"转移归属"语义下才需要，已排除）。

**仍需修的隐患（与语义无关）**：拉群人捕获用的是**单进程内存缓存**（`router.py:55 _chat_inviter_cache`）。拉群到首次 @ 之间若 router 重启或多 worker，owner 可能丢→provision 被拒→需重新加 bot。建议把拉群人在 `bot.added` 时落一条短时 DB pending 行，而非只存内存。

## 3. 目标架构

### 3.1 核心模型

```
WebUI 登录用户（owner_open_id，来自飞书 OAuth 已校验）
   │ 拥有
   ├── Agent#0 = sync 建的 user-profile   ← 默认一级上游（root，upstream=NULL）
   ├── Agent#1 "我的销售助手"（upstream→#0） ← 用户自建（后续阶段）
   └── Agent#k 群聊 P1 测试（kind=group，upstream→#0） ← 拉群自动建
```

四条铁律：

1. **multitenancy 是路由+绑定唯一权威**。webui 前端**不再自报** `profile_name`；改为传 `agent_id`，服务端用已登录会话的 `owner_open_id` 校验归属后解析出 `profile_name`。这堵住当前"客户端信任"洞（`webui_broker_server.py:315` 前端自报 profile）。
2. **每个 agent 默认一级上游 = 该 owner 的 sync user-profile**。upstream 用于建/同步时继承模型/凭据/skill 默认，不是运行时会话继承。群 agent upstream 锁定为拉群人 root，且**凭据继承在 kind='group' 时硬切断**（沿用现有群隔离铁律，群不继承个人 UAT）。
3. **skill/CLI 统一分发**。复用 `_sync_default_skills_for_profile` + `_apply_lark_cli_profile_defaults`（P1 已修，现在 skills 真的会下发）+ upstream 链兜底。建 N 个 agent 全自动拿同一批 shared skill 软链 + lark-cli toolset + 继承 root 模型配置。
4. **群聊/任务/看板都接 agent 选择器**。三个面统一"先选 agent（agent_id）→ 服务端解析 profile"。

### 3.2 路由解析算法（请求→profile）

```
resolve_agent(owner_open_id, agent_id, surface) -> profile_name | reject:
  1. owner_open_id 取自 webui 已校验会话，绝不信前端 body
  2. row = routing.lookup_agent(agent_id)；None → 404
  3. row.owner_open_id != owner_open_id → 403（例外：row 是该 owner 的 sync user-profile）
  4. not row.active → 409
  5. profile = row.profile_name
  6. upstream 链只在 provision/config-load 时遍历一次并物化（MAX_DEPTH=4，环检测，链顶必须是 kind='user' sync profile），不在每请求热路径递归
  7. surface∈{chat,task,kanban} 仅写 metadata 供审计/限流，不改 profile 解析
  8. return profile（RunRequest.profile_name 由 multitenancy 填，前端不可覆盖）
```

视图切换 = 前端在"我有权的 agent 列表"里选一个 `agent_id`，后续请求都带它；服务端每次跑步骤 1-8。owner 永不被这个流程修改。

### 3.3 数据模型草图（加列，不新建表；幂等迁移照 `routing.py` 既有模式）

- `agent_id` TEXT — owner 维度稳定标识，前端选 agent 用，替代传 profile_name。sync 行=canonical id；群/自建行=系统生成短稳定 id。
- `upstream_profile` TEXT NULL — 一级上游 profile_name。sync user 行=NULL（root）；群/自建行默认=该 owner 的 root。
- `kind` 扩展 — 现有 `user`/`group`，新增 `user_agent`（用户自建，后续阶段才用）。
- `owner_open_id` — 复用现有列。扩展为所有非 sync 行都写，值=创建者飞书 open_id。
- `display_label` — 复用现有列，webui 侧边栏显示名，用户可改可重名。
- 索引：复用 `idx_routing_owner_open_id`；新增 `agent_id` 查找索引、`upstream_profile` 反查索引。

不变量：链顶必须是 `kind='user'` 且 `synced_at` 非空的 sync profile。**owner_open_id 仍不可变**（视图切换语义下无人改它，现有 COALESCE 写法保留）。

## 4. 分阶段交付（最小可发先行）

| 阶段 | 范围 | 验收检查 |
|---|---|---|
| **P1 鉴权堵洞**（纯安全，零新功能，可独立发） | `handle_run`/`handle_create_job` 不再信前端 `profile_name`；前端传 `agent_id`，服务端用会话 `owner_open_id`+lookup 校验归属后解析。`agent_id` 对存量单用户=其 sync profile，行为不变。 | 现有 webui 测试零回归；伪造他人 `agent_id`→403；无 `agent_id`→回退 owner sync root |
| **P1.5 拉群人落库**（修隐患） | `bot.added` 时拉群人写一条短时 DB pending 行，provision 时优先读 DB 再读内存缓存。 | router 重启后再首次 @，owner 仍正确；多 worker 下不丢 owner |
| **P2 数据模型迁移** | 加 `agent_id`/`upstream_profile` 列+索引（幂等）；回填 sync 行 `agent_id=canonical,upstream=NULL`、群行 `upstream=owner root`；`list_by_owner` 泛化去掉 group 硬过滤。 | 生产 DB 副本幂等跑两次一致；旧行全回填出合法 root 链 |
| **P3 Agents 端点 + 视图切换** | 新增 `GET /api/run-broker/agents`：列出 owner 全部 agent（sync root + 群 agent）。webui 侧边栏列出并可切换当前 agent。**不含**建/改归属。 | 用户切换 agent，群聊请求命中对应 profile；列表只回本人有权的；切换不改任何 owner |
| **P4 三面接选择器** | 群聊+任务请求统一带 `agent_id`；看板列/卡片携带 `agent_id`（第一阶段非持久，前端态即可）。 | 同一用户切多 agent，群聊/任务/看板分别命中对应 profile 且隔离 |
| **P5 上游链继承 + 用户自建 agent** | config-load 按 upstream 链回退继承（模型/skill 缺失向 root 兜底）；支持用户自建 agent（provision 骨架+默认 upstream=root+统一 skill 分发）。 | thin agent 不配 model 也能跑；自建 agent 自动含 shared skill+lark-cli toolset |

P1 单独可发：它把"前端自报 profile"这个客户端信任洞堵上，即使后续阶段不做也已有安全价值。

## 5. 仍需 owner 拍板的开放问题（视图切换语义下保留的）

1. **拉群人落库（P1.5）做不做、优先级**？这是现有隐患（owner 可能丢），与新功能无关，建议尽早做。
2. **`agent_id` 命名空间**：系统生成不可变 id（推荐）还是允许用户起名？与 sync canonical id 的隔离规则。
3. **看板"列/卡片→agent"绑定要不要持久化**？第一阶段不持久最省（前端态+每请求带 agent_id）；持久化需确认看板产品形态（是否真有"列绑定 agent"语义）。
4. **每 owner agent 数量上限 + RuntimePool 压力**：`RuntimePool` 默认 `max_loaded=50` 全局共享，N 用户×M agent 放大冷启动。建议单 owner ≤10，是否需 pool 调参。
5. **webui `owner_open_id` 可信来源**：鉴权依赖"会话已校验飞书 open_id"。当前 `handle_run` 不强制要求已授权。P1 鉴权要求 webui 用户先完成飞书登录——这是 P1 前置依赖，必须先定。
6. **离职用户**：其 sync root 被 sync `soft_delete_missing` 失活时，挂在下面的群 agent 链顶断了——孤儿化标记+fallback shared config（推荐 b+告警）还是一并停用（合规视角）？
