"""A Claude follow-up that leaves a background agent running still ends its turn."""

from __future__ import annotations

import contextlib
import json
import shlex
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.polling import poll_until
from tests.e2e.conftest import get_mock_requests
from tests.e2e_ui import conftest as ui_fixtures
from tests.e2e_ui.conftest import configure_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_FIRST_TURN_TIMEOUT_MS = 180_000
_SETTLE_TIMEOUT_MS = 30_000
_MOCK_REQUEST_TIMEOUT_S = 30.0
_IDLE_HOLD_S = 3.0
_CONTINUATION_DELAY_S = 10

# Blocks the first main-session Stop that leaves an agent running; Claude then continues the turn.
_BLOCK_ONCE_STOP_HOOK = """
import json, pathlib, sys
payload = json.load(sys.stdin)
marker = pathlib.Path({marker!r})
agent_running = any(
    isinstance(task, dict) and task.get("type") in {{"agent", "local_agent", "subagent"}}
    and task.get("status") not in {{"completed", "failed", "killed", "stopped"}}
    for task in payload.get("background_tasks") or []
)
if agent_running and not payload.get("stop_hook_active") and not payload.get("agent_id"):
    if not marker.exists():
        marker.write_text("blocked")
        print(json.dumps({{"decision": "block", "reason": {reason!r}}}))
"""


def _user_text_seen(mock_llm_server_url: str, token: str) -> bool:
    """Return whether any captured model request carries *token* in user content."""
    for captured in get_mock_requests(mock_llm_server_url):
        for message in captured.get("messages") or []:
            if message.get("role") == "user" and token in json.dumps(message.get("content")):
                return True
    return False


def _release_all_gates(mock_llm_server_url: str) -> None:
    with contextlib.suppress(httpx.HTTPError):
        while (
            httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0).json().get("released")
        ):
            pass


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    composer.press("Enter")


def _configure_background_agent_launch(mock_llm_server_url: str, nonce: str) -> None:
    """Script a first turn, then a turn that launches a held background agent."""
    # Later requests repeat earlier prompts, so each newer routing token is longer.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"first-reply-{nonce}"}],
        match=f"t1-{nonce}",
        required_tools=["Bash"],
    )
    worker_token = f"worker-{nonce}-background-task"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"worker-result-{nonce}", "block": True}],
        match=worker_token,
    )
    agent_arguments = {
        "description": "Background worker",
        "prompt": f"{worker_token}: do the work, then report back.",
        "subagent_type": "general-purpose",
        "run_in_background": True,
    }
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"toolu_{nonce}",
                        "name": "Agent",
                        "arguments": json.dumps(agent_arguments),
                    }
                ]
            },
            {"text": f"launch-reply-{nonce}"},
        ],
        match=f"launch-{nonce}-agent",
        required_tools=["Agent"],
    )


def _open_with_steer_off(page: Page, base_url: str, session_id: str) -> None:
    # With Always steer off, a follow-up sent while the session reads busy waits in the queue.
    page.add_init_script("localStorage.setItem('omnigent:always-steer', 'false')")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)


def _launch_background_agent(page: Page, mock_llm_server_url: str, nonce: str) -> None:
    working = page.get_by_test_id("working-indicator")
    _send(page, f"t1-{nonce} Reply to this first question.")
    expect(page.locator(_ASSISTANT, has_text=f"first-reply-{nonce}").first).to_be_visible(
        timeout=_FIRST_TURN_TIMEOUT_MS
    )
    expect(working).to_have_count(0, timeout=_SETTLE_TIMEOUT_MS)
    _send(page, f"launch-{nonce}-agent Start the background worker agent, then reply.")
    expect(page.locator(_ASSISTANT, has_text=f"launch-reply-{nonce}").first).to_be_visible(
        timeout=60_000
    )
    poll_until(
        lambda: _user_text_seen(mock_llm_server_url, f"worker-{nonce}-background-task"),
        timeout_s=_MOCK_REQUEST_TIMEOUT_S,
        interval_s=0.2,
    )


def _session_status(base_url: str, session_id: str) -> Any:
    return httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10).json().get("status")


@pytest.fixture
def native_claude_session_with_blocking_stop_hook(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    claude_managed_settings: None,
) -> Iterator[tuple[str, str, str]]:
    """A real Claude session whose workspace Stop hook blocks once while an agent runs."""
    continue_token = f"stop-hook-continuation-{uuid.uuid4().hex[:8]}-after-the-agent-launched"
    workspaces = ui_fixtures._REPO_ROOT / ".omnigent" / "e2e-hook-workspaces"
    workspaces.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="blocked-stop-", dir=workspaces) as directory:
        workspace = Path(directory)
        hook_script = workspace / "stop_hook.py"
        hook_script.write_text(
            _BLOCK_ONCE_STOP_HOOK.format(
                marker=str(workspace / "blocked.marker"),
                reason=f"{continue_token}: verify the worker before stopping.",
            ),
            encoding="utf-8",
        )
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(hook_script))}"
        settings = workspace / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(
            json.dumps(
                {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}
            ),
            encoding="utf-8",
        )
        original_create = ui_fixtures.create_native_session

        def create_in_workspace(*args: Any, **kwargs: Any) -> dict[str, Any]:
            kwargs["metadata"] = {**kwargs.get("metadata", {}), "workspace": str(workspace)}
            return original_create(*args, **kwargs)

        monkeypatch.setattr(ui_fixtures, "create_native_session", create_in_workspace)
        with ui_fixtures.native_claude_mock_session_context(
            live_server, mock_llm_server_url, tmp_path_factory
        ) as (base_url, session_id):
            yield base_url, session_id, continue_token


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_follow_up_after_background_agent_launch_settles_and_delivers_next_message(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Working clears after Claude replies, and the next follow-up is not queued."""
    base_url, session_id = native_claude_mock_session
    nonce = uuid.uuid4().hex[:8]
    next_token = f"next-follow-up-{nonce}-sent-after-the-agent-launched"
    next_reply = f"next-reply-{nonce}"
    _configure_background_agent_launch(mock_llm_server_url, nonce)
    configure_mock_llm(
        mock_llm_server_url, [{"text": next_reply}], match=next_token, required_tools=["Bash"]
    )
    # A held sub-agent request would wedge the shared mock-backed runner for later tests.
    request.addfinalizer(lambda: _release_all_gates(mock_llm_server_url))

    page = request.getfixturevalue("page")
    _open_with_steer_off(page, base_url, session_id)
    working = page.get_by_test_id("working-indicator")
    _launch_background_agent(page, mock_llm_server_url, nonce)
    expect(working).to_have_count(0, timeout=_SETTLE_TIMEOUT_MS)
    # A brief idle flash does not count; Working must stay clear throughout.
    hold_until = time.monotonic() + _IDLE_HOLD_S
    while time.monotonic() < hold_until:
        assert working.count() == 0, "Working reappeared while the background agent ran"
        page.wait_for_timeout(100)

    _send(page, f"{next_token} Reply to this follow-up.")
    expect(page.locator(_ASSISTANT, has_text=next_reply).first).to_be_visible(
        timeout=_SETTLE_TIMEOUT_MS
    )
    expect(page.get_by_test_id("composer-queued-strip")).to_have_count(0)
    expect(working).to_have_count(0, timeout=_SETTLE_TIMEOUT_MS)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_blocked_stop_keeps_working_until_claude_stops_with_the_agent_running(
    request: pytest.FixtureRequest,
    native_claude_session_with_blocking_stop_hook: tuple[str, str, str],
    mock_llm_server_url: str,
) -> None:
    """A Stop hook's continuation stays Working and queues follow-ups until Claude stops."""
    base_url, session_id, continue_token = native_claude_session_with_blocking_stop_hook
    nonce = uuid.uuid4().hex[:8]
    continuation_reply = f"continuation-reply-{nonce}"
    queued_token = f"queued-follow-up-{nonce}-sent-during-the-stop-hook-continuation"
    queued_reply = f"queued-reply-{nonce}"
    _configure_background_agent_launch(mock_llm_server_url, nonce)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": continuation_reply, "delay": _CONTINUATION_DELAY_S}],
        match=continue_token,
        required_tools=["Bash"],
    )
    configure_mock_llm(
        mock_llm_server_url, [{"text": queued_reply}], match=queued_token, required_tools=["Bash"]
    )
    request.addfinalizer(lambda: _release_all_gates(mock_llm_server_url))

    page = request.getfixturevalue("page")
    _open_with_steer_off(page, base_url, session_id)
    working = page.get_by_test_id("working-indicator")
    _launch_background_agent(page, mock_llm_server_url, nonce)
    poll_until(
        lambda: _user_text_seen(mock_llm_server_url, continue_token),
        timeout_s=_MOCK_REQUEST_TIMEOUT_S,
        interval_s=0.2,
    )
    expect(working).to_be_visible(timeout=_SETTLE_TIMEOUT_MS)
    poll_until(
        lambda: _session_status(base_url, session_id) == "running",
        timeout_s=_MOCK_REQUEST_TIMEOUT_S,
        interval_s=0.2,
    )

    _send(page, f"{queued_token} Reply to this follow-up.")
    expect(page.get_by_test_id("composer-queued-strip")).to_be_visible(timeout=_SETTLE_TIMEOUT_MS)
    assert not _user_text_seen(mock_llm_server_url, queued_token)
    assert _session_status(base_url, session_id) == "running"

    expect(page.locator(_ASSISTANT, has_text=continuation_reply).first).to_be_visible(
        timeout=_SETTLE_TIMEOUT_MS + _CONTINUATION_DELAY_S * 1000
    )
    expect(page.locator(_ASSISTANT, has_text=queued_reply).first).to_be_visible(
        timeout=_SETTLE_TIMEOUT_MS
    )
    expect(page.get_by_test_id("composer-queued-strip")).to_have_count(0)
    expect(working).to_have_count(0, timeout=_SETTLE_TIMEOUT_MS)
