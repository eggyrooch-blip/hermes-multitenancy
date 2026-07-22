# 2026-07-22 AIHub plugin 授权突发事故与候选修复

生产 `keep-sharetemplate` 一次收到 293 条 permission 事件。旧 Run Broker 为每条事件向 asyncio 默认 executor 投递一次慢安装，与 experts catalog 共用线程池，导致队列积压、部分失败、同 manifest 并发写和 WebUI 专家页 Loading。

候选修复把 SkillHub 安装集中到专用 `max_workers=1` drain；Webhook 与进程启动只负责唤醒。相邻且发布字段兼容的同 plugin permission 事件合并一次处理，仍逐条写回原 `skillhub_events` 终态。293 条回归为 293 installed、1 次 materialization。active plugin 默认 audience 必须包含经 routing 与 profile 目录双重证明的 `sunke`；inactive 保留 package/audience 但目录和执行入口 fail closed；status-less permission 不解禁，active 恢复保留最近 audience。

Claude Fable 首轮评审为 FAIL 并给出 1 个 P0、5 个 P1；整改方案随后获 `PLAN VERDICT: GO`。本机正确 Hermes venv targeted 91/91，全量 2743 passed / 1 skipped / 3 deselected。生产未发布、未重启、未做真实 WebUI 或 Feishu 验收。
