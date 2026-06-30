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
from ._sessions import SessionsNamespace
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
