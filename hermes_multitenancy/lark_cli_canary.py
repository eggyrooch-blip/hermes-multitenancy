"""Read-only lark-cli canary readiness checks."""
from __future__ import annotations

import argparse
import json
import os
import sys
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
    db_path = shared_home / "multitenancy.db"
    if db_path.exists():
        if not (
            os.getenv("HERMES_MULTITENANCY_CREDENTIAL_KEY")
            or os.getenv("HERMES_CREDENTIAL_KEY")
        ):
            checks.append(
                {
                    "name": "credential_vault_key",
                    "ok": False,
                    "status": "missing",
                    "reason": "credential encryption key is required",
                }
            )
            missing = [check["name"] for check in checks if not check.get("ok")]
            return {
                "ready": False,
                "shared_home": str(shared_home),
                "profile_name": profile_name,
                "open_id": open_id,
                "checks": checks,
                "missing": missing,
                "secret_free": True,
            }
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
                checks.append(
                    {
                        "name": "feishu_app_credential",
                        "ok": app_ok,
                        "status": app_status["status"],
                        "app_id_present": bool(app_ok),
                    }
                )

                uat_status = store.get_status(
                    profile_name=profile_name,
                    subject_id=open_id,
                    provider="feishu",
                    secret_kind="uat",
                )
                uat_ok = uat_status["status"] == "valid"
                checks.append(
                    {
                        "name": "user_uat_credential",
                        "ok": uat_ok,
                        "status": uat_status["status"],
                        "profile_name": profile_name,
                        "subject_id": open_id,
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
        checks.extend(
            [
                {"name": "feishu_app_credential", "ok": False, "status": "missing_db"},
                {
                    "name": "user_uat_credential",
                    "ok": False,
                    "status": "missing_db",
                    "profile_name": profile_name,
                    "subject_id": open_id,
                },
            ]
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
    if not argv or argv[0] not in {"preflight", "import-legacy-uat", "import-app-config"}:
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
