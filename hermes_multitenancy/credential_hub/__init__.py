"""Credential hub status aggregation — the single 归口 for credential status.

The ``/auth`` Feishu command and the hermes-web-ui CredentialsView are two
paths over THIS aggregation. It mirrors the WebUI's
``services/hermes/skill-credentials.ts`` semantics for all five credentials
(lark-cli, feishu-project, keep-record, kep-cli, gitlab) so the two surfaces
agree on status, and exposes a redacted, SkillCredentialEntry-compatible shape.

Only the READ path lives here. Starting an auth flow (device flow / QR / OAuth)
stays in the per-tool modules / WebUI controllers and is unaffected.

Status vocabulary (matches the WebUI ``SkillCredentialState``):

    authenticated  — a valid credential is present / live status confirmed login
    configured     — a credential is readable but not an interactive login (gitlab)
    needs_auth     — installed but no/!valid credential → user should auth
    unknown        — credential material exists but validity cannot be confirmed here
    missing        — the tool/credential is not installed for the profile

Package layout (this module is the re-export shim; behaviour is unchanged from
the pre-split single-file module — every public symbol below is importable and
monkeypatchable at ``hermes_multitenancy.credential_hub.<name>`` exactly as
before). Reader/orchestrator submodules resolve monkeypatchable helpers through
this package object so ``monkeypatch.setattr(credential_hub, "_run", ...)`` etc.
keep working across the module boundary:

    model.py        — CredentialRow + ids/titles/status vocab + human_expiry
    _io.py          — fs/subprocess/time helpers (_run, _read_small_text, …)
    skills.py       — profile skill scan + "skill → credential" requirement engine
    readers/        — one module per credential system (lark, feishu_project,
                      keep_record, kep, gitlab)
    orchestrator.py — _collect_credential_rows (ThreadPool) + collect_credential_statuses
"""
from __future__ import annotations

# The pre-split single-file module exposed these stdlib/typing imports, ``logger``,
# and the re-exported ``build_status_subprocess_env`` at top level. External importers
# and monkeypatch targets (``credential_hub.subprocess``, ``credential_hub.Path``,
# ``from credential_hub import build_status_subprocess_env`` …) depend on them, so the
# shim reproduces that namespace verbatim — zero behaviour change across the split.
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..credential_renewal_common import build_status_subprocess_env

logger = logging.getLogger(__name__)

from ._io import (
    _SUBPROCESS_TIMEOUT,
    _normalize_epoch_ms,
    _now_ms,
    _parse_env_file,
    _read_small_text,
    _run,
    _safe_account,
    profile_home_dir,
    profile_root,
)
from .model import (
    CREDENTIAL_ORDER,
    FEISHU_PROJECT,
    GITLAB,
    GITLAB_PERSONAL,
    KEEP_RECORD,
    KEP_CLI,
    KEP_CLI_ENV_IDS,
    KEP_CLI_IDS,
    KEP_CLI_ONLINE,
    KEP_CLI_PRE,
    LARK_CLI,
    S_AUTHENTICATED,
    S_CONFIGURED,
    S_ERROR,
    S_MISSING,
    S_NEEDS_AUTH,
    S_UNKNOWN,
    CredentialRow,
    _TITLES,
    human_expiry,
)
from .skills import (
    _configured_domain_patterns,
    _has_skill,
    _is_resource_delivery_skill_name,
    _kep_skill_env_policy,
    _parse_skill_name,
    _parse_skill_tags,
    _ProfileSkill,
    _requirements_by_id,
    _skillhub_installed_names,
    detect_skill_requirements,
    scan_profile_skills,
)
from .readers.lark import _local_feishu_uat, lark_cli_status
from .readers.feishu_project import (
    _MEEGLE_DEFAULT_HOST,
    _meegle_allow_npx_status,
    _meegle_invocation,
    _meegle_profile,
    _meegle_search_path,
    _which_meegle,
    feishu_project_status,
)
from .readers.keep_record import _keep_record_verified, keep_record_status
from .readers.kep import (
    _decode_jwt_exp_ms,
    _kep_auth_bin,
    _kep_env_status,
    _kep_env_token_present,
    _kep_token_exp_ms,
    _normalize_kep_env_name,
    _ordered_kep_envs,
    _parse_kep_account,
    kep_auth_state_line,
    kep_cli_status,
    kep_cli_statuses,
)
from .readers.gitlab import gitlab_status
from .orchestrator import _collect_credential_rows, collect_credential_statuses
