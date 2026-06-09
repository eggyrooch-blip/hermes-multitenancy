"""Inline secret redaction for tool-row text."""
from __future__ import annotations

import re

INLINE_ASSIGNMENT_RE = re.compile(r"""(^|[\s"'`])([A-Za-z_][A-Za-z0-9_]*)(=(?:"[^"]*"|'[^']*'|[^\s"'`]+))""")
AUTH_HEADER_SECRET_RE = re.compile(
    r"""(Authorization\s*:\s*(?:Bearer|Basic|Token)\s+)([^'"\s]+)""",
    re.IGNORECASE,
)
QUOTED_HEADER_ARG_RE = re.compile(
    r"""((?:^|[\s"'`])(?:-H|--header)\s+)(['"])([A-Za-z0-9_-]+)(\s*:\s*)([^'"]*)(['"])""",
    re.IGNORECASE,
)
UNQUOTED_HEADER_ARG_RE = re.compile(
    r"""((?:^|[\s"'`])(?:-H|--header)\s+)([A-Za-z0-9_-]+)(\s*:\s*)([^\s"'`]+)""",
    re.IGNORECASE,
)
SECRET_FLAG_RE = re.compile(
    r"""((?:^|[\s"'`]))(--?[A-Za-z0-9][A-Za-z0-9-]*)(=|\s+)(?:"([^"]*)"|'([^']*)'|([^\s"'`]+))"""
)
SENSITIVE_NAME_RE = re.compile(
    r"""token|secret|password|api[_-]?key|authorization|cookie|credential|bearer|session[_-]?id|client[_-]?secret|access[_-]?key""",
    re.IGNORECASE,
)


def is_sensitive_name(value: str) -> bool:
    return bool(SENSITIVE_NAME_RE.search(str(value or "")))


def should_redact_header_value(name: str) -> bool:
    return not re.fullmatch(r"authorization", str(name or ""), re.IGNORECASE) and is_sensitive_name(name)


def redact_inline_secrets(value: str) -> str:
    redacted = INLINE_ASSIGNMENT_RE.sub(_replace_inline_assignment, str(value or ""))
    redacted = AUTH_HEADER_SECRET_RE.sub(r"\1[redacted]", redacted)
    redacted = QUOTED_HEADER_ARG_RE.sub(_replace_quoted_header_arg, redacted)
    redacted = UNQUOTED_HEADER_ARG_RE.sub(_replace_unquoted_header_arg, redacted)
    return SECRET_FLAG_RE.sub(_replace_secret_flag, redacted)


def _replace_inline_assignment(match: re.Match[str]) -> str:
    prefix, key = match.group(1), match.group(2)
    if not is_sensitive_name(key):
        return match.group(0)
    return f"{prefix}{key}=[redacted]"


def _replace_quoted_header_arg(match: re.Match[str]) -> str:
    prefix, quote, name, separator, closing_quote = (
        match.group(1),
        match.group(2),
        match.group(3),
        match.group(4),
        match.group(6),
    )
    if quote != closing_quote or not should_redact_header_value(name):
        return match.group(0)
    return f"{prefix}{quote}{name}{separator}[redacted]{quote}"


def _replace_unquoted_header_arg(match: re.Match[str]) -> str:
    prefix, name, separator = match.group(1), match.group(2), match.group(3)
    if not should_redact_header_value(name):
        return match.group(0)
    return f"{prefix}{name}{separator}[redacted]"


def _replace_secret_flag(match: re.Match[str]) -> str:
    prefix, flag, separator = match.group(1), match.group(2), match.group(3)
    normalized_flag = flag.lstrip("-")
    if not is_sensitive_name(normalized_flag):
        return match.group(0)
    if match.group(4) is not None:
        redacted_value = '"[redacted]"'
    elif match.group(5) is not None:
        redacted_value = "'[redacted]'"
    else:
        redacted_value = "[redacted]"
    return f"{prefix}{flag}{separator}{redacted_value}"
