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
- 2026-06-10: personal profiles may explicitly use bot identity only for owner-mapped Feishu group message sends. The run scope derives `HERMES_FEISHU_BOT_ALLOWED_CHAT_IDS` from active routing group rows owned by the sender and passes the same allowlist into the auth broker; unmapped group sends and non-message personal bot writes are rejected before spawn/token lookup. Plain `identity=auto` still keeps the existing default-user behavior when the sender has UAT, so user-identity capability is not degraded.
- 2026-06-11: owner-mapped personal bot card sends may need a prior `im/v1/images` upload to obtain an `image_key`. That upload is allowed only when the personal profile has a non-empty owner-mapped bot chat allowlist, and the actual message send still has to target an allowed chat ID. Calendar/docs/base and other non-IM-image bot writes remain rejected before spawn/token lookup.
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

## 2026-05-19 T4 Restore-CardKit Rerun

During `restore-feishu-cardkit-output`, T4 was rerun through real Feishu private chat after the CardKit/lark-cli identity fixes. Mail was intentionally skipped per operator instruction. The rerun scope was every non-mail `lark-cli` capability with three natural-language requests per capability, plus `shared` credential-boundary checks.

| Path | Coverage | Pass | Blocked | Skipped | Fail | Evidence |
|---|---:|---:|---:|---:|---:|---|
| T4 `feishu_g41a5b5g` Feishu private -> user `lark-cli` | 24 non-mail capabilities x 3 messages = 72 rows | 71 | 1 | 3 mail rows | 0 | `/tmp/hermes-lark-cli-matrix/t4-*fix*.jsonl`, summaries `t4-*-summary-20260519-*.tsv` |

Representative evidence:

- `contact/3` originally failed because the auth broker used profile short id `g41a5b5g` instead of raw Feishu `ou_cf23e7c262afa4b7a006baa75f863ed5`. After `_aiagent_subprocess_env_scope()` switched to raw sender open_id, all contact rows passed.
- `slides/1` originally failed because default `--as user` was injected into a help command. After help/auth/schema/event commands stopped receiving identity injection, slides rows passed.
- Real disposable artifacts created by user identity include docx `Z30LdY1UmoTr0bxnhFtcyqSDnid`, sheet `FfTpsCfKmhz7AbtodhVcoCMOncf`, and slide deck `KXaFsqWW4l1yXpdgjWacIYopn3s`.
- `shared/auth status` remains an expected blocked row: Hermes injects credentials externally, so profile-local interactive `lark-cli auth` management is intentionally unavailable.

Remaining product actions, not Hermes bridge bugs:

1. Add/authorize `search:docs:read` if `drive +search` should work for `owner` UAT.
2. Enable/bind Feishu Mail mailbox if `mail +triage` should work for `owner`.
3. Keep `auth status` expectations documented: Hermes injects credentials externally, so interactive `lark-cli auth` management is intentionally blocked inside profiles.

## Release UAT Gates Added 2026-05-19

Before shipping `restore-feishu-cardkit-output`, keep these as explicit gates:

- T4 real Feishu private chat, no-mail `lark-cli` rerun stays required.
- Real Feishu context-continuity UAT stays required for both private chat and group chat. Each release candidate must run a multi-turn sequence that proves the next turn can use prior user facts, prior assistant facts, and prior tool/file results without the user restating them. A broken follow-up such as answering a different topic, forgetting a marker/code word, or losing the previous file/tool artifact is a release-blocking `fail`.
- Real Feishu file IO media matrix stays required: inbound `.md/.txt/.json/.csv/.xlsx/.pdf/.docx` must read markers; inbound images are allowed only as `blocked` while the vision provider is invalid; outbound generated files must be requested with natural user prompts, not internal `/workspace` or `MEDIA:` instructions.
- Current implementation supports natural outbound prompts via profile SOUL + `hermes-artifact-json` bridge: model emits `filename/format/content|data|rows`, router defaults to `workspace/Downloads` and auto-delivers the file. Focused local regressions passed; latest real natural-prompt rerun is blocked before Hermes receives the request because `lark-cli-authsidecar POST /open-apis/im/v1/messages --as user` returned `HTTP 502: forward request failed` for `20260519_221201` and `20260519_222135`, with no matching `state.db` rows. Do not count this as Hermes file-output failure; rerun once Feishu/lark-cli user message sending is stable.

## Release UAT Gate Added 2026-05-19: Context Continuity

This gate is separate from the lark-cli capability matrix and the file IO media matrix. It exists because a single-turn pass can still hide a broken session/history path.

| Path | Required sequence | Pass condition |
|---|---|---|
| T4 private Feishu UAT | `/new`; send a unique marker plus a code word; follow up with "刚才的代号是什么"; follow up again asking it to relate that code word to the previous answer | The assistant answers the exact marker/code word in each follow-up without the user restating it |
| T2 group Feishu UAT | `@bot /new`; send a unique marker plus a code word in the group; follow up with `@bot 刚才的代号是什么`; then ask one more contextual question | The group profile keeps the same group+sender context and answers the prior marker/code word |
| Private file/tool continuity | Upload or generate a file/tool artifact; then ask "继续用刚才那个文件/结果..." | The assistant uses the previous artifact/result and does not ask for it again |
| Group file/tool continuity | Same as above, but through the group profile and bot identity | The assistant keeps the prior artifact/result within that group profile only |

Evidence must include Feishu message IDs or profile `state.db` rows for every turn. Any `/new`, duplicate-message skip, approval prompt, or interrupted long task must be recorded because it changes the session boundary.

### 2026-05-19 Context Evidence

Two real-message continuity probes were run after the user reported group/private context felt disconnected.

| Probe | Path | Marker base | Result | Evidence |
|---|---|---|---|---|
| Short-term no-tool | T4 private Feishu UAT | `CTX_NOTOOL_PRIVATE_20260519_232945` | pass: no tools used; turn B recalled code word `苜蓿` from turn A | `feishu_g41a5b5g/state.db` rows `5045-5048` |
| Short-term no-tool | T2 group Feishu UAT | `CTX_NOTOOL_GROUP_20260519_233041` | pass: no tools used; turn B recalled code word `苜蓿` from turn A | `feishu_group_dfe8bc83167b_e18e/state.db` rows `2209-2212` |
| Memory-assisted sanity | T4 private Feishu UAT | `CTX_PRIVATE_20260519_232507` | pass: turns B/C recalled `海盐`; turn A used `memory`, so this is not counted as pure session-history proof | `feishu_g41a5b5g/state.db` rows `5037-5044` |
| Memory-assisted sanity | T2 group Feishu UAT | `CTX_GROUP_20260519_232719` | pass: turns B/C recalled `海盐`; turn A used `memory`, so this is not counted as pure session-history proof | `feishu_group_dfe8bc83167b_e18e/state.db` rows `2201-2208` |

Operational note: an earlier context probe sent the next turn while a previous turn was still streaming, causing a visible "Interrupting current task" placeholder in Feishu. That is a test-runner bug, not a passing context result. Future UAT scripts must wait for a stable final assistant row before sending the next turn.

### 2026-05-20 CardKit Final-Card Evidence

The Feishu CardKit release gate also checks the user-visible final card shape after tool execution. The required shape is Hermes product style, not raw native Hermes text: `Tool calls:` with business tools only, optional collapsed reasoning, markdown body, then `Done (x.xs)`.

| Probe | Path | Marker | Result | Evidence |
|---|---|---|---|---|
| Final card lark-cli | T4 private Feishu UAT | `CARDKIT_PRIVATE_SHIP_20260520_000543` | pass: final card contains `Tool calls`, a single visible `lark_cli` duration line, body, and `Done`; no `generating arguments`, `skill_view`, or process narration | Feishu interactive message `om_x100b6ffceb0eaca4b3c2f594fe7937a` |
| Final card lark-cli | T2 group Feishu UAT | `CARDKIT_GROUP_SHIP_20260520_000543` | pass: group mention entered the group profile, `lark_cli` used bot identity to read recent group messages, final card contains clean tool/body/done layout; no internal tool rows or process narration | Feishu interactive message `om_x100b6ffce7e8d0a4b208a3d2c5e70c4`, `feishu_group_dfe8bc83167b_e18e/state.db` rows `2253-2258` |

Regression coverage for this gate: `tests/test_streaming_card_transport.py tests/test_commands.py::test_new_command_resets_session_history tests/test_group_provisioning.py tests/test_lark_cli_tool_registration.py` => `70 passed`.
