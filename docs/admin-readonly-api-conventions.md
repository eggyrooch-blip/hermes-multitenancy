# 只读 admin 资源家法

> 目的：**第四、第五个只读资源照抄这一页就能写完，不用重新发明。**
> 代码形态在 `hermes_multitenancy/agent_relay_admin.py`；本页是它的散文版与背后的理由。
> 参考实现：`hermes_multitenancy/agent_relay_admin_diag.py`（遥测诊断日志）。

## 这页管什么

「只读 admin 资源」= 把 Hermes 生产宿主上的某份运行证据，交给一个外部同事**自己拉**的 HTTP 端点。
不是给员工的产品接口，不是给 WebUI 的 BFF 接口，不参与任何写入。

第一个这样的资源是 relay 的 `GET /v1/admin/logs` + `/v1/admin/stats`（2026-08-26 上线 release-20260826-03，
给云驿侧排查用）。它定下了形状。第二个资源（遥测诊断日志）落在**同一个平面**上：同路径语法、同鉴权语义、
同游标、同错误体、同一条 ingress（`relay.example.com/v1/*` 已反代，Caddy 零改动）。
一致性的代价是 relay unit 加一条只读 bind（见第 2 条末尾），换来的是第四第五个资源不用重新发明任何东西。

## 七条

### 1. 路径

```
/v1/admin/<resource>
```

一个平面，一个前缀。`relay.example.com` 的 Caddy 块已经把 `/v1/*` 反代到 127.0.0.1:8770，
所以新增资源**不需要动 Caddy、不需要动 DNS**。
一个资源通常是两个端点：`<resource>`（行）和 `<resource>-stats`（同窗口的聚合 + 窗口无关的状态），
与 relay 的 `logs` / `stats` 一一对应。

### 2. 鉴权：一个消费者一把 token，按资源 scope

`agent_relay_admin.ADMIN_TOKEN_SCOPES` 是 env → 资源集的最小映射，落 `/etc/hermes-agent-relay.env`（0600 root）：

```python
ADMIN_TOKEN_SCOPES = {
    "HERMES_AGENT_RELAY_ADMIN_TOKEN": {"logs", "stats"},                       # 云驿
    "HERMES_TELEMETRY_DIAG_TOKEN":    {"telemetry-diagnostics", "telemetry-stats"},  # 平台侧
}
```

语义固定，用 `agent_relay_admin.admin_denied(request, resource)`：

| 情况 | 响应 |
|---|---|
| 无 `Authorization: Bearer` 头 | 401 `unauthorized` |
| token 未知 / 该 env 未配置 / **不在本资源 scope 内** | 403 `forbidden` |

**复用平面，不复用凭据。** 两个外部团队共用一把 token 意味着任一方泄漏就等于两份数据一起泄，
吊销任一方就等于把另一方一起切掉。新增消费者 = 新增一行映射 + 一把 token，既有口子的行为一个字不动
（有直接跑真 relay app 的隔离测试锁着）。

两条禁令：

- **不许接受别的平面的凭据**。relay 自己的 actor token 不是 admin token；run-broker 的 master key
  （`HERMES_MULTITENANCY_RUN_BROKER_KEY`）解锁 skills/install、kanban/dispatch、credentials，更不能借。
- **不许调用 `webui_broker/periphery._authorized`**。它在「没配 key + 绑在 loopback」时直接返回 `True`，
  而反向代理过来的请求正是从 loopback 到达的：env 漏配一次就是无鉴权暴露。这是本平面选择自带
  `admin_denied` 而不是复用现成鉴权的原因。

**如果资源的数据在别的 unit 的家目录里**：给 relay unit 加一条只读 bind，不要放宽沙箱。
`ProtectHome=true` 下 `BindReadOnlyPaths` 建不了挂载点（`man systemd.exec` 明写），要改成
`ProtectHome=tmpfs` 再 `BindReadOnlyPaths=-<目录>`（`-` 前缀让目录不存在时也能启动）。
净效果是**收窄**：`/home` 仍是空 tmpfs，只多出一个只读目录，写入被 `Read-only file system` 拒。
落地前在 hermes-pre 起同配置单元实测一次，确认 `ausearch -m avc` 零命中再上生产。

### 3. 窗口

`since` + `until`，毫秒时间戳，都必填，跨度 ≤ **7 天**（`ADMIN_MAX_WINDOW_MS`），且必须是 1970–2100 之间的真实日期
（int64 不等于日期：不校验的话，一个巨大的时间戳会变成读取器深处的 500，而它明明是个 400）。
任何不满足的都是 400 `invalid_range`，用 `agent_relay_admin.admin_window()`。

`since` 的开闭由资源声明，助手用 `allow_empty_window=` 参数区分：
- `logs` / `stats`（既有资源）：`since` 开区间，`until > since`，**行为一个字不动**。
- `telemetry-*`（本资源）：`since` **闭区间**，允许 `since == until` —— 它的游标以 id 为准，
  分页边界落在窗口末刻时续拉就是这一对，拒绝它会把尾部行锁死。
新资源选哪种，在 SPEC 里写清楚并在助手调用处显式传参，不要靠默认值。

### 4. 游标：`since` + `after_id`，两个都要，且 **id 是权威**

续拉写法固定：`since=<最后一条的 ts>&after_id=<最后一条的 id>`，`until` 不变。谓词：

```
since <= ts <= until AND id > after_id        # since 闭区间；允许 since == until
```

**不要**用只靠时间戳的续拉：一轮写入的所有行共用一个时间戳时，它要么原地打转、要么整轮丢失。

**为什么不照抄 relay 的 `(ts > since OR (ts = since AND id > after_id))`**：那种写法里 id 只是同刻的
tie-break，时间戳才是主键——于是读取方**不能**跳过任何前缀或整天文件（被跳掉的行可能仍满足 `ts > since`）。
SQLite 有索引，全表扫一遍无所谓；文件型资源不跳前缀就得每页把整个窗口重读一遍，页一大就是「空页 + 同一个游标」
的永久停滞。id 权威之后，跳前缀、跳整天文件都是可证明安全的，重复和漏行同时消失。
`since == until` 必须被接受：分页边界正好落在窗口最后一刻时，续拉就是这一对，拒绝它就等于把尾部行锁死。

id 从哪来，按这个顺序选：

1. **产出方已经写在行里的稳定 id 最优**（遥测诊断是 `"<YYYYMMDD>-<seq:08d>"`：唯一、按 append 顺序字典序、
   写下就不再变）。不要因为 relay 的 `after_id` 是整数就再造一套编号 —— 调用方要学的是参数名和续拉写法，
   不是它的类型。字符串 id 用 `admin_after_key()` 校验形状（并把 `0` 当作「从头开始」，兼容整数平面的默认值），
   整数 id 用 `admin_after_id()`。
2. 产出方没有 id 时才合成，并且要单调、稳定、对调用方不透明。
3. **游标里只许有 id，不许有字节偏移。** 这条是拿四个评审发现换来的：把偏移交给调用方之后，
   出现过「提示跳过整段记录」「提示落在行中间」「定位失败把游标拨回文件头」「无 id 前缀把分页卡死」。
   定位由服务端自己做：按 id 二分查找（O(log n) 次单行读），必须把「读到文件尾」和「读不动了」分开
   —— 把 EOF 当失败会让游标倒退。定位走自己的字节上限，每一次读都要把剩余额度传下去
   （只在循环外检查的上限不是上限），且**不计入**页预算：只是「找到位置」的请求不该因此返回空页。
4. **要缓存定位就缓存在服务端**：`(文件名, inode, id) → 偏移` 的进程内备忘，上限固定、命中还要复验行边界、
   调用方完全无法影响。它是缓存不是契约：空的、过期的、被写坏的，结果都必须一模一样。
5. **`next.since` 永远是调用方原来的下界**，不许用最后一行的时间去抬高它 —— 时钟回拨或一条脏时间，
   就会让它后面的合法行永久取不到。排序靠 id，不靠时间。

无法参与游标的行（例如缺 id）单独计数（`unpageable_lines`）并跳过：发给调用方要么永远重复、要么卡住分页。
计数是为了让它可见，而不是静默丢。

### 5. 上限与 IO 纪律

- 单页 ≤ **5000** 行（`ADMIN_PAGE_LIMIT`），到顶回 `truncated: true`。
- 单次请求扫描 ≤ **8 MiB**（`ADMIN_SCAN_BYTE_BUDGET`），到顶同样 `truncated: true`。
  预算在**下一次读之前**检查，不是读完之后：读完再判会让续拉点落后一行，预算小于两行时游标原地不动。
  代价是最多超读一行，这是自觉的。单行也要有上限，否则一个没有换行的脏文件会把「有界读」变成
  把整个文件读进内存；**超长行 = 该文件损坏**：计数、记进 `corrupt_files`、放弃该文件换下一个，
  不要追着找换行（那会同时烧掉预算并把游标永久卡在它前面）。状态文件读也要有上限。
- `truncated` 时 `next` **必须**能推进，哪怕这一页一行都没返回（预算被窗口外的、缺 id 的、解析不了的行吃掉都算）。
  续拉点在**每一条读完的行**上前进，不是只在返回的行上；一条可用的 id 都还没读到时，
  用当天的地板 id（`<日期>-00000000`）占位，让下一次请求至少知道该从哪个文件、哪个字节接着读。
  游标只许前进：新的 `after_id` 不得小于调用方传进来的那个。
- 单 token **1 req/s + 120 req/h**（`RateLimiter`），超了 429 + `Retry-After`。
- **所有磁盘读放进 `asyncio.to_thread`**。run-broker 与 WebUI、agent 共用一个事件循环，
  同步扫描是延迟事故，不是慢查询；hermes-1 还有 wbt 写限流的事故史。
- 只读打开；不取产出方的锁；不推进任何游标；不写、不删任何文件。
- append-only 文件的末行可能是半行（写入方不 fsync）：**丢弃且不推进 id**，下一轮整行取回。

### 6. 错误体

`{"error": {"code", "message"}}`，与 relay 的 `_error` 一致；429 额外带 `retry_after` 与 `Retry-After` 头。
固定码：`unauthorized` / `forbidden` / `invalid_range` / `rate_limited` / `internal_error`。
空窗口不是错误，返回 200 + 空 `items`。

### 7. 留存与 prune

**留存由数据的产出方声明并执行，只读资源永不删数据**，只在自己的 `-stats` 响应里回报 `retention_days`，
让调用方不用记。（relay 的 30 天由它自己的 `prune()` 在 SQLite 上执行；遥测诊断的 14 天由导出器按天名执行。）
两个资源留存期不同是正常的——它们是不同的数据，各自的产出方说了算。

## 隐私：返回体是**构造**出来的，不是过滤出来的

一条硬规矩，不接受例外：

- 逐字段白名单构造新对象，**并且白名单要一路到底**：放行一个键不等于放行它底下整棵子树。
  每个值按声明的类型重建，嵌套字典走自己的白名单，counter 块只收数字与具名标签。
  **键也要白名单**：map 的键来自上游，只限长度的话，一整句话可以当成键出现在响应里
  （两轮评审分别用「短字符串值」和「任意短键」打穿过第一版和第二版）。动态分类标签限字符集，
  越界折进 `other`。
- **外部系统回给我们的文本也不许原样转发**。解析后按它的响应契约重建；解析不了就只回形状
  （多少字节、为什么没解析），原文留在宿主本机 0600 的日志里等人来问。
- **状态字段只能从自己读得到的地方取**。资源通过只读 bind 只看到一个目录，那么「上一轮跑得怎么样」
  就得从那个目录里的数据推出来，不能去读挂载外的兄弟文件——那种字段在生产上永远是空的，
  比没有这个字段更糟。
- 数值只收**可表示**的有限数：`Infinity` / `NaN` 是合法 Python、非法 JSON；400 位的 `int` 会让
  `math.isfinite` 自己抛异常；两个有限的 `1e308` 相加就是 `inf`——所以求和之后还要再验一次。
  序列化用 `allow_nan=False` 兜底，并且**放在 handler 的错误边界之内**，否则它抛出来就绕过了统一错误体。
- 字符串只收**机器标签**，且靠语法而不是靠长度或字符集：标识符（可带一段 `:` 后缀，值可加引号）、
  时间戳、URL —— 三种形状之一。「短」不是边界（一句中文私密内容也很短），「ASCII」也不是
  （一句英文正文全是 ASCII）；两轮评审分别用这两种方式打穿过。带空格的自然语言不匹配，这正是目的。
  外部系统专门用来放人话的字段（Hub 的 `message`）**整个不外发**，只回一个 `message_withheld: true`。
- 任何来自行内的值在用作 `in <集合>` 判断前先确认类型：`[] in frozenset` 抛的是 `TypeError`，
  一行脏数据就能把端点打成 500。
- 时间戳也要验可表示范围，并且**一条越界的时间不许终止整个扫描**——否则它后面的行会被永久藏住。
- 解析异常要连 `RecursionError` 一起接（深层嵌套的 JSON 不抛 `ValueError`），否则一行脏数据就是整页 500。
  上游明天加的字段会被丢弃并计数（`dropped_keys`），不会被转发。
  黑名单过滤反过来：上游加一个字段就默认外泄。
- **绝不返回任何用户/助手对话正文**，也不返回能取到正文的句柄。定位用的偏移/inode 可以给，
  那一行的**内容**由我们这边取。
- 同一个人的多个标识只留一个。`profile` 与 `operator` 都是员工账号名，只返回 `operator`。
- 每个资源都要有一条锁死这件事的测试：构造一条含 `content` / `prompt` / `answer` / `preview` 字段的假记录，
  断言返回体里一个都不出现。参考 `tests/test_kep_telemetry_diagnostics_api.py::test_conversation_content_never_leaves`。
- 白名单如果引用了别处的闭集（例如上传契约的 key 集），用测试钉住**相等**而不是 import 它：
  上游放宽时应该是这边红，由人来决定，而不是接口跟着自动放宽。

## 新增一个资源的清单

1. 在 `agent_relay_admin_<name>.py` 里写 reader + 白名单投影 + 两个 handler + `register_*_routes(app)`。
   文件名必须以 `agent_relay` 开头：`deploy/hermes-release.sh` 的发布闭包以 `agent_relay*.py` 为种子，
   再沿 **level-1** 相对 import 递归收敛，所以模块只能 stdlib + 同包 level-1 import，不能伸进
   `hermes_multitenancy` 的其它子包（伸了就会在生产上 ImportError）。
2. 用 `agent_relay_admin` 的 `admin_window` / `admin_after_key` / `admin_error` / `RateLimiter`，不要自己写鉴权；
   在 `ADMIN_TOKEN_SCOPES` 加一行 env→资源集。
3. 在 `agent_relay.py` 的路由注册段加三行。
4. 端点解析自己的数据路径时，**用自己的 env**，不要复用 `HERMES_HOME` 派生的助手 ——
   gateway 单元的 `HERMES_HOME` 是 `…/profiles/<profile>`，其它单元的是 `/home/hermes/.hermes`，
   复用会指到空目录并一直读成「没数据」。注册时打一行 INFO 自检（目录存在/可读/token 是否配置）。
5. 测试：fail-closed 三态、master key 被拒、窗口非法、双游标走到底、半行、白名单锁死。
6. 生产侧（由 sunke 或 team-lead 执行，不由实现方执行）：token 落 `/etc/hermes-agent-relay.env`；
   数据若在别的家目录，再加一条 `BindReadOnlyPaths`。**Caddy 不用动** —— `/v1/*` 已经在反代里。

## 平面只有一个，别再开第二个

这份家法最初是为「第二个资源落在 run-broker 上」写的，那版实现完成过也评审过，最后被一致性优先否掉：
两套路径前缀、两套 token 文件、两套 Caddy 路由，第四个资源就得选边站。**结论：只读 admin 资源一律上
relay 平面**（`/v1/admin/<resource>`）。数据不在 relay 能看到的地方，就给它一条只读 bind，而不是另起一个平面。

反过来的判据：如果将来某个资源的数据**必须**留在别的进程里（比如它要的是内存态、不是文件），那时再评估
是否给那个进程开第二个平面——并且要先把本页的七条原样搬过去，不许趁机改形状。
