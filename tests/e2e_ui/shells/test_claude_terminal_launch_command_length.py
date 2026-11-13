"""UI journey: a claude-native agent with very long instructions still boots its terminal.

The runner starts Claude Code by handing the whole ``claude`` argv — including
``--append-system-prompt <AgentSpec.instructions>`` — to one ``tmux new-session``
client command. tmux rejects a single client command over ~16KB with
``command too long``, so an agent whose prompt is a long playbook never gets a
terminal: the runner marks the session ``failed`` (``native_terminal_start_failed``)
and the chat shows "Native Claude terminal failed to start".

The stock ``omnigent claude`` wrapper spec (a 134-character prompt) is the control,
so the instructions length is the only variable between the two cases.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, _temp_omnigent_mock_config
from tests.e2e_ui.messages.test_message_render_parity import _select_view_mode

# Well past tmux's ~16KB per-command cap once shell-quoted onto the claude argv.
_BIG_INSTRUCTIONS_CHARS = 20_000
_PLAYBOOK_SENTENCE = "You are a meticulous release engineer. Follow the playbook exactly. "
# Bridge prep, model-catalog probes, tmux boot and the WebSocket attach.
_TERMINAL_READY_TIMEOUT_S = 240.0
_POLL_S = 1.0

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.timeout(600),
    pytest.mark.skipif(
        shutil.which("tmux") is None, reason="claude-native terminals run inside tmux"
    ),
    pytest.mark.skipif(shutil.which("claude") is None, reason="requires the claude CLI on PATH"),
]


def _wrapper_spec_yaml(instructions_chars: int | None) -> str:
    """The stock ``omnigent claude`` wrapper spec, optionally with a long playbook prompt."""
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        raw = yaml.safe_load(_materialize_claude_agent_spec(Path(tmp)).read_text())
    if instructions_chars is not None:
        repeats = instructions_chars // len(_PLAYBOOK_SENTENCE) + 1
        raw["prompt"] = (_PLAYBOOK_SENTENCE * repeats)[:instructions_chars]
    return yaml.safe_dump(raw, sort_keys=False)


def _create_claude_native_session(base_url: str, runner_id: str, yaml_text: str) -> str:
    """Register the wrapper agent from *yaml_text*, create its session and bind the runner."""
    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    # A ``*.yaml`` arcname routes the spec_version-less wrapper spec through the
    # compat translator, as the ``omnigent claude`` CLI does.
    bundle = bundle_files({"claude-native-ui.yaml": yaml_text.encode()})
    created = post_session_bundle(
        httpx.post,
        f"{base_url}/v1/sessions",
        bundle,
        metadata={"labels": labels},
        filename="claude-native-ui.tar.gz",
        timeout=30.0,
    )
    created.raise_for_status()
    session_id = str(created.json()["session_id"])
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
    return session_id


@pytest.fixture
def claude_native_session_with_instructions(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound claude-native session whose prompt length is ``request.param``.

    ``None`` keeps the stock wrapper prompt. Mirrors ``native_claude_mock_session``.

    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    with _temp_omnigent_mock_config(
        mock_llm_server_url, "claude", workflow_owned=bool(_server_state.get("workflow_owned"))
    ):
        session_id = _create_claude_native_session(
            live_server, runner_id, _wrapper_spec_yaml(request.param)
        )
        try:
            yield live_server, session_id
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


def _session_snapshot(base_url: str, session_id: str) -> dict[str, Any]:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    snapshot: dict[str, Any] = response.json()
    return snapshot


def _runner_log_cause(message: str) -> str | None:
    """The tmux launch failure line from the runner log the error message points at."""
    match = re.search(r"see the runner log for details: (\S+)", message)
    if match is None:
        return None
    log_path = Path(match.group(1).rstrip(".")).expanduser()
    if not log_path.is_file():
        return None
    for line in reversed(log_path.read_text(errors="replace").splitlines()):
        if "tmux launch failed" in line:
            return line.strip()
    return None


def _shown_failure(page: Page) -> str:
    """Switch to the Chat view, expand the failure the user sees and return its text."""
    if page.get_by_test_id("view-mode-chat").count() > 0:
        _select_view_mode(page, "Chat")
    failure = page.locator(
        '[data-testid="error-pill"], [data-testid="disconnected-indicator"]'
    ).first
    expect(failure).to_be_visible(timeout=30_000)
    pill = page.get_by_test_id("error-pill")
    if pill.count() == 0:
        return failure.inner_text()
    pill.first.click()
    body = pill.first.get_by_test_id("error-message-content")
    expect(body).to_be_visible()
    headline = pill.first.get_by_test_id("error-headline").inner_text()
    return f"{headline}: {body.inner_text()}"


def _launch_failure_report(snapshot: dict[str, Any], shown: str, case: str) -> str:
    error = snapshot.get("last_task_error") or {}
    lines = [
        f"Claude Code terminal never started for the {case} claude-native session.",
        f"UI shows: {shown}",
        f"session status={snapshot.get('status')} last_task_error={json.dumps(error)}",
    ]
    cause = _runner_log_cause(str(error.get("message", "")))
    if cause:
        lines.append(f"runner log: {cause}")
    return "\n".join(lines)


@pytest.mark.parametrize(
    "claude_native_session_with_instructions",
    [
        pytest.param(None, id="stock-prompt"),
        pytest.param(_BIG_INSTRUCTIONS_CHARS, id="20k-instructions"),
    ],
    indirect=True,
)
def test_claude_native_terminal_boots_regardless_of_instructions_size(
    request: pytest.FixtureRequest,
    claude_native_session_with_instructions: tuple[str, str],
    tmp_path: Path,
) -> None:
    """Opening the session and switching to the Terminal view attaches a live Claude Code pane."""
    base_url, session_id = claude_native_session_with_instructions
    case = request.node.callspec.id
    # Requested after the session exists so a recording starts on the journey, not the setup.
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    deadline = time.monotonic() + _TERMINAL_READY_TIMEOUT_S
    connected = page.locator('[data-testid="terminal-view"][data-state="connected"]')
    terminal_segment = page.get_by_test_id("view-mode-terminal")
    switched = False
    while time.monotonic() < deadline:
        if not switched and terminal_segment.count() > 0 and terminal_segment.is_enabled():
            terminal_segment.click()
            switched = True
        if connected.count() > 0:
            break
        snapshot = _session_snapshot(base_url, session_id)
        if snapshot.get("status") == "failed":
            shown = _shown_failure(page)
            page.screenshot(path=str(tmp_path / f"{case}-terminal-start-failed.png"))
            # Hold the cause on screen so a recording ends on it.
            page.wait_for_timeout(2_000)
            pytest.fail(_launch_failure_report(snapshot, shown, case))
        page.wait_for_timeout(int(_POLL_S * 1000))
    else:
        pytest.fail(
            _launch_failure_report(
                _session_snapshot(base_url, session_id),
                f"Terminal view did not attach within {_TERMINAL_READY_TIMEOUT_S:.0f}s",
                case,
            )
        )

    terminal = page.locator('[data-testid="terminal-view"]').last
    expect(terminal).to_have_attribute("data-state", "connected", timeout=30_000)
    assert _session_snapshot(base_url, session_id)["status"] != "failed"
    page.screenshot(path=str(tmp_path / f"{case}-terminal-connected.png"))
    page.wait_for_timeout(2_000)
