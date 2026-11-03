# H-06 cron provider receipt consumer

Status: local candidate; not reviewed, shipped or deployed.

The live Feishu cron fallback now accepts a run only after one provider-confirmed
card message ID and a successful aggregate media receipt from Hermes Agent.
Missing, partial or rejected media receipts return `delivery_error`, clear stale
unconfirmed terminal receipts and prevent the owner cron-session mirror. Delivery admission
still requires one active actor/profile route that owns the selected sink; two
synthetic identities resolve only to themselves.

The Opus review follow-up also closes every legacy bypass: resolved targets are
authoritative for string, list and `all` delivery forms; mixed-target jobs keep
their non-Feishu legs while Feishu remains provider-receipt gated; and the parent
finalizer cannot persist `completed` for a Feishu delivery without the provider
message ID. When a non-Feishu leg fails after a confirmed Feishu send, the
terminal keeps both `delivery_error` and that confirmed ID so the landed card is
not represented as unattempted. Media-only card previews use the cron job name, a legacy producer
returning `None` is covered explicitly, and the terminal concurrency fixture
uses the same non-reentrant lock as production. The exact three-file focused
suite is 167 passed / 0 failed and `tests/test_*cron*.py` is 128 passed / 0
failed on this candidate.

The post-adjudication follow-up also keeps runtime patch installation compatible
with partial scheduler shapes and stops a resolver-less Feishu-capable delivery
before core. Core receives the same resolved-target override that MT inspected,
so the legacy platform-config path cannot re-resolve a non-Feishu job into a
second receiptless Feishu fallback; the one Feishu path requires `receipt_out`
plus `require_receipt=True`. The reproducible focused command is
`python -m pytest -q tests/test_gateway_shutdown_drain.py tests/test_cron_worker_parallel_dispatch.py tests/test_hook_dispatch.py`;
it is 167 passed / 0 failed on this candidate.

This change must release together with Hermes Agent's
`MediaDeliveryReceipt` producer. Deploying the consumer first is intentionally
fail-closed for media delivery. Real Feishu message-ID readback remains a
post-deploy QA gate and was not executed while building this candidate.
