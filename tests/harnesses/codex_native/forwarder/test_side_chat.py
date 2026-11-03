"""Side chat tests for Codex forwarder."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    read_bridge_state,
    write_bridge_state,
)
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "interrupted", "failed"])
async def test_side_chat_turn_status_settles_without_touching_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal_status: str
) -> None:
    monkeypatch.chdir(tmp_path)
    bridge_dir = tmp_path / "parent"
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id="conv_parent",
            socket_path="ws://127.0.0.1:1234",
            thread_id="thread_parent",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_parent",
        ),
    )
    client = _RecordingClient()
    state = fwd._CodexForwarderState(parent_session_id="conv_parent")
    state.note_child_thread("thread_side", "conv_side")
    tracker = fwd._CodexElicitationTaskTracker()
    try:
        for method, status in (
            ("turn/started", "inProgress"),
            ("turn/completed", terminal_status),
        ):
            await fwd._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=bridge_dir,
                event={
                    "method": method,
                    "params": {
                        "threadId": "thread_side",
                        "turn": {"id": "turn_side", "status": status, "items": []},
                    },
                },
                usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_parent"),
                elicitation_tracker=tracker,
                expected_thread_id="thread_parent",
                forwarder_state=state,
            )
    finally:
        await tracker.close()

    statuses = [
        payload["data"]["status"]
        for _, payload in client.posts
        if payload["type"] == "external_session_status"
    ]
    assert statuses == ["running", "failed" if terminal_status == "failed" else "idle"]
    assert all(url == "/v1/sessions/conv_side/events" for url, _ in client.posts)
    interrupted = [
        payload["data"]
        for _, payload in client.posts
        if payload["type"] == "external_session_interrupted"
    ]
    assert interrupted == (
        [{"response_id": "codex_turn_side"}] if terminal_status == "interrupted" else []
    )
    parent_state = read_bridge_state(bridge_dir)
    assert parent_state is not None and parent_state.active_turn_id == "turn_parent"
