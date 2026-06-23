"""Read-only lark-cli canary readiness checks."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .credentials import CredentialStore


def _normalize_scopes(data: dict[str, Any]) -> list[str]:
    scopes = data.get("scopes") or data.get("scope") or []
    if isinstance(scopes, str):
        return sorted({part for part in scopes.replace(",", " ").split() if part})
    if isinstance(scopes, list):
        return sorted({str(part).strip() for part in scopes if str(part).strip()})
    return []


def _credential_key_available() -> bool:
    return bool(os.getenv("HERMES_MULTITENANCY_CREDENTIAL_KEY") or os.getenv("HERMES_CREDENTIAL_KEY"))


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _uat_expires_at(payload: dict[str, Any]) -> int:
    return _as_int(
        payload.get("expires_at")
        or payload.get("expire_at")
        or payload.get("access_token_expires_at")
    )


def _public_app_id_source(shared_home: Path, profile_name: str) -> tuple[bool, str]:
    if str(os.getenv("HERMES_LARK_CLI_APP_ID") or "").strip():
        return True, "env_or_config"

    config_path = Path(shared_home) / "config.yaml"
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        loaded = {}
    extra = (((loaded.get("platforms") or {}).get("feishu") or {}).get("extra") or {})
    if str(extra.get("app_id") or extra.get("FEISHU_APP_ID") or "").strip():
        return True, "env_or_config"

    uat_dir = Path(shared_home) / "profiles" / profile_name / "feishu_uat"
    try:
        candidates = sorted(
            (path for path in uat_dir.glob("*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return False, ""
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and str(payload.get("app_id") or payload.get("client_id") or "").strip():
            return True, "profile_feishu_uat_json"
    return False, ""


def _profile_uat_json_status(shared_home: Path, profile_name: str, open_id: str) -> tuple[bool, int | None, str]:
    path = Path(shared_home) / "profiles" / profile_name / "feishu_uat" / f"{open_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, None, "missing"
    if not isinstance(payload, dict) or not str(payload.get("access_token") or "").strip():
        return False, None, "missing"
    expires_at = _uat_expires_at(payload)
    if expires_at and expires_at <= int(time.time() * 1000) + 60_000:
        return False, expires_at, "expired"
    return True, expires_at or None, "valid"


def import_legacy_uat_to_vault(
    *,
    shared_home: Path,
    profile_name: str,
    open_id: str,
    legacy_uat_dir: Path | None = None,
) -> dict[str, Any]:
    """Import one legacy Feishu UAT JSON into the credential vault."""
    shared_home = shared_home.expanduser()
    profile_name = str(profile_name or "").strip()
    open_id = str(open_id or "").strip()
    legacy_uat_dir = (legacy_uat_dir or (shared_home / "feishu_uat")).expanduser()
    token_path = legacy_uat_dir / f"{open_id}.json"
    if not token_path.is_file():
        return {
            "imported": False,
            "reason": "legacy_uat_json_missing",
            "profile_name": profile_name,
            "open_id": open_id,
            "path": str(token_path),
            "secret_free": True,
        }
    data = json.loads(token_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("access_token"):
        return {
            "imported": False,
            "reason": "legacy_uat_json_invalid",
            "profile_name": profile_name,
            "open_id": open_id,
            "path": str(token_path),
            "secret_free": True,
        }
    scopes = _normalize_scopes(data)
    expires_at = data.get("expires_at")
    store = CredentialStore(shared_home / "multitenancy.db")
    try:
        store.put_credential(
            profile_name=profile_name,
            subject_id=open_id,
            provider="feishu",
            secret_kind="uat",
            payload=data,
            scopes=scopes,
            expires_at=int(expires_at) if expires_at else None,
        )
    finally:
        store.close()
    return {
        "imported": True,
        "profile_name": profile_name,
        "open_id": open_id,
        "path": str(token_path),
        "scope_count": len(scopes),
        "expires_at": int(expires_at) if expires_at else None,
        "secret_free": True,
    }


def import_feishu_app_config_to_vault(
    *,
    shared_home: Path,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Import Feishu app credential from Hermes config into the credential vault."""
    shared_home = shared_home.expanduser()
    config_path = (config_path or (shared_home / "config.yaml")).expanduser()
    if not config_path.is_file():
        return {
            "imported": False,
            "reason": "config_missing",
            "path": str(config_path),
            "secret_free": True,
        }
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    extra = (((loaded.get("platforms") or {}).get("feishu") or {}).get("extra") or {})
    app_id = str(extra.get("app_id") or extra.get("FEISHU_APP_ID") or "").strip()
    app_secret = str(extra.get("app_secret") or extra.get("FEISHU_APP_SECRET") or "").strip()
    domain = str(extra.get("domain") or extra.get("FEISHU_DOMAIN") or "feishu").strip().lower() or "feishu"
    if not app_id or not app_secret:
        return {
            "imported": False,
            "reason": "app_credential_missing",
            "path": str(config_path),
            "app_id_present": bool(app_id),
            "app_secret_present": bool(app_secret),
            "secret_free": True,
        }
    store = CredentialStore(shared_home / "multitenancy.db")
    try:
        store.put_credential(
            profile_name="__global__",
            subject_id="feishu_app",
            provider="feishu",
            secret_kind="app",
            payload={"app_id": app_id, "app_secret": app_secret, "domain": domain},
        )
    finally:
        store.close()
    return {
        "imported": True,
        "profile_name": "__global__",
        "subject_id": "feishu_app",
        "provider": "feishu",
        "secret_kind": "app",
        "app_id": app_id,
        "domain": domain,
        "path": str(config_path),
        "secret_free": True,
    }


def lark_cli_canary_preflight(
    *,
    shared_home: Path,
    profile_name: str,
    open_id: str,
    binary_path: Path | None = None,
) -> dict[str, Any]:
    """Return secret-free readiness for the user_info lark-cli canary."""
    shared_home = shared_home.expanduser()
    profile_name = str(profile_name or "").strip()
    open_id = str(open_id or "").strip()
    binary_path = (binary_path or (shared_home / "bin" / "lark-cli-authsidecar")).expanduser()
    checks: list[dict[str, Any]] = []

    binary_ok = binary_path.is_file()
    checks.append(
        {
            "name": "authsidecar_binary",
            "ok": binary_ok,
            "path": str(binary_path),
        }
    )

    app_ok = False
    uat_ok = False
    key_available = _credential_key_available()
    fallback_app_ok, fallback_app_source = _public_app_id_source(shared_home, profile_name)
    local_uat_ok, local_uat_exp, local_uat_status = _profile_uat_json_status(shared_home, profile_name, open_id)
    db_path = shared_home / "multitenancy.db"
    if db_path.exists():
        try:
            store = CredentialStore(db_path)
            try:
                app_status = store.get_status(
                    profile_name="__global__",
                    subject_id="feishu_app",
                    provider="feishu",
                    secret_kind="app",
                )
                app_payload = None
                if app_status["status"] == "valid":
                    try:
                        app_payload = store.get_secret_for_runtime(
                            profile_name="__global__",
                            subject_id="feishu_app",
                            provider="feishu",
                            secret_kind="app",
                        )
                    except Exception:
                        app_payload = None
                app_ok = app_status["status"] == "valid" and bool(
                    (app_payload or {}).get("app_id") or (app_payload or {}).get("FEISHU_APP_ID")
                )
                app_source = "multitenancy_db" if app_ok else ""
                if not app_ok and fallback_app_ok:
                    app_ok = True
                    app_source = fallback_app_source
                checks.append(
                    {
                        "name": "feishu_app_credential",
                        "ok": app_ok,
                        "status": "valid" if app_ok else app_status["status"],
                        "app_id_present": bool(app_ok),
                        "source": app_source or "multitenancy_db",
                    }
                )

                uat_status = store.get_status(
                    profile_name=profile_name,
                    subject_id=open_id,
                    provider="feishu",
                    secret_kind="uat",
                )
                uat_ok = uat_status["status"] == "valid" and key_available
                uat_source = "multitenancy_db" if uat_ok else ""
                if local_uat_ok:
                    uat_ok = True
                    uat_source = "profile_feishu_uat_json"
                elif local_uat_status != "missing":
                    uat_source = "profile_feishu_uat_json"
                checks.append(
                    {
                        "name": "user_uat_credential",
                        "ok": uat_ok,
                        "status": "valid" if uat_ok else (local_uat_status if uat_source == "profile_feishu_uat_json" else uat_status["status"]),
                        "profile_name": profile_name,
                        "subject_id": open_id,
                        "expires_at": local_uat_exp or uat_status.get("expires_at"),
                        "source": uat_source or "multitenancy_db",
                    }
                )
            finally:
                store.close()
        except Exception as exc:
            checks.append(
                {
                    "name": "credential_vault",
                    "ok": False,
                    "status": "unreadable",
                    "reason": exc.__class__.__name__,
                }
            )
    else:
        app_ok = fallback_app_ok
        uat_ok = local_uat_ok
        checks.extend(
            [
                {
                    "name": "feishu_app_credential",
                    "ok": app_ok,
                    "status": "valid" if app_ok else "missing_db",
                    "app_id_present": bool(app_ok),
                    "source": fallback_app_source or "env_or_config",
                },
                    {
                        "name": "user_uat_credential",
                        "ok": uat_ok,
                        "status": "valid" if uat_ok else (local_uat_status if local_uat_status != "missing" else "missing_db"),
                        "profile_name": profile_name,
                        "subject_id": open_id,
                        "expires_at": local_uat_exp,
                        "source": "profile_feishu_uat_json" if local_uat_status != "missing" else "multitenancy_db",
                    },
            ]
        )
    if db_path.exists() and not key_available and not (app_ok and uat_ok):
        checks.append(
            {
                "name": "credential_vault_key",
                "ok": False,
                "status": "missing",
                "reason": "credential encryption key is required",
            }
        )

    missing = [check["name"] for check in checks if not check.get("ok")]
    ready = not missing
    result: dict[str, Any] = {
        "ready": ready,
        "shared_home": str(shared_home),
        "profile_name": profile_name,
        "open_id": open_id,
        "checks": checks,
        "missing": missing,
        "secret_free": True,
    }
    if ready:
        result["canary"] = {
            "mode": "api",
            "argv": ["api", "GET", "/open-apis/authen/v1/user_info"],
            "identity": "user",
            "risk": "read",
            "reason": "lark-cli user_info canary",
        }
    return result


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in {"preflight", "import-legacy-uat", "import-app-config", "health"}:
        argv.insert(0, "preflight")
    parser = argparse.ArgumentParser(prog="lark-cli-canary-preflight")
    subparsers = parser.add_subparsers(dest="cmd")
    p_preflight = subparsers.add_parser("preflight")
    p_preflight.add_argument("--shared-home", type=Path, default=Path("~/.hermes"))
    p_preflight.add_argument("--profile", required=True)
    p_preflight.add_argument("--open-id", required=True)
    p_preflight.add_argument("--binary", type=Path, default=None)
    p_import = subparsers.add_parser("import-legacy-uat")
    p_import.add_argument("--shared-home", type=Path, default=Path("~/.hermes"))
    p_import.add_argument("--profile", required=True)
    p_import.add_argument("--open-id", required=True)
    p_import.add_argument("--legacy-uat-dir", type=Path, default=None)
    p_import_app = subparsers.add_parser("import-app-config")
    p_import_app.add_argument("--shared-home", type=Path, default=Path("~/.hermes"))
    p_import_app.add_argument("--config", type=Path, default=None)
    p_health = subparsers.add_parser("health")
    p_health.add_argument("--shared-home", type=Path, default=Path("~/.hermes"))
    p_health.add_argument("--profile-home", type=Path, default=None)
    p_health.add_argument("--router-profile-home", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.cmd == "import-legacy-uat":
        result = import_legacy_uat_to_vault(
            shared_home=args.shared_home,
            profile_name=args.profile,
            open_id=args.open_id,
            legacy_uat_dir=args.legacy_uat_dir,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["imported"] else 2
    if args.cmd == "import-app-config":
        result = import_feishu_app_config_to_vault(
            shared_home=args.shared_home,
            config_path=args.config,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["imported"] else 2
    if args.cmd == "health":
        from .upstream_health import upstream_capability_health

        result = upstream_capability_health(
            shared_home=args.shared_home,
            profile_home=args.profile_home,
            router_profile_home=args.router_profile_home,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["ready"] else 2
    result = lark_cli_canary_preflight(
        shared_home=args.shared_home,
        profile_name=args.profile,
        open_id=args.open_id,
        binary_path=getattr(args, "binary", None),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
