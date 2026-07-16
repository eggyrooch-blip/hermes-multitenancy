from __future__ import annotations

from dataclasses import replace
import functools
import json
import logging
import os
import re
from typing import Any, Iterable

from .feishu_adapter_compat import (
    load_feishu_module,
    load_normalize_feishu_message,
    log_feishu_adapter_load_error,
)

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_LABEL_SPLIT_RE = re.compile(r"\s*[:：]\s*", re.UNICODE)
_MERGE_FORWARD_ENV = "HERMES_FEISHU_MERGE_FORWARD_MAX"
_ENRICH_ALWAYS_TYPES = frozenset({"merge_forward", "interactive", "card"})
_APPLICANT_LABELS = ("申请人", "发起人", "applicant", "requester")
_STATUS_LABELS = ("状态", "result", "status")
_BODY_LABELS = ("正文", "内容", "body", "reason", "摘要", "说明")
_SUBJECT_LABELS = ("主题", "标题", "subject", "title")
_APPROVER_LABELS = ("审批人", "approver")


def install_feishu_inbound_richtext_patch() -> None:
    """Patch Feishu inbound normalization with fail-open rich-text enrichment."""
    try:
        feishu_module = load_feishu_module()
        normalize_feishu_message = load_normalize_feishu_message()
    except Exception as exc:
        log_feishu_adapter_load_error(
            logger,
            "[multitenancy] FeishuAdapter not importable yet; inbound richtext patch deferred",
            exc,
        )
        return

    original = getattr(feishu_module, "normalize_feishu_message", None) or normalize_feishu_message
    if original is None or getattr(original, "_hermes_multitenancy_inbound_patched", False):
        return

    @functools.wraps(original)
    def normalize_feishu_message_wrapper(**kwargs: Any) -> Any:
        result = original(**kwargs)
        try:
            return _enrich_normalized_message(result=result, **kwargs)
        except Exception:
            logger.exception(
                "[multitenancy] Feishu inbound richtext enrichment failed type=%s",
                getattr(result, "raw_type", ""),
            )
            return result

    setattr(normalize_feishu_message_wrapper, "_hermes_multitenancy_inbound_patched", True)
    setattr(normalize_feishu_message_wrapper, "_hermes_multitenancy_original", original)
    feishu_module.normalize_feishu_message = normalize_feishu_message_wrapper
    logger.info("[multitenancy] patched Feishu inbound message normalization for rich-text fallback")


def _enrich_normalized_message(
    *,
    result: Any,
    message_type: str,
    raw_content: str,
    mentions: Any = None,
    bot: Any = None,
) -> Any:
    del mentions, bot
    normalized_type = str(getattr(result, "raw_type", "") or message_type or "").strip().lower()
    existing_text = str(getattr(result, "text_content", "") or "")
    if existing_text.strip() and normalized_type not in _ENRICH_ALWAYS_TYPES:
        return result

    payload = _safe_load_feishu_payload(raw_content)
    if normalized_type == "email":
        return _replace_text_content(result, _extract_email_text(payload))
    if normalized_type == "share_user":
        return _replace_text_content(result, _extract_share_user_text(payload))
    if normalized_type == "todo":
        return _replace_text_content(result, _extract_todo_text(payload))
    if normalized_type == "vote":
        return _replace_text_content(result, _extract_vote_text(payload))
    if normalized_type == "merge_forward":
        text_content, image_keys, metadata = _extract_merge_forward_enrichment(payload, result=result)
        return _replace_normalized_message(
            result,
            text_content=text_content,
            image_keys=image_keys,
            metadata=metadata,
        )
    if normalized_type in {"interactive", "card"}:
        return _replace_text_content(result, _extract_interactive_card_text(payload))
    return _replace_text_content(result, _extract_generic_text(payload, normalized_type))


def _replace_text_content(result: Any, text_content: str) -> Any:
    return _replace_normalized_message(result, text_content=text_content)


def _replace_normalized_message(
    result: Any,
    *,
    text_content: str,
    image_keys: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Any:
    resolved_text = _normalize_whitespace(text_content)
    base_image_keys = list(getattr(result, "image_keys", []) or [])
    merged_image_keys = base_image_keys if image_keys is None else _unique_strings([*base_image_keys, *image_keys])
    merged_metadata = dict(getattr(result, "metadata", {}) or {})
    if metadata:
        merged_metadata.update(metadata)
    if (
        resolved_text == str(getattr(result, "text_content", "") or "")
        and merged_image_keys == base_image_keys
        and merged_metadata == dict(getattr(result, "metadata", {}) or {})
    ):
        return result
    return replace(
        result,
        text_content=resolved_text,
        image_keys=merged_image_keys,
        metadata=merged_metadata,
    )


def _safe_load_feishu_payload(raw_content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_content) if raw_content else {}
    except json.JSONDecodeError:
        return {"text": raw_content}
    return parsed if isinstance(parsed, dict) else {"content": parsed}


def _extract_email_text(payload: dict[str, Any]) -> str:
    subject = _find_first_text(payload, keys=("subject", "title", "mail_subject"))
    body = _find_first_text(
        payload,
        keys=("body", "content", "text", "summary", "preview", "plain_text"),
    )
    fallback = _join_lines(_collect_text_segments(payload))
    return _join_lines([subject, body, fallback])


def _extract_share_user_text(payload: dict[str, Any]) -> str:
    name = _find_first_text(payload, keys=("name", "user_name", "en_name", "chat_name"))
    user_id = _find_first_text(payload, keys=("user_id", "open_id", "union_id"))
    if name:
        return _join_lines([f"联系人: {name}", user_id and f"ID: {user_id}"])
    if user_id:
        return f"联系人: {user_id}"
    return "[Contact card]"


def _extract_todo_text(payload: dict[str, Any]) -> str:
    summary = payload.get("summary")
    title = ""
    content = ""
    if isinstance(summary, dict):
        title = _normalize_whitespace(str(summary.get("title", "") or ""))
        content = _join_lines(_collect_text_segments(summary.get("content")))
    title = title or _find_first_text(payload, keys=("title", "task_title", "summary"))
    due_time = _find_first_text(payload, keys=("due_time", "due_at", "deadline", "expire_time"))
    remainder = _join_lines(_collect_text_segments(payload))
    return _join_lines(
        [
            title,
            content,
            due_time and f"截止时间: {due_time}",
            remainder,
        ]
    )


def _extract_vote_text(payload: dict[str, Any]) -> str:
    topic = _find_first_text(payload, keys=("topic", "title", "subject", "question"))
    options_value = payload.get("options")
    options: list[str] = []
    if isinstance(options_value, list):
        for item in options_value:
            option_text = _extract_option_text(item)
            if option_text:
                options.append(f"• {option_text}")
    if not options:
        fallback_options = _collect_option_lines(payload)
        options = [f"• {line}" for line in fallback_options]
    return _join_lines([topic, *options])


def _extract_generic_text(payload: dict[str, Any], raw_type: str) -> str:
    text = _join_lines(_collect_text_segments(payload))
    return text or f"[{raw_type or 'unknown'} 消息]"


def _extract_merge_forward_enrichment(
    payload: dict[str, Any],
    *,
    result: Any,
) -> tuple[str, list[str], dict[str, Any]]:
    title = _find_first_text(payload, keys=("title", "summary", "preview", "description"))
    max_entries = _merge_forward_max_entries()
    entries: list[str] = []
    image_keys: list[str] = []
    uplifted_media: list[dict[str, Any]] = []
    for item in _iter_merge_forward_items(payload):
        line, nested_image_keys, nested_media = _render_forward_entry(item)
        if line:
            entries.append(line)
        image_keys.extend(nested_image_keys)
        uplifted_media.extend(nested_media)
        if len(entries) >= max_entries:
            break
    text_content = _join_lines([title, *entries]) or "[merge_forward 消息]"
    metadata = dict(getattr(result, "metadata", {}) or {})
    metadata.update({"entry_count": len(entries), "title": title})
    if uplifted_media:
        metadata["uplifted_media"] = uplifted_media
    return text_content, _unique_strings(image_keys), metadata


def _extract_interactive_card_text(payload: dict[str, Any]) -> str:
    if "json_card" in payload:
        raw_card = payload.get("json_card")
        try:
            card_payload = json.loads(raw_card) if isinstance(raw_card, str) else raw_card
        except json.JSONDecodeError:
            return "[interactive 消息]"
        if not isinstance(card_payload, dict):
            return "[interactive 消息]"
        subject = _find_header_title(card_payload)
        body = _join_lines(_extract_card_element_text(item) for item in _card_elements(card_payload))
        return _join_lines([subject and f"主题: {subject}", body and f"正文: {body}"]) or "[interactive 消息]"

    card_payload = payload.get("card") if isinstance(payload.get("card"), dict) else payload
    subject = _find_header_title(card_payload) or _find_first_text(
        card_payload,
        keys=("subject", "title", "summary", "subtitle"),
    )
    applicant = ""
    status = ""
    body = ""
    approver = ""
    remaining_lines: list[str] = []
    for line in _collect_text_segments(card_payload):
        parsed = _parse_labeled_line(line)
        if parsed is None:
            remaining_lines.append(line)
            continue
        label, value = parsed
        if not applicant and _label_matches(label, _APPLICANT_LABELS):
            applicant = value
            continue
        if not status and _label_matches(label, _STATUS_LABELS):
            status = value
            continue
        if not body and _label_matches(label, _BODY_LABELS):
            body = value
            continue
        if not subject and _label_matches(label, _SUBJECT_LABELS):
            subject = value
            continue
        if not approver and _label_matches(label, _APPROVER_LABELS):
            approver = value
            continue
        remaining_lines.append(line)

    remaining_body = _join_lines(
        line
        for line in remaining_lines
        if line not in {subject, applicant, status, body, approver}
    )
    body_text = body or remaining_body
    lines = [
        subject and f"主题: {subject}",
        applicant and f"申请人: {applicant}",
        approver and f"审批人: {approver}",
        status and f"状态: {status}",
        body_text and f"正文: {body_text}",
    ]
    if remaining_body and remaining_body != body_text:
        lines.append(remaining_body)
    return _join_lines(lines) or "[interactive 消息]"


def _card_elements(card: dict[str, Any]) -> list[Any]:
    direct = card.get("elements")
    if isinstance(direct, list):
        return direct
    body = card.get("body")
    if not isinstance(body, dict):
        return []
    body_property = body.get("property")
    if isinstance(body_property, dict) and isinstance(body_property.get("elements"), list):
        return body_property["elements"]
    return body.get("elements") if isinstance(body.get("elements"), list) else []


def _extract_card_element_text(element: Any) -> str:
    if isinstance(element, str):
        return _normalize_whitespace(element)
    if isinstance(element, list):
        return _join_lines(_extract_card_element_text(item) for item in element)
    if not isinstance(element, dict):
        return ""

    tag = str(element.get("tag") or "").strip().lower()
    if tag in {"hr", "br", "img", "image", "card_header"}:
        return ""
    prop = element.get("property") if isinstance(element.get("property"), dict) else element
    if tag in {"link", "button"}:
        label = _extract_card_text_value(prop.get("text") or prop.get("content") or prop.get("title"))
        url = str(prop.get("url") or prop.get("href") or "").strip()
        if url:
            return f"[{label}]({url})" if label else url

    parts = [
        _extract_card_text_value(prop.get("text")),
        _extract_card_text_value(prop.get("content")),
        _extract_card_text_value(prop.get("title")),
        _extract_card_text_value(prop.get("label")),
    ]
    for key in ("elements", "fields", "items", "columns"):
        parts.append(_extract_card_element_text(prop.get(key)))
    return _join_lines(parts)


def _extract_card_text_value(value: Any) -> str:
    if isinstance(value, str):
        return _normalize_whitespace(value)
    if isinstance(value, list):
        return "".join(_extract_card_text_value(item) for item in value)
    if not isinstance(value, dict):
        return ""
    prop = value.get("property") if isinstance(value.get("property"), dict) else value
    i18n = prop.get("i18nContent")
    if isinstance(i18n, dict):
        for language in ("zh_cn", "en_us", "ja_jp"):
            text = _extract_card_text_value(i18n.get(language))
            if text:
                return text
    for key in ("content", "text", "title", "elements"):
        text = _extract_card_text_value(prop.get(key))
        if text:
            return text
    return ""


def _render_forward_entry(item: Any) -> tuple[str, list[str], list[dict[str, Any]]]:
    if not isinstance(item, dict):
        text = _normalize_whitespace(str(item or ""))
        return (f"- {text}" if text else ""), [], []

    sender = _find_first_text(item, keys=("sender_name", "user_name", "sender", "name"))
    nested_type = _normalize_whitespace(str(item.get("message_type", "") or item.get("msg_type", "") or "")).lower()
    image_keys: list[str] = []
    uplifted_media: list[dict[str, Any]] = []

    if nested_type == "image" or _find_first_text(item, keys=("image_key",)):
        image_key = _find_first_text(item, keys=("image_key",))
        if image_key:
            image_keys.append(image_key)
        uplifted_media.append(
            {
                "resource_type": "image",
                "image_key": image_key,
                "file_key": "",
                "file_name": "",
                "sender_name": sender,
            }
        )
        body = "[图片]"
    elif nested_type in {"file", "audio", "media", "video"} or _find_first_text(
        item,
        keys=("file_key", "file_name"),
    ):
        file_key = _find_first_text(item, keys=("file_key",))
        file_name = _find_first_text(item, keys=("file_name", "title", "name"))
        uplifted_media.append(
            {
                "resource_type": nested_type or "file",
                "image_key": "",
                "file_key": file_key,
                "file_name": file_name,
                "sender_name": sender,
            }
        )
        body = f"[文件: {file_name}]" if file_name else "[文件]"
    else:
        content = item.get("content") if "content" in item else item
        body = _join_lines(
            [
                _find_first_text(item, keys=("text", "summary", "preview", "title")),
                _join_lines(_collect_text_segments(content)),
            ]
        )
    if sender and body:
        return f"- {sender}: {body}", image_keys, uplifted_media
    if body:
        return f"- {body}", image_keys, uplifted_media
    return "", image_keys, uplifted_media


def _merge_forward_max_entries() -> int:
    value = os.environ.get(_MERGE_FORWARD_ENV, "").strip()
    if not value:
        return 30
    try:
        return max(int(value), 30)
    except ValueError:
        return 30


def _iter_merge_forward_items(payload: dict[str, Any]) -> Iterable[Any]:
    for key in ("messages", "items", "message_list", "records", "content"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                yield item


def _collect_option_lines(payload: Any) -> list[str]:
    options: list[str] = []
    for node in _walk_nodes(payload):
        if isinstance(node, dict) and str(node.get("tag", "") or "").lower() == "option":
            option_text = _extract_option_text(node)
            if option_text:
                options.append(option_text)
    return _unique_strings(options)


def _extract_option_text(value: Any) -> str:
    if isinstance(value, str):
        return _normalize_whitespace(value)
    if isinstance(value, dict):
        return _find_first_text(value, keys=("text", "name", "content", "title", "option"))
    return _normalize_whitespace(str(value or ""))


def _find_first_text(payload: Any, *, keys: tuple[str, ...]) -> str:
    for node in _walk_nodes(payload):
        if not isinstance(node, dict):
            continue
        for key in keys:
            value = node.get(key)
            if isinstance(value, str):
                normalized = _normalize_whitespace(value)
                if normalized:
                    return normalized
            if value is not None and not isinstance(value, (dict, list)):
                normalized = _normalize_whitespace(str(value))
                if normalized:
                    return normalized
    return ""


def _find_header_title(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    header = payload.get("header")
    if not isinstance(header, dict):
        return ""
    header_property = header.get("property")
    title = header_property.get("title") if isinstance(header_property, dict) else header.get("title")
    nested_title = _extract_card_text_value(title)
    if nested_title:
        return nested_title
    if isinstance(title, dict):
        return _join_lines(
            [
                _normalize_whitespace(str(title.get("content", "") or "")),
                _normalize_whitespace(str(title.get("text", "") or "")),
                _normalize_whitespace(str(title.get("name", "") or "")),
            ]
        )
    return _normalize_whitespace(str(title or ""))


def _collect_text_segments(value: Any) -> list[str]:
    collected: list[str] = []
    _collect_text_segments_into(value, collected)
    return _unique_strings(collected)


def _collect_text_segments_into(value: Any, collected: list[str]) -> None:
    if isinstance(value, str):
        normalized = _normalize_whitespace(value)
        if normalized:
            collected.append(normalized)
        return
    if isinstance(value, list):
        for item in value:
            _collect_text_segments_into(item, collected)
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_text_segments_into(item, collected)


def _walk_nodes(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_nodes(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from _walk_nodes(item)


def _parse_labeled_line(line: str) -> tuple[str, str] | None:
    normalized = _normalize_whitespace(line)
    if not normalized:
        return None
    parts = _LABEL_SPLIT_RE.split(normalized, maxsplit=1)
    if len(parts) != 2:
        return None
    label = _normalize_whitespace(parts[0])
    value = _normalize_whitespace(parts[1])
    if not label or not value:
        return None
    return label, value


def _label_matches(label: str, keywords: tuple[str, ...]) -> bool:
    normalized = _normalize_whitespace(label).lower()
    return any(keyword.lower() in normalized for keyword in keywords)


def _join_lines(values: Iterable[Any]) -> str:
    parts: list[str] = []
    for value in values:
        if not value:
            continue
        normalized = _normalize_whitespace(str(value))
        if normalized:
            parts.append(normalized)
    return "\n".join(_unique_strings(parts))


def _normalize_whitespace(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        normalized = _WHITESPACE_RE.sub(" ", line).strip()
        if normalized:
            lines.append(normalized)
    return "\n".join(lines).strip()


def _unique_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
