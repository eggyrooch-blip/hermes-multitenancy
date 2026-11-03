#!/usr/bin/env python3
"""Freeze WorkBuddy's official CLI connector lifecycle without executing it."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import shlex
import tarfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SOURCE_URL = (
    "https://static.workbuddy.cn/connectors-config-v2/connectors-config.zip"
    "?versionId=MTg0NDQ5NTU4MjExNzgxMTM2OTk"
)
SOURCE_SHA256 = "6e3e91c351d65a05999977917b7e87e53ed8e56db4d7a482b9dcc42ed1ce2bfa"
PACKAGES = {
    "ailit": "@co-ailit/ailit-cli",
    "awesun": "@aweray/awesun-cli",
    "beisen-cli": "beisen-cli",
    "chuangkit": "@chuangkit-labs/agent-cli@0.1.3",
    "cnb-api": "@cnbcool/cnb-cli",
    "databuddy": "databuddycli",
    "designkit-buddy-cli": "meitu-designkit-cli",
    "dingtalk": "dingtalk-workspace-cli",
    "feishu": "@larksuite/cli",
    "ihr-cli": "@ihr360cli/ihr-cli",
    "lemonclaw": "@lemonbeijing/lemonclaw-cli",
    "lovrabet-cli": "@lovrabet/lovrabet-cli",
    "miaoda": "miaoda-cli",
    "tencentads": "tencentads-cli",
    "textin-xparse": "xparse-cli",
    "tmeet": "@tencentcloud/tmeet",
    "wecom": "@wecom/cli",
    "yunke-cli": "@yunkeai/omni-cli",
    "zsxq": "zsxq-cli",
}
SETUP = {
    "databuddy": ["databuddycli", "init", "--host", "workbuddy", "--tag", "latest"],
    "textin-xparse": [
        "xparse-cli", "--profile", "workbuddy", "config", "set", "base_url", "https://api.textin.com",
    ],
}
EMBEDDED_NPM = {
    "tc-chengxin": "connectors/tc-chengxin/cli/tc-chengxin-cli.tgz",
}
PINNED_ARCHIVES = {
    "77ircloud": {
        "archive_url": "https://oss-openclaw.77ircloud.com/cli_tools/workbuddy/ircloud-cli-workbuddy/1.0.1/ircloud-cli-workbuddy-1.0.1-linux-amd64.tar.gz",
        "archive_sha256": "09739e7ebf5030dcb3f0ff4ef65f5dab3bc1546117d0e21d75a5afa74c20db94",
        "bin": {"ircloud-cli": "ircloud-cli-workbuddy"},
    },
    "wps-knowledgebase": {
        "archive_url": "https://personal.wpscdn.cn/app/kwiki-cli/beta/v2.0.3-20260803075737/kwiki-cli-linux-amd64-2.0.3.tgz",
        "archive_sha256": "f698b0f24d667c92a57e090c13ca949fde7b8d12b6105dac53f6c37f3c016088",
        "bin": {"kwiki-cli": "package/scripts/run.js"},
    },
}
_SAFE_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_SAFE_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


def _linux(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("linux") or "")
    return str(value or "")


def _argv(value: Any) -> list[str]:
    command = _linux(value).strip()
    if command.startswith("env -u NODE_OPTIONS "):
        command = command.removeprefix("env -u NODE_OPTIONS ")
    if not command or any(token in command for token in ("&&", "||", "|", ";", ">", "<", "$", "~")):
        return []
    return shlex.split(command)


def _auth_steps(value: Any) -> list[list[str]]:
    steps = value if isinstance(value, list) else [value]
    return [
        args for step in steps
        if (args := _argv(step.get("command") if isinstance(step, dict) and "command" in step else step))
    ]


def generate(archive_path: Path) -> list[dict[str, Any]]:
    payload = archive_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != SOURCE_SHA256:
        raise ValueError("WorkBuddy marketplace archive digest mismatch")
    with zipfile.ZipFile(archive_path) as archive:
        catalog = json.loads(archive.read(".codebuddy-connector/connectors.json"))
        ids = sorted(str(row["id"]) for row in catalog["connectors"] if row.get("type") == "cli")
        if len(ids) != 27:
            raise ValueError(f"expected 27 official CLI connectors, got {len(ids)}")
        rows = []
        for connector_id in ids:
            document = json.loads(archive.read(f"connectors/{connector_id}/cli.json"))
            static_env = {
                str(key): str(value).replace("$HOME/", "/home/connector/")
                for key, value in (document.get("env") or {}).items()
                if _SAFE_ENV.fullmatch(str(key)) and value not in {None, ""}
            }
            auth_domains = sorted({
                domain
                for item in ([document.get("auth")] if not isinstance(document.get("auth"), list) else document["auth"])
                for domain in [str((item or {}).get("authUrlDomain") or document.get("authUrlDomain") or "").lower()]
                if _SAFE_DOMAIN.fullmatch(domain)
            })
            if not auth_domains:
                auth_domains = sorted({
                    str(urlparse(value).hostname or "").lower()
                    for key, value in static_env.items()
                    if "AUTH" in key and str(value).startswith("https://")
                    and _SAFE_DOMAIN.fullmatch(str(urlparse(value).hostname or "").lower())
                })
            package = PACKAGES.get(connector_id)
            embedded_resolution = None
            if member := EMBEDDED_NPM.get(connector_id):
                payload = archive.read(member)
                with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as package_archive:
                    package_json = json.loads(package_archive.extractfile("package/package.json").read())
                embedded_resolution = {
                    "state": "embedded",
                    "package": str(package_json["name"]),
                    "version": str(package_json["version"]),
                    "bin": {str(key): str(value) for key, value in package_json["bin"].items()},
                    "tarball_sha256": hashlib.sha256(payload).hexdigest(),
                    "tarball_base64": base64.b64encode(payload).decode(),
                }
                embedded_resolution["resolution_fingerprint"] = hashlib.sha256(
                    json.dumps({key: value for key, value in embedded_resolution.items()
                                if key != "tarball_base64"}, sort_keys=True).encode()
                ).hexdigest()
            pinned_resolution = None
            if pinned := PINNED_ARCHIVES.get(connector_id):
                pinned_resolution = {"state": "pinned_archive", **pinned}
                pinned_resolution["resolution_fingerprint"] = hashlib.sha256(
                    json.dumps(pinned_resolution, sort_keys=True).encode()
                ).hexdigest()
            rows.append({
                "row_key": f"workbuddy:{connector_id}",
                "catalog_id": connector_id,
                "state": "npm_resolvable" if package else (
                    "embedded_npm" if embedded_resolution else (
                        "pinned_archive" if pinned_resolution else "adapter_required"
                    )
                ),
                "package": package,
                "command": "npx" if package else "",
                "args": ["-y", package] if package else [],
                "auth_steps": _auth_steps(document.get("auth")),
                "status_args": _argv(document.get("status")),
                "logout_args": _argv(document.get("unAuth")),
                "setup_args": SETUP.get(connector_id, []),
                "static_env": static_env,
                "status_match": str(document.get("statusMatch") or ""),
                "status_match_json": document.get("statusMatchJson") or {},
                "auth_domains": auth_domains,
                "auth_wait_for_exit": document.get("authWaitForExit") is True,
                "min_version": str((document.get("versionCheck") or {}).get("minVersion") or ""),
                "source_url": SOURCE_URL,
                "source_sha256": SOURCE_SHA256,
                **({"package_resolution": embedded_resolution or pinned_resolution}
                   if embedded_resolution or pinned_resolution else {}),
            })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = generate(args.archive)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
