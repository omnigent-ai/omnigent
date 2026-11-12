"""Structured Codex app-server errors shared by SDK and native executors."""

from __future__ import annotations

import re


class CodexAppServerResponseError(RuntimeError):
    """Structured JSON-RPC error returned by the Codex app-server."""

    def __init__(self, error: object) -> None:
        """Preserve the JSON-RPC code and message while remaining a RuntimeError."""
        self.error = error
        self.code: int | None = None
        self.message: str | None = None
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
            self.code = code if isinstance(code, int) else None
            self.message = message if isinstance(message, str) else None
        super().__init__(str(error))


def is_required_mcp_startup_timeout(error: CodexAppServerResponseError) -> bool:
    """Recognize an explicit thread-initialization rejection before a turn starts."""
    return (
        error.code == -32603
        and error.message is not None
        and re.fullmatch(
            r"error creating thread: Fatal error: Failed to initialize session: "
            r"required MCP servers failed to initialize: [^:\r\n]+: "
            r"timed out handshaking with MCP server after \d+(?:\.\d+)?s: "
            r"deadline has elapsed",
            error.message,
        )
        is not None
    )


def input_too_large_limits(error: CodexAppServerResponseError) -> tuple[int, int] | None:
    """Read the sizes when Codex refused turn input as too large to accept.

    Codex validates ``turn/start`` and ``turn/steer`` input before submitting
    it, answering ``-32602`` with ``data.input_error_code == "input_too_large"``,
    so nothing reached the thread.

    :param error: Structured JSON-RPC response error.
    :returns: ``(actual_chars, max_chars)`` for that refusal, ``(0, 0)`` when it
        omits the sizes, or ``None`` for any other error.
    """
    if error.code != -32602:
        return None
    data = error.error.get("data") if isinstance(error.error, dict) else None
    if not isinstance(data, dict) or data.get("input_error_code") != "input_too_large":
        return None
    actual = data.get("actual_chars")
    limit = data.get("max_chars")
    if isinstance(actual, int) and isinstance(limit, int):
        return actual, limit
    return 0, 0
