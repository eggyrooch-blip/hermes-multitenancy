# 2026-07-22 AIHub plugin 授权突发事故与候选修复

生产 `keep-sharetemplate` 一次收到 293 条 permission 事件。旧 Run Broker 为每条事件向 asyncio 默认 executor 投递一次慢安装，与 experts catalog 共用线程池，导致队列积压、部分失败、同 manifest 并发写和 WebUI 专家页 Loading。

候选修复把 SkillHub 安装集中到专用 `max_workers=1` drain；Webhook 与进程启动只负责唤醒。相邻且发布字段兼容的同 plugin permission 事件合并一次处理，仍逐条写回原 `skillhub_events` 终态。293 条回归为 293 installed、1 次 materialization。active plugin 默认 audience 必须包含经 routing 与 profile 目录双重证明的 `sunke`；inactive 保留 package/audience 但目录和执行入口 fail closed；status-less permission 不解禁，active 恢复保留最近 audience。

Claude Fable 首轮评审为 FAIL 并给出 1 个 P0、5 个 P1；整改方案随后获 `PLAN VERDICT: GO`。首轮修复 `e20a2ad` 已发布生产，WebUI experts API 恢复毫秒级响应；但启动恢复暴露第二个根因：治理断言只扫描 entry+orchestrator，且在失败前已写 active manifest，导致 293 条 permission 账本失败而专家仍 active。生产已把该 manifest 临时设为 inactive，保留 package、audience 与账本。

后续候选 `plugin-governance-all-skills` 经 Cursor Grok 4.5 High 先 NO-GO、补强契约后 `PLAN VERDICT: GO`。实现只扫描 manifest 声明且由该插件实际拥有的 skills，每个门禁必须完整位于至少一个 owned `SKILL.md`；profile/all/department 均在发布前做内容治理，失败也先保留 repo/audience/skills 完整 inactive recovery manifest。其他 plugin 的同名 shared source 由 provenance 拒绝，legacy 空 registry 仅在全库唯一 claim 且 digest 一致时迁移。显式 activation intent、full-snapshot reconcile/uninstall、materialization、治理断言及 manifest 发布纳入同一 per-plugin 跨进程事务锁；status-less permission 不解封，293 条历史失败不改写。本机 targeted 132/132、全量 2758 passed / 1 skipped / 3 deselected，SIM check 通过，独立最终裁决 `pass`；候选已具备发布条件但尚未发布，生产目标插件仍 inactive，未发送 Feishu canary。
