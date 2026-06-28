"""Tests for native Claude transcript forwarding."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
from collections.abc import Callable, Generator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import omnigent.claude_native_forwarder as forwarder
from omnigent.claude_native_bridge import (
    BRIDGE_ID_LABEL_KEY,
    ClaudeMessageDelta,
    ClaudeTranscriptItem,
    prepare_bridge_dir,
    read_active_session_id,
    record_hook_event,
    write_active_session_id,
)
from omnigent.claude_native_forwarder import (
    _persist_native_compaction_item,
    forward_claude_transcript_to_session,
)
from omnigent.reasoning_effort import CLAUDE_EFFORTS, EFFORT_CLEAR_VALUES


@pytest.fixture(autouse=True)
def _allow_tmp_path_as_bridge_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Treat each test's temp dir as the Claude bridge root.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp directory.
    :returns: None.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path)


class _RecordingHTTPServer(ThreadingHTTPServer):
    """
    HTTP server that records JSON POST bodies.

    :param server_address: Host/port tuple for
        :class:`ThreadingHTTPServer`.
    :param RequestHandlerClass: Handler class used for requests.
    """

    requests: queue.Queue[dict[str, Any]]


def _handler_factory(
    requests: queue.Queue[dict[str, Any]],
) -> type[BaseHTTPRequestHandler]:
    """
    Create a request handler that records POST JSON.

    :param requests: Queue receiving decoded request records.
    :returns: A concrete :class:`BaseHTTPRequestHandler` subclass.
    """

    class _Handler(BaseHTTPRequestHandler):
        """Request handler for the test Omnigent endpoint."""

        def log_message(self, format: str, *args: Any) -> None:
            """
            Suppress test HTTP server logging.

            :param format: Log format string.
            :param args: Log format arguments.
            :returns: None.
            """
            del format, args

        def do_POST(self) -> None:
            """
            Record a JSON POST body and return HTTP 202.

            :returns: None.
            """
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            requests.put(
                {
                    "method": "POST",
                    "path": self.path,
                    "body": json.loads(raw.decode("utf-8")),
                    "authorization": self.headers.get("Authorization"),
                }
            )
            self.send_response(202)
            self.end_headers()
            self.wfile.write(b"{}")

        def do_PATCH(self) -> None:
            """
            Record a JSON PATCH body and return HTTP 200.

            :returns: None.
            """
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            requests.put(
                {
                    "method": "PATCH",
                    "path": self.path,
                    "body": json.loads(raw.decode("utf-8")),
                    "authorization": self.headers.get("Authorization"),
                }
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    return _Handler


def _start_recording_server() -> tuple[_RecordingHTTPServer, threading.Thread, str]:
    """
    Start a local HTTP server that records POST bodies.

    :returns: ``(server, thread, base_url)``.
    """
    requests: queue.Queue[dict[str, Any]] = queue.Queue()
    server = _RecordingHTTPServer(("127.0.0.1", 0), _handler_factory(requests))
    server.requests = requests
    thread = threading.Thread(
        target=server.serve_forever,
        name="claude-forwarder-test-ap",
        daemon=True,
    )
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


async def _get_recorded_request(
    server: _RecordingHTTPServer,
    *,
    timeout_s: float = 5.0,
    method: str = "POST",
) -> dict[str, Any]:
    """
    Await one recorded request from the test server, filtered by method.

    The forwarder mirrors Claude's native session id to Omnigent via a
    one-shot ``PATCH /v1/sessions/{id}`` (see
    :func:`_maybe_mirror_external_session_id`). Most tests in this
    file assert on POSTs to ``/events``; defaulting the filter to
    ``"POST"`` lets those tests skip the mirroring PATCH that lands
    at the start of every loop in which the bridge state carries a
    Claude session id. PATCH-specific tests pass ``method="PATCH"``.

    :param server: Recording HTTP server.
    :param timeout_s: Maximum seconds to wait — applied per
        ``queue.get`` call, so the helper can spend up to
        ``timeout_s`` skipping each non-matching request before
        giving up on the next matching one.
    :param method: HTTP method to filter for, e.g. ``"POST"`` or
        ``"PATCH"``. Non-matching requests are silently discarded.
    :returns: Recorded request dict whose ``method`` matches.
    """
    while True:
        try:
            request = await asyncio.to_thread(server.requests.get, True, timeout_s)
        except queue.Empty as exc:
            raise AssertionError(
                f"forwarder did not produce a {method} request",
            ) from exc
        if request.get("method") == method:
            return request


async def _get_recorded_item_request(
    server: _RecordingHTTPServer,
    *,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """
    Await the next ``external_conversation_item`` POST, skipping status edges.

    The forwarder now emits a turn-start ``external_session_status: running``
    (carrying the turn's response id, which drives the live tool-card spinner
    in ap-web) BEFORE a turn's items each poll. Tests that only care about the
    forwarded conversation items use this to skip that leading status edge (and
    any trailing idle) without asserting on it.

    :param server: Recording HTTP server.
    :param timeout_s: Per-``get`` timeout while skipping non-item POSTs.
    :returns: The next recorded ``external_conversation_item`` POST.
    """
    while True:
        request = await _get_recorded_request(server, timeout_s=timeout_s)
        if request["body"].get("type") == "external_conversation_item":
            return request


async def _wait_for_json_file(path: Path, *, timeout_s: float = 5.0) -> dict[str, Any]:
    """
    Wait until a JSON object file exists and can be parsed.

    :param path: JSON file path.
    :param timeout_s: Maximum seconds to wait.
    :returns: Parsed JSON object.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(payload, dict)
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} was not written")


async def _wait_for_json_state(
    path: Path,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """
    Wait until a JSON object file satisfies ``predicate``.

    :param path: JSON file path.
    :param predicate: Function returning ``True`` for the desired
        state, e.g. ``lambda payload: "byte_offset" in payload``.
    :param timeout_s: Maximum seconds to wait.
    :returns: Parsed JSON object satisfying the predicate.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_payload: dict[str, Any] | None = None
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(payload, dict)
            last_payload = payload
            if predicate(payload):
                return payload
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} did not reach expected state; last={last_payload!r}")


@pytest.mark.asyncio
async def test_clear_hook_rotates_active_session_without_reprocessing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Claude ``/clear`` creates a fresh Omnigent session and consumes the hook.

    This exercises the rotation transaction directly: create the new
    session, bind the same runner, transfer the terminal, rewrite the
    active bridge session, clear the old runner binding, and keep the
    hook cursor past the clear record so the next poll does not fork
    again from the same hook line.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = prepare_bridge_dir(
        "conv_old",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    (bridge_dir / "transcript_forwarder.json").write_text("{}", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "source": "clear",
        },
    )
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Mock the Omnigent session-rotation endpoints.

        :param request: Incoming request.
        :returns: Canned Omnigent response.
        """
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_old":
            return httpx.Response(
                200,
                json={
                    "id": "conv_old",
                    "agent_id": "ag_claude",
                    "runner_id": "runner_one",
                    "labels": {
                        "omnigent.ui": "terminal",
                        BRIDGE_ID_LABEL_KEY: "bridge_shared",
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions":
            assert body == {
                "agent_id": "ag_claude",
                "labels": {
                    "omnigent.ui": "terminal",
                    BRIDGE_ID_LABEL_KEY: "bridge_shared",
                },
            }
            return httpx.Response(201, json={"id": "conv_new"})
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_new":
            assert body == {"runner_id": "runner_one"}
            return httpx.Response(200, json={"id": "conv_new"})
        if (
            request.method == "POST"
            and request.url.path
            == "/v1/sessions/conv_old/resources/terminals/terminal_claude_main/transfer"
        ):
            assert body == {"target_session_id": "conv_new"}
            return httpx.Response(200, json={"id": "terminal_claude_main"})
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_old":
            assert body == {
                "runner_id": "",
                "labels": {BRIDGE_ID_LABEL_KEY: "conv_old-cleared"},
            }
            return httpx.Response(200, json={"id": "conv_old"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        hook_state = await forwarder._ensure_hook_state(
            bridge_dir,
            start_at_end=False,
            session_id="conv_old",
        )
        rotated_to = await forwarder._maybe_rotate_session_on_clear(
            client=client,
            session_id="conv_old",
            bridge_dir=bridge_dir,
            state=hook_state,
        )
        replay_state = await forwarder._ensure_hook_state(
            bridge_dir,
            start_at_end=False,
            session_id="conv_new",
        )
        rotated_again = await forwarder._maybe_rotate_session_on_clear(
            client=client,
            session_id="conv_new",
            bridge_dir=bridge_dir,
            state=replay_state,
        )

    assert rotated_to == "conv_new"
    assert rotated_again is None
    assert read_active_session_id(bridge_dir) == "conv_new"
    assert not (bridge_dir / "transcript_forwarder.json").exists()
    assert (bridge_dir / "hook_forwarder.json").exists()
    assert calls == [
        ("GET", "/v1/sessions/conv_old", None),
        (
            "POST",
            "/v1/sessions",
            {
                "agent_id": "ag_claude",
                "labels": {
                    "omnigent.ui": "terminal",
                    BRIDGE_ID_LABEL_KEY: "bridge_shared",
                },
            },
        ),
        ("PATCH", "/v1/sessions/conv_new", {"runner_id": "runner_one"}),
        (
            "POST",
            "/v1/sessions/conv_old/resources/terminals/terminal_claude_main/transfer",
            {"target_session_id": "conv_new"},
        ),
        (
            "PATCH",
            "/v1/sessions/conv_old",
            {"runner_id": "", "labels": {BRIDGE_ID_LABEL_KEY: "conv_old-cleared"}},
        ),
    ]


@pytest.mark.asyncio
async def test_clear_hook_rotation_survives_old_runner_clear_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Old runner-binding cleanup failure must not retry the fork.

    Once the terminal transfer succeeds and the bridge active session is
    updated, retrying the whole rotation would create duplicate fresh
    sessions from the same ``/clear`` hook. The stale old runner binding
    is cleanup only; the executor active-session guard prevents stale
    old-session writes from reaching tmux.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = prepare_bridge_dir(
        "conv_old",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "source": "clear",
        },
    )
    create_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Mock Omnigent rotation endpoints with a failing old-session cleanup.

        :param request: Incoming request.
        :returns: Canned Omnigent response.
        """
        nonlocal create_count
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_old":
            return httpx.Response(
                200,
                json={
                    "id": "conv_old",
                    "agent_id": "ag_claude",
                    "runner_id": "runner_one",
                    "labels": {BRIDGE_ID_LABEL_KEY: "bridge_shared"},
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions":
            create_count += 1
            return httpx.Response(201, json={"id": "conv_new"})
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_new":
            assert body == {"runner_id": "runner_one"}
            return httpx.Response(200, json={"id": "conv_new"})
        if (
            request.method == "POST"
            and request.url.path
            == "/v1/sessions/conv_old/resources/terminals/terminal_claude_main/transfer"
        ):
            assert body == {"target_session_id": "conv_new"}
            return httpx.Response(200, json={"id": "terminal_claude_main"})
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_old":
            assert body == {
                "runner_id": "",
                "labels": {BRIDGE_ID_LABEL_KEY: "conv_old-cleared"},
            }
            return httpx.Response(503, json={"error": {"message": "temporary failure"}})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        hook_state = await forwarder._ensure_hook_state(
            bridge_dir,
            start_at_end=False,
            session_id="conv_old",
        )
        rotated_to = await forwarder._maybe_rotate_session_on_clear(
            client=client,
            session_id="conv_old",
            bridge_dir=bridge_dir,
            state=hook_state,
        )
        replay_state = await forwarder._ensure_hook_state(
            bridge_dir,
            start_at_end=False,
            session_id="conv_new",
        )
        rotated_again = await forwarder._maybe_rotate_session_on_clear(
            client=client,
            session_id="conv_new",
            bridge_dir=bridge_dir,
            state=replay_state,
        )

    assert rotated_to == "conv_new"
    assert rotated_again is None
    assert create_count == 1
    assert read_active_session_id(bridge_dir) == "conv_new"


@pytest.mark.asyncio
async def test_clear_hook_transfer_failure_does_not_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A terminal-transfer failure during /clear must NOT spin into a session loop.

    Regression guard for the unbounded-session-creation bug: when the terminal
    transfer fails (e.g. 400 because the target already owns a terminal), the
    rotation must still consume the clear hook so the forwarder's next poll does
    not re-rotate and create another replacement session every tick.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = prepare_bridge_dir(
        "conv_old",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "SessionStart", "source": "clear"},
    )
    create_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Mock rotation endpoints with a failing terminal transfer."""
        nonlocal create_count
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_old":
            return httpx.Response(
                200,
                json={
                    "id": "conv_old",
                    "agent_id": "ag_claude",
                    "runner_id": "runner_one",
                    "labels": {BRIDGE_ID_LABEL_KEY: "bridge_shared"},
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions":
            create_count += 1
            return httpx.Response(201, json={"id": "conv_new"})
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_new":
            return httpx.Response(200, json={"id": "conv_new"})
        if (
            request.method == "POST"
            and request.url.path
            == "/v1/sessions/conv_old/resources/terminals/terminal_claude_main/transfer"
        ):
            # The failure that triggered the production loop.
            return httpx.Response(400, json={"error": {"message": "Terminal already exists"}})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        hook_state = await forwarder._ensure_hook_state(
            bridge_dir,
            start_at_end=False,
            session_id="conv_old",
        )
        # The transfer 400 is swallowed: rotation reports no new session...
        rotated_to = await forwarder._maybe_rotate_session_on_clear(
            client=client,
            session_id="conv_old",
            bridge_dir=bridge_dir,
            state=hook_state,
        )
        # ...and a second poll must NOT re-rotate (the clear hook was consumed).
        replay_state = await forwarder._ensure_hook_state(
            bridge_dir,
            start_at_end=False,
            session_id="conv_old",
        )
        rotated_again = await forwarder._maybe_rotate_session_on_clear(
            client=client,
            session_id="conv_old",
            bridge_dir=bridge_dir,
            state=replay_state,
        )

    assert rotated_to is None
    assert rotated_again is None
    # Exactly one replacement-session create — not one per poll.
    assert create_count == 1


@pytest.mark.asyncio
async def test_post_clear_supersession_notifies_old_session() -> None:
    """
    A /clear rotation notifies the superseded (old) conversation.

    It POSTs, in order, (1) ``external_session_status: idle`` so the old
    chat's spinner stops once its terminal moves away, (2) a persisted
    assistant ``message`` item linking to the new conversation so a reload
    explains the clear, and (3) a transient ``external_session_superseded``
    redirect event so a live viewer auto-follows. All three are addressed
    to the OLD conversation.
    """
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record each POST and return a benign success."""
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        calls.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"queued": False, "item_id": "item_x"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        await forwarder._post_clear_supersession(
            client,
            old_session_id="conv_old",
            new_session_id="conv_new",
            agent_name="claude-native-ui",
        )

    assert len(calls) == 3
    # Every post is addressed to the OLD conversation.
    assert all(
        (method, path) == ("POST", "/v1/sessions/conv_old/events") for method, path, _ in calls
    )

    _, _, status_body = calls[0]
    assert status_body == {
        "type": "external_session_status",
        "data": {"status": "idle"},
    }

    _, _, notice_body = calls[1]
    assert notice_body is not None
    assert notice_body["type"] == "external_conversation_item"
    assert notice_body["data"]["item_type"] == "message"
    item_data = notice_body["data"]["item_data"]
    assert item_data["role"] == "assistant"
    assert item_data["agent"] == "claude-native-ui"
    notice_text = item_data["content"][0]["text"]
    assert "/clear" in notice_text
    assert "/c/conv_new" in notice_text

    _, _, event_body = calls[2]
    assert event_body == {
        "type": "external_session_superseded",
        "data": {"target_conversation_id": "conv_new"},
    }


@pytest.mark.asyncio
async def test_post_clear_supersession_skips_when_old_equals_new() -> None:
    """
    The notify is a no-op when the old and new ids collapse to one.

    A defensive guard: addressing the "you were cleared" banner + redirect
    at the live session id would dump them onto the active chat.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Fail loudly — no POST should happen."""
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        await forwarder._post_clear_supersession(
            client,
            old_session_id="conv_same",
            new_session_id="conv_same",
            agent_name="claude-native-ui",
        )

    assert calls == []


@pytest.mark.asyncio
async def test_post_clear_supersession_swallows_post_failure() -> None:
    """
    A failed notice/redirect POST is swallowed, not raised.

    The rotation has already completed and reset forwarder state by the
    time this runs, so a notification error must not break the poll loop.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Fail every POST so both best-effort calls hit their except path."""
        return httpx.Response(500, json={"error": {"message": "boom"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        # Must not raise despite both POSTs returning 500.
        await forwarder._post_clear_supersession(
            client,
            old_session_id="conv_old",
            new_session_id="conv_new",
            agent_name="claude-native-ui",
        )


@pytest.mark.asyncio
async def test_clear_hook_consumes_hook_rotated_session_without_duplicate_fork(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Forwarder does not fork again when the SessionStart hook already did.

    The synchronous hook rotates before printing Claude's welcome URL.
    It annotates the hook record so the background forwarder only
    advances its durable cursor and resets transcript state.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = prepare_bridge_dir(
        "conv_old",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    write_active_session_id(bridge_dir, "conv_new")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "source": "clear",
            "omnigent_clear_rotated_to": "conv_new",
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail if the forwarder tries to create another replacement session.

        :param request: Incoming request.
        :returns: Never returns.
        """
        raise AssertionError(f"unexpected Omnigent request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        hook_state = await forwarder._ensure_hook_state(
            bridge_dir,
            start_at_end=False,
            session_id="conv_new",
        )
        rotated_to = await forwarder._maybe_rotate_session_on_clear(
            client=client,
            session_id="conv_new",
            bridge_dir=bridge_dir,
            state=hook_state,
        )
        replay_state = await forwarder._ensure_hook_state(
            bridge_dir,
            start_at_end=False,
            session_id="conv_new",
        )
        rotated_again = await forwarder._maybe_rotate_session_on_clear(
            client=client,
            session_id="conv_new",
            bridge_dir=bridge_dir,
            state=replay_state,
        )

    assert rotated_to == "conv_new"
    assert rotated_again is None
    assert read_active_session_id(bridge_dir) == "conv_new"
    assert (bridge_dir / "hook_forwarder.json").exists()


@pytest.mark.asyncio
async def test_fork_hook_creates_omnigent_fork_and_consumes_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Claude ``/fork`` creates an Omnigent fork and consumes the hook.

    This exercises the branch/fork transaction directly: fork the AP
    session, bind the same runner, transfer the terminal, rewrite the
    active bridge session, clear the old runner binding, and advance
    the hook cursor so the same hook line is not processed again.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = prepare_bridge_dir(
        "conv_old",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    (bridge_dir / "transcript_forwarder.json").write_text("{}", encoding="utf-8")
    transcript_path = tmp_path / "fork.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "attachment",
                "timestamp": "2026-05-27T22:53:13.245Z",
                "sessionId": "claude_fork",
                "forkedFrom": {"sessionId": "claude_old"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("omnigent.claude_native_bridge.time.time", lambda: 1779922393.245)
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "source": "resume",
            "session_id": "claude_fork",
            "transcript_path": str(transcript_path),
            "omnigent_previous_claude_session_id": "claude_old",
            "omnigent_claude_session_was_seen": False,
        },
    )
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Mock the Omnigent fork-rotation endpoints.

        :param request: Incoming request.
        :returns: Canned Omnigent response.
        """
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_old":
            return httpx.Response(
                200,
                json={
                    "id": "conv_old",
                    "agent_id": "ag_claude",
                    "runner_id": "runner_one",
                    "labels": {
                        "omnigent.ui": "terminal",
                        BRIDGE_ID_LABEL_KEY: "bridge_shared",
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions/conv_old/fork":
            assert body == {}
            return httpx.Response(201, json={"id": "conv_fork"})
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_fork":
            assert body == {"runner_id": "runner_one"}
            return httpx.Response(200, json={"id": "conv_fork"})
        if (
            request.method == "POST"
            and request.url.path
            == "/v1/sessions/conv_old/resources/terminals/terminal_claude_main/transfer"
        ):
            assert body == {"target_session_id": "conv_fork"}
            return httpx.Response(200, json={"id": "terminal_claude_main"})
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_old":
            assert body == {"runner_id": ""}
            return httpx.Response(200, json={"id": "conv_old"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        hook_state = await forwarder._ensure_hook_state(
            bridge_dir,
            start_at_end=False,
            session_id="conv_old",
        )
        rotated_to = await forwarder._maybe_rotate_session_on_fork(
            client=client,
            session_id="conv_old",
            bridge_dir=bridge_dir,
            state=hook_state,
        )
        replay_state = await forwarder._ensure_hook_state(
            bridge_dir,
            start_at_end=False,
            session_id="conv_fork",
        )
        rotated_again = await forwarder._maybe_rotate_session_on_fork(
            client=client,
            session_id="conv_fork",
            bridge_dir=bridge_dir,
            state=replay_state,
        )

    assert rotated_to == "conv_fork"
    assert rotated_again is None
    assert read_active_session_id(bridge_dir) == "conv_fork"
    transcript_state = json.loads(
        (bridge_dir / "transcript_forwarder.json").read_text(encoding="utf-8")
    )
    assert transcript_state["transcript_path"] == str(transcript_path)
    assert transcript_state["byte_offset"] == transcript_path.stat().st_size
    assert (bridge_dir / "hook_forwarder.json").exists()
    assert calls == [
        ("GET", "/v1/sessions/conv_old", None),
        ("POST", "/v1/sessions/conv_old/fork", {}),
        ("PATCH", "/v1/sessions/conv_fork", {"runner_id": "runner_one"}),
        (
            "POST",
            "/v1/sessions/conv_old/resources/terminals/terminal_claude_main/transfer",
            {"target_session_id": "conv_fork"},
        ),
        ("PATCH", "/v1/sessions/conv_old", {"runner_id": ""}),
    ]


@pytest.mark.asyncio
async def test_fork_hook_consumes_hook_rotated_session_without_duplicate_fork(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Forwarder does not fork again when the SessionStart hook already did.

    The synchronous hook annotates the branch record with the forked AP
    session id. The background forwarder only advances its durable
    cursor and seeds transcript state past Claude's copied fork
    history.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = prepare_bridge_dir(
        "conv_old",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    write_active_session_id(bridge_dir, "conv_fork")
    transcript_path = tmp_path / "fork.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "already copied"},
                "sessionId": "claude_fork",
                "forkedFrom": {"sessionId": "claude_old"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "source": "resume",
            "transcript_path": str(transcript_path),
            "omnigent_fork_detected": True,
            "omnigent_fork_rotated_to": "conv_fork",
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail if the forwarder tries to create another fork.

        :param request: Incoming request.
        :returns: Never returns.
        """
        raise AssertionError(f"unexpected Omnigent request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        hook_state = await forwarder._ensure_hook_state(
            bridge_dir,
            start_at_end=False,
            session_id="conv_fork",
        )
        rotated_to = await forwarder._maybe_rotate_session_on_fork(
            client=client,
            session_id="conv_fork",
            bridge_dir=bridge_dir,
            state=hook_state,
        )
        replay_state = await forwarder._ensure_hook_state(
            bridge_dir,
            start_at_end=False,
            session_id="conv_fork",
        )
        rotated_again = await forwarder._maybe_rotate_session_on_fork(
            client=client,
            session_id="conv_fork",
            bridge_dir=bridge_dir,
            state=replay_state,
        )

    assert rotated_to == "conv_fork"
    assert rotated_again is None
    assert read_active_session_id(bridge_dir) == "conv_fork"
    transcript_state = json.loads(
        (bridge_dir / "transcript_forwarder.json").read_text(encoding="utf-8")
    )
    assert transcript_state["transcript_path"] == str(transcript_path)
    assert transcript_state["byte_offset"] == transcript_path.stat().st_size
    assert (bridge_dir / "hook_forwarder.json").exists()


@pytest.mark.asyncio
async def test_resume_seen_claude_fork_does_not_create_second_omnigent_fork(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Resuming an already-seen Claude branch does not create another Omnigent fork.

    Claude branch transcripts retain ``forkedFrom`` metadata forever.
    This test fails if the forwarder treats that historical marker
    alone as a fresh `/fork` command after the hook recorded that the
    incoming Claude session had already been seen.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = prepare_bridge_dir(
        "conv_old",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    transcript_path = tmp_path / "fork.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "attachment",
                "timestamp": "2026-05-27T22:53:13.245Z",
                "sessionId": "claude_fork",
                "forkedFrom": {"sessionId": "claude_old"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "source": "resume",
            "session_id": "claude_fork",
            "transcript_path": str(transcript_path),
            "omnigent_previous_claude_session_id": "claude_old",
            "omnigent_claude_session_was_seen": True,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail if the forwarder tries to create another Omnigent fork.

        :param request: Incoming request.
        :returns: Never returns.
        """
        raise AssertionError(f"unexpected Omnigent request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        hook_state = await forwarder._ensure_hook_state(
            bridge_dir,
            start_at_end=False,
            session_id="conv_old",
        )
        rotated_to = await forwarder._maybe_rotate_session_on_fork(
            client=client,
            session_id="conv_old",
            bridge_dir=bridge_dir,
            state=hook_state,
        )

    assert rotated_to is None
    assert read_active_session_id(bridge_dir) == "conv_old"


@pytest.mark.asyncio
async def test_forwarder_posts_visible_transcript_items(tmp_path: Path) -> None:
    """
    The background forwarder reads Claude JSONL and posts Omnigent items.

    This catches the real-Claude failure where a terminal-originated
    prompt/tool/output sequence was written to Claude's transcript
    but no process tailed that transcript into the Omnigent session
    stream.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "user-1",
                        "message": {"role": "user", "content": "read TODO"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-tool-1",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_read_1",
                                    "name": "Read",
                                    "input": {"file_path": "TODO.md"},
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "tool-result-1",
                        "parentUuid": "assistant-tool-1",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_read_1",
                                    "content": "todo contents",
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "attachment",
                        "uuid": "queued-stop",
                        "attachment": {
                            "type": "queued_command",
                            "prompt": "STOP",
                            "commandMode": "prompt",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-text-1",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "hello from transcript"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "subtype": "local_command",
                        "uuid": "bash-input-1",
                        "content": "<bash-input>pwd</bash-input>",
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "subtype": "local_command",
                        "uuid": "bash-output-1",
                        "content": (
                            "<bash-stdout>/tmp/project</bash-stdout><bash-stderr></bash-stderr>"
                        ),
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # The turn-start ``running`` status (carrying the turn's response id)
        # posts first, then the seven transcript items, then the ``Stop`` →
        # idle status. Collect the running edge + 7 items; the trailing idle is
        # not asserted here.
        requests = [await _get_recorded_request(server) for _index in range(8)]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert [request["path"] for request in requests] == ["/v1/sessions/conv_abc/events"] * 8
    assert [request["body"]["type"] for request in requests] == [
        "external_session_status",
        "external_conversation_item",
        "external_conversation_item",
        "external_conversation_item",
        "external_conversation_item",
        "external_conversation_item",
        "external_conversation_item",
        "external_conversation_item",
    ]
    # The leading status is the turn-start ``running`` edge carrying the turn's
    # response id (what drives the live tool-card spinner on the client).
    assert requests[0]["body"]["data"]["status"] == "running"
    assert isinstance(requests[0]["body"]["data"].get("response_id"), str)
    posted = [
        request["body"]["data"]
        for request in requests
        if request["body"]["type"] == "external_conversation_item"
    ]
    assert [item["item_type"] for item in posted] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
        "message",
        "terminal_command",
        "terminal_command",
    ]
    assert posted[0]["item_data"] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "read TODO"}],
    }
    assert posted[1]["item_data"]["name"] == "Read"
    assert posted[1]["item_data"]["call_id"] == "toolu_read_1"
    assert posted[2]["item_data"] == {"call_id": "toolu_read_1", "output": "todo contents"}
    assert posted[3]["item_data"] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "STOP"}],
    }
    assert posted[4]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "hello from transcript"}],
    }
    assert posted[5]["item_data"] == {"kind": "input", "input": "pwd"}
    assert posted[6]["item_data"] == {
        "kind": "output",
        "stdout": "/tmp/project",
        "stderr": "",
    }
    assert posted[1]["response_id"] == posted[2]["response_id"]
    assert posted[3]["response_id"] != posted[2]["response_id"]
    assert posted[4]["response_id"] != posted[2]["response_id"]
    assert posted[5]["response_id"] == posted[6]["response_id"]
    assert posted[5]["response_id"] != posted[4]["response_id"]
    assert posted[1]["response_id"].startswith("resp_claude_")
    # The turn-start running edge carries an assistant turn's response id (here
    # the whole multi-turn transcript flushes in one poll, so it's the last
    # turn's id). The single-turn id↔function_call match is asserted directly in
    # test_forwarder_emits_turn_start_running_with_response_id.
    assert requests[0]["body"]["data"]["response_id"].startswith("resp_claude_")


@pytest.mark.asyncio
async def test_forwarder_mirrors_interrupt_marker_for_ui(tmp_path: Path) -> None:
    """
    End-to-end: Claude's ``[Request interrupted by user]`` IS mirrored to AP.

    Drives the real forwarder over a transcript where the operator interrupts
    a turn (Claude writes its own ``[Request interrupted by user]`` user
    record) and then sends a follow-up. We deliberately keep the marker in
    history so a reload still shows the interruption; the web UI re-classifies
    it as a muted "System: Interrupted" marker (``parseSystemMessage``) rather
    than a raw user bubble. Guards against re-adding a forwarder-side drop
    filter, which would starve the UI of the marker.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "user-1",
                        "message": {"role": "user", "content": "write an essay"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-1",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Once upon a"}],
                        },
                    }
                ),
                # Operator pressed Escape — Claude's own interrupt record.
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "interrupt-1",
                        "message": {"role": "user", "content": "[Request interrupted by user]"},
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "user-2",
                        "message": {"role": "user", "content": "never mind, say hi"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # All 4 items reach AP, in order: user, assistant, the interrupt
        # marker, follow-up user. The marker is kept (the UI renders it as a
        # system marker); if a drop filter regressed, only 3 would post and
        # the 4th collection would hang past the timeout.
        requests = [await _get_recorded_request(server) for _index in range(4)]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    posted = [
        request["body"]["data"]
        for request in requests
        if request["body"]["type"] == "external_conversation_item"
    ]
    texts = [item["item_data"]["content"][0]["text"] for item in posted]
    assert texts == [
        "write an essay",
        "Once upon a",
        "[Request interrupted by user]",
        "never mind, say hi",
    ], f"Forwarder must mirror all turns including the interrupt marker; got {texts!r}"


@pytest.mark.asyncio
async def test_forwarder_posts_web_injected_terminal_transcript_items(tmp_path: Path) -> None:
    """
    Web-injected messages still surface only after Claude records them.

    The ``claude-native`` executor no longer owns transcript streaming
    for Omnigent turns. This fails if a leftover pause/cursor path suppresses
    terminal-originated output after a web message was typed into Claude.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "owned by executor"}],
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # The turn-start ``running`` status posts first (the transcript has an
        # assistant turn), then the assistant message item.
        running = await _get_recorded_request(server)
        request = await _get_recorded_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert running["body"]["type"] == "external_session_status"
    assert running["body"]["data"]["status"] == "running"
    assert request["path"] == "/v1/sessions/conv_abc/events"
    assert request["body"]["type"] == "external_conversation_item"
    assert request["body"]["data"]["item_type"] == "message"
    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "owned by executor"}],
    }


@pytest.mark.asyncio
async def test_forwarder_posts_idle_on_stop_and_ignores_user_prompt_submit(
    tmp_path: Path,
) -> None:
    """
    ``Stop`` → idle (the authoritative turn-end); ``UserPromptSubmit`` ignored.

    ``Stop`` is the fire-once turn-end edge that drives sub-agent terminal
    delivery (via ``external_session_status``, the codex-shared path). The
    ``running`` edge stays PTY-derived, so ``UserPromptSubmit`` must NOT post a
    status. We record ``UserPromptSubmit`` ahead of ``Stop``: the first (and
    only) ``external_session_status`` POST must be the ``idle`` from ``Stop``.
    A ``running`` arriving first would mean ``UserPromptSubmit`` still maps.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "UserPromptSubmit", "session_id": "claude-session"},
    )
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "Stop", "session_id": "claude-session"},
    )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        request = await _get_recorded_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # The first (and only) status POST is the Stop → idle. A ``running``
    # arriving first would mean UserPromptSubmit is still wrongly mapped.
    assert request["path"] == "/v1/sessions/conv_abc/events"
    assert request["body"] == {
        "type": "external_session_status",
        "data": {"status": "idle"},
    }


@pytest.mark.asyncio
async def test_forwarder_ignores_subagent_stop_failure_hook(
    tmp_path: Path,
) -> None:
    """
    A subagent's ``StopFailure`` must not flip the parent session failed.

    Claude Code subagents (spawned via the Agent tool for e.g. Explore)
    inherit the parent's hook settings and write to the same
    ``hooks.jsonl``. A subagent failing must not mark the *parent* turn
    failed — the parent is still running while it awaits the Agent tool
    result. Subagent transcripts live under a ``subagents/`` directory,
    which the forwarder uses to distinguish them from parent events.
    (Running/idle are no longer hook-derived; ``StopFailure`` →
    ``failed`` is the only mapped status left, so this is the surviving
    subagent-skip case.)
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagent_transcript = tmp_path / "session" / "subagents" / "agent-abc.jsonl"
    subagent_transcript.parent.mkdir(parents=True, exist_ok=True)
    subagent_transcript.write_text("", encoding="utf-8")

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "parent-session",
            "transcript_path": str(transcript_path),
        },
    )
    # Subagent fails first — this must NOT surface as the parent failing.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "subagent-session",
            "transcript_path": str(subagent_transcript),
        },
    )
    # Parent turn fails — this SHOULD surface as the one failed edge.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "parent-session",
            "transcript_path": str(transcript_path),
        },
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # Exactly one status POST: the parent's failed. The subagent
        # StopFailure (recorded first) must be skipped, so no second
        # status POST ever arrives — the bounded wait below must time out.
        first = await _get_recorded_request(server)
        with pytest.raises(AssertionError):
            await _get_recorded_request(server, timeout_s=0.5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert first["body"] == {
        "type": "external_session_status",
        "data": {"status": "failed"},
    }


@pytest.mark.asyncio
async def test_forwarder_ignores_subagent_stop_hook(
    tmp_path: Path,
) -> None:
    """
    A subagent's ``Stop`` must not deliver the parent session as idle.

    Claude Code Task subagents inherit the parent's hook settings and write to
    the same ``hooks.jsonl``. A subagent finishing must NOT post ``idle`` for
    the parent — the parent turn is still running while it awaits the Agent
    tool result, and a parent ``idle`` triggers terminal sub-agent delivery.
    Subagent transcripts live under a ``subagents/`` directory, which the
    forwarder uses to skip them. We record a subagent ``Stop`` ahead of the
    parent ``Stop``: the one and only idle POST must be the parent's.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagent_transcript = tmp_path / "session" / "subagents" / "agent-abc.jsonl"
    subagent_transcript.parent.mkdir(parents=True, exist_ok=True)
    subagent_transcript.write_text("", encoding="utf-8")

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "parent-session",
            "transcript_path": str(transcript_path),
        },
    )
    # Subagent stops first — this must NOT surface as the parent going idle.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "subagent-session",
            "transcript_path": str(subagent_transcript),
        },
    )
    # Parent turn ends — this SHOULD surface as the one idle edge.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "parent-session",
            "transcript_path": str(transcript_path),
        },
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # Exactly one status POST: the parent's idle. The subagent ``Stop``
        # (recorded first) must be skipped, so no second status POST arrives —
        # the bounded wait below must time out.
        first = await _get_recorded_request(server)
        with pytest.raises(AssertionError):
            await _get_recorded_request(server, timeout_s=0.5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert first["body"] == {
        "type": "external_session_status",
        "data": {"status": "idle"},
    }


@pytest.mark.asyncio
async def test_forwarder_posts_compaction_in_progress_on_precompact_hook(
    tmp_path: Path,
) -> None:
    """
    Claude Code's ``PreCompact`` hook surfaces as ``in_progress``.

    Claude compacts its own context in the terminal (manual ``/compact``
    or automatic overflow); the Omnigent server never runs the compaction for
    a claude-native session. Without forwarding ``PreCompact``, the web
    UI gets no signal while Claude compacts — the gap the user reported
    (the summary flushes in with no "Compacting…" spinner). The
    forwarder maps it to ``external_compaction_status: in_progress`` so
    Omnigent can publish the spinner SSE.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    # SessionStart (no source) populates transcript_path so the forwarder
    # enters its loop; it is NOT a compaction edge and must NOT post.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "PreCompact", "session_id": "claude-session"},
    )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        request = await _get_recorded_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # First POST is the compaction in_progress — the plain SessionStart
    # before it produced no POST (it is not a compaction edge). If this
    # were external_session_status or absent, the spinner would never
    # appear for claude-native compaction.
    assert request["path"] == "/v1/sessions/conv_abc/events"
    assert request["body"] == {
        "type": "external_compaction_status",
        "data": {"status": "in_progress"},
    }


@pytest.mark.asyncio
async def test_forwarder_posts_compaction_completed_on_compact_session_start(
    tmp_path: Path,
) -> None:
    """
    Post-compaction ``SessionStart source=compact`` surfaces as ``completed``.

    Claude Code has no dedicated post-compaction hook; it resumes on the
    freshly-compacted context with a ``SessionStart`` whose ``source`` is
    ``"compact"``. The forwarder maps exactly that source to
    ``external_compaction_status: completed`` so the web UI upgrades the
    spinner to the permanent "Conversation compacted" marker. Other
    SessionStart sources (startup/resume/clear) are not compaction and
    must not post this.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    # Initial SessionStart enters the loop (not a compaction edge).
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    # Post-compaction SessionStart — the completion signal.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": "claude-session",
        },
    )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        request = await _get_recorded_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # First (and only) POST is compaction completed. If the source check
    # regressed (e.g. firing for every SessionStart), startup/resume would
    # spuriously emit completed and flicker the UI marker.
    assert request["path"] == "/v1/sessions/conv_abc/events"
    assert request["body"] == {
        "type": "external_compaction_status",
        "data": {"status": "completed"},
    }


@pytest.mark.asyncio
async def test_forwarder_does_not_post_compaction_on_non_compact_session_start(
    tmp_path: Path,
) -> None:
    """
    A non-compact ``SessionStart`` (``source=startup``) emits no compaction.

    Guards the source check specifically: only ``source == "compact"``
    is the completion signal. A regression that fired on any
    SessionStart — or used ``source is not None`` instead of
    ``== "compact"`` — would spuriously flash the "Conversation
    compacted" marker on every startup/resume. We record a
    ``startup`` SessionStart followed by ``StopFailure``; because records
    are processed in order, a spurious compaction POST would land BEFORE
    the ``StopFailure`` → failed POST, so asserting the first POST is the
    failed status proves the startup SessionStart emitted nothing.
    (``StopFailure`` is used as the anchor because ``Stop`` no longer
    posts a status — idle now comes from PTY pane activity.)
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "StopFailure", "session_id": "claude-session"},
    )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        request = await _get_recorded_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # The first POST is the StopFailure→failed status, NOT a compaction
    # event: the preceding startup SessionStart produced nothing. If this
    # body were external_compaction_status, the source check regressed.
    assert request["path"] == "/v1/sessions/conv_abc/events"
    assert request["body"] == {
        "type": "external_session_status",
        "data": {"status": "failed"},
    }


class _CountingAuth(httpx.Auth):
    """
    Test httpx Auth that mints a unique bearer per request.

    Stamps ``Bearer token-<n>`` into ``Authorization`` where ``n`` is
    the one-based call count. The counter is the observable that
    proves the forwarder invokes the auth flow per outbound request
    instead of capturing a single Authorization header at client
    construction.
    """

    def __init__(self) -> None:
        """
        Initialize the auth with a zero call counter.

        :returns: None.
        """
        self.calls = 0

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """
        Stamp a fresh ``Bearer token-<n>`` on every outgoing request.

        :param request: Outgoing httpx request.
        :yields: The request with a freshly minted ``Authorization``
            header.
        """
        self.calls += 1
        request.headers["Authorization"] = f"Bearer token-{self.calls}"
        yield request


@pytest.mark.asyncio
async def test_forwarder_uses_auth_to_refresh_token_per_request(tmp_path: Path) -> None:
    """
    Each outbound HTTP request carries a freshly minted bearer token.

    Regression test for the production bug where the forwarder
    captured the bearer at startup and never refreshed it. After the
    ~1h Databricks OAuth token TTL, the stale token caused the
    forwarder to spin in a permanent retry loop while the runner
    kept processing turns — results never reached the UI. The fix
    threads an ``httpx.Auth`` through the forwarder so the
    Authorization header is recomputed on every request.

    This test fails if the forwarder reverts to passing the bearer
    as a static header on the ``AsyncClient`` (httpx snapshots
    construction-time headers into ``client.headers`` and later
    dict mutation does not propagate to in-flight requests).
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    # Two assistant transcript items → two external_conversation_item
    # POSTs, which is all this test needs: distinct bearers on two
    # outbound requests. We use transcript items rather than hook status
    # because running/idle are no longer hook-derived (only
    # StopFailure→failed remains, a single edge).
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "first"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "a2",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "second"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # SessionStart sets transcript_path so the forwarder reads the
    # transcript above; it posts no status of its own.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    auth = _CountingAuth()
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
            auth=auth,
        )
    )
    try:
        # Two external_conversation_item POSTs (one per assistant item).
        # The PATCH that mirrors the Claude session id is filtered out
        # by ``_get_recorded_request``'s default ``method="POST"``.
        first = await _get_recorded_request(server)
        second = await _get_recorded_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # Each POST carried a non-empty bearer minted by the auth flow
    # (matches ``Bearer token-<n>`` for some ``n``). The pattern check
    # would fail with ``None`` if auth were not threaded into the
    # AsyncClient at all.
    assert first["authorization"] is not None and first["authorization"].startswith(
        "Bearer token-"
    ), (
        f"First POST must carry a bearer minted by the counting auth, "
        f"got {first['authorization']!r}. ``None`` means auth was not "
        f"threaded into httpx.AsyncClient."
    )
    assert second["authorization"] is not None and second["authorization"].startswith(
        "Bearer token-"
    ), (
        f"Second POST must carry a bearer minted by the counting auth, "
        f"got {second['authorization']!r}."
    )
    # The load-bearing assertion: the two POSTs carry DIFFERENT
    # bearers. If they were equal, httpx would be reusing a
    # construction-time header snapshot instead of consulting the
    # auth flow per request — that is exactly the production bug.
    assert first["authorization"] != second["authorization"], (
        f"Two consecutive POSTs share the same Authorization "
        f"({first['authorization']!r}). The AsyncClient is reusing a "
        f"snapshot of the original header instead of consulting auth "
        f"on each request — this is the production token-refresh bug."
    )
    # Auth.auth_flow ran at least twice (one per recorded POST).
    # The mirroring PATCH may add one more invocation; the lower
    # bound is what matters — anything less means a request bypassed
    # the auth path entirely.
    assert auth.calls >= 2, (
        f"Expected the counting auth to fire at least twice (one per item POST), got {auth.calls}."
    )


@pytest.mark.asyncio
async def test_forwarder_posts_external_session_status_on_stop_failure_hook(
    tmp_path: Path,
) -> None:
    """
    ``StopFailure`` maps to ``session.status`` failed, not idle.

    A regression that collapses both Stop variants to ``idle`` would
    silently hide turn errors from the web UI — the user would see
    the session return to idle as if everything succeeded.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "StopFailure", "session_id": "claude-session"},
    )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        request = await _get_recorded_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert request["body"] == {
        "type": "external_session_status",
        "data": {"status": "failed"},
    }


@pytest.mark.asyncio
async def test_forwarder_start_at_end_uses_byte_offset_for_new_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Reattach mode seeds from the last complete record and tails from there.

    This catches the hot-path regression where ``start_at_end=True``
    counted every old transcript line and subsequent polls rescanned
    the whole file. The compatibility line-cursor reader is patched
    to fail so the test proves the new byte-offset path is used.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    old_prefix = "".join(
        json.dumps(
            {
                "type": "user",
                "uuid": f"old-{index}",
                "message": {"role": "user", "content": f"old {index}"},
            }
        )
        + "\n"
        for index in range(100)
    )
    partial_record = (
        '{"type":"assistant","uuid":"new-assistant","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"new only"}]}'
    )
    transcript_path.write_text(old_prefix + partial_record, encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )

    def _fail_line_cursor_reader(*args: object, **kwargs: object) -> None:
        """
        Fail if start-at-end falls back to the full-file compatibility reader.

        :param args: Positional reader arguments.
        :param kwargs: Keyword reader arguments.
        :returns: Never returns.
        """
        del args, kwargs
        raise AssertionError("start_at_end should seed and poll with byte offsets")

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.read_transcript_items_since_with_position",
        _fail_line_cursor_reader,
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=True,
            poll_interval_s=0.01,
        )
    )
    try:
        state = await _wait_for_json_file(bridge_dir / "transcript_forwarder.json")
        assert state["byte_offset"] == len(old_prefix.encode("utf-8"))
        with transcript_path.open("a", encoding="utf-8") as handle:
            handle.write("}\n")
        request = await _get_recorded_item_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert request["body"]["type"] == "external_conversation_item"
    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "new only"}],
    }


@pytest.mark.asyncio
async def test_forwarder_migrates_line_cursor_state_to_byte_offset(tmp_path: Path) -> None:
    """
    Old transcript forwarder state gains a byte cursor after one poll.

    Existing users can have ``transcript_forwarder.json`` files that
    only contain ``line_cursor``. The first poll must preserve their
    cursor semantics, forward only new records after that line, and
    persist ``byte_offset`` so later polls avoid full-file rescans.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "old-user",
                "message": {"role": "user", "content": "already forwarded"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "uuid": "new-assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "after old cursor"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    (bridge_dir / "transcript_forwarder.json").write_text(
        json.dumps(
            {
                "transcript_path": str(transcript_path),
                "line_cursor": 1,
                "current_response_id": None,
                "seen_source_ids": [],
            }
        ),
        encoding="utf-8",
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        request = await _get_recorded_item_request(server)
        state = await _wait_for_json_state(
            bridge_dir / "transcript_forwarder.json",
            lambda payload: payload.get("line_cursor") == 2 and "byte_offset" in payload,
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert request["body"]["type"] == "external_conversation_item"
    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "after old cursor"}],
    }
    assert state["line_cursor"] == 2
    assert state["byte_offset"] == transcript_path.stat().st_size


@pytest.mark.asyncio
async def test_forwarder_waits_for_missing_fresh_transcript_without_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A new conversation does not warn before Claude creates its transcript.

    Claude hooks can advertise ``transcript_path`` before the JSONL file
    exists. The forwarder should keep the fresh zero cursor, stay quiet
    while the file is missing, and forward the first item once Claude
    creates the file.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "transcript_path": str(transcript_path),
        },
    )
    caplog.set_level(logging.WARNING, logger="omnigent.claude_native_forwarder")

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_fresh",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        state = await _wait_for_json_state(
            bridge_dir / "transcript_forwarder.json",
            lambda payload: (
                payload.get("byte_offset") == 0 and "cursor_fingerprint" not in payload
            ),
        )
        assert state["line_cursor"] == 0
        assert "cursor invalid" not in caplog.text

        transcript_path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "first-assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "first reply"}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        request = await _get_recorded_item_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert request["body"]["type"] == "external_conversation_item"
    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "first reply"}],
    }
    assert "cursor invalid" not in caplog.text
    assert "cursor missing fingerprint" not in caplog.text
    assert "cursor fingerprint changed" not in caplog.text


@pytest.mark.asyncio
async def test_forwarder_skips_to_end_on_stale_byte_cursor_state(tmp_path: Path) -> None:
    """
    Stale byte-offset state skips to end of the replaced transcript.

    A transcript path can be replaced or truncated between polls (e.g.
    after Claude auto-compacts). The forwarder skips to the end of the
    new file so existing content is not re-forwarded, then picks up
    newly-appended records on subsequent polls.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    # Existing content that should NOT be re-forwarded after the skip.
    existing_content = (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "compacted summary"}],
                },
            }
        )
        + "\n"
    )
    transcript_path.write_text(existing_content, encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    (bridge_dir / "transcript_forwarder.json").write_text(
        json.dumps(
            {
                "transcript_path": str(transcript_path),
                "line_cursor": 25,
                "byte_offset": 4096,
                "cursor_fingerprint": "stale",
                "current_response_id": "resp_old",
                "seen_source_ids": ["byte-4096:25:message"],
            }
        ),
        encoding="utf-8",
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        expected_offset = len(existing_content.encode("utf-8"))
        await _wait_for_json_state(
            bridge_dir / "transcript_forwarder.json",
            lambda payload: (
                payload.get("byte_offset") == expected_offset
                and isinstance(payload.get("cursor_fingerprint"), str)
            ),
        )
        # Drain any non-item requests (e.g. PATCH external_session_id).
        item_posts = []
        while not server.requests.empty():
            req = server.requests.get_nowait()
            if req.get("body", {}).get("type") == "external_conversation_item":
                item_posts.append(req)
        assert item_posts == [], (
            "Forwarder should NOT have posted existing content after skip-to-end"
        )
        # Append a NEW record that should be forwarded.
        new_record = (
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "new-after-compaction",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "new output"}],
                    },
                }
            )
            + "\n"
        )
        with transcript_path.open("a", encoding="utf-8") as f:
            f.write(new_record)
        # The new record should be forwarded.
        request = await _get_recorded_item_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "new output"}],
    }


@pytest.mark.asyncio
async def test_forwarder_skips_to_end_on_out_of_range_byte_cursor_without_fingerprint(
    tmp_path: Path,
) -> None:
    """
    A legacy byte cursor beyond EOF skips to the end of the truncated file.

    Older state files can contain ``byte_offset`` without
    ``cursor_fingerprint``. If the transcript was truncated afterward
    (e.g. compaction), the forwarder skips to the end of the new file
    so existing content is not re-forwarded, then picks up new records.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    existing_content = (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "after truncation"}],
                },
            }
        )
        + "\n"
    )
    transcript_path.write_text(existing_content, encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    (bridge_dir / "transcript_forwarder.json").write_text(
        json.dumps(
            {
                "transcript_path": str(transcript_path),
                "line_cursor": 25,
                "byte_offset": 4096,
            }
        ),
        encoding="utf-8",
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        expected_offset = len(existing_content.encode("utf-8"))
        await _wait_for_json_state(
            bridge_dir / "transcript_forwarder.json",
            lambda payload: (
                payload.get("byte_offset") == expected_offset
                and isinstance(payload.get("cursor_fingerprint"), str)
            ),
        )
        # Drain any non-item requests (e.g. PATCH external_session_id).
        item_posts = []
        while not server.requests.empty():
            req = server.requests.get_nowait()
            if req.get("body", {}).get("type") == "external_conversation_item":
                item_posts.append(req)
        assert item_posts == [], (
            "Forwarder should NOT have posted existing content after skip-to-end"
        )
        # Append a new record — this one should be forwarded.
        new_record = (
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "new-post-truncation",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "new output"}],
                    },
                }
            )
            + "\n"
        )
        with transcript_path.open("a", encoding="utf-8") as f:
            f.write(new_record)
        request = await _get_recorded_item_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "new output"}],
    }


@pytest.mark.asyncio
async def test_forwarder_does_not_replay_after_compaction(tmp_path: Path) -> None:
    """
    Regression test: compaction must not cause the forwarder to re-post
    already-forwarded items.

    Simulates the exact bug scenario: the forwarder has a valid cursor at
    the end of the original transcript, then Claude compacts (rewrites the
    file with new content and different UUIDs). The forwarder must skip to
    the end of the compacted file without posting any of its content, then
    forward only records appended after compaction.

    Before the fix, the forwarder would reset to byte 0 and re-post every
    record in the compacted file, causing the web UI to "replay" the entire
    conversation history in real time.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"

    # Phase 1: write the "original" transcript and compute its fingerprint.
    original_records = "".join(
        json.dumps(
            {
                "type": "assistant",
                "uuid": f"original-{i}",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"original message {i}"}],
                },
            }
        )
        + "\n"
        for i in range(5)
    )
    transcript_path.write_text(original_records, encoding="utf-8")
    original_end = len(original_records.encode())
    original_fingerprint = forwarder._jsonl_cursor_fingerprint(transcript_path, original_end)

    # Phase 2: simulate compaction — replace the file with a summary that
    # has DIFFERENT UUIDs (as Claude does during auto-compaction).
    compacted_records = "".join(
        json.dumps(
            {
                "type": "assistant",
                "uuid": f"compacted-{i}",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"compacted summary {i}"}],
                },
            }
        )
        + "\n"
        for i in range(3)
    )
    transcript_path.write_text(compacted_records, encoding="utf-8")
    compacted_end = len(compacted_records.encode())

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )

    # State file simulates a forwarder that had successfully forwarded the
    # original transcript up to the end. The fingerprint will NOT match the
    # compacted file — this is what triggers the skip-to-end behavior.
    (bridge_dir / "transcript_forwarder.json").write_text(
        json.dumps(
            {
                "transcript_path": str(transcript_path),
                "line_cursor": 5,
                "byte_offset": original_end,
                "cursor_fingerprint": original_fingerprint,
                "current_response_id": "resp_old_turn",
                "seen_source_ids": [f"original-{i}:0:message" for i in range(5)],
            }
        ),
        encoding="utf-8",
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_compaction_test",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # Wait until the forwarder has actually recovered the stale cursor.
        # A fixed sleep is racy on slow CI: if the post-compaction append
        # lands before validation runs, the stale-cursor recovery correctly
        # skips to the then-current end and this test falsely reports that
        # the fresh record was dropped.
        await _wait_for_json_state(
            bridge_dir / "transcript_forwarder.json",
            lambda payload: payload.get("byte_offset") == compacted_end,
        )

        # Drain any non-item requests (e.g. PATCH external_session_id).
        item_posts = []
        while not server.requests.empty():
            req = server.requests.get_nowait()
            if req.get("body", {}).get("type") == "external_conversation_item":
                item_posts.append(req)
        assert item_posts == [], (
            "Forwarder should NOT have posted compacted content. This is the "
            "compaction replay bug — the forwarder re-posted items that "
            "were already in the web UI."
        )

        # Phase 3: append a genuinely new record (Claude resuming work after
        # compaction). This SHOULD be forwarded.
        new_record = (
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "new-after-compaction",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "fresh output after compaction"}],
                    },
                }
            )
            + "\n"
        )
        with transcript_path.open("a", encoding="utf-8") as f:
            f.write(new_record)

        # The new record should be the only item forwarded (the turn also
        # emits a leading running status edge, which this helper skips).
        request = await _get_recorded_item_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # Only the post-compaction record was forwarded.
    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "fresh output after compaction"}],
    }


@pytest.mark.asyncio
async def test_forwarder_migrates_hook_cursor_state_to_byte_offset(tmp_path: Path) -> None:
    """
    Old hook forwarder state gains a byte cursor after one status post.

    Hook state migration must be per-record: a skipped ``SessionStart``
    and a posted ``StopFailure`` should advance the durable byte offset
    so the next poll does not rescan or repost either record.
    (``StopFailure`` is the posted-status anchor because ``Stop`` no
    longer maps to a status — idle now comes from PTY pane activity.)
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "StopFailure", "session_id": "claude-session"},
    )
    (bridge_dir / "hook_forwarder.json").write_text(
        json.dumps({"event_cursor": 1}),
        encoding="utf-8",
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        request = await _get_recorded_request(server)
        state = await _wait_for_json_state(
            bridge_dir / "hook_forwarder.json",
            lambda payload: payload.get("event_cursor") == 2 and "byte_offset" in payload,
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert request["body"] == {
        "type": "external_session_status",
        "data": {"status": "failed"},
    }
    assert state["event_cursor"] == 2
    assert state["byte_offset"] == (bridge_dir / "hooks.jsonl").stat().st_size


@pytest.mark.asyncio
async def test_forwarder_survives_unhandled_loop_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A non-HTTP loop exception is logged and the next poll continues.

    This fails if a disk or parsing exception tears down the
    background forwarder task, which leaves the browser mirror frozen
    without surfacing a session event.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "survives-loop-error",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "after loop error"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    original_forward = forwarder._forward_available_items
    fail_once = True

    async def _fail_once_forward_available_items(
        **kwargs: Any,
    ) -> forwarder.TranscriptForwardState:
        """
        Raise once, then delegate to the real forwarder.

        :param kwargs: Keyword arguments passed by
            :func:`forward_claude_transcript_to_session`.
        :returns: Updated transcript forward state.
        """
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise PermissionError("state write failed")
        return await original_forward(**kwargs)

    monkeypatch.setattr(
        forwarder,
        "_forward_available_items",
        _fail_once_forward_available_items,
    )
    caplog.set_level(logging.ERROR, logger="omnigent.claude_native_forwarder")

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        request = await _get_recorded_item_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert "Claude transcript forwarder loop failed" in caplog.text
    assert request["body"]["type"] == "external_conversation_item"
    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "after loop error"}],
    }


@pytest.mark.asyncio
async def test_forwarder_drops_poison_item_after_bounded_permanent_retries(
    tmp_path: Path,
) -> None:
    """
    Permanent item rejections eventually advance the transcript cursor.

    A malformed transcript item that Omnigent rejects with a permanent 4xx
    should not be reposted forever at the poll interval. After the
    retry budget is exhausted, the forwarder emits a failed status,
    marks the source id handled, and persists the new byte cursor.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "poison-item",
                "message": {"role": "assistant", "content": "bad item"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    retry_tracker = forwarder._PostRetryTracker(
        max_permanent_attempts=2,
        base_delay_s=0.0,
        max_delay_s=0.0,
    )
    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """
        Reject conversation items but accept failure status posts.

        :param request: Outbound HTTP request from the forwarder.
        :returns: HTTP response for the mock Omnigent endpoint.
        """
        payload = json.loads(request.content.decode("utf-8"))
        assert isinstance(payload, dict)
        requests.append(payload)
        if payload["type"] == "external_conversation_item":
            return httpx.Response(422, json={"error": "bad item"})
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dedupe = forwarder._ForwardDedupeState()
        first = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )
        second = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=first,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )

    persisted = json.loads((bridge_dir / "transcript_forwarder.json").read_text("utf-8"))
    # The turn-start ``running`` status (carrying the turn's response id) leads,
    # then the poison item is attempted twice, then the forwarder-failed status.
    assert [request["type"] for request in requests] == [
        "external_session_status",
        "external_conversation_item",
        "external_conversation_item",
        "external_session_status",
    ]
    assert requests[0]["data"]["status"] == "running"
    assert requests[-1]["data"] == {"status": "failed", "response_id": requests[0]["data"]["response_id"]}
    assert first.byte_offset == 0
    assert second.byte_offset == transcript_path.stat().st_size
    assert second.line_cursor == 1
    assert second.seen_source_ids == ("poison-item:0:message",)
    assert persisted["byte_offset"] == transcript_path.stat().st_size
    assert persisted["seen_source_ids"] == ["poison-item:0:message"]


@pytest.mark.asyncio
async def test_forwarder_skips_user_item_on_ambiguous_post_failure(tmp_path: Path) -> None:
    """
    An ambiguous POST failure skips the item instead of re-posting it.

    A user message typed while Claude is busy round-trips through the
    transcript and is POSTed as an ``external_conversation_item``. If
    that POST's response is lost (e.g. a read timeout AFTER the server
    appended the item and published ``session.input.consumed``), the
    server has already committed it — and external items are not deduped
    server-side. Retrying would append a second copy and re-publish the
    consume event, producing a duplicate user bubble in the web UI.
    The forwarder must instead treat the item as delivered:
    mark it handled, advance the byte cursor, and never re-POST it.

    A failure here (the item POSTed twice across two polls) is exactly
    the duplicate-user-message regression this guards against.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "user-msg-1",
                "message": {"role": "user", "content": "hello while busy"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    retry_tracker = forwarder._PostRetryTracker(base_delay_s=0.0, max_delay_s=0.0)
    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """
        Record the POST, then fail the item POST with a read timeout.

        The timeout stands in for "server committed, response lost" —
        the ambiguous case where a blind retry duplicates.

        :param request: Outbound HTTP request from the forwarder.
        :returns: HTTP response (never reached for the item POST).
        :raises httpx.ReadTimeout: For every ``external_conversation_item``
            POST, simulating a lost response.
        """
        payload = json.loads(request.content.decode("utf-8"))
        assert isinstance(payload, dict)
        requests.append(payload)
        if payload["type"] == "external_conversation_item":
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dedupe = forwarder._ForwardDedupeState()
        first = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )
        second = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=first,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )

    item_posts = [r for r in requests if r["type"] == "external_conversation_item"]
    # The item was POSTed exactly once. If the ambiguous-failure skip
    # were missing, the second poll would re-read offset 0 and POST it
    # again (len 2) — the duplicate user bubble.
    assert len(item_posts) == 1
    # No "failed" status: unlike a permanent 4xx rejection, an ambiguous
    # failure most likely succeeded, so we must not flag the turn failed.
    assert all(r["type"] != "external_session_status" for r in requests)
    # Cursor advanced past the item and it is recorded as handled, so it
    # is not re-read on subsequent polls.
    assert first.byte_offset == transcript_path.stat().st_size
    assert first.seen_source_ids == ("user-msg-1:0:message",)
    assert second.byte_offset == transcript_path.stat().st_size


@pytest.mark.asyncio
async def test_forwarder_retries_user_item_on_connect_error(tmp_path: Path) -> None:
    """
    A provably-undelivered POST failure is retried, not dropped.

    A connection-refused error proves the request never reached the
    server, so the item was not committed. Dropping it would silently
    lose a user message. The forwarder must hold the cursor and re-POST
    on the next poll — the complement to the ambiguous-skip behavior, so
    the duplicate fix does not turn into a message-loss bug.

    A failure here (item marked handled / cursor advanced after a
    connect error) would mean a user message is silently lost whenever
    the server is briefly unreachable.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "user-msg-2",
                "message": {"role": "user", "content": "server is down"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    retry_tracker = forwarder._PostRetryTracker(base_delay_s=0.0, max_delay_s=0.0)
    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """
        Fail every item POST with a connection error (never delivered).

        :param request: Outbound HTTP request from the forwarder.
        :returns: HTTP response (never reached for the item POST).
        :raises httpx.ConnectError: For every ``external_conversation_item``
            POST, simulating an unreachable server.
        """
        payload = json.loads(request.content.decode("utf-8"))
        assert isinstance(payload, dict)
        requests.append(payload)
        if payload["type"] == "external_conversation_item":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dedupe = forwarder._ForwardDedupeState()
        first = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )
        second = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=first,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )

    item_posts = [r for r in requests if r["type"] == "external_conversation_item"]
    # Re-POSTed on the second poll (2 attempts): a connect error proves
    # non-delivery, so the item must be retried, not skipped.
    assert len(item_posts) == 2
    # Cursor held at the start and the item never marked handled, so it
    # keeps being retried until it lands.
    assert first.byte_offset == 0
    assert first.seen_source_ids == ()
    assert second.byte_offset == 0


@pytest.mark.asyncio
async def test_forwarder_state_writes_run_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Cursor state persistence uses a worker thread for fsync writes.

    This catches regressions where the async forwarder calls the
    sync atomic writer directly and blocks the event loop for every
    transcript item.
    """
    main_thread_id = threading.get_ident()
    writer_thread_ids: list[int] = []
    original_write = forwarder._write_json_atomic

    def _recording_write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        """
        Record the thread used for the atomic JSON write.

        :param path: Destination JSON path.
        :param payload: JSON payload to write.
        :returns: None.
        """
        writer_thread_ids.append(threading.get_ident())
        original_write(path, payload)

    monkeypatch.setattr(forwarder, "_write_json_atomic", _recording_write_json_atomic)
    await forwarder._write_forward_state_async(
        tmp_path / "bridge",
        forwarder.TranscriptForwardState(
            transcript_path=tmp_path / "session.jsonl",
            line_cursor=0,
            byte_offset=0,
            cursor_fingerprint="fingerprint",
        ),
    )

    assert writer_thread_ids
    assert all(thread_id != main_thread_id for thread_id in writer_thread_ids)


@pytest.mark.asyncio
async def test_forwarder_mirrors_external_session_id_after_hook_event(
    tmp_path: Path,
) -> None:
    """
    Forwarder PATCHes the Omnigent conversation with Claude's session id.

    After the bridge records a hook event carrying ``session_id``
    (every hook from Claude does), the forwarder's first loop pass
    PATCHes ``/v1/sessions/{id}`` with the captured value as
    ``external_session_id``. This is the mirror PR 2's resume flow
    depends on — without it, cold-resume has no way to recover the
    claude-side session that the bridge captured locally.
    """
    bridge_dir = tmp_path / "bridge"
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "a1b2c3d4-1234-5678-9abc-def012345678",
            "transcript_path": str(tmp_path / "session.jsonl"),
        },
    )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        patch_request = await _get_recorded_request(server, method="PATCH")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # Path proves the PATCH targets the right session — a bug that
    # PATCHed e.g. the agent record would route here with a
    # different prefix.
    assert patch_request["path"] == "/v1/sessions/conv_abc"
    # Body asserts the captured Claude id flowed through unchanged.
    # If the bridge state read returned the wrong key or the
    # request body construction dropped the field, the assertion
    # against the literal uuid catches it.
    assert patch_request["body"] == {
        "external_session_id": "a1b2c3d4-1234-5678-9abc-def012345678",
    }


@pytest.mark.asyncio
async def test_forwarder_mirrors_external_session_id_at_most_once(
    tmp_path: Path,
) -> None:
    """
    The mirror PATCH is one-shot per forwarder process.

    The forwarder loop polls every ``poll_interval_s``. Without the
    in-process latch the bridge state file still says
    ``claude_session_id=...`` on every tick, so the loop would
    PATCH on every iteration — hammering the server and racing the
    store's overwrite-protection on every poll. This test pumps the
    loop through multiple iterations (transcript posts) and asserts
    no second PATCH ever arrives.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    # Two assistant messages so the loop has work to do across at
    # least two ticks (the existing forwarder tests show this is
    # plenty for the loop to run multiple poll iterations).
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-1",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "first"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-2",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "second"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # ``StopFailure`` (not ``Stop``) so the hook still produces one status
    # POST — ``Stop`` no longer maps to a status (idle comes from PTY pane
    # activity). Its ``session_id`` is what the mirror PATCH latches onto;
    # the failed status is the third POST the loop-pump below consumes.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "claude-sid-once",
            "transcript_path": str(transcript_path),
        },
    )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_once",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # The first PATCH MUST land — covered by the previous test;
        # consume it so it doesn't pollute the residual-queue check.
        first_patch = await _get_recorded_request(server, method="PATCH")
        assert first_patch["body"]["external_session_id"] == "claude-sid-once"
        # Pump several POST requests through the loop — proves the
        # loop ran multiple iterations after the first PATCH. The
        # bridge state still carries claude_session_id; the only
        # reason no second PATCH arrives is the in-process latch.
        for _ in range(3):
            await _get_recorded_request(server, method="POST")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # Drain whatever is left in the queue and assert no PATCH
    # snuck in. (queue.empty() with FIFO + post-cancel teardown
    # gives a consistent snapshot.)
    leftover_patches: list[dict[str, Any]] = []
    while not server.requests.empty():
        item = server.requests.get_nowait()
        if item.get("method") == "PATCH":
            leftover_patches.append(item)
    # If the latch broke, multiple PATCH requests would accumulate
    # across the ~3 iterations we forced. Asserting on the literal
    # list (not just length) gives a useful diff in failure output.
    assert leftover_patches == []


@pytest.mark.asyncio
async def test_forwarder_does_not_mirror_when_hook_payload_lacks_session_id(
    tmp_path: Path,
) -> None:
    """
    No PATCH when the bridge has not captured a Claude session id.

    If the hook payload arrives without ``session_id`` (or the
    first poll happens before any hook record exists), the bridge
    state file has no ``claude_session_id`` field and the forwarder
    has nothing to mirror. The PATCH must not fire — otherwise we'd
    send a null/empty external_session_id and the route would 400.
    """
    bridge_dir = tmp_path / "bridge"
    # Hook event WITHOUT session_id so bridge state's
    # ``claude_session_id`` stays unset. The transcript_path field
    # is still present so the rest of the loop has something to
    # poll.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "transcript_path": str(tmp_path / "session.jsonl"),
        },
    )
    # Empty transcript file — the loop runs but produces no
    # transcript posts either.
    (tmp_path / "session.jsonl").write_text("", encoding="utf-8")

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_nopatch",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    # Let the loop run a few poll cycles. We can't await a request
    # since none should arrive, so sleep just long enough for the
    # loop to have iterated several times — well above the
    # 10 ms poll interval, well below any reasonable test budget.
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    server.shutdown()
    server.server_close()
    thread.join(timeout=5.0)

    drained: list[dict[str, Any]] = []
    while not server.requests.empty():
        drained.append(server.requests.get_nowait())
    methods = [request["method"] for request in drained]
    # If the forwarder sent ANY PATCH despite missing
    # claude_session_id, the bridge-state-read short-circuit broke
    # — a regression that would route empty/null
    # external_session_id values to the server.
    assert "PATCH" not in methods, f"unexpected PATCH(es): {drained}"


def test_model_alias_for_collapses_concrete_id_to_tier_alias() -> None:
    """
    ``_model_alias_for`` maps a concrete transcript model id to the
    picker's tier alias so a TUI ``/model`` switch lands on a picker
    row. Covers Anthropic + Databricks-gateway id shapes and the
    no-match / empty cases (caller skips the post on ``None``).
    """
    assert forwarder._model_alias_for("claude-opus-4-8") == "opus"
    assert forwarder._model_alias_for("anthropic/claude-opus-4-7") == "opus"
    assert forwarder._model_alias_for("databricks-claude-sonnet-4-6") == "sonnet"
    assert forwarder._model_alias_for("claude-haiku-4-5") == "haiku"
    # Fable (the tier above Opus) collapses to its own alias — a miss
    # here means a TUI switch to claude-fable-5 never reaches the picker.
    assert forwarder._model_alias_for("claude-fable-5") == "fable"
    assert forwarder._model_alias_for("databricks-claude-fable-5") == "fable"
    # Unknown family or empty → None (don't surface an unrenderable id).
    assert forwarder._model_alias_for("gpt-5-4-mini") is None
    assert forwarder._model_alias_for("") is None
    assert forwarder._model_alias_for(None) is None


@pytest.mark.asyncio
async def test_forwarder_mirrors_tui_model_switch_after_baseline(tmp_path: Path) -> None:
    """
    A TUI-side ``/model`` switch is POSTed as ``external_model_change``;
    the spawn-default baseline is NOT (seed-first).

    The first assistant entry establishes the baseline model silently —
    so a passive spawn default never clobbers a pending silent model
    handoff — and a later assistant entry on a different model posts a
    single ``external_model_change`` carrying the normalized tier alias.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"

    def _assistant(uuid: str, model: str, text: str) -> str:
        """
        Build one assistant JSONL line carrying ``message.model``.

        :param uuid: Transcript entry uuid, e.g. ``"a1"``.
        :param model: Concrete model id, e.g. ``"claude-opus-4-8"``.
        :param text: Assistant text content.
        :returns: A JSON-encoded transcript line.
        """
        return json.dumps(
            {
                "type": "assistant",
                "uuid": uuid,
                "message": {
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": text}],
                },
            }
        )

    transcript_path.write_text(_assistant("a1", "claude-opus-4-8", "hi") + "\n", encoding="utf-8")
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    retry_tracker = forwarder._PostRetryTracker()
    dedupe = forwarder._ForwardDedupeState()

    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """
        Accept every forwarder POST and record its payload.

        :param request: Outbound HTTP request from the forwarder.
        :returns: 202 for every event.
        """
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        state = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )
        # First observation seeds the baseline WITHOUT posting a change.
        assert "external_model_change" not in [r["type"] for r in requests]
        assert dedupe.posted_model == "opus"

        # User switches model inside the terminal.
        with transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(_assistant("a2", "claude-sonnet-4-6", "switched") + "\n")
        requests.clear()
        await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )

    model_posts = [r for r in requests if r["type"] == "external_model_change"]
    assert len(model_posts) == 1
    assert model_posts[0]["data"] == {"model": "sonnet"}
    assert dedupe.posted_model == "sonnet"


@pytest.mark.asyncio
async def test_forwarder_retries_model_post_after_transient_failure(tmp_path: Path) -> None:
    """
    A failed ``external_model_change`` POST is retried on a later poll —
    not lost once the switch poll's transcript window is gone.

    ``observed_model`` is sticky across polls, so even a poll whose
    incremental window carries no fresh ``message.model`` (e.g. a plain
    user turn) reconciles the observed alias against the last POSTed one
    and re-attempts the drop. Guards the self-healing contract of the
    model mirror against a single transient Omnigent error.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"

    def _assistant(uuid: str, model: str) -> str:
        """Build an assistant JSONL line carrying ``message.model``."""
        return json.dumps(
            {
                "type": "assistant",
                "uuid": uuid,
                "message": {
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": "x"}],
                },
            }
        )

    def _user(uuid: str) -> str:
        """Build a user JSONL line (no ``message.model``)."""
        return json.dumps(
            {"type": "user", "uuid": uuid, "message": {"role": "user", "content": "thanks"}}
        )

    transcript_path.write_text(_assistant("a1", "claude-opus-4-8") + "\n", encoding="utf-8")
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    retry_tracker = forwarder._PostRetryTracker()
    dedupe = forwarder._ForwardDedupeState()

    model_posts: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """Fail the FIRST external_model_change POST (503); accept the rest."""
        body = json.loads(request.content.decode("utf-8"))
        if body["type"] == "external_model_change":
            model_posts.append(body["data"])
            if len(model_posts) == 1:
                return httpx.Response(503, json={"error": "transient"})
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        async def _poll() -> None:
            nonlocal state
            state = await forwarder._forward_available_items(
                client=client,
                session_id="conv_abc",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=state,
                retry_tracker=retry_tracker,
                dedupe=dedupe,
            )

        # Poll 1: baseline "opus" seeded, no model POST.
        await _poll()
        assert model_posts == []
        assert dedupe.posted_model == "opus"

        # Poll 2: user switches to sonnet; the POST fails transiently.
        with transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(_assistant("a2", "claude-sonnet-4-6") + "\n")
        await _poll()
        assert model_posts == [{"model": "sonnet"}]  # attempted once
        assert dedupe.posted_model == "opus"  # NOT advanced — POST failed
        assert dedupe.observed_model == "sonnet"  # but remembered

        # Poll 3: a plain user turn (no message.model) still retries.
        with transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(_user("u1") + "\n")
        await _poll()
        assert model_posts == [{"model": "sonnet"}, {"model": "sonnet"}]  # retried
        assert dedupe.posted_model == "sonnet"  # now committed


def test_validated_transcript_state_resets_legacy_byte_cursor_without_fingerprint(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Byte-offset state without a fingerprint is treated as stale.

    A replaced transcript cannot be validated from the byte cursor
    alone. The forwarder skips to the end of the transcript to avoid
    re-posting content that was already forwarded.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "replacement",
                "message": {"role": "assistant", "content": "new file"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="omnigent.claude_native_forwarder")

    validated = forwarder._validated_transcript_state(
        forwarder.TranscriptForwardState(
            transcript_path=transcript_path,
            line_cursor=25,
            byte_offset=0,
            current_response_id="resp_old",
            seen_source_ids=("old-source",),
            cursor_fingerprint=None,
        ),
        bridge_dir=tmp_path / "bridge",
        session_id="conv_abc",
    )

    # Cursor skips to end of transcript but preserves seen_source_ids to
    # prevent re-posting items that were already forwarded before the reset.
    expected_end = transcript_path.stat().st_size
    assert validated == forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=expected_end,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, expected_end),
        seen_source_ids=("old-source",),
    )
    assert "cursor missing fingerprint" in caplog.text
    assert "conv_abc" in caplog.text
    assert str(tmp_path / "bridge") in caplog.text


def test_validated_transcript_state_adopts_fingerprint_at_offset_zero_without_reset(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Fresh state at byte_offset=0 with no fingerprint adopts the computed
    fingerprint without resetting seen_source_ids.

    This is the typical case when the forwarder initializes before the
    transcript file exists (fingerprint is None because the file is
    missing), and the file appears later. Since line_cursor is 0 (nothing
    has been read yet), there is no stale position — just adopt the
    fingerprint and keep going.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "first-entry",
                "message": {"role": "assistant", "content": "hello"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="omnigent.claude_native_forwarder")

    pre_existing_seen = ("already-sent-id-1", "already-sent-id-2")
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        current_response_id="resp_in_flight",
        seen_source_ids=pre_existing_seen,
        cursor_fingerprint=None,
    )

    validated = forwarder._validated_transcript_state(
        state,
        bridge_dir=tmp_path / "bridge",
        session_id="conv_fresh",
    )

    expected_fingerprint = forwarder._jsonl_cursor_fingerprint(transcript_path, 0)
    # Fingerprint adopted from the now-existing file, not reset to a blank
    # state. If the fingerprint is None here, the file doesn't exist (test
    # setup bug).
    assert validated.cursor_fingerprint == expected_fingerprint
    assert validated.cursor_fingerprint is not None

    # seen_source_ids preserved — this is the critical fix. Without it, the
    # forwarder would re-read the entire transcript and re-post every item.
    assert validated.seen_source_ids == pre_existing_seen, (
        f"Expected seen_source_ids to be preserved across fingerprint adoption, "
        f"but got {validated.seen_source_ids!r}. If empty, the dedup set was "
        f"cleared and the forwarder will re-post already-delivered items."
    )

    # Other state fields preserved (not zeroed out).
    assert validated.line_cursor == 0
    assert validated.byte_offset == 0
    assert validated.current_response_id == "resp_in_flight"

    # No warning logged — this is a clean adoption, not a stale-cursor reset.
    assert "cursor missing fingerprint" not in caplog.text
    assert "cursor invalid" not in caplog.text
    assert "cursor fingerprint changed" not in caplog.text


def test_validated_transcript_state_preserves_seen_source_ids_on_stale_reset(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    When a cursor reset IS needed (fingerprint changed because the file was
    replaced), seen_source_ids is still preserved to prevent duplicate posts.

    The cursor skips to the end of the replacement file so its existing
    content is not re-forwarded, but the dedup set keeps IDs of items
    already forwarded to the server as a safety net.
    """
    transcript_path = tmp_path / "session.jsonl"
    original_content = (
        json.dumps(
            {
                "type": "assistant",
                "uuid": "original",
                "message": {"role": "assistant", "content": "original"},
            }
        )
        + "\n"
    )
    transcript_path.write_text(original_content, encoding="utf-8")
    original_fingerprint = forwarder._jsonl_cursor_fingerprint(
        transcript_path, len(original_content.encode())
    )

    # Replace the file content — fingerprint at the old offset will differ.
    replacement_content = (
        json.dumps(
            {
                "type": "assistant",
                "uuid": "replacement",
                "message": {"role": "assistant", "content": "replaced"},
            }
        )
        + "\n"
    )
    transcript_path.write_text(replacement_content, encoding="utf-8")

    caplog.set_level(logging.WARNING, logger="omnigent.claude_native_forwarder")

    pre_existing_seen = ("item-a", "item-b", "item-c")
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=5,
        byte_offset=len(original_content.encode()),
        current_response_id="resp_old",
        seen_source_ids=pre_existing_seen,
        cursor_fingerprint=original_fingerprint,
    )

    validated = forwarder._validated_transcript_state(
        state,
        bridge_dir=tmp_path / "bridge",
        session_id="conv_replaced",
    )

    # Cursor skips to end of replacement file (avoids re-posting its content).
    assert validated.line_cursor == 0
    expected_end = len(replacement_content.encode())
    assert validated.byte_offset == expected_end

    # seen_source_ids preserved despite cursor reset — the critical fix.
    # Without this, every item from the replacement file would be posted
    # as new, even if some source IDs overlap with already-forwarded items.
    assert validated.seen_source_ids == pre_existing_seen, (
        f"Expected seen_source_ids to survive cursor reset, but got "
        f"{validated.seen_source_ids!r}. If empty, the dedup safety net "
        f"was destroyed and duplicates will be posted."
    )

    # Warning logged because the fingerprint genuinely changed.
    assert "cursor fingerprint changed" in caplog.text


# ── supervise_forwarder ────────────────────────────────────────────


def _supervisor_kwargs(tmp_path: Path) -> dict[str, Any]:
    """
    Build the kwargs used to invoke :func:`supervise_forwarder` in tests.

    The supervisor passes these through to the (stubbed) forwarder
    coroutine; nothing here has to be a real running service since
    every test patches the forwarder.

    :param tmp_path: Pytest-provided temp directory used as the
        bridge dir argument.
    :returns: Dict of keyword arguments suitable for
        ``supervise_forwarder(**kwargs)``.
    """
    return {
        "base_url": "http://localhost:0",
        "headers": {},
        "session_id": "conv_abc",
        "bridge_dir": tmp_path,
        "agent_name": "claude",
        "start_at_end": False,
    }


@pytest.mark.asyncio
async def test_supervise_forwarder_restarts_after_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A non-cancellation exception in the forwarder restarts it.

    This is the case that left the chat view permanently desynced
    overnight: the forwarder task died inside its own
    ``async with httpx.AsyncClient`` block, the parent's
    ``await _attach_with_reconnect`` kept running, and no one
    restarted the forwarder. With the supervisor we expect a second
    call after the first one raises.
    """
    call_count = 0

    async def fake_forwarder(**_: Any) -> None:
        """Fake forwarder: crash on call 1, signal stop on call 2."""
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # RuntimeError stands in for the kinds of errors that can
            # escape the forwarder's inner ``except Exception`` (e.g.
            # something raised during the ``async with`` setup before
            # the per-iteration try block). The supervisor catches
            # Exception and restarts.
            raise RuntimeError("simulated unrecoverable crash")
        # CancelledError is the ONLY thing the supervisor re-raises,
        # so use it as the test's exit signal once we've verified
        # the restart happened.
        raise asyncio.CancelledError()

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """Record sleeps without waiting."""
        sleeps.append(seconds)

    monkeypatch.setattr(forwarder, "forward_claude_transcript_to_session", fake_forwarder)
    monkeypatch.setattr(forwarder, "_supervisor_sleep", fake_sleep)

    with caplog.at_level(logging.WARNING, logger=forwarder.__name__):
        with pytest.raises(asyncio.CancelledError):
            await forwarder.supervise_forwarder(**_supervisor_kwargs(tmp_path))

    # 2 = first crash + restart. If the supervisor exited after the
    # first crash (the pre-fix behavior), call_count would be 1.
    assert call_count == 2, (
        f"Forwarder should have been called twice (initial + restart), "
        f"got {call_count}. If 1, the supervisor exited on crash "
        f"instead of restarting."
    )
    # One sleep ran — between the crash and the restart. The second
    # call raised CancelledError, which propagates immediately and
    # skips the post-iteration sleep.
    assert sleeps == [forwarder._SUPERVISOR_INITIAL_BACKOFF_S]
    assert "Claude transcript forwarder crashed" in caplog.text


@pytest.mark.asyncio
async def test_supervise_forwarder_propagates_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    :class:`asyncio.CancelledError` exits the supervisor without restarting.

    The parent's ``finally`` block relies on this: ``forwarder.cancel()``
    followed by ``await forwarder`` must complete promptly with a
    single CancelledError, not loop forever on restart.
    """
    call_count = 0
    forwarder_running = asyncio.Event()

    async def fake_forwarder(**_: Any) -> None:
        """Fake forwarder that announces it's running and then blocks."""
        nonlocal call_count
        call_count += 1
        forwarder_running.set()
        # Wait forever — let the test cancel us.
        await asyncio.Event().wait()

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """Record sleeps; cancellation must NOT route through here."""
        sleeps.append(seconds)

    monkeypatch.setattr(forwarder, "forward_claude_transcript_to_session", fake_forwarder)
    monkeypatch.setattr(forwarder, "_supervisor_sleep", fake_sleep)

    supervisor_task = asyncio.create_task(
        forwarder.supervise_forwarder(**_supervisor_kwargs(tmp_path)),
    )
    # Wait until the fake forwarder is actually executing before
    # cancelling, so the cancellation hits inside the forwarder
    # call (the realistic path), not before it even starts.
    await asyncio.wait_for(forwarder_running.wait(), timeout=1.0)
    supervisor_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await supervisor_task

    # Forwarder ran exactly once and no backoff sleep happened —
    # cancellation skipped the restart path entirely.
    assert call_count == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_supervise_forwarder_backoff_grows_on_repeated_crashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Consecutive crashes use exponentially growing backoff, capped at the max.

    Prevents a fast-failing forwarder from POST-storming the Omnigent server
    or burning CPU on tight-loop restarts.
    """
    # 6 crashes is enough to walk past the cap: 1, 2, 4, 8, 16, 30
    # (the 6th would naively be 32 but the cap clamps it to 30).
    crash_budget = 6
    call_count = 0

    async def fake_forwarder(**_: Any) -> None:
        """Crash ``crash_budget`` times, then signal stop."""
        nonlocal call_count
        call_count += 1
        if call_count <= crash_budget:
            raise RuntimeError(f"simulated crash {call_count}")
        raise asyncio.CancelledError()

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """Record sleep durations without waiting."""
        sleeps.append(seconds)

    # Pin monotonic so every run looks instantaneous and the
    # healthy-uptime reset branch never fires.
    monkeypatch.setattr(forwarder, "_supervisor_monotonic", lambda: 1000.0)
    monkeypatch.setattr(forwarder, "forward_claude_transcript_to_session", fake_forwarder)
    monkeypatch.setattr(forwarder, "_supervisor_sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await forwarder.supervise_forwarder(**_supervisor_kwargs(tmp_path))

    # 6 crashes → 6 sleeps with doubling backoff, last clamped to max.
    # If the cap isn't being applied, the 6th entry would be 32.0
    # instead of _SUPERVISOR_MAX_BACKOFF_S (30.0).
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, forwarder._SUPERVISOR_MAX_BACKOFF_S], (
        f"Backoff should double up to the {forwarder._SUPERVISOR_MAX_BACKOFF_S}s "
        f"cap; got {sleeps}."
    )


@pytest.mark.asyncio
async def test_supervise_forwarder_resets_backoff_after_healthy_uptime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A long-running forwarder that eventually crashes resets backoff.

    Without this, a forwarder that ran healthy for hours and then
    hit a transient blip would still wait the full 30s before
    restarting — penalizing successful long runs as if they were
    a crash-loop.
    """
    healthy_threshold = forwarder._SUPERVISOR_HEALTHY_UPTIME_S
    call_count = 0
    # The supervisor calls _supervisor_monotonic() twice per
    # iteration: once at run_started_at, once at run_duration_s.
    # We feed 4 iterations × 2 readings = 8 values, with run 3
    # crossing the healthy threshold.
    monotonic_values = iter(
        [
            # Run 1: short-lived (1s uptime). Backoff stays at initial.
            0.0,
            1.0,
            # Run 2: short-lived (1s uptime). Backoff doubles.
            10.0,
            11.0,
            # Run 3: long-lived (>= threshold). Backoff resets after
            # this iteration completes.
            20.0,
            20.0 + healthy_threshold + 1.0,
            # Run 4: short-lived. Should sleep the post-reset initial
            # value, not the doubled-from-run-3 value.
            200.0,
            201.0,
        ],
    )

    async def fake_forwarder(**_: Any) -> None:
        """Crash 3 times to drive the reset, then signal stop on call 4."""
        nonlocal call_count
        call_count += 1
        if call_count >= 4:
            raise asyncio.CancelledError()
        raise RuntimeError(f"simulated crash {call_count}")

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """Record backoff durations."""
        sleeps.append(seconds)

    monkeypatch.setattr(forwarder, "_supervisor_monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(forwarder, "forward_claude_transcript_to_session", fake_forwarder)
    monkeypatch.setattr(forwarder, "_supervisor_sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await forwarder.supervise_forwarder(**_supervisor_kwargs(tmp_path))

    # Run 1 → sleep 1 (initial). Backoff grows to 2.
    # Run 2 → sleep 2. Backoff grows to 4.
    # Run 3 → healthy, backoff resets to initial BEFORE sleep, then
    #         doubles to 2 after sleep — so sleep value is the initial.
    # Run 4 (CancelledError) → propagates, no further sleep.
    # If the reset branch didn't fire, run 3's sleep would be 4.0
    # instead of 1.0.
    assert sleeps == [
        1.0,
        2.0,
        forwarder._SUPERVISOR_INITIAL_BACKOFF_S,
    ], (
        f"Healthy uptime should reset backoff before the post-iteration sleep; "
        f"got {sleeps}. If the third entry is 4.0, the reset branch is not firing."
    )


@pytest.mark.parametrize(
    "raised_exc",
    [SystemExit("shutdown"), KeyboardInterrupt()],
    ids=["SystemExit", "KeyboardInterrupt"],
)
@pytest.mark.asyncio
async def test_supervise_forwarder_propagates_process_shutdown_signals(
    raised_exc: BaseException,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    :class:`BaseException` subclasses used for shutdown are not swallowed.

    The supervisor only restarts on :class:`Exception`. Process-level
    signals (``KeyboardInterrupt`` from Ctrl-C, ``SystemExit`` from
    ``sys.exit()``) must propagate so the wrapper CLI shuts down
    promptly instead of looping inside an "unkillable" supervisor.
    """
    call_count = 0

    async def fake_forwarder(**_: Any) -> None:
        """Raise the shutdown signal under test on the first call."""
        nonlocal call_count
        call_count += 1
        raise raised_exc

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """Record sleeps; this path should never be reached."""
        sleeps.append(seconds)

    monkeypatch.setattr(forwarder, "forward_claude_transcript_to_session", fake_forwarder)
    monkeypatch.setattr(forwarder, "_supervisor_sleep", fake_sleep)

    with pytest.raises(type(raised_exc)):
        await forwarder.supervise_forwarder(**_supervisor_kwargs(tmp_path))

    # Forwarder ran exactly once and no backoff sleep happened — the
    # shutdown signal propagated through the supervisor without a
    # restart attempt. If call_count is 2+, the supervisor swallowed
    # the signal (the regression).
    assert call_count == 1
    assert sleeps == []


# ── Native task state accumulation ───────────────────────────────────────────


async def _drain_todos_request(server: _RecordingHTTPServer) -> dict[str, Any]:
    """
    Await the first ``external_session_todos`` POST from the forwarder.

    :param server: Recording HTTP server.
    :returns: The ``data`` payload of the matching POST body.
    """
    while True:
        req = await _get_recorded_request(server)
        if req["body"].get("type") == "external_session_todos":
            return req["body"]["data"]


def _record_session_start(bridge_dir: Path, transcript_path: Path) -> None:
    """
    Write a ``SessionStart`` hook event so the forwarder enters its main loop.

    :param bridge_dir: Bridge directory for ``record_hook_event``.
    :param transcript_path: Transcript file path carried in the payload.
    :returns: None.
    """
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )


@pytest.mark.asyncio
async def test_forwarder_posts_todos_on_task_created(tmp_path: Path) -> None:
    """
    A ``TaskCreated`` hook event causes the forwarder to POST an
    ``external_session_todos`` event with the new task at status ``"pending"``.

    This fails if the ``TaskCreated`` branch in the forwarder's hook loop
    fails to set ``native_todos_changed = True`` or if the ``todos_to_post``
    list is not built from the accumulation maps.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    _record_session_start(bridge_dir, transcript_path)
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "TaskCreated",
            "session_id": "claude-session",
            "task_id": "1",
            "task_subject": "Write integration tests",
        },
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        data = await _drain_todos_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert data["todos"] == [
        {
            "content": "Write integration tests",
            "status": "pending",
            # activeForm equals content for native tasks (suppresses
            # duplicate rendering in the panel).
            "activeForm": "Write integration tests",
        }
    ]


@pytest.mark.asyncio
async def test_forwarder_posts_todos_on_task_completed(tmp_path: Path) -> None:
    """
    A ``TaskCreated`` followed by ``TaskCompleted`` causes a final POST
    where the task has status ``"completed"``.

    This fails if ``TaskCompleted`` does not update ``task_statuses`` or
    if ``native_todos_changed`` is not set when it should be.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    _record_session_start(bridge_dir, transcript_path)
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "TaskCreated",
            "session_id": "claude-session",
            "task_id": "1",
            "task_subject": "Fix the bug",
        },
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "TaskCompleted",
            "session_id": "claude-session",
            "task_id": "1",
        },
    )

    server, thread, base_url = _start_recording_server()
    # Drain two consecutive todos POSTs: one for TaskCreated, one for TaskCompleted.
    posted: list[dict[str, Any]] = []
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        posted.append(await _drain_todos_request(server))
        posted.append(await _drain_todos_request(server))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # First POST: task is still pending (from TaskCreated).
    assert posted[0]["todos"][0]["status"] == "pending"
    # Second POST: task is completed (from TaskCompleted).
    assert posted[1]["todos"][0]["status"] == "completed"
    assert posted[1]["todos"][0]["content"] == "Fix the bug"


@pytest.mark.asyncio
async def test_forwarder_posts_raw_todos_on_todo_write(tmp_path: Path) -> None:
    """
    A ``PostToolUse/TodoWrite`` hook event causes the forwarder to POST
    the raw ``tool_input.todos`` list verbatim, bypassing accumulation.

    This fails if the ``record.todos is not None`` branch is not taken
    ahead of the native-task path, or if the list is modified before posting.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    raw_todos = [
        {"content": "Write tests", "status": "in_progress", "activeForm": "Writing tests"},
        {"content": "Review PR", "status": "pending", "activeForm": "Reviewing PR"},
    ]
    _record_session_start(bridge_dir, transcript_path)
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "claude-session",
            "tool_name": "TodoWrite",
            "tool_input": {"todos": raw_todos},
        },
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        data = await _drain_todos_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # The raw list is forwarded verbatim — no accumulation or transformation.
    assert data["todos"] == raw_todos


# ── Sub-agent watcher (Claude Code Task tool) ────────────


def _start_recording_server_with_responses(
    response_for: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[_RecordingHTTPServer, threading.Thread, str]:
    """
    Start a local HTTP server that records POST bodies AND returns
    a customizable response body.

    Variant of :func:`_start_recording_server` for tests that need
    the Omnigent server's response (rather than just a generic 202 ``{}``)
    — used by the sub-agent watcher tests because
    ``external_subagent_start`` returns ``{"child_session_id": "..."}``
    that the forwarder reads back.

    :param response_for: Callback that takes the decoded request
        body and returns the JSON dict to send back. ``None`` (the
        default) responds with ``{}`` like the standard recorder.
    :returns: ``(server, thread, base_url)``.
    """
    requests: queue.Queue[dict[str, Any]] = queue.Queue()

    class _Handler(BaseHTTPRequestHandler):
        """Recording handler with response customization."""

        def log_message(self, format: str, *args: Any) -> None:
            """Suppress test HTTP server logging.

            :param format: Log format string.
            :param args: Log format arguments.
            :returns: None.
            """
            del format, args

        def do_POST(self) -> None:
            """Record a JSON POST body and send a customizable response.

            :returns: None.
            """
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
            requests.put({"method": "POST", "path": self.path, "body": body})
            response_body = {} if response_for is None else response_for(body)
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response_body).encode("utf-8"))

        def do_PATCH(self) -> None:
            """Record a JSON PATCH body and respond ``{}``.

            :returns: None.
            """
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            requests.put(
                {"method": "PATCH", "path": self.path, "body": json.loads(raw.decode("utf-8"))}
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    server = _RecordingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = requests
    thread = threading.Thread(
        target=server.serve_forever,
        name="claude-forwarder-test-ap-subagent",
        daemon=True,
    )
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}"


def _seed_subagent_on_disk(
    *,
    transcript_path: Path,
    subagent_id: str,
    agent_type: str,
    description: str,
    tool_use_id: str,
    transcript_records: list[dict[str, Any]] | None = None,
) -> Path:
    """
    Create the ``.meta.json`` + ``.jsonl`` pair Claude Code would
    write for a Task-tool sub-agent.

    Mirrors the on-disk layout the forwarder's watcher polls:
    ``<transcript_parent>/<transcript_stem>/subagents/agent-<id>.*``.

    :param transcript_path: Parent transcript JSONL path. The sibling
        ``<stem>/subagents/`` directory is created next to it.
    :param subagent_id: Stable Claude-side id (the ``agent-<id>``
        filename stem), e.g. ``"a5c7eff..."``.
    :param agent_type: ``agentType`` value for the meta file,
        e.g. ``"Explore"``.
    :param description: ``description`` value for the meta file.
    :param tool_use_id: ``toolUseId`` value for the meta file.
    :param transcript_records: Optional list of decoded transcript
        rows to seed into the sub-agent's ``.jsonl``. ``None`` /
        empty leaves the transcript empty (the common case when a
        sub-agent has just been spawned).
    :returns: Path to the sub-agent's ``.jsonl`` (handy for tests
        that append rows after the fact).
    """
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    meta_path = subagents_dir / f"agent-{subagent_id}.meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "agentType": agent_type,
                "description": description,
                "toolUseId": tool_use_id,
            }
        ),
        encoding="utf-8",
    )
    jsonl_path = subagents_dir / f"agent-{subagent_id}.jsonl"
    if transcript_records:
        jsonl_path.write_text(
            "\n".join(json.dumps(row) for row in transcript_records) + "\n",
            encoding="utf-8",
        )
    else:
        jsonl_path.write_text("", encoding="utf-8")
    return jsonl_path


async def test_subagent_watcher_posts_external_subagent_start_for_new_meta(
    tmp_path: Path,
) -> None:
    """
    When a new ``agent-<id>.meta.json`` appears under the parent's
    ``subagents/`` dir, the forwarder POSTs ``external_subagent_start``
    with the meta fields and persists the returned ``child_session_id``
    in its durable cursor.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="a5c7eff",
        agent_type="Explore",
        description="Trace the auth flow",
        tool_use_id="toolu_xyz",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )

    def response_for(body: dict[str, Any]) -> dict[str, Any]:
        """Return a minted child id for the subagent_start event.

        :param body: Decoded request body.
        :returns: Response payload.
        """
        if body.get("type") == "external_subagent_start":
            return {"queued": False, "child_session_id": "conv_child_alpha"}
        return {}

    server, _thread, base_url = _start_recording_server_with_responses(response_for)
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_parent",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # Skip the transcript-status / mirror PATCHes that may land
        # before our event, and stop at the first
        # ``external_subagent_start`` we see.
        start_req: dict[str, Any] | None = None
        for _ in range(20):
            req = await _get_recorded_request(server)
            if req["body"].get("type") == "external_subagent_start":
                start_req = req
                break
        assert start_req is not None, "forwarder did not POST external_subagent_start"
        assert start_req["path"] == "/v1/sessions/conv_parent/events"
        assert start_req["body"]["data"] == {
            "subagent_id": "a5c7eff",
            "agent_type": "Explore",
            "description": "Trace the auth flow",
            "tool_use_id": "toolu_xyz",
        }
        # The cursor persists the returned child id so a forwarder
        # restart won't re-mint a duplicate row. Wait on it BEFORE
        # cancelling so the writer's ``asyncio.to_thread`` has time
        # to flush — cancellation can interrupt the inflight write.
        cursor = await _wait_for_json_state(
            bridge_dir / "subagent_forwarder.json",
            lambda payload: "a5c7eff" in payload.get("subagents", {}),
        )
        assert cursor["subagents"]["a5c7eff"]["child_conversation_id"] == "conv_child_alpha"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()


async def test_subagent_watcher_forwards_transcript_items_to_child_session(
    tmp_path: Path,
) -> None:
    """
    After registering a sub-agent, the forwarder tails its
    ``.jsonl`` and POSTs ``external_conversation_item`` events to
    the Omnigent child session id (not the parent's).
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="b6d8fff",
        agent_type="Explore",
        description="Trace data flow",
        tool_use_id="toolu_abc",
        # Real sub-agent transcripts carry ``isSidechain: true`` on
        # every record (that's how Claude marks them as belonging to
        # a child instead of the main thread). The parser's default
        # behavior strips sidechain records, so without this flag
        # the watcher silently posts zero items — pin the real shape
        # here so a regression to that behavior fails this test.
        transcript_records=[
            {
                "isSidechain": True,
                "type": "user",
                "uuid": "sa-user-1",
                "message": {"role": "user", "content": "go"},
            },
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "sa-assistant-1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "looking now"}],
                },
            },
        ],
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )

    def response_for(body: dict[str, Any]) -> dict[str, Any]:
        """Mint a known child id for the start event.

        :param body: Decoded request body.
        :returns: Response payload.
        """
        if body.get("type") == "external_subagent_start":
            return {"queued": False, "child_session_id": "conv_child_beta"}
        return {}

    server, _thread, base_url = _start_recording_server_with_responses(response_for)
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_parent",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # We need: the start event + at least one item event addressed
        # to the child. Drain up to N requests and collect every
        # request bound for the child's ``/events`` path.
        child_path = "/v1/sessions/conv_child_beta/events"
        child_requests: list[dict[str, Any]] = []
        for _ in range(40):
            req = await _get_recorded_request(server)
            if req["path"] == child_path:
                child_requests.append(req)
                if len(child_requests) >= 2:
                    break
        assert len(child_requests) >= 2, (
            f"only saw {len(child_requests)} requests to {child_path}: {child_requests!r}"
        )
        item_types = [r["body"]["type"] for r in child_requests]
        assert "external_conversation_item" in item_types
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()


async def test_subagent_watcher_retry_skips_previously_posted_items(
    tmp_path: Path,
) -> None:
    """
    Retrying a failed child item does not re-post earlier child items.

    The sub-agent watcher intentionally leaves ``byte_offset`` behind
    when a later item fails, so the next poll re-reads the same JSONL
    window. This test pins the durable ``seen_source_ids`` guard: item
    A succeeds, item B fails once, and the retry must post only B.
    Without that guard Omnigent live subscribers can see item A synced back
    twice; the server no longer receives a ``source_id`` key that can
    dedupe the post on AP's side.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagent_jsonl = _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="retry1",
        agent_type="Explore",
        description="retry item flow",
        tool_use_id="toolu_retry",
        transcript_records=[
            {
                "isSidechain": True,
                "type": "user",
                "uuid": "sa-user-retry",
                "message": {"role": "user", "content": "go"},
            },
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "sa-assistant-retry",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                },
            },
        ],
    )
    state = forwarder.SubagentForwardState(
        subagents={
            "retry1": forwarder.SubagentEntry(
                subagent_id="retry1",
                child_conversation_id="conv_child_retry",
            )
        }
    )
    posted_items: list[str] = []
    attempts_by_item: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail the assistant item once and accept everything else.

        :param request: Request issued by the forwarder.
        :returns: Canned Omnigent response.
        """
        body = json.loads(request.content.decode("utf-8"))
        if body.get("type") != "external_conversation_item":
            return httpx.Response(202, json={})
        item_data = body["data"]["item_data"]
        role = item_data["role"]
        text = item_data["content"][0]["text"]
        item_key = f"{role}:{text}"
        posted_items.append(item_key)
        attempts_by_item[item_key] = attempts_by_item.get(item_key, 0) + 1
        if item_key == "assistant:done" and attempts_by_item[item_key] == 1:
            return httpx.Response(503, json={"error": "try again"})
        return httpx.Response(202, json={})

    item_retry_tracker = forwarder._PostRetryTracker(base_delay_s=0.0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ap",
    ) as client:
        first = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=item_retry_tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )
        second = await forwarder._forward_available_subagents(
            client=client,
            parent_session_id="conv_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=first,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=item_retry_tracker,
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert posted_items == ["user:go", "assistant:done", "assistant:done"]
    child_state = second.subagents["retry1"]
    assert child_state.byte_offset == subagent_jsonl.stat().st_size
    assert set(child_state.seen_source_ids) == {
        "sa-user-retry:0:message",
        "sa-assistant-retry:0:message",
    }


async def test_subagent_watcher_skips_subagents_already_in_state(
    tmp_path: Path,
) -> None:
    """
    On forwarder restart, sub-agents already in
    ``subagent_forwarder.json`` are NOT re-registered (no second
    ``external_subagent_start`` POST). This is the idempotency
    contract the cursor file is for — without it, a forwarder
    crash-loop would mint a new child Conversation per restart.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="c0ldc4t",
        agent_type="Explore",
        description="post-restart sub-agent",
        tool_use_id="toolu_qqq",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    # Pre-seed the cursor as if a previous forwarder ran already.
    bridge_dir.mkdir(parents=True, exist_ok=True)
    (bridge_dir / "subagent_forwarder.json").write_text(
        json.dumps(
            {
                "subagents": {
                    "c0ldc4t": {
                        "child_conversation_id": "conv_child_existing",
                        "byte_offset": 0,
                        "last_activity_ts": None,
                        "last_status": None,
                    }
                },
                "updated_at": 0,
            }
        ),
        encoding="utf-8",
    )

    starts: list[dict[str, Any]] = []

    def response_for(body: dict[str, Any]) -> dict[str, Any]:
        """Capture any start events and fail the test loudly.

        :param body: Decoded request body.
        :returns: Response payload (unused, since we don't expect a
            start event in this scenario).
        """
        if body.get("type") == "external_subagent_start":
            starts.append(body)
            return {"queued": False, "child_session_id": "conv_unexpected"}
        return {}

    server, _thread, base_url = _start_recording_server_with_responses(response_for)
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_parent",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    # Let the forwarder run a few ticks. Long enough to scan the
    # subagents dir at least twice; if it would re-register, we'd
    # see the POST in ``starts`` within this window.
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()

    assert starts == [], (
        f"forwarder re-registered a sub-agent that was already in state: {starts!r}"
    )


async def test_subagent_watcher_preserves_parked_sentinel_across_restart(
    tmp_path: Path,
) -> None:
    """
    A sub-agent that exhausted its permanent-failure budget is "parked"
    by writing an empty ``child_conversation_id`` sentinel into the
    cursor. On restart we must round-trip that sentinel — otherwise the
    parked sub-agent silently disappears from state and the next tick
    retries it (defeating the failure-budget cap).
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    _seed_subagent_on_disk(
        transcript_path=transcript_path,
        subagent_id="parked-cat",
        agent_type="Explore",
        description="exhausted start retries last time",
        tool_use_id="toolu_parked",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    # Pre-seed the cursor with a parked entry — empty child id is the
    # sentinel ``_forward_available_subagents`` writes on exhaustion.
    bridge_dir.mkdir(parents=True, exist_ok=True)
    (bridge_dir / "subagent_forwarder.json").write_text(
        json.dumps(
            {
                "subagents": {
                    "parked-cat": {
                        "child_conversation_id": "",
                        "byte_offset": 0,
                        "last_activity_ts": None,
                        "last_status": None,
                    }
                },
                "updated_at": 0,
            }
        ),
        encoding="utf-8",
    )

    starts: list[dict[str, Any]] = []

    def response_for(body: dict[str, Any]) -> dict[str, Any]:
        """Record any start POSTs — none should arrive for the parked id.

        :param body: Decoded request body.
        :returns: Response payload.
        """
        if body.get("type") == "external_subagent_start":
            starts.append(body)
            return {"queued": False, "child_session_id": "conv_should_not_be_used"}
        return {}

    server, _thread, base_url = _start_recording_server_with_responses(response_for)
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_parent",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()

    assert starts == [], f"forwarder retried a parked sub-agent after restart: {starts!r}"


# ---------------------------------------------------------------------------
# In-pane /effort → Omnigent session reasoning_effort mirroring
# ---------------------------------------------------------------------------


@dataclass
class _CapturedRequest:
    """
    One request seen by the effort-sync mock transport.

    :param method: HTTP method, e.g. ``"PATCH"``.
    :param path: Request path, e.g. ``"/v1/sessions/conv_x"``.
    :param body: Parsed JSON body, or ``None`` when the request had no body.
    """

    method: str
    path: str
    body: dict[str, Any] | None


def _slash_command_item(*, name: str, arguments: str) -> ClaudeTranscriptItem:
    """
    Build a ``slash_command`` transcript item as the bridge emits it.

    :param name: Command name with the leading ``/`` already stripped,
        e.g. ``"effort"``.
    :param arguments: Verbatim ``<command-args>`` text, e.g. ``"max"``.
    :returns: A ``slash_command`` item shaped like
        :func:`_user_transcript_items_from_entry` produces.
    """
    return ClaudeTranscriptItem(
        source_id="rec01:0:slash_command",
        item_type="slash_command",
        data={"agent": "claude", "kind": "command", "name": name, "arguments": arguments},
        response_id="resp_1",
    )


async def _run_effort_sync(
    item: ClaudeTranscriptItem,
    *,
    status: int = 200,
) -> list[_CapturedRequest]:
    """
    Drive ``_maybe_sync_effort_from_slash_command`` against a mock AP.

    :param item: Transcript item to feed the helper.
    :param status: HTTP status the mock PATCH endpoint returns, e.g.
        ``503`` to exercise the best-effort swallow path.
    :returns: Every request the helper issued, in order.
    """
    captured: list[_CapturedRequest] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the request and return a canned PATCH response."""
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        captured.append(_CapturedRequest(method=request.method, path=request.url.path, body=body))
        return httpx.Response(status, json={"id": "conv_x"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        await forwarder._maybe_sync_effort_from_slash_command(
            client, session_id="conv_x", item=item
        )
    return captured


@pytest.mark.parametrize("level", sorted(CLAUDE_EFFORTS))
async def test_in_pane_effort_set_patches_session_silently(level: str) -> None:
    """``/effort <level>`` in the pane PATCHes reasoning_effort with silent=True."""
    captured = await _run_effort_sync(_slash_command_item(name="effort", arguments=level))

    assert captured == [
        _CapturedRequest(
            method="PATCH",
            path="/v1/sessions/conv_x",
            body={"reasoning_effort": level, "silent": True},
        )
    ], f"expected one silent reasoning_effort={level} PATCH, got {captured!r}"


@pytest.mark.parametrize("alias", sorted(EFFORT_CLEAR_VALUES))
async def test_in_pane_effort_clear_patches_clear_alias(alias: str) -> None:
    """``/effort default`` (and off/reset) forwards the clear alias verbatim."""
    captured = await _run_effort_sync(_slash_command_item(name="effort", arguments=alias))

    assert captured == [
        _CapturedRequest(
            method="PATCH",
            path="/v1/sessions/conv_x",
            body={"reasoning_effort": alias, "silent": True},
        )
    ], f"expected one silent clear PATCH for alias={alias}, got {captured!r}"


def _message_item() -> ClaudeTranscriptItem:
    """A plain user-message item (not a slash command)."""
    return ClaudeTranscriptItem(
        source_id="rec01:0:message",
        item_type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        response_id="resp_1",
    )


@pytest.mark.parametrize(
    "item",
    [
        pytest.param(_slash_command_item(name="effort", arguments="turbo"), id="unknown-level"),
        pytest.param(_slash_command_item(name="effort", arguments=""), id="no-arg-show"),
        pytest.param(_slash_command_item(name="model", arguments="opus"), id="non-effort-command"),
        pytest.param(_message_item(), id="plain-message"),
    ],
)
async def test_effort_sync_skips_non_effort_changes(item: ClaudeTranscriptItem) -> None:
    """Only a recognized ``/effort`` set/clear PATCHes; everything else no-ops."""
    captured = await _run_effort_sync(item)

    assert captured == [], f"expected no PATCH for this item, got {captured!r}"


async def test_effort_sync_swallows_patch_failure() -> None:
    """A failed PATCH is best-effort — attempted, logged, never raised."""
    captured = await _run_effort_sync(
        _slash_command_item(name="effort", arguments="max"), status=503
    )

    # Attempted exactly once and the 503 swallowed (no exception escaped the await).
    assert len(captured) == 1
    assert captured[0].method == "PATCH"


def test_usage_from_status_state_surfaces_cumulative_cost() -> None:
    """
    ``_usage_from_status_state`` surfaces ``total_cost_usd`` as
    ``cumulative_cost_usd`` so the forwarder posts it for native cost tracking.

    Failure means Claude Code's captured cost never reaches the server, so
    native ``session_usage.total_cost_usd`` stays 0.
    """
    state = {
        "context_window_size": 1_000_000,
        "current_usage": {"input_tokens": 6, "output_tokens": 50},
        "total_cost_usd": 0.42,
    }
    result = forwarder._usage_from_status_state(state)
    assert result is not None
    assert result["cumulative_cost_usd"] == 0.42
    # Token fields still flow for the context ring.
    assert result["input_tokens"] == 6
    assert result["output_tokens"] == 50


def test_usage_from_status_state_omits_cost_when_absent() -> None:
    """
    Without ``total_cost_usd`` in state, no ``cumulative_cost_usd`` is emitted.

    Older Claude Code versions (or a statusLine without a cost block) must not
    cause a bogus 0-cost post that would overwrite a real value with SET.
    """
    state = {
        "context_window_size": 1_000_000,
        "current_usage": {"input_tokens": 6, "output_tokens": 50},
    }
    result = forwarder._usage_from_status_state(state)
    assert result is not None
    assert "cumulative_cost_usd" not in result


@dataclass
class _CapturedDeltaPost:
    """
    One ``POST /events`` body captured during a delta-forwarding test.

    :param url_path: Request URL path, e.g. ``"/v1/sessions/conv_x/events"``.
    :param body: Parsed JSON request body.
    """

    url_path: str
    body: dict[str, Any]


def _write_deltas_file(bridge_dir: Path, records: list[dict[str, Any]]) -> None:
    """
    Append delta records to ``message_deltas.jsonl`` as the hook would.

    :param bridge_dir: Bridge directory.
    :param records: Delta dicts to serialize one-per-line, e.g.
        ``[{"message_id": "m1", "index": 0, "final": True, "delta": "hi"}]``.
    :returns: None.
    """
    bridge_dir.mkdir(parents=True, exist_ok=True)
    with (bridge_dir / "message_deltas.jsonl").open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _delta_capture_client(
    captured: list[_CapturedDeltaPost],
    status_code: int = 202,
) -> httpx.AsyncClient:
    """
    Build an AsyncClient whose ``/events`` POSTs are captured.

    :param captured: List appended to with each observed POST body.
    :param status_code: HTTP status the stub returns, e.g. ``202`` for
        success or ``500`` to exercise the best-effort drop path.
    :returns: An ``httpx.AsyncClient`` bound to the capturing transport.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            _CapturedDeltaPost(url_path=request.url.path, body=json.loads(request.content))
        )
        return httpx.Response(status_code, json={"queued": False})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ap")


async def test_forward_available_deltas_posts_each_and_advances_offset(tmp_path: Path) -> None:
    """
    Each appended chunk is POSTed as an ``external_output_text_delta``.

    Proves the forwarder turns deltas-file lines into the exact event
    shape the Omnigent route expects (delta + message_id + index + final) and
    advances+persists the byte offset so the next poll resumes after
    them. Fails if a field is dropped (UI can't scope/order the buffer)
    or the offset doesn't persist (chunks re-POST on restart).
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    _write_deltas_file(
        bridge_dir,
        [
            {"message_id": "m1", "index": 0, "final": False, "delta": "Hello "},
            {"message_id": "m1", "index": 1, "final": True, "delta": "world"},
        ],
    )
    captured: list[_CapturedDeltaPost] = []
    seen: dict[tuple[str, int], None] = {}
    async with _delta_capture_client(captured) as client:
        new_state = await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys=seen,
        )

    assert [c.url_path for c in captured] == [
        "/v1/sessions/conv_x/events",
        "/v1/sessions/conv_x/events",
    ]
    # Full event shape proves every field survived hook → file → POST.
    assert [c.body for c in captured] == [
        {
            "type": "external_output_text_delta",
            "data": {"delta": "Hello ", "message_id": "m1", "index": 0, "final": False},
        },
        {
            "type": "external_output_text_delta",
            "data": {"delta": "world", "message_id": "m1", "index": 1, "final": True},
        },
    ]
    # Offset advanced to EOF and was persisted, so a reload resumes past
    # the two chunks instead of re-POSTing them.
    assert new_state.byte_offset == os.path.getsize(bridge_dir / "message_deltas.jsonl")
    assert forwarder._read_delta_forward_state(bridge_dir).byte_offset == new_state.byte_offset


async def test_forward_available_deltas_dedupes_by_message_id_and_index(tmp_path: Path) -> None:
    """
    A repeated ``(message_id, index)`` is POSTed at most once.

    The byte offset prevents re-reads on the happy path, but a file
    truncation/rewind can replay records; the in-memory seen-ring must
    still suppress the duplicate. Fails if the dedupe key is wrong (or
    absent), which would double-render a chunk in the live preview.
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    _write_deltas_file(
        bridge_dir,
        [
            {"message_id": "m1", "index": 0, "final": False, "delta": "dup"},
            {"message_id": "m1", "index": 0, "final": False, "delta": "dup"},
            {"message_id": "m1", "index": 1, "final": True, "delta": "next"},
        ],
    )
    captured: list[_CapturedDeltaPost] = []
    seen: dict[tuple[str, int], None] = {}
    async with _delta_capture_client(captured) as client:
        await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys=seen,
        )
    # The duplicate (m1, 0) is collapsed: only the first (m1,0) and the
    # distinct (m1,1) are POSTed — 2 requests, not 3.
    assert [(c.body["data"]["message_id"], c.body["data"]["index"]) for c in captured] == [
        ("m1", 0),
        ("m1", 1),
    ]


async def test_forward_available_deltas_drops_on_http_error(tmp_path: Path) -> None:
    """
    A failed delta POST is swallowed and the offset still advances.

    Deltas are an ephemeral preview; the authoritative final message
    arrives via ``external_conversation_item`` regardless, so a transient
    Omnigent blip must not raise or wedge the tail. Fails if the error
    propagates (would crash the forwarder loop) or the offset stalls
    (would re-POST the failed chunk forever).
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    _write_deltas_file(
        bridge_dir, [{"message_id": "m1", "index": 0, "final": True, "delta": "boom"}]
    )
    captured: list[_CapturedDeltaPost] = []
    seen: dict[tuple[str, int], None] = {}
    async with _delta_capture_client(captured, status_code=500) as client:
        new_state = await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys=seen,
        )
    # The POST was attempted (and 500'd) but no exception escaped, and
    # the offset moved past the chunk so it won't be retried endlessly.
    assert len(captured) == 1
    assert new_state.byte_offset == os.path.getsize(bridge_dir / "message_deltas.jsonl")


def test_delta_forward_state_round_trips(tmp_path: Path) -> None:
    """
    The delta cursor persists and reloads its byte offset.

    Fails if the on-disk shape changes without the reader keeping up —
    a forwarder restart would then re-stream the whole deltas file.
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    # A fresh read with no state file starts at offset 0.
    assert forwarder._read_delta_forward_state(bridge_dir).byte_offset == 0
    forwarder._write_delta_forward_state(bridge_dir, forwarder.DeltaForwardState(byte_offset=512))
    assert forwarder._read_delta_forward_state(bridge_dir).byte_offset == 512


async def test_post_external_output_text_delta_sends_expected_payload(tmp_path: Path) -> None:
    """
    The single-delta POST helper sends the canonical event body.

    Guards the wire contract between the forwarder and the AP
    ``/events`` route in isolation from the file-tailing logic.
    """
    captured: list[_CapturedDeltaPost] = []
    async with _delta_capture_client(captured) as client:
        await forwarder._post_external_output_text_delta(
            client,
            session_id="conv_y",
            delta=ClaudeMessageDelta(message_id="m9", index=4, final=True, delta="tok"),
        )
    assert captured == [
        _CapturedDeltaPost(
            url_path="/v1/sessions/conv_y/events",
            body={
                "type": "external_output_text_delta",
                "data": {"delta": "tok", "message_id": "m9", "index": 4, "final": True},
            },
        )
    ]


# ── deltas-before-done ordering (assistant item hold-back) ────────────


def _write_assistant_transcript(path: Path, uuid: str, text: str) -> None:
    """
    Append one assistant text record to a Claude transcript JSONL file.

    :param path: Transcript file path.
    :param uuid: Record uuid, e.g. ``"u1"``.
    :param text: Assistant text block content, e.g. ``"Hello world"``.
    :returns: None.
    """
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": uuid,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                }
            )
            + "\n"
        )


def _transcript_state_for(transcript_path: Path) -> forwarder.TranscriptForwardState:
    """
    Build a fresh transcript cursor state for ``transcript_path``.

    :param transcript_path: Transcript file the state points at.
    :returns: A zero-cursor :class:`TranscriptForwardState`.
    """
    return forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )


@pytest.mark.asyncio
async def test_assistant_item_held_until_its_deltas_forward(tmp_path: Path) -> None:
    """
    An assistant item whose deltas haven't fully forwarded is deferred.

    Drives the real commit-before-delta race: with only a non-final chunk
    forwarded the item is held (no POST, cursor unadvanced); once the final
    chunk forwards and the joined text byte-equals the item's, it posts
    AFTER the deltas. Posting first would dupe (committed text + a late
    ``live:`` preview from the trailing chunks).
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    transcript_path = tmp_path / "session.jsonl"
    _write_assistant_transcript(transcript_path, "u1", "Hello world")
    # Only the first chunk has been written by the hook so far.
    _write_deltas_file(
        bridge_dir, [{"message_id": "m1", "index": 0, "final": False, "delta": "Hello "}]
    )

    ordering = forwarder._DeltaOrderingState()
    seen_deltas: dict[tuple[str, int], None] = {}
    captured: list[_CapturedDeltaPost] = []
    async with _delta_capture_client(captured) as client:
        delta_state = await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys=seen_deltas,
            ordering=ordering,
        )
        item_state = await forwarder._forward_available_items(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=_transcript_state_for(transcript_path),
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=forwarder._ForwardDedupeState(),
            ordering=ordering,
        )
        # Held: the item was NOT posted and the durable cursor did not
        # advance past it, so the next poll re-reads it. (The turn-start
        # ``external_session_status: running`` edge is filtered out here — this
        # test is about delta-vs-item ordering, not the status edge.)
        assert [
            c.body["type"] for c in captured if c.body["type"] != "external_session_status"
        ] == ["external_output_text_delta"]
        assert item_state.byte_offset == 0
        assert item_state.seen_source_ids == ()

        # Next poll: the hook's final chunk lands, completing the text.
        _write_deltas_file(
            bridge_dir, [{"message_id": "m1", "index": 1, "final": True, "delta": "world"}]
        )
        await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=delta_state,
            seen_keys=seen_deltas,
            ordering=ordering,
        )
        item_state = await forwarder._forward_available_items(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=item_state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=forwarder._ForwardDedupeState(),
            ordering=ordering,
        )

    # The item posted AFTER both of its chunks — the ordering every
    # downstream suppression layer assumes. Content asserted (not just
    # counts) to prove the matched item is the right one.
    item_posts = [c.body for c in captured if c.body["type"] == "external_conversation_item"]
    assert len(item_posts) == 1
    assert item_posts[0]["data"]["item_data"]["content"] == [
        {"type": "output_text", "text": "Hello world"}
    ]
    assert [c.body["type"] for c in captured if c.body["type"] != "external_session_status"][
        :2
    ] == [
        "external_output_text_delta",
        "external_output_text_delta",
    ]
    assert item_state.byte_offset == transcript_path.stat().st_size
    # The matched stream was consumed: a later identical-text message
    # must match its own deltas, not this stale entry.
    assert ordering.texts == {}


@pytest.mark.asyncio
async def test_assistant_item_posts_after_hold_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An item whose deltas never arrive posts once the hold timeout expires.

    Deltas are best-effort (dropped chunks, multi-block messages that never
    byte-match), so the hold must be bounded or such items would never
    persist. Past ``_ASSISTANT_ITEM_DELTA_HOLD_S`` it posts with no match —
    safe, since no forwarded deltas means no live preview to duplicate.
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    transcript_path = tmp_path / "session.jsonl"
    _write_assistant_transcript(transcript_path, "u1", "Hello world")
    # Deltas file exists (hook active) but carries an UNRELATED stream,
    # so the item can never match by text.
    _write_deltas_file(
        bridge_dir, [{"message_id": "m9", "index": 0, "final": True, "delta": "other"}]
    )
    clock = {"now": 100.0}
    monkeypatch.setattr(forwarder, "_hold_monotonic", lambda: clock["now"])

    ordering = forwarder._DeltaOrderingState()
    captured: list[_CapturedDeltaPost] = []
    # Share one dedupe across polls (as the real loop does) so the turn-start
    # ``running`` status fires once, not per call.
    dedupe = forwarder._ForwardDedupeState()
    async with _delta_capture_client(captured) as client:
        await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys={},
            ordering=ordering,
        )
        state = await forwarder._forward_available_items(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=_transcript_state_for(transcript_path),
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
            ordering=ordering,
        )
        # Status edges (the turn-start ``running``) filtered out — this test
        # is about the delta-then-held-item ordering.
        assert [
            c.body["type"] for c in captured if c.body["type"] != "external_session_status"
        ] == ["external_output_text_delta"]
        assert state.byte_offset == 0  # held

        clock["now"] = 100.0 + forwarder._ASSISTANT_ITEM_DELTA_HOLD_S
        state = await forwarder._forward_available_items(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
            ordering=ordering,
        )

    item_posts = [c.body for c in captured if c.body["type"] == "external_conversation_item"]
    assert len(item_posts) == 1
    assert item_posts[0]["data"]["item_data"]["content"] == [
        {"type": "output_text", "text": "Hello world"}
    ]
    assert state.byte_offset == transcript_path.stat().st_size


@pytest.mark.asyncio
async def test_assistant_item_not_held_without_deltas_file(tmp_path: Path) -> None:
    """
    A session whose MessageDisplay hook never fired is never held.

    No deltas file means no live preview, hence no duplicate — holding
    would only add latency. The item posts on the first poll.
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    transcript_path = tmp_path / "session.jsonl"
    _write_assistant_transcript(transcript_path, "u1", "Hello world")

    captured: list[_CapturedDeltaPost] = []
    async with _delta_capture_client(captured) as client:
        state = await forwarder._forward_available_items(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=_transcript_state_for(transcript_path),
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=forwarder._ForwardDedupeState(),
            ordering=forwarder._DeltaOrderingState(),
        )

    item_posts = [c.body for c in captured if c.body["type"] == "external_conversation_item"]
    assert len(item_posts) == 1
    assert state.byte_offset == transcript_path.stat().st_size


@pytest.mark.asyncio
async def test_assistant_item_stays_held_until_true_final_chunk(tmp_path: Path) -> None:
    """
    The commit stays held while a NON-final chunk lands after it.

    Any chunk, not just the final one, can land after the commit (the
    observed ``D D C D`` race). The hold must wait for the ``final`` chunk
    to byte-match, NOT release on "another delta arrived" — else the late
    non-final chunk builds a second ``live:`` preview after the commit.
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    transcript_path = tmp_path / "session.jsonl"
    _write_assistant_transcript(transcript_path, "u1", "Hello big world")

    ordering = forwarder._DeltaOrderingState()
    seen: dict[tuple[str, int], None] = {}
    delta_state = forwarder.DeltaForwardState()
    item_state = _transcript_state_for(transcript_path)
    captured: list[_CapturedDeltaPost] = []
    # Share one dedupe across polls (as the real loop does) so the turn-start
    # ``running`` status fires once, not per poll.
    dedupe = forwarder._ForwardDedupeState()

    async def _poll(client: httpx.AsyncClient) -> None:
        nonlocal delta_state, item_state
        delta_state = await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=delta_state,
            seen_keys=seen,
            ordering=ordering,
        )
        item_state = await forwarder._forward_available_items(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=item_state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
            ordering=ordering,
        )

    async with _delta_capture_client(captured) as client:
        # Poll 1: only the first (non-final) chunk; the commit is ready.
        _write_deltas_file(
            bridge_dir, [{"message_id": "m1", "index": 0, "final": False, "delta": "Hello "}]
        )
        await _poll(client)
        assert item_state.byte_offset == 0  # held — no final chunk yet

        # Poll 2: a SECOND non-final chunk lands AFTER the commit — still held.
        _write_deltas_file(
            bridge_dir, [{"message_id": "m1", "index": 1, "final": False, "delta": "big "}]
        )
        await _poll(client)
        assert item_state.byte_offset == 0  # STILL held: stream not final
        assert not [c for c in captured if c.body["type"] == "external_conversation_item"]

        # Poll 3: the true final chunk lands → byte-matches → released.
        _write_deltas_file(
            bridge_dir, [{"message_id": "m1", "index": 2, "final": True, "delta": "world"}]
        )
        await _poll(client)

    # Commit posts only AFTER all three deltas — the order downstream assumes.
    # The turn-start ``running`` status edge is filtered out (it fires once,
    # before the deltas); this test is about delta-then-commit ordering.
    assert [
        c.body["type"] for c in captured if c.body["type"] != "external_session_status"
    ] == [
        "external_output_text_delta",
        "external_output_text_delta",
        "external_output_text_delta",
        "external_conversation_item",
    ]
    item = next(c.body for c in captured if c.body["type"] == "external_conversation_item")
    assert item["data"]["item_data"]["content"] == [
        {"type": "output_text", "text": "Hello big world"}
    ]
    assert item_state.byte_offset == transcript_path.stat().st_size


@pytest.mark.asyncio
async def test_assistant_item_held_when_final_seen_but_chunk_missing(tmp_path: Path) -> None:
    """
    Seeing the ``final`` chunk is not enough — the join must byte-equal.

    A dropped middle chunk leaves the joined text != commit text, so the
    item stays held despite ``final`` being seen. This is why the release
    gate requires BOTH ``entry.final`` and the byte-equal check.
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    transcript_path = tmp_path / "session.jsonl"
    _write_assistant_transcript(transcript_path, "u1", "Hello big world")
    # Forward index 0 and the FINAL index 2 — but NOT the middle index 1.
    _write_deltas_file(
        bridge_dir,
        [
            {"message_id": "m1", "index": 0, "final": False, "delta": "Hello "},
            {"message_id": "m1", "index": 2, "final": True, "delta": "world"},
        ],
    )

    ordering = forwarder._DeltaOrderingState()
    captured: list[_CapturedDeltaPost] = []
    async with _delta_capture_client(captured) as client:
        await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys={},
            ordering=ordering,
        )
        item_state = await forwarder._forward_available_items(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=_transcript_state_for(transcript_path),
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=forwarder._ForwardDedupeState(),
            ordering=ordering,
        )

    # final WAS seen, but join "Hello world" != commit "Hello big world".
    assert ordering.texts["m1"].final is True
    assert "".join(ordering.texts["m1"].parts) == "Hello world"
    assert item_state.byte_offset == 0  # held despite the final flag
    assert not [c for c in captured if c.body["type"] == "external_conversation_item"]


@pytest.mark.asyncio
async def test_two_identical_text_items_each_match_own_stream(tmp_path: Path) -> None:
    """
    Two assistant messages with identical text are matched by count.

    Consume-once: the first commit pops one stream, the second pops the
    other — both post, ordering ends empty. Identical text renders
    identically, so which physical stream a commit consumes doesn't matter.
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    transcript_path = tmp_path / "session.jsonl"
    _write_assistant_transcript(transcript_path, "u1", "OK")
    _write_assistant_transcript(transcript_path, "u2", "OK")
    _write_deltas_file(
        bridge_dir,
        [
            {"message_id": "mA", "index": 0, "final": True, "delta": "OK"},
            {"message_id": "mB", "index": 0, "final": True, "delta": "OK"},
        ],
    )

    ordering = forwarder._DeltaOrderingState()
    captured: list[_CapturedDeltaPost] = []
    async with _delta_capture_client(captured) as client:
        await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=forwarder.DeltaForwardState(),
            seen_keys={},
            ordering=ordering,
        )
        item_state = await forwarder._forward_available_items(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=_transcript_state_for(transcript_path),
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=forwarder._ForwardDedupeState(),
            ordering=ordering,
        )

    item_posts = [c.body for c in captured if c.body["type"] == "external_conversation_item"]
    assert len(item_posts) == 2  # both released, neither blocked
    assert all(
        p["data"]["item_data"]["content"] == [{"type": "output_text", "text": "OK"}]
        for p in item_posts
    )
    assert ordering.texts == {}  # both streams consumed (consume-once)
    assert item_state.byte_offset == transcript_path.stat().st_size


@pytest.mark.asyncio
async def test_without_hold_commit_posts_before_final_delta(tmp_path: Path) -> None:
    """
    Break-the-feature guard: with the hold disabled the bug reproduces.

    ``ordering=None`` (pre-fix behaviour): the commit posts immediately,
    BEFORE the final delta — the exact order that dupes the ``live:``
    preview. Paired with the hold-on test, this pins the hold as the fix.
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b1", workspace=tmp_path)
    transcript_path = tmp_path / "session.jsonl"
    _write_assistant_transcript(transcript_path, "u1", "Hello world")
    _write_deltas_file(
        bridge_dir, [{"message_id": "m1", "index": 0, "final": False, "delta": "Hello "}]
    )

    seen: dict[tuple[str, int], None] = {}
    delta_state = forwarder.DeltaForwardState()
    captured: list[_CapturedDeltaPost] = []
    async with _delta_capture_client(captured) as client:
        delta_state = await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=delta_state,
            seen_keys=seen,
            ordering=None,
        )
        await forwarder._forward_available_items(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=_transcript_state_for(transcript_path),
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=forwarder._ForwardDedupeState(),
            ordering=None,
        )
        # Bug: with no hold the commit posts immediately, before the final
        # chunk. The turn-start ``running`` status edge is filtered out.
        assert [
            c.body["type"] for c in captured if c.body["type"] != "external_session_status"
        ] == [
            "external_output_text_delta",
            "external_conversation_item",
        ]
        _write_deltas_file(
            bridge_dir, [{"message_id": "m1", "index": 1, "final": True, "delta": "world"}]
        )
        await forwarder._forward_available_deltas(
            client=client,
            session_id="conv_x",
            bridge_dir=bridge_dir,
            state=delta_state,
            seen_keys=seen,
            ordering=None,
        )

    # The final delta lands AFTER the commit — the inverted order that dupes.
    types = [c.body["type"] for c in captured if c.body["type"] != "external_session_status"]
    commit_idx = types.index("external_conversation_item")
    final_delta_idx = max(i for i, t in enumerate(types) if t == "external_output_text_delta")
    assert commit_idx < final_delta_idx


# ── session cost reconciliation (max(S, C)) ───────────────────────────


@pytest.mark.parametrize(
    "state,expected",
    [
        ({"total_cost_usd": 0.5}, 0.5),
        ({"total_cost_usd": 0}, 0.0),
        ({"total_cost_usd": -1.0}, None),  # negative rejected
        ({"total_cost_usd": True}, None),  # bool rejected (not a real cost)
        ({"total_cost_usd": "x"}, None),  # non-numeric rejected
        ({}, None),  # absent
        (None, None),  # no statusLine yet
    ],
)
def test_cumulative_cost_from_status_state(
    state: dict[str, Any] | None, expected: float | None
) -> None:
    """Only a non-negative numeric ``total_cost_usd`` yields a cost."""
    assert forwarder._cumulative_cost_from_status_state(state) == expected


def test_transcript_cost_size_cached_recomputes_only_on_growth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    The cost is recomputed only when the transcript's byte size changes.

    Guards the per-poll optimization: an unchanged transcript must not be
    re-parsed every 0.25s tick, but a grown one must be re-priced.
    """
    calls: list[Path] = []

    def fake_compute(path: Path, *, include_sidechains: bool) -> float | None:
        calls.append(path)
        return float(path.stat().st_size)

    monkeypatch.setattr(forwarder, "compute_transcript_cumulative_cost", fake_compute)
    cache: dict[Path, forwarder._TranscriptCostCacheEntry] = {}
    path = tmp_path / "t.jsonl"
    path.write_text("abc", encoding="utf-8")  # 3 bytes
    assert forwarder._transcript_cost_size_cached(
        path, include_sidechains=True, cache=cache
    ) == pytest.approx(3.0)
    # Second call at the same size → served from cache, no recompute.
    assert forwarder._transcript_cost_size_cached(
        path, include_sidechains=True, cache=cache
    ) == pytest.approx(3.0)
    assert len(calls) == 1
    # File grows → recompute.
    path.write_text("abcdef", encoding="utf-8")  # 6 bytes
    assert forwarder._transcript_cost_size_cached(
        path, include_sidechains=True, cache=cache
    ) == pytest.approx(6.0)
    assert len(calls) == 2
    # Missing file → None, no recompute attempt recorded as a priced call.
    assert (
        forwarder._transcript_cost_size_cached(
            tmp_path / "missing.jsonl", include_sidechains=True, cache=cache
        )
        is None
    )


def test_session_cost_estimate_takes_max_of_status_and_transcript_sum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    ``C`` sums parent + sub-agent transcript cost; the result is max(S, C).

    During a sub-agent run the real-time transcript sum (C) exceeds the
    lagging statusLine total (S), so C is used — this is what lets the
    parent budget see the sub-agent's spend mid-turn. Once S settles
    higher than C, S is used.
    """
    parent = tmp_path / "sess.jsonl"
    parent.write_text("parent", encoding="utf-8")
    subagents_dir = forwarder._subagents_dir_for_transcript(parent)
    subagents_dir.mkdir(parents=True)
    sub_path = subagents_dir / "agent-aaa.jsonl"
    sub_path.write_text("sub", encoding="utf-8")

    per_path_cost = {parent: 0.10, sub_path: 0.55}

    def fake_compute(path: Path, *, include_sidechains: bool) -> float | None:
        return per_path_cost.get(path)

    monkeypatch.setattr(forwarder, "compute_transcript_cumulative_cost", fake_compute)
    entries = [forwarder.SubagentEntry(subagent_id="aaa", child_conversation_id="conv_child")]

    # S stale ($0.005) < C (0.10 + 0.55 = 0.65) → C wins (mid-run).
    assert forwarder._session_cost_estimate(
        parent_transcript_path=parent,
        active_subagents=entries,
        status_cost=0.005,
        cost_cache={},
    ) == pytest.approx(0.65)

    # S settled ($2.00) > C → S wins (no double-count after settle).
    assert forwarder._session_cost_estimate(
        parent_transcript_path=parent,
        active_subagents=entries,
        status_cost=2.0,
        cost_cache={},
    ) == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_forward_session_cost_splits_display_and_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    ``_forward_session_cost`` posts S for display and max(S, C) for policy.

    With a sub-agent present the two fields advance independently and
    monotonically:

    - ``cumulative_cost_usd`` (display) = the statusLine total S verbatim,
      so the badge matches ``/cost``. It stays frozen while S is frozen,
      then jumps when the turn settles.
    - ``policy_cost_usd`` (enforcement) = max(S, transcript estimate C),
      so the gate sees in-flight sub-agent spend while S is frozen.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    parent = tmp_path / "sess.jsonl"
    parent.write_text("parent", encoding="utf-8")

    # statusLine total (S) and transcript estimate (C) are both stubbed so
    # the test can drive them independently across polls.
    status_box = {"value": 0.01}
    monkeypatch.setattr(
        forwarder,
        "read_claude_context_state",
        lambda _bridge: {"total_cost_usd": status_box["value"]},
    )
    estimate_box = {"value": 0.65}
    monkeypatch.setattr(
        forwarder,
        "_session_cost_estimate",
        lambda **_kwargs: estimate_box["value"],
    )
    subagent_state = forwarder.SubagentForwardState(
        subagents={
            "aaa": forwarder.SubagentEntry(subagent_id="aaa", child_conversation_id="conv_child")
        }
    )
    dedupe = forwarder._ForwardDedupeState()

    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") == "external_session_usage":
            posted.append(body["data"])
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:

        async def run() -> None:
            await forwarder._forward_session_cost(
                client=client,
                session_id="conv_parent",
                bridge_dir=bridge_dir,
                parent_transcript_path=parent,
                subagent_state=subagent_state,
                dedupe=dedupe,
                cost_cache={},
            )

        # First poll: display = S (0.01) verbatim, policy = max(0.01, 0.65).
        # If display showed 0.65 here, the badge would diverge from /cost —
        # the exact bug this split fixes.
        await run()
        assert posted == [
            {
                "cumulative_cost_usd": pytest.approx(0.01),
                "policy_cost_usd": pytest.approx(0.65),
            }
        ]
        assert dedupe.posted_cost == pytest.approx(0.01)
        assert dedupe.posted_policy_cost == pytest.approx(0.65)

        # Nothing changed → neither field re-posts. A 2nd post would mean a
        # dedupe baseline wasn't honored.
        await run()
        assert len(posted) == 1

        # A lower transcript read must NOT walk policy back, and S is
        # unchanged → no post at all (both fields monotonic).
        estimate_box["value"] = 0.40
        await run()
        assert len(posted) == 1

        # C advances while S stays frozen (sub-agent still running): only
        # policy_cost_usd re-posts. Proves the badge (S) stays put mid-turn
        # while the gate sees the rising in-flight cost.
        estimate_box["value"] = 0.90
        await run()
        assert posted[-1] == {"policy_cost_usd": pytest.approx(0.90)}
        assert dedupe.posted_policy_cost == pytest.approx(0.90)
        # Display baseline untouched — S never advanced.
        assert dedupe.posted_cost == pytest.approx(0.01)

        # Turn settles: S jumps to the sub-agent-inclusive total. Display
        # advances; policy advances to the same settled value. Both post.
        status_box["value"] = 0.95
        estimate_box["value"] = 0.95
        await run()
        assert posted[-1] == {
            "cumulative_cost_usd": pytest.approx(0.95),
            "policy_cost_usd": pytest.approx(0.95),
        }
        assert dedupe.posted_cost == pytest.approx(0.95)
        assert dedupe.posted_policy_cost == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_forward_session_cost_posts_status_when_no_subagents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    With no sub-agents, display and policy are both the statusLine total.

    There is no statusLine lag to correct without a sub-agent, so the
    transcript estimator must not run and both ``cumulative_cost_usd``
    (display) and ``policy_cost_usd`` (enforcement) equal S — they only
    diverge while a sub-agent is mid-run.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    parent = tmp_path / "sess.jsonl"
    parent.write_text("parent", encoding="utf-8")
    monkeypatch.setattr(
        forwarder, "read_claude_context_state", lambda _bridge: {"total_cost_usd": 0.25}
    )

    def _fail_estimate(**_kwargs: Any) -> float | None:
        raise AssertionError("estimator must not run without sub-agents")

    monkeypatch.setattr(forwarder, "_session_cost_estimate", _fail_estimate)
    dedupe = forwarder._ForwardDedupeState()
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") == "external_session_usage":
            posted.append(body["data"])
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        await forwarder._forward_session_cost(
            client=client,
            session_id="conv_parent",
            bridge_dir=bridge_dir,
            parent_transcript_path=parent,
            subagent_state=forwarder.SubagentForwardState(subagents={}),
            dedupe=dedupe,
            cost_cache={},
        )
    # Both fields = S (0.25). policy_cost_usd present so the gate has a value
    # without a sub-agent too; if it were missing, the engine would fall back
    # to total_cost_usd (also S) — but the forwarder posts it explicitly.
    assert posted == [
        {
            "cumulative_cost_usd": pytest.approx(0.25),
            "policy_cost_usd": pytest.approx(0.25),
        }
    ]
    assert dedupe.posted_cost == pytest.approx(0.25)
    assert dedupe.posted_policy_cost == pytest.approx(0.25)


def test_parse_json_response_returns_value_on_valid_json() -> None:
    """
    A normal JSON body parses through ``_parse_json_response`` unchanged.

    :returns: None.
    """
    resp = httpx.Response(200, json={"id": "conv_abc123"})
    assert forwarder._parse_json_response(resp, context="session snapshot") == {
        "id": "conv_abc123"
    }


def test_parse_json_response_raises_diagnosable_error_on_html_body() -> None:
    """
    An HTML body (e.g. an expired Databricks Apps OAuth login page served
    with a 200) raises a ``RuntimeError`` naming the content type and a
    body snippet, not an opaque ``json.JSONDecodeError``. The original
    parser error is preserved as ``__cause__`` for debugging.

    :returns: None.
    """
    resp = httpx.Response(
        200,
        html="<!DOCTYPE html><html><body>Sign in to continue</body></html>",
    )
    with pytest.raises(RuntimeError) as excinfo:
        forwarder._parse_json_response(resp, context="session 'conv_abc123' snapshot")
    message = str(excinfo.value)
    assert "session 'conv_abc123' snapshot" in message
    assert "text/html" in message
    assert "<!DOCTYPE html>" in message
    assert isinstance(excinfo.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_fetch_session_snapshot_raises_diagnosable_error_on_html_body() -> None:
    """
    ``_fetch_session_snapshot`` surfaces a clear error when the Sessions
    API returns a 200 HTML body instead of JSON — the failure mode behind
    Claude Code's "Unrecognized token '<'" crash when an auth/proxy page is
    served in place of the API response. Without the guard this raised a
    bare ``json.JSONDecodeError`` that the forwarder supervisor turned into
    a silent restart loop.

    :returns: None.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            html="<!DOCTYPE html><html><body>Sign in to continue</body></html>",
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        with pytest.raises(RuntimeError) as excinfo:
            await forwarder._fetch_session_snapshot(client, "conv_abc123")
    message = str(excinfo.value)
    assert "conv_abc123" in message
    assert "text/html" in message


@pytest.mark.asyncio
async def test_forward_session_cost_tags_display_advance_with_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A display-cost (S) advance is tagged with the statusLine's active model.

    claude-native sends no token counts with its cost, so the server has
    nothing to attribute the cost to in the per-model TOKEN USAGE view without
    a ``model`` tag — it would drop the cost from that view. The forwarder
    rides the statusLine model (captured in context.json) on the payload
    whenever the display cost advances. A policy-only mid-turn re-post (S
    frozen, only the gate estimate C advancing) carries NO model: there is no
    new display cost to attribute, so tagging it would be meaningless churn.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    parent = tmp_path / "sess.jsonl"
    parent.write_text("parent", encoding="utf-8")

    status_box = {"value": 0.01}
    monkeypatch.setattr(
        forwarder,
        "read_claude_context_state",
        lambda _bridge: {"total_cost_usd": status_box["value"], "model": "claude-opus-4-8"},
    )
    estimate_box = {"value": 0.65}
    monkeypatch.setattr(
        forwarder,
        "_session_cost_estimate",
        lambda **_kwargs: estimate_box["value"],
    )
    subagent_state = forwarder.SubagentForwardState(
        subagents={
            "aaa": forwarder.SubagentEntry(subagent_id="aaa", child_conversation_id="conv_child")
        }
    )
    dedupe = forwarder._ForwardDedupeState()
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("type") == "external_session_usage":
            posted.append(body["data"])
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:

        async def run() -> None:
            await forwarder._forward_session_cost(
                client=client,
                session_id="conv_parent",
                bridge_dir=bridge_dir,
                parent_transcript_path=parent,
                subagent_state=subagent_state,
                dedupe=dedupe,
                cost_cache={},
            )

        # Display cost advances → the model rides along for per-model attribution.
        await run()
        assert posted == [
            {
                "cumulative_cost_usd": pytest.approx(0.01),
                "policy_cost_usd": pytest.approx(0.65),
                "model": "claude-opus-4-8",
            }
        ]

        # Mid-turn: S frozen, only C (policy) advances → policy-only re-post
        # carries NO model (no new display cost to attribute).
        estimate_box["value"] = 0.90
        await run()
        assert posted[-1] == {"policy_cost_usd": pytest.approx(0.90)}


# ---------------------------------------------------------------------------
# _persist_native_compaction_item tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_native_compaction_item_posts_compaction_event(tmp_path: Path) -> None:
    """
    ``_persist_native_compaction_item`` queries the latest item and posts a compaction event.

    The function GETs ``/v1/sessions/{id}/items?limit=1&order=desc`` to
    find the most recent persisted item, reads post-compaction messages
    from the Claude session, then POSTs a ``compaction`` event using
    that item's id as ``last_item_id`` and the messages as
    ``compacted_messages``.
    """
    get_response = MagicMock()
    get_response.raise_for_status = MagicMock()
    get_response.json.return_value = {"data": [{"id": "item_123"}]}

    post_response = MagicMock()
    post_response.raise_for_status = MagicMock()

    client = AsyncMock()
    client.get.return_value = get_response
    client.post.return_value = post_response

    # Build a fake message returned by get_session_messages.
    fake_msg = MagicMock()
    fake_msg.type = "assistant"
    fake_msg.message = {"content": [{"type": "text", "text": "hello"}]}

    bridge_dir = tmp_path / "bridge"

    with (
        patch(
            "omnigent.claude_native_forwarder.read_claude_session_id",
            return_value="claude-uuid-1",
        ),
        patch(
            "claude_agent_sdk.get_session_messages",
            return_value=[fake_msg],
        ),
    ):
        await _persist_native_compaction_item(
            client, session_id="conv_test", bridge_dir=bridge_dir
        )

    client.get.assert_called_once_with(
        "/v1/sessions/conv_test/items",
        params={"limit": 1, "order": "desc"},
    )
    client.post.assert_called_once()
    post_call = client.post.call_args
    assert post_call[0][0] == "/v1/sessions/conv_test/events"
    body = post_call[1]["json"] if "json" in post_call[1] else post_call[0][1]
    assert body["type"] == "compaction"
    assert body["data"]["last_item_id"] == "item_123"
    assert body["data"]["summary"] is not None
    assert body["data"]["model"] == "unknown"
    assert body["data"]["token_count"] == 0
    # compacted_messages should contain the converted fake message.
    assert body["data"]["compacted_messages"] == [
        {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]


@pytest.mark.asyncio
async def test_persist_native_compaction_item_empty_items_uses_fallback(tmp_path: Path) -> None:
    """
    When no items exist, ``last_item_id`` falls back to a generated boundary id.

    If the session has no persisted items yet (e.g. the very first turn
    was compacted before anything was stored), the function generates
    ``compact_boundary_{session_id}`` as the boundary marker instead of
    crashing on an empty list.
    """
    get_response = MagicMock()
    get_response.raise_for_status = MagicMock()
    get_response.json.return_value = {"data": []}

    post_response = MagicMock()
    post_response.raise_for_status = MagicMock()

    client = AsyncMock()
    client.get.return_value = get_response
    client.post.return_value = post_response

    bridge_dir = tmp_path / "bridge"

    with (
        patch(
            "omnigent.claude_native_forwarder.read_claude_session_id",
            return_value=None,
        ),
    ):
        await _persist_native_compaction_item(
            client, session_id="conv_empty", bridge_dir=bridge_dir
        )

    post_call = client.post.call_args
    body = post_call[1]["json"] if "json" in post_call[1] else post_call[0][1]
    assert body["data"]["last_item_id"].startswith("compact_boundary_")
    # No compacted_messages when claude_sid is None.
    assert "compacted_messages" not in body["data"]


@pytest.mark.asyncio
async def test_compaction_completed_triggers_persist(tmp_path: Path) -> None:
    """
    ``SessionStart source=compact`` triggers both status POST and item persistence.

    When the forwarder processes a ``SessionStart source=compact`` record
    (compaction completed), it must call ``_post_external_compaction_status``
    to surface the status AND ``_persist_native_compaction_item`` to write
    the compaction boundary item.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    # Initial SessionStart populates transcript_path.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    # Post-compaction SessionStart — the completion signal.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "source": "compact",
            "session_id": "claude-session",
        },
    )
    server, thread, base_url = _start_recording_server()
    persist_called = asyncio.Event()

    async def _persist_side_effect(*args: Any, **kwargs: Any) -> None:
        persist_called.set()

    persist_mock = AsyncMock(side_effect=_persist_side_effect)
    with patch(
        "omnigent.claude_native_forwarder._persist_native_compaction_item",
        persist_mock,
    ):
        task = asyncio.create_task(
            forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id="conv_persist",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.01,
            )
        )
        try:
            # Wait for the compaction status POST to arrive.
            request = await _get_recorded_request(server)
            # Wait for _persist_native_compaction_item to be called
            # (it runs right after the POST in the same await chain).
            await asyncio.wait_for(persist_called.wait(), timeout=5.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)

    # The recording server captured the compaction status POST.
    assert request["body"]["type"] == "external_compaction_status"
    assert request["body"]["data"]["status"] == "completed"
    # _persist_native_compaction_item was called with the right session id.
    persist_mock.assert_called_once()
    call_kwargs = persist_mock.call_args
    assert call_kwargs[1]["session_id"] == "conv_persist"


@pytest.mark.asyncio
async def test_compaction_in_progress_does_not_persist(tmp_path: Path) -> None:
    """
    ``PreCompact`` (in_progress) does NOT call ``_persist_native_compaction_item``.

    Only compaction *completion* (``SessionStart source=compact``) writes
    the boundary item. ``PreCompact`` merely forwards the ``in_progress``
    status so the UI shows a spinner — there is no boundary to persist yet.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "PreCompact", "session_id": "claude-session"},
    )
    server, thread, base_url = _start_recording_server()
    persist_mock = AsyncMock()
    with patch(
        "omnigent.claude_native_forwarder._persist_native_compaction_item",
        persist_mock,
    ):
        task = asyncio.create_task(
            forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id="conv_no_persist",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.01,
            )
        )
        try:
            # Wait for the in_progress status POST to arrive.
            request = await _get_recorded_request(server)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)

    assert request["body"]["type"] == "external_compaction_status"
    assert request["body"]["data"]["status"] == "in_progress"
    # _persist_native_compaction_item must NOT be called for in_progress.
    persist_mock.assert_not_called()


@pytest.mark.asyncio
async def test_post_external_session_status_includes_and_omits_response_id() -> None:
    """
    ``_post_external_session_status`` attaches ``response_id`` only when given.

    The turn-bearing edges (native Claude's turn start/end) carry the response
    id so ap-web can drive the bubble's streaming lifecycle; the bare,
    turn-agnostic edges (e.g. the sub-agent quiescence badge) must keep posting
    a ``data`` object with no ``response_id`` key so nothing spuriously matches.
    """
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        await forwarder._post_external_session_status(
            client, session_id="conv_abc", status="running", response_id="resp_1"
        )
        await forwarder._post_external_session_status(
            client, session_id="conv_abc", status="idle"
        )

    assert bodies[0] == {
        "type": "external_session_status",
        "data": {"status": "running", "response_id": "resp_1"},
    }
    # Bare edge: no response_id key (not a null) so the server's optional
    # validation passes and the client never opens a streaming response.
    assert bodies[1] == {
        "type": "external_session_status",
        "data": {"status": "idle"},
    }


@pytest.mark.asyncio
async def test_forward_status_events_stamps_response_id_on_idle(tmp_path: Path) -> None:
    """
    A ``Stop`` → idle edge carries the turn's ``response_id`` when one is known.

    This is what closes the streaming ``activeResponse`` ap-web opened from the
    turn-start ``running`` edge, so the trailing tool card stops spinning.
    """
    bridge_dir = tmp_path / "bridge"
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "Stop", "session_id": "claude-session"},
    )
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        hook_state = await forwarder._ensure_hook_state(
            bridge_dir, start_at_end=False, session_id="conv_abc"
        )
        await forwarder._forward_available_status_events(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            state=hook_state,
            retry_tracker=forwarder._PostRetryTracker(),
            task_subjects={},
            task_statuses={},
            task_order=[],
            response_id="resp_turn_1",
        )

    assert bodies == [
        {
            "type": "external_session_status",
            "data": {"status": "idle", "response_id": "resp_turn_1"},
        }
    ]


@pytest.mark.asyncio
async def test_forwarder_emits_turn_start_running_with_response_id(tmp_path: Path) -> None:
    """
    The first assistant output of a turn publishes ``running`` + its response id.

    Native Claude's running/idle BADGE stays PTY-derived; this id-bearing
    ``running`` edge is the additional signal that lets ap-web open a streaming
    ``activeResponse`` for the turn, so the forwarded tool cards (which share
    the same response id) render LIVE rather than as static completed cards.
    The running edge's response id must equal the forwarded items' response id.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "user-1",
                        "message": {"role": "user", "content": "read TODO"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-tool-1",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_read_1",
                                    "name": "Read",
                                    "input": {"file_path": "TODO.md"},
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # First POST of the poll is the turn-start running status (it runs
        # before the items in _forward_available_items); the two items follow.
        running = await _get_recorded_request(server)
        item_a = await _get_recorded_request(server)
        item_b = await _get_recorded_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert running["body"]["type"] == "external_session_status"
    assert running["body"]["data"]["status"] == "running"
    running_rid = running["body"]["data"]["response_id"]
    assert isinstance(running_rid, str) and running_rid
    # The running edge's response id matches the ASSISTANT turn's forwarded
    # item (the function_call), so that bubble enters the streaming lifecycle
    # on the client. The user message carries its own distinct response id.
    function_call = next(
        body
        for body in (item_a, item_b)
        if body["body"]["type"] == "external_conversation_item"
        and body["body"]["data"]["item_type"] == "function_call"
    )
    assert function_call["body"]["data"]["response_id"] == running_rid
