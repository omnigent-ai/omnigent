"""E2E: set a Claude Code ``/goal`` from the composer and follow its state.

The real ``claude`` CLI runs in the session terminal with the mock LLM playing
both the main model and Claude Code's goal evaluator. The browser sets the goal
through + → Goal, which sends ``/goal <condition>`` into the TUI; the runner
reads the goal Claude Code records in its transcript, and the composer's goal
pill and dialog show it as active, complete, or paused.

Requires the mock LLM lane (``LLM_API_KEY`` unset): the evaluator's verdicts are
scripted, which a real backend cannot guarantee.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    configure_mock_llm,
    native_claude_mock_session_context,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

pytestmark = pytest.mark.skipif(
    bool(os.environ.get("LLM_API_KEY")),
    reason="requires the mock LLM lane: the goal evaluator's verdicts are scripted",
)

# Claude Code's goal checks run until the evaluator reports the condition met.
_GOAL_TIMEOUT_MS = 150_000
# Holds the main turn so the pill is seen while the goal is still active.
_MAIN_TURN_DELAY_S = 8


@pytest.fixture
def claude_goal_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A claude-native session whose Claude Code may run Bash unasked.

    :returns: ``(base_url, session_id)``.
    """
    with native_claude_mock_session_context(
        live_server,
        mock_llm_server_url,
        tmp_path_factory,
        terminal_launch_args=["--allowedTools", "Bash"],
    ) as pair:
        yield pair


def _script_goal(mock_url: str, marker: str, *, met: bool) -> None:
    """Script one main-model reply for *marker* and every evaluator verdict.

    Only Claude Code's main loop offers ``Bash``, so the scripted reply cannot
    be consumed by the evaluator; the evaluator and any other side request get
    the fallback verdict.
    """
    reset_mock_llm(mock_url)
    verdict = (
        {"ok": True, "reason": "The test run shows 12 passed and 0 failed."}
        if met
        else {"ok": False, "reason": "Two tests still fail."}
    )
    for key in ("default", _CLAUDE_MOCK_MODEL):
        set_fallback_mock_llm(mock_url, key, json.dumps(verdict))
    configure_mock_llm(
        mock_url,
        [{"text": "Working on it: the tests are running.", "delay": _MAIN_TURN_DELAY_S}],
        match=marker,
        required_tools=["Bash"],
    )


def _hold_for_recording(page: Page, milliseconds: int) -> None:
    """Pause long enough for a recorded step to be readable; no-op otherwise."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(milliseconds)


def _open_session(page: Page, base_url: str, session_id: str) -> None:
    """Open the session once its Claude Code TUI is attached, on the chat view."""
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)


def _start_goal_from_composer(page: Page, condition: str) -> None:
    page.get_by_test_id("composer-attach").click()
    page.get_by_test_id("composer-goal-action").click()
    expect(page.get_by_text("Claude keeps working until this condition is met")).to_be_visible()
    expect(page.get_by_test_id("goal-current")).to_have_count(0)
    _hold_for_recording(page, 2000)
    page.get_by_test_id("goal-condition").fill(condition)
    _hold_for_recording(page, 2000)
    page.get_by_test_id("goal-start").click()


def _condition(marker: str) -> str:
    return f"All tests pass ({marker})"


@pytest.mark.timeout(420)
def test_goal_set_from_the_composer_shows_active_then_complete(
    request: pytest.FixtureRequest,
    claude_goal_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = claude_goal_session
    marker = f"goal{uuid.uuid4().hex[:8]}"
    _script_goal(mock_llm_server_url, marker, met=True)
    page: Page = request.getfixturevalue("page")
    _open_session(page, base_url, session_id)

    _start_goal_from_composer(page, _condition(marker))

    pill = page.get_by_test_id("composer-goal-mode")
    expect(pill).to_have_attribute("data-state", "working", timeout=_GOAL_TIMEOUT_MS)
    expect(pill).to_have_attribute("aria-label", f"Goal active: {_condition(marker)}")
    pill.hover()
    expect(page.get_by_role("tooltip")).to_contain_text(_condition(marker))
    _hold_for_recording(page, 1500)

    expect(pill).to_have_attribute("data-state", "done", timeout=_GOAL_TIMEOUT_MS)
    expect(pill).to_have_attribute("aria-label", f"Goal complete: {_condition(marker)}")
    pill.click()
    current = page.get_by_test_id("goal-current")
    expect(current).to_contain_text("Complete")
    expect(current).to_contain_text(_condition(marker))
    expect(page.get_by_test_id("goal-status-detail")).to_contain_text("12 passed")
    _hold_for_recording(page, 2500)


@pytest.mark.timeout(420)
def test_a_goal_paused_by_its_checks_is_marked_paused_and_can_be_cleared(
    request: pytest.FixtureRequest,
    claude_goal_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = claude_goal_session
    marker = f"goal{uuid.uuid4().hex[:8]}"
    _script_goal(mock_llm_server_url, marker, met=False)
    page: Page = request.getfixturevalue("page")
    _open_session(page, base_url, session_id)

    _start_goal_from_composer(page, _condition(marker))

    pill = page.get_by_test_id("composer-goal-mode")
    expect(pill).to_have_attribute("data-state", "working", timeout=_GOAL_TIMEOUT_MS)
    # Claude Code pauses the goal once its checks keep finding it unmet in one turn.
    expect(pill).to_have_attribute("data-state", "paused", timeout=_GOAL_TIMEOUT_MS)
    expect(page.get_by_test_id("composer-goal-mode-label")).to_have_text("Paused")
    _hold_for_recording(page, 1500)

    pill.click()
    current = page.get_by_test_id("goal-current")
    expect(current).to_contain_text("Paused")
    expect(current).to_contain_text(_condition(marker))
    expect(current).to_contain_text("Send a message to continue.")
    expect(page.get_by_test_id("goal-start")).to_have_text("Replace goal")
    _hold_for_recording(page, 2000)

    page.get_by_test_id("goal-clear").click()
    expect(pill).to_have_count(0, timeout=_GOAL_TIMEOUT_MS)
    _hold_for_recording(page, 1500)
