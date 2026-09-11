"""Terminal-only dialogs and notifications must reach the chat without approving tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge, forwarder
from omnigent.harnesses.claude_native.tui_messages import terminal_message_from_pane


@pytest.fixture(autouse=True)
def trusted_bridge_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)


TOOL_SEARCH_PROMPT = """────────────────────────────────────────────────────────
 Tool use
 ToolSearch
 Fetches full schema definitions for deferred tools so they can be called.
 Deferred tools appear by name in system-reminder messages.
 (ctrl+o to expand description)
 PreToolUse: ToolSearch requires confirmation for this tool:
 Omnigent policy evaluation unavailable (could not reach or authenticate to
 the Omnigent server); please approve or deny this tool call manually.
 Detail: Databricks token refresh returned no token
 settings.json to update hooks

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for ToolSearch commands in /workspace
   3. No
 Esc to cancel · Tab to amend
"""


@pytest.mark.parametrize("caret", ["❯", "›", ">"])
@pytest.mark.parametrize("with_footer", [True, False])
def test_tool_search_dialog_preserves_context_and_all_choices(
    caret: str, with_footer: bool
) -> None:
    pane = TOOL_SEARCH_PROMPT.replace("❯", caret)
    if not with_footer:
        pane = pane[: pane.index(" Esc to cancel")]
    message = terminal_message_from_pane(pane)
    assert message is not None
    for text in (
        "ToolSearch",
        "Fetches full schema definitions",
        "PreToolUse: ToolSearch requires confirmation",
        "Omnigent policy evaluation unavailable",
        "Databricks token refresh returned no token",
        "settings.json to update hooks",
        "1. Yes",
        "2. Yes, and don't ask again for ToolSearch commands in /workspace",
        "3. No",
    ):
        assert text in message
    assert "────────────────" not in message


def test_dialog_identity_ignores_selected_choice() -> None:
    moved = TOOL_SEARCH_PROMPT.replace("❯ 1.", "  1.").replace("  3. No", "❯ 3. No")
    assert terminal_message_from_pane(moved) == terminal_message_from_pane(TOOL_SEARCH_PROMPT)


@pytest.mark.parametrize("side", ["│", "┃", "║"])
def test_boxed_dialog_preserves_the_same_message(side: str) -> None:
    lines = TOOL_SEARCH_PROMPT.splitlines()[1:]
    pane = "\n".join(
        ["╭────────────────────────────────────────────────────────╮"]
        + [f"{side} {line} {side}" for line in lines]
        + ["╰────────────────────────────────────────────────────────╯"]
    )
    assert terminal_message_from_pane(pane) == terminal_message_from_pane(TOOL_SEARCH_PROMPT)


def test_other_numbered_dialogs_do_not_require_yes_no_choices() -> None:
    message = terminal_message_from_pane(
        "─────────────\nSign in\nChoose an account\n"
        "› 1. Personal\n  2. Organization\nEnter to select"
    )
    assert message == "Sign in\nChoose an account\n1. Personal\n2. Organization\nEnter to select"


@pytest.mark.parametrize(
    "pane",
    [
        "",
        "An answer\n1. Yes\n2. No\n❯",
        "Select a file\n❯ 1. file.txt",
        "An answer quoting options\n❯ 1. Yes\n2. No",
        TOOL_SEARCH_PROMPT + "\n❯\n? for shortcuts",
        TOOL_SEARCH_PROMPT + "\n❯ continue with the next task\n? for shortcuts",
        TOOL_SEARCH_PROMPT.replace("3. No", "2. No"),
    ],
)
def test_non_dialogs_are_not_forwarded(pane: str) -> None:
    assert terminal_message_from_pane(pane) is None


def test_notification_hook_is_registered_without_a_server(tmp_path: Path) -> None:
    settings = bridge.build_hook_settings(tmp_path)
    assert settings["hooks"]["Notification"] == settings["hooks"]["Stop"]
    assert "matcher" not in settings["hooks"]["Notification"][0]


@pytest.mark.parametrize("message", [None, 123, {}, "", "  "])
def test_malformed_notifications_are_ignored(tmp_path: Path, message: object) -> None:
    bridge.record_hook_event(tmp_path, {"hook_event_name": "Notification", "message": message})
    result = bridge.read_hook_events_from_offset(tmp_path, 0, start_event_count=0)
    assert result.records[0].notification_message is None


@pytest.mark.asyncio
async def test_pane_notices_retry_dedupe_and_keep_the_same_id_on_restart(tmp_path: Path) -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(503 if len(calls) == 1 else 200, json={})

    message = terminal_message_from_pane(TOOL_SEARCH_PROMPT)
    dedupe = forwarder._ForwardDedupeState()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for _ in range(5):
            await forwarder._relay_terminal_message(
                client, session_id="session", message=message, response_id="turn", dedupe=dedupe
            )
        assert len(calls) == 2
        for _ in range(2):
            await forwarder._relay_terminal_message(
                client,
                session_id="session",
                message=message,
                response_id="next-turn",
                dedupe=dedupe,
            )
        assert len(calls) == 3
        restarted = forwarder._ForwardDedupeState()
        for _ in range(2):
            await forwarder._relay_terminal_message(
                client,
                session_id="session",
                message=message,
                response_id="next-turn",
                dedupe=restarted,
            )
    assert calls[0] == calls[1]
    assert calls[2] == calls[3]
    assert calls[1]["data"]["source_id"] != calls[2]["data"]["source_id"]
    assert all(call["type"] == "external_conversation_item" for call in calls)
    notice = calls[0]["data"]["item_data"]
    assert notice["level"] == "info"
    assert "Use its approval card if available" in notice["message"]
    assert "Databricks token refresh returned no token" in notice["message"]


@pytest.mark.asyncio
async def test_pane_capture_relays_dialog_without_driving_terminal(tmp_path: Path) -> None:
    (tmp_path / "tmux.json").write_text(
        json.dumps({"socket_path": "socket", "tmux_target": "pane"})
    )
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={})

    dedupe = forwarder._ForwardDedupeState()
    with (
        patch.object(bridge, "_capture_pane", return_value=TOOL_SEARCH_PROMPT) as capture,
        patch.object(bridge, "_run_tmux") as run_tmux,
        patch.object(forwarder, "_PANE_POLL_INTERVAL_S", 0),
    ):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://ap"
        ) as client:
            for _ in range(3):
                await forwarder._forward_pane_signals(
                    client, session_id="session", bridge_dir=tmp_path, dedupe=dedupe
                )
        assert capture.call_count == 3
        run_tmux.assert_not_called()
    assert len(calls) == 1
    assert "3. No" in calls[0]["data"]["item_data"]["message"]


@pytest.mark.asyncio
async def test_notification_delivery_keeps_cursor_until_post_succeeds(tmp_path: Path) -> None:
    bridge.record_hook_event(
        tmp_path,
        {
            "hook_event_name": "Notification",
            "title": "Authentication",
            "message": "Token refresh failed",
            "notification_type": "auth_error",
        },
    )
    bridge.record_hook_event(
        tmp_path,
        {
            "hook_event_name": "Notification",
            "message": "An unfamiliar notification",
            "notification_type": "future_kind",
        },
    )
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(503 if len(calls) == 1 else 200, json={})

    state = forwarder.HookForwardState(event_cursor=0, byte_offset=0)
    tracker = forwarder._PostRetryTracker(base_delay_s=0, max_delay_s=0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        for attempt in range(3):
            state = await forwarder._forward_available_status_events(
                client=client,
                session_id="session",
                bridge_dir=tmp_path,
                state=state,
                retry_tracker=tracker,
                dedupe=forwarder._ForwardDedupeState(),
                task_subjects={},
                task_statuses={},
                task_order=[],
            )
            assert state.event_cursor == (0 if attempt == 0 else 2)
    assert len(calls) == 3
    assert calls[0] == calls[1]
    assert calls[0]["data"]["item_data"]["message"] == "Authentication\n\nToken refresh failed"
    assert calls[2]["data"]["item_data"]["message"] == "An unfamiliar notification"
    assert all(call["type"] == "external_conversation_item" for call in calls)


@pytest.mark.asyncio
async def test_startup_dialog_and_notification_forward_before_transcript_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge.record_hook_event(
        tmp_path, {"hook_event_name": "Notification", "message": "Authentication required"}
    )
    calls: list[dict] = []
    delivered = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if len(calls) >= 2:
            delivered.set()
        return httpx.Response(200, json={})

    @asynccontextmanager
    async def client_factory(*args: object, **kwargs: object) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://ap"
        ) as client:
            yield client

    monkeypatch.setattr("omnigent.cli_auth.open_server_client", client_factory)
    monkeypatch.setattr(forwarder, "_PANE_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(
        forwarder,
        "read_pane_signals",
        lambda _bridge_dir: bridge.PaneSignals(
            terminal_message=terminal_message_from_pane(TOOL_SEARCH_PROMPT)
        ),
    )
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url="http://ap",
            session_id="session",
            bridge_dir=tmp_path,
            headers={},
            agent_name="claude-native-ui",
            poll_interval_s=0.01,
            start_at_end=False,
        )
    )
    try:
        await asyncio.wait_for(delivered.wait(), timeout=5)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert bridge.read_transcript_path(tmp_path) is None
    assert all(call["type"] == "external_conversation_item" for call in calls)
    assert calls[0]["data"]["item_data"]["message"] == "Authentication required"
    assert "ToolSearch" in calls[1]["data"]["item_data"]["message"]
