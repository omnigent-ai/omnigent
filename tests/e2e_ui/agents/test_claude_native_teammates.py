"""Claude Code in-process teammates in the Omnigent web UI.

The real Claude CLI runs against the mock LLM with agent teams enabled; the
scripted lead spawns a named teammate through its own ``Agent`` tool.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import (
    _CLAUDE_MOCK_MODEL,
    _create_native_claude_session,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    configure_mock_llm,
    open_right_rail,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _ASSISTANT, _ensure_chat_view, _send
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _open_terminal_view,
    _type_into_tui,
    _wait_terminal_connected,
)

# Per-session settings layer: the teams gate plus deterministic in-process
# teammates ("auto" may try tmux panes inside the session terminal's server).
_TEAMS_SETTINGS = json.dumps(
    {"env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}, "teammateMode": "in-process"}
)

_TEAMMATE = "buddy"

# Content-routing tokens; the mock picks the longest token found in the user text.
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
_TEAMMATE_CHAT_SUMMARY = "All good over here"

_TURN_TIMEOUT_MS = 120_000
# Spawn turn + teammate turn (incl. its scripted delay) + idle delivery + lead wake.
_SPAWN_IDLE_TIMEOUT_MS = 240_000
_CHAT_TIMEOUT_MS = 180_000
# Fail promptly once the turn has settled and the UI still lacks the item.
_BUG_ASSERT_TIMEOUT_MS = 10_000

_SUBAGENT_ROW = '[data-testid="subagent-row"]'
_SUBAGENT_MAIN_ROW = '[data-testid="subagent-main-row"]'


@pytest.fixture
def native_claude_teams_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A real claude-native session whose Claude process has agent teams enabled."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    with _temp_omnigent_mock_config(
        mock_llm_server_url, "claude", workflow_owned=bool(_server_state.get("workflow_owned"))
    ):
        session_id: str | None = None
        try:
            session_id = _create_native_claude_session(
                live_server, runner_id, terminal_launch_args=["--settings", _TEAMS_SETTINGS]
            )
            yield (live_server, session_id)
        finally:
            if session_id is not None:
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


def _evidence_dir(tmp_path: Path, name: str) -> Path:
    override = os.environ.get("OMNIGENT_TEAMMATES_EVIDENCE_DIR")
    base = Path(override) if override else tmp_path
    path = base / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _script_spawn_turns(mock_url: str, *, teammate_delay_s: float) -> None:
    """Lead spawns ``buddy`` via Agent; buddy answers its task after a delay and idles."""
    configure_mock_llm(
        mock_url, [{"text": "session title"}] * 4, key="title-decoy", match=_TITLE_DECOY
    )
    agent_args = json.dumps(
        {
            "name": _TEAMMATE,
            "description": "Probe teammate",
            "prompt": (
                f"You are the probe teammate. Reply with exactly: {_TEAMMATE_REPLY} "
                f"Then stop. {_TASK_TOKEN}"
            ),
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
        [{"text": _TEAMMATE_REPLY, "delay": teammate_delay_s}],
        key="teammate-task",
        match=_TASK_TOKEN,
    )


def _script_chat_turns(mock_url: str) -> None:
    """Lead relays the user's question to buddy; buddy answers the lead via SendMessage."""
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
        {"to": "team-lead", "summary": _TEAMMATE_CHAT_SUMMARY, "message": _TEAMMATE_CHAT}
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


def _boot_and_spawn_teammate(
    page: Page, base_url: str, session_id: str, mock_url: str, *, teammate_delay_s: float
) -> None:
    """Open the session, let Claude boot, then have the lead spawn ``buddy``."""
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    reset_mock_llm(mock_url)
    set_fallback_mock_llm(mock_url, "default", _BG_TEXT)
    set_fallback_mock_llm(mock_url, _CLAUDE_MOCK_MODEL, _BG_TEXT)

    # First-message background traffic lands on the fallback, not the scripted queues.
    _send(page, "hello teamprobe-warmup: reply with one word")
    expect(page.locator(_ASSISTANT, has_text=_BG_TEXT).first).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )

    _script_spawn_turns(mock_url, teammate_delay_s=teammate_delay_s)
    _send(page, f"Spawn the teammate now. {_SPAWN_TOKEN}")
    expect(page.locator(_ASSISTANT, has_text=_SPAWN_ACK).first).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )


def _open_agents_tab(page: Page) -> Locator:
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    expect(rail.locator(_SUBAGENT_MAIN_ROW)).to_be_visible(timeout=30_000)
    return rail


def _rail_snapshot(page: Page, rail: Locator, evidence: Path, stage: str) -> dict[str, object]:
    rail.get_by_test_id("view-mode-list").click()
    expect(rail.locator(_SUBAGENT_MAIN_ROW)).to_be_visible(timeout=30_000)
    page.wait_for_timeout(1_500)
    list_text = rail.inner_text()
    page.screenshot(path=str(evidence / f"rail-list-{stage}.png"))
    rail.get_by_test_id("view-mode-graph").click()
    expect(rail.get_by_role("button", name="Fit view")).to_be_visible(timeout=30_000)
    page.wait_for_timeout(1_500)
    graph_text = rail.inner_text()
    page.screenshot(path=str(evidence / f"rail-graph-{stage}.png"))
    rail.get_by_test_id("view-mode-list").click()
    expect(rail.locator(_SUBAGENT_MAIN_ROW)).to_be_visible(timeout=30_000)
    badge = rail.get_by_role("tab", name=re.compile("^Agents")).inner_text()
    return {
        "stage": stage,
        "agents_tab_text": badge,
        "subagent_rows": rail.locator(_SUBAGENT_ROW).count(),
        "list_mentions_teammate": _TEAMMATE in list_text,
        "graph_mentions_teammate": _TEAMMATE in graph_text,
        "list_text": list_text,
        "graph_text": graph_text,
    }


def _dump_items(base_url: str, session_id: str, evidence: Path, name: str) -> list[dict]:
    payload = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=30.0).json()
    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    (evidence / f"{name}.json").write_text(json.dumps(items, indent=1))
    return items


def _teammate_items(items: list[dict]) -> list[dict]:
    """Items carrying teammate traffic; the spawn's description alone does not count."""
    hits = []
    for item in items:
        blob = json.dumps(item)
        if "<teammate-message" in blob or "idle_notification" in blob or "<agent-message" in blob:
            hits.append(item)
    return hits


@pytest.mark.nightly
@pytest.mark.timeout(900)
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_in_process_teammate_appears_in_agents_rail(
    request: pytest.FixtureRequest,
    native_claude_teams_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A running, then idle, in-process teammate has an entry in the Agents rail."""
    base_url, session_id = native_claude_teams_session
    evidence = _evidence_dir(tmp_path, "rail")
    page: Page = request.getfixturevalue("page")
    # 30 s keeps buddy visibly running while the rail is inspected.
    _boot_and_spawn_teammate(page, base_url, session_id, mock_llm_server_url, teammate_delay_s=30)

    rail = _open_agents_tab(page)
    # The forwarder registers the teammate a poll tick after the spawn.
    expect(rail.get_by_text(_TEAMMATE, exact=False).first).to_be_visible(timeout=30_000)
    running = _rail_snapshot(page, rail, evidence, "running")

    # Baseline: Claude's own TUI while the teammate runs.
    _open_terminal_view(page)
    page.wait_for_timeout(2_000)
    page.screenshot(path=str(evidence / "terminal-while-running.png"))
    _ensure_chat_view(page)

    expect(page.locator(_ASSISTANT, has_text=_IDLE_ACK).first).to_be_visible(
        timeout=_SPAWN_IDLE_TIMEOUT_MS
    )
    rail = _open_agents_tab(page)
    idle = _rail_snapshot(page, rail, evidence, "idle")
    items = _dump_items(base_url, session_id, evidence, "items-after-idle")
    (evidence / "observations.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "running": running,
                "idle": idle,
                "teammate_items": _teammate_items(items),
            },
            indent=1,
        )
    )

    # Premise guard: the teammate really ran and reported back into this session.
    assert _teammate_items(items), "no teammate traffic reached the parent session"

    expect(rail.get_by_text(_TEAMMATE, exact=False).first).to_be_visible(
        timeout=_BUG_ASSERT_TIMEOUT_MS
    )
    # Distinguishable from a spawned child session: the row carries a Teammate tag.
    expect(rail.get_by_test_id("subagent-teammate-badge").first).to_be_visible(
        timeout=_BUG_ASSERT_TIMEOUT_MS
    )
    rail.get_by_test_id("view-mode-graph").click()
    expect(
        rail.get_by_test_id("subagent-node-badge").filter(has_text="Teammate").first
    ).to_be_visible(timeout=_BUG_ASSERT_TIMEOUT_MS)
    assert running["list_mentions_teammate"] and running["graph_mentions_teammate"]


@pytest.mark.nightly
@pytest.mark.timeout(900)
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_teammate_reply_renders_readably_without_raw_idle_json(
    request: pytest.FixtureRequest,
    native_claude_teams_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Messaging the idle teammate from the TUI shows its prose reply, never raw idle JSON."""
    base_url, session_id = native_claude_teams_session
    evidence = _evidence_dir(tmp_path, "chat")
    page: Page = request.getfixturevalue("page")
    _boot_and_spawn_teammate(page, base_url, session_id, mock_llm_server_url, teammate_delay_s=3)
    expect(page.locator(_ASSISTANT, has_text=_IDLE_ACK).first).to_be_visible(
        timeout=_SPAWN_IDLE_TIMEOUT_MS
    )
    page.screenshot(path=str(evidence / "chat-after-idle.png"))
    after_idle_text = page.locator("body").inner_text()

    _script_chat_turns(mock_llm_server_url)
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _type_into_tui(page, f"hey {_TEAMMATE}, how is it going? {_RELAY_TOKEN}")
    page.wait_for_timeout(3_000)
    page.screenshot(path=str(evidence / "terminal-after-message.png"))
    _ensure_chat_view(page)

    body = page.locator("body")
    expect(page.locator(_ASSISTANT, has_text=_CHAT_ACK).first).to_be_visible(
        timeout=_CHAT_TIMEOUT_MS
    )
    page.wait_for_timeout(2_000)
    page.screenshot(path=str(evidence / "chat-after-reply.png"))
    live_text = body.inner_text()
    page.reload()
    expect(page.locator(_ASSISTANT, has_text=_CHAT_ACK).first).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )
    page.wait_for_timeout(2_000)
    page.screenshot(path=str(evidence / "chat-after-reload.png"))
    reloaded_text = body.inner_text()
    items = _dump_items(base_url, session_id, evidence, "items-after-chat")
    (evidence / "observations.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "after_idle": {
                    "raw_idle_json": "idle_notification" in after_idle_text,
                    "raw_markup": "<teammate-message" in after_idle_text,
                    "task_reply_visible": _TEAMMATE_REPLY in after_idle_text,
                },
                "after_chat_live": {
                    "raw_idle_json": "idle_notification" in live_text,
                    "raw_markup": "<teammate-message" in live_text,
                    "chat_reply_visible": _TEAMMATE_CHAT_MARKER in live_text,
                    "summary_visible": _TEAMMATE_CHAT_SUMMARY in live_text,
                },
                "after_reload": {
                    "raw_idle_json": "idle_notification" in reloaded_text,
                    "raw_markup": "<teammate-message" in reloaded_text,
                    "chat_reply_visible": _TEAMMATE_CHAT_MARKER in reloaded_text,
                    "summary_visible": _TEAMMATE_CHAT_SUMMARY in reloaded_text,
                },
                "teammate_items": _teammate_items(items),
            },
            indent=1,
        )
    )

    # Premise guard: buddy's answer did reach the parent session.
    assert any(_TEAMMATE_CHAT_MARKER in json.dumps(item) for item in items), (
        "buddy's SendMessage reply never reached the parent session"
    )

    expect(body).not_to_contain_text("idle_notification", timeout=_BUG_ASSERT_TIMEOUT_MS)
    expect(body).not_to_contain_text("<teammate-message", timeout=_BUG_ASSERT_TIMEOUT_MS)
    expect(page.get_by_text(_TEAMMATE_CHAT_MARKER, exact=False).first).to_be_visible(
        timeout=_BUG_ASSERT_TIMEOUT_MS
    )
    card = page.get_by_test_id("teammate-message").filter(has_text=_TEAMMATE_CHAT_MARKER).first
    expect(card).to_contain_text(_TEAMMATE, timeout=_BUG_ASSERT_TIMEOUT_MS)
    expect(card).to_contain_text(_TEAMMATE_CHAT_SUMMARY)
