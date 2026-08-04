#!/usr/bin/env bash
# Install the GitLab CLI into the shared Hermes bin dir.
#
# This is the GitLab connector's command channel. Profiles already get that dir
# first on PATH (see agent_real/subprocess_env.py), and glab needs no config
# file or `glab auth login` — it runs purely off the GITLAB_TOKEN / GITLAB_HOST
# pair injected per profile, so installing the binary is the whole job.
#
# The version is PINNED, not "latest": 1.100.0 is the build actually verified
# against our GitLab CE 14.10.5 (glab api /version + glab repo view). Bumping it
# means re-verifying against 14.10 first — a newer glab may assume endpoints
# this instance does not have.
#
# Idempotent: re-running with the target version already installed is a no-op.
set -euo pipefail

GLAB_VERSION="${GLAB_VERSION:-1.100.0}"
GLAB_SHA256="${GLAB_SHA256:-d2a8ccffe924d3ad22feb2d1338840e9a610041c6ea6fd76e9aeda3744f210c2}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
BIN_DIR="${BIN_DIR:-$HERMES_HOME/bin}"
TARGET="$BIN_DIR/glab"

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) GLAB_ARCH="amd64" ;;
  aarch64|arm64) GLAB_ARCH="arm64" ;;
  *) echo "unsupported arch: $ARCH" >&2; exit 1 ;;
esac

if [ "$GLAB_ARCH" != "amd64" ]; then
  echo "NOTE: the pinned checksum covers linux_amd64 only; set GLAB_SHA256 for $GLAB_ARCH" >&2
  [ -n "${GLAB_SHA256_OVERRIDE:-}" ] || { echo "refusing to install unverified binary" >&2; exit 1; }
  GLAB_SHA256="$GLAB_SHA256_OVERRIDE"
fi

if [ -x "$TARGET" ] && "$TARGET" --version 2>/dev/null | grep -q "$GLAB_VERSION"; then
  echo "glab $GLAB_VERSION already installed at $TARGET"
  exit 0
fi

TARBALL="glab_${GLAB_VERSION}_linux_${GLAB_ARCH}.tar.gz"
URL="https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/${TARBALL}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "downloading $URL"
curl -fsSL -m 120 -o "$WORK/$TARBALL" "$URL"

# Verify BEFORE unpacking: an unverified archive is never expanded, let alone run.
echo "$GLAB_SHA256  $WORK/$TARBALL" | sha256sum -c - >/dev/null || {
  echo "CHECKSUM MISMATCH — refusing to install" >&2
  echo "  expected: $GLAB_SHA256" >&2
  echo "  actual:   $(sha256sum "$WORK/$TARBALL" | awk '{print $1}')" >&2
  exit 1
}

tar -xzf "$WORK/$TARBALL" -C "$WORK"
BINARY="$(find "$WORK" -type f -name glab -perm -u+x | head -1)"
[ -n "$BINARY" ] || { echo "glab binary not found in archive" >&2; exit 1; }

mkdir -p "$BIN_DIR"
# Atomic swap so a concurrently-starting agent never sees a half-written binary.
install -m 0755 "$BINARY" "$TARGET.new"
mv -f "$TARGET.new" "$TARGET"

INSTALLED="$("$TARGET" --version 2>&1 | head -1)"
echo "installed: $INSTALLED -> $TARGET"
case "$INSTALLED" in
  *"$GLAB_VERSION"*) ;;
  *) echo "WARNING: installed binary does not report $GLAB_VERSION" >&2; exit 1 ;;
esac
