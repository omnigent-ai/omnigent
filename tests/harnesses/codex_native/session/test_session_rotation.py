"""Session rotation tests for Codex session."""

from __future__ import annotations

import asyncio
import functools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native.bridge import (
    read_bridge_state,
)
from tests.harnesses.codex_native.session._support import (
    _agent_message_event,
    _elicitation_tracker,
    _FakeCodexAppServerClient,
    _started_event,
    _write_forwarder_bridge,
)


def _thread_started_event(thread_id: str) -> dict[str, Any]:
    """
    Build a Codex ``thread/started`` notification.

    :param thread_id: Codex thread id, e.g. ``"thread_123"``.
    :returns: App-server event payload.
    """
    return {"method": "thread/started", "params": {"thread": {"id": thread_id}}}


def test_forwarder_ignores_thread_started_for_current_codex_thread(tmp_path: Path) -> None:
    """
    A duplicate ``thread/started`` notification does not rotate Omnigent sessions.

    Codex can broadcast the current thread after the forwarder has
    already bound it. This fails if the rotation detector treats every
    ``thread/started`` as a clear-session boundary.
    """
    _write_forwarder_bridge(
        tmp_path, session_id="conv_old", thread_id="thread_old", active_turn_id=None
    )

    async def run() -> bool:
        """
        Drive the duplicate notification through the rotation detector.

        :returns: Whether a rotation occurred.
        """
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000") as client:
            target = codex_native_forwarder._ForwarderTarget(
                session_id="conv_old",
                thread_id="thread_old",
                delta_coalescer=codex_native_forwarder._OutputTextDeltaCoalescer(
                    client,
                    "conv_old",
                ),
                usage_coalescer=codex_native_forwarder._SessionUsageCoalescer(
                    client,
                    "conv_old",
                ),
                elicitation_tracker=_elicitation_tracker(),
            )
            return await codex_native_forwarder._maybe_rotate_session_on_thread_started(
                ap_client=client,
                target=target,
                bridge_dir=tmp_path,
                app_server_url=str(tmp_path / "app-server.sock"),
                event=_thread_started_event("thread_old"),
            )

    assert asyncio.run(run()) is False
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.session_id == "conv_old"
    assert state.thread_id == "thread_old"


def test_forwarder_rotates_session_on_new_codex_thread_and_posts_to_new_session(
    tmp_path: Path,
) -> None:
    """
    Native Codex thread switches create a replacement Omnigent session.

    This is the ``/clear`` regression shape: Codex keeps the terminal
    alive but starts a new app-server thread. The forwarder must keep the
    old title and project, move terminal ownership, update bridge state,
    resubscribe to the new thread, and send subsequent status/history
    events to the new AP session.
    """
    _write_forwarder_bridge(
        tmp_path, session_id="conv_old", thread_id="thread_old", active_turn_id=None
    )
    fake_client = _FakeCodexAppServerClient()
    requests: list[tuple[str, str, dict[str, Any] | None]] = []
    posted_events: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Serve Omnigent calls made during Codex session rotation.

        :param request: HTTP request from the forwarder.
        :returns: Fake Omnigent response.
        """
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_old":
            return httpx.Response(
                200,
                json={
                    "id": "conv_old",
                    "agent_id": "ag_codex",
                    "runner_id": "runner_123",
                    "labels": {
                        "omnigent.wrapper": "codex-native-ui",
                        "omnigent.codex_native.bridge_id": "bridge_shared",
                    },
                    "title": "Fix login flow",
                    "project_id": "proj_alpha",
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "conv_new"})
        if request.method == "PATCH" and request.url.path in {
            "/v1/sessions/conv_new",
            "/v1/sessions/conv_old",
        }:
            return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})
        if request.method == "POST" and request.url.path == (
            "/v1/sessions/conv_old/resources/terminals/terminal_codex_main/transfer"
        ):
            return httpx.Response(200, json={"id": "terminal_codex_main"})
        if request.method == "POST" and request.url.path == "/v1/sessions/conv_new/events":
            assert isinstance(body, dict)
            posted_events.append(("conv_new", body))
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(
            500,
            json={"error": f"unexpected {request.method} {request.url.path}"},
        )

    async def run() -> None:
        """
        Drive the real rotation and event handlers.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as ap_client:
            target = codex_native_forwarder._ForwarderTarget(
                session_id="conv_old",
                thread_id="thread_old",
                delta_coalescer=codex_native_forwarder._OutputTextDeltaCoalescer(
                    ap_client,
                    "conv_old",
                ),
                usage_coalescer=codex_native_forwarder._SessionUsageCoalescer(
                    ap_client,
                    "conv_old",
                ),
                elicitation_tracker=_elicitation_tracker(),
            )
            await codex_native_forwarder._subscribe_until_ready(
                fake_client,  # type: ignore[arg-type]
                ap_client,
                session_id=target.session_id,
                bridge_dir=tmp_path,
                thread_id=target.thread_id,
                usage_coalescer=target.usage_coalescer,
                elicitation_tracker=target.elicitation_tracker,
            )
            rotated = await codex_native_forwarder._maybe_rotate_session_on_thread_started(
                ap_client=ap_client,
                target=target,
                bridge_dir=tmp_path,
                app_server_url="ws://127.0.0.1:9876",
                event=_thread_started_event("thread_new"),
            )
            assert rotated
            await codex_native_forwarder._subscribe_until_ready(
                fake_client,  # type: ignore[arg-type]
                ap_client,
                session_id=target.session_id,
                bridge_dir=tmp_path,
                thread_id=target.thread_id,
                usage_coalescer=target.usage_coalescer,
                elicitation_tracker=target.elicitation_tracker,
            )
            await codex_native_forwarder._handle_event(
                ap_client,
                session_id=target.session_id,
                bridge_dir=tmp_path,
                usage_coalescer=target.usage_coalescer,
                elicitation_tracker=target.elicitation_tracker,
                event=_started_event("turn_new"),
                delta_coalescer=target.delta_coalescer,
                expected_thread_id=target.thread_id,
            )
            await codex_native_forwarder._handle_event(
                ap_client,
                session_id=target.session_id,
                bridge_dir=tmp_path,
                usage_coalescer=target.usage_coalescer,
                elicitation_tracker=target.elicitation_tracker,
                event=_agent_message_event(
                    "turn_new",
                    "item_new",
                    "after clear",
                    thread_id="thread_new",
                ),
                delta_coalescer=target.delta_coalescer,
                expected_thread_id=target.thread_id,
            )
            await target.delta_coalescer.close()

    asyncio.run(run())

    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.session_id == "conv_new"
    assert state.thread_id == "thread_new"
    # Rotation must re-persist the ws:// transport it was given, not a
    # clobbered unix path — otherwise the executor would dial a dead unix
    # socket after /clear and steering/interrupt would silently fail.
    assert state.socket_path == "ws://127.0.0.1:9876"
    assert fake_client.requests == [
        ("thread/resume", {"threadId": "thread_old", "excludeTurns": True}),
        ("thread/resume", {"threadId": "thread_new", "excludeTurns": True}),
    ]
    assert (
        "POST",
        "/v1/sessions",
        {
            "agent_id": "ag_codex",
            "labels": {
                "omnigent.wrapper": "codex-native-ui",
                "omnigent.codex_native.bridge_id": "bridge_shared",
            },
            "title": "Fix login flow",
            "project_id": "proj_alpha",
            "workspace": None,
            "git": None,
        },
    ) in requests
    assert (
        "POST",
        "/v1/sessions/conv_old/resources/terminals/terminal_codex_main/transfer",
        {"target_session_id": "conv_new"},
    ) in requests
    assert [
        payload["data"]["status"]
        for _, payload in posted_events
        if payload["type"] == "external_session_status"
    ] == ["running"]
    assert [
        payload["data"]["item_data"]["content"][0]["text"]
        for _, payload in posted_events
        if payload["type"] == "external_conversation_item"
    ] == ["after clear"]


def test_forwarder_rotation_failure_preserves_old_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Failed Codex thread rotation leaves the old forwarding target usable.

    If Omnigent rejects replacement-session creation, the forwarder logs the
    event-handler failure and continues. The old target must remain
    intact; closing its coalescer before the Omnigent move succeeds would
    leave later old-thread streaming in a half-rotated state.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary bridge directory.
    :returns: None.
    """

    class _FakeCoalescer:
        """
        Test coalescer that records lifecycle calls.

        :param session_id: Omnigent session id represented by this fake,
            e.g. ``"conv_old"``.
        """

        def __init__(self, session_id: str) -> None:
            """
            Initialize the fake coalescer.

            :param session_id: Omnigent session id represented by this fake,
                e.g. ``"conv_old"``.
            :returns: None.
            """
            self.session_id = session_id
            self.flushed = False
            self.closed = False

        async def flush(self) -> None:
            """
            Record a flush request.

            :returns: None.
            """
            self.flushed = True

        async def close(self) -> None:
            """
            Record a close request.

            :returns: None.
            """
            self.closed = True

    async def fail_create_thread_replacement_session(**_kwargs: object) -> str:
        """
        Simulate Omnigent rejecting the replacement-session operation.

        :returns: Never returns successfully.
        :raises RuntimeError: Always raised to model Omnigent failure.
        """
        raise RuntimeError("replacement failed")

    fake_delta_coalescer = _FakeCoalescer("conv_old")
    fake_usage_coalescer = _FakeCoalescer("conv_old")
    monkeypatch.setattr(
        codex_native_forwarder,
        "_create_thread_replacement_session",
        fail_create_thread_replacement_session,
    )

    async def run() -> codex_native_forwarder._ForwarderTarget:
        """
        Drive a failed new-thread rotation through the real detector.

        :returns: Forwarder target after the failed rotation attempt.
        """
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000") as client:
            target = codex_native_forwarder._ForwarderTarget(
                session_id="conv_old",
                thread_id="thread_old",
                delta_coalescer=fake_delta_coalescer,  # type: ignore[arg-type]
                usage_coalescer=fake_usage_coalescer,  # type: ignore[arg-type]
                elicitation_tracker=_elicitation_tracker(),
            )
            with pytest.raises(RuntimeError, match="replacement failed"):
                await codex_native_forwarder._maybe_rotate_session_on_thread_started(
                    ap_client=client,
                    target=target,
                    bridge_dir=tmp_path,
                    app_server_url=str(tmp_path / "app-server.sock"),
                    event=_thread_started_event("thread_new"),
                )
            return target

    target = asyncio.run(run())

    assert target.session_id == "conv_old"
    assert target.thread_id == "thread_old"
    assert target.delta_coalescer is fake_delta_coalescer
    assert target.usage_coalescer is fake_usage_coalescer
    assert fake_delta_coalescer.flushed
    assert fake_usage_coalescer.flushed
    assert not fake_delta_coalescer.closed
    assert not fake_usage_coalescer.closed


@dataclass(frozen=True)
class _CapturedSessionEvent:
    """
    Captured AP session event from a Codex forwarder test.

    :param session_id: AP session id parsed from the request path,
        e.g. ``"conv_new"``.
    :param body: Decoded session event body.
    """

    session_id: str
    body: dict[str, Any]


def test_supervise_forwarder_rotation_clears_unparented_pending_child_threads(
    tmp_path: Path,
) -> None:
    """
    Parent thread rotation clears unregistered child-thread markers.

    Codex may omit ``parent_thread_id`` on a child ``thread/started``
    notification. That gap marker is intentionally permissive while the
    parent thread is current, but it must not survive a parent ``/clear``
    rotation and make stale child approvals actionable in the new session.

    :param tmp_path: Pytest temporary directory.
    """
    _write_forwarder_bridge(
        tmp_path, session_id="conv_old", thread_id="thread_old", active_turn_id=None
    )
    unparented_child_started = {
        "method": "thread/started",
        "params": {
            "thread": {
                "id": "thread_child_without_parent",
                "source": {"subAgent": {"thread_spawn": {}}},
            }
        },
    }
    stale_child_approval = {
        "id": 18,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_child_without_parent",
            "turnId": "turn_child",
            "itemId": "item_cmd",
            "command": "date",
        },
    }
    fake_client = _FakeCodexAppServerClient(
        events=[
            unparented_child_started,
            _thread_started_event("thread_new"),
            _started_event("turn_new"),
            stale_child_approval,
        ]
    )
    session_events: list[_CapturedSessionEvent] = []
    hook_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Serve AP calls made while the supervise loop rotates sessions.

        :param request: HTTP request from the forwarder.
        :returns: Fake AP response.
        """
        body = json.loads(request.content) if request.content else None
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_old":
            return httpx.Response(
                200,
                json={
                    "id": "conv_old",
                    "agent_id": "ag_codex",
                    "runner_id": "runner_123",
                    "labels": {},
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "conv_new"})
        if request.method == "PATCH" and request.url.path in {
            "/v1/sessions/conv_new",
            "/v1/sessions/conv_old",
        }:
            return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})
        if request.method == "POST" and request.url.path == (
            "/v1/sessions/conv_old/resources/terminals/terminal_codex_main/transfer"
        ):
            return httpx.Response(200, json={"id": "terminal_codex_main"})
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            assert isinstance(body, dict)
            hook_posts.append(body)
            return httpx.Response(200, json={"decision": "accept"})
        if request.url.path.endswith("/events"):
            assert isinstance(body, dict)
            session_id = request.url.path.split("/")[3]
            session_events.append(_CapturedSessionEvent(session_id=session_id, body=body))
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(
            500,
            json={"error": f"unexpected {request.method} {request.url.path}"},
        )

    async def run() -> None:
        """
        Run the supervise loop over rotation and stale child events.

        :returns: None.
        """
        await codex_native_forwarder.supervise_forwarder(
            base_url="http://127.0.0.1:8000",
            headers={},
            session_id="conv_old",
            bridge_dir=tmp_path,
            app_server_url="ws://127.0.0.1:9876",
            thread_id="thread_old",
            client=fake_client,  # type: ignore[arg-type]
            ap_transport=httpx.MockTransport(handler),
        )

    asyncio.run(run())

    assert [
        event.body["data"]["status"]
        for event in session_events
        if event.session_id == "conv_new" and event.body["type"] == "external_session_status"
    ] == ["running"]
    # The cleared session gets only the startup flag sync, the /clear notice and
    # redirect, never a stale child-thread event.
    assert [event.body["type"] for event in session_events if event.session_id == "conv_old"] == [
        "external_background_work",
        "external_session_status",
        "external_conversation_item",
        "external_session_superseded",
    ]
    assert hook_posts == []
    assert fake_client.responses == []


def test_supervise_forwarder_relaunches_terminal_on_thread_it_started(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A web clear-context answer moves the transferred TUI onto the fresh thread.

    The forwarder starts that thread itself, so the TUI is still attached to the
    planning thread. Once the rotated session is subscribed to the new thread, the
    runner must relaunch the TUI there, or the new session's Terminal view keeps
    showing the old conversation.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Pytest temporary directory.
    """
    _write_forwarder_bridge(
        tmp_path, session_id="conv_old", thread_id="thread_old", active_turn_id=None
    )
    monkeypatch.setattr(
        codex_native_forwarder,
        "_CodexForwarderState",
        functools.partial(
            codex_native_forwarder._CodexForwarderState,
            forwarder_started_thread_id="thread_new",
        ),
    )
    relaunches: list[tuple[str, str, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Serve AP calls made while the forwarder rotates sessions.

        :param request: HTTP request from the forwarder.
        :returns: Fake AP response.
        """
        if request.method == "GET":
            return httpx.Response(200, json={"id": "conv_old", "agent_id": "ag_codex"})
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "conv_new"})
        return httpx.Response(200, json={})

    async def run() -> None:
        """
        Rotate onto the started thread and wait for the terminal relaunch.

        :returns: None.
        """
        relaunched = asyncio.Event()

        class _RotatingClient(_FakeCodexAppServerClient):
            async def iter_events(self) -> Any:
                yield _thread_started_event("thread_new")
                await asyncio.wait_for(relaunched.wait(), timeout=10)

        fake_client = _RotatingClient()

        async def relaunch_terminal(session_id: str, thread_id: str) -> None:
            subscribed = (
                "thread/resume",
                {"threadId": thread_id, "excludeTurns": True},
            ) in fake_client.requests
            relaunches.append((session_id, thread_id, subscribed))
            relaunched.set()

        await codex_native_forwarder.supervise_forwarder(
            base_url="http://127.0.0.1:8000",
            headers={},
            session_id="conv_old",
            bridge_dir=tmp_path,
            app_server_url="ws://127.0.0.1:9876",
            thread_id="thread_old",
            client=fake_client,  # type: ignore[arg-type]
            ap_transport=httpx.MockTransport(handler),
            relaunch_terminal=relaunch_terminal,
        )

    asyncio.run(run())

    assert relaunches == [("conv_new", "thread_new", True)]


@pytest.mark.parametrize("outcome", ["tui_thread", "relaunch_fails"])
def test_terminal_relaunch_skips_tui_threads_and_contains_failures(
    outcome: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Only forwarder-started threads relaunch the TUI, and a failed relaunch is contained.

    A TUI ``/clear`` already shows its new thread, so relaunching it would only
    flicker. A relaunch error must not end the forwarder's subscription.

    :param outcome: Which case the parameter exercises.
    :param caplog: Pytest log capture fixture.
    """
    forwarder_state = codex_native_forwarder._CodexForwarderState(
        forwarder_started_thread_id=None if outcome == "tui_thread" else "thread_new"
    )
    relaunch_terminal = AsyncMock(side_effect=RuntimeError("terminal launch failed"))

    asyncio.run(
        codex_native_forwarder._relaunch_terminal_on_forwarder_thread(
            relaunch_terminal,
            forwarder_state,
            session_id="conv_new",
            thread_id="thread_new",
        )
    )

    if outcome == "tui_thread":
        relaunch_terminal.assert_not_awaited()
    else:
        relaunch_terminal.assert_awaited_once_with("conv_new", "thread_new")
        assert "Codex terminal relaunch on thread thread_new failed" in caplog.text
