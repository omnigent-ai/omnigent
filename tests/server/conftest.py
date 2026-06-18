"""Shared fixtures for server integration tests.

Uses real SqlAlchemyTaskStore + real DBOS workflow with a
ControllableMockClient that replaces the LLM. The mock auto-completes
by default so existing tests pass without modification. For concurrency
tests, use MockCall.block_until / MockCall.release to create
deterministic race windows.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.llms.types import (
    FunctionCallOutput,
    MessageOutput,
    OutputText,
    Response,
    ResponseCompletedEvent,
    ResponseStreamEvent,
    ResponseTextDeltaEvent,
)
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.runtime import init as init_runtime
from omnigent.runtime import pending_elicitations
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import _elicitation_registry, presence
from omnigent.server.app import create_app
from omnigent.server.routes import sessions as sessions_routes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

# ── Controllable mock LLM ─────────────────────────────


@dataclass
class MockCall:
    """
    A single configured LLM call with optional synchronization
    gates.

    :param text: The assistant response text, e.g.
        ``"Hello from mock"``. Ignored when ``tool_calls`` is set.
    :param tool_calls: If set, the response contains function calls
        instead of text. Each dict must have ``"call_id"``,
        ``"name"``, and ``"arguments"`` keys, e.g.
        ``[{"call_id": "c1", "name": "grep", "arguments": "{}"}]``.
    :param block_before_response: If set, the mock awaits this
        event before producing any output. Call ``release()`` from
        the test to unblock.
    :param call_event: Set by the mock when this call is entered.
        Tests can ``await call_event.wait()`` to know the LLM was
        called.
    :param stream_tokens: If ``True``, yield individual text delta
        events before the completed event. If ``False``, yield only
        the completed event.
    :param exception: If set, ``create()`` raises this exception
        instead of returning a response. Used to simulate retryable
        LLM errors (e.g. ``httpx.HTTPStatusError`` with 429).
    :param tool_calls_fn: If set, called with the ``create()`` kwargs
        to produce ``tool_calls`` dynamically. Use when tool call
        arguments depend on runtime state (e.g. response_ids from a
        prior spawn). Takes precedence over static ``tool_calls``.
    :param received_kwargs: Populated by the mock when this call is
        consumed. Contains the kwargs passed to
        ``responses.create()`` so tests can inspect what the LLM
        received (e.g. ``input``, ``instructions``, ``model``).
        ``None`` until the call is executed.
    """

    text: str = "Hello from the test agent."
    tool_calls: list[dict[str, str]] | None = None
    # threading.Event (not asyncio.Event) so the test event loop
    # can ``set()`` cross-loop into DBOS's background event loop
    # where the workflow body runs. asyncio.Event's internal
    # futures are loop-bound — calling ``set()`` from loop A
    # never wakes a ``wait()`` parked on loop B (silent hang
    # under any block=True mock LLM scenario).
    block_before_response: threading.Event | None = None
    call_event: threading.Event = field(default_factory=threading.Event)
    stream_tokens: bool = False
    exception: Exception | None = None
    # Callable[[dict[str, Any]], list[dict[str, str]]] — generates
    # tool_calls dynamically from create() kwargs.
    tool_calls_fn: Any = None
    # Callable[[dict[str, Any]], Exception | None] — predicate
    # that conditionally raises based on inspecting the call's
    # kwargs. Returning None means "do not raise". Useful when
    # parent and sub-agent share the FIFO mock queue and only
    # one of them should fail (route by an input substring).
    exception_fn: Any = None
    # Populated by the mock when this call is consumed. Contains
    # the kwargs passed to responses.create() so tests can inspect
    # what the LLM received (e.g. the input/history).
    # Any: kwargs from responses.create() are heterogeneous.
    received_kwargs: dict[str, Any] | None = field(
        default=None,
        repr=False,
    )

    async def wait_called(self, *, timeout: float = 60.0) -> None:
        """
        Asynchronously wait until this MockCall has been entered.

        Bridges the underlying sync ``threading.Event`` (chosen
        because the workflow body runs on DBOS's
        ``_background_event_loop`` while the test runs on
        pytest-asyncio's loop, and asyncio.Event doesn't sync
        cross-loop) into an awaitable the test can use.

        :param timeout: Max seconds to wait. ``TimeoutError`` is
            raised if exceeded — matches the prior behavior of
            ``asyncio.wait_for(call.call_event.wait(), timeout)``.
        """
        await asyncio.to_thread(self.call_event.wait, timeout)
        if not self.call_event.is_set():
            raise TimeoutError(
                f"MockCall.call_event not set within {timeout}s",
            )

    def release(self) -> None:
        """
        Unblock a call that is waiting on ``block_before_response``.
        """
        if self.block_before_response is not None:
            self.block_before_response.set()


def _build_completed_event(
    text: str,
    tool_calls: list[dict[str, str]] | None = None,
) -> ResponseCompletedEvent:
    """
    Build a ``ResponseCompletedEvent`` with text and/or tool calls.

    :param text: The assistant response text.
    :param tool_calls: Optional list of tool call dicts, each with
        ``"call_id"``, ``"name"``, and ``"arguments"`` keys, e.g.
        ``[{"call_id": "c1", "name": "grep", "arguments": "{}"}]``.
        When provided, function call outputs are included in the
        response alongside any text.
    :returns: A completed event with real ``llms.types`` dataclasses.
    """
    output: list[MessageOutput | FunctionCallOutput] = []
    if tool_calls:
        for tc in tool_calls:
            output.append(
                FunctionCallOutput(
                    call_id=tc["call_id"],
                    name=tc["name"],
                    arguments=tc["arguments"],
                )
            )
    else:
        output.append(MessageOutput(content=[OutputText(text=text)]))
    return ResponseCompletedEvent(
        response=Response(output=output, model="test-model"),
    )


class ControllableMockClient:
    """
    Mock LLM client with per-call synchronization gates.

    Replaces ``_get_llm_client()`` in ``workflow.py``. Each call to
    ``responses.create()`` consumes the next ``MockCall`` from the
    queue. If the queue is exhausted, uses a default auto-completing
    call.

    Usage::

        client = ControllableMockClient()
        # First LLM call blocks until released
        call_1 = client.add_call(text="First", block=True)
        # ... start workflow ...
        await call_1.call_event.wait()  # know the LLM was called
        # ... inject steering message ...
        call_1.release()  # unblock

    :param default_text: Text for auto-generated default calls when
        the queue is empty, e.g. ``"Hello from the test agent."``.
    """

    def __init__(self, default_text: str = "Hello from the test agent.") -> None:
        self._calls: list[MockCall] = []
        self._call_index = 0
        self._lock = threading.Lock()
        self._default_text = default_text
        self.responses = _MockResponsesNamespace(self)

    def add_call(
        self,
        text: str | None = None,
        block: bool = False,
        stream_tokens: bool = False,
        tool_calls: list[dict[str, str]] | None = None,
        tool_calls_fn: Any = None,
        exception: Exception | None = None,
        exception_fn: Any = None,
    ) -> MockCall:
        """
        Enqueue a configured call.

        :param text: Response text. Defaults to ``default_text``.
            Ignored when ``tool_calls`` is provided.
        :param block: If ``True``, the call blocks until
            ``MockCall.release()`` is called.
        :param stream_tokens: If ``True``, emit text delta events
            before the completed event.
        :param tool_calls: If provided, the response contains
            function calls instead of text. Each dict must have
            ``"call_id"``, ``"name"``, and ``"arguments"`` keys.
        :param tool_calls_fn: If provided, called with the
            ``create()`` kwargs to produce ``tool_calls``
            dynamically. Use when arguments depend on runtime
            state (e.g. response_ids from a prior spawn).
        :param exception: If provided, ``create()`` raises this
            instead of returning. Use with ``httpx.HTTPStatusError``
            to simulate retryable LLM errors.
        :returns: The ``MockCall`` for synchronization.
        """
        call = MockCall(
            text=text or self._default_text,
            tool_calls=tool_calls,
            tool_calls_fn=tool_calls_fn,
            block_before_response=threading.Event() if block else None,
            stream_tokens=stream_tokens,
            exception=exception,
            exception_fn=exception_fn,
        )
        self._calls.append(call)
        return call

    def _next_call(self) -> MockCall:
        """
        Return the next MockCall, or a default if queue exhausted.

        :returns: The next ``MockCall`` to execute.
        """
        with self._lock:
            if self._call_index < len(self._calls):
                call = self._calls[self._call_index]
                self._call_index += 1
                return call
            # Default: auto-complete immediately
            return MockCall(text=self._default_text)

    def release_all(self) -> None:
        """
        Release every blocked call so DBOS workflow tasks can exit.

        Called during fixture teardown to prevent the event loop from
        hanging on shutdown.
        """
        for call in self._calls:
            call.release()

    def get_call(self, index: int) -> MockCall:
        """
        Return a queued ``MockCall`` by index.

        Use this instead of accessing ``_calls`` directly so tests
        interact through a public interface.

        :param index: Zero-based index into the queued calls list,
            e.g. ``0`` for the first call.
        :returns: The ``MockCall`` at the given index.
        :raises IndexError: If *index* is out of range.
        """
        return self._calls[index]

    @property
    def call_count(self) -> int:
        """
        Number of ``responses.create()`` invocations so far.

        :returns: The total call count.
        """
        with self._lock:
            return self._call_index


class _MockResponsesNamespace:
    """
    ``client.responses`` namespace that dispatches to
    ``ControllableMockClient``.

    :param client: The parent mock client.
    """

    def __init__(self, client: ControllableMockClient) -> None:
        self._client = client

    async def create(
        self,
        **kwargs: Any,
    ) -> Response | AsyncIterator[ResponseStreamEvent]:
        """
        Mock ``responses.create()``. Consumes the next MockCall,
        optionally awaiting a gate, then returns a Response or
        stream.

        Async to match the real client's ``await create()``.

        :param kwargs: Responses API kwargs — captured on the
            ``MockCall.received_kwargs`` for test inspection.
        :returns: A ``Response`` if ``stream`` is falsy, or an
            async iterator of ``ResponseStreamEvent`` if
            ``stream=True``.
        """
        call = self._client._next_call()
        # Capture kwargs so tests can inspect what the LLM received
        call.received_kwargs = kwargs
        # Resolve dynamic tool_calls if a factory function is set.
        # Returns None to fall back to text (e.g. when the input
        # doesn't match the expected pattern for this call).
        if call.tool_calls_fn is not None:
            dynamic = call.tool_calls_fn(kwargs)
            if dynamic is not None:
                call.tool_calls = dynamic
        # Signal that this call has been entered. threading.Event
        # is thread-safe — set() from any loop wakes wait() in
        # any other loop, which matters because the workflow
        # body runs on DBOS's _background_event_loop while the
        # test runs on pytest-asyncio's loop.
        call.call_event.set()
        # Optionally block until the test releases us. Use
        # asyncio.to_thread to bridge a sync threading.Event
        # wait into the async event loop without parking the
        # loop — the offloaded thread blocks; the loop yields.
        if call.block_before_response is not None:
            await asyncio.to_thread(call.block_before_response.wait)
        # Raise configured exception (simulates retryable errors).
        # exception_fn fires only if its predicate decides this
        # specific kwargs payload should fail; useful for FIFO-
        # shared mocks where only one consumer (parent vs sub-
        # agent) should hit the failure path.
        if call.exception_fn is not None:
            dynamic_exc = call.exception_fn(kwargs)
            if dynamic_exc is not None:
                raise dynamic_exc
        if call.exception is not None:
            raise call.exception

        stream = kwargs.get("stream", False)
        if stream:
            return self._stream(call)
        return _build_completed_event(
            call.text,
            tool_calls=call.tool_calls,
        ).response

    async def _stream(
        self,
        call: MockCall,
    ) -> AsyncIterator[ResponseStreamEvent]:
        """
        Yield streaming events for a call.

        :param call: The ``MockCall`` controlling this stream.
        """
        if call.stream_tokens and not call.tool_calls:
            # Yield individual word tokens as deltas
            for word in call.text.split():
                yield ResponseTextDeltaEvent(delta=word + " ")
        yield _build_completed_event(
            call.text,
            tool_calls=call.tool_calls,
        )


# ── Fixtures ──────────────────────────────────────────


@pytest.fixture()
def mock_llm() -> Iterator[ControllableMockClient]:
    """
    A ``ControllableMockClient`` instance for the current test.

    Tests that need to control LLM timing should call
    ``mock_llm.add_call(block=True)`` before creating responses.

    On teardown, releases all blocked calls so DBOS workflow tasks
    can exit cleanly and the event loop shuts down.
    """
    client = ControllableMockClient()
    yield client
    client.release_all()


@pytest.fixture(autouse=True)
def _reset_elicitation_state() -> Iterator[None]:
    """
    Clear the module-global elicitation state after every test.

    ``pending_elicitations`` and the harness elicitation registries are
    process-global and keyed by ``elicitation_id`` / session id, so an
    entry left behind by one test (an unresolved prompt, a severed
    long-poll, a pre-resolved tombstone) is visible to every later test
    in the same xdist worker. A leaked ``pending_elicitations`` entry
    flips ``GET /v1/sessions/{id}`` into its descendant-walk path for
    all subsequent tests, which breaks tests whose stub stores
    don't implement ``list_conversations`` and adds spurious DB walks
    to everything else.
    """
    yield
    pending_elicitations.reset_for_tests()
    _elicitation_registry.reset_for_tests()
    # Presence is likewise module-global (keyed by conversation/user)
    # with pending leave-grace timers that would fire into later tests.
    presence.reset_for_tests()


# Originals of the sessions-module globals that many tests monkeypatch.
# Captured at conftest import (before any test runs), so the guard below
# can detect a patch that outlived its test. ``_get_runner_client`` /
# ``_get_runner_client_for_resource_access`` are the runner-resolution
# helpers every sessions route shares: a leaked stub here silently
# poisons EVERY later session bind / relay / event forward on the same
# xdist worker (CI saw ``test_sessions_tool_result_forward``'s
# ``_FailingRunnerClient`` stub surface in the WS-tunnel suite as
# "Runner stream relay exited before becoming ready" 503s).
_GUARDED_SESSIONS_GLOBALS: dict[str, Any] = {
    "_get_runner_client": sessions_routes._get_runner_client,
    "_get_runner_client_for_resource_access": (
        sessions_routes._get_runner_client_for_resource_access
    ),
}


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item: pytest.Item) -> None:
    """
    Fail loud if a monkeypatch of a shared sessions global leaked.

    Runs after fixture finalization (``trylast`` orders this impl after
    ``_pytest.runner``'s, which executes monkeypatch undo), so any stub
    still installed at this point escaped its test. This has actually
    happened in CI: pytest-rerunfailures 14.0 under pytest 9 corrupted
    fixture-finalizer state around a flaky-marker rerun, the patching
    test's undo never ran, and every later test on the worker resolved
    runner clients through the dead stub. Restore the original first so
    subsequent tests on this worker stay healthy, then raise so the
    leak is attributed to the polluting test instead of an arbitrary
    later victim.

    :param item: The test item whose teardown just finished.
    :raises RuntimeError: If a guarded module global was left patched.
    """
    leaked = [
        name
        for name, original in _GUARDED_SESSIONS_GLOBALS.items()
        if getattr(sessions_routes, name) is not original
    ]
    if not leaked:
        return
    for name in leaked:
        setattr(sessions_routes, name, _GUARDED_SESSIONS_GLOBALS[name])
    raise RuntimeError(
        f"omnigent.server.routes.sessions.{', '.join(leaked)} left monkeypatched "
        f"after {item.nodeid}: a fixture finalizer (monkeypatch undo) did not run. "
        "The original has been restored for subsequent tests. If no test in this "
        "file patches it, suspect rerun/fixture-teardown plugin breakage "
        "(see the pytest-rerunfailures pin rationale in pyproject.toml)."
    )


@pytest.fixture()
def runtime_init(
    db_uri: str,
    tmp_path: Path,
    mock_llm: ControllableMockClient,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """
    Initialize the runtime with real stores and mock LLM patched in.

    Replaces the former ``task_store`` fixture now that the tasks table
    has been removed. Callers that depended on ``task_store`` for runtime
    initialization should switch to this fixture.

    :param db_uri: SQLite connection URI from the ``db_uri`` fixture.
    :param tmp_path: Pytest temp directory for artifacts and cache.
    :param mock_llm: Controllable mock LLM client for the test.
    :param monkeypatch: Pytest fixture for patching the LLM client
        factory in the workflow module.
    """
    agent_store = SqlAlchemyAgentStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_cache = AgentCache(
        artifact_store=artifact_store,
        cache_dir=tmp_path / ".cache",
    )
    init_runtime(
        conversation_store=conversation_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
        file_store=file_store,
        artifact_store=artifact_store,
    )
    # Patch the LLM client so the mock is used everywhere.
    monkeypatch.setattr(
        "omnigent.runtime.workflow._get_llm_client",
        lambda: mock_llm,
    )
    yield


@pytest.fixture(autouse=True)
def _first_party_origin_on_asgi(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Stamp the first-party sentinel ``Origin`` on every in-process ASGI request.

    The ``require_trusted_origin`` CSRF guard on the multipart routes (POST
    ``/v1/sessions`` bundled-create, file upload) trusts a request carrying
    the sentinel ``Origin``. Test clients built on :class:`httpx.ASGITransport`
    (the in-process app transport used throughout this suite, including the
    per-test Alice/Bob multi-user clients) otherwise send none. The guard
    currently fails open on an absent ``Origin``, but the tests should not
    depend on that (it is a temporary posture, to be closed): stamping the
    sentinel makes them announce themselves the way the real SDK / runner do,
    so they keep passing once absent is no longer allowed.

    Patching :meth:`httpx.ASGITransport.handle_async_request` injects the
    sentinel in one place instead of on every ad-hoc client. It is scoped
    to ``ASGITransport`` so it never touches real outbound HTTP, and it only
    fills in a *missing* ``Origin`` — a test that sets its own (the
    cross-origin / loopback CSRF cases) is left untouched.

    :param monkeypatch: pytest attribute patcher (auto-reverted per test).
    :returns: None.
    """
    original = httpx.ASGITransport.handle_async_request

    async def _with_origin(self: httpx.ASGITransport, request: httpx.Request) -> httpx.Response:
        """
        Inject the sentinel ``Origin`` when absent, then delegate.

        :param self: The patched ``ASGITransport`` instance.
        :param request: The outgoing in-process request.
        :returns: The app's response.
        """
        if "origin" not in request.headers:
            request.headers["origin"] = OMNIGENT_INTERNAL_WS_ORIGIN
        return await original(self, request)

    monkeypatch.setattr(httpx.ASGITransport, "handle_async_request", _with_origin)


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """
    Build the FastAPI app with real stores and real workflow
    execution (mock LLM is patched in via runtime_init fixture).

    No legacy ``/v1/responses`` router — that path was removed
    with the DBOS execution layer. Tests that still need to drive
    a session end-to-end use ``/v1/sessions``.

    :param runtime_init: Fixture that initializes the runtime and
        patches the mock LLM.
    :param db_uri: SQLite database URI.
    :param tmp_path: Pytest temp directory for artifacts and cache.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store = SqlAlchemyAgentStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    return create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
    )


@pytest_asyncio.fixture()
async def client(
    app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """
    Async HTTP client wired to the FastAPI app (no real server).

    On teardown, releases blocked mock calls and destroys DBOS
    before the event loop shuts down. This must happen in an async
    fixture because the pytest-asyncio runner closes the event loop
    immediately after async fixture teardown completes.
    """
    # Initialize the HarnessProcessManager for tests that hit the
    # fallback executor path (when _runner_client is not set).
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    # Release blocked mock calls so background threads can finish.
    mock_llm.release_all()
    # Cancel any background relay tasks started by PATCH-bound
    # tests; the stub runner never responds so they'd otherwise
    # hang teardown. Snapshot first because cancellation fires
    # the done-callback that mutates the dict.
    relay_tasks = [h.task for h in sessions_routes._runner_relay_tasks.values()]
    for task in relay_tasks:
        if not task.done():
            task.cancel()
    for task in relay_tasks:
        # Let SystemExit / KeyboardInterrupt through so Ctrl-C still aborts.
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    sessions_routes._runner_relay_tasks.clear()
    set_harness_process_manager(None)
    await pm.shutdown()
