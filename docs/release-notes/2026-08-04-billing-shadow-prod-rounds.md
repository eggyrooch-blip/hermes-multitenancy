# 2026-08-04 billing shadow production rounds (canary 硬门禁其一)

Status: 核心 replay 两轮通过(mt 侧判据全满足);readiness 第三源 inconclusive(结构性缺口,见下);
canary 硬门禁整体**未完成**。服务状态零变更(业务 DB/配置/服务未动;主机新增物仅 operator 目录与 0600 报告)、零通知、零模型调用;计费保持 dormant。

- 执行: hermes-1 独立 operator 检出 main@6bd1c38,`hermes-multitenancy-billing-shadow` 以 hermes 身份
  mode=ro 原地扫 routing SQLite + org snapshot,planner-only admission replay;三次关键运行经
  ftask capture 留痕,辅助核查为 ssh 直查(档于 .ftask 证据链)。主机新增物仅 operator 目录与 0600 报告。
- Round 1 (22:36, org digest `63574a2c`) 与 Round 2 (23:09, org digest `decaf58d`,
  由 hermes-feishu-sync.timer 23:02 班次自然刷新构成真实 refresh 夹层;两轮均由 in-band
  自证脚本执行,运行前后脚本哈希一致闭链): 均 ok=true,
  universe 1282 全分类(COHORT_WOULD_ENFORCE=1(单操作员 cohort)/ENFORCED_EXISTING=1/
  IDENTITY_INVALID=1/NONCOHORT_LEGACY=1279/DRIFT=0),非 cohort 误伤 0,
  断言块(billing DB 写/gateway ensure/dispatch)全 0(boundary_stops=17 为测量值),
  org 对账 1282/1282 双向零缺,轮内 routing digest 前后一致(`07c886b9`)。
- 健康: 两轮前后 apiserver/两库 quick_check/补丁存活同像;三服务 active(XDG_RUNTIME_DIR 复核)。

## enable 前必须处理的发现
1. `multitenancy_billing_identities` 存在 1 条历史 enforced 行(行级属性与员工标识仅存主机 0600 报告,不入仓)——计费从未 enable 却带 enforced 状态,须清理或转正后重扫。
2. readiness 第三源(20.3 `broker-readiness-snapshot`)本轮 inconclusive: org snapshot 员工
   schema 无 email 字段,CLI 以 canonical email 为必填 join 键;mt 侧 employee JSON 导出器
   与 email 权威源尚未建成(最终放行 slug 的工程项)。
3. 1 个 root 为 IDENTITY_INVALID(独立 fail-closed 桶,明细在主机 0600 报告,24h 销毁)。

依 canary SPEC,本记录不构成 `READY_FOR_SINGLE_OPERATOR_CANARY`——synthetic 生命周期矩阵
与联合放行 artifact 仍是 enable 前置。
