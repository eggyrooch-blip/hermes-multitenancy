# macOS Run Broker profile traversal — local candidate

- Root cause: `profile-default.sb` allowed `PROFILE_HOME` but denied metadata traversal of its `/Users` and `USER_HOME` ancestors, so AIAgent failed before tool initialization with `/Users` EPERM.
- Fix: grant `file-read-metadata` to those two exact ancestors only. No home content/read/write scope was added.
- Evidence: affected tests 241/241; real sandbox resolves the routed profile while host-home listing remains denied; run-scoped cross-owner regression remains denied.
- Local UAT on 8748/8877: actor-bound `lark_cli drive +inspect --url` completed HTTP 200/done with 0 error, 0 auth-required, 9 tool events, no `/Users` EPERM or `Run is unavailable`; same-run state readback contained the expected Base title.
- A failed exploratory call used unsupported `lark-cli base meta`; its auth-required presentation was not a credential-expiry signal. Supported `drive +inspect` succeeded.

Production was not connected or changed. No credentials, Feishu delivery, cron, profile apiserver, or `multitenancy_sessions` data were changed.
