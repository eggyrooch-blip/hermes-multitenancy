# hermes-multitenancy

> **一个飞书 Bot, N 个用户, N 套档案。** 一个 [hermes-agent](https://github.com/NousResearch/hermes-agent) 插件,把每个飞书用户路由到独立的 profile (独立的 SOUL.md, 会话, 记忆, LLM 凭证) —— 不动 hermes-agent 一行代码。

[English](README.md) | **简体中文**

[![tests](https://img.shields.io/badge/tests-71%20passing-brightgreen)](#-测试)
[![hermes 0 patches](https://img.shields.io/badge/hermes--agent-0%20patches-brightgreen)](#-为什么能保持兼容)
[![real Feishu verified](https://img.shields.io/badge/real%20Feishu-verified-brightgreen)](#-端到端验证)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

---

## 😖 为什么写这个插件 (痛点)

我很喜欢 hermes-agent —— 这是我用过最完整的个人 Agent 运行时。但是想把它放进公司里给所有人用的时候, 撞墙了:

> **Hermes 假设 1 个 Bot = 1 个用户。** 一个 "profile" 就是一个 HERMES_HOME 目录。每个 profile 起一个独立的 gateway 进程, 自己持有飞书 App 凭证, 自己跑一个 websocket。所以我想把一个 Bot 部署给 1000 人公司用的方案, 全军覆没:
>
> - **方案 A** —— 起 1000 个 hermes 进程? 1000 × 86 MB 的 lark_oapi 装不进内存, 也别指望 IT 给你开 1000 个飞书 App。
> - **方案 B** —— 一个 Bot, 一个 profile, 1000 人共用一份 SOUL? 那就不是 "千人千面" 了 —— 每个员工拿到的人格一样, 没有用户级记忆。
> - **方案 C** —— 改 `feishu.py` 按用户分流? 每次 hermes 升级我都得手动 re-patch。我试了一下午, 放弃了。
> - **方案 D** —— Fork hermes-agent, 自己维护一条分支? 等于背一笔永久债, 而上游还在快速演进。

**这些方案都不通。** 我想要 hermes 的丰富 UX (流式, 表情反应, 多轮, 会话) **同时** 多租户路由 **同时** 不动 hermes-agent 一行代码。

这个插件就是答案: 用一个 **`pre_gateway_dispatch` hook** 拦截每条飞书消息, 在 SQLite 路由表里查到这个用户对应哪个 profile, 然后派发到一个独立的 `ProfileRuntime` (持有独立的 SOUL + 历史 + LLM 客户端)。一个 Bot 服务 N 个用户, 每个用户感觉自己拥有一个专属的 hermes Agent。

```
飞书 user A ─┐
飞书 user B ─┼─► 1 个 Bot ─► hermes gateway ─► [pre_gateway_dispatch hook]
飞书 user C ─┘                                   │
                                                ├─► profile_a/SOUL.md + 独立 sessions + 独立 LLM
                                                ├─► profile_b/SOUL.md + 独立 sessions + 独立 LLM
                                                └─► profile_c/SOUL.md + 独立 sessions + 独立 LLM
```

**hermes-agent: 改动 0 行。** `git status` 可验。

---

## 🚀 快速上手

### 1. 安装插件

```bash
git clone https://github.com/eggyrooch-blip/hermes-multitenancy ~/projects/hermes-multitenancy

# 软链到 hermes 用户插件目录
mkdir -p ~/.hermes/plugins   # 默认 profile
ln -s ~/projects/hermes-multitenancy/hermes_multitenancy ~/.hermes/plugins/multitenancy

# (如果是命名 profile, 在 ~/.hermes/profiles/<name>/plugins/ 下重复一次)
```

### 2. 在 `config.yaml` 启用

```yaml
# ~/.hermes/config.yaml —— 或某个 profile 的 config
plugins:
  enabled:
    - multitenancy
```

### 3. 写入路由规则

```bash
# 用自带 CLI
python -m hermes_multitenancy.sync apply users.json
```

`users.json` 格式:

```json
[
  {"user_id": "alice", "profile_name": "alice_profile", "open_id": "ou_xxx", "union_id": "on_xxx"},
  {"user_id": "bob",   "profile_name": "bob_profile",   "open_id": "ou_yyy", "union_id": "on_yyy"}
]
```

每个 `profile_name` 必须事先存在于 `~/.hermes/profiles/<name>/` 下, 自带 `SOUL.md` / `config.yaml` / `auth.json`。插件会把 `alice` 的 union_id 路由到 `alice_profile` 的 SOUL+memory, 把 `bob` 的 union_id 路由到 `bob_profile`。

重启 hermes gateway。**搞定。**

---

## ✅ 端到端验证

这不是 paper plugin。作者本人在自己的飞书 Bot 上跑两个测试 profile 实测过:

| 步骤 | 操作 | 实测结果 |
|---|---|---|
| 1 | 用户 A 发 `hi` | Bot 回 `[SPIKE-TEST] hi! ...` (路由到 spike_test profile) |
| 2 | 用户 B 发 `hi` | Bot 回 `[ALICE-TENANT] 你好!...` (路由到 spike_alice, 完全不同的 SOUL, 中文人格) |
| 3 | 用户 A: `I like apples` 然后 `what did I just say I like?` | Bot 回答 `apples` (多轮记忆生效) |
| 4 | 重启 gateway, 用户 A: `what did I say I liked earlier?` | Bot 回答 `apples` (SQLite 持久化跨重启) |
| 5 | `/new` 然后 `tell me what I like` | Bot 回答 "I don't know" (历史确实被清掉了, 缓存 + DB 双清) |

以上全部跑在真实飞书 WebSocket gateway + `https://api.z.ai` (GLM 5.1) 上。

---

## ✨ 功能矩阵

| 功能 | 状态 |
|---|---|
| 按飞书 user (open_id / union_id) 多租户路由 | ✅ |
| LRU 运行时池 (最多 50 个热 profile, 5 分钟空闲淘汰) | ✅ |
| 流式 LLM 输出 (`edit_message` 打字机效果) | ✅ |
| 推理内容分流 (GLM 5.x thinking 模型) | ✅ |
| 表情反应 (👀 → ✅ / ❌) via `adapter.on_processing_*` | ✅ |
| 多轮会话记忆 (SQLite 持久化, 跨重启) | ✅ |
| 引用上下文注入 (回复消息) | ✅ |
| 限流退避 (429 backoff, 与 hermes 主线节奏一致) | ✅ |
| 斜杠命令 (`/help` `/status` `/stop` `/new` `/reset`) | ✅ |
| 幂等 feishu-sync 同步器 (CLI + 库) | ✅ |
| 图像识别 (图片附件) | ✅ —— 委托给 hermes 的 `gateway._prepare_inbound_message_text`, 行为与主线一致 |
| 语音 STT (语音消息) | ✅ —— 同一委托, hermes 的 `transcribe_audio` 处理已缓存音频 |
| 文本文件注入 (.txt / .md / .csv / .log / .json …) | ✅ —— 同一委托, 内容前置到消息中 |
| 引用上下文 (回复消息) | ✅ —— 同一委托, 加上我们自己的 `reply_to_text` 兜底 |
| 多用户共享会话归属 | ✅ —— 同一委托 |
| 工具调用 (真正的 AIAgent loop, 浏览器/搜索/shell) | 🚧 —— 设计接口已就绪, 切换到 hermes 的 `AIAgent` (`run_agent.py:809`) 是 Phase 5 的可选项 |

---

## 🐢 慢? 用 Haiku 替换 GLM 5.1

GLM 5.1 是 *推理* 模型 —— 在吐 `content` 之前要在 `reasoning_content` 里思考 5-15 秒。这让 Bot **看起来** 卡顿, 即使插件本身没问题。两种提速方法:

**方法 1 —— 切换到非推理模型。** 在你 spike profile 的 `config.yaml`:

```yaml
model:
  default: "openrouter/anthropic/claude-3.5-haiku"
fallback:
  - "zai/glm-5.1"
```

在 profile 的 `.env` 设置 `OPENROUTER_API_KEY`。Haiku 端到端快 5-10 倍。

**方法 2 —— 保留 GLM, 接受打字机感。** 插件会在推理阶段显示 `💭 思考中…` 占位, 让用户看到 *在动*, 不是冻死。

---

## 🛡️ 为什么能保持兼容

我们 **只用 hermes-agent 的公开 API** —— `feishu.py` / `gateway/run.py` 等内部模块零 patch。插件加载契约 (`hermes_cli/plugins.py:435 register_hook`) 是唯一入口。

| 我们依赖的公开 API | 稳定性 |
|---|---|
| `pre_gateway_dispatch` hook (`plugins.py:81 VALID_HOOKS`) | ⚠️ 2026-04-21 新增 —— 锁定 hermes-agent 版本 |
| `BasePlatformAdapter.send / send_typing / edit_message` | ✅ 抽象方法, 非常稳定 |
| `BasePlatformAdapter.on_processing_start / on_processing_complete` | ✅ |
| `MessageEvent.source.{user_id, user_id_alt, chat_id}` | ✅ 稳定 |
| `Platform.FEISHU` 枚举 + `ProcessingOutcome` 枚举 | ✅ |
| `gateway.adapters[Platform.FEISHU]` 字典 | ✅ |
| `hermes_constants.get_hermes_home()` (走环境变量读) | ✅ |
| `SendResult.{success, message_id}` | ✅ |
| `gateway._prepare_inbound_message_text(event, source, history)` | ⚠️ 私有 (下划线开头) —— 一次调用覆盖图像 + 语音 + 文件注入 + 引用上下文。签名变了会自动降级到本地图像-only。 |
| `tools.vision_tools.vision_analyze_tool` (本地兜底) | ✅ 工具模块, 仅在 gateway 助手缺失时使用 |

**锁定 `hermes-agent` 版本** (`hermes-agent==X.Y.Z`), 每次升级跑 `pytest tests/test_router_integration.py tests/test_vision.py` —— 集成 + 管线测试会在契约漂移时大声失败。

---

## 🏗️ 架构

```
~/.hermes/plugins/multitenancy/  (软链到本仓库)
  ├─ __init__.py          register(ctx) → ctx.register_hook(pre_gateway_dispatch, ...)
  ├─ router.py            同步 hook + 异步派发 + 命令 + 懒加载单例
  ├─ runtime.py           ProfileRuntime + contextvars 隔离的 HERMES_HOME 切换
  ├─ pool.py              LRU RuntimePool (50 热 / 5 分钟空闲 / 冷启信号量)
  ├─ routing.py           SQLite multitenancy_routing 表 (open_id → profile)
  ├─ sessions.py          SQLite multitenancy_sessions (按用户历史, 持久化)
  ├─ commands.py          parse_command (/help /status /stop /new /reset)
  ├─ agent_real.py        OpenAI 兼容的薄 LLM 客户端 (流式 + 推理内容分流)
  └─ sync/
     ├─ feishu_hr.py      apply_users (幂等同步器)
     └─ cli.py            python -m hermes_multitenancy.sync apply users.json
```

状态存在 `~/.hermes/multitenancy.db` —— 与 hermes 自己的 `state.db` 分开, 写入互不争用。开启 WAL 模式。

---

## ⚙️ 配置项

| `config.yaml` key | 默认值 | 说明 |
|---|---|---|
| `plugins.enabled` | (无) | 必须包含 `multitenancy` |
| `model.default` | (你的 hermes 默认) | 按 profile, 例如 `zai/glm-5.1` 或 `openrouter/anthropic/claude-3.5-haiku` |
| `model.fallback` | (你的 hermes 默认) | 主模型失败时 `agent_real` 用这个 |

| 插件可调项 (`router.py` 里的 Python 常量) | 默认值 | 说明 |
|---|---|---|
| `RuntimePool.max_loaded_runtimes` | 50 | 热池上限 |
| `RuntimePool.idle_evict_seconds`  | 300 | 5 分钟空闲淘汰 |
| `_SESSION_HISTORY_MAX`            | 20  | 每 (profile, user) 保留的消息数 |
| 流式节流 (content)                | 1.0s / 60 字 | 与 hermes 主线一致 |
| 流式节流 (thinking)               | 2.0s 心跳 | 推理预览 |
| 限流退避                          | 0.5s → 1s → 2s | 仅 429; 非 429 重试一次 |

---

## 🎮 斜杠命令

| 命令 | 效果 |
|---|---|
| `/help`   | 列出可用命令 |
| `/status` | 显示当前 profile + 历史长度 + 运行状态 |
| `/new` / `/reset` | 重置当前用户的会话历史 (按 profile 隔离) —— 同时清缓存 + SQLite |
| `/stop`   | 取消当前用户进行中的 LLM 调用 |

---

## 🧪 测试

```bash
# 默认套件 (无网络) —— 71 测试
PYTHONPATH=. python -m pytest tests/ -q

# 真实 LLM 集成 —— 调真实的 GLM 5.1 (或你配置的 provider)
PYTHONPATH=. python -m pytest tests/ -m integration -v
```

---

## 🐛 故障排查

**"插件加载了但没回复"** —— `pkill -f gateway && hermes gateway run`。插件在 gateway 启动时加载, 任何改动都需要重启。

**"所有 Bot 都不响应了"** —— 路由规则的 `open_id` 或 `union_id` 大概率写错了。在 `router.on_pre_gateway_dispatch` 里临时 `print(event.source)` 看飞书实际发过来的值, 对一下日志。

**"user_id 是 `g41a5b5g` 这种, 不是我以为的 `ou_`"** —— 飞书的 `event.source.user_id` 是 hermes 内部短 ID, **不是** open_id。用 `event.source.user_id_alt` (union_id) 作为路由键, 这就是本插件的默认行为。

**"感觉很卡, 1 秒一个字"** —— 大概率用了推理模型。看 [慢? 用 Haiku](#-慢-用-haiku-替换-glm-51)。

**"重启后会话丢了"** —— 检查 `~/.hermes/multitenancy.db` 是否存在, `multitenancy_sessions` 表有没有行。如果是空的, 看 gateway 日志有没有写入错误 (`logger.debug "SessionStore.append failed"`)。

---

## 🤝 贡献

欢迎 Issue 和 PR。

### Bug 报告

提 Bug 时请附:

1. 你机器上 `pytest tests/ -q` 的输出
2. hermes-agent 版本 (`pip show hermes-agent | grep Version`)
3. 插件版本 (`pip show hermes-multitenancy | grep Version`)
4. 相关 gateway 日志 (尤其是 `multitenancy:` 前缀的)

### Pull Request

1. Fork → 起分支 → 跑 `pytest tests/ -q` (必须全绿) → 开 PR
2. **行为变更必须有测试。** 我们卡死 `pytest tests/ -q` 必须 71+ 全绿。
3. **不要大批量重命名** —— 保持 diff 小且可审。
4. **不要 patch `feishu.py`** —— 这个插件存在的全部意义就是 hermes-agent 不被改动。如果你撞到 hermes API 限制, 去上游 https://github.com/NousResearch/hermes-agent 提 issue, 然后在这里链过来。

### 帮我们盯住 hermes-agent 兼容性

如果你升级 `hermes-agent` 后我们的集成测试挂了, 请提 issue 附上:
- 让我们挂掉的 hermes-agent 版本号
- pytest 输出
- 一条上游 commit 的指针 (能找到的话)

我们在 `pyproject.toml` 锁了 `hermes-agent>=1.0`, 但插件加载契约还在演进 —— 需要社区一起盯变化。

### 想要的贡献 (按优先级)

1. **工具调用** —— 用 hermes 的 `AIAgent` 类 (在 `run_agent.py:809`) 替换我们薄的 `agent_real` LLM 客户端, 让 Bot 能用浏览器/搜索/shell 工具。`AIAgent.__init__` 签名有 50+ kwargs; 集成需要小心做按 profile 的 session_db 接线 + 回调桥接到我们的流式 loop。约 200-500 行。
2. **按 profile 拆 `SessionStore`** —— 当前所有会话行都在一个共享 `multitenancy.db`。要做到真正 1000 用户规模, 应该按 profile 拆库 (与 hermes 自己的 profile 隔离对齐)。
3. **Prompt 缓存** —— Anthropic `cache_control` 给 SOUL 前缀加缓存。长期对话 token 成本砍 ~50%。
4. **CI 矩阵** —— GitHub Actions 在多个 `hermes-agent` 版本上跑 `pytest tests/ -q`, 提早发现上游契约漂移。
5. **更多斜杠命令** —— 把 hermes 的 `/update`, `/steer`, `/queue`, `/skill` 从 `gateway/run.py` 移植到 `commands.py`。

---

## 📜 License

MIT —— 见 [LICENSE](LICENSE)。

## 🙏 致谢

构建在 [Nous Research 的 hermes-agent](https://github.com/NousResearch/hermes-agent) 之上 —— 没有 `pre_gateway_dispatch` hook (由 [@KeiraVoss](https://github.com/) 在 2026-04-21 加入), 这个插件就只能 fork 整个上游了。感谢这个 hook。
