"""A Claude Code startup dialog must show in the web UI even when no message is pending.

Claude Code parks on a dialog only a person can answer when its user settings
file is not valid JSON ("Settings Error"). The runner already names such
dialogs when a web message is delivered; a session started from the web with
nothing sent must surface the same sign instead of looking like it is still
starting until the 180 s readiness timeout.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.native_session import create_native_session
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner
from tests.e2e_ui.messages.test_message_render_parity import _select_view_mode
from tests.helpers.ui_configuration import claude_mock_provider_config

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("tmux") is None,
    reason="needs the real Claude Code CLI and tmux",
)

_DIALOG_HEADLINE = "Settings Error"
_DIALOG_SIGN = re.compile(r"settings error|waiting for an answer in its terminal", re.IGNORECASE)
# Claude Code cold boot up to the dialog, including the runner's terminal launch.
_DIALOG_TIMEOUT_S = 90.0
# How long the web UI gets to name the dialog once the pane shows it.
_SIGN_TIMEOUT_S = 60.0

# Visible text outside the embedded terminal, which renders the pane itself.
_CHAT_TEXT_JS = """
() => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const parts = [];
  let node;
  while ((node = walker.nextNode())) {
    const parent = node.parentElement;
    if (!parent || parent.closest('.main-terminal-view, .xterm')) continue;
    const style = window.getComputedStyle(parent);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    const text = node.textContent.trim();
    if (text) parts.push(text);
  }
  return parts.join(' | ');
}
"""


def _write_mock_claude_provider(config_home: Path, mock_llm_server_url: str) -> None:
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(claude_mock_provider_config(mock_llm_server_url))


def _pane_text(native_tmp: Path) -> str:
    # Newest advert first, in case the runner left an older terminal's behind.
    adverts = sorted(native_tmp.rglob("tmux.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not adverts:
        return ""
    try:
        advert = json.loads(adverts[0].read_text())
    except (OSError, ValueError):
        # The runner may still be writing the advert; the caller polls again.
        return ""
    try:
        proc = subprocess.run(
            [
                "tmux",
                "-S",
                advert["socket_path"],
                "capture-pane",
                "-t",
                advert["tmux_target"],
                "-p",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _wait_for_dialog(native_tmp: Path, page: Page) -> str:
    deadline = time.monotonic() + _DIALOG_TIMEOUT_S
    pane = ""
    while time.monotonic() < deadline:
        pane = _pane_text(native_tmp)
        if _DIALOG_HEADLINE in pane:
            return pane
        page.wait_for_timeout(500)
    return pane


@pytest.mark.timeout(300)
def test_startup_dialog_is_reported_without_a_message(
    built_spa: None,
    request: pytest.FixtureRequest,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    claude_config = tmp_path / "claude-config"
    claude_config.mkdir()
    (claude_config / "settings.json").write_text('{ "permissions": [ oops\n')
    config_home = tmp_path / "omnigent-config"
    _write_mock_claude_provider(config_home, mock_llm_server_url)
    # Short TMPDIR: the tmux socket path must fit the AF_UNIX limit.
    with (
        tempfile.TemporaryDirectory(prefix="omni-dialog-") as native_tmp_name,
        server_runner(tmp_path / "stack") as stack,
        httpx.Client(trust_env=False, timeout=15) as client,
    ):
        native_tmp = Path(native_tmp_name)
        stack.start_runner(
            env={
                "CLAUDE_CONFIG_DIR": str(claude_config),
                "OMNIGENT_CONFIG_HOME": str(config_home),
                "TMPDIR": str(native_tmp),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        )
        session_id = create_native_session(client, stack.base_url, harness="claude")["session_id"]
        bind_session_runner(client.patch, stack.base_url, session_id, stack.runner_id)

        page: Page = request.getfixturevalue("page")
        page.goto(f"{stack.base_url}/c/{session_id}")
        expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=60_000)
        _select_view_mode(page, "Chat")

        pane = _wait_for_dialog(native_tmp, page)
        assert _DIALOG_HEADLINE in pane, (
            f"Claude Code did not park on its {_DIALOG_HEADLINE} dialog:\n{pane}\n"
            f"{stack.log_tail()}"
        )

        deadline = time.monotonic() + _SIGN_TIMEOUT_S
        visible = ""
        while time.monotonic() < deadline:
            visible = page.evaluate(_CHAT_TEXT_JS)
            if _DIALOG_SIGN.search(visible):
                break
            page.wait_for_timeout(1000)
        assert _DIALOG_SIGN.search(visible), (
            f"no sign of the {_DIALOG_HEADLINE} dialog in the Chat view {_SIGN_TIMEOUT_S:g}s "
            f"after Claude Code showed it; visible text:\n{visible}\npane:\n{pane}"
        )
        # The person still has to answer it in the terminal; nothing may have dismissed it.
        assert _DIALOG_HEADLINE in _wait_for_dialog(native_tmp, page)
