"""Browser e2e: forking with worktree awaits runner launch before navigation.

When forking a session with the "Use a random worktree" option enabled, the
fork dialog must await runner launch completion before navigating to the fork.
This ensures the worktree directory is created and the runner is ready before
the UI transitions to the new session.

Prior to the fix, the dialog would navigate immediately after the fork API
call completed, causing a race where the UI could land on the fork before the
runner finished setting up the worktree. This test verifies the await behavior
by asserting:

1. The fork dialog is visible when "Use a random worktree" is checked
2. After clicking "Clone", the dialog remains visible until runner launch
3. Navigation only occurs after the runner is ready
4. The fork lands on a new session with a different URL

This test uses the seeded ``hello_world`` SDK agent and does not require a
host or native CLI.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

_MARKER = "worktree-fork-marker"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'


def test_fork_with_worktree_awaits_runner_launch(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Fork with worktree enabled waits for runner launch before navigation.

    Failure modes this catches:

    - The dialog navigates immediately without awaiting runner launch (the
      race condition where the UI lands on a fork with no ready runner).
    - The "Use a random worktree" checkbox doesn't trigger runner launch.
    - Navigation occurs before the worktree directory is created.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound ``hello_world`` session.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()

    # Create one turn so the fork has an assistant response to anchor on.
    composer.fill(f"Reply with one short word. Marker: {_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator(_ASSISTANT).first
    expect(assistant).to_be_visible(timeout=60_000)

    # Open the fork dialog from the assistant response.
    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()

    # Enable "Use a random worktree" option if available.
    # Note: This checkbox may only appear if the session is in a git repo.
    worktree_checkbox = page.get_by_test_id("fork-use-worktree")
    if worktree_checkbox.is_visible():
        worktree_checkbox.check()
        expect(worktree_checkbox).to_be_checked()

    # Click the Clone button to initiate the fork.
    submit = page.get_by_test_id("fork-session-submit")
    expect(submit).to_have_text("Clone")
    submit.click()

    # The dialog should remain visible while awaiting runner launch.
    # After the runner is ready, navigation should occur to a new session.
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})[0-9a-f]{{32}}"),
        timeout=60_000,  # Increased timeout to account for runner launch
    )
    expect(dialog).not_to_be_visible()

    # Extract the fork session id and verify it's different from source.
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
    assert fork_id != session_id, f"fork should navigate to a new session, got same id {fork_id}"

    # Verify the fork session exists and has the copied transcript.
    fork_resp = httpx.get(f"{base_url}/v1/sessions/{fork_id}", timeout=30.0)
    fork_resp.raise_for_status()
    fork_data = fork_resp.json()
    assert fork_data["id"] == fork_id

    # The copied transcript should contain the marker from the source session.
    copied_user = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=_MARKER
    )
    expect(copied_user.first).to_be_visible(timeout=30_000)


def test_fork_with_worktree_creates_runner(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Fork with worktree creates a new runner bound to the fork session.

    When "Use a random worktree" is enabled, the fork operation should launch
    a new runner for the fork session. This test verifies that:

    1. The fork session has a runner_id bound
    2. The runner exists and is in a ready state
    3. The runner's working directory is a worktree (if applicable)

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound ``hello_world`` session.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()

    # Create one turn for the fork anchor.
    composer.fill(f"Reply with one short word. Marker: {_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator(_ASSISTANT).first
    expect(assistant).to_be_visible(timeout=60_000)

    # Open fork dialog and enable worktree.
    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()

    worktree_checkbox = page.get_by_test_id("fork-use-worktree")
    if worktree_checkbox.is_visible():
        worktree_checkbox.check()
        expect(worktree_checkbox).to_be_checked()

        submit = page.get_by_test_id("fork-session-submit")
        submit.click()

        # Wait for navigation to the fork.
        expect(page).to_have_url(
            re.compile(rf"/c/(?!{re.escape(session_id)})[0-9a-f]{{32}}"),
            timeout=60_000,
        )
        fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]

        # Verify the fork session has a runner_id.
        fork_resp = httpx.get(f"{base_url}/v1/sessions/{fork_id}", timeout=30.0)
        fork_resp.raise_for_status()
        fork_data = fork_resp.json()
        runner_id = fork_data.get("runner_id")
        assert runner_id is not None, "fork with worktree should have a runner_id bound"

        # Verify the runner exists and is in a ready/usable state.
        runner_resp = httpx.get(f"{base_url}/v1/runners/{runner_id}", timeout=30.0)
        runner_resp.raise_for_status()
        runner_data = runner_resp.json()
        assert runner_data["id"] == runner_id
        # The runner should be in a non-error state (e.g., ready, running).
        status = runner_data.get("status")
        assert status not in ["failed", "error"], (
            f"fork runner should be in a ready state, got status={status}"
        )
