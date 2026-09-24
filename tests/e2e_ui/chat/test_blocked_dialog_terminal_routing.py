"""E2E: chat mode must route the user to a terminal dialog it is blocked on.

On a claude-native session, sending ``/model`` (or another slash command that
opens an interactive prompt) from the **chat** composer opens a dialog inside
the harness TUI. The chat transcript parks on a muted blocked working
indicator, but the dialog itself lives only in the **terminal view** — so the
chat surface must route the user to where the input is needed, or the session
just reads as hung.

Journey modeled:

1. Start a native-terminal session, stay in **chat** view.
2. A slash command opens an interactive dialog in the terminal TUI.
3. Chat parks on a muted blocked-on-a-dialog working indicator.
4. The dialog waits for input in the terminal view only.

The regression contract: while the session is blocked on a terminal dialog,
the chat surface must route the user to where the input is needed — either
the blocked indicator itself mentions the terminal (e.g. "open the terminal
to respond"), or the dialog is surfaced as an inline chat elicitation.

Drives the ``blocked_on: "dialog open"`` status edge through the Sessions
events route — the same path a native status forwarder posts to (see
``test_working_indicator_blocked_reason.py``) — so the parked state is
deterministic; a live TUI turn's timing would make the assertion flaky. The
edge's production is covered separately: Claude's session status file
(``waitingFor: "dialog open"``) → runner status watcher → SSE is exercised by
the status-file and resource-registry unit suites.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

# The chat transcript's working/blocked shimmer bubble.
_WORKING = '[data-testid="working-indicator"]'
# An inline chat elicitation card — the alternative acceptable fix shape.
_BOTTOM_ELICITATION = '[data-testid="bottom-elicitation"]'

# The blocked indicator must name the dialog, whatever the label's phrasing
# (a bare "Blocked on: dialog open", or a terminal-routing hint). The rotating
# working messages never mention a dialog, so matching this proves the chat is
# parked on the dialog-open block reason without pinning one label shape.
_BLOCKED_ON_DIALOG = re.compile(r"dialog", re.IGNORECASE)


def _publish_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    blocked_on: str | None = None,
) -> None:
    """Publish a session status through the native-harness events route.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param status: Session status to publish, e.g. ``"running"``.
    :param blocked_on: Why the session is parked, e.g. ``"dialog open"``.
        ``None`` omits the field — the session is not parked.
    :returns: None.
    """
    data: dict[str, object] = {"status": status}
    if blocked_on is not None:
        data["blocked_on"] = blocked_on
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_chat_blocked_on_terminal_dialog_routes_user_to_terminal(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Chat parked on a TUI dialog must say the response belongs in the terminal.

    Reaches the parked-on-a-dialog state through the real status pipeline
    (events route → SSE → chat store), then asserts the routing contract:
    chat must tell the user the dialog is waiting in the terminal.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session

    # Steps 1–2 — the user is in chat view while a turn is in flight (the
    # slash command was delivered to the TUI, whose dialog is about to own
    # the input).
    _publish_status(base_url, session_id, "running")
    page.goto(f"{base_url}/c/{session_id}")
    working = page.locator(_WORKING)
    expect(working).to_be_visible(timeout=15_000)

    # Step 3 — the TUI dialog opens and owns the input: the status edge
    # parks the session on "dialog open" and chat shows the muted blocked
    # indicator naming the dialog.
    _publish_status(base_url, session_id, "running", blocked_on="dialog open")
    expect(working).to_contain_text(_BLOCKED_ON_DIALOG, timeout=15_000)

    # Step 4 — the contract. While blocked on a dialog that lives only in
    # the terminal view, the chat surface must route the user there: either
    # the blocked indicator itself names the terminal (e.g. "open the
    # terminal to respond"), or the dialog is surfaced as an inline chat
    # elicitation.
    indicator_text = working.inner_text()
    mentions_terminal = re.search(r"terminal", indicator_text, re.IGNORECASE) is not None
    inline_elicitation = page.locator(_BOTTOM_ELICITATION).count() > 0
    assert mentions_terminal or inline_elicitation, (
        "Chat is parked on a terminal dialog but gives the user no signal that "
        "the response is needed in the terminal view: the blocked indicator "
        f"says only {indicator_text!r} (no mention of the terminal) and no "
        "inline elicitation card is shown. A user staying in chat mode has no "
        "way to know the session is waiting for them and will conclude it hung."
    )
