# AiDock SkillHub → Hermes 对接说明（给王克杰）

> 这是 Hermes multitenancy 提供给 AiDock SkillHub 的**事件接收接口**。
> 你（AiDock 侧）审批通过/更新/撤权后，把事件 POST 到这个接口即可。
> Hermes 会「**收下、校验、去重、入库、快速回执**」，随后由与 WebUI 默认线程池隔离的专用串行 drain 完成下载、校验与安装。进程启动会继续消费 queued backlog；同一 plugin 的相邻 permission 事件会安全合批，但每个原始 event_id 仍保留独立账本终态。

## 1. 接口

```
POST https://hermes.example.com/api/run-broker/skillhub/events
Content-Type: application/json
```

- 走 **443 / HTTPS**,域名 `hermes.example.com`(证书是 `*.example.com`,**别用 IP**,IP 会证书不匹配)。
- 前面是 Caddy 反代,只把这一条路径转发到内部 run-broker(:8766);其它 run-broker 路径不对外。

## 2. 鉴权（固定 Bearer key，**必填**）

这条接口对公网开放,所以是**fail-closed**:没有配置任何 key 时直接拒绝,绝不裸奔。

- **请带固定 Bearer key**:`Authorization: Bearer <key>`。
- 我会给你一个**专用 key**(`HERMES_SKILLHUB_WEBHOOK_KEY`),只用于这条接口——不是 Hermes 主 key,泄了也只影响这一条。
- key 我单独私发给你,**不写在本文档里**。
- (可选,正式期可加)HMAC 签名:若双方约定 `HERMES_SKILLHUB_WEBHOOK_SECRET`,带 `X-AiDock-Timestamp` + `X-AiDock-Signature: sha256=<hex>`,签名 = `HMAC_SHA256(secret, timestamp + "." + event_id + "." + 原始body)`。当前内网走固定 key,这步先不用。

> body 限制:webhook 包体上限 **256KB**(超了返回 413),正常 JSON 远小于此。

## 3. 请求体（你现在推的格式我已兼容）

你 2026-05-27 发我的格式**原样支持**：

```json
{
  "event_type": "skill.install_approved",
  "skill_code": "daily-breaking",
  "release_id": "rel_001",
  "version": "1.0.3",
  "download_url": "https://.../1.0.3.zip",
  "skill_status": "active",
  "audience": {
    "auth_type": "all | auth",
    "users": [{ "profile_id": "profile-xxx" }]
  }
}
```

- **必填**：`event_type`、`skill_code`。缺了返回 400。
- `audience.auth_type=all` → 全员；`auth` → 按 `users[].profile_id`。`profile_id` 必须是 Hermes routing 的 LDAP/`user_id`；若同时提供 `employee_id`，它必须等于同一 LDAP，`open_id` 则必须等于该唯一 active user route 的 open_id。缺失、歧义或不一致会让整条事件以 `AUDIENCE_IDENTITY_INVALID` 失败，且在下载和安装前停止；routing DB 不可用单独返回 `AUDIENCE_ROUTING_UNAVAILABLE`。
- plugin `skill_status=inactive` 只禁用展示与运行，保留已保存包和 audience；无显式 status 的 permission 事件不得把它重新启用。
- active plugin 的 profile audience 默认包含经 Hermes routing 与 profile 目录双重证明的 `sunke`；上游 permission 用户增量追加。该默认身份无法证明时事件显式失败，不做无声 no-op。
- AiDock trusted publisher 免人工复审，但不跳过包完整性、插件所有权、身份绑定、profile 隔离或候选健康检查。同一插件的新 release（包括缓存重试）可原子替换自己的受管 shared/private source；失败会恢复 manifest、shared/private source 和 profile fanout。
- profile 已有同名技能时只跳过该技能入口，不覆盖旧安装，也不撤销已批准的专家 audience；专家运行时仍从各插件的受信 repo 加载隔离技能，未授权 profile 继续 fail-closed。
- 候选健康检查只要求 `owned_skills` 中由该插件实际安装的 profile 入口精确命中 shared/private source；被明确跳过的外部同名入口不再把整个授权判失败，包预检与 expert repo 解析仍必须通过。
- PRD §9.5 的全量嵌套格式（`skill{}`/`release.package{}`/`audience.type`）也兼容，二选一即可。

### 🙏 希望你补两个字段（强烈建议）

| 字段 | 为什么 |
|---|---|
| `event_id` | 幂等键。**你不传我也能跑**——我会用 `skill_code:release_id:sha256(body)[:16]` 合成一个；但你自己给一个稳定 id，重试/对账更干净。 |
| `checksum_sha256` | 包完整性校验。下一期下载 zip 后要拿它比对，缺了安全验收过不了。 |

支持的 `event_type`：`skill.install_approved` / `skill.updated` / `skill.revoked` / `skill.permission_changed` / `skill.rollback_requested`（其它类型也收，标记 `queued_unknown_type`）。

## 4. 响应

成功（200）：

```json
{
  "ok": true,
  "event_id": "daily-breaking:rel_001:11427aedce4bbfca",
  "event_type": "skill.install_approved",
  "skill_code": "daily-breaking",
  "accepted": true,
  "duplicate": false,
  "status": "queued"
}
```

- **幂等**：同一 `event_id`（或同一 body）再 POST → `duplicate: true`，不重复入库，可安全重试。
- 失败（4xx/5xx）：

```json
{ "ok": false, "event_id": "...", "error_code": "INVALID_SIGNATURE", "message": "...", "retryable": false }
```

错误码：`INVALID_JSON` / `INVALID_PAYLOAD`（缺 event_type/skill_code）/ `INVALID_SIGNATURE` / `INTERNAL_ERROR`(retryable)。

## 5. 联调 curl（已在本地实测通过）

```bash
curl -s -X POST https://hermes.example.com/api/run-broker/skillhub/events \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <你的专用key>" \
  -d '{"event_type":"skill.install_approved","skill_code":"daily-breaking","release_id":"rel_001","version":"1.0.3","download_url":"https://example.invalid/1.0.3.zip","skill_status":"active","audience":{"auth_type":"auth","users":[{"profile_id":"profile-owner"}]}}'
```

## 6. 还需要你确认的 2 件事

1. 你那边的 **callback endpoint**（Hermes 装完回传状态用）地址 + 鉴权，方便我下一期对接。
2. 鉴权已定：**固定 Bearer key**（专用 key，我私发给你，不用 HMAC）。

## 7. 当前实现边界

- ✅ 接口可收 / 校验 / 去重 / 入库（表 `skillhub_events`）/ 快速回执；worker 下载并校验 package，安装到受管 plugin repo 与授权 profile。
- ✅ Webhook 不执行慢安装；专用单线程 drain 避免授权突发占满 WebUI 默认 executor，并在启动时恢复 queued 事件。
- ✅ 相邻且发布字段兼容的同 plugin permission 事件合批 ingest，原事件逐条记 installed/failed 结果。
- ⏭️ Hermes → AiDock 的独立 callback endpoint 与失败重试协议仍需双方确认。
