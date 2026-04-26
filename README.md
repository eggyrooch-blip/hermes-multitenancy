# hermes-multitenancy

> **One Feishu bot, N users, N profiles.** A [hermes-agent](https://github.com/NousResearch/hermes-agent) plugin that routes each Feishu user to their own profile (independent SOUL.md, sessions, memories, LLM credentials) — without modifying a single line of hermes-agent.

[![tests](https://img.shields.io/badge/tests-66%20passing-brightgreen)](#testing)
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

```
飞书 user A ─┐
飞书 user B ─┼─► 1 个 Bot ─► hermes gateway ─► [pre_gateway_dispatch hook]
飞书 user C ─┘                                   │
                                                ├─► profile_a/SOUL.md + 独立 sessions + 独立 LLM
                                                ├─► profile_b/SOUL.md + 独立 sessions + 独立 LLM
                                                └─► profile_c/SOUL.md + 独立 sessions + 独立 LLM
```

**Hermes-agent: 0 lines changed.** Verified by `git status`.

---

## 🚀 Quick Start

### 1. Install the plugin

```bash
git clone https://github.com/eggyrooch-blip/hermes-multitenancy ~/projects/hermes-multitenancy

# Symlink into your hermes user-plugin directory
mkdir -p ~/.hermes/plugins   # for default profile
ln -s ~/projects/hermes-multitenancy/hermes_multitenancy ~/.hermes/plugins/multitenancy

# (For named profiles, repeat under ~/.hermes/profiles/<name>/plugins/)
```

### 2. Enable in `config.yaml`

```yaml
# ~/.hermes/config.yaml — or per-profile config
plugins:
  enabled:
    - multitenancy
```

### 3. Add routing rules

```bash
# Use the bundled CLI
python -m hermes_multitenancy.sync apply users.json
```

Where `users.json` is:

```json
[
  {"user_id": "alice", "profile_name": "alice_profile", "open_id": "ou_xxx", "union_id": "on_xxx"},
  {"user_id": "bob",   "profile_name": "bob_profile",   "open_id": "ou_yyy", "union_id": "on_yyy"}
]
```

Each `profile_name` should already exist as a hermes profile directory at `~/.hermes/profiles/<name>/` with its own `SOUL.md`, `config.yaml`, `auth.json`. The plugin will route Feishu messages from `alice`'s union_id to `alice_profile`'s SOUL+memory, and from `bob`'s union_id to `bob_profile`'s.

Restart the hermes gateway. **Done.**

---

## ✅ Proof of end-to-end

This isn't a paper plugin. The current author runs it on his actual default Feishu bot with two test profiles:

| Step | Action | Verified result |
|---|---|---|
| 1 | User A sends `hi` | Bot replies `[SPIKE-TEST] hi! ...` (routed to spike_test profile) |
| 2 | User B sends `hi` | Bot replies `[ALICE-TENANT] 你好！...` (routed to spike_alice, different SOUL, Chinese) |
| 3 | User A: `I like apples` then `what did I just say I like?` | Bot answers `apples` (multi-turn memory works) |
| 4 | Restart gateway. User A: `what did I say I liked earlier?` | Bot answers `apples` (SQLite persistence survives restart) |
| 5 | `/new` then `tell me what I like` | Bot answers "I don't know" (history was actually wiped, both cache + DB) |

These have all been run live against `https://api.z.ai` (GLM 5.1) and Feishu's WebSocket gateway.

---

## ✨ Features

| Feature | Status |
|---|---|
| Multi-tenant routing per Feishu user (open_id / union_id) | ✅ |
| LRU runtime pool (max 50 hot profiles, idle evict 5min) | ✅ |
| Streaming LLM via `edit_message` typewriter | ✅ |
| Reasoning-content split (GLM 5.x thinking models) | ✅ |
| Reactions (👀 → ✅ / ❌) via `adapter.on_processing_*` | ✅ |
| Multi-turn session memory (SQLite-backed, survives restart) | ✅ |
| Reply-context injection (quoted messages) | ✅ |
| Rate-limit retry (429 backoff, mirrors hermes mainstream cadence) | ✅ |
| Slash commands (`/help` `/status` `/stop` `/new` `/reset`) | ✅ |
| Idempotent feishu-sync reconciler (CLI + library) | ✅ |
| Vision (image attachments) | ✅ — wraps hermes' `tools.vision_tools.vision_analyze_tool`, identical UX to mainstream |
| Tool use (real AIAgent loop with browser/search/shell) | 🚧 — design hooks ready, swap to hermes' `AIAgent` (`run_agent.py:809`) is opt-in for Phase 5 |

---

## 🐢 Slow? Try Haiku instead of GLM 5.1

GLM 5.1 is a *reasoning* model — it spends 5-15 seconds in `reasoning_content` before emitting `content`. That makes the bot **feel** sluggish even when the plugin is doing the right thing. Two ways to speed it up:

**Option 1 — Switch to a non-reasoning model.** In your spike profile's `config.yaml`:

```yaml
model:
  default: "openrouter/anthropic/claude-3.5-haiku"
fallback:
  - "zai/glm-5.1"
```

Set `OPENROUTER_API_KEY` in your profile's `.env`. Haiku is 5-10× faster end-to-end.

**Option 2 — Keep GLM but accept the typewriter behaviour.** The plugin shows a `💭 思考中…` placeholder during reasoning so users see *something*; it's not frozen.

---

## 🛡️ How it stays compatible

We use **only public hermes-agent APIs** — zero patches to `feishu.py`, `gateway/run.py`, or any internal module. The plugin loader contract (`hermes_cli/plugins.py:435 register_hook`) is the only entry point.

| Public API we depend on | Stability |
|---|---|
| `pre_gateway_dispatch` hook (`plugins.py:81 VALID_HOOKS`) | ⚠️ added 2026-04-21 — pin hermes-agent version |
| `BasePlatformAdapter.send / send_typing / edit_message` | ✅ abstract methods, very stable |
| `BasePlatformAdapter.on_processing_start / on_processing_complete` | ✅ |
| `MessageEvent.source.{user_id, user_id_alt, chat_id}` | ✅ stable |
| `Platform.FEISHU` enum + `ProcessingOutcome` enum | ✅ |
| `gateway.adapters[Platform.FEISHU]` dict | ✅ |
| `hermes_constants.get_hermes_home()` (read via env) | ✅ |
| `SendResult.{success, message_id}` | ✅ |

**Pin your `hermes-agent` version** (`hermes-agent==X.Y.Z`) and run `pytest tests/test_router_integration.py` after each upgrade — the integration tests will fail loudly on any contract drift.

---

## 🏗️ Architecture

```
~/.hermes/plugins/multitenancy/  (symlink to this repo)
  ├─ __init__.py          register(ctx) → ctx.register_hook(pre_gateway_dispatch, ...)
  ├─ router.py            sync hook + async dispatch + commands + lazy singletons
  ├─ runtime.py           ProfileRuntime + contextvars-isolated HERMES_HOME switch
  ├─ pool.py              LRU RuntimePool (50 hot / 5min idle / cold-start sem)
  ├─ routing.py           SQLite multitenancy_routing table (open_id → profile)
  ├─ sessions.py          SQLite multitenancy_sessions (per-user history, persistent)
  ├─ commands.py          parse_command (/help /status /stop /new /reset)
  ├─ agent_real.py        OpenAI-compat thin LLM client (streaming + reasoning split)
  └─ sync/
     ├─ feishu_hr.py      apply_users (idempotent reconciler)
     └─ cli.py            python -m hermes_multitenancy.sync apply users.json
```

State lives in `~/.hermes/multitenancy.db` — a separate SQLite file from hermes' own `state.db` so writes don't contend. WAL mode is enabled.

---

## ⚙️ Configuration knobs

| `config.yaml` key | Default | Notes |
|---|---|---|
| `plugins.enabled` | (none) | Must include `multitenancy` |
| `model.default` | (your hermes default) | Per-profile, e.g. `zai/glm-5.1` or `openrouter/anthropic/claude-3.5-haiku` |
| `model.fallback` | (your hermes default) | Used by `agent_real` if primary fails |

| Plugin tunable (Python constants in `router.py`) | Default | Notes |
|---|---|---|
| `RuntimePool.max_loaded_runtimes` | 50 | Hot pool cap |
| `RuntimePool.idle_evict_seconds`  | 300 | Drop idle entries after 5min |
| `_SESSION_HISTORY_MAX`            | 20  | Messages kept per (profile, user) |
| Streaming throttle (content)      | 1.0s / 60 chars | Mirrors hermes mainstream cadence |
| Streaming throttle (thinking)     | 2.0s heartbeat | Reasoning preview |
| Rate-limit backoffs               | 0.5s → 1s → 2s | 429-only; non-429 retried once |

---

## 🎮 Slash commands

| Command | Effect |
|---|---|
| `/help`   | List available commands |
| `/status` | Show current profile + history length + run state |
| `/new` / `/reset` | Reset this user's session history (per profile) — clears both cache + SQLite |
| `/stop`   | Cancel the in-flight LLM call for this user |

---

## 🧪 Testing

```bash
# Default suite (no network) — 66 tests
PYTHONPATH=. python -m pytest tests/ -q

# Live LLM integration — calls real GLM 5.1 (or your configured provider)
PYTHONPATH=. python -m pytest tests/ -m integration -v
```

---

## 🐛 Troubleshooting

**"plugin loaded but no replies"** — `pkill -f gateway && hermes gateway run`. Plugins are loaded at gateway startup, so any change requires a restart.

**"all bots stopped responding"** — your routing rule probably has the wrong `open_id` or `union_id`. Check the actual values that arrive from Feishu by adding a temporary `print(event.source)` in `router.on_pre_gateway_dispatch` and watching the gateway log.

**"user_id is `g41a5b5g`-ish, not the `ou_` I expected"** — Feishu's `event.source.user_id` is hermes' internal short ID, **not** open_id. Use `event.source.user_id_alt` (union_id) as your routing key, which is what this plugin does by default.

**"feels slow, 1s per character"** — you're probably using a reasoning model. See [Slow? Try Haiku](#-slow-try-haiku-instead-of-glm-51).

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
2. **Tests are required** for behaviour changes. We hold a hard line on `pytest tests/ -q` staying at 66+ green.
3. **Don't mass-rename** — keep diffs small and reviewable.
4. **No `feishu.py` patches** — the whole point of this plugin is hermes-agent stays unmodified. If you find a hermes API limitation, file an upstream issue at https://github.com/NousResearch/hermes-agent and link it here.

### Helping with hermes-agent compatibility

If you upgrade `hermes-agent` and our integration tests break, please file an issue with:
- The hermes-agent version that broke us
- The pytest output
- A pointer to the upstream commit (if you can find it)

We pin `hermes-agent>=1.0` in `pyproject.toml` but the plugin loader contract evolves — we need community eyes on what changes.

### Wanted contributions (priority order)

1. **Tool use** — invoke hermes' `AIAgent` class (in `run_agent.py:809`) instead of our thin `agent_real` LLM client, so the bot can use browser/search/shell tools. The `AIAgent.__init__` signature has 50+ kwargs; the integration needs careful per-profile session_db wiring + callback bridging into our streaming loop. ~200-500 lines.
2. **Per-profile `SessionStore`** — currently all session rows live in one shared `multitenancy.db`. For true 1000-user scale they should split into per-profile DBs (mirrors hermes' own profile isolation).
3. **Prompt caching** — Anthropic `cache_control` for the SOUL prefix. Cuts token cost ~50% on long-running chats.
4. **CI matrix** — GitHub Actions running `pytest tests/ -q` against multiple `hermes-agent` versions to catch upstream contract drift early.
5. **More slash commands** — port hermes' `/update`, `/steer`, `/queue`, `/skill` from `gateway/run.py` into `commands.py`.

---

## 📜 License

MIT — see [LICENSE](LICENSE).

## 🙏 Acknowledgements

Built on top of [Nous Research's hermes-agent](https://github.com/NousResearch/hermes-agent) — without the `pre_gateway_dispatch` hook (added by [@KeiraVoss](https://github.com/) on 2026-04-21), this plugin would have required forking the entire upstream. Thank you for the hook.
