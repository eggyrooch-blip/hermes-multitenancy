# Profile Execution-Environment Isolation (档 A)

This document describes the per-profile sandboxing model introduced by
the `feature/profile-sandbox-isolation` branch. It is targeted at:

* operators running the hermes-multitenancy bot in production
* future contributors who add new skills or wire in additional secrets

If you are looking for the original design discussion, see
`OpenClaw/方案 — keep-skill-watch 自动化部署 anchor 2026-04-28.md` and
`OpenClaw/排障 — gatekeeper 群聊 kep-prd-analysis 全链路修复 2026-05-08.md`
in the Second Brain vault — those were the prior-art lessons that shaped
the trade-offs below.

---

## 1. What档 A protects against

| Threat | Before档 A | After档 A |
|---|---|---|
| Skill A reads skill B's cached OAuth token via `~/.cache/<other>/token` | ✅ trivially possible | ❌ blocked: `XDG_CACHE_HOME` pivoted to `<profile>/cache`, so XDG-aware caches land per-profile |
| Profile X's subprocess reads profile Y's Feishu UAT from `~/.hermes/feishu_uat/ou_*.json` | ✅ trivially possible via `os.listdir` | ❌ blocked: `FEISHU_UAT_DIR` rebound to `<profile>/feishu_uat/`; the dir only contains the user's own UAT |
| Parent gateway process leaks `OPENAI_API_KEY` (or any shell-exported secret) to a tenant's subprocess via `os.environ` | ✅ leaked by default (`env = os.environ.copy()`) | ❌ blocked: subprocess env is built from a 18-key allowlist |
| Other system users running `ls ~/.hermes/profiles/` enumerate the tenant tree | ✅ mode 0755 | ❌ blocked: mode 0700, applied on every sync |
| A skill/CLI/MCP server that writes a token to `Path.home() / ".myskill"` lands in the shared service user's home | ✅ leaks across profiles | ❌ blocked: `HOME` is pivoted to `<profile>/home`, so unmodified user-token tooling lands inside the routed profile |
| A skill writes a transient secret to `tempfile.gettempdir()` (`/var/folders/...` on macOS) where another tenant's skill can `os.listdir` it | ✅ leaks | ❌ blocked: `TMPDIR` pivoted to `<profile>/tmp` |

---

## 2. What档 A does NOT protect against (yet)

These are explicit non-goals for档 A — they require档 B (sandbox-exec)
or upstream changes:

| Threat | Status |
|---|---|
| Compromised skill calls `cat /Users/kite/.ssh/id_rsa` | open — file-system reads outside the profile are still permitted by the kernel. Closes under档 B |
| Compromised skill opens a TCP socket to `10.2.14.249:443` (internal lateral) | open — no egress controls. Closes under档 B with the `network` profile |
| OAuth callback handler (in upstream hermes-agent) writes a new UAT to `<shared>/feishu_uat/<ou>.json` instead of `<profile>/feishu_uat/<ou>.json` | open — write side still shared. Bridged by sync-pass migration (see §4). Real fix needs an upstream change to route callbacks through the multitenancy router |
| macOS Keychain access by the subprocess | open — Keychain is user-scoped (same `kite` uid); a tenant subprocess can read Keychain entries that another tenant captured. Mitigation: store secrets in `<profile>/tokens/` (see §5), not in Keychain |
| Memory isolation between concurrent subprocesses | open — process boundaries only; no cgroup / mem-limit |

---

## 3. Per-profile directory layout

Every profile is provisioned with the following tree (mode 0700 on every
directory, applied by `_sync_one_profile` on every sync pass):

```
~/.hermes/profiles/<profile_name>/
├── SOUL.md                      # persona
├── config.yaml                  # model + toolset config (per-profile)
├── auth.json                    # provider credential pool (per-profile)
├── .env                         # secrets for this profile only
├── feishu_uat/                  # this user's Feishu UAT JSON files
│   └── ou_<canonical>.json      # 0600
├── tokens/                      # skill_storage chokepoint
│   ├── google_drive.json        # 0600
│   ├── notion.json              # 0600
│   └── ...
├── home/                        # HOME
├── workspace/                   # WORKSPACE and Linux /workspace bind
├── cache/                       # XDG_CACHE_HOME
├── config/                      # XDG_CONFIG_HOME
├── state/                       # XDG_STATE_HOME
├── data/                        # XDG_DATA_HOME
├── tmp/                         # TMPDIR
├── memories/                    # hermes long-term memory
├── sessions/                    # hermes session blobs
├── skills/                      # per-profile skill copies
├── cron/                        # per-profile cronjobs
└── ...                          # logs, plans, skins
```

The isolation pivot directories (`home`, `workspace`, `cache`, `config`,
`state`, `data`, `tmp`) back the env redirect set by
`agent_real._build_subprocess_env`: `HOME`, `WORKSPACE`, `XDG_CACHE_HOME`,
`XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME` and `TMPDIR`.
They are created at provision time so the first AIAgent subprocess spawn
does not have to materialise them at the umask default (typically 0755)
before they are tightened.

The profile runtime deliberately behaves like a small per-user environment:

* `HOME=<profile>/home` catches ordinary `~/.tool/token.json`,
  `Path.home()`, npm/npx caches, OAuth dotdirs and CLI state without any
  skill changes.
* `WORKSPACE=<profile>/workspace` and, under Linux bwrap, `/workspace`
  support OpenClaw/ClawHub-style enterprise skills that already look for
  `/workspace/credentials/...`.
* `HERMES_PROFILE=<profile_name>` is the generic profile identity. Keep's
  existing CLI convention also gets `KEP_PROFILE=<profile_name>`.
* `<shared>/bin` is prepended to `PATH` so Hermes-managed shared binaries
  are installed once while token/state writes still land under the active
  profile.

---

## 4. The env allowlist

Defined in `hermes_multitenancy.agent_real._SUBPROCESS_ENV_ALLOWLIST`.
The current set:

* POSIX basics: `PATH`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `LANG`,
  `LC_ALL`, `LC_CTYPE`, `TZ`
* Python runtime: `PYTHONPATH`, `PYTHONUNBUFFERED`, `PYTHONIOENCODING`,
  `PYTHONDONTWRITEBYTECODE`
* macOS Cocoa: `__CF_USER_TEXT_ENCODING`
* SSL trust stores: `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`,
  `CURL_CA_BUNDLE`
* Hermes plumbing: `HERMES_AIAGENT_SUBPROCESS_TIMEOUT`, `HERMES_MAX_ITERATIONS`,
  `HERMES_MULTITENANCY_APPROVAL_TIMEOUT`, `HERMES_MULTITENANCY_TOOLSETS_MODE`,
  `HERMES_APPROVAL_GATEWAY_TIMEOUT`

**Adding a new env variable**: if you wire a new toggle into the
gateway-process and want the subprocess to inherit it, add it to the
allowlist *explicitly*. Do not pattern-match `HERMES_*` — that
re-introduces the silent-leak risk we just closed. (Background: the
OpenClaw 5/9 incident — `sanitizeEnvVars` is the right pattern.)

---

## 5. Token-bearing skills, MCP servers and CLIs

The default compatibility path is runtime-level, not skill-level. Unmodified
skills, MCP servers and CLI tools should work when they follow the common
conventions above: `$HOME`, XDG dirs, `$WORKSPACE`, `/workspace`, `$PATH`, or
profile/env identity.

The routed AIAgent child also installs a small skill-template bridge before
Hermes core loads skills: `{baseDir}` is expanded to the current skill root,
matching common OpenClaw/ClawHub packages such as `keep-record`. This bridge
lives in `agent_real._install_skill_runtime_compat()` and is intentionally not
a change to the skill package or to hermes-agent.

### WebUI Run Broker session boundary

WebUI requests enter the multitenancy layer through the Run Broker. The
parent process constructs a routed event whose `raw_event` contains the
server-side WebUI `session_id`. That value is part of the tenant/session
identity, not a credential. When the request is executed in an AIAgent
subprocess, `agent_real._event_to_subprocess_payload()` must preserve
`raw_event`, and `aiagent_subprocess._ReplayedEvent` must restore it before
session id resolution runs.

This keeps independent WebUI chats on the same profile from sharing the
fallback `platform:webui:chat_type:webui:user:<open_id>` conversation history.
If an older caller does not provide `raw_event`, the replayed event exposes an
empty dict and the legacy fallback behavior remains unchanged. This boundary
does not change lark-cli credentials, Feishu UAT refresh, bot/user identity
selection, or authsidecar host allowlisting.

WebUI is a request/response surface and does not have the same detached
completion return channel as Feishu. Routed WebUI AIAgent runs therefore set
Hermes session context `async_delivery=False`. Hermes tools that support
background work, such as `delegate_task(background=true)`, can then fall back to
synchronous execution and include the child result in the current response
instead of promising a later callback that WebUI cannot receive. Non-WebUI
surfaces keep `async_delivery=True`, so Feishu, cron and kanban paths continue to
use Hermes' normal asynchronous completion flow. If an older Hermes runtime does
not accept the `async_delivery` context field, multitenancy logs an explicit
warning before retrying the compatibility path.

For group-scoped credentials, keep one encrypted payload in the credential
vault and let `hermes-multitenancy-sync pull-feishu` materialize only the
compatibility file into each authorized profile:

```yaml
# <shared HERMES_HOME>/credential-materialization.yaml
credentials:
  - subject_id: kep-prd-analysis
    provider: gitlab
    secret_kind: token
    target: workspace/credentials/gitlab.token
    profile_file: lists/kep-prd-analysis.txt
    profiles: [gatekeeper]
```

The source vault row is `profile_name=__shared__`, `subject_id`/`provider`/
`secret_kind` from the entry, with payload `{"token": "..."}` by default.
`profile_file` contains one profile name per line and supports `#` comments.
Use `profiles: ["*"]` only for company-wide credentials: it expands to active
`multitenancy_routing` rows at materialization time, so a new employee
inherits the compatibility file after the next org-sync pass without adding
their profile name to a static list. Inactive routes are not targeted.
Targets must stay under `workspace/`, `home/`, or `tokens/`; writes are atomic
and mode `0600`. Because Linux bwrap binds `PROFILE_HOME/workspace` to
`/workspace`, existing skills that read `/workspace/credentials/gitlab.token`
work unchanged. Operators can run the same step directly:

```bash
hermes-multitenancy-sync materialize-credentials --dry-run
hermes-multitenancy-sync materialize-credentials
```

Entries can also declare an env name for skills that already expect a
conventional variable such as `GITLAB_TOKEN`:

```yaml
credentials:
  - subject_id: kep-prd-analysis
    provider: gitlab
    secret_kind: token
    target: workspace/credentials/gitlab.token
    env: GITLAB_TOKEN
    profiles: [gatekeeper]
```

The routed AIAgent process receives `GITLAB_TOKEN` from the vault and
multitenancy registers that name with Hermes' terminal/code env passthrough,
so shell commands can use `${GITLAB_TOKEN}` without the model reading or
printing the token. Hermes-agent output redaction also masks exact values of
secret-like env vars, so an accidental `echo "$GITLAB_TOKEN"` does not render
the raw token back to the employee. Agent file tools should not read
`workspace/credentials/` or `tokens/` directly; those paths are compatibility
inputs for subprocesses.

Company-default skills are inherited via an operator-managed runtime file, not
by committing skill payloads or tokens to this repository:

```yaml
# <shared HERMES_HOME>/profile-skill-defaults.yaml
skills:
  - Keep/keep-record
  - Keep/kep-hades-cli
  - Keep/kep-prd-analysis
  - Keep/kep-prd-review
```

Each listed path is copied from `<shared HERMES_HOME>/skills/` into the
profile's `skills/` directory during org sync and auto-provisioning. Secret
files inside the skill source, such as `.env`, `*.token`, `*.secret`,
`*.key`, and names containing `token`/`secret`/`credential`/`password`, are
never copied. The token source remains the credential vault plus the runtime
env/materialization layer above.

For newly written Hermes-native code, `hermes_multitenancy.skill_storage`
remains the explicit storage API:

```python
from hermes_multitenancy.skill_storage import write_token, read_token

# Write — atomic, mode 0600, in <profile>/tokens/google_drive.json
write_token("google_drive", json.dumps(creds))

# Read — returns None if absent
content = read_token("google_drive")
```

Skill names are validated against `[a-z0-9][a-z0-9._-]{0,62}`. Mixed
case raises `SkillStorageError` rather than silently lowercasing — this
prevents `"Google_Drive"` and `"google_drive"` from sharing a file.

You do not need to rewrite upstream skills only to replace `Path.home()`.
Use `skill_storage` when you are adding a native Hermes integration and want
a narrow, audited token file under `<profile>/tokens/`:

```python
# Before
TOKEN_FILE = Path.home() / ".cache" / "myskill" / "token.json"

# After
from hermes_multitenancy.skill_storage import get_token_path
TOKEN_FILE = get_token_path("myskill", extension="json")
```

---

## 6. Feishu UAT bindings

The org-sync pass (`python -m hermes_multitenancy.sync.cli pull-feishu`) does
two things on every run:

1. Reconciles the routing table from Feishu Contact v3 (existing
   behavior).
2. Copies `<shared>/feishu_uat/<employee.open_id>.json` →
   `<profile>/feishu_uat/<employee.open_id>.json` for every routed user,
   idempotently (`_migrate_feishu_uat_for_employee`).

**New OAuth bindings between sync passes**: the gateway-process OAuth
callback (upstream hermes-agent) currently writes to `<shared>/feishu_uat/`.
The AIAgent subprocess won't see that token until the next sync pass.
For low-latency rebind, trigger sync manually:

```bash
# Sync one department (subtree). Default --soft-delete-missing is off when
# --dept is set, so this won't deactivate routes outside the queried subtree.
python -m hermes_multitenancy.sync.cli pull-feishu --dept <OPEN_DEPARTMENT_ID>

# Or full-org sync (will soft-delete routes for users that disappeared).
python -m hermes_multitenancy.sync.cli pull-feishu
```

(The `soft-delete-missing=false` flag prevents the partial-scope sync
from removing routes for users outside the queried department.)

---

## 7. Verifying isolation in production

Run the bundled `scripts/verify-isolation.sh` against a target profile:

```bash
bash scripts/verify-isolation.sh ~/.hermes/profiles/alice
```

This walks the profile tree and reports:

* directory modes (must be 0700)
* token file modes (must be 0600)
* shared-home enumeration leaks (`ls ~/.hermes/feishu_uat/` should show
  files that alice's subprocess will *not* be able to read after档 B
  ships; today they're already not reached by `FEISHU_UAT_DIR`)

It does NOT spawn a real subprocess — that requires a Feishu event and
a working LLM. For an end-to-end check, send the bot a test message
("ping") from the profile's user and watch the gateway log for the env
keys passed to the spawned child:

```bash
tail -f ~/.hermes/logs/*.log | grep -i 'subprocess spawning'
```

---

## 8. 档 B — kernel-level sandboxing (implemented, opt-in)

档 A closes the convention-based leaks. 档 B closes the
*capability*-based ones (filesystem reads outside the profile, arbitrary
network egress) by wrapping every AIAgent subprocess with Apple's
`sandbox-exec(1)`. **Code is in place but disabled by default — flip the
toggle profile-by-profile during pilot.**

### 8.1 Policy file

`hermes_multitenancy/sandbox/profile-default.sb` is the only policy
currently shipped. Highlights:

* **Default deny** everything; `(import "system.sb")` for the syscall
  baseline Python needs to boot.
* **Reads allowed**: `/usr`, `/System`, `/Library`, `/opt/homebrew`,
  `HERMES_VENV`, `HERMES_AGENT_REPO`, `HERMES_MT_REPO`, plus the
  narrow set of dot-dirs host CLIs use (`~/.kep-cli`, `~/.aws`,
  `~/.config/gh`, `~/.config/git`, `~/.gitconfig`).
* **Writes allowed**: the routed `PROFILE_HOME` subtree, the shared
  `SHARED_HOME/cron` + `/snapshots`, and `/private/tmp`+`/private/var/folders`
  (per-process temp).
* **Hard denies**: `~/.ssh`, `~/Library/Keychains`, `~/.password-store`,
  *other* profiles' home trees under `SHARED_HOME/profiles`.
* **Network**: outbound 443/80/53 (TCP+UDP) only. Inbound denied. Local
  UNIX sockets allowed (asyncio internals). Note: sandbox-exec has no
  IP-range grammar, so intranet (RFC1918) blocking has to come from an
  application-level HTTP interceptor — TODO marker in the policy file.

The shipped policy is the **"network" variant**. A stricter
`readonly.sb` (no network) and a more permissive `trusted.sb` (full
HTTPS, intended for admin profiles) are designed but not yet written —
add them when the default policy has cleared its pilot.

### 8.2 Wrapper code

`agent_real._wrap_with_sandbox(cmd, profile_home)` wraps the argv at
spawn time when both toggles say go:

```python
if os.environ.get("HERMES_USE_SANDBOX") != "1":
    return cmd                              # disabled — no-op

if HERMES_SANDBOX_PROFILES is set and profile_home.name not in list:
    return cmd                              # gated out during pilot

if policy file missing OR /usr/bin/sandbox-exec not executable:
    return cmd  + WARNING log               # loud fallback, not crash
```

Otherwise it prepends:

```
/usr/bin/sandbox-exec -f profile-default.sb \
  -D PROFILE_HOME=...  -D SHARED_HOME=...  -D USER_HOME=... \
  -D HERMES_VENV=...   -D HERMES_AGENT_REPO=... -D HERMES_MT_REPO=...
```

Both `_run_aiagent_subprocess` (one-shot path) and
`_stream_aiagent_subprocess` (CardKit streaming path) use the same
wrapper.

### 8.3 Enabling SOP (recommended rollout)

#### 8.3.1 Pilot one profile (week 0)

Edit the target launchd plist (e.g. `spike_test`):

```bash
/usr/libexec/PlistBuddy \
  -c "Add :EnvironmentVariables:HERMES_USE_SANDBOX string 1" \
  -c "Add :EnvironmentVariables:HERMES_SANDBOX_PROFILES string spike_test" \
  ~/Library/LaunchAgents/ai.hermes.gateway-spike_test.plist

launchctl bootout gui/$(id -u)/ai.hermes.gateway-spike_test
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.gateway-spike_test.plist
```

`bootout` + `bootstrap` is mandatory — `launchctl kickstart -k` does
**not** re-read the plist (OpenClaw 5/9 教训).

Note that `spike_test` is its own gateway; for sandboxing **routed**
profiles (`feishu_g41a5b5g` etc.) the toggle must go on the
`multitenancy_router` plist, since that is the gateway that spawns the
AIAgent subprocess.

#### 8.3.2 Monitor (week 0-1)

```bash
# Live sandbox denies:
log stream --predicate 'sender CONTAINS "sandbox"' --info
# Or look for past denies:
log show --last 1h --predicate 'sender CONTAINS "sandbox"' --info
```

For verbose tracing inside the policy itself, uncomment `(debug deny)`
at the bottom of `profile-default.sb` and bootstrap again.

#### 8.3.3 Expand allowlist (week 1+)

Once `spike_test` runs clean, add real tenants one at a time:

```bash
/usr/libexec/PlistBuddy \
  -c "Set :EnvironmentVariables:HERMES_SANDBOX_PROFILES feishu_g41a5b5g,spike_test" \
  ~/Library/LaunchAgents/ai.hermes.gateway-multitenancy_router.plist
# bootout + bootstrap as above
```

#### 8.3.4 Final state (week 2+)

Once every tenant is covered, drop the allowlist entirely:

```bash
/usr/libexec/PlistBuddy \
  -c "Delete :EnvironmentVariables:HERMES_SANDBOX_PROFILES" \
  ~/Library/LaunchAgents/ai.hermes.gateway-multitenancy_router.plist
# bootout + bootstrap
```

`HERMES_USE_SANDBOX=1` alone now sandboxes everyone.

### 8.4 Rollback

Single env edit + bootout/bootstrap reverts to档 A:

```bash
/usr/libexec/PlistBuddy \
  -c "Delete :EnvironmentVariables:HERMES_USE_SANDBOX" \
  ~/Library/LaunchAgents/ai.hermes.gateway-multitenancy_router.plist
launchctl bootout … && launchctl bootstrap … …
```

If the policy file itself is wrong (subprocess refuses to spawn or
silently fails), the wrapper's loud-fallback path keeps the gateway
working — look for `WARNING [multitenancy] HERMES_USE_SANDBOX=1 but
policy ... is missing` lines.

---

## 9. References

* Commits implementing档 A:
  * `Isolate AIAgent subprocess env from parent gateway`
  * `Harden profile directory tree at provision time`
  * `Add profile-scoped skill_storage for token isolation`
  * `Bind Feishu UAT lookups to the profile-local directory`
* Prior-art notes:
  * `OpenClaw/排障 — gatekeeper 群聊 kep-prd-analysis 全链路修复 2026-05-08.md`
    — the source of the `sanitizeEnvVars` allowlist pattern
  * `OpenClaw/keep-record 最终架构 2026-04-24.md` — the HOME/workspace
    compatibility pattern and `{baseDir}` skill template convention. Hermes
    now implements these once at profile runtime instead of requiring per-skill
    wrappers or workspace fanout scripts.
