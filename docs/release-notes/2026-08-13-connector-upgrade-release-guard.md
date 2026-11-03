# Connector upgrade release guard — 2026-08-13

Production `release-20260813-01` exposed two release-boundary failures: Meegle
was installed over the network from gateway `ExecStartPre`, exceeding the 90s
start timeout, and the failed restart left a SQLite writer transaction that
blocked durable Feishu admission.

The follow-up makes gateway startup check-only, stages Meegle before stopping
the old service, probes the session DB with a no-op write transaction, treats a
failed service start as rollback, restores editable installs using the physical
previous release path, and rolls back failed `SessionStore` transactions with a
10s SQLite busy timeout.

The guard shipped through MR !73 and is contained in production
`release-20260813-04` at multitenancy
`2f46bf2822363072e776ec3a2133ed3c2ed878a6`. Focused tests are 61/61, final
simulation is 4/4, and the second independent T2 review closed both
release-boundary P1 findings. The production executor passed 12/12 probes and
the rollback anchor records SUCCESS. Post-restart logs contain zero database
lock, durable admission, traceback, isolation, or credential-identity errors.

After the lark-cli 1.0.86 / Meegle 1.0.19 rollout, a read-only registered-tool
canary for the unique sunke route passed self=1/cross=0 through the run-scoped
auth broker. The remote latest tag and deployed marker both read
`release-20260813-04`, so `hermes-release.timer` is restored enabled/active.
