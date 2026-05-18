# Runbook：dev 环境换 LLM 模型(踩坑沉淀)

> 来源:2026-05-18 把全 dev 从 `zai/glm-5.1` 换成腾讯 MaaS `anthropic/custom-model-a3` 的实战。
> 目的:下次换模型/换 provider 不再踩同样的坑。所有结论均经实测验证。

## 0. 为什么换(本次背景)

`zai/glm-5.1` extended-thinking 每轮生成数万 reasoning token,在不断膨胀的上下文上把**单个 LLM 轮**推到 7+ 分钟(实测某 webui 会话单轮 459s、reasoning_content 31,273 字符),整次 agentic run ~10 分钟。换成 `custom-model-a3` 后:冷 ~3 分钟、热 ~15 秒。**非网络/非 provider 速度/非 webui/非代码分支** —— 是模型自身 reasoning 失控。

## 1. 模型配在哪

- 每 profile:`~/.hermes/profiles/<p>/config.yaml` 的 `model:` 块(`default` / `provider` / `base_url`)。
- 共享默认:`~/.hermes/config.yaml` 的 `model:` 块。
- dev 里约 **17 个**文件引用模型(1 shared + 16 profile)。`~/.hermes/plugins/multitenancy` 是符号链接 → 代码仓库;**重启进程**即加载当前分支代码,无需重装。

## 2. ⚠️ 硬规则:`model.default` 必须带 `provider/` 前缀

`hermes_multitenancy/agent_real.py:_split_model_spec` 对无 `/` 的 spec 直接
`raise ValueError("model spec missing provider prefix: <spec>")` → 上层
`streaming exhausted (no usable provider returned content)` → **静默吐空、表现为「没回复/卡住」**。

- 对:`default: anthropic/custom-model-a3`、`default: zai/glm-5.1`
- 错:`default: custom-model-a3`、`default: glm-5.1`(部分 profile 长期 bare `glm-5.1` 就是这个老 bug,一直静默失败)
- 自动 provision(`router.py:_profile_config_from_shared_home`)会在 `default` 无 `/` 且有 `provider` 时补成 `provider/default`;但**别依赖它**,直接写全 `provider/model`。

## 3. Anthropic 协议的自定义端点(如腾讯 MaaS / tokenhub)

先辨协议:`POST <host>/v1/messages` = Anthropic Messages;`/v1/chat/completions` = OpenAI。**配错 provider 类型 → 整体失败**。本次端点 `https://tokenhub.tencentmaas.com` 只支持 `/v1/messages`。

配法(每个要换的 config.yaml `model:` 块):
```yaml
model:
  default: anthropic/custom-model-a3       # 必须带 anthropic/ 前缀
  provider: anthropic
  base_url: https://tokenhub.tencentmaas.com
```
Key/base_url 落**运行位**(不入任何文档/笔记):
- `~/.hermes/.env`:`ANTHROPIC_API_KEY=...` / `ANTHROPIC_BASE_URL=https://tokenhub.tencentmaas.com`
- run-broker plist `EnvironmentVariables`:同两个 key(webui 路径走 run-broker,必须给它)
依据 `hermes-agent/hermes_cli/auth.py`:`anthropic` provider `api_key_env_vars=("ANTHROPIC_API_KEY",...)`、`base_url_env_var="ANTHROPIC_BASE_URL"`。

换前先用 curl 实测端点 + key + 模型通(`/v1/messages`,Anthropic body 形状),记延迟。

## 4. macOS 坑:BSD `sed -E` 不支持 `\s`

`sed -i '' -E 's|^\s*default:...|...|'` 在 macOS **静默 0 替换**(看似成功实则没改)。用
`perl -0pi -e 's{^(\s*default:\s*)X$}{${1}Y}mg'`,且**改完必须 grep 核实落盘**。

## 5. 重启语义(别用错,否则改了不生效)

- 改了 plist `EnvironmentVariables` → `launchctl bootout gui/$UID/<label>` + `launchctl bootstrap gui/$UID <plist>`(`kickstart -k` **不重读**变更后的 env)。
- 只改了 config.yaml / 代码(symlink) → `launchctl kickstart -k gui/$UID/<label>` 即可。
- webui「跑 agent」路径 = `com.hermes.multitenancy-run-broker`(改完必须重启它)。
- 每用户飞书 bot = `ai.hermes.gateway-*`(滚它们会短暂断飞书 websocket;dev 可接受)。

## 6. 运行时池缓存陷阱(本次最隐蔽的坑)

run-broker 的 `RuntimePool` 会按 profile 缓存已加载的 ProfileRuntime/config。**在一次「多次改配置 + 多次重启」的修复过程中**,某次 run 可能命中修复中间态(旧 bare 配置)加载的 stale runtime 而失败,即使盘上配置已正确。

→ 结论:配置定型后做**一次干净重启**,再对**真实出问题的那个 profile** 做一次实测,以那次为准。

## 7. 验证协议(不许靠假设/旧日志)

1. 实测:`curl -N POST http://127.0.0.1:8876/api/run-broker/runs -d '{"channel":"webui","profile_name":"<真实profile>","user_key":"v","content":"只回复:好的","delivery_mode":"socket","session_id":"verify-x"}'`。成功 = SSE 出现 `{"kind":"content",...}` + `{"kind":"done"}`。
2. webui 会话追踪查 `~/.hermes-web-ui/hermes-web-ui.db`(**ekko-webui 实际打开的那个库;不是同目录其它 stale .db**)。`messages` 表按 `session_id` 看时间线 + 逐条 delta;先判**在跑 / 已完成 / 失败**(看 last_active、有无收尾 assistant、6-15s 再采是否推进)再下结论。
3. **别读 `error.log` 尾部就下结论** —— 先看它 mtime,旧错误会误导。SSE 的 content/done、或 messages 时间线,才是 ground truth。

## 8. 安全 & 回退

- 每个被改 config 改前备份(本次:`~/.hermes/_glm_to_tencent_bak_20260518-131945/`)。
- 密钥只进 `.env` / plist EnvironmentVariables,**绝不进 docs/notes/log/commit 正文**。
- 回退 = 从备份目录覆盖回原 config + 重启对应 agent。
