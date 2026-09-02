"""Working indicator behavior when the agent is parked on a dialog.

A native-terminal agent can block on a prompt that lives only in its TUI —
a permission request, a sandbox request, an open dialog — which the web
chat does not mirror. The session is still running, so the indicator stays
lit, but a bare rotating "Working…" hides the fact that nothing will move
until someone answers. The status edge carries a ``blocked_on`` reason and
the indicator names it instead.

Drives the real status edges through the Sessions events route (the same
path a native forwarder posts to), so the test is deterministic — no live
LLM turn, whose timing would make the assertions flaky.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

_WORKING = '[data-testid="working-indicator"]'

# Rotating labels the working indicator cycles through — mirror of
# WORKING_MESSAGES in web/src/pages/ChatPage.tsx. Which one shows depends on
# the wall-clock bucket the turn lands on, so the test accepts any of them.
# Keep this list in sync if that pool changes.
_WORKING_LABELS = (
    "Working…",
    "Cooking…",
    "Crunching…",
    "Tinkering…",
    "Pondering…",
    "Brewing…",
)
_WORKING_LABEL_RE = re.compile("|".join(re.escape(label) for label in _WORKING_LABELS))


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
    :param blocked_on: Why the session is parked, e.g. ``"permission
        prompt"``. ``None`` omits the field — the session is not parked.
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


def test_working_indicator_names_what_the_agent_is_parked_on(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The indicator reports the parked reason, then returns to working.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    working = page.locator(_WORKING)

    # 1. A turn is in flight: the indicator is lit with an ordinary label.
    _publish_status(base_url, session_id, "running")
    page.goto(f"{base_url}/c/{session_id}")
    expect(working).to_contain_text(_WORKING_LABEL_RE, timeout=15_000)

    # 2. The agent parks on a prompt that lives only in its TUI. The session
    #    is still running, so the indicator stays lit — but it now says what
    #    it is waiting on rather than shimmering with no explanation.
    _publish_status(base_url, session_id, "running", blocked_on="permission prompt")
    expect(working).to_contain_text("Blocked on: permission prompt", timeout=15_000)

    # 3. The prompt is answered and work resumes: the reason is dropped (it is
    #    not sticky) and the ordinary rotating label comes back.
    _publish_status(base_url, session_id, "running")
    expect(working).to_contain_text(_WORKING_LABEL_RE, timeout=15_000)
    expect(working).not_to_contain_text("Blocked on:", timeout=15_000)

    # 4. The turn ends: the indicator goes out regardless.
    _publish_status(base_url, session_id, "idle")
    expect(working).to_have_count(0, timeout=15_000)
