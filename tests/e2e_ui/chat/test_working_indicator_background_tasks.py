"""Working indicator behavior when background tasks run alongside a turn.

The "Working…" shimmer and the ``background-task-pill`` are independent
surfaces, and the background-shell tally is sticky across turn boundaries
(shells outlive the turn that launched them):

* Turn actively ``running`` with a lingering shell → BOTH show: the shimmer
  reports the live turn and the pill counts the shell.
* Turn genuinely ended (``idle``) with a shell still running → only the pill
  shows; a "Working…" shimmer there would misread as the agent still thinking.

(A claude-native turn-end ``waiting`` is normalized to ``idle`` server-side —
see ``_background_task_delivery_status`` — so the client's "working + shell"
state is the ``running`` case above, not ``waiting``.)

All tests drive the real status edges through the Sessions events route
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
_PILL = '[data-testid="background-task-pill"]'

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
    pill = page.locator(_PILL)

    # 1. Background shells outlive the turn: idle + a positive count. The turn
    #    has ended, so the pill (not the shimmer) names them. The snapshot
    #    caches the count, so a fresh page load hydrates it.
    _publish_status(base_url, session_id, "idle", background_task_count=2)
    page.goto(f"{base_url}/c/{session_id}")
    expect(pill).to_contain_text("2 background tasks", timeout=15_000)
    expect(working).to_have_count(0)

    # 2. A new turn starts (the `running` edge a composer send produces): the
    #    background shell outlives the turn boundary, so the pill STAYS lit and
    #    the rotating "Working…" shimmer lights up beside it — both surfaces at
    #    once. Accept any label in the pool — which one shows depends on the
    #    wall-clock bucket the turn lands on.
    _publish_status(base_url, session_id, "running")
    expect(working).to_contain_text(_WORKING_LABEL_RE, timeout=15_000)
    expect(pill).to_contain_text("2 background tasks")

    # 3. The turn ends with the background shell finished: an authoritative
    #    Stop-hook `0` clears the tally, so both surfaces go out.
    _publish_status(base_url, session_id, "idle", background_task_count=0)
    expect(working).to_have_count(0, timeout=15_000)
    expect(pill).to_have_count(0)


def test_working_shimmer_and_pill_coexist_while_running(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A ``running`` turn with a lingering shell shows the shimmer AND the pill.

    Background shells outlive turn boundaries, so when a new turn goes
    ``running`` the tally persists: the "Working…" shimmer lights for the live
    turn and the pill keeps counting the shell. The two are independent
    surfaces and must show side by side — the regression this guards is the
    pill blinking off the moment the working shimmer appears.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    working = page.locator(_WORKING)
    pill = page.locator(_PILL)

    # A prior turn ended with a background shell: seed the persisted snapshot
    # before navigation so page hydration cannot race the SSE subscription.
    _publish_status(base_url, session_id, "idle", background_task_count=2)
    page.goto(f"{base_url}/c/{session_id}")
    expect(pill).to_contain_text("2 background tasks", timeout=15_000)
    expect(working).to_have_count(0)

    # A new turn starts: the shell outlives the boundary, so both light up —
    # the shimmer for the live turn, the pill for the still-running shell.
    _publish_status(base_url, session_id, "running")
    expect(working).to_contain_text(_WORKING_LABEL_RE, timeout=15_000)
    expect(pill).to_contain_text("2 background tasks", timeout=15_000)


def test_sidebar_spinner_ignores_background_tasks(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The sidebar row stays idle while background shells outlive a turn.

    The turn is over — the session takes a new message immediately — so the
    row must NOT show the running spinner (``SessionStateBadge``
    ``data-state="running"``); a spinner there reads as "still working" and
    the tally that would drive it only refreshes on the next ``Stop`` hook,
    so it can outlive the shells. The in-chat indicator is the honest place
    for the shells, and it stays lit.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server
        fixture.
    :returns: None.
    """
    base_url, session_id = seeded_session
    pill = page.locator(_PILL)
    working = page.locator(_WORKING)
    # The badge sits in the row's time-marker slot (a sibling of the row link),
    # and `seeded_session` holds exactly one session — so the lone running badge
    # is this session's. Idle rows render no badge at all.
    running_badge = page.locator('[data-testid="session-state-badge"][data-state="running"]')

    # 1. Background shells outlive the turn → the chat pill lights up, the
    #    sidebar row does not.
    _publish_status(base_url, session_id, "idle", background_task_count=1)
    page.goto(f"{base_url}/c/{session_id}")
    expect(pill).to_contain_text("1 background task", timeout=15_000)
    expect(running_badge).to_have_count(0)

    # 2. A real turn still lights the row, so the assertion above is about
    #    background work and not a badge that never renders.
    _publish_status(base_url, session_id, "running")
    expect(running_badge).to_have_count(1, timeout=15_000)

    # 3. The turn ends with the last shell finished: the Stop hook's
    #    authoritative `0` clears the tally, so both go out.
    _publish_status(base_url, session_id, "idle", background_task_count=0)
    expect(working).to_have_count(0, timeout=15_000)
    expect(running_badge).to_have_count(0, timeout=15_000)
