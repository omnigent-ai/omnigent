"""Session helper — tracks conversation state for interactive use."""

from __future__ import annotations

import contextlib
import mimetypes
import pathlib
from collections.abc import AsyncIterator, Callable, Iterator

# Import at the type level to avoid circular imports at runtime.
from typing import TYPE_CHECKING, Any, Literal, overload

from ._events import (
    ResponseCancelled,
    ResponseCompleted,
    ResponseCreated,
    ResponseFailed,
    ResponseIncomplete,
    StreamEvent,
)
from ._query import QueryResult, QueryStream
from ._tool_handler import StreamHooks, ToolHandler
from ._types import File, Response

if TYPE_CHECKING:
    from ._client import OmnigentClient


_TERMINAL_STATUSES = frozenset({"completed", "failed", "incomplete", "cancelled"})
_VALID_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)


class Session:
    """Tracks conversation state for interactive use.

    Holds the model name, last response ID, and whether the current
    response is still running. ``send()`` automatically steers if
    a response is in progress, or starts a new turn if the response
    is terminal.

    :param client: The underlying :class:`OmnigentClient`.
    :param model: Agent name to use for requests.
    :param tool_handler: Optional client-side tool execution config.
    :param hooks: Optional lifecycle hooks.
    """

    def __init__(
        self,
        client: OmnigentClient,
        model: str,
        tool_handler: ToolHandler | None = None,
        hooks: StreamHooks | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._tool_handler = tool_handler
        self._hooks = hooks
        self._previous_response_id: str | None = None
        self._current_response_id: str | None = None
        self._is_terminal: bool = True
        self._reasoning_effort: str | None = None
        # Session-local /model override. Forwarded as model_override
        # on POST /v1/responses. Mirrors _reasoning_effort's shape.
        self._model_override: str | None = None
        # Lazily populated from the first /v1/responses event so
        # produced-file lookups can use the session-scoped file endpoints.
        self._conversation_id: str | None = None

    @property
    def model(self) -> str:
        """The agent name for this session."""
        return self._model

    @property
    def current_response_id(self) -> str | None:
        """The most recent response ID, or None if no messages sent."""
        return self._current_response_id

    @property
    def is_streaming(self) -> bool:
        """True if a response is currently in progress."""
        return not self._is_terminal

    @property
    def reasoning_effort(self) -> str | None:
        """Current per-request reasoning-effort override, or None for default."""
        return self._reasoning_effort

    def set_reasoning_effort(self, effort: str | None) -> None:
        """Set or clear the per-request reasoning-effort override."""
        if effort is not None:
            normalized = effort.strip().lower()
            if normalized not in _VALID_REASONING_EFFORTS:
                valid = ", ".join(sorted(_VALID_REASONING_EFFORTS))
                raise ValueError(f"Invalid reasoning effort {effort!r}; expected one of: {valid}")
            effort = normalized
        self._reasoning_effort = effort

    def _reasoning_request(self) -> dict[str, str] | None:
        """Return the Responses API reasoning payload for this session."""
        if self._reasoning_effort is None:
            return None
        return {"effort": self._reasoning_effort}

    @property
    def model_override(self) -> str | None:
        """Current per-request LLM model override, or ``None`` for agent default."""
        return self._model_override

    def set_model_override(self, model: str | None) -> None:
        """
        Set or clear the per-request LLM model override.

        :param model: A non-empty model identifier, e.g.
            ``"databricks-claude-sonnet-4-6"``. Whitespace is trimmed;
            ``None`` clears so subsequent requests fall back to the
            agent spec's model.
        :raises ValueError: When *model* is empty after trimming. No
            other validation — server / runtime surfaces unknown-model
            errors at request time.
        """
        if model is not None:
            normalized = model.strip()
            if not normalized:
                raise ValueError("model override must be a non-empty string")
            model = normalized
        self._model_override = model

    async def send(
        self,
        input: str | list[dict[str, object]],
        *,
        files: list[str] | None = None,
        instructions: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Send a message — auto-steers if a response is in progress.

        Always returns an async iterator. The caller always does
        ``async for event in session.send(text): ...`` regardless
        of whether it steered or started a new turn.

        Three cases:

        1. **Response in progress, steer delivered**: The server
           accepted the message into the running agent's inbox.
           Yields nothing — the existing stream (from the original
           ``send()`` call) will surface the agent's reaction.

        2. **Response in progress, agent already finished**: The
           server created a new response instead of steering.
           Yields the full event stream for that new response.

        3. **No response in progress**: Starts a new turn. Yields
           the full event stream.

        :param input: User text or content block list.
        :param files: Optional file paths to upload and attach.
        :param instructions: Per-request system instructions.
        :yields: Stream events.
        """
        if files:
            input = await self._build_input_with_files(input, files)

        # Auto-steer if response is in progress.
        if not self._is_terminal and self._current_response_id is not None:
            steer_resp = await self._client.responses.steer(
                self._current_response_id,
                input if isinstance(input, str) else str(input),
                model=self._model,
                reasoning=self._reasoning_request(),
                model_override=self._model_override,
            )
            if steer_resp.id == self._current_response_id:
                # Case 1: steering delivered. Nothing to yield.
                return
                yield

            # Case 2: agent finished — server created a new response.
            # Stream it like a normal turn. The input was already
            # included in the steer POST, so the new response has it.
            async for event in self._stream_and_track(steer_resp.id, instructions):
                yield event
            return

        # Case 3: no response in progress — new turn.
        async for event in self._stream_and_track(
            None,
            instructions,
            input=input,
        ):
            yield event

    async def _stream_and_track(
        self,
        previous_response_id: str | None,
        instructions: str | None,
        *,
        input: str | list[dict[str, object]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a response and track session state.

        If ``input`` is None, uses an empty string (for steer-fallback
        where the input was already sent via the steer POST). If
        ``previous_response_id`` is None, uses the session's stored ID.
        """
        self._is_terminal = False
        prev_id = (
            previous_response_id
            if previous_response_id is not None
            else self._previous_response_id
        )
        actual_input: str | list[dict[str, object]] = input if input is not None else ""

        async for event in self._client.responses.stream(
            model=self._model,
            input=actual_input,
            previous_response_id=prev_id,
            tool_handler=self._tool_handler,
            hooks=self._hooks,
            instructions=instructions,
            reasoning=self._reasoning_request(),
            model_override=self._model_override,
        ):
            if isinstance(event, ResponseCreated):
                self._current_response_id = event.response.id
                if event.response.conversation is not None:
                    self._conversation_id = event.response.conversation.id

            if isinstance(
                event, ResponseCompleted | ResponseFailed | ResponseIncomplete | ResponseCancelled
            ):
                self._is_terminal = True
                self._previous_response_id = event.response.id
                self._current_response_id = event.response.id

            yield event

    @overload
    async def query(
        self,
        input: str | list[dict[str, object]],
        *,
        files: list[str] | None = ...,
        tools: list[Callable[..., Any]] | None = ...,
        stream: Literal[False] = ...,
    ) -> QueryResult: ...

    @overload
    async def query(
        self,
        input: str | list[dict[str, object]],
        *,
        files: list[str] | None = ...,
        tools: list[Callable[..., Any]] | None = ...,
        stream: Literal[True],
    ) -> QueryStream: ...

    async def query(
        self,
        input: str | list[dict[str, object]],
        *,
        files: list[str] | None = None,
        tools: list[Callable[..., Any]] | None = None,
        stream: bool = False,
    ) -> QueryResult | QueryStream:
        """Send a prompt and get text (plus any files) back.

        Non-streaming (default) returns a :class:`QueryResult`::

            result = await session.query("make me a chart")
            print(result.text)
            for f in result.files:
                await client.files.for_session("<session-id>").download(
                    f.id, f"./out/{f.filename}"
                )

        Streaming returns a :class:`QueryStream`::

            stream = await session.query("hello", stream=True)
            async for chunk in stream:
                print(chunk, end="", flush=True)
            # After iteration, stream.files holds the produced files.

        Client-side tools can be passed per-call via ``tools=``, or
        configured session-wide via ``client.session(tool_handler=...)``.
        If this turn's ``tools=`` is given, it OVERRIDES any session
        handler for this call only.

        :param input: User text or a list of content-block dicts,
            e.g. ``"hello"`` or
            ``[{"type": "input_text", "text": "hi"}]``.
        :param files: Optional list of local file paths to upload and
            attach to the turn, e.g. ``["./data.csv"]``.
        :param tools: Optional list of ``@tool``-decorated functions
            the agent may call on this turn. Overrides the session's
            configured ``tool_handler`` for this call only.
        :param stream: If True, return a :class:`QueryStream`. If
            False (default), return a :class:`QueryResult`.
        :returns: :class:`QueryResult` (``stream=False``) or
            :class:`QueryStream` (``stream=True``).
        :raises OmnigentError: If the response ends in an error.
        """
        if stream:
            return self._stream_query(input, files=files, tools=tools)
        return await self._collect_query(input, files=files, tools=tools)

    async def _collect_query(
        self,
        input: str | list[dict[str, object]],
        *,
        files: list[str] | None,
        tools: list[Callable[..., Any]] | None,
    ) -> QueryResult:
        """Run a turn; return final text + produced files as a QueryResult."""
        # Local imports avoid a circular dep at module load — _stream
        # imports _session under TYPE_CHECKING.
        from ._blocks import FileBlock, TextDone
        from ._stream import BlockStream
        from ._transforms import merge_text_across_iterations, pipe, skip_intermediate_ends

        with _per_call_tool_override(self, tools):
            block_stream = BlockStream()
            final_text = ""
            produced: list[File] = []
            async for block in pipe(
                block_stream.stream(self, input, files=files),
                # Merges per-iteration text into one TextDone per response,
                # so we don't truncate to just the last iteration's text.
                merge_text_across_iterations(),
                skip_intermediate_ends(),
            ):
                if isinstance(block, TextDone):
                    final_text = block.full_text
                elif isinstance(block, FileBlock):
                    produced.append(await self._get_file(block.file_id))
            return QueryResult(text=final_text, files=produced)

    def _stream_query(
        self,
        input: str | list[dict[str, object]],
        *,
        files: list[str] | None,
        tools: list[Callable[..., Any]] | None,
    ) -> QueryStream:
        """Build a QueryStream that yields text chunks and collects files."""
        produced: list[File] = []
        chunks = self._stream_chunks(input, files=files, tools=tools, produced=produced)
        return QueryStream(chunks=chunks, files=produced)

    async def _stream_chunks(
        self,
        input: str | list[dict[str, object]],
        *,
        files: list[str] | None,
        tools: list[Callable[..., Any]] | None,
        produced: list[File],
    ) -> AsyncIterator[str]:
        """Async generator backing QueryStream.

        Yields text chunks as they arrive. Appends produced files to
        the shared ``produced`` list as :class:`FileBlock` events
        surface, so the owning :class:`QueryStream` sees them through
        its reference to the same list.
        """
        from ._blocks import FileBlock, TextChunk
        from ._stream import BlockStream
        from ._transforms import pipe, skip_intermediate_ends

        with _per_call_tool_override(self, tools):
            block_stream = BlockStream()
            async for block in pipe(
                block_stream.stream(self, input, files=files),
                skip_intermediate_ends(),
            ):
                if isinstance(block, TextChunk):
                    yield block.text
                elif isinstance(block, FileBlock):
                    produced.append(await self._get_file(block.file_id))

    async def _get_file(self, file_id: str) -> File:
        """Fetch produced-file metadata from this session's file namespace."""
        if self._conversation_id is None:
            raise RuntimeError("cannot fetch produced file before conversation is known")
        return await self._client.files.for_session(self._conversation_id).get(file_id)

    async def cancel(self) -> Response | None:
        """Cancel the current in-progress response.

        :returns: The cancelled response, or None if no response is active.
        """
        if self._current_response_id is None:
            return None
        response = await self._client.responses.cancel(self._current_response_id)
        self._is_terminal = True
        self._previous_response_id = response.id
        return response

    def reset(self) -> None:
        """Reset the session — start a new conversation."""
        self._previous_response_id = None
        self._current_response_id = None
        self._is_terminal = True

    def resume_from_response(self, response_id: str) -> None:
        """Resume conversation from a specific response ID."""
        self._previous_response_id = response_id
        self._current_response_id = response_id
        self._is_terminal = True

    async def _build_input_with_files(
        self,
        text: str | list[dict[str, object]],
        file_paths: list[str],
    ) -> list[dict[str, object]]:
        """Upload files and build content blocks."""
        blocks: list[dict[str, object]] = []

        # Add text block.
        if isinstance(text, str) and text:
            blocks.append({"type": "input_text", "text": text})
        elif isinstance(text, list):
            blocks.extend(text)

        # Upload and add file blocks.
        for path in file_paths:
            if self._conversation_id is None:
                raise RuntimeError(
                    "file attachments require an established conversation; "
                    "use the sessions API for first-turn attachments"
                )
            uploaded = await self._client.files.for_session(self._conversation_id).upload(path)
            content_type = mimetypes.guess_type(path)[0]
            if content_type and content_type.startswith("image/"):
                blocks.append({"type": "input_image", "file_id": uploaded.id})
            else:
                blocks.append(
                    {
                        "type": "input_file",
                        "file_id": uploaded.id,
                        "filename": pathlib.Path(path).name,
                    }
                )

        return blocks


@contextlib.contextmanager
def _per_call_tool_override(
    session: Session,
    tools: list[Callable[..., Any]] | None,
) -> Iterator[None]:
    """Temporarily override ``session._tool_handler`` for one call.

    If ``tools`` is ``None``, the session's configured handler is
    used unchanged. Otherwise a handler is built from the decorated
    functions and swapped in for the duration of the ``with`` block;
    the original is restored on exit, even on exception.

    :param session: The session whose ``_tool_handler`` to override.
    :param tools: List of ``@tool``-decorated functions, or ``None``
        to leave the session's handler in place.
    """
    if tools is None:
        yield
        return
    from .tools import build_tool_handler

    previous = session._tool_handler
    session._tool_handler = build_tool_handler(tools)
    try:
        yield
    finally:
        session._tool_handler = previous
