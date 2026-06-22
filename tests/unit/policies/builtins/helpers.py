"""Shared event builders for built-in Google policy tests.

Construct :class:`PolicyEvent` dicts in the current shape:
``tool_call`` carries ``data = {"name", "arguments"}``; ``tool_result``
carries ``data = {"result": <stringified-output>}`` (the server-side
shape). Kept here (not in ``conftest.py``) because they are plain helper
functions shared across the gdrive / gmail / gcalendar test modules, not
fixtures.
"""

from __future__ import annotations

from typing import Any

from omnigent.policies.schema import PolicyEvent


def tool_call_event(
    tool: str,
    arguments: dict[str, Any],  # type: ignore[explicit-any]
    session_state: dict[str, Any] | None = None,  # type: ignore[explicit-any]
) -> PolicyEvent:
    """
    Build a ``tool_call`` :class:`PolicyEvent`.

    :param tool: Tool name, set as ``target`` and ``data.name``, e.g.
        ``"mcp__google__drive_file_update"``.
    :param arguments: Tool arguments under ``data.arguments``, e.g.
        ``{"file_id": "1AbC"}``.
    :param session_state: Optional persisted state, e.g.
        ``{"gdrive_created_file_ids": ["1AbC"]}``. ``None`` means empty.
    :returns: A ``tool_call`` event dict.
    """
    return {
        "type": "tool_call",
        "target": tool,
        "data": {"name": tool, "arguments": arguments},
        "context": {"actor": {}, "usage": {}},
        "session_state": session_state or {},
    }


def llm_request_event(
    model: str = "gpt-4o",
    messages_count: int = 10,
    tools_count: int = 5,
    system_prompt_preview: str = "",
    last_user_message: str = "",
) -> PolicyEvent:
    """
    Build an ``llm_request`` :class:`PolicyEvent`.

    :param model: Model name, e.g. ``"gpt-4o"``.
    :param messages_count: Number of messages in the prompt.
    :param tools_count: Number of tool schemas attached.
    :param system_prompt_preview: Preview of the system prompt
        (first ~200 chars), e.g. ``"You are a helpful assistant."``.
    :param last_user_message: Preview of the last user message
        (first ~500 chars), e.g. ``"my email is alice@test.com"``.
    :returns: An ``llm_request`` event dict.
    """
    return {
        "type": "llm_request",
        "target": None,
        "data": {
            "model": model,
            "messages_count": messages_count,
            "tools_count": tools_count,
            "system_prompt_preview": system_prompt_preview,
            "last_user_message": last_user_message,
        },
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }


def tool_result_event(
    tool: str,
    result: str,
    session_state: dict[str, Any] | None = None,  # type: ignore[explicit-any]
) -> PolicyEvent:
    """
    Build a ``tool_result`` :class:`PolicyEvent` (server-side shape).

    :param tool: Tool that produced the result, e.g.
        ``"mcp__google__docs_document_create"``.
    :param result: Stringified tool output under ``data.result``, e.g.
        ``'{"documentId": "1New"}'``.
    :param session_state: Optional persisted state. ``None`` means empty.
    :returns: A ``tool_result`` event dict.
    """
    return {
        "type": "tool_result",
        "target": tool,
        "data": {"result": result},
        "context": {"actor": {}, "usage": {}},
        "session_state": session_state or {},
    }
