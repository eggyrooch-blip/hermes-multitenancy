# 2026-08-06 计费 enable：撤除启动期签名放行 ceremony

Status: 待合入 main，随 LiteLLM 员工计费上线批发布。生产计费开关另行开启。

## 改了什么

`startup_guard._validate_billing_cohort` 不再调用
`billing_readiness.verify_enabled_environment`。此前 `HERMES_LITELLM_BILLING_ENABLED=true`
时，网关启动必须先满足一整套签名放行凭证：8 个 env（readiness artifact、replay store、
policy digest、code sha、contract major、routing watermark、org sha、broker token）
+ 双 artifact 验签 + nonce live recheck。现在这些都不再是启动前置。

**保留不变**：cohort 形状硬门。`HERMES_LITELLM_BILLING_PAYER_IDS` 为空或含 `*`
仍然抛 `StartupGuardError("billing_canary_cohort_invalid")` —— 它防的是手滑给全员
开计费，与本次撤除的 ceremony 无关。

**未删除**：`billing_readiness.py` 本体原样保留。注意一个如实的说明——摘掉这处调用后，
`verify_enabled_environment` 在生产代码里**已无调用方**（`billing_shadow.py` 只 import
`BillingReadinessError` 与 `cohort_hash`，不走验签路径），即它成为死代码。保留而不删，
是因为签名 artifact 机制本身还可能被 operator 手工核查复用；但「shadow/CLI 仍在调它」
的说法不成立，别据此以为还有活的第二调用点。清理与否另行决定，已记 DEBT。

## 为什么这套 ceremony 失去了存在理由

它存在的目的只有一个：防止「凭据/路由漂移的情况下开了计费，导致静默错记账或卡住运行」。
同批的 `billing-degrade-not-refuse`（sunke 2026-08-06 在 A/B/C 三条路里选 C）已在
运行时层面消灭了这个后果——凭据拿不到时请求照常完成（走共享 key、不标 enforced、
落一条可检索的降级审计），而身份不一致（profile / email / 账号漂移、篡改、缺密钥、
契约不符）**仍然拒绝**。

于是启动期 ceremony 拦的是一个已经不会发生的后果，却仍在阻塞 enable。

## 一条必须写明的运维事实

生产 systemd 单元里，本检查是
`ExecStartPre=…python -m hermes_multitenancy.startup_guard preflight`，
**不带前导 `-`，即致命**。它抛错，网关就起不来——那是 1259 人的生产网关。

这条约束的直接后果：**任何来自计费外部世界的输入（网关、LiteLLM、组织快照、签名 artifact）
都不该成为启动条件**。本单撤除 ceremony 的同时，也没有用别的计费检查去替代它（评审过程中
一度加过「cohort 工号必须在组织快照里」的校验，因为同样的理由撤回，见 SPEC Dead ends）。
计费是记账，不是门禁——这条在运行时成立，在启动期同样成立。

保留的 cohort 形状硬门不违反上面这句：它只读我们自己在 `EnvironmentFile` 里写下的那个
env，不查询任何外部系统，判的是「这份配置本身是否写错了」，失败即部署配置错误，本就该
拦在启动。区别在于**输入是否来自我们控制之外**。

## 与旧发行说明的关系

`docs/release-notes/2026-07-23-billing-plugin-release-gates.md` 与
`docs/release-notes/2026-08-04-billing-shadow-prod-rounds.md` 中「联合放行 artifact /
canary 硬门禁是 enable 前置」「billing must remain off / startup 故意 fail closed」
等表述，自本单起失效。漂移类仍然 fail-closed（在运行时），代码与配置的锚定由受管发布
（探针 + 自动回滚）承担。
