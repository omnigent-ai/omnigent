"""Working indicator behavior when background shells outlive a turn.

A claude-native turn can settle to ``idle`` while background shells keep
running. The forwarder reports that as ``external_session_status: idle``
carrying a positive ``background_task_count``, and the web chat must keep
the working indicator lit — labelled ``"N background tasks still
running"`` — instead of falling idle like the TUI's "N shells still
running" banner.

Both tests drive the real status edges through the Sessions events route
(the same path the claude-native forwarder posts to), so they are
deterministic — no live LLM turn, whose timing and the openai-agents
executor's handling of a blocked mock would make the assertions flaky.
A new turn is represented by its ``running`` status edge (what a composer
send produces); the per-edge background-tally bookkeeping that send() does
locally is covered by the chatStore unit tests.
"""

from __future__ import annotations

import re

import httpx
from playwright.sync_api import Page, expect

_WORKING = '[data-testid="working-indicator"]'

# Rotating labels the working indicator cycles through — mirror of
# WORKING_MESSAGES in web/src/pages/ChatPage.tsx. The running-turn label is
# whichever entry the wall-clock bucket lands on, so the test accepts any of
# them. Keep this list in sync if that pool changes.
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
    background_task_count: int | None = None,
) -> None:
    """Publish a session status through the native-harness events route.

    :param base_url: Base URL of the local e2e server.
    :param session_id: Session/conversation id.
    :param status: Session status to publish, e.g. ``"idle"``.
    :param background_task_count: Background shells still running as of this
        status edge. ``None`` omits the field (no information — the sticky
        tally is left untouched); an explicit ``0`` is the authoritative
        Stop-hook clear that drops the indicator.
    :returns: None.
    """
    data: dict[str, object] = {"status": status}
    if background_task_count is not None:
        data["background_task_count"] = background_task_count
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_background_task_indicator_label_lifecycle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Indicator label tracks background tasks → working turn → cleared.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    working = page.locator(_WORKING)

    # 1. Background shells outlive the turn: idle + a positive count. The
    #    snapshot caches the count, so a fresh page load hydrates it.
    _publish_status(base_url, session_id, "idle", background_task_count=2)
    page.goto(f"{base_url}/c/{session_id}")
    expect(working).to_contain_text("2 background tasks still running", timeout=15_000)

    # 2. A new turn starts (the `running` edge a composer send produces): the
    #    fresh turn supersedes the tally, so the label flips from the
    #    background-task count to a rotating working label (e.g. "Working…",
    #    "Cooking…"). Accept any label in the pool — which one shows depends on
    #    the wall-clock bucket the turn lands on.
    _publish_status(base_url, session_id, "running")
    expect(working).to_contain_text(_WORKING_LABEL_RE, timeout=15_000)
    expect(working).not_to_contain_text("background task", timeout=15_000)

    # 3. The turn ends with the background shell finished: an authoritative
    #    Stop-hook `0` clears the tally, so the indicator goes out.
    _publish_status(base_url, session_id, "idle", background_task_count=0)
    expect(working).to_have_count(0, timeout=15_000)


def test_sidebar_spinner_tracks_background_tasks(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The sidebar row's running spinner tracks background shells too.

    A claude-native turn settles to ``idle`` while shells keep running; the
    sidebar row must show the grey running spinner (``SessionStateBadge``
    ``data-state="running"``), matching the in-chat indicator — not fall idle.
    When the last shell finishes, the ``Stop`` hook's authoritative ``0``
    clears the tally and both the spinner and the chat indicator go out.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    working = page.locator(_WORKING)
    # The badge sits in the row's time-marker slot (a sibling of the row link),
    # and `seeded_session` holds exactly one session — so the lone running badge
    # is this session's. Idle rows render no badge at all.
    running_badge = page.locator('[data-testid="session-state-badge"][data-state="running"]')

    # 1. Background shells outlive the turn → both the chat indicator and the
    #    sidebar row's running spinner light up.
    _publish_status(base_url, session_id, "idle", background_task_count=1)
    page.goto(f"{base_url}/c/{session_id}")
    expect(working).to_contain_text("1 background task still running", timeout=15_000)
    expect(running_badge).to_have_count(1, timeout=15_000)

    # 2. The last shell finishes: the Stop hook reports an authoritative `0`,
    #    which clears the tally — both the chat indicator and the sidebar
    #    spinner go out (idle rows render no badge).
    _publish_status(base_url, session_id, "idle", background_task_count=0)
    expect(working).to_have_count(0, timeout=15_000)
    expect(running_badge).to_have_count(0, timeout=15_000)
