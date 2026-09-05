"""E2E: codex's ``$skill`` spelling in the in-session composer.

Codex splits the two namespaces in its own composer — ``/`` for built-in
commands, ``$`` for skills — so someone driving a codex session from the
Web UI reaches for ``$deslop`` and, before this change, got nothing: the
suggestions menu only opened on ``/``. These tests drive that journey in a
real browser: type ``$`` in a codex session's composer, see the session's
skills (and only the skills — codex has no ``$`` spelling for the
built-ins), and complete one with Tab.

The sigil is harness-gated, so the second test is the control: the same
draft on a non-codex session leaves ``$`` as prose, with no menu.

Both patch the browser-visible session snapshot's ``harness`` and
``skills`` (the same route-patch convention as
``test_custom_codex_native_controls.py``): skills reach the snapshot from
the server's background *runner* fetch, so a live-discovered list would
make the menu's contents depend on whatever the host happens to carry.
Nothing on the server side is under test here — the sigil lives entirely
in ``ChatPage``/``SlashCommandMenu``.

Selectors mirror the component: rows are
``data-testid="slash-menu-item-<name-sans-sigil>"`` and the highlighted
row carries ``data-active="true"`` (see ``SlashCommandMenu.tsx``).
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

# Two skills, so the menu is filtered rather than trivially single-rowed.
_SKILLS = [
    {"name": "deslop", "description": "Remove AI slop from the current changes"},
    {"name": "deep-research", "description": "Run a deep research sweep"},
]


def _patch_session_harness_and_skills(page: Page, session_id: str, harness: str) -> None:
    """Overlay ``harness`` + ``skills`` onto the session GET snapshot.

    :param page: Playwright page, before navigation.
    :param session_id: Session id whose GET snapshot to overlay.
    :param harness: Harness id to report, e.g. ``"codex-native"``.
    """

    def _handle(route: Route) -> None:
        request = route.request
        if urlparse(request.url).path != f"/v1/sessions/{session_id}" or request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["harness"] = harness
        payload["skills"] = _SKILLS
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def test_codex_session_completes_a_dollar_skill(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """``$`` opens the skills menu on a codex session, and Tab completes it.

    While the sigil is unsupported, typing ``$des`` opens nothing and the
    first ``expect`` below times out.

    :param page: Playwright page (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` from the fixture.
    """
    base_url, session_id = seeded_session
    _patch_session_harness_and_skills(page, session_id, "codex-native")
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    # A bare "$" lists the skills — and only the skills. The built-ins are
    # Omnigent's "/" commands; codex has no "$" spelling for them.
    composer.fill("$")
    expect(page.get_by_test_id("slash-menu-item-deslop")).to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("slash-menu-item-deep-research")).to_be_visible()
    expect(page.get_by_test_id("slash-menu-item-context")).to_have_count(0)

    # Narrowing highlights the single match (the highlight is driven by the
    # keyboard-nav filter, so this pins it to the rendered row), and Tab
    # completes it WITH its sigil plus a trailing space for arguments —
    # skills fill rather than execute.
    composer.fill("$des")
    expect(page.get_by_test_id("slash-menu-item-deslop")).to_have_attribute("data-active", "true")
    composer.press("Tab")
    expect(composer).to_have_value("$deslop ")


def test_non_codex_session_leaves_a_dollar_draft_as_prose(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The same draft on a non-codex session opens no menu.

    ``$`` is codex's spelling, so everywhere else it stays prose — a draft
    that starts with one must not sprout a suggestions menu.

    :param page: Playwright page (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` from the fixture.
    """
    base_url, session_id = seeded_session
    _patch_session_harness_and_skills(page, session_id, "claude-sdk")
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    # Same skill name, same session shape — only the harness differs.
    composer.fill("$des")
    expect(page.get_by_test_id("slash-menu-item-deslop")).to_have_count(0)
    # "/" still works here, which is what proves the skills reached the
    # snapshot and the empty "$" menu above is the harness gate, not a
    # missing skill list.
    composer.fill("/des")
    expect(page.get_by_test_id("slash-menu-item-deslop")).to_be_visible(timeout=15_000)
