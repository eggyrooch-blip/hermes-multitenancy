# Lark CLI Capability Matrix UAT - 2026-05-16

## Goal

Validate every currently exposed `lark-cli` domain capability through four Hermes entry paths, with three natural-language messages per capability per path:

1. T1: group profile WebUI -> group profile `lark_cli` using bot identity.
2. T2: Feishu group chat -> group profile `lark_cli` using bot identity.
3. T3: `owner` profile WebUI -> `lark_cli` using user identity when UAT exists.
4. T4: `owner` Feishu private chat -> `lark_cli` using user identity when UAT exists.

The four-message smoke test is not sufficient for this goal. Completion requires each row below to have three verified messages in all four paths, or an explicit product decision that a capability is not applicable to a path.

## Capability Scope

Current `lark-cli --help` domains counted as user-visible capabilities:

| Capability | Primary safe UAT shape |
|---|---|
| approval | read/list approval tasks or instances |
| attendance | query current user's attendance records |
| base | create disposable Base/table/record or query known test Base |
| calendar | agenda/freebusy/create disposable test event |
| contact | get current user / search known user |
| docs | create/fetch/update disposable docx |
| drive | search/create-folder/upload/download disposable file |
| event | list/schema/status bounded checks |
| im | send/list/search message or chat |
| mail | triage/draft only; no actual send unless explicitly approved |
| markdown | create/fetch/overwrite disposable Markdown file |
| minutes | search/query existing minutes; upload only with disposable media |
| okr | list cycles/read objectives; progress write only if allowed |
| openapi-explorer | bounded raw OpenAPI canary such as bot info |
| shared | credential/status/help boundary checks for externally injected auth |
| sheets | create/read/write disposable spreadsheet |
| skill-maker | inspect a command schema and describe a reusable skill wrapper |
| slides | create/fetch/update disposable presentation |
| task | create/get/update/complete disposable task |
| vc | search/query meeting notes/recordings |
| vc-agent | inspect meeting-join command shape; do not join a real meeting |
| whiteboard | query/update disposable whiteboard in a test doc |
| wiki | list spaces/create disposable node if permitted |
| workflow-meeting-summary | read VC/minutes inputs and report bounded summary preconditions |
| workflow-standup-report | combine calendar agenda and task list in a bounded standup check |

Shared/meta skills (`lark-shared`, `lark-skill-maker`, workflow summary helpers) are counted in this matrix because they are exposed to the user as callable `lark-cli` skills. Total coverage is 25 capabilities x 3 messages x 4 paths = 300 rows.

## Evidence Rules

- Each message must be a normal user-facing natural-language request.
- Each result must show either a concrete artifact ID/link, a concrete returned record count/object identity, or a clear permission/precondition error from `lark_cli`.
- "No permission", "unsupported", or "missing resource" is a valid finding only after the request actually routed through `lark_cli`.
- For T2/T4, evidence must come from profile `state.db` or Feishu-visible conversation. API-sent messages that do not enter the Feishu event gateway do not count as T2/T4 input.
- Failures enter fix -> retest. The original failed message and the successful retest both stay in the ledger.

## Batch Plan

| Batch | Capabilities | Risk shape |
|---|---|---|
| A | contact, event, docs, drive, im, calendar | mostly read or disposable write |
| B | markdown, sheets, slides, task, base | disposable write/read/update |
| C | minutes, vc, approval, attendance, okr | read/list first, write only if needed |
| D | mail, whiteboard, wiki | precondition-heavy or higher impact; use draft/disposable resources |

## Result Matrix

Final status at 2026-05-16 05:45 CST:

| Path | Coverage | Pass | Blocked | Fail | Evidence |
|---|---:|---:|---:|---:|---|
| T1 group profile WebUI -> group `lark-cli` | 25 capabilities x 3 messages = 75 rows | 67 | 8 | 0 | `/tmp/hermes-lark-cli-matrix/*webui*.jsonl`, `t1-known-fails-rerun-20260516-0408.jsonl` |
| T2 Feishu group chat -> group `lark-cli` | 25 capabilities x 3 messages = 75 rows | 66 | 9 | 0 | `t2-full-batch-a-20260516-0410.jsonl`, `t2-full-batch-b-20260516-0415.jsonl`, `t2-batch-b-fixes-20260516-0425.jsonl`, `t2-full-batch-c-20260516-0435.jsonl`, cleanup rows |
| T3 owner WebUI -> user `lark-cli` | 25 capabilities x 3 messages = 75 rows | 72 | 3 | 0 | `t3-webui-full-fixed-20260516.jsonl`, `t3-tail-fixed-20260516-040306.jsonl` |
| T4 owner Feishu private chat -> user `lark-cli` | 25 capabilities x 3 messages = 75 rows | 72 | 3 | 0 | `t4-representative-fixed-mark-20260516-035617.jsonl`, `t4-full-batch-a-20260516-0448.jsonl`, `t4-full-batch-b-20260516-0455.jsonl`, `t4-full-batch-c-20260516-0504.jsonl`, cleanup rows |

All four paths are full 25-capability matrices. T2 and T4 are real Feishu message-entry tests, not local simulations.

## Representative Real-Chat Evidence

| Path | User-facing request shape | Verified result |
|---|---|---|
| T2 group Feishu -> group profile bot | Create a Feishu doc through `docs +create --api-version v2` | Created doc `YoKqdGHzfoJi2MxVf5Nc6pIln1e` through bot identity |
| T2 group Feishu -> group profile bot | Search group chat through `im +chat-search` | Found `群聊 P1 测试`, `oc_dfe8bc83167b092e138e4b4e6ac9ade5` |
| T2 group Feishu -> group profile bot | Read today's agenda through `calendar +agenda` | Returned 0 bot calendar events |
| T2 group Feishu -> group profile bot | Run standup workflow via calendar + task | Calendar succeeded, task blocked because `task +get-my-tasks` only supports user identity |
| T2 group Feishu -> group profile bot | Create Base / Markdown / Sheets / Slides artifacts | Created disposable Feishu resources through bot identity |
| T4 owner Feishu private chat -> user profile | Create a Feishu doc through `docs +create --api-version v2` | Created doc `UHPadVNMKot3mrx19cqc2Z0nnrc` |
| T4 owner Feishu private chat -> user profile | Read today's agenda through `calendar +agenda` | Returned 3 events: daily standup reminder, LARKCLI smoke event, UAT test event |
| T4 owner Feishu private chat -> user profile | Read my tasks through `task +get-my-tasks` | Returned 3 tasks |
| T4 owner Feishu private chat -> user profile | Run standup workflow via calendar + task | Both calls succeeded: 3 calendar events and 3 tasks |
| T4 owner Feishu private chat -> user profile | Create Sheets / Slides artifacts | Created `OJ08sDsilhSMZhtTgBac30Nvnxc` sheet and `KHIEspGBXlxhP9dz910cq5YSnQc` slide deck |

## Blocked Findings

These are not Hermes execution failures. They are concrete `lark-cli` identity, permission, or product precondition boundaries.

| Path | Blocked rows | Reason |
|---|---|---|
| T1 group WebUI | `contact/3`, `minutes/3`, `task/3`, `vc/3`, `workflow-meeting-summary/3`, `workflow-standup-report/3` | Group profiles are forced to bot identity; these rows need user identity or user UAT |
| T1 group WebUI | `event/3`, `mail/3` | Event/mail canary is not usable with the current group bot precondition |
| T2 group Feishu | `task/3`, `workflow-standup-report/3` | Same group-bot identity boundary; `task +get-my-tasks` only supports user identity |
| T2 group Feishu | `contact/3`, `drive/3`, `mail/3`, `minutes/3`, `vc/3`, `workflow-meeting-summary/3`, `shared/3` | User-only capability, mailbox/account precondition, missing search scope, or external credential management boundary |
| T3 owner WebUI | `drive/3` | Missing or unavailable drive/search scope for the tested command |
| T3 owner WebUI | `mail/3` | User mailbox capability is not enabled/available for this account |
| T3 owner WebUI | `shared/3` | Auth status is external-provider managed, so interactive auth status cannot enumerate user/bot state |
| T4 owner Feishu private | `drive/3` | Missing `search:docs:read` scope for `drive +search` |
| T4 owner Feishu private | `mail/3` | Feishu Mail reports `user not found`; mailbox is not enabled/bound for this account |
| T4 owner Feishu private | `shared/3` | Auth status is external-provider managed, so interactive auth status cannot enumerate user/bot state |

## Fixes Made During Matrix Run

- Kept only `lark-cli` as the configured Feishu toolset for generated profiles.
- Ran `lark-cli` inside the profile runtime sandbox using the authsidecar, not through terminal or global shell access.
- Forced group profiles to bot identity and user profiles to user identity when UAT credentials exist.
- Added runner locking so Feishu true-chat tests do not interleave multiple matrix sessions into the same chat.
- Hardened matrix prompts to use official shortcut commands, for example `calendar +agenda`, `docs +create --api-version v2`, `task +get-my-tasks`, and `vc +meeting-join`.
- Required final responses to echo a unique test mark on the first line, fixing Feishu private-chat rows that previously answered correctly but were not machine-correlatable.
- Added no-cache/no-history prompt guards so repeated Feishu rows do not reuse previous tool results.
- Added verdict handling for profile-managed external credentials, user-only/bot-only boundaries, Feishu Mail `user not found`, and Feishu artifact links whose tokens are only present in the URL.
- Fixed final artifact checks for Base, Markdown, Sheets, and Slides links.

## Upgrade Note

The local bridge calls the shared `lark-cli-authsidecar` binary through `HERMES_LARK_CLI_BIN`. To upgrade upstream `lark-cli` safely:

1. Build or install the new upstream binary into the shared Hermes bin location as `lark-cli-authsidecar`.
2. Run the read-only canary before replacing production traffic: `python -m hermes_multitenancy.lark_cli_canary --profile <profile>`.
3. Run the matrix runner against WebUI first, then Feishu true-chat representative rows.
4. If the canary or matrix fails, roll back only the shared binary; Hermes profile routing and UAT credentials remain unchanged.

## Final Runner Evidence

Primary evidence files:

- T1: `/tmp/hermes-lark-cli-matrix/t1-known-fails-rerun-20260516-0408.jsonl` plus earlier WebUI full runs.
- T2: `/tmp/hermes-lark-cli-matrix/t2-full-batch-a-20260516-0410.jsonl`, `t2-full-batch-b-20260516-0415.jsonl`, `t2-full-batch-c-20260516-0435.jsonl`, `t2-shared3-final2-20260516-0545.jsonl`.
- T3: `/tmp/hermes-lark-cli-matrix/t3-webui-full-fixed-20260516.jsonl`, `t3-tail-fixed-20260516-040306.jsonl`.
- T4: `/tmp/hermes-lark-cli-matrix/t4-full-batch-a-20260516-0448.jsonl`, `t4-full-batch-b-20260516-0455.jsonl`, `t4-full-batch-c-20260516-0504.jsonl`, `t4-final-cleanup-20260516-0535.jsonl`, `t4-slides3-final-20260516-0542.jsonl`.

Final ledger recompute:

| Path | Pass | Blocked | Missing | Fail |
|---|---:|---:|---:|---:|
| T1 | 67 | 8 | 0 | 0 |
| T2 | 66 | 9 | 0 | 0 |
| T3 | 72 | 3 | 0 | 0 |
| T4 | 72 | 3 | 0 | 0 |

Remaining product actions, not Hermes bridge bugs:

1. Add/authorize `search:docs:read` if `drive +search` should work for `owner` UAT.
2. Enable/bind Feishu Mail mailbox if `mail +triage` should work for `owner`.
3. Keep `auth status` expectations documented: Hermes injects credentials externally, so interactive `lark-cli auth` management is intentionally blocked inside profiles.
