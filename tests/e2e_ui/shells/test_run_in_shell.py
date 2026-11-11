"""E2E: running an agent-suggested command from a chat code block in a new shell.

An assistant reply with a ``bash`` fence gets a "Run in shell" button. Clicking
it launches the agent's default shell (desktop: a Workspace rail tab; phone: the
full-screen shell view) and types the block into it, each line followed by
Enter, so the commands run.

xterm draws to a canvas, so the test checks execution through marker files at
absolute ``tmp_path`` paths that the runner-local shell writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.polling import poll_until
from tests.e2e_ui.conftest import configure_mock_llm


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_run_in_shell_runs_each_line_in_a_new_shell(
    request: pytest.FixtureRequest,
    terminal_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
    width: int,
) -> None:
    """Run in shell opens a new shell and runs both lines of a bash block."""
    base_url, session_id = terminal_session
    first_marker = tmp_path / "first.ok"
    second_marker = tmp_path / "second.ok"
    command = f"touch {first_marker}\ntouch {second_marker}"
    prompt = f"How do I create the markers? {session_id}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Run these in a shell:\n\n```bash\n{command}\n```\n"}],
        match=prompt,
    )

    page: Page = request.getfixturevalue("page")
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(prompt)
    page.get_by_role("button", name="Send", exact=True).click()

    code_block = page.locator(".chat-code-block").filter(has_text=str(second_marker))
    expect(code_block).to_be_visible(timeout=60_000)
    assert not first_marker.exists() and not second_marker.exists()
    code_block.hover()
    code_block.get_by_role("button", name="Run in shell").click()

    if width < 768:
        surface: Locator = page.locator('[data-testid="main-terminal-view"][data-visible="true"]')
    else:
        surface = page.get_by_role("complementary", name="Workspace")
    terminal_view = surface.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=30_000)

    poll_until(lambda: first_marker.exists() and second_marker.exists(), timeout_s=20)
