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
