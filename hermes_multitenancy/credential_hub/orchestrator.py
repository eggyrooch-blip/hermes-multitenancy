"""Concurrent aggregation of all five credential readers + the registry adapter.

The two functions here fan out to the reader/skill helpers. Because the test
suite monkeypatches those helpers on the ``credential_hub`` package namespace
(``credential_hub.lark_cli_status``, ``credential_hub.scan_profile_skills`` …),
this module resolves every one of them through the package object ``_hub`` at
call time so the patches take effect. ``profile_root`` is never patched, so it is
imported and called directly.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from hermes_multitenancy import credential_hub as _hub

from ._io import profile_root
from .model import (
    FEISHU_PROJECT,
    KEEP_RECORD,
    KEP_CLI,
    LARK_CLI,
    CredentialRow,
)


def _collect_credential_rows(
    *,
    profile_name: str,
    open_id: str,
    shared_home: Optional[Path] = None,
    home_dir: Optional[Path] = None,
) -> list[CredentialRow]:
    """Low-level reader: aggregate all five credentials' status, in order.

    This is the canonical reader the Connector Registry wraps. ``home_dir`` is
    the profile's HOME-redirect dir (where per-profile tools drop dotfiles).
    Callers that already know it (the router has the authoritative
    ``profile_home``) should pass it; otherwise it is derived from ``shared_home``
    — which honors ``HERMES_SHARED_HOME``/``HERMES_HOME``.
    """
    from .. import feishu_uat_auth

    shared = Path(shared_home) if shared_home else feishu_uat_auth.resolve_shared_home()
    if home_dir is not None:
        home = Path(home_dir)
        p_dir = home.parent
    else:
        p_dir = _hub.profile_root(shared, profile_name)
        home = p_dir / "home"

    skills = _hub.scan_profile_skills(p_dir)
    req = _hub._requirements_by_id(skills)
    keep_installed = _hub._has_skill(skills, name=KEEP_RECORD, needles=("keep_auth_token", "get_qrcode"))
    kep_installed = bool(req.get(KEP_CLI)) or _hub._has_skill(
        skills, name="kep-hades-cli", tags=("kep-cli",), needles=("kep-auth", "--env online")
    )
    kep_target_env, kep_report_envs = _hub._kep_skill_env_policy(skills)
    gitlab_installed = _hub._has_skill(skills, name="kep-prd-analysis", needles=("gitlab_token",))

    # Each reader shells out to a CLI (kep-auth, meegle via npx, keep-record) and
    # releases the GIL while waiting on the subprocess, so running them concurrently
    # collapses the panel's wall-clock from sum-of-readers (~1.5-2s serial) to
    # max-of-reader (~the slowest single CLI). Readers share no mutable state and each
    # guards its own failure internally (returning a degraded CredentialRow), so a slow
    # or failing one cannot corrupt the others. Order is preserved by ThreadPoolExecutor.map.
    readers = (
        lambda: _hub.lark_cli_status(profile_name=profile_name, open_id=open_id, shared_home=shared,
                                     profile_dir=p_dir, required_by=req.get(LARK_CLI)),
        lambda: _hub.feishu_project_status(profile_dir=p_dir, profile_name=profile_name,
                                           required_by=req.get(FEISHU_PROJECT)),
        lambda: _hub.keep_record_status(home_dir=home, installed=keep_installed),
        lambda: _hub.kep_cli_statuses(profile_dir=p_dir, home_dir=home, profile_name=profile_name,
                                      shared_home=shared, installed=kep_installed, required_by=req.get(KEP_CLI),
                                      target_env=kep_target_env),
        lambda: _hub.gitlab_status(profile_dir=p_dir, installed=gitlab_installed),
    )
    with ThreadPoolExecutor(max_workers=len(readers)) as pool:
        rows: list[CredentialRow] = []
        for result in pool.map(lambda fn: fn(), readers):
            if isinstance(result, list):
                rows.extend(result)
            else:
                rows.append(result)
        return rows


def collect_credential_statuses(
    *,
    profile_name: str,
    open_id: str,
    shared_home: Optional[Path] = None,
    home_dir: Optional[Path] = None,
) -> list[CredentialRow]:
    """Aggregate all five credentials' status for one profile/open_id, in order.

    Adapter over the Connector Registry (the control plane): this routes through
    ``connectors.registry`` so the registry is the source of truth, then maps the
    enriched ``ConnectorStatus`` back to the legacy ``CredentialRow`` — output is
    byte-identical to the pre-registry behavior (the registry only ADDS scope
    fields; ``compat.to_credential_row`` drops them). The lazy import keeps the
    dependency acyclic: ``credential_hub`` is importable without the registry;
    the registry imports ``credential_hub`` readers.
    """
    from ..connectors import compat, registry

    statuses = registry.collect_connector_statuses(
        profile_name=profile_name,
        open_id=open_id,
        shared_home=shared_home,
        home_dir=home_dir,
    )
    return [compat.to_credential_row(status) for status in statuses]
