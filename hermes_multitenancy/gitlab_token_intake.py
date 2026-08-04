"""Employee-supplied GitLab token intake: validate, then vault.

Gates, all of which exist because the GitLab instance enforces none of them:

* **An expiry is mandatory.** CE 14.10 lets a PAT be created with a blank expiry
  and self-managed CE has no instance-wide maximum-lifetime policy, so nothing
  upstream will ever age this credential out. Expiry also bites at runtime — an
  expired record is refused rather than injected.

* **The granted scopes must match the tier the employee picked.** Scopes are
  READ OFF GitLab, not inferred: the employee names the token
  :data:`HERMES_TOKEN_NAME` and we look up that row's ``scopes``. Behavioural
  inference was tried and abandoned — 14.10 permits both ``api`` and ``read_api``
  on every GET (so no GET separates them), and non-GET probes are refused even
  for ``api`` on this instance. A stored scope LABEL is likewise untrustworthy:
  see DEBT.md 2026-08-03, where the recorded label said read-only and the token
  was an admin credential.

* **The runtime contract must already be live.** Storing a record the deployed
  config would never read is the worst outcome available: the card says
  configured and every call still goes out under the shared credential.

* **The user's legacy shared-token file is retired synchronously**, before the
  personal record is stored. Leaving it would put the global token on their disk
  while their env carries their own.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .credentials import CredentialStore

#: GitLab tells us the token's real scopes if we can find the token's own row.
#: We cannot ask "which row am I?" (14.10 has no /personal_access_tokens/self),
#: so the employee names the token by agreement and we look it up by name.
#:
#: Why not infer scopes from endpoint behaviour: 14.10 permits BOTH ``api`` and
#: ``read_api`` on every GET, so no GET distinguishes them, and non-GET probes
#: are refused even for ``api`` on this instance (measured). Behavioural
#: inference was tried twice and is unsound here — read the declared scopes.
_TOKEN_LIST = "/api/v4/personal_access_tokens?per_page=100"

#: The name employees must give the token, so we can find its row.
HERMES_TOKEN_NAME = "hermes"

#: Recorded alongside the scopes to keep the vault row HONEST about what we
#: actually know. The name lookup finds *a* row called HERMES_TOKEN_NAME; no API
#: on CE 14.10 binds a token VALUE to its metadata row (there is no
#: ``/personal_access_tokens/self``), so a user could name a weak token `hermes`
#: and submit a stronger one. The tier check is therefore a MISTAKE-CATCHER, not
#: an enforced boundary, and this marker says so on the row itself.
#:
#: Accepted as debt by sunke 2026-08-03 after the round-3 block. Rationale: the
#: bypass grants no privilege the employee did not already hold — it mislabels,
#: it does not escalate — and it cannot happen by accident. Never treat a stored
#: scope list as audit evidence (see DEBT.md 2026-08-03, where a mislabelled
#: scope hid an admin credential).
SCOPE_BINDING_UNVERIFIED = "gitlab:scope-binding-unverified"

DEFAULT_HOST = "gitlab.gotokeep.com"

#: The two tiers an employee picks between on the card.
#:
#: This split exists because of a hard fact about this GitLab: per the
#: instance's own scope table, ``read_repository`` / ``write_repository`` are
#: "for the repository **through git clone**" only — they grant NO REST API
#: access. glab's mr/issue/ci/repo commands are all REST, so a repository-only
#: token makes the connector inert. An API scope is therefore mandatory, and the
#: only real choice is how much of one.
TIER_READ = "read"     # read_api + read_repository — browse MRs/issues/pipelines, clone
TIER_WRITE = "write"   # api + write_repository — full glab, including writes
VALID_TIERS = (TIER_READ, TIER_WRITE)

#: Lookup outcomes.
PROBE_OK = "probe_ok"                # found the row; scopes are known
PROBE_GIT_ONLY = "probe_git_only"    # no REST access at all => glab cannot work
PROBE_NOT_FOUND = "probe_not_found"  # no active token named HERMES_TOKEN_NAME
PROBE_AMBIGUOUS = "probe_ambiguous"  # several — we cannot tell which one this is
INVALID_TOKEN = "invalid_token"
UNDETERMINED = "undetermined"

#: Scopes each tier requires. The API scope is what makes glab work at all; the
#: repository scope is what makes git clone/push work.
_REQUIRED_SCOPES = {
    TIER_READ: ("read_api", "read_repository"),
    TIER_WRITE: ("api", "write_repository"),
}


class TokenRejected(Exception):
    """A submitted token failed a gate. ``reason`` is shown to the employee."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_expiry(raw: str, *, today: Optional[date] = None) -> int:
    """``YYYY-MM-DD`` -> epoch ms at UTC midnight. Raises TokenRejected."""
    text = str(raw or "").strip()
    if not text:
        raise TokenRejected(
            "到期日必填。GitLab 允许 token 永不过期，我们这边不接受——"
            "请在 GitLab 建 token 时填一个到期日，再把同一个日期填在这里。"
        )
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        # NEVER echo the submitted text back. The token and expiry inputs sit
        # next to each other, so a mis-paste puts a live credential in this
        # string — and the rejection reason is rendered into a Feishu card that
        # persists in the user's chat history.
        raise TokenRejected("到期日格式应为 YYYY-MM-DD，例如 2027-01-31。请重新填写。") from None
    if parsed <= (today or datetime.now(timezone.utc).date()):
        raise TokenRejected("到期日必须是未来的日期，请重新填写。")
    return int(
        datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc).timestamp() * 1000
    )


def probe_token(
    token: str,
    *,
    host: str = DEFAULT_HOST,
    timeout: float = 10.0,
    opener: Any = None,
) -> tuple[str, list[str]]:
    """Read the token's REAL scopes from GitLab. Returns ``(status, scopes)``.

    We ask GitLab to list the submitter's tokens (a GET, reachable with either
    API scope) and pick the row named :data:`HERMES_TOKEN_NAME`. That row's
    ``scopes`` is authoritative — no inference, and it covers the repository
    scopes too, which no endpoint probe could ever establish.

    A 403 on the listing means the token holds no API scope at all: fine for
    ``git clone``, useless to glab. 401 means GitLab does not recognise it.
    """
    request = urllib.request.Request(
        f"https://{host}{_TOKEN_LIST}",
        headers={"PRIVATE-TOKEN": token, "Accept": "application/json"},
        method="GET",
    )
    _open = opener or urllib.request.urlopen
    try:
        with _open(request, timeout=timeout) as response:
            if not 200 <= int(response.status) < 300:
                return UNDETERMINED, []
            rows = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        if code == 401:
            return INVALID_TOKEN, []
        if code == 403:
            return PROBE_GIT_ONLY, []
        return UNDETERMINED, []
    except Exception:
        return UNDETERMINED, []

    if not isinstance(rows, list):
        return UNDETERMINED, []
    matches = [
        row for row in rows
        if isinstance(row, dict)
        and row.get("active")
        and str(row.get("name") or "").strip().lower() == HERMES_TOKEN_NAME
    ]
    if not matches:
        return PROBE_NOT_FOUND, []
    if len(matches) > 1:
        # We cannot tell which of them is the one just submitted, so we would be
        # recording someone else's scope set. Refuse instead of guessing.
        return PROBE_AMBIGUOUS, []
    scopes = [str(s) for s in (matches[0].get("scopes") or []) if str(s).strip()]
    return PROBE_OK, scopes


def gitlab_config_entry(shared_home: Path) -> Optional[dict[str, Any]]:
    """The materialization entry for gitlab, or None."""
    import yaml

    from .credential_materializer import _resolve_config_path

    config = _resolve_config_path(Path(shared_home), None)
    if config is None:
        return None
    raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    for entry in raw.get("credentials") or []:
        if isinstance(entry, dict) and str(entry.get("provider") or "") == "gitlab":
            return entry
    return None


def gitlab_subject_id(shared_home: Path) -> Optional[str]:
    """The subject id the materialization config uses for gitlab, or None."""
    entry = gitlab_config_entry(Path(shared_home))
    if not entry:
        return None
    return str(entry.get("subject_id") or "").strip() or None


def assert_runtime_contract_live(shared_home: Path, *, profile_name: str) -> dict[str, Any]:
    """Refuse intake unless the deployed config will actually USE this profile's token.

    Every part of the runtime contract is checked, not just the ``__self__``
    marker: the entry must name a subject, opt into the self lane, expose the env
    var the CLI reads, AND actually target this submitter. Any one of those
    missing means the token is banked and never injected — the card would say
    configured while every call still used the shared credential.
    """
    from .credential_materializer import (
        SELF_PROFILE_MARKER,
        _target_profiles,
    )

    shared_home = Path(shared_home)
    entry = gitlab_config_entry(shared_home)
    if not entry:
        raise TokenRejected("服务端还没有配置 GitLab 凭据条目，暂时无法收取，请联系管理员。")
    if not str(entry.get("subject_id") or "").strip():
        raise TokenRejected("服务端 GitLab 凭据条目缺少 subject_id，请联系管理员。")
    if str(entry.get("vault_profile") or "").strip() != SELF_PROFILE_MARKER:
        raise TokenRejected(
            "服务端尚未开启「使用个人 token」，现在提交不会生效，因此没有保存。请联系管理员。"
        )
    if not str(entry.get("env") or entry.get("env_name") or "").strip():
        raise TokenRejected(
            "服务端 GitLab 凭据条目没有配置环境变量名，token 不会被注入，因此没有保存。请联系管理员。"
        )
    try:
        targeted = _target_profiles(entry, shared_home=shared_home)
    except Exception:
        raise TokenRejected("服务端 GitLab 凭据条目的适用范围读不出来，请联系管理员。") from None
    if profile_name not in targeted:
        raise TokenRejected(
            "你的账号不在服务端 GitLab 凭据的适用范围里，提交不会生效，因此没有保存。请联系管理员。"
        )
    return entry


def retire_legacy_token_file(
    shared_home: Path, *, profile_name: str, entry: dict[str, Any]
) -> bool:
    """Delete this user's shared-token file. Returns True if one was removed.

    Raises :class:`TokenRejected` when a file exists but cannot be removed:
    proceeding would leave the global token on their disk while their env
    carries their own, which is the split the whole design exists to prevent.
    Called BEFORE storing, so a failure here leaves the user exactly as they
    were rather than half-switched.
    """
    from .credential_materializer import _safe_target

    target_rel = _safe_target(entry.get("target") or "workspace/credentials/token")
    stale = Path(shared_home) / "profiles" / profile_name / target_rel
    if not stale.is_file():
        return False
    try:
        stale.unlink()
    except OSError:
        raise TokenRejected(
            "无法清除你 profile 里旧的共享 token 文件，为避免新旧混用，这次没有保存。请联系管理员。"
        ) from None
    return True


def submit_personal_token(
    *,
    profile_name: str,
    token: str,
    expires_on: str,
    tier: str,
    shared_home: Path,
    host: str = DEFAULT_HOST,
    db_path: Optional[Path] = None,
    prober: Any = None,
) -> dict[str, Any]:
    """Validate an employee's token and store it under their own profile.

    ``tier`` is what the employee SAID they were granting. It is checked against
    what the token can actually do, and a mismatch is refused in both
    directions: a token more powerful than the claim means the user misread what
    they handed over, and one less powerful means the connector would silently
    fail at the job they expect it to do.

    Raises :class:`TokenRejected` with an employee-facing reason on any gate.
    """
    shared_home = Path(shared_home)
    cleaned = str(token or "").strip()
    if not cleaned:
        raise TokenRejected("没收到 token。")
    if tier not in VALID_TIERS:
        raise TokenRejected("请先选择授权档位（只读 / 可写）。")

    expires_at = parse_expiry(expires_on)
    entry = assert_runtime_contract_live(shared_home, profile_name=profile_name)
    subject_id = str(entry["subject_id"]).strip()

    probe = prober or probe_token
    verdict, scopes = probe(cleaned, host=host)

    if verdict == INVALID_TOKEN:
        raise TokenRejected(
            "GitLab 不认这个 token（可能复制少了字符，或者已经被撤销）。请重新复制一次。"
        )
    if verdict == PROBE_GIT_ONLY:
        raise TokenRejected(
            "这个 token 没有任何接口权限，装上也用不了。"
            f"请重建一个，名字填 {HERMES_TOKEN_NAME}："
            "只读档勾 read_api + read_repository，可写档勾 api + write_repository。"
        )
    if verdict == PROBE_NOT_FOUND:
        raise TokenRejected(
            f"在你的 GitLab 账号里找不到名字叫 {HERMES_TOKEN_NAME} 的有效 token。"
            f"请把 token 的名字改成 {HERMES_TOKEN_NAME}（或重建一个这样命名的），我们靠名字确认它的权限。"
        )
    if verdict == PROBE_AMBIGUOUS:
        raise TokenRejected(
            f"你有多个名字叫 {HERMES_TOKEN_NAME} 的有效 token，我无法确认是哪一个。"
            "请在 GitLab 里撤销多余的，只留这一个。"
        )
    if verdict != PROBE_OK:
        raise TokenRejected(
            "暂时无法校验这个 token（连不上 GitLab 或校验超时），没有入库，请稍后重试。"
        )

    granted = {s.strip() for s in scopes}
    # `api` implies read access, so it satisfies a read_api requirement.
    effective = set(granted)
    if "api" in granted:
        effective.add("read_api")
    if "write_repository" in granted:
        effective.add("read_repository")

    missing = [s for s in _REQUIRED_SCOPES[tier] if s not in effective]
    if missing:
        raise TokenRejected(
            f"这个 token 缺少「{'只读' if tier == TIER_READ else '可写'}」档需要的权限："
            f"{'、'.join(missing)}。请重建一个勾上这些 scope 的。"
        )
    if tier == TIER_READ and "api" in granted:
        raise TokenRejected(
            "你选的是「只读」，但这个 token 带着 api 权限——那是对所有项目的完整读写，"
            "比你以为的大。请重建一个只勾 read_api + read_repository 的，或改选「可写」。"
        )

    # Retire the legacy file FIRST: if this fails the user stays exactly as they
    # were, instead of ending up with a personal env token and a global on disk.
    retired = retire_legacy_token_file(shared_home, profile_name=profile_name, entry=entry)

    store = CredentialStore(db_path or shared_home / "multitenancy.db")
    try:
        store.put_credential(
            profile_name=profile_name,
            subject_id=subject_id,
            provider="gitlab",
            secret_kind="token",
            payload={"token": cleaned},
            scopes=sorted(granted) + [SCOPE_BINDING_UNVERIFIED],
            expires_at=expires_at,
        )
    finally:
        store.close()
    return {
        "stored": True,
        "profile_name": profile_name,
        "expires_at": expires_at,
        "tier": tier,
        "scopes": sorted(granted),
        "scope_binding_verified": False,
        "legacy_file_retired": retired,
    }


def _demo() -> None:
    """Self-check: every gate rejects, a matching token stores. No network."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        os.environ.setdefault("HERMES_MULTITENANCY_CREDENTIAL_KEY", "demo-key")
        home = Path(tmp)
        (home / "profiles" / "alice").mkdir(parents=True)

        def write_config(*, self_lane=True, env=True, profiles="[alice]"):
            lines = [
                "credentials:",
                "  - subject_id: kep-prd-skills",
                "    provider: gitlab",
                "    secret_kind: token",
                "    target: workspace/credentials/gitlab.token",
            ]
            if self_lane:
                lines.append("    vault_profile: __self__")
            if env:
                lines.append("    env: GITLAB_TOKEN")
            lines.append(f"    profiles: {profiles}")
            (home / "credential-materialization.yaml").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )

        write_config()
        assert gitlab_subject_id(home) == "kep-prd-skills"
        assert assert_runtime_contract_live(home, profile_name="alice")["subject_id"] == (
            "kep-prd-skills"
        )

        for bad in ("", "not-a-date", "2000-01-01"):
            try:
                parse_expiry(bad)
            except TokenRejected as exc:
                assert not bad.strip() or bad not in exc.reason
            else:  # pragma: no cover
                raise AssertionError(f"expiry {bad!r} should have been rejected")

        common = dict(
            profile_name="alice", token="t", expires_on="2099-01-01",
            tier=TIER_READ, shared_home=home,
        )

        # Every non-OK lookup outcome must refuse.
        for status in (INVALID_TOKEN, UNDETERMINED, PROBE_GIT_ONLY,
                       PROBE_NOT_FOUND, PROBE_AMBIGUOUS):
            try:
                submit_personal_token(**common, prober=lambda *a, _s=status, **k: (_s, []))
            except TokenRejected:
                pass
            else:  # pragma: no cover
                raise AssertionError(f"{status} should have been rejected")

        # Scope/tier mismatches, both directions.
        for scopes, why in (
            (["api", "write_repository"], "read tier given an api token"),
            (["read_api"], "missing read_repository"),
            ([], "no scopes at all"),
        ):
            try:
                submit_personal_token(**common, prober=lambda *a, _s=scopes, **k: (PROBE_OK, _s))
            except TokenRejected:
                pass
            else:  # pragma: no cover
                raise AssertionError(f"should have been rejected: {why}")
        try:
            submit_personal_token(
                **{**common, "tier": TIER_WRITE},
                prober=lambda *a, **k: (PROBE_OK, ["read_api", "read_repository"]),
            )
        except TokenRejected:
            pass
        else:  # pragma: no cover
            raise AssertionError("write tier given a read-only token should be rejected")

        # An incomplete runtime contract must refuse in every shape.
        for kwargs in ({"self_lane": False}, {"env": False}, {"profiles": "[bob]"}):
            write_config(**kwargs)
            try:
                submit_personal_token(
                    **common,
                    prober=lambda *a, **k: (PROBE_OK, ["read_api", "read_repository"]),
                )
            except TokenRejected:
                pass
            else:  # pragma: no cover
                raise AssertionError(f"incomplete contract {kwargs} should have been rejected")
        write_config()

        # Happy path, with a legacy file present: it must be gone afterwards.
        legacy = home / "profiles" / "alice" / "workspace" / "credentials"
        legacy.mkdir(parents=True)
        (legacy / "gitlab.token").write_text("global-token\n", encoding="utf-8")
        result = submit_personal_token(
            **common, prober=lambda *a, **k: (PROBE_OK, ["read_api", "read_repository"])
        )
        assert result["stored"] and result["tier"] == TIER_READ
        assert result["scopes"] == ["read_api", "read_repository"]
        assert result["legacy_file_retired"] is True
        assert not (legacy / "gitlab.token").exists()

        # Name-based lookup: only the active row matching HERMES_TOKEN_NAME counts.
        def _rows(payload):
            body = json.dumps(payload).encode()

            def _open(req, timeout=None):
                class _R:
                    status = 200

                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *a):
                        return False

                    def read(self_inner):
                        return body

                return _R()

            return _open

        assert probe_token("t", opener=_rows(
            [{"name": "hermes", "active": True, "scopes": ["api"]}]
        )) == (PROBE_OK, ["api"])
        assert probe_token("t", opener=_rows(
            [{"name": "other", "active": True, "scopes": ["api"]}]
        )) == (PROBE_NOT_FOUND, [])
        assert probe_token("t", opener=_rows(
            [{"name": "hermes", "active": False, "scopes": ["api"]}]
        )) == (PROBE_NOT_FOUND, [])
        assert probe_token("t", opener=_rows([
            {"name": "hermes", "active": True, "scopes": ["api"]},
            {"name": "hermes", "active": True, "scopes": ["read_api"]},
        ])) == (PROBE_AMBIGUOUS, [])
        print("gitlab_token_intake self-check OK")


if __name__ == "__main__":
    _demo()
