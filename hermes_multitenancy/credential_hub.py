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
    error          — status read failed unexpectedly

The vocabulary is exactly the WebUI ``SkillCredentialState`` set (no ``expired``
— an expired credential collapses to ``needs_auth``; the remaining validity is
carried by the additive ``expires_at`` field for the Feishu card to render).

Subprocess-backed readers (feishu-project via ``meegle``, kep-cli via
``kep-auth``) degrade gracefully when the binary is absent — exactly like the
WebUI — so this module is safe to call anywhere the run-broker runs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Credential ids in display order (identical set + order to the WebUI).
LARK_CLI = "lark-cli"
FEISHU_PROJECT = "feishu-project"
KEEP_RECORD = "keep-record"
KEP_CLI = "kep-cli"
GITLAB = "gitlab"

CREDENTIAL_ORDER = (LARK_CLI, FEISHU_PROJECT, KEEP_RECORD, KEP_CLI, GITLAB)

_TITLES = {
    LARK_CLI: "Lark-cli",
    FEISHU_PROJECT: "飞书项目",
    KEEP_RECORD: "Keep-record",
    KEP_CLI: "kep-cli",
    GITLAB: "GitLab",
}

# Status vocabulary
S_AUTHENTICATED = "authenticated"
S_CONFIGURED = "configured"
S_NEEDS_AUTH = "needs_auth"
S_UNKNOWN = "unknown"
S_MISSING = "missing"
S_ERROR = "error"

_MEEGLE_DEFAULT_HOST = "project.feishu.cn"
_SUBPROCESS_TIMEOUT = 10  # seconds — matches the WebUI execFile timeouts


@dataclass
class CredentialRow:
    """One credential's redacted status (SkillCredentialEntry-compatible)."""

    id: str
    title: str
    provider: str
    installed: bool
    status: str
    expires_at: Optional[int] = None  # epoch ms (additive; lark/keep)
    account_hint: Optional[str] = None
    default_identity: Optional[str] = None
    detail: Optional[str] = None
    required_by: list[str] = field(default_factory=list)
    action: dict[str, Any] = field(default_factory=dict)

    @property
    def authenticated(self) -> bool:
        return self.status == S_AUTHENTICATED

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "provider": self.provider,
            "installed": self.installed,
            "status": self.status,
            "expires_at": self.expires_at,
            "action": self.action or {"kind": "manual", "label": ""},
        }
        if self.account_hint:
            out["account_hint"] = self.account_hint
        if self.default_identity:
            out["default_identity"] = self.default_identity
        if self.detail:
            out["detail"] = self.detail
        if self.required_by:
            out["required_by"] = self.required_by
        return out


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_epoch_ms(value: Any) -> Optional[int]:
    """Parse an epoch value to milliseconds.

    Accepts ms (13-digit) or seconds (10-digit) — keep-record writes
    ``keep_auth_token_expired`` in seconds, so values < 1e12 are scaled to ms.
    """
    if value in (None, ""):
        return None
    try:
        n = int(float(value))
    except (ValueError, TypeError):
        return None
    if n <= 0:
        return None
    return n * 1000 if n < 1_000_000_000_000 else n


def profile_root(shared_home: Path, profile_name: str) -> Path:
    """``<shared_home>/profiles/<profile_name>`` — the profile root dir."""
    return Path(shared_home) / "profiles" / str(profile_name)


def profile_home_dir(shared_home: Path, profile_name: str) -> Path:
    """The HOME-redirect dir where per-profile tools drop their dotfiles."""
    return profile_root(shared_home, profile_name) / "home"


# --- small fs/subprocess helpers -------------------------------------------


def _read_small_text(path: Path, *, max_bytes: int = 64 * 1024) -> str:
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return ""
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _safe_account(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _run(cmd: list[str], *, cwd: Optional[Path] = None, env: Optional[dict[str, str]] = None) -> Optional[subprocess.CompletedProcess]:
    """Run a guarded subprocess. Returns CompletedProcess or None on failure."""
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("credential_hub: subprocess failed (%s): %s", cmd[:1], exc)
        return None


# --- profile skill scan (for required_by + installed) ----------------------


@dataclass
class _ProfileSkill:
    name: str
    text: str
    tags: list[str]
    source: Optional[str] = None  # 'hub' when installed via SkillHub provenance


def _skillhub_installed_names(skills_root: Path) -> set[str]:
    """Names installed via SkillHub — mirrors readSkillHubInstalledNames."""
    names: set[str] = set()
    for rel in (Path(".hub") / "lock.json", Path(".hermes-skillhub.json")):
        raw = _read_small_text(skills_root / rel)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        installed = data.get("installed")
        if isinstance(installed, dict):
            names.update(k for k in installed.keys() if k)
    return names


def scan_profile_skills(profile_dir: Path) -> list[_ProfileSkill]:
    """Walk ``<profile_dir>/skills`` for SKILL.md files (mirrors the WebUI scan)."""
    root = Path(profile_dir) / "skills"
    out: list[_ProfileSkill] = []
    if not root.is_dir():
        return out
    hub_names = _skillhub_installed_names(root)

    def visit(d: Path, depth: int) -> None:
        if depth > 8:
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if name == "node_modules" or name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                visit(Path(entry.path), depth + 1)
                continue
            if name == "SKILL.md":
                text = _read_small_text(Path(entry.path))
                if not text:
                    continue
                skill_name = _parse_skill_name(text) or Path(entry.path).parent.name
                dir_name = Path(entry.path).parent.name
                source = "hub" if (skill_name in hub_names or dir_name in hub_names) else None
                out.append(_ProfileSkill(name=skill_name, text=text,
                                         tags=_parse_skill_tags(text), source=source))

    visit(root, 0)
    return out


def _parse_skill_name(text: str) -> str:
    m = re.search(r'^name:\s*["\']?([^"\'\n]+)["\']?', text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _parse_skill_tags(text: str) -> list[str]:
    m = re.search(r"tags:\s*\[([^\]]+)\]", text, re.MULTILINE)
    if not m:
        return []
    return [t.strip().strip("\"'").lower() for t in m.group(1).split(",") if t.strip()]


def detect_skill_requirements(skill: _ProfileSkill) -> list[str]:
    """Which credential ids a skill needs (mirrors detectSkillCredentialRequirements)."""
    text = f"{skill.name}\n{chr(10).join(skill.tags)}\n{skill.text}".lower()
    source = str(skill.source or "").strip().lower()
    required: list[str] = []

    if any(p.search(text) for p in (
        re.compile(r"\blark[-_ ]?cli\b"),
        re.compile(r"\blarksuite\b"),
        re.compile(r"open\.feishu\.cn"),
        re.compile(r"feishu\.cn/(docx|docs|sheets|wiki|base|minutes|file)"),
        re.compile(r"\bwiki:wiki:readonly\b"),
        re.compile(r"\b(feishu|lark|larksuite)\b.{0,80}\b(docx|docs|base|sheets?|bitable|wiki)\b"),
        re.compile(r"\b(docx|docs|base|sheets?|bitable|wiki)\b.{0,80}\b(feishu|lark|larksuite)\b"),
        re.compile(r"\b(im:message|contact:user|drive:drive|wiki:wiki)\b"),
    )):
        required.append(LARK_CLI)

    if any(p.search(text) for p in (
        re.compile(r"\bmeegle\b"), re.compile(r"\bmeego\b"),
        re.compile(r"\bfeishu[-_ ]?project\b"), re.compile(r"project\.feishu\.cn"),
        re.compile("飞书项目"),
    )):
        required.append(FEISHU_PROJECT)

    # SkillHub-sourced skills always need kep-cli (parity with the TS hubSourced short-circuit).
    hub_sourced = source in ("hub", "aidock-skillhub")
    if hub_sourced or any(p.search(text) for p in (
        re.compile(r"\bkep[-_ ]?cli\b"), re.compile(r"\bkep[-_ ]?auth\b"),
        re.compile(r"\baidock\b"), re.compile(r"\bskillhub\b"),
        re.compile(r"\bkeep[-_ ]?login\b"), re.compile(r"\bproxy[-_ ]?cms\b"),
        re.compile(r"proxy\.cms\.(pre\.)?example\.com"),
        re.compile(r"ark\.example\.com/aidock-cms"),
        re.compile(r"skill/zipfile"),
        re.compile(r"\bkep_profile\b"), re.compile(r"\bkep_no_auto_login\b"),
        re.compile(r"bearer\s+token.*example"),
    )):
        required.append(KEP_CLI)

    if any(p.search(text) for p in (
        re.compile(r"\bkeep-record\b"), re.compile(r"\bkeep_auth_token\b"),
        re.compile(r"\bget_qrcode\b"), re.compile(r"\bpersist_auth\b"),
    )):
        required.append(KEEP_RECORD)

    if any(p.search(text) for p in (
        re.compile(r"\bgitlab_token\b"), re.compile(r"gitlab\.example\.com"),
        re.compile(r"oauth2:\$\{?gitlab_token\}?@"),
    )):
        required.append(GITLAB)

    return required


def _requirements_by_id(skills: list[_ProfileSkill]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for skill in skills:
        for cid in detect_skill_requirements(skill):
            lst = out.setdefault(cid, [])
            if skill.name not in lst:
                lst.append(skill.name)
    for lst in out.values():
        lst.sort()
    return out


def _has_skill(
    skills: list[_ProfileSkill],
    *,
    name: str | None = None,
    tags: tuple[str, ...] = (),
    needles: tuple[str, ...] = (),
) -> bool:
    for skill in skills:
        if name and skill.name == name:
            return True
        if tags and any(t in skill.tags for t in tags):
            return True
        low = skill.text.lower()
        if needles and all(n in low for n in needles):
            return True
    return False


# --- per-tool readers -------------------------------------------------------


def _local_feishu_uat(profile_dir: Path) -> tuple[bool, Optional[int]]:
    """Mirror the WebUI localFeishuUatStatus: read ``<profile_dir>/feishu_uat/*.json``.

    Returns (connected, latest_expires_at_ms). Connected iff some cache file has a
    non-empty access_token AND (no expiry OR expiry > now + 60s).
    """
    d = Path(profile_dir) / "feishu_uat"
    connected = False
    latest_exp: Optional[int] = None
    try:
        names = [p for p in d.iterdir() if p.name.endswith(".json")]
    except OSError:
        return False, None
    for path in names:
        raw = _read_small_text(path)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        token = parsed.get("access_token")
        exp = parsed.get("expires_at")
        try:
            exp_i = int(exp) if exp else 0
        except (ValueError, TypeError):
            exp_i = 0
        if exp_i and (latest_exp is None or exp_i > latest_exp):
            latest_exp = exp_i
        if isinstance(token, str) and token and (not exp_i or exp_i > _now_ms() + 60_000):
            connected = True
    return connected, latest_exp


def lark_cli_status(
    *,
    profile_name: str,
    open_id: str,
    shared_home: Path,
    profile_dir: Optional[Path] = None,
    required_by: Optional[list[str]] = None,
) -> CredentialRow:
    """lark-cli/feishu UAT — mirrors the WebUI larkCliStatus 3-way OR.

    authenticated iff DB status 'valid' OR local feishu_uat/*.json file-cache is
    connected OR lark_cli.default_identity == 'user' (parity with skill-credentials.ts).
    """
    from . import feishu_uat_auth

    row = CredentialRow(
        id=LARK_CLI, title=_TITLES[LARK_CLI], provider="lark", installed=True,
        status=S_UNKNOWN, required_by=required_by or [],
        action={"kind": "feishu_device_flow", "label": "授权"},
    )
    try:
        raw = feishu_uat_auth.credential_status(
            profile_name=profile_name, open_id=open_id, shared_home=shared_home
        )
    except Exception as exc:
        logger.debug("credential_hub: lark-cli status read failed (%s)", exc)
        row.status = S_UNKNOWN
        row.detail = "Lark-cli 状态读取失败"
        return row

    raw_status = str(raw.get("status") or "")
    lark = raw.get("lark_cli") or {}
    default_identity = lark.get("default_identity")
    # Display the REFRESH window, not the ~1h access token: the access token is
    # auto-renewed via the refresh token (refresh_uat_if_needed), so the user
    # only needs to re-auth when the refresh token expires (~30 days). Showing
    # the 1h access expiry alarms users into thinking it's about to break.
    access_exp = raw.get("expires_at")
    refresh_exp = raw.get("refresh_expires_at")
    row.expires_at = int(refresh_exp) if refresh_exp else (int(access_exp) if access_exp else None)

    p_dir = Path(profile_dir) if profile_dir else (Path(shared_home) / "profiles" / profile_name)
    local_connected, local_exp = _local_feishu_uat(p_dir)
    if local_exp and not row.expires_at:
        row.expires_at = local_exp

    # Status vocab matches the WebUI SkillCredentialState (no 'expired'):
    connected = raw_status == "valid" or local_connected or default_identity == "user"
    if connected:
        row.status = S_AUTHENTICATED
        row.default_identity = default_identity or ("user" if local_connected else None)
        row.detail = "Lark-cli 已完成用户授权（访问令牌自动续期，到期前无需重新授权）。"
        row.action["label"] = "重新授权"
    elif raw_status in ("missing", "scope_missing", "expired"):
        row.status, row.detail = S_NEEDS_AUTH, "Lark-cli 需要用户授权后才能访问私有飞书资源。"
    else:
        row.status, row.detail = S_UNKNOWN, "Lark-cli 状态待验证。"
    return row


def _meegle_allow_npx_status() -> bool:
    """Mirror shouldAllowNpxForMeegleStatus — gate npx execution for status reads."""
    override = os.environ.get("HERMES_MEEGLE_STATUS_ALLOW_NPX", "").strip().lower()
    if override:
        return override not in ("0", "false", "no", "off")
    return True  # WebUI default (NODE_ENV !== 'test')


def _meegle_invocation(*, allow_npx: bool = True) -> Optional[list[str]]:
    explicit = os.environ.get("HERMES_MEEGLE_BIN", "").strip()
    if explicit:
        return [explicit]
    direct = shutil.which("meegle")
    if direct:
        return [direct]
    if not allow_npx:
        return None
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", "@lark-project/meegle"]
    return None


def _meegle_profile(profile_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(profile_name or "default")).strip("_") or "default"
    return f"hermes_{safe}"


def feishu_project_status(
    *, profile_dir: Path, profile_name: str, required_by: Optional[list[str]] = None
) -> CredentialRow:
    """feishu-project via meegle CLI `auth status --format json` (guarded)."""
    row = CredentialRow(
        id=FEISHU_PROJECT, title=_TITLES[FEISHU_PROJECT], provider="feishu-project",
        installed=False, status=S_MISSING, required_by=required_by or [],
        action={"kind": "oauth_url", "label": "授权"},
    )
    inv = _meegle_invocation(allow_npx=_meegle_allow_npx_status())
    if inv is None:
        # npx-only but npx execution is disabled for status (or no CLI at all).
        # Mirror the WebUI: available iff npx is present, without running it.
        if shutil.which("npx"):
            row.installed = True
            row.status = S_NEEDS_AUTH
            row.detail = "飞书项目需要授权后才能查询和更新工作项。"
        else:
            row.detail = "飞书项目 CLI 未安装，无法授权或调用飞书项目能力。"
        return row

    host = os.environ.get("HERMES_MEEGLE_HOST", _MEEGLE_DEFAULT_HOST).strip() or _MEEGLE_DEFAULT_HOST
    env = {**os.environ, "MEEGLE_HOST": host}
    cmd = [*inv, "--profile", _meegle_profile(profile_name), "auth", "status", "--format", "json"]
    proc = _run(cmd, cwd=profile_dir, env=env)
    if proc is None or proc.returncode not in (0, None):
        # Binary present but status errored → treat as installed-but-needs-auth
        row.installed = True
        row.status = S_NEEDS_AUTH
        row.detail = "飞书项目需要授权后才能查询和更新工作项。"
        return row
    try:
        parsed = json.loads(proc.stdout or "{}")
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    row.installed = True
    if parsed.get("authenticated") is True:
        row.status = S_AUTHENTICATED
        row.account_hint = _safe_account(parsed.get("account") or (parsed.get("user") or {}).get("name"))
        row.detail = "飞书项目 CLI 已在当前 profile 完成授权。"
        row.action["label"] = "重新授权"
    else:
        row.status = S_NEEDS_AUTH
        row.detail = "飞书项目需要授权后才能查询和更新工作项。"
    return row


def _keep_record_verified(home_dir: Path, token: str) -> tuple[bool, Optional[str]]:
    marker = _read_small_text(Path(home_dir) / ".keepai" / "webui-auth-verified.json")
    if not marker:
        return False, None
    try:
        parsed = json.loads(marker)
    except (json.JSONDecodeError, TypeError):
        return False, None
    expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return parsed.get("token_sha256") == expected, _safe_account(parsed.get("account_hint"))


def keep_record_status(*, home_dir: Path, installed: bool = True) -> CredentialRow:
    """keep-record — reads ``<home>/.keepai/.env`` + webui verification marker."""
    row = CredentialRow(
        id=KEEP_RECORD, title=_TITLES[KEEP_RECORD], provider="keep",
        installed=installed, status=S_MISSING,
        action={"kind": "skill_flow", "label": "扫码认证", "command": "/keep-record auth"},
    )
    if not installed:
        row.status = S_MISSING
        row.detail = "Keep-record skill 未在该 profile 安装。"
        return row

    env = _parse_env_file(Path(home_dir) / ".keepai" / ".env")
    token = env.get("keep_auth_token") or ""
    if not token:
        row.status = S_NEEDS_AUTH
        row.detail = "Keep-record 需要扫码授权。"
        return row

    row.action["label"] = "重新扫码"
    row.account_hint = env.get("keep_username") or None
    row.expires_at = _normalize_epoch_ms(env.get("keep_auth_token_expired"))

    # Status vocab matches the WebUI exactly: the webui-auth-verified marker is
    # the source of truth (the WebUI ignores token expiry for keep-record). The
    # expires_at field above is additive info for the Feishu card only.
    verified, marker_account = _keep_record_verified(home_dir, token)
    if marker_account and not row.account_hint:
        row.account_hint = marker_account
    if verified:
        row.status = S_AUTHENTICATED
        row.detail = "Keep-record 已通过 WebUI 扫码验证。"
    else:
        row.status = S_UNKNOWN
        row.detail = "Keep-record 本地凭证存在，但未经 WebUI 验证，建议重新扫码确认。"
    return row


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    text = _read_small_text(path)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _kep_auth_bin(shared_home: Path) -> str:
    explicit = os.environ.get("HERMES_KEP_AUTH_BIN", "").strip()
    if explicit:
        return explicit
    return str(Path(shared_home) / "bin" / "kep-auth")


def kep_cli_status(
    *,
    profile_dir: Path,
    home_dir: Path,
    profile_name: str,
    shared_home: Path,
    installed: bool = False,
    required_by: Optional[list[str]] = None,
) -> CredentialRow:
    """kep-cli — keyring presence + live ``kep-auth status`` (guarded)."""
    row = CredentialRow(
        id=KEP_CLI, title=_TITLES[KEP_CLI], provider="keep",
        installed=installed, status=S_MISSING, required_by=required_by or [],
        action={"kind": "oauth_url", "label": "认证"},
    )
    if not installed:
        row.detail = "该 profile 没有安装依赖 kep-cli 的 skill。"
        return row

    keyring = Path(home_dir) / ".kep-cli" / "keyring-fallback"
    try:
        has_token = keyring.is_dir() and any("token-key:online:" in p.name for p in keyring.iterdir())
    except OSError:
        has_token = False

    bin_path = _kep_auth_bin(shared_home)
    live: Optional[str] = None
    account: Optional[str] = None
    if Path(bin_path).exists():
        env = {
            **os.environ,
            "HOME": str(home_dir),
            "HERMES_HOME": str(profile_dir),
            "KEP_PROFILE": str(profile_name),
            "KEP_NO_AUTO_LOGIN": "1",
        }
        proc = _run([bin_path, "--profile", profile_name, "--env", "online", "status"], cwd=profile_dir, env=env)
        if proc is not None:
            out = f"{proc.stdout}\n{proc.stderr}".lower()
            if re.search(r"not\s*logged\s*in", out):
                live = "not_logged_in"
            elif re.search(r"logged\s*in|state:\s*(valid|logged\s*in)", out):
                live = "logged_in"
                account = _parse_kep_account(f"{proc.stdout}\n{proc.stderr}")
            elif proc.returncode == 3 or re.search(r"unauthorized|401", out):
                # WebUI maps exit-3 / 401 in the error path to not_logged_in.
                live = "not_logged_in"

    if live == "logged_in":
        row.status, row.account_hint = S_AUTHENTICATED, account
        row.detail = "kep-auth 已验证该 profile 登录。"
        row.action["label"] = "重新认证"
    elif live == "not_logged_in":
        row.status = S_NEEDS_AUTH
        row.detail = "kep-auth 报告该 profile 未登录。"
    elif has_token:
        row.status = S_UNKNOWN
        row.detail = "kep-cli 凭证存在，但无法在线校验有效性。"
    else:
        row.status = S_NEEDS_AUTH
        row.detail = "kep-cli 需要登录。"
    return row


def _parse_kep_account(output: str) -> Optional[str]:
    for key in ("operator", "user"):
        m = re.search(rf"^{key}:\s*(.+)$", output, re.MULTILINE | re.IGNORECASE)
        if m:
            return _safe_account(re.sub(r"\s*<[^>]+>\s*", "", m.group(1)).strip())
    return None


def gitlab_status(*, profile_dir: Path, installed: bool = False) -> CredentialRow:
    """gitlab — readable profile token file ⇒ configured (no interactive flow)."""
    token_path = Path(profile_dir) / "workspace" / "credentials" / "gitlab.token"
    can_read = bool(_read_small_text(token_path).strip())
    is_installed = installed or can_read
    row = CredentialRow(
        id=GITLAB, title=_TITLES[GITLAB], provider="gitlab",
        installed=is_installed, status=S_MISSING,
        action={"kind": "manual", "label": "配置"},
    )
    if can_read:
        row.status = S_CONFIGURED
        row.detail = "当前 profile 可读取 GitLab token（不展示内容）。"
        row.action["label"] = "刷新"
    elif is_installed:
        row.status = S_NEEDS_AUTH
        row.detail = "需要为当前 profile 提供可读的 GitLab token。"
    else:
        row.detail = "该 profile 没有 GitLab 相关 skill 或 token。"
    return row


def collect_credential_statuses(
    *,
    profile_name: str,
    open_id: str,
    shared_home: Optional[Path] = None,
    home_dir: Optional[Path] = None,
) -> list[CredentialRow]:
    """Aggregate all five credentials' status for one profile/open_id, in order.

    ``home_dir`` is the profile's HOME-redirect dir (where per-profile tools drop
    dotfiles). Callers that already know it (the router has the authoritative
    ``profile_home``) should pass it; otherwise it is derived from ``shared_home``
    — which honors ``HERMES_SHARED_HOME``/``HERMES_HOME``.
    """
    from . import feishu_uat_auth

    shared = Path(shared_home) if shared_home else feishu_uat_auth.resolve_shared_home()
    if home_dir is not None:
        home = Path(home_dir)
        p_dir = home.parent
    else:
        p_dir = profile_root(shared, profile_name)
        home = p_dir / "home"

    skills = scan_profile_skills(p_dir)
    req = _requirements_by_id(skills)
    keep_installed = _has_skill(skills, name=KEEP_RECORD, needles=("keep_auth_token", "get_qrcode"))
    kep_installed = bool(req.get(KEP_CLI)) or _has_skill(
        skills, name="kep-hades-cli", tags=("kep-cli",), needles=("kep-auth", "--env online")
    )
    gitlab_installed = _has_skill(skills, name="kep-prd-analysis", needles=("gitlab_token",))

    return [
        lark_cli_status(profile_name=profile_name, open_id=open_id, shared_home=shared,
                        profile_dir=p_dir, required_by=req.get(LARK_CLI)),
        feishu_project_status(profile_dir=p_dir, profile_name=profile_name, required_by=req.get(FEISHU_PROJECT)),
        keep_record_status(home_dir=home, installed=keep_installed),
        kep_cli_status(profile_dir=p_dir, home_dir=home, profile_name=profile_name, shared_home=shared,
                       installed=kep_installed, required_by=req.get(KEP_CLI)),
        gitlab_status(profile_dir=p_dir, installed=gitlab_installed),
    ]


# --- display helpers --------------------------------------------------------


def human_expiry(expires_at: Optional[int], *, now_ms: Optional[int] = None) -> str:
    """Render an expiry timestamp (ms) as a short zh phrase, or '' if unknown."""
    if not expires_at:
        return ""
    now = now_ms if now_ms is not None else _now_ms()
    delta_ms = int(expires_at) - now
    if delta_ms <= 0:
        return "已过期"
    days = delta_ms // (24 * 3600 * 1000)
    if days >= 1:
        return f"{days}天后过期"
    hours = delta_ms // (3600 * 1000)
    if hours >= 1:
        return f"{hours}小时后过期"
    minutes = max(1, delta_ms // (60 * 1000))
    return f"{minutes}分钟后过期"
