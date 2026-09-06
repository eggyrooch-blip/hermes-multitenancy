from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Optional, Sequence


HEADLESS_OAUTH_ENTRY_WORDS = frozenset({"authorize", "login", "oauth", "signin"})
_CLI_VALUE_FLAGS = frozenset({"--env", "--profile", "-p"})
OAUTH_CLI_GATE_BY_DETAIL = {
    "kep-auth": "kep-auth-shim",
    "lark-cli": "lark-registered-tool",
    "meegle": "meegle-shim",
}


def require_registered_oauth_cli_gates(definitions: object) -> None:
    """Fail closed when an OAuth connector names no concrete headless gate."""
    items = getattr(definitions, "items", lambda: ())()
    for connector_id, definition in items:
        if getattr(getattr(definition, "ui", None), "action", "") not in {
            "oauth_url",
            "feishu_device_flow",
        }:
            continue
        detail = str(getattr(getattr(definition, "invocation", None), "detail", "") or "")
        if detail not in OAUTH_CLI_GATE_BY_DETAIL:
            raise RuntimeError(
                f"OAuth connector {connector_id!r} has no headless gate for {detail!r}"
            )


def cli_positional_words(argv: list[str]) -> list[str]:
    """Return command words while excluding known global flags and values."""
    words: list[str] = []
    skip_next = False
    for raw in argv:
        item = str(raw or "").strip().lower()
        if skip_next:
            skip_next = False
            if item == "auth" or item in HEADLESS_OAUTH_ENTRY_WORDS:
                words.append(item)
            continue
        if item in _CLI_VALUE_FLAGS:
            skip_next = True
            continue
        if any(item.startswith(f"{flag}=") for flag in _CLI_VALUE_FLAGS if flag.startswith("--")):
            continue
        if item.startswith("-"):
            continue
        if item:
            words.append(item)
    return words


def is_headless_oauth_attempt(argv: list[str]) -> bool:
    """Recognize interactive auth grammar without trusting fixed argv offsets."""
    words = cli_positional_words(argv)
    for index, word in enumerate(words):
        if word in HEADLESS_OAUTH_ENTRY_WORDS:
            return True
        if word == "auth" and (
            index + 1 == len(words) or words[index + 1] in HEADLESS_OAUTH_ENTRY_WORDS
        ):
            return True
    return False


def _validated_command(
    shim_dir: Path,
    command: Optional[Sequence[str]],
) -> tuple[str, list[str]]:
    if not command:
        return "", []
    try:
        resolved_real = Path(command[0]).expanduser().resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ValueError("Meegle real binary is unavailable") from exc
    if not resolved_real.is_file() or not os.access(resolved_real, os.X_OK):
        raise ValueError("Meegle real binary must be executable")
    if resolved_real.is_relative_to(shim_dir):
        raise ValueError("Meegle real binary must not resolve to its own shim")
    return str(resolved_real), [str(item) for item in command[1:]]


def install_meegle_oauth_guard(
    shim_dir: Path,
    *,
    real_binary: Optional[Path] = None,
    real_command: Optional[Sequence[str]] = None,
) -> Path:
    """Keep interactive Meegle OAuth out of an agent's headless terminal."""
    shim_dir = Path(shim_dir).expanduser().resolve(strict=False)
    shim_dir.mkdir(parents=True, exist_ok=True)
    wrapper = (shim_dir / "meegle").resolve(strict=False)
    if real_binary is not None and real_command is not None:
        raise ValueError("Provide only one Meegle real command")
    command = real_command if real_command is not None else ([str(real_binary)] if real_binary else None)
    resolved_real, fixed_args = _validated_command(shim_dir, command)
    wrapper.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import os
            import sys

            real_binary = {resolved_real!r}
            fixed_args = {fixed_args!r}
            ENTRY_WORDS = {set(HEADLESS_OAUTH_ENTRY_WORDS)!r}
            VALUE_FLAGS = {set(_CLI_VALUE_FLAGS)!r}

            def positional_words(argv):
                words = []
                skip_next = False
                for raw in argv:
                    item = str(raw or "").strip().lower()
                    if skip_next:
                        skip_next = False
                        if item == "auth" or item in ENTRY_WORDS:
                            words.append(item)
                        continue
                    if item in VALUE_FLAGS:
                        skip_next = True
                        continue
                    if any(item.startswith(flag + "=") for flag in VALUE_FLAGS if flag.startswith("--")):
                        continue
                    if item.startswith("-"):
                        continue
                    if item:
                        words.append(item)
                return words

            words = positional_words(sys.argv[1:])
            interactive = any(word in ENTRY_WORDS for word in words)
            interactive = interactive or any(
                word == "auth" and (index + 1 == len(words) or words[index + 1] in ENTRY_WORDS)
                for index, word in enumerate(words)
            )
            if interactive:
                print(
                    "Interactive Meegle OAuth is disabled in headless runs. "
                    "Use the Hermes Connectors authorization link.",
                    file=sys.stderr,
                )
                raise SystemExit(77)
            if not real_binary:
                print("Meegle is unavailable in this headless run.", file=sys.stderr)
                raise SystemExit(75)
            os.execve(real_binary, [real_binary, *fixed_args, *sys.argv[1:]], dict(os.environ))
            """
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def install_meegle_npx_oauth_guard(shim_dir: Path, *, real_binary: Path) -> Path:
    """Guard only Meegle's pinned npx package; delegate every other npx call."""
    shim_dir = Path(shim_dir).expanduser().resolve(strict=False)
    shim_dir.mkdir(parents=True, exist_ok=True)
    wrapper = (shim_dir / "npx").resolve(strict=False)
    resolved_real, _ = _validated_command(shim_dir, [str(real_binary)])
    wrapper.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import os
            import shlex
            import sys

            real_binary = {resolved_real!r}
            ENTRY_WORDS = {set(HEADLESS_OAUTH_ENTRY_WORDS)!r}
            VALUE_FLAGS = {set(_CLI_VALUE_FLAGS)!r}

            def positional_words(argv):
                words = []
                skip_next = False
                for raw in argv:
                    item = str(raw or "").strip().lower()
                    if skip_next:
                        skip_next = False
                        if item == "auth" or item in ENTRY_WORDS:
                            words.append(item)
                        continue
                    if item in VALUE_FLAGS:
                        skip_next = True
                        continue
                    if any(item.startswith(flag + "=") for flag in VALUE_FLAGS if flag.startswith("--")):
                        continue
                    if item.startswith("-"):
                        continue
                    if item:
                        words.append(item)
                return words

            def is_meegle_package(value):
                value = str(value or "").strip().lower()
                return value == "@lark-project/meegle" or value.startswith(
                    "@lark-project/meegle@"
                )

            def meegle_package_index(argv):
                for index, raw in enumerate(argv):
                    item = str(raw or "").strip().lower()
                    if is_meegle_package(item):
                        return index
                    if item.startswith("--package=") and is_meegle_package(
                        item.split("=", 1)[1]
                    ):
                        return index
                    if item in {"--package", "-p"} and index + 1 < len(argv):
                        if is_meegle_package(argv[index + 1]):
                            return index + 1
                return None

            argv = sys.argv[1:]
            package_index = meegle_package_index(argv)
            command_argv = argv[package_index + 1:] if package_index is not None else []
            words = positional_words(command_argv)
            for index, raw in enumerate(command_argv[:-1]):
                if str(raw).strip().lower() == "-c":
                    try:
                        words.extend(positional_words(shlex.split(command_argv[index + 1])))
                    except ValueError:
                        words.append("oauth")
            interactive = any(word in ENTRY_WORDS for word in words)
            interactive = interactive or any(
                word == "auth" and (index + 1 == len(words) or words[index + 1] in ENTRY_WORDS)
                for index, word in enumerate(words)
            )
            if package_index is not None and interactive:
                print(
                    "Interactive Meegle OAuth is disabled in headless runs. "
                    "Use the Hermes Connectors authorization link.",
                    file=sys.stderr,
                )
                raise SystemExit(77)
            os.execve(real_binary, [real_binary, *argv], dict(os.environ))
            """
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper
