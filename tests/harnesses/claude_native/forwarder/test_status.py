"""Status tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    record_hook_event,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _start_recording_server,
)


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
        forwarder.forward_claude_transcript_to_session(
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
@pytest.mark.parametrize("submit_log_fails", [False, True])
async def test_forwarder_posts_idle_on_stop_and_ignores_user_prompt_submit(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    submit_log_fails: bool,
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
    caplog.set_level("INFO", logger="omnigent.harnesses.claude_native.forwarder")
    from omnigent.harnesses.claude_native import forwarder

    original_info = forwarder._logger.info
    submit_log_attempts = 0

    def log_info(message: object, *args: object, **kwargs: Any) -> None:
        nonlocal submit_log_attempts
        if kwargs.get("extra", {}).get("event_name") == "claude_native_prompt_submit_hook":
            submit_log_attempts += 1
            if submit_log_fails:
                raise OSError("log destination unavailable")
        original_info(message, *args, **kwargs)

    monkeypatch.setattr(forwarder._logger, "info", log_info)
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
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "claude-session",
            "prompt": "private prompt",
        },
    )
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "UserPromptSubmit", "session_id": "subagent-session"},
    )
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "Stop", "session_id": "claude-session"},
    )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
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
    # The Stop hook carries its authoritative background-shell count (0 here,
    # no background tasks) so a finished shell clears the indicator.
    assert request["body"] == {
        "type": "external_session_status",
        "data": {"status": "idle", "background_task_count": 0, "turn_completed": True},
    }

    submitted = [
        r
        for r in caplog.records
        if getattr(r, "event_name", None) == "claude_native_prompt_submit_hook"
    ]
    assert submit_log_attempts == 1
    assert "private prompt" not in caplog.text
    if submit_log_fails:
        assert submitted == []
    else:
        assert len(submitted) == 1
        assert submitted[0].session_id == "conv_abc"
        assert submitted[0].attributes["claude_session_id"] == "claude-session"
        assert submitted[0].attributes["hook_cursor"] == 2
        assert "private prompt" not in str(submitted[0].attributes)


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
        forwarder.forward_claude_transcript_to_session(
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

    context = request["body"]["data"].pop("failure_context")
    assert context["detail_source"] == "missing"
    assert context["native_session_id"] == "claude-session"
    assert request["body"] == {
        "type": "external_session_status",
        "data": {"status": "failed"},
    }


@pytest.mark.parametrize(
    ("payload_fields", "expected_detail"),
    [
        (
            {"error": "server_error", "last_assistant_message": "API Error: 500 Overloaded"},
            "API Error: 500 Overloaded",
        ),
        (
            {"error": "rate_limit"},
            "Claude Code ended the turn with an API error (rate_limit).",
        ),
        (
            {
                "error": "server_error",
                "last_assistant_message": "I am waiting for a background task.",
            },
            "I am waiting for a background task.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_forwarder_attaches_stop_failure_reason_to_failed_edge(
    tmp_path: Path,
    payload_fields: dict[str, str],
    expected_detail: str,
) -> None:
    """
    The failed edge carries the hook's own error text, else its category.

    The transcript mirror can land the error after the edge or never, so
    without this the server reports the turn's last prose or no detail.
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
        {"hook_event_name": "StopFailure", "session_id": "claude-session", **payload_fields},
    )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
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

    # ``failure_detail``, not ``output``: wire output is labeled a Codex error.
    context = request["body"]["data"].pop("failure_context")
    assert context["native_error_category"] == payload_fields["error"]
    assert context["detail_source"] == (
        "hook_last_assistant_message"
        if "last_assistant_message" in payload_fields
        else "hook_error_category"
    )
    assert "native_api_error_message" not in context
    assert request["body"] == {
        "type": "external_session_status",
        "data": {"status": "failed", "failure_detail": expected_detail},
    }


@pytest.mark.asyncio
async def test_forwarder_posts_idle_with_count_when_stop_has_background_tasks(
    tmp_path: Path,
) -> None:
    """
    ``Stop`` with ``background_tasks`` posts ``idle`` plus the shell count.

    The turn really has ended, so the status is ``idle`` — the spinner stays
    lit off the count instead (``showsWorking`` is ``isWorking || tally > 0``).
    The count is the one thing Claude's status file cannot report: its
    ``shell`` literal is a boolean and the indicator renders a number, which
    is why this hook still posts at all.
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
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session",
            "background_tasks": [
                {
                    "id": "abc123",
                    "type": "shell",
                    "status": "running",
                    "description": "Wait for CI",
                    "command": "sleep 120",
                },
            ],
        },
    )
    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
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

    assert request["path"] == "/v1/sessions/conv_abc/events"
    assert request["body"] == {
        "type": "external_session_status",
        "data": {
            "status": "idle",
            "turn_completed": True,
            "background_task_count": 1,
            # Per-shell detail rides alongside the count so the UI can name the
            # running shells (see BackgroundTaskInfo / _normalize_background_task).
            "background_tasks": [
                {
                    "id": "abc123",
                    "type": "shell",
                    "status": "running",
                    "description": "Wait for CI",
                    "command": "sleep 120",
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_post_external_session_status_includes_and_omits_response_id() -> None:
    """
    ``post_external_session_status`` attaches ``response_id`` only when given.

    The turn-bearing edges (native Claude's turn start/end) carry the response
    id so ap-web can drive the bubble's streaming lifecycle; the bare,
    turn-agnostic edges must keep posting
    a ``data`` object with no ``response_id`` key so nothing spuriously matches.
    """
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        await forwarder.post_external_session_status(
            client, session_id="conv_abc", status="running", response_id="resp_1"
        )
        await forwarder.post_external_session_status(client, session_id="conv_abc", status="idle")

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
    dedupe = forwarder._ForwardDedupeState()
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
            dedupe=dedupe,
            task_subjects={},
            task_statuses={},
            task_order=[],
            response_id="resp_turn_1",
        )

    # The posted turn-end edge records the turn as a PENDING settle for
    # scheduled-wake detection (it activates once the transcript is quiet).
    assert dedupe.pending_settled_response_id == "resp_turn_1"
    assert dedupe.settled_response_id is None

    assert bodies == [
        {
            "type": "external_session_status",
            # The Stop→idle edge carries the turn's response id AND the
            # background-shell tally (0 here — no shells); the live-tool-card
            # and background-task features share this one status edge.
            "data": {
                "status": "idle",
                "turn_completed": True,
                "background_task_count": 0,
                "response_id": "resp_turn_1",
            },
        }
    ]


@pytest.mark.asyncio
async def test_forwarder_publishes_no_status_for_assistant_output(tmp_path: Path) -> None:
    """
    Assistant output forwards items and publishes NO session status.

    Claude's ``sessions/<pid>.json`` owns the running/idle badge. A status edge
    derived from the transcript can only fire once a poll has parsed assistant
    output, so on a short turn it lands *after* the file's ``idle`` and
    re-asserts ``running`` on a session that already finished — the user sees
    idle → running → idle. The items still carry their own ``response_id``.
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
        forwarder.forward_claude_transcript_to_session(
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
        # Both POSTs of the poll are items — no status edge precedes them.
        item_a = await _get_recorded_request(server)
        item_b = await _get_recorded_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # Neither POST is a status edge — the transcript path publishes none.
    assert [body["body"]["type"] for body in (item_a, item_b)] == [
        "external_conversation_item",
        "external_conversation_item",
    ]
    # The assistant turn's item still carries its own response id, which is
    # what groups its bubble and its tool cards on the client.
    function_call = next(
        body for body in (item_a, item_b) if body["body"]["data"]["item_type"] == "function_call"
    )
    rid = function_call["body"]["data"]["response_id"]
    assert isinstance(rid, str) and rid


@pytest.mark.asyncio
async def test_short_turn_poll_posts_items_without_a_status_edge(tmp_path: Path) -> None:
    """
    Regression: a short turn's poll must not re-assert ``running``.

    The status file reports the turn ending the moment Claude settles, but a
    transcript-derived edge can only fire once a poll has parsed assistant
    output — so it arrived *after* that ``idle`` and flipped the session back to
    ``running``, then ``Stop`` closed it again: the user saw
    idle → running → idle on every short turn.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "u1",
                        "message": {"role": "user", "content": "i'll keep testing"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Sounds good."}],
                        },
                    }
                ),
            ]
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
    posted: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """
        Record every forwarder POST body.

        :param request: Outbound HTTP request from the forwarder.
        :returns: HTTP 202 for the mock Omnigent endpoint.
        """
        posted.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=forwarder._ForwardDedupeState(),
        )

    assert [body["type"] for body in posted] == ["external_conversation_item"] * 2
    assert not [body for body in posted if body["type"] == "external_session_status"]


@pytest.mark.asyncio
async def test_forwarder_does_not_leave_running_open_for_slash_command_only_turn(
    tmp_path: Path,
) -> None:
    """
    A ``/model``-only turn must not leave an id-bearing ``running`` dangling.

    Surfaced CLI built-ins (``/model``, ``/effort``, ...) become a
    ``slash_command`` item that opens its OWN response id but produce no LLM
    turn — so no ``Stop`` hook ever fires to close it. The forwarder's
    turn-start edge still publishes ``running`` + that id, which opens a
    streaming ``activeResponse`` in the web UI. Because the web store
    suppresses the trailing bare (id-less) PTY ``idle`` while a response is
    streaming, nothing clears it: the composer's Stop button stays lit and
    the session looks busy even though the terminal is free.

    The invariant: a poll that forwards only a slash-command item (no
    assistant output) must either skip the id-bearing ``running`` edge or
    emit a matching ``idle``/``failed`` carrying the same id, so the turn's
    lifecycle closes.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "prior-assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Earlier reply."}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "slash-model",
                        "message": {
                            "role": "user",
                            "content": (
                                "<command-name>/model</command-name>\n"
                                "            <command-message>model</command-message>\n"
                                "            <command-args>opus</command-args>"
                            ),
                        },
                    }
                ),
            ]
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
        payload = json.loads(request.content.decode("utf-8"))
        assert isinstance(payload, dict)
        requests.append(payload)
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dedupe = forwarder._ForwardDedupeState()
        await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=retry_tracker,
            dedupe=dedupe,
        )

    statuses = [
        request["data"] for request in requests if request["type"] == "external_session_status"
    ]
    running_ids = {
        status.get("response_id")
        for status in statuses
        if status["status"] == "running" and status.get("response_id") is not None
    }
    closed_ids = {
        status.get("response_id") for status in statuses if status["status"] in ("idle", "failed")
    }
    # Any id-bearing ``running`` opened for the slash-command-only turn must
    # be closed within the same poll — otherwise the web UI is stuck busy
    # until the next real message. (No LLM turn means no later Stop hook.)
    dangling = running_ids - closed_ids
    assert not dangling, (
        "slash-command-only turn left an id-bearing running status open with "
        f"no matching idle/failed: {dangling}"
    )
    # Stronger: the forwarder opens NO id-bearing running for this turn at all
    # (there is no assistant output to render live, so nothing to stream).
    assert running_ids == set()
    # The slash_command item itself still forwards — the switch stays visible
    # in the web transcript; only the phantom ``running`` edge is suppressed.
    forwarded = [
        request["data"] for request in requests if request["type"] == "external_conversation_item"
    ]
    assert any(item["item_type"] == "slash_command" for item in forwarded)
