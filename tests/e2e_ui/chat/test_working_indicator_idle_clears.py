"""The main chat Working indicator clears when the server reports idle.

Regression: the indicator reads only ``sessionStatus``, but the
``session.status`` handler used to drop a bare ``idle`` (no ``response_id``)
whenever an ``activeResponse`` was still ``streaming`` — deferring to
``response_end`` to own that lifecycle. ``response_end`` only settles the
local ``status``/``activeResponse``, never ``sessionStatus``, so a dropped
idle stranded the one field the shimmer reads: the server, sidebar, and
local status all reported idle while "Working…" stayed lit.

This drives the exact edge shape the claude-native PTY-activity watcher
emits on a plain turn — a turn-start ``running`` carrying a ``response_id``
(which opens the streaming ``activeResponse``), then a trailing bare
``idle`` with no ``response_id`` once the pane quiesces. The fix makes the
client trust ``session.status`` 1:1, so that bare idle clears Working.

Both edges go through the Sessions events route (the same path the
claude-native forwarder posts to), so the test is deterministic — no live
LLM turn whose timing would make it flaky.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

_WORKING = '[data-testid="working-indicator"]'


def _publish_status(
    base_url: str, session_id: str, status: str, response_id: str | None = None
) -> None:
    """Publish a session status through the native-harness events route.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param status: Session status to publish, e.g. ``"running"``.
    :param response_id: Optional in-flight turn id. Set on the turn-start
        ``running`` edge (opening the streaming ``activeResponse``) and
        omitted on the trailing PTY-activity ``idle`` — reproducing the
        bare, id-less idle the old guard dropped.
    :returns: None.
    """
    data: dict[str, str] = {"status": status}
    if response_id is not None:
        data["response_id"] = response_id
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_bare_idle_clears_working_indicator(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A trailing bare ``idle`` clears Working even while a turn was streaming.

    1. A turn-start ``running`` edge carrying a ``response_id`` opens the
       streaming ``activeResponse`` and lights the indicator.
    2. A trailing ``idle`` with no ``response_id`` (the PTY-activity
       watcher's quiescence edge) arrives while that response is still
       locally streaming — the exact case the dropped-idle guard covered.
       The indicator must go out; before the fix it stayed lit forever.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    working = page.locator(_WORKING)
    response_id = "resp_idle_clears_1"

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=20_000)

    # Turn starts: the id-bearing running edge opens a streaming activeResponse.
    _publish_status(base_url, session_id, "running", response_id=response_id)
    expect(working).to_be_visible(timeout=15_000)

    # Turn ends: a bare idle (no response_id) — the trailing PTY-activity
    # edge. The server says idle, so Working must clear.
    _publish_status(base_url, session_id, "idle")
    expect(working).to_have_count(0, timeout=15_000)
