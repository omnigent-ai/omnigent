"""omnigent client SDK — Python client for the omnigent server API.

Headless HTTP/SSE client for invoking agents, tracking conversation
state, and consuming the response stream as either raw events or
semantic blocks. No UI or terminal dependencies — frontends layer
on top of this.

Usage::

    from omnigent_client import OmnigentClient

    async with OmnigentClient(base_url="http://localhost:8080") as client:
        session = client.session(model="archer")
        async for event in session.send("hello"):
            ...

Or consume semantic blocks via :class:`BlockStream`::

    from omnigent_client import BlockStream, pipe, skip_intermediate_ends

    stream = BlockStream()
    async for block in pipe(
        stream.stream(session, "hello"),
        skip_intermediate_ends(),
    ):
        ...
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._blocks import (
        AnyBlock,
        BlockContext,
        CompactionBlock,
        ErrorBlock,
        FileBlock,
        NativeToolBlock,
        ReasoningBlock,
        ReasoningChunk,
        ReasoningStartBlock,
        ResponseEndBlock,
        ResponseStartBlock,
        RetryBlock,
        StreamBlock,
        TextChunk,
        TextDone,
        ToolExecution,
        ToolGroup,
        ToolResultBlock,
    )
    from ._child_status import (
        TERMINAL_TASK_STATUSES,
        child_session_busy,
        child_summary_busy,
    )
    from ._client import OmnigentClient
    from ._errors import OmnigentError, ToolCallDenied
    from ._events import MCP_ELICITATION_METHOD, ElicitationRequest
    from ._query import QueryResult, QueryStream
    from ._server import LocalServer
    from ._session import Session
    from ._sessions import RegisteredAgent, SessionsNamespace
    from ._sessions_chat import SessionsChat, SessionToolCallInfo, ToolCallable
    from ._stream import BlockStream, format_tool_args_brief
    from ._tool_handler import (
        ElicitationRequestCtx,
        StreamHooks,
        ToolCallInfo,
        ToolHandler,
    )
    from ._transforms import (
        merge_text_across_iterations,
        only_agent,
        pipe,
        skip_blocks,
        skip_intermediate_ends,
    )
    from ._types import File
    from .tools import ToolMetadata, ToolState, tool

# Submodule -> the public names it provides. Resolved on first attribute
# access so that importing one submodule (e.g. ``_http`` for a URL
# helper) no longer builds the whole client: ``_sessions`` and ``_sse``
# reach into ``omnigent.server.schemas``, whose Pydantic models cost
# ~290ms to construct.
_EXPORTS: dict[str, tuple[str, ...]] = {
    "_blocks": (
        "AnyBlock",
        "BlockContext",
        "CompactionBlock",
        "ErrorBlock",
        "FileBlock",
        "NativeToolBlock",
        "ReasoningBlock",
        "ReasoningChunk",
        "ReasoningStartBlock",
        "ResponseEndBlock",
        "ResponseStartBlock",
        "RetryBlock",
        "StreamBlock",
        "TextChunk",
        "TextDone",
        "ToolExecution",
        "ToolGroup",
        "ToolResultBlock",
    ),
    "_child_status": (
        "TERMINAL_TASK_STATUSES",
        "child_session_busy",
        "child_summary_busy",
    ),
    "_client": ("OmnigentClient",),
    "_errors": (
        "OmnigentError",
        "ToolCallDenied",
    ),
    "_events": (
        "ElicitationRequest",
        "MCP_ELICITATION_METHOD",
    ),
    "_query": (
        "QueryResult",
        "QueryStream",
    ),
    "_server": ("LocalServer",),
    "_session": ("Session",),
    "_sessions": (
        "RegisteredAgent",
        "SessionsNamespace",
    ),
    "_sessions_chat": (
        "SessionToolCallInfo",
        "SessionsChat",
        "ToolCallable",
    ),
    "_stream": (
        "BlockStream",
        "format_tool_args_brief",
    ),
    "_tool_handler": (
        "ElicitationRequestCtx",
        "StreamHooks",
        "ToolCallInfo",
        "ToolHandler",
    ),
    "_transforms": (
        "merge_text_across_iterations",
        "only_agent",
        "pipe",
        "skip_blocks",
        "skip_intermediate_ends",
    ),
    "_types": ("File",),
    "tools": (
        "ToolMetadata",
        "ToolState",
        "tool",
    ),
}

_NAME_TO_MODULE: dict[str, str] = {
    name: module for module, names in _EXPORTS.items() for name in names
}


def __getattr__(name: str) -> object:
    """
    Import the submodule backing *name* on first access (PEP 562).

    :param name: A public attribute from :data:`__all__`, e.g.
        ``"OmnigentClient"``.
    :returns: The requested object.
    :raises AttributeError: If *name* is not exported.
    """
    module = _NAME_TO_MODULE.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value  # cache so later reads skip __getattr__
    return value


def __dir__() -> list[str]:
    """
    List the module's public names without importing them.

    :returns: Sorted :data:`__all__`.
    """
    return sorted(__all__)


__all__ = [
    "MCP_ELICITATION_METHOD",
    "TERMINAL_TASK_STATUSES",
    "AnyBlock",
    "BlockContext",
    "BlockStream",
    "CompactionBlock",
    "ElicitationRequest",
    "ElicitationRequestCtx",
    "ErrorBlock",
    "File",
    "FileBlock",
    "LocalServer",
    "NativeToolBlock",
    "OmnigentClient",
    "OmnigentError",
    "QueryResult",
    "QueryStream",
    "ReasoningBlock",
    "ReasoningChunk",
    "ReasoningStartBlock",
    "RegisteredAgent",
    "ResponseEndBlock",
    "ResponseStartBlock",
    "RetryBlock",
    "Session",
    "SessionToolCallInfo",
    "SessionsChat",
    "SessionsNamespace",
    "StreamBlock",
    "StreamHooks",
    "TextChunk",
    "TextDone",
    "ToolCallDenied",
    "ToolCallInfo",
    "ToolCallable",
    "ToolExecution",
    "ToolGroup",
    "ToolHandler",
    "ToolMetadata",
    "ToolResultBlock",
    "ToolState",
    "child_session_busy",
    "child_summary_busy",
    "format_tool_args_brief",
    "merge_text_across_iterations",
    "only_agent",
    "pipe",
    "skip_blocks",
    "skip_intermediate_ends",
    "tool",
]
