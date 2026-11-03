# LiteLLM per-employee Hermes Key consumer (2026-07-22)

Production impact: none yet. This change is implemented and tested on the task
branch but has not been shipped or enabled.

## Changed

- Replaced Multitenancy's direct LiteLLM admin/callback design with the
  versioned AI Gateway `ensure/ack` contract.
- Added encrypted, per-payer Hermes Key storage with probe, ACK recovery,
  renewal jitter/backoff, invalid-key repair, and irreversible
  `legacy -> enforced` state.
- Forced one payer key across main, auxiliary, media/vision, child Agent, and
  warm-worker model paths; disabled billing-unsafe fallbacks.
- Added distinct 401, monthly-budget 429, and ordinary rate-limit handling.
- Removed the LiteLLM callback, callback config fragment, SpendLogs index
  migration, and their tests. AI Gateway is now the only management plane.
- Hardened identity trust, cross-payer SQLite access, secret redaction, service
  Bearer isolation, and `0600` vault permissions.

## Operator action before rollout

1. Deploy and verify the matching AI Gateway contract and private TLS route.
2. Install the dedicated broker Bearer and credential encryption key through a
   root-owned systemd EnvironmentFile.
3. Confirm the LiteLLM model base URL and begin with an explicit canary payer
   list.
4. Run Feishu/WebUI UAT and verify profile apiserver plus
   `multitenancy_sessions` context mirroring before expanding the cohort.

No production deployment or migration is performed by this change alone.
