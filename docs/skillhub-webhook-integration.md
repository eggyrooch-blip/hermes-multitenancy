# AiDock SkillHub → Hermes 对接说明（给王克杰）

> 这是 Hermes multitenancy 提供给 AiDock SkillHub 的**事件接收接口**。
> 你（AiDock 侧）审批通过/更新/撤权后，把事件 POST 到这个接口即可。
> 本期 Hermes 做的是「**收下、校验、去重、入库、回执**」；真正的下载安装/symlink/状态回传是下一期，事件先以 `queued` 落库。

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
- `audience.auth_type=all` → 全员；`auth` → 按 `users[].profile_id`。
- `skill_status=inactive` 表示技能下线（后续走解除授权）。
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

## 6. 还需要你确认的 3 件事

1. `audience.users[].profile_id` 里的值，和 Hermes 的 profile 名怎么映射？（你早先发过 `profile-owner` + `ldap`）
2. 你那边的 **callback endpoint**（Hermes 装完回传状态用）地址 + 鉴权，方便我下一期对接。
3. 鉴权用哪种：dev 先裸 POST？正式期上 HMAC 还是 Bearer？

## 7. 本期边界

- ✅ 已交付：接口可收 / 校验 / 去重 / 入库（表 `skillhub_events`）/ 回执，14 个单测 + 真实 curl 通过。
- ⏭️ 下一期：下载 zip → 校验 checksum → 装进 router 托管库 → 给 profile 建 symlink → 归 kep-cli 凭证 → callback 回传你。
