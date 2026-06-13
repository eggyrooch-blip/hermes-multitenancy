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
_SENDER_ATTR = "_mt_merge_forward_sender_by_mid"  # {message_id: forwarder open_id}
_SENDER_MAP_MAX = 256

_MEDIA_EXPIRED_HINT = "[资源已过期(14005)，请直接发送]"
_FORWARD_EXPIRED_HINT = "[转发内容含已过期资源(14005)，请直接发送]"


def _sender_map(adapter: Any) -> dict:
    """Per-message_id forwarder-open_id map on the adapter.

    Keyed by message_id (not a single shared attribute) so concurrent forwards
    from different senders on the same gateway can't race each other's UAT token.
    """
    m = getattr(adapter, _SENDER_ATTR, None)
    if not isinstance(m, dict):
        m = {}
        setattr(adapter, _SENDER_ATTR, m)
    return m

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
    media_markers: Optional[dict[str, str]] = None,
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
        mid = str(it.get("message_id") or "")
        body = str((media_markers or {}).get(mid) or _item_text(it))
        pad = indent * (depth + 1)
        lines.append(indent * depth + prefix)
        for bl in body.splitlines() or [""]:
            lines.append(pad + bl)
        if mid and children.get(mid):  # nested merge_forward — recurse, no extra API
            lines.extend(
                _render_sub_tree(
                    mid,
                    children,
                    names,
                    depth=depth + 1,
                    counter=counter,
                    indent=indent,
                    media_markers=media_markers,
                )
            )
    return lines


def _render_forwarded_messages(
    message_id: str,
    items: list[dict],
    names: dict[str, str],
    *,
    media_markers: Optional[dict[str, str]] = None,
) -> Optional[str]:
    if not items:
        return None
    children = _build_children_map(items, message_id)
    body_lines = _render_sub_tree(
        message_id,
        children,
        names,
        depth=0,
        counter=[0],
        media_markers=media_markers,
    )
    if not body_lines:
        return None
    inner = "\n".join(body_lines)
    return f"<forwarded_messages>\n{inner}\n</forwarded_messages>"


def _expand_merge_forward_blocking(message_id: str, sender_open_id: str) -> Optional[str]:
    """Fetch + render a merge_forward as <forwarded_messages>. None on failure/empty."""
    items = _fetch_sub_messages_blocking(message_id, sender_open_id)
    if not items:
        return None
    names = _resolve_names_blocking([_item_sender_open_id(it) for it in items], sender_open_id)
    return _render_forwarded_messages(message_id, items, names)


async def _expand_merge_forward(message_id: str, sender_open_id: str) -> Optional[str]:
    return await asyncio.to_thread(_expand_merge_forward_blocking, message_id, sender_open_id)


def _is_merge_forward_hint_error(exc: Exception) -> bool:
    # Match only specific Feishu errcodes (14005 = resource deleted, 230002 =
    # no permission), not a bare "permission" substring which could false-match
    # unrelated transport errors and mislabel a live resource as expired.
    text = str(exc or "").lower()
    return "14005" in text or "230002" in text or "resource has been deleted" in text


def _forward_hint_transcript(text: str) -> str:
    return f"<forwarded_messages>\n{text}\n</forwarded_messages>"


def _media_marker_for_item(image_keys: list[str], media_refs: list[tuple[str, str, str]]) -> str:
    if image_keys:
        return "[图片]"
    file_name = ""
    if media_refs:
        file_name = str(media_refs[0][2] or "").strip()
    if file_name:
        return f"[文件:{file_name}]"
    return "[文件]"


async def _collect_merge_forward_media(
    adapter: Any,
    container_message_id: str,
    items: list[dict],
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    from .feishu_reply_quote_api import (
        _QUOTE_MEDIA_MAX,
        _download_reply_quote_media,
        _normalize_parent_item,
        _parent_image_keys,
        _parent_media_refs,
    )

    # IMPORTANT: a merge_forward's sub-message resources are owned by the
    # CONTAINER message, not the sub-item. The im/v1/messages/{id}/resources
    # endpoint returns 234003 "File not in msg" for a sub-item message_id +
    # the sub-item's image_key, but HTTP 200 for the container message_id +
    # that same image_key (verified against real Feishu data). So we download
    # every sub-item's media via the container message_id, and key the inline
    # markers by the sub-item message_id only for transcript render-matching.
    media_markers: dict[str, str] = {}
    media: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    for item in items:
        normalized = _normalize_parent_item(item)
        image_keys = _parent_image_keys(item, normalized)
        media_refs = _parent_media_refs(item, normalized)
        if not image_keys and not media_refs:
            continue
        sub_message_id = str(item.get("message_id") or "")
        if not sub_message_id:
            continue
        success_marker = _media_marker_for_item(image_keys, media_refs)
        if len(media) >= _QUOTE_MEDIA_MAX:
            media_markers[sub_message_id] = success_marker
            continue
        try:
            downloaded = await _download_reply_quote_media(adapter, container_message_id, item)
        except Exception:
            logger.debug(
                "[multitenancy] merge_forward media download failed (container=%s sub=%s)",
                container_message_id,
                sub_message_id,
                exc_info=True,
            )
            media_markers[sub_message_id] = _MEDIA_EXPIRED_HINT
            continue
        if not downloaded:
            media_markers[sub_message_id] = _MEDIA_EXPIRED_HINT
            continue
        media_markers[sub_message_id] = success_marker
        remaining = _QUOTE_MEDIA_MAX - len(media)
        for path, media_type in downloaded[:remaining]:
            if path and path not in seen_paths:
                media.append((path, media_type))
                seen_paths.add(path)
    return media_markers, media


async def _expand_merge_forward_with_media(
    adapter: Any,
    message_id: str,
    sender_open_id: str,
) -> tuple[Optional[str], list[tuple[str, str]]]:
    try:
        items = await asyncio.to_thread(_fetch_sub_messages_blocking, message_id, sender_open_id)
    except Exception as exc:
        if _is_merge_forward_hint_error(exc):
            return _forward_hint_transcript(_FORWARD_EXPIRED_HINT), []
        raise
    if not items:
        return None, []
    names = await asyncio.to_thread(
        _resolve_names_blocking,
        [_item_sender_open_id(it) for it in items],
        sender_open_id,
    )
    media_markers, media = await _collect_merge_forward_media(adapter, message_id, items)
    transcript = _render_forwarded_messages(message_id, items, names, media_markers=media_markers)
    return transcript, media


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
            mid = str(kwargs.get("message_id") or "")
            open_id = str(getattr(sender_id, "open_id", "") or "")
            if mid and open_id:
                m = _sender_map(self)
                if len(m) >= _SENDER_MAP_MAX:
                    m.clear()  # bound memory; a dropped entry only costs a fallback
                m[mid] = open_id
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
            sender_open_id = str(_sender_map(self).pop(message_id, "") or "")
            if not message_id or not sender_open_id:
                return result
            transcript, media = await _expand_merge_forward_with_media(self, message_id, sender_open_id)
            if not transcript:
                return result
            if not isinstance(result, tuple) or len(result) < 4:
                return result
            media_urls = list(result[2] or []) + [path for path, _media_type in media]
            media_types = list(result[3] or []) + [media_type for _path, media_type in media]
            logger.info(
                "[multitenancy] merge_forward expanded via UAT API (mid=%s, %d chars, %d media)",
                message_id,
                len(transcript),
                len(media),
            )
            return (transcript, result[1], media_urls, media_types) + tuple(result[4:])
        except Exception:
            logger.exception("[multitenancy] merge_forward API expansion failed; using fallback text")
            return result

    setattr(wrapped, _EXTRACT_FLAG, True)
    FeishuAdapter._extract_message_content = wrapped
    logger.info("[multitenancy] installed merge_forward UAT-API expansion on FeishuAdapter")
