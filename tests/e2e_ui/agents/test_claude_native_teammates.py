r"""UI journeys: Claude Code in-process teammates in the Omnigent web UI.

Claude Code (the ``claude-native`` wrapper) can spawn an in-process *teammate*
through its own ``Agent`` tool when agent teams are enabled — a named agent
that shares the parent process and is never created via ``sys_session_create``,
so Omnigent has no child-session row for it. Two user-visible symptoms follow
on the running build:

1. A running teammate appears nowhere in the Subagents rail (list or graph
   view). The rail renders from ``useChildSessions`` — the Omnigent session
   tree — and while Task-tool workers get shadow child rows via the
   claude-native forwarder's ``external_subagent_start`` event, teammates
   never do, so an actively running teammate looks like nothing is happening.

2. Teammate deliveries reach the parent transcript as ``<teammate-message>``
   text, including a machine-side ``idle_notification`` JSON twin for every
   teammate turn, and the chat renders that JSON object verbatim in a user
   bubble instead of a readable item.

Both journeys drive the REAL ``claude`` CLI against the scripted mock LLM:
the parent model's turn is a scripted ``Agent`` tool call that spawns the
in-process teammate ``buddy``, the teammate's own turns are scripted replies
(its prose goes to the lead via a scripted ``SendMessage``), and Claude Code's
genuine team machinery produces the deliveries Omnigent must render. The
tests assert the EXPECTED behavior, so on the buggy build each fails at
exactly its reported symptom.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_native_claude_session,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    configure_mock_llm,
    open_right_rail,
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

# Content-routing tokens for the mock LLM queues. Longest match wins, so the
# stage-2 tokens are longer than the stage-1 tokens that remain in the
# conversation history, and the title-request decoy is longest of all.
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
# Short budget for the buggy-build assertions so a failing run stays tight.
_BUG_ASSERT_TIMEOUT_MS = 10_000


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
    """A runner-bound claude-native session with agent teams enabled (mock LLM).

    Claude Code gates agent teams on ``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS``
    reaching the ``claude`` process (a ``--settings`` ``env`` block is applied
    too late for the gate), and the terminal child inherits the runner's env —
    so the shared runner is respawned with the variable set, and torn down
    afterwards so later tests respawn it clean.

    :returns: ``(base_url, session_id)``.
    """
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
    """Script the mock LLM for: user's TUI message reaches ``buddy``, buddy answers.

    The lead relays the user's question via ``SendMessage`` (a scripted model
    cannot route free text itself), and buddy sends its prose reply back to
    the lead the way real teammates do — a ``SendMessage`` to ``team-lead`` —
    then idles, which is what produces the prose + ``idle_notification`` pair
    in the parent transcript.
    """
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
    """Open the session, spawn the in-process teammate, and wait until it idled.

    The ``IDLE_ACK`` bubble is the parent's response to the wake turn Claude
    Code runs when the teammate's idle notification is delivered, so its
    visibility guarantees the teammate exists and its delivery has already
    been mirrored into the Omnigent transcript — without depending on how
    (or whether) the delivery itself is rendered.
    """
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
def test_running_teammate_visible_in_subagents_rail(
    page: Page,
    native_claude_teams_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A spawned in-process teammate has an entry in the Subagents rail.

    Journey: start a claude-native session → the agent spawns the named
    in-process teammate ``buddy`` via its own ``Agent`` tool → open the
    Workspace rail's Agents tab. Expected: some entry names the running
    teammate (list or graph view). On the buggy build the rail shows only
    the main Claude Code row, so the teammate assertion fails.
    """
    base_url, session_id = native_claude_teams_session
    _boot_and_spawn_teammate(page, base_url, session_id, mock_llm_server_url)

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    expect(rail.get_by_test_id("subagent-main-row")).to_be_visible(timeout=30_000)

    teammate_entry = rail.get_by_text(re.compile(rf"{_TEAMMATE}|{_TEAMMATE_DESCRIPTION}"))
    try:
        expect(teammate_entry.first).to_be_visible(timeout=_BUG_ASSERT_TIMEOUT_MS)
        return
    except AssertionError:
        pass
    rail.get_by_test_id("view-mode-graph").click()
    try:
        expect(teammate_entry.first).to_be_visible(timeout=_BUG_ASSERT_TIMEOUT_MS)
    except AssertionError:
        pytest.fail(
            f"running in-process teammate '{_TEAMMATE}' has no entry in the "
            "Subagents rail (checked list and graph views) — the user cannot "
            "see, select, or navigate to it"
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
    """Teammate turns render as readable prose, never as raw idle_notification JSON.

    Journey: spawn the in-process teammate ``buddy`` → from the session TUI,
    message it and get its answer back into the parent conversation. Expected:
    the chat shows the teammate's prose reply as a readable item and never
    prints the machine-side ``{"type":"idle_notification",...}`` twin as a raw
    JSON object. On the buggy build the raw JSON is rendered verbatim, so the
    final assertion fails.
    """
    base_url, session_id = native_claude_teams_session
    _boot_and_spawn_teammate(page, base_url, session_id, mock_llm_server_url)

    _script_chat_turns(mock_llm_server_url)
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _type_into_tui(page, f"hey {_TEAMMATE}, how is it going? {_RELAY_TOKEN}")
    _ensure_chat_view(page)

    # The teammate's prose answer must reach the conversation in readable
    # form (it does today too — wrapped in raw <teammate-message> text), and
    # the wake ack proves the post-reply deliveries were already mirrored.
    body = page.locator("body")
    expect(body).to_contain_text(_TEAMMATE_CHAT_MARKER, timeout=_CHAT_TIMEOUT_MS)
    expect(body).to_contain_text(_CHAT_ACK, timeout=_CHAT_TIMEOUT_MS)

    expect(body).not_to_contain_text("idle_notification", timeout=_BUG_ASSERT_TIMEOUT_MS)
