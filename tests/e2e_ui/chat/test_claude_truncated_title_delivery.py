"""A real Claude prompt under a clipped title accepts messages from web chat."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from tests.e2e_ui.conftest import set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _pane_text,
    _tmux_advert,
    _wait_terminal_connected,
)

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.min_runner_version("0.18.0"),
    pytest.mark.skipif(
        shutil.which("claude") is None or shutil.which("tmux") is None,
        reason="requires real Claude Code and tmux",
    ),
    pytest.mark.skipif(
        any(path.is_file() for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS),
        reason="managed Claude settings require a container for local model replies",
    ),
]


@pytest.fixture(autouse=True)
def _clipped_title_renderer() -> None:
    """Older CLI renderers wrap this title instead of producing the clipped fixture."""
    version = subprocess.check_output(["claude", "--version"], text=True, timeout=10)
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None or tuple(map(int, match.groups())) < (2, 1, 296):
        pytest.skip("clipped-title renderer fixture requires Claude Code 2.1.296 or newer")


@pytest.mark.timeout(300)
def test_web_message_reaches_claude_beneath_a_truncated_title(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Resize and rename the real TUI, then verify one delivered and persisted turn."""
    base_url, session_id = native_claude_mock_session
    proof = Path(os.environ.get("OMNIGENT_E2E_PROOF_DIR", str(tmp_path)))
    proof.mkdir(parents=True, exist_ok=True)
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    set_fallback_mock_llm(mock_llm_server_url, "default", "BASELINE_TITLE_READY")
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "BASELINE_TITLE_READY")
    _send(page, "Reply with BASELINE_TITLE_READY.")
    expect(
        page.locator('[data-testid="message-bubble"][data-role="assistant"]')
        .filter(has_text="BASELINE_TITLE_READY")
        .last
    ).to_be_visible(timeout=60_000)
    expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=60_000)

    advert = _tmux_advert(base_url, session_id)
    assert advert is not None
    tmux = ["tmux", "-S", advert["socket_path"]]
    target = advert["tmux_target"]
    title = " ".join(
        ["Check resilient message delivery beneath a session title wider than the terminal"] * 3
    )
    subprocess.run(
        [*tmux, "send-keys", "-t", target, "-l", "/rename " + title], check=True, timeout=10
    )
    typed = "".join(title[-24:].split())
    deadline = time.monotonic() + 10
    while typed not in "".join(_pane_text(base_url, session_id).split()):
        if time.monotonic() >= deadline:
            raise AssertionError(
                "the /rename draft never landed in the composer:\n"
                + _pane_text(base_url, session_id)
            )
        time.sleep(0.2)
    subprocess.run([*tmux, "send-keys", "-t", target, "Enter"], check=True, timeout=10)
    subprocess.run(
        [*tmux, "resize-window", "-t", target, "-x", "72", "-y", "30"], check=True, timeout=10
    )

    deadline = time.monotonic() + 30
    pane = ""
    clipped_title = False
    while time.monotonic() < deadline:
        pane = _pane_text(base_url, session_id)
        lines = pane.splitlines()
        clipped_title = any(
            line.startswith(" ")
            and not line.startswith("  ")
            and line.rstrip().endswith("… ─")
            and not line.strip().startswith("─")
            and index + 1 < len(lines)
            and lines[index + 1].strip().startswith("❯")
            for index, line in enumerate(lines)
        )
        if clipped_title:
            break
        time.sleep(0.2)
    (proof / "before-send-terminal.txt").write_text(pane)
    assert clipped_title, f"The real terminal did not render the target title shape:\n{pane}"
    initial_pid = subprocess.check_output(
        [*tmux, "display-message", "-p", "-t", target, "#{pane_pid}"], text=True, timeout=10
    ).strip()

    set_fallback_mock_llm(mock_llm_server_url, "default", "TRUNCATED_TITLE_DELIVERED")
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "TRUNCATED_TITLE_DELIVERED")
    _send(page, "Reply with TRUNCATED_TITLE_DELIVERED.")
    reply = page.locator('[data-testid="message-bubble"][data-role="assistant"]').filter(
        has_text="TRUNCATED_TITLE_DELIVERED"
    )
    expect(reply).to_have_count(1, timeout=60_000)
    expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=60_000)
    assert (
        subprocess.check_output(
            [*tmux, "display-message", "-p", "-t", target, "#{pane_pid}"], text=True, timeout=10
        ).strip()
        == initial_pid
    )
    page.screenshot(path=str(proof / "delivered.png"), full_page=True)
    deadline = time.monotonic() + 30
    items: list[dict[str, object]] = []
    persisted = False
    while not persisted and time.monotonic() < deadline:
        response = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/items", params={"limit": 100}, timeout=10
        )
        response.raise_for_status()
        items = response.json()["data"]
        persisted = any(
            item.get("role") == "assistant"
            and "TRUNCATED_TITLE_DELIVERED" in json.dumps(item.get("content", []))
            for item in items
        )
        if not persisted:
            time.sleep(0.1)
    (proof / "transcript.json").write_text(json.dumps(items, indent=2))
    assert persisted, "the assistant reply was not persisted to the transcript within 30 s"
    assert not [item for item in items if item.get("type") == "error"]
    assert (
        sum(
            "TRUNCATED_TITLE_DELIVERED" in json.dumps(item.get("content", []))
            for item in items
            if item.get("role") == "assistant"
        )
        == 1
    )
    page.reload()
    _ensure_chat_view(page)
    expect(reply).to_have_count(1, timeout=30_000)
    page.screenshot(path=str(proof / "reloaded.png"), full_page=True)
