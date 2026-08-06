# Hermes Health-Check — 部署与运维指南

> 五探针健康检查系统，systemd timer 驱动，飞书群 webhook 告警。

## 安装

```bash
# 1. 复制脚本和 unit 到生产（hermes-1 上执行）
cp deploy/health_probes.py ~/.hermes/health_probes.py
cp deploy/hermes-health-check.sh ~/.hermes/bin/hermes-health-check.sh
chmod +x ~/.hermes/bin/hermes-health-check.sh

# 2. 安装 systemd units
cp deploy/hermes-health-check.service ~/.config/systemd/user/
cp deploy/hermes-health-check.timer ~/.config/systemd/user/

# 3. 配置飞书 webhook（复用已有的 alert.env）
# 确认 ~/.hermes/update-center/alert.env 包含：
# HERMES_UPDATE_CENTER_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/<id>

# 4. 启用 timer
systemctl --user daemon-reload
systemctl --user enable --now hermes-health-check.timer

# 5. 验证
systemctl --user list-timers | grep health-check
systemctl --user start hermes-health-check.service  # 手动触发一次
cat ~/.hermes/health-check/health-check.log
```

## 五个探针

| # | 探针名 | 检查内容 | 阈值 | 告警级别 |
|---|--------|----------|------|----------|
| 1 | `api_error_rate` | gateway 日志中 ERROR/CRITICAL 占比 | **>10%** | P1 |
| 2 | `queue_backlog` | kanban.db 中超时 30 分钟的 todo/claimed 任务数 | **>20** | P1 |
| 3 | `zombie_tasks` | claimed/running 状态但心跳超时 10 分钟的任务数 | **>0** | P1 |
| 4 | `notify_failures` | gateway 日志中 delivery error/send failed 次数 | **>3** (5 分钟内) | P1 |
| 5 | `billing_drift` | multitenancy.db 中有 billing identity 但无 key_id 或未 enforced 的员工数 | **>5** | P2 |

## 数据源路径

探针只读以下已知路径（不用 `find /`）：

| 探针 | 数据源 |
|------|--------|
| 1, 4 | `~/.hermes/profiles/*/logs/gateway.log` |
| 2, 3 | `~/.hermes/kanban.db`（只读 SQLite） |
| 5 | `~/.hermes/multitenancy.db`（只读 SQLite） |

## 告警机制

- **飞书群 webhook**：复用 `update_center_alert.py` 的同一个 webhook（`HERMES_UPDATE_CENTER_WEBHOOK`）
- **去重窗口**：30 分钟内同一探针不重复告警
- **恢复通知**：探针从 alert 恢复到 pass 时发一条 🟢 恢复消息
- **失败链路**：`OnFailure=hermes-update-center-alert@%n.service`（健康检查自身失败也会告警）

## 调参

所有阈值都是 `health_probes.py` 中的模块级常量。修改后重启 timer 即生效：

```python
# deploy/health_probes.py
API_ERROR_RATE_THRESHOLD = 0.10      # 探针 1：10%
QUEUE_BACKLOG_THRESHOLD = 20.0       # 探针 2：20 个任务
ZOMBIE_TASK_THRESHOLD = 0.0          # 探针 3：0 个容忍
HEARTBEAT_TIMEOUT_SECONDS = 600      # 探针 3：10 分钟
NOTIFY_FAILURE_THRESHOLD = 3.0       # 探针 4：3 次
BILLING_DRIFT_THRESHOLD = 5.0        # 探针 5：5 人
```

bash 脚本中可通过环境变量覆盖路径：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `HERMES_HOME` | `$HOME/.hermes` | Hermes 主目录 |
| `HERMES_UPDATE_CENTER_WEBHOOK` | （从 alert.env） | 飞书群 webhook URL |
| `HERMES_HEALTH_PYTHON` | 自动检测 | Python 解释器路径 |

## 故障排查

```bash
# 查看最近日志
tail -20 ~/.hermes/health-check/health-check.log

# 手动触发一次
systemctl --user start hermes-health-check.service

# 手动跑探针看结果
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/health_probes.py \
  --kanban-db ~/.hermes/kanban.db \
  --multitenancy-db ~/.hermes/multitenancy.db \
  --gateway-log ~/.hermes/profiles/multitenancy_router/logs/gateway.log \
  --json

# 清除去重状态（强制重新告警）
rm ~/.hermes/health-check/probe_*
```

## 相关文档

- 事故分级处置：`runbook-事故分级.md`（Obsidian hermes 目录）
- 备份脚本：`deploy/hermes-backup.sh`
- 看门狗：`~/.hermes/bin/gateway-watchdog.sh`（gateway 存活检测，与本系统互补）
