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

Production remains on `release-20260812-06`; `hermes-release.timer` is disabled
until this guard is shipped and a new protected release succeeds. Focused tests
are 61/61, final simulation is 4/4, and the second independent T2 review closed
both release-boundary P1 findings. The candidate is reviewed but not shipped.
