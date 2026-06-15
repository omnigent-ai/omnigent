"""Unit tests for :class:`omnigent_client._sessions_chat.SessionsChat`.

These exercise the chat helper end-to-end through a fake
:class:`SessionsNamespace`. We use a real stub class (not MagicMock)
so unexpected calls raise loudly per the project's test-integrity
guide. ``ServerStreamEvent`` instances are constructed from real
typed Pydantic classes — never MagicMocks — so isinstance checks
inside :meth:`SessionsChat.send` actually fire.

Each test names the production behavior it pins:

* ``test_send_*``: that ``send()`` posts the right event payload,
  yields events from the underlying stream, and stops at the
  first turn-terminal event. Failure indicates either the wrong
  message envelope or a runaway iteration that doesn't terminate.
* ``test_query_*``: that ``query()`` folds delta events into the
  final text and resolves file artifacts via the injected
  ``files_getter``. Failure means assistant text is being dropped
  or file artifacts are silently lost.
* ``test_cancel_*``: that ``cancel()`` reaches the namespace's
  ``interrupt`` path with the correct session id.
* ``test_files_*``: that the file-upload pipeline runs through the
  injected uploader and produces the right content blocks.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from omnigent_client._query import QueryResult, QueryStream
from omnigent_client._sessions import Session, SessionsNamespace
from omnigent_client._sessions_chat import (
    SessionsChat,
    SessionToolCallInfo,
)
from omnigent_client._tool_handler import StreamHooks
from omnigent_client._types import File

from omnigent.server.schemas import (
    CompletedEvent,
    CreatedEvent,
    ElicitationRequestEvent,
    ElicitationRequestParams,
    OutputFileDoneEvent,
    OutputItemDoneEvent,
    OutputTextDeltaEvent,
    ReasoningStartedEvent,
    ReasoningSummaryTextDeltaEvent,
    ReasoningTextDeltaEvent,
    ResponseObject,
    ServerStreamEvent,
    SessionHeartbeatEvent,
    SessionStatusEvent,
)

_ASSISTANT_MESSAGE_ITEM: dict[str, Any] = {
    "id": "msg_assistant",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "output_text", "text": "final-only text"}],
}

# ── Stubs ─────────────────────────────────────────────────────────────


@dataclass
class _PostEventCall:
    """
    Single recorded ``post_event`` call.

    :param session_id: The session id passed to ``post_event``.
    :param event: The event payload dict passed to ``post_event``.
    """

    session_id: str
    event: dict[str, Any]


@dataclass
class _ResolveElicitationCall:
    """
    Single recorded ``resolve_elicitation`` call.

    :param session_id: Session id passed to ``resolve_elicitation``.
    :param elicitation_id: Elicitation id passed in the URL path.
    :param result: MCP-shape result body.
    """

    session_id: str
    elicitation_id: str
    result: dict[str, Any]


@dataclass
class _StreamScript:
    """
    Scripted reply for one ``stream()`` invocation.

    :param events: Events to yield, in order.
    :param session_id: Session id the call must target.
    """

    events: list[ServerStreamEvent]
    session_id: str | None = None


class _FakeNamespace(SessionsNamespace):
    """
    Drop-in replacement for :class:`SessionsNamespace`.

    Subclasses :class:`SessionsNamespace` so isinstance checks
    elsewhere in the SDK still pass, but overrides every public
    method to record calls or return scripted data. The base
    constructor is bypassed because we don't need a real
    ``httpx.AsyncClient``.

    :param stream_scripts: Queue of :class:`_StreamScript` payloads;
        each :meth:`stream` call pops the next one.
    :param session_obj: The :class:`Session` returned by both
        :meth:`create` and :meth:`get`.
    """

    def __init__(
        self,
        stream_scripts: list[_StreamScript],
        session_obj: Session,
    ) -> None:
        # Skip super().__init__: we don't want a real httpx client.
        self._stream_scripts = stream_scripts
        self._session_obj = session_obj
        self.post_event_calls: list[_PostEventCall] = []
        self.resolve_elicitation_calls: list[_ResolveElicitationCall] = []
        self.interrupt_calls: list[str] = []
        self.create_calls: list[tuple[bytes, str]] = []
        self.get_calls: list[str] = []

    async def create(  # type: ignore[override]
        self,
        bundle: bytes,
        *,
        filename: str = "agent.tar.gz",
        title: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Session:
        del title, labels
        self.create_calls.append((bundle, filename))
        return self._session_obj

    async def get(self, session_id: str) -> Session:  # type: ignore[override]
        self.get_calls.append(session_id)
        return self._session_obj

    async def post_event(  # type: ignore[override]
        self,
        session_id: str,
        event: dict[str, Any],
    ) -> None:
        self.post_event_calls.append(
            _PostEventCall(session_id=session_id, event=event),
        )

    async def resolve_elicitation(  # type: ignore[override]
        self,
        session_id: str,
        elicitation_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        self.resolve_elicitation_calls.append(
            _ResolveElicitationCall(
                session_id=session_id,
                elicitation_id=elicitation_id,
                result=result,
            )
        )
        return {"queued": False}

    async def interrupt(self, session_id: str) -> None:  # type: ignore[override]
        self.interrupt_calls.append(session_id)

    def stream(  # type: ignore[override]
        self,
        session_id: str,
    ) -> AsyncIterator[ServerStreamEvent]:
        if not self._stream_scripts:
            raise AssertionError(
                f"_FakeNamespace.stream({session_id!r}) called but no "
                "scripts remain — production code is opening more "
                "streams than the test expected."
            )
        script = self._stream_scripts.pop(0)
        if script.session_id is not None and script.session_id != session_id:
            raise AssertionError(
                f"stream() targeted {session_id!r} but the next "
                f"scripted reply expected {script.session_id!r}"
            )
        return _replay(script.events)


class _GatedReadyNamespace(_FakeNamespace):
    """
    Fake namespace whose stream has an explicit ready gate.

    The real Sessions SSE endpoint emits a ready heartbeat after the
    subscriber slot is registered. This fake blocks that heartbeat
    until the test releases it, and fails if the client posts the user
    message before the heartbeat has been delivered.

    :param session_obj: Session returned by ``get`` / ``create``.
    :param visible_events: Events to publish after the input event.
    """

    def __init__(
        self,
        *,
        session_obj: Session,
        visible_events: list[ServerStreamEvent],
    ) -> None:
        """
        Create the gated-ready namespace fake.

        :param session_obj: Session returned by ``get`` / ``create``,
            e.g. ``Session(id="conv_abc", ...)``.
        :param visible_events: Events to publish after posting, e.g.
            ``[OutputTextDeltaEvent(...)]``.
        :returns: None.
        """
        super().__init__(stream_scripts=[], session_obj=session_obj)
        self._visible_events = visible_events
        self.stream_opened = False
        self.ready_requested = asyncio.Event()
        self.release_ready = asyncio.Event()
        self.ready_delivered = False

    async def post_event(  # type: ignore[override]
        self,
        session_id: str,
        event: dict[str, Any],
    ) -> None:
        """
        Publish visible events only when the stream is already open.

        :param session_id: Session id passed to ``post_event``.
        :param event: Event payload passed to ``post_event``.
        :returns: None.
        """
        if not self.ready_delivered:
            raise AssertionError(
                "SessionsChat posted the user message before the "
                "stream-ready heartbeat was delivered."
            )
        await super().post_event(session_id, event)

    def stream(  # type: ignore[override]
        self,
        session_id: str,
    ) -> AsyncIterator[ServerStreamEvent]:
        """
        Mark the stream open only when the returned iterator is advanced.

        :param session_id: Session id to stream.
        :returns: Async iterator of events visible after ``post_event``.
        """

        async def _stream() -> AsyncIterator[ServerStreamEvent]:
            """
            Yield the ready heartbeat, then post-triggered events.

            :yields: The ready heartbeat and events selected by
                ``post_event``.
            """
            if session_id != self._session_obj.id:
                raise AssertionError(
                    f"stream() targeted {session_id!r}; expected {self._session_obj.id!r}"
                )
            self.stream_opened = True
            self.ready_requested.set()
            await self.release_ready.wait()
            self.ready_delivered = True
            yield SessionHeartbeatEvent(type="session.heartbeat")
            while not self.post_event_calls:
                await asyncio.sleep(0)
            for event in self._visible_events:
                yield event

        return _stream()


async def _replay(events: list[ServerStreamEvent]) -> AsyncIterator[ServerStreamEvent]:
    """
    Yield each pre-built event in order.

    :param events: Events to replay.
    :yields: Each event in order.
    """
    for event in events:
        yield event


@dataclass
class _UploaderCall:
    """
    Single recorded file-upload invocation.

    :param path: Local path passed to the uploader.
    """

    path: str


class _FakeUploader:
    """
    Real stub class for the ``files_uploader`` callable.

    Returns a synthesized :class:`File` for every call and records the
    paths. Using a real class instead of MagicMock guarantees that an
    unexpected attribute access raises ``AttributeError`` rather than
    silently returning a child MagicMock.
    """

    def __init__(self) -> None:
        self.calls: list[_UploaderCall] = []

    async def __call__(self, path: str) -> File:
        """
        Record the call and synthesize a deterministic :class:`File`.

        :param path: Local file path to "upload".
        :returns: A :class:`File` whose id encodes the path so test
            assertions can correlate uploads with content blocks.
        """
        self.calls.append(_UploaderCall(path=path))
        return File(
            id=f"file_{pathlib.Path(path).stem}",
            filename=pathlib.Path(path).name,
            bytes=0,
            created_at=1700000000,
        )


@dataclass
class _GetterCall:
    """
    Single recorded file-fetch invocation.

    :param file_id: File id passed to the getter.
    """

    file_id: str


class _FakeGetter:
    """
    Real stub class for the ``files_getter`` callable.

    Returns a synthesized :class:`File` per call. Same rationale as
    :class:`_FakeUploader` for not using MagicMock.

    :param files: Optional pre-seeded file map keyed by id; if a
        requested id is not in the map a :class:`File` is
        synthesized.
    """

    def __init__(self, files: dict[str, File] | None = None) -> None:
        self.calls: list[_GetterCall] = []
        self._files = files or {}

    async def __call__(self, file_id: str) -> File:
        self.calls.append(_GetterCall(file_id=file_id))
        if file_id in self._files:
            return self._files[file_id]
        return File(
            id=file_id,
            filename=f"{file_id}.bin",
            bytes=0,
            created_at=1700000000,
        )


# ── Helpers ───────────────────────────────────────────────────────────


def _make_session(
    session_id: str = "conv_abc",
    agent_id: str = "ag_abc",
    status: str = "running",
) -> Session:
    """
    Build a :class:`Session` snapshot for use as the fake namespace's
    canned response.

    :param session_id: Session id, e.g. ``"conv_abc"``.
    :param agent_id: Agent id, e.g. ``"ag_abc"``.
    :param status: Session status, e.g. ``"running"``.
    :returns: A :class:`Session` instance.
    """
    return Session(
        id=session_id,
        agent_id=agent_id,
        status=status,
        created_at=1700000000,
    )


def _completed_event(
    response_id: str = "resp_1",
    *,
    output: list[dict[str, Any]] | None = None,
) -> CompletedEvent:
    """
    Build a real :class:`CompletedEvent` for terminating a turn.

    :param response_id: Response id, e.g. ``"resp_1"``.
    :param output: Optional terminal response output list, e.g.
        ``[{"type": "message", "role": "assistant", "content": [...]}]``.
    :returns: A typed :class:`CompletedEvent`.
    """
    return CompletedEvent(
        type="response.completed",
        response=ResponseObject(
            id=response_id,
            status="completed",
            model="test-model",
            created_at=1700000000,
            output=output or [],
        ),
    )


def _created_event(response_id: str = "resp_1") -> CreatedEvent:
    """
    Build a real :class:`CreatedEvent` for lifecycle hook tests.

    :param response_id: Response id, e.g. ``"resp_1"``.
    :returns: A typed :class:`CreatedEvent`.
    """
    return CreatedEvent(
        type="response.created",
        response=ResponseObject(
            id=response_id,
            status="in_progress",
            model="test-model",
            created_at=1700000000,
        ),
    )


# ── send() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_posts_message_event_and_yields_events_until_terminal() -> None:
    session = _make_session()
    delta1 = OutputTextDeltaEvent(type="response.output_text.delta", delta="Hello ")
    delta2 = OutputTextDeltaEvent(type="response.output_text.delta", delta="world")
    completed = _completed_event()
    # An event AFTER the terminal one. The chat helper must NOT yield
    # it — turn boundaries are defined by the terminal response event.
    extra = SessionStatusEvent(type="session.status", conversation_id="conv_abc", status="idle")

    ns = _FakeNamespace(
        stream_scripts=[
            _StreamScript(events=[delta1, delta2, completed, extra]),
        ],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
    )

    yielded = [event async for event in chat.send("hi")]

    # Posted exactly one event with the right envelope. Failure here
    # means the wire shape diverged from SessionEventInput's schema.
    assert len(ns.post_event_calls) == 1
    call = ns.post_event_calls[0]
    assert call.session_id == "conv_abc"
    assert call.event == {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
        },
    }

    # Exactly the 3 events up to and including the terminal — the
    # post-terminal SessionStatusEvent was correctly suppressed.
    # If 4 events leak through, the turn-boundary check regressed
    # and downstream consumers will see two turns merged.
    assert yielded == [delta1, delta2, completed]


@pytest.mark.asyncio
async def test_send_fires_stream_hooks_for_sessions_events() -> None:
    """
    SessionsChat must expose the same lifecycle hooks as legacy Session.

    Regression for the sessions-first SDK path: callers could pass hooks
    to ``client.session(...)`` but not to ``client.sessions_chat(...)``,
    and no hooks fired while iterating sessions-native events.
    """
    session = _make_session()
    message_content = [{"type": "output_text", "text": "done"}]
    calls: list[tuple[Any, ...]] = []

    def on_response_start(ctx: Any) -> None:
        calls.append(("response_start", ctx.response.id, ctx.response.status))

    async def on_reasoning_start(ctx: Any) -> None:
        del ctx
        calls.append(("reasoning_start",))

    def on_reasoning_end(ctx: Any) -> None:
        calls.append(("reasoning_end", ctx.reasoning_text, ctx.summary_text))

    def on_message_start(ctx: Any) -> None:
        calls.append(("message_start", ctx.response_id))

    def on_message_end(ctx: Any) -> None:
        calls.append(("message_end", ctx.content))

    def on_tool_call_start(ctx: Any) -> None:
        calls.append(
            (
                "tool_start",
                ctx.name,
                ctx.arguments,
                ctx.call_id,
                ctx.executed_by,
            )
        )

    def on_tool_call_end(ctx: Any) -> None:
        calls.append(("tool_end", ctx.call_id, ctx.output))

    def on_file_output(ctx: Any) -> None:
        calls.append(("file", ctx.file_id, ctx.filename, ctx.content_type))

    def on_response_end(ctx: Any) -> None:
        calls.append(("response_end", ctx.response.id, ctx.status))

    hooks = StreamHooks(
        on_response_start=on_response_start,
        on_reasoning_start=on_reasoning_start,
        on_reasoning_end=on_reasoning_end,
        on_message_start=on_message_start,
        on_message_end=on_message_end,
        on_tool_call_start=on_tool_call_start,
        on_tool_call_end=on_tool_call_end,
        on_file_output=on_file_output,
        on_response_end=on_response_end,
    )
    events: list[ServerStreamEvent] = [
        _created_event(),
        ReasoningStartedEvent(type="response.reasoning.started"),
        ReasoningTextDeltaEvent(type="response.reasoning_text.delta", delta="thinking"),
        ReasoningSummaryTextDeltaEvent(
            type="response.reasoning_summary_text.delta",
            delta="summary",
        ),
        OutputTextDeltaEvent(type="response.output_text.delta", delta="hi"),
        OutputItemDoneEvent(
            type="response.output_item.done",
            item={
                "id": "fc_1",
                "type": "function_call",
                "status": "completed",
                "name": "lookup",
                "arguments": '{"q": "x"}',
                "call_id": "call_1",
            },
        ),
        OutputItemDoneEvent(
            type="response.output_item.done",
            item={
                "id": "fco_1",
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "tool output",
            },
        ),
        OutputFileDoneEvent(
            type="response.output_file.done",
            file_id="file_xyz",
            filename="report.pdf",
            content_type="application/pdf",
        ),
        OutputItemDoneEvent(
            type="response.output_item.done",
            item={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": message_content,
            },
        ),
        _completed_event(),
    ]
    ns = _FakeNamespace(
        stream_scripts=[_StreamScript(events=events)],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
        hooks=hooks,
    )

    yielded = [event async for event in chat.send("hi")]

    assert yielded == events
    assert calls == [
        ("response_start", "resp_1", "in_progress"),
        ("reasoning_start",),
        ("reasoning_end", "thinking", "summary"),
        ("message_start", "resp_1"),
        ("tool_start", "lookup", {"q": "x"}, "call_1", "server"),
        ("tool_end", "call_1", "tool output"),
        ("file", "file_xyz", "report.pdf", "application/pdf"),
        ("message_end", message_content),
        ("response_end", "resp_1", "completed"),
    ]


@pytest.mark.asyncio
async def test_send_routes_elicitation_hook_to_resolve_endpoint() -> None:
    """
    Elicitation hooks must resolve the sessions-native approval request.

    Without this, an SDK user who registers ``on_elicitation_request``
    through the sessions chat helper sees the prompt but the parked
    workflow never receives the approval verdict.
    """
    session = _make_session()
    calls: list[tuple[str, str, str]] = []

    async def on_elicitation_request(ctx: Any) -> bool:
        calls.append((ctx.elicitation_id, ctx.message, ctx.response_id))
        return True

    request = ElicitationRequestEvent(
        type="response.elicitation_request",
        elicitation_id="elicit_1",
        params=ElicitationRequestParams(
            message="Approve this?",
            requestedSchema={"type": "object"},
            phase="pre_tool_use",
            policy_name="approval",
            content_preview="tool call",
            target_session_id="conv_child",
        ),
    )
    ns = _FakeNamespace(
        stream_scripts=[_StreamScript(events=[_created_event(), request, _completed_event()])],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
        hooks=StreamHooks(on_elicitation_request=on_elicitation_request),
    )

    [event async for event in chat.send("hi")]

    assert calls == [("elicit_1", "Approve this?", "resp_1")]
    assert ns.resolve_elicitation_calls == [
        _ResolveElicitationCall(
            session_id="conv_child",
            elicitation_id="elicit_1",
            result={"action": "accept"},
        )
    ]


@pytest.mark.asyncio
async def test_send_raises_on_failed_status_with_error_message() -> None:
    """A terminal ``session.status: failed`` raises with its error message.

    Regression for the silent headless ``-p`` failure mode: a SETUP-phase
    failure (spec resolution, spawn-env build) ends the turn before the
    LLM stream starts, so no ``response.failed`` / ``FailedEvent`` is ever
    emitted — the only terminal signal is ``session.status: failed``
    carrying the error. ``send()`` must treat that as terminal and raise
    :class:`OmnigentError` with the carried message, instead of
    blocking until the stream closes and returning empty text.
    """
    from omnigent_client._errors import OmnigentError

    from omnigent.server.schemas import ErrorDetail

    session = _make_session()
    failed = SessionStatusEvent(
        type="session.status",
        conversation_id="conv_abc",
        status="failed",
        error=ErrorDetail(
            code="runner_error",
            message="turn setup failed: no resolvable model for provider 'acme'",
        ),
    )

    ns = _FakeNamespace(
        stream_scripts=[_StreamScript(events=[failed])],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
    )

    with pytest.raises(OmnigentError) as excinfo:
        async for _ in chat.send("hi"):
            pass

    # The raised error must carry the real message — not a generic
    # placeholder. Without the fix, send() never raises and the headless
    # caller returns empty text.
    assert "turn setup failed: no resolvable model for provider 'acme'" in str(excinfo.value)
    assert excinfo.value.code == "runner_error"


@pytest.mark.asyncio
async def test_send_raises_generic_on_failed_status_without_error() -> None:
    """A ``failed`` status with no error detail still raises (generic msg).

    The schema's ``error`` field is optional and some runner failure call
    sites carry only an HTTP status, so ``send()`` may see a ``failed``
    status with ``error=None``. It must still raise (so the turn never
    hangs) with a non-empty fallback message rather than crashing on the
    missing field.
    """
    from omnigent_client._errors import OmnigentError

    session = _make_session()
    failed = SessionStatusEvent(
        type="session.status",
        conversation_id="conv_abc",
        status="failed",
        error=None,
    )

    ns = _FakeNamespace(
        stream_scripts=[_StreamScript(events=[failed])],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
    )

    with pytest.raises(OmnigentError) as excinfo:
        async for _ in chat.send("hi"):
            pass

    assert "turn failed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_send_accepts_prebuilt_content_blocks() -> None:
    session = _make_session()
    ns = _FakeNamespace(
        stream_scripts=[_StreamScript(events=[_completed_event()])],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
    )

    blocks: list[dict[str, Any]] = [
        {"type": "input_text", "text": "first"},
        {"type": "input_text", "text": "second"},
    ]
    [event async for event in chat.send(blocks)]

    # Pre-built blocks pass through unchanged — proves _build_content
    # extends rather than rewrapping when given a list.
    assert ns.post_event_calls[0].event["data"]["content"] == blocks


@pytest.mark.asyncio
async def test_send_with_files_uploads_and_appends_input_blocks(
    tmp_path: pathlib.Path,
) -> None:
    session = _make_session()
    uploader = _FakeUploader()
    # Image to verify the input_image branch; PDF for input_file.
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    ns = _FakeNamespace(
        stream_scripts=[_StreamScript(events=[_completed_event()])],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=uploader,
        files_getter=None,
        session=session,
    )

    [event async for event in chat.send("look at these", files=[str(img), str(pdf)])]

    # Both files were uploaded.
    assert [c.path for c in uploader.calls] == [str(img), str(pdf)]

    content = ns.post_event_calls[0].event["data"]["content"]
    # 1 text + 2 file blocks, in that order.
    assert len(content) == 3
    assert content[0] == {"type": "input_text", "text": "look at these"}
    # Image dispatches to input_image with file_id only (no filename).
    assert content[1] == {"type": "input_image", "file_id": "file_pic"}
    # Non-image dispatches to input_file with filename preserved.
    assert content[2] == {
        "type": "input_file",
        "file_id": "file_doc",
        "filename": "doc.pdf",
    }


@pytest.mark.asyncio
async def test_send_with_files_but_no_uploader_raises() -> None:
    session = _make_session()
    ns = _FakeNamespace(
        stream_scripts=[],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
    )
    with pytest.raises(RuntimeError, match="files_uploader"):
        async for _ in chat.send("hi", files=["./x.txt"]):
            pytest.fail("Should have raised before any event was yielded")
    # No HTTP traffic should have happened either — failing loud
    # before the namespace is touched is part of the contract.
    assert ns.post_event_calls == []


# ── query() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_collects_text_from_deltas() -> None:
    session = _make_session()
    ns = _FakeNamespace(
        stream_scripts=[
            _StreamScript(
                events=[
                    OutputTextDeltaEvent(type="response.output_text.delta", delta="Hello "),
                    OutputTextDeltaEvent(type="response.output_text.delta", delta="world"),
                    _completed_event(),
                ],
            ),
        ],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
    )

    result = await chat.query("hi")

    assert isinstance(result, QueryResult)
    # Concatenation of every delta proves the fold path. If
    # truncated to "world", the deltas are being overwritten
    # instead of appended.
    assert result.text == "Hello world"
    assert result.files == []


@pytest.mark.asyncio
async def test_query_opens_stream_before_posting_message() -> None:
    """
    ``query()`` must wait for stream readiness before posting.

    The Sessions SSE endpoint has no replay buffer. If the client
    posts before the server has acknowledged the live subscription,
    a fast response can complete before the subscriber exists,
    producing an empty ``QueryResult`` even though the server
    generated text.
    """
    session = _make_session()
    ns = _GatedReadyNamespace(
        session_obj=session,
        visible_events=[
            OutputTextDeltaEvent(type="response.output_text.delta", delta="fast reply"),
            _completed_event(),
        ],
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
    )

    task = asyncio.create_task(chat.query("hi"))
    await asyncio.wait_for(ns.ready_requested.wait(), timeout=1.0)
    await asyncio.sleep(0)

    assert task.done() is False, (
        "query() should be waiting for the stream-ready heartbeat; "
        "if it already finished or failed, send() likely posted before "
        "the subscription was acknowledged."
    )
    assert ns.post_event_calls == []

    ns.release_ready.set()
    result = await asyncio.wait_for(task, timeout=1.0)

    assert ns.stream_opened is True
    assert result.text == "fast reply"


@pytest.mark.asyncio
async def test_query_uses_final_message_item_when_deltas_absent() -> None:
    """
    Some harnesses produce no ``response.output_text.delta`` events
    but do include assistant text in ``response.output_item.done``.
    ``query()`` must surface that text so headless ``omnigent run
    -p`` prints the answer instead of returning an empty string.
    """
    session = _make_session()
    message_done = OutputItemDoneEvent(
        type="response.output_item.done",
        item=dict(_ASSISTANT_MESSAGE_ITEM),
    )
    ns = _FakeNamespace(
        stream_scripts=[_StreamScript(events=[message_done, _completed_event()])],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
    )

    result = await chat.query("hi")

    assert result.text == "final-only text"


@pytest.mark.asyncio
async def test_query_uses_terminal_response_text_when_deltas_and_items_absent() -> None:
    """
    Terminal ``response.completed`` snapshots are the last fallback
    for provider text. If the stream has no deltas and no message
    item events, ``query()`` must still return assistant text from
    ``response.output``.
    """
    session = _make_session()
    ns = _FakeNamespace(
        stream_scripts=[
            _StreamScript(events=[_completed_event(output=[dict(_ASSISTANT_MESSAGE_ITEM)])]),
        ],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
    )

    result = await chat.query("hi")

    assert result.text == "final-only text"


@pytest.mark.asyncio
async def test_query_prefers_deltas_over_final_snapshot_text() -> None:
    """
    If deltas are present, ``query()`` must not append final snapshot
    text again. Otherwise providers that emit both surfaces would
    duplicate every answer in headless output.
    """
    session = _make_session()
    ns = _FakeNamespace(
        stream_scripts=[
            _StreamScript(
                events=[
                    OutputTextDeltaEvent(type="response.output_text.delta", delta="delta text"),
                    OutputItemDoneEvent(
                        type="response.output_item.done",
                        item=dict(_ASSISTANT_MESSAGE_ITEM),
                    ),
                    _completed_event(output=[dict(_ASSISTANT_MESSAGE_ITEM)]),
                ],
            ),
        ],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
    )

    result = await chat.query("hi")

    assert result.text == "delta text"


@pytest.mark.asyncio
async def test_query_resolves_files_via_getter() -> None:
    session = _make_session()
    pre = File(
        id="file_xyz",
        filename="report.pdf",
        bytes=42,
        created_at=1700000000,
    )
    getter = _FakeGetter(files={"file_xyz": pre})
    ns = _FakeNamespace(
        stream_scripts=[
            _StreamScript(
                events=[
                    OutputTextDeltaEvent(type="response.output_text.delta", delta="here"),
                    OutputFileDoneEvent(
                        type="response.output_file.done",
                        file_id="file_xyz",
                        filename="report.pdf",
                        content_type="application/pdf",
                    ),
                    _completed_event(),
                ],
            ),
        ],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=getter,
        session=session,
    )

    result = await chat.query("make me a pdf")

    # Exactly one getter call — proves the file id was extracted from
    # the typed event field, not duplicated or skipped.
    assert [c.file_id for c in getter.calls] == ["file_xyz"]
    assert result.text == "here"
    assert result.files == [pre]


@pytest.mark.asyncio
async def test_query_file_event_without_getter_raises() -> None:
    session = _make_session()
    ns = _FakeNamespace(
        stream_scripts=[
            _StreamScript(
                events=[
                    OutputFileDoneEvent(
                        type="response.output_file.done",
                        file_id="file_xyz",
                    ),
                    _completed_event(),
                ],
            ),
        ],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
    )

    # Failing loud is the design — if we silently dropped the file
    # the caller would never know their artifact was lost.
    with pytest.raises(RuntimeError, match="files_getter"):
        await chat.query("make me a pdf")


@pytest.mark.asyncio
async def test_query_stream_yields_text_chunks_and_collects_files() -> None:
    session = _make_session()
    getter = _FakeGetter()
    ns = _FakeNamespace(
        stream_scripts=[
            _StreamScript(
                events=[
                    OutputTextDeltaEvent(type="response.output_text.delta", delta="part1 "),
                    OutputFileDoneEvent(
                        type="response.output_file.done",
                        file_id="file_a",
                    ),
                    OutputTextDeltaEvent(type="response.output_text.delta", delta="part2"),
                    _completed_event(),
                ],
            ),
        ],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=getter,
        session=session,
    )

    stream = await chat.query("hi", stream=True)
    assert isinstance(stream, QueryStream)

    chunks: list[str] = []
    async for chunk in stream:
        chunks.append(chunk)

    # Both deltas observed in order. If only one chunk, the iterator
    # exited early; if duplicates, the underlying iterator is being
    # consumed twice.
    assert chunks == ["part1 ", "part2"]
    # The file artifact was collected via the shared list reference.
    files = stream.files
    assert len(files) == 1
    assert files[0].id == "file_a"


@pytest.mark.asyncio
async def test_query_stream_uses_final_message_item_when_deltas_absent() -> None:
    """
    Streaming ``query(stream=True)`` must use the same provider-text
    fallback as non-streaming ``query()`` when no text deltas arrive.
    Otherwise callers iterating :class:`QueryStream` would see an
    empty stream even though the final assistant message had text.
    """
    session = _make_session()
    message_done = OutputItemDoneEvent(
        type="response.output_item.done",
        item=dict(_ASSISTANT_MESSAGE_ITEM),
    )
    ns = _FakeNamespace(
        stream_scripts=[
            _StreamScript(
                events=[
                    OutputTextDeltaEvent(type="response.output_text.delta", delta=""),
                    message_done,
                    _completed_event(),
                ],
            ),
        ],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
    )

    stream = await chat.query("hi", stream=True)

    chunks: list[str] = []
    async for chunk in stream:
        chunks.append(chunk)

    # Empty deltas are not meaningful text. This proves the stream
    # skips them and yields the final assistant message fallback
    # exactly once instead of producing ["", "final-only text"].
    assert chunks == ["final-only text"]


# ── cancel() / refresh() ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_calls_namespace_interrupt() -> None:
    session = _make_session()
    ns = _FakeNamespace(stream_scripts=[], session_obj=session)
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
    )

    await chat.cancel()

    # interrupt() reached with the bound session id, exactly once.
    assert ns.interrupt_calls == ["conv_abc"]


@pytest.mark.asyncio
async def test_refresh_updates_cached_session() -> None:
    session = _make_session(status="running")
    ns = _FakeNamespace(stream_scripts=[], session_obj=session)
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
    )
    # Swap in a different snapshot for the next get().
    ns._session_obj = _make_session(status="idle")

    refreshed = await chat.refresh()

    assert refreshed.status == "idle"
    # status property now reflects the refreshed snapshot — proves
    # _session was actually replaced and not just returned.
    assert chat.status == "idle"
    assert ns.get_calls == ["conv_abc"]


# ── create() factory ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_factory_creates_session_and_wires_helpers() -> None:
    session = _make_session()
    ns = _FakeNamespace(stream_scripts=[], session_obj=session)
    uploader = _FakeUploader()
    getter = _FakeGetter()
    hooks = StreamHooks()

    chat = await SessionsChat.create(
        namespace=ns,
        bundle=b"bundle-bytes",
        filename="agent.tar.gz",
        files_uploader=uploader,
        files_getter=getter,
        hooks=hooks,
    )

    # Created exactly one session with the uploaded bundle bytes.
    assert ns.create_calls == [(b"bundle-bytes", "agent.tar.gz")]
    assert chat.session_id == "conv_abc"
    assert chat.agent_id == "ag_abc"
    assert chat._hooks is hooks


# ── tool_callables validation + dispatch ──────────────────────────────


@dataclass
class _AgentToolsCall:
    """
    Single recorded ``agent_tools_getter`` invocation.

    :param agent_id: The agent id passed to the getter.
    """

    agent_id: str


class _FakeAgentToolsGetter:
    """
    Real stub class for the ``agent_tools_getter`` callable.

    Returns a fixed tool list per call and records each invocation.
    Real class (not MagicMock) so unexpected attribute access fails
    loud rather than silently returning a child MagicMock — see
    Rule 3 of the project test guide.

    :param tools: The tool entries to return on every call.
    """

    def __init__(self, tools: list[dict[str, Any]]) -> None:
        self.calls: list[_AgentToolsCall] = []
        self._tools = tools

    async def __call__(self, agent_id: str, session_id: str | None = None) -> list[dict[str, Any]]:
        """
        Record the call and return the canned tool list.

        :param agent_id: Agent id passed by the SUT.
        :param session_id: Session id passed by the SUT (unused
            in test stub).
        :returns: The pre-seeded tool list.
        """
        self.calls.append(_AgentToolsCall(agent_id=agent_id))
        return list(self._tools)


def _action_required_event(
    *,
    name: str,
    call_id: str,
    arguments: str = "{}",
    item_id: str = "fc_1",
) -> OutputItemDoneEvent:
    """
    Build a real :class:`OutputItemDoneEvent` carrying an
    action_required function_call item.

    Wire shape mirrors the server's ``_to_api_item`` for a
    ``function_call`` item with status ``action_required`` — that
    is the exact shape the server emits for spec-declared
    client-runtime tools.

    :param name: Tool name, e.g. ``"open_in_editor"``.
    :param call_id: Server-assigned call id, e.g.
        ``"call_abc"``.
    :param arguments: JSON-encoded arguments string, e.g.
        ``'{"path": "x.py"}'``.
    :param item_id: Conversation-item id, e.g. ``"fc_1"``.
    :returns: A typed :class:`OutputItemDoneEvent`.
    """
    return OutputItemDoneEvent(
        type="response.output_item.done",
        item={
            "id": item_id,
            "type": "function_call",
            "status": "action_required",
            "name": name,
            "call_id": call_id,
            "arguments": arguments,
        },
    )


@pytest.mark.asyncio
async def test_send_no_client_tools_no_callables_works() -> None:
    """
    Agent declares no client-side tools and caller passes no
    callables: validation passes silently, no dispatch fires.
    """
    session = _make_session()
    getter = _FakeAgentToolsGetter(tools=[])
    ns = _FakeNamespace(
        stream_scripts=[_StreamScript(events=[_completed_event()])],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
        tool_callables=None,
        agent_tools_getter=getter,
    )

    yielded = [event async for event in chat.send("hi")]

    # Only the terminal event was yielded. If validation or
    # dispatch had fired spuriously, we'd see extra events or an
    # exception instead.
    assert len(yielded) == 1
    # Spec was fetched exactly once for validation. If 0, the
    # validation step was skipped (silent success of a wrong
    # config). If 2+, the cache logic regressed.
    assert getter.calls == [_AgentToolsCall(agent_id="ag_abc")]


@pytest.mark.asyncio
async def test_send_client_tools_missing_callable_raises() -> None:
    """
    Agent declares a client-runtime tool but no callable is
    supplied: stream-start raises ``ValueError`` before any HTTP
    traffic.
    """
    session = _make_session()
    getter = _FakeAgentToolsGetter(
        tools=[{"name": "open_in_editor", "runtime": "client"}],
    )
    ns = _FakeNamespace(stream_scripts=[], session_obj=session)
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
        tool_callables=None,
        agent_tools_getter=getter,
    )

    # ValueError names the missing tool — that's how a developer
    # debugs the agent/SDK config mismatch. If the message
    # changed and no longer included the tool name, the
    # diagnostic value of the error is gone.
    with pytest.raises(ValueError, match="open_in_editor"):
        async for _ in chat.send("hi"):
            pytest.fail("send() yielded an event before validation raised")

    # No HTTP traffic was issued — failing loud BEFORE the SSE
    # stream is opened is the contract; if we'd opened the stream
    # we'd risk a parked turn waiting on a tool we can't satisfy.
    assert ns.post_event_calls == []


@pytest.mark.asyncio
async def test_send_extra_callable_not_in_spec_raises() -> None:
    """
    Caller supplies a callable for a tool the spec doesn't
    declare as runtime: client. Fail loud — silently ignoring it
    would mask a config typo.
    """
    session = _make_session()
    getter = _FakeAgentToolsGetter(tools=[])

    async def _unused_tool(_info: SessionToolCallInfo) -> str:
        # The pytest.fail makes a regression visible: if dispatch
        # ever reaches this callable despite the spec rejecting
        # it, the test fails loud rather than silently passing.
        pytest.fail("callable for a non-spec tool should never be dispatched")

    ns = _FakeNamespace(stream_scripts=[], session_obj=session)
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
        tool_callables={"unknown_tool": _unused_tool},
        agent_tools_getter=getter,
    )

    with pytest.raises(ValueError, match="unknown_tool"):
        async for _ in chat.send("hi"):
            pytest.fail("send() yielded an event before validation raised")


@pytest.mark.asyncio
async def test_send_all_callables_present_passes_validation() -> None:
    """
    Agent declares one client tool, caller supplies a callable
    for it: validation passes and the turn streams normally.
    """
    session = _make_session()
    getter = _FakeAgentToolsGetter(
        tools=[
            {"name": "open_in_editor", "runtime": "client"},
            # A server-runtime tool — must NOT require a callable.
            {"name": "search.web", "runtime": "server"},
        ],
    )

    async def _open_in_editor(_info: SessionToolCallInfo) -> str:
        return "ok"

    ns = _FakeNamespace(
        stream_scripts=[_StreamScript(events=[_completed_event()])],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
        tool_callables={"open_in_editor": _open_in_editor},
        agent_tools_getter=getter,
    )

    yielded = [event async for event in chat.send("hi")]

    # Validation passed — terminal event reached without raise.
    # If the server-runtime tool had been treated as needing a
    # callable, this test would have raised ValueError above.
    assert len(yielded) == 1


@pytest.mark.asyncio
async def test_send_validation_cached_across_calls() -> None:
    """
    The agent-spec fetch only fires once per chat helper, even
    across multiple ``send()`` invocations on the same instance.
    Caching is critical: hammering the agents endpoint per turn
    would be a noticeable regression in real workloads.
    """
    session = _make_session()
    getter = _FakeAgentToolsGetter(tools=[])
    ns = _FakeNamespace(
        stream_scripts=[
            _StreamScript(events=[_completed_event(response_id="r1")]),
            _StreamScript(events=[_completed_event(response_id="r2")]),
        ],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
        tool_callables=None,
        agent_tools_getter=getter,
    )

    [event async for event in chat.send("first")]
    [event async for event in chat.send("second")]

    # Exactly one fetch despite two send() calls. If we fetched
    # twice the cache logic broke; if we fetched zero times the
    # validation never ran.
    assert len(getter.calls) == 1


@pytest.mark.asyncio
async def test_send_dispatches_action_required_calls_callable_and_posts_output() -> None:
    """
    When the server emits an action_required function_call item,
    the SDK invokes the callable and posts a
    function_call_output event back into the session.
    """
    session = _make_session()
    getter = _FakeAgentToolsGetter(
        tools=[{"name": "open_in_editor", "runtime": "client"}],
    )

    invocations: list[SessionToolCallInfo] = []
    hook_calls: list[tuple[Any, ...]] = []

    async def _callable(info: SessionToolCallInfo) -> str:
        invocations.append(info)
        return "did the thing"

    action_required = _action_required_event(
        name="open_in_editor",
        call_id="call_xyz",
        arguments='{"path": "foo.py"}',
        item_id="fc_42",
    )
    ns = _FakeNamespace(
        stream_scripts=[
            _StreamScript(events=[action_required, _completed_event()]),
        ],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
        tool_callables={"open_in_editor": _callable},
        agent_tools_getter=getter,
        hooks=StreamHooks(
            on_tool_call_start=lambda ctx: hook_calls.append(
                (
                    "start",
                    ctx.name,
                    ctx.arguments,
                    ctx.call_id,
                    ctx.executed_by,
                )
            ),
            on_tool_call_end=lambda ctx: hook_calls.append(
                ("end", ctx.name, ctx.call_id, ctx.output)
            ),
        ),
    )

    yielded = [event async for event in chat.send("please")]

    # Both the action_required event and the terminal event are
    # observable to the caller — dispatch is a side-effect, not a
    # consumed-and-hidden filter. Assert exact length, identity of
    # the action_required event (it's the same instance the script
    # injected), and type of the terminal event. Avoids a fragile
    # full-equality compare on the pydantic CompletedEvent (which
    # could break if a future schema adds an optional field) while
    # still failing loud if the dispatch path consumes the event.
    assert len(yielded) == 2, (
        f"Expected 2 yielded events (action_required + terminal); "
        f"got {len(yielded)}. If 1, the dispatch path consumed the "
        f"action_required event instead of forwarding it."
    )
    assert yielded[0] is action_required
    assert isinstance(yielded[1], CompletedEvent)

    # The callable received the parsed arguments dict (not the
    # raw JSON string) and the right ids. If `arguments` were a
    # string, this test would fail with a type error rather than
    # just an assertion mismatch — that's the design.
    assert len(invocations) == 1
    info = invocations[0]
    assert info.name == "open_in_editor"
    assert info.call_id == "call_xyz"
    assert info.item_id == "fc_42"
    assert info.arguments == {"path": "foo.py"}

    # The user message + the function_call_output were posted, in
    # that order. The output event echoes the call_id back so the
    # parked turn can correlate.
    assert len(ns.post_event_calls) == 2
    user_msg, tool_output = ns.post_event_calls
    assert user_msg.event["type"] == "message"
    assert tool_output.event == {
        "type": "function_call_output",
        "data": {"call_id": "call_xyz", "output": "did the thing"},
    }
    assert hook_calls == [
        ("start", "open_in_editor", {"path": "foo.py"}, "call_xyz", "client"),
        ("end", "open_in_editor", "call_xyz", "did the thing"),
    ]


@pytest.mark.asyncio
async def test_send_dispatches_sync_callable() -> None:
    """
    The dispatch path accepts plain (non-async) callables — async
    is the common case but the type alias permits both.
    """
    session = _make_session()
    getter = _FakeAgentToolsGetter(
        tools=[{"name": "compute", "runtime": "client"}],
    )

    def _sync_callable(info: SessionToolCallInfo) -> str:
        return f"sync result for {info.name}"

    ns = _FakeNamespace(
        stream_scripts=[
            _StreamScript(
                events=[
                    _action_required_event(name="compute", call_id="call_s"),
                    _completed_event(),
                ],
            ),
        ],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
        tool_callables={"compute": _sync_callable},
        agent_tools_getter=getter,
    )

    [event async for event in chat.send("go")]

    # The output event carries the sync return value verbatim. If
    # the dispatch code awaited a non-awaitable, this assertion
    # would fail with a TypeError before reaching the check.
    output_call = ns.post_event_calls[1]
    assert output_call.event["data"]["output"] == "sync result for compute"


@pytest.mark.asyncio
async def test_send_does_not_dispatch_completed_function_call_items() -> None:
    """
    Server-executed function_call items arrive with
    ``status == "completed"`` (not action_required). The dispatch
    path must skip them — touching them would double-execute the
    tool.
    """
    session = _make_session()
    getter = _FakeAgentToolsGetter(tools=[])

    completed_function_call = OutputItemDoneEvent(
        type="response.output_item.done",
        item={
            "id": "fc_done",
            "type": "function_call",
            "status": "completed",
            "name": "search.web",
            "call_id": "call_done",
            "arguments": "{}",
        },
    )
    ns = _FakeNamespace(
        stream_scripts=[
            _StreamScript(events=[completed_function_call, _completed_event()]),
        ],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
        tool_callables=None,
        agent_tools_getter=getter,
    )

    [event async for event in chat.send("go")]

    # Only one post_event call — the user message. NO
    # function_call_output post happened, proving the dispatch
    # path correctly filtered on status. If the count is 2, the
    # filter regressed and we double-handled a server-executed
    # tool.
    assert len(ns.post_event_calls) == 1
    assert ns.post_event_calls[0].event["type"] == "message"


@pytest.mark.asyncio
async def test_send_callables_without_getter_raises() -> None:
    """
    Caller supplies ``tool_callables`` but no
    ``agent_tools_getter``. We can't validate, so we fail loud —
    silently skipping validation would defeat the safety
    contract.
    """
    session = _make_session()

    async def _cb(_info: SessionToolCallInfo) -> str:
        return "x"

    ns = _FakeNamespace(stream_scripts=[], session_obj=session)
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
        tool_callables={"some_tool": _cb},
        agent_tools_getter=None,
    )

    with pytest.raises(RuntimeError, match="agent_tools_getter"):
        async for _ in chat.send("hi"):
            pytest.fail("send() should raise before yielding")


@pytest.mark.asyncio
async def test_stream_validates_tool_callables_at_stream_start() -> None:
    """
    Direct ``chat.stream()`` (no user-message post) must run the
    same tool_callables validation as :meth:`send`. If it didn't,
    a misconfigured chat helper could open the SSE stream, observe
    an action_required event, and dispatch to a missing or wrong
    callable.
    """
    session = _make_session()
    getter = _FakeAgentToolsGetter(
        tools=[{"name": "open_in_editor", "runtime": "client"}],
    )
    ns = _FakeNamespace(stream_scripts=[], session_obj=session)
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
        tool_callables=None,
        agent_tools_getter=getter,
    )

    # ``ns.stream_scripts`` is empty: if validation didn't fire
    # first, ``_FakeNamespace.stream`` would raise an
    # ``AssertionError`` ("no scripts remain"), which would mask
    # the ValueError we're asserting on. Passing this match means
    # we never reached the namespace stream call.
    with pytest.raises(ValueError, match="open_in_editor"):
        async for _ in chat.stream():
            pytest.fail("stream() yielded an event before validation raised")


@pytest.mark.asyncio
async def test_stream_dispatches_action_required_calls() -> None:
    """
    ``chat.stream()`` dispatches action_required function_call
    items to the registered callable and posts the result back —
    same dispatch path as :meth:`send`.
    """
    session = _make_session()
    getter = _FakeAgentToolsGetter(
        tools=[{"name": "open_in_editor", "runtime": "client"}],
    )

    invocations: list[SessionToolCallInfo] = []

    async def _callable(info: SessionToolCallInfo) -> str:
        invocations.append(info)
        return "stream-result"

    action_required = _action_required_event(
        name="open_in_editor",
        call_id="call_stream",
    )
    ns = _FakeNamespace(
        stream_scripts=[
            _StreamScript(events=[action_required, _completed_event()]),
        ],
        session_obj=session,
    )
    chat = SessionsChat(
        namespace=ns,
        files_uploader=None,
        files_getter=None,
        session=session,
        tool_callables={"open_in_editor": _callable},
        agent_tools_getter=getter,
    )

    yielded = [event async for event in chat.stream()]

    # Both events surface to the caller (dispatch is side-effect,
    # not consume-and-hide).
    assert len(yielded) == 2
    # The callable fired exactly once with the right call_id.
    assert len(invocations) == 1
    assert invocations[0].call_id == "call_stream"
    # function_call_output was posted — no user message because
    # stream() does not post one (unlike send()).
    assert len(ns.post_event_calls) == 1
    assert ns.post_event_calls[0].event == {
        "type": "function_call_output",
        "data": {"call_id": "call_stream", "output": "stream-result"},
    }
