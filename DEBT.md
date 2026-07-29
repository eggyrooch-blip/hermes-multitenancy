# DEBT — 已知残留债(评审裁决 ship-with-debt 的遗留项)

## 2026-07-29 skillhub-plugin-skill-takeover · PT-001 残留(codex round3 concerns,#p1 收窄)

**场景(窄)**:profile 带既有 personal skills 时的**首次**插件同名接管中,`install_shared_skill_for_profile` 已替换目标后、`.hermes-personal-installs.json` 非原子写入截断/失败——单品链会被 `_restore_standalone_skill` 还原,但该 profile **无关的 personal 所有权元数据可能损坏**(首装无 `_active_plugin_state_transaction_locked` 快照兜底)。

**根治方向(评审建议)**:让 `_active_plugin_state_transaction_locked` 对首装也做快照(插件 manifest + 目标 + 两个 profile manifest),并加 failure-after-real-manifest-mutation 的逐字节回滚测试。

**现状缓解**:takeover 本体已可逆(displace→install 失败还原→成功才摘所有权);PT-002 全局锁已闭;此债只在"首装 × personal-installs 写入中途死"交叠时触发。
