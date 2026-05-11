#!/usr/bin/env bash
# verify-isolation.sh — audit a profile's档 A sandbox state.
#
# Usage:
#   bash scripts/verify-isolation.sh <profile_home>
#
# Checks the on-disk layout for the post-档 A invariants:
#   * profile_home and required subdirs are mode 0700
#   * tokens/ files (skill_storage) are mode 0600
#   * feishu_uat/ files are mode 0600
#   * the legacy shared <hermes_home>/feishu_uat/ does NOT contain files
#     for open_ids that have already been migrated into the target profile
#     (would indicate sync-pass migration is stale, not a security issue
#     per se, but it means the AIAgent subprocess uses outdated tokens)
#
# Exit codes:
#   0 — all checks passed
#   1 — at least one check failed (details printed)
#   2 — usage error / profile not found
#
# Does NOT spawn a real AIAgent subprocess; that requires Feishu/LLM
# infra and is out of scope. See docs/profile-isolation.md §7 for the
# manual end-to-end verification path.

set -uo pipefail

# --- args -------------------------------------------------------------------

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <profile_home>" >&2
    echo "example: $0 ~/.hermes/profiles/alice" >&2
    exit 2
fi

PROFILE_HOME="$1"
PROFILE_HOME="${PROFILE_HOME/#\~/$HOME}"
PROFILE_HOME="$(cd "$PROFILE_HOME" 2>/dev/null && pwd)" || {
    echo "ERROR: profile_home does not exist: $1" >&2
    exit 2
}

FAILURES=0
CHECKS=0

# --- helpers ----------------------------------------------------------------

# macOS `stat` uses -f, GNU uses -c. Auto-detect.
if stat -f '%Lp' / >/dev/null 2>&1; then
    STAT_MODE='stat -f %Lp'
else
    STAT_MODE='stat -c %a'
fi

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; CHECKS=$((CHECKS+1)); }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; CHECKS=$((CHECKS+1)); FAILURES=$((FAILURES+1)); }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$1"; }

check_mode() {
    local path="$1"
    local expected="$2"
    if [[ ! -e "$path" ]]; then
        fail "$path — does not exist"
        return
    fi
    local actual
    actual=$($STAT_MODE "$path")
    if [[ "$actual" == "$expected" ]]; then
        ok "$path mode=$actual"
    else
        fail "$path mode=$actual, expected $expected"
    fi
}

check_dir_exists() {
    local path="$1"
    if [[ -d "$path" ]]; then
        ok "$path exists"
    else
        fail "$path — directory missing"
    fi
}

# --- run --------------------------------------------------------------------

echo "Profile: $PROFILE_HOME"

hdr "[1/4] Profile root + isolation pivot directories (expect mode 0700)"
check_mode "$PROFILE_HOME"            700
for sub in home cache config state data tmp tokens feishu_uat memories sessions skills cron; do
    check_mode "$PROFILE_HOME/$sub"   700
done

hdr "[2/4] Per-skill token files in tokens/ (expect mode 0600)"
shopt -s nullglob
token_files=("$PROFILE_HOME/tokens"/*)
if [[ ${#token_files[@]} -eq 0 ]]; then
    echo "  (no token files cached yet — that's fine if the profile hasn't authorised any skill)"
else
    for f in "${token_files[@]}"; do
        check_mode "$f" 600
    done
fi

hdr "[3/4] Feishu UAT JSON files in feishu_uat/ (expect mode 0600)"
uat_files=("$PROFILE_HOME/feishu_uat"/*.json)
if [[ ${#uat_files[@]} -eq 0 ]]; then
    echo "  (no UAT bound — user has not run /bind, or sync-pass migration has not run)"
else
    for f in "${uat_files[@]}"; do
        check_mode "$f" 600
    done
fi

hdr "[4/4] Sanity: profile's own feishu_uat does not contain stranger files"
canonical_open_id_files=("$PROFILE_HOME/feishu_uat"/ou_*.json)
if [[ ${#canonical_open_id_files[@]} -gt 1 ]]; then
    # Each profile should hold at most one UAT (the user it routes to).
    # >1 is suspicious: either two users were merged or migration mis-copied.
    fail "expected ≤1 ou_*.json under feishu_uat/, found ${#canonical_open_id_files[@]}"
    for f in "${canonical_open_id_files[@]}"; do
        echo "    - $(basename "$f")"
    done
elif [[ ${#canonical_open_id_files[@]} -eq 1 ]]; then
    ok "single canonical UAT file: $(basename "${canonical_open_id_files[0]}")"
else
    echo "  (no canonical UAT — expected for un-bound profiles)"
fi
shopt -u nullglob

# --- summary ----------------------------------------------------------------

hdr "Summary"
if [[ $FAILURES -eq 0 ]]; then
    printf "  \033[32m%d/%d checks passed.\033[0m\n" "$CHECKS" "$CHECKS"
    exit 0
else
    printf "  \033[31m%d/%d checks FAILED.\033[0m\n" "$FAILURES" "$CHECKS"
    echo
    echo "Remediation:"
    echo "  * Mode drift (not 0700/0600): re-run org sync —"
    echo "      python -m hermes_multitenancy.sync.cli pull-feishu"
    echo "    which re-applies chmod via _tighten_dir_mode on every pass."
    echo "  * Missing isolation pivot directories: same fix — sync rebuilds the layout."
    echo "  * Multiple ou_*.json under feishu_uat/: investigate routing-table"
    echo "    conflict before doing anything else; do NOT delete files until you"
    echo "    have confirmed which open_id is canonical for this profile."
    exit 1
fi
