"""Run ``/reload-skills`` and ``/reload-plugins`` from the Claude Code chat composer.

A real Claude CLI runs in the session terminal against the mock model. Each
journey changes disk during the session, runs the reload from the chat menu,
then runs the newly loaded skill and checks the model received its body. The
tmux pane's process id and the earlier turn in the model context show the
same Claude process kept running through the reload.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.host import isolated_host
from tests._helpers.native_session import create_native_session
from tests.e2e_ui.chat.test_claude_skills_menu_terminal_parity import (
    _CLAUDE_MOCK_MODEL,
    _PLUGIN,
    _host_command,
    _install_claude_plugin,
)
from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _select_view_mode

# Record at the viewport size so chat text stays readable in the demo.
pytestmark = pytest.mark.browser_context_args(record_video_size={"width": 1280, "height": 720})

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_FIRST_REPLY = "Hello! Ready when you are."
_FIRST_TURN_TIMEOUT_MS = 180_000
_TURN_TIMEOUT_MS = 120_000


def _pane_metadata(base_url: str, session_id: str) -> dict[str, Any] | None:
    rows = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0).json()
    terminal = next((r for r in rows.get("data", []) if r.get("type") == "terminal"), None)
    return None if terminal is None else terminal["metadata"]


def _tmux(meta: dict[str, Any], *args: str) -> str:
    proc = subprocess.run(
        ["tmux", "-S", meta["tmux_socket"], *args, "-t", meta["tmux_target"]],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _pane_pid(base_url: str, session_id: str) -> str:
    """Process id of the process tmux started for the Claude TUI."""
    meta = _pane_metadata(base_url, session_id)
    return "" if meta is None else _tmux(meta, "display-message", "-p", "#{pane_pid}").strip()


def _wait_tui_ready(base_url: str, session_id: str) -> str:
    """Wait for the Claude TUI's input prompt; return the pane's process id."""
    deadline = time.monotonic() + _FIRST_TURN_TIMEOUT_MS / 1000
    pane = ""
    while time.monotonic() < deadline:
        meta = _pane_metadata(base_url, session_id)
        pane = "" if meta is None else _tmux(meta, "capture-pane", "-p")
        if "❯" in pane or "? for shortcuts" in pane:
            return _pane_pid(base_url, session_id)
        time.sleep(1)
    raise AssertionError(f"Claude TUI never showed its prompt; pane:\n{pane}")


def _user_texts(mock_url: str) -> list[list[str]]:
    """The user text blocks of every Anthropic request, one list per request."""
    resp = httpx.get(f"{mock_url}/mock/message_requests", timeout=10.0)
    resp.raise_for_status()
    requests: list[list[str]] = []
    for entry in resp.json()["requests"]:
        texts: list[str] = []
        for message in entry["body"].get("messages") or []:
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts.extend(
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
        requests.append(texts)
    return requests


def _open_chat(page: Page, base_url: str, session_id: str, host_id: str) -> Any:
    reviewed_key = json.dumps(f"omnigent:imports-reviewed:{host_id}")
    page.add_init_script(f"localStorage.setItem({reviewed_key}, 'true')")
    page.goto(f"{base_url}/c/{session_id}")
    _select_view_mode(page, "Chat")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=60_000)
    return composer


def _say_hello(page: Page, composer: Any) -> None:
    """Run one ordinary turn so later requests show whether the context survived."""
    composer.press_sequentially("hello", delay=40)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT).filter(has_text=_FIRST_REPLY)).to_be_visible(
        timeout=_FIRST_TURN_TIMEOUT_MS
    )
    page.wait_for_timeout(3_000)


def _show_no_match(page: Page, composer: Any, query: str) -> None:
    composer.fill("")
    composer.press_sequentially(query, delay=60)
    expect(page.get_by_text("No matching skills")).to_be_visible(timeout=15_000)
    page.wait_for_timeout(3_500)
    composer.fill("")


def _run_reload_from_menu(
    page: Page, composer: Any, steps: _StepLog, name: str, expected: str | re.Pattern[str]
) -> None:
    """Pick *name* from the ``/reload`` menu and open its output."""
    composer.press_sequentially("/reload", delay=60)
    row = page.get_by_test_id(f"slash-menu-item-{name}")
    expect(row).to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("slash-menu-item-reload-plugins")).to_be_visible()
    steps.mark("/reload menu lists both commands")
    page.wait_for_timeout(3_000)
    row.click()
    steps.mark(f"{name} clicked")
    card = page.get_by_test_id("slash-command-card").filter(has_text=name)
    expect(card).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    steps.mark(f"{name} card shown")
    page.wait_for_timeout(2_000)
    card.click()
    output = page.get_by_text(expected).last
    expect(output).to_be_visible(timeout=15_000)
    output.scroll_into_view_if_needed()
    steps.mark(f"{name} output shown")
    page.wait_for_timeout(4_500)


def _run_skill_from_menu(
    page: Page, composer: Any, steps: _StepLog, query: str, skill: str, reply: str
) -> None:
    composer.press_sequentially(query, delay=60)
    row = page.get_by_test_id(f"slash-menu-item-{skill}")
    expect(row).to_be_visible(timeout=30_000)
    steps.mark(f"{query} lists {skill}")
    page.wait_for_timeout(3_000)
    row.click()
    if composer.input_value().strip():
        page.get_by_role("button", name="Send", exact=True).click()
    steps.mark(f"{skill} sent")
    expect(page.get_by_test_id("slash-command-card").filter(has_text=skill)).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )
    expect(page.locator(_ASSISTANT).filter(has_text=reply)).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    steps.mark("skill reply shown")
    page.wait_for_timeout(5_000)


def _assert_same_conversation(mock_url: str, token: str) -> None:
    """The request carrying the skill body still holds the earlier ``hello`` turn."""
    carrying = [texts for texts in _user_texts(mock_url) if any(token in t for t in texts)]
    assert carrying, f"no model request carried the skill body token {token!r}"
    assert any("hello" in t for t in carrying[-1]), (
        f"the reload lost the earlier turn; user texts: {carrying[-1]!r}"
    )


def _require_claude() -> None:
    if shutil.which("claude") is None or shutil.which("tmux") is None:
        pytest.skip("needs the claude CLI and tmux")


class _StepLog:
    """Seconds from page creation to each verified step, for timing demo captions."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._started = time.monotonic()
        self._steps: list[dict[str, Any]] = []

    def mark(self, label: str) -> None:
        self._steps.append({"at": round(time.monotonic() - self._started, 2), "step": label})

    def save(self, page: Page) -> None:
        record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
        if record_dir:
            final_screen = page.locator("main").aria_snapshot()
            Path(record_dir, f"{self._name}-steps.json").write_text(
                json.dumps({"steps": self._steps, "final_screen": final_screen}, indent=2)
            )


@contextmanager
def _claude_session(
    live_server: str, mock_url: str, tmp_path: Path
) -> Iterator[tuple[str, str, Path, Path]]:
    """Yield ``(session_id, host_id, workspace, claude_config_dir)`` once the TUI is up."""
    _require_claude()
    claude_config_dir = tmp_path / "claude-config"
    claude_config_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    set_fallback_mock_llm(mock_url, _CLAUDE_MOCK_MODEL, _FIRST_REPLY)
    host_env = {
        "CLAUDE_CONFIG_DIR": str(claude_config_dir),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    with (
        httpx.Client(base_url=live_server, timeout=20) as client,
        isolated_host(
            tmp_path / "host",
            live_server,
            env=host_env,
            command=_host_command(live_server, mock_url),
        ) as host,
    ):
        host.wait_online(client, timeout=120, poll_interval=0.5)
        created = create_native_session(
            client,
            live_server,
            harness="claude",
            metadata={"host_id": host.host_id, "workspace": str(workspace)},
        )
        session_id = str(created["session_id"])
        try:
            yield session_id, host.host_id, workspace, claude_config_dir
        finally:
            client.delete(f"/v1/sessions/{session_id}")


def _finish(page: Page, steps: _StepLog, live_server: str, session_id: str, pid: str) -> None:
    """Check the session survived the reload, then end the clip before teardown."""
    assert _pane_pid(live_server, session_id) == pid, "the Claude process was restarted"
    assert urlparse(page.url).path == f"/c/{session_id}"
    steps.save(page)
    page.close()


@pytest.mark.timeout(900)
def test_reload_skills_from_chat_loads_a_skill_added_during_the_session(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A skill written after Claude started runs once ``/reload-skills`` picks it up."""
    token = "late skill body marker"
    reply = "The late skill loaded and ran."
    configure_mock_llm(mock_llm_server_url, [{"text": reply}] * 4, match=token)
    with _claude_session(live_server, mock_llm_server_url, tmp_path) as (
        session_id,
        host_id,
        workspace,
        _,
    ):
        pid = _wait_tui_ready(live_server, session_id)
        page = request.getfixturevalue("page")
        steps = _StepLog("reload-skills")
        composer = _open_chat(page, live_server, session_id, host_id)
        steps.mark("chat open")
        _say_hello(page, composer)
        steps.mark("hello answered")

        skill_dir = workspace / ".claude" / "skills" / "late-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: late-skill\ndescription: Added while Claude was running.\n---\n\n"
            f"Reply with the {token}.\n"
        )
        steps.mark("skill written to disk")
        _show_no_match(page, composer, "/late")
        steps.mark("/late shows no match")
        _run_reload_from_menu(page, composer, steps, "reload-skills", "(1 added)")
        _run_skill_from_menu(page, composer, steps, "/late", "late-skill", reply)

        _assert_same_conversation(mock_llm_server_url, token)
        _finish(page, steps, live_server, session_id, pid)


@pytest.mark.timeout(900)
def test_reload_plugins_from_chat_activates_a_plugin_installed_during_the_session(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A plugin installed after Claude started runs once ``/reload-plugins`` activates it."""
    # Body text of the plugin's ``simplify`` skill, written by the install helper.
    token = "Body of simplify."
    reply = "The plugin skill loaded and ran."
    configure_mock_llm(mock_llm_server_url, [{"text": reply}] * 4, match=token)
    with _claude_session(live_server, mock_llm_server_url, tmp_path) as (
        session_id,
        host_id,
        _,
        claude_config_dir,
    ):
        pid = _wait_tui_ready(live_server, session_id)
        page = request.getfixturevalue("page")
        steps = _StepLog("reload-plugins")
        composer = _open_chat(page, live_server, session_id, host_id)
        steps.mark("chat open")
        _say_hello(page, composer)
        steps.mark("hello answered")

        _install_claude_plugin(claude_config_dir, tmp_path / "marketplace")
        steps.mark("plugin installed with the claude CLI")
        _show_no_match(page, composer, f"/{_PLUGIN}")
        steps.mark(f"/{_PLUGIN} shows no match")
        reloaded = re.compile(r"Reloaded: \d+ plugins? ·")
        _run_reload_from_menu(page, composer, steps, "reload-plugins", reloaded)
        _run_skill_from_menu(page, composer, steps, f"/{_PLUGIN}", f"{_PLUGIN}:simplify", reply)

        _assert_same_conversation(mock_llm_server_url, token)
        _finish(page, steps, live_server, session_id, pid)
