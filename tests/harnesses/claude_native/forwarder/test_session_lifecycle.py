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
from omnigent.native._native_supersession import post_clear_supersession
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _start_recording_server,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project_available", "checkpoint_crash"),
    [(True, False), (False, False), (True, True)],
    ids=["project-available", "project-not-found", "checkpoint-crash"],
)
async def test_clear_hook_rotates_active_session_without_reprocessing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    project_available: bool,
    checkpoint_crash: bool,
) -> None:
    """
    Claude ``/clear`` creates a fresh Omnigent session and consumes the hook.

    This exercises the rotation transaction directly: create the new
    session with the old title and project, bind the same runner,
    transfer the terminal, rewrite the active bridge session, clear the
    old runner binding, and keep the hook cursor past the clear record
    so the next poll does not fork again from the same hook line. A
    project the server reports as not found (deleted, or owned by another
    user) gets one unfiled retry and the same transaction.
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
                    "title": "Fix login flow",
                    "project_id": "proj_alpha",
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions":
            if project_available or "project_id" not in body:
                return httpx.Response(201, json={"id": "conv_new"})
            return httpx.Response(404, json={"error": {"message": "Project not found"}})
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
        save_hook_state = forwarder._write_hook_state_async

        async def crash_after_checkpoint(*args: Any, **kwargs: Any) -> None:
            await save_hook_state(*args, **kwargs)
            raise asyncio.CancelledError

        if checkpoint_crash:
            monkeypatch.setattr(forwarder, "_write_hook_state_async", crash_after_checkpoint)
            with pytest.raises(asyncio.CancelledError):
                await forwarder._maybe_rotate_session_on_clear(
                    client=client, session_id="conv_old", bridge_dir=bridge_dir, state=hook_state
                )
            monkeypatch.setattr(forwarder, "_write_hook_state_async", save_hook_state)
            rotated_to = None
        else:
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

    assert rotated_to == (None if checkpoint_crash else "conv_new")
    assert rotated_again is None
    assert read_active_session_id(bridge_dir) == "conv_new"
    assert (bridge_dir / "transcript_forwarder.json").exists() is checkpoint_crash
    assert (bridge_dir / "hook_forwarder.json").exists()
    unfiled_body = {
        "agent_id": "ag_claude",
        "labels": {"omnigent.ui": "terminal", BRIDGE_ID_LABEL_KEY: "bridge_shared"},
        "title": "Fix login flow",
    }
    filed_body = {
        **unfiled_body,
        "project_id": "proj_alpha",
        "workspace": None,
        "git": None,
        "native_clear_source_id": "conv_old",
    }
    creates = [("POST", "/v1/sessions", filed_body)]
    if not project_available:
        creates.append(("POST", "/v1/sessions", unfiled_body))
    assert calls == [
        ("GET", "/v1/sessions/conv_old", None),
        *creates,
        ("PATCH", "/v1/sessions/conv_new", {"runner_id": "runner_one"}),
        (
            "POST",
            "/v1/sessions/conv_old/resources/terminals/terminal_claude_main/transfer",
            {"target_session_id": "conv_new"},
        ),
        (
            "PATCH",
            "/v1/sessions/conv_old",
            {"labels": {BRIDGE_ID_LABEL_KEY: "conv_old-cleared"}},
        ),
    ]

    from omnigent.harnesses.claude_native.clear_recovery import (
        pending_clear_supersessions,
        replay_clear_supersessions,
    )

    pending = pending_clear_supersessions(bridge_dir)
    assert [(old, new) for _, old, new in pending] == [("conv_old", "conv_new")]
    # Bridge reattachment replaces hook cursors but must retain pending cleanup.
    prepare_bridge_dir("conv_new", bridge_id="bridge_shared", workspace=tmp_path)
    cleanup_calls = []
    detach_attempts = 0

    def cleanup_handler(request: httpx.Request) -> httpx.Response:
        nonlocal detach_attempts
        cleanup_calls.append((request.method, request.url.path, json.loads(request.content)))
        if request.method == "PATCH" and json.loads(request.content) == {"runner_id": ""}:
            detach_attempts += 1
            if checkpoint_crash and detach_attempts == 1:
                return httpx.Response(503)
        return httpx.Response(200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(cleanup_handler), base_url="http://ap"
    ) as resumed_client:
        await replay_clear_supersessions(
            resumed_client, pending_clear_supersessions(bridge_dir), agent_name="claude-native-ui"
        )
        assert bool(pending_clear_supersessions(bridge_dir)) is checkpoint_crash
        await replay_clear_supersessions(
            resumed_client, pending_clear_supersessions(bridge_dir), agent_name="claude-native-ui"
        )
        assert pending_clear_supersessions(bridge_dir) == []
        count = len(cleanup_calls)
        await replay_clear_supersessions(
            resumed_client, pending_clear_supersessions(bridge_dir), agent_name="claude-native-ui"
        )
        assert len(cleanup_calls) == count
    assert detach_attempts == (2 if checkpoint_crash else 1)
    assert all(path.startswith("/v1/sessions/conv_old") for _, path, _ in cleanup_calls)
    notices = [
        body for _, _, body in cleanup_calls if body.get("type") == "external_conversation_item"
    ]
    assert {body["data"]["source_id"] for body in notices} == {"native-clear:conv_old:conv_new"}


@pytest.mark.asyncio
@pytest.mark.parametrize("measured_resume_prefix", [False, True])
@pytest.mark.parametrize("transcript_delayed", [False, True])
@pytest.mark.parametrize("transfer_rejected", [False, True])
async def test_clear_after_resume_preserves_input_recorded_before_cursor_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    measured_resume_prefix: bool,
    transcript_delayed: bool,
    transfer_rejected: bool,
) -> None:
    """A fresh cleared transcript must not inherit its parent's skip-prefix policy."""
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = prepare_bridge_dir("conv_old", bridge_id="bridge_shared", workspace=tmp_path)
    old_transcript = tmp_path / "old.jsonl"
    old_transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "old-input",
                "message": {"role": "user", "content": "old history"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    old_content = old_transcript.read_text(encoding="utf-8")
    parent_prefix = old_transcript.stat().st_size
    forwarder._write_forward_state(
        bridge_dir,
        forwarder.TranscriptForwardState(
            transcript_path=old_transcript,
            line_cursor=0,
            byte_offset=parent_prefix,
            cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(old_transcript, parent_prefix),
        ),
    )
    transcript = tmp_path / "new.jsonl"
    native_record = (
        json.dumps(
            {
                "type": "user",
                "uuid": "after-clear",
                "message": {"role": "user", "content": "new input"},
            }
        )
        + "\n"
    )
    if not transcript_delayed:
        transcript.write_text(native_record, encoding="utf-8")
    target_session = "conv_old" if transfer_rejected else "conv_new"
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "SessionStart", "source": "clear", "transcript_path": str(transcript)},
    )
    mirrored: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "conv_old",
                    "agent_id": "agent",
                    "runner_id": "runner",
                    "labels": {BRIDGE_ID_LABEL_KEY: "bridge_shared"},
                },
            )
        body = json.loads(request.content) if request.content else {}
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"id": "conv_new"})
        if request.url.path.endswith("/transfer") and transfer_rejected:
            return httpx.Response(400, json={"detail": "terminal transfer rejected"})
        if (
            request.method == "POST"
            and request.url.path == f"/v1/sessions/{target_session}/events"
        ):
            assert body["type"] == "external_conversation_item"
            mirrored.append(body)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://ap"
    ) as client:
        hooks = await forwarder._ensure_hook_state(
            bridge_dir, start_at_end=False, session_id="conv_old"
        )
        rotated_to = await forwarder._maybe_rotate_session_on_clear(
            client=client, session_id="conv_old", bridge_dir=bridge_dir, state=hooks
        )
        assert rotated_to == (None if transfer_rejected else "conv_new")
        assert read_active_session_id(bridge_dir) == target_session
        if transcript_delayed:
            transcript.write_text(native_record, encoding="utf-8")
        state = await forwarder._ensure_state_for_transcript(
            bridge_dir=bridge_dir,
            state=None,
            transcript_path=transcript,
            start_at_end=True,
            start_at_offset=parent_prefix if measured_resume_prefix else None,
            session_id=target_session,
        )
        assert state.byte_offset == 0
        for _ in range(2):
            state = await forwarder._forward_available_items(
                client=client,
                session_id=target_session,
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=forwarder._ForwardDedupeState(),
            )
    assert [event["data"]["source_id"] for event in mirrored] == ["after-clear:0:message"]
    assert transcript.read_text(encoding="utf-8") == native_record

    assert old_transcript.read_text(encoding="utf-8") == old_content


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["status", "transport"])
@pytest.mark.parametrize("sync_hook", [False, True], ids=["forwarder", "hook"])
async def test_clear_hook_rotation_survives_old_runner_clear_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    sync_hook: bool,
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
                "labels": {BRIDGE_ID_LABEL_KEY: "conv_old-cleared"},
            }
            from omnigent.harnesses.claude_native.clear_recovery import pending_clear_supersessions

            assert [(old, new) for _, old, new in pending_clear_supersessions(bridge_dir)] == [
                ("conv_old", "conv_new")
            ]
            if failure == "transport":
                raise httpx.ConnectError("temporary connection failure", request=request)
            return httpx.Response(503, json={"error": {"message": "temporary failure"}})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    payload: dict[str, Any] = {"hook_event_name": "SessionStart", "source": "clear"}
    if sync_hook:
        from omnigent.harnesses.claude_native import hook

        with httpx.Client(transport=transport) as sync_client:
            rotated = hook._create_clear_replacement_session(
                sync_client, "http://ap", "conv_old", bridge_dir
            )
        assert rotated == "conv_new"
        payload["omnigent_clear_rotated_to"] = rotated
    record_hook_event(bridge_dir, payload)
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
@pytest.mark.parametrize("detach_failure", [False, True])
async def test_post_clear_supersession_notifies_old_session(detach_failure, caplog) -> None:
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
        if detach_failure and request.method == "PATCH":
            return httpx.Response(503, json={"error": {"message": "temporarily unavailable"}})
        return httpx.Response(200, json={"queued": False, "item_id": "item_x"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
        await post_clear_supersession(
            client,
            old_session_id="conv_old",
            new_session_id="conv_new",
            agent_name="claude-native-ui",
            detach_runner=True,
        )

    assert ("Failed to detach superseded session after /clear" in caplog.text) is detach_failure
    assert len(calls) == 4
    assert calls[-1] == ("PATCH", "/v1/sessions/conv_old", {"runner_id": ""})
    # Every post is addressed to the OLD conversation.
    assert all(
        (method, path) == ("POST", "/v1/sessions/conv_old/events") for method, path, _ in calls[:3]
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
        await post_clear_supersession(
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
        await post_clear_supersession(
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


@pytest.mark.asyncio
async def test_clear_cleanup_retry_preserves_replacement_subagent_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import asynccontextmanager
    from itertools import count
    from types import SimpleNamespace

    from omnigent.harnesses.claude_native.clear_recovery import (
        pending_clear_supersessions,
        record_clear_supersession,
    )

    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = prepare_bridge_dir("conv_old", bridge_id="bridge_shared", workspace=tmp_path)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "SessionStart", "transcript_path": str(transcript)},
    )
    write_active_session_id(bridge_dir, "conv_old")
    started = {sid: asyncio.Event() for sid in ("conv_old", "conv_new")}
    cancelled = {sid: asyncio.Event() for sid in started}
    workers: dict[str, asyncio.Task[Any]] = {}
    worker_starts: list[str] = []
    cleanup_attempts: asyncio.Queue[tuple[bool, bool]] = asyncio.Queue()

    async def child_history(*, parent_session_id: str, **_kwargs: Any) -> Any:
        worker_starts.append(parent_session_id)
        worker = asyncio.current_task()
        assert worker is not None
        workers[parent_session_id] = worker
        started[parent_session_id].set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled[parent_session_id].set()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_old":
            if "labels" in json.loads(request.content):
                assert cancelled["conv_old"].is_set()
                return httpx.Response(200)
            assert json.loads(request.content) == {"runner_id": ""}
            cleanup_attempts.put_nowait(
                (cancelled["conv_old"].is_set(), cancelled["conv_new"].is_set())
            )
            return httpx.Response(503)
        return httpx.Response(200, json={})

    @asynccontextmanager
    async def open_client(*_args: Any, **_kwargs: Any):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="http://ap"
        ) as client:
            yield client

    # Advance only the forwarder's retry clock, leaving asyncio deadlines real.
    ticks = count()
    monkeypatch.setattr(
        forwarder,
        "time",
        SimpleNamespace(monotonic=lambda: float(next(ticks)), time=forwarder.time.time),
    )
    monkeypatch.setattr("omnigent.cli_auth.open_server_client", open_client)
    monkeypatch.setattr(forwarder, "_forward_available_subagents", child_history)
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url="http://ap",
            headers={},
            session_id="conv_old",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.001,
        )
    )
    try:
        await asyncio.wait_for(started["conv_old"].wait(), 5)
        record_clear_supersession(bridge_dir, "conv_old", "conv_new")
        write_active_session_id(bridge_dir, "conv_new")
        assert await asyncio.wait_for(cleanup_attempts.get(), 5) == (True, False)
        await asyncio.wait_for(started["conv_new"].wait(), 5)
        replacement = workers["conv_new"]
        for _ in range(2):
            assert await asyncio.wait_for(cleanup_attempts.get(), 5) == (True, False)
        assert workers["conv_new"] is replacement
        assert not replacement.done()
        assert worker_starts == ["conv_old", "conv_new"]
        assert [(old, new) for _, old, new in pending_clear_supersessions(bridge_dir)] == [
            ("conv_old", "conv_new")
        ]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", 503, 403, 404])
async def test_clear_replay_rekeys_before_notifications_and_detach(
    tmp_path: Path, failure: str | int
) -> None:
    from omnigent.harnesses.claude_native.clear_recovery import (
        pending_clear_supersessions,
        record_clear_supersession,
        replay_clear_supersessions,
    )

    record_clear_supersession(tmp_path, "conv_old", "conv_new")
    requests: list[tuple[str, str, dict[str, Any]]] = []
    recovered = False

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.method, request.url.path, body))
        if not recovered:
            assert body == {"labels": {BRIDGE_ID_LABEL_KEY: "conv_old-cleared"}}
            if failure == "transport":
                raise httpx.ConnectError("temporary connection failure", request=request)
            assert isinstance(failure, int)
            return httpx.Response(failure)
        return httpx.Response(200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://ap"
    ) as client:
        await replay_clear_supersessions(
            client, pending_clear_supersessions(tmp_path), agent_name="claude-native-ui"
        )
        assert len(requests) == 1
        assert requests[0][:2] == ("PATCH", "/v1/sessions/conv_old")
        transient = failure in ("transport", 503)
        assert bool(pending_clear_supersessions(tmp_path)) is transient
        recovered = True
        await replay_clear_supersessions(
            client, pending_clear_supersessions(tmp_path), agent_name="claude-native-ui"
        )
        assert pending_clear_supersessions(tmp_path) == []

    if transient:
        assert [method for method, _, _ in requests[1:]] == [
            "PATCH",
            "POST",
            "POST",
            "POST",
            "PATCH",
        ]
        assert requests[1][2] == {"labels": {BRIDGE_ID_LABEL_KEY: "conv_old-cleared"}}
        assert requests[-1][2] == {"runner_id": ""}
        assert all(path.startswith("/v1/sessions/conv_old") for _, path, _ in requests)
    else:
        assert len(requests) == 1
