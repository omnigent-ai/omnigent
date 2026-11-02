"""Classify native Codex shell input before content normalization."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

CODEX_SHELL_AUTHORIZED_HEADER = "X-Omnigent-Codex-Shell-Authorized"

_authorized_shell_command: ContextVar[str | None] = ContextVar(
    "authorized_native_shell_command", default=None
)


@contextlib.contextmanager
def native_shell_authorization(command: str | None) -> Iterator[None]:
    """Bind runner-validated authorization to one delivery, never its user content."""
    token = _authorized_shell_command.set(command)
    try:
        yield
    finally:
        _authorized_shell_command.reset(token)


def native_shell_command_is_authorized(command: str) -> bool:
    """Require authorization for the exact shell body being delivered."""
    return _authorized_shell_command.get() == command


def shell_command_from_input(input_value: object) -> str | None:
    """Classify raw harness input, including its latest role-keyed user message."""
    if isinstance(input_value, list):
        for item in reversed(input_value):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if item.get("role") == "user" and isinstance(content, (str, list)) and content:
                return shell_command_from_content(content)
    return shell_command_from_content(input_value)


def shell_command_from_content(content: object) -> str | None:
    """Return the command from a lone text input, preserving shell-significant whitespace.

    Attachments, multiple blocks, ``!!`` and empty commands remain model input.
    """
    if isinstance(content, list):
        if len(content) != 1:
            return None
        block = content[0]
        if not isinstance(block, dict) or not all(isinstance(key, str) for key in block):
            return None
        if block.get("type") not in {"input_text", "text"}:
            return None
        content = block.get("text")
    if not isinstance(content, str):
        return None
    text = content.lstrip()
    if not text.startswith("!") or text.startswith("!!"):
        return None
    command = text[1:].lstrip().rstrip("\r\n")
    return command if command.strip() else None
