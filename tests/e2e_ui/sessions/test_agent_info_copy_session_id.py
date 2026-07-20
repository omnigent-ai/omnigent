"""Browser e2e for copying a session id from the header info popover.

The top-right agent-info popover is the session metadata surface: it carries
session cost, token usage, and session policies. The durable ``conv_...`` id
belongs there too so users can recover a killed native terminal with a CLI
resume command.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect


# The header only mounts once the session binds and hydrates, so the
# info trigger can lag behind ``goto`` — clicking it while the header is
# still settling occasionally times out (or opens a popover that re-renders
# out from under the copy button). Rerun on failure rather than paper over
# the race with longer per-action waits.
@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_agent_info_copies_session_id(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The header info popover copies the durable session id.

    Failure modes this catches:

    - The session id is only exposed in sidebar row actions, not in the
      top-right info block requested by reviewers.
    - The copy handler writes a title / URL instead of the exact ``conv_...``.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-info-copy-id-{uuid.uuid4().hex[:8]}"
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()

    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)
    page.goto(f"{base_url}/c/{session_id}")

    # Wait for the header info trigger to mount + become actionable before
    # clicking: the button only renders once the session binds, and clicking
    # mid-hydration is the main flake source here.
    trigger = page.get_by_test_id("agent-info-trigger")
    expect(trigger).to_be_visible(timeout=30_000)
    trigger.click()

    expect(page.get_by_test_id("agent-info-session-id")).to_have_text(session_id)
    copy_button = page.get_by_test_id("agent-info-copy-session-id")
    expect(copy_button).to_be_visible()
    copy_button.click(force=True)
    expect(page.get_by_role("button", name="Copied session ID")).to_be_visible()

    copied = page.evaluate("navigator.clipboard.readText()")
    assert copied == session_id
