"""Principal-scoped authorization for private final-answer sources."""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from .source_envelope import normalize_tool_source_refs


PrivateSourceVerifier = Callable[[Path, str, str], str | None]


def _document_base_url() -> str:
    configured = str(os.environ.get("HERMES_FEISHU_DOCUMENT_BASE_URL") or "").rstrip("/")
    try:
        parsed = urlsplit(configured)
    except ValueError:
        parsed = None
    return configured if parsed and parsed.scheme == "https" and parsed.hostname else "https://feishu.cn"


def _live_lark_doc_target(profile_home: Path, owner_open_id: str, locator: str) -> str | None:
    from .agent_real import _lark_cli_auth_broker_scope

    with _lark_cli_auth_broker_scope(profile_home, owner_open_id) as broker_env:
        binary = broker_env.get("HERMES_LARK_CLI_BIN")
        if not binary or broker_env.get("LARKSUITE_CLI_DEFAULT_AS") != "user":
            return None
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(profile_home / "home"),
            "HERMES_HOME": str(profile_home),
            "WORKSPACE": str(profile_home / "workspace"),
            **broker_env,
        }
        completed = subprocess.run(
            [binary, "api", "GET", f"/open-apis/docx/v1/documents/{locator}", "--as", "user", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
            check=False,
        )
        if completed.returncode != 0:
            return None
        try:
            payload = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, Mapping) or payload.get("ok") is False:
            return None
        data = payload.get("data", payload)
        if not isinstance(data, Mapping):
            return None
        document = data.get("document", data)
        if not isinstance(document, Mapping):
            return None
        returned_id = str(document.get("document_id") or data.get("document_id") or "").strip()
        if returned_id != locator:
            return None
        return f"{_document_base_url()}/docx/{locator}"


def authorize_private_source_refs(
    profile_home: Path,
    owner_open_id: str,
    refs: Iterable[Mapping[str, object]],
    *,
    verify: PrivateSourceVerifier = _live_lark_doc_target,
) -> list[dict[str, str]]:
    """Return only private refs readable by the current live user identity."""
    owner = str(owner_open_id or "").strip()
    if not owner:
        return []
    normalized = normalize_tool_source_refs({"source_refs": list(refs)}, profile_home)
    authorized: list[dict[str, str]] = []
    for ref in normalized:
        if ref["type"] != "lark_doc":
            continue
        target = verify(profile_home, owner, ref["locator"])
        if target:
            authorized.append({
                "id": ref["id"],
                "type": "lark_doc",
                "label": ref["label"],
                "target": target,
            })
    return authorized
