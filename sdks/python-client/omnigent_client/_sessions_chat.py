"""Sessions-API-native chat helper.

A higher-level wrapper over :class:`omnigent_client._sessions.SessionsNamespace`
that mirrors enough of :class:`omnigent_client._session.Session`'s
public surface for downstream consumers (terminal REPL, ``omnigent
chat``, ``inner/cli.py``, …) to migrate without rewriting their event
loops, while being implemented entirely on top of ``/v1/sessions``
(no ``/v1/responses`` dependency).

The two helpers differ in one important way: the legacy
:class:`omnigent_client._session.Session` synthesizes a multi-turn
conversation by threading ``previous_response_id`` across one-shot
``/v1/responses`` calls. :class:`SessionsChat` instead binds to a
single durable session id once and re-uses it for every turn —
matching the server-side conversation lifecycle defined in
``omnigent/server/API.md`` ("Sessions API"). Per the same spec
there is no event replay; this helper opens a fresh SSE subscription
per :meth:`send` call, posts the input event, and yields the typed
:data:`omnigent.server.schemas.ServerStreamEvent` envelopes
until the turn's terminal ``response.*`` event arrives.

The helper does NOT re-export under the existing public name
``Session`` — that name already belongs to
:class:`omnigent_client._session.Session` and renaming would break
in-flight migrations. Instead we expose
:class:`SessionsChat` (chat helper) alongside the lower-level
:class:`SessionsNamespace` (raw HTTP wrapper).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import mimetypes
import pathlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, overload

from omnigent.server.schemas import (
    CancelledEvent,
    CompletedEvent,
    CreatedEvent,
    ElicitationRequestEvent,
    FailedEvent,
    IncompleteEvent,
    InProgressEvent,
    OutputFileDoneEvent,
    OutputItemDoneEvent,
    OutputTextDeltaEvent,
    QueuedEvent,
    ReasoningStartedEvent,
    ReasoningSummaryTextDeltaEvent,
    ReasoningTextDeltaEvent,
    ServerStreamEvent,
    SessionStatusEvent,
)

from ._errors import OmnigentError
from ._files import FilesNamespace
from ._query import QueryResult, QueryStream
from ._sessions import Session, SessionsNamespace
from ._tool_handler import (
    ElicitationRequestCtx,
    FileOutputCtx,
    MessageEndCtx,
    MessageStartCtx,
    ReasoningEndCtx,
    ReasoningStartCtx,
    ResponseEndCtx,
    ResponseStartCtx,
    StreamHooks,
    ToolCallEndCtx,
    ToolCallStartCtx,
)
from ._types import File, Response

# Text block types accepted when falling back from streamed deltas
# to final assistant message content.
_OUTPUT_TEXT_BLOCK_TYPES: frozenset[str] = frozenset({"output_text", "text"})

# Wire ``type`` literal that the input-message wire format uses for
# user-text events. Mirrors the ``"message"`` arm of
# :class:`omnigent.server.schemas.SessionEventInput`. Kept as a
# named constant so a single grep finds every emit/match site.
_MESSAGE_INPUT_TYPE: str = "message"

# Wire ``type`` literal for the function_call_output event posted back
# to the session after a client-side tool callable finishes. Mirrors
# the ``"function_call_output"`` arm of
# :class:`omnigent.server.schemas.SessionEventInput`. Kept as a
# named constant so a single grep finds every emit site.
_FUNCTION_CALL_OUTPUT_TYPE: str = "function_call_output"

# Wire ``type`` literal for committed function_call output items
# surfaced via :class:`OutputItemDoneEvent`. Matches
# ``ConversationItem.type`` for function-call items. Used inside
# :meth:`SessionsChat.send` to filter the dispatch path.
_FUNCTION_CALL_ITEM_TYPE: str = "function_call"

# Wire literal for the function_call status that signals the client
# must execute the tool and post a result back. Set by the runtime
# for tools whose spec carries
# ``runtime: client``. The dispatch loop in :meth:`SessionsChat.send`
# only reacts to items in this status — terminal function_call items
# (``status == "completed"``) are server-executed and need no
# client-side action.
_ACTION_REQUIRED_STATUS: str = "action_required"

# Wire literal for the spec ``runtime`` field that identifies a
# tool as client-executed. A spec tool entry with
# ``runtime: "client"`` declares that the runtime will surface its
# function_call items as ``action_required`` and the SDK must
# dispatch them to a caller-supplied callable. Tools with any other
# value (typically ``"server"``) are server-executed and require
# no callable on the SDK side.
_RUNTIME_CLIENT: str = "client"


@dataclass(frozen=True)
class SessionToolCallInfo:
    """
    Context passed to a client-side tool callable.

    Distinct from the legacy
    :class:`omnigent_client._tool_handler.ToolCallInfo` — that
    type is bound to the ``/v1/responses`` ``ToolHandler`` dispatch
    loop and carries a ``response_id`` / ``iteration`` /
    ``agent_name`` triple sourced from the responses-API event
    stream. The Sessions-API surface dispatches off
    :class:`OutputItemDoneEvent` items, which carry only ``id``,
    ``call_id``, ``name``, and ``arguments``. Defining a
    session-specific dataclass keeps the surface narrow and avoids
    populating fields with empty-string sentinels.

    :param name: Tool name as declared in the agent spec, e.g.
        ``"open_in_editor"``.
    :param arguments: Parsed JSON arguments dict, e.g.
        ``{"path": "foo.py"}``. The ``OutputItemDoneEvent``'s
        ``item.arguments`` field is a JSON string per OpenResponses
        wire format; this dataclass exposes the already-parsed
        dict so callables don't repeat the parse step. If the
        server emits malformed JSON, dispatch fails loud rather
        than silently passing an empty dict — see
        :meth:`SessionsChat._dispatch_tool_call`.
    :param call_id: Server-assigned tool-call id, e.g.
        ``"call_abc123"``. Echoed back on the
        ``function_call_output`` event so the runtime can correlate
        the result with the original call.
    :param item_id: Conversation-item id, e.g. ``"fc_abc123"``,
        or ``None`` if the server omits the field on the wire
        (older payloads). Distinct from ``call_id`` — the item id
        is the persisted store row, the call id is what the LLM
        uses to thread its request/response.
    """

    name: str
    arguments: dict[str, Any]
    call_id: str
    item_id: str | None


# Type alias for the callable signature accepted by
# ``tool_callables``. Sync or async permitted; a string return is
# expected (the server's ``function_call_output`` event carries a
# string ``output`` field). Aliased once so both the
# :class:`SessionsChat` parameter docstrings and the
# ``client.sessions_chat`` factory document the same shape.
ToolCallable = Callable[[SessionToolCallInfo], Awaitable[str] | str]


class _AgentToolsGetter(Protocol):
    """
    Protocol for the agent-tools fetcher injected into :class:`SessionsChat`.

    Returns the list of tool entries for an agent, where each entry
    is a dict carrying at least ``name`` and (post-F1) ``runtime``.
    The SDK reads ``entry.get("runtime")`` to decide which entries
    require a client-side callable; entries with no ``runtime``
    field default to server-executed and need no callable.

    Defined as a Protocol (not a bare ``Callable``) so the parameter
    name ``agent_id`` is preserved in hover docs and so the
    expected return shape is documented in one place.
    """

    async def __call__(self, agent_id: str, session_id: str | None = None) -> list[dict[str, Any]]:
        """
        Fetch the tool list for an agent.

        :param agent_id: Durable agent identifier, e.g.
            ``"ag_abc123"``.
        :param session_id: Session identifier for session-scoped
            lookups, e.g. ``"conv_abc123"``. ``None`` for legacy
            callers.
        :returns: The agent's tool entries from its spec, in
            declaration order. Empty list if the agent declares
            no tools.
        :raises Exception: Any error from the underlying transport
            (e.g. 404 if the agent no longer exists). Propagated to
            the caller of :meth:`SessionsChat.send`.
        """
        ...


# Concrete event classes that signal a turn's terminal state. Used
# by :meth:`SessionsChat.send` to know when to stop iterating the
# per-turn stream subscription. Matches the response-lifecycle
# terminal set listed in ``omnigent/server/schemas.py`` (and
# the ``_TERMINAL_STATUSES`` set in
# :mod:`omnigent_client._session`).
_TURN_TERMINAL_EVENT_TYPES: tuple[type[ServerStreamEvent], ...] = (
    CompletedEvent,
    FailedEvent,
    IncompleteEvent,
    CancelledEvent,
)

_RESPONSE_START_EVENT_TYPES: tuple[type[ServerStreamEvent], ...] = (
    CreatedEvent,
    QueuedEvent,
    InProgressEvent,
)

# Newer servers emit an immediate ``session.heartbeat`` after the
# live-tail subscriber is registered. Older servers do not, so keep a
# short fallback instead of hanging forever before posting the user's
# message.
_STREAM_READY_TIMEOUT_S: float = 1.0


@dataclass
class _StreamHookState:
    """
    Per-subscription hook bookkeeping.

    Sessions streams are long-lived and event-oriented, so lifecycle
    hooks need a tiny amount of local state to avoid duplicate starts
    and to pair reasoning/message end hooks with their starts.
    """

    started_response_ids: set[str] = field(default_factory=set)
    current_response_id: str = ""
    message_started: bool = False
    in_reasoning: bool = False
    reasoning_text: str = ""
    reasoning_summary_text: str = ""
    completed_tool_call_ids: set[str] = field(default_factory=set)


class SessionsChat:
    """
    Sessions-API-native chat helper bound to a single durable session.

    Usage mirrors :class:`omnigent_client._session.Session` for
    consumer-side migration parity::

        chat = await client.sessions_chat(bundle=b"...")
        async for event in chat.send("hello"):
            ...
        await chat.cancel()
        result = await chat.query("summarize")
        print(result.text)

    Construction is async because creating the underlying server
    session is an HTTP call. Use :meth:`OmnigentClient.sessions_chat`
    or the explicit factory :meth:`create` rather than calling the
    constructor directly — the constructor only wires already-resolved
    state and does NOT issue any HTTP requests.

    :param namespace: The :class:`SessionsNamespace` this helper
        delegates to. Borrowed from the parent
        :class:`OmnigentClient`; the chat helper does not own
        the underlying ``httpx.AsyncClient``.
    :param files_uploader: Async callable that uploads a local path
        and returns a :class:`File`. Typically
        ``client.files.for_session(session_id).upload``. Injected
        (rather than referencing the client directly) so unit tests
        can mock the upload boundary without standing up a full
        client. ``None`` means
        ``files=`` arguments to :meth:`send` / :meth:`query` will
        raise — flagging missing wiring loud rather than silently
        skipping uploads.
    :param files_getter: Async callable that fetches a :class:`File`
        by id. Used by :meth:`query` to materialize file artifacts
        emitted via :class:`OutputFileDoneEvent`. Same dependency-
        injection rationale as ``files_uploader``.
    :param session: The freshly created :class:`Session` snapshot
        returned by :meth:`SessionsNamespace.create`.
    :param tool_callables: Optional mapping from tool name to a
        sync/async callable that executes the tool. Required iff
        the agent's spec declares tools with ``runtime: "client"``;
        validated at stream-start (in :meth:`send`) rather than at
        construction time so callers can build the chat helper
        before knowing which tools the agent will surface. ``None``
        is equivalent to an empty dict.
    :param agent_tools_getter: Optional async callable that returns
        the agent's spec-declared tool entries given an
        ``agent_id``. Used by :meth:`send` to validate
        ``tool_callables`` against the spec at stream-start time.
        ``None`` is permitted — when no callable is wired, the
        validation is skipped entirely (so existing call sites
        that don't pass ``tool_callables`` keep working). When
        ``tool_callables`` is non-empty but ``agent_tools_getter``
        is ``None`` :meth:`send` raises rather than silently
        skipping validation.
    :param hooks: Optional lifecycle hooks fired from sessions stream
        events.
    """

    def __init__(
        self,
        namespace: SessionsNamespace,
        files_uploader: _FilesUploader | None,
        files_getter: _FilesGetter | None,
        session: Session,
        tool_callables: dict[str, ToolCallable] | None = None,
        agent_tools_getter: _AgentToolsGetter | None = None,
        hooks: StreamHooks | None = None,
    ) -> None:
        """
        Wire the chat helper around an already-created session.

        Prefer :meth:`create` over the constructor — see the class
        docstring for why.

        :param namespace: The :class:`SessionsNamespace` this helper
            delegates to.
        :param files_uploader: Optional file-upload callable; see
            class docstring.
        :param files_getter: Optional file-fetch callable; see
            class docstring.
        :param session: The :class:`Session` snapshot returned by
            :meth:`SessionsNamespace.create`.
        :param tool_callables: Optional name -> callable map for
            client-side tool execution; see class docstring.
        :param agent_tools_getter: Optional async fetcher for the
            agent's tool list; see class docstring.
        :param hooks: Optional lifecycle hooks fired from sessions
            stream events; see class docstring.
        """
        self._namespace = namespace
        self._files_uploader = files_uploader
        self._files_getter = files_getter
        self._session = session
        self._tool_callables: dict[str, ToolCallable] = (
            dict(tool_callables) if tool_callables else {}
        )
        self._agent_tools_getter = agent_tools_getter
        self._hooks = hooks or StreamHooks()
        # ``True`` once :meth:`_validate_tool_callables` has run for
        # this session. The check is idempotent and the result
        # invariant for a session bound to a single immutable
        # agent_id, so we cache the verdict to avoid hammering
        # ``client.agents.get`` on every ``send()`` call.
        self._tool_callables_validated: bool = False

    @classmethod
    async def create(
        cls,
        namespace: SessionsNamespace,
        bundle: bytes,
        *,
        filename: str = "agent.tar.gz",
        files_uploader: _FilesUploader | None = None,
        files_getter: _FilesGetter | None = None,
        files_namespace: FilesNamespace | None = None,
        tool_callables: dict[str, ToolCallable] | None = None,
        agent_tools_getter: _AgentToolsGetter | None = None,
        hooks: StreamHooks | None = None,
    ) -> SessionsChat:
        """
        Create a new server-side session and return a chat helper bound to it.

        Calls :meth:`SessionsNamespace.create` once, then constructs
        a :class:`SessionsChat` over the resulting :class:`Session`.

        :param namespace: The :class:`SessionsNamespace` to delegate
            to, e.g. ``client.sessions``.
        :param bundle: Gzipped agent tarball bytes uploaded through
            multipart ``POST /v1/sessions``.
        :param filename: Filename for the multipart upload, e.g.
            ``"agent.tar.gz"``.
        :param files_uploader: Optional file-upload callable, e.g.
            ``client.files.for_session(session_id).upload``.
            ``None`` (the default) means ``files=`` arguments to
            :meth:`send` / :meth:`query` will raise.
        :param files_getter: Optional file-fetch callable, e.g.
            ``client.files.for_session(session_id).get``.
        :param files_namespace: Optional unbound files namespace.
            When provided, :meth:`create` binds it to the newly
            created session and uses the session-scoped upload/get
            methods.
        :param tool_callables: Optional name -> callable map for
            client-side tool execution. Validated against the
            agent's spec on the first :meth:`send` call.
        :param agent_tools_getter: Optional async fetcher returning
            the agent's tool list (with the spec ``runtime`` field
            on each entry). Used to validate ``tool_callables``
            against the spec at stream-start time.
        :param hooks: Optional lifecycle hooks fired from sessions
            stream events.
        :returns: A :class:`SessionsChat` bound to the newly created
            session.
        :raises OmnigentError: If session creation fails.
        """
        session = await namespace.create(bundle, filename=filename)
        if files_namespace is not None:
            session_files = files_namespace.for_session(session.id)
            files_uploader = session_files.upload
            files_getter = session_files.get
        return cls(
            namespace=namespace,
            files_uploader=files_uploader,
            files_getter=files_getter,
            session=session,
            tool_callables=tool_callables,
            agent_tools_getter=agent_tools_getter,
            hooks=hooks,
        )

    @property
    def session_id(self) -> str:
        """
        The durable session identifier this helper is bound to.

        :returns: The session id, e.g. ``"conv_abc123"``.
        """
        return self._session.id

    @property
    def agent_id(self) -> str:
        """
        The bound agent's durable identifier.

        :returns: The agent id, e.g. ``"ag_abc123"``.
        """
        return self._session.agent_id

    @property
    def status(self) -> str:
        """
        Last-known session status from the most recent snapshot.

        Note: this is point-in-time — the actual server-side status
        may have advanced since :meth:`create`/:meth:`refresh` was
        last called. Re-call :meth:`refresh` for a fresh value.

        :returns: One of ``"idle"``, ``"running"``, or ``"failed"``.
        """
        return self._session.status

    async def refresh(self) -> Session:
        """
        Fetch a fresh snapshot from the server and update internal state.

        Calls :meth:`SessionsNamespace.get` and replaces the cached
        :class:`Session`. Useful after a reconnect to reconcile
        local state with the server.

        :returns: The freshly fetched :class:`Session` snapshot.
        :raises OmnigentError: If the session no longer exists
            (404) or another HTTP error occurs.
        """
        self._session = await self._namespace.get(self._session.id)
        return self._session

    async def send(
        self,
        input: str | list[dict[str, Any]],
        *,
        files: list[str] | None = None,
    ) -> AsyncIterator[ServerStreamEvent]:
        """
        Post a user message to the session and yield typed events for the turn.

        Opens a fresh SSE subscription (multiple subscribers are
        permitted by the server contract — see API.md "Stream
        Session"), posts the message event, and yields each
        :data:`ServerStreamEvent` until a turn-terminal event
        (:class:`CompletedEvent`, :class:`FailedEvent`,
        :class:`IncompleteEvent`, or :class:`CancelledEvent`) is
        observed. The subscription is closed once the terminal
        event has been yielded.

        Always returns an async iterator: the caller does
        ``async for event in chat.send("hi"): ...`` regardless of
        whether the message triggered an immediate turn or was
        appended to an active turn's queue (the server picks the
        right path; the client just observes events).

        :param input: User text or a list of OpenResponses-style
            content blocks, e.g. ``"hello"`` or
            ``[{"type": "input_text", "text": "hi"}]``.
        :param files: Optional list of local paths to upload and
            attach as ``input_file``/``input_image`` blocks. Requires
            ``files_uploader`` to have been wired at construction
            time.
        :yields: Typed :data:`ServerStreamEvent` instances in
            arrival order, validated by
            :class:`pydantic.TypeAdapter` inside
            :meth:`SessionsNamespace.stream`.
        :raises OmnigentError: If the underlying ``post_event`` or
            ``stream`` call fails (e.g. 404 if the session was
            deleted out from under us).
        :raises RuntimeError: If ``files`` is non-empty but no
            ``files_uploader`` was wired at construction.
        :raises ValueError: If the agent's spec declares a
            client-side tool that has no matching entry in
            ``tool_callables``, or if ``tool_callables`` carries a
            name not declared as a client-runtime tool in the spec.
        """
        # Validate ``tool_callables`` against the agent's
        # spec-declared tools BEFORE opening the SSE stream. Doing
        # this here (rather than in __init__) means construction is
        # cheap and the consumer can build the chat helper before
        # knowing which agent's tool list it needs to satisfy. The
        # helper caches the verdict so subsequent ``send()`` /
        # ``query()`` / ``stream()`` calls don't re-fetch the spec.
        await self._validate_tool_callables()

        content = await self._build_content(input, files)
        event_payload = {
            "type": _MESSAGE_INPUT_TYPE,
            "data": {"role": "user", "content": content},
        }

        # Subscription/post ordering: per API.md "Reconnect Contract",
        # the server has no replay buffer. ``stream()`` returns an
        # async iterator whose HTTP connection opens on first
        # ``__anext__``; constructing the iterator is not enough. Start
        # the first read and, when the server provides an immediate
        # ready/heartbeat event, wait for it before posting so fast
        # turns cannot publish all output before this subscriber exists.
        stream_aiter = self._namespace.stream(self._session.id)
        hook_state = _StreamHookState()
        first_event_task = asyncio.create_task(stream_aiter.__anext__())
        try:
            first_event: ServerStreamEvent | None
            try:
                first_event = await asyncio.wait_for(
                    asyncio.shield(first_event_task),
                    timeout=_STREAM_READY_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                first_event = None
            except StopAsyncIteration:
                return
            await self._namespace.post_event(self._session.id, event_payload)
            if first_event is None:
                try:
                    event = await first_event_task
                except StopAsyncIteration:
                    return
            else:
                event = first_event
            while True:
                await self._fire_stream_hooks(event, hook_state)
                yield event
                # Dispatch client-side tool calls inline, BEFORE
                # checking the terminal flag. The server emits
                # action_required function_call items mid-turn
                # (the turn parks waiting for the
                # function_call_output post); a terminal event
                # only arrives after the parked turn resumes and
                # completes. Filtering on
                # ``status == action_required`` is what
                # distinguishes spec-declared client tools (we
                # must execute) from server-executed function
                # calls whose output items arrive with
                # ``status == "completed"`` and need no client
                # action.
                if isinstance(event, OutputItemDoneEvent):
                    await self._maybe_dispatch_tool_call(event, hook_state)
                if isinstance(event, _TURN_TERMINAL_EVENT_TYPES):
                    return
                # A SETUP-phase failure (spec resolution, spawn-env
                # build) ends the turn before the LLM stream starts, so
                # no response.failed / FailedEvent is ever emitted — the
                # only terminal signal is ``session.status: failed``.
                # Raise loud with the carried error message so headless
                # callers (``-p``) surface the failure instead of
                # blocking until the stream closes and returning empty
                # text. The event is yielded above first so a push-based
                # consumer still observes it before the raise.
                if isinstance(event, SessionStatusEvent) and event.status == "failed":
                    message = (
                        event.error.message
                        if event.error is not None and event.error.message
                        else "turn failed"
                    )
                    code = event.error.code if event.error is not None else None
                    raise OmnigentError(message, code=code)
                try:
                    event = await stream_aiter.__anext__()
                except StopAsyncIteration:
                    return
        finally:
            if not first_event_task.done():
                first_event_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await first_event_task
            # Closing the underlying generator releases the SSE
            # connection promptly; httpx would GC it otherwise but
            # an explicit close avoids holding the pool slot until
            # GC. ``aclose`` is the standard async-generator API.
            await _aclose(stream_aiter)

    async def _validate_tool_callables(self) -> None:
        """
        Check ``tool_callables`` against the agent's spec-declared tools.

        Idempotent: only fetches the spec once per session. The
        check fails loud (``ValueError``) on either side of the
        spec/callables symmetry:

        * a spec tool with ``runtime: "client"`` that has no
          matching key in ``tool_callables`` — the agent will
          park forever waiting for an output we can never produce.
        * a key in ``tool_callables`` that does not name a
          ``runtime: "client"`` tool — the callable will never
          fire, and silently ignoring it would mask a config bug
          (typically a typo in the tool name).

        :raises ValueError: On either symmetry violation, with a
            message naming the offending tool(s).
        :raises RuntimeError: If ``tool_callables`` is non-empty
            but no ``agent_tools_getter`` was wired — silently
            skipping validation in that case would defeat the
            whole point of the check.
        """
        if self._tool_callables_validated:
            return

        # Fast path: nothing to validate. Skips the agent fetch
        # entirely so the common no-client-tools case stays
        # zero-overhead.
        if not self._tool_callables and self._agent_tools_getter is None:
            self._tool_callables_validated = True
            return

        if self._tool_callables and self._agent_tools_getter is None:
            raise RuntimeError(
                "SessionsChat received tool_callables but no "
                "agent_tools_getter was wired at construction. "
                "Pass agent_tools_getter=... when calling "
                "SessionsChat.create() (or use "
                "OmnigentClient.sessions_chat which wires it "
                "for you) so the SDK can validate the callables "
                "against the agent's spec."
            )

        # ``_agent_tools_getter`` is non-None here because the
        # earlier guards eliminate the (None getter, any
        # callables) cases.
        assert self._agent_tools_getter is not None
        tools = await self._agent_tools_getter(self._session.agent_id, self._session.id)
        client_tool_names = _extract_client_tool_names(tools)

        missing = client_tool_names - set(self._tool_callables.keys())
        extra = set(self._tool_callables.keys()) - client_tool_names
        if missing or extra:
            raise ValueError(_format_validation_error(missing, extra))

        self._tool_callables_validated = True

    async def _maybe_dispatch_tool_call(
        self,
        event: OutputItemDoneEvent,
        hook_state: _StreamHookState,
    ) -> None:
        """
        If the event carries a client-side tool call, run it and post the result.

        Filters for ``item.type == "function_call"`` AND
        ``item.status == "action_required"``. Any other shape is
        ignored — message items, completed function_call items
        (those are server-executed), and so on.

        Once the callable returns a string, posts a
        ``function_call_output`` event back into the session so
        the parked turn can resume.

        :param event: The :class:`OutputItemDoneEvent` to inspect.
        :param hook_state: Per-subscription hook state used to pair
            client-executed tool starts with their local result and
            suppress duplicate end hooks if the server later echoes the
            same ``function_call_output`` item.
        :raises KeyError: If the action-required item is missing
            its ``call_id`` or ``name`` field — the wire shape is
            non-negotiable, so failing loud is correct.
        :raises ValueError: If no callable is registered for the
            tool name. Validation in
            :meth:`_validate_tool_callables` should have caught
            this before the stream opened, so reaching here means
            the server emitted an unexpected tool name.
        """
        item = event.item
        if item.get("type") != _FUNCTION_CALL_ITEM_TYPE:
            return
        if item.get("status") != _ACTION_REQUIRED_STATUS:
            return

        info = _build_tool_call_info(item)
        callable_for_tool = self._tool_callables.get(info.name)
        if callable_for_tool is None:
            raise ValueError(
                f"SessionsChat received an action_required "
                f"function_call for tool {info.name!r} but no "
                f"callable is registered for that name. Either "
                f"the spec validation in "
                f"_validate_tool_callables was bypassed (bug) or "
                f"the server emitted a tool name not declared in "
                f"the agent spec."
            )

        output_str = await _invoke_callable(callable_for_tool, info)
        hook_state.completed_tool_call_ids.add(info.call_id)
        await _call_hook(
            self._hooks.on_tool_call_end,
            ToolCallEndCtx(
                name=info.name,
                call_id=info.call_id,
                agent_name="",
                output=output_str,
            ),
        )
        await self._namespace.post_event(
            self._session.id,
            {
                "type": _FUNCTION_CALL_OUTPUT_TYPE,
                "data": {"call_id": info.call_id, "output": output_str},
            },
        )

    async def cancel(self) -> None:
        """
        Interrupt the running turn (if any).

        Convenience wrapper over :meth:`SessionsNamespace.interrupt`.
        Idempotent server-side: cancelling an already-idle session
        is harmless from the server's perspective (the
        ``session.interrupted`` event is published unconditionally).

        :raises OmnigentError: If the session does not exist
            (404).
        """
        await self._namespace.interrupt(self._session.id)

    async def post_event(self, event: dict[str, Any]) -> None:
        """
        Low-level: post an arbitrary event into the session.

        Most callers want :meth:`send` (for user messages) or
        :meth:`cancel` (for interrupts). This is the escape hatch
        for posting tool outputs, approvals, or other event types
        the server understands. The body is forwarded as-is to
        :meth:`SessionsNamespace.post_event`.

        :param event: Event dict with ``type`` and ``data`` keys,
            e.g. ``{"type": "function_call_output", "data":
            {"call_id": "...", "output": "..."}}``. Validated
            server-side per ``type``.
        :raises OmnigentError: If the session does not exist
            (404) or the event is rejected (400).
        """
        await self._namespace.post_event(self._session.id, event)

    async def stream(self) -> AsyncIterator[ServerStreamEvent]:
        """
        Subscribe to the live SSE stream for this session.

        Like :meth:`send`, this validates ``tool_callables`` against
        the agent's spec at stream-start (the first call) before
        opening the SSE connection, and dispatches any
        ``action_required`` ``function_call`` items the server
        emits to the registered callables. Unlike :meth:`send`, no
        user message is posted — this is the long-lived
        subscription path for callers that drive the session via
        :meth:`post_event` or another producer. Iteration ends
        when the server closes the stream (``[DONE]``).

        :returns: An async iterator of typed
            :data:`ServerStreamEvent` instances.
        :raises OmnigentError: If the session does not exist
            (404) when opening the stream.
        :raises ValueError: If the agent's spec declares a
            client-side tool that has no matching entry in
            ``tool_callables``, or if ``tool_callables`` carries a
            name not declared as a client-runtime tool in the spec.
        :raises RuntimeError: If ``tool_callables`` is non-empty
            but no ``agent_tools_getter`` was wired.
        """
        # Validate BEFORE opening the SSE stream — same contract as
        # :meth:`send`. Skipping this for raw ``stream()`` callers
        # would let a misconfigured chat helper open a subscription,
        # observe an action_required event, and then either dispatch
        # to the wrong callable or silently drop the call.
        await self._validate_tool_callables()

        stream_aiter = self._namespace.stream(self._session.id)
        hook_state = _StreamHookState()
        try:
            async for event in stream_aiter:
                await self._fire_stream_hooks(event, hook_state)
                yield event
                # Same dispatch path as :meth:`send` — see the
                # comment there for the action_required vs completed
                # filter rationale.
                if isinstance(event, OutputItemDoneEvent):
                    await self._maybe_dispatch_tool_call(event, hook_state)
        finally:
            await _aclose(stream_aiter)

    async def _fire_stream_hooks(
        self,
        event: ServerStreamEvent,
        state: _StreamHookState,
    ) -> None:
        """
        Translate sessions SSE events into public ``StreamHooks`` callbacks.

        The legacy responses client already exposed these lifecycle hooks.
        This adapter keeps the sessions-first helper observability-compatible
        without changing the typed event stream consumers already iterate.
        """
        if isinstance(event, _RESPONSE_START_EVENT_TYPES):
            response = _response_from_server_object(event.response)
            await self._ensure_response_started(response, state)
            return

        if isinstance(event, ReasoningStartedEvent):
            state.in_reasoning = True
            state.reasoning_text = ""
            state.reasoning_summary_text = ""
            await _call_hook(self._hooks.on_reasoning_start, ReasoningStartCtx())
            return

        if isinstance(event, ReasoningTextDeltaEvent):
            state.reasoning_text += event.delta
            return

        if isinstance(event, ReasoningSummaryTextDeltaEvent):
            state.reasoning_summary_text += event.delta
            return

        if isinstance(event, OutputTextDeltaEvent):
            await self._end_reasoning_if_open(state)
            if not state.message_started:
                state.message_started = True
                await _call_hook(
                    self._hooks.on_message_start,
                    MessageStartCtx(response_id=state.current_response_id),
                )
            return

        if isinstance(event, OutputItemDoneEvent):
            await self._fire_output_item_hooks(event.item, state)
            return

        if isinstance(event, OutputFileDoneEvent):
            await _call_hook(
                self._hooks.on_file_output,
                FileOutputCtx(
                    file_id=event.file_id,
                    filename=event.filename,
                    content_type=event.content_type,
                ),
            )
            return

        if isinstance(event, ElicitationRequestEvent):
            await self._handle_elicitation_request(event, state)
            return

        if isinstance(event, _TURN_TERMINAL_EVENT_TYPES):
            await self._end_reasoning_if_open(state)
            response = _response_from_server_object(event.response)
            await self._ensure_response_started(response, state)
            await _call_hook(
                self._hooks.on_response_end,
                ResponseEndCtx(response=response, status=response.status),
            )

    async def _ensure_response_started(
        self,
        response: Response,
        state: _StreamHookState,
    ) -> None:
        """
        Fire ``on_response_start`` once for a response id.

        Some older sessions streams may omit ``response.created`` and
        surface only the terminal response snapshot. In that case the
        terminal path calls this before ``on_response_end`` so consumers
        still get a balanced lifecycle.
        """
        if response.id in state.started_response_ids:
            state.current_response_id = response.id
            return
        state.started_response_ids.add(response.id)
        state.current_response_id = response.id
        await _call_hook(self._hooks.on_response_start, ResponseStartCtx(response=response))

    async def _end_reasoning_if_open(self, state: _StreamHookState) -> None:
        """Fire ``on_reasoning_end`` if a reasoning block is currently open."""
        if not state.in_reasoning:
            return
        state.in_reasoning = False
        await _call_hook(
            self._hooks.on_reasoning_end,
            ReasoningEndCtx(
                reasoning_text=state.reasoning_text,
                summary_text=state.reasoning_summary_text,
            ),
        )

    async def _fire_output_item_hooks(
        self,
        item: dict[str, Any],
        state: _StreamHookState,
    ) -> None:
        """Fire hooks derived from a completed output item."""
        item_type = item.get("type")
        if item_type == "message":
            await self._end_reasoning_if_open(state)
            if not state.message_started:
                state.message_started = True
                await _call_hook(
                    self._hooks.on_message_start,
                    MessageStartCtx(response_id=state.current_response_id),
                )
            raw_content = item.get("content")
            content = raw_content if isinstance(raw_content, list) else []
            await _call_hook(self._hooks.on_message_end, MessageEndCtx(content=content))
            state.message_started = False
            return

        if item_type == _FUNCTION_CALL_ITEM_TYPE:
            await _call_hook(
                self._hooks.on_tool_call_start,
                ToolCallStartCtx(
                    name=str(item.get("name", "")),
                    arguments=_parse_hook_arguments(item.get("arguments", "{}")),
                    call_id=str(item.get("call_id", "")),
                    agent_name=str(item.get("agent_name", "")),
                    executed_by=(
                        "client" if item.get("status") == _ACTION_REQUIRED_STATUS else "server"
                    ),
                ),
            )
            return

        if item_type == _FUNCTION_CALL_OUTPUT_TYPE:
            call_id = str(item.get("call_id", ""))
            if call_id in state.completed_tool_call_ids:
                return
            await _call_hook(
                self._hooks.on_tool_call_end,
                ToolCallEndCtx(
                    name=str(item.get("name", "")),
                    call_id=call_id,
                    agent_name=str(item.get("agent_name", "")),
                    output=str(item.get("output", "")),
                ),
            )

    async def _handle_elicitation_request(
        self,
        event: ElicitationRequestEvent,
        state: _StreamHookState,
    ) -> None:
        """
        Route a sessions elicitation through ``on_elicitation_request``.

        No registered hook means fail-closed decline, matching the
        deprecated responses client and avoiding a parked workflow that
        waits forever for a decision the SDK will never send.
        """
        params = event.params
        accepted = await _invoke_elicitation_hook(
            self._hooks,
            ElicitationRequestCtx(
                elicitation_id=event.elicitation_id,
                message=params.message,
                requested_schema=params.requestedSchema or {},
                mode=params.mode,
                phase=params.phase or "",
                policy_name=params.policy_name or "",
                content_preview=params.content_preview or "",
                response_id=state.current_response_id,
                url=params.url,
            ),
        )
        target_session_id = params.target_session_id or self._session.id
        await self._namespace.resolve_elicitation(
            target_session_id,
            event.elicitation_id,
            {"action": "accept" if accepted else "decline"},
        )

    @overload
    async def query(
        self,
        input: str | list[dict[str, Any]],
        *,
        files: list[str] | None = ...,
        stream: Literal[False] = ...,
    ) -> QueryResult: ...

    @overload
    async def query(
        self,
        input: str | list[dict[str, Any]],
        *,
        files: list[str] | None = ...,
        stream: Literal[True],
    ) -> QueryStream: ...

    async def query(
        self,
        input: str | list[dict[str, Any]],
        *,
        files: list[str] | None = None,
        stream: bool = False,
    ) -> QueryResult | QueryStream:
        """
        Send a turn and collect (or stream) the assistant's text output.

        Non-streaming (default) returns a :class:`QueryResult` once
        the turn finishes::

            result = await chat.query("make me a chart")
            print(result.text)
            for f in result.files:
                ...

        Streaming returns a :class:`QueryStream` that yields text
        chunks as they arrive::

            stream = await chat.query("hello", stream=True)
            async for chunk in stream:
                print(chunk, end="", flush=True)
            print(stream.files)  # populated after iteration ends

        Implemented as a thin fold over :meth:`send`'s typed event
        stream. Text is concatenated from
        :class:`OutputTextDeltaEvent` deltas; file artifacts are
        materialized from :class:`OutputFileDoneEvent` events via
        ``files_getter`` (so the caller gets full :class:`File`
        objects, not just ids).

        :param input: User text or content-block list.
        :param files: Optional list of local paths to upload.
        :param stream: If True, return a :class:`QueryStream`;
            otherwise return a :class:`QueryResult`.
        :returns: :class:`QueryResult` or :class:`QueryStream`.
        :raises OmnigentError: If the underlying HTTP calls fail.
        :raises RuntimeError: If a file artifact is observed but no
            ``files_getter`` was wired (so the caller can't get the
            full :class:`File`).
        """
        if stream:
            return self._stream_query(input, files=files)
        return await self._collect_query(input, files=files)

    async def await_turn(self, *, timeout: float | None = 1200.0) -> QueryResult:
        """
        Collect the next auto-triggered turn without posting a message.

        Used by headless runners to follow async orchestrator sessions
        across multiple turns. Unlike :meth:`query`, no user message is
        posted — this helper subscribes to the live stream and waits for
        the server to start a new turn (e.g. an inbox auto-wake when a
        sub-agent completes), then collects its text output.

        A ``timeout`` guards against the race window where the turn
        already completed before the subscription opened. If no terminal
        event arrives within ``timeout`` seconds, the coroutine returns
        an empty :class:`QueryResult` so the caller can refresh session
        status and decide how to proceed.

        :param timeout: Seconds to wait for the turn to complete. ``None``
            waits indefinitely. Defaults to 1200 (20 minutes).
        :returns: :class:`QueryResult` with the turn's text (may be empty
            if the turn completed before we subscribed).
        :raises OmnigentError: Propagated from :meth:`stream` if the
            session does not exist.
        """
        text_parts: list[str] = []
        produced: list[File] = []

        async def _collect() -> None:
            async for event in self.stream():
                if isinstance(event, OutputTextDeltaEvent):
                    if event.delta:
                        text_parts.append(event.delta)
                elif isinstance(event, OutputItemDoneEvent) and not text_parts:
                    text_parts.extend(_assistant_text_from_output_item(event.item))
                elif isinstance(event, OutputFileDoneEvent):
                    produced.append(await self._fetch_file(event))
                elif isinstance(event, CompletedEvent):
                    if not text_parts:
                        text_parts.extend(_assistant_text_from_response(event.response.output))
                    break
                elif isinstance(event, _TURN_TERMINAL_EVENT_TYPES):
                    break

        try:
            async with asyncio.timeout(timeout):
                await _collect()
        except asyncio.TimeoutError:
            # Timeout is expected when the session completes before the deadline
            # or the race window is missed; return whatever text was collected so
            # far per the method contract (empty QueryResult is valid).
            pass
        return QueryResult(text="".join(text_parts), files=produced)

    async def _collect_query(
        self,
        input: str | list[dict[str, Any]],
        *,
        files: list[str] | None,
    ) -> QueryResult:
        """
        Drain :meth:`send` and assemble a :class:`QueryResult`.

        Concatenates :class:`OutputTextDeltaEvent` payloads in
        arrival order, with assistant message items / terminal
        response output as a provider fallback when no deltas are
        emitted. File artifacts go through ``files_getter``; if
        missing, raises loud rather than returning bare ids.

        :param input: User text or content blocks.
        :param files: Optional local paths to upload.
        :returns: :class:`QueryResult` with joined text and full
            :class:`File` entries.
        :raises RuntimeError: If a file artifact is observed but no
            ``files_getter`` was wired.
        """
        text_parts: list[str] = []
        produced: list[File] = []
        async for event in self.send(input, files=files):
            if isinstance(event, OutputTextDeltaEvent):
                if event.delta:
                    text_parts.append(event.delta)
            elif isinstance(event, OutputItemDoneEvent) and not text_parts:
                text_parts.extend(_assistant_text_from_output_item(event.item))
            elif isinstance(event, CompletedEvent) and not text_parts:
                text_parts.extend(_assistant_text_from_response(event.response.output))
            elif isinstance(event, OutputFileDoneEvent):
                produced.append(await self._fetch_file(event))
        return QueryResult(text="".join(text_parts), files=produced)

    def _stream_query(
        self,
        input: str | list[dict[str, Any]],
        *,
        files: list[str] | None,
    ) -> QueryStream:
        """
        Build a :class:`QueryStream` whose chunk iterator is backed by :meth:`send`.

        The shared file list is mutated as
        :class:`OutputFileDoneEvent` events arrive, matching the
        contract :class:`QueryStream` documents.

        :param input: User text or content blocks.
        :param files: Optional local paths to upload.
        :returns: A :class:`QueryStream` ready for iteration.
        """
        produced: list[File] = []
        chunks = self._stream_chunks(input, files=files, produced=produced)
        return QueryStream(chunks=chunks, files=produced)

    async def _stream_chunks(
        self,
        input: str | list[dict[str, Any]],
        *,
        files: list[str] | None,
        produced: list[File],
    ) -> AsyncIterator[str]:
        """
        Async generator that yields text deltas and side-effects file fetches.

        :param input: User text or content blocks.
        :param files: Optional local paths to upload.
        :param produced: Caller-owned list, mutated as files arrive.
        :yields: Assistant text fragments in arrival order.
        :raises RuntimeError: If a file artifact is observed but no
            ``files_getter`` was wired.
        """
        emitted_text = False
        async for event in self.send(input, files=files):
            if isinstance(event, OutputTextDeltaEvent):
                if event.delta:
                    emitted_text = True
                    yield event.delta
            elif isinstance(event, OutputItemDoneEvent) and not emitted_text:
                for text in _assistant_text_from_output_item(event.item):
                    emitted_text = True
                    yield text
            elif isinstance(event, CompletedEvent) and not emitted_text:
                for text in _assistant_text_from_response(event.response.output):
                    emitted_text = True
                    yield text
            elif isinstance(event, OutputFileDoneEvent):
                produced.append(await self._fetch_file(event))

    async def _fetch_file(self, event: OutputFileDoneEvent) -> File:
        """
        Resolve a :class:`File` from an :class:`OutputFileDoneEvent`.

        :param event: The event carrying the file id.
        :returns: The full :class:`File` object.
        :raises RuntimeError: If no ``files_getter`` was wired —
            failing loud so the caller knows the result list will
            not be populated correctly rather than silently
            dropping the artifact.
        """
        if self._files_getter is None:
            raise RuntimeError(
                "SessionsChat received an OutputFileDoneEvent but no "
                "files_getter was wired at construction. Pass "
                "a session-scoped files_getter when calling SessionsChat.create() "
                "to materialize file artifacts."
            )
        # We rely on the typed ``file_id`` accessor rather than
        # dict-poking so a future schema change surfaces as a clean
        # type error.
        return await self._files_getter(event.file_id)

    async def _build_content(
        self,
        input: str | list[dict[str, Any]],
        files: list[str] | None,
    ) -> list[dict[str, Any]]:
        """
        Normalize ``input`` + ``files`` into a content-block list.

        Accepts either a bare string or a pre-built content-block
        list, then appends ``input_file``/``input_image`` blocks
        for any uploaded files. Mirrors
        :meth:`omnigent_client._session.Session._build_input_with_files`
        so consumers see the same wire shape regardless of which
        helper they use.

        :param input: User text or content-block list.
        :param files: Optional local paths to upload.
        :returns: A list of content-block dicts ready for the wire.
        :raises RuntimeError: If ``files`` is non-empty but no
            ``files_uploader`` was wired.
        """
        blocks: list[dict[str, Any]] = []
        if isinstance(input, str):
            if input:
                blocks.append({"type": "input_text", "text": input})
        else:
            blocks.extend(input)

        if not files:
            return blocks

        if self._files_uploader is None:
            raise RuntimeError(
                "SessionsChat.send() received files= but no files_uploader "
                "was wired at construction. Pass "
                "a session-scoped files_uploader when calling SessionsChat.create() "
                "to enable file attachments."
            )

        for path in files:
            uploaded = await self._files_uploader(path)
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


# ── Type aliases for the injected callables ───────────────────────────
#
# Defined as Protocols rather than Callable[...] so the param names
# are documented and IDE hover lands on something meaningful.


class _FilesUploader(Protocol):
    """
    Protocol for the file-upload callable injected into :class:`SessionsChat`.

    Matches the signature of
    :meth:`omnigent_client._files.FilesNamespace.upload`. Defined
    as a Protocol (not a bare ``Callable``) so the parameter name
    is preserved in hover docs.
    """

    async def __call__(self, path: str) -> File:
        """
        Upload a local file and return its server-side :class:`File`.

        :param path: Local filesystem path to the file to upload,
            e.g. ``"./data.csv"``.
        :returns: The created :class:`File` object.
        """
        ...


class _FilesGetter(Protocol):
    """
    Protocol for the file-fetch callable injected into :class:`SessionsChat`.

    Matches the signature of
    :meth:`omnigent_client._files.FilesNamespace.get`.
    """

    async def __call__(self, file_id: str) -> File:
        """
        Fetch a file's metadata by id.

        :param file_id: Server-issued file identifier, e.g.
            ``"file_abc123"``.
        :returns: The :class:`File` metadata.
        """
        ...


# ── Helpers ───────────────────────────────────────────────────────────


def _response_from_server_object(server_response: Any) -> Response:
    """
    Convert a server schema ``ResponseObject`` into the SDK dataclass.

    Sessions streams use Pydantic schema objects while public hooks
    expose SDK dataclasses. Converting at this boundary preserves the
    hook API shared with the legacy responses client.
    """
    if hasattr(server_response, "model_dump"):
        data = server_response.model_dump(mode="json")
    elif isinstance(server_response, dict):
        data = server_response
    else:
        data = {}
    return Response.from_dict(data)


async def _call_hook(hook: Any, ctx: Any) -> Any:
    """Call a hook (sync or async) and return its result."""
    if hook is None:
        return None
    result = hook(ctx)
    if inspect.isawaitable(result):
        return await result
    return result


async def _invoke_elicitation_hook(
    hooks: StreamHooks,
    ctx: ElicitationRequestCtx,
) -> bool:
    """Invoke an elicitation hook, declining fail-closed on absence/error."""
    if hooks.on_elicitation_request is None:
        return False
    try:
        return bool(await _call_hook(hooks.on_elicitation_request, ctx))
    except Exception:
        return False


def _parse_hook_arguments(raw_args: object) -> dict[str, object]:
    """
    Best-effort argument parsing for hook context.

    Hook callbacks are observers. Malformed function-call arguments
    should not make the event stream itself fail before the stricter
    dispatch path has a chance to validate action-required calls.
    """
    if isinstance(raw_args, dict):
        return raw_args
    if not isinstance(raw_args, str) or not raw_args:
        return {}
    try:
        parsed = json.loads(raw_args)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_tool_call_info(item: dict[str, Any]) -> SessionToolCallInfo:
    """
    Parse an action_required ``function_call`` item into a typed info object.

    ``name`` and ``call_id`` are required wire fields (see
    ``omnigent/server/schemas.py:OutputItemDoneEvent``) —
    reading via ``[]`` makes a missing field surface as a
    ``KeyError``, which is the project's "fail loud" stance for
    required wire fields.

    ``arguments`` arrives as a JSON string per the OpenResponses
    wire format (matches the server's ``FunctionCallData`` shape).
    Parsed once here so the callable receives a ready-to-use dict.
    Some server paths synthesize an already-parsed dict — accept
    both shapes rather than forcing a re-serialize step.

    :param item: The wire-shape function_call item from
        :class:`OutputItemDoneEvent.item`.
    :returns: The parsed :class:`SessionToolCallInfo`.
    :raises KeyError: If ``name`` or ``call_id`` is missing from
        the item.
    :raises TypeError: If ``arguments`` is neither a JSON string
        nor a dict.
    :raises json.JSONDecodeError: If ``arguments`` is a
        non-empty string that doesn't parse as JSON.
    """
    name = item["name"]
    call_id = item["call_id"]
    raw_item_id = item.get("id")
    item_id: str | None = str(raw_item_id) if raw_item_id is not None else None

    raw_args = item.get("arguments", "{}")
    if isinstance(raw_args, str):
        parsed_args: dict[str, Any] = json.loads(raw_args) if raw_args else {}
    elif isinstance(raw_args, dict):
        parsed_args = raw_args
    else:
        raise TypeError(
            f"function_call arguments for {name!r} must be a "
            f"JSON string or dict, got {type(raw_args).__name__}"
        )
    return SessionToolCallInfo(
        name=name,
        arguments=parsed_args,
        call_id=call_id,
        item_id=item_id,
    )


async def _invoke_callable(
    callable_for_tool: ToolCallable,
    info: SessionToolCallInfo,
) -> str:
    """
    Invoke a tool callable (sync or async) and validate its return.

    Uses :func:`inspect.isawaitable` rather than
    :func:`inspect.iscoroutinefunction` because the callable may
    be a wrapper / partial / lambda returning a coroutine — the
    runtime check on the actual return value is what matters.

    :param callable_for_tool: The user-supplied callable.
    :param info: Context to pass to the callable.
    :returns: The string output to post back as the
        ``function_call_output``.
    :raises TypeError: If the callable returns (or resolves to)
        a non-string value. The server's
        ``function_call_output`` contract carries a string
        ``output`` field — accepting other types here would lead
        to a confusing 400 from the server later.
    """
    result = callable_for_tool(info)
    if inspect.isawaitable(result):
        output_str = await result
    else:
        output_str = result
    if not isinstance(output_str, str):
        raise TypeError(
            f"tool_callable for {info.name!r} must return a str "
            f"(or awaitable resolving to a str); got "
            f"{type(output_str).__name__}"
        )
    return output_str


def _assistant_text_from_output_item(item: dict[str, Any]) -> list[str]:
    """
    Extract assistant text from a streamed ``output_item.done`` item.

    Some harnesses may complete with text present only on the
    final assistant message item rather than as
    ``response.output_text.delta`` tokens. ``SessionsChat.query``
    is the high-level text API, so it must fold those message items
    in as well as deltas.

    :param item: The ``item`` dict from an
        :class:`OutputItemDoneEvent`, e.g. ``{"type": "message",
        "role": "assistant", "content": [...]}``.
    :returns: Text blocks from assistant message items. Empty for
        non-message items, non-assistant messages, or malformed
        content.
    """
    if item.get("type") != "message" or item.get("role") != "assistant":
        return []
    return _assistant_text_from_content(item.get("content"))


def _assistant_text_from_response(output: list[dict[str, Any]]) -> list[str]:
    """
    Extract assistant text from a terminal response snapshot.

    Used as a fallback for providers that only include final text in
    ``response.completed.response.output``.

    :param output: Response ``output`` list from a terminal event.
    :returns: Text blocks from assistant message items.
    """
    parts: list[str] = []
    for item in output:
        parts.extend(_assistant_text_from_output_item(item))
    return parts


def _assistant_text_from_content(raw_content: object) -> list[str]:
    """
    Extract text from an assistant message ``content`` list.

    :param raw_content: The raw ``content`` field from a message
        item. Expected shape is ``[{"type": "output_text", "text":
        "..."}]``.
    :returns: Text strings in order, omitting malformed blocks.
    """
    if not isinstance(raw_content, list):
        return []
    parts: list[str] = []
    for block in raw_content:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in _OUTPUT_TEXT_BLOCK_TYPES:
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return parts


def _extract_client_tool_names(tools: list[dict[str, Any]]) -> set[str]:
    """
    Return the set of names for tools whose spec runtime is ``client``.

    Reads ``runtime`` and ``name`` defensively because the
    agent-tools wire shape isn't fully nailed down across F1
    yet — entries missing ``runtime`` are treated as
    server-executed (the default), and malformed entries
    (non-string ``name``, missing ``name``) are skipped rather
    than crashed-on. A spec-side validator should reject these
    upstream; reaching this branch indicates a server-side bug,
    but the SDK shouldn't refuse to validate the well-formed
    siblings of a malformed entry.

    :param tools: Tool entries from
        ``agent_tools_getter(agent_id)``, e.g.
        ``[{"name": "open_in_editor", "runtime": "client"}]``.
    :returns: Set of tool names that require a client-side
        callable. Empty if no entries declare
        ``runtime: "client"``.
    """
    names: set[str] = set()
    for entry in tools:
        if entry.get("runtime") != _RUNTIME_CLIENT:
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        names.add(name)
    return names


def _format_validation_error(missing: set[str], extra: set[str]) -> str:
    """
    Build the ``ValueError`` message for a tool_callables mismatch.

    Names both directions of the mismatch in sorted order so the
    diagnostic is deterministic across runs (set iteration order
    is not stable across Python releases for the same set
    contents).

    :param missing: Spec-declared client-runtime tool names that
        have no matching callable.
    :param extra: ``tool_callables`` keys that don't name a
        spec-declared client-runtime tool.
    :returns: Human-readable error message describing both
        sides of the mismatch and how to fix it.
    """
    parts: list[str] = []
    if missing:
        parts.append("missing callable(s) for client-side tool(s): " + ", ".join(sorted(missing)))
    if extra:
        parts.append(
            "tool_callables key(s) not declared as runtime: "
            "client in the agent spec: " + ", ".join(sorted(extra))
        )
    return (
        "SessionsChat tool_callables do not match the agent's "
        "spec-declared client-side tools: "
        + "; ".join(parts)
        + ". Adjust either the spec or the tool_callables map so "
        "every client-runtime tool has a callable and every "
        "callable maps to a declared tool."
    )


async def _aclose(iterator: AsyncIterator[ServerStreamEvent]) -> None:
    """
    Close an async generator iterator returned by :meth:`SessionsNamespace.stream`.

    :meth:`SessionsNamespace.stream` is an async generator, so the
    iterator returned always exposes ``aclose``. Calling it tears
    down the underlying ``httpx`` SSE connection promptly rather
    than waiting for GC. We assert the attribute exists rather than
    silently skipping — if a future refactor returns a non-generator
    iterator from :meth:`SessionsNamespace.stream`, the assert
    surfaces the contract change immediately rather than leaking
    the connection.

    :param iterator: The async generator iterator to close.
    """
    aclose = getattr(iterator, "aclose", None)
    assert aclose is not None, (
        "SessionsNamespace.stream() must return an async generator "
        "exposing aclose(); got an iterator without it. This is a "
        "contract violation — the SSE connection would leak."
    )
    await aclose()


__all__ = ["SessionToolCallInfo", "SessionsChat", "ToolCallable"]
