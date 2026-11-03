# 遥测诊断日志接口 — 对接文档草案

> **这是给刘帅的飞书文档草案，不是仓内文档。** 上线后由 sunke 贴成飞书 docx（章节骨架照
> 《Relay 管理员日志与用量接口（已上线）》：`https://keep.feishu.cn/docx/GRHQdwvdro0bG9xK8d2cHJrInKc`），
> 并给对方 edit 权限。下面 `<>` 里的占位在贴之前替换。
> 仓内的家法在 `docs/admin-readonly-api-conventions.md`，不进这份文档。
> 本文档所在平面 = relay 管理员日志那两个接口的同一个平面，章节骨架照抄那份 docx。

---

## 遥测诊断日志接口（已上线）

> ✅ **已上线生产**：`<release-YYYYMMDD-NN>`，`<HH:MM>` 重启生效。
> 你现在可以自己拉 Hermes 侧每条遥测记录的诊断日志，不用再找我们导文件。

### 背景

Hermes 每 10 分钟把 skill 使用记录映射成 kep-telemetry lifecycle 记录 POST 给 Hub。
在这之前，一条记录发出去之后本机只留结果不留证据：成功的只剩一个 `content_hash`，
被拒的只有 `run_id / reason / at / skill` 四个字段，**当时发了什么、Hub 原话怎么答、batch_id 是多少，全丢**。
所以「这条为什么没进 Kibana / 为什么字段不对」在我们这边查不出来，只能人工捞。

现在导出器每次结算都落一行本地诊断日志，这两个接口是它的读出口。
**不新建日志服务，也不需要你那边改任何东西。**

### 两个接口

都在 `relay.example.com`（内网 DNS，与 relay 管理员日志接口同一个平面、同一套语义），
鉴权用**单独发给你的** token，和云驿那边用的管理员 token 是两把、互相读不到对方的资源。
请求头：`Authorization: Bearer <TOKEN>`。

#### 1. `GET /v1/admin/telemetry-diagnostics` — 诊断行

参数：

| 参数 | 必填 | 说明 |
|-|-|-|
| `since` | 是 | 毫秒时间戳，**闭区间**下界（`since == until` 也合法，续拉到窗口末刻时会用到）；窗口最长 **7 天** |
| `until` | 是 | 毫秒时间戳，闭区间上界 |
| `after_id` | 否 | 续拉游标，形如 `20260922-00000137`（传 `0` 等于从头开始）；**见下面的分页** |

返回 `{"items":[...], "truncated": false, "next": null, "gap": false, "dropped_keys": 0, "unreadable_lines": 0}`，
按时间升序。每条：

| 字段 | 说明 |
|-|-|
| `id` | 行 id `<日期>-<序号>`，唯一、按写入顺序递增；配合 `since` 做续拉 |
| `at` / `at_epoch_ms` | 结算时刻（RFC3339 +08:00 / 毫秒），字段名与你那边一致 |
| `kind` | `upload_attempt`（发出去过） / `local_reject`（我们自己拒了，Hub 永远见不到它） / `pass_summary`（整轮报告） / `truncated`（当天日志触顶） |
| `run_id` | `sha256("hermes\|profile\|session_id\|message_id")[:32]`，即 Hub 侧那个 run id |
| `content_hash` | 发出去那份 payload 的 sha256 |
| `skill` | 上报用的 skill 名；未通过字符集时为 `null` 且 `skill_withheld: true` |
| `operator` | `X-Operator` 头里的值（Hermes profile，即员工账号名）—— 你那边收到的就是它 |
| `batch_id` | 这条记录坐的那个 POST 的 id，**对齐你服务端日志的主键** |
| `url` | 投递的 skill-runs 端点 |
| `attempt` | 第几次尝试（1 = 首次） |
| `first_at` | 这条记录**首次**被拒的时刻（`at` 是本次）——和你那边 dead-letter 行同名，直接对得上 |
| `reason` / `class` | 服务端拒绝原因原文 / 分级（`retryable` `repairable` `permanent` `conflict`） |
| `request` | **这次新补的**：当时 POST 的 lifecycle 记录全文（闭集字段，无会话内容） |
| `response` | **这次新补的**：`{status, body, error}`。`body` 是把你 Hub 的响应体**按 skill-runs 契约重建**后的对象（`accepted` / `rejected` / `errors[]` / `message` / `code`），不是原样转发的字符串；不是 JSON 或不符合契约时 `body` 为 `null`，改回 `body_unparsed: {reason, bytes}`（原文留在我们本机日志里，需要就找我们）。`error` 是这条记录在 `errors[]` 里的那一项（整批 100 条被拒时也不会被截掉）。
Hub 响应里的 `message` 字段**不外发**（那是专门放人话的字段），只回一个 `message_withheld: true`；需要原文找我们 |
| `verdict` | `{outcome: confirmed \| dead_letter \| retry, reason}` |
| `source_offset` / `source_inode` | **这次新补的**：这条记录来自哪一行审计。定位用，**内容不外发**，要原行找我们 |
| `report` | 仅 `pass_summary`：那一轮的完整导出报告（计数） |

**收录范围**：每条结算一行（成功/被拒/待重试都在），加上每轮一条 `pass_summary` ——
所以「一条都没发」的轮次（认证不可用、限流停住）也留痕。

**分页**：单页最多 5000 条、单次最多扫 8 MiB，超出返回 `"truncated": true` 并在 `next` 里给出续拉参数。
`truncated: true` 时 `next` 一定非空，即使这一页一条都没返回（窗口里前面全是旧行时会这样）——照拿 `next` 继续就行。
续拉用 `since=<next.since>&after_id=<next.after_id>`，`until` 不变。

> ❗ **一定要带 `after_id`。** 一轮导出写入的所有行**共用同一个时间戳**，只用 `since` 续拉永远走不到下一页。
> 游标以 `after_id` 为准（判据是 `since <= at <= until 且 id > after_id`，`since` 是闭区间），
> 所以 `since` 跟不跟着动都不会重复、也不会漏；把响应里的 `next` 原样回传最省事。
> 游标里只有这两个参数，没有别的。

```bash
# 首拉：最近 6 小时
curl -sS -H "Authorization: Bearer $TOKEN" \
  "https://relay.example.com/v1/admin/telemetry-diagnostics?since=$(( ($(date +%s) - 21600) * 1000 ))&until=$(( $(date +%s) * 1000 ))" | jq .

# 续拉：把上一页的 next 原样带上
curl -sS -H "Authorization: Bearer $TOKEN" \
  "https://relay.example.com/v1/admin/telemetry-diagnostics?since=<next.since>&until=<同上>&after_id=<next.after_id>" | jq .
```

#### 2. `GET /v1/admin/telemetry-stats` — 窗口聚合 + 当前状态

同样的 `since` / `until`。返回：

```json
{
  "rows": {"total": 312, "by_kind": {"upload_attempt": 298, "pass_summary": 14}},
  "verdicts": {"confirmed": 0, "dead_letter": 298},
  "rejected_by_reason": {"invalid_enum": 298},
  "rejected_by_class": {"repairable": 298},
  "truncated": false,
  "state": {
    "available": true, "days": 3, "bytes": 412773,
    "files": ["export-20260920.ndjson", "export-20260921.ndjson", "export-20260922.ndjson"],
    "retention_days": 14,
    "last_pass": {"at": "2026-09-22T15:06:00.000+08:00", "rows": 21, "dropped": 0, "errors": 0, "stopped": null}
  }
}
```

`state` 与时间窗无关，是「日志现在在盘上是什么样、上一轮跑得怎么样」的探针 —— 想确认导出器活着，拉这个就够。

### 怎么把一行对回你 Hub 侧的日志

三个键：`batch_id`（哪一次 POST）+ `operator`（`X-Operator` 头）+ `run_id`（哪条记录）。
`response.error` 里还带着这条记录在你返回的 `errors[]` 里的原项，包含 `index` 与 `reason` 原文。

### 其他约定

- **留存 14 天**，到期由导出器自己按天删；这两个接口只读，不删任何东西（`retention_days` 每次都回报）。
- 不带 token 返回 401；token 不对返回 403；**token 未配置时一律 403**（fail-closed）。
- 参数缺失、非整数、区间倒置、窗口超 7 天、`after_id` 为负，都是 400 `invalid_range`，错误体 `{"error":{"code","message"}}`。
- 读得太密返回 429 带 `Retry-After`（限 1 次/秒、120 次/小时）—— 这台机器上还跑着生产飞书链路。
- 响应里还有四个自证字段：`skipped_lines`（非记录行，比如导出包的首行 header，一律不外发）、`unreadable_lines`（读不了的行）、`unpageable_lines`（缺 id、进不了分页的行）、
  `corrupt_files`（因为有超长行被整体放弃的日文件）。后三个不为 0 就是我们这边的问题，直接找我们。
- **日志里没有任何会话正文**，返回体是白名单构造的，不是过滤出来的：
  `request` 只含上传契约的闭集字段，审计原行的内容从来不出网。这条是刻意的。
- 诊断数据**不经由被诊断的那条上报通道**，所以 `client=hermes` 还在被拒的这段时间，这个口子照样能用。

### 存档：当前已知问题

生产上现有 `<N>` 条死信全部是 `invalid_enum:client: "hermes"` —— 网关的 `client` 枚举还没放开 `hermes`。
拉一条看 `response.body` 就能看到原话。下面几条可以直接当第一批查询目标：

- `run_id`: `<run_id_1>` / `batch_id`: `<batch_id_1>`
- `run_id`: `<run_id_2>` / `batch_id`: `<batch_id_2>`

---

> 📌 token 我私发给你。要加字段或加筛选条件（比如按 `class`、按 `operator` 过滤），随时说。
