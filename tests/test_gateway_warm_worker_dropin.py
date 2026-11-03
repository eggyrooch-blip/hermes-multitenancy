"""The warm-worker drop-in is load-bearing, not a tuning knob.

Without ``HERMES_AIAGENT_WARM_WORKER=1`` the tenant turn runs in a one-shot
subprocess that exits milliseconds after the turn, and Python kills the
background review daemon thread on interpreter shutdown — silently, with no
exception and no log line. Production measured 1659 review clients created and
22 closed (1.3%) in that state.

The drop-in directory is not in git, so ``install-gateway-dropins.sh`` is the
only thing that brings it back after a host rebuild. These tests pin both ends:
the file says the right thing, and the installer actually installs it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DROPIN = REPO / "deploy" / "hermes-gateway-warm-worker.conf"
INSTALLER = REPO / "deploy" / "install-gateway-dropins.sh"
INSTALLED_NAME = "35-warm-worker.conf"


def test_dropin_sets_the_warm_worker_flag():
    text = DROPIN.read_text(encoding="utf-8")
    assert "[Service]" in text
    assert "Environment=HERMES_AIAGENT_WARM_WORKER=1" in text


def test_dropin_records_why_it_exists():
    """A future reader must not mistake this for a perf knob and delete it."""
    text = DROPIN.read_text(encoding="utf-8")
    assert "daemon" in text.lower()
    assert "one-shot" in text.lower()


def test_installer_installs_the_dropin():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "hermes-gateway-warm-worker.conf" in text
    assert INSTALLED_NAME in text


def test_installed_name_sorts_before_the_gateway_defaults():
    """systemd applies drop-ins in lexical order; keep ours deterministic."""
    assert INSTALLED_NAME[:2].isdigit()


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("0", False), ("true", False), ("", False), (None, False)],
)
def test_enablement_reads_exactly_one(monkeypatch, value, expected):
    """The flag is an exact `== "1"` check — `true`/`yes` do NOT enable it."""
    from hermes_multitenancy.agent_real.warm_worker import (
        _aiagent_warm_worker_enabled,
    )

    if value is None:
        monkeypatch.delenv("HERMES_AIAGENT_WARM_WORKER", raising=False)
    else:
        monkeypatch.setenv("HERMES_AIAGENT_WARM_WORKER", value)
    assert _aiagent_warm_worker_enabled() is expected


def test_installer_also_covers_the_templated_gateway_unit():
    """Expert bots run as hermes-gateway@<profile>.service instances.

    A drop-in under hermes-gateway.service.d/ does NOT apply to them. Verified on
    production 2026-08-19: the expert_krd gateway process carried no
    HERMES_AIAGENT_WARM_WORKER at all, so its tenants would have kept the killed
    review while gateway-only greps looked green.
    """
    text = INSTALLER.read_text(encoding="utf-8")
    assert "hermes-gateway@.service.d" in text
    # both destinations must receive the same file
    assert text.count(f'"$_warm_dir/{INSTALLED_NAME}"') == 1
    assert '"$DROPIN_DIR" "$TEMPLATE_DROPIN_DIR"' in text
