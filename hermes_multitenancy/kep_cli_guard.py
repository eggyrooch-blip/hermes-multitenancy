"""Per-profile shim that makes kep-cli family commands profile-correct.

kep-cli (kep-auth/kep-cli/halo-cli/hades-cli/asgard-cli/phantasia-cli) identifies the
tenant ONLY via the ``--profile`` flag, defaulting to the ``KEP_PROFILE`` env var. The
multitenancy sandbox sets ``KEP_PROFILE`` correctly, but a skill/LLM that spawns kep-auth
in its own subprocess can drop the env, so the command falls back to the empty default
profile and reports a false "not logged in" — while the connector probe (which always
passes ``--profile`` explicitly) shows authenticated.

This shim mirrors :mod:`hermes_multitenancy.lark_cli_guard`: PATH-prepended wrapper scripts
that inject ``--profile`` derived from ``KEP_PROFILE`` → ``HERMES_PROFILE`` → the profile
name recovered from ``$HOME`` (``<profiles>/<name>/home`` → ``<name>``). ``$HOME`` is the
most reliably-preserved sandbox var, so the profile is recovered even when ``KEP_PROFILE``
was stripped. The wrapper calls the real binary by absolute path (never via PATH) to avoid
recursing into itself.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

# kep-cli family binaries that accept a ``--profile`` flag. NOTE: ``kep-cli`` itself (the
# installer/doctor meta-CLI) is deliberately EXCLUDED — it has no ``--profile`` flag, so
# shimming it would break ``kep-cli install`` ("unknown flag: --profile"). Only per-system
# CLIs that are tenant-scoped belong here.
KEP_CLI_SHIM_NAMES = (
    "kep-auth",
    "halo-cli",
    "hades-cli",
    "asgard-cli",
    "phantasia-cli",
)


def _shim_program(real_binary: str) -> str:
    """POSIX-sh wrapper: inject --profile unless already present, then exec the real bin."""
    return textwrap.dedent(
        f"""\
        #!/bin/sh
        # hermes-multitenancy kep-cli profile shim — auto-generated, do not edit.
        # Recover the tenant profile from the most reliable signal available. $HOME is
        # the ultimate fallback: the sandbox always pivots HOME to <profiles>/<name>/home,
        # so the profile is recoverable even if KEP_PROFILE was dropped by a caller.
        prof="${{KEP_PROFILE:-${{HERMES_PROFILE:-}}}}"
        if [ -z "$prof" ]; then
            prof=$(basename "$(dirname "$HOME")" 2>/dev/null)
        fi
        REAL={real_binary!r}
        # If the caller already passed --profile, do not inject a second one.
        for a in "$@"; do
            if [ "$a" = "--profile" ]; then
                exec "$REAL" "$@"
            fi
        done
        if [ -n "$prof" ]; then
            exec "$REAL" --profile "$prof" "$@"
        fi
        exec "$REAL" "$@"
        """
    )


def install_kep_cli_shim(shim_dir: Path, *, real_bin_dir: Path) -> Path:
    """Write the kep-cli family wrapper scripts into ``shim_dir``.

    Each wrapper execs ``<real_bin_dir>/<name>`` (absolute path → no PATH recursion) with
    ``--profile`` injected. Returns the primary (kep-auth) shim path. Idempotent: rewrites
    the scripts each call so a moved real_bin_dir is picked up.
    """
    shim_dir = Path(shim_dir)
    real_bin_dir = Path(real_bin_dir)
    shim_dir.mkdir(parents=True, exist_ok=True)
    primary = shim_dir / "kep-auth"
    for name in KEP_CLI_SHIM_NAMES:
        real = real_bin_dir / name
        # Only shim a CLI the profile actually has — the shim set mirrors what was
        # dispatched into the shared bin, so a profile without hades-cli gets no hades shim.
        if not real.exists():
            continue
        body = _shim_program(str(real))
        path = shim_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return primary
