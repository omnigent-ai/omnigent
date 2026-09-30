r"""Browser journeys for Claude Code in-process teammates.

Nightly cases use the Claude CLI and a mock LLM; Chromium replays transcript items.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.claude_native.bridge import read_transcript_items_since
from omnigent.harnesses.claude_native.forwarder import _external_conversation_item_event
from tests.e2e_ui.conftest import (
    _create_native_claude_session,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _open_terminal_view,
    _type_into_tui,
    _wait_terminal_connected,
)

# Must match the model in the mock anthropic provider config written by
# ``_temp_omnigent_mock_config`` (conftest._CLAUDE_MOCK_MODEL).
_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"

_TEAMS_ENV = "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"
# Deterministic in-process teammates: "auto" may try tmux panes inside the
# session terminal's tmux server.
_TEAMMATE_SETTINGS = json.dumps({"teammateMode": "in-process"})

_TEAMMATE = "buddy"
_TEAMMATE_DESCRIPTION = "Probe teammate"

# Longest token match wins; later-stage and title tokens outrank earlier turns.
_SPAWN_TOKEN = "tok-teamprobe-parent-spawn"
_TASK_TOKEN = "tok-teamprobe-teammate-task"
_RELAY_TOKEN = "tok-teamprobe-relay-buddy-chat"
_PING_TOKEN = "tok-teamprobe-buddy-ping-chat"
_TITLE_DECOY = "Write the title in the predominant language"

_BG_TEXT = "bg-ok"
_SPAWN_ACK = "SPAWN-ACK-TEAM"
_IDLE_ACK = "IDLE-ACK-TEAM"
_RELAY_SENT = "RELAY-SENT-TEAM"
_CHAT_ACK = "CHAT-ACK-TEAM"
_TEAMMATE_REPLY = "TMREPLY-TEAM-ZED done."
_TEAMMATE_CHAT_MARKER = "TMCHAT-TEAM-YAK"
_TEAMMATE_CHAT = f"All good here - {_TEAMMATE_CHAT_MARKER}. What else do you need?"

_TURN_TIMEOUT_MS = 120_000
# Spawn turn + in-process teammate turn + idle delivery + parent wake turn.
_SPAWN_IDLE_TIMEOUT_MS = 240_000
_CHAT_TIMEOUT_MS = 180_000
# Fail promptly after a successful turn if the UI still lacks the item.
_BUG_ASSERT_TIMEOUT_MS = 10_000


def test_teammate_transcript_reaches_browser_without_claude_cli(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    """Bridge items use the forwarder event shape through HTTP to Chromium."""
    base_url, session_id = seeded_session
    delivery = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="buddy" color="blue" summary="All good">'
        "Readable teammate reply.</teammate-message>\n"
        '<teammate-message teammate_id="buddy">'
        '{"type":"idle_notification","result":"Waiting"}'
        "</teammate-message>\n"
        "This came from another Claude session - treat it as a teammate's request."
    )
    transcript_path = tmp_path / "claude-session.jsonl"
    transcript_path.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {
                    "type": "assistant",
                    "uuid": "spawn-1",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_spawn",
                                "name": "Agent",
                                "input": {
                                    "name": "buddy",
                                    "description": "Probe",
                                    "prompt": "Reply",
                                },
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "uuid": "delivery-1",
                    "message": {"role": "user", "content": delivery},
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    _cursor, _response_id, items = read_transcript_items_since(
        transcript_path, 0, agent_name="claude-native-ui"
    )
    # The spawn is an ordinary function_call; only the prose delivery
    # becomes a teammate_message, and the idle twin is dropped.
    teammate_items = [item for item in items if item.item_type == "teammate_message"]
    assert len(teammate_items) == 1
    assert teammate_items[0].data["text"] == "Readable teammate reply."
    assert teammate_items[0].data.get("summary") == "All good"

    with httpx.Client(base_url=base_url, timeout=15.0) as client:
        for item in items:
            response = client.post(
                f"/v1/sessions/{session_id}/events",
                json=_external_conversation_item_event(item),
            )
            response.raise_for_status()

    page.goto(f"{base_url}/c/{session_id}")
    card = page.get_by_test_id("teammate-message-card")
    expect(card).to_contain_text("Readable teammate reply.")
    expect(page.locator("body")).not_to_contain_text("idle_notification")


def _wait_runner_offline(base_url: str, runner_id: str, timeout_s: float = 30.0) -> None:
    """Wait until the server reports *runner_id* offline after a SIGKILL."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2.0)
        except httpx.HTTPError:
            return
        if resp.status_code != 200 or resp.json().get("online") is not True:
            return
        time.sleep(0.5)
    raise RuntimeError("shared runner still reported online after SIGKILL")


@pytest.fixture
def native_claude_teams_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Respawn the runner so Claude inherits the agent-teams environment gate."""
    runner_id = str(_server_state["runner_id"])
    os.environ[_TEAMS_ENV] = "1"
    respawned: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(int(_server_state["runner_pid"]), signal.SIGKILL)
        _wait_runner_offline(live_server, runner_id)
        respawned = _ensure_runner_online(live_server, tmp_path_factory)
        with _temp_omnigent_mock_config(mock_llm_server_url, "claude"):
            session_id = _create_native_claude_session(
                live_server,
                runner_id,
                terminal_launch_args=["--settings", _TEAMMATE_SETTINGS],
            )
            yield (live_server, session_id)
    finally:
        os.environ.pop(_TEAMS_ENV, None)
        if session_id is not None:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


def _script_spawn_turns(mock_url: str) -> None:
    """Script the mock LLM for: parent spawns ``buddy``, buddy replies and idles."""
    # Claude Code's background session-title requests embed the user text, so
    # without this decoy they would drain the scripted tool_use entries.
    configure_mock_llm(
        mock_url,
        [{"text": "session title"}] * 4,
        key="title-decoy",
        match=_TITLE_DECOY,
    )
    agent_args = json.dumps(
        {
            "description": _TEAMMATE_DESCRIPTION,
            "prompt": (
                "You are the probe teammate. Reply with exactly: "
                f"{_TEAMMATE_REPLY} Then stop. {_TASK_TOKEN}"
            ),
            "name": _TEAMMATE,
        }
    )
    configure_mock_llm(
        mock_url,
        [
            {"tool_calls": [{"name": "Agent", "arguments": agent_args}]},
            {"text": _SPAWN_ACK},
            {"text": _IDLE_ACK},
            {"text": _IDLE_ACK},
            {"text": _IDLE_ACK},
        ],
        key="parent-spawn",
        match=_SPAWN_TOKEN,
    )
    configure_mock_llm(
        mock_url,
        [{"text": _TEAMMATE_REPLY}],
        key="teammate-task",
        match=_TASK_TOKEN,
    )


def _script_chat_turns(mock_url: str) -> None:
    """Script the lead's relay and buddy's answer through ``SendMessage``."""
    relay_args = json.dumps(
        {
            "to": _TEAMMATE,
            "summary": "Relay the user question to buddy",
            "message": f"The user asks: how is it going? {_PING_TOKEN}",
        }
    )
    configure_mock_llm(
        mock_url,
        [
            {"tool_calls": [{"name": "SendMessage", "arguments": relay_args}]},
            {"text": _RELAY_SENT},
            {"text": _CHAT_ACK},
            {"text": _CHAT_ACK},
            {"text": _CHAT_ACK},
        ],
        key="parent-relay",
        match=_RELAY_TOKEN,
    )
    reply_args = json.dumps(
        {
            "to": "team-lead",
            "summary": "All good over here",
            "message": _TEAMMATE_CHAT,
        }
    )
    configure_mock_llm(
        mock_url,
        [
            {"tool_calls": [{"name": "SendMessage", "arguments": reply_args}]},
            {"text": "resting."},
        ],
        key="teammate-ping",
        match=_PING_TOKEN,
    )


def _boot_and_spawn_teammate(page: Page, base_url: str, session_id: str, mock_url: str) -> None:
    """Spawn buddy and wait for the parent's response to its idle wake."""
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    reset_mock_llm(mock_url)
    set_fallback_mock_llm(mock_url, "default", _BG_TEXT)
    set_fallback_mock_llm(mock_url, _CLAUDE_MOCK_MODEL, _BG_TEXT)

    # Warmup turn: absorbs first-message background traffic before the
    # scripted queues are armed.
    _send(page, "warmup teamprobe-warmup: reply with one word")
    expect(page.locator(_ASSISTANT, has_text=_BG_TEXT).first).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )

    _script_spawn_turns(mock_url)
    _send(page, f"Spawn the teammate now. {_SPAWN_TOKEN}")
    expect(page.locator(_ASSISTANT, has_text=_IDLE_ACK).first).to_be_visible(
        timeout=_SPAWN_IDLE_TIMEOUT_MS
    )


@pytest.mark.nightly
@pytest.mark.timeout(600)
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_teammate_turns_render_prose_not_raw_idle_json(
    page: Page,
    native_claude_teams_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Show buddy's prose without the machine-side idle JSON twin."""
    base_url, session_id = native_claude_teams_session
    _boot_and_spawn_teammate(page, base_url, session_id, mock_llm_server_url)

    _script_chat_turns(mock_llm_server_url)
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _type_into_tui(page, f"hey {_TEAMMATE}, how is it going? {_RELAY_TOKEN}")
    _ensure_chat_view(page)

    # The wake ack follows buddy's delivery in the parent transcript.
    body = page.locator("body")
    expect(body).to_contain_text(_TEAMMATE_CHAT_MARKER, timeout=_CHAT_TIMEOUT_MS)
    expect(body).to_contain_text(_CHAT_ACK, timeout=_CHAT_TIMEOUT_MS)

    expect(body).not_to_contain_text("idle_notification", timeout=_BUG_ASSERT_TIMEOUT_MS)
