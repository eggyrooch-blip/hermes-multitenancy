# hermes-multitenancy

> **One Feishu bot, N users, N profiles.** A [hermes-agent](https://github.com/NousResearch/hermes-agent) plugin that routes each Feishu user to their own profile (independent SOUL.md, sessions, memories, LLM credentials) — without modifying a single line of hermes-agent.

**English** | [简体中文](README.zh-CN.md)

[![tests](https://img.shields.io/badge/tests-139%20passing-brightgreen)](#testing)
[![hermes 0 patches](https://img.shields.io/badge/hermes--agent-0%20patches-brightgreen)](#how-it-stays-compatible)
[![real Feishu verified](https://img.shields.io/badge/real%20Feishu-verified-brightgreen)](#proof-of-end-to-end)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

---

## 😖 Why I built this (the pain)

I love hermes-agent — it's the most polished personal-agent runtime I've used. But shipping it inside a company hit one wall:

> **Hermes assumes 1 bot = 1 user.** A "profile" is a HERMES_HOME directory. Each profile spawns its own gateway process, owns its own Feishu app credentials, and runs its own websocket. So my plan to put a single bot into a 1000-person company collapsed:
>
> - **Option A** — 1000 separate hermes processes? Can't fit 1000 × 86 MB lark_oapi in memory, can't get IT to provision 1000 Feishu apps.
> - **Option B** — Single bot, single profile, all 1000 users sharing one SOUL? Then it's not "千人千面" anymore — every employee gets the same persona, no per-user memory.
> - **Option C** — Patch `feishu.py` to demux by user? Every hermes upgrade I'd have to re-patch by hand. I tried this for an afternoon and gave up.
> - **Option D** — Fork hermes-agent, maintain my own branch? I'd be carrying a permanent debt against an active upstream.

**None of those work.** I wanted hermes' rich UX (streaming, reactions, multi-turn, sessions) AND multi-tenant routing AND zero patches to hermes-agent.

This plugin is the answer: a **`pre_gateway_dispatch` hook** intercepts every Feishu message, looks up the user in a SQLite routing table, dispatches to a per-user `ProfileRuntime` that holds an independent SOUL + history + LLM client. One bot serves N users, each user feels like they have their own personal hermes agent.

```mermaid
flowchart LR
    admin["Feishu admin / operator"]
    app["One Feishu app + one bot\nshared APP_ID / APP_SECRET"]
    contact["Feishu Contact v3\ndepartments + users"]
    sync["pull-feishu org sync\nsnapshot + profiles + routes"]
    table["SQLite multitenancy_routing\nopen_id / union_id -> profile"]
    userA["Feishu user A\nopen_id ou_*"]
    userB["Feishu user B\nopen_id ou_*"]
    unknown["Unknown sender\nnot in sync result"]
    gateway["Hermes gateway\nsingle websocket"]
    router["multitenancy router\npre_gateway_dispatch"]
    guard["tenant boundary guard\ncanonical sender / env lock / media filter / exec opt-in"]
    slash["Hermes slash control plane\nregistry / skill / plugin / quick / unknown"]
    approval["approval bridge\nsession env + stream events + decision file"]
    cron["shared cron store\n~/.hermes/cron/jobs.json"]
    profileA["profile: ee966643\ncanonical Feishu user_id"]
    profileB["profile: g41a5b5g\ncanonical Feishu user_id"]
    fallback["fallback profile: feishu_ou_xxx\nauto-provision only"]
    aiagent["AIAgent subprocess\nprofile HERMES_HOME + shared-return bridges"]
    feishu["Feishu CardKit / IM\ntext, cards, files"]

    admin --> app
    admin --> contact --> sync --> table
    sync --> profileA
    sync --> profileB
    userA --> app
    userB --> app
    unknown --> app
    app --> gateway --> router --> guard
    guard -->|slash command| slash
    slash -->|gateway handler / quick alias / plugin / unknown| feishu
    slash -->|quick exec only when explicitly enabled| aiagent
    slash -->|skill invocation| aiagent
    guard --> table
    table -->|active route| profileA --> aiagent --> feishu
    table -->|active route| profileB --> aiagent
    guard -->|route miss + auto-provision| fallback --> aiagent
    aiagent -->|MEDIA path must stay in profile home| feishu
    aiagent -->|cronjob create| cron -->|gateway ticker delivers| feishu
    aiagent -->|dangerous command approval_required/resolved| approval -->|router prompt + decision file| feishu
    feishu -->|/approve / /deny| router --> approval
```

**Hermes-agent: 0 lines changed.** Verified by `git status`.

---

## 🧭 Implementation map for agents

If you are an agent taking over this repo, this is the main contract:

1. **Entry point, no Hermes core patches.** `hermes_multitenancy.register(ctx)` registers a `pre_gateway_dispatch` hook. For Feishu messages the hook returns `{"action": "skip"}` and the plugin's `handle_async()` owns routing and replies.
2. **Identity uses the canonical sender.** `_resolve_sender_for_routing()` prefers the real Feishu `open_id` (`ou_*`) from the Feishu contextvar, `event.sender_open_id`, `source.open_id/user_id`, and `raw/raw_event/event`. `user_id_alt` / `union_id` is only a legacy route lookup helper, not the new session key.
3. **Routes live in SQLite.** `multitenancy_routing.open_id -> profile_name` decides which `~/.hermes/profiles/<profile>/` handles the turn. A real `ou_*` will not be absorbed by a stale `union_id`; legacy alt routes are used only when no real `ou_*` is available.
4. **Normal messages run inside the routed profile.** The router builds a profile-scoped event, writes the resolved `sender_open_id` back to the event, then dispatches to the streaming AIAgent subprocess. The child runs with that profile's `HERMES_HOME`; `agent_real._build_subprocess_env` strips the parent gateway's environment down to an explicit allowlist and pivots `HOME`/`XDG_*`/`TMPDIR` into `<profile>/{home,cache,config,state,data,tmp}` so skill caches stay tenant-scoped. Feishu UAT tokens are loaded from `<profile>/feishu_uat/<open_id>.json` (rebound at runtime by `_configure_feishu_uat_home`); the org-sync pass migrates the legacy shared `<hermes_home>/feishu_uat/<ou_*>.json` forward. See `docs/profile-isolation.md`.
5. **Cron/reminder jobs use the shared gateway scheduler store.** Inside the AIAgent subprocess, `agent_real._configure_cron_home()` temporarily binds `cron.jobs` and `tools.cronjob_tools` to the shared Hermes home (`~/.hermes/cron/jobs.json`), not `~/.hermes/profiles/<profile>/cron/jobs.json`. This matters because the single gateway cron ticker only watches the shared store; otherwise "remind me in 3 minutes" jobs can stay forever `scheduled`.
6. **Dangerous-command approvals cross the subprocess boundary.** The profile AIAgent registers `tools.approval` with a router-compatible gateway session key (`multitenancy:<platform>:<profile>:<chat>:<sender>`). The child also sets child-local `HERMES_SESSION_KEY` / `HERMES_GATEWAY_SESSION` / `HERMES_EXEC_ASK`, because terminal/process guards may run in worker threads that do not inherit contextvars. The child emits `approval_required` / `approval_resolved`; the parent `_stream_aiagent_subprocess()` must forward those events to the router; the router prompts Feishu; `/approve` / `/deny` writes a decision file that releases the child and resumes Hermes' native approval flow. The matching Hermes core terminal guard must run before sandbox/environment creation, otherwise the approval prompt can be blocked by environment startup.
7. **CardKit heartbeat lives in the parent router.** The router primes the card and sends idle heartbeat status updates before the child emits tokens; the heartbeat stops once reasoning/tool/content events arrive.
8. **Memory is keyed by `(profile, canonical sender)`.** `_history_key()` does not use `sender_alt or sender`, so stale/shared alternate IDs cannot merge two users' memory.
9. **Slash commands never leak into the LLM.** `/model`, `/reasoning`, `/reload-mcp` and other registry commands use Hermes gateway handlers; skill slash rewrites into native skill invocation for the routed profile; plugin slash delegates to `hermes_cli.plugins.get_plugin_command_handler`; quick alias/exec follows config; unknown slash returns Hermes-style unknown-command.
10. **Slash handlers run with a profile context lock.** Gateway/plugin handlers execute inside `_profile_gateway_context()`, which serializes temporary `HERMES_HOME` and gateway session-key overrides so concurrent slash commands cannot cross profiles.
11. **Local exec is off by default.** `quick_commands` alias remains available; `type: exec` is denied unless `multitenancy.allow_quick_exec: true` or `HERMES_MULTITENANCY_ALLOW_QUICK_EXEC=1` is set. Allowed exec inherits the routed profile's `HERMES_HOME`. Keep it off in production until profile sandboxing is enforced.
12. **Attachments and file replies stay in profile scope.** Inbound attachments still delegate to Hermes' native `_prepare_inbound_message_text`; the plugin adds a bounded fallback for locally cached tabular files (`.csv` / `.xlsx`) when upstream does not inline them. Outbound `MEDIA:<path>` replies are filtered so only paths resolving inside the routed `profile_home` are delivered. Tool/browser artifacts discovered in profile-local download/cache/tmp/data directories are published into `profile_home/workspace/Downloads` first so Feishu delivery and the WebUI file browser consume the same profile-visible location.
13. **Background terminal notify is not claimed as supported.** A child-local `process_registry` is invisible to the parent gateway watcher. Each child calls `agent.close()` on exit to clean such resources instead of leaving unmanaged background processes. True `terminal(background=true, notify_on_complete=true)` support should move process ownership into the parent process.
14. **Production posture.** For company deployments, prefer `HERMES_MULTITENANCY_AUTO_PROVISION=0` and keep `multitenancy.allow_quick_exec=false`. Application-layer isolation (route/session/slash/media boundaries) is handled by this plugin. Profile execution-environment sandboxing档 A — parent-env allowlist, HOME/XDG/TMPDIR pivot, `chmod 0700` on the profile tree, per-profile `feishu_uat/` and `tokens/` directories — is enabled by default; verify a deployed profile with `scripts/verify-isolation.sh`. Kernel-level containment via `sandbox-exec` (档 B) is on the roadmap; until it lands, treat档 A as defense-in-depth rather than an authorisation boundary. Full details: `docs/profile-isolation.md`.

---

## 👥 Roles

| Role | Owns |
|---|---|
| Feishu admin | Creates or reuses one internal Feishu app, enables the bot/websocket/scopes, and keeps the shared app credential out of git. Production stores it in `multitenancy_credentials` as the global Feishu app row. |
| Platform operator | Installs hermes + this plugin, keeps the gateway running, manages routing rows and profile directories. |
| End user | Authorizes once through the Feishu auth/UAT flow, then talks to the same bot. User tokens refresh offline from the shared Hermes home. |
| Agent profile owner | Maintains each profile's `SOUL.md`, `config.yaml`, `.env`, tool policy, session DB, and model credentials. |

## 🔁 App ID reuse model

You do **not** need one Feishu app per user. Reuse one Feishu app/bot for all tenants:

1. Store the shared Feishu app credential in the multitenancy credential vault (`profile_name=__global__`, `subject_id=feishu_app`, `provider=feishu`, `secret_kind=app`). Env/default Hermes config is only a migration or fallback source.
2. Route by the real Feishu sender `open_id` (`ou_*`). The router can fall back to `union_id` (`on_*`) for migration/legacy rows, but new users should be keyed by `ou_*`.
3. Per-user Feishu UAT tokens are first captured by OAuth into `~/.hermes/feishu_uat/<open_id>.json` (gateway-side, shared), then migrated to `~/.hermes/profiles/<profile>/feishu_uat/<open_id>.json` by the org-sync pass — that per-profile copy is what the AIAgent subprocess actually reads. Do not commit token files. See `docs/profile-isolation.md` §6.
4. Keep per-profile model/tool credentials under `~/.hermes/profiles/<profile>/`. The Feishu app is shared; profile persona, memory, tools and LLM credentials stay isolated.

Discussion group: [Eggyrooch's Feishu group invite](https://applink.feishu.cn/client/chat/chatter/add_by_link?link_token=419if828-a007-453f-ad1c-31edef49520f).

---

## 🚀 Quick Start

### 1. Install the plugin

Use Hermes' plugin installer for normal installs. It clones this repository into
`~/.hermes/plugins/multitenancy`, reads the root `plugin.yaml`, and adds
`multitenancy` to `plugins.enabled` when `--enable` is supplied.

```bash
hermes plugins install eggyrooch-blip/hermes-multitenancy --enable
hermes plugins list
hermes gateway restart
```

For local development, use an editable checkout:

```bash
git clone https://github.com/eggyrooch-blip/hermes-multitenancy ~/projects/hermes-multitenancy
cd ~/projects/hermes-multitenancy
hermes plugins install "file://$PWD" --force --enable
python -m pip install --no-deps -e ".[test]"   # optional: only for running this repo's tests
hermes gateway restart
```

### 2. Enable in `config.yaml`

The installer handles this when you pass `--enable`. For manual installs,
ensure the default gateway home contains:

```yaml
# ~/.hermes/config.yaml
plugins:
  enabled:
    - multitenancy
```

### 3. Configure one shared Feishu bot

Use your existing Hermes Feishu app credentials as a migration source, then store them in the multitenancy credential vault. The exact surrounding Hermes config can vary by version; the important part is that all profiles reuse one shared app credential while user UAT stays profile-scoped.

```yaml
# ~/.hermes/config.yaml
platforms:
  feishu:
    enabled: true
    extra:
      app_id: "${FEISHU_APP_ID}"
      app_secret: "${FEISHU_APP_SECRET}"
```

Then run your Hermes Feishu authorization/UAT flow for each real user. Token files initially land under the shared home (OAuth callback writes there):

```text
~/.hermes/feishu_uat/ou_xxx.json
~/.hermes/feishu_uat/ou_yyy.json
```

The next org-sync pass copies each user's token forward into their profile (`~/.hermes/profiles/<profile>/feishu_uat/<open_id>.json`), which is the path the AIAgent subprocess actually reads from after档 A profile isolation. See `docs/profile-isolation.md` §6.

### 4. Sync Feishu org into profiles + routes

If your Feishu app has Contact read scopes, use the Python org sync:

```bash
# Preview only; does not write profiles or DB rows
python ~/.hermes/plugins/multitenancy/sync.py pull-feishu --dry-run

# Apply and save an org snapshot
mkdir -p ~/.hermes/org-snapshots
python ~/.hermes/plugins/multitenancy/sync.py pull-feishu \
  --snapshot-out ~/.hermes/org-snapshots
```

The sync command reuses the current `HERMES_HOME` Feishu config (`config.yaml` / `.env` / environment), pulls Feishu Contact v3 departments and users, creates `~/.hermes/profiles/<user_id>/` from Feishu `user_id`, and writes `multitenancy_routing`. It only updates a managed org block inside `SOUL.md`; manually edited content outside that block is preserved.

If you do not have Contact scopes yet, or want to run a strict manual allowlist, keep using the JSON route reconciler:

```bash
# Directory-plugin install path
python ~/.hermes/plugins/multitenancy/sync.py apply users.json

# Editable/pip install path, if you installed this repo as a package too
hermes-multitenancy-sync apply users.json
```

Where `users.json` is:

```json
[
  {"user_id": "alice", "profile_name": "alice_profile", "open_id": "ou_xxx", "union_id": "on_xxx"},
  {"user_id": "bob",   "profile_name": "bob_profile",   "open_id": "ou_yyy", "union_id": "on_yyy"}
]
```

Each `profile_name` should already exist as a hermes profile directory at `~/.hermes/profiles/<name>/` with its own `SOUL.md`, `config.yaml`, `auth.json` or `.env`. The plugin will route Feishu messages from `ou_xxx` to `alice_profile`'s SOUL+memory, and from `ou_yyy` to `bob_profile`.

For first-run UAT you can also leave auto-provision enabled (`HERMES_MULTITENANCY_AUTO_PROVISION=1`, the default). An unseen sender `ou_new_user` gets a deterministic fallback profile such as `~/.hermes/profiles/feishu_ou_new_user/`, seeded from the shared Hermes config. Once org sync learns that user's canonical Feishu `user_id`, it takes over the route to the `user_id` profile.

Restart the hermes gateway. **Done.**

### 5. Verify

```bash
hermes plugins list
hermes gateway status
sqlite3 ~/.hermes/multitenancy.db 'select open_id, profile_name, active from multitenancy_routing;'
```

Send two different Feishu users the same prompt through the same bot. The
gateway log should show different sender `ou_*` values and different profile
homes.

### 6. Automate, scope, and recover

After the first sync, run a periodic full sync through cron or a systemd timer. A full sync handles join/move/leave: new employees get profiles + routes, department or manager changes refresh only the managed org block in `SOUL.md`, and users missing from the full Contact result are soft-deleted from routing (`active=0`) while their profiles, memories, and sessions remain on disk.

```cron
*/30 * * * * HERMES_HOME=/Users/kite/.hermes /usr/bin/python3 /Users/kite/.hermes/plugins/multitenancy/sync.py pull-feishu --snapshot-out /Users/kite/.hermes/org-snapshots >> /Users/kite/.hermes/logs/multitenancy-sync.log 2>&1
```

When only some people need org sync, use a department-scoped sync or a manual allowlist:

```bash
# Sync one department subtree; out-of-scope routes are not soft-deleted by default
python ~/.hermes/plugins/multitenancy/sync.py pull-feishu --dept <open_department_id> --dry-run
python ~/.hermes/plugins/multitenancy/sync.py pull-feishu --dept <open_department_id>

# Strict allowlist mode: unknown users do not auto-create fallback profiles
export HERMES_MULTITENANCY_AUTO_PROVISION=0
```

If sync goes wrong, stop the timer first, then inspect `pull-feishu --dry-run` and the latest snapshot. A user can send `/status` in Feishu to see the current profile; locally, run `hermes -p <profile_name> chat` to enter that profile. Unknown-user fallback profiles live at `~/.hermes/profiles/feishu_<open_id>/`.

---

## ✅ Proof of end-to-end

This isn't a paper plugin. The current UAT chain has been run against a real Feishu bot with two independent Feishu users on the same bot:

| Step | Action | Verified result |
|---|---|---|
| 1 | User A → same bot → router | Routed by real `ou_*` open_id to the existing `coder` profile. |
| 2 | User B → same bot → router | Auto-provisioned and routed to a new `feishu_ou_xxx` profile, not the `coder` profile. |
| 3 | Both users send the same tool-heavy UAT case set | AIAgent subprocess runs with the correct profile home and sender open_id scope. |
| 4 | Replies stream back through Feishu CardKit / IM | Text cards and file-message paths are delivered through the Feishu adapter. |
| 5 | Full dual-account stress suite | Run with `--users <userA>,<userB> --parallel-users`; each case records independent `case_id::user` checkpoint entries. |
| 6 | Dynamic slash control plane | Dual-account `slash` suite passed `16/16`: `/model`, `/reasoning`, `/reload-mcp` use gateway handlers; skill slash rewrites into native skill invocation; plugin slash delegates to `hermes_cli.plugins.get_plugin_command_handler`; quick alias bypasses the LLM, and quick exec is explicit opt-in only; unknown slash returns Hermes-style unknown-command. |

These checks were run live through Feishu's WebSocket gateway and an OpenAI-compatible model provider. Real open_ids, tokens, chat IDs and app secrets are intentionally omitted from this repository.

---

## ✨ Features

| Feature | Status |
|---|---|
| Multi-tenant routing per Feishu user (open_id / union_id) | ✅ |
| LRU runtime pool (max 50 hot profiles, idle evict 5min) | ✅ |
| Streaming LLM via `edit_message` typewriter | ✅ |
| Reasoning-content split for thinking models | ✅ |
| Reactions (👀 → ✅ / ❌) via `adapter.on_processing_*` | ✅ |
| Multi-turn session memory (SQLite-backed, survives restart) | ✅ |
| Reply-context injection (quoted messages) | ✅ |
| Rate-limit retry (429 backoff, mirrors hermes mainstream cadence) | ✅ |
| Hermes slash command control plane | ✅ — dynamically recognizes Hermes registry commands; `/model`, `/reasoning`, `/reload-mcp` use gateway handlers; skill slash rewrites into native skill invocation; plugin slash delegates to `hermes_cli.plugins.get_plugin_command_handler`; quick_commands support alias and explicitly-enabled exec; unknown slash returns Hermes-style unknown-command and never leaks into the LLM |
| Idempotent feishu-sync reconciler (CLI + library) | ✅ |
| Python Feishu Contact org sync (`pull-feishu`) | ✅ — creates/updates profiles, SOUL managed blocks and route rows |
| Vision (image attachments) | ✅ — delegates to hermes' `gateway._prepare_inbound_message_text`, identical to mainstream |
| Audio STT (voice messages) | ✅ — same delegate, hermes' `transcribe_audio` runs on cached audio |
| Text-file inject (.txt / .md / .csv / .log / .json …) | ✅ — same delegate, content prepended to message |
| Reply context (quoted message) | ✅ — same delegate, plus our own `reply_to_text` fallback |
| Multi-user shared-session attribution | ✅ — same delegate |
| Tool use (real AIAgent loop with browser/search/shell) | ✅ — via isolated `AIAgent` subprocess bridge |
| Cron / reminder proactive delivery | ✅ — WebUI/broker-created jobs default to `deliver=feishu`, are stored in the routed profile cron store, and are executed/delivered by the router multi-profile worker |
| Dangerous-command approval proactive delivery | ✅ — child `approval_required`/`approval_resolved` → parent stream parser → router Feishu prompt → `/approve`/`/deny` decision file; child-local session env covers terminal worker threads; core terminal guard runs before environment creation |
| CardKit idle heartbeat | ✅ — parent router prime + heartbeat; does not depend on early child tokens |
| Background terminal `notify_on_complete` | ⚠️ not claimed — child registry is invisible to the parent gateway; child exit calls `agent.close()` to avoid orphaned background work |
| Feishu CardKit / IM file-message replies | ✅ — streaming cards plus native `MEDIA:<path>` delivery reuse, filtered to the routed profile home |

---

## 🛡️ How it stays compatible

We keep **zero patches to hermes-agent**: no edits to `feishu.py`, `gateway/run.py`, or upstream modules. The plugin loader contract (`hermes_cli/plugins.py:435 register_hook`) is the gateway entry point; the AIAgent/tool bridge also consumes a few Hermes integration surfaces and has tests around each one.

| Public API we depend on | Stability |
|---|---|
| `pre_gateway_dispatch` hook (`plugins.py:81 VALID_HOOKS`) | ⚠️ added 2026-04-21 — pin hermes-agent version |
| `BasePlatformAdapter.send / send_typing / edit_message` | ✅ abstract methods, very stable |
| `BasePlatformAdapter.on_processing_start / on_processing_complete` | ✅ |
| `MessageEvent.source.{user_id, user_id_alt, chat_id}` | ✅ stable |
| `Platform.FEISHU` enum + `ProcessingOutcome` enum | ✅ |
| `gateway.adapters[Platform.FEISHU]` dict | ✅ |
| `hermes_constants.get_hermes_home()` (read via env) | ✅ |
| `hermes_cli.commands.resolve_command / is_gateway_known_command` | ✅ — slash command recognition comes from Hermes' central registry, with a tiny fallback for tests outside Hermes |
| `SendResult.{success, message_id}` | ✅ |
| `gateway._prepare_inbound_message_text(event, source, history)` | ⚠️ private (leading underscore) — covers vision + STT + file inject + reply context in one call. Falls back to local vision-only on signature change. |
| `gateway.stream_consumer.GatewayStreamConsumer` | ⚠️ Hermes integration surface — reused for Feishu CardKit streaming when present, with text-edit fallback. |
| `gateway._deliver_media_from_response(response, event, adapter)` | ⚠️ private — reused so `MEDIA:<path>` file replies follow the native Feishu path after filtering paths to the routed profile home. No-op if unavailable. |
| `run_agent.AIAgent` | ⚠️ core runtime class — isolated in `aiagent_subprocess.py` so failures fall back to the legacy OpenAI-compatible path. |
| `tools.feishu_oapi_client.sender_open_id_scope` | ⚠️ Feishu UAT bridge — scopes token lookup to `<FEISHU_UAT_DIR>/<open_id>.json`. `agent_real._configure_feishu_uat_home` rebinds `FEISHU_UAT_DIR` to `<profile>/feishu_uat/` per subprocess so a tenant only sees its own UAT (档 A isolation). |
| `tools.vision_tools.vision_analyze_tool` (local fallback) | ✅ tool module, used only when the gateway helper is missing |

**Pin your `hermes-agent` version** (`hermes-agent==X.Y.Z`) and run `pytest tests/test_router_integration.py tests/test_vision.py` after each upgrade — the integration + pipeline tests will fail loudly on any contract drift.

---

## 🧭 Upstream strategy

This repository is intended to stay a third-party Hermes plugin, not a fork.
That keeps rollout fast and avoids adding Feishu-multitenancy-specific policy
to Hermes core. A good upstream PR to `NousResearch/hermes-agent` would be
small and generic, for example:

| Upstream candidate | Why |
|---|---|
| Expose real Feishu sender `open_id` directly on `MessageEvent.source` | Removes raw-event parsing from this plugin and helps any Feishu plugin. |
| Document `pre_gateway_dispatch` gateway hooks and deferred processing lifecycle | Makes router-style plugins easier to build safely. |
| Stabilize CardKit streaming/media delivery extension points | Lets plugins reuse native Feishu UX without touching private gateway helpers. |

The full multitenancy router should only be proposed as a bundled Hermes plugin
after those generic surfaces settle and external usage proves the behavior.

---

## 🏗️ Architecture

```
~/.hermes/plugins/multitenancy/  (installed by `hermes plugins install`)
  ├─ plugin.yaml          Hermes directory-plugin manifest
  ├─ after-install.md     post-install checklist rendered by Hermes
  ├─ __init__.py          root shim → hermes_multitenancy.register(ctx)
  ├─ sync.py              route-sync wrapper for directory-plugin installs
  └─ hermes_multitenancy/
     ├─ __init__.py       register(ctx) → ctx.register_hook(pre_gateway_dispatch, ...)
     ├─ router.py         sync hook + async dispatch + commands + lazy singletons
     ├─ runtime.py        ProfileRuntime + contextvars-isolated HERMES_HOME switch
     ├─ pool.py           LRU RuntimePool (50 hot / 5min idle / cold-start sem)
     ├─ routing.py        SQLite multitenancy_routing table (open_id → profile)
     ├─ sessions.py       SQLite multitenancy_sessions (per-user history, persistent)
     ├─ commands.py       Hermes registry-backed slash command parser
     ├─ agent_real.py     AIAgent subprocess bridge + legacy OpenAI-compat fallback
     ├─ aiagent_subprocess.py isolated child-process entry point for AIAgent/tool loop
     └─ sync/
        ├─ feishu_hr.py   apply_users (idempotent reconciler)
        ├─ feishu_org.py  Feishu Contact v3 pull + profile/SOUL/route sync
        └─ cli.py         shared implementation for route sync
```

State lives in `~/.hermes/multitenancy.db` — a separate SQLite file from hermes' own `state.db` so writes don't contend. WAL mode is enabled.

---

## ⚙️ Configuration knobs

| `config.yaml` key | Default | Notes |
|---|---|---|
| `plugins.enabled` | (none) | Must include `multitenancy` |
| `model.default` | (your hermes default) | Per-profile model selection; use whatever provider/model your Hermes deployment already standardizes on. |
| `model.fallback` | (your hermes default) | Used by `agent_real` if primary fails |
| `multitenancy.toolsets_mode` | `merge_default` | When a profile sets `platform_toolsets.feishu`, merge it with Hermes' default Feishu toolsets so web/browser/search remain available. Set `explicit` for strict replacement. |
| `multitenancy.allow_quick_exec` | `false` | Allows `quick_commands` entries with `type: exec` over Feishu multitenancy. Keep off until the profile sandbox is enforced; allowed exec inherits the routed profile's `HERMES_HOME`. |

| Sync command / env var | Default | Notes |
|---|---|---|
| `pull-feishu --dry-run` | off | Preview planned changes without writing profiles or DB rows |
| `pull-feishu --dept <id>` | off | Sync one Feishu department subtree; out-of-scope routes are not soft-deleted by default |
| `pull-feishu --soft-delete-missing` | on for full sync, off for `--dept` | Soft-delete active routes missing from the current pull |
| `pull-feishu --no-soft-delete-missing` | off | Force missing routes to stay active, useful for pilots and incident recovery |
| `HERMES_MULTITENANCY_AUTO_PROVISION` | `1` | Auto-create `feishu_<open_id>` fallback profiles for unknown Feishu senders; set `0` for strict allowlist mode |
| `HERMES_MULTITENANCY_ALLOW_QUICK_EXEC` | unset / off | Environment override for `quick_commands` exec. Prefer config-level allowlisting plus sandboxing in production. |

| Plugin tunable (Python constants in `router.py`) | Default | Notes |
|---|---|---|
| `RuntimePool.max_loaded_runtimes` | 50 | Hot pool cap |
| `RuntimePool.idle_evict_seconds`  | 300 | Drop idle entries after 5min |
| `_SESSION_HISTORY_MAX`            | 20  | Messages kept per (profile, user) |
| Streaming throttle (content)      | 1.0s / 60 chars | Mirrors hermes mainstream cadence |
| Streaming throttle (thinking)     | 2.0s heartbeat | Reasoning preview |
| CardKit idle heartbeat            | 2.5s | Keeps the card active before the first agent event |
| Approval bridge timeout           | 300s | Override with `HERMES_MULTITENANCY_APPROVAL_TIMEOUT` |
| Rate-limit backoffs               | 0.5s → 1s → 2s | 429-only; non-429 retried once |

---

## 🎮 Slash commands

| Command | Effect |
|---|---|
| `/help`   | List available commands |
| `/status` | Show current profile + history length + run state |
| `/new` / `/reset` | Reset this user's session history (per profile) — clears both cache + SQLite |
| `/stop`   | Cancel the in-flight LLM call for this user |
| Other Hermes gateway commands | Recognized dynamically from Hermes' registry and delegated to the gateway handler when available; otherwise they return a control-plane warning instead of entering the agent prompt. |

---

## 🧪 Testing

```bash
# Default suite (no network)
PYTHONPATH=/path/to/hermes-agent python -m pytest tests/ -q -m "not integration"

# Focused Feishu multitenancy regression suite used for this PR
PYTHONPATH=/path/to/hermes-agent python -m pytest \
  tests/test_hook_dispatch.py \
  tests/test_aiagent_subprocess.py \
  tests/test_streaming_card_transport.py \
  -q

# Live LLM integration — calls your configured provider
PYTHONPATH=. python -m pytest tests/ -m integration -v
```

Full dual-account Feishu UAT lives in the Hermes UAT worktree, not this plugin
package:

```bash
python scripts/stress_test_feishu_pipeline.py \
  --suite full \
  --users UserA,UserB \
  --parallel-users \
  --chat-id "$HERMES_FEISHU_TEST_CHAT_ID" \
  --fixtures .uat/fixtures/dual-users.local.json \
  --allow-destructive \
  --strict-identity \
  --route-mode multitenant \
  --require-card-final \
  --checkpoint ~/.hermes/uat/checkpoints/full-dual-20260505.jsonl
```

---

## 🐛 Troubleshooting

**"plugin loaded but no replies"** — `pkill -f gateway && hermes gateway run`. Plugins are loaded at gateway startup, so any change requires a restart.

**"all bots stopped responding"** — your routing rule probably has the wrong `open_id` or `union_id`. Check the actual values that arrive from Feishu by adding a temporary `print(event.source)` in `router.on_pre_gateway_dispatch` and watching the gateway log.

**"org sync routed someone incorrectly"** — stop cron/systemd first, then run `pull-feishu --dry-run` to inspect planned changes. The user can send `/status` in Feishu to see the current profile; locally inspect `sqlite3 ~/.hermes/multitenancy.db 'select user_id, open_id, profile_name, active from multitenancy_routing;'`.

**"I need to bypass a bad route immediately"** — soft-delete the active route and let the next message auto-provision a fallback profile:

```bash
sqlite3 ~/.hermes/multitenancy.db \
  "update multitenancy_routing set active=0, deleted_at=strftime('%s','now'), updated_at=strftime('%s','now'), version=version+1 where open_id='ou_xxx' and active=1;"
```

The fallback profile is `~/.hermes/profiles/feishu_ou_xxx/`; open it with `hermes -p feishu_ou_xxx chat`.

**"user_id is `g41a5b5g`-ish, not the `ou_` I expected"** — some Feishu/Hermes paths expose a short SDK user ID on `event.source.user_id`. This plugin now resolves the real sender `open_id` from Feishu raw sender metadata/context first, and only falls back to `user_id_alt` / `union_id` for legacy rows.

**"Feishu tools work, but news/web search does not"** — the profile likely has `platform_toolsets.feishu` set to Feishu-only tools. The default `merge_default` mode now merges explicit Feishu entries with Hermes' default Feishu toolsets, preserving `web_search` / `web_extract`. If you intentionally need a small schema, set `multitenancy.toolsets_mode: explicit` or `HERMES_MULTITENANCY_TOOLSETS_MODE=explicit`.

**"feels slow, 1s per character"** — check the gateway log for model latency, Feishu rate-limit retries, and CardKit update throttling. Reasoning-capable models may stream `reasoning_content` before final text; the plugin surfaces that as progress instead of hiding it.

**"sessions lost after restart"** — verify `~/.hermes/multitenancy.db` exists and the `multitenancy_sessions` table has rows. If it's empty, check for write errors in the gateway log (`logger.debug "SessionStore.append failed"`).

---

## 🤝 Contributing

Issues and PRs welcome.

### Bug reports

When filing a bug, please include:

1. Output of `pytest tests/ -q` from your machine
2. Hermes-agent version (`pip show hermes-agent | grep Version`)
3. Plugin version (`pip show hermes-multitenancy | grep Version`)
4. Relevant gateway log lines (especially anything with `multitenancy:` prefix)

### Pull requests

1. Fork → create a branch → run `pytest tests/ -q` (must be green) → open PR.
2. **Tests are required** for behaviour changes. We hold a hard line on `pytest tests/ -q -m "not integration"` staying at 128+ green.
3. **Don't mass-rename** — keep diffs small and reviewable.
4. **No `feishu.py` patches** — the whole point of this plugin is hermes-agent stays unmodified. If you find a hermes API limitation, file an upstream issue at https://github.com/NousResearch/hermes-agent and link it here.

### Helping with hermes-agent compatibility

If you upgrade `hermes-agent` and our integration tests break, please file an issue with:
- The hermes-agent version that broke us
- The pytest output
- A pointer to the upstream commit (if you can find it)

We pin `hermes-agent>=1.0` in `pyproject.toml` but the plugin loader contract evolves — we need community eyes on what changes.

### Wanted contributions (priority order)

1. **Per-profile `SessionStore`** — session rows are isolated by `(profile, canonical sender)` in the shared `multitenancy.db`; splitting into per-profile DBs is still useful scale hardening and mirrors hermes' own profile layout.
2. **Prompt caching** — Anthropic `cache_control` for the SOUL prefix. Cuts token cost ~50% on long-running chats.
3. **CI matrix** — GitHub Actions running `pytest tests/ -q` against multiple `hermes-agent` versions to catch upstream contract drift early.
4. **More live UAT fixtures** — broaden destructive/write-path coverage without relying on shared production-like resources.

---

## 📜 License

MIT — see [LICENSE](LICENSE).

## 🙏 Acknowledgements

Built on top of [Nous Research's hermes-agent](https://github.com/NousResearch/hermes-agent) — without the `pre_gateway_dispatch` hook (added by [@KeiraVoss](https://github.com/) on 2026-04-21), this plugin would have required forking the entire upstream. Thank you for the hook.
