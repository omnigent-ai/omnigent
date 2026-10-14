"""UI journey: a launcher question that parks Claude Code's start must show in chat.

Managed hosts start Claude Code through a launcher wrapper configured as
``harness.claude-native.command`` (Databricks' ``ug``/``isaac``). The wrapper can
stop on a question of its own before Claude Code runs — "Upgrade Claude Code if
available? (Y/n)" — so the pane waits for a keypress while the chat view, where
the person is, shows nothing. A stand-in wrapper reproduces that pause through
the same config override; the pane is read through the product's terminal-attach
socket because the bridge's tmux socket lives in the runner sandbox's private /tmp.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import websockets
import yaml
from playwright.sync_api import Page, expect

from tests._helpers.native_session import create_native_session
from tests._helpers.session import bind_session_runner
from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _USER,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _type_into_tui,
    _wait_terminal_connected,
)

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="claude CLI is required for native Claude e2e",
)

_log = logging.getLogger(__name__)

_UPGRADE_QUESTION = "Upgrade Claude Code if available? (Y/n)"
# Output the ug wrapper prints before Claude Code runs.
_LAUNCHER_SCRIPT = """\
#!/usr/bin/env bash
if [ "${1:-}" = "--" ]; then shift; fi
current=$(claude --version 2>/dev/null | awk '{print $1}')
echo "Generating claude-code MCP client config..."
echo "No changes made to ${HOME}/.claude.json."
echo "Managed settings drift detected, applying updates..."
echo "Installing ug CLI..."
echo "ug CLI installed (0.1.0+5f7eed4)."
echo "! ug requires Claude Code 2.1.290 or newer. Your current version is Claude Code ${current}."
printf '%s ' 'UPGRADE_QUESTION'
IFS= read -r answer
exec claude "$@"
""".replace("UPGRADE_QUESTION", _UPGRADE_QUESTION)

_CARD_HEADLINE = "Claude Code can't start until you answer a question in the terminal"
_ERROR_PILL = '[data-testid="error-pill"]'
_WORKING = '[data-testid="working-indicator"]'

_ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\r")
_PROMPT_TIMEOUT_S = 90.0
_CLAUDE_BOOT_TIMEOUT_S = 120.0


def _terminal_id(base_url: str, session_id: str) -> str | None:
    """Return the session's first terminal resource id, or ``None`` before it exists."""
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources/terminals", timeout=10)
    if response.status_code != 200:
        return None
    data = response.json().get("data") or []
    return str(data[0]["id"]) if data else None


def _pane_output(
    base_url: str, session_id: str, terminal_id: str, *, settle_s: float = 1.5
) -> str:
    """Attach read-only and return the ANSI-stripped output replayed until it goes quiet."""
    url = base_url.replace("http", "ws", 1) + (
        f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach?read_only=true"
    )

    async def attach() -> bytes:
        chunks: list[bytes] = []
        async with websockets.connect(url, max_size=None, open_timeout=20) as ws:
            deadline = time.monotonic() + settle_s
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    frame = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except (TimeoutError, websockets.ConnectionClosed):
                    break
                if isinstance(frame, bytes):
                    chunks.append(frame)
        return b"".join(chunks)

    # Playwright's sync API owns this thread's event loop once a page exists.
    with ThreadPoolExecutor(max_workers=1) as pool:
        raw = pool.submit(asyncio.run, attach()).result()
    return _ANSI.sub(b"", raw).decode("utf-8", "replace")


def _wait_for_pane_text(base_url: str, session_id: str, needle: str, *, timeout_s: float) -> str:
    """Block until the Claude terminal's output contains *needle*; return that output."""
    deadline = time.monotonic() + timeout_s
    output = ""
    while time.monotonic() < deadline:
        terminal_id = _terminal_id(base_url, session_id)
        if terminal_id is not None:
            output = _pane_output(base_url, session_id, terminal_id)
            if needle in output:
                return output
        time.sleep(1.0)
    raise AssertionError(
        f"terminal never showed {needle!r} within {timeout_s}s; last output:\n{output}"
    )


def _write_launcher_workspace(workspace: Path) -> None:
    """Install the stand-in launcher through the production per-workspace override."""
    launcher = workspace / "ug-stand-in.sh"
    launcher.write_text(_LAUNCHER_SCRIPT)
    launcher.chmod(0o755)
    config = workspace / ".omnigent" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        yaml.safe_dump({"harness": {"claude-native": {"command": str(launcher), "args": ["--"]}}})
    )


@pytest.fixture
def parked_launcher_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A claude-native session whose launcher wrapper is parked on its (Y/n) question.

    The workspace must be reachable from the runner; under the prepared repro
    environment pass ``--basetemp`` inside the worktree.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    session_id: str | None = None
    try:
        runner_id = str(_server_state["runner_id"])
        workspace = tmp_path_factory.mktemp("launcher_workspace").resolve()
        _write_launcher_workspace(workspace)
        with _temp_omnigent_mock_config(
            mock_llm_server_url, "claude", workflow_owned=bool(_server_state.get("workflow_owned"))
        ):
            created = create_native_session(
                httpx, live_server, harness="claude", metadata={"workspace": str(workspace)}
            )
            session_id = str(created["session_id"])
            bind_session_runner(httpx.patch, live_server, session_id, runner_id, timeout=10.0)
            yield (live_server, session_id)
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            # Escalate to SIGKILL if the runner ignores SIGTERM, as the native fixtures do.
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_launcher_question_is_announced_in_chat(
    request: pytest.FixtureRequest,
    parked_launcher_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A message sent to a parked launcher gets a card naming the terminal question.

    Mirrors the report: send a message from the chat view, read the card, answer
    the question in the Terminal view, then resend and get a reply.
    """
    base_url, session_id = parked_launcher_session
    pane = _wait_for_pane_text(
        base_url, session_id, _UPGRADE_QUESTION, timeout_s=_PROMPT_TIMEOUT_S
    )
    _log.info("launcher parked on its question:\n%s", pane)
    reset_mock_llm(mock_llm_server_url)
    token = f"ast-{uuid.uuid4().hex[:8]}"
    set_fallback_mock_llm(mock_llm_server_url, "default", token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, token)

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    _send(page, "hello")
    expect(page.locator(_USER, has_text="hello")).to_have_count(1, timeout=30_000)
    card = page.locator(_ERROR_PILL)
    expect(card).to_have_count(1, timeout=30_000)
    expect(card).to_contain_text(_CARD_HEADLINE)
    card.locator('button[aria-expanded="false"]').click()
    expect(card).to_contain_text(_UPGRADE_QUESTION)
    expect(card).to_contain_text("send your message again")
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)

    terminal_id = _terminal_id(base_url, session_id)
    assert terminal_id is not None, "the Claude terminal disappeared after the failed send"
    assert _UPGRADE_QUESTION in _pane_output(base_url, session_id, terminal_id), (
        "the failed send did not leave the launcher parked on its question"
    )

    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _type_into_tui(page, "y")
    deadline = time.monotonic() + _CLAUDE_BOOT_TIMEOUT_S
    while "❯" not in (pane := _pane_output(base_url, session_id, terminal_id)):
        assert time.monotonic() < deadline, f"Claude Code did not start after the answer:\n{pane}"
        page.wait_for_timeout(1000)

    _ensure_chat_view(page)
    _send(page, "hello")
    expect(page.locator(_ASSISTANT, has_text=token)).to_have_count(1, timeout=90_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
