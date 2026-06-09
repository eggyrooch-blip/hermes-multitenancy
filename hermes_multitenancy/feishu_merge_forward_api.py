"""Issue 5 — expand Feishu ``merge_forward`` by fetching sub-messages via API.

Real Feishu ``merge_forward`` push events carry ONLY metadata (a title) — the
forwarded sub-messages are NOT inline. They must be fetched with
``GET /open-apis/im/v1/messages/{message_id}`` which returns a flat
``data.items[]`` (each item has ``upper_message_id`` pointing at its parent
container). We rebuild the tree (openclaw-lark ``merge-forward.ts`` algorithm)
and render ``<forwarded_messages>``.

Two facts make this plugin-only and env-specific:

1. The current upstream runtime core has NO ``_expand_merge_forward`` /
   ``set_merge_forward_fetcher`` hook (owner's fork had it; the editable switch
   to upstream dropped it). So we do the whole expansion in the plugin.
2. A user's *private* forwarded messages are refused for the bot tenant token
   (``230002``/permission); they are only readable with THAT user's UAT. So we
   resolve the forwarder's UAT (owner's proven ``_fetch_sub_messages_blocking``)
   keyed on the sender's ``open_id``.

Patch sites (method-level, dispatched at call time, idempotent, fail-open):
- ``FeishuAdapter._process_inbound_message`` — captures the inbound
  ``sender_id.open_id`` onto the adapter (``_extract_message_content`` does not
  receive the sender).
- ``FeishuAdapter._extract_message_content`` — when the raw message_type is
  ``merge_forward``, replace the placeholder text with the fetched transcript.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

_HOOK_INSTALLED = False
_EXTRACT_FLAG = "_hermes_multitenancy_merge_forward_api_patched"
_PROCESS_FLAG = "_hermes_multitenancy_merge_forward_sender_capture_patched"
_SENDER_ATTR = "_mt_merge_forward_sender_open_id"

_MAX_ENTRIES = 60          # safety cap on rendered sub-messages
_MAX_DEPTH = 6             # nested merge_forward recursion cap

_FEISHU_API_HOSTS = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}


def _feishu_api_host() -> str:
    domain = str(os.getenv("FEISHU_DOMAIN") or "feishu").strip().lower()
    return _FEISHU_API_HOSTS.get(domain, _FEISHU_API_HOSTS["feishu"])


# --------------------------------------------------------------------------- #
# UAT token + API fetch  (verbatim from owner's proven merge_forward_hook.py)  #
# --------------------------------------------------------------------------- #
def _resolve_uat_token(sender_open_id: str) -> str:
    """Load (refreshing if needed) the forwarder's UAT access_token."""
    from .feishu_uat_auth import (  # local import: avoid import cost at register
        _load_best_uat_payload,
        _profile_name_for_open_id,
        refresh_uat_if_needed,
        resolve_shared_home,
    )

    if not sender_open_id:
        raise RuntimeError("merge_forward expansion requires sender_open_id")

    shared_home = resolve_shared_home()
    profile_name = _profile_name_for_open_id(shared_home, sender_open_id)

    payload = None
    try:
        payload = refresh_uat_if_needed(
            profile_name=profile_name, open_id=sender_open_id, shared_home=shared_home
        )
    except Exception as exc:  # refresh is best-effort; fall back to stored token
        logger.debug("[multitenancy] merge_forward UAT refresh failed (%s); using stored", exc)
    if not payload:
        payload = _load_best_uat_payload(shared_home, profile_name, sender_open_id)

    token = str((payload or {}).get("access_token") or "").strip()
    if not token:
        raise RuntimeError(f"no UAT access_token for open_id={sender_open_id[:8]}…")
    return token


def _fetch_sub_messages_blocking(message_id: str, sender_open_id: str) -> list[dict]:
    """Blocking UAT fetch of a merge_forward's flat sub-message items."""
    if not message_id:
        raise RuntimeError("merge_forward expansion requires message_id")
    token = _resolve_uat_token(sender_open_id)
    url = (
        f"{_feishu_api_host()}/open-apis/im/v1/messages/"
        f"{urllib.parse.quote(message_id, safe='')}?user_id_type=open_id"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (fixed host)
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("code") != 0:
        raise RuntimeError(f"im/v1/messages code={body.get('code')} msg={body.get('msg')}")
    return list((body.get("data") or {}).get("items") or [])


def _resolve_names_blocking(open_ids: list[str], sender_open_id: str) -> dict[str, str]:
    """Best-effort UAT contact lookup: open_id → display name. {} on failure."""
    ids = [oid for oid in dict.fromkeys(open_ids) if oid]
    if not ids:
        return {}
    try:
        token = _resolve_uat_token(sender_open_id)
        query = "&".join(f"user_ids={urllib.parse.quote(oid, safe='')}" for oid in ids)
        url = f"{_feishu_api_host()}/open-apis/contact/v3/users/batch?user_id_type=open_id&{query}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (fixed host)
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("code") != 0:
            return {}
        names: dict[str, str] = {}
        for item in (body.get("data") or {}).get("items") or []:
            oid = str(item.get("open_id") or "")
            name = str(item.get("name") or item.get("nickname") or "")
            if oid and name:
                names[oid] = name
        return names
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Tree build + render  (openclaw-lark merge-forward.ts algorithm)              #
# --------------------------------------------------------------------------- #
def _item_sender_open_id(item: dict) -> str:
    sender = item.get("sender") or {}
    return str(sender.get("id") or sender.get("open_id") or "")


def _item_text(item: dict) -> str:
    """Best-effort per-sub-message text, reusing core's normalize_feishu_message."""
    msg_type = str(item.get("msg_type") or "")
    body = item.get("body") or {}
    raw_content = body.get("content")
    if isinstance(raw_content, (dict, list)):
        raw_content = json.dumps(raw_content, ensure_ascii=False)
    raw_content = str(raw_content or "")
    try:
        from gateway.platforms.feishu import normalize_feishu_message  # type: ignore

        normalized = normalize_feishu_message(message_type=msg_type, raw_content=raw_content)
        text = str(getattr(normalized, "text_content", "") or "").strip()
        if text:
            return text
    except Exception:
        pass
    # Fallbacks for the common shapes.
    try:
        parsed = json.loads(raw_content) if raw_content else {}
    except Exception:
        parsed = {}
    if isinstance(parsed, dict):
        for key in ("text", "content", "title"):
            val = parsed.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return f"[{msg_type or 'message'}]"


def _build_children_map(items: list[dict], root_id: str) -> dict[str, list[dict]]:
    children: dict[str, list[dict]] = {}
    for it in items:
        parent = str(it.get("upper_message_id") or root_id)
        children.setdefault(parent, []).append(it)
    for kids in children.values():
        kids.sort(key=lambda x: int(str(x.get("create_time") or "0") or "0"))
    return children


def _format_create_time(item: dict) -> str:
    raw = str(item.get("create_time") or "").strip()
    if not raw:
        return ""
    try:
        import datetime

        ms = int(raw)
        # Feishu create_time is epoch milliseconds; render as +08:00 (Beijing).
        tz = datetime.timezone(datetime.timedelta(hours=8))
        return datetime.datetime.fromtimestamp(ms / 1000.0, tz=tz).isoformat(timespec="seconds")
    except Exception:
        return raw


def _render_sub_tree(
    parent_id: str,
    children: dict[str, list[dict]],
    names: dict[str, str],
    *,
    depth: int,
    counter: list[int],
    indent: str = "    ",
) -> list[str]:
    lines: list[str] = []
    if depth > _MAX_DEPTH:
        return lines
    for it in children.get(parent_id, []):
        if counter[0] >= _MAX_ENTRIES:
            lines.append(indent * depth + "… (更多消息已省略)")
            break
        counter[0] += 1
        oid = _item_sender_open_id(it)
        name = names.get(oid) or (oid[:8] + "…" if oid else "未知")
        ts = _format_create_time(it)
        prefix = f"[{ts}] {name}:" if ts else f"{name}:"
        body = _item_text(it)
        pad = indent * (depth + 1)
        lines.append(indent * depth + prefix)
        for bl in body.splitlines() or [""]:
            lines.append(pad + bl)
        mid = str(it.get("message_id") or "")
        if mid and children.get(mid):  # nested merge_forward — recurse, no extra API
            lines.extend(
                _render_sub_tree(mid, children, names, depth=depth + 1, counter=counter, indent=indent)
            )
    return lines


def _expand_merge_forward_blocking(message_id: str, sender_open_id: str) -> Optional[str]:
    """Fetch + render a merge_forward as <forwarded_messages>. None on failure/empty."""
    items = _fetch_sub_messages_blocking(message_id, sender_open_id)
    if not items:
        return None
    names = _resolve_names_blocking([_item_sender_open_id(it) for it in items], sender_open_id)
    children = _build_children_map(items, message_id)
    body_lines = _render_sub_tree(message_id, children, names, depth=0, counter=[0])
    if not body_lines:
        return None
    inner = "\n".join(body_lines)
    return f"<forwarded_messages>\n{inner}\n</forwarded_messages>"


async def _expand_merge_forward(message_id: str, sender_open_id: str) -> Optional[str]:
    return await asyncio.to_thread(_expand_merge_forward_blocking, message_id, sender_open_id)


# --------------------------------------------------------------------------- #
# Install                                                                      #
# --------------------------------------------------------------------------- #
def install_feishu_merge_forward_api_patch() -> None:
    """Idempotently patch FeishuAdapter for API-backed merge_forward expansion."""
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    try:
        from gateway.platforms.feishu import FeishuAdapter  # type: ignore
    except Exception:
        logger.info("[multitenancy] FeishuAdapter not importable yet; merge_forward API patch deferred")
        return
    _patch_process_inbound(FeishuAdapter)
    _patch_extract(FeishuAdapter)
    _HOOK_INSTALLED = True


def _patch_process_inbound(FeishuAdapter: Any) -> None:
    original = getattr(FeishuAdapter, "_process_inbound_message", None)
    if original is None or getattr(original, _PROCESS_FLAG, False):
        return

    @functools.wraps(original)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            sender_id = kwargs.get("sender_id")
            open_id = str(getattr(sender_id, "open_id", "") or "")
            if open_id:
                setattr(self, _SENDER_ATTR, open_id)
        except Exception:
            logger.debug("[multitenancy] merge_forward sender capture failed", exc_info=True)
        return await original(self, *args, **kwargs)

    setattr(wrapped, _PROCESS_FLAG, True)
    FeishuAdapter._process_inbound_message = wrapped


def _patch_extract(FeishuAdapter: Any) -> None:
    original = getattr(FeishuAdapter, "_extract_message_content", None)
    if original is None or getattr(original, _EXTRACT_FLAG, False):
        return

    @functools.wraps(original)
    async def wrapped(self: Any, message: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original(self, message, *args, **kwargs)
        try:
            raw_type = str(getattr(message, "message_type", "") or "").strip().lower()
            if raw_type != "merge_forward":
                return result
            message_id = str(getattr(message, "message_id", "") or "")
            sender_open_id = str(getattr(self, _SENDER_ATTR, "") or "")
            if not message_id or not sender_open_id:
                return result
            transcript = await _expand_merge_forward(message_id, sender_open_id)
            if not transcript:
                return result
            # result is a tuple: (text, inbound_type, media_urls, media_types, mentions)
            rest = tuple(result[1:])
            logger.info(
                "[multitenancy] merge_forward expanded via UAT API (mid=%s, %d chars)",
                message_id, len(transcript),
            )
            return (transcript,) + rest
        except Exception:
            logger.exception("[multitenancy] merge_forward API expansion failed; using fallback text")
            return result

    setattr(wrapped, _EXTRACT_FLAG, True)
    FeishuAdapter._extract_message_content = wrapped
    logger.info("[multitenancy] installed merge_forward UAT-API expansion on FeishuAdapter")
