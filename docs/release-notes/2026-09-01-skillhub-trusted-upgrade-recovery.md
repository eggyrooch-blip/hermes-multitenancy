# SkillHub trusted plugin upgrade recovery candidate

Status: local candidate; not shipped or deployed.

- Trusted AiDock publishers still pass integrity, ownership, identity, isolation, and expert-health gates; trust removes manual review only.
- Active profile-mode upgrades validate plugin-private collision sources, and failed candidates restore those sources with the existing manifest/shared/profile transaction.
- A cached retry of a different verified release retains same-plugin overwrite authority; cross-plugin collisions remain blocked.
- Plugin grants require `profile_id` to resolve exactly one active user route. Optional `employee_id` and `open_id` must agree with that row, otherwise the whole event fails before download or plugin writes.

Local evidence: the four targeted test files pass 173/173. Production recovery and WebUI visibility remain post-ship gates.
