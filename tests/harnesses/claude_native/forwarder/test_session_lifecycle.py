"""Session lifecycle tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    BRIDGE_ID_LABEL_KEY,
    prepare_bridge_dir,
    read_active_session_id,
    record_hook_event,
    write_active_session_id,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _start_recording_server,
)


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
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path / "root")
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
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path / "root")
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
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path / "root")
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
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path / "root")
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
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path / "root")
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
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge.time.time", lambda: 1779922393.245
    )
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
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path / "root")
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
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path / "root")
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
        forwarder.forward_claude_transcript_to_session(
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
        forwarder.forward_claude_transcript_to_session(
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
