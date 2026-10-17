"""E2E (phone): the background-task tally must show whether the agent is working.

On a phone the end-of-thread "Working…" shimmer is the first thing to leave the
viewport (scrolling up to re-read, the keyboard opening), and a typed draft keeps
the composer's Send arrow instead of its Stop square. Once a background shell
outlives a turn, the ``N background task`` tally in the composer workspace bar is
the only persistent status surface at the bottom of the screen, so it must look
and read differently while a turn runs than while the session is idle.

Journeys (the reporter's, claude-native on a phone):

1. ask Claude Code to run a shell in the background and let the turn finish ->
   the ``1 background task`` tally appears above the composer,
2. type a follow-up draft (not sent) and scroll up to re-read earlier messages,
   so the shimmer is off screen -> a new turn starts on the session (sent from a
   second client, which keeps the draft) -> the tally must carry the working
   state visually and in its accessible name, and revert once the turn settles,
3. the literal single-device step: send the follow-up from the phone's own
   composer -> the tally must carry the working state while the agent works.

Harness notes:

- The real ``claude`` CLI runs against the mock model. The scripted model issues
  a ``Bash`` ``run_in_background`` call; Claude Code runs the shell and its own
  Stop hook reports the background task, which the runner's forwarder publishes
  as the tally. No status edge is injected.
- ``--allowedTools Bash(sleep:*)`` pre-allows the scripted ``sleep`` so the
  shell runs without an approval pause.
- The phone is desktop Chromium at Playwright's "iPhone 13" profile (390x664),
  so a recorder run with ``--device "iPhone 13"`` films pixel-exact.
- The held follow-up is released only once the CLI's model request is actually
  waiting on the mock's gate; a release with nothing pending is a no-op.
- Skips when the ``claude`` CLI is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests._helpers.native_session import create_native_session
from tests._helpers.session import bind_session_runner
from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.helpers.ui_configuration import _CLAUDE_MOCK_MODEL

_PHONE = pytest.mark.browser_context_args(
    viewport={"width": 390, "height": 664},
    device_scale_factor=3,
    is_mobile=True,
    has_touch=True,
)
_DESKTOP_VIEWPORT = {"width": 1280, "height": 800}

_WORKING = '[data-testid="working-indicator"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_TALLY = '[data-testid="background-task-pill"]'
_TASK_INDICATORS = '[data-testid="composer-task-indicators"]'
_WORKSPACE_BAR = '[data-testid="composer-workspace-controls"]'
_TERMINAL_VIEW = '[data-testid="terminal-view"]'

_BACKGROUND_COMMAND = "sleep 600"
_BACKGROUND_DESCRIPTION = "Keep a long sleep running in the background"
_DRAFT = "Also summarize the sleep's output once it finishes"
_REPLY_MARKER = "background-sleep-started"
# Long enough to overflow a 664px-tall phone viewport on its own.
_LONG_REPLY = "\n\n".join(
    [f"The sleep is running in the background ({_REPLY_MARKER})."]
    + [
        f"Paragraph {i}: while it runs I can keep answering questions; the shell "
        "keeps going until it exits or you stop it."
        for i in range(1, 13)
    ]
)
_FOLLOW_UP_REPLY = "follow-up answered while the sleep keeps running"

# claude-native auto-launch + first-run pre-accept + WS attach.
_TERMINAL_READY_TIMEOUT_MS = 120_000
# A mock turn: CLI round trip through the bridge + hook + forwarder.
_TURN_TIMEOUT_MS = 120_000
_MENU_OPEN_TIMEOUT_S = 30.0
_SCROLL_SETTLE_TIMEOUT_S = 20.0
# The CLI's model request reaches the mock seconds after the UI shows the turn.
_GATE_PENDING_TIMEOUT_S = 90.0


@pytest.fixture
def native_claude_background_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound claude-native session allowed to run the scripted background shell.

    :param live_server: Spawned server fixture; its runner is reused.
    :param mock_llm_server_url: Session-scoped mock LLM server base URL.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    if shutil.which("claude") is None:
        pytest.skip("claude CLI is required for the claude-native background-task journey")
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    with _temp_omnigent_mock_config(
        mock_llm_server_url, "claude", workflow_owned=bool(_server_state.get("workflow_owned"))
    ):
        created = create_native_session(
            httpx,
            live_server,
            harness="claude",
            metadata={"terminal_launch_args": ["--allowedTools", "Bash(sleep:*)"]},
        )
        session_id = str(created["session_id"])
        bind_session_runner(httpx.patch, live_server, session_id, runner_id, timeout=10.0)
        try:
            yield (live_server, session_id)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


def _evidence_dir(request: pytest.FixtureRequest) -> Path:
    """Per-test directory for the captured bottom-chrome states."""
    root = Path(str(request.config.getoption("--output"))) / "background-task-working-cue"
    path = root / request.node.name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _select_view(page: Page, option: str) -> None:
    """Switch to the Chat or Terminal view.

    A phone header folds the switch into its single "…" menu; a desktop header
    shows the segmented ``view-mode-toggle`` instead. A terminal that has just
    connected grabs keyboard focus, which dismisses the phone's non-modal menu
    if it lands right after the tap; a user simply taps the menu again.
    """
    kebab = page.get_by_test_id("header-conversation-actions").or_(
        page.get_by_test_id("session-actions-menu")
    )
    segment = page.get_by_test_id(f"view-mode-{option}")
    expect(kebab.or_(segment).first).to_be_visible(timeout=_TERMINAL_READY_TIMEOUT_MS)
    if segment.count() > 0:
        expect(segment).to_be_enabled(timeout=30_000)
        segment.click()
        return
    item = page.get_by_test_id(f"view-mode-menu-{option}")
    deadline = time.monotonic() + _MENU_OPEN_TIMEOUT_S
    while True:
        kebab.click()
        try:
            expect(item).to_be_visible(timeout=3_000)
            break
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
    item.click()


def _wait_for_claude_tui(page: Page) -> None:
    """Wait for the live Claude Code TUI to attach, then return to the Chat view."""
    _select_view(page, "terminal")
    expect(page.locator(_TERMINAL_VIEW).last).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    _select_view(page, "chat")


def _tally(page: Page) -> Locator:
    return page.locator(_TALLY)


def _start_background_shell(page: Page, mock_url: str) -> None:
    """Ask Claude Code to run a shell in the background and wait for the tally."""
    go_token = f"bg-shell-{uuid.uuid4().hex[:6]}"
    bash_args = json.dumps(
        {
            "command": _BACKGROUND_COMMAND,
            "run_in_background": True,
            "description": _BACKGROUND_DESCRIPTION,
        }
    )
    configure_mock_llm(
        mock_url,
        [
            {"tool_calls": [{"name": "Bash", "arguments": bash_args}]},
            {"text": _LONG_REPLY},
        ],
        key="bg-cue-shell",
        match=go_token,
        required_tools=["Bash"],
    )
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(
        f"Run `{_BACKGROUND_COMMAND}` in the background and tell me once it started {go_token}"
    )
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT).filter(has_text=_REPLY_MARKER).first).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )
    expect(_tally(page)).to_have_text("1", timeout=_TURN_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)


def _hold_follow_up(mock_url: str) -> str:
    """Script the next turn's reply to stay open until the gate is released."""
    follow_token = f"bg-cue-follow-{uuid.uuid4().hex[:6]}"
    configure_mock_llm(
        mock_url,
        [{"text": _FOLLOW_UP_REPLY, "block": True}],
        key="bg-cue-follow-up",
        match=follow_token,
        required_tools=["Bash"],
    )
    return follow_token


def _release_follow_up(mock_url: str) -> None:
    """Let the held reply finish once the CLI's request is waiting on the gate."""
    deadline = time.monotonic() + _GATE_PENDING_TIMEOUT_S
    while time.monotonic() < deadline:
        if httpx.get(f"{mock_url}/gate/pending", timeout=5.0).json().get("pending"):
            break
        time.sleep(0.25)
    httpx.post(f"{mock_url}/gate/release", timeout=5.0)


# The tallest scrollable descendant of the transcript (role="log") is the
# StickToBottom viewport; tag it so later evaluations can address it.
_TAG_SCROLLER = """
() => {
  const log = document.querySelector('[role="log"]');
  let best = null;
  log.querySelectorAll('*').forEach((el) => {
    if (el.scrollHeight > el.clientHeight + 4) {
      if (!best || el.scrollHeight > best.scrollHeight) best = el;
    }
  });
  const el = best || log;
  el.setAttribute('data-pw-scroller', '1');
  return { scrollHeight: el.scrollHeight, clientHeight: el.clientHeight };
}
"""
_SCROLL_TO_TOP = """
() => {
  const el = document.querySelector('[data-pw-scroller]');
  el.scrollTop = 0;
  return el.scrollTop;
}
"""


_SCROLLER_TOP = "() => document.querySelector('[data-pw-scroller]').scrollTop"


def _scroll_to_top(page: Page) -> None:
    """Scroll the transcript back to its first message (re-reading earlier turns).

    Right after a turn the transcript may still pin itself to the bottom (a
    late-committed item, a settling row measurement), so keep scrolling up
    until it rests at the top, the way a reader would.
    """
    size = page.evaluate(_TAG_SCROLLER)
    assert size["scrollHeight"] > size["clientHeight"] + 50, (
        f"the thread does not overflow the phone viewport: {size}"
    )
    deadline = time.monotonic() + _SCROLL_SETTLE_TIMEOUT_S
    while True:
        page.evaluate(_SCROLL_TO_TOP)
        page.wait_for_timeout(250)
        if page.evaluate(_SCROLLER_TOP) <= 2:
            return
        assert time.monotonic() < deadline, "the transcript kept leaving its top"


def _in_viewport(locator: Locator) -> bool | None:
    if locator.count() == 0:
        return None
    box = locator.first.bounding_box()
    if box is None:
        return False
    viewport = locator.page.viewport_size
    assert viewport is not None
    return box["y"] + box["height"] > 0 and box["y"] < viewport["height"]


def _capture_bottom_chrome(page: Page, evidence_dir: Path, label: str) -> dict[str, Any]:
    """Record what the bottom of the phone screen shows: tally, composer and pixels."""
    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    page.wait_for_timeout(300)
    bar = page.locator(_WORKSPACE_BAR)
    tally = _tally(page)
    composer = page.get_by_label("Message the agent")
    viewport = page.viewport_size
    assert viewport is not None
    bar_box = bar.bounding_box()
    tally_box = tally.bounding_box()
    assert bar_box is not None and tally_box is not None
    bottom_png = page.screenshot(
        clip={
            "x": 0,
            "y": bar_box["y"],
            "width": viewport["width"],
            "height": viewport["height"] - bar_box["y"],
        }
    )
    tally_png = page.screenshot(clip=tally_box)
    (evidence_dir / f"bottom-{label}.png").write_bytes(bottom_png)
    (evidence_dir / f"tally-{label}.png").write_bytes(tally_png)
    state = {
        "tally_name": tally.get_attribute("aria-label"),
        "tally_text": tally.inner_text(),
        "indicators_html": page.locator(_TASK_INDICATORS).inner_html(),
        "indicators_aria": page.locator(_TASK_INDICATORS).aria_snapshot(),
        "status_texts": [
            text.strip()
            for text in page.get_by_role("status").all_text_contents()
            if "background task" in text
        ],
        "composer_value": composer.input_value(),
        "composer_placeholder": composer.get_attribute("placeholder"),
        "send_button_visible": page.get_by_role("button", name="Send", exact=True).is_visible(),
        "interrupt_button_count": page.get_by_role("button", name="Interrupt", exact=True).count(),
        "shimmer_attached": page.locator(_WORKING).count() > 0,
        "shimmer_in_viewport": _in_viewport(page.locator(_WORKING)),
        "bottom_png_sha256": hashlib.sha256(bottom_png).hexdigest(),
        "tally_png_sha256": hashlib.sha256(tally_png).hexdigest(),
    }
    (evidence_dir / f"state-{label}.json").write_text(json.dumps(state, indent=2) + "\n")
    return state


def _assert_tally_carries_working_state(idle: dict[str, Any], working: dict[str, Any]) -> None:
    """The tally must look and read differently while the agent works than while idle."""
    failures: list[str] = []
    if (
        working["indicators_html"] == idle["indicators_html"]
        and working["tally_png_sha256"] == idle["tally_png_sha256"]
    ):
        failures.append(
            "the tally renders exactly its idle presentation while the agent works "
            f"(text {working['tally_text']!r}, same markup and pixels)"
        )
    if working["tally_name"] == idle["tally_name"]:
        failures.append(
            f"the tally's accessible name is {working['tally_name']!r} both while the agent "
            "works and while idle"
        )
    assert not failures, "no working cue at the bottom of the phone screen:\n- " + "\n- ".join(
        failures
    )


@_PHONE
@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_tally_shows_working_state_while_shimmer_is_off_screen(
    request: pytest.FixtureRequest,
    native_claude_background_session: tuple[str, str],
    mock_llm_server_url: str,
    browser: Browser,
) -> None:
    """With a draft typed and the shimmer scrolled away, a new turn must show on the tally."""
    base_url, session_id = native_claude_background_session
    evidence_dir = _evidence_dir(request)
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", "ok")
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "ok")

    page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _wait_for_claude_tui(page)
    _start_background_shell(page, mock_llm_server_url)

    composer = page.get_by_label("Message the agent")
    composer.fill(_DRAFT)
    _scroll_to_top(page)
    idle = _capture_bottom_chrome(page, evidence_dir, "idle")

    follow_token = _hold_follow_up(mock_llm_server_url)
    second_client = browser.new_context(viewport=_DESKTOP_VIEWPORT)
    try:
        other = second_client.new_page()
        other.goto(f"{base_url}/c/{session_id}")
        other_composer = other.get_by_label("Message the agent")
        expect(other_composer).to_be_visible(timeout=30_000)
        other_composer.fill(f"How is the sleep doing? {follow_token}")
        other.get_by_role("button", name="Send", exact=True).click()

        shimmer = page.locator(_WORKING)
        expect(shimmer).to_be_attached(timeout=_TURN_TIMEOUT_MS)
        if _in_viewport(shimmer):
            _scroll_to_top(page)
        expect(shimmer).not_to_be_in_viewport()
        expect(composer).to_have_value(_DRAFT)
        working = _capture_bottom_chrome(page, evidence_dir, "working")
        _assert_tally_carries_working_state(idle, working)
    finally:
        _release_follow_up(mock_llm_server_url)
        second_client.close()

    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)
    expect(_tally(page)).to_have_text("1")
    settled = _capture_bottom_chrome(page, evidence_dir, "settled")
    assert settled["tally_name"] == idle["tally_name"], "the tally did not revert once idle"


@_PHONE
@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_tally_shows_working_state_for_follow_up_sent_from_the_phone(
    request: pytest.FixtureRequest,
    native_claude_background_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """The report's single-device step: send the follow-up from the phone and watch the tally."""
    base_url, session_id = native_claude_background_session
    evidence_dir = _evidence_dir(request)
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", "ok")
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "ok")

    page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _wait_for_claude_tui(page)
    _start_background_shell(page, mock_llm_server_url)
    idle = _capture_bottom_chrome(page, evidence_dir, "idle")

    follow_token = _hold_follow_up(mock_llm_server_url)
    composer = page.get_by_label("Message the agent")
    composer.fill(f"How is the sleep doing? {follow_token}")
    page.get_by_role("button", name="Send", exact=True).click()
    try:
        expect(page.locator(_WORKING)).to_be_attached(timeout=_TURN_TIMEOUT_MS)
        working = _capture_bottom_chrome(page, evidence_dir, "working")
        _assert_tally_carries_working_state(idle, working)
    finally:
        _release_follow_up(mock_llm_server_url)

    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)
    expect(_tally(page)).to_have_text("1")
