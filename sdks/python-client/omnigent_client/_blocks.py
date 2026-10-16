"""Stream block types with context.

Every block carries a ``BlockContext`` describing which agent produced
it, at what depth, and in which turn. The simple case ignores context.
Multi-agent frontends route by ``block.ctx.agent``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ._async_questions import AsyncUserInput
from ._types import Response


@dataclass
class BlockContext:
    """Metadata attached to every stream block.

    :param agent: Name of the agent that produced this block, e.g.
        ``"coder.researcher"``. ``None`` for the root agent.
    :param depth: Nesting depth of the agent in the sub-agent tree.
        ``0`` for the root agent.
    :param turn: Turn number within the current response.
    :param timestamp: Monotonic timestamp when the block was created.
    """

    agent: str | None = None
    depth: int = 0
    turn: int = 0
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(kw_only=True)
class StreamBlock:
    """Base for all stream blocks."""

    ctx: BlockContext = field(default_factory=BlockContext)


# ── Response lifecycle ───────────────────────────────────


@dataclass(kw_only=True)
class ResponseStartBlock(StreamBlock):
    """The response has started.

    :param model: Agent model name, e.g. ``"coder"``.
    :param response_id: Server-assigned response ID.
    """

    model: str
    response_id: str


# ── Tool calls ───────────────────────────────────────────


@dataclass(kw_only=True)
class ToolExecution:
    """A single tool call paired with its result.

    :param name: Tool name, e.g. ``"Read"``.
    :param arguments: Parsed arguments dict.
    :param args_summary: One-line summary of the arguments, e.g. ``"test.py"``.
    :param call_id: Server-assigned call ID.
    :param agent_name: Name of the agent that invoked the tool.
    :param executed_by: ``"server"`` or ``"client"``.
    :param output: Tool output text, or ``None`` if not yet available.
    """

    name: str
    arguments: dict[str, object] = field(default_factory=dict)
    args_summary: str
    call_id: str
    agent_name: str
    executed_by: str = "server"
    output: str | None = None


@dataclass(kw_only=True)
class ToolGroup(StreamBlock):
    """A batch of tool calls from one iteration.

    :param executions: The tool calls in this group.
    :param iteration: The iteration number within the response.
    """

    executions: list[ToolExecution] = field(default_factory=list)
    iteration: int = 0


@dataclass(kw_only=True)
class ToolResultBlock(StreamBlock):
    """A tool result, emitted after the tool executes.

    :param name: Tool name, e.g. ``"Read"``.
    :param call_id: Server-assigned call ID.
    :param agent_name: Name of the agent that invoked the tool.
    :param output: Tool output text.
    :param arguments: Parsed arguments from the matching tool call,
        retained so result-only renderers can use call metadata.
    :param args_summary: One-line summary of the matching tool call
        arguments.
    """

    name: str
    call_id: str
    agent_name: str
    output: str
    arguments: dict[str, object] = field(default_factory=dict)
    args_summary: str = ""


@dataclass(kw_only=True)
class NativeToolBlock(StreamBlock):
    """A provider-native tool output (web_search, mcp, etc.).

    :param tool_type: Provider tool type, e.g. ``"web_search_call"``.
    :param label: Human-readable label for display, e.g. ``"search"``.
    :param data: Raw provider data dict.
    """

    tool_type: str
    label: str
    data: dict[str, object] = field(default_factory=dict)


# ── Text ─────────────────────────────────────────────────


@dataclass(kw_only=True)
class TextChunk(StreamBlock):
    """A flushed chunk of streamed text.

    :param text: The text content of this chunk.
    """

    text: str


@dataclass(kw_only=True)
class TextDone(StreamBlock):
    """Complete text from a text-streaming section.

    :param full_text: The complete accumulated text.
    :param has_code_blocks: Whether the text contains fenced code blocks.
    """

    full_text: str
    has_code_blocks: bool = False
    async_user_input: AsyncUserInput | None = None


# ── Reasoning ────────────────────────────────────────────


@dataclass(kw_only=True)
class ReasoningStartBlock(StreamBlock):
    """Reasoning has started — show a thinking indicator."""


@dataclass(kw_only=True)
class ReasoningChunk(StreamBlock):
    """An incremental reasoning chunk — analog of :class:`TextChunk`.

    Emitted while reasoning is still in progress so renderers can
    show live progress (e.g. Codex's command/reasoning stream during
    the long tool-call window). The eventual :class:`ReasoningBlock`
    is suppressed when any chunks were emitted, to avoid the
    formatter re-rendering the same text as a summary panel.

    :param text: The incremental reasoning text, e.g. one flushed
        line of summary tokens.
    """

    text: str


@dataclass(kw_only=True)
class ReasoningBlock(StreamBlock):
    """A completed reasoning/thinking block.

    Emitted only when no :class:`ReasoningChunk` was streamed for
    this reasoning section. Carries the full accumulated reasoning
    so non-streaming renderers (logs, web UIs that prefer cards)
    still get a single summary block.

    :param reasoning_text: The raw reasoning text.
    :param summary_text: A summary of the reasoning.
    """

    reasoning_text: str
    summary_text: str


# ── Status ───────────────────────────────────────────────


@dataclass(kw_only=True)
class ErrorBlock(StreamBlock):
    """An error during the response.

    :param message: The free-form error message from the server's
        :class:`ErrorInfo`. May be empty when the server emitted
        ``response.error`` without populating it — renderers should
        fall back to :attr:`code` in that case so the user sees at
        least the error classification instead of a blank panel.
    :param source: Where the error originated, e.g. ``"llm"``.
    :param code: The machine-readable error code from the server's
        :class:`ErrorInfo` payload (e.g. ``"llm_auth_failed"``,
        ``"executor_error"``). Empty when the server omitted it.
        Carried separately from ``message`` so renderers can show
        a useful label even when the free-form message is blank.
    """

    message: str
    source: str
    code: str = ""


@dataclass(kw_only=True)
class RetryBlock(StreamBlock):
    """The server is retrying.

    :param source: What is being retried, e.g. ``"tool"``.
    :param attempt: Current attempt number.
    :param max_attempts: Maximum retry attempts.
    :param delay_seconds: Delay before the next attempt.
    """

    source: str
    attempt: int
    max_attempts: int
    delay_seconds: float


@dataclass(kw_only=True)
class CompactionBlock(StreamBlock):
    """Conversation is being compacted."""


@dataclass(kw_only=True)
class FileBlock(StreamBlock):
    """A file artifact produced by the agent.

    :param file_id: Server-assigned file ID.
    :param filename: Original filename, or ``None`` if unknown.
    """

    file_id: str
    filename: str | None = None


@dataclass(kw_only=True)
class ResponseEndBlock(StreamBlock):
    """The response reached a terminal state.

    :param status: Terminal status, e.g. ``"completed"`` or ``"failed"``.
    :param response: The full response object, or ``None``.
    """

    status: str
    response: Response | None = None


# Union of all block types (for type hints).
AnyBlock = (
    ResponseStartBlock
    | ToolGroup
    | ToolResultBlock
    | NativeToolBlock
    | TextChunk
    | TextDone
    | ReasoningStartBlock
    | ReasoningChunk
    | ReasoningBlock
    | ErrorBlock
    | RetryBlock
    | CompactionBlock
    | FileBlock
    | ResponseEndBlock
)
