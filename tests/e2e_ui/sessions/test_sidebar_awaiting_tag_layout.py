"""Browser e2e: a selected row's title must not slide under its approval tag.

The sidebar's "Needs response" tag is absolutely positioned, so the row's right
padding is the only thing holding the title clear of it. That reserve narrows
when the trailing pin/kebab are revealed — and it has to narrow on exactly the
states where the tag also fades out, or the title moves into space the tag still
occupies.

`focus-within` fires for a plain mouse click, which the tag's `:focus-visible`
fade does not react to. Clicking a row therefore narrowed the reserve while the
tag stayed fully opaque, and the title ran underneath it. Because the tag surface
is translucent (`bg-brand-accent/15`), the collision reads as a washed-out
opacity glitch rather than the layout problem it is.

jsdom reports every box as 0x0, so only a real browser can measure this. The
companion unit test (``Sidebar.test.tsx``) pins the class contract; this pins the
geometry that contract exists to protect.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

_UPDATES_WS_RE = re.compile(r"/v1/sessions/updates")

# Long enough that the title must truncate at the default font size — a short
# title clears the tag trivially and would pass without exercising anything.
LONG_TITLE = "Fix badge opacity and inbox color in the sidebar"


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _awaiting_tag(row: Locator) -> Locator:
    """Locate the row's "Needs response" tag (the awaiting state badge)."""
    return row.locator('[data-testid="session-state-badge"][data-state="awaiting"]')


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give the seeded session a deliberately long title."""
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    ).raise_for_status()


def _force_pending_approval(page: Page, session_id: str) -> None:
    """Make the session list report a pending approval for *session_id*.

    The row's awaiting state comes from ``pending_elicitations_count`` on the list
    payload (see ``useSessionState``). Patching the field in beats driving a real
    agent to an approval prompt: no LLM turn, and the row's state can't race the
    socket mid-measurement. Layout is the subject here, not the plumbing that
    sets the count.

    Also silences ``WS /v1/sessions/updates`` — the live stream would push the
    real (zero) count back over the injected one and unmount the tag.
    """
    page.route_web_socket(_UPDATES_WS_RE, lambda ws: None)

    def _handler(route: Route) -> None:
        request = route.request
        # Only the list endpoint — not /v1/sessions/{id} or its sub-resources.
        if request.method != "GET" or urlparse(request.url).path != "/v1/sessions":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        for conv in payload.get("data", []):
            if conv.get("id") == session_id:
                conv["pending_elicitations_count"] = 1
        route.fulfill(
            status=response.status,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions?*", _handler)


def _tag_overhang(row: Locator, session_id: str) -> float | None:
    """Return how far the painted title glyphs cross the tag's left edge.

    Positive = the title runs under the tag (the bug). Measures the *painted*
    glyph extent via a Range rather than the element box: the title clips with
    ``text-overflow: ellipsis``, so its box can end before the glyphs do, and
    comparing boxes alone would miss the visible overlap.
    """
    return row.evaluate(
        """(element, sessionId) => {
            const title = element.querySelector(`a[href="/c/${sessionId}"] .truncate`);
            const tag = element.querySelector('[data-testid="session-state-badge"]');
            if (!title || !tag) return null;
            const titleBox = title.getBoundingClientRect();
            const range = document.createRange();
            range.selectNodeContents(title);
            const painted = range.getBoundingClientRect().width;
            // A clipped title paints only as far as its box; an unclipped one
            // paints exactly its glyph width.
            const titleEnd = titleBox.left + Math.min(painted, titleBox.width);
            return +(titleEnd - tag.getBoundingClientRect().left).toFixed(1);
        }""",
        session_id,
    )


def test_selected_row_title_stays_clear_of_the_approval_tag(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Clicking an awaiting row must not slide its title under the visible tag.

    A plain click focuses the row (``:focus-within``) without matching
    ``:focus-visible``, so the trailing controls stay hidden and the tag stays
    opaque. The reserve must hold in that state.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound (idle) session.
    """
    base_url, session_id = seeded_session
    _set_title(base_url, session_id, LONG_TITLE)

    _force_pending_approval(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible(timeout=30_000)
    tag = _awaiting_tag(row)
    expect(tag).to_be_visible(timeout=30_000)

    resting = _tag_overhang(row, session_id)
    assert resting is not None, "row is missing its title or tag"

    # Click the row, then move the pointer away so only the focus state remains
    # — the selected row as the user leaves it.
    link = row.locator(f'a[href="/c/{session_id}"]')
    link.click(position={"x": 3, "y": 3})
    page.mouse.move(0, 0)
    page.wait_for_timeout(600)  # let any padding transition settle

    # The tag is still fully visible (no :focus-visible match), so the title has
    # to respect its space.
    assert tag.evaluate("e => getComputedStyle(e.parentElement).opacity") == "1", (
        "precondition failed: the tag faded on a plain click, so this no longer "
        "covers the selected-row case"
    )
    selected = _tag_overhang(row, session_id)
    assert selected is not None, "row lost its title or tag once selected"
    assert selected <= resting, (
        f"selecting the row pushed the title {selected}px past the tag's left edge "
        f"(was {resting}px when idle) — the reserve narrowed on a trigger the "
        "tag's fade does not share"
    )
