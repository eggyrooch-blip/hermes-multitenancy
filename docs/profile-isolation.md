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
| A skill that writes a token to `Path.home() / ".myskill"` lands in the shared home | ✅ leaks across profiles | ⚠️ partially: HOME is NOT pivoted (host CLIs like `kep-cli` rely on it); use `skill_storage.write_token()` to land in `<profile>/tokens/` explicitly |
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
├── home/                        # provisioned but NOT used as HOME pivot (see below)
├── cache/                       # XDG_CACHE_HOME
├── config/                      # XDG_CONFIG_HOME
├── state/                       # XDG_STATE_HOME
├── data/                        # XDG_DATA_HOME
├── tmp/                         # TMPDIR
├── memories/                    # hermes long-term memory
├── sessions/                    # hermes session blobs
├── skills/                      # per-profile skill copies
├── cron/                        # per-profile cronjobs
└── ...                          # logs, plans, workspace, skins
```

The five "isolation pivot" directories (`cache`, `config`, `state`,
`data`, `tmp`) back the env redirect set by
`agent_real._build_subprocess_env` (XDG_CACHE_HOME, XDG_CONFIG_HOME,
XDG_STATE_HOME, XDG_DATA_HOME, TMPDIR). They are created at provision time
so the first AIAgent subprocess spawn does not have to materialise them at
the umask default (typically 0755) before they are tightened.

**`home/` is intentionally NOT used as the HOME env target.** A prior
iteration of `_build_subprocess_env` rebound `HOME` to `<profile>/home/`
(modelled after the OpenClaw `keep-record` workspace-bridge pattern), but
that turned out to be a host/container category error: the OpenClaw pattern
works inside Docker where the workspace IS the sandbox; this plugin runs as
a host Python process where `Path.home()` must keep pointing at the real
user home so host-installed CLI tools (`kep-cli`, `aws`, `gh`, …) can find
`~/.kep-cli`, `~/.aws`, `~/.config/gh` etc. Skill-level token isolation is
enforced explicitly via `skill_storage.write_token()` rather than
implicitly via HOME redirection. The `home/` subdir is still provisioned
so future per-profile-home features can use it, but the env var is not
rebound.

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

## 5. Skill token storage

Skills caching OAuth tokens or API keys MUST go through
`hermes_multitenancy.skill_storage`:

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

If you migrate an upstream skill that hardcodes `Path.home() / ".cache"
/ "myskill"`, the migration path is:

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

## 8. Roadmap to档 B (kernel-level sandboxing)

档 A closes the convention-based leaks. To close the *capability*-based
ones (file-system reads outside the profile, arbitrary network egress),
the next step is `sandbox-exec` policies under `hermes_multitenancy/sandbox/`:

* `readonly.sb` — file-read into profile + system trees only, no network
* `network.sb` — readonly + 443/80 outbound to a small allowlist
* `trusted.sb` — readonly + full HTTPS outbound (admin profile only)

These will be invoked by wrapping `_run_aiagent_subprocess`'s `argv` with
`/usr/bin/sandbox-exec -f <policy>.sb -D PROFILE_HOME=...` when
`HERMES_USE_SANDBOX=1` is set.

Until档 B ships, treat档 A as defense-in-depth, not as an authorisation
boundary. A skill that wants to read `~/.ssh/` can still do so.

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
  * `OpenClaw/keep-record 最终架构 2026-04-24.md` — the HOME pivot
    pattern (`process.env.HOME = workspace-bridge`). Tried in档 A v1,
    rolled back because that pattern is container-only — see
    `架构 — Hermes Profiles 安装 kep-cli 2026-05-07.md` for why a host
    Python process must keep `Path.home()` at the real user home.
