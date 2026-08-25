# Account-ledger production exporter runbook

Status: local candidate only; not shipped or installed. Production remains unchanged.

This runbook installs and runs the root-only, GET-only producer for the existing
`account_ledger` verifier. It never creates, updates, blocks, or deletes a Hermes
route, LiteLLM account, or key. It sends only paginated `GET /user/list` and
`GET /key/list` requests and publishes the four envelopes only after their
signatures and common snapshot ID pass the pinned verifier.

## Fixed trust and secret files

The following material must arrive from the approved offline secret-management
path. Do not generate a temporary key on `hermes-1`, do not derive one from an
environment variable, and do not fall back to an ambient LiteLLM login.

| Path | Owner/mode | Purpose |
|---|---|---|
| `/etc/hermes/account-ledger-signing-private.pem` | `root:root 0600` | Ed25519 PKCS8 signer; exporter only |
| `/etc/hermes/account-ledger-public.pem` | `root:root 0644` | pinned Ed25519 verifier trust root |
| `/etc/hermes/account-ledger-fingerprint.key` | `root:root 0600` | HMAC subject fingerprints |
| `/etc/hermes/account-ledger-litellm-readonly.key` | `root:root 0600` | management credential constrained in-process to the two GET endpoints |
| `/etc/hermes/account-ledger-export.env` | `root:root 0600` | non-secret, explicit input/output paths and LiteLLM origin |

Provision approved material with `install`, never by shell interpolation that
would echo secret bytes. The public key must be the exact SubjectPublicKeyInfo
PEM derived from the approved private key; the exporter compares them and exits
before HTTP or output on any mismatch.

```bash
/usr/bin/install -d -o root -g root -m 0755 /etc/hermes
/usr/bin/install -o root -g root -m 0600 APPROVED_SIGNING_PRIVATE.pem /etc/hermes/account-ledger-signing-private.pem
/usr/bin/install -o root -g root -m 0644 APPROVED_SIGNING_PUBLIC.pem /etc/hermes/account-ledger-public.pem
/usr/bin/install -o root -g root -m 0600 APPROVED_FINGERPRINT.key /etc/hermes/account-ledger-fingerprint.key
/usr/bin/install -o root -g root -m 0600 APPROVED_LITELLM_READONLY.key /etc/hermes/account-ledger-litellm-readonly.key
```

Those four `APPROVED_*` sources are placeholders for the operator's protected
secret-transfer path, not files to create on production. Stop if that path or
its audit receipt is unavailable.

## Dedicated immutable runtime

The oneshot must never execute the Hermes-user-owned gateway interpreter or an
editable module below `/home/hermes`: root execution there would expose the
signer and LiteLLM bearer to code that `hermes` can replace. The normal non-root
release pipeline builds a bundle tar (wheelhouse plus the reviewed systemd unit)
and a fully hashed requirements lock;
production never invokes its ambient Python, pip, or uv. `cryptography>=43.0`
and the exact exporter wheel must both be present in that `--require-hashes`
lock. The protected release receipt supplies the two SHA-256 values and commit
ID; stop rather than accepting a digest file carried inside the artifact.

The 2026-08-11 production read-only probe verified `/`, `/usr`, `/usr/bin`, and
every absolute executable below as uid 0, non-symlink, and not group/world
writable. Re-run that owner/mode/ancestor probe after any OS update. The only
bootstrap interpreter is `/usr/bin/python3.11` (uid 0, mode 0755, regular file);
`/usr/bin/python3` is a symlink and is forbidden. `/usr/bin/uv` and
`/usr/local/bin/uv` are absent and must not be supplied from `PATH`.

<!-- root-install:start -->
```bash
# Run from the already verified /usr/bin/bash --noprofile --norc. These values
# are copied verbatim from the protected release receipt, never from the tar.
readonly RELEASE_ID='REPLACE_WITH_40_HEX_COMMIT'
readonly EXPECTED_ARTIFACT_SHA256='REPLACE_WITH_64_HEX_ARTIFACT_DIGEST'
readonly EXPECTED_LOCK_SHA256='REPLACE_WITH_64_HEX_LOCK_DIGEST'
readonly ARTIFACT='/var/lib/hermes/account-ledger/incoming/account-ledger-export-bundle.tar'
readonly LOCK='/var/lib/hermes/account-ledger/incoming/requirements.lock'
[[ "$RELEASE_ID" =~ ^[0-9a-f]{40}$ ]]
[[ "$EXPECTED_ARTIFACT_SHA256" =~ ^[0-9a-f]{64}$ ]]
[[ "$EXPECTED_LOCK_SHA256" =~ ^[0-9a-f]{64}$ ]]

# Incoming files and their full ancestors must already be root-owned and not
# group/world writable; neither leaf may be a symlink.
assert_root_chain() {
  local current="$1" owner mode
  while true; do
    test ! -L "$current"
    read -r owner mode < <(/usr/bin/stat -c '%U %a' "$current")
    test "$owner" = root
    (( (8#$mode & 8#022) == 0 ))
    if test "$current" = /; then break; fi
    current="${current%/*}"
    if test -z "$current"; then current=/; fi
  done
}
for executable in \
  /usr/bin/bash /usr/bin/sha256sum /usr/bin/tar /usr/bin/install \
  /usr/bin/mv /usr/bin/ln /usr/bin/chown /usr/bin/chmod /usr/bin/find \
  /usr/bin/readlink /usr/bin/namei /usr/bin/stat /usr/bin/python3.11 \
  /usr/bin/systemctl /usr/bin/systemd-analyze /usr/bin/journalctl
do
  assert_root_chain "$executable"
  test -f "$executable" && test ! -L "$executable"
done
assert_root_chain "$ARTIFACT"
assert_root_chain "$LOCK"
/usr/bin/namei -l "$ARTIFACT"
/usr/bin/namei -l "$LOCK"
test ! -L "$ARTIFACT" && test ! -L "$LOCK"
test "$(/usr/bin/stat -c '%U:%G %a' "$ARTIFACT")" = 'root:root 600'
test "$(/usr/bin/stat -c '%U:%G %a' "$LOCK")" = 'root:root 600'

# Digest both release inputs before tar, Python, pip, or artifact code is processed.
printf '%s  %s\n' "$EXPECTED_ARTIFACT_SHA256" "$ARTIFACT" | /usr/bin/sha256sum -c -
printf '%s  %s\n' "$EXPECTED_LOCK_SHA256" "$LOCK" | /usr/bin/sha256sum -c -

readonly STAGE="/opt/hermes/.account-ledger-export-${RELEASE_ID}.$$"
readonly RELEASE_DIR="/opt/hermes/account-ledger-export-${RELEASE_ID}"
/usr/bin/install -d -o root -g root -m 0755 /opt/hermes
test ! -e "$STAGE" && test ! -e "$RELEASE_DIR"
/usr/bin/install -d -o root -g root -m 0755 "$STAGE"
/usr/bin/install -d -o root -g root -m 0755 "$STAGE/bundle"
/usr/bin/tar --extract --file "$ARTIFACT" --directory "$STAGE/bundle" \
  --no-same-owner --no-same-permissions
test -d "$STAGE/bundle/wheelhouse"
test -f "$STAGE/bundle/deploy/hermes-account-ledger-export.service"

# Fixed, probed OS Python creates the venv with a non-symlink interpreter copy.
/usr/bin/python3.11 -m venv --copies "$STAGE/venv"
test ! -L "$STAGE/venv/bin/python"
test -z "$(/usr/bin/find "$STAGE" -xdev -type l -print -quit)"
test -z "$(/usr/bin/find "$STAGE" -xdev \( ! -user root -o -perm /022 \) -print -quit)"
"$STAGE/venv/bin/python" -m pip install --no-index --require-hashes \
  --find-links "$STAGE/bundle/wheelhouse" --requirement "$LOCK"
/usr/bin/chmod 0644 "$STAGE/bundle/deploy/hermes-account-ledger-export.service"
/usr/bin/chown -R root:root "$STAGE"
/usr/bin/chmod -R go-w "$STAGE"
/usr/bin/mv -T "$STAGE" "$RELEASE_DIR"
test ! -e /opt/hermes/.account-ledger-export.next
/usr/bin/ln -s "account-ledger-export-${RELEASE_ID}" /opt/hermes/.account-ledger-export.next
/usr/bin/mv -Tf /opt/hermes/.account-ledger-export.next /opt/hermes/account-ledger-export

# The final mv is the atomic symlink switch. Validate the full resolved tree
# before the first execution of installed exporter code.
readonly RUNTIME='/opt/hermes/account-ledger-export'
readonly RUNTIME_PY="$RUNTIME/venv/bin/python"
/usr/bin/namei -l "$RUNTIME_PY"
test ! -L "$RUNTIME_PY"
NONROOT="$(/usr/bin/find -L "$RUNTIME" -xdev ! -user root -print -quit)"
WRITABLE="$(/usr/bin/find -L "$RUNTIME" -xdev -perm /022 -print -quit)"
printf 'non-root-owned=%s group/world-writable=%s\n' "${NONROOT:+1}" "${WRITABLE:+1}"
test -z "$NONROOT" && test -z "$WRITABLE"
"$RUNTIME_PY" - <<'PY'
from pathlib import Path
import cryptography
import hermes_multitenancy.account_ledger_export
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
root = Path('/opt/hermes/account-ledger-export').resolve(strict=True)
crypto_module = Path(cryptography.__file__).resolve(strict=True)
exporter_module = Path(hermes_multitenancy.account_ledger_export.__file__).resolve(strict=True)
print('cryptography_version=' + cryptography.__version__)
print('cryptography_inside_runtime=' + str(root in crypto_module.parents).lower())
print('exporter_inside_runtime=' + str(root in exporter_module.parents).lower())
raise SystemExit(0 if root in crypto_module.parents and root in exporter_module.parents else 1)
PY
"$RUNTIME_PY" -m pip check
```
<!-- root-install:end -->

Any non-root owner, group/world-writable component, import outside the dedicated
runtime, missing `cryptography`, or failed `pip check` keeps the service disabled.
The two aggregate ownership checks must read exactly
`non-root-owned=0 group/world-writable=0`.
The exporter has no alternate interpreter, crypto implementation, or unsigned
fallback.

## Install the oneshot

The output parent is private and the final per-run directory must not already
exist. Keep the org snapshot path explicit: do not use “latest by mtime”. The
source files may be written by `hermes`, but they are never trusted in place:
the root exporter opens them without following the leaf symlink, binds SQLite's
actual open descriptor to the locator inode (including the live WAL), and copies
both sources into its root-owned 0700 staging directory before parsing. It reads
the frozen database through `/proc/self/fd/<fd>` on Linux. Trust/signing inputs,
frozen files, output, and the dedicated runtime require a root-owned ancestor
chain with no group/world-writable component.

```bash
/usr/bin/install -d -o root -g root -m 0700 /var/lib/hermes/account-ledger
/usr/bin/install -o root -g root -m 0644 /opt/hermes/account-ledger-export/bundle/deploy/hermes-account-ledger-export.service /etc/systemd/system/.hermes-account-ledger-export.service.new
/usr/bin/mv -Tf /etc/systemd/system/.hermes-account-ledger-export.service.new /etc/systemd/system/hermes-account-ledger-export.service
# `/etc/hermes/account-ledger-export.env` is provisioned directly by the same
# approved root-only config path as the trust files; never copy it from a home checkout.
/usr/bin/systemctl daemon-reload
/usr/bin/systemctl disable hermes-account-ledger-export.service
```

The approved environment file contains paths and origin only, never secret
bytes. Example shape:

```ini
HERMES_ACCOUNT_LEDGER_ORG_SNAPSHOT=/home/hermes/.hermes/org-snapshots/org-EXPLICIT-FROZEN.json
HERMES_ACCOUNT_LEDGER_DATABASE=/home/hermes/.hermes/multitenancy.db
HERMES_ACCOUNT_LEDGER_LITELLM_BASE_URL=https://litellm.sre.example.com
HERMES_ACCOUNT_LEDGER_LITELLM_KEY_FILE=/etc/hermes/account-ledger-litellm-readonly.key
HERMES_ACCOUNT_LEDGER_OUTPUT_DIR=/var/lib/hermes/account-ledger/EXPLICIT-RUN-ID
```

## Preflight and run

The ownership check may print only fixed paths, modes, and counts. Never dump
the PEM, HMAC key, bearer, envelopes, or employee rows into a terminal/log.

```bash
/usr/bin/stat -c '%U:%G %a %n' \
  /etc/hermes/account-ledger-signing-private.pem \
  /etc/hermes/account-ledger-public.pem \
  /etc/hermes/account-ledger-fingerprint.key \
  /etc/hermes/account-ledger-litellm-readonly.key \
  /etc/hermes/account-ledger-export.env
test "$(/usr/bin/stat -c '%U:%G %a' /etc/systemd/system/hermes-account-ledger-export.service)" = 'root:root 644'
test "$(/usr/bin/readlink -f /opt/hermes/account-ledger-export)" = "/opt/hermes/account-ledger-export-${RELEASE_ID}"
/usr/bin/systemd-analyze verify /etc/systemd/system/hermes-account-ledger-export.service
/usr/bin/systemctl start hermes-account-ledger-export.service
/usr/bin/systemctl show hermes-account-ledger-export.service -p Result -p ExecMainStatus
/usr/bin/journalctl -u hermes-account-ledger-export.service -n 20 --no-pager
```

Expected success is `Result=success`, `ExecMainStatus=0`, four explicit layer
counts equal to the frozen roster count, and every missing/unexpected/duplicate/
cross-identity count equal to zero. Findings contain only 64-hex HMAC subject
fingerprints. Exit `1` is a valid signed audit with findings; exit `2` means the
input, trust, signer, permissions, source locator, HTTP inventory, or schema was
not trustworthy. Neither result authorizes remediation.

For the currently known duplicate account bindings, the exporter is expected to
exit `2` before HTTP and before publishing until the separately authorized
containment/remediation step has made the account binding unique. Do not weaken
that guard to obtain a report.

## Rollback

There is no timer and no online request-path integration. On any failure, leave
the oneshot disabled, preserve the published 0600 snapshot directory if one was
successfully verified, and restore the previous root-only `/etc/hermes` backup.
Do not delete or rewrite routes, accounts, keys, profiles, sessions, or employee
resources. Re-run only after the trust/runtime/input blocker is resolved.
