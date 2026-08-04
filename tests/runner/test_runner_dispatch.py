"""End-to-end runner-dispatch tests: server → runner → spawned harness.

The load-bearing assertion: the runner FastAPI app, when given a
real :class:`HarnessProcessManager`, accepts a
POST /v1/sessions/{conversation_id}/events?stream=true,
spawns a harness subprocess (using the existing
``omnigent/runtime/harnesses/`` machinery — NOT a parallel impl),
forwards the request to the harness via UDS, and streams the
harness's SSE response back through the runner's own SSE response.

Architecture verified end-to-end:
- A real ``HarnessProcessManager`` is started (writes its instance
  dir, runs orphan sweep, starts the idle reaper).
- A test-only harness module is registered in ``_HARNESS_MODULES``.
- The runner FastAPI app is built with ``process_manager=mgr``.
- The test posts to the runner's
  /v1/sessions/{conversation_id}/events?stream=true with a message
  body + harness name.
- The runner calls ``mgr.get_client()`` → spawns a uvicorn
  subprocess running the test harness on a UDS → returns the
  per-conversation httpx client.
- The runner POSTs the request to the harness via that client.
- The harness drives an LLM call via ``run_turn`` and streams SSE
  back.
- The runner relays each SSE chunk through to the test client.

The OpenAI-key-gated test runs the full chain against gpt-4o-mini.
The unkeyed test asserts on plumbing only (handler is reached,
503 on bad harness name).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from omnigent.runner.app import (
    _build_spawn_env_from_spec,
    _forward_harness_response,
    _resolve_harness_config,
)
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from omnigent.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE
from omnigent.spec.types import AgentSpec, ExecutorSpec, MCPServerConfig, SharePolicy
from tests.runner.helpers import NullServerClient

_TEST_HARNESS_NAME = "runner-test-default"
_TEST_HARNESS_MODULE = "tests._fixtures.runner_test_harness"


@pytest.fixture(autouse=True)
def _assume_harness_clis_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the sub-agent dispatch CLI preflight for hermetic tests.

    The named-mode child-create path refuses to spawn a sub-agent whose
    harness CLI (``claude`` / ``codex`` / ``pi``) is absent from ``PATH``
    (see ``missing_harness_cli``, dispatched from ``tool_dispatch``). These
    dispatch tests run in a hermetic environment where those binaries may be
    absent (e.g. CI), so without this stub they would fail at the preflight
    instead of exercising the create / continue logic under test. Tests that
    specifically assert the preflight re-patch ``missing_harness_cli`` in
    their own body, which wins over this autouse default.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.missing_harness_cli",
        lambda harness: None,
    )


@asynccontextmanager
async def _runner_test_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Create a test client against a runner ASGI app.

    :param app: Runner app under test.
    :returns: Async context manager yielding an ``httpx.AsyncClient``.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        yield client


class _FakeHarnessStream:
    """
    Async context manager that yields scripted harness SSE chunks.

    :param chunks: SSE chunks returned by ``aiter_text``.
    :param status_code: HTTP status exposed to the runner.
    """

    def __init__(self, chunks: list[str], status_code: int = 200) -> None:
        """
        Store scripted stream state.

        :param chunks: SSE chunks returned by ``aiter_text``.
        :param status_code: HTTP status exposed to the runner.
        """
        self._chunks = chunks
        self.status_code = status_code

    async def __aenter__(self) -> _FakeHarnessStream:
        """
        Enter the fake stream context.

        :returns: This fake stream.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """
        Exit the fake stream without suppressing exceptions.

        :param exc_type: Exception type from the context, if any.
        :param exc: Exception value from the context, if any.
        :param tb: Traceback from the context, if any.
        :returns: None.
        """
        del exc_type, exc, tb

    async def aiter_text(self) -> AsyncIterator[str]:
        """
        Yield scripted text chunks.

        :returns: Async iterator of SSE chunks.
        """
        for chunk in self._chunks:
            yield chunk


async def _await_bg_turn_task(conv: str, *, timeout: float = 10.0) -> None:
    """Await the fire-and-forget background turn task for *conv* before draining.

    The ``POST /events`` background path returns 202 before its turn task
    (named ``turn-{conv}``) finishes publishing the terminal ``session.status``.
    Awaiting that task by name removes the race where a status-queue drain's
    timeout expires under heavy CI load before the task completes. A task that
    already finished is absent from ``asyncio.all_tasks()`` (it published its
    terminal status synchronously on the way out), so a ``None`` lookup is a
    safe no-op.

    :param conv: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :param timeout: Hard cap in seconds for awaiting the task.
    """
    turn_task = next(
        (t for t in asyncio.all_tasks() if t.get_name() == f"turn-{conv}"),
        None,
    )
    if turn_task is not None:
        await asyncio.wait_for(turn_task, timeout=timeout)


async def _drain_published_statuses(
    conv: str,
    *,
    until: str,
    timeout: float,
) -> list[str]:
    """Collect ``session.status`` values a runner published for a session.

    Reads the runner's module-level per-session event queue
    (``omnigent.runner.app._session_event_queues_ref``) — the same queue
    the SSE ``/stream`` endpoint drains — and returns the ordered list of
    ``session.status`` values seen, stopping once *until* is published. This
    polls the in-process queue rather than a concurrent SSE ``GET`` because
    ``httpx.ASGITransport`` does not interleave a streaming response with a
    concurrent ``POST`` on the same client, so a live SSE subscriber would
    never observe the background turn's events.

    :param conv: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :param until: Stop once this ``session.status`` value is observed,
        e.g. ``"failed"``.
    :param timeout: Hard cap in seconds — if *until* never arrives the poll
        gives up and returns what it saw, so a hang regression fails the
        assertion instead of spinning forever.
    :returns: Ordered ``session.status`` values published for *conv*.
    """
    from omnigent.runner.app import _session_event_queues_ref

    statuses: list[str] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        queue = _session_event_queues_ref.get(conv)
        drained = False
        while queue is not None and not queue.empty():
            event = queue.get_nowait()
            drained = True
            if isinstance(event, dict) and event.get("type") == "session.status":
                status = event.get("status")
                if isinstance(status, str):
                    statuses.append(status)
        if until in statuses:
            return statuses
        if not drained:
            # Let the background turn task make progress before re-polling.
            await asyncio.sleep(0.02)
    return statuses


async def _drain_failed_status_event(
    conv: str,
    *,
    timeout: float,
) -> dict[str, Any] | None:
    """Return the first ``session.status: failed`` event a runner published.

    Mirrors :func:`_drain_published_statuses` but returns the full event
    dict (not just the status string) so a test can assert the carried
    ``error`` payload. A SETUP-phase failure forwards its
    error message on the terminal ``failed`` event instead of dropping it.

    :param conv: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :param timeout: Hard cap in seconds; returns ``None`` if no failed
        event arrives so a regression fails the assertion rather than
        hanging.
    :returns: The ``session.status: failed`` event dict, or ``None``.
    """
    from omnigent.runner.app import _session_event_queues_ref

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        queue = _session_event_queues_ref.get(conv)
        drained = False
        while queue is not None and not queue.empty():
            event = queue.get_nowait()
            drained = True
            if (
                isinstance(event, dict)
                and event.get("type") == "session.status"
                and event.get("status") == "failed"
            ):
                return event
        if not drained:
            await asyncio.sleep(0.02)
    return None


class _FakeHarnessClient:
    """
    Harness client stub exposing ``stream`` for runner proxy tests.

    :param chunks: SSE chunks returned by the fake stream.
    """

    def __init__(self, chunks: list[str]) -> None:
        """
        Store scripted stream chunks.

        :param chunks: SSE chunks returned by the fake stream.
        """
        self._chunks = chunks

    def stream(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, object],
        timeout: float | None,
    ) -> _FakeHarnessStream:
        """
        Return a fake streaming response.

        :param method: HTTP method, e.g. ``"POST"``.
        :param url: Harness endpoint path.
        :param json: JSON body sent to the harness.
        :param timeout: Request timeout.
        :returns: Fake stream context manager.
        """
        del method, url, json, timeout
        return _FakeHarnessStream(self._chunks)


class _FakeProcessManager:
    """
    Process manager stub for runner dispatch tests.

    :param harness_client: Optional harness client to return.
    """

    def __init__(self, harness_client: _FakeHarnessClient | None = None) -> None:
        """
        Store the optional harness client.

        :param harness_client: Optional harness client to return.
        """
        self._harness_client = harness_client
        # Records (conversation_id, harness_name, env) for every get_client
        # call: the actual spawn_env the process manager would have launched
        # the harness subprocess with.
        self.get_client_calls: list[tuple[str, str, dict[str, str] | None]] = []
        # Conversation ids passed to release(), in call order.
        self.release_calls: list[str] = []

    async def get_client(
        self,
        conversation_id: str,
        harness_name: str,
        *,
        env: dict[str, str] | None = None,
    ) -> _FakeHarnessClient:
        """
        Return the configured fake harness client.

        :param conversation_id: Omnigent conversation id.
        :param harness_name: Harness name requested by the runner.
        :param env: Optional spawn environment.
        :returns: Configured fake harness client.
        :raises AssertionError: If no fake client was configured.
        """
        self.get_client_calls.append((conversation_id, harness_name, env))
        if self._harness_client is None:
            raise AssertionError("get_client should not be called")
        return self._harness_client

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        """Reaper in-flight marker — no-op for this stub (issue #1414)."""
        del conversation_id, response_id

    def clear_in_flight(self, conversation_id: str) -> None:
        """Reaper in-flight clear — no-op for this stub (issue #1414)."""
        del conversation_id

    async def release(self, conversation_id: str) -> None:
        """Record a release call — real process teardown is a no-op here.

        Both dispatch paths call this on an in-conversation agent switch
        to force a fresh harness subprocess (a same-harness cache hit
        would otherwise silently keep serving the PREVIOUS agent's
        seeded instructions). Recorded so the value actually
        fires, not just that the code path was reached.
        """
        self.release_calls.append(conversation_id)


@pytest.fixture
async def started_manager() -> AsyncIterator[HarnessProcessManager]:
    """A real, started HarnessProcessManager with the test harness registered.

    Uses a short ``/tmp/oa-rtest`` parent rather than pytest's
    ``tmp_path`` because UDS paths on Linux are capped at 108 chars
    and the manager's per-conversation socket layout
    (``<parent>/ap-<uuid32>/<conv_id>.sock``) blows past that when
    nested under pytest's already-long ``/tmp/pytest-of-.../...``
    tree.

    Yields the started manager; on teardown, shuts it down so any
    spawned subprocesses are reaped before the test ends.
    """
    import shutil
    import uuid

    short_parent = Path(f"/tmp/oa-rtest-{uuid.uuid4().hex[:8]}")
    short_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Inject the test-only harness module into the registry. We
    # mutate the dict directly per the registry's documented test-
    # injection pattern; restore on teardown to avoid leaking into
    # other tests.
    _HARNESS_MODULES[_TEST_HARNESS_NAME] = _TEST_HARNESS_MODULE
    mgr = HarnessProcessManager(tmp_parent=short_parent)
    await mgr.start()
    try:
        yield mgr
    finally:
        await mgr.shutdown()
        _HARNESS_MODULES.pop(_TEST_HARNESS_NAME, None)
        shutil.rmtree(short_parent, ignore_errors=True)


# ── Plumbing tests (no LLM key required) ─────────────────


def test_forward_harness_response_preserves_no_body_responses() -> None:
    """204/304 harness side-channel responses must not serialize JSON null.

    Returning ``JSONResponse(status_code=204, content=None)`` writes ``b"null"``
    even though Uvicorn/HTTP semantics require an empty body for 204. That
    manifests under uvicorn as ``Response content longer than Content-Length``.
    """
    response = _forward_harness_response(httpx.Response(204, content=b""))

    assert response.status_code == 204
    assert response.body == b""
    assert b"content-length" not in dict(response.raw_headers)


def test_forward_harness_response_preserves_json_body() -> None:
    response = _forward_harness_response(httpx.Response(404, json={"error": "not_found"}))

    assert response.status_code == 404
    assert response.body == b'{"error":"not_found"}'
    assert dict(response.raw_headers)[b"content-length"] == b"21"


@pytest.mark.asyncio
async def test_runner_post_without_manager_returns_501() -> None:
    """Scaffold-mode preserved when no manager is wired up."""
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type] # no process_manager → scaffold
    async with _runner_test_client(app) as http:
        response = await http.post(
            "/v1/sessions/conv_x/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "harness": _TEST_HARNESS_NAME,
                "model": "fake/model",
                "content": [],
            },
        )
        assert response.status_code == 501
        assert "HarnessProcessManager" in response.json()["detail"]


@pytest.mark.asyncio
async def test_runner_resolves_harness_from_fallback_when_no_agent_id(
    started_manager: HarnessProcessManager,
) -> None:
    """Without agent_id or server_base_url, runner falls back to the
    test-default harness. Verifies the fallback path doesn't crash."""
    app = create_runner_app(
        process_manager=started_manager,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        # No agent_id → runner falls back to "runner-test-default"
        # harness. With that registered in _HARNESS_MODULES, the
        # runner must spawn the harness and return its SSE stream.
        # Missing LLM credentials are represented inside that stream
        # as ``response.failed``, not as a runner spawn failure.
        response = await http.post(
            "/v1/sessions/c_fallback/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "model": "x",
                "content": [{"role": "user", "content": "test"}],
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: response.created" in response.text


class _RecordingProcessManager:
    """
    Process manager stub that records the harness name get_client saw.

    Unlike :class:`_FakeProcessManager`, this captures the resolved
    harness name so a test can assert which harness the runner chose,
    and signals an event when the background turn reaches dispatch.

    :param captured: Dict the recorded harness name is written into
        under the ``"harness"`` key.
    :param reached: Event set once ``get_client`` is called.
    """

    def __init__(self, captured: dict[str, str], reached: asyncio.Event) -> None:
        """
        Store the capture sink and the reached-dispatch event.

        :param captured: Dict the harness name is written into.
        :param reached: Event set once ``get_client`` is called.
        """
        self._captured = captured
        self._reached = reached

    async def get_client(
        self,
        conversation_id: str,
        harness_name: str,
        *,
        env: dict[str, str] | None = None,
    ) -> _FakeHarnessClient:
        """
        Record the harness name and return an empty fake harness client.

        :param conversation_id: Omnigent conversation id.
        :param harness_name: Harness name the runner resolved — the
            value under test.
        :param env: Optional spawn environment (ignored).
        :returns: A fake harness client with an empty SSE stream so the
            background turn completes immediately.
        """
        del conversation_id, env
        self._captured["harness"] = harness_name
        self._reached.set()
        return _FakeHarnessClient([])

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        """Reaper in-flight marker — no-op for this stub (issue #1414)."""
        del conversation_id, response_id

    def clear_in_flight(self, conversation_id: str) -> None:
        """Reaper in-flight clear — no-op for this stub (issue #1414)."""
        del conversation_id


@pytest.mark.asyncio
async def test_runner_resolves_agent_from_server_snapshot_when_msg_lacks_agent_id() -> None:
    """A turn-triggering message that races ahead of session assignment
    arrives with no ``agent_id`` and an empty spec cache. The runner must
    resolve the agent from the authoritative server snapshot
    (``GET /v1/sessions/{id}``) rather than falling through to the
    test-only ``runner-test-default`` harness, which would silently drop
    the turn (the first-message race).
    """
    conv = "conv_ondemand_race"
    resolved_agent_id = "ag_resolved_from_snapshot"
    resolved_harness = "runner-test-resolved"

    def _server_handler(request: httpx.Request) -> httpx.Response:
        """
        Stub Omnigent server: the session snapshot carries the agent_id.

        :param request: Outbound request from the runner.
        :returns: Snapshot with ``agent_id`` for the session GET; benign
            payloads otherwise so the background turn can proceed.
        """
        if request.method == "GET" and request.url.path == f"/v1/sessions/{conv}":
            return httpx.Response(200, json={"id": conv, "agent_id": resolved_agent_id})
        if request.url.path.endswith("/items"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [],
                    "first_id": None,
                    "last_id": None,
                    "has_more": False,
                },
            )
        return httpx.Response(200, json={})

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    )

    async def _snapshot_spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Resolve the spec for the agent_id read from the snapshot.

        :param agent_id: Agent id the runner resolved. MUST equal the
            snapshot's agent_id — the message body carried none, so any
            other value means the on-demand snapshot path didn't run.
        :param session_id: Session id (unused).
        :returns: A minimal spec whose harness is ``resolved_harness``.
        """
        # The agent_id can only come from the server snapshot here — the
        # POST body below omits it. If this fires with a different value
        # (or not at all), the on-demand resolution path is broken.
        assert agent_id == resolved_agent_id
        return AgentSpec(
            spec_version=1,
            name="ondemand-agent",
            executor=ExecutorSpec(type="omnigent", config={"harness": resolved_harness}),
        )

    captured: dict[str, str] = {}
    reached_dispatch = asyncio.Event()
    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _RecordingProcessManager(captured, reached_dispatch),
        ),
        spec_resolver=_snapshot_spec_resolver,
        server_client=server_client,
    )
    try:
        async with _runner_test_client(app) as http:
            response = await http.post(
                # No ``?stream=true`` → background turn, the production
                # path the Omnigent server uses to forward session messages.
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "x",
                    "content": [{"role": "user", "content": "hi"}],
                    # No agent_id — this is the race condition under test.
                },
            )
            # Background turn accepted; dispatch happens asynchronously.
            assert response.status_code == 202
            await asyncio.wait_for(reached_dispatch.wait(), timeout=10.0)
    finally:
        await server_client.aclose()

    # The harness came from the snapshot-resolved spec, so the
    # runner fetched agent_id from the server when the message lacked it.
    # Without the fix this is "runner-test-default" (the fallback) and the
    # real turn never dispatches.
    assert captured["harness"] == resolved_harness


class _ContentCapturingProcessManager:
    """
    Process manager stub that captures the body sent to the harness.

    Returns a harness client whose ``stream`` records the JSON body
    (which carries the turn's ``content`` history) into a shared sink
    and yields an empty SSE stream so the background turn completes
    immediately.

    :param captured: Dict the harness request body is written into
        under the ``"body"`` key.
    :param reached: Event set once the harness stream is opened.
    """

    def __init__(self, captured: dict[str, Any], reached: asyncio.Event) -> None:
        """
        Store the capture sink and the reached-dispatch event.

        :param captured: Dict the harness request body is written into.
        :param reached: Event set once the harness stream is opened.
        """
        self._captured = captured
        self._reached = reached

    async def get_client(
        self,
        conversation_id: str,
        harness_name: str,
        *,
        env: dict[str, str] | None = None,
    ) -> _ContentCapturingHarnessClient:
        """
        Return a harness client that records the body it is sent.

        :param conversation_id: Omnigent conversation id (unused).
        :param harness_name: Harness name the runner resolved (unused).
        :param env: Optional spawn environment (unused).
        :returns: A capturing harness client.
        """
        del conversation_id, harness_name, env
        return _ContentCapturingHarnessClient(self._captured, self._reached)

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        """Reaper in-flight marker — no-op for this stub (issue #1414)."""
        del conversation_id, response_id

    def clear_in_flight(self, conversation_id: str) -> None:
        """Reaper in-flight clear — no-op for this stub (issue #1414)."""
        del conversation_id


class _ContentCapturingHarnessClient:
    """
    Harness client stub that records the JSON body of each stream.

    :param captured: Dict the request body is written into under
        ``"body"``.
    :param reached: Event set once ``stream`` is invoked.
    """

    def __init__(self, captured: dict[str, Any], reached: asyncio.Event) -> None:
        """
        Store the capture sink and reached event.

        :param captured: Dict the request body is written into.
        :param reached: Event set once ``stream`` is invoked.
        """
        self._captured = captured
        self._reached = reached

    def stream(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any],
        timeout: float | None,
    ) -> _FakeHarnessStream:
        """
        Record the body and return an empty SSE stream.

        :param method: HTTP method (unused).
        :param url: Harness endpoint path (unused).
        :param json: JSON body sent to the harness — captured here.
        :param timeout: Request timeout (unused).
        :returns: An empty fake stream so the turn completes at once.
        """
        del method, url, timeout
        self._captured["body"] = json
        self._reached.set()
        return _FakeHarnessStream([])


@pytest.mark.asyncio
async def test_runner_reloads_full_history_on_cold_cache_after_restart() -> None:
    """A message to a cold session reloads prior history, not just itself.

    Regression for an agent (e.g. nessie) losing all chat context after a
    server/runner restart. On restart the runner's in-memory
    ``_session_histories`` cache is empty; the old code seeded it with ONLY
    the incoming message (``setdefault(conv, []).append(...)``), so the
    harness ran the turn with no prior context. This is acute for the
    claude-sdk harness, which on a cold SDK session replays the in-memory
    history verbatim as the prompt — a one-message cache erased the whole
    conversation.

    The fix rehydrates the full history from the store on the first touch of
    a conversation. The stub server models invariant I1 (persist-before-
    forward): its ``GET /items`` returns the prior turns AND the just-posted
    message (``item_3``), and the forwarded body carries
    ``persisted_item_id="item_3"`` — so the reload drops that exact item by
    id and appends the runner's copy, with no duplication.
    """
    from omnigent.runner import app as runner_app

    conv = "conv_restart_history_reload"
    prior_user = "what is the capital of France?"
    prior_assistant = "Paris."
    new_user = "and of Germany?"

    def _server_handler(request: httpx.Request) -> httpx.Response:
        """
        Stub Omnigent server: snapshot + full persisted history on ``/items``.

        :param request: Outbound request from the runner.
        :returns: Snapshot for the session GET; the persisted history
            (prior turns + the new message, per invariant I1) for
            ``/items``; benign payloads otherwise.
        """
        if request.method == "GET" and request.url.path == f"/v1/sessions/{conv}":
            return httpx.Response(200, json={"id": conv, "agent_id": "ag_restart"})
        if request.url.path.endswith("/items"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "item_1",
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": prior_user}],
                        },
                        {
                            "id": "item_2",
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": prior_assistant}],
                        },
                        # Persist-before-forward (I1): the new message is
                        # already in the store when the runner reloads.
                        {
                            "id": "item_3",
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": new_user}],
                        },
                    ],
                    "first_id": "item_1",
                    "last_id": "item_3",
                    "has_more": False,
                },
            )
        return httpx.Response(200, json={})

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    )

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Resolve a minimal spec for the restarted session.

        :param agent_id: Agent id resolved from the snapshot (unused).
        :param session_id: Session id (unused).
        :returns: A minimal spec on a benign test harness.
        """
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="restart-agent",
            executor=ExecutorSpec(
                type="omnigent",
                config={"harness": "runner-test-resolved"},
            ),
        )

    captured: dict[str, Any] = {}
    reached = asyncio.Event()
    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _ContentCapturingProcessManager(captured, reached),
        ),
        spec_resolver=_spec_resolver,
        server_client=server_client,
    )
    # Simulate a fresh runner process: no cached history for this conv.
    runner_app._session_histories_ref.pop(conv, None)
    try:
        async with _runner_test_client(app) as http:
            response = await http.post(
                # No ``?stream=true`` → background turn, the production path
                # the Omnigent server uses to forward session messages.
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "x",
                    # The store id the Omnigent server persisted for this turn
                    # (matches ``item_3`` from the stub ``/items``), so the
                    # cold-cache reload drops that exact item and the dedup
                    # fires (no duplicate).
                    "persisted_item_id": "item_3",
                    "content": [{"type": "input_text", "text": new_user}],
                },
            )
            assert response.status_code == 202
            await asyncio.wait_for(reached.wait(), timeout=10.0)
    finally:
        await server_client.aclose()
        runner_app._session_histories_ref.pop(conv, None)

    content = captured["body"]["content"]
    texts = [
        block.get("text")
        for item in content
        if isinstance(item, dict)
        for block in item.get("content", [])
        if isinstance(block, dict)
    ]
    # Prior context survived the restart...
    assert prior_user in texts, f"prior user turn missing from reloaded history: {texts}"
    assert prior_assistant in texts, f"prior assistant turn missing: {texts}"
    # ...and the new message is present exactly once (reload didn't dup it).
    assert texts.count(new_user) == 1, f"new message not delivered exactly once: {texts}"
    # The full 3-item history reached the harness, not just the new message.
    assert len(content) == 3, f"expected full history, got {len(content)} items: {content}"


@pytest.mark.asyncio
async def test_runner_cold_cache_appends_message_when_store_lacks_it() -> None:
    """A cold-cache message NOT yet in the store is appended, not dropped.

    Not every forward is persist-before-forward (invariant I1): native-
    terminal web injections (claude-native/codex-native) are forwarded
    WITHOUT persisting first, so a fresh ``GET /items`` returns the prior
    turns but NOT the just-posted message. If the cold-cache reload simply
    overwrote ``_session_histories`` with that load, the new input would be
    dropped — and the native executor, which types only the LATEST user
    message into its pane, would inject stale text.

    This drives the cold path with a stub server whose history reload
    excludes the new message and asserts the harness still receives it,
    appended as the latest turn, with prior context preserved.
    """
    from omnigent.runner import app as runner_app

    conv = "conv_cold_cache_append"
    prior_user = "first question"
    prior_assistant = "first answer"
    new_user = "second question not yet persisted"

    def _server_handler(request: httpx.Request) -> httpx.Response:
        """
        Stub Omnigent server: history reload that does NOT include the new message.

        :param request: Outbound request from the runner.
        :returns: Snapshot for the session GET; prior turns only (no
            new message, modeling a forward-without-persist) for
            ``/items``; benign payloads otherwise.
        """
        if request.method == "GET" and request.url.path == f"/v1/sessions/{conv}":
            return httpx.Response(200, json={"id": conv, "agent_id": "ag_cold"})
        if request.url.path.endswith("/items"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "item_1",
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": prior_user}],
                        },
                        {
                            "id": "item_2",
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": prior_assistant}],
                        },
                        # No item for ``new_user`` — the forward did not
                        # persist it before reaching the runner.
                    ],
                    "first_id": "item_1",
                    "last_id": "item_2",
                    "has_more": False,
                },
            )
        return httpx.Response(200, json={})

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    )

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Resolve a minimal spec for the session.

        :param agent_id: Agent id resolved from the snapshot (unused).
        :param session_id: Session id (unused).
        :returns: A minimal spec on a benign test harness.
        """
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="cold-cache-agent",
            executor=ExecutorSpec(
                type="omnigent",
                config={"harness": "runner-test-resolved"},
            ),
        )

    captured: dict[str, Any] = {}
    reached = asyncio.Event()
    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _ContentCapturingProcessManager(captured, reached),
        ),
        spec_resolver=_spec_resolver,
        server_client=server_client,
    )
    runner_app._session_histories_ref.pop(conv, None)
    try:
        async with _runner_test_client(app) as http:
            response = await http.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "x",
                    # No ``persisted_item_id``: the native-terminal forward
                    # skipped persist-before-forward, so there's nothing in the
                    # store to drop — the runner must append, not dedup.
                    "content": [{"type": "input_text", "text": new_user}],
                },
            )
            assert response.status_code == 202
            await asyncio.wait_for(reached.wait(), timeout=10.0)
    finally:
        await server_client.aclose()
        runner_app._session_histories_ref.pop(conv, None)

    content = captured["body"]["content"]
    texts = [
        block.get("text")
        for item in content
        if isinstance(item, dict)
        for block in item.get("content", [])
        if isinstance(block, dict)
    ]
    # Prior context preserved, and the not-yet-persisted message was
    # appended (not dropped) as the latest turn — present exactly once.
    assert texts == [prior_user, prior_assistant, new_user], (
        f"expected prior history + appended new message, got {texts}"
    )


@pytest.mark.asyncio
async def test_runner_cold_cache_keeps_trailing_user_when_no_persisted_id() -> None:
    """A real trailing user message is kept when no ``persisted_item_id`` is sent.

    Regression for the id-based dedup replacing the old role heuristic. The
    earlier fix unconditionally popped a trailing *user* item, assuming it was
    always this turn's persisted input. That's wrong when the forward did NOT
    persist-before-forward AND the store legitimately ends with a user
    message — e.g. a crash mid-turn where the prior user prompt was persisted
    but its assistant reply never was, or a native-terminal injection. Popping
    there deletes real history.

    With id-based dedup, no ``persisted_item_id`` means nothing is dropped: the
    real trailing user message survives and the new message is appended.
    """
    from omnigent.runner import app as runner_app

    conv = "conv_cold_cache_keep_user"
    # A prior user prompt whose assistant reply was never persisted (e.g. the
    # runner crashed mid-turn), so the store ends on a USER message.
    prior_user = "prompt whose reply was lost to a crash"
    new_user = "follow-up not persisted before forward"

    def _server_handler(request: httpx.Request) -> httpx.Response:
        """
        Stub Omnigent server: history reload ending on a real prior user message.

        :param request: Outbound request from the runner.
        :returns: Snapshot for the session GET; a single prior user item
            (no assistant reply, no new message) for ``/items``; benign
            payloads otherwise.
        """
        if request.method == "GET" and request.url.path == f"/v1/sessions/{conv}":
            return httpx.Response(200, json={"id": conv, "agent_id": "ag_keep"})
        if request.url.path.endswith("/items"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "item_1",
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": prior_user}],
                        },
                    ],
                    "first_id": "item_1",
                    "last_id": "item_1",
                    "has_more": False,
                },
            )
        return httpx.Response(200, json={})

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    )

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Resolve a minimal spec for the session.

        :param agent_id: Agent id (unused).
        :param session_id: Session id (unused).
        :returns: A minimal spec on a benign test harness.
        """
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="keep-user-agent",
            executor=ExecutorSpec(
                type="omnigent",
                config={"harness": "runner-test-resolved"},
            ),
        )

    captured: dict[str, Any] = {}
    reached = asyncio.Event()
    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _ContentCapturingProcessManager(captured, reached),
        ),
        spec_resolver=_spec_resolver,
        server_client=server_client,
    )
    runner_app._session_histories_ref.pop(conv, None)
    try:
        async with _runner_test_client(app) as http:
            response = await http.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "x",
                    # No ``persisted_item_id`` → the trailing user item is NOT
                    # this turn's input, so it must be kept.
                    "content": [{"type": "input_text", "text": new_user}],
                },
            )
            assert response.status_code == 202
            await asyncio.wait_for(reached.wait(), timeout=10.0)
    finally:
        await server_client.aclose()
        runner_app._session_histories_ref.pop(conv, None)

    content = captured["body"]["content"]
    texts = [
        block.get("text")
        for item in content
        if isinstance(item, dict)
        for block in item.get("content", [])
        if isinstance(block, dict)
    ]
    # The real prior user message survived (old role heuristic would drop it)
    # and the new message was appended.
    assert texts == [prior_user, new_user], f"trailing user message must be preserved, got {texts}"


@pytest.mark.asyncio
async def test_runner_cold_cache_uses_resolved_message_not_stored_file_id() -> None:
    """Cold-cache reload of a media turn uses the resolved block, not the store copy.

    The server persists the PRE-resolution body (``file_id`` blocks) and the
    runner resolves ``file_id`` → ``image_url`` itself. So on a cold cache the
    ``GET /items`` tail is the just-posted message in its *unresolved* form,
    while ``message_body`` is the *resolved* form. Any content-equality dedup
    would never match (the blocks differ), which would both duplicate the
    message AND leave an unresolved ``file_id`` block in the history forwarded
    to the harness.

    The fix drops the persisted item by id (``persisted_item_id``, forwarded by
    the server) and appends the runner-resolved message, so the harness sees
    exactly one, fully resolved copy regardless of the content mismatch.
    """
    from omnigent.runner import app as runner_app

    conv = "conv_cold_cache_media"
    prior_user = "earlier question"
    prior_assistant = "earlier answer"
    prompt_text = "what is this?"

    def _server_handler(request: httpx.Request) -> httpx.Response:
        """
        Stub Omnigent server: file resolution, snapshot, and a history reload
        whose tail is the UNRESOLVED (``file_id``) copy of this message.

        :param request: Outbound request from the runner.
        :returns: File metadata / bytes for resolution; the persisted
            history (ending with the unresolved media message, per I1)
            for ``/items``; snapshot for the session GET.
        """
        path = request.url.path
        if path == f"/v1/sessions/{conv}":
            return httpx.Response(200, json={"id": conv, "agent_id": "ag_media"})
        if path.endswith("/resources/files/file_img/content"):
            return httpx.Response(200, content=b"png-bytes", headers={"content-type": "image/png"})
        if path.endswith("/resources/files/file_img"):
            return httpx.Response(
                200,
                json={"id": "file_img", "filename": "photo.png", "content_type": "image/png"},
            )
        if path.endswith("/items"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "item_1",
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": prior_user}],
                        },
                        {
                            "id": "item_2",
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": prior_assistant}],
                        },
                        # I1 persisted THIS message — but in its unresolved
                        # (file_id) form, exactly as received.
                        {
                            "id": "item_3",
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_image",
                                    "file_id": "file_img",
                                    "filename": "photo.png",
                                },
                                {"type": "input_text", "text": prompt_text},
                            ],
                        },
                    ],
                    "first_id": "item_1",
                    "last_id": "item_3",
                    "has_more": False,
                },
            )
        return httpx.Response(200, json={})

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    )

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Resolve a minimal spec for the session.

        :param agent_id: Agent id (unused).
        :param session_id: Session id (unused).
        :returns: A minimal spec on a benign test harness.
        """
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="media-agent",
            executor=ExecutorSpec(
                type="omnigent",
                config={"harness": "runner-test-resolved"},
            ),
        )

    captured: dict[str, Any] = {}
    reached = asyncio.Event()
    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _ContentCapturingProcessManager(captured, reached),
        ),
        spec_resolver=_spec_resolver,
        server_client=server_client,
    )
    runner_app._session_histories_ref.pop(conv, None)
    try:
        async with _runner_test_client(app) as http:
            response = await http.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "x",
                    # Server forwards the id of the (unresolved) item it
                    # persisted; the runner drops it from the reload by id.
                    "persisted_item_id": "item_3",
                    "content": [
                        {"type": "input_image", "file_id": "file_img", "filename": "photo.png"},
                        {"type": "input_text", "text": prompt_text},
                    ],
                },
            )
            assert response.status_code == 202
            await asyncio.wait_for(reached.wait(), timeout=10.0)
    finally:
        await server_client.aclose()
        runner_app._session_histories_ref.pop(conv, None)

    content = captured["body"]["content"]
    # Prior context + the media turn = three items, not four (no duplicate).
    assert len(content) == 3, f"expected no duplicate of the media message: {content}"
    image_msg = content[-1]
    image_block = image_msg["content"][0]
    # The stored unresolved copy was dropped; the resolved block is used —
    # no ``file_id`` leaks to the harness.
    assert "file_id" not in image_block, f"unresolved file_id leaked to harness: {image_block}"
    assert image_block.get("image_url", "").startswith("data:image/png;base64,"), (
        f"expected resolved image_url block, got {image_block}"
    )
    # No unresolved file_id anywhere in the forwarded history.
    flat = json.dumps(content)
    assert "file_id" not in flat, f"unresolved file_id present in forwarded history: {flat}"


@pytest.mark.asyncio
async def test_runner_post_returns_503_when_spec_resolver_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec resolver failures are surfaced as structured 503 errors.

    :param caplog: Pytest log capture, used to confirm the raw cause is
        logged server-side (the other half of the log-and-genericize
        contract).
    :returns: None.
    """

    async def _failing_spec_resolver(
        agent_id: str, session_id: str | None = None
    ) -> AgentSpec | None:
        """
        Raise the resolver failure under test.

        :param agent_id: Agent id requested by the runner.
        :returns: Never returns.
        :raises RuntimeError: Always.
        """
        raise RuntimeError(f"spec resolver unavailable for {agent_id}")

    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager()),
        spec_resolver=_failing_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            response = await http.post(
                "/v1/sessions/conv_spec_resolver_failed/events?stream=true",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_missing",
                    "model": "x",
                    "content": [],
                },
            )

    assert response.status_code == 503
    body = response.json()
    # The structured error slug is preserved for the caller; the detail is a
    # fixed client-safe string. The raw resolver exception text must not leak
    # into the HTTP body (it is logged on the runner instead).
    assert body["error"] == "spec_resolver_failed"
    assert body["detail"] == "Request failed on the runner; see runner logs for details."
    assert "spec resolver unavailable" not in body["detail"]
    # The other half of the contract: the raw cause IS logged for operators.
    # If this fails, log-and-genericize logged nothing and the detail is the
    # only record of the failure — defeating the diagnostic path.
    assert "spec resolver unavailable for ag_missing" in caplog.text


@pytest.mark.asyncio
async def test_runner_stream_emits_failed_when_tool_spec_resolver_fails() -> None:
    """Streaming spec resolver failures emit ``response.failed`` SSE.

    :returns: None.
    """
    chunks = [
        (
            "event: response.created\ndata: "
            '{"type":"response.created","response":{"id":"resp_1"}}\n\n'
        ),
        (
            'event: response.output_item.done\ndata: {"type":"response.output_item.done",'
            '"item":{"type":"function_call","status":"action_required",'
            '"name":"sys_os_read","call_id":"call_1","arguments":"{}"}}\n\n'
        ),
    ]

    async def _failing_spec_resolver(
        agent_id: str, session_id: str | None = None
    ) -> AgentSpec | None:
        """
        Raise during local tool dispatch spec resolution.

        :param agent_id: Agent id requested by the runner.
        :returns: Never returns.
        :raises RuntimeError: Always.
        """
        raise RuntimeError(f"stream spec resolver unavailable for {agent_id}")

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient(chunks)),
        ),
        spec_resolver=_failing_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        response = await http.post(
            "/v1/sessions/conv_stream_spec_resolver_failed/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "harness": _TEST_HARNESS_NAME,
                "agent_id": "ag_stream",
                "model": "x",
                "content": [],
            },
        )

    assert response.status_code == 200
    assert "event: response.created" in response.text
    assert "event: response.failed" in response.text
    # The exception class (a safe, generic label) is still surfaced as the
    # error ``type``; the failure message is a fixed client-safe string. The
    # raw resolver exception text must not leak into the SSE stream (it is
    # logged on the runner instead).
    assert "RuntimeError" in response.text
    assert "Failed to resolve the agent spec for this turn." in response.text
    assert "stream spec resolver unavailable for ag_stream" not in response.text


_INSTRUCTION_WARN_CHUNKS = [
    'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_iw_1"}}\n\n',
    (
        "event: response.completed\ndata: "
        '{"type":"response.completed","response":{"id":"resp_iw_1","status":"completed"}}\n\n'
    ),
]


async def _post_stream_message(http: httpx.AsyncClient, conv: str, **body: Any) -> httpx.Response:
    """POST a minimal ``?stream=true`` message body for the warn-site tests.

    :param http: Test HTTP client bound to the runner app.
    :param conv: Conversation id.
    :param body: Extra fields merged into the message body (``harness``,
        ``harness_override``, ``agent_id``, ...).
    :returns: The runner's HTTP response.
    """
    payload: dict[str, Any] = {
        "type": "message",
        "role": "user",
        "model": "x",
        "content": [],
        **body,
    }
    return await http.post(f"/v1/sessions/{conv}/events?stream=true", json=payload)


@pytest.mark.asyncio
async def test_instruction_delivery_warn_fires_once_for_undelivered_harness(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The delivery-gap warning does not re-fire turn over turn.

    ``kimi`` is declared ``NOT_DELIVERED``; an authored spec talking to it
    must warn on the first turn and stay silent on a second turn in the same
    conversation. The harness — and so the effective delivery value — is
    identical across both turns, which under the dedup rule ("at most once
    per (conversation, harness, delivery value)") is the arm that must stay
    at exactly one warning. Like its agent-switch sibling, a presence-only
    marker also satisfies this arm, so it constrains the repeat-turn case
    rather than the shape of the key.
    """
    conv = "conv_instruction_warn_once"

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="kimi-agent",
            instructions="Be a helpful coding assistant.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "kimi"}),
        )

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient(_INSTRUCTION_WARN_CHUNKS)),
        ),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            r1 = await _post_stream_message(http, conv, agent_id="ag_kimi")
            assert r1.status_code == 200
            r2 = await _post_stream_message(http, conv, agent_id="ag_kimi")
            assert r2.status_code == 200

    warnings = [
        rec for rec in caplog.records if "instructions are not delivered" in rec.getMessage()
    ]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}: {warnings}"
    assert "instruction_delivery=not-delivered" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_instruction_delivery_warn_silent_without_authored_instructions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No author instructions → no warning, even on a non-delivering harness."""
    conv = "conv_instruction_warn_silent_no_author"

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="kimi-agent-no-instructions",
            executor=ExecutorSpec(type="omnigent", config={"harness": "kimi"}),
        )

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient(_INSTRUCTION_WARN_CHUNKS)),
        ),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            response = await _post_stream_message(http, conv, agent_id="ag_kimi_no_instr")
            assert response.status_code == 200

    assert not any("instructions are not delivered" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_instruction_delivery_warn_silent_for_delivering_harness(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A delivering harness (``codex``, ``composed-per-turn``) never warns."""
    conv = "conv_instruction_warn_silent_delivering"

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="codex-agent",
            instructions="Be a helpful coding assistant.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "codex"}),
        )

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient(_INSTRUCTION_WARN_CHUNKS)),
        ),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            response = await _post_stream_message(http, conv, agent_id="ag_codex")
            assert response.status_code == 200

    assert not any("instructions are not delivered" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_instruction_delivery_warn_uses_harness_override(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warn check follows ``harness_override``, not the spec's declared harness.

    The spec declares ``codex`` (delivering); the session's
    ``harness_override`` sends the turn to ``kimi`` (not delivered) instead
    — the warning must reflect the harness the turn actually ran on.
    """
    conv = "conv_instruction_warn_override"

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="codex-agent-overridden",
            instructions="Be a helpful coding assistant.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "codex"}),
        )

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient(_INSTRUCTION_WARN_CHUNKS)),
        ),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            response = await _post_stream_message(
                http, conv, agent_id="ag_codex_override", harness_override="kimi"
            )
            assert response.status_code == 200

    warnings = [
        rec for rec in caplog.records if "instructions are not delivered" in rec.getMessage()
    ]
    assert len(warnings) == 1
    assert "'kimi'" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_instruction_delivery_warn_body_harness_override_wins_over_body_harness(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A body with BOTH `harness` and `harness_override` set must let the
    override win — exercising _stream_message_to_harness's own
    body.get("harness")-truthy else-branch (not the _resolve_effective_turn
    harness_override-only path above, which does not
    depend on body precedence).
    """
    conv = "conv_instruction_warn_body_harness_and_override"

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="both-harness-fields-agent",
            instructions="Be a helpful coding assistant.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "codex"}),
        )

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient(_INSTRUCTION_WARN_CHUNKS)),
        ),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            response = await _post_stream_message(
                http,
                conv,
                agent_id="ag_both_harness_fields",
                # harness (delivering) AND harness_override (not delivered)
                # both present in the same body — override must win.
                harness="codex",
                harness_override="kimi",
            )
            assert response.status_code == 200

    warnings = [
        rec for rec in caplog.records if "instructions are not delivered" in rec.getMessage()
    ]
    assert len(warnings) == 1, (
        f"expected harness_override='kimi' to win over harness='codex'; got {warnings}"
    )
    assert "'kimi'" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_instruction_delivery_warn_canonicalizes_body_harness_alias(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A body-supplied `harness` alias must be canonicalized before capability
    lookup, matching the alias-inheritance contract in docs/AGENT_YAML_SPEC.md.

    ``acp:custom-agent`` canonicalizes to the base ``acp`` harness, which IS
    a delivering harness (FIRST_USER_PREFIX). If canonicalization were
    skipped, ``harness_capabilities().get("acp:custom-agent")`` would miss
    and fall back to UNKNOWN, firing a spurious warning.
    """
    conv = "conv_instruction_warn_body_harness_alias"

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="acp-alias-agent",
            instructions="Be a helpful coding assistant.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "acp:custom-agent"}),
        )

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient(_INSTRUCTION_WARN_CHUNKS)),
        ),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            response = await _post_stream_message(
                http,
                conv,
                agent_id="ag_acp_alias",
                harness="acp:custom-agent",
            )
            assert response.status_code == 200

    assert not any(
        "instructions are not delivered" in rec.getMessage()
        or "declares no instruction_delivery capability" in rec.getMessage()
        for rec in caplog.records
    ), "acp:custom-agent must canonicalize to the delivering 'acp' harness, not warn as unknown"


@pytest.mark.asyncio
async def test_instruction_delivery_warn_unknown_capability_fires_undetermined_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A harness with no registered capability declaration fires the actual
    UNKNOWN-branch warning (undetermined delivery), not the NOT_DELIVERED
    one — driving the runner's real warning code path end-to-end, not just
    constructing an ``InstructionDelivery.UNKNOWN`` value in isolation.
    """
    conv = "conv_instruction_warn_unknown_capability"

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="unregistered-harness-agent",
            instructions="Be a helpful coding assistant.",
            executor=ExecutorSpec(type="omnigent", config={"harness": _TEST_HARNESS_NAME}),
        )

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient(_INSTRUCTION_WARN_CHUNKS)),
        ),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            response = await _post_stream_message(
                http,
                conv,
                agent_id="ag_unregistered_harness",
                harness=_TEST_HARNESS_NAME,
            )
            assert response.status_code == 200

    warnings = [
        rec
        for rec in caplog.records
        if "declares no instruction_delivery capability" in rec.getMessage()
    ]
    assert len(warnings) == 1, f"expected exactly one UNKNOWN-capability warning, got {warnings}"
    assert "instruction_delivery=unknown" in warnings[0].getMessage()
    assert "undetermined" in warnings[0].getMessage()
    assert not any("will not see AgentSpec.instructions" in rec.getMessage() for rec in warnings)


class _RecordingHarnessClient:
    """Fake harness client that records the event body posted to it."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self.posted_bodies: list[dict[str, Any]] = []

    def stream(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, object],
        timeout: float | None,
    ) -> _FakeHarnessStream:
        del method, url, timeout
        self.posted_bodies.append(json)  # type: ignore[arg-type]
        return _FakeHarnessStream(self._chunks)


@pytest.mark.asyncio
async def test_opencode_native_wire_instructions_use_gated_composed_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """opencode-native gets the gated composed value WITH per-request text in it.

    The harness-conditional swap in ``_stream_message_to_harness`` replaces
    ``body["instructions"]`` wholesale for this harness, so the composed value
    it swaps in must already carry this turn's per-request text — otherwise
    genuine caller instructions are silently dropped whenever the spec
    resolves, silently dropping caller input at the swap.

    A caller-supplied ``?stream=true`` body carries RAW per-request text, not
    an internal fallback, so the swap must preserve it rather than discard it.
    """

    async def _fake_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _fake_auto_create_opencode_terminal,
    )

    conv = "conv_opencode_gated_wire"

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="opencode-agent",
            instructions="Be a concise assistant.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        )

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        response = await _post_stream_message(
            http,
            conv,
            agent_id="ag_opencode",
            # Genuine per-request instructions from the caller. Must compose
            # with the agent's authored text, not be replaced by it.
            instructions="Answer only in French.",
        )
        assert response.status_code == 200

    assert recording_client.posted_bodies, "harness never received a request"
    assert recording_client.posted_bodies[0]["instructions"] == (
        "Be a concise assistant.\n\nAnswer only in French."
    ), (
        f"opencode-native must receive the agent's text AND this turn's "
        f"per-request text — got "
        f"{recording_client.posted_bodies[0].get('instructions')!r}. A value of "
        f"'Be a concise assistant.' alone means the gated swap overwrote the "
        f"caller's instructions instead of composing them."
    )


@pytest.mark.asyncio
async def test_direct_stream_instruction_composition_uses_sub_agent_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Direct ``?stream=true`` instruction composition uses the SUB-AGENT's
    own spec, not the root/parent's — for a session dispatched with a known
    harness (so this reaches the "harness already known" else-branch of
    ``_stream_message_to_harness``, not the ``_resolve_effective_turn`` path
    that a background turn would take).
    """

    async def _fake_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _fake_auto_create_opencode_terminal,
    )

    conv = "conv_subagent_composition"

    sub_spec = AgentSpec(
        spec_version=1,
        name="worker",
        instructions="Sub-agent-specific instructions.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
    )
    root_spec = AgentSpec(
        spec_version=1,
        name="root",
        instructions="Root instructions — must not be used for this turn.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        sub_agents=[sub_spec],
    )

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return root_spec

    class _SubAgentSnapshotClient(NullServerClient):
        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": "ag_worker", "sub_agent_name": "worker"}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=_SubAgentSnapshotClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        response = await _post_stream_message(
            http,
            conv,
            agent_id="ag_root",
            harness="opencode-native",
        )
        assert response.status_code == 200

    assert recording_client.posted_bodies, "harness never received a request"
    assert recording_client.posted_bodies[0]["instructions"] == "Sub-agent-specific instructions."


@pytest.mark.asyncio
async def test_direct_stream_instruction_composition_ignores_stale_spec_cache_after_agent_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Direct ``?stream=true`` composition must not use a ``_session_spec_cache``
    entry left over from a PREVIOUS agent on this same conversation.

    The background-turn twin (``_run_turn_bg_setup_and_stream``) detects an
    agent switch (dispatched agent_id differs from the previously recorded
    one) and clears ``_session_spec_cache`` before reading it. This
    direct-stream path (harness already known, so it reads the cache
    directly rather than going through ``_resolve_effective_turn``) must
    apply the same check — the cache is populated here via a real
    background turn for agent A, then drives a direct-stream turn for a
    DIFFERENT agent B and asserts B's instructions are used, not A's stale
    cached spec.
    """

    async def _fake_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _fake_auto_create_opencode_terminal,
    )

    conv = "conv_agent_switch_cache_staleness"

    specs = {
        "ag_agent_a": AgentSpec(
            spec_version=1,
            name="agent-a",
            instructions="Agent A's instructions — must not be reused for agent B.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        ),
        "ag_agent_b": AgentSpec(
            spec_version=1,
            name="agent-b",
            instructions="Agent B's instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        ),
    }

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        return specs[agent_id]

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        # Background turn for agent A populates _session_spec_cache[conv]
        # with A's spec (and _session_agent_ids[conv] = "ag_agent_a").
        bg_resp = await http.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_agent_a",
                "model": "x",
                "content": [{"role": "user", "content": "hi"}],
            },
        )
        assert bg_resp.status_code == 202
        await _await_bg_turn_task(conv)

        # Direct-stream turn for a DIFFERENT agent B, harness already known
        # (the else-branch that reads _session_spec_cache directly).
        response = await _post_stream_message(
            http,
            conv,
            agent_id="ag_agent_b",
            harness="opencode-native",
        )
        assert response.status_code == 200

    assert recording_client.posted_bodies, "harness never received a request"
    assert recording_client.posted_bodies[-1]["instructions"] == "Agent B's instructions."


@pytest.mark.asyncio
async def test_direct_stream_releases_harness_process_on_agent_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A same-harness A->B agent switch on the direct ``?stream=true`` path
    must release the cached harness subprocess, not just evict the spec/
    snapshot dicts.

    Regression: ``process_manager.get_client()`` reuses an existing cached
    subprocess entry regardless of a NEW env/config on that call — it only
    spawns fresh when no entry exists yet. A stateful harness that seeds
    its options once from the FIRST system prompt/instructions it ever
    sees (claude-sdk builds its SDK options from the first spec; Hermes
    only prefixes gated instructions while no vendor session id exists
    yet) would silently keep serving agent A's instructions to agent B's
    turns for the rest of that process's life if only the spec/snapshot
    caches were invalidated — the composed wire ``instructions`` field
    would correctly say "B", but the actual subprocess running underneath
    would still be A's, seeded with A's config. The background dispatch
    path already calls ``process_manager.release()`` on its own explicit
    agent-switch branch; this direct-stream path must match it.
    """

    async def _fake_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _fake_auto_create_opencode_terminal,
    )

    conv = "conv_direct_stream_agent_switch_release"

    specs = {
        "ag_agent_a": AgentSpec(
            spec_version=1,
            name="agent-a",
            instructions="Agent A's instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        ),
        "ag_agent_b": AgentSpec(
            spec_version=1,
            name="agent-b",
            instructions="Agent B's instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        ),
    }

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        return specs[agent_id]

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    fake_process_manager = _FakeProcessManager(recording_client)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, fake_process_manager),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        # Turn 1: direct-stream for agent A. No prior recorded agent, so
        # this is not itself an agent SWITCH — release may or may not fire
        # here (nothing to release yet); what matters is turn 2 below.
        first = await _post_stream_message(
            http, conv, agent_id="ag_agent_a", harness="opencode-native"
        )
        assert first.status_code == 200
        release_calls_after_first_turn = len(fake_process_manager.release_calls)

        # Turn 2: direct-stream for a DIFFERENT agent B on the SAME
        # conversation, same harness — a genuine same-harness agent
        # switch. Must release the cached subprocess.
        second = await _post_stream_message(
            http, conv, agent_id="ag_agent_b", harness="opencode-native"
        )
        assert second.status_code == 200

    assert len(fake_process_manager.release_calls) > release_calls_after_first_turn, (
        f"Expected process_manager.release({conv!r}) to be called on the "
        f"A->B agent switch; release_calls={fake_process_manager.release_calls!r}."
    )
    assert conv in fake_process_manager.release_calls


@pytest.mark.asyncio
async def test_direct_stream_ignores_spec_cache_with_no_recorded_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_session_spec_cache`` can be populated by a path that never records
    ``_session_agent_ids`` (e.g. ``_resolve_session_spec_entry``, called by
    several session-scoped endpoints off a server snapshot's own agent_id) —
    this is not "stale from a KNOWN prior
    agent" but "cached with UNKNOWN provenance entirely"
    (``_session_agent_ids.get(conv_id) is None``). An unset entry must be
    treated as a cache MISS, not as "no conflict, safe to trust" — an
    exact-match requirement, not a mismatch-only check.

    Drives this via ``GET /v1/sessions/{id}/resources``, which calls
    ``_resolve_session_agent_spec`` → ``_resolve_session_spec_entry`` with
    NO ``agent_id_hint`` (it has no per-turn dispatched agent to pass) — it
    populates ``_session_spec_cache`` purely from the session snapshot's
    own agent_id, without ever touching ``_session_agent_ids``. This is
    genuinely UNKNOWN provenance, distinct from a KNOWN-agent-A-then-B
    mismatch: the direct-stream cold-boot path itself now writes
    ``_session_agent_ids`` after resolving (closing an earlier gap), so
    driving this via two direct-stream turns for A then B would no longer
    exercise the unknown-provenance branch at all — it would just be an
    ordinary known switch. The resources endpoint is the one remaining
    code path that populates the spec cache with no agent-id recording
    whatsoever, so the scenario stays meaningful.
    """

    async def _fake_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _fake_auto_create_opencode_terminal,
    )

    conv = "conv_no_provenance_cache"

    specs = {
        "ag_no_prov_a": AgentSpec(
            spec_version=1,
            name="agent-a",
            instructions="Agent A's instructions — must not leak to agent B.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        ),
        "ag_no_prov_b": AgentSpec(
            spec_version=1,
            name="agent-b",
            instructions="Agent B's instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        ),
    }

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        return specs[agent_id]

    class _AgentASnapshotClient(NullServerClient):
        """Server snapshot always reports agent A — feeds the resources
        endpoint's ``_resolve_session_agent_spec`` call, which populates
        ``_session_spec_cache`` WITHOUT touching ``_session_agent_ids``
        (that's the provenance gap under test)."""

        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": "ag_no_prov_a"}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=_AgentASnapshotClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        # Populate _session_spec_cache with agent A's spec via a path that
        # NEVER records _session_agent_ids — genuine unknown provenance,
        # not a known-A-then-B switch.
        resources_resp = await http.get(f"/v1/sessions/{conv}/resources")
        assert resources_resp.status_code == 200

        # Direct-stream turn for agent B — must not reuse the
        # unknown-provenance cache entry left by the resources call.
        second = await _post_stream_message(
            http, conv, agent_id="ag_no_prov_b", harness="opencode-native"
        )
        assert second.status_code == 200

    assert recording_client.posted_bodies, "harness never received a request"
    assert recording_client.posted_bodies[-1]["instructions"] == "Agent B's instructions."


@pytest.mark.asyncio
async def test_background_dispatch_ignores_spec_cache_with_no_recorded_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The BACKGROUND dispatch path (``_run_turn_bg_setup_and_stream``) has its
    own separate cache-provenance check from the direct-stream path; they do
    not share one resolver.

    Genuine unknown provenance is populated via ``GET
    /v1/sessions/{id}/resources``. The direct-stream cold-boot path records
    ``_session_agent_ids`` after resolving, so a direct-stream turn would be
    an ordinary known A-then-B switch rather than the unknown-provenance
    case. Turn 2, for a DIFFERENT agent, goes through the background dispatch
    path and must not trust that unknown-provenance cache entry.
    """

    async def _fake_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _fake_auto_create_opencode_terminal,
    )

    conv = "conv_bg_no_provenance_cache"

    specs = {
        "ag_bg_no_prov_a": AgentSpec(
            spec_version=1,
            name="agent-a",
            instructions="Agent A's instructions — must not leak to agent B.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        ),
        "ag_bg_no_prov_b": AgentSpec(
            spec_version=1,
            name="agent-b",
            instructions="Agent B's instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        ),
    }

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        return specs[agent_id]

    class _AgentASnapshotClient(NullServerClient):
        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": "ag_bg_no_prov_a"}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=_AgentASnapshotClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        # Populate _session_spec_cache with agent A's spec via a path that
        # NEVER records _session_agent_ids — genuine unknown provenance.
        resources_resp = await http.get(f"/v1/sessions/{conv}/resources")
        assert resources_resp.status_code == 200

        # BACKGROUND dispatch for a DIFFERENT agent — must not reuse the
        # unknown-provenance cache entry.
        bg_resp = await http.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_bg_no_prov_b",
                "model": "x",
                "content": [{"role": "user", "content": "hi"}],
            },
        )
        assert bg_resp.status_code == 202
        await _await_bg_turn_task(conv)

    assert recording_client.posted_bodies, "harness never received a request"
    assert recording_client.posted_bodies[-1]["instructions"] == "Agent B's instructions."


@pytest.mark.asyncio
async def test_direct_stream_with_no_agent_id_does_not_trust_a_differently_tagged_cache() -> None:
    """
    A direct-stream turn dispatched with NO ``agent_id`` at all must not
    be handed a cache entry tagged for a DIFFERENT, previously-dispatched
    agent.

    Regression: the composition-site provenance check used to read
    ``not _agent_id or _session_agent_ids.get(conv_id) == _agent_id`` — an
    unset/unknown ``_agent_id`` short-circuited the ``or`` to True,
    treating "we don't know who this turn is for" as automatic "no
    conflict" and handing back whatever was cached, regardless of who it
    actually belonged to. Turn 1 dispatches A, turn 2 dispatches B (a real
    switch), and an unrelated resources call then tags
    ``_session_spec_cache[conv]`` with B. Turn 3 has NO ``agent_id`` in
    its body at all — with the bug, it would inherit B's cached
    instructions merely because "no id" was treated as harmless; the fix
    treats an entry tagged for a KNOWN agent as a miss when the current
    turn's agent isn't known, so composition degrades to "unknown"
    instead of guessing.
    """
    conv = "conv_no_agent_id_turn_no_leak"

    specs = {
        "ag_no_id_a": AgentSpec(
            spec_version=1,
            name="agent-a",
            instructions="Agent A's instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        ),
        "ag_no_id_b": AgentSpec(
            spec_version=1,
            name="agent-b",
            instructions="Agent B's instructions — must not leak to a no-agent-id turn.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        ),
    }

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        return specs[agent_id]

    class _AgentBSnapshotClient(NullServerClient):
        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": "ag_no_id_b"}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=_AgentBSnapshotClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        # Turn 1: agent A.
        first = await _post_stream_message(http, conv, agent_id="ag_no_id_a", harness="hermes")
        assert first.status_code == 200

        # Turn 2: agent B — a real switch, so _session_agent_ids[conv] = B.
        second = await _post_stream_message(http, conv, agent_id="ag_no_id_b", harness="hermes")
        assert second.status_code == 200

        # An unrelated resources call tags _session_spec_cache[conv] with B.
        resources_resp = await http.get(f"/v1/sessions/{conv}/resources")
        assert resources_resp.status_code == 200

        # Turn 3: NO agent_id at all in the body. Must NOT inherit B's
        # cached instructions.
        third = await _post_stream_message(http, conv, harness="hermes")
        assert third.status_code == 200

    last_body = recording_client.posted_bodies[-1]
    assert last_body.get("instructions") != (
        "Agent B's instructions — must not leak to a no-agent-id turn."
    ), (
        f"A no-agent-id turn must not be handed B's cached instructions — "
        f"got instructions={last_body.get('instructions')!r}."
    )


@pytest.mark.asyncio
async def test_direct_stream_survives_resources_snapshot_of_different_agent_mid_conversation() -> (
    None
):
    """
    A B-tagged spec-cache entry must not drive a turn dispatched as A.

    Forces a REAL A-vs-B provenance conflict all the way to the tagged-cache
    check, with both of the defenses that would otherwise mask it held off:

    - The B-tagged entry is written by real code, not seeded. The
      ``GET .../resources`` call runs BEFORE this session has dispatched any
      turn, so ``_session_agent_ids`` holds no marker for it to hint with and
      ``_resolve_session_spec_entry`` falls back to the session snapshot —
      which reports agent B — caching B's spec tagged B. Once a marker does
      exist that endpoint hints with it and the snapshot is never consulted,
      so this ordering is the only way the conflicting entry can arise.
    - The marker is then set to A directly. Reaching that state via a real A
      turn is impossible: a turn whose agent id differs from the marker trips
      ``_invalidate_session_agent_state`` first, which clears the spec cache
      outright and masks exactly the check under test. That switch
      invalidation is a SEPARATE defense with its own tests; this one targets
      the per-entry provenance tag sitting behind it.

    With the marker reading "A" the A turn is not flagged as a switch at all,
    so untagged-cache behavior would hand back whatever sits under
    ``conv_id`` — B's spec, and B's instructions on the wire. The entry's own
    agent tag is what makes it a clean miss and forces A to be re-resolved.
    """
    from omnigent.runner.app import _session_agent_ids_ref

    conv = "conv_resources_snapshot_of_other_agent"
    resolved_agent_ids: list[str] = []

    specs = {
        "ag_snap_leak_a": AgentSpec(
            spec_version=1,
            name="agent-a",
            instructions="Agent A's instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        ),
        "ag_snap_leak_b": AgentSpec(
            spec_version=1,
            name="agent-b",
            instructions="Agent B's instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        ),
    }

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        resolved_agent_ids.append(agent_id)
        return specs[agent_id]

    class _AgentBSnapshotClient(NullServerClient):
        """Session snapshot reports agent B — the lagging/independent view
        the resources endpoint resolves off when it has no marker to hint
        with."""

        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": "ag_snap_leak_b"}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=_AgentBSnapshotClient(),  # type: ignore[arg-type]
    )
    try:
        async with _runner_test_client(app) as http:
            # No turn has run, so no marker exists to hint with: this
            # resolves off the snapshot (agent B) and is the FIRST write to
            # _session_spec_cache[conv] — B's spec, tagged B.
            resources_resp = await http.get(f"/v1/sessions/{conv}/resources")
            assert resources_resp.status_code == 200
            assert resolved_agent_ids == ["ag_snap_leak_b"], (
                f"setup failed: the resources call was supposed to resolve the "
                f"snapshot's agent B and leave a B-tagged cache entry — resolver "
                f"saw {resolved_agent_ids!r}. Without that entry the assertion "
                f"below would be vacuous."
            )

            # Model "this session last dispatched as A" without routing
            # through a switch that would clear the cache first (see
            # docstring). This is the state the tagged read must survive.
            _session_agent_ids_ref[conv] = "ag_snap_leak_a"

            # Not a switch (marker already says A), so nothing evicts the
            # B-tagged entry before the tagged read keyed on A hits it.
            turn = await _post_stream_message(
                http, conv, agent_id="ag_snap_leak_a", harness="hermes"
            )
            assert turn.status_code == 200
    finally:
        _session_agent_ids_ref.pop(conv, None)

    assert [b["instructions"] for b in recording_client.posted_bodies] == [
        "Agent A's instructions."
    ], (
        f"Wire instructions must be A's — got "
        f"{[b.get('instructions') for b in recording_client.posted_bodies]!r}. "
        f"'Agent B's instructions.' means the B-tagged entry the resources "
        f"call left behind was trusted for a turn dispatched as A."
    )
    assert resolved_agent_ids[-1] == "ag_snap_leak_a", (
        f"the A turn must re-resolve A after missing the B-tagged entry — "
        f"resolver saw {resolved_agent_ids!r}."
    )


@pytest.mark.asyncio
async def test_tool_schemas_cache_retries_after_unresolved_spec_turn() -> None:
    """
    Stale-negative-cache scenario: ``_session_tool_schemas``
    used to cache an empty tool list whenever the read-gate missed,
    REGARDLESS of whether a spec was actually available to build schemas
    from. A turn whose spec resolution is transiently unavailable (e.g. the
    agent hasn't propagated to the spec store yet) permanently pinned this
    conversation's tool list to empty — even once a LATER turn's spec
    resolves fine, because ``[] is not None`` reads as a settled cache hit.
    An unresolved-spec turn leaves the cache UNSET, so the next turn (spec
    now resolvable) retries and populates real schemas.
    """
    conv = "conv_tool_schema_retry"
    call_count = 0

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec | None:
        nonlocal call_count
        del session_id, agent_id
        call_count += 1
        if call_count == 1:
            # Turn 1: spec transiently unavailable — a legitimate "not
            # resolvable right now" None, not an exception.
            return None
        return AgentSpec(
            spec_version=1,
            name="agent-a",
            executor=ExecutorSpec(type="omnigent", config={"harness": "runner-test-default"}),
        )

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        first = await http.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_retry",
                "model": "x",
                "content": [{"role": "user", "content": "hi"}],
            },
        )
        assert first.status_code == 202
        await _await_bg_turn_task(conv)

        second = await http.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_retry",
                "model": "x",
                "content": [{"role": "user", "content": "hi again"}],
            },
        )
        assert second.status_code == 202
        await _await_bg_turn_task(conv)

    assert call_count >= 2, (
        f"expected spec_resolver to be re-invoked on turn 2 (turn 1's None "
        f"result must not be trusted as final) — got {call_count} calls."
    )
    second_tools = recording_client.posted_bodies[-1].get("tools")
    assert second_tools, (
        f"turn 2's real spec must produce non-empty tool schemas — got "
        f"{second_tools!r}. An empty result means turn 1's unresolved-spec "
        f"empty tool cache was wrongly trusted as final."
    )


@pytest.mark.asyncio
async def test_tool_schemas_mcp_only_partial_result_is_not_cached_as_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A turn where the builtin (``ToolManager``) half fails but MCP resolution
    succeeds must not cache the MCP-only remainder as the conversation's
    final tool list.

    The MCP merge reads the builtin half back out of ``_session_tool_schemas``
    and treats an absent entry as ``[]``, so on a transient ToolManager
    failure it would cache ``[] + mcp_schemas`` — and stamp the matching MCP
    spec hash. Both gates then read as settled on every later turn (the tool
    cache is non-``None``, the hash matches), so the builtin half is never
    retried and the agent runs the rest of the conversation with MCP tools
    only. The failed turn caches nothing, and the next turn
    (ToolManager now working) publishes builtin AND MCP schemas together.
    """
    conv = "conv_tool_schema_partial"
    toolmanager_calls = 0

    class _FakeToolManager:
        """Fails the first build (transient), succeeds afterwards."""

        def __init__(self, spec: object, workdir: object = None) -> None:
            nonlocal toolmanager_calls
            del spec, workdir
            toolmanager_calls += 1
            if toolmanager_calls == 1:
                raise RuntimeError("transient: tool registry unavailable")

        def get_tool_schemas(self) -> list[dict[str, Any]]:
            return [{"name": "sys_builtin_thing"}]

    class _FakeProxyMcpManager:
        """MCP half always resolves — the combination under test."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def schemas_for(self, spec: object) -> SimpleNamespace:
            del spec
            return SimpleNamespace(schemas=[{"name": "jira__search"}])

    import omnigent.runner.app as runner_app_mod
    import omnigent.tools.manager as tool_manager_mod

    monkeypatch.setattr(tool_manager_mod, "ToolManager", _FakeToolManager)
    monkeypatch.setattr(runner_app_mod, "ProxyMcpManager", _FakeProxyMcpManager)

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="agent-a",
            mcp_servers=[MCPServerConfig(name="jira", transport="http", url="http://x")],
            executor=ExecutorSpec(type="omnigent", config={"harness": "runner-test-default"}),
        )

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        for _text in ("hi", "hi again"):
            resp = await http.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_partial",
                    "model": "x",
                    "content": [{"role": "user", "content": _text}],
                },
            )
            assert resp.status_code == 202
            await _await_bg_turn_task(conv)

    assert toolmanager_calls >= 2, (
        f"the builtin half must be retried on turn 2 — ToolManager was built "
        f"{toolmanager_calls} time(s). Exactly 1 means turn 1's MCP-only "
        f"partial result was cached as final."
    )
    _names = json.dumps(recording_client.posted_bodies[-1].get("tools") or [])
    assert "sys_builtin_thing" in _names and "jira__search" in _names, (
        f"turn 2 must publish the builtin AND MCP schemas together — got "
        f"{_names}. A wire list carrying only 'jira__search' means the "
        f"MCP-only partial set from turn 1 was trusted as the final tool list."
    )


@pytest.mark.asyncio
async def test_agent_switch_closes_and_rebuilds_the_comment_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An in-conversation agent switch must close and evict the session's
    comment relay so the next turn rebuilds it from the new agent's spec.

    The relay is agent-derived — its advertised tool set is
    ``build_native_relay_tool_schemas(resolved_spec)`` — but it is stored
    untagged in ``_session_comment_relays`` and ``_ensure_comment_relay_started``
    gates purely on presence. Native harnesses ignore the wire ``tools`` list,
    so the relay IS their entire tool surface: left running across a switch,
    the new agent keeps being served the PREVIOUS agent's gated tools for the
    life of the session. Switching A -> B closes the A relay
    and starts a B relay advertising B's tools.
    """
    conv = "conv_relay_switch"
    started_tools: list[list[str]] = []
    closed = 0

    class _FakeRelay:
        def close(self) -> None:
            nonlocal closed
            closed += 1

    def _fake_start_tool_relay(*, tools: list[dict[str, Any]], **kwargs: object) -> _FakeRelay:
        del kwargs
        started_tools.append([t["name"] for t in tools])
        return _FakeRelay()

    def _fake_build_native_relay_tool_schemas(spec: Any) -> list[dict[str, Any]]:
        return [{"name": f"relay_for_{getattr(spec, 'name', None)}"}]

    import omnigent.claude_native_bridge as claude_bridge_mod
    import omnigent.runner.tool_dispatch as tool_dispatch_mod

    monkeypatch.setattr(claude_bridge_mod, "start_tool_relay", _fake_start_tool_relay)
    monkeypatch.setattr(claude_bridge_mod, "post_tools_changed", lambda _bridge_dir: None)
    monkeypatch.setattr(
        tool_dispatch_mod,
        "build_native_relay_tool_schemas",
        _fake_build_native_relay_tool_schemas,
    )

    specs = {
        "ag_relay_a": AgentSpec(
            spec_version=1,
            name="agent-a",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        ),
        "ag_relay_b": AgentSpec(
            spec_version=1,
            name="agent-b",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        ),
    }

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        return specs[agent_id]

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        for _agent in ("ag_relay_a", "ag_relay_b"):
            resp = await http.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": _agent,
                    "model": "x",
                    "content": [{"role": "user", "content": "hi"}],
                },
            )
            assert resp.status_code == 202
            await _await_bg_turn_task(conv)

    assert started_tools == [["relay_for_agent-a"], ["relay_for_agent-b"]], (
        f"the A->B switch must close the A relay and start a B relay carrying "
        f"B's tool surface — relays started with {started_tools!r}. A single "
        f"entry means agent B kept being served A's relay tools."
    )
    assert closed == 1, (
        f"the previous agent's relay must be closed on the switch, not just "
        f"dropped — close() called {closed} time(s)."
    )


@pytest.mark.asyncio
async def test_list_session_resources_uses_recorded_agent_id_as_hint() -> None:
    """
    Dropped-provenance-hint scenario: ``list_session_resources``
    (``GET /v1/sessions/{id}/resources``) used to call
    ``_resolve_session_agent_spec(session_id)`` with no hint, so it always
    resolved off whatever the session snapshot reported — even once
    ``_session_agent_ids`` already recorded this session's REAL current
    agent from a dispatched turn, and the snapshot is lagging (the same
    in-flight-switch race already closed at other call sites that pass the
    hint). Once a turn has dispatched agent A, a resources
    call resolves A's spec via the ``agent_id_hint``, not the snapshot's
    (stale) agent B.
    """
    conv = "conv_resources_hint"
    resolved_agent_ids: list[str] = []

    specs = {
        "ag_hint_a": AgentSpec(
            spec_version=1,
            name="agent-a",
            instructions="Agent A.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        ),
        "ag_hint_b": AgentSpec(
            spec_version=1,
            name="agent-b",
            instructions="Agent B.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        ),
    }

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        resolved_agent_ids.append(agent_id)
        return specs[agent_id]

    class _StaleBSnapshotClient(NullServerClient):
        """Session snapshot always reports agent B — models a server-side
        snapshot lagging an in-flight switch to A."""

        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": "ag_hint_b"}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=_StaleBSnapshotClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        # Dispatch turn for agent A. hermes has no cold-boot step, so this
        # only records _session_agent_ids[conv] = "A" — no spec resolution
        # yet, so the resources call below is the first _session_spec_cache
        # write for this conversation.
        first = await _post_stream_message(http, conv, agent_id="ag_hint_a", harness="hermes")
        assert first.status_code == 200

        # The resources call has no per-request agent_id of its own; the
        # session snapshot (stale) reports B. Pre-fix this resolved B's spec.
        resources_resp = await http.get(f"/v1/sessions/{conv}/resources")
        assert resources_resp.status_code == 200

    assert "ag_hint_b" not in resolved_agent_ids and "ag_hint_a" in resolved_agent_ids, (
        f"list_session_resources must resolve the session's recorded agent "
        f"(A) via agent_id_hint, not the stale snapshot's agent (B) — "
        f"resolver saw {resolved_agent_ids!r}."
    )


@pytest.mark.parametrize(
    "harness_name",
    [
        pytest.param("claude-native", id="claude-native"),
        pytest.param("codex-native", id="codex-native"),
        pytest.param("pi-native", id="pi-native"),
        pytest.param("cursor-native", id="cursor-native"),
        pytest.param("opencode-native", id="opencode-native"),
        pytest.param("kimi-native", id="kimi-native"),
        pytest.param("runner-test-default", id="repl"),
    ],
)
@pytest.mark.asyncio
async def test_initialize_session_arms_use_request_agent_id_as_hint(
    harness_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Every ``_initialize_session`` terminal arm must resolve its spec with the
    request's own ``agent_id`` as the hint.

    Each arm used to call ``_resolve_session_agent_spec(session_id)`` bare,
    even though ``agent_id`` is this function's just-validated, authoritative
    identity for the whole call — already written into ``_session_spec_cache``
    before the arm runs. Without the hint, resolution falls back to a
    session-snapshot fetch that can be absent or lagging right after session
    creation, and the terminal launches with none of the spec-derived inputs.

    The snapshot GET 404s for the whole test, so a bare resolve raises
    ``OmnigentError`` and every arm degrades to no spec — the
    ``skills_filter`` can then only arrive via the hint. The
    resolver is also checked to have seen that exact agent id, and it keys the
    spec it returns off the id it was handed, so a wrong or dropped id cannot
    produce this value either.

    :param harness_name: Harness selecting the arm under test.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import omnigent.runner.app as runner_app_mod
    from omnigent.errors import OmnigentError
    from omnigent.terminals import TerminalRegistry

    agent_id = f"ag_hint_{harness_name.replace('-', '_')}"
    captured: dict[str, Any] = {}
    resolved_agent_ids: list[str] = []

    async def _capture_kwargs_terminal(*args: object, **kwargs: object) -> None:
        """Claude / codex / REPL arms hand the resolved spec straight in."""
        del args
        if "skills_filter" in kwargs:
            captured["skills_filter"] = kwargs.get("skills_filter")
        else:
            _spec = kwargs.get("agent_spec")
            captured["skills_filter"] = getattr(_spec, "skills_filter", None)

    async def _capture_launch_native_terminal(
        _harness: str,
        _ctx: object,
        *,
        ensure_locks: object = None,
        resolve_agent_spec: Any = None,
        build_context: Any = None,
        **_kw: object,
    ) -> bool:
        """pi / cursor / opencode / kimi arms pass a resolver callable; the
        claude / codex arms enrich the launch context instead, so the spec
        they resolve arrives on the context this returns."""
        del ensure_locks, _kw
        if resolve_agent_spec is not None:
            try:
                _spec = await resolve_agent_spec()
            except OmnigentError:
                _spec = None
            captured["skills_filter"] = getattr(_spec, "skills_filter", None)
        if build_context is not None:
            _built = await build_context(_ctx)
            _filter = getattr(_built, "skills_filter", None)
            if _filter is None:
                _filter = getattr(getattr(_built, "agent_spec", None), "skills_filter", None)
            captured["skills_filter"] = _filter
        return True

    async def _no_transfer(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return False

    async def _codex_needs_terminal(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    for _name in (
        "_auto_create_claude_terminal",
        "_auto_create_codex_terminal",
        "_auto_create_repl_terminal",
    ):
        monkeypatch.setattr(runner_app_mod, _name, _capture_kwargs_terminal)
    monkeypatch.setattr(runner_app_mod, "_launch_native_terminal", _capture_launch_native_terminal)
    monkeypatch.setattr(
        runner_app_mod, "_claude_native_terminal_arrives_via_transfer", _no_transfer
    )
    monkeypatch.setattr(
        runner_app_mod, "_codex_session_needs_runner_terminal", _codex_needs_terminal
    )
    monkeypatch.setattr(
        runner_app_mod, "_ensure_orchestrator_skills_in_bundle", lambda *a, **k: None
    )

    conv = f"conv_arm_{harness_name.replace('-', '_')}"

    async def _spec_resolver(resolved_agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Key the returned spec off the id handed in, so a wrong id cannot
        produce the expected ``skills_filter``."""
        del session_id
        resolved_agent_ids.append(resolved_agent_id)
        return AgentSpec(
            spec_version=1,
            name=f"agent-for-{resolved_agent_id}",
            instructions="Agent instructions.",
            skills_filter=[f"skill-for-{resolved_agent_id}"],
            executor=ExecutorSpec(type="omnigent", config={"harness": harness_name}),
        )

    class _BrokenSnapshotClient(NullServerClient):
        """Session snapshot GET fails outright — models a session not yet
        propagated to the server's read path, the race the hint must survive."""

        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 404

                def json(self) -> dict[str, object]:
                    return {"error": "not_found"}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(_FakeHarnessClient([]))),
        spec_resolver=_spec_resolver,
        server_client=_BrokenSnapshotClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )
    async with _runner_test_client(app) as http:
        resp = await http.post(
            "/v1/sessions",
            json={"session_id": conv, "agent_id": agent_id},
        )
        assert resp.status_code == 201, resp.text

    assert resolved_agent_ids == [agent_id], (
        f"the request's exact agent_id must be what reaches the spec resolver "
        f"— resolver saw {resolved_agent_ids!r}, expected [{agent_id!r}]."
    )
    assert captured.get("skills_filter") == [f"skill-for-{agent_id}"], (
        f"the {harness_name} arm must receive the spec resolved via the "
        f"request's own agent_id hint, not lose it because the session "
        f"snapshot GET is broken — captured={captured!r}."
    )


@pytest.mark.asyncio
async def test_agent_cache_reset_route_forces_fresh_resolve() -> None:
    """
    ``POST /v1/sessions/{id}/agent-cache/reset`` must clear the cached
    spec so a turn dispatched immediately after re-resolves it (observing
    whatever the resolver returns NOW) rather than reusing the pre-reset
    entry.

    Modeled by a resolver that returns a DIFFERENT spec on its second call
    for the SAME agent id (simulating the agent's definition having
    changed) — if the cache survived the reset, the post-reset turn would
    still observe the stale first-call spec. Uses a non-native harness
    (``claude-sdk``) and background dispatch so each turn resolves the
    spec exactly once (no cold-boot second resolve to confound the count),
    keeping the causal chain from "reset happened" to "fresh resolve
    happened" unambiguous.
    """
    conv = "conv_agent_cache_reset_forces_fresh_resolve"

    calls = {"n": 0}

    def _spec_for_call(n: int) -> AgentSpec:
        return AgentSpec(
            spec_version=1,
            name="agent-a",
            instructions=f"Agent A's instructions v{n}.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        )

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        calls["n"] += 1
        return _spec_for_call(calls["n"])

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        first_resp = await http.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_reset_prov_a",
                "model": "x",
                "content": [{"role": "user", "content": "hi"}],
            },
        )
        assert first_resp.status_code == 202
        await _await_bg_turn_task(conv)

        reset_resp = await http.post(f"/v1/sessions/{conv}/agent-cache/reset", json={})
        assert reset_resp.status_code == 200

        second_resp = await http.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_reset_prov_a",
                "model": "x",
                "content": [{"role": "user", "content": "hi again"}],
            },
        )
        assert second_resp.status_code == 202
        await _await_bg_turn_task(conv)

    assert [b["instructions"] for b in recording_client.posted_bodies] == [
        "Agent A's instructions v1.",
        "Agent A's instructions v2.",
    ], (
        f"Expected a fresh resolve (v2) after the agent-cache reset — got "
        f"{[b['instructions'] for b in recording_client.posted_bodies]!r}. A "
        f"repeated 'v1' means the cached spec survived the reset and was "
        f"trusted for the next turn instead of being re-resolved."
    )


def test_agent_cache_reset_clears_the_agent_id_marker_too() -> None:
    """
    ``_clear_session_agent_caches`` (shared by the ``/reset-state`` and
    ``/agent-cache/reset`` routes, and by ``_invalidate_session_agent_state``
    on an in-conversation switch) must pop ``_session_agent_ids`` along
    with the tagged caches it already pops.

    Source-level guard, matching the established pattern for this
    closure-local, HTTP-unobservable state (see
    ``test_direct_and_background_switch_sites_share_one_invalidation_routine``
    above): every tagged cache entry carries its own provenance now, so a
    stale ``_session_agent_ids`` value can no longer cause a WRONG spec to
    be trusted — but it can still cause an informational reader (MCP
    execute, background-title, ``_session_harness_name``, ...) to look up
    the wrong agent's tag right after a reset. Leaving the reset routes'
    shared cache-clearing helper without this pop would silently
    reintroduce that staleness.
    """
    import inspect

    import omnigent.runner.app as runner_app_mod

    source = inspect.getsource(runner_app_mod)
    start = source.index("def _clear_session_agent_caches(")
    end = source.index("\n    async def _invalidate_session_agent_state(", start)
    body = source[start:end]

    assert "_session_agent_ids.pop(" in body, (
        "_clear_session_agent_caches must pop _session_agent_ids(session_id) "
        "so it doesn't outlive the caches it's supposed to describe."
    )


@pytest.mark.asyncio
async def test_background_dispatch_warns_and_uses_parent_on_unresolvable_sub_agent_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Background dispatch (``_run_turn_bg_setup_and_stream``) runs the turn on
    the PARENT spec and warns when the recovered ``sub_agent_name`` (from a
    session snapshot, via ``_recover_sub_agent_name``) does not resolve in
    the spec tree.

    This used to raise ``NOT_FOUND`` and publish a ``failed`` status for the
    turn. Both halves of the replacement are asserted: the warning must be
    emitted AND the turn must reach the parent's harness — a silent fallback
    satisfies neither.

    No prior ``POST /v1/sessions`` call here: the cache starts empty, so the
    background turn's own on-demand ``spec_resolver`` call is the ONLY
    resolution that happens. Background dispatch calls
    ``spec_resolver`` only when the cache is empty, so a pre-populated cache
    would leave that call unmade and the turn would never exercise the
    no-match path at all.

    :param caplog: Pytest log capture, used to confirm the unresolved
        sub-agent is reported rather than passed over in silence.
    """
    conv = "conv_bg_recovered_sub_agent_gone"

    # A non-native harness: the turn now runs to completion instead of
    # stopping at the unresolved sub-agent, so it reaches cold boot, and a
    # native harness would try to launch a vendor CLI that isn't present.
    root_spec_without_worker = AgentSpec(
        spec_version=1,
        name="root",
        instructions="Root instructions.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    calls = {"n": 0}

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        calls["n"] += 1
        return root_spec_without_worker

    class _SnapshotWithSubAgentName(NullServerClient):
        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": "ag_root", "sub_agent_name": "worker"}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    recorder = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    pm = _FakeProcessManager(recorder)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, pm),
        spec_resolver=_spec_resolver,
        server_client=_SnapshotWithSubAgentName(),  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_test_client(app) as http:
            bg_resp = await http.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_root",
                    "model": "x",
                    "content": [{"role": "user", "content": "hi"}],
                },
            )
            assert bg_resp.status_code == 202
            await _await_bg_turn_task(conv)
            statuses = await _drain_published_statuses(conv, until="idle", timeout=2.0)

    assert "failed" not in statuses, (
        f"An unresolvable recorded sub_agent_name must not fail the turn; "
        f"published statuses were {statuses!r}."
    )
    assert "'worker'" in caplog.text and "did not resolve" in caplog.text, (
        f"The unresolved sub-agent must be warned about; got {caplog.text!r}."
    )
    assert [harness for _conv, harness, _env in pm.get_client_calls] == ["claude-sdk"], (
        f"The PARENT's harness must drive the turn; got {pm.get_client_calls!r}."
    )
    assert recorder.posted_bodies, "harness never received a request"
    assert recorder.posted_bodies[-1]["instructions"] == "Root instructions.", (
        f"The PARENT's authored instructions must compose for the turn; got "
        f"{recorder.posted_bodies[-1]['instructions']!r}."
    )
    assert calls["n"] == 1, (
        f"Expected exactly one spec_resolver call (the on-demand resolution "
        f"when the cache is empty); got {calls['n']}. A count of 2+ here "
        f"would mean the scenario isn't reproducing the real single-resolution "
        f"code path."
    )


@pytest.mark.asyncio
async def test_background_dispatch_succeeds_when_cache_already_holds_resolved_child() -> None:
    """
    A background turn must succeed using an ALREADY-RESOLVED child spec
    from the cache — not re-search that child's own descendants for its
    own name and (mis)report a miss.

    Session creation resolves the "worker" sub_agent_name and caches the
    CHILD spec directly (not the root) — this is the real, common shape:
    ``_session_spec_cache[conv]`` holds ``worker``'s own :class:`AgentSpec`
    after ``POST /v1/sessions``. The first background turn then re-derives
    ``sub_agent_name="worker"`` (via ``_recover_sub_agent_name``) and must
    re-resolve it against the cached spec. Regression: ``_find_spec_by_name``
    used to only search ``spec.sub_agents``, so handing it the already-
    resolved child looked for "worker" as a grandchild of itself and
    (falsely) reported a miss, failing the very first real background turn
    for any session created with a sub_agent_name.
    """
    # A non-native harness (no session-create-time terminal auto-launch
    # adapter) — the subject here is _find_spec_by_name's root self-check,
    # not opencode's terminal machinery, which needs a real opencode CLI on
    # PATH and would otherwise fail session-create for an unrelated reason.
    conv = "conv_bg_cache_already_resolved_child"

    worker_spec = AgentSpec(
        spec_version=1,
        name="worker",
        instructions="Worker instructions.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )
    root_spec = AgentSpec(
        spec_version=1,
        name="root",
        instructions="Root instructions — must not be used for this turn.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        sub_agents=[worker_spec],
    )

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return root_spec

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        create_resp = await http.post(
            "/v1/sessions",
            json={
                "session_id": conv,
                "agent_id": "ag_root",
                "sub_agent_name": "worker",
            },
        )
        assert create_resp.status_code == 201

        bg_resp = await http.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_root",
                "model": "x",
                "content": [{"role": "user", "content": "hi"}],
            },
        )
        assert bg_resp.status_code == 202
        await _await_bg_turn_task(conv)
        statuses = await _drain_published_statuses(conv, until="idle", timeout=2.0)

    assert "failed" not in statuses, (
        f"Background turn should succeed using the already-resolved cached "
        f"child spec, not fail as if 'worker' were unresolvable; got "
        f"statuses={statuses}."
    )
    assert recording_client.posted_bodies, "harness never received a request"
    assert recording_client.posted_bodies[-1]["instructions"] == "Worker instructions."


@pytest.mark.asyncio
async def test_background_dispatch_evicts_rejected_cache_entry_not_just_agent_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A background-path provenance rejection must EVICT the untrusted cache
    entry, not just update ``_session_agent_ids`` to the current agent.

    Regression: turn 1 (agent B) correctly rejects an unknown-provenance
    stale cache entry (agent A's spec) for THIS read, but
    ``_session_agent_ids[conv]`` is unconditionally set to "B" regardless
    of whether resolution succeeds afterward. If resolution then fails (or
    is never attempted), the stale agent-A spec is left untouched in
    ``_session_spec_cache``. Turn 2 (also agent B) recomputes provenance as
    "prior == dispatched" (both now "B", since turn 1 already wrote that) —
    vacuously true — and trusts the still-resident agent-A spec as if it
    were agent B's, laundering stale data back in as soon as the identity
    record catches up to it.
    """

    async def _fake_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _fake_auto_create_opencode_terminal,
    )

    conv = "conv_bg_provenance_reject_no_evict"

    spec_a = AgentSpec(
        spec_version=1,
        name="agent-a",
        instructions="Agent A's instructions — must never reach agent B's turn.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
    )
    spec_b = AgentSpec(
        spec_version=1,
        name="agent-b",
        instructions="Agent B's instructions.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
    )

    resolver_calls_for_b = {"n": 0}

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        if agent_id == "ag_agent_a":
            return spec_a
        resolver_calls_for_b["n"] += 1
        if resolver_calls_for_b["n"] == 1:
            # Turn 1's on-demand resolution fails — cached_spec stays None
            # for THIS read (correct degrade), but must not leave the
            # stale agent-A entry sitting in the cache for turn 2 to find.
            raise RuntimeError("transient resolver failure")
        return spec_b

    # The snapshot's reported agent_id tracks whichever agent most recently
    # dispatched — like a real server, which updates its own conv.agent_id
    # record BEFORE routing a turn to the runner (so any snapshot GET
    # issued afterward, e.g. by the post-turn continuation task, correctly
    # sees the new agent). A snapshot fixed to agent A regardless of what is
    # dispatched would be an additional, unrealistic staleness source,
    # separate from the background path's own provenance-rejection handling.
    current_agent_id = {"id": "ag_agent_a"}

    class _CurrentAgentSnapshotClient(NullServerClient):
        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": current_agent_id["id"]}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=_CurrentAgentSnapshotClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        # Populate _session_spec_cache with agent A's spec, tagged A, and
        # _session_agent_ids[conv] = "ag_agent_a" — the direct-stream
        # opencode cold-boot side effect.
        first = await _post_stream_message(
            http, conv, agent_id="ag_agent_a", harness="opencode-native"
        )
        assert first.status_code == 200

        # A real server updates its own agent_id record before routing a
        # turn to a different agent — reflect that here before dispatching
        # for B, so any snapshot-driven resolution from this point on
        # (e.g. the post-turn continuation task) sees the CURRENT agent,
        # not a frozen "always agent A".
        current_agent_id["id"] = "ag_agent_b"

        # Turn 1: BACKGROUND dispatch for agent B. Provenance is rejected
        # (prior is the KNOWN "ag_agent_a", a real mismatch against B),
        # resolution then fails — must degrade, not use A's spec, for
        # THIS turn.
        bg_first = await http.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_agent_b",
                "model": "x",
                "content": [{"role": "user", "content": "one"}],
            },
        )
        assert bg_first.status_code == 202
        await _await_bg_turn_task(conv)

        # Turn 2: BACKGROUND dispatch for agent B again. Resolution would
        # now succeed (spec_b) if attempted — the cache must not launder
        # the still-resident agent-A entry back in as trusted just because
        # _session_agent_ids now (trivially) matches.
        bg_second = await http.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_agent_b",
                "model": "x",
                "content": [{"role": "user", "content": "two"}],
            },
        )
        assert bg_second.status_code == 202
        await _await_bg_turn_task(conv)

    assert recording_client.posted_bodies, "harness never received a request"
    assert recording_client.posted_bodies[-1]["instructions"] == "Agent B's instructions.", (
        f"Turn 2 dispatched with the wrong instructions — the stale "
        f"agent-A cache entry was laundered back in as trusted; got "
        f"{recording_client.posted_bodies[-1]['instructions']!r}."
    )


@pytest.mark.asyncio
async def test_opencode_cold_boot_uses_current_agent_spec_after_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The opencode-native cold-boot terminal auto-create must resolve the
    CURRENT turn's agent spec, not a stale spec left over from a previous
    agent on the same conversation.

    Cold-boot calls ``_resolve_session_agent_spec(conv_id,
    agent_id_hint=_early_turn_agent_id)`` — passing THIS turn's already-
    known agent id directly, rather than letting the resolver fall back to
    a session-snapshot fetch that can lag an in-flight agent switch.
    ``_resolve_session_spec_entry`` tags whatever it caches with the agent
    id it actually resolved for, and every reader (including cold-boot)
    checks that tag before trusting a hit — so a same-conversation A→B
    switch correctly hands cold-boot agent B's spec on turn 2, not a
    leftover A entry.
    """

    captured_specs: list[Any] = []

    async def _capturing_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args
        captured_specs.append(kwargs.get("agent_spec"))

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _capturing_auto_create_opencode_terminal,
    )

    conv = "conv_opencode_cold_boot_agent_switch"

    specs = {
        "ag_agent_a": AgentSpec(
            spec_version=1,
            name="agent-a",
            instructions="Agent A's instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        ),
        "ag_agent_b": AgentSpec(
            spec_version=1,
            name="agent-b",
            instructions="Agent B's instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        ),
    }

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        return specs[agent_id]

    # cold-boot's _resolve_session_agent_spec derives its agent_id from the
    # SESSION SNAPSHOT (a GET), not from the dispatched turn body — a real
    # server updates conv.agent_id before routing a turn to a different
    # agent, so reflect that here (see the sibling background-dispatch
    # eviction test's identical rationale for a static-mock confound).
    current_agent_id = {"id": "ag_agent_a"}

    class _CurrentAgentSnapshotClient(NullServerClient):
        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": current_agent_id["id"]}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=_CurrentAgentSnapshotClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        # Turn 1: agent A. Cold-boot fires (no terminal registered yet —
        # the fake adapter above never registers one, so cold-boot fires
        # again on every subsequent turn too, matching a resource_registry
        # with no real terminal backing).
        first = await _post_stream_message(
            http, conv, agent_id="ag_agent_a", harness="opencode-native"
        )
        assert first.status_code == 200

        # Turn 2: agent B (switch) — update the "server's" record first.
        current_agent_id["id"] = "ag_agent_b"
        second = await _post_stream_message(
            http, conv, agent_id="ag_agent_b", harness="opencode-native"
        )
        assert second.status_code == 200

    assert [b["instructions"] for b in recording_client.posted_bodies] == [
        "Agent A's instructions.",
        "Agent B's instructions.",
    ], "Wire instructions did not correctly track the agent switch."

    assert len(captured_specs) == 2, (
        f"Expected cold-boot to fire on both turns; got {len(captured_specs)} calls."
    )
    assert captured_specs[0] is not None and captured_specs[0].name == "agent-a"
    assert captured_specs[1] is not None and captured_specs[1].name == "agent-b", (
        f"Turn 2's opencode cold-boot received {getattr(captured_specs[1], 'name', None)!r}'s "
        f"spec instead of agent B's — it read the stale cache before the "
        f"per-turn provenance guard was applied."
    )


@pytest.mark.asyncio
async def test_direct_stream_three_turn_switch_does_not_launder_stale_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A->B->B on the direct-stream path must deliver A, B, B — not A, B, A.

    Cold-boot passes THIS turn's already-known agent id as a hint to
    ``_resolve_session_agent_spec`` instead of falling back to a
    session-snapshot fetch that can lag an in-flight switch, and every
    cache entry it writes is tagged with the agent id it actually
    resolved for — so a same-conversation A→B switch cannot leave a
    same-session-keyed A entry that a later B turn would mistake for its
    own. A THIRD turn (still B) runs specifically because two
    turns (A->B) alone would not surface a laundering bug — it takes a
    subsequent turn for the SAME agent to expose whether an earlier turn's
    cold-boot silently re-cached the wrong identity.
    """

    captured_specs: list[Any] = []

    async def _capturing_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args
        captured_specs.append(kwargs.get("agent_spec"))

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _capturing_auto_create_opencode_terminal,
    )

    conv = "conv_direct_stream_three_turn_switch"

    specs = {
        "ag_agent_a": AgentSpec(
            spec_version=1,
            name="agent-a",
            instructions="Agent A's instructions — must never resurface after turn 2.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        ),
        "ag_agent_b": AgentSpec(
            spec_version=1,
            name="agent-b",
            instructions="Agent B's instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        ),
    }

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        return specs[agent_id]

    # A session-snapshot GET that LAGS one turn behind the actually
    # dispatched agent. That is the hazard under test: cold-boot deriving
    # its own agent identity from a snapshot that has not caught up yet
    # would bind the turn to the PREVIOUS agent. Cold-boot must not consult
    # this snapshot at all — it receives the turn's already-known agent id
    # directly — so the lag here is the hazard itself, not a fixture defect
    # to be kept in sync.
    snapshot_agent_id = {"id": "ag_agent_a"}

    class _LaggingSnapshotClient(NullServerClient):
        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": snapshot_agent_id["id"]}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=_LaggingSnapshotClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        # Turn 1: agent A. snapshot_agent_id deliberately left at "A" (not
        # yet updated) to simulate the server's own record still lagging
        # at the moment this turn is dispatched.
        first = await _post_stream_message(
            http, conv, agent_id="ag_agent_a", harness="opencode-native"
        )
        assert first.status_code == 200

        # Turn 2: agent B (switch). snapshot_agent_id is STILL "A" here —
        # the server hasn't caught up yet either, so if cold-boot consulted
        # the snapshot (instead of the turn's own dispatched agent id) it
        # would resolve A's spec for what is actually B's turn.
        second = await _post_stream_message(
            http, conv, agent_id="ag_agent_b", harness="opencode-native"
        )
        assert second.status_code == 200

        # NOW the snapshot catches up.
        snapshot_agent_id["id"] = "ag_agent_b"

        # Turn 3: agent B again. Must still see B — not a laundered-back-in
        # agent-A entry from turn 2's cold-boot.
        third = await _post_stream_message(
            http, conv, agent_id="ag_agent_b", harness="opencode-native"
        )
        assert third.status_code == 200

    assert [b["instructions"] for b in recording_client.posted_bodies] == [
        "Agent A's instructions — must never resurface after turn 2.",
        "Agent B's instructions.",
        "Agent B's instructions.",
    ], (
        f"Wire instructions must be A, B, B — got "
        f"{[b['instructions'] for b in recording_client.posted_bodies]!r}. A "
        f"third value of 'Agent A's instructions.' means the stale cache "
        f"entry was laundered back in as trusted."
    )
    assert [getattr(s, "name", None) for s in captured_specs] == [
        "agent-a",
        "agent-b",
        "agent-b",
    ], (
        f"Cold-boot must resolve the CURRENT turn's agent on every call — "
        f"got {[getattr(s, 'name', None) for s in captured_specs]!r}."
    )


def test_direct_and_background_switch_sites_share_one_invalidation_routine() -> None:
    """
    Both the direct-stream and background-dispatch agent-switch/
    provenance-reject sites in ``_stream_message_to_harness`` and
    ``_run_turn_bg_setup_and_stream`` must call the SAME shared
    invalidation routine (``_invalidate_session_agent_state``) — not two
    independently-maintained inline cache-pop lists.

    Regression: the direct-stream site used to inline its own narrower
    list (spec + snapshot only), while the background site inlined a
    different, wider list (spec + skills + claude-launch-config + tool
    schemas + snapshot, but still missing the MCP spec hash that
    ``_clear_session_agent_caches`` — used elsewhere, e.g. the explicit
    ``/agent-cache/reset`` route). Three independently
    maintained enumerations of "the agent-derived cache set" is how a gap
    like this recurs: a cache added to one list silently isn't added to
    the others. The check here is structural/source-level rather than
    runtime behavioural because the caches in question — skills, tool
    schemas, MCP spec hash, claude launch config — are all closures local
    to ``create_runner_app``, with no attribute or endpoint exposing them
    for a test to poke or observe directly; asserting the single shared
    call site is what actually guards against the divergence: both
    dispatch paths must route through the one shared invalidation routine
    rather than each maintaining its own eviction list.
    """
    import inspect

    import omnigent.runner.app as runner_app_mod

    source = inspect.getsource(runner_app_mod)
    direct_stream_start = source.index("async def _stream_message_to_harness(")
    direct_stream_body = source[direct_stream_start : direct_stream_start + 4000]
    background_start = source.index("async def _run_turn_bg_setup_and_stream(")
    background_body = source[background_start : background_start + 4000]

    assert "_invalidate_session_agent_state(" in direct_stream_body, (
        "_stream_message_to_harness must call the shared "
        "_invalidate_session_agent_state helper on its switch/provenance-"
        "reject branch, not an inline cache-pop list of its own."
    )
    assert "_invalidate_session_agent_state(" in background_body, (
        "_run_turn_bg_setup_and_stream must call the shared "
        "_invalidate_session_agent_state helper on its switch/provenance-"
        "reject branch, not an inline cache-pop list of its own."
    )


@pytest.mark.asyncio
async def test_direct_stream_composition_uses_parent_and_keeps_caller_instructions(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Direct ``?stream=true`` instruction composition (known-harness branch)
    composes against the PARENT spec, and warns, when the recorded
    ``sub_agent_name`` no longer resolves in the cached spec tree.

    This used to null the spec out and report the turn as indeterminate, so
    composition was skipped entirely and the caller's ``instructions``
    reached the harness verbatim. Composition now runs — but additively:
    the caller's text is combined with the parent's authored instructions,
    never replaced by them. All three halves are asserted (warning emitted,
    parent's authored text present, caller's text still present), so
    dropping any one of them fails this test.

    :param monkeypatch: Pytest monkeypatch, used to stub terminal creation.
    :param caplog: Pytest log capture, used to confirm the unresolved
        sub-agent is reported rather than passed over in silence.
    """

    async def _fake_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _fake_auto_create_opencode_terminal,
    )

    conv = "conv_direct_stream_sub_agent_gone"

    root_spec_with_worker = AgentSpec(
        spec_version=1,
        name="root",
        instructions="Root instructions — composed for this turn.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        sub_agents=[
            AgentSpec(
                spec_version=1,
                name="worker",
                instructions="Worker instructions.",
                executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
            )
        ],
    )
    root_spec_without_worker = AgentSpec(
        spec_version=1,
        name="root",
        instructions="Root instructions — composed for this turn.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
    )

    calls = {"n": 0}

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        calls["n"] += 1
        # First call: session-create resolves the tree WITH "worker" so
        # creation succeeds and caches an entry keyed to the creating agent.
        # Later calls (the direct-stream turn, dispatched as a DIFFERENT
        # agent so the cache-provenance check misses and this resolver is
        # consulted again): the tree has since lost "worker".
        return root_spec_with_worker if calls["n"] == 1 else root_spec_without_worker

    recorder = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recorder)),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_test_client(app) as http:
            create_resp = await http.post(
                "/v1/sessions",
                json={
                    "session_id": conv,
                    "agent_id": "ag_root_creator",
                    "sub_agent_name": "worker",
                },
            )
            assert create_resp.status_code == 201

            # Direct-stream turn dispatched as a DIFFERENT agent id than the
            # session-create call, so the cache-provenance check misses and the
            # composition path falls through to a fresh resolver call (which now
            # returns the tree without "worker"). The recorded sub_agent_name
            # "worker" (session-scoped, not agent-scoped) is still recovered.
            response = await _post_stream_message(
                http,
                conv,
                agent_id="ag_other",
                harness="opencode-native",
                instructions="Caller-supplied instructions must survive.",
            )
            assert response.status_code == 200

    assert "'worker'" in caplog.text and "did not resolve" in caplog.text, (
        f"The unresolved sub-agent must be warned about; got {caplog.text!r}."
    )
    assert recorder.posted_bodies, "harness never received a request"
    _composed = recorder.posted_bodies[-1]["instructions"]
    assert isinstance(_composed, str)
    assert "Caller-supplied instructions must survive." in _composed, (
        f"Caller-supplied instructions must compose additively, not be "
        f"replaced by the parent's authored text; got {_composed!r}."
    )
    assert "Root instructions" in _composed, (
        f"The PARENT's authored instructions must drive composition once the "
        f"sub-agent no longer resolves; got {_composed!r}."
    )


@pytest.mark.asyncio
async def test_no_harness_direct_stream_warns_and_derives_harness_from_parent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    ``_resolve_effective_turn`` (the NO-harness direct-stream path) derives
    harness and spawn_env from the PARENT spec, and warns, when the recorded
    ``sub_agent_name`` no longer resolves in the spec tree.

    This used to raise ``RuntimeError``, which the route answered with a 503
    ``spec_resolver_failed``. Both halves are asserted: the warning must be
    emitted AND the harness the process manager is asked for must be the
    parent's — a silent fallback satisfies neither.

    :param caplog: Pytest log capture, used to confirm the unresolved
        sub-agent is reported rather than passed over in silence.
    """
    # A non-native parent harness: the turn now runs to completion instead of
    # stopping at the unresolved sub-agent, so it reaches cold boot, and a
    # native harness would try to launch a vendor CLI that isn't present.
    # The declared child keeps a DIFFERENT harness, so "the parent's harness
    # drove the turn" is observable rather than a coincidence.
    root_spec_with_worker = AgentSpec(
        spec_version=1,
        name="root",
        instructions="Root instructions.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        sub_agents=[
            AgentSpec(
                spec_version=1,
                name="worker",
                instructions="Worker instructions.",
                executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
            )
        ],
    )
    root_spec_without_worker = AgentSpec(
        spec_version=1,
        name="root",
        instructions="Root instructions.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    calls = {"n": 0}

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        calls["n"] += 1
        return root_spec_with_worker if calls["n"] == 1 else root_spec_without_worker

    conv = "conv_no_harness_direct_stream_sub_agent_gone"
    pm = _FakeProcessManager(_RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS))
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, pm),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_test_client(app) as http:
            create_resp = await http.post(
                "/v1/sessions",
                json={
                    "session_id": conv,
                    "agent_id": "ag_root_creator",
                    "sub_agent_name": "worker",
                },
            )
            assert create_resp.status_code == 201

            # No "harness" field → the resolver branch → _resolve_effective_turn.
            # A different agent id than session-create keeps the cache from
            # being trusted, forcing a fresh resolver call that no longer has
            # "worker" in the tree.
            response = await http.post(
                f"/v1/sessions/{conv}/events?stream=true",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_other",
                    "model": "x",
                    "content": [],
                },
            )

    assert response.status_code == 200, (
        f"An unresolvable sub_agent_name must not fail the turn; got "
        f"{response.status_code} with body {response.text!r}."
    )
    assert "'worker'" in caplog.text and "did not resolve" in caplog.text, (
        f"The unresolved sub-agent must be warned about; got {caplog.text!r}."
    )
    assert "claude-sdk" in [harness for _conv, harness, _env in pm.get_client_calls], (
        f"The PARENT's harness must drive the turn, not the declared child's; "
        f"got {pm.get_client_calls!r}."
    )


@pytest.mark.asyncio
async def test_resolve_session_spec_entry_warns_and_caches_parent_spec_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    ``_resolve_session_spec_entry`` returns and caches the PARENT entry, and
    warns, when the session snapshot's ``sub_agent_name`` no longer resolves
    in the freshly-fetched spec tree. Exercised end-to-end via
    ``GET /v1/sessions/{id}/resources``, one of the callers with no local
    ``except OmnigentError``.

    This used to raise ``NOT_FOUND``, which reached the global
    ``OmnigentError`` handler as a 404 and left the cache empty — nothing
    was published, because the raise happened before the cache write.

    The LATER request is what this asserts. ``caplog`` is cleared after the
    first one, so the fresh resolution's warning cannot stand in for the
    cached read's — reading a combined log would pass even with the cached
    read silent, which is the defect this covers.

    Both cache-hit returns are exercised: the locked one (no
    ``agent_id_hint``, reached through the snapshot branch) and the lock-free
    fast path (``agent_id_hint`` supplied, which nothing else here covers).

    :param caplog: Pytest log capture, read once per request.
    """
    conv = "conv_resolve_spec_entry_sub_agent_gone"

    root_spec = AgentSpec(
        spec_version=1,
        name="root",
        instructions="Root instructions.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        # No sub_agents: the snapshot's "worker" sub_agent_name can't resolve.
    )

    calls = {"n": 0}

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        calls["n"] += 1
        return root_spec

    class _SnapshotWithGoneSubAgent(NullServerClient):
        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": "ag_root", "sub_agent_name": "worker"}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient([])),
        ),
        spec_resolver=_spec_resolver,
        server_client=_SnapshotWithGoneSubAgent(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            response = await http.get(f"/v1/sessions/{conv}/resources")
        assert response.status_code == 200, (
            f"An unresolvable sub_agent_name must not fail the request; got "
            f"{response.status_code} with body {response.text!r}."
        )
        assert "did not resolve" in caplog.text, "the fresh resolution should warn"

        # Everything below is about a request served FROM THE CACHE.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            second = await http.get(f"/v1/sessions/{conv}/resources")
        assert second.status_code == 200
        assert calls["n"] == 1, (
            f"The PARENT entry must be published into the session cache, so "
            f"this request is served from it; spec_resolver ran {calls['n']} "
            f"times."
        )
        assert "'worker'" in caplog.text and "did not resolve" in caplog.text, (
            f"A cached PARENT kept after a miss must be reported on the read "
            f"that returns it, not only on the resolution that produced it. "
            f"Warnings on this request alone: {caplog.text!r}."
        )
        locked_read_warnings = caplog.text

        # The lock-free fast path. ``list_session_resources`` passes
        # ``agent_id_hint=_session_agent_ids.get(session_id)``, which is unset
        # above (no session was created), so the reads so far all took the
        # snapshot branch and the locked return. Creating a session records
        # the agent id, so the read below returns from the hinted hit instead
        # — a different return statement, with its own provenance check.
        created_conv = f"{conv}_created"
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            created = await http.post(
                "/v1/sessions",
                json={
                    "session_id": created_conv,
                    "agent_id": "ag_root",
                    "sub_agent_name": "worker",
                },
            )
        assert created.status_code == 201, created.text

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            hinted = await http.get(f"/v1/sessions/{created_conv}/resources")
        assert hinted.status_code == 200, hinted.text
        assert "'worker'" in caplog.text and "did not resolve" in caplog.text, (
            f"The lock-free cache hit must report the kept parent too; "
            f"warnings on this request alone: {caplog.text!r} (the locked "
            f"read reported {locked_read_warnings!r})."
        )


@pytest.mark.asyncio
async def test_gated_harness_preserves_caller_instructions_on_resolution_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Resolver failure during InstructionComposition must not drop
    caller-supplied ``instructions`` for a gated-delivery harness.

    Regression: harness is already known (independent of the resolver, per
    the ``conv_stream_spec_resolver_failed``-shaped case), so
    ``InstructionComposition``'s own best-effort resolution attempt is the
    only thing that fails here. That failure must degrade gracefully —
    composition/warn-check unknown — and must NOT fall through to the
    harness-conditional wire swap that would otherwise pop/overwrite
    ``body["instructions"]`` with the (unavailable) gated composed value.
    """

    async def _fake_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _fake_auto_create_opencode_terminal,
    )

    conv = "conv_gated_resolution_failure"

    async def _failing_spec_resolver(
        agent_id: str, session_id: str | None = None
    ) -> AgentSpec | None:
        raise RuntimeError(f"instruction composition resolver unavailable for {agent_id}")

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_failing_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        response = await _post_stream_message(
            http,
            conv,
            agent_id="ag_gated_resolution_failure",
            harness="opencode-native",
            instructions="Caller-supplied instructions that must survive.",
        )
        assert response.status_code == 200

    assert recording_client.posted_bodies, "harness never received a request"
    assert (
        recording_client.posted_bodies[0]["instructions"]
        == "Caller-supplied instructions that must survive."
    )


@pytest.mark.asyncio
async def test_gated_harness_preserves_caller_instructions_when_resolver_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A resolver returning ``None`` (a valid "no spec found" result, not a
    raised exception) must ALSO not drop caller-supplied ``instructions``
    for a gated-delivery harness.

    Companion to the RuntimeError-based resolution failure. If only
    a *raised* exception set the preservation flag, a resolver that
    legitimately returns ``None`` would fall straight through to the
    gated-harness wire swap and had its ``composed`` value (necessarily
    unavailable, since there's no spec to compose from) pop/overwrite the
    caller-supplied instructions anyway.
    """

    async def _fake_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _fake_auto_create_opencode_terminal,
    )

    conv = "conv_gated_resolution_none"

    async def _none_returning_spec_resolver(
        agent_id: str, session_id: str | None = None
    ) -> AgentSpec | None:
        del agent_id, session_id
        return None

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_none_returning_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        response = await _post_stream_message(
            http,
            conv,
            agent_id="ag_gated_resolution_none",
            harness="opencode-native",
            instructions="Caller-supplied instructions that must survive a None resolver.",
        )
        assert response.status_code == 200

    assert recording_client.posted_bodies, "harness never received a request"
    assert (
        recording_client.posted_bodies[0]["instructions"]
        == "Caller-supplied instructions that must survive a None resolver."
    )


@pytest.mark.asyncio
async def test_gated_harness_preserves_caller_instructions_with_no_agent_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A gated harness with NO ``agent_id`` on the body (and no cache entry)
    must preserve caller-supplied ``instructions``, not strip them.

    Composition must be marked "unresolved" whenever the spec is genuinely
    unavailable, not only when BOTH an agent id AND a configured resolver
    are present. A missing agent id alone would otherwise leave the flag
    ``False`` and fall straight through to the gated wire-swap, silently
    stripping instructions the caller already supplied.
    The fix removed the separate flag: the swap now gates directly on
    whether a spec was positively resolved, and no agent id means no spec
    was ever attempted.
    """

    async def _fake_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _fake_auto_create_opencode_terminal,
    )

    conv = "conv_gated_no_agent_id"

    async def _unreached_spec_resolver(
        agent_id: str, session_id: str | None = None
    ) -> AgentSpec | None:
        raise AssertionError("resolver must not be called with no agent_id")

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_unreached_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        response = await _post_stream_message(
            http,
            conv,
            # No agent_id at all.
            harness="opencode-native",
            instructions="Caller-supplied instructions that must survive a missing agent_id.",
        )
        assert response.status_code == 200

    assert recording_client.posted_bodies, "harness never received a request"
    assert (
        recording_client.posted_bodies[0]["instructions"]
        == "Caller-supplied instructions that must survive a missing agent_id."
    )


@pytest.mark.asyncio
async def test_gated_harness_preserves_caller_instructions_with_no_resolver_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A gated harness with an ``agent_id`` but NO configured
    ``spec_resolver`` at all (``None``) must also preserve caller-supplied
    ``instructions``, not strip them — the other half of the fall-through
    the old two-condition flag missed (agent id present, resolver absent).
    """

    async def _fake_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _fake_auto_create_opencode_terminal,
    )

    conv = "conv_gated_no_resolver"

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        response = await _post_stream_message(
            http,
            conv,
            agent_id="ag_no_resolver_configured",
            harness="opencode-native",
            instructions="Caller-supplied instructions that must survive no configured resolver.",
        )
        assert response.status_code == 200

    assert recording_client.posted_bodies, "harness never received a request"
    assert (
        recording_client.posted_bodies[0]["instructions"]
        == "Caller-supplied instructions that must survive no configured resolver."
    )


@pytest.mark.asyncio
async def test_instruction_delivery_warn_does_not_double_fire_after_agent_switch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    An agent switch that does not change the delivery value must not re-warn.

    The dedup is scoped to
    ``(conversation, harness, delivery value)``. BOTH agents here
    resolve to the same harness (``kimi``), so the effective value is unchanged
    across the switch and the conversation must stay at exactly one warning —
    re-warning would report the same underlying gap twice.

    The dedup marker is therefore popped only at ``delete_session``, never by
    the in-conversation "agent switch detected" cache-clear block
    (``_session_spec_cache.pop`` et al. in ``_run_turn_bg_setup_and_stream``);
    clearing it there would collapse the contract back to per-switch
    behaviour.

    A presence-only ``set[str]`` marker also emits exactly one warning for
    a same-delivery switch, so this scenario does not constrain the shape of
    the key. What does constrain it is
    ``test_instruction_delivery_warn_refires_when_the_switch_changes_delivery``
    (a changed value must warn again) and
    ``test_instruction_delivery_warn_does_not_refire_for_a_recurring_value``
    (A -> B -> A must not re-warn for A).
    """
    conv = "conv_instruction_warn_agent_switch"

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        return AgentSpec(
            spec_version=1,
            name=f"kimi-agent-{agent_id}",
            instructions="Be a helpful coding assistant.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "kimi"}),
        )

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient(_INSTRUCTION_WARN_CHUNKS)),
        ),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async def _post_bg(agent_id: str) -> None:
        async with _runner_test_client(app) as http:
            resp = await http.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": agent_id,
                    "model": "x",
                    "content": [{"role": "user", "content": "hi"}],
                },
            )
            assert resp.status_code == 202
            await _await_bg_turn_task(conv)

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _post_bg("ag_kimi_a")
        # Different agent_id on the second turn triggers the
        # agent-switch-detected cache-clear block.
        await _post_bg("ag_kimi_b")

    warnings = [
        rec for rec in caplog.records if "instructions are not delivered" in rec.getMessage()
    ]
    assert len(warnings) == 1, (
        f"expected exactly one warning across an agent switch, got {warnings}"
    )


@pytest.mark.asyncio
async def test_hermes_wire_instructions_use_gated_composed_value() -> None:
    """hermes gets the gated composed value WITH per-request text in it.

    Mirrors the opencode-native case: hermes's executor reads the wire
    ``instructions`` field via the standard ``system_prompt`` pipeline, so
    the runner's harness-conditional overwrite must swap in a composed value
    that already includes this turn's per-request text — discarding the
    caller's value would drop genuine per-request instructions.
    """
    conv = "conv_hermes_gated_wire"

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="hermes-agent",
            instructions="Be a concise assistant.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        )

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        response = await _post_stream_message(
            http,
            conv,
            agent_id="ag_hermes",
            instructions="Answer only in French.",
        )
        assert response.status_code == 200

    assert recording_client.posted_bodies, "harness never received a request"
    assert recording_client.posted_bodies[0]["instructions"] == (
        "Be a concise assistant.\n\nAnswer only in French."
    ), (
        f"hermes must receive the agent's text AND this turn's per-request "
        f"text — got {recording_client.posted_bodies[0].get('instructions')!r}."
    )


def test_build_spawn_env_applies_model_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-session ``/model`` override overrides ``HARNESS_<H>_MODEL``.

    Regression: ``/model`` was recorded in the session readout but the turn
    still used the provider/catalog default, because the SDK harnesses take
    their model from the spawn-env env var (which only the native CLIs'
    ``--model`` path honored). The override must override the baked-in
    ``HARNESS_CLAUDE_SDK_MODEL`` so the switch actually takes effect.

    :param tmp_path: Pytest temp dir for an isolated provider config.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    (tmp_path / "config.yaml").write_text(
        "providers:\n"
        "  anthropic:\n"
        "    kind: key\n"
        "    default: true\n"
        "    anthropic:\n"
        "      base_url: https://api.anthropic.com\n"
        "      api_key: $ANTHROPIC_API_KEY\n"
        "      models:\n"
        "        default: test-default\n"
    )
    spec = AgentSpec(
        spec_version=1,
        name="x",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    base = _build_spawn_env_from_spec(spec, "claude-sdk")
    overridden = _build_spawn_env_from_spec(spec, "claude-sdk", model_override="claude-sonnet-4-6")
    assert base is not None and overridden is not None
    # Baseline uses the provider/catalog default (not the override) …
    assert base["HARNESS_CLAUDE_SDK_MODEL"] != "claude-sonnet-4-6"
    # … and the override wins, landing in the model env var the SDK reads.
    assert overridden["HARNESS_CLAUDE_SDK_MODEL"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_direct_stream_genuine_harness_swap_rebuilds_spawn_env_for_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A GENUINE harness swap (different canonical harnesses) must reach the
    process manager with spawn_env actually rebuilt for the NEW harness —
    not merely warn correctly or fall back to None. Asserts the real env
    dict ``process_manager.get_client`` was called with.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")

    conv = "conv_genuine_harness_swap_spawn_env"

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="swap-agent",
            executor=ExecutorSpec(
                type="omnigent",
                config={"harness": "claude-sdk"},
                model="gpt-5.4",
            ),
        )

    fake_pm = _FakeProcessManager(_FakeHarnessClient(_INSTRUCTION_WARN_CHUNKS))
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, fake_pm),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        response = await _post_stream_message(
            http,
            conv,
            agent_id="ag_genuine_swap",
            # harness (claude-sdk) and harness_override (codex) are
            # genuinely DIFFERENT canonical harnesses — a real swap.
            harness="claude-sdk",
            harness_override="codex",
            # Stale spawn_env shaped for the ORIGINAL (claude-sdk) harness —
            # must not reach the process for the overridden (codex) one.
            spawn_env={"HARNESS_CLAUDE_SDK_MODEL": "stale-claude-model"},
        )
        assert response.status_code == 200

    assert fake_pm.get_client_calls, "process manager get_client was never called"
    _conv_id, _harness_name, env = fake_pm.get_client_calls[-1]
    assert _harness_name == "codex"
    assert env is not None
    assert "HARNESS_CLAUDE_SDK_MODEL" not in env, (
        f"stale claude-sdk spawn_env leaked into the codex process: {env!r}"
    )
    assert env.get("HARNESS_CODEX_MODEL") == "gpt-5.4"


@pytest.mark.asyncio
async def test_direct_stream_alias_only_override_does_not_clear_spawn_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``harness_override`` that's merely an ALIAS of the same canonical
    harness (e.g. harness="opencode-native", harness_override="opencode")
    is NOT an actual swap and must not clear/rebuild a valid caller-supplied
    ``spawn_env`` — a raw-string compare would falsely register this as a
    swap (the two spellings differ as strings) and discard it for no reason.
    """

    async def _fake_auto_create_opencode_terminal(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_opencode_terminal",
        _fake_auto_create_opencode_terminal,
    )

    conv = "conv_alias_only_override_spawn_env"

    fake_pm = _FakeProcessManager(_FakeHarnessClient(_INSTRUCTION_WARN_CHUNKS))
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, fake_pm),
        spec_resolver=None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        response = await _post_stream_message(
            http,
            conv,
            agent_id="ag_alias_only_override",
            harness="opencode-native",
            # Alias of the SAME canonical harness — not a real swap.
            harness_override="opencode",
            spawn_env={"OPENCODE_CALLER_SUPPLIED": "must-survive"},
        )
        assert response.status_code == 200

    assert fake_pm.get_client_calls, "process manager get_client was never called"
    _conv_id, _harness_name, env = fake_pm.get_client_calls[-1]
    assert _harness_name == "opencode-native"
    assert env == {"OPENCODE_CALLER_SUPPLIED": "must-survive"}, (
        f"alias-only override incorrectly cleared/rebuilt spawn_env: {env!r}"
    )


@pytest.mark.asyncio
async def test_resolve_harness_config_applies_harness_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-session ``harness_override`` replaces the spec's brain harness.

    The web UI's new-chat picker persists the override on the session and
    the server forwards it in the message body; the runner must spawn THAT
    harness (with its spawn-env shape), not the spec's declared one — else
    the snapshot would claim "pi" while claude-sdk actually runs.

    :param tmp_path: Pytest temp dir for an isolated provider config.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    (tmp_path / "config.yaml").write_text(
        "providers:\n"
        "  anthropic:\n"
        "    kind: key\n"
        "    default: true\n"
        "    anthropic:\n"
        "      base_url: https://api.anthropic.com\n"
        "      api_key: $ANTHROPIC_API_KEY\n"
        "      models:\n"
        "        default: test-default\n"
    )
    spec = AgentSpec(
        spec_version=1,
        name="x",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    async def _resolver(_agent_id: str, _session_id: str | None) -> AgentSpec:
        return spec

    # Baseline: no override resolves the spec's declared harness.
    harness, spawn_env = await _resolve_harness_config(
        agent_id="ag_x", spec_resolver=_resolver, session_id="conv_x"
    )
    assert harness == "claude-sdk"
    assert spawn_env is not None and "HARNESS_CLAUDE_SDK_MODEL" in spawn_env

    # Override: the harness AND its spawn-env shape follow the override.
    harness, spawn_env = await _resolve_harness_config(
        agent_id="ag_x",
        spec_resolver=_resolver,
        session_id="conv_x",
        harness_override="pi",
    )
    assert harness == "pi", (
        f"harness_override='pi' resolved to {harness!r} — the override "
        f"was ignored and the spec's declared harness won."
    )
    # The spawn env must be built FOR the overridden harness (pi env keys),
    # not the spec's claude-sdk shape — a claude env here means the harness
    # name and env were resolved inconsistently.
    assert spawn_env is not None and "HARNESS_PI_MODEL" in spawn_env, (
        f"Expected a pi spawn-env; got keys {sorted(spawn_env or {})!r}"
    )


@pytest.mark.asyncio
async def test_runner_background_turn_emits_failed_when_spawn_env_build_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn-env build failure must end the turn, never hang on "running".

    Regression for the silent-hang failure mode: when
    ``_build_claude_sdk_spawn_env`` raises (e.g. a generic provider routed
    to the claude-sdk harness has no resolvable model, raising
    ``OmnigentError``), the setup phase of ``_run_turn_bg`` failed before
    the streaming block's own error handling. Because the background turn
    task's only done-callback was ``_background_tasks.discard``, the
    exception was swallowed: ``_active_turns`` stayed set and no terminal
    ``session.status`` event was published, so the REPL spun on "working"
    forever with no output.

    The fix wraps the setup phase so any pre-stream exception routes through
    ``_on_proxy_stream_end``, which clears the active turn and publishes
    ``session.status: failed``. The background-turn path is driven
    (no ``?stream=true`` — the production path the Omnigent server uses) and
    asserts the ``failed`` status reaches the session SSE stream.

    :param monkeypatch: pytest fixture used to force the spawn-env build to
        raise the same error class the no-model provider path produces.
    """
    from omnigent.errors import ErrorCode, OmnigentError

    conv = "conv_spawn_env_build_raises"

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return a claude-sdk spec so the runner takes the claude-sdk
        spawn-env builder path.

        :param agent_id: Agent id requested by the runner (unused).
        :param session_id: Session id (unused).
        :returns: A minimal claude-sdk spec.
        """
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="claude-sdk-agent",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        )

    def _raising_build(
        spec: object, *, cwd: object = None, workdir: object = None
    ) -> dict[str, str]:
        """
        Stand in for ``_build_claude_sdk_spawn_env`` and fail the way the
        no-model generic-provider path does.

        :param spec: The agent spec (unused).
        :param workdir: Bundle workdir (unused).
        :returns: Never returns.
        :raises OmnigentError: Always — mirrors the no-model provider error.
        """
        del spec, workdir
        raise OmnigentError(
            "No model resolved for the 'claude-sdk' harness on a generic provider.",
            code=ErrorCode.INVALID_INPUT,
        )

    # ``_build_spawn_env_from_spec`` imports this from workflow at call time,
    # so patching the workflow attribute reaches the runner's call site.
    monkeypatch.setattr(
        "omnigent.runtime.workflow._build_claude_sdk_spawn_env",
        _raising_build,
    )

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient([])),
        ),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_test_client(app) as http:
        response = await http.post(
            # No ``?stream=true`` → background turn (the production path the
            # Omnigent server uses to forward session messages).
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_claude_sdk",
                "model": "x",
                "content": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 202
        await _await_bg_turn_task(conv)
        statuses = await _drain_published_statuses(conv, until="failed", timeout=2.0)

    # The turn published "running" then "failed" — it reached a terminal
    # state and cleared. Without the fix, the setup-phase OmnigentError is
    # swallowed: only "running" is published, ``_active_turns`` stays set, and
    # the session hangs on "working" forever (the silent-hang regression).
    assert statuses == ["running", "failed"]


@pytest.mark.asyncio
async def test_runner_failed_status_carries_setup_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SETUP-phase failure forwards its error message on the ``failed`` event.

    Regression for the silent-REPL failure mode: ending the turn on a
    pre-stream error stopped the spinner but rendered no text, because the
    runner published a bare ``session.status: failed`` with no error
    detail — the message never left the runner. This drives the same
    spawn-env-build failure as
    :func:`test_runner_background_turn_emits_failed_when_spawn_env_build_raises`
    and asserts the published ``failed`` event now carries the normalized
    ``{code, message}`` error so Omnigent and the REPL can render it.

    :param monkeypatch: pytest fixture used to force the spawn-env build to
        raise the no-model provider error.
    """
    from omnigent.errors import ErrorCode, OmnigentError

    conv = "conv_failed_status_carries_error"
    raised_message = "No model resolved for the 'claude-sdk' harness on a generic provider."

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return a claude-sdk spec so the spawn-env builder path is taken.

        :param agent_id: Agent id (unused).
        :param session_id: Session id (unused).
        :returns: A minimal claude-sdk spec.
        """
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="claude-sdk-agent",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        )

    def _raising_build(
        spec: object, *, cwd: object = None, workdir: object = None
    ) -> dict[str, str]:
        """
        Fail the spawn-env build the way the no-model provider path does.

        :param spec: The agent spec (unused).
        :param workdir: Bundle workdir (unused).
        :returns: Never returns.
        :raises OmnigentError: Always.
        """
        del spec, workdir
        raise OmnigentError(raised_message, code=ErrorCode.INVALID_INPUT)

    monkeypatch.setattr(
        "omnigent.runtime.workflow._build_claude_sdk_spawn_env",
        _raising_build,
    )

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient([])),
        ),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_test_client(app) as http:
        response = await http.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_claude_sdk",
                "model": "x",
                "content": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 202
        await _await_bg_turn_task(conv)
        failed_event = await _drain_failed_status_event(conv, timeout=2.0)

    # The failed event must carry the real setup error message — not a
    # bare status. Without the fix ``error`` is absent and the REPL
    # renders nothing.
    assert failed_event is not None
    error = failed_event.get("error")
    assert isinstance(error, dict)
    # The raised OmnigentError message is wrapped as "turn setup
    # failed: <message>" by _run_turn_bg's setup-phase handler.
    assert raised_message in error["message"]
    # Normalized shape always has a code so the wire ErrorDetail validates.
    assert error["code"]


# ── Harness-stream failure → terminal session.status ────


async def _drain_status_events(
    conv: str,
    *,
    until: str,
    timeout: float,
) -> list[dict[str, Any]]:
    """Collect full ``session.status`` events a runner published for a session.

    Like :func:`_drain_published_statuses` but returns the full event dicts,
    so one drain can assert both the status order and the carried ``error``
    payload — the queue is consumed by reading, so a test cannot drain twice.

    :param conv: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :param until: Stop once this ``session.status`` value is observed,
        e.g. ``"failed"``.
    :param timeout: Hard cap in seconds — if *until* never arrives the poll
        gives up and returns what it saw, so a hang regression fails the
        assertion instead of spinning forever.
    :returns: Ordered ``session.status`` event dicts published for *conv*.
    """
    from omnigent.runner.app import _session_event_queues_ref

    events: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        queue = _session_event_queues_ref.get(conv)
        drained = False
        while queue is not None and not queue.empty():
            event = queue.get_nowait()
            drained = True
            if isinstance(event, dict) and event.get("type") == "session.status":
                events.append(event)
        if any(event.get("status") == until for event in events):
            return events
        if not drained:
            # Let the background turn task make progress before re-polling.
            await asyncio.sleep(0.02)
    return events


_STREAM_FAILURE_MESSAGE = "harness turn failed: executor stream disconnected"

_SSE_RESPONSE_CREATED = (
    'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_sf_1"}}\n\n'
)
_SSE_RESPONSE_FAILED = (
    "event: response.failed\ndata: "
    '{"type":"response.failed","response":{"status":"failed"},'
    f'"error":{{"message":"{_STREAM_FAILURE_MESSAGE}","code":"executor_error"}}}}\n\n'
)
_SSE_RESPONSE_COMPLETED = (
    "event: response.completed\ndata: "
    '{"type":"response.completed","response":{"id":"resp_sf_1","status":"completed"}}\n\n'
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "frames", "until", "expected_statuses"),
    [
        pytest.param(
            "codex-native",
            [_SSE_RESPONSE_CREATED, _SSE_RESPONSE_FAILED],
            "failed",
            ["running", "failed"],
            id="codex-native-failed-stream",
        ),
        pytest.param(
            _TEST_HARNESS_NAME,
            [_SSE_RESPONSE_CREATED, _SSE_RESPONSE_FAILED],
            "failed",
            ["running", "failed"],
            id="subprocess-harness-failed-stream",
        ),
        pytest.param(
            _TEST_HARNESS_NAME,
            [_SSE_RESPONSE_CREATED, _SSE_RESPONSE_FAILED, _SSE_RESPONSE_COMPLETED],
            "idle",
            ["running", "idle"],
            id="failure-superseded-by-completion",
        ),
    ],
)
async def test_runner_publishes_terminal_failed_when_harness_stream_fails(
    harness: str,
    frames: list[str],
    until: str,
    expected_statuses: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A harness stream that ends after ``response.failed`` publishes ``failed``.

    Regression for the stuck working indicator in codex-native sessions:
    when a turn failed inside the harness, the scaffold emitted
    ``response.failed`` on the SSE stream, but the runner's proxy relay
    called ``_on_proxy_stream_end`` with no error, so the turn ended with
    ``session.status: idle``. For codex-native sessions ``idle`` is
    suppressed (the Codex app-server forwarder owns that edge — and posts
    nothing when Codex never started a turn), so NO terminal status was
    published at all: the web UI showed the error block yet spun on
    "working" forever. For subprocess harnesses the terminal edge was
    published with the wrong value (``idle`` instead of ``failed``).

    A ``response.completed`` after an earlier in-stream ``response.failed``
    supersedes the failure: the turn ended successfully, so the terminal
    edge must be ``idle``.

    :param harness: Harness name baked into the resolved spec, driving
        the runner's native-vs-subprocess status-edge policy.
    :param frames: Scripted harness SSE frames for the turn stream.
    :param until: Terminal ``session.status`` value the drain waits for.
    :param expected_statuses: Exact ordered ``session.status`` values the
        runner must publish for the turn.
    :param monkeypatch: Pytest fixture, used to isolate the codex-native
        bridge directory.
    :param tmp_path: Pytest temp dir receiving the isolated bridge files.
    """
    conv = f"conv_stream_failed_{until}_{harness.replace('-', '_')}"
    # Keep the codex-native pre-turn bridge writes (write_mcp_bridge_config)
    # out of the real ``~/.omnigent/codex-native`` tree. The module documents
    # this monkeypatch as the supported test isolation point.
    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", tmp_path)

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Return a spec pinned to the parametrized harness.

        :param agent_id: Agent id requested by the runner (unused).
        :param session_id: Session id (unused).
        :returns: A minimal spec for the parametrized harness.
        """
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="stream-fail-agent",
            executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
        )

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient(frames)),
        ),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_test_client(app) as http:
        response = await http.post(
            # No ``?stream=true`` → background turn (the production path the
            # Omnigent server uses to forward session messages).
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_stream_fail",
                "model": "x",
                "content": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 202
        # Await the background turn task directly so we know it has completed
        # (and published its terminal status) before draining — the same race
        # guard the sibling failed-status tests use.
        await _await_bg_turn_task(conv)
        events = await _drain_status_events(conv, until=until, timeout=2.0)

    statuses = [event.get("status") for event in events]
    # The turn must reach the parametrized terminal state. Without the fix,
    # the failed-stream cases regress in two distinct ways: codex-native
    # publishes only ["running"] (idle is suppressed, so the drain times out
    # and the working indicator hangs), and the subprocess harness publishes
    # ["running", "idle"] (the wrong terminal edge).
    assert statuses == expected_statuses, (
        f"Expected session.status sequence {expected_statuses}, got {statuses}. "
        f"A missing terminal value means the turn never cleared (stuck working "
        f"indicator); 'idle' in place of 'failed' means the in-stream "
        f"response.failed was dropped at stream end."
    )
    if until == "failed":
        error = events[-1].get("error")
        # The terminal failed edge must carry the harness's real error so
        # clients can render it — a bare ``failed`` with no payload would
        # clear the spinner but tell the user nothing.
        assert isinstance(error, dict)
        assert _STREAM_FAILURE_MESSAGE in error["message"]


# ── Runner-local OS env dispatch ────────────────────────


@pytest.mark.asyncio
async def test_runner_os_env_tools_use_agent_spec_cwd() -> None:
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.runner.tool_dispatch import _execute_os_env_tool
    from omnigent.spec.types import AgentSpec

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        spec = AgentSpec(
            spec_version=1,
            os_env=OSEnvSpec(
                type="caller_process",
                cwd=str(root),
                sandbox=OSEnvSandboxSpec(type="none"),
            ),
        )

        write = await _execute_os_env_tool(
            "sys_os_write",
            {"path": "note.txt", "content": "hello\nplanet\n"},
            agent_spec=spec,
            conversation_id="conv_runner_os_env_test",
        )
        assert json.loads(write)["created"] is True
        assert root.joinpath("note.txt").read_text() == "hello\nplanet\n"

        edit = await _execute_os_env_tool(
            "sys_os_edit",
            {"path": "note.txt", "oldText": "planet", "newText": "world"},
            agent_spec=spec,
            conversation_id="conv_runner_os_env_test",
        )
        assert json.loads(edit)["replacements"] == 1

        read = await _execute_os_env_tool(
            "sys_os_read",
            {"path": "note.txt"},
            agent_spec=spec,
            conversation_id="conv_runner_os_env_test",
        )
        assert json.loads(read)["content"] == "hello\nworld\n"

        shell = await _execute_os_env_tool(
            "sys_os_shell",
            {"command": "pwd"},
            agent_spec=spec,
            conversation_id="conv_runner_os_env_test",
        )
        shell_result = json.loads(shell)
        assert shell_result["exit_code"] == 0
        assert Path(shell_result["stdout"].strip()).resolve() == root.resolve()


@pytest.mark.asyncio
async def test_runner_os_env_placeholder_cwd_uses_cli_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Runner-local OS tools map ``cwd: .`` to the CLI workspace.

    Remote ``run --server`` uploads the spec to an app server,
    but the local runner still owns filesystem access. A placeholder
    cwd resolves to the local
    project root the CLI passed into the runner, not to the
    runner-owned temp fallback.

    :param monkeypatch: Pytest environment patch fixture.
    :param tmp_path: Per-test temp root for workspace and fallback paths.
    :returns: None.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.runner.tool_dispatch import _execute_os_env_tool

    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", str(tmp_path / "fallback"))
    spec = AgentSpec(
        spec_version=1,
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=".",
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )

    out = await _execute_os_env_tool(
        "sys_os_write",
        {"path": "created.txt", "content": "from workspace"},
        agent_spec=spec,
        conversation_id="conv_runner_workspace",
        runner_workspace=workspace.resolve(),
    )

    assert json.loads(out)["created"] is True
    assert workspace.joinpath("created.txt").read_text() == "from workspace"
    assert not (tmp_path / "fallback").exists()


@pytest.mark.asyncio
async def test_runner_os_env_tools_default_to_conversation_workspace(monkeypatch) -> None:
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.runner.tool_dispatch import _execute_os_env_tool
    from omnigent.spec.types import AgentSpec

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", td)
        spec = AgentSpec(
            spec_version=1,
            os_env=OSEnvSpec(
                type="caller_process",
                sandbox=OSEnvSandboxSpec(type="none"),
            ),
        )

        out = await _execute_os_env_tool(
            "sys_os_write",
            {"path": "created.txt", "content": "hi"},
            agent_spec=spec,
            conversation_id="conv/default workspace",
        )
        assert json.loads(out)["created"] is True
        assert Path(td, "conv_default_workspace", "workspace", "created.txt").read_text() == "hi"


def test_clone_os_env_spec_preserves_all_sandbox_fields() -> None:
    """Cloning an OSEnvSpec must preserve every sandbox field.

    Same class of bug as :func:`omnigent.inner.terminal._clone_sandbox_spec`
    guards against: hand-enumerated
    field copies silently drop security-critical fields (egress_rules,
    egress_allow_private_destinations, env_passthrough, etc.) when new
    fields are added to :class:`OSEnvSandboxSpec`. Asserts the
    runner-side clone is field-complete by comparing the dataclass dicts
    and verifying list-typed fields are not aliased with the original.

    :returns: None.
    """
    import dataclasses

    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.runner.tool_dispatch import _clone_os_env_spec

    sandbox = OSEnvSandboxSpec(
        type="darwin_seatbelt",
        read_paths=["~/.examplecfg"],
        write_paths=["."],
        write_files=["~/.ssh/known_hosts"],
        allow_network=True,
        cwd_allow_hidden=[".git", ".venv"],
        cwd_hidden_scan_max_entries=12345,
        cwd_hidden_scan_overflow="warn",
        env_passthrough=["EXAMPLE_HOST", "EXAMPLE_VALUE"],
        egress_rules=["GET api.github.com/repos/databricks/*/**"],
        egress_allow_private_destinations=True,
    )
    spec = OSEnvSpec(
        type="caller_process",
        cwd="/tmp/work",
        sandbox=sandbox,
        fork=True,
        start_in_scratch=True,
    )

    clone = _clone_os_env_spec(spec)

    # Every OSEnvSpec field round-trips.
    assert dataclasses.asdict(clone) == dataclasses.asdict(spec), (
        "clone must preserve every OSEnvSpec / OSEnvSandboxSpec field; "
        "hand-enumerated copies silently drop newly-added fields."
    )
    # Mutable list fields are copied, not aliased.
    assert clone.sandbox is not sandbox
    for name in (
        "read_paths",
        "write_paths",
        "write_files",
        "cwd_allow_hidden",
        "env_passthrough",
        "egress_rules",
    ):
        original_list = getattr(sandbox, name)
        cloned_list = getattr(clone.sandbox, name)
        assert cloned_list == original_list
        assert cloned_list is not original_list, (
            f"{name} must be a new list so later mutation of the clone "
            "does not leak into the original spec."
        )


def test_effective_runner_os_env_defaults_when_spec_has_no_os_env(monkeypatch) -> None:
    """Agent specs without ``os_env`` get a runner-owned workspace cwd.

    :param monkeypatch: Pytest environment patch fixture.
    :returns: None.
    """
    from omnigent.runner.tool_dispatch import _effective_runner_os_env_spec
    from omnigent.spec.types import AgentSpec

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", td)
        spec = AgentSpec(spec_version=1)

        os_env = _effective_runner_os_env_spec(spec, "conv/no os env")

        assert os_env.type == "caller_process"
        assert Path(os_env.cwd) == Path(td, "conv_no_os_env", "workspace")
        assert Path(os_env.cwd).is_dir()


def test_effective_runner_os_env_uses_cli_workspace_when_spec_has_no_os_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Agent specs without ``os_env`` use the CLI workspace when available.

    :param monkeypatch: Pytest environment patch fixture.
    :param tmp_path: Per-test temp root for workspace and fallback paths.
    :returns: None.
    """
    from omnigent.runner.tool_dispatch import _effective_runner_os_env_spec
    from omnigent.spec.types import AgentSpec

    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", str(tmp_path / "fallback"))
    spec = AgentSpec(spec_version=1)

    os_env = _effective_runner_os_env_spec(
        spec,
        "conv/no os env",
        runner_workspace=workspace.resolve(),
    )

    assert os_env.type == "caller_process"
    assert Path(os_env.cwd) == workspace.resolve()
    assert not (tmp_path / "fallback").exists()


def test_effective_runner_os_env_runner_workspace_overrides_absolute_spec_cwd(
    tmp_path: Path,
) -> None:
    """
    runner_workspace wins over an absolute ``os_env.cwd`` in the spec.

    Per designs/SESSION_WORKSPACE_SELECTION.md "How this maps onto
    runtime": absolute spec cwds are session-create-time
    boundaries, not runtime overrides. When the runner is launched
    with ``OMNIGENT_RUNNER_WORKSPACE`` set, it always wins —
    otherwise picking ``~/universe/src/foo`` for an agent
    declaring ``cwd: ~/universe`` would silently relocate up to
    ``~/universe`` at runtime.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.runner.tool_dispatch import _effective_runner_os_env_spec
    from omnigent.spec.types import AgentSpec

    workspace = tmp_path / "picked-subdir"
    workspace.mkdir()
    spec_cwd = tmp_path / "agent-spec-cwd"
    spec_cwd.mkdir()
    spec = AgentSpec(
        spec_version=1,
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(spec_cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )

    os_env = _effective_runner_os_env_spec(
        spec,
        "conv_abs_override",
        runner_workspace=workspace.resolve(),
    )

    # Workspace wins over the absolute spec cwd.
    assert Path(os_env.cwd) == workspace.resolve()
    assert Path(os_env.cwd) != spec_cwd.resolve()


def test_effective_runner_os_env_absolute_spec_cwd_used_without_runner_workspace(
    tmp_path: Path,
) -> None:
    """
    Without runner_workspace, an absolute ``os_env.cwd`` in the
    spec is used as-is.

    The no-env-var fallback path, so unit runs / pure local
    runs that construct an agent spec directly without the env
    var continue to honor whatever the spec declared.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.runner.tool_dispatch import _effective_runner_os_env_spec
    from omnigent.spec.types import AgentSpec

    spec_cwd = tmp_path / "agent-spec-cwd"
    spec_cwd.mkdir()
    spec = AgentSpec(
        spec_version=1,
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(spec_cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )

    os_env = _effective_runner_os_env_spec(
        spec,
        "conv_no_workspace_abs",
        runner_workspace=None,
    )

    assert Path(os_env.cwd) == spec_cwd


@pytest.mark.asyncio
async def test_runner_terminal_dispatch_passes_cli_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Runner terminal tools receive the CLI workspace in ``ToolContext``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Workspace path exported to the runner.
    :returns: None.
    """
    from omnigent.inner.datamodel import TerminalEnvSpec
    from omnigent.runner.tool_dispatch import _execute_terminal_tool
    from omnigent.terminals import TerminalRegistry
    from omnigent.tools.base import ToolContext
    from omnigent.tools.builtins.sys_terminal import SysTerminalLaunchTool

    workspace = tmp_path / "project"
    workspace.mkdir()
    spec = AgentSpec(
        spec_version=1,
        terminals={"zsh": TerminalEnvSpec(command="zsh")},
    )
    captured: dict[str, object] = {}

    def _fake_invoke(
        self: SysTerminalLaunchTool,
        arguments: str,
        ctx: ToolContext,
    ) -> str:
        """
        Capture the runner-built context without launching tmux.

        :param self: Bound launch tool instance.
        :param arguments: JSON arguments forwarded by dispatch.
        :param ctx: Tool context created by runner dispatch.
        :returns: JSON status payload.
        """
        del self
        captured["arguments"] = arguments
        captured["workspace"] = ctx.workspace
        return json.dumps({"status": "captured"})

    monkeypatch.setattr(SysTerminalLaunchTool, "invoke", _fake_invoke)

    out = await _execute_terminal_tool(
        "sys_terminal_launch",
        {"terminal": "zsh", "session": "s1"},
        terminal_registry=TerminalRegistry(),
        agent_spec=spec,
        conversation_id="conv_terminal_dispatch",
        task_id="task_terminal_dispatch",
        agent_id="ag_terminal_dispatch",
        runner_workspace=workspace.resolve(),
    )

    assert json.loads(out)["status"] == "captured"
    assert json.loads(captured["arguments"]) == {"terminal": "zsh", "session": "s1"}
    assert captured["workspace"] == workspace.resolve()


class _StubTerminalInstance:
    """Minimal stand-in for a launched ``TerminalInstance``.

    ``terminal_resource_view`` reads only stable ``TerminalInstance``
    fields; the launch/close tool's ``invoke`` is monkeypatched in
    here so no real tmux instance is created.

    :param running: Value surfaced as ``metadata.running`` in the
        resource view.
    """

    def __init__(self, running: bool = True) -> None:
        self.running = running
        # ``None`` => the view uses the default environment id, so we
        # don't need to fabricate an OSEnvironment.
        self.os_env = None
        self.socket_path = Path("/tmp/omnigent-test-tmux.sock")
        self.tmux_target = "main"
        # ``terminal_resource_view`` reads this to project the effective
        # web-attach transport into metadata; ``None`` => the global default.
        self.terminal_transport = None
        # Records on_activity callbacks the dispatch wires up so a fresh
        # launch's pane-activity watcher start is observable (and so the
        # call doesn't AttributeError against this stub).
        self.activity_watchers: list[Callable[[], None]] = []

    def start_idle_watcher_thread(
        self,
        on_idle: Callable[[], None] | None = None,
        *,
        on_activity: Callable[[], None] | None = None,
        **kwargs: object,
    ) -> None:
        """Record the activity callback instead of polling real tmux."""
        del kwargs
        if on_activity is not None:
            self.activity_watchers.append(on_activity)


class _StubTerminalRegistry:
    """Registry stub whose ``get`` returns a fixed instance.

    The launch/close tool's ``invoke`` is monkeypatched, so the tool
    never touches the registry; only ``_emit_terminal_resource_event``
    calls ``get`` (on the fresh-launch branch) to build the resource
    view. A real stub class (not ``MagicMock``) so an unexpected extra
    call surfaces as a recorded entry rather than silently passing.

    :param instance: Instance returned for every ``get``, or ``None``
        to simulate a registry miss.
    """

    def __init__(self, instance: _StubTerminalInstance | None) -> None:
        self._instance = instance
        self.get_calls: list[tuple[str, str, str]] = []

    def get(
        self,
        conversation_id: str,
        terminal_name: str,
        session_key: str,
    ) -> _StubTerminalInstance | None:
        """Record the lookup and return the configured instance."""
        self.get_calls.append((conversation_id, terminal_name, session_key))
        return self._instance


def _capturing_publish_event(
    captured: list[dict[str, Any]],
) -> Any:
    """Build a ``publish_event`` stub that records published events.

    :param captured: List the returned callable appends each event
        dict to (the session id is discarded — every event in these
        tests targets the same conversation).
    :returns: A ``(session_id, event) -> None`` callable.
    """

    def _publish(session_id: str, event: dict[str, Any]) -> None:
        del session_id
        captured.append(event)

    return _publish


@pytest.mark.asyncio
async def test_terminal_launch_dispatch_emits_resource_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh ``sys_terminal_launch`` publishes ``session.resource.created``.

    Verifies the runner dispatcher surfaces a tool-launched terminal
    on the live SSE stream mid-turn (the whole point of this change),
    with the same resource shape the REST path emits.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.runner.tool_dispatch import _execute_terminal_tool
    from omnigent.tools.base import ToolContext
    from omnigent.tools.builtins.sys_terminal import SysTerminalLaunchTool

    spec = AgentSpec(spec_version=1)

    def _fake_invoke(self: SysTerminalLaunchTool, arguments: str, ctx: ToolContext) -> str:
        del self, arguments, ctx
        return json.dumps({"terminal": "zsh", "session": "s1", "status": "launched"})

    monkeypatch.setattr(SysTerminalLaunchTool, "invoke", _fake_invoke)

    registry = _StubTerminalRegistry(_StubTerminalInstance(running=True))
    published: list[dict[str, Any]] = []

    out = await _execute_terminal_tool(
        "sys_terminal_launch",
        {"terminal": "zsh", "session": "s1"},
        terminal_registry=registry,
        agent_spec=spec,
        conversation_id="conv_emit",
        task_id="task_emit",
        agent_id="ag_emit",
        publish_event=_capturing_publish_event(published),
    )

    # Tool output is returned unchanged to the harness.
    assert json.loads(out)["status"] == "launched"
    # Exactly one live event — the fresh-launch resource.created. A
    # second event would mean the close branch or a duplicate fired.
    assert len(published) == 1, f"expected 1 published event, got {published}"
    event = published[0]
    assert event["type"] == "session.resource.created"
    # Resource shape matches the REST path: deterministic id +
    # terminal type so the web rail / relay handle both identically.
    assert event["resource"]["id"] == "terminal_zsh_s1"
    assert event["resource"]["type"] == "terminal"
    assert event["resource"]["metadata"]["terminal_name"] == "zsh"
    # The instance was looked up from the registry to build the view.
    assert registry.get_calls == [("conv_emit", "zsh", "s1")]


@pytest.mark.asyncio
async def test_terminal_launch_idempotent_does_not_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``already_running`` launch publishes nothing.

    Re-launching an existing ``(terminal, session)`` returns
    ``status: already_running``; emitting ``session.resource.created``
    again would double-create the same id in the rail.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.runner.tool_dispatch import _execute_terminal_tool
    from omnigent.tools.base import ToolContext
    from omnigent.tools.builtins.sys_terminal import SysTerminalLaunchTool

    spec = AgentSpec(spec_version=1)

    def _fake_invoke(self: SysTerminalLaunchTool, arguments: str, ctx: ToolContext) -> str:
        del self, arguments, ctx
        return json.dumps({"terminal": "zsh", "session": "s1", "status": "already_running"})

    monkeypatch.setattr(SysTerminalLaunchTool, "invoke", _fake_invoke)

    registry = _StubTerminalRegistry(_StubTerminalInstance(running=True))
    published: list[dict[str, Any]] = []

    await _execute_terminal_tool(
        "sys_terminal_launch",
        {"terminal": "zsh", "session": "s1"},
        terminal_registry=registry,
        agent_spec=spec,
        conversation_id="conv_emit",
        task_id="task_emit",
        agent_id="ag_emit",
        publish_event=_capturing_publish_event(published),
    )

    # No event: the resource already existed before this launch.
    assert published == []
    # And we didn't even bother looking up the instance to build a view.
    assert registry.get_calls == []


@pytest.mark.asyncio
async def test_terminal_close_dispatch_emits_resource_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful ``sys_terminal_close`` publishes ``session.resource.deleted``.

    Verifies the symmetric teardown path: closing a terminal removes
    it from the rail live, with the deleted-event shape the REST path
    and web UI already handle.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.runner.tool_dispatch import _execute_terminal_tool
    from omnigent.tools.base import ToolContext
    from omnigent.tools.builtins.sys_terminal import SysTerminalCloseTool

    spec = AgentSpec(spec_version=1)

    def _fake_invoke(self: SysTerminalCloseTool, arguments: str, ctx: ToolContext) -> str:
        del self, arguments, ctx
        return json.dumps({"terminal": "zsh", "session": "s1", "status": "closed"})

    monkeypatch.setattr(SysTerminalCloseTool, "invoke", _fake_invoke)

    # Close doesn't look up the instance (no resource view to build),
    # so a miss-returning registry is fine here.
    registry = _StubTerminalRegistry(None)
    published: list[dict[str, Any]] = []

    await _execute_terminal_tool(
        "sys_terminal_close",
        {"terminal": "zsh", "session": "s1"},
        terminal_registry=registry,
        agent_spec=spec,
        conversation_id="conv_emit",
        task_id="task_emit",
        agent_id="ag_emit",
        publish_event=_capturing_publish_event(published),
    )

    assert len(published) == 1, f"expected 1 published event, got {published}"
    event = published[0]
    assert event["type"] == "session.resource.deleted"
    assert event["resource_id"] == "terminal_zsh_s1"
    assert event["resource_type"] == "terminal"
    assert event["session_id"] == "conv_emit"
    # Deleted carries only the id; no registry lookup needed.
    assert registry.get_calls == []


@pytest.mark.asyncio
async def test_runner_read_inbox_continues_after_malformed_terminal_idle_item() -> None:
    """
    Malformed terminal idle items must not abort the inbox drain.

    :returns: None.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    session_inbox.put_nowait(
        {
            "handle_id": "handle_before",
            "tool_name": "sys_os_shell",
            "status": "completed",
            "output": "before",
        }
    )
    session_inbox.put_nowait({"type": "terminal_idle", "source": "zsh"})
    session_inbox.put_nowait(
        {
            "handle_id": "handle_after",
            "tool_name": "sys_os_shell",
            "status": "completed",
            "output": "after",
        }
    )

    inbox_output = await execute_tool(
        tool_name="sys_read_inbox",
        arguments="{}",
        session_inbox=session_inbox,
    )

    assert "task handle_before completed" in inbox_output
    assert "sys_os_shell returned: before" in inbox_output
    assert "malformed terminal_idle inbox item ignored" in inbox_output
    assert "terminal-idle inbox payload requires non-empty string session" in inbox_output
    assert "task handle_after completed" in inbox_output
    assert "sys_os_shell returned: after" in inbox_output
    assert session_inbox.empty()


@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    [
        ("{", "malformed JSON arguments"),
        ("", "malformed JSON arguments"),
        ("   ", "malformed JSON arguments"),
        ("[]", "arguments must be a JSON object"),
        ("42", "arguments must be a JSON object"),
        ("null", "arguments must be a JSON object"),
        ('"scalar"', "arguments must be a JSON object"),
    ],
)
@pytest.mark.asyncio
async def test_execute_tool_rejects_non_object_arguments(
    monkeypatch: pytest.MonkeyPatch,
    arguments: str,
    expected_error: str,
) -> None:
    """
    Malformed or non-object argument payloads fail before tool dispatch.

    A silent ``{}`` substitution would run a default/no-argument system
    tool after bad input; the shared parser must reject these shapes and
    the underlying OS-env handler must never be entered.
    """
    from omnigent.runner import tool_dispatch
    from omnigent.runner.tool_dispatch import execute_tool

    calls: list[object] = []

    async def _spy_os_env_tool(*args: object, **kwargs: object) -> str:
        calls.append((args, kwargs))
        return json.dumps({"ok": True})

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _spy_os_env_tool)

    output = await execute_tool(tool_name="sys_os_read", arguments=arguments)

    assert json.loads(output) == {"error": expected_error}
    assert calls == []


@pytest.mark.asyncio
async def test_execute_tool_accepts_empty_object_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid ``{}`` still reaches the tool with an empty argument dict."""
    from omnigent.runner import tool_dispatch
    from omnigent.runner.tool_dispatch import execute_tool

    calls: list[dict[str, Any]] = []

    async def _spy_os_env_tool(
        tool_name: str,
        args: dict[str, Any],
        **_kwargs: object,
    ) -> str:
        del tool_name
        calls.append(args)
        return json.dumps({"ok": True})

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _spy_os_env_tool)

    output = await execute_tool(tool_name="sys_os_read", arguments="{}")

    assert json.loads(output) == {"ok": True}
    assert calls == [{}]


# ── End-to-end with real harness subprocess + real LLM ──


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set; runner→harness→LLM e2e skipped",
)
@pytest.mark.asyncio
async def test_runner_dispatches_to_spawned_harness_with_real_llm(
    started_manager: HarnessProcessManager,
) -> None:
    """The flagship architectural test.

    Server-side httpx → runner FastAPI's
    ``POST /v1/sessions/{conversation_id}/events?stream=true`` →
    runner asks ``HarnessProcessManager`` to spawn / fetch the
    per-conversation harness → uvicorn subprocess on a UDS →
    test harness's ``run_turn`` → real OpenAI gpt-4o-mini → SSE
    chunks stream back through harness UDS → runner's proxy_stream
    → test httpx client.

    Successful streaming with the expected event sequence means:
    1. The runner package uses the existing HarnessProcessManager
       (no parallel impl).
    2. A real harness subprocess is spawned per (conversation, harness).
    3. The runner correctly relays the harness's SSE bytes.
    4. The runner is exercising the architectural shape from
       designs/RUNNER.md §4 — not just calling an LLM directly.
    """
    app = create_runner_app(
        process_manager=started_manager,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        async with http.stream(
            "POST",
            "/v1/sessions/conv_runner_e2e_test/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "harness": _TEST_HARNESS_NAME,
                "content": [{"role": "user", "content": "Reply with the single word 'pong'."}],
                "model": "openai/gpt-4o-mini",
                "instructions": "You echo single words.",
                "connection_params": {"api_key": os.environ["OPENAI_API_KEY"]},
            },
            timeout=120.0,
        ) as response:
            assert response.status_code == 200, (
                f"runner dispatch failed: {response.status_code} "
                f"{(await response.aread()).decode('utf-8', errors='replace')}"
            )
            assert response.headers["content-type"].startswith("text/event-stream")
            body = b"".join([chunk async for chunk in response.aiter_bytes()])

    # Parse the SSE bytes back into events.
    events = _parse_sse(body)
    types = [t for t, _ in events]
    # Must START with response.created and END with response.completed —
    # both ends of the harness's emitted stream made it through
    # the runner's proxy. A real LLM produces at least one delta in
    # between.
    assert types[0] == "response.created", (
        f"first event must be response.created; got types={types}"
    )
    assert types[-1] == "response.completed", (
        f"last event must be response.completed; got types={types}"
    )
    assert any(t == "response.output_text.delta" for t in types), (
        f"expected text deltas from real LLM; got types={types}"
    )
    # The subprocess actually got spawned — verify by checking the
    # manager's internal state. (Direct attribute access; the manager
    # is a test-fixture instance so this is fine.)
    assert "conv_runner_e2e_test" in started_manager._entries, (
        "manager should have a spawned entry for the scaffold conversation; "
        "if missing, the runner didn't actually dispatch through "
        "HarnessProcessManager.get_client"
    )


def _parse_sse(raw: bytes) -> list[tuple[str, dict]]:
    """Decode SSE bytes into ``[(event_type, payload), ...]``."""
    text = raw.decode("utf-8", errors="replace")
    events: list[tuple[str, dict]] = []
    current_type: str | None = None
    current_data: list[str] = []
    for line in text.split("\n"):
        if line.startswith("event: "):
            current_type = line[len("event: ") :]
        elif line.startswith("data: "):
            current_data.append(line[len("data: ") :])
        elif line == "" and current_type is not None and current_data:
            try:
                payload = json.loads("\n".join(current_data))
            except json.JSONDecodeError:
                payload = {"_raw": "\n".join(current_data)}
            events.append((current_type, payload))
            current_type = None
            current_data = []
    return events


def test_maybe_signal_changed_files_throttles_within_window() -> None:
    """
    ``_maybe_signal_changed_files`` emits at most one
    ``session.changed_files.invalidated`` per throttle window per
    session, then re-emits after the window elapses.

    The monotonic ``now`` is injected so this is deterministic with no
    real sleep. Regression target: a multi-file turn must collapse to
    one refetch trigger (leading-edge throttle), not fire per write.
    """
    from omnigent.runner.tool_dispatch import (
        _CHANGED_FILES_SIGNAL_THROTTLE_S,
        _maybe_signal_changed_files,
    )

    published: list[dict[str, object]] = []

    def _pub(_sid: str, event: dict[str, Any]) -> None:
        published.append(event)

    sid = "conv_changed_files_throttle_unique"
    # First call emits immediately (leading edge).
    _maybe_signal_changed_files(sid, _pub, now=100.0)
    # Within the window — suppressed.
    _maybe_signal_changed_files(sid, _pub, now=100.0 + _CHANGED_FILES_SIGNAL_THROTTLE_S / 2)
    # After the window — emits again.
    _maybe_signal_changed_files(sid, _pub, now=100.0 + _CHANGED_FILES_SIGNAL_THROTTLE_S + 0.01)

    # 2 = first (leading) + post-window; the middle call was throttled.
    # If 3, the throttle window is not being honored; if 1, the
    # post-window re-emit is broken.
    assert len(published) == 2, f"expected 2 signals (leading + post-window), got {len(published)}"
    for event in published:
        assert event["type"] == "session.changed_files.invalidated"
        assert event["session_id"] == sid
        assert event["environment_id"] == "default"

    # A missing publisher or session id is a no-op (no crash, no emit).
    _maybe_signal_changed_files(None, _pub, now=200.0)
    _maybe_signal_changed_files(sid, None, now=200.0)
    assert len(published) == 2


def test_subagent_read_tools_are_runner_local() -> None:
    """
    ``sys_session_list`` and ``sys_session_get_history`` dispatch locally in the runner.

    If this regresses, native harnesses that call these tools fall through to
    spec-callable resolution and the user sees "not in local dispatch table"
    instead of sub-agent recovery data.
    """
    from omnigent.runner.tool_dispatch import should_dispatch_locally

    assert should_dispatch_locally("sys_session_list") is True
    assert should_dispatch_locally("sys_session_get_history") is True
    # get_info is a runner-local read like list/get_history; if it falls out of
    # the local dispatch table the orchestrator's status checks break.
    assert should_dispatch_locally("sys_session_get_info") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "subagent_args",
    [
        pytest.param("continue", id="plain-string-contract"),
        pytest.param({"input": "continue"}, id="object-input-contract"),
    ],
)
async def test_sys_session_send_reuses_existing_child_session(
    monkeypatch: pytest.MonkeyPatch,
    subagent_args: str | dict[str, str],
) -> None:
    """
    Re-sending to the same ``(agent, title)`` continues the existing child.

    This catches the duplicate-create regression behind unreliable
    continuation: if the runner POSTs ``/v1/sessions`` despite the existing
    child row, this assertion fails and the user would see a duplicate-title server
    error instead of a continuation. The parameterized ``args`` values also
    the runner preserves the public plain-string contract while accepting
    Nessie's object form.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param subagent_args: ``sys_session_send`` ``args`` payload.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    create_posts = 0
    event_posts: list[dict[str, Any]] = []
    published: list[dict[str, Any]] = []

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_posts
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_parent":
            return httpx.Response(
                200,
                json={"labels": {"omnigent.turn_actor": "bob@example.com"}},
            )
        if (
            request.method == "GET"
            and request.url.path == "/v1/sessions/conv_parent/child_sessions"
        ):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "conv_existing",
                            "tool": "claude",
                            "session_name": "issue-1756",
                            "busy": False,
                        }
                    ]
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions":
            create_posts += 1
            return httpx.Response(500, json={"error": "duplicate"})
        if request.method == "POST" and request.url.path == "/v1/sessions/conv_existing/events":
            event_posts.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "agent": "claude",
                        "title": "issue-1756",
                        "args": subagent_args,
                    }
                ),
                server_client=server_client,
                conversation_id="conv_parent",
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="claude")]),
                session_inbox=session_inbox,
                publish_event=_capturing_publish_event(published),
            )
        finally:
            runner_app.unregister_subagent_work("conv_existing")
            runner_app._session_inboxes_ref.pop("conv_parent", None)

    payload = json.loads(output)
    assert create_posts == 0, "continuation must not create a duplicate child session"
    assert payload["conversation_id"] == "conv_existing"
    assert payload["status"] == "launching"
    assert "continued ok" not in payload["message"]
    assert event_posts[0]["created_by"] == "bob@example.com"
    assert event_posts[0]["data"]["content"][0]["text"] == "continue"
    assert published[-1]["type"] == "session.child_session.updated"
    assert published[-1]["child"]["current_task_status"] == "launching"
    assert published[-1]["child"]["busy"] is False


@pytest.mark.asyncio
async def test_sys_session_send_named_child_retries_without_rejected_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing runner token cannot make new-child dispatch teardown the child."""
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    event_posts: list[dict[str, Any]] = []
    delete_posts = 0

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        nonlocal delete_posts
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_parent_retry":
            return httpx.Response(
                200,
                json={"labels": {"omnigent.turn_actor": "bob@example.com"}},
            )
        if (
            request.method == "GET"
            and request.url.path == "/v1/sessions/conv_parent_retry/child_sessions"
        ):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"id": "conv_child_retry"})
        if request.method == "POST" and request.url.path == "/v1/sessions/conv_child_retry/events":
            event_posts.append(json.loads(request.content))
            if len(event_posts) == 1:
                return httpx.Response(403, json={"error": "created_by rejected"})
            return httpx.Response(202, json={"queued": True})
        if request.method == "DELETE" and request.url.path == "/v1/sessions/conv_child_retry":
            delete_posts += 1
            return httpx.Response(204)
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps({"agent": "worker", "title": "retry", "args": "continue"}),
                server_client=server_client,
                conversation_id="conv_parent_retry",
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="worker")]),
                session_inbox=session_inbox,
            )
        finally:
            runner_app.unregister_subagent_work("conv_child_retry")
            runner_app._session_inboxes_ref.pop("conv_parent_retry", None)

    payload = json.loads(output)
    assert payload["conversation_id"] == "conv_child_retry"
    assert payload["status"] == "launching"
    assert len(event_posts) == 2
    assert event_posts[0]["created_by"] == "bob@example.com"
    assert "created_by" not in event_posts[1]
    assert delete_posts == 0


@pytest.mark.asyncio
async def test_sys_session_send_existing_child_retries_without_rejected_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing runner token cannot make existing-child dispatch fail closed."""
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    event_posts: list[dict[str, Any]] = []

    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_parent_existing":
            return httpx.Response(
                200,
                json={"labels": {"omnigent.turn_actor": "bob@example.com"}},
            )
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_existing_retry":
            return httpx.Response(
                200,
                json={
                    "id": "conv_existing_retry",
                    "parent_session_id": "conv_parent_existing",
                    "title": "worker:retry",
                },
            )
        if (
            request.method == "POST"
            and request.url.path == "/v1/sessions/conv_existing_retry/events"
        ):
            event_posts.append(json.loads(request.content))
            if len(event_posts) == 1:
                return httpx.Response(403, json={"error": "created_by rejected"})
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps({"session_id": "conv_existing_retry", "args": "continue"}),
                server_client=server_client,
                conversation_id="conv_parent_existing",
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="worker")]),
                session_inbox=session_inbox,
            )
        finally:
            runner_app.unregister_subagent_work("conv_existing_retry")
            runner_app._session_inboxes_ref.pop("conv_parent_existing", None)

    payload = json.loads(output)
    assert payload["conversation_id"] == "conv_existing_retry"
    assert payload["status"] == "launching"
    assert len(event_posts) == 2
    assert event_posts[0]["created_by"] == "bob@example.com"
    assert "created_by" not in event_posts[1]


def _spec_with_subagent_harness(harness: str) -> SimpleNamespace:
    """
    Build a parent-spec stub declaring one ``worker`` sub-agent.

    Mirrors the AP-style ``sub_agents`` shape ``_subagent_harness``
    walks: ``executor.config["harness"]`` falling back to
    ``executor.type``.

    :param harness: The sub-agent's declared harness, e.g.
        ``"codex-native"``.
    :returns: A structural parent-spec stub for ``execute_tool``.
    """
    return SimpleNamespace(
        sub_agents=[
            SimpleNamespace(
                name="worker",
                executor=SimpleNamespace(type="omnigent", config={"harness": harness}),
            )
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "model"),
    [
        pytest.param("claude-native", "databricks-claude-sonnet-4-6", id="claude-native"),
        pytest.param("codex-native", "databricks-gpt-5-4", id="codex-native"),
        pytest.param("claude-sdk", "databricks-claude-sonnet-4-6", id="claude-sdk"),
    ],
)
async def test_sys_session_send_model_lands_in_child_create_body(
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    model: str,
) -> None:
    """
    A per-dispatch ``model`` reaches the child create as ``model_override``.

    The server persists ``model_override`` on the child row, where the
    native launch paths read it as ``--model`` and the SDK harness path
    as ``HARNESS_<H>_MODEL``. If the create body drops the field, the
    child silently runs on the harness default — the exact silent-drop
    failure this feature forbids.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param harness: Declared sub-agent harness under test.
    :param model: Family-appropriate model id for *harness*.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    create_bodies: list[dict[str, Any]] = []

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Serve fresh-create child lookup, create, and message POSTs."""
        if (
            request.method == "GET"
            and request.url.path == "/v1/sessions/conv_parent_model/child_sessions"
        ):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and request.url.path == "/v1/sessions":
            create_bodies.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "conv_child_model"})
        if request.method == "POST" and request.url.path == "/v1/sessions/conv_child_model/events":
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "agent": "worker",
                        "title": "fix-auth",
                        "args": {
                            "input": "fix the auth bug",
                            "model": model,
                        },
                    }
                ),
                server_client=server_client,
                conversation_id="conv_parent_model",
                agent_spec=_spec_with_subagent_harness(harness),
                session_inbox=session_inbox,
            )
        finally:
            runner_app.unregister_subagent_work("conv_child_model")
            runner_app._session_inboxes_ref.pop("conv_parent_model", None)

    payload = json.loads(output)
    assert payload["status"] == "launching"
    # Exactly one create, carrying the override verbatim — the value the
    # server persists and the harness launch consumes.
    assert len(create_bodies) == 1, "fresh named send must create exactly one child"
    assert create_bodies[0]["model_override"] == model
    assert create_bodies[0]["sub_agent_name"] == "worker"


@pytest.mark.asyncio
async def test_sys_session_send_blocks_fresh_dispatch_when_harness_cli_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh dispatch whose harness CLI is absent fails loud, creates nothing.

    Without this preflight a missing CLI surfaces only as a lazy first-turn
    boot failure (the pi harness raises ImportError, which the parent sees as
    a generic "turn failed" inbox item), and the orchestrator may re-dispatch
    into the same wall. The tool must instead return an actionable error
    naming the missing binary and install command, BEFORE creating any child
    session.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.onboarding.harness_install import HarnessInstallSpec
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    # Override the autouse "all CLIs present" stub: pi's CLI is absent here.
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.missing_harness_cli",
        lambda harness: HarnessInstallSpec("Pi", "pi", "@earendil-works/pi-coding-agent"),
    )
    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")

    create_posts = 0
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Serve the fresh-create child lookup; count (and reject) any create."""
        nonlocal create_posts
        if (
            request.method == "GET"
            and request.url.path == "/v1/sessions/conv_parent_nopi/child_sessions"
        ):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and request.url.path == "/v1/sessions":
            create_posts += 1
            return httpx.Response(201, json={"id": "conv_should_not_exist"})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "agent": "worker",
                        "title": "review-auth",
                        "args": {"input": "review the diff"},
                    }
                ),
                server_client=server_client,
                conversation_id="conv_parent_nopi",
                agent_spec=_spec_with_subagent_harness("pi"),
                session_inbox=session_inbox,
            )
        finally:
            runner_app._session_inboxes_ref.pop("conv_parent_nopi", None)

    # The output is a plain error string (not a JSON status payload) naming
    # the missing binary and how to install it — what the orchestrator/human
    # needs to unblock. If this regresses, the dispatch would instead create a
    # child that can never boot.
    assert output.startswith("Error:")
    assert "'pi' CLI" in output
    assert "npm install -g @earendil-works/pi-coding-agent" in output
    # No child was created — the guard returned before the create POST. A
    # nonzero count would mean we spawned a worker doomed to fail at boot.
    assert create_posts == 0


@pytest.mark.asyncio
async def test_sys_session_send_model_rejected_for_existing_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Passing ``model`` on a continuation send fails loud, sends nothing.

    A native child bakes ``--model`` in at terminal launch, so applying
    a new model to an existing session would be silently ignored. The
    tool must return an actionable error (continue without ``model`` or
    close and respawn) instead of continuing on the wrong model.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    create_posts = 0
    event_posts = 0

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Serve the existing-child lookup; count any writes."""
        nonlocal create_posts, event_posts
        if (
            request.method == "GET"
            and request.url.path == "/v1/sessions/conv_parent_model_cont/child_sessions"
        ):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "conv_existing_model",
                            "tool": "worker",
                            "session_name": "fix-auth",
                            "busy": False,
                        }
                    ]
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions":
            create_posts += 1
            return httpx.Response(201, json={"id": "conv_dup"})
        if request.method == "POST" and request.url.path.endswith("/events"):
            event_posts += 1
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "agent": "worker",
                        "title": "fix-auth",
                        "args": {"input": "continue", "model": "claude-opus-4-8"},
                    }
                ),
                server_client=server_client,
                conversation_id="conv_parent_model_cont",
                agent_spec=_spec_with_subagent_harness("claude-native"),
                session_inbox=session_inbox,
            )
        finally:
            runner_app._session_inboxes_ref.pop("conv_parent_model_cont", None)

    assert output.startswith("Error:"), output
    # The error must name the existing session and the recovery paths.
    assert "conv_existing_model" in output
    assert "sys_session_close" in output
    # No write happened: the wrong-model continuation never started.
    assert create_posts == 0
    assert event_posts == 0


@pytest.mark.asyncio
async def test_sys_session_send_model_rejected_in_by_id_mode() -> None:
    """
    ``model`` plus ``session_id`` fails loud before any server call.

    By-id mode always targets an existing session, where a model
    override cannot take effect — the tool must reject it instead of
    silently dropping the field.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    requests_seen = 0
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Count every request — none is expected."""
        nonlocal requests_seen
        requests_seen += 1
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "session_id": "conv_some_child",
                        "args": {"input": "continue", "model": "claude-opus-4-8"},
                    }
                ),
                server_client=server_client,
                conversation_id="conv_parent_by_id_model",
                session_inbox=session_inbox,
            )
        finally:
            runner_app._session_inboxes_ref.pop("conv_parent_by_id_model", None)

    assert output.startswith("Error:"), output
    assert "model" in output
    # Rejected before lookup: a misaddressed override must not even read
    # the target session.
    assert requests_seen == 0


@pytest.mark.asyncio
async def test_sys_session_send_model_rejected_for_unplumbed_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``model`` for a harness without override plumbing fails loud.

    Unknown harnesses have no runner-side model-override path, so
    the persisted value would be silently ignored. The error must name
    the harness so the orchestrator understands why the dispatch failed.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    create_posts = 0

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Serve the empty child lookup; count creates."""
        nonlocal create_posts
        if (
            request.method == "GET"
            and request.url.path == "/v1/sessions/conv_parent_unplumbed/child_sessions"
        ):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and request.url.path == "/v1/sessions":
            create_posts += 1
            return httpx.Response(201, json={"id": "conv_never"})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "agent": "worker",
                        "title": "summarize",
                        "args": {"input": "summarize this", "model": "some-model"},
                    }
                ),
                server_client=server_client,
                conversation_id="conv_parent_unplumbed",
                agent_spec=_spec_with_subagent_harness("unknown-harness"),
                session_inbox=session_inbox,
            )
        finally:
            runner_app._session_inboxes_ref.pop("conv_parent_unplumbed", None)

    assert output.startswith("Error:"), output
    assert "unknown-harness" in output
    # The unsupported dispatch must not create a child that would then
    # silently run on the harness default.
    assert create_posts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "model", "expected_rule"),
    [
        pytest.param(
            "claude-native", "databricks-gpt-5-4", "only runs Claude models", id="gpt-on-claude"
        ),
        pytest.param(
            "codex-native",
            "databricks-claude-sonnet-4-6",
            "only runs GPT models",
            id="claude-on-codex",
        ),
        pytest.param(
            "claude-native",
            "databricks-meta-llama-3.3-70b-instruct",
            "only runs Claude models",
            id="unknown-family-on-claude",
        ),
    ],
)
async def test_sys_session_send_model_rejected_for_wrong_family(
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    model: str,
    expected_rule: str,
) -> None:
    """
    A cross-family ``model`` fails loud at dispatch, before any create.

    The single-vendor workers can only run their own vendor's models; a
    wrong-family id would otherwise spawn a child that errors opaquely
    at the harness/gateway. The error names the rule so the orchestrator
    can re-dispatch with a compatible model or the pi worker.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param harness: Sub-agent harness under test.
    :param model: Cross-family or undeterminable model id.
    :param expected_rule: Rule text the error must contain.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    create_posts = 0

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Serve the empty child lookup; count creates."""
        nonlocal create_posts
        if (
            request.method == "GET"
            and request.url.path == "/v1/sessions/conv_parent_family/child_sessions"
        ):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and request.url.path == "/v1/sessions":
            create_posts += 1
            return httpx.Response(201, json={"id": "conv_never"})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "agent": "worker",
                        "title": "task",
                        "args": {"input": "do the task", "model": model},
                    }
                ),
                server_client=server_client,
                conversation_id="conv_parent_family",
                agent_spec=_spec_with_subagent_harness(harness),
                session_inbox=session_inbox,
            )
        finally:
            runner_app._session_inboxes_ref.pop("conv_parent_family", None)

    assert output.startswith("Error:"), output
    assert expected_rule in output
    assert model in output
    # The rejected dispatch must not create a child that would then fail
    # opaquely on its first turn.
    assert create_posts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_model",
    [
        pytest.param("claude; rm -rf /", id="shell-metacharacters"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param(42, id="non-string"),
    ],
)
async def test_sys_session_send_model_invalid_rejected_before_any_server_call(
    bad_model: object,
) -> None:
    """
    Malformed ``model`` values fail loud before any server traffic.

    The override eventually lands on a command line (``--model``), so
    shell-shaped or non-string values must be rejected at the tool
    boundary — not persisted and not silently dropped.

    :param bad_model: The invalid ``model`` payload under test.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    requests_seen = 0
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Count every request — none is expected."""
        nonlocal requests_seen
        requests_seen += 1
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "agent": "worker",
                        "title": "fix-auth",
                        "args": {"input": "fix it", "model": bad_model},
                    }
                ),
                server_client=server_client,
                conversation_id="conv_parent_bad_model",
                agent_spec=_spec_with_subagent_harness("claude-native"),
                session_inbox=session_inbox,
            )
        finally:
            runner_app._session_inboxes_ref.pop("conv_parent_bad_model", None)

    assert output.startswith("Error:"), output
    assert "model" in output
    # Validation precedes every lookup/create — nothing reached the server.
    assert requests_seen == 0


def _spec_with_real_subagent(harness: str) -> AgentSpec:
    """
    Build a real parent :class:`AgentSpec` with one ``worker`` sub-agent.

    Unlike :func:`_spec_with_subagent_harness` (a structural stub), this
    is a fully-typed spec the model-provider resolution can walk — the
    normalization gate resolves ``executor.auth`` / ``profile`` / config
    providers on the sub-spec.

    :param harness: The sub-agent's declared harness, e.g.
        ``"claude-native"``.
    :returns: The parent spec.
    """
    return AgentSpec(
        spec_version=1,
        name="parent",
        sub_agents=[
            AgentSpec(
                spec_version=1,
                name="worker",
                executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
            )
        ],
    )


def _isolate_model_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, yaml_text: str
) -> None:
    """
    Point provider resolution at an isolated config, no ambient creds.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir holding ``config.yaml``.
    :param yaml_text: The config contents, e.g. a ``providers:`` block.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    (tmp_path / "config.yaml").write_text(yaml_text)


@dataclass
class _ModelSendResult:
    """
    Outcome of one fresh-create ``sys_session_send`` model dispatch.

    :param output: The tool output string (JSON handle or ``Error:``).
    :param create_bodies: The ``POST /v1/sessions`` bodies the mock
        server captured — the persisted ``model_override`` lives here.
    """

    output: str
    create_bodies: list[dict[str, Any]]


async def _dispatch_model_send(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_spec: Any,
    model: str,
    conv_id: str,
) -> _ModelSendResult:
    """
    Drive one fresh-create ``sys_session_send`` carrying ``args.model``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param agent_spec: The parent spec under test.
    :param model: The requested per-dispatch model id.
    :param conv_id: A unique parent conversation id per test.
    :returns: The tool output and the captured create bodies.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    create_bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Serve fresh-create child lookup, create, and message POSTs."""
        if (
            request.method == "GET"
            and request.url.path == f"/v1/sessions/{conv_id}/child_sessions"
        ):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and request.url.path == "/v1/sessions":
            create_bodies.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "conv_child_norm"})
        if request.method == "POST" and request.url.path == "/v1/sessions/conv_child_norm/events":
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "agent": "worker",
                        "title": "task",
                        "args": {"input": "do the task", "model": model},
                    }
                ),
                server_client=server_client,
                conversation_id=conv_id,
                agent_spec=agent_spec,
                session_inbox=session_inbox,
            )
        finally:
            runner_app.unregister_subagent_work("conv_child_norm")
            runner_app._session_inboxes_ref.pop(conv_id, None)
    return _ModelSendResult(output=output, create_bodies=create_bodies)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "model", "expected"),
    [
        pytest.param(
            "claude-native",
            "claude-sonnet-4-6",
            "databricks-claude-sonnet-4-6",
            id="canonical-claude-localized",
        ),
        pytest.param(
            "codex-native", "gpt-5-4", "databricks-gpt-5-4", id="canonical-gpt-localized"
        ),
        pytest.param(
            "claude-native",
            "databricks-claude-sonnet-4-6",
            "databricks-claude-sonnet-4-6",
            id="already-local-unchanged",
        ),
        pytest.param(
            "claude-native",
            "us.anthropic.claude-sonnet-4-6",
            "us.anthropic.claude-sonnet-4-6",
            id="non-mechanical-passthrough",
        ),
    ],
)
async def test_sys_session_send_localizes_canonical_model_for_gateway_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    harness: str,
    model: str,
    expected: str,
) -> None:
    """
    A gateway-routed child persists the gateway-local spelling.

    With a Databricks default provider, a bare canonical vendor id
    (``claude-sonnet-4-6``) would die at the gateway ("model not
    found"); the gate must persist the ``databricks-``-prefixed
    spelling as ``model_override`` — and ONLY for mechanical ids:
    already-local and vendor-prefixed shapes pass through verbatim.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir for the isolated provider config.
    :param harness: The sub-agent harness under test.
    :param model: The requested per-dispatch model id.
    :param expected: The id the create body must persist.
    """
    _isolate_model_providers(
        monkeypatch,
        tmp_path,
        "providers:\n  workspace:\n    kind: databricks\n    profile: prof-a\n    default: true\n",
    )
    result = await _dispatch_model_send(
        monkeypatch,
        agent_spec=_spec_with_real_subagent(harness),
        model=model,
        conv_id="conv_parent_norm_gateway",
    )
    payload = json.loads(result.output)
    assert payload["status"] == "launching"
    # The persisted override is the localized id — this is the value the
    # server stores and the harness launch consumes.
    assert len(result.create_bodies) == 1
    assert result.create_bodies[0]["model_override"] == expected


@pytest.mark.asyncio
async def test_sys_session_send_strips_gateway_prefix_for_vendor_direct_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A vendor-direct child persists the bare canonical spelling.

    With an Anthropic API-key default provider, a ``databricks-``
    prefixed id would be rejected by the vendor API; the gate must
    strip the prefix before persisting.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir for the isolated provider config.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _isolate_model_providers(
        monkeypatch,
        tmp_path,
        "providers:\n"
        "  anthropic:\n"
        "    kind: key\n"
        "    default: true\n"
        "    anthropic:\n"
        "      base_url: https://api.anthropic.com\n"
        "      api_key: $ANTHROPIC_API_KEY\n",
    )
    result = await _dispatch_model_send(
        monkeypatch,
        agent_spec=_spec_with_real_subagent("claude-native"),
        model="databricks-claude-opus-4-8",
        conv_id="conv_parent_norm_direct",
    )
    payload = json.loads(result.output)
    assert payload["status"] == "launching"
    assert len(result.create_bodies) == 1
    # Stripped: the vendor API only routes the bare canonical id.
    assert result.create_bodies[0]["model_override"] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_sys_session_send_passes_model_through_when_provider_undeterminable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An undeterminable child provider leaves the requested id untouched.

    The structural sub-spec stub has no auth/profile attributes, so
    provider resolution degrades to "none" — the gate must neither
    crash the dispatch nor guess a transform.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir for the isolated provider config.
    """
    _isolate_model_providers(monkeypatch, tmp_path, "")
    result = await _dispatch_model_send(
        monkeypatch,
        agent_spec=_spec_with_subagent_harness("claude-native"),
        model="claude-sonnet-4-6",
        conv_id="conv_parent_norm_unknown",
    )
    payload = json.loads(result.output)
    assert payload["status"] == "launching"
    assert len(result.create_bodies) == 1
    # Pass-through: no provider kind, no transform — fail-loud at the
    # harness remains the safety net.
    assert result.create_bodies[0]["model_override"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_sys_session_send_family_guard_runs_before_normalization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The family guard fires on the RAW requested id, before any localize.

    A GPT id on a claude worker must be rejected quoting exactly what
    the caller sent (``gpt-5-4``, not ``databricks-gpt-5-4``) and no
    child may be created — even though the gateway provider would have
    localized the id had the guard passed.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir for the isolated provider config.
    """
    _isolate_model_providers(
        monkeypatch,
        tmp_path,
        "providers:\n  workspace:\n    kind: databricks\n    profile: prof-a\n    default: true\n",
    )
    result = await _dispatch_model_send(
        monkeypatch,
        agent_spec=_spec_with_real_subagent("claude-native"),
        model="gpt-5-4",
        conv_id="conv_parent_norm_family",
    )
    assert result.output.startswith("Error:"), result.output
    assert "only runs Claude models" in result.output
    # The error quotes the caller's raw id, so the guard ran first.
    assert "'gpt-5-4'" in result.output
    assert result.create_bodies == []


@pytest.mark.asyncio
async def test_sys_list_models_dispatches_locally_with_static_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``execute_tool`` routes ``sys_list_models`` to the catalog enumerator.

    With a subscription default (static — no HTTP), the payload must
    carry one row per declared sub-agent plus ``self``, each in the
    documented ``{source, verified, models, note}`` shape with the
    curated claude ids surviving the claude-family filter.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir for the isolated provider config.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    _isolate_model_providers(
        monkeypatch,
        tmp_path,
        "providers:\n  claude:\n    kind: subscription\n    cli: claude\n    default: true\n",
    )
    output = await execute_tool(
        tool_name="sys_list_models",
        arguments="{}",
        agent_spec=_spec_with_real_subagent("claude-native"),
        conversation_id="conv_list_models",
    )
    payload = json.loads(output)
    assert set(payload) == {"worker", "self"}
    worker = payload["worker"]
    assert worker["source"] == "static"
    assert worker["verified"] is False
    # The curated claude aliases survive the claude-family filter — the
    # exact ids an orchestrator may pass back as args.model.
    assert [m["id"] for m in worker["models"]] == [
        "claude-fable-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ]
    assert worker["note"]


@pytest.mark.asyncio
async def test_sys_list_models_requires_agent_spec() -> None:
    """
    ``sys_list_models`` with no resolvable spec fails loud, not empty.

    A silent ``{}`` would read as "no workers exist" — the error string
    tells the orchestrator the runner couldn't resolve its spec.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    output = await execute_tool(
        tool_name="sys_list_models",
        arguments="{}",
        agent_spec=None,
        conversation_id="conv_list_models_nospec",
    )
    assert output.startswith("Error:")
    assert "agent spec" in output


@pytest.mark.asyncio
async def test_sys_session_send_by_id_rejects_closed_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    By-id ``sys_session_send`` refuses closed direct children.

    The close tool hands orchestrators a durable ``conversation_id``.
    Without checking ``omnigent.closed=true`` in by-id mode, an
    orchestrator could keep chatting with the exact child it had just
    closed, bypassing the named lookup that skips closed rows.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    event_posts = 0
    registrations: list[str] = []
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    monkeypatch.setattr(
        runner_app,
        "register_child_session",
        lambda child_id, **_kwargs: registrations.append(child_id),
    )

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        nonlocal event_posts
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_closed":
            return httpx.Response(
                200,
                json={
                    "id": "conv_closed",
                    "title": "researcher:auth",
                    "parent_session_id": "conv_parent",
                    "labels": {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE},
                    "busy": False,
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions/conv_closed/events":
            event_posts += 1
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "session_id": "conv_closed",
                        "args": "please continue",
                    }
                ),
                server_client=server_client,
                conversation_id="conv_parent",
                session_inbox=session_inbox,
            )
        finally:
            runner_app._session_inboxes_ref.pop("conv_parent", None)

    payload = json.loads(output)
    assert payload["error"] == "session_closed"
    assert payload["conversation_id"] == "conv_closed"
    assert event_posts == 0
    assert registrations == []


@pytest.mark.asyncio
async def test_sys_session_send_completion_drains_from_parent_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A completed async sub-agent turn arrives through ``sys_read_inbox``.

    ``sys_session_send`` does not inline the child result in the
    tool result. If completion delivery is not wired, the drain returns the
    empty-inbox sentinel instead of the child marker.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Serve child-session create, lookup, and message POST requests."""
        if (
            request.method == "GET"
            and request.url.path == "/v1/sessions/conv_parent_inbox/child_sessions"
        ):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"id": "conv_child_inbox"})
        if request.method == "POST" and request.url.path == "/v1/sessions/conv_child_inbox/events":
            return httpx.Response(202, json={"queued": True})
        if (
            request.method == "POST"
            and request.url.path == "/v1/sessions/conv_parent_inbox/policies/evaluate"
        ):
            return httpx.Response(200, json={"result": "POLICY_ACTION_UNSPECIFIED"})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "agent": "worker",
                        "title": "phase-a",
                        "args": "run phase a",
                    }
                ),
                server_client=server_client,
                conversation_id="conv_parent_inbox",
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="worker")]),
                session_inbox=session_inbox,
            )
            payload = json.loads(output)
            assert payload["status"] == "launching"
            assert "CHILD_MARKER" not in payload["message"]

            runner_app.mark_subagent_work_terminal(
                "conv_child_inbox",
                status="completed",
                output="CHILD_MARKER",
            )
            inbox_output = await execute_tool(
                tool_name="sys_read_inbox",
                arguments="{}",
                server_client=server_client,
                conversation_id="conv_parent_inbox",
                session_inbox=session_inbox,
            )
        finally:
            runner_app.unregister_subagent_work("conv_child_inbox")
            runner_app._session_inboxes_ref.pop("conv_parent_inbox", None)

    assert "sub-agent task conv_child_inbox completed" in inbox_output
    assert "worker:phase-a returned: CHILD_MARKER" in inbox_output


@pytest.mark.asyncio
async def test_subagent_inbox_cleanup_does_not_unregister_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Draining an old child result must not delete a newer turn's work entry.

    Named sub-agents reuse the same child session. A parent can start a second
    turn after the first has completed but before reading the first result. The
    first inbox item's cleanup is guarded by the per-dispatch work id so it
    cannot unregister the second running turn.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    parent_id = "conv_parent_reused_child"
    child_id = "conv_reused_child"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    lookup_count = 0

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Serve two sends to the same child and policy checks for drains."""
        nonlocal lookup_count
        if (
            request.method == "GET"
            and request.url.path == f"/v1/sessions/{parent_id}/child_sessions"
        ):
            lookup_count += 1
            if lookup_count == 1:
                return httpx.Response(200, json={"data": []})
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": child_id,
                            "title": "worker:repeat",
                            "sub_agent_name": "worker",
                            "busy": False,
                        }
                    ]
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"id": child_id})
        if request.method == "POST" and request.url.path == f"/v1/sessions/{child_id}/events":
            return httpx.Response(202, json={"queued": True})
        if (
            request.method == "POST"
            and request.url.path == f"/v1/sessions/{parent_id}/policies/evaluate"
        ):
            return httpx.Response(200, json={"result": "POLICY_ACTION_UNSPECIFIED"})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            for prompt in ("first", "second"):
                output = await execute_tool(
                    tool_name="sys_session_send",
                    arguments=json.dumps(
                        {
                            "agent": "worker",
                            "title": "repeat",
                            "args": prompt,
                        }
                    ),
                    server_client=server_client,
                    conversation_id=parent_id,
                    agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="worker")]),
                    session_inbox=session_inbox,
                )
                assert json.loads(output)["status"] == "launching"
                if prompt == "first":
                    runner_app.mark_subagent_work_terminal(
                        child_id,
                        status="completed",
                        output="FIRST_RESULT",
                    )

            first_drain = await execute_tool(
                tool_name="sys_read_inbox",
                arguments="{}",
                server_client=server_client,
                conversation_id=parent_id,
                session_inbox=session_inbox,
            )
            current = runner_app.get_subagent_work(child_id)
            assert current is not None, (
                "Draining the first turn must not unregister the second turn's "
                "active work entry for the reused child session."
            )
            assert current.status == "launching"

            runner_app.mark_subagent_work_terminal(
                child_id,
                status="completed",
                output="SECOND_RESULT",
            )
            second_drain = await execute_tool(
                tool_name="sys_read_inbox",
                arguments="{}",
                server_client=server_client,
                conversation_id=parent_id,
                session_inbox=session_inbox,
            )
            assert runner_app.get_subagent_work(child_id) is None, (
                "Draining the matching second turn must unregister terminal "
                "work; otherwise the registry leaks completed child entries."
            )
        finally:
            runner_app.unregister_subagent_work(child_id)
            runner_app._session_inboxes_ref.pop(parent_id, None)

    assert "worker:repeat returned: FIRST_RESULT" in first_drain
    assert "SECOND_RESULT" not in first_drain
    assert "worker:repeat returned: SECOND_RESULT" in second_drain


def _sse_text_turn(text: str) -> list[str]:
    """Script one scaffold turn's SSE frames carrying ``text`` as output.

    The runner's ``proxy_stream`` accumulates ``response.output_text.delta``
    chunks and, on ``response.completed``, commits an assistant message to
    ``_session_histories`` — which is what ``_extract_last_assistant_text``
    reads for the sub-agent's delivered output. So a turn that emits
    ``text`` here is the turn whose result the parent should receive.

    :param text: Assistant text this turn streams, e.g. ``"FINAL"``.
    :returns: SSE frames for a created → one-delta → completed turn.
    """
    return [
        'event: response.created\ndata: {"type":"response.created",'
        '"response":{"id":"resp_multiturn"}}\n\n',
        "event: response.output_text.delta\ndata: "
        + json.dumps({"type": "response.output_text.delta", "delta": text})
        + "\n\n",
        'event: response.completed\ndata: {"type":"response.completed",'
        '"response":{"id":"resp_multiturn","status":"completed"}}\n\n',
    ]


class _GatedTwoTurnHarnessStream:
    """Per-turn-scripted harness stream that blocks turn 1 mid-flight.

    Turn 1 yields ``response.created`` + the intermediate text delta, sets
    ``started`` (the sync gate signalling the turn is live and in
    ``_active_turns``), then awaits ``release`` before yielding
    ``response.completed``. This holds turn 1 active deterministically while
    a second message is posted (which buffers a continuation) — no
    sleeps, no polling of runner internals. Turn 2 (and any later turn)
    streams its scripted frames straight through.

    :param turns: Per-turn SSE frame lists, indexed by call order.
    :param call_index: Shared 1-based turn counter (mutated per ``stream``).
    :param started: Set once turn 1 has emitted its intermediate delta.
    :param release: Awaited by turn 1 before emitting ``response.completed``.
    """

    def __init__(
        self,
        turns: list[list[str]],
        call_index: list[int],
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        """Store scripted turns and the turn-1 synchronization events.

        :param turns: Per-turn SSE frame lists, indexed by call order.
        :param call_index: Shared 1-based turn counter; this stream reads
            and advances it so each turn serves the next script.
        :param started: Set once turn 1 has emitted its intermediate delta.
        :param release: Gates turn 1's terminal ``response.completed``.
        """
        self._call_index = call_index
        self._call_index[0] += 1
        self._turn_number = self._call_index[0]
        self._frames = turns[min(self._turn_number - 1, len(turns) - 1)]
        self._started = started
        self._release = release
        self.status_code = 200

    async def __aenter__(self) -> _GatedTwoTurnHarnessStream:
        """Enter the stream context.

        :returns: This stream.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Exit without suppressing exceptions.

        :param exc_type: Exception type from the context, if any.
        :param exc: Exception value from the context, if any.
        :param tb: Traceback from the context, if any.
        :returns: None.
        """
        del exc_type, exc, tb

    async def aiter_text(self) -> AsyncIterator[str]:
        """Yield scripted frames, blocking turn 1 before it completes.

        :returns: Async iterator of SSE frames.
        """
        if self._turn_number == 1:
            # Emit created + the intermediate delta, then hand control back to
            # the caller (started) and wait (release) so turn 1 stays the
            # active turn while a continuation message buffers.
            yield self._frames[0]
            yield self._frames[1]
            self._started.set()
            await self._release.wait()
            yield self._frames[2]
            return
        for frame in self._frames:
            yield frame


class _GatedTwoTurnHarnessClient:
    """Harness client whose ``stream`` returns the gated two-turn stream.

    Also implements ``post`` (202) so the runner's best-effort mid-turn
    injection forward — fired when the second message buffers — succeeds
    quietly instead of raising into the buffering path.

    :param turns: Per-turn SSE frame lists.
    :param started: Set once turn 1 has emitted its intermediate delta.
    :param release: Gates turn 1's terminal ``response.completed``.
    """

    def __init__(
        self,
        turns: list[list[str]],
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        """Store the scripts and synchronization events.

        :param turns: Per-turn SSE frame lists.
        :param started: Set once turn 1 emits its intermediate delta.
        :param release: Gates turn 1's terminal ``response.completed``.
        """
        self._turns = turns
        self._started = started
        self._release = release
        self._call_index = [0]

    def stream(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, object],
        timeout: float | None,
    ) -> _GatedTwoTurnHarnessStream:
        """Return the next gated turn stream.

        :param method: HTTP method (ignored).
        :param url: Harness endpoint path (ignored).
        :param json: JSON body (ignored).
        :param timeout: Request timeout (ignored).
        :returns: Gated two-turn stream for the current turn.
        """
        del method, url, json, timeout
        return _GatedTwoTurnHarnessStream(
            self._turns, self._call_index, self._started, self._release
        )

    async def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: float | None,
    ) -> httpx.Response:
        """Accept the runner's mid-turn injection forward (best-effort).

        :param url: Harness endpoint path (ignored).
        :param json: Forwarded message body (ignored — the buffered copy is
            what drives the continuation; this fake never echoes an
            ``injection.consumed`` marker, so the buffer survives).
        :param timeout: Request timeout (ignored).
        :returns: A 202 so the forward is treated as accepted.
        """
        del url, json, timeout
        return httpx.Response(202, json={"queued": True})


@pytest.mark.asyncio
async def test_scaffold_subagent_defers_terminal_delivery_while_continuation_buffered() -> None:
    """A scaffold child running two turns delivers ONLY the final turn's text.

    Reproduces the multi-turn delivery bug: ``_on_proxy_stream_end`` used to
    mark a scaffold child's work entry terminal (``completed`` +
    ``_extract_last_assistant_text``) at EVERY successful turn end. A child
    that runs a second turn without a fresh parent ``sys_session_send`` — here
    a buffered continuation message drained by ``_check_and_start_next_turn``
    — then had its FIRST turn's intermediate narration delivered as the
    result, and its real final synthesis dropped by the already-terminal +
    ``delivered`` short-circuit in ``mark_subagent_work_terminal``.

    Determinism: turn 1's harness stream blocks (``release``) after emitting
    its intermediate delta and signals ``started`` so the caller can post the
    second message while turn 1 is provably the active turn (so it buffers a
    continuation). Releasing turn 1 then lets the continuation run to its own
    empty-buffer stream end. No sleeps, no polling of runner internals.
    """
    from omnigent.runner import app as runner_app

    parent_id = "conv_parent_multiturn_defer"
    child_id = "conv_child_multiturn_defer"
    started = asyncio.Event()
    release = asyncio.Event()
    turns = [_sse_text_turn("INTERMEDIATE_NARRATION"), _sse_text_turn("FINAL_SYNTHESIS")]

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return a non-native scaffold spec so the success-delivery path runs.

        The scaffold harness name is unknown to ``_build_spawn_env_from_spec``
        (no model needed) and is not ``claude-native`` / ``codex-native``, so
        ``_is_native_harness`` is False and the scaffold completion branch in
        ``_on_proxy_stream_end`` is exercised.

        :param agent_id: Agent id requested by the runner (unused).
        :param session_id: Session id (unused).
        :returns: A minimal scaffold spec bound to the scaffold harness.
        """
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="scaffold-multiturn-agent",
            executor=ExecutorSpec(type="omnigent", config={"harness": _TEST_HARNESS_NAME}),
        )

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_GatedTwoTurnHarnessClient(turns, started, release)),
        ),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    # Register the child as tracked sub-agent work with a real parent inbox —
    # the same module-level surface a parent ``sys_session_send`` uses. The
    # inbox queue is the observable delivery surface.
    parent_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[parent_id] = parent_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="multiturn",
    )
    try:
        async with _runner_test_client(app) as http:
            # Turn 1: starts a background turn that blocks before completing.
            resp1 = await http.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_scaffold",
                    "model": "x",
                    "content": [{"type": "input_text", "text": "do the work"}],
                },
            )
            assert resp1.status_code == 202

            # Sync gate: turn 1 is live (in _active_turns) and mid-stream.
            await asyncio.wait_for(started.wait(), timeout=10.0)

            # Concurrent action: a second message arrives while turn 1 is
            # active, so it buffers a continuation (the fake never emits an
            # injection.consumed marker, so the buffered copy survives).
            resp2 = await http.post(
                f"/v1/sessions/{child_id}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_scaffold",
                    "model": "x",
                    "content": [{"type": "input_text", "text": "now refine"}],
                },
            )
            # 202 with status "buffered" means the message buffered against
            # the active turn rather than starting its own — the precondition
            # for the deferral path. "accepted" would mean turn 1 has no
            # continuation pending, and the deferral path never applies.
            assert resp2.status_code == 202
            assert resp2.json()["status"] == "buffered"

            # End turn 1. With the fix, the non-empty buffer defers delivery;
            # the continuation turn 2 then runs to its own empty-buffer end.
            release.set()

            # Wait for the child to fully finish: terminal work entry +
            # exactly one delivered inbox item. The continuation turn is
            # guaranteed to reach _on_proxy_stream_end again with an empty
            # buffer, so this never hangs unless the result was stranded.
            deadline = asyncio.get_running_loop().time() + 10.0
            while asyncio.get_running_loop().time() < deadline:
                entry = runner_app.get_subagent_work(child_id)
                if entry is not None and entry.delivered:
                    break
                await asyncio.sleep(0.02)

            entry = runner_app.get_subagent_work(child_id)
            delivered_items: list[dict[str, Any]] = []
            while not parent_inbox.empty():
                delivered_items.append(parent_inbox.get_nowait())
    finally:
        release.set()
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    # Exactly one terminal payload reached the parent inbox. Two items would
    # mean the bug's double-delivery (intermediate + final); zero would mean
    # the deferral stranded the result (a worse failure than the bug).
    assert len(delivered_items) == 1, (
        f"expected exactly one terminal delivery, got {len(delivered_items)}: "
        f"{[i.get('output') for i in delivered_items]}"
    )
    item = delivered_items[0]
    assert item["status"] == "completed"  # success terminal status
    # The delivered output is the FINAL turn's text, not turn 1's intermediate
    # narration. The failure mode is turn 1's "INTERMEDIATE_NARRATION" being
    # delivered as the terminal result while the final synthesis is dropped.
    assert item["output"] == "FINAL_SYNTHESIS", (
        f"parent must receive the final turn's synthesis, got {item['output']!r}; "
        f"'INTERMEDIATE_NARRATION' here means turn 1 was delivered prematurely."
    )
    # The work entry settled terminal-and-delivered on the continuation turn.
    assert entry is not None
    assert entry.status == "completed"
    assert entry.delivered is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "policy_response", "expected_output", "blocked_output"),
    [
        pytest.param(
            "completed",
            {"result": "POLICY_ACTION_DENY", "reason": "secret output"},
            "[Result suppressed by policy: secret output]",
            "SECRET_MARKER",
            id="completed-deny-suppresses",
        ),
        pytest.param(
            "completed",
            {"result": "POLICY_ACTION_ALLOW", "data": "<REDACTED>"},
            "<REDACTED>",
            "SECRET_MARKER",
            id="completed-allow-data-transforms",
        ),
        pytest.param(
            "failed",
            {"result": "POLICY_ACTION_DENY", "reason": "failed secret output"},
            "[Result suppressed by policy: failed secret output]",
            "SECRET_MARKER",
            id="failed-deny-suppresses",
        ),
    ],
)
async def test_sys_read_inbox_applies_subagent_tool_result_policy(
    status: str,
    policy_response: dict[str, Any],
    expected_output: str,
    blocked_output: str,
) -> None:
    """
    ``sys_read_inbox`` evaluates delayed sub-agent output as TOOL_RESULT.

    ``sys_session_send`` returns a launching handle immediately, so the
    child output arrives after the original tool call. The delayed
    output must still pass through Omnigent policy evaluation before the LLM
    sees it in the inbox drain.

    :param status: Terminal sub-agent status being drained.
    :param policy_response: Fake Omnigent policy verdict body.
    :param expected_output: Output expected in the drained inbox text.
    :param blocked_output: Raw child output that policy must remove.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    session_inbox.put_nowait(
        {
            "type": "sub_agent",
            "task_id": "conv_child_policy",
            "handle_id": "conv_child_policy",
            "conversation_id": "conv_child_policy",
            "tool_name": "worker",
            "agent": "worker",
            "title": "phase-policy",
            "status": status,
            "output": "SECRET_MARKER",
        }
    )
    policy_requests: list[dict[str, Any]] = []

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Capture the Omnigent policy evaluation request."""
        if (
            request.method == "POST"
            and request.url.path == "/v1/sessions/conv_parent_policy/policies/evaluate"
        ):
            policy_requests.append(json.loads(request.content))
            return httpx.Response(200, json=policy_response)
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        inbox_output = await execute_tool(
            tool_name="sys_read_inbox",
            arguments="{}",
            server_client=server_client,
            conversation_id="conv_parent_policy",
            session_inbox=session_inbox,
        )

    assert len(policy_requests) == 1, (
        "Delayed sub-agent output must be policy-checked exactly once: "
        "0 means raw output bypassed TOOL_RESULT policy; >1 means duplicate evaluation."
    )
    event = policy_requests[0]["event"]
    assert event["type"] == "PHASE_TOOL_RESULT"
    assert event["data"]["result"] == "SECRET_MARKER"
    assert event["request_data"]["name"] == "sys_session_send"
    assert event["request_data"]["args"] == {
        "agent": "worker",
        "title": "phase-policy",
        "conversation_id": "conv_child_policy",
    }
    assert expected_output in inbox_output
    assert blocked_output not in inbox_output


@pytest.mark.asyncio
async def test_sys_read_inbox_requeues_subagent_output_on_transient_policy_failure() -> None:
    """
    Transient policy-evaluation failures must not destroy child output.

    The first drain receives a non-JSON policy response, so
    ``sys_read_inbox`` must fail closed and hide the raw child output.
    Because no real DENY/ASK/ALLOW verdict exists yet, the original
    payload must remain retryable; otherwise the second drain could not
    return the child result after the policy service recovers.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    parent_id = "conv_parent_policy_retry"
    child_id = "conv_child_policy_retry"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="worker",
        title="retry-policy",
    )
    runner_app.mark_subagent_work_terminal(
        child_id,
        status="completed",
        output="SECRET_RETRY_MARKER",
    )
    policy_attempts = 0
    work_after_second_drain: object = "not-drained"

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """
        Fail policy evaluation once, then allow the retry.

        :param request: Omnigent policy-evaluation request.
        :returns: Non-JSON response on first call, allow verdict later.
        """
        nonlocal policy_attempts
        if (
            request.method == "POST"
            and request.url.path == f"/v1/sessions/{parent_id}/policies/evaluate"
        ):
            policy_attempts += 1
            if policy_attempts == 1:
                return httpx.Response(200, content=b"not-json")
            return httpx.Response(200, json={"result": "POLICY_ACTION_UNSPECIFIED"})
        return httpx.Response(404, json={"error": str(request.url)})

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_server_handler),
            base_url="http://server",
        ) as server_client:
            first_drain = await execute_tool(
                tool_name="sys_read_inbox",
                arguments="{}",
                server_client=server_client,
                conversation_id=parent_id,
                session_inbox=session_inbox,
            )
            assert "[Result suppressed by policy: policy evaluation failed]" in first_drain
            assert "SECRET_RETRY_MARKER" not in first_drain
            assert runner_app.get_subagent_work(child_id) is not None, (
                "A transient policy failure must not unregister completed "
                "sub-agent work, or the real child output is lost permanently."
            )
            assert session_inbox.qsize() == 1, (
                "The original payload must be requeued for a later policy "
                "retry instead of being consumed by the failed drain."
            )

            second_drain = await execute_tool(
                tool_name="sys_read_inbox",
                arguments="{}",
                server_client=server_client,
                conversation_id=parent_id,
                session_inbox=session_inbox,
            )
            work_after_second_drain = runner_app.get_subagent_work(child_id)
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)

    assert policy_attempts == 2
    assert "worker:retry-policy returned: SECRET_RETRY_MARKER" in second_drain
    assert work_after_second_drain is None


def test_list_tasks_is_not_runner_local_builtin() -> None:
    """
    ``list_tasks`` is no longer a framework builtin.

    User/local tools may still choose that name, so the runner must not
    claim it as a local lifecycle tool or relay it to native harnesses as
    a framework-owned builtin.
    """
    from omnigent.runner.tool_dispatch import (
        _NATIVE_RELAY_BUILTIN_TOOLS,
        should_dispatch_locally,
    )

    assert should_dispatch_locally("list_tasks") is False
    assert "list_tasks" not in _NATIVE_RELAY_BUILTIN_TOOLS


@pytest.mark.asyncio
async def test_sys_cancel_task_stops_subagent_and_dedupes_late_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``sys_cancel_task`` hard-stops a running claude-native child cleanly.

    The created child carries the ``claude-code-native-ui`` wrapper label, so
    the cancel routes to ``stop_session`` (the claude-native hard-stop). The
    mock server marks the child cancelled when it receives the event, matching
    the synchronous claude-native stop path (``_handle_claude_native_stop``
    kills the pane and reclaims the work entry). A later completion attempt
    must not enqueue a second completed inbox item.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    stops: list[dict[str, Any]] = []

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Serve the child create/message flow and cancellation event."""
        if (
            request.method == "GET"
            and request.url.path == "/v1/sessions/conv_parent_cancel/child_sessions"
        ):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and request.url.path == "/v1/sessions":
            # claude-native sub-agent: the server stamps the wrapper label so
            # the cancel routes to stop_session (the hard-stop path).
            return httpx.Response(
                201,
                json={
                    "id": "conv_child_cancel",
                    "labels": {"omnigent.wrapper": "claude-code-native-ui"},
                },
            )
        if (
            request.method == "POST"
            and request.url.path == "/v1/sessions/conv_child_cancel/events"
        ):
            body = json.loads(request.content)
            if body.get("type") == "stop_session":
                stops.append(body)
                runner_app.mark_subagent_work_terminal(
                    "conv_child_cancel",
                    status="cancelled",
                    output="[System: sub-agent stopped]",
                )
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "agent": "runner",
                        "title": "phase-c",
                        "args": "run phase c",
                    }
                ),
                server_client=server_client,
                conversation_id="conv_parent_cancel",
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="runner")]),
                session_inbox=session_inbox,
            )
            cancel_output = json.loads(
                await execute_tool(
                    tool_name="sys_cancel_task",
                    arguments=json.dumps({"task_id": "conv_child_cancel"}),
                    server_client=server_client,
                    conversation_id="conv_parent_cancel",
                    session_async_tasks={},
                )
            )
            runner_app.mark_subagent_work_terminal(
                "conv_child_cancel",
                status="completed",
                output="SHOULD_NOT_DELIVER",
            )
            inbox_output = await execute_tool(
                tool_name="sys_read_inbox",
                arguments="{}",
                session_inbox=session_inbox,
            )
        finally:
            runner_app.unregister_subagent_work("conv_child_cancel")
            runner_app._session_inboxes_ref.pop("conv_parent_cancel", None)

    # ``data`` rides along because SessionEventInput requires it on older
    # servers — omitting it 422'd sub-agent cancellation in production.
    assert stops == [{"type": "stop_session", "data": {}}]
    assert cancel_output == {
        "cancelled": True,
        "task_id": "conv_child_cancel",
        "status": "cancelled",
    }
    assert inbox_output == (
        "[System: sub-agent task conv_child_cancel cancelled — runner:phase-c]"
    ), "Cancelled sub-agent work must produce exactly one cancelled inbox item."


@pytest.mark.asyncio
async def test_sys_cancel_task_reports_codex_native_cancel_as_best_effort() -> None:
    """
    Unconfirmed codex-native cancel must not promise terminal inbox status.

    Codex-native has no runner-side hard-stop path, so the cancel routes to
    ``interrupt`` (not ``stop_session``, which the runner 204 no-ops for every
    non-claude-native harness). For codex-native that interrupt is itself a
    best-effort no-op — the child can remain running after the POST returns —
    so the tool result must say cancellation is best-effort instead of telling
    the parent to wait forever for a terminal inbox item.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    parent_id = "conv_parent_codex_cancel"
    child_id = "conv_child_codex_cancel"
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="codex_impl",
        title="native",
        wrapper_label="codex-native-ui",
    )
    stops: list[dict[str, Any]] = []

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """
        Accept the interrupt without marking the child terminal (codex no-op).

        :param request: Request sent to the child session events route.
        :returns: Accepted response.
        """
        if request.method == "POST" and request.url.path == f"/v1/sessions/{child_id}/events":
            stops.append(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(404, json={"error": str(request.url)})

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_server_handler),
            base_url="http://server",
        ) as server_client:
            cancel_output = json.loads(
                await execute_tool(
                    tool_name="sys_cancel_task",
                    arguments=json.dumps({"task_id": child_id}),
                    server_client=server_client,
                    conversation_id=parent_id,
                    session_async_tasks={},
                )
            )
    finally:
        runner_app.unregister_subagent_work(child_id)

    # codex-native routes to interrupt, not stop_session (which the runner
    # 204 no-ops for non-claude-native harnesses). ``data`` rides along for
    # SessionEventInput compatibility with older servers.
    assert stops == [{"type": "interrupt", "data": {}}]
    assert cancel_output == {
        "cancel_requested": True,
        "cancel_confirmed": False,
        "best_effort": True,
        "task_id": child_id,
        "status": "launching",
        "message": (
            "Interrupt forwarded, but a runner-side hard-stop is not wired "
            "for codex-native workers yet; the child may keep running and no "
            "terminal inbox status is guaranteed."
        ),
    }


@pytest.mark.asyncio
async def test_sys_cancel_task_interrupts_non_native_subagent() -> None:
    """
    A non-native (in-process) sub-agent cancel must post ``interrupt``.

    In-process harnesses (e.g. ``claude-sdk``) have no wrapper label. The
    runner's ``stop_session`` handler 204 no-ops for them, so posting
    ``stop_session`` would silently leave the child running and the parent
    work entry stuck ``running``. ``interrupt`` is the path they honor —
    ``_interrupted_sessions`` → ``_on_proxy_stream_end`` marks the turn
    cancelled and wakes the parent. This guards against regressing to an
    unconditional ``stop_session`` (which dropped in-process cancellation).
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    parent_id = "conv_parent_inproc_cancel"
    child_id = "conv_child_inproc_cancel"
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="researcher",
        title="analysis",
        wrapper_label=None,  # in-process child has no native wrapper label
    )
    posts: list[dict[str, Any]] = []

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """
        Accept the interrupt; the in-process turn is cancelled out-of-band.

        :param request: Request sent to the child session events route.
        :returns: Accepted response (deferred cancel, child still running).
        """
        if request.method == "POST" and request.url.path == f"/v1/sessions/{child_id}/events":
            posts.append(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(404, json={"error": str(request.url)})

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_server_handler),
            base_url="http://server",
        ) as server_client:
            cancel_output = json.loads(
                await execute_tool(
                    tool_name="sys_cancel_task",
                    arguments=json.dumps({"task_id": child_id}),
                    server_client=server_client,
                    conversation_id=parent_id,
                    session_async_tasks={},
                )
            )
    finally:
        runner_app.unregister_subagent_work(child_id)

    # The regression guard: a non-native child must route to interrupt, never
    # the stop_session that the runner no-ops for non-claude-native harnesses.
    # ``data`` rides along for SessionEventInput compatibility with older servers.
    assert posts == [{"type": "interrupt", "data": {}}]
    # Not codex → generic (non-best-effort) pending result; the terminal
    # status will arrive on the inbox once the interrupted turn ends.
    assert cancel_output == {
        "cancel_requested": True,
        "cancel_confirmed": False,
        "task_id": child_id,
        "status": "launching",
        "message": (
            "Cancel requested; cancellation has not been confirmed yet. "
            "Use sys_read_inbox to observe terminal status."
        ),
    }


def test_session_status_to_task_status_maps_known_values() -> None:
    """
    ``_session_status_to_task_status`` maps a session.status value to the
    child-summary ``current_task_status`` (different vocabularies), and
    returns None for unknown values so the caller omits the field.
    """
    from omnigent.runner.app import _session_status_to_task_status

    assert _session_status_to_task_status("launching") == "launching"
    assert _session_status_to_task_status("running") == "in_progress"
    assert _session_status_to_task_status("waiting") == "in_progress"
    assert _session_status_to_task_status("idle") == "completed"
    assert _session_status_to_task_status("failed") == "failed"
    assert _session_status_to_task_status("bogus") is None


def test_truncate_child_preview_caps_with_ellipsis() -> None:
    """
    ``_truncate_child_preview`` returns short text unchanged and truncates
    text past the cap to exactly the cap + a single ellipsis char (so the
    child rail preview matches the server-side truncation).
    """
    from omnigent.runner.app import _CHILD_PREVIEW_MAX_CHARS, _truncate_child_preview

    assert _truncate_child_preview("hello world") == "hello world"

    long_text = "x" * (_CHILD_PREVIEW_MAX_CHARS + 50)
    out = _truncate_child_preview(long_text)
    assert out.endswith("…")
    assert len(out) == _CHILD_PREVIEW_MAX_CHARS + 1


def test_register_unregister_child_session_roundtrip() -> None:
    """
    ``register_child_session`` stores the parent fan-out metadata and
    ``unregister_child_session`` drops it (used to mirror a child's
    status/preview deltas onto the parent stream).
    """
    from omnigent.runner.app import (
        _child_session_parents,
        register_child_session,
        unregister_child_session,
    )

    child_id = "conv_child_roundtrip_unique"
    register_child_session(
        child_id,
        parent_session_id="conv_parent_roundtrip_unique",
        title="researcher:auth",
        tool="researcher",
        session_name="auth",
    )
    meta = _child_session_parents.get(child_id)
    assert meta is not None
    assert meta.parent_id == "conv_parent_roundtrip_unique"
    assert meta.title == "researcher:auth"
    assert meta.last_busy is None

    unregister_child_session(child_id)
    assert _child_session_parents.get(child_id) is None


# ── sys_session_get_history / _list / _close runner dispatch ────────
#
# These verify the runner-local handler that makes get_history/list/close
# work for harness agents (claude-sdk/codex/openai-agents), whose
# Omnigent tool calls surface as action_required and route through
# the runner — NOT the in-process inner Session. Without this dispatch the
# runner returns "not in local dispatch table"; with it, a harness agent
# reads a sibling's items. The handler calls the Omnigent server's existing
# REST endpoints over a real httpx.AsyncClient backed by MockTransport, so
# the same request/response objects are in play as in production.


def _session_query_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    """
    Build an AsyncClient whose requests are answered by ``handler``.

    :param handler: Maps an ``httpx.Request`` to a canned
        ``httpx.Response`` (routes by method + path).
    :returns: An ``httpx.AsyncClient`` pointed at a fake Omnigent server.
    """
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    )


@pytest.mark.asyncio
async def test_session_list_maps_children_and_skips_closed() -> None:
    """
    ``sys_session_list`` maps ``child_sessions`` rows to
    ``{agent, title, conversation_id}`` and drops closed and
    colonless rows, matching ``SysSessionListTool``.
    """
    from omnigent.runner.tool_dispatch import _execute_session_query_tool

    def handler(request: httpx.Request) -> httpx.Response:
        # Parent-detection snapshot: this caller is top-level (no parent),
        # so there is no main/sibling enrichment — only its own children.
        if request.url.path == "/v1/sessions/conv_parent":
            return httpx.Response(200, json={"id": "conv_parent", "parent_session_id": None})
        assert request.url.path == "/v1/sessions/conv_parent/child_sessions"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "c1",
                        "title": "researcher:auth",
                        "tool": "researcher",
                        "session_name": "auth",
                    },
                    {
                        "id": "c2",
                        "title": "ui:claude-native-ui:1",
                        "tool": "claude-native-ui",
                        "session_name": "1",
                    },
                    {
                        "id": "c3",
                        "title": "researcher:done",
                        "tool": "researcher",
                        "session_name": "done",
                        "labels": {
                            CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE,
                            "attempt": 1,
                        },
                    },
                    {
                        "id": "c5",
                        "title": "researcher:legacy:closed:c5",
                        "tool": "researcher",
                        "session_name": "legacy",
                    },
                    {
                        "id": "c4",
                        "title": "legacy-untyped",
                        "tool": "legacy-untyped",
                        "session_name": None,
                    },
                ],
            },
        )

    async with _session_query_client(handler) as client:
        out = json.loads(
            await _execute_session_query_tool(
                "sys_session_list", "{}", conversation_id="conv_parent", server_client=client
            )
        )
    # c3 (explicitly closed despite its mixed-type label map), c5
    # (legacy title tombstone), and c4
    # (no colon) dropped; the ui:-added child surfaces under its bound
    # agent + label.
    assert out["sub_agents"] == [
        {"agent": "researcher", "title": "auth", "conversation_id": "c1"},
        {"agent": "claude-native-ui", "title": "1", "conversation_id": "c2"},
    ]


@pytest.mark.asyncio
async def test_session_list_adds_main_and_siblings_for_child_caller() -> None:
    """
    When the caller is itself a child (a user-added agent), sys_session_list
    also surfaces ``main`` (its parent) and its siblings — so an added agent
    with no children of its own can still discover the conversation_ids to
    peek. The caller is excluded from its own sibling list.
    """
    from omnigent.runner.tool_dispatch import _execute_session_query_tool

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/sessions/conv_added/child_sessions":
            # The added agent has no children of its own.
            return httpx.Response(200, json={"object": "list", "data": []})
        if path == "/v1/sessions/conv_added":
            # It IS a child of conv_main.
            return httpx.Response(200, json={"id": "conv_added", "parent_session_id": "conv_main"})
        if path == "/v1/sessions/conv_main/child_sessions":
            # main's children = the added agent itself + one sibling.
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "conv_added",
                            "title": "ui:claude-native-ui:1",
                            "tool": "claude-native-ui",
                            "session_name": "1",
                        },
                        {
                            "id": "conv_sib",
                            "title": "researcher:auth",
                            "tool": "researcher",
                            "session_name": "auth",
                        },
                    ],
                },
            )
        raise AssertionError(f"unexpected path {path}")

    async with _session_query_client(handler) as client:
        out = json.loads(
            await _execute_session_query_tool(
                "sys_session_list", "{}", conversation_id="conv_added", server_client=client
            )
        )
    # No own children; gains main (its parent) + the sibling, with itself
    # excluded from the sibling list.
    assert out["sub_agents"] == [
        {"agent": "main", "title": None, "conversation_id": "conv_main"},
        {"agent": "researcher", "title": "auth", "conversation_id": "conv_sib"},
    ]


@pytest.mark.asyncio
async def test_session_peek_returns_chronological_projected_items() -> None:
    """
    ``sys_session_get_history`` reads ``GET /items`` (newest-first), reverses to
    chronological, projects each item, and labels with the target's
    parsed agent/title from its snapshot — matching ``SysSessionGetHistoryTool``.
    """
    from omnigent.runner.tool_dispatch import _execute_session_query_tool

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/conv_target/items":
            assert request.url.params["order"] == "desc"
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "i2",
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "found it"}],
                        },
                        {
                            "id": "i1",
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "where is the bug"}],
                        },
                    ],
                },
            )
        if request.url.path == "/v1/sessions/conv_target":
            return httpx.Response(200, json={"id": "conv_target", "title": "researcher:auth"})
        raise AssertionError(f"unexpected path {request.url.path}")

    async with _session_query_client(handler) as client:
        out = json.loads(
            await _execute_session_query_tool(
                "sys_session_get_history",
                json.dumps({"conversation_id": "conv_target", "tail_items": 5}),
                conversation_id="conv_caller",
                server_client=client,
            )
        )
    assert out["conversation_id"] == "conv_target"
    assert out["agent"] == "researcher"
    assert out["title"] == "auth"
    # Reversed to chronological (user ask first), and message text is
    # extracted from the content blocks — the projection ran.
    assert [(i["role"], i["text"]) for i in out["items"]] == [
        ("user", "where is the bug"),
        ("assistant", "found it"),
    ]


@pytest.mark.asyncio
async def test_session_peek_appends_pending_elicitation_from_snapshot() -> None:
    """
    ``sys_session_get_history`` appends the target's parked elicitations (read
    off its snapshot) after the stored items.

    A parked elicitation never lands in the conversation store, so the
    ``/items`` response ends at the last message. The snapshot's
    ``pending_elicitations`` block is the only place the prompt lives;
    get_history must read it and project a ``pending_elicitation`` item so the
    parent agent isn't blind to a sub-agent awaiting input.
    """
    from omnigent.runner.tool_dispatch import _execute_session_query_tool

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/conv_target/items":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "i1",
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "ask me 3 questions"}],
                        },
                    ],
                },
            )
        if request.url.path == "/v1/sessions/conv_target":
            return httpx.Response(
                200,
                json={
                    "id": "conv_target",
                    "title": "researcher:auth",
                    "pending_elicitations": [
                        {
                            "type": "response.elicitation_request",
                            "elicitation_id": "elicit_bio",
                            "params": {
                                "mode": "form",
                                "message": "Answer 3 questions on human biology",
                                "requestedSchema": {"properties": {"q1": {}, "q2": {}}},
                            },
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    async with _session_query_client(handler) as client:
        out = json.loads(
            await _execute_session_query_tool(
                "sys_session_get_history",
                json.dumps({"conversation_id": "conv_target", "tail_items": 5}),
                conversation_id="conv_caller",
                server_client=client,
            )
        )
    items = out["items"]
    # 2 = 1 stored message + 1 synthesized pending elicitation. If 1,
    # the snapshot's pending_elicitations weren't read/appended and the
    # parent stays blind to the prompt.
    assert len(items) == 2
    # Stored item first (chronological), elicitation appended last.
    assert items[0]["type"] == "message"
    elicit = items[-1]
    assert elicit["type"] == "pending_elicitation"
    assert elicit["elicitation_id"] == "elicit_bio"
    # Prompt + fields show the snapshot payload reached the projector.
    assert elicit["prompt"] == "Answer 3 questions on human biology"
    assert elicit["fields"] == ["q1", "q2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected_error",
    [(404, "session_not_found"), (403, "session_out_of_tree")],
)
async def test_session_peek_maps_access_errors(status: int, expected_error: str) -> None:
    """A 404/403 from ``GET /items`` maps to the in-process tool's typed errors."""
    from omnigent.runner.tool_dispatch import _execute_session_query_tool

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "x"})

    async with _session_query_client(handler) as client:
        out = json.loads(
            await _execute_session_query_tool(
                "sys_session_get_history",
                json.dumps({"conversation_id": "conv_other"}),
                conversation_id="conv_caller",
                server_client=client,
            )
        )
    assert out["error"] == expected_error
    assert out["conversation_id"] == "conv_other"


@pytest.mark.asyncio
async def test_session_close_patches_tombstoned_title() -> None:
    """
    ``sys_session_close`` PATCHes a closed label and internal tombstone.

    The title tombstone frees the DB unique slot so future
    ``sys_session_send`` of the same ``(agent, title)`` creates a
    fresh child. The ``omnigent.closed=true`` label is the
    behavioral marker that direct write paths and clients consume.

    The caller (``conv_caller``) and target (``conv_target``) share the
    same ``root_conversation_id`` and the target is a sub-agent, so the
    tree-scope gate passes and the PATCH is issued.
    """
    # _execute_session_query_tool is the runner's REST dispatch entry point
    # for session-query tools. Its tree-scoping (_session_close_via_rest) is
    # distinct from the in-process path's.
    from omnigent.runner.tool_dispatch import _execute_session_query_tool

    patched: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_target":
            return httpx.Response(
                200,
                json={
                    "id": "conv_target",
                    "title": "researcher:auth",
                    "root_conversation_id": "conv_root",
                    "parent_session_id": "conv_caller",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(
                200,
                json={"id": "conv_caller", "root_conversation_id": "conv_root"},
            )
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_target":
            patched.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "conv_target"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _session_query_client(handler) as client:
        out = json.loads(
            await _execute_session_query_tool(
                "sys_session_close",
                json.dumps({"conversation_id": "conv_target"}),
                conversation_id="conv_caller",
                server_client=client,
            )
        )
    # Tombstone embeds the conv id so repeated closes stay unique, and
    # the explicit label makes the closed state observable without
    # exposing the suffix as UI text.
    assert patched["title"] == "researcher:auth:closed:conv_target"
    assert patched["labels"] == {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE}
    assert out == {
        "closed": True,
        "conversation_id": "conv_target",
        "agent": "researcher",
        "title": "auth",
    }


@pytest.mark.asyncio
async def test_session_close_rejects_out_of_tree_target_without_patch() -> None:
    """
    ``sys_session_close`` refuses a target in a different spawn tree and
    issues NO PATCH.

    Close is a write: the REST path must enforce the same tree-scoping as
    the in-process path. Here the target's ``root_conversation_id``
    (``conv_other_root``) differs from the caller's (``conv_root``), so
    the tool returns ``session_out_of_tree`` and the tombstone PATCH is
    never sent — the target's title is left intact. Without the
    gate, edit access alone would let an agent close a sub-agent in one
    of its other, unrelated trees.
    """
    from omnigent.runner.tool_dispatch import _execute_session_query_tool

    patched = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal patched
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_target":
            return httpx.Response(
                200,
                json={
                    "id": "conv_target",
                    "title": "researcher:auth",
                    "root_conversation_id": "conv_other_root",
                    "parent_session_id": "conv_other_parent",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(
                200,
                json={"id": "conv_caller", "root_conversation_id": "conv_root"},
            )
        if request.method == "PATCH":
            patched = True
            return httpx.Response(200, json={"id": "conv_target"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _session_query_client(handler) as client:
        out = json.loads(
            await _execute_session_query_tool(
                "sys_session_close",
                json.dumps({"conversation_id": "conv_target"}),
                conversation_id="conv_caller",
                server_client=client,
            )
        )
    assert out == {"error": "session_out_of_tree", "conversation_id": "conv_target"}
    # The tombstone write must never have been issued.
    assert patched is False


@pytest.mark.asyncio
async def test_session_close_rejects_top_level_target() -> None:
    """
    ``sys_session_close`` refuses a top-level session (no parent) even
    when it shares the caller's root, and issues no PATCH.

    A top-level session in the caller's own tree (its root) has no
    ``parent_session_id``; close only operates on sub-agents, so the
    tool returns ``session_not_a_sub_agent``.
    """
    from omnigent.runner.tool_dispatch import _execute_session_query_tool

    patched = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal patched
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_root":
            return httpx.Response(
                200,
                json={
                    "id": "conv_root",
                    "title": "some top-level title",
                    "root_conversation_id": "conv_root",
                    "parent_session_id": None,
                },
            )
        if request.method == "PATCH":
            patched = True
            return httpx.Response(200, json={"id": "conv_root"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _session_query_client(handler) as client:
        out = json.loads(
            await _execute_session_query_tool(
                "sys_session_close",
                json.dumps({"conversation_id": "conv_root"}),
                # Caller IS conv_root, so its self-snapshot is the same row.
                conversation_id="conv_root",
                server_client=client,
            )
        )
    assert out == {"error": "session_not_a_sub_agent", "conversation_id": "conv_root"}
    assert patched is False


def test_agent_tools_are_runner_local() -> None:
    """
    ``sys_agent_get`` / ``sys_agent_download`` dispatch locally in the
    runner. If this regresses, native harnesses calling them fall
    through to spec-callable resolution and the orchestrator can't
    inspect or fork agents.
    """
    from omnigent.runner.tool_dispatch import should_dispatch_locally

    assert should_dispatch_locally("sys_agent_get") is True
    assert should_dispatch_locally("sys_agent_download") is True
    assert should_dispatch_locally("sys_agent_list") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [
        pytest.param("sys_agent_get", id="get"),
        pytest.param("sys_agent_download", id="download"),
    ],
)
async def test_agent_tools_map_404_to_agent_not_found(
    tool_name: str,
    tmp_path: Path,
) -> None:
    """
    Both agent tools map a 404 to ``agent_not_found`` — the orchestrator
    gets a typed reason instead of a raw status. If the mapping
    regressed, it couldn't tell "no such agent/session" from a transport
    error.

    :param tool_name: The agent tool under test.
    :param tmp_path: Workspace dir (only the download path needs it).
    """
    from omnigent.runner.tool_dispatch import execute_tool

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "missing"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name=tool_name,
            arguments=json.dumps({"session_id": "conv_missing"}),
            server_client=server_client,
            conversation_id="conv_caller",
            runner_workspace=tmp_path,
        )

    info = json.loads(output)
    assert info["error"] == "agent_not_found"
    assert info["session_id"] == "conv_missing"


@pytest.mark.parametrize(
    "opt_in, expected_writes",
    [
        pytest.param("none", set(), id="no-opt-in"),
        pytest.param(
            "agents",
            {"sys_session_send", "sys_session_close"},
            id="declared-agents",
        ),
        pytest.param(
            "spawn",
            {"sys_session_send", "sys_session_close", "sys_session_create"},
            id="spawn-flag",
        ),
    ],
)
def test_native_relay_builtin_set_matches_toolmanager_gating(
    opt_in: str,
    expected_writes: set[str],
) -> None:
    """
    The native relay advertises exactly ``ToolManager``'s builtin schemas
    intersected with ``_NATIVE_RELAY_BUILTIN_TOOLS``.

    claude-native / codex-native ignore the harness ``tools`` list, so the
    relay is their only tool surface; ``_ensure_comment_relay_started``
    applies this same ``ToolManager(spec).get_tool_schemas()`` ∩
    ``_NATIVE_RELAY_BUILTIN_TOOLS`` filter. This locks two invariants:

    - **Parity**: the always-on orchestrator/discovery surface (agent
      reads, session reads, async inbox reads/cancels, comment tools)
      reaches native harnesses.
    - **Gating fidelity**: the spawn writes are relayed per the two
      distinct grants, matching what non-native harnesses get via
      ``request.tools``. ``tools.agents`` permits only the declared
      sub-agent list (send + close, NO create); ``spawn: true`` (set
      by the native wrapper specs) additionally grants create —
      launching arbitrary agents or custom bundles. A regressed gate
      either strands an opted-in native agent or hands an un-opted
      agent the spawn surface.

    :param opt_in: Which opt-in arm the spec uses — ``"none"``,
        ``"agents"`` (declared ``tools.agents``), or ``"spawn"``
        (top-level ``spawn: true``).
    :param expected_writes: Exact spawn-write tool names expected in
        the relayed set for this opt-in arm.
    """
    from omnigent.runner.tool_dispatch import _NATIVE_RELAY_BUILTIN_TOOLS
    from omnigent.spec.types import ToolsConfig
    from omnigent.tools.manager import ToolManager

    if opt_in == "agents":
        spec = AgentSpec(
            spec_version=1,
            tools=ToolsConfig(agents=["researcher"]),
            sub_agents=[AgentSpec(spec_version=1, name="researcher")],
        )
    elif opt_in == "spawn":
        spec = AgentSpec(spec_version=1, spawn=True)
    else:
        spec = AgentSpec(spec_version=1)
    schema_names = {s["function"]["name"] for s in ToolManager(spec).get_tool_schemas()}
    relayed = schema_names & _NATIVE_RELAY_BUILTIN_TOOLS

    # Always-on reads/discovery reach every native agent — if any is
    # missing, the orchestrator running under claude-native can't list or
    # inspect agents/sessions.
    assert {"sys_agent_get", "sys_agent_download", "sys_agent_list"} <= relayed
    assert {"sys_session_list", "sys_session_get_history", "sys_session_get_info"} <= relayed
    assert {"sys_call_async", "sys_read_inbox", "sys_cancel_async", "sys_cancel_task"} <= relayed
    assert {"list_comments", "update_comment"} <= relayed

    # Exact-set check on the writes: an extra name means a grant leaked
    # beyond its arm (e.g. create from tools.agents alone — letting a
    # whitelisted-sub-agents spec launch arbitrary bundles); a missing
    # name strands an opted-in agent.
    spawn_writes = {"sys_session_send", "sys_session_close", "sys_session_create"}
    assert relayed & spawn_writes == expected_writes
    # Model awareness rides the dispatch grant: relayed iff send is.
    assert ("sys_list_models" in relayed) == ("sys_session_send" in expected_writes)
    # Advise-models also requires a routing client; default caps have none.
    assert "sys_advise_models" not in relayed

    # OS tools ride a separate unconditional relay path (overriding the
    # bridge's static versions), so they must never be in the builtin set —
    # otherwise they'd be double-advertised and bypass that override.
    os_tools = {"sys_os_read", "sys_os_write", "sys_os_edit", "sys_os_shell"}
    assert not (os_tools & _NATIVE_RELAY_BUILTIN_TOOLS)


@pytest.mark.parametrize(
    "declares_terminals, expected_terminal_tools",
    [
        pytest.param(
            True,
            {
                "sys_terminal_launch",
                "sys_terminal_send",
                "sys_terminal_read",
                "sys_terminal_list",
                "sys_terminal_close",
            },
            id="terminals-declared",
        ),
        pytest.param(False, set(), id="no-terminals"),
    ],
)
def test_native_relay_advertises_terminal_tools_per_spec_gate(
    declares_terminals: bool,
    expected_terminal_tools: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The native relay advertises ``sys_terminal_*`` iff the spec declares
    ``terminals``.

    claude-native / codex-native ignore the harness ``tools`` list, so
    the relay (``ToolManager(spec).get_tool_schemas()`` ∩
    ``_NATIVE_RELAY_BUILTIN_TOOLS``, applied by
    ``_ensure_comment_relay_started``) is the ONLY way the five
    terminal tools reach the real CLI. This locks both directions of
    the gate:

    - **Advertised when granted**: a spec with a ``terminals:`` block
      must relay all five tools — a missing name strands a native
      agent that was granted terminals.
    - **Withheld when not granted**: a spec without ``terminals:``
      must relay none — a leaked name hands tmux access to an
      un-opted agent.

    :param declares_terminals: Whether the spec carries a
        ``terminals:`` block.
    :param expected_terminal_tools: Exact ``sys_terminal_*`` names
        expected in the relayed set for this arm.
    :param monkeypatch: Pytest monkeypatch fixture, used to install a
        fresh :class:`TerminalRegistry` singleton so ToolManager's
        terminal-tool registration (which looks it up via
        ``get_terminal_registry()``) works without runtime ``init()``.
    """
    from omnigent.inner.datamodel import TerminalEnvSpec
    from omnigent.runner.tool_dispatch import _NATIVE_RELAY_BUILTIN_TOOLS
    from omnigent.runtime import _globals as rt_globals
    from omnigent.terminals.registry import TerminalRegistry
    from omnigent.tools.manager import ToolManager

    monkeypatch.setattr(rt_globals, "_terminal_registry", TerminalRegistry())

    terminals = {"bash": TerminalEnvSpec(command="bash")} if declares_terminals else None
    spec = AgentSpec(spec_version=1, terminals=terminals)

    schema_names = {s["function"]["name"] for s in ToolManager(spec).get_tool_schemas()}
    relayed = schema_names & _NATIVE_RELAY_BUILTIN_TOOLS

    all_terminal_tools = {
        "sys_terminal_launch",
        "sys_terminal_send",
        "sys_terminal_read",
        "sys_terminal_list",
        "sys_terminal_close",
    }
    # Exact-set check on the terminal family: a missing name means the
    # relay filter dropped a granted tool (set regression in
    # _NATIVE_RELAY_BUILTIN_TOOLS); an extra name on the no-terminals
    # arm means ToolManager registered terminal tools without the spec
    # gate (registration regression).
    assert relayed & all_terminal_tools == expected_terminal_tools


def test_session_create_is_runner_local() -> None:
    """
    ``sys_session_create`` dispatches locally in the runner. If it
    regresses out of the local table, a native harness calling it falls
    through to spec-callable resolution and the orchestrator can't spawn
    child sessions.
    """
    from omnigent.runner.tool_dispatch import should_dispatch_locally

    assert should_dispatch_locally("sys_session_create") is True


@pytest.mark.asyncio
async def test_session_list_global_sessions_filter_and_connectivity() -> None:
    """
    The global ``sessions`` view fetches GET /v1/sessions (forwarding the
    ``agent_name`` filter), projects each row, and annotates
    ``runner_online`` by checking each UNIQUE runner once: the
    agent_name filter reaches the server; sessions are projected with
    status + parentage; and connectivity is folded in without a
    per-session status fan-out (two sessions share runner r1 → exactly
    one /v1/runners/r1/status call).
    """
    from omnigent.runner.tool_dispatch import _execute_session_query_tool

    runner_status_calls: list[str] = []
    sessions_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/sessions/conv_x/child_sessions":
            return httpx.Response(200, json={"object": "list", "data": []})
        if path == "/v1/sessions/conv_x":
            return httpx.Response(200, json={"id": "conv_x", "parent_session_id": None})
        if path == "/v1/sessions":
            sessions_params.update(dict(request.url.params))
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "s1",
                            "agent_name": "researcher",
                            "title": "auth",
                            "status": "running",
                            "runner_id": "r1",
                            "parent_session_id": None,
                        },
                        {
                            "id": "s2",
                            "agent_name": "researcher",
                            "title": "payments",
                            "status": "idle",
                            "runner_id": "r1",
                            "parent_session_id": None,
                        },
                    ],
                },
            )
        if path == "/v1/runners/r1/status":
            runner_status_calls.append("r1")
            return httpx.Response(200, json={"runner_id": "r1", "online": True})
        raise AssertionError(f"unexpected path {path}")

    async with _session_query_client(handler) as client:
        out = json.loads(
            await _execute_session_query_tool(
                "sys_session_list",
                json.dumps({"agent_name": "researcher"}),
                conversation_id="conv_x",
                server_client=client,
            )
        )

    # agent_name forwarded to the server-side filter.
    assert sessions_params.get("agent_name") == "researcher"
    # Both sessions projected with status + connectivity from the single
    # shared-runner status lookup.
    assert out["sessions"] == [
        {
            "session_id": "s1",
            "agent_name": "researcher",
            "title": "auth",
            "status": "running",
            "runner_id": "r1",
            "runner_online": True,
            "parent_session_id": None,
        },
        {
            "session_id": "s2",
            "agent_name": "researcher",
            "title": "payments",
            "status": "idle",
            "runner_id": "r1",
            "runner_online": True,
            "parent_session_id": None,
        },
    ]
    # Connectivity resolved once per UNIQUE runner — two sessions share
    # r1, so exactly one status call (not one per session). A count of 2
    # would mean the dedup regressed into a per-session fan-out.
    assert runner_status_calls == ["r1"]


@pytest.mark.asyncio
async def test_sys_agent_download_rejects_path_in_dest_filename(tmp_path: Path) -> None:
    """
    ``sys_agent_download`` rejects a ``dest_filename`` containing a path
    separator (a traversal attempt) and writes nothing. If the guard
    regressed, a bundle could be written outside the working directory.

    :param tmp_path: Pytest temp dir used as the runner workspace.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"data",
            headers={"X-Agent-Name": "a", "X-Agent-Version": "1"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_agent_download",
            arguments=json.dumps({"session_id": "conv_x", "dest_filename": "../escape.tar.gz"}),
            server_client=server_client,
            conversation_id="conv_caller",
            runner_workspace=tmp_path,
        )

    info = json.loads(output)
    assert "error" in info
    # Nothing was written anywhere under the workspace.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_sys_agent_download_rejects_symlink_escape_from_cwd(tmp_path: Path) -> None:
    """
    ``sys_agent_download`` refuses to follow a symlink that redirects the
    bundle write outside the os_env cwd. ``dest_filename`` is a bare name,
    but if the cwd already holds a symlink of that name pointing elsewhere,
    a naive ``write_bytes`` would clobber the symlink target outside the
    sandbox. The realpath-containment guard must catch it and
    write nothing to the outside target.

    :param tmp_path: Pytest temp dir; holds both the workspace and an
        outside directory the symlink points at.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_target = outside / "stolen.tar.gz"
    # A symlink inside the workspace whose name matches the caller's
    # dest_filename but whose target escapes the workspace.
    (workspace / "escape.tar.gz").symlink_to(outside_target)

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"payload",
            headers={"X-Agent-Name": "a", "X-Agent-Version": "1"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_agent_download",
            arguments=json.dumps({"session_id": "conv_x", "dest_filename": "escape.tar.gz"}),
            server_client=server_client,
            conversation_id="conv_caller",
            runner_workspace=workspace,
        )

    info = json.loads(output)
    assert "error" in info
    # The outside target was never created — the guard blocked the write.
    assert not outside_target.exists()


@pytest.mark.asyncio
async def test_sys_agent_download_writes_bundle_to_workspace(tmp_path: Path) -> None:
    """
    ``sys_agent_download`` writes the fetched ``.tar.gz`` bytes into the
    agent's os_env cwd (here ``runner_workspace`` = tmp_path) and returns
    the path. The full path: fetch bytes, derive the default
    filename from the X-Agent-* headers, and persist to the agent-visible
    disk. If the write regressed, the file wouldn't exist or the bytes
    wouldn't match.

    :param tmp_path: Pytest temp dir used as the runner workspace, so the
        resolved os_env cwd is a real local directory.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    bundle_bytes = b"\x1f\x8b\x08fake-tar-gz-bytes"

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/conv_x/agent/contents":
            return httpx.Response(
                200,
                content=bundle_bytes,
                headers={"X-Agent-Name": "my agent", "X-Agent-Version": "5"},
            )
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_agent_download",
            arguments=json.dumps({"session_id": "conv_x"}),
            server_client=server_client,
            conversation_id="conv_caller",
            runner_workspace=tmp_path,
        )

    info = json.loads(output)
    # Default filename: agent name sanitized (space → "_") + version.
    expected = tmp_path / "my_agent-v5.tar.gz"
    assert info["path"] == str(expected)
    assert info["bytes_written"] == len(bundle_bytes)
    # The bundle actually landed on disk with the exact bytes — a
    # mismatch means the write path or byte handling broke.
    assert expected.read_bytes() == bundle_bytes


@pytest.mark.asyncio
async def test_sys_agent_get_projects_agent_metadata() -> None:
    """
    ``sys_agent_get`` projects ``GET /v1/sessions/{id}/agent`` into the
    orchestrator-facing fields: agent_id, name, version, description,
    harness, MCP server summaries, and policy summaries. If the
    projection dropped a field or used the wrong key, the asserted
    values would differ.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_x/agent":
            return httpx.Response(
                200,
                json={
                    "id": "ag_777",
                    "object": "agent",
                    "name": "researcher",
                    "version": 4,
                    "description": "Finds things",
                    "created_at": 1,
                    "harness": "claude-sdk",
                    "mcp_servers": [{"name": "fs", "transport": "stdio", "args": []}],
                    "policies": [{"name": "guard", "type": "label", "on": ["input"]}],
                },
            )
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_agent_get",
            arguments=json.dumps({"session_id": "conv_x"}),
            server_client=server_client,
            conversation_id="conv_caller",
        )

    info = json.loads(output)
    # agent_id comes from AgentObject.id (not a top-level "agent_id").
    assert info["agent_id"] == "ag_777"
    assert info["name"] == "researcher"
    assert info["version"] == 4
    assert info["harness"] == "claude-sdk"
    # MCP/policy summaries pass through as-is so the orchestrator sees
    # the agent's tool/guardrail surface.
    assert info["mcp_servers"] == [{"name": "fs", "transport": "stdio", "args": []}]
    assert info["policies"] == [{"name": "guard", "type": "label", "on": ["input"]}]


@pytest.mark.asyncio
async def test_sys_agent_list_degrades_when_sources_fail(tmp_path: Path) -> None:
    """
    A failing source degrades to an empty section rather than failing the
    whole call. Here the server 500s both list endpoints and no local
    config dir exists, so all three sections come back empty — but the
    tool still returns a well-formed result. If a source error
    propagated, the tool would return an ``error`` instead.

    :param tmp_path: Workspace dir with no agent-config subdir.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_agent_list",
            arguments="{}",
            server_client=server_client,
            conversation_id="conv_caller",
            runner_workspace=tmp_path,
        )

    info = json.loads(output)
    assert info == {"builtins": [], "session_agents": [], "local_configs": []}


@pytest.mark.asyncio
async def test_sys_agent_list_merges_three_sources(tmp_path: Path) -> None:
    """
    ``sys_agent_list`` merges built-ins (GET /v1/agents), session-bound
    agents (GET /v1/sessions), and locally-authored config YAMLs (a scan
    of the os_env cwd's agent-config subdir). All three sources
    are fetched and projected: a built-in's id/name, a session's
    session_id+agent binding, and a local config's name/path from the
    YAML on disk. If any source were dropped or mis-projected, the
    corresponding section would be empty or wrong.

    :param tmp_path: Pytest temp dir used as the runner workspace, so the
        local-config scan reads a real directory.
    """
    from omnigent.runner.tool_dispatch import _AGENT_CONFIG_SUBDIR, execute_tool

    # Author a local config on disk so the scan has something to find.
    configs_dir = tmp_path / _AGENT_CONFIG_SUBDIR
    configs_dir.mkdir(parents=True)
    (configs_dir / "my-agent.yaml").write_text(
        "name: my-agent\ndescription: a local one\nprompt: hi\n", encoding="utf-8"
    )

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return httpx.Response(
                200,
                json={"data": [{"id": "ag_b", "name": "claude-native-ui", "harness": "claude"}]},
            )
        if request.url.path == "/v1/sessions":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "conv_1",
                            "agent_id": "ag_s",
                            "agent_name": "nessie",
                            "status": "idle",
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_agent_list",
            arguments="{}",
            server_client=server_client,
            conversation_id="conv_caller",
            runner_workspace=tmp_path,
        )

    info = json.loads(output)
    # Built-ins projected from GET /v1/agents (id → agent_id).
    assert info["builtins"] == [
        {"agent_id": "ag_b", "name": "claude-native-ui", "description": None, "harness": "claude"}
    ]
    # Session-bound agents carry session_id so the caller can then
    # sys_agent_get / sys_agent_download them.
    assert info["session_agents"] == [
        {"session_id": "conv_1", "agent_id": "ag_s", "agent_name": "nessie", "status": "idle"}
    ]
    # Local config discovered by the on-disk scan, with its parsed name.
    assert len(info["local_configs"]) == 1
    assert info["local_configs"][0]["name"] == "my-agent"
    assert info["local_configs"][0]["description"] == "a local one"
    assert info["local_configs"][0]["path"].endswith("my-agent.yaml")


@pytest.mark.asyncio
async def test_sys_session_create_maps_agent_not_found() -> None:
    """
    A 404 from the create maps to ``agent_not_found`` so the LLM gets a
    typed reason rather than a raw status. If the mapping regressed, the
    orchestrator couldn't tell a bad agent_id from a transport failure.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no agent"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"agent_id": "ag_missing"}),
            server_client=server_client,
            conversation_id="conv_caller",
        )

    info = json.loads(output)
    assert info["error"] == "agent_not_found"
    assert info["agent_id"] == "ag_missing"


@pytest.mark.asyncio
async def test_sys_session_create_spawns_child_under_caller() -> None:
    """
    ``sys_session_create`` POSTs a JSON create with
    ``parent_session_id`` forced to the caller (child-only), passes the
    agent_id, title, and a queued initial message, and returns a handle
    carrying the new child's id. If parent_session_id weren't forced to
    the caller, an orchestrator could create top-level/sibling sessions —
    so the asserted request body is the security-critical check.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    captured: dict[str, Any] = {}

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions":
            captured.update(json.loads(request.content))
            return httpx.Response(
                201,
                json={
                    "id": "conv_child",
                    "agent_id": "ag_x",
                    "agent_name": "researcher",
                    "status": "idle",
                },
            )
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"agent_id": "ag_x", "title": "auth", "message": "start"}),
            server_client=server_client,
            conversation_id="conv_caller",
        )

    # Child-only: parent forced to the caller; agent + title + queued
    # message threaded through to the create body.
    assert captured["parent_session_id"] == "conv_caller"
    assert captured["agent_id"] == "ag_x"
    assert captured["title"] == "auth"
    assert captured["initial_items"][0]["data"]["content"][0]["text"] == "start"
    handle = json.loads(output)
    assert handle["conversation_id"] == "conv_child"
    assert handle["agent_id"] == "ag_x"
    assert handle["agent_name"] == "researcher"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        # Both modes at once: the two create different agents, so the
        # handler must refuse rather than silently pick one.
        {"agent_id": "ag_x", "config_path": "helper.yaml"},
        # Neither mode: nothing to launch.
        {"title": "auth"},
    ],
)
async def test_sys_session_create_requires_exactly_one_mode(
    arguments: dict[str, Any],
) -> None:
    """
    ``sys_session_create`` rejects both-or-neither of ``agent_id`` /
    ``config_path`` without touching the server.

    If the mode split regressed to a silent preference, an orchestrator
    passing both could launch the wrong agent with no signal.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"server must not be reached on invalid mode args: {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps(arguments),
            server_client=server_client,
            conversation_id="conv_caller",
        )

    info = json.loads(output)
    assert "exactly one of 'agent_id'" in info["error"]


def _parse_multipart_create(request: httpx.Request) -> dict[str, Any]:
    """
    Decode a captured multipart ``POST /v1/sessions`` request body.

    Uses the stdlib email parser (multipart/form-data is MIME) rather
    than hand-rolled boundary splitting.

    :param request: The captured httpx request.
    :returns: Dict with ``metadata`` (parsed JSON dict) and ``bundle``
        (raw bytes of the uploaded file part).
    """
    import email
    import email.policy

    header = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = email.message_from_bytes(header + request.content, policy=email.policy.HTTP)
    parts: dict[str, Any] = {}
    for part in message.iter_parts():  # type: ignore[attr-defined]
        disposition = part.get("Content-Disposition", "")
        payload = part.get_payload(decode=True)
        if 'name="metadata"' in disposition:
            parts["metadata"] = json.loads(payload.decode())
        elif 'name="bundle"' in disposition:
            parts["bundle"] = payload
    return parts


@pytest.mark.asyncio
async def test_sys_session_create_bundle_mode_uploads_child_under_caller(
    tmp_path: Path,
) -> None:
    """
    Bundle mode bundles a local agent config, POSTs the multipart
    create with ``parent_session_id`` forced to the caller, queues the
    optional message as the child's first event, and returns a handle
    built from the server's created-agent identifiers.

    The asserted multipart metadata is the security-critical check
    (child-only, mirroring agent_id mode); the tarball content check
    the local config actually traversed bundling — an empty
    bundle would create a session the server rejects at first turn.
    """
    import io
    import tarfile

    from omnigent.runner.tool_dispatch import execute_tool

    config_text = "name: helper\nprompt: do helpful things\n"
    (tmp_path / "helper.yaml").write_text(config_text)

    create_requests: list[httpx.Request] = []
    event_bodies: list[dict[str, Any]] = []

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions":
            create_requests.append(request)
            return httpx.Response(
                201,
                json={
                    "session_id": "conv_child",
                    "agent_id": "ag_new",
                    "agent_name": "helper",
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions/conv_child/events":
            event_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps(
                {"config_path": "helper.yaml", "title": "auth", "message": "start"}
            ),
            server_client=server_client,
            conversation_id="conv_caller",
            runner_workspace=tmp_path,
        )

    # Exactly one multipart create; parent forced to the caller
    # (child-only) and the title threaded into the metadata part.
    assert len(create_requests) == 1, (
        f"expected exactly one create POST, got {len(create_requests)}"
    )
    parts = _parse_multipart_create(create_requests[0])
    assert parts["metadata"] == {"parent_session_id": "conv_caller", "title": "auth"}

    # The uploaded bundle is a gzipped tar holding the authored config
    # verbatim — the local file traversed materialize → tar.
    with tarfile.open(fileobj=io.BytesIO(parts["bundle"]), mode="r:gz") as tf:
        names = tf.getnames()
        assert names == ["helper.yaml"]
        member = tf.extractfile("helper.yaml")
        assert member is not None
        assert member.read().decode() == config_text

    # The optional message was queued as the child's first user event —
    # this is what starts the child's turn (same pattern as named send).
    assert len(event_bodies) == 1
    assert event_bodies[0]["data"]["content"][0]["text"] == "start"

    handle = json.loads(output)
    assert handle["conversation_id"] == "conv_child"
    # agent_id/agent_name come from the server's CreatedSessionResponse,
    # not the caller's args — the orchestrator needs the NEW agent's id.
    assert handle["agent_id"] == "ag_new"
    assert handle["agent_name"] == "helper"


@pytest.mark.asyncio
async def test_sys_session_create_config_path_escape_rejected(
    tmp_path: Path,
) -> None:
    """
    A ``config_path`` resolving outside the working directory is
    refused before any disk read or server call.

    This mirrors the sys_agent_download containment guard:
    without it, an orchestrator could exfiltrate arbitrary host files
    by bundling them into an uploaded agent.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    workdir = tmp_path / "work"
    workdir.mkdir()
    (tmp_path / "outside.yaml").write_text("name: outside\n")

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"server must not be reached on escape: {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"config_path": "../outside.yaml"}),
            server_client=server_client,
            conversation_id="conv_caller",
            runner_workspace=workdir,
        )

    info = json.loads(output)
    assert "escapes the working directory" in info["error"]


@pytest.mark.asyncio
async def test_sys_session_create_config_not_found(tmp_path: Path) -> None:
    """
    A missing ``config_path`` returns the typed ``config_not_found``
    error so the LLM can distinguish a bad path from a transport
    failure, without any server call.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"server must not be reached: {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_create",
            arguments=json.dumps({"config_path": "nope.yaml"}),
            server_client=server_client,
            conversation_id="conv_caller",
            runner_workspace=tmp_path,
        )

    info = json.loads(output)
    assert info["error"] == "config_not_found"
    assert info["config_path"] == "nope.yaml"


@pytest.mark.asyncio
async def test_sys_session_get_info_defaults_to_caller_session() -> None:
    """
    Omitting ``session_id`` describes the caller's own session — the
    runner targets ``GET /v1/sessions/{conversation_id}``. With no
    runner bound, connectivity is unknown (``None``) and no
    runner-status call is made. If the default-to-caller logic
    regressed, the request path would be wrong and the GET would 404.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    requested_paths: list[str] = []

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(
                200,
                json={
                    "id": "conv_caller",
                    "agent_id": "ag_self",
                    "agent_name": "main",
                    "status": "idle",
                    "created_at": 1,
                    "runner_id": None,
                    "pending_elicitations": [],
                },
            )
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_get_info",
            arguments="{}",
            server_client=server_client,
            conversation_id="conv_caller",
        )

    info = json.loads(output)
    assert info["session_id"] == "conv_caller"
    # No runner bound → connectivity unknown, and the status endpoint is
    # never queried (a stray /v1/runners call would mean the None-runner
    # short-circuit regressed).
    assert info["runner_online"] is None
    assert info["pending_elicitations"] == []
    assert info["pending_elicitation_count"] == 0
    assert not any(p.startswith("/v1/runners") for p in requested_paths)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_code,expected_error",
    [
        pytest.param(404, "session_not_found", id="not-found"),
        pytest.param(403, "access_denied", id="forbidden"),
        pytest.param(401, "access_denied", id="unauthorized"),
    ],
)
async def test_sys_session_get_info_maps_error_statuses(
    status_code: int,
    expected_error: str,
) -> None:
    """
    A 404 maps to ``session_not_found``; 401/403 map to
    ``access_denied`` — so the LLM gets a typed reason instead of a raw
    HTTP status. If the mapping regressed, the orchestrator couldn't
    distinguish "no such session" from "you can't read it".

    :param status_code: HTTP status the mocked Omnigent server returns.
    :param expected_error: The typed error string the tool should emit.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "x"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_get_info",
            arguments=json.dumps({"session_id": "conv_missing"}),
            server_client=server_client,
            conversation_id="conv_caller",
        )

    info = json.loads(output)
    assert info["error"] == expected_error
    assert info["session_id"] == "conv_missing"


@pytest.mark.asyncio
async def test_sys_session_share_defaults_to_caller_and_puts_grant() -> None:
    """
    Omitting ``session_id`` shares the caller's own session: the runner
    PUTs to ``/v1/sessions/{conversation_id}/permissions`` with the
    grantee and the numeric level mapped from the friendly name. If the
    default-to-caller logic or the name->level mapping regressed, the
    request path or body would be wrong (and an agent's "share this
    session" would silently hit the wrong session or wrong level).
    """
    from omnigent.runner.tool_dispatch import execute_tool

    requests: list[tuple[str, str, dict[str, Any]]] = []

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={"user_id": "alice@example.com", "conversation_id": "conv_caller", "level": 2},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_share",
            arguments=json.dumps({"user_id": "alice@example.com", "level": "edit"}),
            server_client=server_client,
            conversation_id="conv_caller",
            # Sharing a named user only needs the non-public tier enabled.
            agent_spec=AgentSpec(spec_version=1, agent_session_sharing=SharePolicy.NON_PUBLIC),
        )

    # Exactly one PUT to the caller's own permissions sub-resource, with
    # level "edit" mapped to the server's numeric 2 (1=read/2=edit/3=manage).
    assert requests == [
        (
            "PUT",
            "/v1/sessions/conv_caller/permissions",
            {"user_id": "alice@example.com", "level": 2},
        )
    ]
    result = json.loads(output)
    assert result == {
        "shared": True,
        "session_id": "conv_caller",
        "user_id": "alice@example.com",
        "level": "edit",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_code,expected_error",
    [
        pytest.param(404, "session_not_found", id="not-found"),
        pytest.param(403, "access_denied", id="forbidden"),
        pytest.param(401, "access_denied", id="unauthorized"),
    ],
)
async def test_sys_session_share_maps_error_statuses(
    status_code: int,
    expected_error: str,
) -> None:
    """
    A 404 maps to ``session_not_found``; 401/403 map to ``access_denied``
    — a typed reason instead of a raw status, matching the sibling
    session tools so the LLM can distinguish "no such session" from
    "you can't manage it".

    :param status_code: HTTP status the mocked Omnigent server returns.
    :param expected_error: The typed error string the tool should emit.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "x"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_share",
            arguments=json.dumps({"user_id": "alice@example.com", "session_id": "conv_x"}),
            server_client=server_client,
            conversation_id="conv_caller",
            agent_spec=AgentSpec(spec_version=1, agent_session_sharing=SharePolicy.NON_PUBLIC),
        )

    result = json.loads(output)
    assert result["error"] == expected_error
    assert result["session_id"] == "conv_x"


@pytest.mark.asyncio
async def test_sys_session_share_rejects_bad_level_without_calling_server() -> None:
    """
    An unknown ``level`` is rejected client-side before any PUT — so a
    typo can't fall through to the server or silently skip the grant. A
    request reaching the handler would mean the level validation
    regressed.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    called = False

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_share",
            arguments=json.dumps({"user_id": "alice@example.com", "level": "admin"}),
            server_client=server_client,
            conversation_id="conv_caller",
            # Share enabled (non-public) so the call reaches level validation.
            agent_spec=AgentSpec(spec_version=1, agent_session_sharing=SharePolicy.NON_PUBLIC),
        )

    assert called is False  # validation must short-circuit before the PUT
    assert "level must be one of" in json.loads(output)["error"]


@pytest.mark.asyncio
async def test_sys_session_share_surfaces_server_message_on_4xx() -> None:
    """
    A 4xx the typed branches don't claim (here the server's 400 for a
    ``__public__`` grant above read level) surfaces the server's own
    ``{"error": {"message": ...}}`` text rather than a bare "returned
    400". If the detail-extraction regressed, the agent would see only
    the status code and couldn't tell that public is read-only — the
    exact actionable reason the server gave.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    # Mirrors the OmnigentError envelope the server's exception handler
    # emits (omnigent/server/app.py) for the public + level>read guard.
    server_message = "Public access is limited to read-only (level 1)"

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"code": "INVALID_INPUT", "message": server_message}}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_share",
            arguments=json.dumps(
                {"user_id": "__public__", "level": "edit", "session_id": "conv_x"}
            ),
            server_client=server_client,
            conversation_id="conv_caller",
            # agent_session_sharing: public lets __public__ pass the runner
            # gate and reach the server, which rejects level>read for public.
            agent_spec=AgentSpec(spec_version=1, agent_session_sharing=SharePolicy.PUBLIC),
        )

    result = json.loads(output)
    # The server's verbatim message is surfaced, not flattened to a status.
    assert result["error"] == server_message
    assert result["status_code"] == 400
    assert result["session_id"] == "conv_x"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "share_policy",
    [
        pytest.param(None, id="no-spec"),
        pytest.param(SharePolicy.NONE, id="share-none"),
    ],
)
async def test_sys_session_share_disabled_without_share_flag(
    share_policy: SharePolicy | None,
) -> None:
    """
    With no spec (``None``) or ``agent_session_sharing: none``, the
    runner refuses the grant client-side and never PUTs — the
    ``agent_session_sharing`` flag is the real gate, not just tool
    advertisement, so a prompt-injected call naming the tool can't
    escalate. A PUT reaching the handler would mean the runner-side
    policy gate regressed.

    :param share_policy: The spec's ``agent_session_sharing`` policy
        under test (or ``None`` for a missing spec).
    """
    from omnigent.runner.tool_dispatch import execute_tool

    called = False

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    spec = (
        None
        if share_policy is None
        else AgentSpec(spec_version=1, agent_session_sharing=share_policy)
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_share",
            arguments=json.dumps({"user_id": "alice@example.com", "session_id": "conv_x"}),
            server_client=server_client,
            conversation_id="conv_caller",
            agent_spec=spec,
        )

    assert called is False  # the gate must short-circuit before the PUT
    assert "not enabled" in json.loads(output)["error"]


@pytest.mark.asyncio
async def test_sys_session_share_non_public_rejects_public_grant() -> None:
    """
    Under ``agent_session_sharing: non-public`` a grant to a named user
    is allowed, but a ``__public__`` grant is refused client-side before
    any PUT — the
    non-public tier must not be able to expose the transcript anonymously
    even if the model (or an injection) asks for it. A PUT here would
    mean the public sub-gate regressed.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    called = False

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_share",
            arguments=json.dumps({"user_id": "__public__", "session_id": "conv_x"}),
            server_client=server_client,
            conversation_id="conv_caller",
            agent_spec=AgentSpec(spec_version=1, agent_session_sharing=SharePolicy.NON_PUBLIC),
        )

    assert called is False  # public sub-gate must short-circuit before the PUT
    assert "public" in json.loads(output)["error"]


@pytest.mark.asyncio
async def test_sys_session_share_public_allows_public_grant() -> None:
    """
    Under ``agent_session_sharing: public`` a ``__public__`` read grant
    passes the runner gate and PUTs to the permissions endpoint — the
    positive case the
    non-public/none gates exclude. If the gate wrongly blocked it, public
    sharing would be impossible even when the spec explicitly opts in.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    requests: list[tuple[str, str, dict[str, Any]]] = []

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={"user_id": "__public__", "conversation_id": "conv_caller", "level": 1},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_share",
            arguments=json.dumps({"user_id": "__public__"}),
            server_client=server_client,
            conversation_id="conv_caller",
            agent_spec=AgentSpec(spec_version=1, agent_session_sharing=SharePolicy.PUBLIC),
        )

    # __public__ reached the server as a level-1 (read) grant on the caller.
    assert requests == [
        ("PUT", "/v1/sessions/conv_caller/permissions", {"user_id": "__public__", "level": 1})
    ]
    result = json.loads(output)
    assert result == {
        "shared": True,
        "session_id": "conv_caller",
        "user_id": "__public__",
        "level": "read",
    }


@pytest.mark.asyncio
async def test_sys_session_get_info_projects_metadata_and_runner_connectivity() -> None:
    """
    ``sys_session_get_info`` projects ``GET /v1/sessions/{id}`` metadata
    and folds in live runner connectivity from ``GET
    /v1/runners/{id}/status``.

    The full runner-dispatch path: read the session snapshot,
    derive the effective model (a per-session ``model_override`` wins
    over the spec's ``llm_model``), count pending approval prompts, and
    attach ``runner_online``. If the projection regressed (dropped a
    field, skipped the runner-status call, or picked the wrong model),
    the asserted values would differ. The transcript is intentionally
    absent — get_info is metadata-only (``sys_session_get_history`` returns
    items).
    """
    from omnigent.runner.tool_dispatch import execute_tool

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_target":
            return httpx.Response(
                200,
                json={
                    "id": "conv_target",
                    "agent_id": "ag_xyz",
                    "agent_name": "researcher",
                    "status": "running",
                    "created_at": 1,
                    "title": "auth flow",
                    "runner_id": "runner_1",
                    "host_id": None,
                    "reasoning_effort": "high",
                    "parent_session_id": "conv_parent",
                    "sub_agent_name": "researcher",
                    "llm_model": "anthropic/claude-sonnet-4-6",
                    "model_override": "claude-opus-4-8",
                    "workspace": "/repo",
                    "git_branch": "feature/x",
                    "pending_elicitations": [{"id": "el_1"}, {"id": "el_2"}],
                },
            )
        if request.method == "GET" and request.url.path == "/v1/runners/runner_1/status":
            return httpx.Response(200, json={"runner_id": "runner_1", "online": True})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_get_info",
            arguments=json.dumps({"session_id": "conv_target"}),
            server_client=server_client,
            conversation_id="conv_caller",
        )

    info = json.loads(output)
    assert info["session_id"] == "conv_target"
    assert info["status"] == "running"
    assert info["title"] == "auth flow"
    assert info["agent_id"] == "ag_xyz"
    assert info["agent_name"] == "researcher"
    assert info["runner_id"] == "runner_1"
    # Live connectivity folded in from the runners status endpoint —
    # None here would mean the best-effort status call was skipped.
    assert info["runner_online"] is True
    assert info["parent_session_id"] == "conv_parent"
    # Effective model: the per-session override wins over the spec
    # default. "anthropic/claude-sonnet-4-6" here would mean the
    # override was ignored.
    assert info["model"] == "claude-opus-4-8"
    # Two outstanding approval prompts surfaced from the snapshot — both
    # the prompts themselves and a count. If the projection dropped the
    # prompts (count-only), the orchestrator couldn't tell what the
    # blocked session is waiting on.
    assert info["pending_elicitation_count"] == 2
    assert info["pending_elicitations"] == [{"id": "el_1"}, {"id": "el_2"}]
    # Metadata-only: the full transcript is never embedded.
    assert "items" not in info


@pytest.mark.asyncio
async def test_sys_session_get_info_hides_native_ui_wrapper_agent_name() -> None:
    """A native-UI session describes itself with its clean public name.

    Regression for the leak where ``sys_session_get_info`` returned the raw
    bound ``agent_name`` ``"pi-native-ui"``, which the Pi agent then repeated to
    the user ("I'm pi (agent name: pi-native-ui)"). The projection must map the
    internal ``-native-ui`` wrapper name to its display name (``"Pi"``) so the
    implementation detail never reaches the model.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_pi":
            return httpx.Response(
                200,
                json={
                    "id": "conv_pi",
                    "agent_id": "ag_pi",
                    "agent_name": "pi-native-ui",
                    "status": "running",
                    "title": "hi what agent are you?",
                    "runner_id": "runner_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/runners/runner_1/status":
            return httpx.Response(200, json={"runner_id": "runner_1", "online": True})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_get_info",
            arguments=json.dumps({"session_id": "conv_pi"}),
            server_client=server_client,
            conversation_id="conv_pi",
        )

    info = json.loads(output)
    # The internal wrapper name is rewritten to the public display name; the
    # raw ``pi-native-ui`` must not appear anywhere in the tool output.
    assert info["agent_name"] == "Pi"
    assert "pi-native-ui" not in output


@pytest.mark.asyncio
async def test_sys_session_send_session_id_posts_to_direct_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``sys_session_send`` in by-session-id mode verifies the target is a
    direct child of the caller, posts the message, and returns a running
    handle. The unified session_id mode: the message reaches the
    existing child and the handle carries its id with ``status:running``
    (the result is delivered asynchronously via ``sys_read_inbox``).

    :param monkeypatch: Stubs the runner-local child registration so the
        dispatch runs without a live runner.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    event_posts: list[dict[str, Any]] = []

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_child":
            # Target IS a direct child of the caller.
            return httpx.Response(
                200,
                json={
                    "id": "conv_child",
                    "parent_session_id": "conv_caller",
                    "title": "researcher:auth",
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions/conv_child/events":
            event_posts.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps({"session_id": "conv_child", "args": "continue please"}),
                server_client=server_client,
                conversation_id="conv_caller",
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="researcher")]),
                session_inbox=session_inbox,
            )
        finally:
            runner_app.unregister_subagent_work("conv_child")
            runner_app._session_inboxes_ref.pop("conv_caller", None)

    # Message reached the existing child; handle carries its id + running status.
    assert event_posts[0]["data"]["content"][0]["text"] == "continue please"
    handle = json.loads(output)
    assert handle["conversation_id"] == "conv_child"
    assert handle["status"] == "launching"


@pytest.mark.asyncio
async def test_sys_session_send_session_id_rejects_non_child() -> None:
    """
    By-session-id send refuses a target that is NOT a direct child of the
    caller (``parent_session_id`` mismatch) — returning
    ``session_out_of_tree`` and posting NO message. This is the
    child-only safety guarantee: an orchestrator can't drive a sibling or
    an unrelated session it merely has read access to. If the parentage
    check regressed, the message would be posted and the assertion on
    ``event_posts`` would fail.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    event_posts: list[dict[str, Any]] = []

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_other":
            # Target's parent is someone ELSE, not the caller.
            return httpx.Response(
                200,
                json={
                    "id": "conv_other",
                    "parent_session_id": "conv_someone_else",
                    "title": "x:y",
                },
            )
        if request.url.path == "/v1/sessions/conv_other/events":
            event_posts.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps({"session_id": "conv_other", "args": "hi"}),
                server_client=server_client,
                conversation_id="conv_caller",
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="researcher")]),
                session_inbox=session_inbox,
            )
        finally:
            runner_app._session_inboxes_ref.pop("conv_caller", None)

    info = json.loads(output)
    assert info["error"] == "session_out_of_tree"
    # No message was posted to the non-child session.
    assert event_posts == []


@pytest.mark.parametrize("empty_output", ["", "   ", "\n\t "])
def test_format_async_task_item_empty_subagent_completion_reads_as_no_output(
    empty_output: str,
) -> None:
    """
    An empty sub-agent completion renders "produced no output", not "returned:".

    A native child that idles with no assistant text is delivered as ``""`` —
    the runner deliberately avoids fabricating output from stale runner
    history (see
    ``test_external_status_idle_without_output_omits_stale_history_preview``).
    The parent LLM must then see an explicit "produced no output" line rather
    than a dangling ``"…returned: "`` that reads as a truncated/garbled handoff.

    :param empty_output: An empty or whitespace-only child output.
    """
    from omnigent.runner.tool_dispatch import _format_async_task_item

    line = _format_async_task_item(
        {
            "type": "sub_agent",
            "handle_id": "conv_child_empty",
            "agent": "worker",
            "title": "phase-a",
            "status": "completed",
            "output": empty_output,
        }
    )
    # Reads as no-output; the dangling content-free "returned:" must not appear.
    # With the old formatter, an empty output rendered "…returned: ]" and this
    # would fail on the missing "produced no output" / present "returned:".
    assert "produced no output" in line
    assert "returned:" not in line


def test_format_async_task_item_nonempty_subagent_completion_shows_output() -> None:
    """
    A non-empty sub-agent completion still renders its returned text.

    Guards against the empty-output branch swallowing a real result.
    """
    from omnigent.runner.tool_dispatch import _format_async_task_item

    line = _format_async_task_item(
        {
            "type": "sub_agent",
            "handle_id": "conv_child_real",
            "agent": "worker",
            "title": "phase-a",
            "status": "completed",
            "output": "review done: LGTM",
        }
    )
    assert "returned: review done: LGTM" in line
    assert "produced no output" not in line


@pytest.mark.asyncio
async def test_sys_session_send_rejects_both_session_id_and_named_target() -> None:
    """
    Supplying both ``session_id`` and ``agent``/``title`` fails loud.

    The by-session-id and named ``(agent, title)`` modes can point at different
    children, so silently letting ``session_id`` win would misroute the message
    with no signal to the caller. The dispatch must reject the ambiguity before
    making any server call.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    server_called = False

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """
        Raise if any server call is made.

        :param request: Any Omnigent request — none should occur on the reject path.
        :returns: A 404 (also records the unexpected call).
        """
        nonlocal server_called
        server_called = True
        return httpx.Response(404, json={"error": str(request.url)})

    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps(
                    {
                        "session_id": "conv_direct_child",
                        "agent": "claude",
                        "title": "issue-1",
                        "args": "do the thing",
                    }
                ),
                server_client=server_client,
                conversation_id="conv_parent_ambiguous",
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="claude")]),
                session_inbox=session_inbox,
            )
        finally:
            runner_app._session_inboxes_ref.pop("conv_parent_ambiguous", None)

    # The ambiguity is rejected with a fail-loud error naming both modes.
    # Without the guard, session_id silently wins and routing proceeds into
    # _send_to_existing_session (which would hit the server handler below).
    assert "both 'session_id' and 'agent'/'title'" in output
    assert server_called is False


@pytest.mark.asyncio
async def test_create_session_reinit_preserves_existing_inbox() -> None:
    """
    A reconnect re-POST of ``/v1/sessions`` must not wipe the session inbox.

    The Omnigent server re-POSTs ``/v1/sessions`` for every bound conversation
    on each runner WebSocket (re)connect — including in-process reconnects of a
    still-alive runner after a transient blip. A sub-agent completion that lands
    while the socket is down delivers its result into the parent's
    ``_session_inboxes`` queue and latches the work entry ``delivered``, which
    makes redelivery short-circuit. If the re-init blindly replaced the queue,
    that already-delivered payload would be orphaned and the parent would drain
    an empty inbox and hang forever. Drives the real
    ``POST /v1/sessions`` route twice for the same session id and asserts the
    inbox (and a sentinel sitting in it) survives the second call.

    :returns: None.
    """
    from omnigent.runner import app as runner_app

    session_id = "conv_reinit_inbox_guard"
    agent_id = "ag_reinit_inbox_guard"
    # Real stub manager: get_client must succeed so create_session reaches the
    # inbox-init lines. ``_FakeHarnessClient([])`` is never streamed on this path.
    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient([])),
        ),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    # The inbox ref is a module-wide singleton, so it can carry state from an
    # earlier session in the same process.
    runner_app._session_inboxes_ref.pop(session_id, None)
    sentinel = {"type": "subagent_result", "marker": "SURVIVE_REINIT"}
    try:
        async with _runner_test_client(app) as http:
            first = await http.post(
                "/v1/sessions",
                json={"session_id": session_id, "agent_id": agent_id},
            )
            # 201 means create_session ran end-to-end (past the inbox init)
            # rather than short-circuiting (400 missing fields / 501 scaffold /
            # 503 spawn failure), so the inbox we inspect next is route-created.
            assert first.status_code == 201, (
                f"First POST /v1/sessions should create the session (201); got "
                f"{first.status_code} with body {first.text!r}."
            )
            assert first.json()["id"] == session_id

            inbox_after_first = runner_app._session_inboxes_ref.get(session_id)
            # The route must have installed a real inbox queue. If None, the
            # init path didn't run and everything below would be vacuous.
            assert inbox_after_first is not None, (
                "POST /v1/sessions did not create a session inbox; the "
                "inbox-init line in create_session did not run."
            )
            # Stand in for a delivered sub-agent payload waiting to be drained.
            inbox_after_first.put_nowait(sentinel)

            second = await http.post(
                "/v1/sessions",
                json={"session_id": session_id, "agent_id": agent_id},
            )
            # The reconnect re-init must also succeed; a non-201 here would mean
            # the simulated reconnect couldn't re-run the handler at all.
            assert second.status_code == 201, (
                f"Reconnect re-POST /v1/sessions should succeed (201); got "
                f"{second.status_code} with body {second.text!r}."
            )

            inbox_after_second = runner_app._session_inboxes_ref.get(session_id)
            # The load-bearing assertion: the inbox object is preserved across
            # re-init. Without the ``if session_id not in _session_inboxes``
            # guard, line ~4093 would assign a brand-new asyncio.Queue() here,
            # so this would be a different object and the sentinel would vanish.
            assert inbox_after_second is inbox_after_first, (
                "Re-init replaced the session inbox with a new queue. The "
                "guard in create_session must skip re-creating an existing "
                "inbox or a delivered sub-agent payload is orphaned and the "
                "parent hangs on an empty inbox."
            )
            # Content survives: the delivered payload is still drainable. A
            # fresh empty queue would make this drain raise QueueEmpty.
            assert inbox_after_second.get_nowait() == sentinel, (
                "Sentinel payload did not survive the re-init; the parent's "
                "delivered sub-agent result was lost when the inbox was wiped."
            )
    finally:
        runner_app._session_inboxes_ref.pop(session_id, None)


@pytest.mark.asyncio
async def test_create_session_unknown_sub_agent_name_warns_and_uses_parent_spec(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    ``POST /v1/sessions`` with a ``sub_agent_name`` absent from the spec
    tree creates the session against the PARENT spec and warns.

    This used to answer 404 ``sub_agent_not_found``. Both halves are
    asserted: the warning must be emitted AND the harness the process
    manager is asked for must be the root's, not the declared child's —
    a silent fallback satisfies neither.

    :param caplog: Pytest log capture, used to confirm the unresolved
        sub-agent is reported rather than passed over in silence.
    """
    sub_spec = AgentSpec(
        spec_version=1,
        name="worker",
        instructions="Worker instructions.",
        # Deliberately a DIFFERENT harness from the root, so "the parent
        # drove the session" is observable rather than a coincidence.
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )
    root_spec = AgentSpec(
        spec_version=1,
        name="root",
        instructions="Root instructions.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        sub_agents=[sub_spec],
    )

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return root_spec

    pm = _FakeProcessManager(_FakeHarnessClient([]))
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, pm),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_test_client(app) as http:
            response = await http.post(
                "/v1/sessions",
                json={
                    "session_id": "conv_unknown_sub_agent",
                    "agent_id": "ag_root",
                    "sub_agent_name": "does-not-exist",
                },
            )
    assert response.status_code == 201, (
        f"An unresolvable sub_agent_name must not fail the create; got "
        f"{response.status_code} with body {response.text!r}."
    )
    assert "'does-not-exist'" in caplog.text and "did not resolve" in caplog.text, (
        f"The unresolved sub-agent must be warned about; got {caplog.text!r}."
    )
    assert [harness for _conv, harness, _env in pm.get_client_calls] == ["opencode-native"], (
        f"The PARENT's harness must drive the session, not the declared "
        f"child's; got {pm.get_client_calls!r}."
    )


@pytest.mark.asyncio
async def test_create_session_sub_agent_name_matching_root_name_warns_as_unresolved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    ``POST /v1/sessions`` with a ``sub_agent_name`` equal to the ROOT
    spec's own name (where the root has no matching CHILD) is a MISS, and
    is reported as one.

    A coincidental name match at the root is not a resolved sub-agent.
    ``_find_spec_by_name`` briefly self-matched its own ``spec`` argument
    against ``name`` before searching children, added to fix an unrelated
    idempotency bug (re-resolving an ALREADY-RESOLVED cached child spec
    against itself). That self-match had no way to tell "fresh root that
    happens to share a name" apart from "already-resolved child", so a
    top-level coordinator named e.g. "worker" with no "worker" sub-agent
    resolved as if the child had been found — and reported nothing.

    The session proceeds on the root's spec either way, so the request
    status alone cannot distinguish the two. The warning is what does:
    it is emitted for a miss and never for a genuine match.

    :param caplog: Pytest log capture, used to distinguish a reported miss
        from a self-match that quietly looked like success.
    """
    root_spec = AgentSpec(
        spec_version=1,
        name="worker",
        instructions="Root instructions — must never be treated as the resolved child.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
        # No sub_agents at all: "worker" only coincidentally matches the
        # ROOT's own name, there is no genuine child by that name.
    )

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return root_spec

    pm = _FakeProcessManager(_FakeHarnessClient([]))
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, pm),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_test_client(app) as http:
            response = await http.post(
                "/v1/sessions",
                json={
                    "session_id": "conv_root_name_coincidence",
                    "agent_id": "ag_worker_root",
                    "sub_agent_name": "worker",
                },
            )
    assert response.status_code == 201, (
        f"An unresolvable sub_agent_name must not fail the create; got "
        f"{response.status_code} with body {response.text!r}."
    )
    assert "'worker'" in caplog.text and "did not resolve" in caplog.text, (
        f"A root whose own name coincidentally matches sub_agent_name, with no "
        f"genuine child by that name, must be reported as an unresolved "
        f"sub-agent — not accepted as a silent self-match. Got {caplog.text!r}."
    )
    assert [harness for _conv, harness, _env in pm.get_client_calls] == ["opencode-native"], (
        f"The root's own spec drives the session; got {pm.get_client_calls!r}."
    )


@pytest.mark.parametrize("second_turn", ["background", "known_harness"])
@pytest.mark.asyncio
async def test_unresolvable_sub_agent_warns_again_on_later_turns(
    caplog: pytest.LogCaptureFixture,
    second_turn: str,
) -> None:
    """A parent kept after a miss must not become a resolved child on turn 2.

    Session creation misses on ``sub_agent_name``, warns, and caches the
    PARENT spec for the session. Later turns read that cache instead of
    resolving again, and decide "is this the already-resolved child?" by
    comparing the cached spec's name against the requested name. A root
    whose own name equals the requested sub-agent satisfies that equality,
    so the cached parent answers as though it were the child — and the
    warning that made turn 1 honest never fires again.

    The session is identical on every turn, so the answer must be too:
    warn, then continue with the parent. Asserted per turn with the create's
    own warning cleared first, so a single warning at create cannot stand in
    for the later turn's.

    :param caplog: Pytest log capture, read once per turn.
    :param second_turn: Which dispatch path drives the turn after create.
    """
    conv = f"conv_fallback_second_turn_{second_turn}"

    # The root's own name equals the requested sub-agent and there is no such
    # child, so the cached parent satisfies a name-equality identity check.
    # A non-native harness so the turn reaches cold boot without a vendor CLI.
    root_spec = AgentSpec(
        spec_version=1,
        name="worker",
        instructions="Root instructions.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return root_spec

    recorder = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    pm = _FakeProcessManager(recorder)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, pm),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            created = await http.post(
                "/v1/sessions",
                json={
                    "session_id": conv,
                    "agent_id": "ag_root",
                    "sub_agent_name": "worker",
                },
            )
        assert created.status_code == 201, created.text
        assert "did not resolve" in caplog.text, (
            f"create should warn on the miss; got {caplog.text!r}"
        )

        # Everything after this point is about the SECOND turn alone.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            if second_turn == "background":
                bg = await http.post(
                    f"/v1/sessions/{conv}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": "ag_root",
                        "model": "x",
                        "content": [{"role": "user", "content": "hi"}],
                    },
                )
                assert bg.status_code == 202, bg.text
                await _await_bg_turn_task(conv)
                statuses = await _drain_published_statuses(conv, until="idle", timeout=2.0)
                assert "failed" not in statuses, (
                    f"the later turn must not fail; statuses={statuses!r}"
                )
            else:
                streamed = await _post_stream_message(
                    http,
                    conv,
                    agent_id="ag_root",
                    harness="claude-sdk",
                    instructions="Caller-supplied instructions.",
                )
                assert streamed.status_code == 200, streamed.text

    assert "'worker'" in caplog.text and "did not resolve" in caplog.text, (
        f"The {second_turn} turn reused a PARENT kept after a miss and reported "
        f"nothing. The same session asked the same question and got a silent "
        f"answer. Warnings seen on this turn: {caplog.text!r}."
    )
    assert recorder.posted_bodies, "harness never received a request"
    composed = recorder.posted_bodies[-1].get("instructions")
    assert isinstance(composed, str) and "Root instructions." in composed, (
        f"the PARENT's authored instructions must drive the later turn; got {composed!r}."
    )


@pytest.mark.asyncio
async def test_spec_published_before_sub_agent_decision_is_not_served(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A spec cached mid-resolution must not answer a concurrent reader.

    Background resolution publishes the freshly resolved PARENT, then awaits
    the session snapshot to learn which sub-agent the session names. Inside
    that await the entry exists but the sub-agent question is undecided, and
    an entry that claims to be decided is handed to any reader that arrives —
    ``GET /resources`` used to be answered from it, with the parent's spec and
    no warning at all.

    The reader must not be served from an undecided entry. It waits for the
    fact instead — joining the same single-flight snapshot fetch — and reports
    the unresolved sub-agent once that fetch lands.

    :param caplog: Pytest log capture, read during and after the window.
    """
    conv = "conv_publish_before_decision"
    at_snapshot = asyncio.Event()
    release = asyncio.Event()
    root_spec = AgentSpec(
        spec_version=1,
        name="worker",
        instructions="Root instructions.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return root_spec

    class _ParkedSnapshot(NullServerClient):
        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs
            if url.endswith(f"/v1/sessions/{conv}"):
                at_snapshot.set()
                await release.wait()

                class _Resp(NullServerClient._Response):
                    status_code = 200

                    def json(self) -> dict[str, object]:
                        return {"agent_id": "ag_root", "sub_agent_name": "worker"}

                return _Resp()
            return await super().get(url)

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)),
        ),
        spec_resolver=_spec_resolver,
        server_client=_ParkedSnapshot(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            bg = await http.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "ag_root",
                    "model": "x",
                    "content": [{"role": "user", "content": "hi"}],
                },
            )
            assert bg.status_code == 202, bg.text
            await asyncio.wait_for(at_snapshot.wait(), timeout=5.0)

            reader = asyncio.create_task(http.get(f"/v1/sessions/{conv}/resources"))
            for _ in range(50):
                await asyncio.sleep(0)
            assert not reader.done(), (
                f"the reader was answered from an entry published before the "
                f"sub-agent decision was made; it returned "
                f"{reader.result().text!r} while the snapshot fetch was still "
                f"in flight, having logged {caplog.text!r}."
            )

            release.set()
            response = await asyncio.wait_for(reader, timeout=5.0)
            await _await_bg_turn_task(conv)

    assert response.status_code == 200, response.text
    assert "'worker'" in caplog.text and "did not resolve" in caplog.text, (
        f"once the snapshot lands the unresolved sub-agent must be reported; got {caplog.text!r}."
    )


@pytest.mark.asyncio
async def test_failed_sub_agent_recovery_does_not_silence_the_next_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dropped snapshot must not make the session answer silently forever.

    ``_session_snapshot`` reports a failed fetch as a not-ok snapshot whose
    fields are all ``None`` rather than by raising, so a recovery that reads
    ``sub_agent_name`` off one cannot tell "this session has no sub-agent"
    from "the fetch failed". Turn 1 here fails that fetch; the entry it
    publishes must not claim the sub-agent question was answered.

    Turn 2 recovers the name successfully. It used to take the cached entry as
    an already-resolved child — the root's own name matches the request — and
    every later turn stayed silent, off one transient failure.

    :param caplog: Pytest log capture, read once per turn.
    """
    conv = "conv_recovery_failure_then_success"
    snapshot_fails = {"now": True}
    root_spec = AgentSpec(
        spec_version=1,
        name="worker",
        instructions="Root instructions.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return root_spec

    class _FlakySnapshot(NullServerClient):
        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs
            if url.endswith(f"/v1/sessions/{conv}"):
                if snapshot_fails["now"]:
                    raise RuntimeError("session snapshot unavailable")

                class _Resp(NullServerClient._Response):
                    status_code = 200

                    def json(self) -> dict[str, object]:
                        return {"agent_id": "ag_root", "sub_agent_name": "worker"}

                return _Resp()
            return await super().get(url)

    recorder = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recorder)),
        spec_resolver=_spec_resolver,
        server_client=_FlakySnapshot(),  # type: ignore[arg-type]
    )
    turn = {
        "type": "message",
        "role": "user",
        "agent_id": "ag_root",
        "model": "x",
        "content": [{"role": "user", "content": "hi"}],
    }
    async with _runner_test_client(app) as http:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            first = await http.post(f"/v1/sessions/{conv}/events", json=turn)
            assert first.status_code == 202, first.text
            await _await_bg_turn_task(conv)
            await _drain_published_statuses(conv, until="idle", timeout=2.0)
        assert "did not resolve" not in caplog.text, (
            f"turn 1 cannot name an unresolved sub-agent — the lookup failed, "
            f"so nothing is known yet; got {caplog.text!r}."
        )

        # The snapshot recovers. Everything below is about THIS turn.
        snapshot_fails["now"] = False
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            second = await http.post(f"/v1/sessions/{conv}/events", json=turn)
            assert second.status_code == 202, second.text
            await _await_bg_turn_task(conv)
            statuses = await _drain_published_statuses(conv, until="idle", timeout=2.0)

    assert "failed" not in statuses, f"the later turn must not fail; got {statuses!r}"
    assert "'worker'" in caplog.text and "did not resolve" in caplog.text, (
        f"the turn that finally learns the sub-agent name must report that it "
        f"does not resolve. A cache entry left over from the failed lookup "
        f"answered 'already the child' instead. Warnings on this turn: "
        f"{caplog.text!r}."
    )
    assert recorder.posted_bodies, "harness never received a request"
    composed = recorder.posted_bodies[-1].get("instructions")
    assert isinstance(composed, str) and "Root instructions." in composed, (
        f"the PARENT's authored instructions must drive the turn; got {composed!r}."
    )


@pytest.mark.asyncio
async def test_unknown_provenance_state_is_not_trusted_by_either_reader() -> None:
    """A provenance state nobody vouched for must be trusted by nobody.

    Both provenance questions are written as allowlists — they name the
    states they accept — rather than as "anything but UNDETERMINED". The
    difference only shows up for a state that exists but is listed by
    neither: an allowlist rejects it, a denylist admits it silently and
    hands a caller a spec on evidence no one supplied.

    Both helpers are closures over the runner app, so this asserts the
    property at the source level rather than by calling them: each must
    decide by naming the states it accepts, and neither may decide by
    excluding a state. That is what makes an added state default to
    untrusted. A regression to ``is not UNDETERMINED`` — which is what these
    two returns did before, and what admits an unlisted state silently —
    fails here.
    """
    import ast
    import inspect

    from omnigent.runner import app as runner_app

    tree = ast.parse(inspect.getsource(runner_app))
    helpers = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_session_spec_is_resolved_child", "_session_spec_provenance_is_settled"}
    }
    assert set(helpers) == {
        "_session_spec_is_resolved_child",
        "_session_spec_provenance_is_settled",
    }, f"both provenance allowlists must exist; found {sorted(helpers)}"

    for name, node in helpers.items():
        for cmp_node in [n for n in ast.walk(node) if isinstance(n, ast.Compare)]:
            for op in cmp_node.ops:
                assert not isinstance(op, (ast.IsNot, ast.NotEq)), (
                    f"{name} decides by EXCLUDING a provenance state. An "
                    f"allowlist names what it accepts, so a state added later "
                    f"is untrusted until someone lists it; a negative test "
                    f"admits it by default, which is how a cached parent "
                    f"fallback came to be served as a resolved child."
                )
        # ...and it must still positively name something, or it accepts nothing.
        names = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name)
        }
        assert "RESOLVED" in names, f"{name} must name the state(s) it accepts"


# ── approval-event flattening (elicitation-approval hang regression) ──────


@pytest.mark.asyncio
async def test_approval_event_flattened_for_harness_scaffold() -> None:
    """A nested approval envelope is flattened to the scaffold's ApprovalEvent.

    Regression for the elicitation-approval hang: the server forwards the
    verdict as ``{"type": "approval", "data": {...}}``, but the harness
    scaffold's ``ApprovalEvent`` requires ``elicitation_id`` / ``action`` /
    ``content`` at the TOP level. If the runner forwards the envelope verbatim
    the harness 422s and the parked ``ctx.elicit`` Future never resolves (the
    turn hangs after a human approves). The runner must translate the envelope
    into the flat event the scaffold validates — for every scaffold harness.
    """
    from omnigent.runtime.harnesses._scaffold import ApprovalEvent

    captured: dict[str, Any] = {}

    class _CapturingHarnessClient:
        async def post(
            self, url: str, *, json: dict[str, Any], timeout: float | None = None
        ) -> httpx.Response:
            captured["url"] = url
            captured["body"] = json
            return httpx.Response(204)

    mgr = _FakeProcessManager(harness_client=cast(Any, _CapturingHarnessClient()))
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, mgr),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        resp = await http.post(
            "/v1/sessions/conv_x/events",
            json={
                "type": "approval",
                "data": {
                    "elicitation_id": "elicit_x",
                    "action": "accept",
                    "content": {"note": "ok"},
                },
            },
        )

    assert resp.status_code == 204
    # Forwarded body is FLAT — no ``data`` envelope.
    assert captured["body"] == {
        "type": "approval",
        "elicitation_id": "elicit_x",
        "action": "accept",
        "content": {"note": "ok"},
    }
    # And it validates as the scaffold's ApprovalEvent (i.e. no 422).
    ApprovalEvent.model_validate(captured["body"])


@pytest.mark.asyncio
async def test_approval_event_without_content_flattened() -> None:
    """A decline verdict with no form content flattens without a ``content`` key."""
    from omnigent.runtime.harnesses._scaffold import ApprovalEvent

    captured: dict[str, Any] = {}

    class _CapturingHarnessClient:
        async def post(
            self, url: str, *, json: dict[str, Any], timeout: float | None = None
        ) -> httpx.Response:
            captured["body"] = json
            return httpx.Response(204)

    mgr = _FakeProcessManager(harness_client=cast(Any, _CapturingHarnessClient()))
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, mgr),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        resp = await http.post(
            "/v1/sessions/conv_y/events",
            json={"type": "approval", "data": {"elicitation_id": "e2", "action": "decline"}},
        )

    assert resp.status_code == 204
    assert captured["body"] == {"type": "approval", "elicitation_id": "e2", "action": "decline"}
    ApprovalEvent.model_validate(captured["body"])


@pytest.mark.asyncio
async def test_spawn_handle_round_trips_to_sys_cancel_async(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawned handle can be passed directly to ``sys_cancel_async``."""
    from omnigent.runner import tool_dispatch

    async def _slow(**_kw: Any) -> str:
        await asyncio.sleep(30)
        return "late"

    monkeypatch.setattr(tool_dispatch, "execute_tool", _slow)
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    spawn_kw: dict[str, Any] = {
        "server_client": None,
        "terminal_registry": None,
        "resource_registry": None,
        "agent_spec": None,
        "conversation_id": "conv_roundtrip",
        "task_id": None,
        "agent_id": None,
        "agent_name": None,
        "runner_workspace": None,
        "mcp_manager": None,
        "filesystem_registry": None,
    }
    handle_raw = tool_dispatch._spawn_async_tool(
        {"tool": "slow", "args": "{}"},
        session_inbox=inbox,
        session_async_tasks=tasks,
        **spawn_kw,
    )
    handle = json.loads(handle_raw)
    assert handle["task_id"] == handle["handle_id"]
    assert handle["status"] == "in_progress"
    assert "sys_cancel_async" in handle["message"]
    assert f"handle_id={handle['handle_id']!r}" in handle["message"]

    bg_task, _evt = tasks[handle["handle_id"]]
    cancel_raw = tool_dispatch._cancel_async_tool(
        {"handle_id": handle["handle_id"]},
        session_async_tasks=tasks,
    )
    assert json.loads(cancel_raw) == {
        "cancelled": True,
        "handle_id": handle["handle_id"],
    }

    await bg_task
    assert inbox.get_nowait()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_spawn_async_tool_cancels_losing_future_no_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_spawn_async_tool`` races the tool coro against ``cancel_event.wait()``
    via ``asyncio.wait(FIRST_COMPLETED)``. The losing future must be cancelled
    in BOTH branches, otherwise every ``sys_call_async`` permanently leaks one
    pending task (the orphaned ``cancel_event.wait()`` on success, or the
    orphaned tool coro on cancel).
    """
    from omnigent.runner import tool_dispatch

    spawn_kw: dict[str, Any] = {
        "server_client": None,
        "terminal_registry": None,
        "resource_registry": None,
        "agent_spec": None,
        "conversation_id": "conv_leak",
        "task_id": None,
        "agent_id": None,
        "agent_name": None,
        "runner_workspace": None,
        "mcp_manager": None,
        "filesystem_registry": None,
    }

    def _leaked(before: set[asyncio.Task[Any]]) -> list[str]:
        cur = asyncio.current_task()
        return [
            t.get_name()
            for t in asyncio.all_tasks()
            if t not in before and t is not cur and not t.done()
        ]

    # Success path: tool finishes first, so cancel_event.wait() is the loser.
    async def _fast(**_kw: Any) -> str:
        return "ok"

    monkeypatch.setattr(tool_dispatch, "execute_tool", _fast)
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}
    before = set(asyncio.all_tasks())
    tool_dispatch._spawn_async_tool(
        {"tool": "noop", "args": "{}"},
        session_inbox=inbox,
        session_async_tasks=tasks,
        **spawn_kw,
    )
    bg_task, _evt = next(iter(tasks.values()))
    await bg_task
    await asyncio.sleep(0)  # let the .cancel() propagate to the losing future
    assert inbox.get_nowait()["status"] == "completed"
    assert _leaked(before) == []

    # Cancel path: cancel_event fires first, so the tool coro is the loser.
    async def _slow(**_kw: Any) -> str:
        await asyncio.sleep(30)
        return "late"

    monkeypatch.setattr(tool_dispatch, "execute_tool", _slow)
    inbox = asyncio.Queue()
    tasks = {}
    before = set(asyncio.all_tasks())
    tool_dispatch._spawn_async_tool(
        {"tool": "slow", "args": "{}"},
        session_inbox=inbox,
        session_async_tasks=tasks,
        **spawn_kw,
    )
    bg_task, evt = next(iter(tasks.values()))
    await asyncio.sleep(0)  # let _bg reach asyncio.wait
    evt.set()
    await bg_task
    await asyncio.sleep(0)
    assert inbox.get_nowait()["status"] == "cancelled"
    assert _leaked(before) == []


@pytest.mark.asyncio
async def test_spawn_async_tool_phase_tool_call_policy_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_spawn_async_tool`` evaluates PHASE_TOOL_CALL policy via the AP server
    before dispatching.  A DENY verdict must suppress execution and push a
    failed item to the inbox — the tool must never run.
    """
    from omnigent.runner import tool_dispatch

    executed: list[str] = []

    async def _should_not_run(**_kw: Any) -> str:
        executed.append("ran")
        return "should not reach"

    monkeypatch.setattr(tool_dispatch, "execute_tool", _should_not_run)

    policy_requests: list[dict[str, Any]] = []

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/policies/evaluate" in request.url.path:
            policy_requests.append(json.loads(request.content))
            return httpx.Response(
                200, json={"result": "POLICY_ACTION_DENY", "reason": "test deny"}
            )
        return httpx.Response(404)

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        tool_dispatch._spawn_async_tool(
            {"tool": "sys_os_shell", "args": '{"command": "echo hi"}'},
            session_inbox=inbox,
            session_async_tasks=tasks,
            server_client=server_client,
            terminal_registry=None,
            resource_registry=None,
            agent_spec=None,
            conversation_id="conv_policy_deny",
            task_id=None,
            agent_id=None,
            agent_name=None,
            runner_workspace=None,
            mcp_manager=None,
            filesystem_registry=None,
        )
        bg_task, _evt = next(iter(tasks.values()))
        await bg_task

    assert executed == [], "Tool must not execute when PHASE_TOOL_CALL is denied"
    assert len(policy_requests) == 1
    event = policy_requests[0]["event"]
    assert event["type"] == "PHASE_TOOL_CALL"
    assert event["data"]["name"] == "sys_os_shell"
    # arguments must arrive as a dict so argument-aware policies (e.g. safety
    # rules that inspect arguments.command) see the same structure every
    # in-turn evaluation path delivers — not a raw JSON string.
    assert isinstance(event["data"]["arguments"], dict), (
        "arguments must be a dict, not a JSON string"
    )
    assert event["data"]["arguments"] == {"command": "echo hi"}

    item = inbox.get_nowait()
    assert item["status"] == "failed"
    assert "policy" in item["output"].lower()


@pytest.mark.asyncio
async def test_spawn_async_tool_phase_tool_call_policy_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A PHASE_TOOL_CALL ALLOW verdict must let the tool execute normally.
    """
    from omnigent.runner import tool_dispatch

    executed: list[str] = []

    async def _fast(**_kw: Any) -> str:
        executed.append("ran")
        return "ok"

    monkeypatch.setattr(tool_dispatch, "execute_tool", _fast)

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        if "/policies/evaluate" in request.url.path:
            return httpx.Response(200, json={"result": "POLICY_ACTION_ALLOW"})
        return httpx.Response(404)

    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        tool_dispatch._spawn_async_tool(
            {"tool": "sys_os_shell", "args": '{"command": "echo hi"}'},
            session_inbox=inbox,
            session_async_tasks=tasks,
            server_client=server_client,
            terminal_registry=None,
            resource_registry=None,
            agent_spec=None,
            conversation_id="conv_policy_allow",
            task_id=None,
            agent_id=None,
            agent_name=None,
            runner_workspace=None,
            mcp_manager=None,
            filesystem_registry=None,
        )
        bg_task, _evt = next(iter(tasks.values()))
        await bg_task

    assert executed == ["ran"], "Tool must execute when PHASE_TOOL_CALL is allowed"
    item = inbox.get_nowait()
    assert item["status"] == "completed"
    assert item["output"] == "ok"


@pytest.mark.asyncio
async def test_reset_state_evicts_the_session_init_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The session-init envelope must not outlive an agent switch.

    ``_session_init_envelopes`` caches the create-time envelope for a 60s TTL
    and ``_fresh_session_init_envelope`` hands its ``snapshot.labels`` to
    native spawn and bridge lookups as ``optional_labels``. The envelope is
    agent-derived — it carries the agent id it was built for and the labels
    chosen for that agent's session, bridge id among them — but it was in
    neither the cache audit nor ``_clear_session_agent_caches``, so a switch
    left the NEW agent pointed at the PREVIOUS agent's bridge for the rest of
    the TTL. After ``/reset-state`` a turn dispatched as B
    resolves its bridge without A's labels.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import omnigent.claude_native_bridge as claude_bridge_mod
    import omnigent.runner.app as runner_app_mod
    import omnigent.runner.tool_dispatch as tool_dispatch_mod
    from omnigent.claude_native_bridge import BRIDGE_ID_LABEL_KEY

    conv = "conv_init_envelope_reset"
    seen_labels: list[Any] = []

    async def _fake_bridge_id_with_optional_labels(
        *, server_client: object, session_id: str, session_labels: Any = None
    ) -> str:
        del server_client, session_id
        seen_labels.append(session_labels)
        return "bridge-resolved"

    async def _noop_async(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def _no_transfer(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return False

    monkeypatch.setattr(
        runner_app_mod,
        "_claude_native_bridge_id_with_optional_labels",
        _fake_bridge_id_with_optional_labels,
    )
    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_claude_terminal", _noop_async
    )
    monkeypatch.setattr(
        runner_app_mod, "_claude_native_terminal_arrives_via_transfer", _no_transfer
    )
    monkeypatch.setattr(
        runner_app_mod,
        "_ensure_orchestrator_skills_in_bundle",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        claude_bridge_mod, "start_tool_relay", lambda **kwargs: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(claude_bridge_mod, "post_tools_changed", lambda _d: None)
    monkeypatch.setattr(tool_dispatch_mod, "build_native_relay_tool_schemas", lambda _spec: [])

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        return AgentSpec(
            spec_version=1,
            name=f"agent-{agent_id}",
            executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
        )

    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(_FakeHarnessClient([]))),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        created = await http.post(
            "/v1/sessions",
            json={
                "session_id": conv,
                "agent_id": "ag_env_a",
                "sub_agent_name": None,
                "session_init": {
                    "protocol_version": 2,
                    "server_version": "0.6.0.dev0",
                    "session_id": conv,
                    "agent_id": "ag_env_a",
                    "sub_agent_name": None,
                    "snapshot": {
                        "created_at": 1234,
                        "updated_at": 1234,
                        "workspace": None,
                        "labels": {BRIDGE_ID_LABEL_KEY: "bridge-A"},
                    },
                },
            },
        )
        assert created.status_code == 201, created.text

        reset = await http.post(f"/v1/sessions/{conv}/reset-state")
        assert reset.status_code == 200, reset.text

        turn = await http.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_env_b",
                "model": "x",
                "content": [{"role": "user", "content": "hi"}],
            },
        )
        assert turn.status_code == 202
        await _await_bg_turn_task(conv)

    _stale = [labels for labels in seen_labels if (labels or {}).get(BRIDGE_ID_LABEL_KEY)]
    assert _stale == [], (
        f"agent B's turn must not be handed agent A's create-time labels — "
        f"bridge lookups saw {seen_labels!r}. A surviving 'bridge-A' means the "
        f"session-init envelope outlived the switch and pointed the new agent "
        f"at the previous agent's bridge."
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        pytest.param("/resources", id="list_session_resources"),
        pytest.param("/resources/environments/default", id="get_session_environment"),
        pytest.param("/models", id="get_session_models"),
        pytest.param("/skills", id="get_session_skills"),
    ],
)
@pytest.mark.asyncio
async def test_session_scoped_reads_resolve_the_recorded_agent_not_the_snapshot(
    endpoint: str,
) -> None:
    """
    Session-scoped REST reads must resolve the session's RECORDED agent.

    Each of these handlers takes only a ``session_id`` path param, so before
    the fix they resolved off the session snapshot — an independently-lagging
    source that can still report the PREVIOUS agent while a switch is in
    flight. ``_session_agent_ids`` already holds what the session last
    dispatched as, and is passed as ``agent_id_hint``.

    The snapshot here reports B for the whole test while the recorded marker
    says A, so a handler that drops the hint sends B to the resolver and
    fails the assertion.

    :param endpoint: Session-scoped path suffix under test.
    """
    conv = f"conv_scoped_read_{endpoint.strip('/').replace('/', '_')}"
    resolved_agent_ids: list[str] = []

    specs = {
        "ag_scoped_a": AgentSpec(
            spec_version=1,
            name="agent-a",
            instructions="Agent A.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        ),
        "ag_scoped_b": AgentSpec(
            spec_version=1,
            name="agent-b",
            instructions="Agent B.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        ),
    }

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        resolved_agent_ids.append(agent_id)
        return specs[agent_id]

    class _StaleBSnapshotClient(NullServerClient):
        """Snapshot lags on agent B for the whole run."""

        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": "ag_scoped_b"}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    recording_client = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording_client)),
        spec_resolver=_spec_resolver,
        server_client=_StaleBSnapshotClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        # Records _session_agent_ids[conv] = A. hermes has no cold-boot step,
        # so nothing else is cached and the read below is the first resolve.
        first = await _post_stream_message(http, conv, agent_id="ag_scoped_a", harness="hermes")
        assert first.status_code == 200

        resp = await http.get(f"/v1/sessions/{conv}{endpoint}")
        assert resp.status_code == 200, resp.text

    assert "ag_scoped_b" not in resolved_agent_ids, (
        f"{endpoint} must resolve the session's recorded agent (A) via "
        f"agent_id_hint, not the lagging snapshot's agent (B) — resolver saw "
        f"{resolved_agent_ids!r}."
    )
    assert "ag_scoped_a" in resolved_agent_ids, (
        f"{endpoint} was expected to resolve agent A — resolver saw {resolved_agent_ids!r}."
    )


@pytest.mark.asyncio
async def test_instruction_delivery_warn_refires_when_the_switch_changes_delivery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A switch to a harness with a DIFFERENT delivery value must warn again.

    The other half of the rule is a switch where the delivery gap is
    identical, which stays at one warning per conversation. Both are the
    same rule — dedup on
    what the warning was ABOUT — and a presence-only marker can only express
    the first. With one, agent B's genuinely-undelivered instructions are
    silently suppressed by the marker agent A left behind, which is the whole
    point of the warning.

    Here A runs on ``kimi`` (``NOT_DELIVERED``) and B on a harness that
    declares no capability at all (``UNKNOWN``), so both warnings are
    warranted and they are distinct messages.

    A presence-only marker keyed on ``conv_id`` alone cannot express this:
    B's turn would find the conversation already recorded and stay silent,
    giving one warning where two are warranted.

    :param caplog: Pytest log capture fixture.
    """
    conv = "conv_instruction_warn_delivery_changed"

    specs = {
        "ag_warn_kimi": AgentSpec(
            spec_version=1,
            name="kimi-agent",
            instructions="Be a helpful coding assistant.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "kimi"}),
        ),
        "ag_warn_unknown": AgentSpec(
            spec_version=1,
            name="unknown-capability-agent",
            instructions="Be a helpful coding assistant.",
            executor=ExecutorSpec(type="omnigent", config={"harness": _TEST_HARNESS_NAME}),
        ),
    }

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        return specs[agent_id]

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient(_INSTRUCTION_WARN_CHUNKS)),
        ),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async def _post_bg(agent_id: str) -> None:
        async with _runner_test_client(app) as http:
            resp = await http.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": agent_id,
                    "model": "x",
                    "content": [{"role": "user", "content": "hi"}],
                },
            )
            assert resp.status_code == 202
            await _await_bg_turn_task(conv)

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _post_bg("ag_warn_kimi")
        await _post_bg("ag_warn_unknown")

    not_delivered = [
        rec for rec in caplog.records if "instructions are not delivered" in rec.getMessage()
    ]
    undetermined = [
        rec
        for rec in caplog.records
        if "declares no instruction_delivery capability" in rec.getMessage()
    ]
    assert len(not_delivered) == 1, (
        f"agent A's not-delivered gap must warn once — got {not_delivered}."
    )
    assert len(undetermined) == 1, (
        f"agent B runs on a harness with a DIFFERENT (unknown) delivery value, so its "
        f"gap must warn too — got {undetermined}. Zero means the previous agent's "
        f"marker suppressed a warning about a gap it never applied to."
    )


@pytest.mark.parametrize(
    "writer",
    [
        pytest.param("background_spec", id="background-spec-write"),
        pytest.param("initialize_session_spec", id="initialize-session-spec-write"),
    ],
)
@pytest.mark.asyncio
async def test_invalidate_midflight_drops_the_stale_fill(writer: str) -> None:
    """
    A fill parked mid-flight must not publish after the session is invalidated.

    Shared race harness: park the spec resolver inside the fill under test,
    invalidate via ``/reset-state`` while it is parked, release it, then issue
    a follow-up read for the SAME agent. Same-agent is the load-bearing
    detail — it models a spec/MCP edit rather than an agent switch, so the
    per-entry provenance tag matches and cannot mask the defect. If the parked
    fill published, the follow-up hits that entry and never calls the resolver
    again; if it was correctly dropped, the resolver runs a second time.

    Both parametrizations park the SHARED spec resolver, so what they pin is
    the session-spec write specifically. A writer whose fill parks somewhere
    else needs its own isolated race — see
    ``test_skills_fill_parked_past_invalidation_does_not_publish``, which parks
    in skills discovery rather than here.

    A setter that published unconditionally, without its generation check,
    would let the parked fill land and both parametrizations would then see
    exactly one resolver call.

    :param writer: Which writer class parks in this run.
    """
    conv = f"conv_race_{writer}"
    agent_id = "ag_race"
    resolver_calls: list[str] = []
    parked = asyncio.Event()
    release = asyncio.Event()

    async def _spec_resolver(resolved_agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        resolver_calls.append(resolved_agent_id)
        if len(resolver_calls) == 1:
            parked.set()
            await release.wait()
        return AgentSpec(
            spec_version=1,
            name=f"spec-{resolved_agent_id}-{len(resolver_calls)}",
            instructions="Agent instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        )

    class _StableAgentSnapshotClient(NullServerClient):
        """Snapshot always reports the same agent — this race is a same-agent
        spec edit, not a switch."""

        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": agent_id}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(_FakeHarnessClient([]))),
        spec_resolver=_spec_resolver,
        server_client=_StableAgentSnapshotClient(),  # type: ignore[arg-type]
    )

    async with _runner_test_client(app) as http:

        async def _start_parked_fill() -> Any:
            if writer == "background_spec":
                return await http.post(
                    f"/v1/sessions/{conv}/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": agent_id,
                        "model": "x",
                        "content": [{"role": "user", "content": "hi"}],
                    },
                )
            if writer == "initialize_session_spec":
                return await http.post(
                    "/v1/sessions",
                    json={"session_id": conv, "agent_id": agent_id},
                )
            raise AssertionError(f"unhandled writer {writer!r}")

        in_flight = asyncio.create_task(_start_parked_fill())
        await asyncio.wait_for(parked.wait(), timeout=5.0)

        # Invalidate while the fill is parked. Bumps the generation, so the
        # parked fill's captured generation is now stale.
        reset = await http.post(f"/v1/sessions/{conv}/reset-state")
        assert reset.status_code == 200, reset.text

        release.set()
        await in_flight
        if writer == "background_spec":
            await _await_bg_turn_task(conv)

        follow_up = await http.get(f"/v1/sessions/{conv}/models")
        assert follow_up.status_code == 200, follow_up.text

    assert len(resolver_calls) >= 2, (
        f"the follow-up read must re-resolve after the mid-flight invalidation "
        f"— resolver was called {len(resolver_calls)} time(s): {resolver_calls!r}. "
        f"Exactly one call means the parked fill published its value after the "
        f"reset and the follow-up was served from that stale entry."
    )


@pytest.mark.asyncio
async def test_instruction_delivery_warn_does_not_refire_for_a_recurring_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A -> B -> A must warn for A exactly once across the whole sequence.

    The dedup is a LIFETIME rule: at most once per (conversation, harness,
    delivery value) for as long as the conversation lives. A marker
    holding only the most recently warned pair satisfies the two-agent cases
    but not this one — B displaces A's record, so when A recurs it looks
    unseen and warns a second time for a gap already reported. Membership in
    the set of pairs already warned for is what the contract actually asks for.

    A marker holding only the last-warned pair cannot express this: the third
    turn emits a second not-delivered warning for A, because B displaced A's
    record.

    :param caplog: Pytest log capture fixture.
    """
    conv = "conv_instruction_warn_recurring"

    specs = {
        "ag_recur_a": AgentSpec(
            spec_version=1,
            name="kimi-agent",
            instructions="Be a helpful coding assistant.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "kimi"}),
        ),
        "ag_recur_b": AgentSpec(
            spec_version=1,
            name="unknown-capability-agent",
            instructions="Be a helpful coding assistant.",
            executor=ExecutorSpec(type="omnigent", config={"harness": _TEST_HARNESS_NAME}),
        ),
    }

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        return specs[agent_id]

    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager,
            _FakeProcessManager(_FakeHarnessClient(_INSTRUCTION_WARN_CHUNKS)),
        ),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async def _post_bg(agent_id: str) -> None:
        async with _runner_test_client(app) as http:
            resp = await http.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": agent_id,
                    "model": "x",
                    "content": [{"role": "user", "content": "hi"}],
                },
            )
            assert resp.status_code == 202
            await _await_bg_turn_task(conv)

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _post_bg("ag_recur_a")
        await _post_bg("ag_recur_b")
        # A recurs. Its (harness, delivery) pair was already warned for on the
        # first turn and must not warn again, however many other values have
        # been seen in between.
        await _post_bg("ag_recur_a")

    not_delivered = [
        rec for rec in caplog.records if "instructions are not delivered" in rec.getMessage()
    ]
    undetermined = [
        rec
        for rec in caplog.records
        if "declares no instruction_delivery capability" in rec.getMessage()
    ]
    assert len(not_delivered) == 1, (
        f"agent A's not-delivered value must warn exactly once across A->B->A — "
        f"got {len(not_delivered)}: {[r.getMessage() for r in not_delivered]}. Two "
        f"means the marker only remembered the LAST value warned for, so A looked "
        f"unseen once B had displaced it."
    )
    assert len(undetermined) == 1, (
        f"agent B's distinct (unknown) value must warn exactly once — got {len(undetermined)}."
    )


@pytest.mark.asyncio
async def test_skills_fill_parked_past_invalidation_does_not_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ISOLATED race for the ``_session_skills_cache`` write.

    Parks inside skills DISCOVERY (``resolve_harness_skills``, which runs in a
    worker thread after the spec is already resolved and cached), not in the
    shared spec resolver — so the spec write has completed and is not part of
    what is being tested. Reset while parked, release, then read ``/skills``
    again: discovery must run a second time. If the parked fill published, the
    follow-up finds its entry (tagged for the same agent, TTL unexpired) and
    never discovers again.

    Isolated to the skills guard: the spec cache is evicted by the reset
    regardless, so discovery would re-run on that account alone.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import threading

    import omnigent.runner.app as runner_app_mod

    conv = "conv_race_skills"
    agent_id = "ag_race_skills"
    discover_calls = 0
    parked = threading.Event()
    release = threading.Event()

    def _blocking_resolve_harness_skills(ctx: object, harness: object) -> list[Any]:
        nonlocal discover_calls
        del ctx, harness
        discover_calls += 1
        if discover_calls == 1:
            parked.set()
            release.wait(timeout=10.0)
        return []

    monkeypatch.setattr(runner_app_mod, "resolve_harness_skills", _blocking_resolve_harness_skills)

    async def _spec_resolver(resolved_agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        return AgentSpec(
            spec_version=1,
            name=f"spec-{resolved_agent_id}",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        )

    class _StableAgentSnapshotClient(NullServerClient):
        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": agent_id}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(_FakeHarnessClient([]))),
        spec_resolver=_spec_resolver,
        server_client=_StableAgentSnapshotClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        in_flight = asyncio.create_task(http.get(f"/v1/sessions/{conv}/skills"))
        await asyncio.get_running_loop().run_in_executor(None, parked.wait, 10.0)

        reset = await http.post(f"/v1/sessions/{conv}/reset-state")
        assert reset.status_code == 200, reset.text

        release.set()
        assert (await in_flight).status_code == 200

        follow_up = await http.get(f"/v1/sessions/{conv}/skills")
        assert follow_up.status_code == 200, follow_up.text

    assert discover_calls >= 2, (
        f"skills discovery must re-run after the mid-flight invalidation — ran "
        f"{discover_calls} time(s). Exactly one means the parked skills fill "
        f"published after the reset and satisfied the follow-up read."
    )


@pytest.mark.asyncio
async def test_cursor_model_names_fill_parked_past_invalidation_does_not_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ISOLATED race for the ``_session_cursor_model_names`` write.

    Parks inside cursor model DISCOVERY, which runs in a worker thread after
    the spec is resolved and cached, so the spec write is complete and is not
    part of the subject. Reset while parked, release, then drive a model
    change: the confirmation must carry no expected display name. A published
    mapping survives the reset and confirms against the previous agent's
    display names.

    Observed at the read site rather than on the container, because the
    endpoint re-runs discovery on every call — a follow-up request cannot
    distinguish a dropped write from a republished one.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import threading

    from omnigent import cursor_native, cursor_native_bridge

    conv = "conv_race_cursor_models"
    agent_id = "ag_race_cursor_models"
    parked = threading.Event()
    release = threading.Event()
    injected: list[tuple[str, str | None]] = []

    def _blocking_list_cursor_cli_model_options() -> list[dict[str, str]]:
        parked.set()
        release.wait(timeout=10.0)
        return [{"id": "sonnet-4-thinking", "displayName": "Sonnet 4 Thinking"}]

    def _inject_model(
        _bridge_dir: Path,
        *,
        model: str,
        expected_display_name: str | None,
        timeout_s: float,
    ) -> None:
        del timeout_s
        injected.append((model, expected_display_name))

    monkeypatch.setattr(
        cursor_native, "list_cursor_cli_model_options", _blocking_list_cursor_cli_model_options
    )
    monkeypatch.setattr(cursor_native_bridge, "inject_model_command", _inject_model)

    async def _no_op_auto_create(*args: object, **kwargs: object) -> None:
        # Terminal launch is unrelated to this race and needs a cursor-agent
        # CLI this environment does not provide.
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_cursor_terminal", _no_op_auto_create
    )

    async def _spec_resolver(resolved_agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        return AgentSpec(
            spec_version=1,
            name=f"spec-{resolved_agent_id}",
            executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
        )

    class _StableAgentSnapshotClient(NullServerClient):
        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": agent_id}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(_FakeHarnessClient([]))),
        spec_resolver=_spec_resolver,
        server_client=_StableAgentSnapshotClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        create = await http.post("/v1/sessions", json={"session_id": conv, "agent_id": agent_id})
        assert create.status_code == 201, create.text

        in_flight = asyncio.create_task(http.get(f"/v1/sessions/{conv}/cursor-model-options"))
        await asyncio.get_running_loop().run_in_executor(None, parked.wait, 10.0)

        reset = await http.post(f"/v1/sessions/{conv}/reset-state")
        assert reset.status_code == 200, reset.text

        release.set()
        assert (await in_flight).status_code == 200

        # Re-establish the spec and dispatched-agent state the reset evicted,
        # so the model-change event routes to the cursor handler and reaches
        # the mapping read.
        recreate = await http.post("/v1/sessions", json={"session_id": conv, "agent_id": agent_id})
        assert recreate.status_code == 201, recreate.text

        change = await http.post(
            f"/v1/sessions/{conv}/events",
            json={"type": "model_change", "model": "sonnet-4-thinking"},
        )
        assert change.status_code == 204, change.text

    assert injected and injected[-1][1] is None, (
        f"the model-name mapping resolved before the mid-flight invalidation "
        f"must not publish after it — confirmation used {injected!r}. A "
        f"surviving entry confirms against the previous agent's display names."
    )


@pytest.mark.asyncio
async def test_agent_marker_parked_past_invalidation_does_not_publish() -> None:
    """
    ISOLATED race for the ``_session_agent_ids`` marker write.

    The marker is the identity the provenance-gated cache reads key off, so a
    stale one re-points later reads at an agent the session has moved off.
    Parks the direct-stream turn inside spec resolution, resets while it is
    parked, switches the server snapshot to a DIFFERENT agent, then releases.
    A ``/models`` read follows: it hints with the marker, so if the parked
    turn republished its agent the read resolves that agent again and never
    consults the new snapshot.

    Driven entirely through the public endpoints — the marker itself is a
    per-app closure local, and the observable consequence is which agent the
    follow-up read resolves.
    """
    conv = "conv_race_agent_marker"
    resolver_calls: list[str] = []
    parked = asyncio.Event()
    release = asyncio.Event()
    snapshot_agent = {"id": "agent_a"}

    async def _spec_resolver(resolved_agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        resolver_calls.append(resolved_agent_id)
        if len(resolver_calls) == 1:
            parked.set()
            await release.wait()
        return AgentSpec(
            spec_version=1,
            name=f"spec-{resolved_agent_id}",
            instructions="Agent instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        )

    class _SwitchableSnapshotClient(NullServerClient):
        async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
            del kwargs
            current = snapshot_agent["id"]

            class _Resp(NullServerClient._Response):
                status_code = 200

                def json(self) -> dict[str, object]:
                    return {"agent_id": current}

            if url.endswith(f"/v1/sessions/{conv}"):
                return _Resp()
            return await super().get(url)

    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(_FakeHarnessClient([]))),
        spec_resolver=_spec_resolver,
        server_client=_SwitchableSnapshotClient(),  # type: ignore[arg-type]
    )

    async with _runner_test_client(app) as http:
        in_flight = asyncio.create_task(
            http.post(
                f"/v1/sessions/{conv}/events?stream=true",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "agent_a",
                    "model": "x",
                    "content": [{"role": "user", "content": "hi"}],
                },
            )
        )
        await asyncio.wait_for(parked.wait(), timeout=5.0)

        reset = await http.post(f"/v1/sessions/{conv}/reset-state")
        assert reset.status_code == 200, reset.text

        # The session has moved on to a different agent while the turn above
        # is parked mid-resolution.
        snapshot_agent["id"] = "agent_b"

        release.set()
        await in_flight

        models = await http.get(f"/v1/sessions/{conv}/models")
        assert models.status_code == 200, models.text

    assert resolver_calls[-1] == "agent_b", (
        f"the follow-up read must resolve the session's CURRENT agent — "
        f"resolver saw {resolver_calls!r}. A trailing 'agent_a' means the "
        f"turn parked before the reset republished its marker, and the read "
        f"hinted with it instead of consulting the new snapshot."
    )


# ── Cross-path resolution contract ───────────────────────────────────────
#
# Three dispatch paths resolve the turn's spec independently:
#   1. direct ?stream=true with NO harness in the body  -> _resolve_effective_turn
#   2. direct ?stream=true WITH a known harness         -> _stream_message_to_harness
#   3. background non-stream POST /events              -> _run_turn_bg_setup_and_stream
#
# They are NOT unified, and deliberately so: unification needs nine
# independently varying behaviour policies, and no zero-behaviour-selection
# refactor can preserve current semantics, because the paths have observably
# OPPOSITE outcomes for a missing child and for a raising resolver. That
# divergence is correct — the three carry different transport contracts (a
# synchronous error response, a failure arriving asynchronously against an
# already-sent 202, and deliberate graceful degradation).
#
# The real defect was that the divergence was undocumented and unpinned, so any
# path could drift silently. The table below records it. It deliberately does NOT
# assert sameness where the semantics differ: asserting sameness would be false
# and would force a bad unification later. Changing any single path's
# behaviour makes this table fail, which is the point.

_CONTRACT_PARENT_WITH_CHILD = {
    "name": "root",
    "instructions": "Root instructions.",
    "child": "worker",
    "child_instructions": "Worker instructions.",
}


def _contract_root_spec(*, with_child: bool) -> AgentSpec:
    """Build the contract test's parent spec, with or without the child.

    :param with_child: Whether ``worker`` exists in the sub-agent tree.
    :returns: The root ``AgentSpec``.
    """
    return AgentSpec(
        spec_version=1,
        name=_CONTRACT_PARENT_WITH_CHILD["name"],
        instructions=_CONTRACT_PARENT_WITH_CHILD["instructions"],
        executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        sub_agents=(
            [
                AgentSpec(
                    spec_version=1,
                    name=_CONTRACT_PARENT_WITH_CHILD["child"],
                    instructions=_CONTRACT_PARENT_WITH_CHILD["child_instructions"],
                    executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
                )
            ]
            if with_child
            else []
        ),
    )


class _ContractSnapshotClient(NullServerClient):
    """Session snapshot naming the requested sub-agent.

    All three paths recover the sub-agent name from the snapshot
    (``_recover_sub_agent_name``), so one client drives every adapter and the
    scenario — not the fixture — is what differs between them.
    """

    def __init__(self, conv: str) -> None:
        super().__init__()
        self._conv = conv

    async def get(self, url: str, **kwargs: object) -> NullServerClient._Response:
        del kwargs
        _conv = self._conv

        class _Resp(NullServerClient._Response):
            status_code = 200

            def json(self) -> dict[str, object]:
                return {"agent_id": "ag_contract_root", "sub_agent_name": "worker"}

        if url.endswith(f"/v1/sessions/{_conv}"):
            return _Resp()
        return await super().get(url)


_CONTRACT_CALLER_INSTRUCTIONS = "Caller-supplied instructions."


async def _contract_run_no_harness(
    http: httpx.AsyncClient, conv: str, recording: _RecordingHarnessClient
) -> dict[str, Any]:
    """Adapter 1: direct ``?stream=true`` with no harness in the body."""
    resp = await http.post(
        f"/v1/sessions/{conv}/events?stream=true",
        json={
            "type": "message",
            "role": "user",
            "agent_id": "ag_contract_root",
            "model": "x",
            "content": [],
        },
    )
    return {
        "status": resp.status_code,
        "error": (resp.json().get("error") if resp.status_code >= 400 else None),
        "instructions": (
            recording.posted_bodies[-1].get("instructions") if recording.posted_bodies else None
        ),
    }


async def _contract_run_known_harness(
    http: httpx.AsyncClient, conv: str, recording: _RecordingHarnessClient
) -> dict[str, Any]:
    """Adapter 2: direct ``?stream=true`` with the harness already known."""
    resp = await http.post(
        f"/v1/sessions/{conv}/events?stream=true",
        json={
            "type": "message",
            "role": "user",
            "agent_id": "ag_contract_root",
            "harness": "hermes",
            "model": "x",
            "content": [],
            "instructions": _CONTRACT_CALLER_INSTRUCTIONS,
        },
    )
    return {
        "status": resp.status_code,
        "error": (resp.json().get("error") if resp.status_code >= 400 else None),
        "instructions": (
            recording.posted_bodies[-1].get("instructions") if recording.posted_bodies else None
        ),
    }


async def _contract_run_background(
    http: httpx.AsyncClient, conv: str, recording: _RecordingHarnessClient
) -> dict[str, Any]:
    """Adapter 3: background non-stream dispatch, observed via terminal status.

    The POST is answered 202 before resolution runs, so a failure can only be
    reported asynchronously — this adapter therefore reports the terminal
    ``session.status`` rather than an HTTP code.
    """
    resp = await http.post(
        f"/v1/sessions/{conv}/events",
        json={
            "type": "message",
            "role": "user",
            "agent_id": "ag_contract_root",
            "model": "x",
            "content": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 202, resp.text
    await _await_bg_turn_task(conv)
    statuses = await _drain_published_statuses(conv, until="failed", timeout=1.0)
    return {
        "status": resp.status_code,
        "terminal_status": statuses[-1] if statuses else None,
        "instructions": (
            recording.posted_bodies[-1].get("instructions") if recording.posted_bodies else None
        ),
    }


_CONTRACT_ADAPTERS = {
    "no_harness": _contract_run_no_harness,
    "known_harness": _contract_run_known_harness,
    "background": _contract_run_background,
}


def _contract_resolver_for(scenario: str, calls: list[str]) -> Any:
    """Build the scenario's ``spec_resolver``.

    :param scenario: Contract scenario key.
    :param calls: Mutable list each invocation appends to, so a scenario can
        assert how many resolutions a path actually performed.
    :returns: An async resolver matching that scenario's failure mode.
    """

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec | None:
        del session_id
        calls.append(agent_id)
        if scenario == "resolver_raises":
            raise RuntimeError("contract: resolver unavailable")
        if scenario == "resolver_none":
            return None
        return _contract_root_spec(with_child=(scenario != "child_missing"))

    return _resolver


@pytest.mark.parametrize(
    "scenario, path, expected",
    [
        # ── Child present: the one scenario where all three genuinely agree.
        # Each selects the SAME child identity, to the extent its transport
        # lets us observe it (the two direct paths reach the harness and can be
        # read off the wire; background does too).
        pytest.param(
            "child_present",
            "no_harness",
            {"status": 200, "instructions": "Worker instructions."},
            id="child_present-no_harness",
        ),
        pytest.param(
            "child_present",
            "known_harness",
            # Same child selected; the trailing caller text is this adapter's
            # own per-request instructions composing additively on top (only
            # the known-harness adapter sends any, because its degradation
            # rows need something to preserve). The CHILD half is the shared
            # assertion — the suffix is adapter asymmetry, not divergence.
            {
                "status": 200,
                "instructions": (f"Worker instructions.\n\n{_CONTRACT_CALLER_INSTRUCTIONS}"),
            },
            id="child_present-known_harness",
        ),
        pytest.param(
            "child_present",
            "background",
            {"terminal_status": "idle", "instructions": "Worker instructions."},
            id="child_present-background",
        ),
        # ── Missing child: all three paths AGREE, and the agreement is the
        # point. A ``sub_agent_name`` that no longer resolves is one question,
        # and the answer does not depend on which transport asked it: warn,
        # then run the PARENT's spec. The rows below pin the parent's authored
        # text on every path, so a path that reverted to erroring — or to
        # dropping the spec — fails here rather than drifting quietly.
        # These paths used to answer 503, degrade to no spec, and fail the
        # turn respectively.
        pytest.param(
            "child_missing",
            "no_harness",
            {"status": 200, "error": None, "instructions": "Root instructions."},
            id="child_missing-no_harness-parent",
        ),
        pytest.param(
            "child_missing",
            "known_harness",
            # The caller's own per-request text still composes additively on
            # top of the parent's authored instructions rather than being
            # replaced by them — the same shape as the child_present row.
            {
                "status": 200,
                "instructions": (f"Root instructions.\n\n{_CONTRACT_CALLER_INSTRUCTIONS}"),
            },
            id="child_missing-known_harness-parent",
        ),
        pytest.param(
            "child_missing",
            "background",
            {"status": 202, "terminal_status": "idle", "instructions": "Root instructions."},
            id="child_missing-background-parent",
        ),
        # ── Resolver raises: same three-way split, different trigger.
        pytest.param(
            "resolver_raises",
            "no_harness",
            {"status": 503, "error": "spec_resolver_failed"},
            id="resolver_raises-no_harness-503",
        ),
        pytest.param(
            "resolver_raises",
            "known_harness",
            {"status": 200, "instructions": _CONTRACT_CALLER_INSTRUCTIONS},
            id="resolver_raises-known_harness-degrades",
        ),
        pytest.param(
            "resolver_raises",
            "background",
            {"status": 202, "terminal_status": "failed"},
            id="resolver_raises-background-async-failure",
        ),
        # ── Resolver returns None: a legitimate "no such agent" answer rather
        # than a transport failure, so the split is NOT the same as above.
        pytest.param(
            "resolver_none",
            "no_harness",
            # NOT a 503, unlike this path's other two failure rows. A resolver
            # returning None is a legitimate "no spec" answer rather than a
            # transport failure, so this path falls back and dispatches with no
            # composed instructions instead of erroring. This asymmetry within
            # a single path is exactly the kind of thing that used to be
            # invisible.
            {"status": 200, "error": None, "instructions": None},
            id="resolver_none-no_harness-falls-back",
        ),
        pytest.param(
            "resolver_none",
            "known_harness",
            {"status": 200, "instructions": _CONTRACT_CALLER_INSTRUCTIONS},
            id="resolver_none-known_harness-continues-unknown-spec",
        ),
        pytest.param(
            "resolver_none",
            "background",
            # ``status`` alone asserts nothing here: the adapter hard-asserts
            # 202 before it waits and always reports it, so a row expecting
            # only that would pass whether the turn finished, failed async, or
            # never reached a harness at all. The discriminating keys are the
            # terminal status and the absence of composed instructions.
            #
            # ``resolver_calls == 2`` is pinned deliberately. A None result
            # is NOT cached — a cached None would serve a stale negative to
            # later reads — so each consumer re-asks: once in background
            # setup, once again for composition.
            # The count documents that consequence; it is not a claim that
            # resolving twice is desirable.
            {
                "status": 202,
                "terminal_status": "idle",
                "instructions": None,
                "resolver_calls": 2,
            },
            id="resolver_none-background",
        ),
        # ── Cache already holds the effective child. Session-create resolved
        # and cached "worker" itself; the dispatch must reuse that entry.
        # ``resolver_calls == 1`` is the load-bearing assertion — it is the
        # create's own resolution and nothing more, so a second resolve would
        # mean the shortcut was lost.
        pytest.param(
            "cache_holds_child",
            "background",
            {
                "terminal_status": "idle",
                "instructions": "Worker instructions.",
                "resolver_calls": 1,
            },
            id="cache_holds_child-background-shortcut",
        ),
        pytest.param(
            "cache_holds_child",
            "known_harness",
            {
                "status": 200,
                "instructions": (f"Worker instructions.\n\n{_CONTRACT_CALLER_INSTRUCTIONS}"),
                "resolver_calls": 1,
            },
            id="cache_holds_child-known_harness-shortcut",
        ),
        # Pinned SEPARATELY, and it genuinely differs: this path does
        # NOT take the cached-child shortcut. It resolves a second time
        # (``resolver_calls == 2``) and arrives at the same child, so the
        # observable instructions match while the work done does not. Recorded
        # as-is rather than "corrected" — whether the extra resolve is worth
        # removing is a separate question from pinning today's behaviour.
        pytest.param(
            "cache_holds_child",
            "no_harness",
            {"status": 200, "instructions": "Worker instructions.", "resolver_calls": 2},
            id="cache_holds_child-no_harness-resolves-again",
        ),
    ],
)
@pytest.mark.asyncio
async def test_cross_path_resolution_contract(
    scenario: str,
    path: str,
    expected: dict[str, Any],
) -> None:
    """Pin the resolution behaviour matrix across all three dispatch paths.

    Reading the table: ``child_present`` and ``child_missing`` are the
    scenarios where all three paths agree, and for ``child_missing`` the
    agreement is load-bearing — an unresolvable ``sub_agent_name`` gets one
    answer (warn, then the parent's spec) no matter which transport asked.
    The remaining scenarios still record transport-driven differences that
    are intended: a synchronous 503, graceful degradation preserving the
    caller's own instructions, and an asynchronous terminal ``failed``
    against a 202 that has already gone out. Those differences come from
    what each transport can report, not from differing answers.

    If you change one path and a row here starts failing, that is the point:
    decide whether the contract moved, do not quietly re-align the table.

    :param scenario: Which resolution outcome the fake resolver produces.
    :param path: Which dispatch path adapter drives the turn.
    :param expected: Subset of the adapter's normalized result to assert.
    """
    conv = f"conv_contract_{scenario}_{path}"
    calls: list[str] = []
    recording = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording)),
        spec_resolver=_contract_resolver_for(scenario, calls),
        server_client=_ContractSnapshotClient(conv),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        if scenario == "cache_holds_child":
            # Session-create resolves "worker" and caches the CHILD spec
            # directly, which is the common real shape. The dispatch below
            # must then reuse it rather than resolving again.
            created = await http.post(
                "/v1/sessions",
                json={
                    "session_id": conv,
                    "agent_id": "ag_contract_root",
                    "sub_agent_name": "worker",
                },
            )
            assert created.status_code == 201, created.text
            assert len(calls) == 1, f"session-create should resolve once, got {calls!r}"
        result = await _CONTRACT_ADAPTERS[path](http, conv, recording)
    result["resolver_calls"] = len(calls)

    for key, want in expected.items():
        assert result.get(key) == want, (
            f"cross-path contract drift: scenario={scenario!r} path={path!r} "
            f"key={key!r} expected {want!r}, got {result.get(key)!r}. "
            f"Full observed result: {result!r}. If this change is intended, "
            f"update the matrix deliberately — where paths differ, they differ "
            f"because their transports report differently, not because they "
            f"answer the same question differently."
        )


@pytest.mark.parametrize(
    "caller_instructions, expected",
    [
        pytest.param(None, "Agent authored instructions.", id="agent-text-only"),
        pytest.param(
            "Answer only in French.",
            "Agent authored instructions.\n\nAnswer only in French.",
            id="agent-text-plus-per-request",
        ),
    ],
)
@pytest.mark.asyncio
async def test_direct_stream_composes_instructions_for_ordinary_harness(
    caller_instructions: str | None,
    expected: str,
) -> None:
    """A direct ``?stream=true`` turn composes instructions for ORDINARY harnesses.

    The harness-conditional swap only ever wrote the composed value back for
    the two harnesses whose executors read the wire field directly. Every
    other delivery harness — codex, copilot, open-responses, openai-agents,
    pi — kept whatever raw text the caller happened to send, so an agent's own
    authored instructions never reached them, and a turn carrying no caller
    text got no ``instructions`` field at all. The background dispatch path
    has always composed here; this path silently did not.

    Both arms assert the standard additive composition the background path
    produces: agent text alone, and agent text with the turn's per-request
    text appended.

    :param caller_instructions: Per-request text on the wire, or ``None``.
    :param expected: The composed value the harness must receive.
    """
    conv = f"conv_ordinary_compose_{'with' if caller_instructions else 'without'}_request"

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="codex-agent",
            instructions="Agent authored instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "codex"}),
        )

    recording = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording)),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    _body: dict[str, Any] = {"agent_id": "ag_ordinary", "harness": "codex"}
    if caller_instructions is not None:
        _body["instructions"] = caller_instructions
    async with _runner_test_client(app) as http:
        response = await _post_stream_message(http, conv, **_body)
        assert response.status_code == 200

    assert recording.posted_bodies, "harness never received a request"
    assert recording.posted_bodies[0].get("instructions") == expected, (
        f"an ordinary harness must receive the composed instructions — got "
        f"{recording.posted_bodies[0].get('instructions')!r}, expected {expected!r}. "
        f"A value of {caller_instructions!r} (or None) means the direct path "
        f"delivered only the caller's raw text and dropped the agent's own."
    )


@pytest.mark.asyncio
async def test_background_turn_composes_agent_and_per_request_instructions() -> None:
    """A BACKGROUND (non-stream) turn delivers agent text AND per-request text.

    The direct ``?stream=true`` path threads this separately, and the
    cross-path background adapter sends no ``instructions`` field at all,
    leaving the background path's own threading of the raw
    caller text: it captures it before composition, carries it out-of-band on
    ``TurnDispatch.per_request_instructions`` (the wire field by then holds the
    composed string, so the two are not interchangeable), and hands it back to
    composition downstream. Breaking any link in that chain silently drops the
    caller's instructions, with nothing else in the turn signalling the loss.
    """
    conv = "conv_bg_per_request_instructions"

    async def _spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(
            spec_version=1,
            name="hermes-agent",
            instructions="Agent authored instructions.",
            executor=ExecutorSpec(type="omnigent", config={"harness": "hermes"}),
        )

    recording = _RecordingHarnessClient(_INSTRUCTION_WARN_CHUNKS)
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _FakeProcessManager(recording)),
        spec_resolver=_spec_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_test_client(app) as http:
        resp = await http.post(
            f"/v1/sessions/{conv}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "ag_bg_per_request",
                "model": "x",
                "instructions": "Answer only in French.",
                "content": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 202, resp.text
        await _await_bg_turn_task(conv)

    assert recording.posted_bodies, "harness never received a request"
    _wire = recording.posted_bodies[-1].get("instructions")
    assert _wire == "Agent authored instructions.\n\nAnswer only in French.", (
        f"the background path must deliver the agent's text AND this turn's "
        f"per-request text — got {_wire!r}. 'Agent authored instructions.' "
        f"alone means the raw caller text was dropped somewhere between the "
        f"request body and composition."
    )
