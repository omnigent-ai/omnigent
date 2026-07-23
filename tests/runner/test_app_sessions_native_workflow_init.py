"""Tests for native session initialization, dispatch, status, and streams."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.runner import tool_dispatch as _tool_dispatch
from omnigent.runner.app import (
    _RUNNER_DISPATCHED_FIELD,
    ResolvedSpec,
    _resolved_workdir_for_spec,
    _session_labels_for_runner_spawn,
)
from omnigent.runner.resource_registry import (
    SessionResourceRegistry,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec, LocalToolInfo
from tests.runner.conftest import (
    _build_app_with_mcp_tool,
    _build_interrupt_app,
    _build_lifecycle_app,
    _FakeFileServerClient,
    _FakeProcessManager,
    _ReadTimeoutTransport,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
async def test_session_labels_for_runner_spawn_timeout_is_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Timed-out optional label resolution returns the spawn fallback quietly.

    Native harness spawn can recover by using the session id when labels
    cannot be fetched. A slow Omnigent session lookup therefore must not emit a
    warning with traceback; that was noisy and misleading for a best-effort
    lookup.

    :param caplog: Pytest log capture fixture.
    :returns: None.
    """
    transport = _ReadTimeoutTransport()
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        with caplog.at_level(logging.DEBUG, logger="omnigent.runner.app"):
            labels = await _session_labels_for_runner_spawn(
                server_client=client,
                session_id="1dbe53c9796da07f3960b9226435a5c8",
            )

    assert labels == {}
    assert [(request.method, request.url.path) for request in transport.requests] == [
        ("GET", "/v1/sessions/1dbe53c9796da07f3960b9226435a5c8/labels")
    ]
    timeout = transport.requests[0].extensions.get("timeout")
    assert isinstance(timeout, dict)
    assert timeout["read"] == 1.0

    timeout_records = [
        record
        for record in caplog.records
        if "Timed out resolving session labels" in record.getMessage()
    ]
    assert len(timeout_records) == 1
    assert timeout_records[0].levelno == logging.DEBUG
    assert timeout_records[0].exc_info is None
    assert "Failed to resolve session labels" not in caplog.text


@pytest.mark.asyncio
async def test_session_labels_for_runner_spawn_empty_200_body_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A 200 response with an empty (non-JSON) body returns the fallback.

    The Databricks Apps proxy can return HTTP 200 with an empty body
    when the server event loop is starved. Parsing that with
    ``resp.json()`` raises ``JSONDecodeError``; left unguarded it
    propagated out of ``_ensure_comment_relay_started`` and aborted
    every message turn before any LLM call (observed in production:
    "turn setup failed: Expecting value: line 1 column 1 (char 0)").
    Labels are a best-effort spawn hint, so a bad body must degrade to
    ``{}`` like the timeout / non-200 paths — not raise.

    :param caplog: Pytest log capture fixture.
    :returns: None.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        # 200 with an empty body — the exact proxy-under-load shape.
        return httpx.Response(200, content=b"")

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
            labels = await _session_labels_for_runner_spawn(
                server_client=client,
                session_id="2d4033c255b393808b12437cbdc9c47f",
            )

    # Recovered to the fallback instead of raising JSONDecodeError —
    # if the guard is removed, this call raises and the test errors out.
    assert labels == {}
    # The non-JSON 200 is logged once at WARNING with no traceback;
    # absence of this record would mean the bad body was swallowed
    # silently (or, worse, that the guard never ran).
    json_records = [
        record
        for record in caplog.records
        if "Session labels response was not valid JSON" in record.getMessage()
    ]
    assert len(json_records) == 1
    assert json_records[0].levelno == logging.WARNING


@pytest.mark.asyncio
async def test_sessions_native_resolves_file_id_before_harness() -> None:
    """Remote runner resolves raw web ``file_id`` blocks before harness input."""
    harness_client = _ScriptedHarnessClient(
        [_sse({"type": "response.completed", "response": {"id": "resp_1"}})]
    )
    pm = _FakeProcessManager(harness_client)
    server_client = _FakeFileServerClient()
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/d43f93c220661ddaf203a63b45050304/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "0e36e3219954d2deaef06b8e2a936f38",
                "model": "test-agent",
                "content": [
                    {
                        "type": "input_image",
                        "file_id": "07b38328508bae2010c8b9933a310846",
                        "filename": "photo.png",
                    },
                    {"type": "input_text", "text": "what is this?"},
                ],
            },
        )

    assert resp.status_code == 202
    assert server_client.get_calls == [
        # file_id blocks are resolved first (before the harness sees them)...
        "/v1/sessions/d43f93c220661ddaf203a63b45050304/resources/files/07b38328508bae2010c8b9933a310846",
        "/v1/sessions/d43f93c220661ddaf203a63b45050304/resources/files/07b38328508bae2010c8b9933a310846/content",
        # ...then the cold in-memory cache is rehydrated from the store
        # (empty here) before the turn is dispatched.
        "/v1/sessions/d43f93c220661ddaf203a63b45050304/items",
    ]
    for _ in range(20):
        if harness_client.posted_bodies:
            break
        await asyncio.sleep(0.05)
    posted = harness_client.posted_bodies[0]
    image_block = posted["content"][0]["content"][0]
    assert image_block == {
        "type": "input_image",
        "filename": "photo.png",
        "image_url": "data:image/png;base64,cG5nLWJ5dGVz",
    }
    assert "file_id" not in image_block


@pytest.mark.asyncio
async def test_runner_session_tool_schemas_use_resolved_bundle_workdir(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    tool_dir = bundle_dir / "tools" / "python"
    tool_dir.mkdir(parents=True)
    (tool_dir / "bundle_tool.py").write_text(
        "from omnigent_client.tools import tool\n\n"
        "@tool\n"
        "def bundle_tool(text: str) -> str:\n"
        "    return text\n"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = AgentSpec(
        spec_version=1,
        name="bundle-agent",
        local_tools=[
            LocalToolInfo(
                name="bundle_tool",
                path="tools/python/bundle_tool.py",
                language="python",
            )
        ],
    )
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "status": "action_required",
                    "name": "bundle_tool",
                    "call_id": "call_bundle",
                    "arguments": json.dumps({"text": "from-bundle"}),
                },
            }
        ),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=workspace,
    )
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/c7f36aa769270cac30144784fad50acc/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "31ebfedf721b44dabd76f662cb70a400",
                "model": "bundle-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        for _ in range(20):
            if harness_client.posted_bodies:
                break
            await asyncio.sleep(0.05)
        for _ in range(100):
            if harness_client.patched_events:
                break
            await asyncio.sleep(0.05)

    assert harness_client.posted_bodies, "harness must receive the turn"
    schemas = harness_client.posted_bodies[0].get("tools") or []
    assert any(s.get("function", {}).get("name") == "bundle_tool" for s in schemas), (
        f"expected bundled local tool schema, got {schemas}"
    )


def test_resolved_workdir_for_spec_prefers_bundle_workdir(tmp_path: Path) -> None:
    """``_resolved_workdir_for_spec`` uses ``ResolvedSpec.workdir`` over fallback.

    Bundle-deployed agents carry their own workdir (where
    ``tools/python/*.py`` live). The dispatch path must thread that
    workdir into ``dispatch_tool_locally`` so native python tools are
    found at call time — not the generic ``runner_workspace``.
    """
    bundle_dir = tmp_path / "bundle"
    runner_workspace = tmp_path / "workspace"
    spec = AgentSpec(spec_version=1, name="bundle-agent")
    entry = ResolvedSpec(spec=spec, workdir=bundle_dir)

    assert _resolved_workdir_for_spec(entry, runner_workspace) == bundle_dir


def test_resolved_workdir_for_spec_falls_back_without_bundle(tmp_path: Path) -> None:
    """Non-bundle specs fall back to ``runner_workspace`` (prior behavior).

    A bare ``AgentSpec`` (no ResolvedSpec wrapper) or a ``ResolvedSpec``
    with ``workdir=None`` carries no bundle dir, so dispatch must keep
    using the CLI launch workspace exactly as base did.
    """
    runner_workspace = tmp_path / "workspace"
    bare_spec = AgentSpec(spec_version=1, name="plain-agent")

    # Unwrapped spec → no workdir → fallback.
    assert _resolved_workdir_for_spec(bare_spec, runner_workspace) == runner_workspace
    # ResolvedSpec with no workdir → fallback.
    wrapped_no_workdir = ResolvedSpec(spec=bare_spec, workdir=None)
    assert _resolved_workdir_for_spec(wrapped_no_workdir, runner_workspace) == runner_workspace
    # Missing fallback stays None (don't fabricate a path).
    assert _resolved_workdir_for_spec(bare_spec, None) is None


@pytest.mark.asyncio
async def test_sessions_native_dispatches_native_tool_with_bundle_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle agent's native python tool dispatches against the bundle workdir.

    End-to-end through ``POST /v1/sessions/{conv}/events`` (no live LLM):
    the scripted harness emits an ``action_required`` for a spec-declared
    python tool, and the runner must dispatch it locally with
    ``runner_workspace`` set to the resolved ``ResolvedSpec.workdir`` (the
    bundle dir), not the generic CLI ``runner_workspace``. This is the
    dispatch-time counterpart to
    :func:`test_runner_session_tool_schemas_use_resolved_bundle_workdir`,
    which only proved schema generation used the bundle workdir.
    """
    bundle_dir = tmp_path / "bundle"
    tool_dir = bundle_dir / "tools" / "python"
    tool_dir.mkdir(parents=True)
    (tool_dir / "bundle_tool.py").write_text(
        "from omnigent_client.tools import tool\n\n"
        "@tool\n"
        "def bundle_tool(text: str) -> str:\n"
        "    return text\n"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = AgentSpec(
        spec_version=1,
        name="bundle-agent",
        local_tools=[
            LocalToolInfo(
                name="bundle_tool",
                path="tools/python/bundle_tool.py",
                language="python",
            )
        ],
    )

    captured_workspaces: list[Path | None] = []

    async def _fake_dispatch(*, runner_workspace: Path | None = None, **kwargs: Any) -> str:
        captured_workspaces.append(runner_workspace)
        return "ok"

    monkeypatch.setattr(_tool_dispatch, "dispatch_tool_locally", _fake_dispatch)

    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "status": "action_required",
                    "name": "bundle_tool",
                    "call_id": "call_bundle",
                    "arguments": json.dumps({"text": "from-bundle"}),
                },
            }
        ),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=workspace,
    )
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/c7f36aa769270cac30144784fad50acc/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "31ebfedf721b44dabd76f662cb70a400",
                "model": "bundle-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        for _ in range(100):
            if captured_workspaces:
                break
            await asyncio.sleep(0.05)

    assert captured_workspaces, "native tool must be dispatched locally"
    assert captured_workspaces[0] == bundle_dir, (
        "dispatch must use the resolved bundle workdir, not runner_workspace "
        f"({workspace!r}); got {captured_workspaces[0]!r}"
    )


@pytest.mark.asyncio
async def test_sessions_native_marks_and_clears_in_flight_turn() -> None:
    """proxy_stream registers the live turn with the process manager.

    Regression test for #1414. The idle reaper skips conversations present in
    the manager's ``_in_flight_response_ids``, but that map had no writers, so
    a turn running past the idle window was reaped mid-stream. The runner must
    call ``mark_in_flight`` on ``response.created`` (so the reaper spares the
    live turn) and ``clear_in_flight`` at stream end (so the now-idle entry can
    later be reclaimed — not leaked, cf. #1349). Before the fix the runner
    never called either, so both recorded lists stay empty.
    """
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_live"}}),
        _sse({"type": "response.completed", "response": {"id": "resp_live"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)
    spec = AgentSpec(spec_version=1, name="plain-agent")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/ce84b0dc308668bb715607e42ae268b0/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "e61df75e32ee590087e03aa37b33abac",
                "model": "plain-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        # Wait for the background turn to finish (clear runs at stream end).
        for _ in range(100):
            if pm.cleared_in_flight:
                break
            await asyncio.sleep(0.05)

    # Live turn was registered with the reaper's in-flight guard on
    # response.created, then cleared once the stream ended.
    assert pm.marked_in_flight == [("ce84b0dc308668bb715607e42ae268b0", "resp_live")], (
        pm.marked_in_flight
    )
    assert pm.cleared_in_flight == ["ce84b0dc308668bb715607e42ae268b0"], pm.cleared_in_flight


class _StreamErrorHarnessClient(_ScriptedHarnessClient):
    """Harness whose stream yields its frames then drops mid-stream.

    Mirrors the production reaper-kill failure: after ``response.created``
    the per-conversation client is force-closed and ``aiter_text`` raises
    ``httpx.ReadError``, which proxy_stream surfaces as the
    "Harness stream connection error." terminal failure.
    """

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Return a context manager whose stream errors after the frames."""
        del method, url, timeout
        self.posted_bodies.append(json)
        frames = self._sse_frames

        class _ErrCtx:
            status_code = 200

            async def __aenter__(self) -> _StreamErrorHarnessClient._ErrHandle:
                return _StreamErrorHarnessClient._ErrHandle(frames)

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _ErrCtx()

    class _ErrHandle:
        """Stream handle that raises ``ReadError`` after yielding its frames."""

        status_code = 200

        def __init__(self, frames: list[str]) -> None:
            """Store the frames to yield before erroring."""
            self._frames = frames

        async def aiter_text(self) -> AsyncIterator[str]:
            """Yield each scripted frame, then drop the stream mid-flight."""
            for frame in self._frames:
                yield frame
            raise httpx.ReadError("harness subprocess closed mid-stream")


@pytest.mark.asyncio
async def test_sessions_native_clears_in_flight_when_stream_errors() -> None:
    """clear_in_flight fires even when a turn ends abnormally.

    The fix clears the reaper's in-flight marker in ``_on_proxy_stream_end``,
    which is reached on every terminal path — not only on ``response.completed``.
    A turn that streams ``response.created`` and then drops mid-stream (exactly
    the reaper-kill failure: ``httpx.ReadError`` → "Harness stream connection
    error.") must still clear the marker; a missed clear would leave the entry
    permanently in-flight and therefore never reaped — the inverse of #1414
    (cf. #1349).
    """
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_drop"}}),
    ]
    harness_client = _StreamErrorHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)
    spec = AgentSpec(spec_version=1, name="plain-agent")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/9217a860245985f541fd686eb2a32b73/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "965906f5d9fb596610dda599a80faaee",
                "model": "plain-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        # Wait for the background turn to error out (clear runs at stream end).
        for _ in range(100):
            if pm.cleared_in_flight:
                break
            await asyncio.sleep(0.05)

    # Marked live on response.created, then cleared despite the mid-stream drop.
    assert pm.marked_in_flight == [("9217a860245985f541fd686eb2a32b73", "resp_drop")], (
        pm.marked_in_flight
    )
    assert pm.cleared_in_flight == ["9217a860245985f541fd686eb2a32b73"], pm.cleared_in_flight


@pytest.mark.asyncio
async def test_sessions_native_clears_in_flight_on_context_overflow_live_stream() -> None:
    """clear_in_flight fires for a live (``stream=true``) turn that overflows context.

    Regression for a leak where a context-window overflow on the live-stream
    path left the reaper's in-flight marker set forever: proxy_stream raised
    _ContextWindowOverflow uncaught on this path, so _on_proxy_stream_end never
    ran and the idle reaper (which skips anything in-flight) never reclaimed
    the harness. The background-turn path already handled this; live turns did
    not.
    """
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_overflow"}}),
        _sse(
            {
                "type": "response.failed",
                "error": {
                    "message": (
                        "context_length_exceeded: 5000 tokens > 4096 maximum context length"
                    ),
                    "code": "context_length_exceeded",
                },
            }
        ),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)
    spec = AgentSpec(spec_version=1, name="plain-agent")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    conv_id = "b4f6a4f0f2f74d76a2e4c0c9a8e0f9aa"
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "965906f5d9fb596610dda599a80faaee",
                "model": "plain-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )
        # Drain the live SSE stream like a real browser client would. Pre-fix
        # this can surface the uncaught overflow as a transport error; either
        # way the assertions below are what pin the regression.
        with contextlib.suppress(Exception):
            async for _chunk in resp.aiter_text():
                pass

    # Marked live on response.created, then cleared despite the overflow.
    assert pm.marked_in_flight == [(conv_id, "resp_overflow")], pm.marked_in_flight
    assert pm.cleared_in_flight == [conv_id], (
        f"in-flight marker never cleared on live-stream context overflow "
        f"(got {pm.cleared_in_flight}) -- the reaper would skip this "
        f"conversation's harness forever"
    )


@pytest.mark.asyncio
async def test_stop_session_clears_in_flight_marker() -> None:
    """A mid-stream cancel clears the reaper's in-flight marker.

    Guards the in-flight-tracking contract against a #1349-class inverse leak:
    because ``mark_in_flight`` (set on ``response.created``) *persists*, a
    cancel must still clear it, or ``has_active_turn`` stays true and the idle
    reaper skips the subprocess forever. The clear happens because cancelling
    the turn task raises ``CancelledError`` into ``_run_turn_bg``'s handler,
    which runs ``_on_proxy_stream_end`` (→ ``clear_in_flight``). This test locks
    that path so a future change to the cancel teardown can't silently strand
    the marker.
    """
    import asyncio as _aio

    gate = _aio.Event()  # never set → harness blocks after response.created
    app, pm, hc = _build_interrupt_app(gate)
    conv_id = "a136ad3e8265e86eba8564d6cda81a14"
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "b528b24f9d6ece39ef11de7fb6dfeedf",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "blocked"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        # Wait until response.created is processed (marker set), then stop.
        await _aio.wait_for(hc.post_seen.wait(), timeout=5.0)
        for _ in range(100):
            if pm.marked_in_flight:
                break
            await _aio.sleep(0.05)
        assert pm.marked_in_flight == [(conv_id, "resp_int")], pm.marked_in_flight

        stop_resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "stop_session"},
        )
        assert stop_resp.status_code == 204, stop_resp.text
        gate.set()  # release the blocked stream so teardown completes cleanly
        for _ in range(100):
            if pm.cleared_in_flight:
                break
            await _aio.sleep(0.05)

    # The cancel teardown must have cleared the marker (else the reaper would
    # skip this subprocess forever and has_active_turn would stay true). Clear
    # is idempotent, so it may fire on more than one teardown path — what
    # matters is the end state: marked, then cleared, no longer active.
    assert conv_id in pm.cleared_in_flight, pm.cleared_in_flight
    assert not pm.has_active_turn(conv_id)


class _SignalOnCreatedHarnessClient(_ScriptedHarnessClient):
    """Streams its frames, firing an event the moment ``response.created`` is sent.

    Lets a test distinguish the lazy turn-spec resolution (which runs only after
    the harness has started streaming) from the eager setup-phase resolutions
    that precede it.
    """

    def __init__(self, sse_frames: list[str], created: asyncio.Event) -> None:
        """Store the frames and the event to fire on ``response.created``."""
        super().__init__(sse_frames)
        self._created = created

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        """Return a context manager that signals once ``response.created`` is sent."""
        del method, url, timeout
        self.posted_bodies.append(json)
        frames = self._sse_frames
        created = self._created

        class _Ctx:
            status_code = 200

            async def __aenter__(self) -> _SignalOnCreatedHarnessClient._Handle:
                return _SignalOnCreatedHarnessClient._Handle(frames, created)

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _Ctx()

    class _Handle:
        """Stream handle that fires *created* right after the ``response.created`` frame."""

        status_code = 200

        def __init__(self, frames: list[str], created: asyncio.Event) -> None:
            """Store the frames and the response.created signal."""
            self._frames = frames
            self._created = created

        async def aiter_text(self) -> AsyncIterator[str]:
            """Yield each frame, signalling once ``response.created`` has been sent."""
            for frame in self._frames:
                yield frame
                if '"response.created"' in frame:
                    self._created.set()


@pytest.mark.asyncio
async def test_sessions_native_clears_in_flight_on_lazy_spec_error() -> None:
    """A lazy turn-spec resolution failure mid-dispatch still clears the marker.

    Regression for a #1349-class inverse leak. For a non-MCP agent the turn
    spec is resolved lazily at tool-dispatch time — after ``response.created``
    has already set the in-flight marker. A transient resolver failure there
    drives ``proxy_stream``'s lazy-spec-error early ``return``, which (unlike a
    stream error or a cancel) exits the generator *cleanly* — so neither the
    drain nor ``_run_turn_bg``'s ``CancelledError`` handler runs
    ``_on_proxy_stream_end``. Routing that early return through
    ``_on_proxy_stream_end`` is what clears the marker; without it the reaper
    skips the subprocess forever and ``has_active_turn`` stays true.

    The resolver is gated on ``response.created`` so it succeeds for the two
    setup-phase resolutions (spec cache + harness pick) — letting the turn
    stream — and fails only on the lazy dispatch call.
    """
    import asyncio as _aio

    created = _aio.Event()
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_lazy"}}),
        _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "status": "action_required",
                    "name": "sys_list_models",
                    "call_id": "call_lazy",
                    "arguments": "{}",
                },
            }
        ),
        _sse({"type": "response.completed", "response": {"id": "resp_lazy"}}),
    ]
    harness_client = _SignalOnCreatedHarnessClient(sse_frames, created)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> Any:
        # Before the harness streams response.created the two setup-phase
        # resolutions run: return None (uncached spec → default harness) so the
        # turn streams without populating _session_spec_cache. Once streaming
        # has started the only caller is the lazy dispatch resolution — fail it.
        del agent_id, session_id
        if created.is_set():
            raise RuntimeError("transient lazy spec resolution failure")
        return None

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    conv_id = "a15313a5b85c6fa97a92d1e2d74d44dc"
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "4d198b17724988de49d7ac2b4d29605b",
                "model": "plain-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        # Wait for the background turn to finish; the marker should be cleared.
        for _ in range(100):
            if pm.cleared_in_flight:
                break
            await _aio.sleep(0.05)

    # Marked live on response.created; the lazy-spec-error early return must
    # still finalize the turn and clear the marker.
    assert pm.marked_in_flight == [(conv_id, "resp_lazy")], pm.marked_in_flight
    assert conv_id in pm.cleared_in_flight, pm.cleared_in_flight
    assert not pm.has_active_turn(conv_id)


@pytest.mark.asyncio
async def test_sessions_native_dispatches_builtin_tool_with_runner_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle agent's builtin OS-env tool dispatches in runner_workspace.

    Bundle workdirs are only for spec-local native python tools. Builtins
    such as ``sys_os_write`` run in the caller process and must keep the
    original runner workspace even when the agent spec was resolved from an
    extracted bundle directory.
    """
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = AgentSpec(spec_version=1, name="bundle-agent")

    captured_workspaces: list[Path | None] = []

    async def _fake_dispatch(*, runner_workspace: Path | None = None, **kwargs: Any) -> str:
        captured_workspaces.append(runner_workspace)
        return "ok"

    monkeypatch.setattr(_tool_dispatch, "dispatch_tool_locally", _fake_dispatch)

    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "status": "action_required",
                    "name": "sys_os_write",
                    "call_id": "call_write",
                    "arguments": json.dumps(
                        {"path": "created-by-tool.txt", "content": "from workspace"}
                    ),
                },
            }
        ),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=workspace,
    )
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/f690906478f5c81a97fd4301a80cb213/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "31ebfedf721b44dabd76f662cb70a400",
                "model": "bundle-agent",
                "content": [{"type": "input_text", "text": "write a file"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        for _ in range(100):
            if captured_workspaces:
                break
            await asyncio.sleep(0.05)

    assert captured_workspaces, "builtin tool must be dispatched locally"
    assert captured_workspaces[0] == workspace, (
        "builtin OS-env dispatch must use runner_workspace, not the bundle workdir "
        f"({bundle_dir!r}); got {captured_workspaces[0]!r}"
    )


@pytest.mark.asyncio
async def test_mcp_execute_dispatches_builtin_tool_with_runner_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/mcp/execute`` also keeps builtin OS-env tools in runner_workspace."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = AgentSpec(spec_version=1, name="bundle-agent")

    captured_workspaces: list[Path | None] = []

    async def _fake_execute_tool(*, runner_workspace: Path | None = None, **kwargs: Any) -> str:
        captured_workspaces.append(runner_workspace)
        return "ok"

    monkeypatch.setattr(_tool_dispatch, "execute_tool", _fake_execute_tool)

    harness_client = _ScriptedHarnessClient(
        [_sse({"type": "response.completed", "response": {"id": "resp_1"}})]
    )
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=workspace,
    )
    async with _runner_client(app) as client:
        seed_resp = await client.post(
            "/v1/sessions/38f6cf055029a2a23b227a8305f76c9d/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "31ebfedf721b44dabd76f662cb70a400",
                "model": "bundle-agent",
                "content": [{"type": "input_text", "text": "seed"}],
                "harness": "openai-agents",
            },
        )
        assert seed_resp.status_code == 202
        for _ in range(100):
            if harness_client.posted_bodies:
                break
            await asyncio.sleep(0.05)

        execute_resp = await client.post(
            "/v1/sessions/38f6cf055029a2a23b227a8305f76c9d/mcp/execute",
            json={
                "method": "tools/call",
                "params": {
                    "name": "sys_os_write",
                    "arguments": {"path": "created-by-tool.txt", "content": "from workspace"},
                },
            },
        )

    assert execute_resp.status_code == 200
    assert execute_resp.json() == {"result": {"output": "ok"}}
    assert captured_workspaces == [workspace]


@pytest.mark.asyncio
async def test_mcp_execute_dispatches_full_namespaced_mcp_tool_name() -> None:
    """``/mcp/execute`` must not strip the MCP server prefix before dispatch."""
    app, mcp_manager, _harness_client, _server_client = _build_app_with_mcp_tool(
        tool_name="jira__search_issues"
    )
    async with _runner_client(app) as client:
        seed_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "6a09e2c1b63301fc6be99bb645418905",
                "agent_id": "0e36e3219954d2deaef06b8e2a936f38",
            },
        )
        assert seed_resp.status_code == 201, seed_resp.text

        execute_resp = await client.post(
            "/v1/sessions/6a09e2c1b63301fc6be99bb645418905/mcp/execute",
            json={
                "method": "tools/call",
                "params": {
                    "name": "jira__search_issues",
                    "arguments": {"query": "asyncio"},
                },
            },
        )

    assert execute_resp.status_code == 200
    assert execute_resp.json() == {"result": {"output": "called jira__search_issues"}}
    assert mcp_manager.call_tool_invocations == [("jira__search_issues", {"query": "asyncio"})]


@pytest.mark.asyncio
async def test_sessions_native_path_injects_mcp_schemas() -> None:
    """``POST /v1/sessions/{conv}/events`` with a message body injects MCP schemas.

    Sessions-native clients must get the same MCP injection that the
    legacy ``/v1/responses`` path provides.
    """
    app, _mcp_manager, harness_client, _server_client = _build_app_with_mcp_tool()
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/4e92b5a0c0ee6db3f874f9c4a3f855a5/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "0e36e3219954d2deaef06b8e2a936f38",
                "model": "test-agent",
                "input": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
                "has_mcp_servers": True,
            },
        )
        # Sessions-native POST returns 202; the turn runs as a
        # background task. Wait for the background turn to complete.
        assert resp.status_code == 202
        await asyncio.sleep(0.1)

    assert harness_client.posted_bodies, "harness must receive at least one event"
    body = harness_client.posted_bodies[0]
    schemas = body.get("tools") or []
    assert any(s.get("name") == "jira_search_issues" for s in schemas), (
        f"MCP schema must be injected on sessions-native path; got {schemas}"
    )


@pytest.mark.asyncio
async def test_action_required_marker_round_trips_to_relayed_frame() -> None:
    """The runner stamps ``omnigent_runner_dispatched`` on action_required frames.

    The Omnigent executor's ``_runner_dispatches`` predicate reads this marker
    to skip server-side dispatch. Without the stamp it'd race the
    runner's dispatch and return "unknown server-side tool."
    """
    app, _mcp_manager, _client, server_client = _build_app_with_mcp_tool()
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/4e92b5a0c0ee6db3f874f9c4a3f855a5/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "0e36e3219954d2deaef06b8e2a936f38",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
                "has_mcp_servers": True,
            },
        )
        relayed = []
        async for chunk in resp.aiter_text():
            relayed.append(chunk)
    stream_text = "".join(relayed)

    # The relayed action_required frame must carry the marker.
    assert f'"{_RUNNER_DISPATCHED_FIELD}": true' in stream_text, (
        f"action_required event must be stamped with the dispatch marker; "
        f"stream text was {stream_text!r}"
    )
    # Runner dispatched the MCP tool through the Omnigent server proxy (AP mode).
    assert server_client.call_tool_invocations == [("jira_search_issues", {})], (
        f"runner must dispatch the MCP tool via ProxyMcpManager (AP server); "
        f"got {server_client.call_tool_invocations}"
    )


@pytest.mark.asyncio
async def test_create_session_threads_resolved_bundle_dir_to_codex_spawn_env(
    tmp_path: Path,
) -> None:
    """Session pre-spawn must include bundle-dir env for Codex skills.

    The real e2e flow creates the session before the first turn.
    ``HarnessProcessManager`` fixes env on first spawn and ignores env
    on later cache hits, so dropping the resolved bundle workdir here
    means the later turn cannot recover ``HARNESS_CODEX_BUNDLE_DIR``.
    Codex then only sees host/default skills, not bundled fixture
    skills.
    """
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    spec = AgentSpec(
        spec_version=1,
        name="codex-bundle-agent",
        skills_filter=["codex_e2e_xyz_greet_a3f9c2"],
        executor=ExecutorSpec(
            config={"harness": "codex", "profile": "test-profile"},
            model="databricks-gpt-5-4-mini",
        ),
    )
    harness_client = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness_client)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir)

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "415c9954e2fe4b9276083a4d2c66f689",
                "agent_id": "12c8c7631b209d1027416b4bf7604999",
            },
        )

    assert resp.status_code == 201
    assert pm.get_client_calls
    conversation_id, harness, env = pm.get_client_calls[-1]
    assert conversation_id == "415c9954e2fe4b9276083a4d2c66f689"
    assert harness == "codex"
    assert env is not None
    assert env["HARNESS_CODEX_BUNDLE_DIR"] == str(bundle_dir)
    assert env["HARNESS_CODEX_SKILLS_FILTER"] == '["codex_e2e_xyz_greet_a3f9c2"]'


@pytest.mark.asyncio
async def test_create_session_envelope_is_single_flight_and_skips_metadata_callbacks() -> None:
    """Concurrent v2 initialization resolves once and uses supplied metadata."""

    class _ServerClient:
        def __init__(self) -> None:
            self.get_paths: list[str] = []

        async def get(self, path: str, **_kwargs: Any) -> Any:
            self.get_paths.append(path)
            if path.endswith("/items"):
                return type(
                    "Response",
                    (),
                    {"status_code": 200, "json": lambda self: {"data": []}},
                )()
            raise AssertionError(f"unexpected metadata callback: {path}")

    server_client = _ServerClient()
    harness_client = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness_client)
    resolver_entered = asyncio.Event()
    release_resolver = asyncio.Event()
    resolver_calls = 0

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        nonlocal resolver_calls
        del agent_id, session_id
        resolver_calls += 1
        resolver_entered.set()
        await release_resolver.wait()
        return AgentSpec(spec_version=1, name="single-flight")

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
        resource_registry=SessionResourceRegistry(terminal_registry=None),
    )
    session_id = "initv2_8e32600337d08f59ad381caf96a90659"
    agent_id = "agentv2_880b5afda28ad55ff74cbeb9b5fc67fb"
    payload = {
        "session_id": session_id,
        "agent_id": agent_id,
        "sub_agent_name": None,
        "session_init": {
            "protocol_version": 2,
            "server_version": "0.6.0.dev0",
            "session_id": session_id,
            "agent_id": agent_id,
            "sub_agent_name": None,
            "snapshot": {
                "created_at": 1234,
                "updated_at": 1234,
                "workspace": None,
                "labels": {},
            },
        },
    }

    async with _runner_client(app) as client:
        first = asyncio.create_task(client.post("/v1/sessions", json=payload))
        await resolver_entered.wait()
        second = asyncio.create_task(client.post("/v1/sessions", json=payload))
        await asyncio.sleep(0)
        release_resolver.set()
        first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status_code == second_response.status_code == 201
    assert first_response.json()["created_at"] == 1234
    assert first_response.json()["session_init_protocol_version"] == 2
    assert resolver_calls == 1
    assert len(pm.get_client_calls) == 1
    assert server_client.get_paths == [f"/v1/sessions/{session_id}/items"]


@pytest.mark.asyncio
async def test_create_session_preserves_existing_event_queue() -> None:
    """Session init must not orphan a stream subscriber's event queue.

    The Omnigent relay's ``GET /stream`` lazily creates the per-session event
    queue when it connects before ``POST /v1/sessions`` runs (the relay
    can race ahead of init). Init used to *unconditionally replace* that
    queue, orphaning the relay on the now-dead object: ``_publish_event``
    then enqueued onto the new queue while the relay's generator blocked
    forever on the old one, so later events never reached the server. For
    claude-native that dropped the PTY-watcher ``idle`` edge (emitted
    asynchronously after the turn), stranding the session's web status at
    "working". Init must PRESERVE an existing queue — assert the
    pre-attached queue object survives init unchanged.
    """
    from omnigent.runner.app import _session_event_queues_ref

    app, _pm, _hc = _build_lifecycle_app()
    # Simulate the relay's GET /stream having already attached (lazily
    # created the queue) before init runs.
    sentinel: asyncio.Queue[Any] = asyncio.Queue()
    _session_event_queues_ref["943f9d13fadeff4db5bb295673530474"] = sentinel
    try:
        async with _runner_client(app) as client:
            resp = await client.post(
                "/v1/sessions",
                json={
                    "session_id": "943f9d13fadeff4db5bb295673530474",
                    "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
                },
            )
        assert resp.status_code == 201
        # Same object → a relay already blocked on it keeps receiving
        # events that ``_publish_event`` enqueues after init.
        assert _session_event_queues_ref.get("943f9d13fadeff4db5bb295673530474") is sentinel
    finally:
        _session_event_queues_ref.pop("943f9d13fadeff4db5bb295673530474", None)


@pytest.mark.asyncio
async def test_has_active_work_reports_process_manager_turns() -> None:
    """The runner idle watchdog sees active harness turns.

    :returns: None.
    """
    app, pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "8e32600337d08f59ad381caf96a90659",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )

    assert resp.status_code == 201
    assert app.state.has_active_work() is False

    pm.mark_turn_active("8e32600337d08f59ad381caf96a90659")

    assert app.state.has_active_work() is True


@pytest.mark.asyncio
async def test_create_session_missing_fields() -> None:
    """``POST /v1/sessions`` with missing fields returns 400."""
    app, _pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": "8e32600337d08f59ad381caf96a90659"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_session_scaffold_mode() -> None:
    """``POST /v1/sessions`` returns 501 when process_manager is None."""
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "8e32600337d08f59ad381caf96a90659",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
    assert resp.status_code == 501


@pytest.mark.asyncio
async def test_get_session_status_idle() -> None:
    """``GET /v1/sessions/{id}`` returns idle after session creation."""
    app, _pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={
                "session_id": "8e32600337d08f59ad381caf96a90659",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        resp = await client.get("/v1/sessions/8e32600337d08f59ad381caf96a90659")
    assert resp.status_code == 200
    assert resp.json()["status"] == "idle"


@pytest.mark.asyncio
async def test_get_session_status_running() -> None:
    """``GET /v1/sessions/{id}`` returns running when a turn is active."""
    app, pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={
                "session_id": "8e32600337d08f59ad381caf96a90659",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        pm.mark_turn_active("8e32600337d08f59ad381caf96a90659")
        resp = await client.get("/v1/sessions/8e32600337d08f59ad381caf96a90659")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"


@pytest.mark.asyncio
async def test_get_session_unknown() -> None:
    """``GET /v1/sessions/{id}`` returns 404 for unknown session."""
    app, _pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        resp = await client.get("/v1/sessions/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_session() -> None:
    """``DELETE /v1/sessions/{id}`` releases harness and cleans caches."""
    app, pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={
                "session_id": "8e32600337d08f59ad381caf96a90659",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        resp = await client.delete("/v1/sessions/8e32600337d08f59ad381caf96a90659")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["session_id"] == "8e32600337d08f59ad381caf96a90659"
    assert "8e32600337d08f59ad381caf96a90659" in pm.released
    assert not pm.has_session("8e32600337d08f59ad381caf96a90659")


@pytest.mark.asyncio
async def test_delete_session_with_active_turn() -> None:
    """``DELETE /v1/sessions/{id}`` cancels active turn before release."""
    app, pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        await client.post(
            "/v1/sessions",
            json={
                "session_id": "8e32600337d08f59ad381caf96a90659",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
        pm.mark_turn_active("8e32600337d08f59ad381caf96a90659")
        resp = await client.delete("/v1/sessions/8e32600337d08f59ad381caf96a90659")
    assert resp.status_code == 200
    assert "8e32600337d08f59ad381caf96a90659" in pm.cancelled
    assert "8e32600337d08f59ad381caf96a90659" in pm.released


@pytest.mark.asyncio
async def test_session_stream_receives_events() -> None:
    """``GET /v1/sessions/{id}/stream`` yields events published by proxy_stream."""
    app, _pm, _hc = _build_lifecycle_app()

    async with _runner_client(app) as client:
        # Create the session first.
        await client.post(
            "/v1/sessions",
            json={
                "session_id": "4ee52d986b72704408b5ff36fe8421e0",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )

        collected: list[dict[str, Any]] = []

        async def _subscribe() -> None:
            """Subscribe to SSE and collect events until [DONE]."""
            async with client.stream(
                "GET", "/v1/sessions/4ee52d986b72704408b5ff36fe8421e0/stream"
            ) as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        payload = line[6:]
                        if payload == "[DONE]":
                            return
                        collected.append(json.loads(payload))

        sub_task = asyncio.create_task(_subscribe())
        await asyncio.sleep(0.05)

        # Trigger a turn — proxy_stream publishes events via
        # session_stream. The stream stays open across turns;
        # deleting the session sends [DONE].
        resp = await client.post(
            "/v1/sessions/4ee52d986b72704408b5ff36fe8421e0/events",
            json={
                "type": "message",
                "role": "user",
                "model": "test-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )
        async for _ in resp.aiter_text():
            pass

        # Allow turn-end bookkeeping to run.
        await asyncio.sleep(0.05)

        # Delete the session to close the stream ([DONE]).
        await client.delete("/v1/sessions/4ee52d986b72704408b5ff36fe8421e0")

        await asyncio.wait_for(sub_task, timeout=5.0)

    # session.status=running + harness frames + session.status=idle.
    statuses = [e.get("status") for e in collected if e.get("type") == "session.status"]
    assert "running" in statuses, f"session.status=running must appear, got statuses: {statuses}"
    assert statuses[-1] in ("idle", "failed"), (
        f"last session.status must be idle or failed, got statuses: {statuses}"
    )
    harness_events = [e for e in collected if e.get("type") != "session.status"]
    assert len(harness_events) >= 2, (
        f"Expected at least 2 harness events, got {len(harness_events)}: {harness_events}"
    )


@pytest.mark.asyncio
async def test_session_stream_emits_heartbeat_on_idle() -> None:
    """The session stream emits an immediate and idle ``session.heartbeat``."""
    runner_app_module = sys.modules[create_runner_app.__module__]
    original = runner_app_module._SESSION_STREAM_HEARTBEAT_S
    runner_app_module._SESSION_STREAM_HEARTBEAT_S = 0.05
    try:
        app, _pm, _hc = _build_lifecycle_app()
        async with _runner_client(app) as client:
            await client.post(
                "/v1/sessions",
                json={
                    "session_id": "2fa978f2a04f84d78d2dde3c4de2a306",
                    "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
                },
            )
            collected: list[dict[str, Any]] = []

            async def _subscribe() -> None:
                async with client.stream(
                    "GET", "/v1/sessions/2fa978f2a04f84d78d2dde3c4de2a306/stream"
                ) as stream:
                    async for line in stream.aiter_lines():
                        if line.startswith("data: "):
                            payload = line[6:]
                            if payload == "[DONE]":
                                return
                            collected.append(json.loads(payload))

            sub_task = asyncio.create_task(_subscribe())
            await asyncio.sleep(0.2)
            await client.delete("/v1/sessions/2fa978f2a04f84d78d2dde3c4de2a306")
            await asyncio.wait_for(sub_task, timeout=5.0)

        heartbeats = [e for e in collected if e.get("type") == "session.heartbeat"]
        assert len(heartbeats) >= 1, f"Expected at least 1 session.heartbeat, got {collected}"
        assert collected[0] == {"type": "session.heartbeat"}, (
            "The first stream frame must be the ready heartbeat. Omnigent waits "
            "for this before forwarding fast no-replay user input."
        )
    finally:
        runner_app_module._SESSION_STREAM_HEARTBEAT_S = original


@pytest.mark.asyncio
async def test_create_session() -> None:
    """``POST /v1/sessions`` spawns harness and returns SessionResponse shape."""
    app, pm, _hc = _build_lifecycle_app()
    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": "8e32600337d08f59ad381caf96a90659",
                "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
            },
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "8e32600337d08f59ad381caf96a90659"
    assert body["agent_id"] == "880b5afda28ad55ff74cbeb9b5fc67fb"
    assert body["status"] == "idle"
    assert "created_at" in body
    assert body["items"] == []
    assert pm.has_session("8e32600337d08f59ad381caf96a90659")
