"""Actor-scoped previews for Feishu/Lark links without fetching tenant HTML."""
from __future__ import annotations

import json
import os
import posixpath
import subprocess
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DocumentInspector = Callable[[Path, str, str], Mapping[str, str] | None]

_ROOT_DOMAINS = ("feishu.cn", "larksuite.com")
_OPEN_PLATFORM_HOSTS = frozenset({"open.feishu.cn", "open.larksuite.com"})
_MAX_PUBLIC_HTML_BYTES = 1_000_000
_TYPE_LABELS = {
    "open_platform": "开放平台文档",
    "wiki": "知识库文档",
    "docx": "飞书文档",
    "doc": "飞书文档",
    "sheet": "飞书表格",
    "base": "多维表格",
    "slides": "飞书幻灯片",
    "folder": "云盘文件夹",
    "file": "云盘文件",
    "task": "飞书任务",
    "calendar": "飞书日历",
    "approval": "飞书审批",
    "meeting": "飞书会议",
    "minutes": "飞书妙记",
    "im": "飞书消息",
    "meegle": "飞书项目",
    "feishu": "飞书链接",
}
_DOCUMENT_KINDS = frozenset({"wiki", "docx", "doc", "sheet", "base", "slides", "folder", "file"})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og_title = ""
        self.title_parts: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "meta" and values.get("property", "").lower() == "og:title":
            self.og_title = values.get("content", "")
        elif tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)


def _inspect_public_page(url: str) -> str | None:
    request = Request(url, headers={"Accept": "text/html", "User-Agent": "Hermes-Link-Preview/1.0"})
    try:
        with build_opener(_NoRedirect).open(request, timeout=2) as response:
            if response.headers.get_content_type() != "text/html":
                return None
            payload = response.read(_MAX_PUBLIC_HTML_BYTES + 1)
            if len(payload) > _MAX_PUBLIC_HTML_BYTES:
                return None
            charset = response.headers.get_content_charset() or "utf-8"
    except (OSError, TimeoutError, ValueError):
        return None
    parser = _TitleParser()
    try:
        parser.feed(payload.decode(charset, errors="replace"))
    except (LookupError, ValueError):
        return None
    title = parser.og_title or "".join(parser.title_parts)
    return " ".join(title.split())[:500] or None


def _kind_for_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("unsupported Feishu link") from exc
    host = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not any(host == root or host.endswith(f".{root}") for root in _ROOT_DOMAINS)
    ):
        raise ValueError("unsupported Feishu link")
    path = "/" + posixpath.normpath(unquote(parsed.path)).lstrip("/").lower()
    if host in _OPEN_PLATFORM_HOSTS and path.startswith("/document/"):
        return "open_platform"
    if host == "project.feishu.cn" or "/meego/" in path:
        return "meegle"
    prefixes = (
        ("/wiki/", "wiki"),
        ("/docx/", "docx"),
        ("/doc/", "doc"),
        ("/sheets/", "sheet"),
        ("/base/", "base"),
        ("/bitable/", "base"),
        ("/slides/", "slides"),
        ("/drive/folder/", "folder"),
        ("/file/", "file"),
        ("/client/todo/task", "task"),
        ("/task/", "task"),
        ("/calendar/", "calendar"),
        ("/client/calendar/", "calendar"),
        ("/approval/", "approval"),
        ("/client/approval/", "approval"),
        ("/minutes/", "minutes"),
        ("/vc/", "meeting"),
        ("/video/", "meeting"),
        ("/message/", "im"),
        ("/client/chat/", "im"),
    )
    return next((kind for prefix, kind in prefixes if path.startswith(prefix)), "feishu")


def _inspect_document(profile_home: Path, owner: str, url: str) -> Mapping[str, str] | None:
    from .agent_real import _lark_cli_auth_broker_scope

    with _lark_cli_auth_broker_scope(profile_home, owner) as broker_env:
        binary = broker_env.get("HERMES_LARK_CLI_BIN")
        if not binary or broker_env.get("LARKSUITE_CLI_DEFAULT_AS") != "user":
            raise RuntimeError("link preview credential unavailable")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(profile_home / "home"),
            "HERMES_HOME": str(profile_home),
            "WORKSPACE": str(profile_home / "workspace"),
            **broker_env,
        }
        completed = subprocess.run(
            [binary, "drive", "+inspect", "--url", url, "--as", "user", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=2,
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
    if isinstance(data, Mapping) and isinstance(data.get("data"), Mapping):
        data = data["data"]
    if not isinstance(data, Mapping):
        return None
    title = str(data.get("title") or data.get("name") or "").strip()
    if not title:
        return None
    return {
        "title": title[:500],
        "type": str(data.get("type") or data.get("doc_type") or data.get("obj_type") or "").strip(),
    }


def resolve_feishu_link_previews(
    profile_home: Path,
    owner: str,
    urls: list[str],
    *,
    inspect_document: DocumentInspector = _inspect_document,
    inspect_public_page: Callable[[str], str | None] = _inspect_public_page,
) -> list[dict[str, str]]:
    previews: list[dict[str, str]] = []
    document_rows: list[tuple[int, str]] = []
    public_page_rows: list[tuple[int, str]] = []
    for url in urls:
        kind = _kind_for_url(url)
        preview = {
            "kind": kind,
            "title": "",
            "type_label": _TYPE_LABELS[kind],
            "url": url,
            "status": "generic",
        }
        if kind in _DOCUMENT_KINDS:
            document_rows.append((len(previews), url))
        elif kind == "open_platform":
            public_page_rows.append((len(previews), url))
        previews.append(preview)
    if document_rows or public_page_rows:
        # The HTTP boundary caps this at ten; parallel calls keep the whole paste
        # inside the same three-second budget as a single upstream inspection.
        with ThreadPoolExecutor(max_workers=len(document_rows) + len(public_page_rows)) as pool:
            document_futures = [pool.submit(inspect_document, profile_home, owner, url) for _, url in document_rows]
            public_page_futures = [pool.submit(inspect_public_page, url) for _, url in public_page_rows]
            for (index, _url), future in zip(document_rows, document_futures):
                try:
                    metadata = future.result()
                except Exception:
                    metadata = None
                if metadata and str(metadata.get("title") or "").strip():
                    previews[index]["title"] = str(metadata["title"]).strip()[:500]
                    previews[index]["status"] = "resolved"
                else:
                    previews[index]["status"] = "forbidden"
            for (index, _url), future in zip(public_page_rows, public_page_futures):
                try:
                    title = future.result()
                except Exception:
                    title = None
                if title:
                    previews[index]["title"] = title
                    previews[index]["status"] = "resolved"
    return previews
