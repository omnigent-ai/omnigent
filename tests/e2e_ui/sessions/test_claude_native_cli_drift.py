"""A real Claude native session must keep the web UI in step with the CLI.

Each test drives the real Claude Code TUI through the web UI against a scripted
mock model and checks what the web UI shows while background work runs.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from omnigent.harnesses.claude_native.bridge import BRIDGE_ID_LABEL_KEY
from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from tests.e2e_ui.chat.test_claude_native_permission_switch_from_dont_ask import (
    _render_terminal,
    _terminal_id,
)
from tests.e2e_ui.conftest import (
    configure_mock_llm,
    native_claude_mock_session_context,
    open_right_rail,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _select_view_mode


def _require_real_claude() -> None:
    for binary in ("claude", "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} executable")
    if any(path.is_file() for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS):
        pytest.skip("machine-managed Claude settings override mock auth")


def _scripted_fallbacks(mock_url: str) -> None:
    set_fallback_mock_llm(mock_url, key="default", text="Acknowledged.")
    set_fallback_mock_llm(mock_url, key="_policy_llm_", text='{"action":"allow","reason":""}')


@contextmanager
def _session_page(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Page]]:
    """Yield a skip-permissions Claude session and a page closed before it is deleted."""
    with native_claude_mock_session_context(
        live_server,
        mock_url,
        tmp_path_factory,
        terminal_launch_args=["--dangerously-skip-permissions"],
    ) as (base_url, session_id):
        browser = request.getfixturevalue("browser")
        context = browser.new_context(**request.getfixturevalue("browser_context_args"))
        try:
            yield base_url, session_id, context.new_page()
        finally:
            context.close()


def _open_chat(page: Page, base_url: str, session_id: str) -> Locator:
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_test_id("view-mode-chat").click(timeout=90_000)
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_enabled(timeout=120_000)
    return composer


def _send(page: Page, composer: Locator, text: str) -> None:
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _session(base_url: str, session_id: str) -> dict:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return response.json()


def _children(base_url: str, parent_id: str) -> list[dict]:
    response = httpx.get(f"{base_url}/v1/sessions/{parent_id}/child_sessions", timeout=10.0)
    response.raise_for_status()
    return response.json()["data"]


def _wait_for(predicate: Callable[[], object], timeout: float, what: str) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


def _open_agents_tab(page: Page) -> Locator:
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    return rail


def _agents_row(rail: Locator, child_id: str) -> Locator:
    row = rail.locator(f'[data-testid="subagent-row"][data-child-session-id="{child_id}"]')
    expect(row).to_be_visible(timeout=30_000)
    return row


def _open_terminal(page: Page) -> Locator:
    _select_view_mode(page, "Terminal")
    expect(page.locator('[data-testid="terminal-view"]').last).to_have_attribute(
        "data-state", "connected", timeout=120_000
    )
    tui_input = page.locator(".xterm-helper-textarea").last
    tui_input.focus()
    return tui_input


def _type_into_cli(page: Page, text: str) -> None:
    _open_terminal(page)
    page.keyboard.type(text, delay=15)
    page.keyboard.press("Enter")


def _cli_screen(base_url: str, session_id: str) -> str:
    terminal_id = _terminal_id(base_url, session_id)
    assert terminal_id is not None, "the session has no Claude terminal"
    screen = _render_terminal(base_url, session_id, terminal_id)
    return "\n".join(line.rstrip() for line in screen.splitlines()).strip()


def _assistant_reply(page: Page, text: str) -> Locator:
    return page.locator('[data-testid="message-bubble"][data-role="assistant"]').filter(
        has_text=text
    )


def _report(label: str, payload: object) -> None:
    print(f"{label}: {json.dumps(payload, indent=2, default=str)}")


class _MockRequestLog:
    """Incrementally serialized mock-model request bodies."""

    def __init__(self, mock_url: str) -> None:
        self._mock_url = mock_url
        self.bodies: list[str] = []

    def refresh(self) -> list[str]:
        response = httpx.get(f"{self._mock_url}/mock/requests", timeout=10.0)
        captured = response.json()["requests"]
        self.bodies.extend(json.dumps(body) for body in captured[len(self.bodies) :])
        return self.bodies

    def contains(self, text: str) -> bool:
        return any(text in body for body in self.bodies)


def _agent_call(call_id: str, prompt: str, *, background: bool) -> dict:
    arguments = {
        "subagent_type": "general-purpose",
        "description": "Run slow check",
        "prompt": prompt,
    }
    if background:
        arguments["run_in_background"] = True
    return {
        "tool_calls": [{"call_id": call_id, "name": "Agent", "arguments": json.dumps(arguments)}]
    }


def _bash_call(call_id: str, arguments: dict) -> dict:
    return {
        "tool_calls": [{"call_id": call_id, "name": "Bash", "arguments": json.dumps(arguments)}]
    }


_LULL_PARENT = "CLI_DRIFT_LULL_PARENT: delegate the slow check to a general-purpose agent."
_LULL_CHILD = (
    "CLI_DRIFT_LULL_CHILD_TASK: run the slow twenty second check in the shell and report."
)
_LULL_SLEEP_RESULT = '"tool_use_id": "toolu_cli_drift_lull_sleep"'
# Longer than the forwarder's 5 s transcript-lull threshold, shorter than the sleep.
_LULL_DRIFT_HOLD_S = 6


@pytest.mark.timeout(420)
def test_subagent_in_long_tool_call_stays_working(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    claude_managed_settings: None,
) -> None:
    """A sub-agent waiting on a long Bash call must not be shown as finished."""
    _require_real_claude()
    mock_url = mock_llm_server_url
    reset_mock_llm(mock_url)
    configure_mock_llm(
        mock_url,
        [
            _agent_call("toolu_cli_drift_lull_child", _LULL_CHILD, background=False),
            {"text": "The slow check is running in a sub-agent."},
        ],
        match=_LULL_PARENT,
        required_tools=["Agent"],
    )
    configure_mock_llm(
        mock_url,
        [
            _bash_call(
                "toolu_cli_drift_lull_sleep",
                {"command": "sleep 20; echo slow-check-ok", "description": "Slow check"},
            ),
            {"text": "Slow check finished after the wait."},
        ],
        match=_LULL_CHILD,
        required_tools=["Bash"],
    )
    _scripted_fallbacks(mock_url)

    with _session_page(request, live_server, mock_url, tmp_path_factory) as (
        base_url,
        parent_id,
        page,
    ):
        composer = _open_chat(page, base_url, parent_id)
        rail = _open_agents_tab(page)
        requests_log = _MockRequestLog(mock_url)
        _send(page, composer, _LULL_PARENT)
        sent_at = time.time()
        child = _wait_for(
            lambda: next(iter(_children(base_url, parent_id)), None),
            timeout=120,
            what="the Claude sub-agent child session",
        )
        child_id = child["id"]
        avatar = _agents_row(rail, child_id).get_by_test_id("subagent-status-avatar")

        samples: list[dict] = []
        bash_served_at: float | None = None
        drift_since: float | None = None
        terminal_open = False
        cli_screen = ""
        while time.time() - sent_at < 150:
            requests_log.refresh()
            snapshot = next(c for c in _children(base_url, parent_id) if c["id"] == child_id)
            activity = avatar.get_attribute("data-activity")
            now = time.time()
            if bash_served_at is None and requests_log.contains(_LULL_CHILD):
                bash_served_at = now
            sleeping = bash_served_at is not None and not requests_log.contains(_LULL_SLEEP_RESULT)
            samples.append(
                {
                    "since_send_s": round(now - sent_at, 2),
                    "child_bash_call_served": bash_served_at is not None,
                    "sleep_result_returned": requests_log.contains(_LULL_SLEEP_RESULT),
                    "child_busy": snapshot.get("busy"),
                    "child_task_status": snapshot.get("current_task_status"),
                    "ui_activity": activity,
                }
            )
            if bash_served_at is not None and not sleeping:
                break
            drift_since = (drift_since or now) if sleeping and activity != "working" else None
            if drift_since is not None and not terminal_open and now - drift_since >= 2:
                _open_terminal(page)
                terminal_open = True
            if drift_since is not None and now - drift_since >= _LULL_DRIFT_HOLD_S:
                cli_screen = _cli_screen(base_url, parent_id)
                break
            time.sleep(0.25)

        _report("lull", {"parent_id": parent_id, "child_id": child_id, "samples": samples})
        assert bash_served_at is not None, "the sub-agent never started its Bash call"
        assert drift_since is None, (
            f"the sub-agent row showed {samples[-1]['ui_activity']!r} for "
            f"{_LULL_DRIFT_HOLD_S}s while its `sleep 20` was still running; CLI:\n{cli_screen}"
        )


_SHELL_RESULT = '"tool_use_id": "toolu_cli_drift_background_shell"'
_SHELL_OBSERVE_S = 20


@pytest.mark.timeout(420)
@pytest.mark.parametrize("interrupted", [False, True], ids=["turn-completes", "esc-interrupt"])
def test_background_shell_count_after_turn_ends(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    claude_managed_settings: None,
    interrupted: bool,
) -> None:
    """A background shell still running after the turn ends keeps its composer count."""
    _require_real_claude()
    mock_url = mock_llm_server_url
    prompt = (
        "CLI_DRIFT_BACKGROUND_SHELL_INTERRUPTED: start the long job, then keep working."
        if interrupted
        else "CLI_DRIFT_BACKGROUND_SHELL_COMPLETED: start the long job and finish."
    )
    continuation = {"text": "The long job is running in the background."}
    if interrupted:
        continuation["delay"] = 90
    reset_mock_llm(mock_url)
    configure_mock_llm(
        mock_url,
        [
            _bash_call(
                "toolu_cli_drift_background_shell",
                {
                    "command": "sleep 120",
                    "description": "Long background job",
                    "run_in_background": True,
                },
            ),
            continuation,
        ],
        match=prompt,
        required_tools=["Bash"],
    )
    _scripted_fallbacks(mock_url)

    with _session_page(request, live_server, mock_url, tmp_path_factory) as (
        base_url,
        session_id,
        page,
    ):
        composer = _open_chat(page, base_url, session_id)
        requests_log = _MockRequestLog(mock_url)
        _send(page, composer, prompt)
        _wait_for(
            lambda: requests_log.refresh() and requests_log.contains(_SHELL_RESULT),
            timeout=120,
            what="the background shell to start",
        )
        if interrupted:
            _open_terminal(page)
            page.keyboard.press("Escape")
            time.sleep(2)
            cli_screen = _cli_screen(base_url, session_id)
            _select_view_mode(page, "Chat")
        else:
            expect(_assistant_reply(page, continuation["text"])).to_be_visible(timeout=60_000)
            cli_screen = _cli_screen(base_url, session_id)
        turn_ended_at = time.time()

        pill = page.get_by_test_id("background-task-pill")
        samples: list[dict] = []
        while time.time() - turn_ended_at < _SHELL_OBSERVE_S:
            snapshot = _session(base_url, session_id)
            samples.append(
                {
                    "since_turn_end_s": round(time.time() - turn_ended_at, 2),
                    "status": snapshot.get("status"),
                    "background_task_count": snapshot.get("background_task_count"),
                    "pill": pill.get_attribute("aria-label") if pill.count() else None,
                }
            )
            if samples[-1]["pill"]:
                break
            time.sleep(0.5)

        _report("background-shell", {"session_id": session_id, "samples": samples})
        assert samples[-1]["pill"] == "1 background task still running", (
            f"no background-task count {_SHELL_OBSERVE_S}s after the turn ended while "
            f"`sleep 120` still runs; CLI:\n{cli_screen}"
        )


_CLEAR_PARENT = "CLI_DRIFT_CLEAR_PARENT: start the background check agent."
_CLEAR_CHILD = "CLI_DRIFT_CLEAR_CHILD_TASK: run the twenty second background check and report."
_CLEAR_REPLY = "Started the background check before clearing."
# The child's sleep ends ~20 s after it starts; give it a further 60 s to settle.
_CLEAR_SETTLE_S = 90


def _session_ids(base_url: str) -> set[str]:
    response = httpx.get(f"{base_url}/v1/sessions", params={"limit": 50}, timeout=10.0)
    response.raise_for_status()
    return {session["id"] for session in response.json()["data"]}


def _rotated_session(base_url: str, existing: set[str], agent_id: str) -> str | None:
    response = httpx.get(f"{base_url}/v1/sessions", params={"limit": 50}, timeout=10.0)
    response.raise_for_status()
    for session in response.json()["data"]:
        labels = session.get("labels") or {}
        if (
            session["id"] not in existing
            and session.get("agent_id") == agent_id
            and BRIDGE_ID_LABEL_KEY in labels
        ):
            return session["id"]
    return None


@pytest.mark.timeout(480)
def test_background_subagent_settles_after_clear(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    claude_managed_settings: None,
) -> None:
    """A background sub-agent that outlives /clear settles in the cleared chat."""
    _require_real_claude()
    mock_url = mock_llm_server_url
    reset_mock_llm(mock_url)
    configure_mock_llm(
        mock_url,
        [
            _agent_call("toolu_cli_drift_clear_child", _CLEAR_CHILD, background=True),
            {"text": _CLEAR_REPLY},
        ],
        match=_CLEAR_PARENT,
        required_tools=["Agent"],
    )
    configure_mock_llm(
        mock_url,
        [
            _bash_call(
                "toolu_cli_drift_clear_sleep",
                {"command": "sleep 20; echo ok", "description": "Background check"},
            ),
            {"text": "The background check passed."},
        ],
        match=_CLEAR_CHILD,
        required_tools=["Bash"],
    )
    _scripted_fallbacks(mock_url)

    with _session_page(request, live_server, mock_url, tmp_path_factory) as (
        base_url,
        old_id,
        page,
    ):
        composer = _open_chat(page, base_url, old_id)
        _send(page, composer, _CLEAR_PARENT)
        expect(_assistant_reply(page, _CLEAR_REPLY)).to_be_visible(timeout=120_000)
        child = _wait_for(
            lambda: next(iter(_children(base_url, old_id)), None),
            timeout=60,
            what="the background sub-agent child session",
        )
        child_id = child["id"]
        agent_id = _session(base_url, old_id)["agent_id"]
        existing = _session_ids(base_url)
        _type_into_cli(page, "/clear")
        cleared_at = time.time()
        new_id = _wait_for(
            lambda: _rotated_session(base_url, existing, agent_id),
            timeout=60,
            what="the session /clear rotates to",
        )
        rotated_url = re.compile(rf"/c/{re.escape(new_id)}")
        try:
            expect(page).to_have_url(rotated_url, timeout=30_000)
            ui_followed_clear = True
        except AssertionError:
            ui_followed_clear = False

        page.goto(f"{base_url}/c/{old_id}")
        page.get_by_test_id("view-mode-chat").click(timeout=90_000)
        avatar = _agents_row(_open_agents_tab(page), child_id).get_by_test_id(
            "subagent-status-avatar"
        )
        samples: list[dict] = []
        while time.time() - cleared_at < _CLEAR_SETTLE_S:
            snapshot = next(c for c in _children(base_url, old_id) if c["id"] == child_id)
            samples.append(
                {
                    "since_clear_s": round(time.time() - cleared_at, 2),
                    "old_status": _session(base_url, old_id).get("status"),
                    "child_status": _session(base_url, child_id).get("status"),
                    "child_busy": snapshot.get("busy"),
                    "child_task_status": snapshot.get("current_task_status"),
                    "ui_activity": avatar.get_attribute("data-activity"),
                    "working_indicator": page.get_by_test_id("working-indicator").count() > 0,
                }
            )
            time.sleep(1)
        cli_screen = _cli_screen(base_url, new_id)

        _report(
            "clear",
            {
                "old_id": old_id,
                "new_id": new_id,
                "child_id": child_id,
                "ui_followed_clear": ui_followed_clear,
                "clear_notice_shown": page.get_by_text("ended by").count() > 0,
                "samples": samples,
            },
        )
        final = samples[-1]
        assert final["ui_activity"] != "working" and final["old_status"] == "idle", (
            f"{_CLEAR_SETTLE_S}s after /clear the cleared chat still shows its background "
            f"sub-agent as {final['ui_activity']!r} and the chat as {final['old_status']!r}; "
            f"CLI:\n{cli_screen}"
        )
