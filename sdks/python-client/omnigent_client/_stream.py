"""BlockStream — event dispatch state machine that emits semantic blocks.

Consumes the raw event stream from ``session.send()`` and produces
a stream of typed blocks with context. Each block carries a
``BlockContext`` identifying which agent produced it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

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
    TextChunk,
    TextDone,
    ToolExecution,
    ToolGroup,
    ToolResultBlock,
)
from ._events import (
    CompactionInProgress,
    ErrorEvent,
    MessageDone,
    NativeToolCall,
    OutputFileDone,
    ReasoningDelta,
    ReasoningStarted,
    ReasoningSummaryDelta,
    ReasoningSummaryPartDone,
    ResponseCancelled,
    ResponseCompleted,
    ResponseCreated,
    ResponseFailed,
    ResponseIncomplete,
    ResponseInProgress,
    ResponseQueued,
    RetryEvent,
    TextDelta,
    ToolCall,
    ToolResult,
)

if TYPE_CHECKING:
    from ._session import Session


def format_tool_args_brief(name: str, arguments: dict[str, object]) -> str:
    """
    Format tool arguments for inline display next to the ``⏵ <name>``
    line.

    Public so frontends that re-render historical tool calls (e.g. the
    REPL's conversation-resume preview) can produce the same
    ``args_summary`` string the live stream produced when the call
    first ran. Without a single source of truth, "this is what you
    saw originally" diverges from "this is what the renderer
    chooses now" the moment either side changes.

    :param name: Tool name, e.g. ``"Read"``.
    :param arguments: Parsed arguments dict, e.g.
        ``{"file_path": "/x/y.py"}``.
    :returns: One-line summary, e.g. ``"y.py"`` for ``Read`` or the
        tool's primary user-meaningful field. Falls back to a JSON
        encoding of the full dict (truncated to 80 chars) for
        unrecognized tools.
    """
    if not arguments:
        return ""
    _KEYS = {
        "Read": "file_path",
        "Write": "file_path",
        "Edit": "file_path",
        "Bash": "command",
        "Glob": "pattern",
        "Grep": "pattern",
        "web_search": "query",
    }
    key = _KEYS.get(name)
    if key and key in arguments:
        s = str(arguments[key])
        if key == "file_path" and "/" in s:
            s = s.rsplit("/", 1)[-1]
        return s[:80] + "…" if len(s) > 80 else s
    try:
        s = json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(arguments)
    return s[:80] + "…" if len(s) > 80 else s


def _format_native_label(tool_type: str, data: dict[str, object]) -> str:
    """Format a native tool label."""
    if tool_type == "web_search_call":
        action = data.get("action")
        if isinstance(action, dict):
            at = action.get("type", "")
            if at == "search":
                return f"web search: {str(action.get('query', ''))[:80]}"
            if at == "open_page":
                return f"web open: {str(action.get('url', ''))[:80]}"
        return "web search"
    if tool_type == "mcp_call":
        n = data.get("name", "")
        return f"mcp: {n}" if n else "mcp call"
    return tool_type.replace("_", " ")


def _output_text_from_message_content(content: list[dict[str, object]]) -> str:
    """
    Extract completed assistant text from a message item.

    :param content: Message content blocks from ``MessageDone``.
    :returns: Concatenated ``output_text`` content.
    """
    parts: list[str] = []
    for block in content:
        if block.get("type") != "output_text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


class BlockStream:
    """Consumes a session event stream and emits semantic stream blocks.

    :param text_flush_threshold: Min chars to buffer before flushing
        on a word boundary. Default 30.
    """

    def __init__(self, text_flush_threshold: int = 30) -> None:
        self._flush_threshold = text_flush_threshold

    async def stream(
        self,
        session: Session,
        input: str | list[dict[str, object]],
        *,
        files: list[str] | None = None,
    ) -> AsyncIterator[AnyBlock]:
        """Stream semantic blocks for one turn.

        :param session: The :class:`Session` whose event stream to fold.
            The session's ``send()`` method is invoked internally.
        :param input: User text or a list of content-block dicts,
            e.g. ``"hello"`` or ``[{"type": "input_text", "text": "hi"}]``.
        :param files: Optional list of file paths to upload and attach
            to the turn, e.g. ``["./data.csv"]``.
        :returns: Async iterator of :class:`AnyBlock` values in order.
        """
        in_reasoning = False
        reasoning_text = ""
        summary_text = ""
        # Per-section accumulator for reasoning chunks awaiting a
        # newline / threshold flush. Mirrors ``accumulated`` for text.
        reasoning_accumulated = ""
        # Set when this reasoning section streamed any
        # ``ReasoningChunk``. The final ``ReasoningBlock`` is then
        # suppressed so renderers don't show the same text twice
        # (once live, once as a summary panel).
        reasoning_chunks_emitted = False
        in_text = False
        accumulated = ""
        full_text = ""
        pending_tools: dict[str, ToolExecution] = {}
        # Long-lived metadata for tool calls rendered in this stream.
        # Unlike ``pending_tools``, this survives text deltas so a
        # delayed ``ToolResult`` can still render with the original
        # tool name/arguments instead of being dropped.
        tool_executions_by_call_id: dict[str, ToolExecution] = {}
        # Dedup set: every ``call_id`` we've yielded a
        # ``ToolGroup`` for so far. Survives ``pending_tools``
        # clears (which fire on each ``TextDelta`` to flush
        # ``ToolResultBlock``s for completed tools), so the
        # post-stream action_required event for an MCP tool
        # whose inline observed event already rendered doesn't
        # silently re-render once the intervening text deltas
        # cleared ``pending_tools``. Call_ids are SDK-assigned
        # ``toolu_*`` ids — globally unique within this turn,
        # so the set never grows pathologically.
        seen_call_ids: set[str] = set()
        # Dedup set: every ``call_id`` we've yielded a
        # ``ToolResultBlock`` for. Same survives-clear semantics
        # as ``seen_call_ids`` for the call-line side, but on the
        # result side. Necessary because every action_required
        # tool's result fires TWICE on the SSE stream: once
        # inline from ``_dispatch_action_required`` (the moment
        # the dispatch returns) and once from the
        # ``response.completed`` flush in
        # ``_translate_omnigent_event``. Without this, the result
        # panel renders twice.
        seen_result_call_ids: set[str] = set()
        agent: str | None = None
        turn = 0
        started = False

        def _ctx() -> BlockContext:
            depth = agent.count(".") if agent else 0
            return BlockContext(agent=agent, depth=depth, turn=turn)

        async for event in session.send(input, files=files):
            # ── Response lifecycle ───────────────────
            if isinstance(event, ResponseCreated):
                # Tool calls were already yielded immediately. Emit
                # result-only groups for tools that got output between
                # ResponseCompleted and this ResponseCreated.
                for ex in list(pending_tools.values()):
                    if ex.output is not None:
                        yield ToolResultBlock(
                            name=ex.name,
                            call_id=ex.call_id,
                            agent_name=ex.agent_name,
                            output=ex.output,
                            arguments=ex.arguments,
                            args_summary=ex.args_summary,
                            ctx=_ctx(),
                        )
                pending_tools.clear()
                tool_executions_by_call_id.clear()
                agent = event.response.model
                if not started:
                    started = True
                    yield ResponseStartBlock(
                        model=agent,
                        response_id=event.response.id,
                        ctx=_ctx(),
                    )
                else:
                    turn += 1

            elif isinstance(event, ResponseQueued | ResponseInProgress):
                pass

            # ── Reasoning ────────────────────────────
            elif isinstance(event, ReasoningStarted):
                # Already reasoning = a new summarized-thinking block in the
                # same section. Flush this block's tail + a separator so
                # consecutive thought items don't run together.
                if in_reasoning:
                    yield ReasoningChunk(text=reasoning_accumulated + "\n\n", ctx=_ctx())
                    reasoning_accumulated = ""
                    reasoning_chunks_emitted = True
                    continue
                # Entering reasoning closes open text (symmetric with
                # TextDelta closing reasoning) — else interleaved
                # think→speak→think orphans + concatenates text.
                if in_text:
                    if accumulated:
                        yield TextChunk(text=accumulated, ctx=_ctx())
                        accumulated = ""
                    yield TextDone(
                        full_text=full_text,
                        has_code_blocks="```" in full_text,
                        ctx=_ctx(),
                    )
                    in_text = False
                    full_text = ""
                in_reasoning = True
                reasoning_text = ""
                summary_text = ""
                reasoning_accumulated = ""
                reasoning_chunks_emitted = False
                yield ReasoningStartBlock(ctx=_ctx())

            elif isinstance(event, ReasoningDelta | ReasoningSummaryDelta):
                # An out-of-order delta (no preceding ReasoningStarted)
                # marks an implicit start — Codex's bridged events
                # arrive this way. Emit the start block once so the
                # formatter has its "thinking…" anchor.
                if not in_reasoning:
                    # Close any open text first — same boundary as ReasoningStarted.
                    if in_text:
                        if accumulated:
                            yield TextChunk(text=accumulated, ctx=_ctx())
                            accumulated = ""
                        yield TextDone(
                            full_text=full_text,
                            has_code_blocks="```" in full_text,
                            ctx=_ctx(),
                        )
                        in_text = False
                        full_text = ""
                    in_reasoning = True
                    reasoning_text = ""
                    summary_text = ""
                    reasoning_accumulated = ""
                    reasoning_chunks_emitted = False
                    yield ReasoningStartBlock(ctx=_ctx())
                if isinstance(event, ReasoningDelta):
                    reasoning_text += event.delta
                else:
                    summary_text += event.delta
                # Stream the delta as a ReasoningChunk so the TUI can
                # render mid-flight. Mirrors the TextDelta line/
                # threshold flush so chunks land on natural breaks.
                reasoning_accumulated += event.delta
                while "\n" in reasoning_accumulated:
                    line, reasoning_accumulated = reasoning_accumulated.split("\n", 1)
                    yield ReasoningChunk(text=line + "\n", ctx=_ctx())
                    reasoning_chunks_emitted = True
                if len(reasoning_accumulated) >= self._flush_threshold:
                    last_space = reasoning_accumulated.rfind(" ")
                    if last_space > 0:
                        yield ReasoningChunk(
                            text=reasoning_accumulated[: last_space + 1],
                            ctx=_ctx(),
                        )
                        reasoning_accumulated = reasoning_accumulated[last_space + 1 :]
                        reasoning_chunks_emitted = True

            elif isinstance(event, ReasoningSummaryPartDone):
                # A summary part (paragraph) finished. Codex reasoning
                # summaries stream as one continuous run of
                # ReasoningSummaryDelta with no newline and no fresh
                # ReasoningStarted between parts; the boundary arrives
                # out-of-band here. Flush this part's held tail and insert
                # a separator so the next part renders on its own line
                # instead of glued onto this one ("…names.I have…"). See
                # issue #654. Mirrors the ReasoningStarted-between-parts
                # branch above. No-op outside reasoning or before any
                # content streamed (no leading separator).
                if in_reasoning and (reasoning_accumulated or reasoning_chunks_emitted):
                    yield ReasoningChunk(text=reasoning_accumulated + "\n\n", ctx=_ctx())
                    reasoning_accumulated = ""
                    reasoning_chunks_emitted = True

            # ── Text ─────────────────────────────────
            elif isinstance(event, TextDelta):
                if in_reasoning:
                    in_reasoning = False
                    if reasoning_accumulated:
                        yield ReasoningChunk(
                            text=reasoning_accumulated,
                            ctx=_ctx(),
                        )
                        reasoning_chunks_emitted = True
                        reasoning_accumulated = ""
                    if not reasoning_chunks_emitted:
                        yield ReasoningBlock(
                            reasoning_text=reasoning_text,
                            summary_text=summary_text,
                            ctx=_ctx(),
                        )
                # Emit results for tools that completed.
                for ex in list(pending_tools.values()):
                    if ex.output is not None:
                        yield ToolResultBlock(
                            name=ex.name,
                            call_id=ex.call_id,
                            agent_name=ex.agent_name,
                            output=ex.output,
                            arguments=ex.arguments,
                            args_summary=ex.args_summary,
                            ctx=_ctx(),
                        )
                pending_tools.clear()

                in_text = True
                accumulated += event.delta
                full_text += event.delta

                while "\n" in accumulated:
                    line, accumulated = accumulated.split("\n", 1)
                    yield TextChunk(text=line + "\n", ctx=_ctx())

                if len(accumulated) >= self._flush_threshold:
                    last_space = accumulated.rfind(" ")
                    if last_space > 0:
                        yield TextChunk(text=accumulated[: last_space + 1], ctx=_ctx())
                        accumulated = accumulated[last_space + 1 :]

            # ── Tool calls ───────────────────────────
            elif isinstance(event, ToolCall):
                # Dedupe by call_id. Under the claude-sdk harness's
                # MCP path, a tool call surfaces as TWO ToolCall
                # events with correlated call_ids: an inline
                # observed event (status="completed") emitted as
                # the inner SDK parses the tool_use block, and a
                # post-stream action_required event emitted when
                # the SDK invokes the MCP-server handler. The
                # adapter (omnigent/runtime/harnesses/
                # _executor_adapter.py) threads the SDK's
                # tool_use_id through both so they share a
                # call_id; this block keeps the first occurrence
                # (the inline render) and drops the second so the
                # REPL doesn't render ``⏵ tool_name`` twice. See
                # designs/RUN_OMNIGENT_REPL_PARITY.md.
                #
                # Non-MCP paths emit exactly one ToolCall per
                # call_id, so the second-arrival branch never
                # fires for them.
                if event.call_id in seen_call_ids:
                    # Already rendered the ⏵ line for this call_id
                    # (e.g. via the inline observed event from the
                    # harness's content_block_stop, or via the
                    # ``ToolCallInProgress`` event the Omnigent server
                    # emits at action_required arrival). Re-register
                    # in pending_tools so the eventual ``ToolResult``
                    # can pair by call_id — the prior pending entry
                    # may have been cleared by the
                    # ``pending_tools.clear()`` that fires on every
                    # ``TextDelta`` between observed and
                    # action_required (or between action_required
                    # and the post-PATCH function_call_output).
                    execution = tool_executions_by_call_id.get(event.call_id)
                    if execution is None:
                        execution = ToolExecution(
                            name=event.name,
                            arguments=event.arguments,
                            args_summary=format_tool_args_brief(event.name, event.arguments),
                            call_id=event.call_id,
                            agent_name=event.agent_name,
                            executed_by="server",
                        )
                        tool_executions_by_call_id[event.call_id] = execution
                    pending_tools[event.call_id] = execution
                    continue
                seen_call_ids.add(event.call_id)

                if in_reasoning:
                    in_reasoning = False
                    if reasoning_accumulated:
                        yield ReasoningChunk(
                            text=reasoning_accumulated,
                            ctx=_ctx(),
                        )
                        reasoning_chunks_emitted = True
                        reasoning_accumulated = ""
                    if not reasoning_chunks_emitted:
                        yield ReasoningBlock(
                            reasoning_text=reasoning_text,
                            summary_text=summary_text,
                            ctx=_ctx(),
                        )
                if in_text:
                    if accumulated:
                        yield TextChunk(text=accumulated, ctx=_ctx())
                        accumulated = ""
                    yield TextDone(
                        full_text=full_text,
                        has_code_blocks="```" in full_text,
                        ctx=_ctx(),
                    )
                    in_text = False
                    full_text = ""

                execution = ToolExecution(
                    name=event.name,
                    arguments=event.arguments,
                    args_summary=format_tool_args_brief(event.name, event.arguments),
                    call_id=event.call_id,
                    agent_name=event.agent_name,
                    executed_by="server",
                )
                pending_tools[event.call_id] = execution
                tool_executions_by_call_id[event.call_id] = execution
                # Yield immediately so the user sees the tool call
                # before execution. output=None means the formatter
                # shows the call line but no result panel.
                yield ToolGroup(executions=[execution], ctx=_ctx())

            elif isinstance(event, ToolResult):
                if event.call_id in seen_result_call_ids:
                    # Already rendered the result panel. The late
                    # ``response.completed`` flush emission is
                    # redundant; drop it so a subsequent
                    # ``ResponseCreated`` doesn't yield it again
                    # via the pending_tools sweep.
                    continue
                ex = pending_tools.get(event.call_id) or tool_executions_by_call_id.get(
                    event.call_id
                )
                if ex is None:
                    # Result arrived after a TextDelta cleared
                    # ``pending_tools``. If the matching call line
                    # never rendered, we have no name/agent for the
                    # panel — drop the event rather than render
                    # with placeholders.
                    continue
                ex.output = event.output
                ex.executed_by = "client"
                if event.arguments:
                    ex.arguments = event.arguments
                # Yield the result panel IMMEDIATELY so multi-
                # tool turns don't bunch result rendering at
                # end-of-turn. Each dispatch's
                # ``_live_publish`` of the function_call_output
                # arrives here as the tool finishes.
                seen_result_call_ids.add(event.call_id)
                yield ToolResultBlock(
                    name=ex.name,
                    call_id=ex.call_id,
                    agent_name=ex.agent_name,
                    output=event.output,
                    arguments=ex.arguments,
                    args_summary=ex.args_summary,
                    ctx=_ctx(),
                )
                # Drop from ``pending_tools`` so the next
                # ``TextDelta`` / ``ResponseCreated`` sweep
                # doesn't re-yield this result.
                pending_tools.pop(event.call_id, None)

            # ── Native tools ─────────────────────────
            elif isinstance(event, NativeToolCall):
                yield NativeToolBlock(
                    tool_type=event.tool_type,
                    label=_format_native_label(event.tool_type, event.data),
                    data=event.data,
                    ctx=_ctx(),
                )

            # ── Message done ─────────────────────────
            elif isinstance(event, MessageDone):
                if in_reasoning:
                    in_reasoning = False
                    if reasoning_accumulated:
                        yield ReasoningChunk(
                            text=reasoning_accumulated,
                            ctx=_ctx(),
                        )
                        reasoning_chunks_emitted = True
                        reasoning_accumulated = ""
                    if not reasoning_chunks_emitted:
                        yield ReasoningBlock(
                            reasoning_text=reasoning_text,
                            summary_text=summary_text,
                            ctx=_ctx(),
                        )
                if in_text:
                    if accumulated:
                        yield TextChunk(text=accumulated, ctx=_ctx())
                        accumulated = ""
                    yield TextDone(
                        full_text=full_text,
                        has_code_blocks="```" in full_text,
                        ctx=_ctx(),
                    )
                    in_text = False
                    full_text = ""
                else:
                    text = _output_text_from_message_content(event.content)
                    if text:
                        # No prior deltas: synthesize a TextChunk so
                        # TextChunk-only consumers (Session._stream_chunks)
                        # still see the message.
                        yield TextChunk(text=text, ctx=_ctx())
                        yield TextDone(
                            full_text=text,
                            has_code_blocks="```" in text,
                            ctx=_ctx(),
                        )

            # ── Status events ────────────────────────
            elif isinstance(event, CompactionInProgress):
                yield CompactionBlock(ctx=_ctx())

            elif isinstance(event, RetryEvent):
                yield RetryBlock(
                    source=event.source,
                    attempt=event.attempt,
                    max_attempts=event.max_attempts,
                    delay_seconds=event.delay_seconds,
                    ctx=_ctx(),
                )

            elif isinstance(event, ErrorEvent):
                # Pass ``code`` through too — renderers need it as a
                # fallback label when ``message`` is empty (otherwise
                # the error panel shows just ``[llm]`` with no hint
                # as to what went wrong).
                yield ErrorBlock(
                    message=event.error.message,
                    source=event.source,
                    code=event.error.code,
                    ctx=_ctx(),
                )

            elif isinstance(event, OutputFileDone):
                yield FileBlock(file_id=event.file_id, filename=event.filename, ctx=_ctx())

            # ── Terminal events ──────────────────────
            elif isinstance(
                event,
                ResponseCompleted | ResponseFailed | ResponseIncomplete | ResponseCancelled,
            ):
                if in_reasoning:
                    in_reasoning = False
                    if reasoning_accumulated:
                        yield ReasoningChunk(
                            text=reasoning_accumulated,
                            ctx=_ctx(),
                        )
                        reasoning_chunks_emitted = True
                        reasoning_accumulated = ""
                    if not reasoning_chunks_emitted:
                        yield ReasoningBlock(
                            reasoning_text=reasoning_text,
                            summary_text=summary_text,
                            ctx=_ctx(),
                        )
                if in_text:
                    if accumulated:
                        yield TextChunk(text=accumulated, ctx=_ctx())
                        accumulated = ""
                    yield TextDone(
                        full_text=full_text,
                        has_code_blocks="```" in full_text,
                        ctx=_ctx(),
                    )
                    in_text = False
                    full_text = ""

                yield ResponseEndBlock(
                    status=event.response.status,
                    response=event.response,
                    ctx=_ctx(),
                )
